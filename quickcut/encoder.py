from __future__ import annotations

import io
import math
import os
import re
import struct
import zlib
from typing import Iterable


TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)

# The fallback is intentionally tiny and deterministic. It gives a useful local
# smoke-test path without downloading weights, while OpenCLIP can be selected for
# semantic retrieval in a production run.
COLOR_WORDS = {
    "sky": (0.35, 0.55, 0.95), "blue": (0.25, 0.45, 0.95), "water": (0.15, 0.45, 0.8),
    "ocean": (0.1, 0.4, 0.75), "sea": (0.1, 0.4, 0.75), "green": (0.25, 0.7, 0.2),
    "forest": (0.1, 0.45, 0.15), "tree": (0.2, 0.55, 0.15), "grass": (0.35, 0.7, 0.15),
    "sunset": (0.9, 0.3, 0.12), "sunrise": (0.95, 0.45, 0.12), "warm": (0.85, 0.35, 0.12),
    "red": (0.85, 0.08, 0.06), "orange": (0.9, 0.3, 0.06), "yellow": (0.9, 0.75, 0.08),
    "night": (0.04, 0.06, 0.16), "dark": (0.04, 0.04, 0.05), "bright": (0.9, 0.9, 0.8),
    "people": (0.55, 0.35, 0.25), "person": (0.55, 0.35, 0.25), "group": (0.55, 0.35, 0.25),
    "scenic": (0.35, 0.6, 0.45), "mountain": (0.3, 0.35, 0.4), "city": (0.45, 0.45, 0.5),
    "travel": (0.4, 0.5, 0.45), "trip": (0.4, 0.5, 0.45), "love": (0.8, 0.2, 0.28),
}


def _norm(values: Iterable[float]) -> list[float]:
    values = list(values)
    length = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / length for v in values]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n)) / ((sum(x * x for x in a) ** 0.5 or 1.0) * (sum(x * x for x in b) ** 0.5 or 1.0))


def parse_ppm(data: bytes) -> tuple[int, int, bytes]:
    """Parse an ffmpeg P6/P3 frame without requiring Pillow or OpenCV."""
    if not data.startswith((b"P6", b"P3")):
        raise ValueError("Expected a PPM keyframe from ffmpeg")
    tokens: list[bytes] = []
    pos = 0
    while len(tokens) < 4:
        while pos < len(data) and data[pos] in b" \t\r\n":
            pos += 1
        if pos < len(data) and data[pos:pos + 1] == b"#":
            pos = data.find(b"\n", pos) + 1
            continue
        end = pos
        while end < len(data) and data[end] not in b" \t\r\n":
            end += 1
        tokens.append(data[pos:end])
        pos = end
    width, height, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
    if maxval > 255:
        raise ValueError("Unsupported 16-bit PPM")
    if data[:2] == b"P6":
        # P6 has one separator after maxval; subsequent bytes are pixels.
        # A dark first pixel can itself be 0x09/0x0a/0x20. Skipping arbitrary
        # whitespace here used to corrupt valid frames and create empty indexes.
        if pos >= len(data) or data[pos] not in b" \t\r\n":
            raise ValueError("Missing PPM raster separator")
        pos += 1
        pixels = data[pos:]
        if len(pixels) != width * height * 3:
            raise ValueError("Invalid PPM raster length")
        return width, height, pixels
    values = bytes(int(x) for x in data[pos:].split())
    return width, height, values


def ppm_to_png(data: bytes) -> bytes:
    """Convert an ffmpeg PPM frame to PNG using only the standard library."""
    width, height, pixels = parse_ppm(data)
    rows = b"".join(b"\x00" + pixels[row * width * 3:(row + 1) * width * 3] for row in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")


class FallbackEncoder:
    name = "fallback-color-histogram"

    def encode_image(self, ppm: bytes) -> list[float]:
        width, height, pixels = parse_ppm(ppm)
        if len(pixels) < width * height * 3:
            raise ValueError("Truncated PPM keyframe")
        bins = [0.0] * 64
        brightness = [0.0] * 8
        total = width * height
        for i in range(0, total * 3, 3):
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            idx = (r // 64) * 16 + (g // 64) * 4 + (b // 64)
            bins[idx] += 1.0
            brightness[(r + g + b) // 96] += 1.0
        return _norm([x / total for x in bins + brightness])

    def encode_text(self, text: str) -> list[float]:
        tokens = [t.lower() for t in TOKEN_RE.findall(text)]
        color = [0.0, 0.0, 0.0]
        for token in tokens:
            vector = COLOR_WORDS.get(token)
            if vector:
                color = [color[i] + vector[i] for i in range(3)]
        return _norm(color + [float(len(tokens)), float(sum(len(t) for t in tokens))])

    def score_text_image(self, text: str, image_embedding: list[float]) -> float:
        # Map the color prototype to the histogram's average color channels.
        if len(image_embedding) < 64:
            return 0.0
        bins = image_embedding[:64]
        channel = [0.0, 0.0, 0.0]
        total = sum(bins) or 1.0
        for idx, value in enumerate(bins):
            channel[0] += ((idx // 16) + 0.5) / 4 * value
            channel[1] += (((idx % 16) // 4) + 0.5) / 4 * value
            channel[2] += ((idx % 4) + 0.5) / 4 * value
        channel = _norm([x / total for x in channel])
        query = self.encode_text(text)[:3]
        return max(0.0, cosine(channel, query)) if any(query) else 0.15


class OpenClipEncoder:
    name = "openclip"

    def __init__(self) -> None:
        try:
            import open_clip  # type: ignore
            import torch  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as exc:
            raise RuntimeError("OpenCLIP backend requires quickcut[ml]") from exc
        # MobileCLIP-S2 is the smallest model name documented by OpenCLIP's
        # DataCompDR registry; override it for a local checkpoint if needed.
        model_name = os.getenv("QUICKCUT_CLIP_MODEL", "MobileCLIP-S2")
        pretrained = os.getenv("QUICKCUT_CLIP_PRETRAINED", "datacompdr")
        self._torch = torch
        self._Image = Image
        self._model, _, self._preprocess = open_clip.create_model_and_transforms(model_name, pretrained=pretrained)
        self._tokenizer = open_clip.get_tokenizer(model_name)
        self._model.eval()
        self._text_cache: dict[str, list[float]] = {}

    def _image(self, ppm: bytes):
        return self._Image.open(io.BytesIO(ppm)).convert("RGB")

    def encode_image(self, ppm: bytes) -> list[float]:
        with self._torch.inference_mode():
            out = self._model.encode_image(self._preprocess(self._image(ppm)).unsqueeze(0))
            out /= out.norm(dim=-1, keepdim=True)
        return out[0].cpu().tolist()

    def encode_text(self, text: str) -> list[float]:
        if text in self._text_cache:
            return self._text_cache[text]
        with self._torch.inference_mode():
            out = self._model.encode_text(self._tokenizer([text]))
            out /= out.norm(dim=-1, keepdim=True)
        embedding = out[0].cpu().tolist()
        self._text_cache[text] = embedding
        return embedding

    def score_text_image(self, text: str, image_embedding: list[float]) -> float:
        return cosine(self.encode_text(text), image_embedding)


_ENCODER_CACHE: dict[str, object] = {}


def make_encoder(mode: str | None = None):
    mode = (mode or os.getenv("QUICKCUT_ENCODER", "fallback")).lower()
    if mode in _ENCODER_CACHE:
        return _ENCODER_CACHE[mode]
    if mode in {"openclip", "mobileclip", "auto"}:
        try:
            encoder = OpenClipEncoder()
            _ENCODER_CACHE[mode] = encoder
            return encoder
        except Exception as exc:
            if mode != "auto":
                raise
            print(f"[quickcut] OpenCLIP unavailable, using fallback encoder: {exc}")
    encoder = FallbackEncoder()
    _ENCODER_CACHE[mode] = encoder
    return encoder

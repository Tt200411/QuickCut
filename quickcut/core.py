from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .asr import (
    assign_transcripts,
    load_transcript_sidecar,
    make_asr,
    save_transcript_sidecar,
)
from .encoder import make_encoder, ppm_to_png
from .types import IndexManifest, Shot, Timeline, TimelineItem


def _run(command: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def _ffmpeg_binary() -> str:
    configured = os.getenv("QUICKCUT_FFMPEG")
    if configured and Path(configured).exists():
        return configured
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("FFmpeg is required. Install it on PATH or run: pip install -e '.[runtime]'") from exc


def _require_binary(name: str) -> str:
    if name == "ffmpeg":
        return _ffmpeg_binary()
    path = shutil.which(name)
    if path:
        return path
    raise RuntimeError(f"{name} is required. Install FFmpeg and ensure it is on PATH.")


def probe_video(video_path: str | Path) -> dict[str, Any]:
    probe = shutil.which("ffprobe")
    if probe:
        result = _run([
            probe, "-v", "error", "-show_entries", "format=duration,size", "-of", "json", str(video_path)
        ])
        data = json.loads(result.stdout.decode("utf-8"))
        fmt = data.get("format", {})
        return {"duration": float(fmt.get("duration", 0.0)), "size": int(fmt.get("size", 0) or 0)}
    # imageio-ffmpeg bundles ffmpeg but not ffprobe. Parse its stable stderr
    # duration line so the demo still works on a clean macOS environment.
    result = subprocess.run([_ffmpeg_binary(), "-hide_banner", "-i", str(video_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not match:
        raise RuntimeError("Could not read video duration; install ffprobe or provide a valid media file")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return {"duration": duration, "size": video_path.stat().st_size}


def extract_keyframe(video_path: str | Path, timestamp: float) -> bytes:
    ffmpeg = _require_binary("ffmpeg")
    result = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}", "-i", str(video_path),
        "-frames:v", "1", "-vf", "scale=256:-2", "-f", "image2pipe", "-vcodec", "ppm", "pipe:1"
    ])
    return result.stdout


def _index_id(
    video_path: Path,
    source_size: int,
    source_mtime: float,
    window: float,
    stride: float,
    encoder: str,
    transcript_backend: str = "none",
) -> str:
    key = f"{video_path.resolve()}|{source_size}|{source_mtime:.6f}|{window}|{stride}|{encoder}|{transcript_backend}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _cache_path(cache_dir: Path, index_id: str) -> Path:
    return cache_dir / f"{index_id}.json"


def save_manifest(manifest: IndexManifest, cache_dir: str | Path) -> Path:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, manifest.index_id)
    path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_manifest(path: str | Path) -> IndexManifest:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    shots = [Shot(**shot) for shot in data.pop("shots", [])]
    manifest = IndexManifest(shots=shots, **data)
    # Older manifests did not persist shot transcripts.  If a sidecar exists,
    # hydrate those fields at load time while keeping the manifest JSON stable.
    if manifest.transcript_file:
        sidecar = path.parent / manifest.transcript_file
    else:
        sidecar = path.with_name(f"{manifest.index_id}.transcript.json")
    if sidecar.exists() and any(not shot.transcript for shot in manifest.shots):
        try:
            _payload, segments = load_transcript_sidecar(sidecar)
            assign_transcripts(manifest.shots, segments)
        except (OSError, ValueError, TypeError):
            # A damaged optional sidecar should not make a visual-only index
            # unusable.  Retrieval simply runs without transcript evidence.
            pass
    return manifest


def find_manifest(cache_dir: str | Path, index_id: str) -> IndexManifest:
    return load_manifest(_cache_path(Path(cache_dir), index_id))


def build_index(
    video_path: str | Path,
    cache_dir: str | Path = "cache",
    *,
    window: float = 3.0,
    stride: float | None = None,
    encoder_mode: str | None = None,
    asr_mode: str | None = None,
    force: bool = False,
) -> IndexManifest:
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    stride = stride or window
    metadata = probe_video(video_path)
    encoder = make_encoder(encoder_mode)
    asr = make_asr(asr_mode)
    index_id = _index_id(
        video_path, metadata["size"], video_path.stat().st_mtime, window, stride, encoder.name, asr.name
    )
    existing = _cache_path(Path(cache_dir), index_id)
    if existing.exists() and not force:
        return load_manifest(existing)
    duration = metadata["duration"]
    shots: list[Shot] = []
    # Store absolute keyframe paths in the manifest so a later API/UI process
    # can load cached images even when its working directory differs.
    keyframe_dir = Path(cache_dir).expanduser().resolve() / f"{index_id}.keyframes"
    keyframe_dir.mkdir(parents=True, exist_ok=True)
    start = 0.0
    while start < duration and len(shots) < 240:
        end = min(duration, start + window)
        midpoint = (start + end) / 2
        try:
            ppm = extract_keyframe(video_path, midpoint)
            embedding = encoder.encode_image(ppm)
            keyframe_path = keyframe_dir / f"shot-{len(shots):04d}.png"
            keyframe_path.write_bytes(ppm_to_png(ppm))
        except Exception as exc:
            if encoder.name != "fallback-color-histogram":
                raise RuntimeError(f"Unable to encode keyframe at {midpoint:.2f}s: {exc}") from exc
            embedding = []
            keyframe_path = None
        shots.append(Shot(shot_id=f"shot-{len(shots):04d}", start=round(start, 3), end=round(end, 3),
                          keyframe=str(keyframe_path) if keyframe_path else None,
                          embedding=embedding, backend=encoder.name))
        start += stride
    transcript_segments = []
    if asr.name != "none":
        try:
            transcript_segments = asr.transcribe(video_path)
        except Exception as exc:
            # ``auto`` is intentionally resilient to a missing model download,
            # unsupported audio codec, or unavailable local runtime.  Explicit
            # backends surface the failure so callers know indexing is partial.
            requested_asr = (asr_mode or os.getenv("QUICKCUT_ASR", "none")).lower()
            if requested_asr == "auto":
                print(f"[quickcut] ASR unavailable, using visual-only index: {exc}")
                asr = make_asr("none")
                index_id = _index_id(
                    video_path, metadata["size"], video_path.stat().st_mtime, window, stride, encoder.name, asr.name
                )
            else:
                raise RuntimeError(f"Unable to transcribe source video with {asr.name}: {exc}") from exc
    assign_transcripts(shots, transcript_segments)
    transcript_file = None
    if transcript_segments or asr.name != "none":
        transcript_file = f"{index_id}.transcript.json"
        save_transcript_sidecar(
            Path(cache_dir) / transcript_file,
            transcript_segments,
            index_id=index_id,
            source_video=str(video_path),
            source_size=metadata["size"],
            source_mtime=video_path.stat().st_mtime,
            backend=asr.name,
            model=getattr(asr, "model", None),
            language=getattr(asr, "language", None),
        )
    manifest = IndexManifest(
        index_id=index_id, source_video=str(video_path), source_size=metadata["size"],
        source_mtime=video_path.stat().st_mtime, duration=duration, window=window, stride=stride,
        created_at=datetime.now(timezone.utc).isoformat(), encoder=encoder.name, shots=shots,
        transcript_backend=asr.name, transcript_file=transcript_file,
    )
    save_manifest(manifest, cache_dir)
    return manifest


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w\u4e00-\u9fff]+", text, re.UNICODE) if len(token) > 1}


def _transcript_score(query: str, transcript: str) -> float:
    q, t = _tokens(query), _tokens(transcript)
    return len(q & t) / max(1, len(q))


def parse_prompt(prompt: str, duration: float | None = None) -> tuple[list[str], float]:
    matches = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b", prompt.lower())
    budget = float(matches[0]) if matches else (duration or 8.0)
    budget = min(15.0, max(5.0, budget))
    clean = re.sub(r"\b\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds)\b", "", prompt, flags=re.I)
    beats = [piece.strip() for piece in re.split(r"[,;]|\band\b|\u548c|\u4e0e", clean, flags=re.I) if piece.strip()]
    return beats or [clean.strip() or prompt.strip()], budget


def localize(manifest: IndexManifest, prompt: str, *, encoder_mode: str | None = None, top_k: int = 3, threshold: float = 0.22) -> Timeline:
    beats, budget = parse_prompt(prompt)
    encoder = make_encoder(encoder_mode or manifest.encoder)
    beat_budget = budget / max(1, min(len(beats), top_k))
    ranked: list[tuple[float, Shot, str]] = []
    for beat in beats:
        candidates: list[tuple[float, Shot]] = []
        for shot in manifest.shots:
            if shot.embedding:
                score = encoder.score_text_image(beat, shot.embedding)
            else:
                score = 0.0
            score += 0.35 * _transcript_score(beat, shot.transcript)
            candidates.append((score, shot))
        candidates.sort(key=lambda value: value[0], reverse=True)
        for score, shot in candidates[:top_k]:
            ranked.append((score, shot, beat))
    ranked.sort(key=lambda value: value[0], reverse=True)
    selected: list[tuple[float, Shot, str]] = []
    used: set[str] = set()
    target_capacity = budget
    candidate_limit = min(len(manifest.shots), max(top_k, math.ceil(budget / max(manifest.window, 0.1)) + 1))
    capacity = 0.0
    for score, shot, beat in ranked:
        if shot.shot_id in used:
            continue
        selected.append((score, shot, beat))
        used.add(shot.shot_id)
        capacity += min(shot.duration, beat_budget)
        if len(selected) >= candidate_limit or capacity >= target_capacity:
            break
    selected.sort(key=lambda value: value[1].start)
    if not selected or max(value[0] for value in selected) < threshold:
        return Timeline(version="1.0", source_video=manifest.source_video, query=prompt, duration_budget=budget,
                        index_id=manifest.index_id, coverage="abstain", message="No source segment cleared the confidence threshold; ask for a more concrete visual query or add licensed B-roll.")
    items: list[TimelineItem] = []
    remaining = budget
    for idx, (score, shot, beat) in enumerate(selected):
        length = min(shot.duration, beat_budget, remaining)
        if length <= 0.1:
            continue
        center = (shot.start + shot.end) / 2
        start = max(shot.start, center - length / 2)
        end = min(shot.end, start + length)
        items.append(TimelineItem(item_id=f"item-{idx:03d}", provenance="source", asset_id=shot.shot_id,
                                  start=round(start, 3), end=round(end, 3), score=round(score, 4), transcript=shot.transcript,
                                  license_note="Source video provided by user"))
        remaining -= end - start
    timeline = Timeline(version="1.0", source_video=manifest.source_video, query=prompt, duration_budget=budget,
                        index_id=manifest.index_id, coverage="source", items=items,
                        message=f"Retrieved {len(items)} source segment(s) with cached index {manifest.index_id}.")
    validate_timeline(timeline)
    return timeline


def validate_timeline(timeline: Timeline) -> None:
    if not 5.0 <= timeline.duration_budget <= 15.0:
        raise ValueError("duration_budget must be between 5 and 15 seconds")
    if timeline.coverage == "source" and not timeline.items:
        raise ValueError("source timeline must contain at least one item")
    if timeline.coverage == "source" and timeline.duration < 5.0:
        raise ValueError(f"source timeline duration {timeline.duration:.2f}s is shorter than the 5s minimum")
    if timeline.duration > timeline.duration_budget + 0.05:
        raise ValueError(f"timeline duration {timeline.duration:.2f}s exceeds budget {timeline.duration_budget:.2f}s")
    for item in timeline.items:
        if item.end <= item.start:
            raise ValueError(f"invalid interval for {item.item_id}")
        if item.provenance not in {"source", "external", "generated"}:
            raise ValueError(f"invalid provenance for {item.item_id}")


def update_timeline(timeline: Timeline, edits: list[dict[str, Any]]) -> Timeline:
    """Apply replace/trim/reorder edits to a timeline without touching its index."""
    timeline = deepcopy(timeline)
    by_id = {item.item_id: item for item in timeline.items}
    for edit in edits:
        item_id = edit.get("item_id")
        if item_id not in by_id:
            raise KeyError(f"Unknown timeline item: {item_id}")
        item = by_id[item_id]
        if "start" in edit:
            item.start = float(edit["start"])
        if "end" in edit:
            item.end = float(edit["end"])
        if "asset_id" in edit:
            item.asset_id = str(edit["asset_id"])
        if "provenance" in edit:
            item.provenance = edit["provenance"]
        if "license_note" in edit:
            item.license_note = str(edit["license_note"])
    order = [str(edit["item_id"]) for edit in edits if "order" in edit]
    if order:
        timeline.items.sort(key=lambda item: order.index(item.item_id) if item.item_id in order else len(order))
    validate_timeline(timeline)
    return timeline


def render_timeline(timeline: Timeline, output_path: str | Path, *, preset: str | None = None) -> Path:
    validate_timeline(timeline)
    if timeline.coverage != "source" or not timeline.items:
        raise ValueError("Only source-backed timelines can be rendered by this MVP")
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _require_binary("ffmpeg")
    source = Path(timeline.source_video)
    preset = preset or timeline.preset
    with tempfile.TemporaryDirectory(prefix="quickcut-render-") as temp:
        temp_dir = Path(temp)
        clips: list[Path] = []
        for idx, item in enumerate(timeline.items):
            clip = temp_dir / f"clip-{idx:03d}.mp4"
            filters = []
            if preset == "warm":
                filters.append("eq=saturation=1.08:contrast=1.03")
            vf = ",".join(filters) if filters else "null"
            _run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{item.start:.3f}", "-t", f"{item.duration:.3f}",
                "-i", str(source), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-an", "-movflags", "+faststart", str(clip), "-y"
            ])
            clips.append(clip)
        concat = temp_dir / "concat.txt"
        concat.write_text("\n".join(f"file '{clip.as_posix()}'" for clip in clips), encoding="utf-8")
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(output_path), "-y"])
    return output_path

"""Media probing, frame extraction and source/proxy timestamp mapping."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path, PurePosixPath

from .core import _ffmpeg_binary


class MediaError(RuntimeError):
    pass


def run_media(args: list[str]) -> bytes:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise MediaError(result.stderr.decode("utf-8", errors="replace")[-4000:])
    return result.stdout


def ffprobe() -> str:
    configured = os.getenv("QUICKCUT_FFPROBE")
    binary = configured or shutil.which("ffprobe")
    if not binary:
        raise MediaError("FFprobe is required for ingestion; set QUICKCUT_FFPROBE or add it to PATH")
    return str(binary)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return "sha256:" + result.hexdigest()


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def us(value) -> int:
    return round(Fraction(str(value)) * 1_000_000)


def rational(value: str) -> dict:
    rate = Fraction(value)
    if rate <= 0:
        raise MediaError(f"Invalid rate: {value}")
    return {"numerator": rate.numerator, "denominator": rate.denominator}


def resolve_uri(root: Path, uri: str) -> Path:
    p = PurePosixPath(uri)
    if not uri or p.is_absolute() or ".." in p.parts or "\\" in uri or ":" in uri:
        raise ValueError(f"Unsafe project URI: {uri}")
    target = (root / uri).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"URI escapes project root: {uri}")
    return target


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def probe_media(path: Path) -> dict:
    data = json.loads(run_media([ffprobe(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))
    videos = [s for s in data["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise MediaError("No playable video stream")
    video = videos[0]
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    duration = video.get("duration")
    if duration in (None, "N/A"):
        duration = data.get("format", {}).get("duration")
    if duration in (None, "N/A") or us(duration) <= 0:
        raise MediaError("Missing or invalid video duration")
    fps = video.get("avg_frame_rate", "0/0")
    if fps in {"0/0", "0/1"}:
        fps = video.get("r_frame_rate", "0/0")
    return {
        "duration_us": us(duration),
        "video": {"width": video["width"], "height": video["height"], "fps": rational(fps),
                  "time_base": video["time_base"], "start_time_us": us(video.get("start_time", "0")),
                  "codec": video["codec_name"], "stream_index": video["index"]},
        "audio": ({"present": True, "sample_rate": int(audio["sample_rate"]),
                   "channels": audio["channels"], "codec": audio["codec_name"],
                   "start_time_us": us(audio.get("start_time", "0"))} if audio else {"present": False}),
    }


def frame_timestamps(path: Path, metadata: dict) -> list[dict]:
    data = json.loads(run_media([
        ffprobe(), "-v", "error", "-select_streams", str(metadata["video"]["stream_index"]),
        "-show_frames", "-show_entries", "frame=best_effort_timestamp,duration_time,pkt_duration_time",
        "-of", "json", str(path),
    ]))
    frames = data.get("frames", [])
    if not frames or any("best_effort_timestamp" not in f for f in frames):
        raise MediaError("Decoded video has missing presentation timestamps")
    timebase = Fraction(metadata["video"]["time_base"])
    origin = int(frames[0]["best_effort_timestamp"])
    result = []
    for frame in frames:
        pts = int(frame["best_effort_timestamp"])
        timestamp = round((pts - origin) * timebase * 1_000_000)
        if result and timestamp <= result[-1]["source_time_us"]:
            raise MediaError("Non-increasing frame presentation timestamps")
        result.append({"source_pts": pts, "source_time_us": timestamp})
    metadata["video"]["source_origin_pts"] = origin
    metadata["video"]["source_origin_us"] = round(origin * timebase * 1_000_000)
    steps = {b["source_time_us"] - a["source_time_us"] for a, b in zip(result, result[1:])}
    metadata["video"]["variable_frame_rate"] = len(steps) > 1 and max(steps) - min(steps) > 2
    tail_duration = frames[-1].get("duration_time", frames[-1].get("pkt_duration_time"))
    tail = us(tail_duration) if tail_duration else (
        result[-1]["source_time_us"] - result[-2]["source_time_us"] if len(result) > 1 else metadata["duration_us"])
    metadata["duration_us"] = result[-1]["source_time_us"] + max(1, tail)
    return result


def valid_png(path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as img:
            if img.format != "PNG":
                return False
            img.load()
        return True
    except (OSError, ValueError):
        return False


def extract_frames(path: Path, frames: list[dict], segments: list[dict], directory: Path, stream_index: int = 0) -> None:
    # Select decoded frame indices so the declared source PTS is the actual evidence.
    from bisect import bisect_left
    timestamps = [f["source_time_us"] for f in frames]
    selected = []
    for segment in segments:
        lo, hi = segment["source_range_us"]["begin"], segment["source_range_us"]["end"]
        middle = (lo + hi) // 2
        index = min(bisect_left(timestamps, middle), len(frames) - 1)
        if timestamps[index] >= hi:
            index = max(0, index - 1)
        if timestamps[index] < lo:
            segment["keyframes"] = []
            segment["quality"] = {"usable": False, "flags": ["NO_FRAME_IN_WINDOW"]}
            continue
        selected.append((segment, index))
    indices = sorted({index for _, index in selected})
    directory.mkdir(parents=True, exist_ok=True)
    # Batch each command to avoid OS command-line limits on long videos.
    for offset in range(0, len(indices), 128):
        batch = indices[offset:offset + 128]
        expression = "+".join(f"eq(n\\,{index})" for index in batch)
        run_media([_ffmpeg_binary(), "-v", "error", "-y", "-noautorotate", "-i", str(path),
                   "-map", f"0:{stream_index}", "-vf", f"select={expression},scale=256:-2", "-fps_mode", "vfr",
                   "-start_number", str(offset), str(directory / "%08d.png")])
    positions = {index: pos for pos, index in enumerate(indices)}
    for segment, index in selected:
        image = directory / f"{positions[index]:08d}.png"
        if not valid_png(image):
            raise MediaError(f"Invalid extracted PNG for {segment['segment_id']}")
        segment["keyframes"] = [{"source_time_us": timestamps[index], "_path": str(image)}]


def create_proxy(source: Path, target: Path, frames: list[dict], width: int, stream_index: int = 0) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    # Preserve display PTS and frame count, including VFR. Audio stays in the original asset.
    run_media([_ffmpeg_binary(), "-v", "error", "-y", "-noautorotate", "-i", str(source),
               "-map", f"0:{stream_index}", "-an", "-vf", f"scale='min({width},iw)':-2,setpts=PTS-STARTPTS",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-bf", "0",
               "-fps_mode", "passthrough", "-enc_time_base", "1:1000000",
               "-video_track_timescale", "1000000", str(target)])
    metadata = probe_media(target)
    proxy_frames = frame_timestamps(target, metadata)
    if len(frames) != len(proxy_frames):
        raise MediaError("Proxy frame count differs from source")
    mapping = []
    for original, proxy in zip(frames, proxy_frames):
        if abs(original["source_time_us"] - proxy["source_time_us"]) > 2:
            raise MediaError("Proxy presentation timestamps do not match the source")
        mapping.append({**original, "proxy_pts": proxy["source_pts"], "proxy_time_us": proxy["source_time_us"]})
    return {"method": "decoded_frame_pts", "tolerance_us": 2, "audio": "original_asset_only",
            "proxy_time_base": metadata["video"]["time_base"], "frames": mapping}

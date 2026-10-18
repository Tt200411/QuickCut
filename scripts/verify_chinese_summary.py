"""Verify a rendered local summary and optionally measure retained source audio.

Usage::

    python scripts/verify_chinese_summary.py outputs/final_chinese_summary.mp4 \
        --original-only outputs/original_only.mp4 \
        --narration outputs/narration.wav --report outputs/verification.json

The optional audio regression requires NumPy. It estimates source/narration
gains from decoded samples; an audio stream's mere presence is not evidence
that the original soundtrack survived. The default narration filter matches
QuickCut's local mixer; use --narration-filter '' for an already normalized WAV.
Whisper language/content verification and visual review remain separate checks.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quickcut.core import _require_binary


def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def ffprobe_json(ffprobe: str, video: Path, *arguments: str) -> dict[str, Any]:
    return json.loads(run([ffprobe, "-v", "error", *arguments, "-of", "json", str(video)]).stdout)


def timestamp(value: str) -> float:
    hour, minute, second, millisecond = map(int, re.split(r"[:,]", value))
    if minute > 59 or second > 59:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    return hour * 3600 + minute * 60 + second + millisecond / 1000


def inspect_subtitles(path: Path, duration: float) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip())
    cues = []
    previous_end = 0.0
    for number, block in enumerate(blocks, 1):
        lines = block.splitlines()
        match = re.fullmatch(r"(\d{2,}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2,}:\d{2}:\d{2},\d{3})", lines[1]) if len(lines) >= 3 else None
        if not match or lines[0].strip() != str(number):
            errors.append(f"Malformed/nonsequential SRT cue {number}")
            continue
        start, end = map(timestamp, match.groups())
        text = "\n".join(lines[2:]).strip()
        if start < previous_end - 0.000001:
            errors.append(f"Subtitle cue {number} overlaps the preceding cue")
        if end <= start:
            errors.append(f"Subtitle cue {number} has zero or negative duration")
        if start < 0 or end > duration + 0.001:
            errors.append(f"Subtitle cue {number} exceeds the video duration")
        if not text:
            errors.append(f"Subtitle cue {number} has no text")
        if re.search(r"[A-Za-z]", text):
            errors.append(f"Subtitle cue {number} contains English letters")
        if cues and cues[-1]["text"] == text and start <= previous_end + 0.04:
            errors.append(f"Subtitle cue {number} repeats the same sentence across a cut")
        cues.append({"start": start, "end": end, "text": text})
        previous_end = end
    if not cues:
        errors.append("No valid subtitle cues")
    return {"path": str(path), "count": len(cues), "last_end": previous_end, "cues": cues}, errors


def audio_regression(ffmpeg: str, video: Path, original: Path, narration: Path | None,
                     duration: float, narration_filter: str) -> dict[str, Any]:
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Audio regression requires the local NumPy package") from exc

    sample_rate = 12000

    def decode(path: Path, audio_filter: str = ""):
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-vn"]
        if audio_filter:
            command += ["-af", audio_filter]
        command += ["-t", str(duration), "-ar", str(sample_rate), "-ac", "2", "-f", "f32le", "pipe:1"]
        return np.frombuffer(run(command).stdout, dtype="<f4").astype(np.float64).reshape(-1, 2)

    mixed, source = decode(video), decode(original)
    voice = decode(narration, narration_filter) if narration else None
    frame_count = round(duration * sample_rate)

    def fit_length(signal):
        return np.pad(signal[:frame_count], ((0, max(0, frame_count - len(signal))), (0, 0))).ravel()

    y, source = fit_length(mixed), fit_length(source)
    source -= source.mean()
    y -= y.mean()
    columns = [source]
    if voice is not None:
        voice = fit_length(voice)
        voice -= voice.mean()
        columns.append(voice)
    matrix = np.column_stack(columns)
    gains, _, rank, _ = np.linalg.lstsq(matrix, y, rcond=None)
    estimated = matrix @ gains
    residual = y - estimated
    original_isolated = y - (gains[1] * voice if voice is not None else 0)

    def correlation(a, b):
        denominator = np.linalg.norm(a) * np.linalg.norm(b)
        return float(np.dot(a, b) / denominator) if denominator > 1e-12 else 0.0

    energy = float(np.dot(y, y))
    source_rms = float(np.sqrt(np.mean(source * source)))
    source_correlation = correlation(original_isolated, source)
    result = {
        "method": "least_squares_on_decoded_stereo_samples",
        "sample_rate": sample_rate,
        "compared_duration": frame_count / sample_rate,
        "original_only": str(original),
        "narration": str(narration) if narration else None,
        "narration_filter": narration_filter if narration else None,
        "source_gain": float(gains[0]),
        "source_rms": source_rms,
        "source_correlation_after_removing_narration": source_correlation,
        "reconstruction_correlation": correlation(y, estimated),
        "residual_rms": float(np.sqrt(np.mean(residual * residual))),
        "relative_residual_rms": float(np.linalg.norm(residual) / max(np.linalg.norm(y), 1e-12)),
        "explained_energy_fraction": 1 - float(np.dot(residual, residual)) / max(energy, 1e-12),
        "matrix_rank": int(rank),
    }
    if voice is not None:
        voice_gain = float(np.dot(voice, y) / max(np.dot(voice, voice), 1e-12))
        without_source = y - voice_gain * voice
        result["narration_gain"] = float(gains[1])
        result["source_partial_r_squared"] = 1 - float(np.dot(residual, residual)) / max(float(np.dot(without_source, without_source)), 1e-12)
    result["source_retained"] = bool(source_rms > 1e-5 and rank == len(columns)
                                     and gains[0] > 0.02 and source_correlation > 0.50)
    return result


def verify(args: argparse.Namespace) -> dict[str, Any]:
    video = args.video.expanduser().resolve()
    ffmpeg = _require_binary("ffmpeg")
    ffprobe = args.ffprobe or shutil.which("ffprobe")
    if not ffprobe and Path("/opt/homebrew/bin/ffprobe").is_file():
        ffprobe = "/opt/homebrew/bin/ffprobe"
    if not ffprobe:
        raise RuntimeError("ffprobe is required for independent stream/frame verification")
    errors: list[str] = []
    metadata = ffprobe_json(ffprobe, video, "-show_streams", "-show_format")
    duration = float(metadata["format"]["duration"])
    report: dict[str, Any] = {"video": str(video), "format_duration": duration, "streams": [], "errors": errors}
    if not 0 < duration <= args.max_duration + 0.000001:
        errors.append(f"Video duration {duration:.6f}s exceeds maximum {args.max_duration}s")
    durations = {}
    for kind, selector, expected_codec in (("video", "v:0", "h264"), ("audio", "a:0", "aac")):
        stream = next((entry for entry in metadata["streams"] if entry["codec_type"] == kind), None)
        if stream is None:
            errors.append(f"Missing {kind} stream")
            continue
        frames = ffprobe_json(ffprobe, video, "-select_streams", selector, "-read_intervals", "%+#32",
                              "-show_frames", "-show_entries", "frame=pts_time,best_effort_timestamp_time")
        decoded_frames = frames.get("frames", [])
        first = decoded_frames[0] if decoded_frames else {}
        pts_value = first.get("pts_time", first.get("best_effort_timestamp_time"))
        pts = float(pts_value) if pts_value is not None else None
        stream_duration = float(stream.get("duration", duration))
        durations[kind] = stream_duration
        summary = {key: stream.get(key) for key in ("codec_type", "codec_name", "time_base", "start_time", "duration", "sample_rate", "channels", "width", "height", "avg_frame_rate") if key in stream}
        summary["first_decoded_frame_pts"] = pts
        report["streams"].append(summary)
        if stream.get("codec_name") != expected_codec:
            errors.append(f"Expected {expected_codec} {kind}; found {stream.get('codec_name')}")
        if pts is None or not abs(pts) <= 0.000001:
            errors.append(f"First decoded {kind} frame PTS is {pts}")
        if "start_time" not in stream or abs(float(stream["start_time"])) > 0.000001:
            errors.append(f"{kind} stream does not start at zero")
    if len(durations) == 2 and abs(durations["video"] - durations["audio"]) > 0.08:
        errors.append("Video/audio stream durations differ by more than 80 ms")
    decoding = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-xerror", "-err_detect", "explode",
                               "-i", str(video), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                              capture_output=True, text=True)
    report["full_decode"] = {"returncode": decoding.returncode, "diagnostics": decoding.stderr.strip()}
    if decoding.returncode or decoding.stderr.strip():
        errors.append("Complete FFmpeg decode reported an error")
    srt = (args.srt or video.with_suffix(".srt")).expanduser().resolve()
    if srt.is_file():
        # The final still frame remains visible until the container/audio end;
        # some FFmpeg muxers omit that last frame's duration from video metadata.
        report["subtitles"], subtitle_errors = inspect_subtitles(srt, duration)
        errors.extend(subtitle_errors)
    else:
        errors.append(f"Missing subtitle file: {srt}")
    timeline_path = (args.timeline or video.with_suffix(".json")).expanduser().resolve()
    if timeline_path.is_file():
        timeline = json.loads(timeline_path.read_text())
        narration_text = str(timeline.get("narration", ""))
        cue_text = "".join(cue["text"] for cue in report.get("subtitles", {}).get("cues", []))
        compact = lambda text: re.sub(r"\s+", "", text)
        if narration_text and compact(narration_text) != compact(cue_text):
            errors.append("SRT text does not match timeline narration sentences")
        report["timeline"] = {"path": str(timeline_path), "narration_matches_subtitles": bool(narration_text and compact(narration_text) == compact(cue_text))}
    if args.original_only:
        report["source_audio"] = audio_regression(ffmpeg, video, args.original_only.resolve(),
                                                    args.narration.resolve() if args.narration else None,
                                                    duration, args.narration_filter)
        if not report["source_audio"]["source_retained"]:
            errors.append("Source audio retention could not be demonstrated by sample regression")
    report["passed"] = not errors
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", type=Path)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--timeline", type=Path)
    parser.add_argument("--original-only", type=Path)
    parser.add_argument("--narration", type=Path)
    parser.add_argument("--narration-filter", default="loudnorm=I=-18:TP=-2:LRA=7")
    parser.add_argument("--max-duration", type=float, default=60.0)
    parser.add_argument("--ffprobe")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = verify(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        report = {"video": str(args.video), "passed": False, "errors": [str(exc)]}
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

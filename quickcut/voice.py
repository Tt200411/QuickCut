from __future__ import annotations

"""Local caption and narration helpers.

The macOS `say` command is used when available, so narration stays on-device.
The adapter is deliberately small and can be replaced by a local Kokoro/Piper
backend later without changing the Story/Timeline API.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from .core import _require_binary, render_timeline
from .types import Timeline


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        whole += 1
        millis = 0
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"


def write_srt(timeline: Timeline, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    captions = timeline.captions or [{"start": 0.0, "end": timeline.duration, "text": timeline.query}]
    lines: list[str] = []
    for idx, caption in enumerate(captions, 1):
        text = str(caption.get("text", "")).strip()
        if not text:
            continue
        lines.extend([str(idx), f"{_srt_time(caption['start'])} --> {_srt_time(caption['end'])}", text, ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def synthesize_local_narration(text: str, output_path: str | Path, *, voice: str | None = None, rate: int | None = None) -> Path:
    """Create a local narration track using macOS say or an installed espeak."""
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not text.strip():
        raise ValueError("Narration text cannot be empty")
    say = shutil.which("say")
    ffmpeg = _require_binary("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="quickcut-tts-") as temp:
        text_file = Path(temp) / "narration.txt"
        aiff = Path(temp) / "narration.aiff"
        text_file.write_text(text, encoding="utf-8")
        if say:
            command = [say]
            if voice:
                command += ["-v", voice]
            if rate:
                command += ["-r", str(rate)]
            command += ["-f", str(text_file), "-o", str(aiff)]
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            espeak = shutil.which("espeak") or shutil.which("espeak-ng")
            if not espeak:
                raise RuntimeError("No local TTS engine found; install macOS say, espeak, or a local Piper/Kokoro adapter")
            subprocess.run([espeak, "-w", str(aiff), text], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(aiff), "-ar", "22050", "-ac", "1", str(output_path), "-y"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return output_path


def _apply_captions(video: Path, timeline: Timeline, output: Path, srt: Path) -> None:
    ffmpeg = _require_binary("ffmpeg")
    subtitle_path = srt.as_posix().replace("'", "\\'")
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
        "-vf", f"subtitles='{subtitle_path}'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-an", str(output), "-y",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _mix_narration(video: Path, audio: Path, output: Path, duration: float) -> None:
    ffmpeg = _require_binary("ffmpeg")
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0", "-af", "apad", "-t", f"{duration:.3f}",
        "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(output), "-y",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def render_story_timeline(timeline: Timeline, output_path: str | Path, *, captions: bool = True, narration: bool = True, voice: str | None = None) -> Path:
    """Render montage video, optionally burn captions and add local narration."""
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quickcut-story-") as temp:
        temp_dir = Path(temp)
        current = render_timeline(timeline, temp_dir / "base.mp4", preset=timeline.preset)
        if captions:
            captioned = temp_dir / "captioned.mp4"
            _apply_captions(current, timeline, captioned, write_srt(timeline, temp_dir / "captions.srt"))
            current = captioned
        if narration and timeline.narration.strip():
            audio = synthesize_local_narration(timeline.narration, temp_dir / "narration.wav", voice=voice)
            _mix_narration(current, audio, output_path, timeline.duration)
        else:
            shutil.copyfile(current, output_path)
    return output_path

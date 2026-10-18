from __future__ import annotations

"""Local caption and narration helpers.

The macOS `say` command is used when available, so narration stays on-device.
The adapter is deliberately small and can be replaced by a local Kokoro/Piper
backend later without changing the Story/Timeline API.
"""

import re
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
    # Merge identical text across adjacent cuts.  The JSON timeline keeps one
    # cue per item for editing, while the burned subtitle stays stable through
    # a camera cut instead of flashing the same phrase repeatedly.
    entries: list[dict[str, object]] = []
    for idx, caption in enumerate(captions):
        text = str(caption.get("text", "")).strip()
        if not text:
            continue
        # A blank continuation row means the same sentence remains active
        # across the following cut. Extend this cue to the next non-empty row
        # while keeping the JSON representation one row per visual item.
        end = caption["end"]
        next_idx = idx + 1
        while next_idx < len(captions) and not str(captions[next_idx].get("text", "")).strip():
            end = captions[next_idx]["end"]
            next_idx += 1
        if entries and entries[-1]["text"] == text and abs(float(entries[-1]["end"]) - float(caption["start"])) < 0.01:
            entries[-1]["end"] = end
        else:
            entries.append({"start": caption["start"], "end": end, "text": text})
    lines: list[str] = []
    for idx, caption in enumerate(entries, 1):
        lines.extend([str(idx), f"{_srt_time(float(caption['start']))} --> {_srt_time(float(caption['end']))}", str(caption['text']), ""])
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


def _media_duration(path: Path) -> float:
    """Read a media duration without requiring ffprobe in the base install."""
    probe = shutil.which("ffprobe")
    if probe:
        result = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return max(0.0, float(result.stdout.strip()))
    ffmpeg = _require_binary("ffmpeg")
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr.decode("utf-8", errors="replace"))
    if not match:
        raise RuntimeError(f"Could not read narration duration: {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _fit_narration_to_duration(audio: Path, output: Path, duration: float) -> Path:
    """Time-scale local speech so its final word lands inside the story.

    ``apad`` alone only fills a short narration with silence.  That leaves
    captions describing the story after the voice has stopped.  A gentle
    ``atempo`` correction keeps the whole spoken track present for the visual
    timeline while preserving the original soundtrack underneath it.
    """
    source_duration = _media_duration(audio)
    if source_duration <= 0.01 or duration <= 0.01:
        shutil.copyfile(audio, output)
        return output
    tempo = source_duration / float(duration)
    filters: list[str] = []
    # atempo accepts 0.5..2.0 per filter; chain only when a backend returns an
    # unusually short/long track.  Normal macOS ``say`` output uses one pass.
    while tempo < 0.5:
        filters.append("atempo=0.5")
        tempo /= 0.5
    while tempo > 2.0:
        filters.append("atempo=2.0")
        tempo /= 2.0
    filters.append(f"atempo={tempo:.6f}")
    filters.extend(["aresample=48000", "atrim=duration=%.3f" % float(duration), "asetpts=PTS-STARTPTS"])
    ffmpeg = _require_binary("ffmpeg")
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(audio), "-vn",
        "-af", ",".join(filters), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(output), "-y",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return output


def _has_audio_stream(video: Path) -> bool:
    ffmpeg = _require_binary("ffmpeg")
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return bool(re.search(r"Stream #\S+[^\n]*Audio:", result.stderr.decode("utf-8", errors="replace")))


def _apply_captions(video: Path, timeline: Timeline, output: Path, srt: Path) -> None:
    ffmpeg = _require_binary("ffmpeg")
    subtitle_path = srt.as_posix().replace("'", "\\'")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
        "-map", "0:v:0", "-vf", f"setpts=PTS-STARTPTS,subtitles='{subtitle_path}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-bf", "0", "-pix_fmt", "yuv420p",
    ]
    if _has_audio_stream(video):
        # Burning captions should not silently discard the source soundtrack.
        # Re-encode after resetting PTS so the captioned video and soundtrack
        # share an exact zero origin.  Stream-copying preserves container edit
        # lists from the concat stage and is a common source of visible drift.
        command += ["-map", "0:a:0?", "-af", "asetpts=PTS-STARTPTS", "-c:a", "aac",
                    "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    else:
        command += ["-an"]
    command += [str(output), "-y"]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _mix_narration(video: Path, audio: Path, output: Path, duration: float) -> None:
    ffmpeg = _require_binary("ffmpeg")
    has_original_audio = _has_audio_stream(video)
    if has_original_audio:
        # Keep the camera/production track underneath local narration.  Both
        # inputs are explicitly rebased to t=0 and the original track is the
        # first mix input, so the mix has a deterministic timeline even when
        # the source contains a non-zero MP4 edit-list offset.
        filter_complex = (
            "[0:a:0]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,volume=0.55[orig];"
            "[1:a:0]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,volume=1.0,apad[narr];"
            "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "[aout]", "-filter_complex", filter_complex,
            "-t", f"{duration:.3f}", "-vf", "setpts=PTS-STARTPTS", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "23", "-bf", "0", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(output), "-y",
        ]
    else:
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-af", "asetpts=PTS-STARTPTS,apad",
            "-t", f"{duration:.3f}", "-vf", "setpts=PTS-STARTPTS", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "23", "-bf", "0", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(output), "-y",
        ]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


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
            fitted = _fit_narration_to_duration(audio, temp_dir / "narration-fitted.wav", timeline.duration)
            _mix_narration(current, fitted, output_path, timeline.duration)
        else:
            shutil.copyfile(current, output_path)
    return output_path

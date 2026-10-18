from __future__ import annotations

"""Local caption and narration helpers.

The macOS `say` command is used when available, so narration stays on-device.
The adapter is deliberately small and can be replaced by a local Kokoro/Piper
backend later without changing the Story/Timeline API.
"""

import math
import re
import shutil
import subprocess
import tempfile
import wave
from functools import lru_cache
from pathlib import Path

from .core import _require_binary, render_timeline
from .types import Timeline


def _srt_time(seconds: float) -> str:
    milliseconds = round(max(0.0, float(seconds)) * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole, millis = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"


def _caption_entries(timeline: Timeline) -> list[dict[str, object]]:
    """Return bounded, ordered sentence cues, preserving cut continuations."""
    captions = timeline.captions or [{"start": 0.0, "end": timeline.duration, "text": timeline.query}]
    # Merge identical text across adjacent cuts.  The JSON timeline keeps one
    # cue per item for editing, while the burned subtitle stays stable through
    # a camera cut instead of flashing the same phrase repeatedly.
    entries: list[dict[str, object]] = []
    for caption in sorted(captions, key=lambda cue: float(cue["start"])):
        text = str(caption.get("text", "")).strip()
        start, end = float(caption["start"]), float(caption["end"])
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("Subtitle timestamps must be finite")
        start, end = max(0.0, start), min(timeline.duration, end)
        if round(end * 1000) <= round(start * 1000):
            continue
        if not text:
            if entries and start <= float(entries[-1]["end"]) + 0.01:
                entries[-1]["end"] = max(float(entries[-1]["end"]), end)
            continue
        if entries and entries[-1]["text"] == text and start <= float(entries[-1]["end"]) + 0.01:
            entries[-1]["end"] = max(float(entries[-1]["end"]), end)
        else:
            if entries and float(entries[-1]["end"]) > start:
                entries[-1]["end"] = start
                if round(start * 1000) <= round(float(entries[-1]["start"]) * 1000):
                    entries.pop()
            entries.append({"start": start, "end": end, "text": text})
    return entries


def write_srt(timeline: Timeline, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = _caption_entries(timeline)
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
    if rate is not None and rate <= 0:
        raise ValueError("Narration rate must be positive")
    chinese = bool(re.search(r"[\u3400-\u9fff]", text))
    # The system default may be an English voice.  Chinese text always uses a
    # Chinese voice, even when no locale was supplied by a CLI caller.
    if chinese and voice is None:
        voice = "Reed (中文（中国大陆）)"
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
            command = [espeak, "-w", str(aiff)]
            if chinese:
                command += ["-v", "cmn"]
            if rate:
                command += ["-s", str(rate)]
            subprocess.run(command + [text], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
    filters.extend(["asetpts=PTS-STARTPTS", "aresample=48000:async=1:first_pts=0", "apad",
                    "atrim=duration=%.6f" % float(duration), "asetpts=PTS-STARTPTS"])
    ffmpeg = _require_binary("ffmpeg")
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(audio), "-vn",
        "-af", ",".join(filters), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(output), "-y",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return output


def synthesize_timed_narration(
    timeline: Timeline, output_path: str | Path, *, voice: str | None = None,
    rate: int | None = None, sentence_pause: float = 0.18,
) -> Path:
    """Synthesize sentence WAVs and use their measured lengths for subtitle timing.

    Matching explicit captions retain their visual beat timing. Otherwise the
    measured speech lengths proportionally divide the timeline. A sentence is
    synthesized exactly once, so crossing an edit cannot repeat or flash it.
    The output is exact-length 48 kHz PCM, and timings/tempo are retained in the
    timeline metadata for verification.
    """
    if not timeline.narration.strip() or timeline.duration <= 0:
        raise ValueError("Timed narration needs non-empty text and a positive timeline duration")
    if sentence_pause < 0:
        raise ValueError("Sentence pause cannot be negative")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    is_chinese = bool(re.search(r"[\u3400-\u9fff]", timeline.narration))
    selected_voice = voice or ("Reed (中文（中国大陆）)" if is_chinese else None)
    selected_rate = rate if rate is not None else (145 if is_chinese else None)
    entries = _caption_entries(timeline) if timeline.captions else []
    compact = lambda text: re.sub(r"\s+", "", text)
    explicit = bool(entries) and compact("".join(str(c["text"]) for c in entries)) == compact(timeline.narration)
    sentences = [str(c["text"]) for c in entries] if explicit else [
        part.strip() for part in re.findall(r"[^。！？!?\n]+[。！？!?]?", timeline.narration) if part.strip()
    ]
    ffmpeg = _require_binary("ffmpeg")
    sample_rate = 48000
    total_samples = round(timeline.duration * sample_rate)
    track = bytearray(total_samples * 2)
    metadata: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="quickcut-sentences-") as temp:
        work = Path(temp)
        trimmed: list[Path] = []
        durations: list[float] = []
        for i, text in enumerate(sentences):
            raw = synthesize_local_narration(text, work / f"raw-{i}.wav", voice=selected_voice, rate=selected_rate)
            clean = work / f"clean-{i}.wav"
            # Remove only boundary silence; keep rhetorical pauses within each
            # sentence. Otherwise the last word may finish far before its cue.
            trim = "silenceremove=start_periods=1:start_duration=0.01:start_silence=0.04:start_threshold=-50dB"
            subprocess.run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(raw),
                "-af", f"{trim},areverse,{trim},areverse,asetpts=PTS-STARTPTS",
                "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s16le", str(clean), "-y",
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            duration = _media_duration(clean)
            if duration <= 0.01:
                raise ValueError(f"Local TTS returned no speech for sentence {i + 1}")
            trimmed.append(clean)
            durations.append(duration)
        if not explicit:
            cursor = 0.0
            total_raw = sum(durations)
            entries = []
            available = timeline.duration - min(sentence_pause, timeline.duration / (2 * len(sentences))) * len(sentences)
            pause = (timeline.duration - available) / len(sentences)
            for text, raw_duration in zip(sentences, durations):
                end = cursor + available * raw_duration / total_raw + pause
                entries.append({"start": cursor, "end": min(end, timeline.duration), "text": text})
                cursor = end
        for i, (cue, raw, raw_duration) in enumerate(zip(entries, trimmed, durations)):
            start_sample = round(float(cue["start"]) * sample_rate)
            end_sample = min(total_samples, round(float(cue["end"]) * sample_rate))
            span = (end_sample - start_sample) / sample_rate
            speech_duration = span - min(sentence_pause, span * 0.1)
            if speech_duration <= 0.01:
                raise ValueError(f"Sentence {i + 1} has no positive speech interval")
            fitted = _fit_narration_to_duration(raw, work / f"fitted-{i}.wav", speech_duration)
            with wave.open(str(fitted), "rb") as wav:
                if wav.getnchannels() != 1 or wav.getframerate() != sample_rate or wav.getsampwidth() != 2:
                    raise RuntimeError("Narration fitting returned unexpected PCM format")
                pcm = wav.readframes(wav.getnframes())
            pcm = pcm[:(end_sample - start_sample) * 2]
            track[start_sample * 2:start_sample * 2 + len(pcm)] = pcm
            metadata.append({
                "text": cue["text"], "start": round(start_sample / sample_rate, 6),
                "end": round(end_sample / sample_rate, 6), "raw_duration": round(raw_duration, 6),
                "speech_end": round(start_sample / sample_rate + speech_duration, 6),
                "tempo": round(raw_duration / speech_duration, 6),
            })
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(track)
    timeline.captions = [{"start": c["start"], "end": c["end"], "text": c["text"]} for c in metadata]
    timeline.story["narration_voice"] = {"engine": "macOS say" if shutil.which("say") else "espeak", "voice": selected_voice, "rate": selected_rate, "local": True}
    timeline.story["narration_audio"] = {"duration": total_samples / sample_rate, "timing": "measured_sentence_wav", "explicit_cue_timing": explicit, "sentences": metadata}
    return output


def _has_audio_stream(video: Path) -> bool:
    ffmpeg = _require_binary("ffmpeg")
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return bool(re.search(r"Stream #\S+[^\n]*Audio:", result.stderr.decode("utf-8", errors="replace")))


@lru_cache(maxsize=4)
def _caption_ffmpeg(preferred: str) -> str:
    """Find an installed FFmpeg with libass, including the bundled local build."""
    candidates = [preferred]
    try:
        import imageio_ffmpeg
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    for candidate in dict.fromkeys(candidates):
        result = subprocess.run([candidate, "-hide_banner", "-filters"], capture_output=True, text=True, check=True)
        if re.search(r"\bsubtitles\s+V->V", result.stdout):
            return candidate
    raise RuntimeError("Caption burning requires a local FFmpeg with the libass subtitles filter")


def _apply_captions(video: Path, timeline: Timeline, output: Path, srt: Path) -> None:
    ffmpeg = _caption_ffmpeg(_require_binary("ffmpeg"))
    subtitle_path = srt.as_posix().replace("'", "\\'")
    style = timeline.story.get("subtitle_style", {
        "FontName": "PingFang SC", "FontSize": 20, "Outline": 1.3,
        "Shadow": 0, "MarginV": 26, "Alignment": 2,
    })
    if isinstance(style, dict):
        style = ",".join(f"{key}={value}" for key, value in style.items())
    if not isinstance(style, str):
        raise ValueError("Subtitle style must be an ASS style string or dictionary")
    style = style.replace("'", "\\'")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
        "-map", "0:v:0", "-vf", f"setpts=PTS-STARTPTS,subtitles='{subtitle_path}':force_style='{style}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-bf", "0", "-pix_fmt", "yuv420p",
    ]
    if _has_audio_stream(video):
        # Burning captions should not silently discard the source soundtrack.
        # Re-encode after resetting PTS so the captioned video and soundtrack
        # share an exact zero origin.  Stream-copying preserves container edit
        # lists from the concat stage and is a common source of visible drift.
        command += ["-map", "0:a:0?", "-af", "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0", "-c:a", "aac",
                    "-b:a", "192k", "-ar", "48000", "-ac", "2"]
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
            f"[0:a:0]asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,volume=0.30,apad,atrim=duration={duration:.6f}[orig];"
            f"[1:a:0]asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,loudnorm=I=-18:TP=-2:LRA=7,apad,atrim=duration={duration:.6f}[narr];"
            "[orig][narr]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95:level=0:latency=1,asetpts=PTS-STARTPTS[aout]"
        )
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "[aout]", "-filter_complex", filter_complex,
            "-t", f"{duration:.3f}", "-vf", "setpts=PTS-STARTPTS", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "19", "-bf", "0", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(output), "-y",
        ]
    else:
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-af", "asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,apad",
            "-t", f"{duration:.3f}", "-vf", "setpts=PTS-STARTPTS", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "19", "-bf", "0", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(output), "-y",
        ]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def render_story_timeline(timeline: Timeline, output_path: str | Path, *, captions: bool = True, narration: bool = True, voice: str | None = None, rate: int | None = None) -> Path:
    """Render montage video, optionally burn captions and add local narration."""
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quickcut-story-") as temp:
        temp_dir = Path(temp)
        current = render_timeline(timeline, temp_dir / "base.mp4", preset=timeline.preset)
        fitted = None
        if narration and timeline.narration.strip():
            fitted = synthesize_timed_narration(timeline, temp_dir / "narration-fitted.wav", voice=voice, rate=rate)
        if captions:
            captioned = temp_dir / "captioned.mp4"
            _apply_captions(current, timeline, captioned, write_srt(timeline, temp_dir / "captions.srt"))
            current = captioned
        if fitted is not None:
            _mix_narration(current, fitted, output_path, timeline.duration)
        else:
            shutil.copyfile(current, output_path)
    return output_path

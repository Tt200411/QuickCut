from __future__ import annotations

import subprocess
import math
import struct
import wave

import pytest

from quickcut.core import _has_audio_stream, _require_binary
from quickcut.types import Timeline, TimelineItem
from quickcut.voice import _mix_narration, _srt_time, render_story_timeline, synthesize_timed_narration, write_srt


def test_write_srt_uses_relative_timeline_captions(tmp_path):
    timeline = Timeline(
        version="1.0", source_video="source.mp4", query="travel", duration_budget=6,
        index_id="idx", coverage="source", items=[TimelineItem("item-000", "source", "shot-0", 0, 6)],
        captions=[{"start": 0, "end": 3.25, "text": "The journey begins."}],
    )
    path = write_srt(timeline, tmp_path / "captions.srt")
    text = path.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:03,250" in text
    assert "The journey begins." in text


def test_caption_render_keeps_original_audio(tmp_path):
    ffmpeg = _require_binary("ffmpeg")
    source = tmp_path / "source.mp4"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=skyblue:s=320x180:d=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", str(source), "-y",
    ], check=True)
    timeline = Timeline(
        version="1.0", source_video=str(source), query="travel", duration_budget=6,
        index_id="idx", coverage="source", items=[TimelineItem("item-000", "source", "shot-0", 0, 6)],
        captions=[{"start": 0, "end": 6, "text": "The journey begins."}],
    )
    output = render_story_timeline(timeline, tmp_path / "captioned.mp4", narration=False)
    assert _has_audio_stream(output)
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(output), "-f", "null", "-"], check=True)

    # Caption rendering must rebase both streams to the same zero origin.  A
    # non-zero first video PTS (typically one H.264 frame from encoder delay)
    # is enough to make a narration/caption feel late in strict players.
    video_probe = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "info", "-i", str(output),
        "-vf", "showinfo", "-an", "-frames:v", "1", "-f", "null", "-",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    audio_probe = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "info", "-i", str(output),
        "-vn", "-af", "ashowinfo", "-frames:a", "1", "-f", "null", "-",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert "pts_time:0" in video_probe.stderr
    assert "pts_time:0" in audio_probe.stderr


def test_narration_mix_rebases_and_bounds_both_streams(tmp_path):
    ffmpeg = _require_binary("ffmpeg")
    source = tmp_path / "source.mp4"
    narration = tmp_path / "narration.wav"
    output = tmp_path / "mixed.mp4"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=skyblue:s=320x180:d=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-bf", "0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", str(source), "-y",
    ], check=True)
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "sine=frequency=880:duration=2", "-c:a", "pcm_s16le", str(narration), "-y",
    ], check=True)
    _mix_narration(source, narration, output, 6.0)
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(output), "-f", "null", "-"], check=True)
    video_probe = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "info", "-i", str(output),
        "-vf", "showinfo", "-an", "-frames:v", "1", "-f", "null", "-",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    audio_probe = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "info", "-i", str(output),
        "-vn", "-af", "ashowinfo", "-frames:a", "1", "-f", "null", "-",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert "pts_time:0" in video_probe.stderr
    assert "pts_time:0" in audio_probe.stderr

    # A nonempty audio stream alone does not prove the soundtrack survived.
    # The original 440 Hz component must remain after the 880 Hz narration ends.
    pcm = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "3", "-i", str(output),
        "-t", "1", "-vn", "-ar", "8000", "-ac", "1", "-f", "s16le", "-",
    ], check=True, capture_output=True).stdout
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    energy_440 = abs(sum(value * complex(math.cos(2 * math.pi * 440 * i / 8000), math.sin(2 * math.pi * 440 * i / 8000)) for i, value in enumerate(samples))) / len(samples)
    energy_880 = abs(sum(value * complex(math.cos(2 * math.pi * 880 * i / 8000), math.sin(2 * math.pi * 880 * i / 8000)) for i, value in enumerate(samples))) / len(samples)
    assert energy_440 > 100
    assert energy_440 > energy_880 * 10


def test_srt_merges_sentence_across_cuts_and_removes_invalid_cues(tmp_path):
    timeline = Timeline(
        "1.0", "source.mp4", "旅行", 6, "idx", "source",
        items=[TimelineItem("a", "source", "a", 0, 6)],
        captions=[
            {"start": 0, "end": 2, "text": "兔子开始反击。"},
            {"start": 2, "end": 3, "text": ""},
            {"start": 3, "end": 4, "text": "兔子开始反击。"},
            {"start": 3.5, "end": 6.5, "text": "森林恢复平静。"},
            {"start": 6, "end": 6, "text": "无效。"},
        ],
    )
    srt = write_srt(timeline, tmp_path / "captions.srt").read_text()
    assert srt.count("兔子开始反击。") == 1
    assert "00:00:00,000 --> 00:00:03,500" in srt
    assert "00:00:03,500 --> 00:00:06,000" in srt
    assert "无效" not in srt
    assert _srt_time(59.9996) == "00:01:00,000"
    assert _srt_time(3599.9996) == "01:00:00,000"


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), float("-inf")])
def test_srt_rejects_nonfinite_timestamps(tmp_path, bad_time):
    timeline = Timeline(
        "1.0", "source.mp4", "旅行", 6, "idx", "source",
        items=[TimelineItem("a", "source", "a", 0, 6)],
        captions=[{"start": 0, "end": bad_time, "text": "兔子开始反击。"}],
    )
    with pytest.raises(ValueError, match="finite"):
        write_srt(timeline, tmp_path / "captions.srt")


@pytest.mark.parametrize("explicit_cues", [False, True])
def test_timed_narration_aligns_real_wavs_and_exact_sentence_cues(tmp_path, monkeypatch, explicit_cues):
    def fake_tts(text, output_path, *, voice=None, rate=None):
        assert voice == "Reed (中文（中国大陆）)"
        assert rate == 145
        duration = 1 if text == "兔子起身。" else 2
        subprocess.run([
            _require_binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}",
            "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le", str(output_path), "-y",
        ], check=True)
        return output_path

    monkeypatch.setattr("quickcut.voice.synthesize_local_narration", fake_tts)
    timeline = Timeline(
        "1.0", "source.mp4", "中文概括", 6, "idx", "source",
        items=[TimelineItem("a", "source", "a", 0, 1), TimelineItem("b", "source", "b", 1, 6)],
        narration="兔子起身。森林恢复平静。",
        captions=[{"start": 0, "end": 2, "text": "兔子起身。"},
                  {"start": 2, "end": 6, "text": "森林恢复平静。"}] if explicit_cues else [],
    )
    path = synthesize_timed_narration(timeline, tmp_path / "timed.wav")
    with wave.open(str(path)) as wav:
        assert wav.getnframes() == 6 * 48000
        assert wav.getframerate() == 48000
        assert wav.getsampwidth() == 2
    cues = timeline.captions
    assert [cue["text"] for cue in cues] == ["兔子起身。", "森林恢复平静。"]
    assert cues[0]["start"] == 0
    assert cues[0]["end"] == cues[1]["start"]
    assert cues[-1]["end"] == 6
    assert all(cue["end"] > cue["start"] for cue in cues)
    metadata = timeline.story["narration_audio"]
    assert metadata["explicit_cue_timing"] is explicit_cues
    assert metadata["sentences"][-1]["speech_end"] == pytest.approx(5.82)
    assert metadata["sentences"][0]["raw_duration"] == pytest.approx(1, abs=0.03)
    assert metadata["sentences"][1]["raw_duration"] == pytest.approx(2, abs=0.03)
    if explicit_cues:
        assert cues[0]["end"] == 2
    else:
        assert cues[0]["end"] == pytest.approx(2.06, abs=0.03)

from __future__ import annotations

import subprocess

from quickcut.core import _has_audio_stream, _require_binary
from quickcut.types import Timeline, TimelineItem
from quickcut.voice import render_story_timeline, write_srt


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

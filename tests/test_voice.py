from __future__ import annotations

from quickcut.types import Timeline, TimelineItem
from quickcut.voice import write_srt


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

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from quickcut.core import (
    MAX_TIMELINE_SECONDS,
    _has_audio_stream,
    _probable_credit_shot,
    _require_binary,
    build_index,
    localize,
    parse_prompt,
    render_timeline,
    update_timeline,
    validate_timeline,
)
from quickcut.types import IndexManifest, Shot, Timeline, TimelineItem


def make_video(path: Path) -> None:
    subprocess.run([
        _require_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=skyblue:s=320x180:d=4", "-f", "lavfi", "-i", "color=c=darkgreen:s=320x180:d=4",
        "-f", "lavfi", "-i", "color=c=orange:s=320x180:d=4", "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[outv]", "-map", "[outv]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path), "-y"
    ], check=True)


def test_index_localize_and_edit_round_trip(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    make_video(video)
    manifest = build_index(video, tmp_path / "cache", window=3, encoder_mode="fallback")
    assert manifest.shots
    assert Path(manifest.shots[0].keyframe).is_absolute()
    timeline = localize(manifest, "sky blue scenic 8 seconds", encoder_mode="fallback")
    validate_timeline(timeline)
    assert timeline.index_id == manifest.index_id
    assert 5.0 <= timeline.duration <= 8.0
    edited = update_timeline(timeline, [{"item_id": timeline.items[0].item_id, "end": timeline.items[0].start + 1.0}])
    assert edited.index_id == manifest.index_id
    assert edited.duration <= edited.duration_budget
    rendered = render_timeline(edited, tmp_path / "clip.mp4")
    assert rendered.exists() and rendered.stat().st_size > 0


def test_render_keeps_source_audio_and_produces_decodable_h264(tmp_path: Path):
    video = tmp_path / "av-sample.mp4"
    subprocess.run([
        _require_binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=skyblue:s=320x180:d=8",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", str(video), "-y",
    ], check=True)
    manifest = build_index(video, tmp_path / "cache", window=3, encoder_mode="fallback")
    timeline = localize(manifest, "sky blue scenic 8 seconds", encoder_mode="fallback")
    rendered = render_timeline(timeline, tmp_path / "av-clip.mp4")
    assert _has_audio_stream(rendered)
    # Decoding the complete output catches malformed concat/H.264 timestamps
    # that may look like a corrupted image in hardware players.
    subprocess.run([
        _require_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(rendered),
        "-f", "null", "-",
    ], check=True)


def test_localize_avoids_end_credit_roll_for_basic_montage():
    manifest = IndexManifest(
        index_id="long", source_video="source.mp4", source_size=1, source_mtime=1.0,
        duration=600.0, window=3.0, stride=3.0, created_at="now", encoder="fallback",
        shots=[
            Shot("shot-credit", 540, 543, transcript="forest credits"),
            Shot("shot-a", 100, 103, transcript="forest path"),
            Shot("shot-b", 200, 203, transcript="forest group"),
            Shot("shot-c", 300, 303, transcript="forest close"),
        ],
    )
    assert _probable_credit_shot(manifest, manifest.shots[0], "forest") is True
    assert _probable_credit_shot(manifest, manifest.shots[0], "show the end credits") is False
    timeline = localize(manifest, "forest 8 seconds", encoder_mode="fallback", avoid_credits=True)
    assert all(item.asset_id != "shot-credit" for item in timeline.items)


def test_story_duration_accepts_one_minute_and_clamps_longer_requests():
    _, budget = parse_prompt("a complete story 60 seconds")
    assert budget == MAX_TIMELINE_SECONDS
    _, chinese_budget = parse_prompt("一个完整故事 60秒")
    assert chinese_budget == MAX_TIMELINE_SECONDS
    _, clamped = parse_prompt("a complete story 120 seconds")
    assert clamped == MAX_TIMELINE_SECONDS

    validate_timeline(Timeline(
        version="1.0", source_video="source.mp4", query="story",
        duration_budget=MAX_TIMELINE_SECONDS, index_id="long", coverage="source",
        items=[TimelineItem(f"item-{idx:02d}", "source", f"shot-{idx:02d}", idx * 3, (idx + 1) * 3)
               for idx in range(20)],
    ))

    too_long = Timeline(
        version="1.0", source_video="source.mp4", query="story",
        duration_budget=MAX_TIMELINE_SECONDS + 0.1, index_id="long", coverage="source",
        items=[TimelineItem("item-00", "source", "shot-00", 0, 3)],
    )
    try:
        validate_timeline(too_long)
    except ValueError as exc:
        assert "between 5 and 60 seconds" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("durations above one minute must be rejected")

from __future__ import annotations

import json
import math
import re
import subprocess
from array import array
from pathlib import Path

import pytest

import quickcut.core as core
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


@pytest.mark.parametrize("damage", ["missing-field", "deleted-file", "truncated-file"])
def test_index_rebuilds_caches_without_real_keyframes(tmp_path: Path, monkeypatch, damage: str):
    video = tmp_path / "sample.mp4"
    make_video(video)
    cache = tmp_path / "cache"
    manifest = build_index(video, cache, window=6, encoder_mode="fallback")
    manifest_path = cache / f"{manifest.index_id}.json"
    payload = json.loads(manifest_path.read_text())
    frame = Path(manifest.shots[0].keyframe)
    if damage == "missing-field":
        payload["shots"][0].pop("keyframe")
        manifest_path.write_text(json.dumps(payload))
    elif damage == "deleted-file":
        frame.unlink()
    else:
        frame.write_bytes(b"\x89PNG\r\n\x1a\n")
    extract = core.extract_keyframe
    calls = []

    def record_extract(path, timestamp):
        calls.append(timestamp)
        return extract(path, timestamp)

    monkeypatch.setattr(core, "extract_keyframe", record_extract)
    rebuilt = build_index(video, cache, window=6, encoder_mode="fallback")
    assert len(calls) == len(rebuilt.shots)
    assert all(Path(shot.keyframe).is_file() for shot in rebuilt.shots)
    assert core._manifest_has_keyframes(rebuilt)
    calls.clear()
    build_index(video, cache, window=6, encoder_mode="fallback")
    assert calls == []  # A complete index remains reusable.


def test_fallback_index_does_not_hide_failed_extraction(tmp_path: Path, monkeypatch):
    video = tmp_path / "sample.mp4"
    video.touch()
    monkeypatch.setattr(core, "probe_video", lambda _: {"duration": 3.0, "size": 1})

    def fail_extract(*args):
        raise RuntimeError("damaged media")

    monkeypatch.setattr(core, "extract_keyframe", fail_extract)
    with pytest.raises(RuntimeError, match="Unable to encode keyframe.*damaged media"):
        build_index(video, tmp_path / "cache", encoder_mode="fallback")
    assert not list((tmp_path / "cache").glob("*.json"))


def test_probe_accepts_string_path_without_ffprobe(tmp_path: Path, monkeypatch):
    video = tmp_path / "sample.mp4"
    make_video(video)
    which = core.shutil.which
    monkeypatch.setattr(core.shutil, "which", lambda name: None if name == "ffprobe" else which(name))
    result = core.probe_video(str(video))
    assert result["duration"] == 12.0
    assert result["size"] == video.stat().st_size


def test_index_covers_tail_beyond_240_windows(tmp_path: Path, monkeypatch):
    video = tmp_path / "long.mp4"
    video.touch()
    monkeypatch.setattr(core, "probe_video", lambda _: {"duration": 241.0, "size": 1})
    monkeypatch.setattr(core, "extract_keyframe", lambda *_: b"P6\n1 1\n255\n\x80\x80\x80")
    manifest = build_index(video, tmp_path / "cache", window=1, encoder_mode="fallback")
    assert len(manifest.shots) == 241
    assert manifest.shots[-1].end == 241.0


@pytest.mark.parametrize("window,stride", [(0, None), (3, 0), (-1, 1), (1, float("nan"))])
def test_index_rejects_invalid_windows(tmp_path: Path, window, stride):
    video = tmp_path / "sample.mp4"
    video.touch()
    with pytest.raises(ValueError, match="positive finite"):
        build_index(video, window=window, stride=stride)


def test_render_preserves_audio_at_reordered_fractional_cuts_and_zero_pts(tmp_path: Path):
    ffmpeg = _require_binary("ffmpeg")
    source = tmp_path / "sync-source.mp4"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=8",
        "-f", "lavfi", "-i",
        "aevalsrc=0.2*sin(2*PI*if(lt(t\\,2)\\,440\\,if(lt(t\\,4)\\,660\\,if(lt(t\\,6)\\,880\\,1100)))*t):s=48000:d=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source), "-y",
    ], check=True)
    timeline = Timeline(
        version="1.0", source_video=str(source), query="sync test", duration_budget=6,
        index_id="sync", coverage="source", items=[
            TimelineItem("first", "source", "a", 4, 6.04),
            TimelineItem("second", "source", "b", 0.6, 2.4),
            TimelineItem("third", "source", "c", 6, 7.36),
        ],
    )
    rendered = render_timeline(timeline, tmp_path / "sync-output.mp4")
    # -copyts keeps container timing visible, instead of letting FFmpeg shift
    # a delayed first stream to zero before the assertions see it.
    probe = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "info", "-copyts", "-i", str(rendered),
        "-vf", "showinfo", "-af", "ashowinfo", "-t", "0.05", "-f", "null", "-",
    ], check=True, capture_output=True, text=True)
    assert re.search(r"Parsed_showinfo.*n:\s*0\s+pts:\s*0\s+pts_time:0(?:\s|$)", probe.stderr)
    assert re.search(r"Parsed_ashowinfo.*n:0\s+pts:0\s+pts_time:0(?:\s|$)", probe.stderr)
    decoded = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(rendered),
        "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1",
    ], check=True, capture_output=True)
    samples = array("f", decoded.stdout)
    assert abs(len(samples) / 48000 - timeline.duration) < 0.04
    output_cursor = 0.0
    for item, expected_frequency in zip(timeline.items, [880, 440, 1100]):
        start = round((output_cursor + 0.3) * 48000)
        section = samples[start:start + 9600]
        rms = math.sqrt(sum(value * value for value in section) / len(section))
        # Spectral projection verifies the original sound belongs to the
        # corresponding source cut, rather than merely checking an audio track.
        real = sum(value * math.cos(2 * math.pi * expected_frequency * i / 48000)
                   for i, value in enumerate(section))
        imag = sum(value * math.sin(2 * math.pi * expected_frequency * i / 48000)
                   for i, value in enumerate(section))
        amplitude = 2 * math.hypot(real, imag) / len(section)
        assert rms > 0.08
        assert amplitude > 0.12
        output_cursor += item.duration
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-xerror", "-i", str(rendered),
        "-f", "null", "-",
    ], check=True)


def test_summary_verifier_rejects_overlapping_zero_length_and_repeated_cues(tmp_path: Path):
    from scripts.verify_chinese_summary import inspect_subtitles

    srt = tmp_path / "invalid.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n故事开始。\n\n"
        "2\n00:00:01,900 --> 00:00:03,000\n故事开始。\n\n"
        "3\n00:00:03,000 --> 00:00:03,000\n故事结束。\n",
        encoding="utf-8",
    )
    _, errors = inspect_subtitles(srt, 5)
    assert any("overlaps" in error for error in errors)
    assert any("zero or negative" in error for error in errors)
    assert any("repeats" in error for error in errors)


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

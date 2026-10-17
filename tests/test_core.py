from __future__ import annotations

import json
import subprocess
from pathlib import Path

from quickcut.core import _require_binary, build_index, localize, render_timeline, update_timeline, validate_timeline


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
    timeline = localize(manifest, "sky blue scenic 8 seconds", encoder_mode="fallback")
    validate_timeline(timeline)
    assert timeline.index_id == manifest.index_id
    assert 5.0 <= timeline.duration <= 8.0
    edited = update_timeline(timeline, [{"item_id": timeline.items[0].item_id, "end": timeline.items[0].start + 1.0}])
    assert edited.index_id == manifest.index_id
    assert edited.duration <= edited.duration_budget
    rendered = render_timeline(edited, tmp_path / "clip.mp4")
    assert rendered.exists() and rendered.stat().st_size > 0

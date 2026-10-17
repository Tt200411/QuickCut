"""Run the complete QuickCut MVP on a generated fixture video."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from quickcut.core import _require_binary, build_index, localize, render_timeline, update_timeline


def main() -> None:
    root = Path("outputs/smoke-demo")
    root.mkdir(parents=True, exist_ok=True)
    video = root / "fixture.mp4"
    ffmpeg = _require_binary("ffmpeg")
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=skyblue:s=320x180:d=4", "-f", "lavfi", "-i", "color=c=darkgreen:s=320x180:d=4",
        "-f", "lavfi", "-i", "color=c=orange:s=320x180:d=4", "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[outv]", "-map", "[outv]", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", str(video), "-y",
    ], check=True)

    manifest = build_index(video, root / "cache", window=3, encoder_mode="fallback")
    timeline = localize(manifest, "sky blue scenic shots for an 8 second vlog opening", encoder_mode="fallback")
    edited = update_timeline(timeline, [{"item_id": timeline.items[0].item_id, "end": timeline.items[0].start + 1.0}])
    rendered = render_timeline(edited, root / "quickcut-edited.mp4")
    unsupported = localize(manifest, "a spaceship on Mars", encoder_mode="fallback")
    (root / "timeline.json").write_text(json.dumps(timeline.to_dict(), indent=2), encoding="utf-8")
    (root / "timeline-edited.json").write_text(json.dumps(edited.to_dict(), indent=2), encoding="utf-8")
    print(json.dumps({
        "index_id": manifest.index_id,
        "encoder": manifest.encoder,
        "source_duration": manifest.duration,
        "timeline_duration": timeline.duration,
        "edited_duration": edited.duration,
        "rendered": str(rendered),
        "unsupported_coverage": unsupported.coverage,
    }, indent=2))


if __name__ == "__main__":
    main()

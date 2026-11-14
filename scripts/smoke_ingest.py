"""Generate six test videos and check ingestion and cache reuse."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from quickcut.core import _ffmpeg_binary
from quickcut.ingest import IngestConfig, SourceSpec, ingest_assets, validate_asset_manifest
from quickcut.media import read_json, run_media


def generate_video(path: Path, *, duration=3, fps="25", size="320x180", audio="none", vfr=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [_ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i",
            f"testsrc2=size={size}:rate={fps}:duration={duration}"]
    if audio != "none":
        signal = "anullsrc=r=16000:cl=mono" if audio == "silence" else "sine=frequency=440:sample_rate=16000"
        args += ["-f", "lavfi", "-i", signal, "-t", str(duration), "-c:a", "aac"]
    if vfr:
        args += ["-vf", "select='if(lt(t,1),1,not(mod(n,3)))'", "-fps_mode", "vfr"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    run_media(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="outputs/ingest-smoke")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    inputs = root / "inputs"
    specs = [
        ("short-01.mp4", {"duration": 3, "audio": "tone"}, "user"),
        ("short-02.mp4", {"duration": 4, "fps": "30000/1001", "size": "480x270"}, "user"),
        ("short-03.mp4", {"duration": 2, "audio": "silence"}, "user"),
        ("long-01.mp4", {"duration": 65, "fps": "15", "audio": "tone"}, "user"),
        ("broll-01.mp4", {"duration": 3, "fps": "30", "vfr": True}, "external"),
        ("broll-02.mp4", {"duration": 4, "size": "180x320", "fps": "24"}, "external"),
    ]
    sources = []
    for name, options, origin in specs:
        path = inputs / name
        if not path.exists():
            generate_video(path, **options)
        sources.append(SourceSpec(path, origin, "Synthetic test pattern; tone/silence, no speech or semantic evidence"))
    project = root / "project"
    first = ingest_assets(project, sources, config=IngestConfig(), project_id="project-ingest-smoke")
    if first["processing"]["status"] != "success" or len(first["assets"]) != 6:
        raise RuntimeError(json.dumps(first["processing"], ensure_ascii=False))
    validate_asset_manifest(first, project)
    second = ingest_assets(project, sources, project_id="project-ingest-smoke")
    report = read_json(project / ".quickcut/run-report.json")
    if first != second or not all(stage["cache_hit"] for stage in report["stages"]):
        raise RuntimeError("Repeated ingestion did not reuse the same manifest and stage caches")
    print(json.dumps({"status": "PASS", "assets": len(first["assets"]), "segments": len(first["segments"]),
                      "revision": first["revision"], "all_stages_reused": True,
                      "manifest": str(project / "asset-manifest.json"),
                      "evidence": "Real generated media; no speech model or semantic validation"}, indent=2))


if __name__ == "__main__":
    main()

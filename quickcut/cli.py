from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import build_index, find_manifest, localize, render_timeline, update_timeline


def main() -> None:
    parser = argparse.ArgumentParser(prog="quickcut", description="Local prompt-guided source localization and editing")
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", help="Index a source video once")
    index.add_argument("video")
    index.add_argument("--cache-dir", default="cache")
    index.add_argument("--window", type=float, default=3.0)
    index.add_argument("--stride", type=float)
    index.add_argument("--encoder", choices=["fallback", "auto", "openclip", "mobileclip"], default=None)
    index.add_argument("--force", action="store_true")

    search = sub.add_parser("search", help="Localize a prompt against a cached index")
    search.add_argument("index_id")
    search.add_argument("prompt")
    search.add_argument("--cache-dir", default="cache")
    search.add_argument("--output", default="outputs/timeline.json")
    search.add_argument("--encoder", choices=["fallback", "auto", "openclip", "mobileclip"], default=None)

    render = sub.add_parser("render", help="Render a timeline JSON with FFmpeg")
    render.add_argument("timeline")
    render.add_argument("--output", default="outputs/quickcut.mp4")
    render.add_argument("--preset", default=None, choices=["clean", "warm"])

    edit = sub.add_parser("edit", help="Apply JSON edits to a timeline without re-indexing")
    edit.add_argument("timeline")
    edit.add_argument("edits", help="JSON list, e.g. '[{\"item_id\":\"item-000\",\"end\":4.2}]'")
    edit.add_argument("--output", default="outputs/timeline-edited.json")

    args = parser.parse_args()
    if args.command == "index":
        manifest = build_index(args.video, args.cache_dir, window=args.window, stride=args.stride, encoder_mode=args.encoder, force=args.force)
        print(json.dumps({"index_id": manifest.index_id, "encoder": manifest.encoder, "shots": len(manifest.shots), "duration": manifest.duration, "cache": str(Path(args.cache_dir) / (manifest.index_id + ".json"))}, indent=2))
    elif args.command == "search":
        manifest = find_manifest(args.cache_dir, args.index_id)
        timeline = localize(manifest, args.prompt, encoder_mode=args.encoder)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "render":
        data = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
        from .types import Timeline, TimelineItem
        items = [TimelineItem(**item) for item in data.pop("items", [])]
        data.pop("duration", None)
        output = render_timeline(Timeline(items=items, **data), args.output, preset=args.preset)
        print(output)
    elif args.command == "edit":
        data = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
        from .types import Timeline, TimelineItem
        items = [TimelineItem(**item) for item in data.pop("items", [])]
        data.pop("duration", None)
        timeline = update_timeline(Timeline(items=items, **data), json.loads(args.edits))
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2))

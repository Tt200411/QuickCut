from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .core import (
    DEFAULT_TIMELINE_SECONDS,
    MAX_TIMELINE_SECONDS,
    MIN_TIMELINE_SECONDS,
    build_index,
    find_manifest,
    localize,
    render_timeline,
    update_timeline,
)
from .download import download_video
from .retrieval import RetrievalConfig, Retriever
from .story import build_basic_vlog
from .voice import render_story_timeline


def _load_timeline(path: str):
    from .types import Timeline, TimelineItem
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = [TimelineItem(**item) for item in data.pop("items", [])]
    data.pop("duration", None)
    return Timeline(items=items, **data)


def _retrieve_to_file(args) -> dict:
    if args.broll_library:
        raise ValueError(
            "--broll-library cannot register or relocate media. Import the external files "
            "with quickcut.ingest first, then issue a C request referencing the new "
            "AssetManifest revision. No CandidateSet was written."
        )
    manifest_path, request_path = Path(args.manifest), Path(args.request)
    output = Path(args.output)
    if output.is_symlink() or output.resolve() in {manifest_path.resolve(), request_path.resolve()}:
        raise ValueError("CandidateSet output must not overwrite an input or symbolic link")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_name(output.name + ".lock")
    # A retrieval may take seconds or minutes; serialize writers so two calls
    # cannot both replace revision n with different content labelled n+1.
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValueError(f"CandidateSet output is locked by another writer: {lock}") from exc
    staged = None
    try:
        with handle:
            handle.write(str(os.getpid()))
        previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
        config = RetrievalConfig(
            encoder_mode=args.encoder,
            low_confidence_threshold=args.low_confidence_threshold,
        )
        candidates = Retriever(config).retrieve(
            manifest, request, project_root=args.project_root,
            document_id=args.document_id, revision=args.revision, previous=previous,
        )
        if candidates != previous:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                             prefix=".candidates-", delete=False) as stream:
                staged = Path(stream.name)
                json.dump(candidates, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, output)
        return candidates
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        lock.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(prog="quickcut", description="Local prompt-guided source localization and editing")
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", help="Index a source video once")
    index.add_argument("video")
    index.add_argument("--cache-dir", default="cache")
    index.add_argument("--window", type=float, default=3.0)
    index.add_argument("--stride", type=float)
    index.add_argument("--encoder", choices=["fallback", "auto", "openclip", "mobileclip"], default=None)
    index.add_argument("--asr", dest="asr_mode", choices=["none", "auto", "faster-whisper"], default=None,
                       help="Transcript backend; auto uses faster-whisper when installed")
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

    basic = sub.add_parser("basic", help="Build a rhythmic source-grounded Vlog opening")
    basic.add_argument("video")
    basic.add_argument("prompt")
    basic.add_argument("--cache-dir", default="cache")
    basic.add_argument("--duration", type=float, default=DEFAULT_TIMELINE_SECONDS,
                       help=f"Story duration in seconds ({MIN_TIMELINE_SECONDS:g}-{MAX_TIMELINE_SECONDS:g})")
    basic.add_argument("--encoder", choices=["fallback", "auto", "openclip", "mobileclip"], default=None)
    basic.add_argument("--asr", dest="asr_mode", choices=["none", "auto", "faster-whisper"], default=None)
    basic.add_argument("--planner", choices=["deterministic", "auto", "ollama", "qwen2.5-vl"], default="deterministic",
                       help="Story planner; ollama/qwen2.5-vl use a local Ollama model")
    basic.add_argument("--timeline", default="outputs/basic-timeline.json")
    basic.add_argument("--render", action="store_true", help="Also render MP4 with local captions and narration")
    basic.add_argument("--output", default="outputs/basic-vlog.mp4")
    basic.add_argument("--voice", default=None, help="macOS say voice name, e.g. Ting-Ting")

    story_render = sub.add_parser("render-story", help="Render a story timeline with captions and local narration")
    story_render.add_argument("timeline")
    story_render.add_argument("--output", default="outputs/story-vlog.mp4")
    story_render.add_argument("--no-captions", action="store_true")
    story_render.add_argument("--no-narration", action="store_true")
    story_render.add_argument("--voice", default=None)

    download = sub.add_parser("download", help="Download one user-provided or openly licensed source video")
    download.add_argument("url")
    download.add_argument("--output-dir", default="inputs")
    download.add_argument("--max-duration", type=int, default=900)

    retrieve_cmd = sub.add_parser(
        "retrieve",
        help="Module B: run a RetrievalRequest against an AssetManifest and emit a CandidateSet",
    )
    retrieve_cmd.add_argument("manifest", help="Path to the project AssetManifest JSON")
    retrieve_cmd.add_argument("request", help="Path to the RetrievalRequest JSON")
    retrieve_cmd.add_argument(
        "--broll-library",
        action="append",
        default=[],
        help="Unsupported until A has registered library media; import via quickcut.ingest instead",
    )
    retrieve_cmd.add_argument("--output", default="outputs/candidates.json")
    retrieve_cmd.add_argument("--document-id", default="candidates-001")
    retrieve_cmd.add_argument("--revision", type=int, default=None,
                              help="Optional explicit revision; existing output otherwise increments only on change")
    retrieve_cmd.add_argument(
        "--encoder",
        choices=["fallback", "auto", "openclip", "mobileclip"],
        default=None,
        help="Visual encoder for keyframes that exist on disk",
    )
    retrieve_cmd.add_argument(
        "--project-root",
        default=None,
        help="Project root used to resolve relative media/keyframe URIs",
    )
    retrieve_cmd.add_argument(
        "--low-confidence-threshold",
        type=float,
        default=0.22,
        help="Candidates below this ranking_score get a low_confidence flag",
    )

    args = parser.parse_args()
    if args.command == "index":
        manifest = build_index(args.video, args.cache_dir, window=args.window, stride=args.stride,
                               encoder_mode=args.encoder, asr_mode=args.asr_mode, force=args.force)
        print(json.dumps({"index_id": manifest.index_id, "encoder": manifest.encoder,
                          "transcript_backend": manifest.transcript_backend,
                          "shots": len(manifest.shots), "duration": manifest.duration,
                          "cache": str(Path(args.cache_dir) / (manifest.index_id + ".json"))}, indent=2))
    elif args.command == "search":
        manifest = find_manifest(args.cache_dir, args.index_id)
        timeline = localize(manifest, args.prompt, encoder_mode=args.encoder)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "render":
        timeline = _load_timeline(args.timeline)
        output = render_timeline(timeline, args.output, preset=args.preset)
        print(output)
    elif args.command == "edit":
        timeline = update_timeline(_load_timeline(args.timeline), json.loads(args.edits))
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "basic":
        manifest = build_index(args.video, args.cache_dir, window=3.0, encoder_mode=args.encoder,
                               asr_mode=args.asr_mode)
        timeline = build_basic_vlog(manifest, args.prompt, duration=args.duration, encoder_mode=args.encoder,
                                    planner=args.planner)
        Path(args.timeline).parent.mkdir(parents=True, exist_ok=True)
        Path(args.timeline).write_text(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        result = {"index_id": manifest.index_id, "timeline": str(Path(args.timeline)),
                  "duration": timeline.duration, "transcript_backend": manifest.transcript_backend}
        if args.render:
            result["output"] = str(render_story_timeline(timeline, args.output, voice=args.voice))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "render-story":
        timeline = _load_timeline(args.timeline)
        print(render_story_timeline(timeline, args.output, captions=not args.no_captions,
                                    narration=not args.no_narration, voice=args.voice))
    elif args.command == "download":
        print(download_video(args.url, args.output_dir, max_duration=args.max_duration))
    elif args.command == "retrieve":
        try:
            candidates = _retrieve_to_file(args)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
        return 2 if candidates.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())

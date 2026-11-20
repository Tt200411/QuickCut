"""Run the legacy single-video/system-voice demo to two isolated MP4 outputs.

This compatibility demo requires OpenCLIP, faster-whisper and the requested
local Qwen model. Narration uses the legacy system voice, not IndexTTS or a
reference WAV; it does not export a Jianying project. For the current A–F
workflow with IndexTTS/reference audio and Jianying, launch QuickCut.command.

From the repository root, with the project virtualenv active:

source .venv/bin/activate
python scripts/run_full_demo.py

"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from quickcut.core import MIN_TIMELINE_SECONDS, build_index, localize, render_timeline, update_timeline
from quickcut.production_pipeline import Runtime, ollama_json, start_ollama
from quickcut.story import build_basic_vlog
from quickcut.types import Timeline
from quickcut.voice import render_story_timeline


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VIDEO = ROOT / "inputs" / "demo.mp4"
DEFAULT_VOICE = "Reed (Chinese (China mainland))"
OLLAMA_URL = "http://127.0.0.1:11434"


def _write_json(path: Path, timeline: Timeline) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _model_tag(name: str) -> str:
    """Only an omitted tag is equivalent to :latest; model sizes are distinct."""
    name = name.strip()
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def _ollama_has_model(base_url: str, model: str) -> bool:
    try:
        payload = ollama_json(Runtime(ollama_url=base_url, model=model), "/api/tags", timeout=5)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Ollama is not answering at {base_url}: {exc}") from exc
    names = [str(item.get("name", "")) for item in payload.get("models", [])]
    return any(_model_tag(name) == _model_tag(model) for name in names if name)


def ensure_ollama(base_url: str, model: str) -> None:
    """Use the production loopback/proxy checks and pull only the exact tag."""
    start_ollama(Runtime(ollama_url=base_url, model=model))
    if not _ollama_has_model(base_url, model):
        ollama = shutil.which("ollama")
        if not ollama:
            raise RuntimeError(f"Model {model} is not installed and the ollama command was not found")
        print(f"Pulling {model}. The first download can take several minutes.")
        subprocess.run([ollama, "pull", model], check=True,
                       env={**os.environ, "OLLAMA_HOST": base_url})
        if not _ollama_has_model(base_url, model):
            raise RuntimeError(f"Requested model {model} is still not listed at {base_url} after pull")


def _verify_story_model(story: Timeline, model: str) -> str:
    planner = str(story.story.get("planner", ""))
    inference = story.story.get("inference") or {}
    actual = inference.get("model") if isinstance(inference, dict) else None
    if (not planner.startswith("ollama:") or _model_tag(planner.removeprefix("ollama:")) != _model_tag(model)
            or not isinstance(actual, str) or _model_tag(actual) != _model_tag(model)):
        raise RuntimeError(f"Story model mismatch: requested {model}, planner={planner or 'missing'}, "
                           f"inference={actual or 'missing'}; narration was not rendered")
    return planner


def _output_directory(value: str | None) -> Path:
    if value is not None:
        output = Path(value).expanduser().absolute()
        # Even an existing empty directory may belong to another run.
        output.mkdir(parents=True, exist_ok=False)
        return output
    parent = ROOT / "outputs"
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="legacy-demo-", dir=parent))


def shorten_first_item(timeline: Timeline) -> tuple[Timeline, str]:
    """Move the first item's out point earlier without dropping below 5 seconds."""
    item = timeline.items[0]
    spare = timeline.duration - MIN_TIMELINE_SECONDS
    cut = min(1.0, item.duration - 0.2, spare)
    if cut < 0.05:
        return timeline, "first item left unchanged; a shorter cut would drop the timeline under 5 seconds"
    new_end = round(item.end - cut, 3)
    edited = update_timeline(timeline, [{"item_id": item.item_id, "end": new_end}])
    note = f"trimmed {item.item_id} end from {item.end:.3f} to {new_end:.3f}"
    return edited, note


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Legacy single-video demo with system narration and two MP4s. "
                                    "Use QuickCut.command for current A–F / IndexTTS reference audio / Jianying.")
    parser.add_argument("--video", default=str(DEFAULT_VIDEO), help="One source video. Default: inputs/demo.mp4")
    parser.add_argument("--prompt", default="风景和人物，做成 15 秒", help="Retrieval prompt, including the desired length")
    parser.add_argument("--story-prompt", default="风景和人物", help="Prompt for the Qwen story")
    parser.add_argument("--duration", type=float, default=30.0, help="Story length in seconds")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="macOS say voice for narration")
    parser.add_argument("--cache-dir", default=str(ROOT / "cache"))
    parser.add_argument("--model", default="qwen2.5vl:3b")
    parser.add_argument("--output-dir", help="New output directory; existing paths are refused. Default: unique outputs/legacy-demo-* directory")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    os.environ.setdefault("QUICKCUT_ENCODER", "openclip")
    os.environ.setdefault("QUICKCUT_OLLAMA_URL", OLLAMA_URL)
    os.environ["QUICKCUT_VLM_MODEL"] = args.model
    video = Path(args.video).expanduser()
    if not video.is_absolute():
        video = ROOT / video
    if not video.exists():
        print(f"Missing source video: {video}", file=sys.stderr)
        print("Put one MP4 at inputs/demo.mp4, or pass --video.", file=sys.stderr)
        return 1

    try:
        output_dir = _output_directory(args.output_dir)
    except OSError as exc:
        print(f"Output directory must be new and writable: {exc}", file=sys.stderr)
        return 1
    print(f"Legacy system-voice demo. Current IndexTTS/Jianying workflow: {ROOT / 'QuickCut.command'}")
    print(f"Outputs: {output_dir}")
    print("Checking Ollama...")
    ensure_ollama(os.environ["QUICKCUT_OLLAMA_URL"], args.model)

    print("Indexing. The first OpenCLIP and Whisper download is the slow part.")
    manifest = build_index(video, args.cache_dir, encoder_mode="openclip", asr_mode="faster-whisper")
    if manifest.encoder.startswith("fallback") or not manifest.shots or manifest.transcript_backend != "faster-whisper":
        print(
            f"Index is not ready for this demo: encoder={manifest.encoder}, shots={len(manifest.shots)}, "
            f"ASR={manifest.transcript_backend}; faster-whisper is required",
            file=sys.stderr,
        )
        return 1
    print(f"Indexed {manifest.index_id}: encoder={manifest.encoder}, shots={len(manifest.shots)}, asr={manifest.transcript_backend}")

    print("Searching...")
    timeline = localize(manifest, args.prompt, encoder_mode="openclip")
    timeline_path = output_dir / "timeline.json"
    _write_json(timeline_path, timeline)
    if timeline.coverage != "source" or not timeline.items:
        print(f"Search abstained. Change --prompt so it matches the video. Wrote {timeline_path}", file=sys.stderr)
        return 1

    edited, trim_note = shorten_first_item(timeline)
    edited_path = output_dir / "timeline-edited.json"
    _write_json(edited_path, edited)
    print(trim_note)

    print("Rendering the trimmed timeline...")
    clip = render_timeline(edited, output_dir / "quickcut.mp4")

    print("Planning the story with Qwen and rendering captions plus narration...")
    story = build_basic_vlog(
        manifest,
        args.story_prompt,
        duration=args.duration,
        encoder_mode="openclip",
        planner="ollama",
    )
    story_path = output_dir / "basic-timeline.json"
    _write_json(story_path, story)
    try:
        planner = _verify_story_model(story, args.model)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    story_clip = render_story_timeline(story, output_dir / "basic-vlog.mp4", voice=args.voice)
    summary = {
        "demo_kind": "legacy_single_video_system_voice",
        "current_production_entry": "QuickCut.command",
        "voice_engine": "system",
        "voice": args.voice,
        "requested_model": args.model,
        "actual_model": story.story["inference"]["model"],
        "output_directory": str(output_dir),
        "index_id": manifest.index_id,
        "encoder": manifest.encoder,
        "transcript_backend": manifest.transcript_backend,
        "shots": len(manifest.shots),
        "search_coverage": timeline.coverage,
        "trim": trim_note,
        "clip": str(clip),
        "story_planner": planner,
        "story_clip": str(story_clip),
        "timeline": str(timeline_path),
        "edited_timeline": str(edited_path),
        "story_timeline": str(story_path),
    }
    (output_dir / "run-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

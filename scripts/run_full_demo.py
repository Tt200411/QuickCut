"""Run the local QuickCut path from one source video to both MP4s.

The script indexes with OpenCLIP and Whisper, localizes a prompt, trims the
first timeline item, renders that edit, then builds a Qwen story with captions
and local narration. From the repository root, with the project virtualenv active:

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
import time
import urllib.error
import urllib.request
from pathlib import Path

from quickcut.core import MIN_TIMELINE_SECONDS, build_index, localize, render_timeline, update_timeline
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


def _ollama_ready(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _ollama_has_model(base_url: str, model: str) -> bool:
    family = model.split(":", 1)[0]
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=5) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ollama is not answering at {base_url}: {exc}") from exc
    names = [str(item.get("name", "")) for item in payload.get("models", [])]
    return any(name == model or name.startswith(family + ":") for name in names)


def ensure_ollama(base_url: str, model: str) -> None:
    """Use a running Ollama, or start one and wait until the model is listed."""
    if not _ollama_ready(base_url):
        ollama = shutil.which("ollama")
        if not ollama:
            raise RuntimeError("Ollama is not running and the ollama command was not found")
        log_path = ROOT / "outputs" / "ollama-serve.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("ab")
        print(f"Starting Ollama. Logs: {log_path}")
        subprocess.Popen([ollama, "serve"], stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            if _ollama_ready(base_url):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"Ollama did not start within 60 seconds. See {log_path}")
    if not _ollama_has_model(base_url, model):
        ollama = shutil.which("ollama")
        if not ollama:
            raise RuntimeError(f"Model {model} is not installed and the ollama command was not found")
        print(f"Pulling {model}. The first download can take several minutes.")
        subprocess.run([ollama, "pull", model], check=True)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Index, search, trim, render, and build a Qwen story")
    parser.add_argument("--video", default=str(DEFAULT_VIDEO), help="One source video. Default: inputs/demo.mp4")
    parser.add_argument("--prompt", default="风景和人物，做成 15 秒", help="Retrieval prompt, including the desired length")
    parser.add_argument("--story-prompt", default="风景和人物", help="Prompt for the Qwen story")
    parser.add_argument("--duration", type=float, default=30.0, help="Story length in seconds")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="macOS say voice for narration")
    parser.add_argument("--cache-dir", default=str(ROOT / "cache"))
    parser.add_argument("--model", default="qwen2.5vl:3b")
    args = parser.parse_args()

    os.chdir(ROOT)
    os.environ.setdefault("QUICKCUT_ENCODER", "openclip")
    os.environ.setdefault("QUICKCUT_OLLAMA_URL", OLLAMA_URL)
    video = Path(args.video).expanduser()
    if not video.is_absolute():
        video = ROOT / video
    if not video.exists():
        print(f"Missing source video: {video}", file=sys.stderr)
        print("Put one MP4 at inputs/demo.mp4, or pass --video.", file=sys.stderr)
        return 1

    output_dir = ROOT / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    print("Checking Ollama...")
    ensure_ollama(os.environ["QUICKCUT_OLLAMA_URL"], args.model)

    print("Indexing. The first OpenCLIP and Whisper download is the slow part.")
    manifest = build_index(video, args.cache_dir, encoder_mode="openclip", asr_mode="auto")
    if manifest.encoder.startswith("fallback") or not manifest.shots:
        print(
            f"Index is not ready for this demo: encoder={manifest.encoder}, shots={len(manifest.shots)}",
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
    story_clip = render_story_timeline(story, output_dir / "basic-vlog.mp4", voice=args.voice)
    planner = str(story.story.get("planner", ""))
    summary = {
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
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not planner.startswith("ollama:"):
        print(f"Story planner was {planner or 'empty'}, expected ollama:{args.model}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

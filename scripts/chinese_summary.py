"""Execute a local, visually grounded Chinese summary with durable evidence.

Plan: python scripts/chinese_summary.py plan SOURCE --start 378 --end 493.5
Render: python scripts/chinese_summary.py render --cues editorial_cues.json
The reviewed source interval and optional editorial narration are explicit;
shot selection and order always come from the saved local Qwen response.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quickcut.core import build_index, load_manifest, render_timeline, validate_timeline, _require_binary
from quickcut.types import Timeline, TimelineItem
from quickcut.vlm import plan_with_ollama
from quickcut.voice import synthesize_timed_narration, write_srt, _apply_captions, _mix_narration


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def contact(video, times, path, labels=None, columns=4):
    from PIL import Image, ImageDraw
    width, height, label_height = 384, 216, 28
    sheet = Image.new("RGB", (width * columns, (height + label_height) * ((len(times) + columns - 1) // columns)), "#141b22")
    draw = ImageDraw.Draw(sheet)
    for i, t in enumerate(times):
        data = subprocess.run([
            _require_binary("ffmpeg"), "-v", "error", "-ss", str(t), "-i", str(video),
            "-frames:v", "1", "-vf", f"scale={width}:{height}", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        ], capture_output=True, check=True).stdout
        frame = Image.open(io.BytesIO(data))
        x, y = (i % columns) * width, (i // columns) * (height + label_height)
        sheet.paste(frame, (x, y))
        draw.text((x + 8, y + height + 7), labels[i] if labels else f"{t:.2f}s", fill="white")
    sheet.save(path, quality=94)


def plan(args, out):
    manifest = build_index(args.source, out / "fresh_index", window=3, stride=3,
                           asr_mode="faster-whisper", force=True)
    source_times = list(range(0, int(manifest.duration), 20))
    contact(manifest.source_video, source_times, out / "source_contact.jpg", columns=5)
    manifest.shots = [replace(s, end=min(s.end, args.end)) if args.start <= s.start < args.end else s
                      for s in manifest.shots]
    ids = [s.shot_id for s in manifest.shots if args.start <= s.start < args.end]
    save(out / "planning_manifest.json", manifest.to_dict())
    if args.stage == "index":
        print(json.dumps({"index_id": manifest.index_id, "real_keyframes": len(manifest.shots),
                          "candidate_count": len(ids), "asr": manifest.transcript_backend}))
        return
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else (
        "从真实接触表中选择二十个镜头，按源时间正序组成不超过六十秒的完整中文故事。"
        "三幕分别建立人物环境、行动和障碍、完成事件并收束。必须保留窗口后部的结局。"
        "中文旁白自然沉稳，约一百八十字，不得夹杂英文、编造事件、使用创作说明或选入片尾。"
    )
    story = plan_with_ollama(manifest, prompt, candidate_shot_ids=ids, duration=60, timeout=900, chronological=True)
    save(out / "planning_manifest.json", manifest.to_dict())
    save(out / "qwen_plan.json", story)
    print(json.dumps({"shot_order": story["shot_order"], "metrics": story["inference"]["metrics"]}, ensure_ascii=False))


def render(args, out):
    manifest = load_manifest(out / "planning_manifest.json")
    story = json.loads((out / "qwen_plan.json").read_text())
    by_id = {s.shot_id: s for s in manifest.shots}
    order = story["shot_order"]
    if not order or len(order) != len(set(order)):
        raise ValueError("Qwen must select distinct real shots")
    if not set(order) <= set(story["inference"]["grounded_shot_ids"]):
        raise ValueError("Every selected shot must have actually been shown to the local model")
    selected = [by_id[i] for i in order]
    if any(b.start < a.end - .001 for a, b in zip(selected, selected[1:])):
        raise ValueError("Qwen plan must be chronological and non-overlapping; replan instead of silently sorting")
    if any(s.end > args.credits_start for s in selected):
        raise ValueError("Plan crosses the reviewed end-credit boundary")
    items = [TimelineItem(f"item-{i:03d}", "source", s.shot_id, s.start, s.end, transcript=s.transcript)
             for i, s in enumerate(selected)]
    timeline = Timeline("1.0", manifest.source_video, story.get("theme", "林间故事"), 60,
                        manifest.index_id, "source", items=items, preset="clean", story=story)
    timeline.story["source_review"] = {"credits_start": args.credits_start,
        "candidate_start": min(by_id[i].start for i in story["inference"]["candidate_shot_ids"]),
        "candidate_end": max(by_id[i].end for i in story["inference"]["candidate_shot_ids"]),
        "fresh_keyframes": len(story["inference"]["grounded_shot_ids"]),
        "asr_backend": manifest.transcript_backend, "asr_sidecar": manifest.transcript_file}
    timeline.story["subtitle_style"] = "Fontname=PingFang SC,Fontsize=20,Outline=1.3,Shadow=0,MarginV=26,Alignment=2"
    if args.cues:
        editorial = json.loads(Path(args.cues).read_text())
        timeline.captions = editorial["captions"]
        timeline.narration = "".join(c["text"] for c in timeline.captions)
        timeline.story["qwen_narration"] = timeline.story.get("narration", "")
        timeline.story["narration"] = timeline.narration
        timeline.story["editorial_narration"] = {"reason": "对照实际画面润色中文并按动作对齐句子", "source_file": str(Path(args.cues).resolve())}
        if editorial.get("beats"):
            timeline.story["editorial_beats"] = editorial["beats"]
            timeline.story["qwen_beats"] = timeline.story["beats"]
            timeline.story["beats"] = editorial["beats"]
    else:
        timeline.narration = story["narration"]
    if re.search(r"[A-Za-z]", timeline.narration):
        raise ValueError("Chinese narration must not contain Latin words")
    validate_timeline(timeline)
    stem = out / "final_chinese_summary"
    wav = synthesize_timed_narration(timeline, out / "narration.wav", voice=args.voice, rate=args.rate)
    srt = write_srt(timeline, stem.with_suffix(".srt"))
    save(stem.with_suffix(".json"), timeline.to_dict())
    print("Narration and subtitles ready", flush=True)
    original = render_timeline(timeline, out / "original_only.mp4", preset="clean")
    print("Original audio reference ready", flush=True)
    # Keep the reference with exactly the soundtrack used by the final mix.
    captioned = out / "original_only_captioned.mp4"
    _apply_captions(original, timeline, captioned, srt)
    _mix_narration(captioned, wav, stem.with_suffix(".mp4"), timeline.duration)
    cursor = 0.0
    times, labels = [], []
    for i, item in enumerate(items):
        times.append(cursor + item.duration / 2)
        labels.append(f"{i+1:02d} | edit {cursor:.1f}s | source {item.start:.1f}-{item.end:.1f}s")
        cursor += item.duration
    contact(stem.with_suffix(".mp4"), times, out / "final_chinese_summary_contact.jpg", labels)
    print(str(stem.with_suffix(".mp4").resolve()), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["index", "plan", "render"])
    parser.add_argument("source", nargs="?")
    parser.add_argument("--output-dir", default="outputs/chinese_summary")
    parser.add_argument("--start", type=float, default=378)
    parser.add_argument("--end", type=float, default=493.5)
    parser.add_argument("--credits-start", type=float, default=494)
    parser.add_argument("--prompt-file")
    parser.add_argument("--cues")
    parser.add_argument("--voice", default="Reed (中文（中国大陆）)")
    parser.add_argument("--rate", type=int, default=145)
    args = parser.parse_args()
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (render if args.stage == "render" else plan)(args, out)


if __name__ == "__main__":
    main()

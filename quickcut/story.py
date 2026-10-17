from __future__ import annotations

"""Story and montage planning built on top of the cached retrieval index.

The planner is intentionally deterministic. It can be replaced by a local LLM
later, but a user should still get a coherent Basic Vlog export when no LLM or
VLM weights are installed.
"""

import math
from typing import Any

from .core import localize, validate_timeline
from .encoder import cosine
from .types import IndexManifest, Shot, Timeline, TimelineItem
from .vlm import plan_story


def _transcript_sentences(manifest: IndexManifest) -> list[str]:
    return [shot.transcript.strip() for shot in manifest.shots if shot.transcript.strip()]


def _prompt_words(prompt: str) -> list[str]:
    words = [word.strip(" ,.;:!?，。！？") for word in prompt.split()]
    return [word for word in words if len(word) > 1]


def write_story(manifest: IndexManifest, prompt: str, *, style: str = "vlog", planner: str = "deterministic") -> dict[str, Any]:
    """Turn cached transcript evidence and the prompt into a compact story plan.

    This is the local no-LLM baseline for Advanced: every sentence is grounded in
    the prompt or transcript, so a future Ollama/Qwen planner can replace this
    function without changing the Timeline contract.
    """
    if planner != "deterministic":
        generated = plan_story(manifest, prompt, mode=planner)
        if generated:
            generated.setdefault("style", style)
            return generated
    transcript = _transcript_sentences(manifest)
    words = _prompt_words(prompt)
    theme = " ".join(words[:8]) or "this journey"
    if transcript:
        evidence = " ".join(transcript[:3])
        narration = f"We set out with {theme}. {evidence}. The journey keeps moving, one moment at a time."
    else:
        narration = f"We set out with {theme}. The first light opens the journey, the middle gathers momentum, and the final view lets the moment breathe."
    beats = [
        {"id": "hook", "label": "Open with a hook", "text": f"A striking first glimpse of {theme}.", "source": "prompt"},
        {"id": "journey", "label": "Build the journey", "text": "Movement, people, and changing viewpoints carry the middle.", "source": "transcript" if transcript else "prompt"},
        {"id": "close", "label": "Close on atmosphere", "text": "Let the final scenic frame hold for a breath.", "source": "source"},
    ]
    return {"style": style, "theme": theme, "beats": beats, "narration": narration, "grounded_transcript": bool(transcript)}


def _fallback_items(manifest: IndexManifest, duration: float) -> list[TimelineItem]:
    target_count = min(len(manifest.shots), max(3, math.ceil(duration / max(manifest.window, 0.1))))
    if target_count == 0:
        return []
    positions = [round(i * (len(manifest.shots) - 1) / max(1, target_count - 1)) for i in range(target_count)]
    items: list[TimelineItem] = []
    per_item = duration / target_count
    for idx, position in enumerate(positions):
        shot = manifest.shots[position]
        length = min(shot.duration, per_item)
        center = (shot.start + shot.end) / 2
        start = max(shot.start, center - length / 2)
        items.append(TimelineItem(item_id=f"item-{idx:03d}", provenance="source", asset_id=shot.shot_id,
                                  start=round(start, 3), end=round(start + length, 3),
                                  transcript=shot.transcript, score=0.0,
                                  license_note="Source video provided by user"))
    return items


def _reorder_montage(items: list[TimelineItem], manifest: IndexManifest, *, close_on_atmosphere: bool = True) -> list[TimelineItem]:
    if len(items) < 3:
        return items
    hook = max(items, key=lambda item: item.score)
    remaining = [item for item in items if item.item_id != hook.item_id]
    shots = {shot.shot_id: shot for shot in manifest.shots}
    ordered: list[TimelineItem] = [hook]
    # Greedily pick the next shot with a different visual embedding. This
    # creates a real change of viewpoint when CLIP is enabled and still falls
    # back to source order for the no-weight histogram path.
    while remaining and len(ordered) < len(items) - (1 if close_on_atmosphere else 0):
        previous = shots.get(ordered[-1].asset_id)
        if previous and previous.embedding:
            candidate = max(
                remaining,
                key=lambda item: 1.0 - cosine(previous.embedding, shots.get(item.asset_id, previous).embedding)
                if shots.get(item.asset_id, previous).embedding else 0.0,
            )
        else:
            candidate = min(remaining, key=lambda item: item.start)
        ordered.append(candidate)
        remaining = [item for item in remaining if item.item_id != candidate.item_id]
    if close_on_atmosphere:
        close = max(remaining, key=lambda item: item.end)
        return [*ordered, close]
    return ordered


def _captions(items: list[TimelineItem], beats: list[dict[str, Any]], prompt: str) -> list[dict[str, Any]]:
    captions: list[dict[str, Any]] = []
    cursor = 0.0
    for idx, item in enumerate(items):
        text = item.transcript or (beats[idx % len(beats)]["text"] if beats else prompt)
        captions.append({"start": round(cursor, 3), "end": round(cursor + item.duration, 3), "text": text})
        cursor += item.duration
    return captions


def build_basic_vlog(manifest: IndexManifest, prompt: str, *, duration: float = 15.0, encoder_mode: str | None = None, planner: str = "deterministic") -> Timeline:
    """Create a rhythmic, source-grounded Vlog opening from one indexed video."""
    duration = min(15.0, max(5.0, float(duration)))
    story = write_story(manifest, prompt, style="basic-vlog", planner=planner)
    timeline = localize(manifest, f"{prompt} {duration:g} seconds", encoder_mode=encoder_mode, top_k=5)
    if timeline.coverage == "abstain":
        items = _fallback_items(manifest, duration)
        if not items:
            return timeline
        timeline = Timeline(version="1.0", source_video=manifest.source_video, query=prompt, duration_budget=duration,
                            index_id=manifest.index_id, coverage="source", items=items,
                            message="No prompt candidate cleared the threshold; composed an evenly spaced source montage.")
    timeline.items = _reorder_montage(timeline.items, manifest)
    timeline.story = story
    timeline.narration = story["narration"]
    timeline.captions = _captions(timeline.items, story["beats"], prompt)
    timeline.preset = "warm"
    validate_timeline(timeline)
    return timeline

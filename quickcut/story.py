from __future__ import annotations

"""Story and montage planning built on top of the cached retrieval index.

The deterministic planner is always available. When requested, a local
Ollama/Qwen planner chooses and orders a small, retrieval-grounded candidate
pool; its validated source-shot order is written directly to the Timeline.
"""

import math
import os
from typing import Any

from .core import _probable_credit_shot, localize, validate_timeline
from .encoder import cosine
from .types import IndexManifest, Shot, Timeline, TimelineItem
from .vlm import plan_story


def _transcript_sentences(manifest: IndexManifest) -> list[str]:
    return [shot.transcript.strip() for shot in manifest.shots if shot.transcript.strip()]


def _prompt_words(prompt: str) -> list[str]:
    words = [word.strip(" ,.;:!?，。！？") for word in prompt.split()]
    return [word for word in words if len(word) > 1]


def write_story(
    manifest: IndexManifest,
    prompt: str,
    *,
    style: str = "vlog",
    planner: str = "deterministic",
    candidate_shot_ids: set[str] | list[str] | None = None,
) -> dict[str, Any]:
    """Turn cached transcript evidence and the prompt into a compact story plan.

    The deterministic path grounds every sentence in the prompt or transcript.
    The optional Ollama/Qwen path adds a whitelisted ``shot_order`` and
    per-beat source-shot IDs without changing the Timeline contract.
    """
    if planner != "deterministic":
        generated = plan_story(manifest, prompt, mode=planner, candidate_shot_ids=candidate_shot_ids)
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
    return {"style": style, "theme": theme, "beats": beats, "narration": narration,
            "grounded_transcript": bool(transcript), "shot_selection": False,
            "shot_order": [], "selected_shot_ids": []}


def _qwen_items(
    manifest: IndexManifest,
    story: dict[str, Any],
    duration: float,
    *,
    avoid_credits: bool = True,
) -> list[TimelineItem]:
    """Build editable timeline items from Qwen's validated shot order.

    The order is intentionally taken from the model plan, rather than sorted
    by source timestamp or retrieval score.  Only IDs present in the index can
    become timeline items; malformed or stale IDs simply disappear.  An empty
    or too-short plan lets the deterministic retrieval path take over.
    """
    source_ids = story.get("shot_order") or story.get("selected_shot_ids") or []
    if not isinstance(source_ids, list):
        return []
    shots = {shot.shot_id: shot for shot in manifest.shots}
    ordered: list[Shot] = []
    seen: set[str] = set()
    for source_id in source_ids:
        shot_id = str(source_id)
        shot = shots.get(shot_id)
        if shot is None or shot_id in seen:
            continue
        if avoid_credits and _probable_credit_shot(manifest, shot, story.get("theme", "")):
            continue
        seen.add(shot_id)
        ordered.append(shot)
    if not ordered:
        return []

    per_item = duration / len(ordered)
    items: list[TimelineItem] = []
    remaining = duration
    beats = story.get("beats") if isinstance(story.get("beats"), list) else []
    beat_by_shot: dict[str, int] = {}
    for beat_idx, beat in enumerate(beats):
        if not isinstance(beat, dict):
            continue
        source_ids = beat.get("source_shot_ids")
        if isinstance(source_ids, list):
            for source_id in source_ids:
                beat_by_shot.setdefault(str(source_id), beat_idx)
    for idx, shot in enumerate(ordered):
        length = min(shot.duration, per_item, remaining)
        if length <= 0.1:
            continue
        center = (shot.start + shot.end) / 2
        start = max(shot.start, center - length / 2)
        end = min(shot.end, start + length)
        beat_text = ""
        if beats:
            beat_idx = beat_by_shot.get(shot.shot_id, min(idx, len(beats) - 1))
            beat = beats[beat_idx]
            if isinstance(beat, dict):
                beat_text = str(beat.get("text") or "")
        items.append(TimelineItem(
            item_id=f"item-{idx:03d}", provenance="source", asset_id=shot.shot_id,
            start=round(start, 3), end=round(end, 3), score=1.0,
            transcript=shot.transcript or beat_text,
            license_note="Source video provided by user",
        ))
        remaining -= end - start
        if remaining <= 0.1:
            break
    # A one-shot plan cannot satisfy the minimum source montage duration when
    # index windows are short.  Fall back to retrieval in that case rather
    # than silently emitting an invalid timeline.
    if sum(item.duration for item in items) < 5.0:
        return []
    return items


def _pad_qwen_items(
    selected: list[TimelineItem],
    fallback: list[TimelineItem],
    duration: float,
) -> list[TimelineItem]:
    """Fill a short Qwen selection with retrieval candidates.

    Qwen owns the opening order.  Small local models sometimes choose only
    three 3-second windows for a 15-second request; padding keeps the requested
    duration without inventing frames, while leaving the model-selected
    prefix intact and recording those source IDs in the timeline.
    """
    result = list(selected)
    used = {item.asset_id for item in result}
    remaining = duration - sum(item.duration for item in result)
    for candidate in fallback:
        if remaining <= 0.1:
            break
        if candidate.asset_id in used:
            continue
        length = min(candidate.duration, remaining)
        if length <= 0.1:
            continue
        result.append(TimelineItem(
            item_id=f"item-{len(result):03d}", provenance=candidate.provenance,
            asset_id=candidate.asset_id, start=candidate.start,
            end=round(candidate.start + length, 3),
            license_note=candidate.license_note, score=candidate.score,
            transcript=candidate.transcript,
        ))
        used.add(candidate.asset_id)
        remaining -= length
    return result


def _candidate_shot_ids(manifest: IndexManifest, timeline: Timeline) -> list[str]:
    """Build a small, relevant visual pool for the local VLM.

    The coarse retriever narrows a long source first. Qwen then sees those
    windows plus a few adjacent alternatives instead of eight evenly spaced
    frames from the entire movie. This keeps image grounding relevant while
    still giving the model real choices to replace and reorder.
    """
    try:
        limit = max(3, int(os.getenv("QUICKCUT_VLM_MAX_FRAMES", "8")))
    except ValueError:
        limit = 8
    by_id = {shot.shot_id: (idx, shot) for idx, shot in enumerate(manifest.shots)}
    selected = [by_id[item.asset_id] for item in timeline.items if item.asset_id in by_id]
    result: list[str] = []
    seen: set[str] = set()

    def add(shot: Shot) -> None:
        if shot.shot_id in seen or _probable_credit_shot(manifest, shot, timeline.query):
            return
        seen.add(shot.shot_id)
        result.append(shot.shot_id)

    for _idx, shot in selected:
        add(shot)
    for idx, _shot in selected:
        for neighbor in (idx - 1, idx + 1):
            if 0 <= neighbor < len(manifest.shots):
                add(manifest.shots[neighbor])
            if len(result) >= limit:
                return result[:limit]
    return result[:limit]


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
    # A Basic opening should not jump into end-credit rolls simply because
    # their text-heavy frames match a broad visual prompt. Explicit credits
    # requests still pass through via localize's query opt-in.
    timeline = localize(manifest, f"{prompt} {duration:g} seconds", encoder_mode=encoder_mode,
                        top_k=5, avoid_credits=True)
    if timeline.coverage == "abstain":
        items = _fallback_items(manifest, duration)
        if not items:
            return timeline
        timeline = Timeline(version="1.0", source_video=manifest.source_video, query=prompt, duration_budget=duration,
                            index_id=manifest.index_id, coverage="source", items=items,
                            message="No prompt candidate cleared the threshold; composed an evenly spaced source montage.")
    # Give Qwen only a relevant, legal source pool. The VLM still decides
    # which of these windows appear and in what order.
    candidate_ids = _candidate_shot_ids(manifest, timeline)
    story = write_story(manifest, prompt, style="basic-vlog", planner=planner,
                        candidate_shot_ids=candidate_ids)
    qwen_items = _qwen_items(manifest, story, duration, avoid_credits=True) if story.get("shot_selection") else []
    if qwen_items:
        timeline.items = _pad_qwen_items(qwen_items, timeline.items, duration)
        selected_count = len(qwen_items)
        padded_count = len(timeline.items) - selected_count
        suffix = f"; padded with {padded_count} retrieval segment(s)" if padded_count else ""
        timeline.message = f"Qwen selected and ordered {selected_count} source segment(s) from the indexed candidate pool{suffix}."
    else:
        timeline.items = _reorder_montage(timeline.items, manifest)
    timeline.story = story
    timeline.narration = story["narration"]
    timeline.captions = _captions(timeline.items, story["beats"], prompt)
    timeline.preset = "warm"
    validate_timeline(timeline)
    return timeline

from __future__ import annotations

"""Story and montage planning built on top of the cached retrieval index.

The deterministic planner is always available. When requested, a local
Ollama/Qwen planner chooses and orders a small, retrieval-grounded candidate
pool; its validated source-shot order is written directly to the Timeline.
"""

import math
import os
import re
from typing import Any

from .core import (
    DEFAULT_TIMELINE_SECONDS,
    MAX_TIMELINE_SECONDS,
    MIN_TIMELINE_SECONDS,
    _probable_credit_shot,
    localize,
    validate_timeline,
)
from .encoder import cosine
from .types import IndexManifest, Shot, Timeline, TimelineItem
from .vlm import plan_story


def _transcript_sentences(manifest: IndexManifest) -> list[str]:
    return [shot.transcript.strip() for shot in manifest.shots if shot.transcript.strip()]


def _prompt_words(prompt: str) -> list[str]:
    words = [word.strip(" ,.;:!?，。！？") for word in prompt.split()]
    return [word for word in words if len(word) > 1]


def _narration_word_count(text: str) -> int:
    """Count spoken words while keeping the helper friendly to CJK text."""
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*|[\u4e00-\u9fff]", text or ""))


def _complete_narration(story: dict[str, Any], prompt: str, duration: float | None) -> str:
    """Ensure long edits have one readable narration arc across all beats.

    Qwen2.5-VL can satisfy the JSON schema with a very short one-line
    narration. For a 60-second edit that leaves the middle and resolution
    unexplained, so construct a conservative spoken bridge from the model's
    three beat texts. The bridge describes editorial progression without
    inventing events that are absent from the source evidence.
    """
    current = str(story.get("narration") or "").strip()
    beats = story.get("beats") if isinstance(story.get("beats"), list) else []
    texts = [str(beat.get("text") or "").strip() for beat in beats[:3] if isinstance(beat, dict)]
    texts += ["", "", ""]
    theme = str(story.get("theme") or prompt).strip(" ,.;:!?，。！？") or "this journey"
    theme = " ".join(theme.split()[:12])
    def clip_spoken(text: str, limit: int = 14) -> str:
        # Prefer complete model sentences. A raw word slice can end with an
        # article ("a") or conjunction ("and"), which sounds broken in TTS.
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
        chosen: list[str] = []
        for sentence in sentences:
            # Never cut a complete model sentence merely to hit the soft word
            # budget; a clipped clause such as "and a" makes the local TTS
            # narration sound broken and loses the event's meaning.
            if not chosen or _narration_word_count(" ".join(chosen + [sentence])) <= limit:
                chosen.append(sentence)
            else:
                break
        if chosen:
            return " ".join(chosen).strip(" ,.;:!?，。！？") + "."
        words = text.split()
        clipped_words = words[:limit]
        while clipped_words and clipped_words[-1].lower().strip(" ,.;:!?，。！？") in {
            "a", "an", "the", "and", "or", "to", "of", "with", "they", "that", "this",
        }:
            clipped_words.pop()
        clipped = " ".join(clipped_words).strip(" ,.;:!?，。！？")
        return f"{clipped}." if clipped else "The selected images carry the story forward."

    structured = (
        f"First, we enter {theme}. {clip_spoken(texts[0], 18)} This opening sets the scene. "
        f"Next, the journey develops. {clip_spoken(texts[1], 18)} The middle shows the change and keeps the thread moving. "
        f"Finally, the story settles. {clip_spoken(texts[2], 18)} The ending resolves the moment and gives the viewer time to understand it."
    )
    # A model narration that already covers the three beats is preferable;
    # otherwise keep its useful prose after the explicit three-act bridge.
    current_lower = current.lower()
    beat_markers = [" ".join(text.split()[:4]).lower() for text in texts[:3] if text]
    malformed = bool(
        re.search(r"[,.;:!?]\s*[,.;:!?]", current)
        or re.search(r"\b(?:a|an|the|and|or|to|of|with|by|in)\s*[.!?]$", current, flags=re.I)
    )
    covered = not malformed and len(beat_markers) == 3 and all(marker in current_lower for marker in beat_markers)
    try:
        target = max(24, int(math.ceil(float(duration) * 1.5))) if duration is not None else 0
    except (TypeError, ValueError):
        target = 0
    # Keep speech inside a one-minute edit. At a calm 90 words/minute the
    # requested 60-second floor is about 90 words; allowing a short margin
    # avoids cutting the final beat when the local TTS track is mixed.
    maximum = target + 24 if target else 0
    result = current if covered and (not maximum or _narration_word_count(current) <= maximum) else structured
    if target and _narration_word_count(result) < target:
        result = f"{result} The pacing remains purposeful: the opening establishes context, the middle shows meaningful change, and the closing image gives the viewer time to understand the result."
    # The template above is deliberately long enough for the 60-second
    # target in normal English. If a very short/CJK prompt still falls below
    # the requested word floor, add one final neutral sentence rather than
    # repeating a shot description mechanically.
    while target and _narration_word_count(result) < target:
        result = f"{result} Each transition stays connected to the same story, with a clear beginning, development, and ending."
    return result


def write_story(
    manifest: IndexManifest,
    prompt: str,
    *,
    style: str = "vlog",
    planner: str = "deterministic",
    candidate_shot_ids: set[str] | list[str] | None = None,
    duration: float | None = None,
) -> dict[str, Any]:
    """Turn cached transcript evidence and the prompt into a compact story plan.

    The deterministic path grounds every sentence in the prompt or transcript.
    The optional Ollama/Qwen path adds a whitelisted ``shot_order`` and
    per-beat source-shot IDs without changing the Timeline contract.
    """
    if planner != "deterministic":
        generated = plan_story(manifest, prompt, mode=planner, candidate_shot_ids=candidate_shot_ids,
                               duration=duration)
        if generated:
            generated.setdefault("style", style)
            return _ensure_story_structure(generated, prompt, duration=duration)
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
    fallback = {"style": style, "theme": theme, "beats": beats, "narration": narration,
                "grounded_transcript": bool(transcript), "shot_selection": False,
                "shot_order": [], "selected_shot_ids": [],
                "duration_seconds": float(duration) if duration is not None else None}
    return _ensure_story_structure(fallback, prompt, duration=duration)


def _ensure_story_structure(story: dict[str, Any], prompt: str, *, duration: float | None = None) -> dict[str, Any]:
    """Make the model response executable as a complete three-act story.

    Small local VLMs occasionally return one beat, omit a label, or mention a
    selected shot only at the top level. Preserve their prose, then fill the
    missing hook/journey/close structure and partition the validated order so
    every selected source window belongs to one narrated beat. This keeps a
    long (up to one minute) edit from becoming an unstructured caption loop.
    """
    result = dict(story)
    raw_beats = result.get("beats") if isinstance(result.get("beats"), list) else []
    beats: list[dict[str, Any]] = []
    defaults = (
        ("hook", "Open with the clearest moment that sets the scene.", "prompt"),
        ("journey", "Follow the change in action and viewpoint through the middle.", "source"),
        ("close", "Resolve the moment with a final image that lets the story settle.", "source"),
    )
    for idx, (beat_id, default_text, default_source) in enumerate(defaults):
        candidate = raw_beats[idx] if idx < len(raw_beats) and isinstance(raw_beats[idx], dict) else {}
        beats.append({
            "id": str(candidate.get("id") or beat_id),
            "label": str(candidate.get("label") or beat_id.title()),
            "text": str(candidate.get("text") or default_text),
            "source": str(candidate.get("source") or default_source),
            "source_shot_ids": [str(value) for value in candidate.get("source_shot_ids", [])
                                if str(value).strip()],
        })
    order = result.get("shot_order") or result.get("selected_shot_ids") or []
    if not isinstance(order, list):
        order = []
    order = list(dict.fromkeys(str(value) for value in order if str(value).strip()))
    # If a response only references source IDs inside beats, recover that
    # order before assigning the three act ranges.
    if not order:
        for beat in beats:
            for shot_id in beat["source_shot_ids"]:
                if shot_id not in order:
                    order.append(shot_id)
    referenced = [shot_id for beat in beats for shot_id in beat["source_shot_ids"]]
    if order and (set(referenced) != set(order) or len(referenced) != len(set(referenced))):
        base, extra = divmod(len(order), len(beats))
        cursor = 0
        for idx, beat in enumerate(beats):
            size = base + (1 if idx < extra else 0)
            beat["source_shot_ids"] = order[cursor:cursor + size]
            cursor += size
    result["beats"] = beats
    result["shot_order"] = order
    result["selected_shot_ids"] = list(order)
    result["shot_selection"] = bool(order) and bool(result.get("shot_selection", True))
    result.setdefault("theme", prompt)
    result.setdefault("narration", f"We set out with {prompt}, follow the change, and let the final moment settle.")
    result["narration"] = _complete_narration(result, prompt, duration)
    result["duration_seconds"] = float(duration) if duration is not None else result.get("duration_seconds")
    return result


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
    a few 3-second windows for a longer request; padding keeps the requested
    duration without inventing frames, while leaving the model-selected
    prefix intact and recording those source IDs in the timeline.  This lets
    a small local VLM choose the story beats while the retriever supplies
    enough source coverage to explain the event within the one-minute cap.
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
        # Keyframe count controls visual evidence, while the candidate count
        # scales with requested duration so a 60s story can cover enough
        # distinct source windows. An explicit override is useful on small
        # machines with a very tight Ollama context.
        configured = int(os.getenv("QUICKCUT_VLM_CANDIDATE_SHOTS", "0"))
        if configured > 0:
            limit = max(3, configured)
        else:
            target = math.ceil(timeline.duration_budget / max(manifest.window, 0.1)) + 3
            limit = max(8, target)
        limit = min(32, limit)
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


def _fallback_items(manifest: IndexManifest, duration: float, query: str = "") -> list[TimelineItem]:
    # The fallback is still a Basic montage, so apply the same end-credit
    # guard as scored retrieval. Otherwise a low-confidence 60-second request
    # could reintroduce the tail card that the normal path filtered out.
    shots = [shot for shot in manifest.shots if not _probable_credit_shot(manifest, shot, query)]
    target_count = min(len(shots), max(3, math.ceil(duration / max(manifest.window, 0.1))))
    if target_count == 0:
        return []
    positions = [round(i * (len(shots) - 1) / max(1, target_count - 1)) for i in range(target_count)]
    items: list[TimelineItem] = []
    per_item = duration / target_count
    for idx, position in enumerate(positions):
        shot = shots[position]
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
    beat_sentences: list[list[str]] = []
    for beat in beats:
        raw_text = str(beat.get("text") or "") if isinstance(beat, dict) else ""
        # Split long model prose into readable caption-sized clauses. A full
        # beat paragraph repeated over every three-second shot becomes a wall
        # of text in a 60-second export.
        sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+|[,，;；]\s*", raw_text) if part.strip()]
        beat_sentences.append(sentences or [raw_text or prompt])
    captions: list[dict[str, Any]] = []
    cursor = 0.0
    beat_offsets = [0] * len(beat_sentences)
    beat_texts = {str(beat.get("text") or "").strip() for beat in beats if isinstance(beat, dict)}
    for idx, item in enumerate(items):
        # Keep generated beat text grouped into hook/journey/close ranges for
        # long deterministic stories. Cycling beats every shot makes a
        # one-minute montage read like a loop instead of a resolved event.
        if item.transcript and item.transcript.strip() not in beat_texts:
            text = item.transcript
        elif beats:
            beat_idx = min(len(beats) - 1, int(idx * len(beats) / max(1, len(items))))
            sentences = beat_sentences[beat_idx]
            text = sentences[beat_offsets[beat_idx] % len(sentences)]
            beat_offsets[beat_idx] += 1
        else:
            text = prompt
        captions.append({"start": round(cursor, 3), "end": round(cursor + item.duration, 3), "text": text})
        cursor += item.duration
    return captions


def build_basic_vlog(
    manifest: IndexManifest,
    prompt: str,
    *,
    duration: float = DEFAULT_TIMELINE_SECONDS,
    encoder_mode: str | None = None,
    planner: str = "deterministic",
) -> Timeline:
    """Create a complete, source-grounded story of up to one minute.

    Longer requests allocate enough source windows for the full budget and
    preserve the model's hook→journey→close structure.  The upper bound is
    shared with :func:`quickcut.core.validate_timeline` so every entry point
    rejects the same invalid duration.
    """
    duration = min(MAX_TIMELINE_SECONDS, max(MIN_TIMELINE_SECONDS, float(duration)))
    # A Basic opening should not jump into end-credit rolls simply because
    # their text-heavy frames match a broad visual prompt. Explicit credits
    # requests still pass through via localize's query opt-in.
    # A one-minute request needs a larger coarse retrieval pool than the old
    # five-shot opening.  Keep the pool proportional to the source window so
    # Qwen can choose enough distinct source moments to cover the full budget.
    retrieval_top_k = min(
        len(manifest.shots),
        max(5, math.ceil(duration / max(manifest.window, 0.1)) + 1),
    )
    timeline = localize(manifest, f"{prompt} {duration:g} seconds", encoder_mode=encoder_mode,
                        top_k=retrieval_top_k, avoid_credits=True)
    if timeline.coverage == "abstain":
        items = _fallback_items(manifest, duration, prompt)
        if not items:
            return timeline
        timeline = Timeline(version="1.0", source_video=manifest.source_video, query=prompt, duration_budget=duration,
                            index_id=manifest.index_id, coverage="source", items=items,
                            message="No prompt candidate cleared the threshold; composed an evenly spaced source montage.")
    # Give Qwen only a relevant, legal source pool. The VLM still decides
    # which of these windows appear and in what order.
    candidate_ids = _candidate_shot_ids(manifest, timeline)
    story = write_story(manifest, prompt, style="basic-vlog", planner=planner,
                        candidate_shot_ids=candidate_ids, duration=duration)
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

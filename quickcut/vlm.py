from __future__ import annotations

"""Optional local story planner adapters.

The Basic path never requires a VLM. When Ollama is running locally, this
module can turn transcript and shot metadata into a richer beat plan while
keeping the same JSON contract. No remote API is used.
"""

import json
import os
import base64
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener, urlopen

from .core import DEFAULT_TIMELINE_SECONDS, MAX_TIMELINE_SECONDS, MIN_TIMELINE_SECONDS
from .types import IndexManifest


def _shot_id_values(value: object) -> list[str]:
    """Normalize the several compact forms Qwen tends to emit for shot IDs."""
    if isinstance(value, str):
        # Accept a single ID as well as a comma/newline separated list.  The
        # whitelist check in ``normalize_shot_plan`` remains authoritative.
        return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def normalize_shot_plan(raw: object, *, allowed_shot_ids: set[str], max_beats: int = 3) -> dict:
    """Normalize and validate a Qwen source-shot plan.

    A VLM is allowed to describe a story, but it must select from the indexed
    source windows.  Unknown IDs are dropped before they can reach the
    timeline renderer.  ``source_shot_ids`` is the canonical beat field and
    ``shot_order`` is the canonical flattened order consumed by story.py.
    """
    if not isinstance(raw, dict):
        raise ValueError("Local VLM returned a non-object story plan")
    raw = dict(raw)
    raw_beats = raw.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        raise ValueError("Local VLM returned an invalid story plan")

    beats: list[dict] = []
    flattened: list[str] = []
    seen: set[str] = set()
    for beat in raw_beats[:max_beats]:
        if not isinstance(beat, dict):
            continue
        # Different Qwen checkpoints use ``shot_ids``, ``shots`` or a scalar
        # ``shot_id`` despite the requested schema.  Normalize all of them.
        source_ids: list[str] = []
        for key in ("source_shot_ids", "shot_ids", "source_shots", "shot_id", "source_shot_id"):
            source_ids.extend(_shot_id_values(beat.get(key)))
        valid_ids: list[str] = []
        for shot_id in source_ids:
            if shot_id in allowed_shot_ids and shot_id not in valid_ids:
                valid_ids.append(shot_id)
        for shot_id in valid_ids:
            if shot_id not in seen:
                flattened.append(shot_id)
                seen.add(shot_id)
        beats.append({
            "id": str(beat.get("id") or "journey"),
            "label": str(beat.get("label") or "Continue the journey"),
            "text": str(beat.get("text") or ""),
            "source": str(beat.get("source") or "source"),
            "source_shot_ids": valid_ids,
        })
    if not beats:
        raise ValueError("Local VLM returned no usable beats")

    # Prefer an explicit top-level order when present, while still applying
    # the same whitelist and de-duplication.  This lets Qwen express a
    # cross-beat montage order directly rather than relying on source order.
    explicit_order = _shot_id_values(raw.get("shot_order") or raw.get("selected_shot_ids"))
    if explicit_order:
        explicit_valid: list[str] = []
        for shot_id in explicit_order:
            if shot_id in allowed_shot_ids and shot_id not in explicit_valid:
                explicit_valid.append(shot_id)
        # If the model supplied an unusable order, retain valid beat-level
        # choices rather than letting one hallucinated ID erase the plan.
        if explicit_valid:
            # A few Qwen responses put the hook and journey in ``shot_order``
            # but mention the close shots only inside the final beat. Preserve
            # the model's explicit order first, then append those legal beat
            # choices so every narrated beat has a visible source segment.
            flattened = explicit_valid + [shot_id for shot_id in flattened if shot_id not in explicit_valid]
        elif not flattened:
            flattened = [
                shot_id for shot_id in _shot_id_values(raw.get("source_shot_ids"))
                if shot_id in allowed_shot_ids
            ]

    # Keep the returned story structure executable. Small VLMs occasionally
    # repeat the hook IDs in the close beat or omit one of the ordered shots.
    # Preserve the model's order and prose, but assign the ordered source
    # windows to contiguous beats when its beat references are inconsistent.
    referenced_ids = [shot_id for beat in beats for shot_id in beat["source_shot_ids"]]
    if len(beats) >= 2 and flattened and (
        len(referenced_ids) != len(set(referenced_ids))
        or set(referenced_ids) != set(flattened)
    ):
        base, extra = divmod(len(flattened), len(beats))
        cursor = 0
        for idx, beat in enumerate(beats):
            size = base + (1 if idx < extra else 0)
            beat["source_shot_ids"] = flattened[cursor:cursor + size]
            cursor += size

    raw["beats"] = beats
    raw["shot_order"] = flattened
    raw["selected_shot_ids"] = list(flattened)
    raw["shot_selection"] = bool(flattened)
    return raw


def _open_ollama(request: Request, *, timeout: float, base_url: str):
    # The desktop environment may expose a system HTTP proxy that is not
    # reachable from the local runtime. Ollama is a localhost service, so
    # bypass proxies for loopback requests while preserving normal behavior
    # for an explicitly configured remote endpoint.
    hostname = urlsplit(base_url).hostname
    if hostname in {"127.0.0.1", "localhost", "::1"}:
        return build_opener(ProxyHandler({})).open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


def plan_with_ollama(
    manifest: IndexManifest,
    prompt: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 30.0,
    candidate_shot_ids: set[str] | list[str] | None = None,
    duration: float | None = None,
) -> dict:
    model = model or os.getenv("QUICKCUT_VLM_MODEL", "qwen2.5vl:3b")
    base_url = (base_url or os.getenv("QUICKCUT_OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
    allowed_ids = set(candidate_shot_ids) if candidate_shot_ids is not None else {shot.shot_id for shot in manifest.shots}
    available_shots = [shot for shot in manifest.shots if shot.shot_id in allowed_ids]
    if not available_shots:
        raise ValueError("Local VLM has no valid source-shot candidates")
    evidence = [
        {"shot_id": shot.shot_id, "start": shot.start, "end": shot.end, "transcript": shot.transcript,
         "image_available": bool(shot.keyframe)}
        for shot in available_shots
    ]
    try:
        # Four frames keep Qwen2.5-VL inside its small CPU context while still
        # giving it a wide temporal sample of a long source video. Increase
        # QUICKCUT_VLM_MAX_FRAMES only when using a larger local context.
        max_frames = max(1, int(os.getenv("QUICKCUT_VLM_MAX_FRAMES", "4")))
    except ValueError:
        max_frames = 4
    candidates = available_shots[::max(1, len(available_shots) // max_frames)][:max_frames]
    candidate_ids = {shot.shot_id for shot in candidates}
    # Keep the metadata for the complete candidate pool. Images are sampled
    # separately above, but a one-minute story needs more than four legal IDs
    # to cover its source windows without repeating the same shot. The cap is
    # intentionally modest so the local model's context remains bounded.
    try:
        requested_duration = (
            max(MIN_TIMELINE_SECONDS, min(MAX_TIMELINE_SECONDS, float(duration)))
            if duration is not None else DEFAULT_TIMELINE_SECONDS
        )
    except (TypeError, ValueError):
        requested_duration = DEFAULT_TIMELINE_SECONDS
    typical_window = sorted(max(0.1, shot.duration) for shot in available_shots)[len(available_shots) // 2]
    target_shots = max(3, min(len(available_shots), int((requested_duration + typical_window - 0.01) // typical_window)))
    evidence_limit = max(max_frames * 3, target_shots)
    # Keep every image-bearing sample in the legal evidence set even when the
    # candidate pool is ordered by source time. A long-video pool may sample
    # the last shot outside the first ``evidence_limit`` records; dropping it
    # here would make its keyframe visible to Qwen but its ID illegal in the
    # response schema.
    visual_evidence = [entry for entry in evidence if entry["shot_id"] in candidate_ids]
    metadata_evidence = [entry for entry in evidence if entry["shot_id"] not in candidate_ids]
    evidence = (visual_evidence + metadata_evidence)[:evidence_limit]
    evidence_ids = {shot["shot_id"] for shot in evidence}
    chinese_requested = bool(re.search(r"[\u4e00-\u9fff]", prompt))
    language_instruction = (
        "Respond entirely in Simplified Chinese: theme, narration, beat labels, and beat text must all be Chinese."
        if chinese_requested else
        "Respond in the language used by the prompt."
    )
    images: list[str] = []
    image_slots: list[str] = []
    for shot in candidates:
        if not shot.keyframe:
            continue
        try:
            image = base64.b64encode(Path(shot.keyframe).read_bytes()).decode("ascii")
        except OSError:
            continue
        images.append(image)
        image_slots.append(shot.shot_id)
    instruction = {
        "prompt": prompt,
        "shots": evidence,
        "image_slots": image_slots,
        "duration_seconds": requested_duration,
        "target_shot_count": target_shots,
        "task": f"Act as the video editor. {language_instruction} Build a complete story for about {requested_duration:g} seconds. Choose enough distinct source shots to cover that duration (target {target_shots}) and put them in a purposeful montage order. Then write exactly three beats in hook, journey, close order. Write a coherent local voiceover with at least {max(24, min(110, int(requested_duration * 1.5)))} words or Chinese characters, covering the hook, journey, and close instead of a single summary sentence. You MUST copy exact shot_id strings from shots into shot_order and each beat's source_shot_ids. Every beat source_shot_id must also appear in shot_order. Do not leave these arrays empty or repeat a shot. Use different shots when they show different moments. Ground every sentence in what is visibly present in the supplied frames or transcript: when transcript is empty, describe only observable composition, characters, movement, and setting. Do not name an animal, object, action, outcome, time of day, or emotion unless the frames clearly support it. Never invent a chase, sunset, return home, dialogue, or other concrete event absent from the prompt, transcript, or images.",
        "schema": {"theme": "string", "narration": "string", "shot_order": ["shot-0000"], "beats": [{"id": "hook|journey|close", "label": "string", "text": "string", "source": "prompt|transcript|source", "source_shot_ids": ["shot-0000"]}]},
    }
    # Ollama accepts a JSON Schema in ``format``. Supplying the exact enum of
    # legal IDs makes source selection a constrained decoding task instead of
    # relying on the model to remember an opaque identifier convention.
    shot_id_schema = {"type": "string", "enum": sorted(evidence_ids)}
    # Require enough IDs for the requested duration when the candidate pool
    # contains them. This prevents a 60s request from silently collapsing to
    # three short clips; the story builder still pads from retrieval if a
    # small model returns fewer legal IDs than requested.
    order_min = min(target_shots, max(1, len(evidence_ids)))
    order_max = min(max(8, target_shots), max(order_min, len(evidence_ids)))
    response_schema = {
        "type": "object",
        "required": ["theme", "narration", "shot_order", "beats"],
        "properties": {
            "theme": {"type": "string"},
            "narration": {"type": "string", "minLength": max(24, min(110, int(requested_duration * 1.5)))},
            "shot_order": {"type": "array", "items": shot_id_schema, "minItems": order_min, "maxItems": order_max},
            "beats": {"type": "array", "minItems": 3, "maxItems": 3, "items": {
                "type": "object",
                "required": ["id", "label", "text", "source", "source_shot_ids"],
                "properties": {
                    "id": {"type": "string", "enum": ["hook", "journey", "close"]}, "label": {"type": "string"},
                    "text": {"type": "string"}, "source": {"type": "string"},
                    "source_shot_ids": {"type": "array", "items": shot_id_schema, "minItems": 1},
                },
            }},
        },
    }
    try:
        default_num_ctx = 16384 if requested_duration >= 30.0 else 8192
        num_ctx = max(4096, int(os.getenv("QUICKCUT_VLM_NUM_CTX", str(default_num_ctx))))
    except ValueError:
        num_ctx = 8192
    try:
        configured_timeout = float(os.getenv("QUICKCUT_VLM_TIMEOUT", ""))
    except ValueError:
        configured_timeout = 0.0
    # CPU VLM generation for a 60-second plan can take longer than the old
    # short-clip timeout. Keep short requests responsive while allowing long
    # local planning to finish; callers can override this with an environment
    # variable when running on slower hardware.
    effective_timeout = configured_timeout if configured_timeout > 0 else max(timeout, 120.0 if requested_duration >= 30.0 else timeout)
    request = Request(
        f"{base_url}/api/generate",
        data=json.dumps({"model": model, "prompt": json.dumps(instruction, ensure_ascii=False),
                         "images": images, "format": response_schema, "stream": False,
                         # Qwen2.5-VL commonly defaults to a 4096-token
                         # context. Multiple keyframes plus the evidence
                         # schema can exceed that limit, so request a larger
                         # local context explicitly.
                         "options": {"num_ctx": num_ctx}}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with _open_ollama(request, timeout=effective_timeout, base_url=base_url) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except OSError:
            detail = ""
        raise RuntimeError(f"Local Ollama VLM unavailable at {base_url}: HTTP {exc.code} {detail}") from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Local Ollama VLM unavailable at {base_url}: {exc}") from exc
    raw = payload.get("response", payload)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            # A model can wrap JSON in a markdown fence even when format=json
            # is requested. Strip only the fence; do not execute or interpret
            # arbitrary output.
            cleaned = raw.strip()
            if cleaned.startswith("```") and cleaned.endswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
                raw = json.loads(cleaned)
            else:
                raise ValueError("Local VLM returned invalid JSON") from exc
    # Restrict the model to IDs actually sent in the evidence payload.  This
    # prevents a plausible-looking but unseen source ID from becoming a clip.
    raw = normalize_shot_plan(raw, allowed_shot_ids=evidence_ids)
    raw["theme"] = str(raw.get("theme") or prompt)
    raw["narration"] = str(raw.get("narration") or f"We set out with {prompt}.")
    raw["planner"] = f"ollama:{model}"
    return raw


def plan_story(
    manifest: IndexManifest,
    prompt: str,
    *,
    mode: str = "deterministic",
    candidate_shot_ids: set[str] | list[str] | None = None,
    duration: float | None = None,
) -> dict:
    mode = (mode or "deterministic").lower()
    if mode in {"ollama", "qwen", "qwen2.5-vl", "auto"}:
        try:
            return plan_with_ollama(manifest, prompt, candidate_shot_ids=candidate_shot_ids,
                                    duration=duration)
        except (RuntimeError, ValueError):
            if mode != "auto":
                raise
    return {}

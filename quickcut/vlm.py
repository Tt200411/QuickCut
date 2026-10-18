from __future__ import annotations

"""Optional local story planner adapters.

The Basic path never requires a VLM. When Ollama is running locally, this
module can turn transcript and shot metadata into a richer beat plan while
keeping the same JSON contract. No remote API is used.
"""

import json
import os
import base64
import hashlib
import io
import math
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener, urlopen

from .core import DEFAULT_TIMELINE_SECONDS, MAX_TIMELINE_SECONDS, MIN_TIMELINE_SECONDS
from .types import IndexManifest, Shot


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



def _visual_payload(
    available_shots: list[Shot],
    *,
    require_visual: bool,
    visual_mode: str,
    contact_sheet_size: int,
) -> tuple[list[str], list[dict], list[dict]]:
    """Encode every candidate's real keyframe, with an auditable image mapping.

    A labeled contact sheet amortizes the VLM image-token cost without allowing
    metadata-only candidates into a visually grounded selection. No old cache
    entry with a missing or corrupt keyframe is silently accepted in strict mode.
    """
    if visual_mode not in {"contact_sheet", "frames"}:
        raise ValueError("VLM visual_mode must be contact_sheet or frames")
    if not 1 <= contact_sheet_size <= 24:
        raise ValueError("VLM contact_sheet_size must be between 1 and 24")
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError as exc:
        raise RuntimeError("Local visual planning requires Pillow to validate keyframes") from exc

    frames: list[tuple[object, object, bytes]] = []
    keyframes: list[dict] = []
    for shot in available_shots:
        try:
            if not shot.keyframe:
                raise ValueError("keyframe path is missing")
            contents = Path(shot.keyframe).read_bytes()
            with Image.open(io.BytesIO(contents)) as source:
                source.load()
                frame = source.convert("RGB")
        except (OSError, ValueError) as exc:
            if require_visual:
                raise ValueError(
                    f"Local VLM requires a readable keyframe for {shot.shot_id}: {exc}. "
                    "Rebuild the visual index; metadata-only caches cannot be used."
                ) from exc
            continue
        frames.append((shot, frame, contents))
        keyframes.append({
            "shot_id": shot.shot_id, "path": str(Path(shot.keyframe).resolve()),
            "sha256": hashlib.sha256(contents).hexdigest(),
            "width": frame.width, "height": frame.height,
        })
    if require_visual and not frames:
        raise ValueError("Local VLM received no real keyframes")

    images: list[str] = []
    image_slots: list[dict] = []
    group_size = contact_sheet_size if visual_mode == "contact_sheet" else 1
    for start in range(0, len(frames), group_size):
        group = frames[start:start + group_size]
        if visual_mode == "frames":
            # Re-encode the validated RGB image, avoiding ambiguous formats or
            # corrupt trailing data from an otherwise decodable cache entry.
            image = group[0][1]
        else:
            columns = min(4, len(group))
            tile_width, tile_height, label_height = 320, 180, 24
            rows = math.ceil(len(group) / columns)
            image = Image.new("RGB", (columns * tile_width, rows * (tile_height + label_height)), "#171717")
            draw = ImageDraw.Draw(image)
            for tile_index, (shot, frame, _) in enumerate(group):
                x = (tile_index % columns) * tile_width
                y = (tile_index // columns) * (tile_height + label_height)
                thumb = ImageOps.contain(frame, (tile_width, tile_height))
                image.paste(thumb, (x + (tile_width - thumb.width) // 2,
                                    y + (tile_height - thumb.height) // 2))
                draw.text((x + 6, y + tile_height + 5),
                          f"{shot.shot_id}  {shot.start:.1f}-{shot.end:.1f}s", fill="white")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        encoded = output.getvalue()
        images.append(base64.b64encode(encoded).decode("ascii"))
        image_slots.append({
            "image_index": len(images) - 1,
            "layout": "row-major, left to right, top to bottom" if visual_mode == "contact_sheet" else "single keyframe",
            "shot_ids": [shot.shot_id for shot, _, _ in group],
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "width": image.width, "height": image.height,
        })
    return images, image_slots, keyframes



class VLMSelectionError(ValueError):
    """Rejected model decisions, retaining the actual inference for auditing."""

    def __init__(self, message: str, *, inference: dict):
        super().__init__(message)
        self.inference = inference


def _validate_strict_selection(
    model_plan: object,
    *,
    shots: list[Shot],
    duration: float,
    min_shots: int,
    max_shots: int,
    act_candidate_shot_ids: dict[str, list[str]] | None,
    target_shot_counts: dict[str, int] | None,
    chronological: bool,
) -> None:
    """Check model choices without synthesizing IDs or repartitioning acts."""
    if not isinstance(model_plan, dict):
        raise ValueError("Strict VLM selection requires an object response")
    order = model_plan.get("shot_order")
    if not isinstance(order, list) or not order or not all(isinstance(value, str) for value in order):
        raise ValueError("Strict VLM selection requires an explicit nonempty shot_order array")
    by_id = {shot.shot_id: shot for shot in shots}
    if len(order) != len(set(order)):
        raise ValueError("Strict VLM shot_order contains duplicate IDs")
    if not set(order) <= by_id.keys():
        raise ValueError("Strict VLM shot_order contains ungrounded or unknown IDs")
    if not min_shots <= len(order) <= max_shots:
        raise ValueError(f"Strict VLM selected {len(order)} shots; expected {min_shots}–{max_shots}")
    selected_duration = sum(by_id[shot_id].duration for shot_id in order)
    if selected_duration > duration + 0.001:
        raise ValueError(f"Strict VLM source selection is {selected_duration:g}s, exceeding {duration:g}s budget")
    if chronological and any(by_id[left].start > by_id[right].start for left, right in zip(order, order[1:])):
        raise ValueError("Strict VLM shot_order is not chronological")
    beats = model_plan.get("beats")
    act_ids = ["hook", "journey", "close"]
    if not isinstance(beats, list) or len(beats) != 3 or any(not isinstance(beat, dict) for beat in beats):
        raise ValueError("Strict VLM selection requires exactly three acts")
    if [beat.get("id") for beat in beats] != act_ids:
        raise ValueError("Strict VLM acts must be hook, journey, close in that order")
    flattened = []
    for beat in beats:
        act_id = beat["id"]
        ids = beat.get("source_shot_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(value, str) for value in ids):
            raise ValueError(f"Strict VLM {act_id} requires a nonempty source_shot_ids array")
        if target_shot_counts and len(ids) != target_shot_counts[act_id]:
            raise ValueError(f"Strict VLM {act_id} selected {len(ids)} shots; expected {target_shot_counts[act_id]}")
        if act_candidate_shot_ids and not set(ids) <= set(act_candidate_shot_ids[act_id]):
            raise ValueError(f"Strict VLM {act_id} selected an ID outside its act candidates")
        flattened.extend(ids)
    if flattened != order:
        raise ValueError("Strict VLM beat source IDs must partition shot_order exactly, without overlaps, extra IDs, omissions, or reordered IDs")


def plan_with_ollama(
    manifest: IndexManifest,
    prompt: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 30.0,
    candidate_shot_ids: set[str] | list[str] | None = None,
    duration: float | None = None,
    require_visual: bool = True,
    visual_mode: str | None = None,
    contact_sheet_size: int = 12,
    strict_selection: bool = True,
    act_candidate_shot_ids: dict[str, list[str]] | None = None,
    target_shot_counts: dict[str, int] | None = None,
    chronological: bool = False,
) -> dict:
    model = model or os.getenv("QUICKCUT_VLM_MODEL", "qwen2.5vl:3b")
    base_url = (base_url or os.getenv("QUICKCUT_OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
    allowed_ids = set(candidate_shot_ids) if candidate_shot_ids is not None else {shot.shot_id for shot in manifest.shots}
    available_shots = [shot for shot in manifest.shots if shot.shot_id in allowed_ids]
    if not available_shots:
        raise ValueError("Local VLM has no valid source-shot candidates")
    visual_mode = visual_mode or os.getenv("QUICKCUT_VLM_VISUAL_MODE", "contact_sheet")
    images, image_slots, keyframes = _visual_payload(
        available_shots, require_visual=require_visual, visual_mode=visual_mode,
        contact_sheet_size=contact_sheet_size,
    )
    grounded_ids = {entry["shot_id"] for entry in keyframes}
    evidence = [
        {"shot_id": shot.shot_id, "start": shot.start, "end": shot.end, "transcript": shot.transcript,
         "image_available": shot.shot_id in grounded_ids}
        for shot in available_shots
    ]
    try:
        requested_duration = (
            max(MIN_TIMELINE_SECONDS, min(MAX_TIMELINE_SECONDS, float(duration)))
            if duration is not None else DEFAULT_TIMELINE_SECONDS
        )
    except (TypeError, ValueError):
        requested_duration = DEFAULT_TIMELINE_SECONDS
    typical_window = sorted(max(0.1, shot.duration) for shot in available_shots)[len(available_shots) // 2]
    # Full windows must fit within the maximum budget. ceil() would demand
    # 20 three-second shots for a 58-second request, making strict validation
    # impossible. Variable-length candidates are checked again after inference.
    target_shots = max(1, min(len(available_shots), int(requested_duration // typical_window)))
    evidence_ids = {shot["shot_id"] for shot in evidence}
    act_ids = {"hook", "journey", "close"}
    if act_candidate_shot_ids is not None:
        if set(act_candidate_shot_ids) != act_ids or any(not ids or not set(ids) <= evidence_ids for ids in act_candidate_shot_ids.values()):
            raise ValueError("act_candidate_shot_ids must give nonempty grounded candidates for hook, journey, close")
    if target_shot_counts is not None:
        if set(target_shot_counts) != act_ids or any(not isinstance(count, int) or count < 1 for count in target_shot_counts.values()):
            raise ValueError("target_shot_counts must give a positive integer for hook, journey, close")
        target_shots = sum(target_shot_counts.values())
        if target_shots > len(available_shots):
            raise ValueError("Requested act shot counts exceed the number of visual candidates")
        if act_candidate_shot_ids and any(target_shot_counts[act_id] > len(set(act_candidate_shot_ids[act_id])) for act_id in act_ids):
            raise ValueError("Requested act shot count exceeds that act's visual candidates")
    chinese_requested = bool(re.search(r"[\u4e00-\u9fff]", prompt))
    language_instruction = (
        "Respond entirely in Simplified Chinese: theme, narration, beat labels, and beat text must all be Chinese."
        if chinese_requested else
        "Respond in the language used by the prompt."
    )
    instruction = {
        "prompt": prompt,
        "shots": evidence,
        "image_slots": image_slots,
        "visual_format": visual_mode,
        "image_reading_instruction": "Each supplied image contains labeled keyframes. Read every tile using its exact shot_id. Tile order is row-major, left to right then top to bottom. All candidates have a real frame; do not infer a shot solely from neighboring timestamps.",
        "duration_seconds": requested_duration,
        "target_shot_count": target_shots,
        "target_shot_counts": target_shot_counts,
        "act_candidate_shot_ids": act_candidate_shot_ids,
        "selection_constraints": "shot_order is authoritative. The concatenation of hook, journey, close source_shot_ids MUST equal shot_order exactly. Never list a candidate unless it is selected; do not add IDs, duplicate IDs between acts, or reorder them. The total selected source duration must not exceed duration_seconds.",
        "chronological": chronological,
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
    order_max = target_shots if strict_selection else min(max(8, target_shots), max(order_min, len(evidence_ids)))
    if strict_selection and target_shots < 3:
        raise ValueError("Strict three-act planning needs at least three source shots within its budget")

    def beat_schema(act_id: str | None = None) -> dict:
        candidate_ids = act_candidate_shot_ids[act_id] if act_id and act_candidate_shot_ids else sorted(evidence_ids)
        count = target_shot_counts[act_id] if act_id and target_shot_counts else None
        return {
            "type": "object", "required": ["id", "label", "text", "source", "source_shot_ids"],
            "properties": {
                "id": {"type": "string", "enum": [act_id] if act_id else ["hook", "journey", "close"]},
                "label": {"type": "string"}, "text": {"type": "string"}, "source": {"type": "string"},
                "source_shot_ids": {"type": "array", "items": {"type": "string", "enum": candidate_ids},
                                    "minItems": count or 1, "maxItems": count or max(1, target_shots - 2), "uniqueItems": True},
            },
        }
    beat_items = ({"anyOf": [beat_schema(act_id) for act_id in ("hook", "journey", "close")]}
                  if act_candidate_shot_ids or target_shot_counts else beat_schema())
    response_schema = {
        "type": "object",
        "required": ["theme", "narration", "shot_order", "beats"],
        "properties": {
            "theme": {"type": "string"},
            "narration": {"type": "string", "minLength": max(24, min(110, int(requested_duration * 1.5)))},
            "shot_order": {"type": "array", "items": shot_id_schema, "minItems": order_min, "maxItems": order_max, "uniqueItems": True},
            "beats": {"type": "array", "minItems": 3, "maxItems": 3, "items": beat_items},
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
    request_prompt = json.dumps(instruction, ensure_ascii=False)
    request = Request(
        f"{base_url}/api/generate",
        data=json.dumps({"model": model, "prompt": request_prompt,
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
    raw_response = payload.get("response", payload)
    raw = raw_response
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
    model_plan = json.loads(json.dumps(raw))
    inference = {
        "model": payload.get("model", model), "endpoint": base_url,
        "request_prompt": request_prompt, "response": raw_response,
        "model_plan": model_plan,
        "input_image_count": len(images), "image_slots": image_slots,
        "visual_mode": visual_mode, "require_visual": require_visual,
        "strict_selection": strict_selection, "chronological": chronological,
        "grounded_shot_ids": [shot.shot_id for shot in available_shots if shot.shot_id in grounded_ids],
        "candidate_shot_ids": [shot.shot_id for shot in available_shots],
        "keyframes": keyframes,
        "metrics": {key: payload[key] for key in (
            "created_at", "done", "done_reason", "total_duration", "load_duration",
            "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration",
        ) if key in payload},
    }
    if strict_selection:
        try:
            _validate_strict_selection(
                model_plan, shots=available_shots, duration=requested_duration,
                min_shots=order_min, max_shots=order_max,
                act_candidate_shot_ids=act_candidate_shot_ids,
                target_shot_counts=target_shot_counts, chronological=chronological,
            )
        except ValueError as exc:
            inference["validation_error"] = str(exc)
            raise VLMSelectionError(str(exc), inference=inference) from exc
    raw = normalize_shot_plan(raw, allowed_shot_ids=evidence_ids)
    if require_visual and not raw["shot_order"]:
        raise VLMSelectionError("Local VLM returned no visually grounded source-shot selection", inference=inference)
    inference["normalization_changed"] = any(raw.get(key) != model_plan.get(key) for key in ("beats", "shot_order"))
    inference["selected_source_duration"] = sum(shot.duration for shot in available_shots if shot.shot_id in raw["shot_order"])
    raw["inference"] = inference
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

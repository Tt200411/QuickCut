from __future__ import annotations

"""Optional local story planner adapters.

The Basic path never requires a VLM. When Ollama is running locally, this
module can turn transcript and shot metadata into a richer beat plan while
keeping the same JSON contract. No remote API is used.
"""

import json
import os
import base64
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener, urlopen

from .types import IndexManifest


def _open_ollama(request: Request, *, timeout: float, base_url: str):
    # The desktop environment may expose a system HTTP proxy that is not
    # reachable from the local runtime. Ollama is a localhost service, so
    # bypass proxies for loopback requests while preserving normal behavior
    # for an explicitly configured remote endpoint.
    hostname = urlsplit(base_url).hostname
    if hostname in {"127.0.0.1", "localhost", "::1"}:
        return build_opener(ProxyHandler({})).open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


def plan_with_ollama(manifest: IndexManifest, prompt: str, *, model: str | None = None, base_url: str | None = None, timeout: float = 30.0) -> dict:
    model = model or os.getenv("QUICKCUT_VLM_MODEL", "qwen2.5vl:3b")
    base_url = (base_url or os.getenv("QUICKCUT_OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
    evidence = [
        {"shot_id": shot.shot_id, "start": shot.start, "end": shot.end, "transcript": shot.transcript,
         "image_available": bool(shot.keyframe)}
        for shot in manifest.shots
    ]
    try:
        max_frames = max(1, int(os.getenv("QUICKCUT_VLM_MAX_FRAMES", "8")))
    except ValueError:
        max_frames = 8
    candidates = manifest.shots[::max(1, len(manifest.shots) // max_frames)][:max_frames]
    candidate_ids = {shot.shot_id for shot in candidates}
    # Bound long-video requests while retaining all sampled frames and a few
    # transcript-bearing windows for grounding.
    evidence = [shot for shot in evidence if shot["shot_id"] in candidate_ids or shot["transcript"]][: max_frames * 3]
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
        "task": "Write a 3-beat source-grounded montage plan. Return JSON with theme, narration, and beats[]. Never invent concrete events absent from the prompt or transcript.",
        "schema": {"theme": "string", "narration": "string", "beats": [{"id": "hook|journey|close", "label": "string", "text": "string", "source": "prompt|transcript|source"}]},
    }
    request = Request(
        f"{base_url}/api/generate",
        data=json.dumps({"model": model, "prompt": json.dumps(instruction, ensure_ascii=False),
                         "images": images, "format": "json", "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with _open_ollama(request, timeout=timeout, base_url=base_url) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Local Ollama VLM unavailable at {base_url}: {exc}") from exc
    raw = payload.get("response", payload)
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or not isinstance(raw.get("beats"), list) or not raw["beats"]:
        raise ValueError("Local VLM returned an invalid story plan")
    beats = []
    for beat in raw["beats"][:3]:
        if not isinstance(beat, dict):
            continue
        beats.append({
            "id": str(beat.get("id") or "journey"),
            "label": str(beat.get("label") or "Continue the journey"),
            "text": str(beat.get("text") or prompt),
            "source": str(beat.get("source") or "source"),
        })
    if not beats:
        raise ValueError("Local VLM returned no usable beats")
    raw["beats"] = beats
    raw["theme"] = str(raw.get("theme") or prompt)
    raw["narration"] = str(raw.get("narration") or f"We set out with {prompt}.")
    raw["planner"] = f"ollama:{model}"
    return raw


def plan_story(manifest: IndexManifest, prompt: str, *, mode: str = "deterministic") -> dict:
    mode = (mode or "deterministic").lower()
    if mode in {"ollama", "qwen", "qwen2.5-vl", "auto"}:
        try:
            return plan_with_ollama(manifest, prompt)
        except (RuntimeError, ValueError):
            if mode != "auto":
                raise
    return {}

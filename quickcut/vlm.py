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
from urllib.request import Request, urlopen

from .types import IndexManifest


def plan_with_ollama(manifest: IndexManifest, prompt: str, *, model: str | None = None, base_url: str | None = None, timeout: float = 30.0) -> dict:
    model = model or os.getenv("QUICKCUT_VLM_MODEL", "qwen2.5vl:3b")
    base_url = (base_url or os.getenv("QUICKCUT_OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
    evidence = [
        {"shot_id": shot.shot_id, "start": shot.start, "end": shot.end, "transcript": shot.transcript,
         "image_available": bool(shot.keyframe)}
        for shot in manifest.shots
    ]
    max_frames = max(1, int(os.getenv("QUICKCUT_VLM_MAX_FRAMES", "8")))
    candidates = manifest.shots[::max(1, len(manifest.shots) // max_frames)][:max_frames]
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
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Local Ollama VLM unavailable at {base_url}: {exc}") from exc
    raw = payload.get("response", payload)
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or not raw.get("beats"):
        raise ValueError("Local VLM returned an invalid story plan")
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

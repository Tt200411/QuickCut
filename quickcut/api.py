from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .core import (
    DEFAULT_TIMELINE_SECONDS,
    build_index,
    find_manifest,
    localize,
    render_timeline,
    update_timeline,
)
from .story import build_basic_vlog
from .types import Timeline, TimelineItem
from .voice import render_story_timeline


try:
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.responses import FileResponse
except ImportError as exc:  # pragma: no cover - exercised when optional extra is absent
    FastAPI = None  # type: ignore
    _FASTAPI_ERROR = exc


def _timeline_from_dict(data: dict[str, Any]) -> Timeline:
    data = dict(data)
    data.pop("duration", None)
    items = [TimelineItem(**item) for item in data.pop("items", [])]
    return Timeline(items=items, **data)


def create_app():
    if FastAPI is None:
        raise RuntimeError("API dependencies are missing. Install with: pip install -e '.[server]'") from _FASTAPI_ERROR
    app = FastAPI(title="QuickCut", version="0.1.0", description="Prompt-guided source localization MVP")
    cache_dir = Path(os.getenv("QUICKCUT_CACHE", "cache"))
    output_dir = Path(os.getenv("QUICKCUT_OUTPUTS", "outputs"))
    output_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "quickcut", "cache_dir": str(cache_dir)}

    @app.post("/index")
    async def index(
        video: UploadFile = File(...),
        window: float = Form(3.0),
        encoder: str | None = Form(None),
        asr: str | None = Form(None),
    ):
        suffix = Path(video.filename or "source.mp4").suffix or ".mp4"
        source_dir = cache_dir / "sources"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_path = source_dir / f"upload-{uuid.uuid4().hex}{suffix}"
        source_path.write_bytes(await video.read())
        manifest = build_index(source_path, cache_dir, window=window, encoder_mode=encoder, asr_mode=asr)
        return {
            "index_id": manifest.index_id,
            "encoder": manifest.encoder,
            "transcript_backend": manifest.transcript_backend,
            "shots": len(manifest.shots),
            "duration": manifest.duration,
        }

    @app.post("/localize")
    def localize_route(index_id: str, prompt: str, encoder: str | None = None):
        try:
            timeline = localize(find_manifest(cache_dir, index_id), prompt, encoder_mode=encoder)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return timeline.to_dict()

    @app.post("/basic")
    def basic_route(index_id: str, prompt: str, duration: float = DEFAULT_TIMELINE_SECONDS, encoder: str | None = None,
                    planner: str = "deterministic"):
        try:
            timeline = build_basic_vlog(find_manifest(cache_dir, index_id), prompt,
                                        duration=duration, encoder_mode=encoder, planner=planner)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return timeline.to_dict()

    @app.post("/edit")
    def edit_route(payload: dict[str, Any]):
        try:
            timeline = update_timeline(_timeline_from_dict(payload["timeline"]), payload.get("edits", []))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return timeline.to_dict()

    @app.post("/render")
    def render_route(payload: dict[str, Any]):
        try:
            timeline = _timeline_from_dict(payload["timeline"])
            target = output_dir / Path(str(payload.get("filename", "quickcut.mp4"))).name
            result = render_timeline(timeline, target, preset=payload.get("preset"))
        except (KeyError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(result, media_type="video/mp4", filename=result.name)

    @app.post("/render-story")
    def render_story_route(payload: dict[str, Any]):
        try:
            timeline = _timeline_from_dict(payload["timeline"])
            target = output_dir / Path(str(payload.get("filename", "quickcut-story.mp4"))).name
            result = render_story_timeline(
                timeline, target,
                captions=bool(payload.get("captions", True)),
                narration=bool(payload.get("narration", True)),
                voice=payload.get("voice"),
            )
        except (KeyError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(result, media_type="video/mp4", filename=result.name)

    return app


app = create_app() if FastAPI is not None else None

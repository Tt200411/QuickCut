"""Isolated real-model stages for the local workbench (no fixture switch)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def execute(action, data):
    if action == "ingest":
        from .ingest import IngestConfig, SourceSpec, ingest_assets
        return ingest_assets(data["root"], [SourceSpec(**item) for item in data["sources"]],
                             project_id=data["project_id"], config=IngestConfig(**data["config"]))
    if action == "retrieve":
        from .retrieval import RetrievalConfig, retrieve
        return retrieve(read(data["manifest"]), read(data["request"]), project_root=data["root"],
                        document_id="candidates-" + data["project_id"],
                        config=RetrievalConfig(encoder_mode="openclip", minimum_ranking_score=0.12))
    if action == "draft":
        from .planning.production import generate_draft
        root = Path(data["root"])
        return generate_draft(read(root / "brief.json"), read(root / "asset-manifest.json"), read(root / "candidate-set.json"),
                              retrieval_request=read(root / "retrieval-request.json"), project_root=root,
                              model=data["model"], base_url=data["ollama_url"], timeout=600,
                              archive_dir=root / "model-evidence")
    if action == "voice":
        from .voice_package import build_voice_package
        config = read(data["config"])
        if config.get("engine") != "indextts2":
            raise ValueError("真实工作台要求 IndexTTS2 配置，不允许自动替换为系统语音")
        repo = Path(__file__).resolve().parents[1]
        for settings in [config, *config.get("cue_overrides", {}).values()]:
            for key in ("model_dir", "reference_audio"):
                if key in settings:
                    path = Path(settings[key]).expanduser()
                    settings[key] = str(path if path.is_absolute() else repo / path)
        story = read(data["story"])
        return build_voice_package(story, data["root"], config=config, fixture=False,
                                   mode=data.get("mode", "measure"), revision=data["revision"],
                                   expected_revision=story["revision"],
                                   previous=read(data["previous"]) if data.get("previous") else None,
                                   request=read(data["request"]) if data.get("request") else None)
    if action == "probe_tts":
        from scripts.doctor_pipeline import check_configured_tts
        return check_configured_tts(data["config"])
    raise ValueError(f"Unknown pipeline action: {action}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["ingest", "retrieve", "draft", "voice", "probe_tts"])
    parser.add_argument("payload", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    from .media import write_json
    try:
        result = execute(args.action, read(args.payload))
        write_json(args.result, result)
    except Exception as exc:
        write_json(args.result, {"status": "failed", "code": getattr(exc, "code", "STAGE_FAILED"),
                                 "message": str(exc), "error_type": type(exc).__name__})
        raise


if __name__ == "__main__":
    main()

from __future__ import annotations

import json

from quickcut.story import build_basic_vlog, write_story
from quickcut.types import IndexManifest, Shot, Timeline, TimelineItem
from quickcut.vlm import normalize_shot_plan, plan_story, plan_with_ollama


def make_manifest() -> IndexManifest:
    return IndexManifest(
        index_id="story-index", source_video="source.mp4", source_size=1, source_mtime=1.0,
        duration=12.0, window=3.0, stride=3.0, created_at="now", encoder="fallback",
        shots=[
            Shot("shot-0000", 0, 3, transcript="scenic blue ocean opening"),
            Shot("shot-0001", 3, 6, transcript="friends walk through the forest"),
            Shot("shot-0002", 6, 9, transcript="group moments on the road"),
            Shot("shot-0003", 9, 12, transcript="atmospheric sunset close"),
        ],
    )


def test_basic_story_plan_uses_transcript_and_keeps_editable_timeline():
    manifest = make_manifest()
    plan = write_story(manifest, "scenic travel and group moments")
    assert plan["grounded_transcript"] is True
    timeline = build_basic_vlog(manifest, "scenic travel and group moments", duration=12, encoder_mode="fallback")
    assert timeline.coverage == "source"
    assert timeline.index_id == manifest.index_id
    assert 5 <= timeline.duration <= 12
    assert timeline.story["beats"]
    assert timeline.narration
    assert len(timeline.captions) == len(timeline.items)


def test_deterministic_vlm_mode_is_noop_without_local_ollama():
    plan = plan_story(make_manifest(), "a quiet journey", mode="deterministic")
    assert plan == {}


def test_ollama_plan_normalizes_partial_json(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"response": {"beats": [{"text": "A grounded opening"}]}}'

    monkeypatch.setattr("quickcut.vlm._open_ollama", lambda request, timeout, base_url: Response())
    plan = plan_with_ollama(make_manifest(), "a quiet journey", timeout=1)
    assert plan["narration"]
    assert plan["theme"] == "a quiet journey"
    assert plan["beats"][0]["source"] == "source"


def test_ollama_plan_preserves_source_shot_ids_and_order(monkeypatch):
    """The model's structured shot decisions must survive the Ollama adapter."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            payload = {
                "theme": "a quiet road",
                "narration": "The road opens, then the view settles.",
                "shot_order": ["shot-0002", "shot-0000", "shot-0003"],
                "selected_shot_ids": ["shot-0002", "shot-0000", "shot-0003"],
                "beats": [
                    {"id": "hook", "text": "Open on motion", "source_shot_ids": ["shot-0002"]},
                    {"id": "journey", "text": "Move through the road", "source_shot_ids": ["shot-0000"]},
                    {"id": "close", "text": "Let the view settle", "source_shot_ids": ["shot-0003"]},
                ],
            }
            return json.dumps({"response": json.dumps(payload)}).encode("utf-8")

    monkeypatch.setattr("quickcut.vlm._open_ollama", lambda request, timeout, base_url: Response())
    plan = plan_with_ollama(make_manifest(), "a quiet road", timeout=1)
    assert plan["shot_order"] == ["shot-0002", "shot-0000", "shot-0003"]
    assert plan["selected_shot_ids"] == plan["shot_order"]
    assert plan["beats"][0]["source_shot_ids"] == ["shot-0002"]


def test_normalize_shot_plan_whitelists_ids_and_deduplicates_order():
    plan = normalize_shot_plan(
        {
            "beats": [
                {"id": "hook", "source_shot_ids": ["shot-0001", "shot-9999", "shot-0001"]},
                {"id": "close", "shot_id": "shot-0002"},
            ],
            "shot_order": ["shot-9999", "shot-0002", "shot-0001", "shot-0002"],
        },
        allowed_shot_ids={"shot-0000", "shot-0001", "shot-0002"},
    )
    assert plan["shot_order"] == ["shot-0002", "shot-0001"]
    assert plan["selected_shot_ids"] == plan["shot_order"]
    assert plan["beats"][0]["source_shot_ids"] == ["shot-0001"]
    assert plan["beats"][1]["source_shot_ids"] == ["shot-0002"]


def test_normalize_shot_plan_keeps_beat_shots_missing_from_top_level_order():
    plan = normalize_shot_plan(
        {
            "beats": [
                {"id": "hook", "source_shot_ids": ["shot-0000"]},
                {"id": "journey", "source_shot_ids": ["shot-0001"]},
                {"id": "close", "source_shot_ids": ["shot-0002"]},
            ],
            "shot_order": ["shot-0000", "shot-0001"],
        },
        allowed_shot_ids={"shot-0000", "shot-0001", "shot-0002"},
    )
    assert plan["shot_order"] == ["shot-0000", "shot-0001", "shot-0002"]


def test_normalize_shot_plan_rebalances_inconsistent_beat_references():
    plan = normalize_shot_plan(
        {
            "beats": [
                {"id": "hook", "source_shot_ids": ["shot-0000"]},
                {"id": "journey", "source_shot_ids": ["shot-0001", "shot-0002"]},
                {"id": "close", "source_shot_ids": ["shot-0000"]},
            ],
            "shot_order": ["shot-0000", "shot-0001", "shot-0002", "shot-0003", "shot-0004"],
        },
        allowed_shot_ids={f"shot-{idx:04d}" for idx in range(5)},
    )
    assert [beat["source_shot_ids"] for beat in plan["beats"]] == [
        ["shot-0000", "shot-0001"],
        ["shot-0002", "shot-0003"],
        ["shot-0004"],
    ]


def _retrieval_timeline(manifest: IndexManifest) -> Timeline:
    return Timeline(
        version="1.0", source_video=manifest.source_video, query="test", duration_budget=9.0,
        index_id=manifest.index_id, coverage="source", items=[
            TimelineItem("item-000", "source", "shot-0000", 0, 3, score=0.3),
            TimelineItem("item-001", "source", "shot-0001", 3, 6, score=0.2),
            TimelineItem("item-002", "source", "shot-0002", 6, 9, score=0.1),
        ],
    )


def test_qwen_shot_order_drives_story_timeline(monkeypatch):
    """A valid Qwen order must be reflected in rendered timeline item order."""
    manifest = make_manifest()
    for shot in manifest.shots:
        shot.transcript = ""
    monkeypatch.setattr("quickcut.story.localize", lambda *args, **kwargs: _retrieval_timeline(manifest))
    monkeypatch.setattr(
        "quickcut.story.plan_story",
        lambda *args, **kwargs: {
            "planner": "ollama:qwen2.5vl:3b",
            "theme": "a road journey",
            "narration": "The road opens and settles.",
            "shot_order": ["shot-0002", "shot-0000", "shot-0001"],
            "selected_shot_ids": ["shot-0002", "shot-0000", "shot-0001"],
            "shot_selection": True,
            "beats": [
                {"id": "hook", "text": "Open with movement", "source_shot_ids": ["shot-0002"]},
                {"id": "journey", "text": "Continue through the road", "source_shot_ids": ["shot-0000"]},
                {"id": "close", "text": "End on the view", "source_shot_ids": ["shot-0001"]},
            ],
        },
    )
    timeline = build_basic_vlog(manifest, "a road journey", duration=9, encoder_mode="fallback", planner="ollama")
    assert [item.asset_id for item in timeline.items] == ["shot-0002", "shot-0000", "shot-0001"]
    assert timeline.story["shot_order"] == ["shot-0002", "shot-0000", "shot-0001"]
    assert timeline.story["beats"][0]["source_shot_ids"] == ["shot-0002"]
    assert [item.transcript for item in timeline.items] == [
        "Open with movement", "Continue through the road", "End on the view"
    ]


def test_qwen_invalid_shot_ids_are_ignored_or_fallback(monkeypatch):
    """Hallucinated IDs must never reach a source-backed timeline."""
    manifest = make_manifest()
    monkeypatch.setattr("quickcut.story.localize", lambda *args, **kwargs: _retrieval_timeline(manifest))
    monkeypatch.setattr(
        "quickcut.story.plan_story",
        lambda *args, **kwargs: {
            "planner": "ollama:qwen2.5vl:3b",
            "theme": "a safe journey",
            "narration": "The available shots carry the story.",
            "shot_order": ["shot-does-not-exist", "shot-0001", "shot-9999"],
            "selected_shot_ids": ["shot-does-not-exist", "shot-0001", "shot-9999"],
            "shot_selection": True,
            "beats": [
                {"id": "hook", "text": "Start safely", "source_shot_ids": ["shot-does-not-exist"]},
                {"id": "journey", "text": "Continue", "source_shot_ids": ["shot-0001"]},
                {"id": "close", "text": "Close safely", "source_shot_ids": ["shot-9999"]},
            ],
        },
    )
    timeline = build_basic_vlog(manifest, "a safe journey", duration=9, encoder_mode="fallback", planner="ollama")
    valid_ids = {shot.shot_id for shot in manifest.shots}
    assert timeline.coverage == "source"
    assert timeline.items
    assert all(item.asset_id in valid_ids for item in timeline.items)
    assert all(item.end > item.start for item in timeline.items)


def test_auto_planner_falls_back_when_ollama_is_unavailable(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("quickcut.vlm.plan_with_ollama", fail)
    assert plan_story(make_manifest(), "a quiet journey", mode="auto") == {}

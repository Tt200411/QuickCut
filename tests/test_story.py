from __future__ import annotations

import json

from quickcut.story import build_basic_vlog, write_story, _narration_word_count
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


def test_basic_story_can_fill_a_one_minute_budget_with_complete_beats():
    manifest = IndexManifest(
        index_id="long-story", source_video="source.mp4", source_size=1, source_mtime=1.0,
        duration=600.0, window=3.0, stride=3.0, created_at="now", encoder="fallback",
        shots=[Shot(f"shot-{idx:04d}", idx * 3, (idx + 1) * 3,
                    transcript=f"story moment {idx}") for idx in range(20)],
    )
    timeline = build_basic_vlog(manifest, "a complete journey with a clear ending", duration=60,
                                encoder_mode="fallback")
    assert timeline.coverage == "source"
    assert timeline.duration_budget == 60
    assert timeline.duration == 60
    assert [beat["id"] for beat in timeline.story["beats"]] == ["hook", "journey", "close"]
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


def test_story_shape_fills_three_acts_for_a_long_request(monkeypatch):
    """A short local-model response is expanded into a complete three-act plan."""
    manifest = make_manifest()
    monkeypatch.setattr(
        "quickcut.story.plan_story",
        lambda *args, **kwargs: {
            "planner": "ollama:qwen2.5vl:3b",
            "theme": "a road journey",
            "narration": "The road opens, changes, and settles.",
            "shot_selection": True,
            "shot_order": ["shot-0002", "shot-0000", "shot-0001", "shot-0003"],
            "beats": [{"id": "hook", "text": "Open on motion", "source_shot_ids": ["shot-0002"]}],
        },
    )
    plan = write_story(
        manifest, "a complete road journey", planner="ollama",
        candidate_shot_ids=[shot.shot_id for shot in manifest.shots], duration=60,
    )
    assert plan["duration_seconds"] == 60
    assert [beat["id"] for beat in plan["beats"]] == ["hook", "journey", "close"]
    assert [shot_id for beat in plan["beats"] for shot_id in beat["source_shot_ids"]] == plan["shot_order"]
    assert all(beat["text"] for beat in plan["beats"])


def test_long_story_narration_covers_all_three_acts(monkeypatch):
    manifest = make_manifest()
    monkeypatch.setattr(
        "quickcut.story.plan_story",
        lambda *args, **kwargs: {
            "planner": "ollama:qwen2.5vl:3b", "theme": "forest journey",
            "narration": "A forest journey begins.", "shot_selection": True,
            "shot_order": [shot.shot_id for shot in manifest.shots],
            "beats": [
                {"id": "hook", "text": "The path opens", "source_shot_ids": ["shot-0000"]},
                {"id": "journey", "text": "Friends move onward", "source_shot_ids": ["shot-0001", "shot-0002"]},
                {"id": "close", "text": "Sunset settles", "source_shot_ids": ["shot-0003"]},
            ],
        },
    )
    plan = write_story(manifest, "forest journey", planner="ollama", duration=60)
    assert _narration_word_count(plan["narration"]) >= 90
    narration = plan["narration"].lower()
    assert "the path opens" in narration
    assert "friends move onward" in narration
    assert "sunset settles" in narration


def test_ollama_duration_schema_scales_source_order(monkeypatch):
    """The JSON schema asks Qwen for enough distinct windows for 60 seconds."""
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            request = captured["request"]
            payload = json.loads(request.data.decode("utf-8"))
            captured["body"] = payload
            ids = [f"shot-{idx:04d}" for idx in range(4)]
            response = {
                "theme": "a complete journey", "narration": "The scene opens, moves, and settles.",
                "shot_order": ids,
                "beats": [
                    {"id": "hook", "label": "Hook", "text": "Open", "source": "source", "source_shot_ids": ids[:1]},
                    {"id": "journey", "label": "Journey", "text": "Move", "source": "source", "source_shot_ids": ids[1:3]},
                    {"id": "close", "label": "Close", "text": "Settle", "source": "source", "source_shot_ids": ids[3:]},
                ],
            }
            return json.dumps({"response": json.dumps(response)}).encode("utf-8")

    def capture(request, timeout, base_url):
        captured["request"] = request
        return Response()

    monkeypatch.setattr("quickcut.vlm._open_ollama", capture)
    manifest = IndexManifest(
        index_id="long", source_video="source.mp4", source_size=1, source_mtime=1.0,
        duration=60.0, window=3.0, stride=3.0, created_at="now", encoder="fallback",
        shots=[Shot(f"shot-{idx:04d}", idx * 3, (idx + 1) * 3) for idx in range(20)],
    )
    plan_with_ollama(manifest, "a complete journey", candidate_shot_ids=[shot.shot_id for shot in manifest.shots], duration=60)
    body = captured["body"]
    schema = body["format"]
    assert schema["properties"]["shot_order"]["minItems"] == 20
    assert schema["properties"]["shot_order"]["maxItems"] == 20
    instruction = json.loads(body["prompt"])
    assert instruction["target_shot_count"] == 20

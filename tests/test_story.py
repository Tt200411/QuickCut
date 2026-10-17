from __future__ import annotations

from quickcut.story import build_basic_vlog, write_story
from quickcut.types import IndexManifest, Shot
from quickcut.vlm import plan_story, plan_with_ollama


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


def test_auto_planner_falls_back_when_ollama_is_unavailable(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("quickcut.vlm.plan_with_ollama", fail)
    assert plan_story(make_manifest(), "a quiet journey", mode="auto") == {}

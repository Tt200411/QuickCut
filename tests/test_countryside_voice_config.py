"""Verify the real-media entry point chooses its declared TTS backend."""
import json
from types import SimpleNamespace

import pytest

from scripts import run_countryside_handoff as countryside


@pytest.fixture
def pipeline(monkeypatch):
    document = {"kind": "AssetManifest", "document_id": "manifest", "revision": 1,
                "assets": [], "processing": {"status": "success"}}
    story = {"kind": "StoryPlan", "document_id": "story", "revision": 1,
             "status": "ready_for_edit"}
    result = SimpleNamespace(story_plan=story, alignment={})
    monkeypatch.setattr(countryside, "load_sources", lambda: [])
    monkeypatch.setattr(countryside, "ingest_assets", lambda *a, **kw: document)
    monkeypatch.setattr(countryside, "build_outline", lambda *a: [])
    monkeypatch.setattr(countryside, "countryside_request", lambda *a: {})
    monkeypatch.setattr(countryside, "retrieve", lambda *a, **kw: {})
    monkeypatch.setattr(countryside, "plan", lambda *a, **kw: result)
    monkeypatch.setattr(countryside, "recall_report", lambda *a: {})
    monkeypatch.setattr(countryside, "summarize", lambda *a: {})


def test_default_countryside_selects_index_and_preserves_failed_package(tmp_path, monkeypatch, pipeline):
    config = {"engine": "indextts2", "model_revision": "740dcaf"}
    loaded = []
    def load(path):
        loaded.append(path)
        return config
    monkeypatch.setattr(countryside, "load_voice_config", load)
    voice = {"kind": "VoicePackage", "document_id": "voice", "revision": 1,
             "synthesis_profile": config, "status": "needs_retime"}

    def needs_retime(*args, **kwargs):
        assert kwargs["config"] == config
        assert kwargs["fixture"] is False
        raise countryside.IntegrationError("VOICE_NEEDS_RETIME", "cue over budget", action_owner="D", voice=voice)

    monkeypatch.setattr(countryside, "build_from_plan", needs_retime)
    assert countryside.main(tmp_path) == 1
    assert loaded == [countryside.DEFAULT_VOICE_CONFIG]
    report = json.loads((tmp_path / "run-report.json").read_text())
    assert report["voice"]["requested_engine"] == "indextts2"
    assert report["voice"]["synthesis_profile"] == config
    assert report["voice"]["status"] == "needs_retime"
    assert report["f_inputs"]["ready_for_f"] is False
    assert json.loads((tmp_path / "voice-package.json").read_text()) == voice
    assert not (tmp_path / "timeline.json").exists()


def test_invalid_index_config_stops_before_ingest(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid TTS configuration must fail before expensive media/model work")
    monkeypatch.setattr(countryside, "ingest_assets", forbidden)
    path = tmp_path / "invalid.json"
    path.write_text('{"engine": "auto"}')
    root = tmp_path / "output"
    assert countryside.cli([str(root), "--voice-config", str(path)]) == 2
    assert not root.exists()


def test_system_voice_requires_explicit_flag(tmp_path, monkeypatch, pipeline):
    monkeypatch.setattr(countryside, "load_voice_config", lambda *a: pytest.fail("explicit system voice does not load IndexTTS2"))
    def build(*args, **kwargs):
        assert kwargs["config"] == {"engine": "auto", "rate": 145}
        voice = {"kind": "VoicePackage", "document_id": "voice", "revision": 1,
                 "synthesis_profile": {"engine": "say"}, "status": "ready"}
        return SimpleNamespace(voice_package=voice, timeline={}, assets=[])
    monkeypatch.setattr(countryside, "build_from_plan", build)
    assert countryside.cli([str(tmp_path), "--system-voice"]) == 0
    report = json.loads((tmp_path / "run-report.json").read_text())
    assert report["voice"]["synthesis_profile"]["engine"] == "say"
    assert report["voice"]["config"] is None

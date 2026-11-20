"""Legacy demo orchestration tests; model replies and media encoders are fixtures."""
from copy import deepcopy
import io
import json
from pathlib import Path

import pytest

from quickcut import story as story_module, vlm
from quickcut.types import IndexManifest, Shot, Timeline, TimelineItem
from scripts import run_full_demo as demo


@pytest.mark.parametrize("listed,requested,present", [
    ("qwen2.5vl:3b", "qwen2.5vl:7b", False),
    ("qwen2.5vl:7b", "qwen2.5vl:7b", True),
    ("qwen2.5vl:latest", "qwen2.5vl", True),
    ("qwen2.5vl", "qwen2.5vl:latest", True),
])
def test_model_listing_does_not_substitute_another_size(monkeypatch, listed, requested, present):
    calls = []
    def tags(runtime, endpoint, timeout):
        calls.append((runtime.ollama_url, endpoint))
        return {"models": [{"name": listed}]}
    monkeypatch.setattr(demo, "ollama_json", tags)
    assert demo._ollama_has_model("http://127.0.0.1:11435", requested) is present
    assert calls == [("http://127.0.0.1:11435", "/api/tags")]


def test_pull_uses_and_rechecks_the_configured_local_server(monkeypatch):
    started, pulled = [], []
    listings = iter([False, True])
    monkeypatch.setattr(demo, "start_ollama", lambda runtime: started.append(runtime.ollama_url))
    monkeypatch.setattr(demo, "_ollama_has_model", lambda url, model: next(listings))
    monkeypatch.setattr(demo.shutil, "which", lambda name: "/local/ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://wrong-server.invalid:11434")
    monkeypatch.setattr(demo.subprocess, "run", lambda command, **kw: pulled.append((command, kw)))
    demo.ensure_ollama("http://localhost:11435", "qwen2.5vl:7b")
    assert started == ["http://localhost:11435"]
    assert pulled[0][0] == ["/local/ollama", "pull", "qwen2.5vl:7b"]
    assert pulled[0][1]["env"]["OLLAMA_HOST"] == "http://localhost:11435"
    assert pulled[0][1]["check"] is True
    assert next(listings, "exhausted") == "exhausted"


def test_pull_success_without_requested_tag_still_fails(monkeypatch):
    monkeypatch.setattr(demo, "start_ollama", lambda runtime: None)
    monkeypatch.setattr(demo, "_ollama_has_model", lambda url, model: False)
    monkeypatch.setattr(demo.shutil, "which", lambda name: "/local/ollama")
    monkeypatch.setattr(demo.subprocess, "run", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="still not listed"):
        demo.ensure_ollama("http://127.0.0.1:11434", "qwen2.5vl:7b")


def test_demo_service_setup_rejects_remote_model_endpoint():
    from quickcut.production_pipeline import PipelineError
    with pytest.raises(PipelineError, match="只允许连接本机"):
        demo.ensure_ollama("https://model.example.test", "qwen2.5vl:7b")


@pytest.fixture
def local_demo(tmp_path, monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUICKCUT_VLM_MODEL", "qwen2.5vl:3b")
    monkeypatch.setenv("QUICKCUT_OLLAMA_URL", "http://127.0.0.1:11435")
    monkeypatch.setenv("QUICKCUT_ENCODER", "openclip")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"source bytes are not decoded by this orchestration test")
    shots = []
    for index in range(3):
        frame = tmp_path / f"frame-{index}.png"
        Image.new("RGB", (32, 18), (index * 50, 100, 180)).save(frame)
        shots.append(Shot(f"shot-{index:04d}", index * 3, (index + 1) * 3,
                          keyframe=str(frame), transcript="Source landscape.", backend="openclip"))
    manifest = IndexManifest("demo-index", str(video), video.stat().st_size, 1.0, 9.0, 3.0, 3.0,
                             "fixture", "openclip", shots, transcript_backend="faster-whisper")
    timeline = Timeline("1.0", str(video), "landscape", 9.0, manifest.index_id, "source",
                        [TimelineItem(f"item-{i}", "source", shot.shot_id, shot.start, shot.end)
                         for i, shot in enumerate(shots)])
    state = {"manifest": manifest, "indexes": [], "requests": [], "renders": [],
             "ensure": [], "response_model": None}
    monkeypatch.setattr(demo, "ensure_ollama", lambda url, model: state["ensure"].append((url, model)))
    def index(*args, **kwargs):
        state["indexes"].append(kwargs)
        return manifest
    monkeypatch.setattr(demo, "build_index", index)
    monkeypatch.setattr(demo, "localize", lambda *a, **kw: deepcopy(timeline))
    monkeypatch.setattr(story_module, "localize", lambda *a, **kw: deepcopy(timeline))
    def reply(request, **kwargs):
        payload = json.loads(request.data)
        state["requests"].append(payload)
        plan = {"shot_order": [s.shot_id for s in shots], "narration": "The path crosses green hills.",
                "beats": [{"id": name, "text": "A source view.", "source_shot_ids": [shot.shot_id]}
                          for name, shot in zip(("hook", "journey", "close"), shots)]}
        return io.BytesIO(json.dumps({"model": state["response_model"] or payload["model"],
                                     "done": True, "response": json.dumps(plan)}).encode())
    monkeypatch.setattr(vlm, "_open_ollama", reply)
    def render(value, destination, **kwargs):
        state["renders"].append((Path(destination).name, kwargs))
        Path(destination).write_bytes(b"encoder fixture, not a playable movie")
        return Path(destination)
    monkeypatch.setattr(demo, "render_timeline", render)
    monkeypatch.setattr(demo, "render_story_timeline", render)
    state["args"] = ["--video", str(video), "--model", "qwen2.5vl:7b", "--duration", "9"]
    return state


def test_requested_model_reaches_real_planner_binding_and_summary(local_demo, tmp_path):
    assert demo.main(local_demo["args"]) == 0
    assert local_demo["ensure"] == [("http://127.0.0.1:11435", "qwen2.5vl:7b")]
    assert local_demo["requests"][0]["model"] == "qwen2.5vl:7b"
    assert local_demo["indexes"][0]["asr_mode"] == "faster-whisper"
    assert [name for name, _ in local_demo["renders"]] == ["quickcut.mp4", "basic-vlog.mp4"]
    summary = json.loads(next((tmp_path / "outputs").glob("legacy-demo-*/run-summary.json")).read_text())
    assert summary["actual_model"] == summary["requested_model"] == "qwen2.5vl:7b"
    assert summary["demo_kind"] == "legacy_single_video_system_voice"
    assert summary["voice_engine"] == "system"
    assert summary["current_production_entry"] == "QuickCut.command"


def test_wrong_inference_model_is_rejected_before_voice_render(local_demo, tmp_path, capsys):
    local_demo["response_model"] = "qwen2.5vl:3b"
    assert demo.main(local_demo["args"]) == 1
    assert [name for name, _ in local_demo["renders"]] == ["quickcut.mp4"]
    assert not list((tmp_path / "outputs").glob("legacy-demo-*/basic-vlog.mp4"))
    assert "Story model mismatch" in capsys.readouterr().err


@pytest.mark.parametrize("backend", ["none", "other-asr"])
def test_missing_required_whisper_stops_before_search_or_render(local_demo, capsys, backend):
    local_demo["manifest"].transcript_backend = backend
    assert demo.main(local_demo["args"]) == 1
    assert local_demo["renders"] == local_demo["requests"] == []
    assert "faster-whisper is required" in capsys.readouterr().err


def test_default_outputs_are_unique_and_preserve_previous_run(local_demo, tmp_path):
    legacy = tmp_path / "outputs"
    legacy.mkdir()
    sentinel = legacy / "quickcut.mp4"
    sentinel.write_bytes(b"existing delivery")
    assert demo.main(local_demo["args"]) == 0
    first = next(legacy.glob("legacy-demo-*"))
    first_snapshot = {p.name: p.read_bytes() for p in first.iterdir()}
    assert demo.main(local_demo["args"]) == 0
    assert len(list(legacy.glob("legacy-demo-*"))) == 2
    assert {p.name: p.read_bytes() for p in first.iterdir()} == first_snapshot
    assert sentinel.read_bytes() == b"existing delivery"


def test_explicit_existing_output_is_refused_before_model_work(local_demo, tmp_path, capsys):
    existing = tmp_path / "delivery"
    existing.mkdir()
    (existing / "basic-vlog.mp4").write_bytes(b"previous delivery")
    assert demo.main(local_demo["args"] + ["--output-dir", str(existing)]) == 1
    assert local_demo["ensure"] == local_demo["indexes"] == local_demo["renders"] == []
    assert (existing / "basic-vlog.mp4").read_bytes() == b"previous delivery"
    assert "must be new" in capsys.readouterr().err


def test_new_explicit_output_directory_is_used(local_demo, tmp_path):
    destination = tmp_path / "requested-output"
    assert demo.main(local_demo["args"] + ["--output-dir", str(destination)]) == 0
    assert (destination / "quickcut.mp4").is_file()
    assert (destination / "basic-vlog.mp4").is_file()
    assert not (tmp_path / "outputs").exists()

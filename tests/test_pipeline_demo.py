"""无需 ffmpeg：替换媒体边界，真实执行 C、E 测试音调和 D。"""
import importlib.util
import json
from pathlib import Path

import pytest

from quickcut.media import digest, write_json
from quickcut.planning import build_outline, build_retrieval_request

SPEC = importlib.util.spec_from_file_location(
    "pipeline_demo", Path(__file__).resolve().parents[1] / "scripts/demo_pipeline.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


def read(root, name):
    return json.loads((root / name).read_text(encoding="utf-8"))


def manifest_document(root):
    return {"schema_version": "0.1.0", "kind": "AssetManifest", "document_id": "manifest-test",
            "project_id": "project-pipeline-demo", "revision": 1, "fixtures_only": False,
            "owner": "A", "inputs": [], "processing": {"status": "success", "errors": []},
            "assets": [{"asset_id": "synthetic", "revision": 1, "origin": "user", "media_kind": "video",
                        "uri": "source.mp4", "content_hash": digest(root / "source.mp4"),
                        "duration_us": 30_000_000, "audio": {"present": True, "sample_rate": 48000,
                        "channels": 1}, "video": {"width": 320, "height": 180, "fps": demo.OUTPUT["fps"]}}],
            "segments": [{"segment_id": f"shot-{i}", "asset_id": "synthetic", "asset_revision": 1,
                          "granularity": "coarse_shot", "parent_segment_id": None,
                          "source_range_us": {"begin": i * 10_000_000, "end": (i + 1) * 10_000_000},
                          "quality": {"usable": True, "flags": []}, "keyframes": []}
                         for i in range(3)]}


@pytest.fixture
def offline(monkeypatch):
    calls = {"ingest": 0, "validate": 0}
    monkeypatch.setattr(demo, "capabilities", lambda: {"test_boundary": "media mocked"})
    monkeypatch.setattr(demo, "git_state", lambda: {"head": "test-only"})

    def synthesize(root, window_us):
        source = root / "source.mp4"
        source.write_bytes(b"unit-test media boundary; not a real video")
        return source

    def ingest(root, sources, **kwargs):
        calls["ingest"] += 1
        assert kwargs["config"].window_us >= 3_500_000
        manifest = manifest_document(root)
        write_json(root / "asset-manifest.json", manifest)
        return manifest

    def validate(root, manifest, assets, voice=None):
        calls["validate"] += 1
        assert digest(root / "source.mp4") == manifest["assets"][0]["content_hash"]
        for asset in (voice or {}).get("assets", []):
            assert digest(root / asset["uri"]) == asset["content_hash"]
        return {"status": "passed", "test_media_boundary": True}

    monkeypatch.setattr(demo, "synthesize_video", synthesize)
    monkeypatch.setattr(demo, "ingest_assets", ingest)
    monkeypatch.setattr(demo, "validate_media", validate)
    return calls


def test_nonempty_root_refused_before_work(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "keep.txt"
    marker.write_bytes(b"keep")
    monkeypatch.setattr(demo, "capabilities", lambda: pytest.fail("拒绝前不得运行阶段"))
    assert demo.main(["--root", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "ROOT_NOT_EMPTY"
    assert marker.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [marker]


def test_symlink_root_refused(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(demo.DemoError, match="符号链接"):
        demo.prepare_root(alias)
    assert not list(target.iterdir())


@pytest.mark.parametrize("seconds", [5, 10, 15])
def test_retrieval_binds_manifest_and_request(tmp_path, seconds):
    (tmp_path / "source.mp4").write_bytes(b"fixture")
    manifest = manifest_document(tmp_path)
    brief = demo.make_brief(manifest["project_id"], seconds)
    outline = build_outline(brief)
    request = build_retrieval_request(brief, manifest, outline, broll_queries=False)
    labeled, candidate = demo.retrieve_candidates(manifest, request, outline, tmp_path)
    assert candidate["kind"] == "CandidateSet"
    assert candidate["owner"] == "B"
    assert candidate["fixtures_only"] is True
    assert candidate["inputs"] == [demo.ref(labeled), demo.ref(request)]
    assert candidate["results"]
    assert all(c["ranking_score"] > 0 for row in candidate["results"] for c in row["candidates"])
    assert manifest["fixtures_only"] is False


@pytest.mark.parametrize("seconds", [10, 15])
def test_supported_real_c_e_d(tmp_path, offline, seconds):
    report = demo.run_demo(tmp_path, target_seconds=seconds)
    assert report["status"] == "ready_for_render", report.get("feedback")
    assert report["calls"] == {"A": 1, "B": 1, "E": 1, "D": 1}
    assert report["fixturesOnly"] is True and report["manifestFixturesOnly"] is False
    assert report["manifest_unchanged"] is True
    timeline = read(tmp_path, "timeline-r1.json")
    assert timeline["duration_us"] == seconds * 1_000_000
    assert {t["kind"] for t in timeline["tracks"]} == {"video", "source_audio", "narration", "subtitle"}
    assert all(t.get("clips", t.get("cues")) for t in timeline["tracks"])
    voice = read(tmp_path, "voice-package-r1.json")
    assert voice["measurement_source"] == "fixture_tone"
    assert all(not c["cache_hit"] for c in voice["cues"])
    assert all(s["elapsed_seconds"] >= 0 for s in report["stages"])
    assert report["render"]["output"] == timeline["output"]
    for uri in report["render"]["files"].values():
        assert not Path(uri).is_absolute() and (tmp_path / uri).is_file()
    union = read(tmp_path, "assets-union-r1.json")["assets"]
    assert union == read(tmp_path, "asset-manifest.json")["assets"] + voice["assets"]
    assert read(tmp_path, "run-report.json") == report
    assert offline["ingest"] == 1 and offline["validate"] == 2


@pytest.mark.parametrize("seconds", [5, 10, 15])
def test_unsupported_stops_before_bridge(tmp_path, offline, monkeypatch, seconds):
    monkeypatch.setattr(demo.integration, "build_from_plan", lambda *a, **kw: pytest.fail("不得调用 E/D"))
    report = demo.run_demo(tmp_path, "unsupported", seconds)
    assert report["status"] == "needs_input"
    assert report["calls"] == {"A": 1, "B": 1, "E": 0, "D": 0}
    assert report["expected_stop"] is True
    assert report["render"] is None
    assert report["feedback"]["affected_ids"] == ["beat-middle"]
    assert not list(tmp_path.glob("voice-package*"))
    assert not list(tmp_path.glob("timeline*"))


def test_edit_only_one_cue_misses_without_reingest(tmp_path, offline):
    report = demo.run_demo(tmp_path, "edit")
    assert report["status"] == "ready_for_render", report.get("feedback")
    assert offline["ingest"] == 1
    assert report["calls"] == {"A": 1, "B": 1, "E": 2, "D": 2}
    assert report["manifest_hash_before"] == report["manifest_hash_after"]
    first = read(tmp_path, "voice-package-r1.json")
    second = read(tmp_path, "voice-package-r2.json")
    assert [c["cue_id"] for c in second["cues"] if not c["cache_hit"]] == ["cue-02"]
    assert second["invalidated_cue_ids"] == ["cue-02"]
    assert report["edit"]["cache_hits"] == ["cue-01", "cue-03"]
    assert second["revision"] == read(tmp_path, "story-plan-r2.json")["revision"] == 2
    assert first["inputs"][0]["revision"] == 1 and second["inputs"][0]["revision"] == 2
    assert report["render"]["files"]["timeline"] == "timeline-r2.json"


def test_five_seconds_reports_real_voice_budget_failure(tmp_path, offline):
    report = demo.run_demo(tmp_path, target_seconds=5)
    assert report["status"] == "failed"
    assert report["feedback"]["code"] == "VOICE_NEEDS_RETIME"
    assert report["feedback"]["affected_ids"] == ["cue-03"]
    assert report["calls"] == {"A": 1, "B": 1, "E": 1, "D": 0}
    assert read(tmp_path, "voice-package-r1.json")["status"] == "needs_retime"
    assert len(read(tmp_path, "assets-union-r1.json")["assets"]) == 4
    assert report["render"] is None
    assert not (tmp_path / "timeline-r1.json").exists()


def test_setup_failure_persists_feedback_and_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "git_state", lambda: {})
    def unavailable():
        raise RuntimeError("ffprobe 缺失")
    monkeypatch.setattr(demo, "capabilities", unavailable)
    assert demo.main(["--root", str(tmp_path)]) == 2
    stderr = json.loads(capsys.readouterr().err)
    assert stderr["feedback"]["message"] == "ffprobe 缺失"
    assert read(tmp_path, "feedback.json")["stage"] == "setup"
    assert read(tmp_path, "run-report.json")["status"] == "failed"


def test_manifest_validation_failure_stops_c_e_d(tmp_path, offline, monkeypatch):
    def corrupt(*args):
        raise ValueError("媒体哈希不一致")
    monkeypatch.setattr(demo, "validate_media", corrupt)
    report = demo.run_demo(tmp_path)
    assert report["status"] == "failed"
    assert report["calls"] == {"A": 1, "B": 0, "E": 0, "D": 0}
    assert report["feedback"]["stage"] == "A-validate"
    assert not (tmp_path / "candidate-set.json").exists()


def test_bridge_observers_restored_on_d_failure(tmp_path, offline, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("D 拒绝")
    original_voice = demo.integration.voice_package.build_voice_package
    monkeypatch.setattr(demo.integration, "compile_timeline", fail)
    report = demo.run_demo(tmp_path)
    assert report["status"] == "failed" and report["render"] is None
    assert (tmp_path / "voice-package-r1.json").is_file()
    assert not (tmp_path / "timeline-r1.json").exists()
    assert demo.integration.voice_package.build_voice_package is original_voice
    assert demo.integration.compile_timeline is fail


def test_media_validation_calls_a_and_checks_pts_and_paths(tmp_path, monkeypatch):
    (tmp_path / "source.mp4").write_bytes(b"test boundary")
    manifest = manifest_document(tmp_path)
    asset = manifest["assets"][0]
    asset.update(proxy_uri="proxy.mp4", source_pts_uri="pts.json", proxy_mapping_uri="mapping.json")
    (tmp_path / "proxy.mp4").write_bytes(b"test proxy boundary")
    frames = [{"source_pts": 0, "source_time_us": 0}]
    write_json(tmp_path / "pts.json", {"frames": frames})
    write_json(tmp_path / "mapping.json", {"frames": [{**frames[0], "proxy_pts": 0, "proxy_time_us": 0}]})
    validated, commands = [], []
    monkeypatch.setattr(demo, "validate_asset_manifest", lambda *a: validated.append(a))
    monkeypatch.setattr(demo, "_ffmpeg_binary", lambda: "unused-ffmpeg")
    monkeypatch.setattr(demo, "run_command", lambda args: commands.append(args))
    assert demo.validate_media(tmp_path, manifest, manifest["assets"])["status"] == "passed"
    assert validated == [(manifest, tmp_path)] and len(commands) == 2
    write_json(tmp_path / "mapping.json", {"frames": []})
    with pytest.raises(demo.DemoError, match="PTS"):
        demo.validate_media(tmp_path, manifest, manifest["assets"])
    asset["proxy_uri"] = "../outside.mp4"
    with pytest.raises(ValueError, match="Unsafe"):
        demo.validate_media(tmp_path, manifest, manifest["assets"])


def test_cli_success_defaults_to_ten_seconds(tmp_path, offline, capsys):
    assert demo.main(["--root", str(tmp_path), "--case", "edit"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["target_seconds"] == 10
    assert report["status"] == "ready_for_render"


@pytest.fixture
def index_backend(monkeypatch, tmp_path):
    """Replace only neural inference; execute the real E package and D bridge."""
    import array
    import shutil
    from quickcut import indextts2, voice_package

    calls = []

    class Backend:
        def __init__(self, config):
            self.config = config

        def cue_settings(self, config, language):
            return Path(config["reference_audio"]), {
                "engine": "indextts2", "model_revision": config["model_revision"],
                "model": "IndexTeam/IndexTTS-2", "language": language, "seed": 42,
            }

        def synthesize(self, text, output, reference, seed):
            calls.append((text, reference, seed))
            voice_package.write_pcm(output, array.array("h", [1500] * 48000))

    def forbidden(*args, **kwargs):
        pytest.fail("IndexTTS2 mode must never select a system voice or fixture tone")

    monkeypatch.setattr(indextts2, "IndexTTS2Backend", Backend)
    monkeypatch.setattr(voice_package, "select_profile", forbidden)
    monkeypatch.setattr(voice_package, "normalize_wav", shutil.copyfile)
    monkeypatch.setattr(demo, "REPO", tmp_path)
    speaker = tmp_path / "inputs/reference.wav"
    speaker.parent.mkdir()
    voice_package.write_pcm(speaker, array.array("h", [1500] * 480))
    config = tmp_path / "indextts.json"
    config.write_text(json.dumps({"engine": "indextts2", "model_revision": "740dcaf",
                                 "reference_audio": "inputs/reference.wav", "model_dir": "models/index"}))
    return config, calls, Backend


def test_explicit_index_config_executes_e_and_keeps_synthetic_provenance(tmp_path, offline, index_backend):
    config, calls, _ = index_backend
    root = tmp_path / "run"
    report = demo.run_demo(root, target_seconds=15, voice_config=config)
    assert report["status"] == "ready_for_render", report.get("feedback")
    assert report["fixturesOnly"] is True
    assert report["capabilities"]["E"]["package_ready"] is True
    assert report["voice"]["synthesis_profile"]["model_revision"] == "740dcaf"
    voice = read(root, "voice-package-r1.json")
    assert voice["measurement_source"] == "measured_pcm_samples"
    assert voice["fixtures_only"] is True
    assert len(calls) == 3
    assert all(call[1] == demo.REPO / "inputs/reference.wav" for call in calls)
    assert "IndexTTS2" in report["limitations"][2]


def test_index_failure_is_saved_and_never_falls_back(tmp_path, offline, index_backend, monkeypatch):
    config, _, backend = index_backend

    def unavailable(*args):
        raise RuntimeError("IndexTTS2 unavailable")

    monkeypatch.setattr(backend, "synthesize", unavailable)
    root = tmp_path / "run"
    report = demo.run_demo(root, voice_config=config)
    assert report["status"] == "failed"
    assert report["feedback"]["code"] == "VOICE_NOT_READY"
    assert report["voice"]["status"] == "failed"
    assert report["voice"]["synthesis_profile"]["engine"] == "indextts2"
    assert all(error["message"] == "IndexTTS2 unavailable" for error in report["voice"]["errors"])
    assert report["calls"]["D"] == 0
    assert not (root / "timeline-r1.json").exists()


@pytest.mark.parametrize("config", [{}, {"engine": "auto"}, [], {"engine": "indextts2", "cue_overrides": []}])
def test_voice_config_rejects_implicit_or_invalid_backend_before_ingest(tmp_path, offline, config):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(config))
    report = demo.run_demo(tmp_path / "run", voice_config=path)
    assert report["status"] == "failed"
    assert report["feedback"]["code"] == "INVALID_VOICE_CONFIG"
    assert report["calls"] == {"A": 0, "B": 0, "E": 0, "D": 0}


def test_voice_config_missing_models_reports_setup_failure(tmp_path, offline):
    config = tmp_path / "missing.json"
    config.write_text(json.dumps({"engine": "indextts2", "model_revision": "740dcaf",
                                 "model_dir": str(tmp_path / "missing-models")}))
    report = demo.run_demo(tmp_path / "run", voice_config=config)
    assert report["feedback"]["code"] == "INVALID_VOICE_CONFIG"
    assert "Missing local IndexTTS2 model files" in report["feedback"]["message"]
    assert report["calls"]["A"] == 0


@pytest.mark.parametrize("overrides", [{"rate": 145}, {"model_revision": "another-version"}, {"emotion": "happy"}])
def test_voice_config_rejects_unsupported_cue_overrides_before_ingest(tmp_path, offline, index_backend, overrides):
    config_path, calls, _ = index_backend
    config = read(tmp_path, config_path.name)
    config["cue_overrides"] = {"cue-01": overrides}
    config_path.write_text(json.dumps(config))
    report = demo.run_demo(tmp_path / "run", voice_config=config_path)
    assert report["feedback"]["code"] == "INVALID_VOICE_CONFIG"
    assert "only reference_audio and seed" in report["feedback"]["message"]
    assert report["calls"]["A"] == 0 and calls == []


@pytest.mark.parametrize("scope", ["base", "cue_override"])
@pytest.mark.parametrize("case", ["missing", "invalid", "empty", "truncated"])
def test_voice_config_validates_each_reference_wav_before_ingest(tmp_path, offline, index_backend, scope, case):
    import wave

    config_path, calls, _ = index_backend
    config = read(tmp_path, config_path.name)
    reference = tmp_path / "bad-reference.wav"
    if case == "invalid":
        reference.write_bytes(b"not audio")
    elif case in {"empty", "truncated"}:
        with wave.open(str(reference), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            if case == "truncated":
                wav.writeframes(b"\x01\x00" * 160)
        if case == "truncated":
            reference.write_bytes(reference.read_bytes()[:-10])
    if scope == "base":
        config["reference_audio"] = str(reference)
    else:
        config["cue_overrides"] = {"cue-01": {"reference_audio": str(reference), "seed": 7}}
    config_path.write_text(json.dumps(config))
    report = demo.run_demo(tmp_path / "run", voice_config=config_path)
    assert report["status"] == "failed"
    assert report["feedback"]["code"] == "INVALID_VOICE_CONFIG"
    assert report["calls"]["A"] == 0 and calls == []


def test_voice_config_checks_per_cue_settings(tmp_path, offline, index_backend, monkeypatch):
    config_path, _, backend = index_backend
    config = read(tmp_path, config_path.name)
    config["cue_overrides"] = {"cue-01": {"seed": -1}}
    config_path.write_text(json.dumps(config))
    original = backend.cue_settings

    def settings(self, config, language):
        if config.get("seed") == -1:
            raise ValueError("IndexTTS2 seed must be non-negative")
        return original(self, config, language)

    monkeypatch.setattr(backend, "cue_settings", settings)
    report = demo.run_demo(tmp_path / "run", voice_config=config_path)
    assert report["feedback"]["code"] == "INVALID_VOICE_CONFIG"
    assert "seed" in report["feedback"]["message"]
    assert report["calls"]["A"] == 0

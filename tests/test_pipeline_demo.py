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
def test_manual_fixture_explicit_and_version_bound(tmp_path, seconds):
    (tmp_path / "source.mp4").write_bytes(b"fixture")
    manifest = manifest_document(tmp_path)
    brief = demo.make_brief(manifest["project_id"], seconds)
    request = build_retrieval_request(brief, manifest, build_outline(brief), broll_queries=False)
    candidate = demo.manual_candidates(manifest, request)
    assert candidate["fixtures_only"] is True
    assert candidate["owner"] == "B"
    assert candidate["provenance"]["source"] == "manual_fixture"
    assert candidate["provenance"]["retrieval_executed"] is False
    assert candidate["inputs"] == [demo.ref(manifest), demo.ref(request)]
    assert all(c["evidence"] == ["fixture_annotation"] for r in candidate["results"] for c in r["candidates"])
    assert manifest["fixtures_only"] is False


@pytest.mark.parametrize("seconds", [10, 15])
def test_supported_real_c_e_d(tmp_path, offline, seconds):
    report = demo.run_demo(tmp_path, target_seconds=seconds)
    assert report["status"] == "ready_for_render", report.get("feedback")
    assert report["calls"] == {"A": 1, "E": 1, "D": 1}
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
    assert report["calls"] == {"A": 1, "E": 0, "D": 0}
    assert report["expected_stop"] is True
    assert report["render"] is None
    assert report["feedback"]["affected_ids"] == ["beat-middle"]
    assert not list(tmp_path.glob("voice-package*"))
    assert not list(tmp_path.glob("timeline*"))


def test_edit_only_one_cue_misses_without_reingest(tmp_path, offline):
    report = demo.run_demo(tmp_path, "edit")
    assert report["status"] == "ready_for_render", report.get("feedback")
    assert offline["ingest"] == 1
    assert report["calls"] == {"A": 1, "E": 2, "D": 2}
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
    assert report["calls"] == {"A": 1, "E": 1, "D": 0}
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
    assert report["calls"] == {"A": 1, "E": 0, "D": 0}
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

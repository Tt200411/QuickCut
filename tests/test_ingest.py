from __future__ import annotations

import copy
import json
import shutil
import wave
from pathlib import Path

import pytest

import quickcut.ingest as ingest
from quickcut.core import _ffmpeg_binary
from quickcut.ingest import IngestConfig, SourceSpec, ingest_assets, validate_asset_manifest
from quickcut.media import MediaError, digest, read_json, resolve_uri, run_media
from quickcut.types import TranscriptSegment
from scripts.smoke_ingest import generate_video


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "sample.mp4"
    generate_video(path, duration=1.2, fps="25")
    return path


def stages(root):
    return read_json(root / ".quickcut/run-report.json")["stages"]


def test_identity_cache_origin_and_relocation(video, tmp_path):
    root = tmp_path / "project"
    first = ingest_assets(root, [SourceSpec(video)], project_id="project-test")
    assert first["processing"]["status"] == "success"
    assert first["fixtures_only"] is False
    assert first["assets"][0]["transcript"]["status"] == "no_audio"
    again = ingest_assets(root, [SourceSpec(video)])
    assert first == again and all(s["cache_hit"] for s in stages(root))
    renamed = tmp_path / "renamed.mp4"
    shutil.copyfile(video, renamed)
    same = ingest_assets(root, [SourceSpec(renamed)])
    assert len(same["assets"]) == 1 and same == first
    external = ingest_assets(root, [SourceSpec(video, "external", "test library")])
    assert len(external["assets"]) == 2
    assert {a["origin"] for a in external["assets"]} == {"user", "external"}
    assert all(s["cache_hit"] for s in stages(root) if s["asset_id"] == first["assets"][0]["asset_id"])
    relocated = tmp_path / "moved-project"
    shutil.copytree(root, relocated)
    result = ingest_assets(relocated, [])
    assert result == external and all(s["cache_hit"] for s in stages(relocated))
    validate_asset_manifest(result, relocated)


def test_modified_file_keeps_identity_bumps_revision_and_preserves_old_media(video, tmp_path):
    root = tmp_path / "project"
    first = ingest_assets(root, [SourceSpec(video)])
    generate_video(video, duration=2, fps="30")
    second = ingest_assets(root, [SourceSpec(video)])
    old, new = first["assets"][0], second["assets"][0]
    assert old["asset_id"] == new["asset_id"] and new["revision"] == old["revision"] + 1
    assert new["content_hash"] != old["content_hash"]
    assert resolve_uri(root, old["uri"]).is_file()
    assert second["revision"] > first["revision"]
    assert all(s["asset_revision"] == new["revision"] for s in second["segments"])


def test_broken_keyframe_is_rebuilt(video, tmp_path):
    root = tmp_path / "project"
    first = ingest_assets(root, [SourceSpec(video)])
    frame = next(s["keyframes"][0] for s in first["segments"] if s["keyframes"])
    resolve_uri(root, frame["uri"]).write_bytes(b"not png")
    second = ingest_assets(root, [])
    assert second["processing"]["status"] == "success"
    assert not next(s for s in stages(root) if s["stage"] == "keyframes")["cache_hit"]
    assert next(s for s in stages(root) if s["stage"] == "proxy")["cache_hit"]
    validate_asset_manifest(second, root)


def test_failure_isolation_and_missing_input_is_not_replaced_by_stale_success(video, tmp_path):
    root = tmp_path / "project"
    missing = tmp_path / "missing.mp4"
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"invalid video")
    first = ingest_assets(root, [SourceSpec(video), SourceSpec(missing), SourceSpec(broken)])
    assert first["processing"]["status"] == "partial" and len(first["assets"]) == 1
    assert len(first["processing"]["errors"]) == 2
    video.unlink()
    second = ingest_assets(root, [SourceSpec(video)])
    assert second["processing"]["status"] == "failed" and not second["assets"]
    assert all(e["affected_ids"] for e in second["processing"]["errors"])


def test_asr_states_and_config_only_invalidates_asr(tmp_path, monkeypatch):
    model_dir = tmp_path / "mock-weights"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"mock test weights; not an inference model")
    monkeypatch.setattr(ingest, "_resolve_asr_model", lambda config: model_dir)
    path = tmp_path / "audio.mp4"
    generate_video(path, duration=1, audio="silence")
    root = tmp_path / "project"
    disabled = ingest_assets(root, [SourceSpec(path)])
    assert disabled["assets"][0]["transcript"]["status"] == "disabled"

    class EmptyASR:
        def __init__(self, **kwargs):
            pass

        def transcribe(self, path):
            return []

    monkeypatch.setattr(ingest, "FasterWhisperASR", EmptyASR)
    enabled = IngestConfig(asr="faster-whisper", asr_language="zh")
    silent = ingest_assets(root, [], config=enabled)
    assert silent["assets"][0]["transcript"]["status"] == "no_speech"
    assert all(s["cache_hit"] for s in stages(root) if s["stage"] != "asr")

    class SpeechASR(EmptyASR):
        def transcribe(self, path):
            return [TranscriptSegment("mock-1", 0.1, 0.8, "测试语音")]

    monkeypatch.setattr(ingest, "FasterWhisperASR", SpeechASR)
    speech = ingest_assets(root, [], config=IngestConfig(asr="faster-whisper", asr_model="base"))
    assert speech["assets"][0]["transcript"]["status"] == "success"
    shot = next(s for s in speech["segments"] if s["granularity"] == "coarse_shot")
    assert shot["transcript"]["text"] == "测试语音"
    assert shot["transcript"]["segments"][0]["source_range_us"] == {"begin": 100000, "end": 800000}
    assert all(s["cache_hit"] for s in stages(root) if s["stage"] != "asr")

    reused = ingest_assets(root, [], config=IngestConfig(asr="faster-whisper", asr_model="base"))
    assert reused == speech and all(s["cache_hit"] for s in stages(root))
    (model_dir / "model.bin").write_bytes(b"changed mock weights")
    changed_weights = ingest_assets(root, [], config=IngestConfig(asr="faster-whisper", asr_model="base"))
    assert changed_weights["revision"] > reused["revision"]
    assert not next(s for s in stages(root) if s["stage"] == "asr")["cache_hit"]
    assert all(s["cache_hit"] for s in stages(root) if s["stage"] != "asr")

    class BrokenASR(EmptyASR):
        def transcribe(self, path):
            raise RuntimeError("test backend failure")

    monkeypatch.setattr(ingest, "FasterWhisperASR", BrokenASR)
    failed = ingest_assets(root, [], config=IngestConfig(asr="faster-whisper", asr_model="small", max_attempts=2))
    assert failed["assets"][0]["transcript"]["status"] == "failed"
    assert failed["processing"]["status"] == "partial"
    assert failed["processing"]["errors"][0]["attempts"] == 2
    assert failed["assets"][0]["proxy_status"] == "success"
    assert failed["assets"][0]["transcript"]["profile"]["weight_hashes"]["model.bin"] == digest(model_dir / "model.bin")


def test_keyframe_failure_keeps_media_but_does_not_publish_visual_evidence(video, tmp_path, monkeypatch):
    def broken(*args):
        raise MediaError("test extraction failure")
    monkeypatch.setattr(ingest, "extract_frames", broken)
    root = tmp_path / "project"
    result = ingest_assets(root, [SourceSpec(video)])
    assert len(result["assets"]) == 1 and result["processing"]["status"] == "partial"
    assert result["assets"][0]["proxy_status"] == "success"
    assert all(not s["quality"]["usable"] and not s["keyframes"] for s in result["segments"])
    assert result["processing"]["errors"][0]["code"] == "KEYFRAME_MISSING"


def test_missing_model_is_failure_not_no_speech(tmp_path, monkeypatch):
    path = tmp_path / "audio.mp4"
    generate_video(path, duration=1, audio="tone")
    def unavailable(config):
        raise RuntimeError("model not installed")
    monkeypatch.setattr(ingest, "_resolve_asr_model", unavailable)
    result = ingest_assets(tmp_path / "project", [SourceSpec(path)], config=IngestConfig(asr="faster-whisper"))
    assert result["assets"][0]["transcript"]["status"] == "failed"
    assert result["processing"]["status"] == "partial"


def test_nonzero_source_origin_preserves_audio_for_asr(tmp_path, monkeypatch):
    original = tmp_path / "original.mp4"
    generate_video(original, duration=2, audio="tone")
    shifted = tmp_path / "shifted.mp4"
    run_media([_ffmpeg_binary(), "-v", "error", "-y", "-i", str(original),
               "-c", "copy", "-output_ts_offset", "5", str(shifted)])
    model_dir = tmp_path / "mock-model"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"mock weights")
    monkeypatch.setattr(ingest, "_resolve_asr_model", lambda config: model_dir)

    class InspectASR:
        def __init__(self, **kwargs):
            assert kwargs["allow_missing_audio"] is False

        def transcribe(self, path):
            with wave.open(str(path)) as wav:
                assert wav.getnframes() / wav.getframerate() > 1.8
                assert any(wav.readframes(wav.getnframes()))
            return [TranscriptSegment("mock", 0, 1, "test")]

    monkeypatch.setattr(ingest, "FasterWhisperASR", InspectASR)
    root = tmp_path / "project"
    result = ingest_assets(root, [SourceSpec(shifted)], config=IngestConfig(asr="faster-whisper"))
    assert result["processing"]["status"] == "success"
    assert result["assets"][0]["video"]["source_origin_us"] >= 4_900_000
    assert result["assets"][0]["transcript"]["status"] == "success"


def test_vfr_fractional_fps_source_and_proxy_mapping(tmp_path):
    path = tmp_path / "variable.mp4"
    generate_video(path, duration=2, fps="30000/1001", vfr=True)
    root = tmp_path / "project"
    manifest = ingest_assets(root, [SourceSpec(path)], config=IngestConfig(window_us=700000, coarse_window_us=1400000))
    assert manifest["processing"]["status"] == "success"
    asset = manifest["assets"][0]
    assert asset["video"]["variable_frame_rate"] is True
    mapping = read_json(resolve_uri(root, asset["proxy_mapping_uri"]))
    assert all(abs(f["source_time_us"] - f["proxy_time_us"]) <= 2 for f in mapping["frames"])
    assert len({f["source_pts"] for f in mapping["frames"]}) == len(mapping["frames"])
    shots = [s for s in manifest["segments"] if s["granularity"] == "coarse_shot"]
    assert shots[0]["source_range_us"]["begin"] == 0
    assert shots[-1]["source_range_us"]["end"] == asset["duration_us"]
    assert all(a["source_range_us"]["end"] == b["source_range_us"]["begin"] for a, b in zip(shots, shots[1:]))
    validate_asset_manifest(manifest, root)


@pytest.mark.parametrize("uri", ["../secret", "/absolute", "C:/absolute", "x\\file"])
def test_unsafe_uri_rejected(tmp_path, uri):
    with pytest.raises(ValueError):
        resolve_uri(tmp_path, uri)


@pytest.mark.parametrize("kwargs", [{"window_us": 0}, {"coarse_window_us": 1}, {"proxy_width": 3},
                                   {"asr": "auto"}, {"max_attempts": 4}])
def test_invalid_config_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        ingest_assets(tmp_path / "project", [], config=IngestConfig(**kwargs))


def test_stale_reference_and_invalid_source_range_rejected(video, tmp_path):
    root = tmp_path / "project"
    original = ingest_assets(root, [SourceSpec(video)])
    stale = copy.deepcopy(original)
    stale["segments"][0]["asset_revision"] += 1
    with pytest.raises(ValueError, match="Stale"):
        validate_asset_manifest(stale, root)
    invalid = copy.deepcopy(original)
    invalid["segments"][0]["source_range_us"]["end"] += 10000000
    with pytest.raises(ValueError, match="interval"):
        validate_asset_manifest(invalid, root)


def test_cli_partial_exit_and_sources_relative_to_json(video, tmp_path, capsys):
    source_list = tmp_path / "sources.json"
    source_list.write_text(json.dumps([{"path": video.name}, {"path": "absent.mp4"}]), encoding="utf-8")
    assert ingest.main(["--sources", str(source_list), "--project-root", str(tmp_path / "project")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


def test_project_mismatch_and_single_writer(video, tmp_path):
    root = tmp_path / "project"
    ingest_assets(root, [SourceSpec(video)], project_id="project-test")
    with pytest.raises(ValueError, match="mismatch"):
        ingest_assets(root, [], project_id="project-other")
    lock = root / ".quickcut/ingest.lock"
    lock.touch()
    with pytest.raises(RuntimeError, match="Another ingestion"):
        ingest_assets(root, [])


def test_new_project_requires_input(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        ingest_assets(tmp_path / "empty", [])


def test_source_key_preserves_identity_across_move_and_edit(video, tmp_path):
    root = tmp_path / "project"
    first = ingest_assets(root, [SourceSpec(video, source_key="camera-1-take-1")])
    moved = tmp_path / "moved.mp4"
    generate_video(moved, duration=2)
    second = ingest_assets(root, [SourceSpec(moved, source_key="camera-1-take-1")])
    assert first["assets"][0]["asset_id"] == second["assets"][0]["asset_id"]
    assert second["assets"][0]["revision"] == 2

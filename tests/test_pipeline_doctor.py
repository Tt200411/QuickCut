"""离线预检测试：mock 工具不构成真实媒体或模型推理证据。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from quickcut import core, ingest, media
from scripts import doctor_pipeline as doctor


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("预检不应访问网络")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.fixture
def tools(monkeypatch):
    paths = {name: f"/mock/{name}" for name in ("ffmpeg", "ffprobe", "say")}
    monkeypatch.setattr(doctor.shutil, "which", lambda name: paths.get(name) or (
        name if name in paths.values() else None))
    monkeypatch.setattr(core, "_ffmpeg_binary", lambda: "/mock/ffmpeg")
    monkeypatch.setattr(media, "ffprobe", lambda: "/mock/ffprobe")
    monkeypatch.setattr(doctor.metadata, "version", lambda name: "1.2.3")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "mock version 1.0\nextra", ""))
    monkeypatch.setattr(doctor.subprocess, "run", run)
    return paths, run


def checks(report):
    return {item["name"]: item for item in report["required"]["checks"]}


def invoke(capsys, *args):
    code = doctor.main(list(args))
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    assert code == report["exit_code"]
    return code, report


def test_json_and_required_optional_status_are_separate(tools, capsys, monkeypatch):
    def version(name):
        if name == "torch":
            raise doctor.metadata.PackageNotFoundError(name)
        return "1.2.3"

    monkeypatch.setattr(doctor.metadata, "version", version)
    code, report = invoke(capsys, "--require-media")
    assert code == 0
    assert report["status"] == report["required"]["status"] == "ok"
    assert report["optional"]["status"] == "incomplete"
    assert report["environment"]["python"]
    assert report["environment"]["platform"]
    optional = {c["name"]: c for c in report["optional"]["checks"]}
    packages = {p["name"]: p for p in optional["packages"]["checks"]}
    assert packages["torch"] == {"name": "torch", "status": "missing", "version": None}
    assert packages["quickcut"]["version"] == "1.2.3"
    assert optional["ollama"]["status"] == "not_checked"
    assert optional["A.asset_manifest"]["status"] == "skipped"
    assert report["evidence"]["real_inference"] == "not_run"
    assert "fixture-tone" in report["evidence"]["fixtures"]
    assert report["policy"] == {"read_only": True, "network_access": False, "install": False, "download": False}
    for call in tools[1].call_args_list:
        assert call.args[0][1:] == ["-version"]
        assert call.kwargs["timeout"] == doctor.TOOL_TIMEOUT
        assert call.kwargs["stdin"] == subprocess.DEVNULL
        assert not call.kwargs.get("shell", False)


def test_all_tools_missing_are_structured_and_default_is_report_only(tools, monkeypatch, capsys):
    monkeypatch.setattr(core, "_ffmpeg_binary", Mock(side_effect=RuntimeError("缺少 FFmpeg")))
    monkeypatch.setattr(media, "ffprobe", Mock(side_effect=media.MediaError("缺少 FFprobe")))
    tools[0].clear()
    code, report = invoke(capsys)
    assert code == 0 and report["status"] == "degraded"
    assert report["required"]["enforced"] is False
    assert checks(report)["A.core.ffmpeg"]["error"]["type"] == "RuntimeError"
    assert checks(report)["A.media.ffprobe"]["error"]["type"] == "MediaError"
    assert checks(report)["E.ffmpeg_path"]["status"] == "failed"
    assert checks(report)["E.local_tts"]["status"] == "failed"
    assert invoke(capsys, "--require-media")[0] == 2
    tools[1].assert_not_called()


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe", "say"])
def test_each_required_media_dependency_can_fail_strict_mode(tools, name, capsys):
    del tools[0][name]
    assert invoke(capsys, "--require-media")[0] == 2


def test_a_override_does_not_satisfy_e_path(tools, monkeypatch, capsys):
    del tools[0]["ffmpeg"]
    tools[0]["a-override"] = "/mock/a-ffmpeg"
    monkeypatch.setattr(core, "_ffmpeg_binary", lambda: "/mock/a-ffmpeg")
    code, report = invoke(capsys, "--require-media")
    assert code == 2
    assert checks(report)["A.core.ffmpeg"]["status"] == "ok"
    assert checks(report)["E.ffmpeg_path"]["status"] == "failed"


@pytest.mark.parametrize("exception", [
    subprocess.TimeoutExpired(["ffmpeg", "-version"], doctor.TOOL_TIMEOUT),
    FileNotFoundError("执行前被移除"),
    PermissionError("不可执行"),
])
def test_subprocess_errors_have_no_traceback(tools, exception, capsys):
    tools[1].side_effect = exception
    code, report = invoke(capsys, "--require-media")
    assert code == 2
    assert checks(report)["A.core.ffmpeg"]["error"]["type"] == type(exception).__name__


@pytest.mark.parametrize("returncode,stdout,stderr", [(3, "", "broken"), (0, "", "")])
def test_invalid_version_response_fails(tools, capsys, returncode, stdout, stderr):
    tools[1].return_value = subprocess.CompletedProcess([], returncode, stdout, stderr)
    code, report = invoke(capsys, "--require-media")
    assert code == 2
    assert checks(report)["A.core.ffmpeg"]["status"] == "failed"


@pytest.mark.parametrize("engine", ["espeak-ng", "espeak"])
def test_e_alternative_system_tts_is_found(tools, engine, capsys):
    del tools[0]["say"]
    tools[0][engine] = f"/mock/{engine}"
    code, report = invoke(capsys, "--require-media")
    assert code == 0
    assert checks(report)["E.local_tts"]["selected"] == engine


def test_ollama_config_is_reported_without_execution(tools, monkeypatch, capsys):
    monkeypatch.setenv("QUICKCUT_OLLAMA_URL", "http://unreachable.invalid:11434")
    monkeypatch.setenv("QUICKCUT_VLM_MODEL", "not-installed")
    monkeypatch.setenv("QUICKCUT_VLM_TIMEOUT", "invalid-but-not-used")
    _, report = invoke(capsys, "--require-media")
    ollama = next(c for c in report["optional"]["checks"] if c["name"] == "ollama")
    assert ollama["configuration"]["QUICKCUT_VLM_MODEL"] == "not-installed"
    assert ollama["configuration"]["QUICKCUT_VLM_TIMEOUT"] == "invalid-but-not-used"
    assert len(tools[1].call_args_list) == 3
    assert all("ollama" not in call.args[0][0] for call in tools[1].call_args_list)


@pytest.fixture
def project(tmp_path):
    # 此文件只用于真实 A 哈希/引用校验；不是可解码媒体或真实推理样本。
    original = tmp_path / "original.mp4"
    original.write_bytes(b"hash-only test fixture")
    manifest = {
        "schema_version": "0.1.0", "kind": "AssetManifest", "fixtures_only": False,
        "assets": [{"asset_id": "asset-1", "revision": 1, "uri": "original.mp4",
                    "content_hash": media.digest(original), "duration_us": 1000000}],
        "segments": [],
    }
    (tmp_path / "asset-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, manifest


def save_manifest(root, manifest):
    (root / "asset-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_real_a_validator_is_called_read_only(tools, project, monkeypatch, capsys):
    root, manifest = project
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    validate = Mock(wraps=ingest.validate_asset_manifest)
    monkeypatch.setattr(ingest, "validate_asset_manifest", validate)
    code, report = invoke(capsys, "--project-root", str(root), "--require-media")
    assert code == 0
    validate.assert_called_once_with(manifest, root)
    assert checks(report)["A.asset_manifest"]["validation"] == "ok"
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after
    manifest["assets"][0]["content_hash"] = "sha256:wrong"
    save_manifest(root, manifest)
    code, report = invoke(capsys, "--project-root", str(root), "--require-media")
    assert code == 2
    assert "hash mismatch" in checks(report)["A.asset_manifest"]["error"]["message"]


@pytest.mark.parametrize("uri", ["../outside.mp4", "/tmp/outside.mp4", "C:\\outside.mp4", "http://host/media", "", 123])
def test_unsafe_uri_rejected_before_a_reads_files(tools, project, uri, monkeypatch, capsys):
    root, manifest = project
    manifest["assets"][0]["uri"] = uri
    save_manifest(root, manifest)
    validate = Mock()
    monkeypatch.setattr(ingest, "validate_asset_manifest", validate)
    code, report = invoke(capsys, "--project-root", str(root), "--require-media")
    assert code == 2
    assert checks(report)["A.asset_manifest"]["path_safety"] == "failed"
    validate.assert_not_called()


@pytest.mark.parametrize("target", ["manifest", "media", "transcript"])
def test_symlink_escape_rejected(tools, project, tmp_path, target, capsys):
    root, manifest = project
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_text(json.dumps(manifest), encoding="utf-8")
    if target == "manifest":
        path = root / "asset-manifest.json"
        path.unlink()
        path.symlink_to(outside)
    else:
        (root / "escape").symlink_to(outside)
        if target == "media":
            manifest["assets"][0]["uri"] = "escape"
        else:
            manifest["assets"][0]["transcript"] = {"uri": "escape"}
        save_manifest(root, manifest)
    code, report = invoke(capsys, "--project-root", str(root), "--require-media")
    assert code == 2
    assert checks(report)["A.asset_manifest"]["path_safety"] == "failed"


@pytest.mark.parametrize("case", ["missing_manifest", "missing_media", "empty_assets", "fixture", "malformed", "wrong_shape", "missing_fields"])
def test_manifest_failures_are_structured(tools, project, capsys, case):
    root, original = project
    manifest = copy.deepcopy(original)
    if case == "missing_media":
        (root / "original.mp4").unlink()
    elif case == "empty_assets":
        manifest["assets"] = []
    elif case == "fixture":
        manifest["fixtures_only"] = True
    elif case == "missing_fields":
        del manifest["assets"]
    save_manifest(root, manifest)
    if case == "missing_manifest":
        (root / "asset-manifest.json").unlink()
    elif case in ("malformed", "wrong_shape"):
        (root / "asset-manifest.json").write_text("{" if case == "malformed" else "[]", encoding="utf-8")
    code, report = invoke(capsys, "--project-root", str(root), "--require-media")
    assert code == 2
    entry = checks(report)["A.asset_manifest"]
    assert entry["status"] == "failed" and entry["error"]["type"]
    assert invoke(capsys, "--project-root", str(root))[0] == 0


def test_missing_project_root_is_not_created(tools, tmp_path, capsys):
    root = tmp_path / "does-not-exist"
    assert invoke(capsys, "--project-root", str(root), "--require-media")[0] == 2
    assert not root.exists()


@pytest.mark.parametrize("strict,expected", [(False, 0), (True, 2)])
def test_script_process_exit_code_with_empty_path(tmp_path, strict, expected):
    env = dict(os.environ, PATH="", QUICKCUT_FFMPEG=str(tmp_path / "no-ffmpeg"),
               QUICKCUT_FFPROBE=str(tmp_path / "no-ffprobe"), PYTHONDONTWRITEBYTECODE="1")
    script = Path(doctor.__file__).resolve()
    command = [sys.executable, str(script)] + (["--require-media"] if strict else [])
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True,
                            text=True, timeout=20, check=False)
    assert result.returncode == expected
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["exit_code"] == expected
    assert checks(report)["E.ffmpeg_path"]["status"] == "failed"
    assert checks(report)["A.media.ffprobe"]["status"] == "failed"


def test_a_import_failure_is_structured(tools, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "a_binary", Mock(side_effect=ImportError("A 不可导入")))
    code, report = invoke(capsys, "--require-media")
    assert code == 2
    assert checks(report)["A.core.ffmpeg"]["error"]["type"] == "ImportError"


@pytest.fixture
def index_config(tmp_path, monkeypatch):
    from quickcut import indextts2

    model_dir = tmp_path / "checkpoints"
    for filename in (
        "config.yaml", "gpt.pth", "s2mel.pth", "bpe.model", "wav2vec2bert_stats.pt",
        "feat1.pt", "feat2.pt", "hf_cache/w2v-bert-2.0/config.json",
        "hf_cache/w2v-bert-2.0/preprocessor_config.json",
        "hf_cache/w2v-bert-2.0/model.safetensors",
        "hf_cache/semantic_codec_model.safetensors", "hf_cache/campplus_cn_common.bin",
        "hf_cache/bigvgan/config.json", "hf_cache/bigvgan/bigvgan_generator.pt",
    ):
        path = model_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"file-presence fixture, not model weights")
    reference = tmp_path / "reference.wav"
    with wave.open(str(reference), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 160)
    config = {"engine": "indextts2", "model_dir": str(model_dir),
              "model_revision": "740dcaf", "reference_audio": str(reference), "device": "mps"}
    path = tmp_path / "voice-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(indextts2, "version", lambda name: "2.0.0")
    # A constructor reaching inference would be an accidental model load.
    monkeypatch.setattr(indextts2.IndexTTS2Backend, "synthesize",
                        Mock(side_effect=AssertionError("preflight must not synthesize")))
    torch = SimpleNamespace(
        device=lambda name: SimpleNamespace(type=name.partition(":")[0],
                                           index=int(name.partition(":")[2]) if ":" in name else None),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
        cuda=SimpleNamespace(is_available=lambda: True, device_count=lambda: 1),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    return path, config, torch


def test_index_preflight_uses_selected_model_without_requiring_say(tools, index_config, capsys):
    path, config, _ = index_config
    del tools[0]["say"]
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 0
    entry = checks(report)["E.local_tts"]
    assert entry["selected"] == "indextts2"
    assert entry["model_revision"] == "740dcaf"
    assert entry["device"] == "mps"
    assert entry["reference_audio"]["frames"] == 160
    assert entry["real_inference"] == "not_run"
    assert report["evidence"]["real_inference"] == "not_run"


def test_index_relative_paths_use_repository_from_another_cwd(tools, index_config, monkeypatch, capsys):
    path, config, _ = index_config
    monkeypatch.setattr(doctor, "PROJECT_ROOT", path.parent)
    other = path.parent / "other-working-directory"
    other.mkdir()
    monkeypatch.chdir(other)
    config["model_dir"] = "checkpoints"
    config["reference_audio"] = "reference.wav"
    config["cue_overrides"] = {"cue-01": {"reference_audio": "reference.wav"}}
    path.write_text(json.dumps(config), encoding="utf-8")
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 0
    entry = checks(report)["E.local_tts"]
    assert Path(entry["model_dir"]) == path.parent / "checkpoints"
    assert Path(entry["reference_audio"]["path"]) == path.parent / "reference.wav"

    # A missing per-cue reference must not be hidden by a valid base reference.
    config["cue_overrides"]["cue-01"]["reference_audio"] = "missing.wav"
    path.write_text(json.dumps(config), encoding="utf-8")
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 2
    assert "reference_audio" in checks(report)["E.local_tts"]["error"]["message"]


@pytest.mark.parametrize("case", ["invalid", "empty", "truncated"])
def test_index_override_wav_is_checked_beyond_file_presence(tools, index_config, capsys, case):
    path, config, _ = index_config
    reference = path.parent / "override.wav"
    if case == "invalid":
        reference.write_bytes(b"not audio")
    elif case == "empty":
        with wave.open(str(reference), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
    else:
        reference.write_bytes(Path(config["reference_audio"]).read_bytes()[:-2])
    config["cue_overrides"] = {"cue-01": {"reference_audio": str(reference)}}
    path.write_text(json.dumps(config), encoding="utf-8")
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 2
    entry = checks(report)["E.local_tts"]
    assert entry["status"] == "failed"
    assert "cue-01 reference_audio" in entry["error"]["message"]


@pytest.mark.parametrize("key", ["rate", "model_dir", "device", "emotion", "engine"])
def test_index_override_keys_match_e_contract(tools, index_config, capsys, key):
    path, config, _ = index_config
    config["cue_overrides"] = {"cue-01": {key: 145}}
    path.write_text(json.dumps(config), encoding="utf-8")
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 2
    assert "cue-01 overrides support only reference_audio and seed" in checks(report)["E.local_tts"]["error"]["message"]


def test_index_valid_seed_only_override_reuses_checked_base_wav(tools, index_config, capsys):
    path, config, _ = index_config
    config["cue_overrides"] = {"cue-01": {"seed": 7}}
    path.write_text(json.dumps(config), encoding="utf-8")
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 0
    assert checks(report)["E.local_tts"]["status"] == "ok"


@pytest.mark.parametrize("missing", ["gpt.pth", "hf_cache/w2v-bert-2.0/model.safetensors"])
def test_missing_index_weights_cannot_pass_using_system_voice(tools, index_config, capsys, missing):
    path, config, _ = index_config
    (Path(config["model_dir"]) / missing).unlink()
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 2 and report["status"] == "degraded"
    entry = checks(report)["E.local_tts"]
    assert entry["status"] == "failed" and entry["selected"] == "indextts2"
    assert missing in entry["error"]["message"]
    assert invoke(capsys, "--voice-config", str(path))[0] == 0


@pytest.mark.parametrize("case", ["missing", "invalid", "empty"])
def test_index_reference_failures_block_preflight(tools, index_config, capsys, case):
    path, config, _ = index_config
    reference = Path(config["reference_audio"])
    if case == "missing":
        reference.unlink()
    elif case == "invalid":
        reference.write_bytes(b"not a WAV")
    else:
        with wave.open(str(reference), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 2
    assert checks(report)["E.local_tts"]["status"] == "failed"


@pytest.mark.parametrize("device", ["mps", "cuda", "cuda:1"])
def test_index_unavailable_device_is_reported(tools, index_config, capsys, device):
    path, config, torch = index_config
    config["device"] = device
    path.write_text(json.dumps(config), encoding="utf-8")
    torch.backends.mps.is_available = lambda: False
    torch.cuda.is_available = lambda: device == "cuda:1"
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 2
    assert "unavailable" in checks(report)["E.local_tts"]["error"]["message"]


def test_index_missing_runtime_is_reported(tools, index_config, monkeypatch, capsys):
    from quickcut import indextts2

    path, _, _ = index_config
    monkeypatch.setattr(indextts2, "version", Mock(side_effect=indextts2.PackageNotFoundError("indextts")))
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 2
    assert "Python environment" in checks(report)["E.local_tts"]["error"]["message"]


@pytest.mark.parametrize("content", ["{", "[]", '{"engine": "fixture-tone"}'])
def test_invalid_voice_config_never_falls_back_to_system_voice(tools, tmp_path, capsys, content):
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")
    code, report = invoke(capsys, "--voice-config", str(path), "--require-media")
    assert code == 2
    assert checks(report)["E.local_tts"]["status"] == "failed"


def test_default_preflight_does_not_import_torch(tools, monkeypatch, capsys):
    original_import = doctor.importlib.import_module

    def import_module(name, *args, **kwargs):
        assert name != "torch", "system TTS preflight must not import torch"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(doctor.importlib, "import_module", import_module)
    assert invoke(capsys, "--require-media")[0] == 0

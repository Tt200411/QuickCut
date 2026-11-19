"""健康检查只验证可见资源，不加载模型或执行推理。"""
from dataclasses import replace
from pathlib import Path
import json

import pytest

from quickcut import production_pipeline as pipeline


LFS_POINTER = (b"version https://git-lfs.github.com/spec/v1\n"
               b"oid sha256:" + b"a" * 64 + b"\nsize 123456789\n")
EDITOR = Path("/Applications/VideoFusion-macOS.app/Contents/Info.plist")


@pytest.fixture
def runtime_env(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "REPO", tmp_path)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    executable = tmp_path / "python"
    executable.write_bytes(b"test interpreter")
    asr = tmp_path / "asr"
    asr.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (asr / name).write_bytes(b"x" * 2048)
    weights = tmp_path / "weights"
    weights.mkdir()
    for name in ("gpt.pth", "s2mel.pth", "config.yaml"):
        (weights / name).write_bytes(b"x" * 2048)
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF" + b"x" * 2048)
    config = tmp_path / "voice.json"
    config.write_text(json.dumps({"engine": "indextts2", "model_dir": "weights", "reference_audio": "reference.wav"}))
    vision = tmp_path / "vision.bin"
    vision.write_bytes(b"x" * 2048)
    deployment = tmp_path / "outputs/vision-runtime/deployment-manifest.json"
    deployment.parent.mkdir(parents=True)
    deployment.write_text(json.dumps({"status": "ready", "files": [{"path": str(vision), "bytes": vision.stat().st_size}]}))
    monkeypatch.setattr(pipeline, "ollama_json", lambda *args, **kwargs: {"models": [{"name": "qwen2.5vl:3b"}]})
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: "/test/bin/" + name)
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: False if path == EDITOR else original_is_file(path))

    def unexpected(*args, **kwargs):
        pytest.fail("健康检查不应启动进程或执行推理")

    monkeypatch.setattr(pipeline.subprocess, "Popen", unexpected)
    monkeypatch.setattr(pipeline, "worker", unexpected)
    return pipeline.Runtime(python=str(executable), vision_python=str(executable), tts_python=str(executable),
                            voice_config=str(config), asr_model=str(asr))


def check(status, name):
    return next(item for item in status["checks"] if item["name"] == name)


def test_generation_and_editor_readiness_preserve_legacy_ready(runtime_env, monkeypatch):
    status = pipeline.runtime_status(runtime_env)
    assert status["generation_ready"] is True
    assert status["editor_ready"] is False
    assert status["ready"] is False
    assert status["checked_at"]
    assert "不加载模型" in status["scope"]
    for item in status["checks"]:
        if "解释器" in item["name"]:
            assert "仅检查解释器文件存在" in item["detail"]

    original_is_file, original_open = Path.is_file, Path.open
    monkeypatch.setattr(Path, "is_file", lambda path: True if path == EDITOR else original_is_file(path))
    from io import BytesIO
    import plistlib
    monkeypatch.setattr(Path, "open", lambda path, *args, **kwargs:
                        BytesIO(plistlib.dumps({"CFBundleShortVersionString": "6.1.0"})) if path == EDITOR
                        else original_open(path, *args, **kwargs))
    status = pipeline.runtime_status(runtime_env)
    assert status["ready"] is status["generation_ready"] is status["editor_ready"] is True
    status = pipeline.runtime_status(replace(runtime_env, python=runtime_env.python + ".missing"))
    assert status["generation_ready"] is status["ready"] is False
    assert status["editor_ready"] is True
    assert not all(item["ready"] for item in status["checks"] if item["stage"] != "editor")


@pytest.mark.parametrize("resource,name", [
    ("reference.wav", "配音权重与参考音频"),
    ("weights/gpt.pth", "配音权重与参考音频"),
    ("weights/s2mel.pth", "配音权重与参考音频"),
    ("weights/config.yaml", "配音权重与参考音频"),
    ("asr/model.bin", "Whisper 语音识别权重"),
    ("asr/config.json", "Whisper 语音识别权重"),
    ("asr/tokenizer.json", "Whisper 语音识别权重"),
    ("vision.bin", "中文视觉检索权重"),
])
def test_lfs_pointer_is_not_a_ready_resource(runtime_env, tmp_path, resource, name):
    path = tmp_path / resource
    # 补齐长度，确保不能只靠体积或部署记录识别指针。
    path.write_bytes(LFS_POINTER.ljust(2048, b" "))
    status = pipeline.runtime_status(runtime_env)
    item = check(status, name)
    assert not item["ready"]
    assert "Git LFS 指针" in item["detail"]
    assert str(path) in item["detail"]
    assert not status["generation_ready"]


@pytest.mark.parametrize("name", ["model.bin", "config.json", "tokenizer.json"])
def test_local_asr_requires_visible_files(runtime_env, tmp_path, name):
    (tmp_path / "asr" / name).unlink()
    item = check(pipeline.runtime_status(runtime_env), "Whisper 语音识别权重")
    assert not item["ready"]
    assert name in item["detail"]
    assert "文件缺失" in item["detail"]


def test_missing_local_asr_directory_does_not_use_remote_cache(runtime_env, tmp_path):
    model = str(tmp_path / "missing-asr")
    item = check(pipeline.runtime_status(replace(runtime_env, asr_model=model)), "Whisper 语音识别权重")
    assert not item["ready"]
    assert model + "/model.bin" in item["detail"]


def test_remote_model_name_keeps_existing_cache_check(runtime_env, tmp_path):
    cached = tmp_path / "hf/hub/models--Systran--faster-whisper-small/snapshots/revision/model.bin"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x" * 2048)
    runtime = replace(runtime_env, asr_model="small")
    assert check(pipeline.runtime_status(runtime), "Whisper 语音识别权重")["ready"]
    assert runtime.asr_model == "small"
    cached.write_bytes(LFS_POINTER.ljust(2048, b" "))
    item = check(pipeline.runtime_status(runtime), "Whisper 语音识别权重")
    assert not item["ready"]
    assert "Git LFS 指针" in item["detail"]


def test_resource_check_reads_only_a_bounded_header(tmp_path, monkeypatch):
    path = tmp_path / "large.bin"
    with path.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024)
    original_open = Path.open
    reads = []

    class BoundedReader:
        def __enter__(self):
            self.stream = original_open(path, "rb")
            return self

        def read(self, size=-1):
            reads.append(size)
            assert 0 < size <= 512
            return self.stream.read(size)

        def __exit__(self, *args):
            self.stream.close()

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: BoundedReader())
    assert pipeline._resource_issue(path) is None
    assert reads == [512]

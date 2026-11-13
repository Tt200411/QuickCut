"""只读、离线的工程预检；fixture 通过不代表真实模型推理通过。"""
from __future__ import annotations

import argparse
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

# 直接运行脚本时也使用本仓库，并避免导入生成 __pycache__。
if __package__ in (None, ""):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOOL_TIMEOUT = 5
PACKAGES = (
    "quickcut", "imageio-ffmpeg", "pillow", "fastapi", "uvicorn",
    "python-multipart", "gradio", "open_clip_torch", "torch", "torchvision",
    "faster-whisper", "ctranslate2", "av", "tokenizers", "onnxruntime",
    "huggingface-hub", "yt-dlp", "pytest", "httpx",
)


def failure(exc):
    return {"type": type(exc).__name__, "message": str(exc)}


def check_binary(name, resolver):
    result = {"name": name, "status": "failed", "path": None}
    try:
        binary = resolver()
        if not binary:
            raise FileNotFoundError("未找到可执行工具")
        result["path"] = str(binary)
        executable = shutil.which(str(binary))
        if not executable:
            raise FileNotFoundError(f"工具不存在或不可执行: {binary}")
        result["path"] = executable
        command = [executable, "-version"]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TOOL_TIMEOUT, check=False,
            stdin=subprocess.DEVNULL,
        )
        result["returncode"] = completed.returncode
        output = (completed.stdout or completed.stderr or "").strip()
        result["version"] = output.splitlines()[0] if output else None
        if completed.returncode:
            raise RuntimeError(f"-version 退出码 {completed.returncode}: {output[-2000:]}")
        if not output:
            raise RuntimeError("-version 未返回版本信息")
        result["status"] = "ok"
    except Exception as exc:
        result["error"] = failure(exc)
    return result


def a_binary(module, function):
    return getattr(importlib.import_module(module), function)()


def check_tts():
    engines = {name: shutil.which(name) for name in ("say", "espeak-ng", "espeak")}
    selected = next((name for name, path in engines.items() if path), None)
    return {
        "name": "E.local_tts", "status": "ok" if selected else "failed",
        "engines": engines, "selected": selected,
        "scope": "仅检查默认系统 TTS 可执行文件；未合成、未验证中文 voice。IndexTTS2 需单独配置和验证。",
    }


def check_packages():
    checks = []
    for name in PACKAGES:
        entry = {"name": name, "status": "ok", "version": None}
        try:
            entry["version"] = metadata.version(name)
        except metadata.PackageNotFoundError:
            entry["status"] = "missing"
        except Exception as exc:
            entry.update(status="failed", error=failure(exc))
        checks.append(entry)
    return checks


def check_manifest(project_root):
    result = {
        "name": "A.asset_manifest", "status": "skipped",
        "path_safety": "not_checked", "validation": "not_run",
    }
    if project_root is None:
        result["reason"] = "未提供 --project-root"
        return result
    result["status"] = "failed"
    try:
        root = Path(project_root).expanduser().resolve()
        result["project_root"] = str(root)
        media = importlib.import_module("quickcut.media")
        manifest_path = media.resolve_uri(root, "asset-manifest.json")
        result["path"] = str(manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("asset-manifest.json 必须是 JSON object")

        # 先检查全部 URI，再允许 A 读取媒体；包括 A 当前未检查的 transcript URI。
        def check_uris(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "uri" or key.endswith("_uri"):
                        if item is not None:
                            if not isinstance(item, str):
                                raise ValueError(f"{key} 必须是项目相对 URI 字符串")
                            media.resolve_uri(root, item)
                    else:
                        check_uris(item)
            elif isinstance(value, list):
                for item in value:
                    check_uris(item)

        result["path_safety"] = "checking"
        check_uris(manifest)
        result["path_safety"] = "ok"
        result["fixtures_only"] = manifest.get("fixtures_only")
        result["validation"] = "running"
        ingest = importlib.import_module("quickcut.ingest")
        ingest.validate_asset_manifest(manifest, root)
        result["validation"] = "ok"
        result["assets"] = len(manifest["assets"])
        if not manifest["assets"]:
            raise ValueError("清单无媒体资产，不能认定真实媒体预检通过")
        result["status"] = "ok"
        result["scope"] = "A 真实清单校验：引用、文件哈希、区间和 PNG；未运行媒体解码或模型推理。"
    except Exception as exc:
        if result["path_safety"] in ("not_checked", "checking"):
            result["path_safety"] = "failed"
        if result["validation"] == "running":
            result["validation"] = "failed"
        result["error"] = failure(exc)
    return result


def build_report(*, project_root=None, require_media=False):
    required = [
        {"name": "python", "status": "ok" if sys.version_info >= (3, 10) else "failed",
         "version": platform.python_version(), "minimum": "3.10"},
        check_binary("A.core.ffmpeg", lambda: a_binary("quickcut.core", "_ffmpeg_binary")),
        check_binary("A.media.ffprobe", lambda: a_binary("quickcut.media", "ffprobe")),
        check_binary("E.ffmpeg_path", lambda: shutil.which("ffmpeg")),
        check_tts(),
    ]
    manifest = check_manifest(project_root)
    if project_root is not None:
        required.append(manifest)
    packages = check_packages()
    optional = [
        {"name": "packages", "status": "ok" if all(p["status"] == "ok" for p in packages) else "incomplete",
         "checks": packages, "scope": "仅读取安装元数据；extras 按实际引擎需要启用，未导入模型包。"},
        {"name": "ollama", "status": "not_checked",
         "configuration": {
             "QUICKCUT_VLM_MODEL": os.getenv("QUICKCUT_VLM_MODEL", "qwen2.5vl:3b"),
             "QUICKCUT_OLLAMA_URL": os.getenv("QUICKCUT_OLLAMA_URL", "http://127.0.0.1:11434"),
             "QUICKCUT_VLM_TIMEOUT": os.getenv("QUICKCUT_VLM_TIMEOUT", "120"),
         },
         "scope": "仅说明配置；未访问端点、未运行 Ollama、未下载模型、未验证真实推理。"},
    ]
    if project_root is None:
        optional.append(manifest)
    required_ok = all(c["status"] == "ok" for c in required)
    exit_code = 2 if require_media and not required_ok else 0
    return {
        "schema_version": "1", "status": "ok" if required_ok else "degraded",
        "require_media": require_media, "exit_code": exit_code,
        "environment": {"python": platform.python_version(), "executable": sys.executable,
                        "platform": "-".join((platform.system(), platform.release(), platform.machine())),
                        "system": platform.system(),
                        "machine": platform.machine()},
        "required": {"status": "ok" if required_ok else "failed", "enforced": require_media, "checks": required},
        "optional": {"status": "ok" if all(c["status"] == "ok" for c in optional) else "incomplete", "checks": optional},
        "policy": {"read_only": True, "network_access": False, "install": False, "download": False},
        "evidence": {
            "fixtures": "rule/fixed、fixture-tone 与 mock 仅验证协议或工程行为，不代表真实视觉/语音推理。",
            "real_inference": "not_run",
            "limits": "-version 和 PATH 不证明编码器/滤镜、中文 voice、模型权重、成片或编辑器验收通过。",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", help="可选项目根；只读校验 asset-manifest.json 和媒体引用")
    parser.add_argument("--require-media", action="store_true", help="必需媒体预检失败时退出 2；默认仅报告并退出 0")
    args = parser.parse_args(argv)
    report = build_report(project_root=args.project_root, require_media=args.require_media)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())

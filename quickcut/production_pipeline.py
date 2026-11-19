"""Real A–F workflow used by both the HTML workbench and acceptance CLI.

Models run sequentially in separate processes. Draft approval is an explicit
input; a ready file is never manufactured from a fixture or a failed stage.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.parse

from .media import write_json, resolve_uri
from .planning import build_outline, build_retrieval_request
from .planning.contracts import envelope, ref

REPO = Path(__file__).resolve().parents[1]
OUTPUT = {"width": 1280, "height": 720, "fps": {"numerator": 30, "denominator": 1}, "sample_rate": 48000}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def now():
    return datetime.now(timezone.utc).isoformat()


class PipelineError(RuntimeError):
    def __init__(self, code, message, *, stage="pipeline", detail=None):
        super().__init__(message)
        self.code, self.stage, self.detail = code, stage, detail


@dataclass(frozen=True)
class Runtime:
    python: str = str(REPO / ".venv/bin/python")
    vision_python: str = str(REPO / ".venv-vision/bin/python")
    tts_python: str = str(REPO / "outputs/indextts2-runtime/upstream/.venv/bin/python")
    voice_config: str = str(REPO / "examples/voice_package/indextts2.config.json")
    model: str = "qwen2.5vl:3b"
    ollama_url: str = "http://127.0.0.1:11434"
    asr_model: str = "small"

    @classmethod
    def configured(cls):
        names = {"python": "QUICKCUT_PIPELINE_PYTHON", "vision_python": "QUICKCUT_VISION_PYTHON",
                 "tts_python": "QUICKCUT_TTS_PYTHON", "voice_config": "QUICKCUT_VOICE_CONFIG",
                 "model": "QUICKCUT_VLM_MODEL", "ollama_url": "QUICKCUT_OLLAMA_URL",
                 "asr_model": "QUICKCUT_ASR_MODEL"}
        return cls(**{key: os.environ[value] for key, value in names.items() if os.environ.get(value)})


def ollama_json(runtime, endpoint, payload=None, timeout=5):
    location = urllib.parse.urlsplit(runtime.ollama_url)
    if location.scheme not in {"http", "https"} or location.hostname not in {"127.0.0.1", "localhost", "::1"} or location.username or location.password:
        raise PipelineError("LOCAL_MODEL_REQUIRED", "本地工作台只允许连接本机的模型服务", stage="C")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(runtime.ollama_url.rstrip("/") + endpoint, data=data,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.load(response)


def start_ollama(runtime):
    try:
        ollama_json(runtime, "/api/tags")
        return
    except (OSError, ValueError):
        pass
    if runtime.ollama_url.rstrip("/") != "http://127.0.0.1:11434":
        raise PipelineError("OLLAMA_UNAVAILABLE", "配置的 Qwen 服务尚未启动", stage="C")
    binary = shutil.which("ollama")
    if not binary:
        raise PipelineError("OLLAMA_MISSING", "未安装 Ollama，无法运行真实选镜", stage="C")
    logs = REPO / "outputs/workbench-runtime"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "ollama.log").open("ab") as log:
        process = subprocess.Popen([binary, "serve"], stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    for _ in range(40):
        try:
            ollama_json(runtime, "/api/tags", timeout=1)
            return
        except (OSError, ValueError):
            if process.poll() is not None:
                break
            time.sleep(0.25)
    raise PipelineError("OLLAMA_UNAVAILABLE", "Qwen 服务未能启动，请查看运行记录", stage="C")


def unload_qwen(runtime):
    # Release the vision model before loading IndexTTS on a 16 GiB Mac.
    ollama_json(runtime, "/api/generate", {"model": runtime.model, "keep_alive": 0}, timeout=30)


def _resource_issue(path, *, minimum_bytes=1, expected_bytes=None):
    try:
        if not path.is_file():
            return f"文件缺失：{path}"
        size = path.stat().st_size
        # 只读固定长度的文件头，不扫描权重或解码媒体。
        with path.open("rb") as stream:
            header = stream.read(512)
        if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
            return f"Git LFS 指针，尚未下载实际文件：{path}"
        if size < minimum_bytes:
            return f"文件为空或过小：{path}"
        if expected_bytes is not None and size != expected_bytes:
            return f"文件长度与部署记录不符：{path}"
    except OSError as exc:
        return f"文件不可读：{path}（{exc}）"
    return None


def runtime_status(runtime):
    checks = []
    for stage, executable in (("A", runtime.python), ("B", runtime.vision_python), ("E", runtime.tts_python)):
        checks.append({"stage": stage, "name": {"A": "视频接入与语音识别", "B": "中文视觉检索", "E": "IndexTTS2 配音"}[stage] + " Python 解释器",
                       "ready": Path(executable).is_file(),
                       "detail": executable + "\n仅检查解释器文件存在；未验证依赖包或模型推理。"})
    asr_path = Path(runtime.asr_model).expanduser()
    local_asr = asr_path.is_dir() or asr_path.is_absolute() or runtime.asr_model.startswith((".", "~"))
    if local_asr:
        asr_issues = [issue for name in ("model.bin", "config.json", "tokenizer.json")
                      if (issue := _resource_issue(asr_path / name, minimum_bytes=1001 if name == "model.bin" else 1))]
        asr_ready = not asr_issues
        asr_detail = "\n".join(asr_issues) if asr_issues else str(asr_path) + "\n本地 ASR 必要文件可见；未加载模型。"
    else:
        cached_asr = list((Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface"))) / "hub" /
                          ("models--Systran--faster-whisper-" + runtime.asr_model) / "snapshots").glob("*/model.bin"))
        asr_issues = [_resource_issue(path, minimum_bytes=1001) for path in cached_asr]
        asr_ready = any(issue is None for issue in asr_issues)
        asr_detail = runtime.asr_model + "\n" + ("缓存权重可见；未加载模型。" if asr_ready else
                     "\n".join(asr_issues) or "未找到本地缓存权重；本检查不下载模型。")
    checks.append({"stage": "A", "name": "Whisper 语音识别权重", "ready": asr_ready,
                   "detail": asr_detail})
    deployment_path = REPO / "outputs/vision-runtime/deployment-manifest.json"
    vision_ready, vision_detail = False, "检索权重尚未完成部署"
    if deployment_path.is_file():
        try:
            deployment = read(deployment_path)
            files = deployment.get("files", [])
            issues = [issue for item in files
                      if (issue := _resource_issue(Path(item["path"]), expected_bytes=item["bytes"]))]
            vision_ready = deployment.get("status") == "ready" and bool(files) and not issues
            vision_detail = ("nllb-clip-base · 部署文件可见；未加载模型。" if vision_ready else
                             "\n".join(issues) or "检索权重尚未完成部署")
        except (OSError, ValueError, KeyError, TypeError):
            vision_detail = "检索模型部署记录无效"
    checks.append({"stage": "B", "name": "中文视觉检索权重", "ready": vision_ready, "detail": vision_detail})
    try:
        models = ollama_json(runtime, "/api/tags").get("models", [])
        ready = any(item.get("name") == runtime.model or item.get("model") == runtime.model for item in models)
        checks.append({"stage": "C", "name": "Qwen 看图与规划", "ready": ready, "detail": runtime.model})
    except Exception as exc:
        checks.append({"stage": "C", "name": "Qwen 看图与规划", "ready": False, "detail": str(exc)})
    for binary in ("ffmpeg", "ffprobe"):
        checks.append({"stage": "F", "name": binary, "ready": bool(shutil.which(binary)), "detail": shutil.which(binary)})
    voice_ready = False
    voice_detail = runtime.voice_config
    try:
        config_issue = _resource_issue(Path(runtime.voice_config))
        if config_issue:
            raise ValueError(config_issue)
        config = read(runtime.voice_config)
        def local_path(value):
            path = Path(value).expanduser()
            return path if path.is_absolute() else REPO / path
        weights = local_path(config["model_dir"])
        resources = [weights / name for name in ("gpt.pth", "s2mel.pth", "config.yaml")]
        resources.append(local_path(config["reference_audio"]))
        issues = [issue for path in resources if (issue := _resource_issue(path))]
        if config.get("engine") != "indextts2":
            issues.insert(0, "声音配置的 engine 必须为 indextts2")
        voice_ready = not issues
        voice_detail = "\n".join(issues) if issues else runtime.voice_config + "\n必要权重和参考音频可见；未加载模型或解码音频。"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        voice_detail = f"声音配置、模型或参考音频不可用（配置：{runtime.voice_config}）：{exc}"
    checks.append({"stage": "E", "name": "配音权重与参考音频", "ready": voice_ready, "detail": voice_detail})
    app = Path("/Applications/VideoFusion-macOS.app/Contents/Info.plist")
    version = None
    if app.is_file():
        with app.open("rb") as stream:
            version = plistlib.load(stream).get("CFBundleShortVersionString")
    checks.append({"stage": "editor", "name": "剪映专业版", "ready": version == "6.1.0", "detail": version or "未找到"})
    return {"ready": all(item["ready"] for item in checks),
            "generation_ready": all(item["ready"] for item in checks if item["stage"] != "editor"),
            "editor_ready": all(item["ready"] for item in checks if item["stage"] == "editor"), "checks": checks,
            "scope": "轻量安装、资源可见性与服务检查，参考音频取当前默认声音配置；不加载模型、不执行推理、不验证媒体内容。解释器存在不代表依赖包完整。每次生成仍会验证任务资源并实际推理；剪映仅影响打开工程。",
            "checked_at": now()}


def worker(runtime, action, payload, root, result_name, *, progress=None, timeout=2400, cancel_event=None):
    root = Path(root)
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    request_path = log_dir / (result_name + ".request.json")
    output_path = root / (result_name + ".json")
    write_json(request_path, payload)
    executable = runtime.vision_python if action == "retrieve" else runtime.tts_python if action in {"voice", "probe_tts"} else runtime.python
    if not Path(executable).is_file():
        raise PipelineError("RUNTIME_MISSING", f"缺少 {action} 独立运行环境：{executable}", stage=action)
    env = dict(os.environ, PYTHONPATH=str(REPO), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               PYTORCH_ENABLE_MPS_FALLBACK="1", QUICKCUT_CLIP_MODEL="nllb-clip-base", QUICKCUT_CLIP_PRETRAINED="v1")
    deployment_path = REPO / "outputs/vision-runtime/deployment-manifest.json"
    if action == "retrieve" and deployment_path.is_file():
        deployment = read(deployment_path)
        env.update({key: str(value) for key, value in deployment.get("environment", {}).items()
                    if key.startswith("QUICKCUT_CLIP_")})
    started = time.monotonic()
    source_names = {"ingest": ["ingest.py", "asr.py", "media.py"], "retrieve": ["retrieval.py", "encoder.py"],
                    "draft": ["planning/production.py", "planning/selection.py", "planning/planning.py", "planning/validator.py"],
                    "voice": ["voice_package.py", "indextts2.py"]}.get(action, ["indextts2.py"])
    execution = {"started_at": now(), "action": action, "python": executable,
                 "code_sha256": {name: hashlib.sha256((REPO / "quickcut" / name).read_bytes()).hexdigest() for name in source_names},
                 "local_models_only": True}
    write_json(log_dir / (result_name + ".execution.json"), execution)
    with (log_dir / (result_name + ".log")).open("w", encoding="utf-8") as log:
        process = subprocess.Popen([executable, "-m", "quickcut.pipeline_worker", action, str(request_path), str(output_path)],
                                   cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise PipelineError("CANCELLED", "服务正在关闭，已停止本次模型任务", stage=action)
                if time.monotonic() - started > timeout:
                    raise PipelineError("STAGE_TIMEOUT", f"{action} 超过运行时限，详细记录已保存", stage=action)
                try:
                    process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    pass
            returncode = process.returncode
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    result = read(output_path) if output_path.is_file() else {}
    execution.update(exit_code=returncode, elapsed_seconds=time.monotonic() - started, finished_at=now())
    write_json(log_dir / (result_name + ".execution.json"), execution)
    if returncode:
        raise PipelineError(result.get("code", "STAGE_FAILED"), result.get("message", f"{action} 执行失败，请查看阶段日志"),
                            stage=action, detail={"log": str(log_dir / (result_name + ".log"))})
    if progress:
        progress(action, f"完成，用时 {time.monotonic() - started:.1f} 秒")
    return result


def make_brief(project_id, prompt, duration):
    brief = envelope("Brief", "brief-" + project_id, project_id, 1, fixtures_only=False, owner="C", inputs=[])
    brief.update(requirements={"story": prompt, "format": "thematic_montage", "language": "zh-CN",
                              "target_duration_us": round(duration * 1_000_000), "aspect_ratio": {"width": 16, "height": 9},
                              "tone": "自然、简洁", "narration": {"enabled": True, "style": "中文旁白"},
                              "picture_policy": "thematic_montage", "broll_policy": {"enabled": True,
                              "roles": ["establishing", "atmosphere", "detail"], "must_not_assert_user_events": True},
                              "target_editor": "Jianying 6.1.0"},
                 constraints={"local_inference_only": True, "preserve_source_audio": True, "editable_tracks_required": True})
    return brief


def job_runtime(root, settings, runtime):
    """New jobs consume their captured voice configuration, including preflight."""
    if settings.get("voice_config"):
        path = resolve_uri(Path(root), settings["voice_config"])
        if not path.is_file():
            raise PipelineError("VOICE_CONFIG_MISSING", "任务保存的声音配置缺失，请重新导入参考音频", stage="E")
        return replace(runtime, voice_config=str(path))
    return runtime


def analyze(root, settings, runtime, progress, *, cancel_event=None):
    root = Path(root)
    runtime = job_runtime(root, settings, runtime)
    progress("preflight", "检查真实模型与运行环境")
    start_ollama(runtime)
    checks = runtime_status(runtime)
    write_json(root / "runtime-check.json", checks)
    missing = [item for item in checks["checks"] if not item["ready"] and item["stage"] != "editor"]
    if missing:
        raise PipelineError("MODELS_NOT_READY", "缺少运行环境：" + "、".join(item["name"] for item in missing), detail=missing)
    tts = worker(runtime, "probe_tts", {"config": runtime.voice_config}, root, "tts-preflight", timeout=90, cancel_event=cancel_event)
    if tts.get("status") != "ok":
        raise PipelineError("TTS_NOT_READY", str(tts.get("error", "IndexTTS2 检查失败")), stage="E", detail=tts)
    brief = make_brief(settings["project_id"], settings["prompt"], settings["duration"])
    write_json(root / "brief.json", brief)
    outline = build_outline(brief)
    window = max(beat["duration_budget_us"] for beat in outline)
    progress("A", "分析真实视频、抽帧并识别原声")
    manifest = worker(runtime, "ingest", {"root": str(root), "sources": settings["sources"],
                      "project_id": settings["project_id"], "config": {"window_us": window,
                      "coarse_window_us": max(30_000_000, window), "proxy_width": 640,
                      "asr": "faster-whisper", "asr_model": runtime.asr_model, "asr_language": "zh"}},
                      root, "asset-manifest", progress=progress, cancel_event=cancel_event)
    if manifest.get("processing", {}).get("status") != "success":
        raise PipelineError("INGEST_INCOMPLETE", "部分素材或语音识别失败，请查看接入报告", stage="A", detail=manifest.get("processing"))
    request = build_retrieval_request(brief, manifest, outline, broll_queries=True,
                                     document_id="request-" + settings["project_id"])
    origins = sorted({asset["origin"] for asset in manifest["assets"]})
    for query in request["queries"]:
        query["allowed_origins"] = origins
        query["top_k"] = 6
    write_json(root / "retrieval-request.json", request)
    progress("B", "用真实中文视觉模型检索镜头")
    from .retrieval import _fingerprint, _implementation_fingerprints
    candidates = read(root / "candidate-set.json") if (root / "candidate-set.json").is_file() else {}
    profile = candidates.get("retrieval_profile", {})
    reuse = (candidates.get("status") == "ok" and profile.get("input_fingerprints") == {
                "AssetManifest": _fingerprint(manifest), "RetrievalRequest": _fingerprint(request)}
             and profile.get("implementation_fingerprints") == _implementation_fingerprints())
    if reuse:
        progress("B", "素材和实现指纹一致，复用此前真实模型检索结果")
    else:
        candidates = worker(runtime, "retrieve", {"root": str(root), "manifest": str(root / "asset-manifest.json"),
                            "request": str(root / "retrieval-request.json"), "project_id": settings["project_id"]},
                            root, "candidate-set", progress=progress, cancel_event=cancel_event)
    write_json(root / "retrieval-cache-check.json", {"cache_hit": reuse, "checked_at": now(), "implementation": _implementation_fingerprints()})
    errors = [item for item in candidates.get("results", []) if item.get("status") == "error" or item.get("errors")]
    if candidates.get("status") == "error" or errors:
        raise PipelineError("RETRIEVAL_FAILED", "检索遇到素材或推理错误，未将它当成无匹配", stage="B", detail=errors)
    progress("C", "Qwen 正在查看候选画面、选择镜头并起草旁白")
    try:
        draft = worker(runtime, "draft", {"root": str(root), "model": runtime.model, "ollama_url": runtime.ollama_url},
                       root, "draft", cancel_event=cancel_event)
    finally:
        try:
            unload_qwen(runtime)
        except Exception as exc:
            # Keep the original inference result/error; E checks release again.
            write_json(root / "qwen-unload-warning.json", {"at": now(), "message": str(exc)})
    return draft


def render(root, confirmation, runtime, progress, *, cancel_event=None):
    from copy import deepcopy
    from .planning.production import confirm_draft
    from .integration import complete_voice_timeline
    from .retiming import content_hash
    from .export import export_project
    root = Path(root)
    attempt = confirmation.get("revision", 1)
    if type(attempt) is not int or attempt < 1:
        raise PipelineError("INVALID_REVISION", "生成修订必须是正整数", stage="D")
    attempt_root = root / "revisions" / f"r{attempt}"
    try:
        attempt_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise PipelineError("REVISION_EXISTS", "这一修订已有记录；请创建新的生成修订，不能覆盖历史", stage="D") from exc
    started = now()
    rounds = {}

    def trace(name, value, *, current=True):
        write_json(attempt_root / name, value)
        if current:
            write_json(root / name, value)

    def observe_fit(record):
        iteration = record["iteration"]
        rounds[iteration] = record
        trace(f"fit-round-{iteration}.json", record, current=False)
        trace(f"pending-fit-{iteration}.json", record["pending_timeline"], current=False)
        trace(f"fit-request-{iteration}.json", record["request"])
        if record.get("reply") is not None:
            trace(f"fit-reply-{iteration}.json", record["reply"], current=False)
        trace("fit-rounds.json", [rounds[key] for key in sorted(rounds)])

    try:
        trace("attempt.json", {"revision": attempt, "status": "running", "started_at": started}, current=False)
        trace("approval.json", confirmation)
        settings = read(root / "settings.json")
        runtime = job_runtime(root, settings, runtime)
        trace("settings.json", settings, current=False)
        # Freeze the exact source documents for this attempt, including failures
        # before a final export exists. Top-level names remain current aliases.
        draft, brief, manifest, candidates = [read(root / name) for name in
            ("draft.json", "brief.json", "asset-manifest.json", "candidate-set.json")]
        for name, doc in (("draft.json", draft), ("brief.json", brief), ("asset-manifest.json", manifest),
                          ("candidate-set.json", candidates), ("retrieval-request.json", read(root / "retrieval-request.json"))):
            trace(name, doc, current=False)
        trace("fit-rounds.json", [])
        result = confirm_draft(draft, approved_text_by_beat=confirmation["approved_text_by_beat"],
                               reviewer=confirmation["reviewer"], reviewed_at=confirmation["reviewed_at"],
                               confirmation_kind=confirmation.get("confirmation_kind", "human"), plan_revision=attempt,
                               selection_overrides=confirmation.get("selection_overrides"))
        trace("story-plan.json", result.story_plan)
        trace("planning-run.json", result.run)
        trace("alignment.json", result.alignment)
        boundaries = {"story_plan": ref(result.story_plan), "asset_manifest": ref(manifest), "edits": confirmation.get("boundaries", [])}
        trace("boundaries.json", boundaries)
        progress("E", "IndexTTS2 正在用参考音频生成真实旁白")
        unload_qwen(runtime)
        # Workers read immutable inputs for this revision, never a mutable alias.
        trace("voice-config.json", read(runtime.voice_config), current=False)
        base = {"root": str(root), "story": str(attempt_root / "story-plan.json"),
                "config": str(attempt_root / "voice-config.json")}
        revision = (attempt - 1) * 10 + 1
        measure_name = f"voice-measured-r{revision}"
        previous_voice = root / "voice-package.json"
        previous_path = None
        if previous_voice.exists():
            trace("previous-voice.json", read(previous_voice), current=False)
            previous_path = str(attempt_root / "previous-voice.json")
        measured = worker(runtime, "voice", {**base, "revision": revision, "previous": previous_path},
                          root, measure_name, progress=progress, cancel_event=cancel_event)
        trace(measure_name + ".json", measured, current=False)
        current_voice = attempt_root / (measure_name + ".json")

        def fit_provider(request):
            nonlocal current_voice
            iteration = request["iteration"]
            # observe_fit saved the complete pending/request before this dispatch.
            path = attempt_root / f"fit-request-{iteration}.json"
            progress("D/E", f"按真实配音时长适配，第 {iteration} 轮")
            name = f"voice-fit-r{request['voice_revision']}"
            voice = worker(runtime, "voice", {**base, "revision": request["voice_revision"], "mode": "fit",
                           "request": str(path), "previous": str(current_voice)}, root, name, progress=progress, cancel_event=cancel_event)
            trace(name + ".json", voice, current=False)
            current_voice = attempt_root / (name + ".json")
            return voice

        progress("D", "对齐画面、原声、旁白和字幕")
        built = complete_voice_timeline(result, measured, brief=brief, manifest=manifest, candidate_set=candidates,
                                       output=OUTPUT, expected_story_revision=result.story_plan["revision"],
                                       timeline_document_id="timeline-" + brief["project_id"], timeline_revision=revision,
                                       fit_provider=fit_provider, max_iterations=2, project_root=root,
                                       boundaries=boundaries if boundaries["edits"] else None, fit_observer=observe_fit)
        trace("voice-package.json", built.voice_package)
        # Apply presentation settings before final freezing. Keep the original D
        # output and never rewrite its fit request, pending hash or E reply.
        trace("timeline-d.json", built.timeline, current=False)
        subtitle_position = settings.get("subtitle_position", "bottom")
        if subtitle_position not in {"top", "bottom"}:
            raise PipelineError("INVALID_SUBTITLE_POSITION", "字幕位置必须是顶部或底部", stage="F")
        before_presentation = content_hash(built.timeline)
        built.timeline = deepcopy(built.timeline)
        built.timeline["render_policy"]["subtitle_position"] = subtitle_position
        trace("presentation-policy.json", {"subtitle_position": subtitle_position,
            "source": "settings.json", "applied_before_final_freeze": True,
            "d_timeline_content_hash": before_presentation,
            "final_timeline_content_hash": content_hash(built.timeline),
            "timing_changed": False, "fit_receipt_changed": False}, current=False)
        trace("timeline.json", built.timeline)
        trace("fit-rounds.json", built.fit_rounds)
        progress("F", "渲染剪辑视频并打包可编辑剪映工程")
        package = root / f"export-r{attempt}"
        exported = export_project(built.timeline, manifest, built.voice_package, root, package,
                                  preview=True, name="QuickCut-" + root.name)
        trace("export-manifest.json", exported, current=False)
        archive = shutil.make_archive(str(root / f"QuickCut-project-r{attempt}"), "zip", package)
        report = {"status": "completed", "completed_at": now(), "fixtures_only": False,
                  "models": {"ASR": "faster-whisper/" + runtime.asr_model,
                             "B": candidates.get("retrieval_profile", {}), "C": runtime.model,
                             "E": built.voice_package.get("synthesis_profile", {})},
                  "timeline": ref(built.timeline), "duration_us": built.timeline["duration_us"],
                  "export": ref(exported), "files": {"video": f"{package.name}/preview.mp4", "package": Path(archive).name},
                  "package_directory": package.name, "revision": attempt,
                  "editor": {"status": "not_opened"}, "approval": str(attempt_root / "approval.json"),
                  "confirmation_kind": confirmation.get("confirmation_kind", "human"),
                  "revision_archive": str(attempt_root.relative_to(root))}
        trace("run-report.json", report)
        trace("attempt.json", {"revision": attempt, "status": "completed", "started_at": started,
                               "finished_at": now(), "report": "run-report.json"}, current=False)
        return report
    except Exception as exc:
        # Keep the original exception semantics for Jobs.fail, including CANCELLED.
        for record in getattr(exc, "fit_rounds", []) or []:
            observe_fit(record)
        voice = getattr(exc, "voice_package", None)
        if voice is not None:
            trace("voice-failed.json", voice, current=False)
        error = {"code": getattr(exc, "code", "PIPELINE_FAILED"), "message": str(exc),
                 "stage": getattr(exc, "stage", "D/E/F"), "detail": getattr(exc, "detail", None),
                 "action_owner": getattr(exc, "action_owner", None),
                 "affected_ids": list(getattr(exc, "affected_ids", []))}
        trace("error.json", error)
        trace("attempt.json", {"revision": attempt, "status": "interrupted" if error["code"] == "CANCELLED" else "failed",
                               "started_at": started, "finished_at": now(), "error": error}, current=False)
        raise


def open_editor(root):
    from .export import install_jianying_draft, ExportError
    root = Path(root)
    report = read(root / "run-report.json")
    binding = {key: report[key] for key in ("revision", "package_directory", "export")}
    existing = root / "editor-install.json"
    try:
        installed = read(existing) if existing.is_file() else {}
        if installed.get("export_binding") == binding:
            if not Path(installed["draft_directory"]).is_dir():
                raise PipelineError("DRAFT_MISSING", "已安装草稿被移走，请重新生成工程")
        else:
            installed = install_jianying_draft(root / report["package_directory"],
                name=f"QuickCut-{root.name}-r{report['revision']}")
            installed["export_binding"] = binding
            write_json(root / f"editor-install-r{report['revision']}.json", installed)
            write_json(existing, installed)
    except ExportError as exc:
        if exc.code == "EDITOR_RUNNING":
            return {"status": "waiting_for_editor_close", "message": "视频和工程已生成。请保存并完全退出剪映，再点击“打开剪映”。"}
        raise
    subprocess.run(["open", "-b", "com.lemon.lvpro"], check=True, capture_output=True, timeout=20)
    return {"status": "opened", "draft_name": installed["draft_name"], "draft_directory": installed["draft_directory"],
            "message": "剪映已启动，请在草稿列表打开 " + installed["draft_name"], "editor_import_verified": False}

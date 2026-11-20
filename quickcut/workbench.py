"""Local HTML workbench: upload → actual models → review → video + Jianying."""
from __future__ import annotations

import argparse
from array import array
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import uuid
import wave

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .media import resolve_uri, write_json
from . import production_pipeline as pipeline
from .subtitle_style import STYLE_CHOICES

MAX_FILE_BYTES = 4 * 1024**3
MAX_REFERENCE_BYTES = 20 * 1024**2
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
TERMINAL = {"needs_review", "completed", "failed", "interrupted"}
VOICE_PRESETS = {
    "male": {"label": "男声", "path": "materials/reference-audio/20261117/男声.wav"},
    "female": {"label": "女声", "path": "materials/reference-audio/20261117/魅力游移型女1.wav"},
}
SUBTITLE_CHOICES = STYLE_CHOICES


def _repo_path(value):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else pipeline.REPO / path).resolve()


def _voice_source(runtime):
    try:
        path = _repo_path(runtime.voice_config)
        raw = path.read_bytes()
        config = json.loads(raw)
        if not isinstance(config, dict) or config.get("engine") != "indextts2":
            raise ValueError("需要 IndexTTS2 配置")
        for key in ("model_dir", "reference_audio"):
            if not isinstance(config.get(key), str) or not config[key].strip():
                raise ValueError(f"缺少 {key}")
        overrides = config.get("cue_overrides", {})
        if (not isinstance(overrides, dict) or any(not isinstance(value, dict)
                or set(value) - {"reference_audio", "seed"} for value in overrides.values())):
            raise ValueError("逐句配置仅支持 reference_audio 和 seed")
        return path, raw, config
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(422, f"无法读取默认音色配置：{exc}") from exc


def _reference_info(path):
    """Validate the actual PCM payload, including truncated and silent uploads."""
    try:
        if path.stat().st_size > MAX_REFERENCE_BYTES:
            raise HTTPException(413, "参考音频不能超过 20 MB")
        with wave.open(str(path), "rb") as wav:
            channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
            if wav.getcomptype() != "NONE" or width != 2 or channels < 1 or rate < 1:
                raise ValueError("请使用 PCM16 WAV 音频")
            if not rate <= frames <= rate * 30:
                raise ValueError("参考音频时长应为 1–30 秒")
            if frames * channels * width > path.stat().st_size:
                raise ValueError("参考音频数据不完整")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * channels * width:
                raise ValueError("参考音频数据不完整")
        samples = array("h", pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        peak = max(abs(sample) for sample in samples)
        if peak < 32 or rms < 8:
            raise ValueError("参考音频没有有效声音，请提供清晰的人声")
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "format": "PCM16",
                "sample_rate": rate, "channels": channels, "duration_us": round(frames * 1_000_000 / rate),
                "size_bytes": path.stat().st_size, "rms": round(rms, 3)}
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        raise HTTPException(422, f"参考 WAV 无效：{exc}") from exc


def _voice_presets_status():
    result = {}
    for preset, spec in VOICE_PRESETS.items():
        try:
            _reference_info(_repo_path(spec["path"]))
            available = True
        except HTTPException:
            available = False
        result[preset] = {"label": spec["label"], "available": available}
    return result


def _reference_voice_label(root, settings):
    original = root / "voice-reference-config.json"
    if original.is_file():
        config = pipeline.read(original)
        if config.get("voice_label"):
            return config["voice_label"]
    reference = settings.get("voice_config_source", {}).get("reference", settings.get("reference_audio", {}))
    return Path(reference.get("filename") or "任务参考音频").stem


def _apply_voice_preset(root, preset, settings):
    """Select a reference without rewriting the audio used by past revisions."""
    if not isinstance(preset, str) or preset not in {"reference", *VOICE_PRESETS}:
        raise HTTPException(422, "音色应为任务参考音色、男声或女声")
    config_path = root / "voice-config.json"
    current = pipeline.read(config_path)
    original_path = root / "voice-reference-config.json"
    original = pipeline.read(original_path) if original_path.is_file() else copy.deepcopy(current)
    if preset == "reference":
        config = copy.deepcopy(original)
        reference = Path(config["reference_audio"])
        info = _reference_info(reference)
        metadata = settings.get("voice_config_source", {}).get("reference", settings.get("reference_audio", {}))
        metadata = {**metadata, **info, "path": str(reference.relative_to(root))}
    else:
        spec = VOICE_PRESETS[preset]
        source = _repo_path(spec["path"])
        info = _reference_info(source)
        audio = source.read_bytes()
        if hashlib.sha256(audio).hexdigest() != info["sha256"]:
            raise HTTPException(409, "参考音频已变化，请重试")
        reference = root / "reference" / (info["sha256"] + ".wav")
        if reference.exists() and reference.read_bytes() != audio:
            raise HTTPException(409, "已保存的参考音频发生变化，请重新导入素材")
        config = copy.deepcopy(current)
        config["reference_audio"] = str(reference.resolve())
        config["voice_label"] = spec["label"]
        for item in config.get("cue_overrides", {}).values():
            if "reference_audio" in item:
                item["reference_audio"] = config["reference_audio"]
        metadata = {**info, "kind": "preset", "preset": preset,
                    "filename": source.name, "uri": str(source),
                    "path": str(reference.relative_to(root))}
    # All requested values and audio are validated before any task file changes.
    if not original_path.exists():
        write_json(original_path, original)
    if preset != "reference" and not reference.exists():
        reference.parent.mkdir(parents=True, exist_ok=True)
        with reference.open("xb") as stream:
            stream.write(audio)
    write_json(config_path, config)
    settings["voice"] = preset
    settings["reference_audio"] = metadata


def _subtitle_fields(payload):
    chosen = {}
    for key, allowed in SUBTITLE_CHOICES.items():
        if key not in payload or payload[key] in (None, ""):
            continue
        value = payload[key]
        if not isinstance(value, str) or value not in allowed:
            raise HTTPException(422, "字幕字号、字重或颜色无效")
        chosen[key] = value
    return chosen


class Jobs:
    def __init__(self, root, runtime):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.runtime = runtime
        self.lock = threading.RLock()
        self.editor_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quickcut-models")
        self.active = set()
        for state in self.root.glob("*/job.json"):
            data = pipeline.read(state)
            if data.get("status") not in TERMINAL:
                draft_path = state.parent / "draft.json"
                can_review = (data.get("stage") in {"E", "D", "D/E", "F", "review"}
                              and draft_path.is_file()
                              and pipeline.read(draft_path).get("status") == "needs_review")
                data.update(status="interrupted", can_review=can_review,
                            message="服务已重启。素材与草案已保留，可返回方案继续生成。" if can_review else
                                    "服务已重启。原始素材与阶段记录保留，请重新分析。")
                write_json(state, data)

    def directory(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise HTTPException(404, "找不到此任务")
        path = self.root / job_id
        if not (path / "job.json").is_file():
            raise HTTPException(404, "找不到此任务")
        return path

    def get(self, job_id):
        with self.lock:
            return pipeline.read(self.directory(job_id) / "job.json")

    def update(self, job_id, **values):
        with self.lock:
            data = self.get(job_id)
            data.update(values, updated_at=pipeline.now())
            write_json(self.directory(job_id) / "job.json", data)
            return data

    def event(self, job_id, stage, message):
        if self.stop_event.is_set():
            raise pipeline.PipelineError("CANCELLED", "服务正在关闭，已停止本次任务")
        with self.lock:
            data = self.get(job_id)
            data.update(stage=stage, message=message, updated_at=pipeline.now())
            data.setdefault("events", []).append({"at": pipeline.now(), "stage": stage, "message": message})
            write_json(self.directory(job_id) / "job.json", data)

    def fail(self, job_id, exc):
        data = self.get(job_id)
        error = {"code": getattr(exc, "code", "PIPELINE_FAILED"), "message": str(exc),
                 "stage": getattr(exc, "stage", data.get("stage")),
                 "detail": getattr(exc, "detail", None),
                 "action_owner": getattr(exc, "action_owner", None),
                 "affected_ids": list(getattr(exc, "affected_ids", []))}
        root = self.directory(job_id)
        write_json(root / "error.json", error)
        attempt = data.get("render_attempt")
        attempt_root = root / "revisions" / f"r{attempt}"
        # render owns revision creation; a retry that is refused before dispatch
        # must never overwrite the previous attempt's evidence.
        archive_failure = (type(attempt) is int and attempt_root.is_dir()
                           and error["code"] != "REVISION_EXISTS")
        if archive_failure and not (attempt_root / "error.json").exists():
            write_json(attempt_root / "error.json", error)
        if hasattr(exc, "fit_rounds"):
            write_json(root / "fit-rounds.json", exc.fit_rounds)
            if archive_failure:
                write_json(attempt_root / "fit-rounds.json", exc.fit_rounds)
        if archive_failure and getattr(exc, "voice_package", None) is not None:
            write_json(attempt_root / "voice-failed.json", exc.voice_package)
        can_review = (root / "draft.json").is_file() and pipeline.read(root / "draft.json").get("status") == "needs_review"
        self.update(job_id, status="interrupted" if error["code"] == "CANCELLED" else "failed",
                    error=error, message=error["message"], can_review=can_review)

    def close(self):
        self.stop_event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        for job_id in list(self.active):
            self.update(job_id, status="interrupted", message="服务已关闭，任务未继续运行")
        self.active.clear()

    def open_editor(self, root):
        with self.editor_lock:
            return pipeline.open_editor(root)

    def submit(self, job_id, action, confirmation=None):
        with self.lock:
            if job_id in self.active:
                raise HTTPException(409, "此任务正在运行")
            self.active.add(job_id)
            self.update(job_id, status="queued", message="已排队；模型依次运行以控制内存")
        self.executor.submit(self.run, job_id, action, confirmation)

    def run(self, job_id, action, confirmation):
        root = self.directory(job_id)
        progress = lambda stage, message: self.event(job_id, stage, message)
        import fcntl
        lock_dir = pipeline.REPO / "outputs/workbench-runtime"
        lock_dir.mkdir(parents=True, exist_ok=True)
        model_lock = (lock_dir / "models.lock").open("a+")
        try:
            while True:
                if self.stop_event.is_set():
                    raise pipeline.PipelineError("CANCELLED", "服务正在关闭，任务已取消")
                try:
                    fcntl.flock(model_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.stop_event.wait(0.3)
            self.update(job_id, status="analyzing" if action == "analyze" else "rendering")
            if action == "analyze":
                pipeline.analyze(root, pipeline.read(root / "settings.json"), self.runtime, progress, cancel_event=self.stop_event)
                self.update(job_id, status="needs_review", stage="review", message="请检查选镜、入出点和旁白，确认后生成")
            else:
                report = pipeline.render(root, confirmation, self.runtime, progress, cancel_event=self.stop_event)
                progress("completed", "视频与工程已保存")
                self.update(job_id, status="completed", stage="completed", message="剪辑视频与剪映工程已生成", result=report)
                if pipeline.read(root / "settings.json").get("open_editor", True):
                    try:
                        editor = self.open_editor(root)
                    except Exception as exc:
                        editor = {"status": "failed", "message": str(exc)}
                    report["editor"] = editor
                    write_json(root / "run-report.json", report)
                    self.update(job_id, result=report)
        except Exception as exc:
            self.fail(job_id, exc)
        finally:
            fcntl.flock(model_lock.fileno(), fcntl.LOCK_UN)
            model_lock.close()
            with self.lock:
                self.active.discard(job_id)

    @staticmethod
    def review_edits(data, draft):
        """Recover editable values by frozen candidate identity, never by new slot."""
        texts, boundaries = {}, {}
        confirmed = data.get("last_confirmation", {})
        if confirmed.get("draft_id", draft["draft_id"]) == draft["draft_id"]:
            texts.update(confirmed.get("approved_text_by_beat", {}))
            overrides = confirmed.get("selection_overrides") or {}
            for beat in draft["beats"]:
                ids = overrides.get(beat["beat_id"], beat["selected_candidate_ids"])
                candidates = {item["candidate_id"]: item for item in beat["candidates"]}
                for edit in confirmed.get("boundaries", []):
                    if edit.get("beat_id") != beat["beat_id"]:
                        continue
                    index = edit.get("shot_index")
                    candidate_id = edit.get("candidate_id")
                    if candidate_id is None and type(index) is int and 0 <= index < len(ids):
                        candidate_id = ids[index]
                    candidate = candidates.get(candidate_id)
                    if candidate and edit.get("segment_id") == candidate["segment_id"]:
                        boundaries.setdefault(beat["beat_id"], {})[candidate_id] = copy.deepcopy(edit.get("source_range_us"))
        if data.get("draft_edits_for") == draft["draft_id"]:
            texts.update(data.get("draft_text_edits", {}))
            # This is the current edit snapshot; removed shots must not reappear
            # from a previous confirmation if added again later.
            boundaries = copy.deepcopy(data.get("draft_boundary_edits", {}))
        elif not texts:
            texts.update(data.get("draft_text_edits", {}))
        return texts, boundaries

    def public(self, job_id):
        data = self.get(job_id)
        root = self.directory(job_id)
        if data["status"] == "needs_review":
            draft = pipeline.read(root / "draft.json")
            from .planning.production import preview_selections
            from .planning.selection import narration_review
            selection = preview_selections(draft, data.get("selection_overrides"))
            texts, boundaries = self.review_edits(data, draft)
            data["draft"] = {"draft_id": draft["draft_id"], "review_revision": data.get("review_revision", 0), "beats": []}
            asset_numbers = {asset["asset_id"]: i + 1 for i, asset in enumerate(draft.get("inputs", {}).get("AssetManifest", {}).get("assets", []))}
            for beat in draft["beats"]:
                chosen = {item["candidate_id"]: item for item in beat["candidates"]}
                selected_ids = selection[beat["beat_id"]]["selected_candidate_ids"]
                planned = {item["candidate_id"]: item for item in selection[beat["beat_id"]]["planned_shots"]}
                shots = []
                for index, candidate_id in enumerate(selected_ids):
                    candidate = chosen[candidate_id]
                    source = candidate["source_range_us"]
                    # Start with an explicit proposed interval; the editor can refine it.
                    length = planned.get(candidate_id, {}).get("suggested_duration_us", min(
                        source["end"] - source["begin"], beat["duration_budget_us"] // len(selected_ids)))
                    interval = boundaries.get(beat["beat_id"], {}).get(candidate_id)
                    if (not isinstance(interval, dict) or type(interval.get("begin")) is not int
                            or type(interval.get("end")) is not int
                            or not source["begin"] <= interval["begin"] < interval["end"] <= source["end"]):
                        interval = {"begin": source["begin"], "end": source["begin"] + length}
                    shots.append({"shot_index": index, "candidate_id": candidate_id,
                                  "asset_id": candidate["asset_id"], "segment_id": candidate["segment_id"],
                                  "origin": candidate["origin"], "available_range_us": source,
                                  "source_range_us": copy.deepcopy(interval),
                                  "summary": candidate.get("summary", ""), "transcript": candidate.get("transcript", ""),
                                  "transcript_evidence": candidate.get("transcript_evidence", {}),
                                  "media_url": f"/api/jobs/{job_id}/media/{candidate['asset_id']}"})
                text = texts.get(beat["beat_id"], beat["narration_draft"])
                review = narration_review(text, beat["duration_budget_us"], max_chars=beat.get("narration_max_chars"))
                data["draft"]["beats"].append({"beat_id": beat["beat_id"], "purpose": beat["purpose"],
                                                "duration_budget_us": beat["duration_budget_us"],
                                                "narration_draft": text, "narration_review": review,
                                                "review_warnings": review["warnings"],
                                                "narration_max_chars": beat.get("narration_max_chars", 160), "shots": shots,
                                                "available_candidates": [{"candidate_id": item["candidate_id"], "label":
                                                    f"素材 {asset_numbers.get(item['asset_id'], '?')} · {item['source_range_us']['begin']/1e6:.2f}–{item['source_range_us']['end']/1e6:.2f} 秒"}
                                                    for item in beat["candidates"]]})
        if data.get("result"):
            data["downloads"] = {"video": f"/api/jobs/{job_id}/files/video", "package": f"/api/jobs/{job_id}/files/package",
                                 "report": f"/api/jobs/{job_id}/files/report"}
        settings_path = root / "settings.json"
        if settings_path.is_file():
            settings = pipeline.read(settings_path)
            data["presentation"] = {
                "voice": settings.get("voice") or "reference",
                "reference_voice_label": _reference_voice_label(root, settings),
                "speech_rate": settings.get("speech_rate", 1.0),
                "subtitle_layout": settings.get("subtitle_layout") or "balanced",
                "subtitle_size": settings.get("subtitle_size") or "medium",
                "subtitle_weight": settings.get("subtitle_weight") or "regular",
                "subtitle_color": settings.get("subtitle_color") or "white",
            }
        return data


def create_app(*, root=None, runtime=None):
    runtime = runtime or pipeline.Runtime.configured()
    jobs = Jobs(root or pipeline.REPO / "outputs/workbench", runtime)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()

    app = FastAPI(title="QuickCut 本地工作台", lifespan=lifespan)
    app.state.jobs, app.state.csrf_token = jobs, token

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0]
        if host not in {"localhost", "127.0.0.1", "testserver"}:
            return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if origin and origin != expected:
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
            if not secrets.compare_digest(request.headers.get("x-quickcut-token", ""), token):
                return JSONResponse({"detail": "页面已过期，请刷新后重试"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (Path(__file__).parent / "web/index.html").read_text(encoding="utf-8")

    @app.get("/api/config")
    def config():
        from .music import catalog_status
        try:
            _, _, voice = _voice_source(runtime)
            label = voice.get("voice_label") or Path(voice["reference_audio"]).stem
            default_voice = {"label": str(label), "ready": True}
        except HTTPException:
            default_voice = {"label": "默认音色尚未配置", "ready": False}
        return {"token": token, "max_file_bytes": MAX_FILE_BYTES, "max_reference_bytes": MAX_REFERENCE_BYTES,
                "music": catalog_status(),
                "default_voice": default_voice,
                "voice_presets": _voice_presets_status(),
                "models": {"vision": "nllb-clip-base", "planning": runtime.model, "voice": "IndexTTS2"}}

    @app.get("/api/health")
    def health():
        return pipeline.runtime_status(runtime)

    @app.get("/api/jobs")
    def history():
        with jobs.lock:
            items = [pipeline.read(path) for path in jobs.root.glob("*/job.json")]
        return sorted(items, key=lambda item: item["created_at"], reverse=True)[:30]

    @app.post("/api/jobs")
    async def create_job(files: list[UploadFile] = File(...), prompt: str = Form(...), duration: float = Form(30),
                         origin: str = Form("user"), open_editor: bool = Form(True),
                         subtitle_position: str = Form("bottom"), reference_audio: UploadFile | None = File(None),
                         auto_music: bool = Form(False)):
        if not prompt.strip() or len(prompt) > 2000:
            raise HTTPException(422, "请输入 1–2000 字的剪辑要求")
        if not math.isfinite(duration) or not 10 <= duration <= 120:
            raise HTTPException(422, "成片时长应为 10–120 秒")
        if not 1 <= len(files) <= 12 or origin not in {"user", "external"}:
            raise HTTPException(422, "请选择 1–12 个视频和有效素材来源")
        if subtitle_position not in {"top", "bottom"}:
            raise HTTPException(422, "旁白字幕位置应为顶部或底部")
        if auto_music:
            from .music import catalog_status
            if not catalog_status()["available"]:
                raise HTTPException(422, "配乐曲库尚未准备好，请先安装曲库或关闭自动配乐")
        if any(Path(file.filename or "").suffix.lower() not in VIDEO_SUFFIXES for file in files):
            raise HTTPException(422, "请选择 MP4、MOV、MKV、WebM、M4V 或 AVI 视频")
        if reference_audio and Path(reference_audio.filename or "").suffix.lower() != ".wav":
            raise HTTPException(422, "参考音频请选择 WAV 文件")
        voice_source_path, voice_source_bytes, voice = _voice_source(runtime)
        job_id = uuid.uuid4().hex[:12]
        root = jobs.root / job_id
        uploads = root / "uploads"
        uploads.mkdir(parents=True)
        sources = []
        try:
            reference_path = root / "reference" / "voice.wav"
            reference_path.parent.mkdir()
            if reference_audio:
                size = 0
                with reference_path.open("xb") as stream:
                    while chunk := await reference_audio.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_REFERENCE_BYTES:
                            raise HTTPException(413, "参考音频不能超过 20 MB")
                        stream.write(chunk)
                reference_source = {"kind": "upload", "filename": Path(reference_audio.filename).name}
            else:
                default_reference = _repo_path(voice["reference_audio"])
                try:
                    if default_reference.stat().st_size > MAX_REFERENCE_BYTES:
                        raise HTTPException(413, "默认参考音频不能超过 20 MB")
                    reference_path.write_bytes(default_reference.read_bytes())
                except OSError as exc:
                    raise HTTPException(422, f"无法读取默认参考音频：{exc}") from exc
                reference_source = {"kind": "default", "filename": default_reference.name, "uri": str(default_reference)}
            reference = {**reference_source, **_reference_info(reference_path), "path": "reference/voice.wav"}
            voice["model_dir"] = str(_repo_path(voice["model_dir"]))
            voice["reference_audio"] = str(reference_path.resolve())
            normalized_cues = []
            for cue_id, override in voice.get("cue_overrides", {}).items():
                if "reference_audio" in override:
                    override["reference_audio"] = voice["reference_audio"]
                    normalized_cues.append(cue_id)
            if reference_audio:
                voice["voice_label"] = Path(reference_audio.filename).stem
            write_json(root / "voice-config.json", voice)
            write_json(root / "voice-reference-config.json", voice)
            voice_source = {"uri": str(voice_source_path), "sha256": hashlib.sha256(voice_source_bytes).hexdigest(),
                            "frozen_at": pipeline.now(), "reference": reference,
                            "reference_policy": "one_frozen_reference_per_job", "normalized_cue_reference_ids": normalized_cues}
            for index, file in enumerate(files):
                name = Path(file.filename or "video.mp4").name
                path = uploads / f"{index:02d}{Path(name).suffix.lower()}"
                size = 0
                with path.open("xb") as stream:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise HTTPException(413, "单个视频不能超过 4 GB")
                        stream.write(chunk)
                if not size:
                    raise HTTPException(422, "视频文件为空")
                sources.append({"path": str(path), "origin": origin, "source_key": f"upload-{index}-{name}",
                                "provenance_note": f"工作台导入：{name}；来源由提交者选择为 {origin}"})
            settings = {"project_id": "project-" + job_id, "prompt": prompt.strip(), "duration": duration,
                        "sources": sources, "open_editor": open_editor, "subtitle_position": subtitle_position,
                        "auto_music": auto_music,
                        "voice_config": "voice-config.json", "voice_config_source": voice_source,
                        "reference_audio": reference}
            write_json(root / "settings.json", settings)
            write_json(root / "job.json", {"job_id": job_id, "created_at": pipeline.now(), "updated_at": pipeline.now(),
                                          "status": "created", "prompt": prompt.strip(), "duration": duration,
                                          "file_names": [Path(file.filename or "video").name for file in files], "events": []})
        except Exception:
            # Only remove this request's newly created incomplete upload directory.
            import shutil
            shutil.rmtree(root)
            raise
        finally:
            for file in files:
                await file.close()
            if reference_audio:
                await reference_audio.close()
        jobs.submit(job_id, "analyze")
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return jobs.public(job_id)

    @app.post("/api/jobs/{job_id}/confirm")
    def confirm(job_id: str, payload: dict):
        with jobs.lock:
            data = jobs.public(job_id)
            if data["status"] != "needs_review" or job_id in jobs.active:
                raise HTTPException(409, "当前任务不在方案确认阶段或仍在运行")
            if payload.get("draft_id") != data["draft"]["draft_id"]:
                raise HTTPException(409, "方案版本已变化，请刷新")
            if payload.get("review_revision", 0) != data["draft"]["review_revision"]:
                raise HTTPException(409, "选镜已经变化，请刷新后检查")
            reviewer = payload.get("reviewer", "")
            if not isinstance(reviewer, str):
                raise HTTPException(422, "确认者应为文本")
            reviewer = reviewer.strip()
            if not reviewer or len(reviewer) > 100 or payload.get("confirmed") is not True:
                raise HTTPException(422, "请确认已检查选镜、边界和旁白")
            confirmation_kind = payload.get("confirmation_kind", "human")
            if confirmation_kind not in {"human", "assistant_review"}:
                raise HTTPException(422, "确认方式无效")
            text = payload.get("approved_text_by_beat")
            expected = {beat["beat_id"] for beat in data["draft"]["beats"]}
            if not isinstance(text, dict) or set(text) != expected or any(not isinstance(value, str) or not value.strip() or len(value) > 160 for value in text.values()):
                raise HTTPException(422, "请逐段填写 1–160 字符旁白")
            from .planning.selection import narration_review
            for beat in data["draft"]["beats"]:
                review = narration_review(text[beat["beat_id"]], beat["duration_budget_us"], max_chars=beat["narration_max_chars"])
                if review["warnings"]:
                    raise HTTPException(422, f"{beat['purpose']}旁白需修改：" + "；".join(item["message"] for item in review["warnings"]))
            edits = payload.get("boundaries")
            slots = {(beat["beat_id"], shot["shot_index"]): shot for beat in data["draft"]["beats"] for shot in beat["shots"]}
            if not isinstance(edits, list) or len(edits) != len(slots):
                raise HTTPException(422, "请确认每个镜头的入出点")
            seen, validated = set(), []
            for edit in edits:
                if not isinstance(edit, dict) or not isinstance(edit.get("source_range_us"), dict) or type(edit.get("shot_index")) is not int or not isinstance(edit.get("beat_id"), str):
                    raise HTTPException(422, "镜头区间格式无效")
                key = (edit.get("beat_id"), edit.get("shot_index"))
                shot = slots.get(key)
                interval = edit.get("source_range_us", {})
                begin, end = interval.get("begin"), interval.get("end")
                if (not shot or key in seen or edit.get("segment_id") != shot["segment_id"]
                        or type(begin) is not int or type(end) is not int
                        or not shot["available_range_us"]["begin"] <= begin < end <= shot["available_range_us"]["end"]):
                    raise HTTPException(422, "镜头入出点必须位于该候选的真实区间内")
                seen.add(key)
                validated.append({"beat_id": key[0], "shot_index": key[1], "candidate_id": shot["candidate_id"], "segment_id": shot["segment_id"],
                                  "source_range_us": {"begin": begin, "end": end}, "method": "manual",
                                  "reviewer": reviewer, "reason": "用户在预览界面确认的源视频入出点" if confirmation_kind == "human" else "助手验收核对真实画面后的源视频入出点"})
            for beat in data["draft"]["beats"]:
                length = sum(item["source_range_us"]["end"] - item["source_range_us"]["begin"]
                             for item in validated if item["beat_id"] == beat["beat_id"])
                if length > beat["duration_budget_us"]:
                    raise HTTPException(422, "这一段选中的镜头总长超过预算，请缩短入出点")
            root = jobs.directory(job_id)
            style = _subtitle_fields(payload)
            speech_rate = payload.get("speech_rate")
            if speech_rate is not None and (type(speech_rate) not in (int, float) or speech_rate not in {0.8, 0.9, 1.0, 1.1}):
                raise HTTPException(422, "配音语速应为 0.8、0.9、1 或 1.1 倍")
            settings = pipeline.read(root / "settings.json")
            if payload.get("voice") not in (None, ""):
                _apply_voice_preset(root, payload["voice"], settings)
            settings.update(style)
            if speech_rate is not None:
                settings["speech_rate"] = speech_rate
            write_json(root / "settings.json", settings)
            confirmation = {"draft_id": payload["draft_id"], "reviewer": reviewer, "reviewed_at": pipeline.now(),
                            "confirmation_kind": confirmation_kind, "approved_text_by_beat": text, "boundaries": validated,
                            "revision": data.get("render_attempt", 0) + 1, "selection_overrides": data.get("selection_overrides")}
            saved_boundaries = {}
            for item in validated:
                saved_boundaries.setdefault(item["beat_id"], {})[item["candidate_id"]] = item["source_range_us"]
            jobs.update(job_id, render_attempt=confirmation["revision"], last_confirmation=confirmation,
                        draft_edits_for=payload["draft_id"], draft_text_edits=text,
                        draft_boundary_edits=saved_boundaries, error=None)
            jobs.submit(job_id, "render", confirmation)
        return {"job_id": job_id, "status": "queued"}

    @app.post("/api/jobs/{job_id}/selection")
    def selection_edit(job_id: str, payload: dict):
        from .planning.production import preview_selections
        with jobs.lock:
            data = jobs.get(job_id)
            draft = pipeline.read(jobs.directory(job_id) / "draft.json")
            if data["status"] != "needs_review" or payload.get("draft_id") != draft["draft_id"] or payload.get("review_revision", 0) != data.get("review_revision", 0):
                raise HTTPException(409, "方案状态已变化，请刷新")
            selections = payload.get("selection_overrides")
            try:
                preview = preview_selections(draft, selections)
            except Exception as exc:
                raise HTTPException(422, str(exc)) from exc
            expected = {beat["beat_id"] for beat in draft["beats"]}
            text = payload.get("draft_text_edits", {})
            if (not isinstance(text, dict) or not set(text) <= expected
                    or any(not isinstance(value, str) or len(value) > 160 for value in text.values())):
                raise HTTPException(422, "旁白草稿格式无效")
            saved_text, saved_boundaries = jobs.review_edits(data, draft)
            saved_text.update(text)
            edits = payload.get("draft_boundaries", [])
            if not isinstance(edits, list):
                raise HTTPException(422, "镜头草稿区间格式无效")
            candidates = {(beat["beat_id"], item["candidate_id"]): item
                          for beat in draft["beats"] for item in beat["candidates"]}
            seen = set()
            for edit in edits:
                if (not isinstance(edit, dict) or not isinstance(edit.get("beat_id"), str)
                        or not isinstance(edit.get("candidate_id"), str)
                        or not isinstance(edit.get("source_range_us"), dict)):
                    raise HTTPException(422, "镜头草稿区间格式无效")
                key = (edit["beat_id"], edit["candidate_id"])
                candidate = candidates.get(key)
                interval = edit["source_range_us"]
                begin, end = interval.get("begin"), interval.get("end")
                if (not candidate or key in seen or edit.get("segment_id") != candidate["segment_id"]
                        or type(begin) is not int or type(end) is not int
                        or not candidate["source_range_us"]["begin"] <= begin < end <= candidate["source_range_us"]["end"]):
                    raise HTTPException(422, "草稿入出点必须位于对应候选的真实区间内")
                seen.add(key)
                saved_boundaries.setdefault(key[0], {})[key[1]] = {"begin": begin, "end": end}
            # Slot indexes change when reordering; keep only candidate identities
            # still selected, so a replacement starts from its own planned range.
            saved_boundaries = {beat_id: {candidate_id: interval for candidate_id, interval in saved_boundaries.get(beat_id, {}).items()
                                         if candidate_id in row["selected_candidate_ids"]}
                                for beat_id, row in preview.items()}
            jobs.update(job_id, selection_overrides=selections, draft_edits_for=draft["draft_id"],
                        draft_text_edits=saved_text, draft_boundary_edits=saved_boundaries,
                        review_revision=data.get("review_revision", 0) + 1)
            return jobs.public(job_id)

    @app.post("/api/jobs/{job_id}/return-review")
    def return_review(job_id: str):
        with jobs.lock:
            data = jobs.get(job_id)
            draft_ready = (jobs.directory(job_id) / "draft.json").is_file()
            recoverable = data["status"] == "completed" or (data["status"] in {"failed", "interrupted"} and data.get("can_review"))
            if job_id in jobs.active or not draft_ready or not recoverable:
                raise HTTPException(409, "此任务没有可以恢复的已完成草案")
            jobs.update(job_id, status="needs_review", stage="review", error=None,
                        message="已回到方案。可以修改镜头和旁白，再次确认后生成新成片；刚才的成片仍可查看。")
        return {"job_id": job_id}

    @app.post("/api/jobs/{job_id}/retry-analysis")
    def retry_analysis(job_id: str):
        with jobs.lock:
            data = jobs.get(job_id)
            if data["status"] not in {"failed", "interrupted"} or job_id in jobs.active:
                raise HTTPException(409, "此任务尚未停止")
            jobs.update(job_id, error=None, can_review=False, selection_overrides=None,
                        draft_text_edits={}, draft_boundary_edits={}, draft_edits_for=None,
                        last_confirmation={}, review_revision=0)
            jobs.submit(job_id, "analyze")
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}/media/{asset_id}")
    def media(job_id: str, asset_id: str):
        root = jobs.directory(job_id)
        path = root / "asset-manifest.json"
        if not path.is_file():
            raise HTTPException(404, "素材尚未准备好")
        asset = next((item for item in pipeline.read(path)["assets"] if item["asset_id"] == asset_id), None)
        if asset is None:
            raise HTTPException(404, "找不到素材")
        try:
            path = resolve_uri(root, asset["uri"])
        except ValueError as exc:
            raise HTTPException(400, "素材路径无效") from exc
        return FileResponse(path)

    @app.get("/api/jobs/{job_id}/files/{kind}")
    def artifact(job_id: str, kind: str, download: bool = False):
        root = jobs.directory(job_id)
        report = pipeline.read(root / "run-report.json") if (root / "run-report.json").exists() else {}
        names = {"video": report.get("files", {}).get("video"), "package": report.get("files", {}).get("package"),
                 "report": "run-report.json", "error": "error.json"}
        if kind not in names or not names[kind]:
            raise HTTPException(404, "文件尚未生成")
        path = resolve_uri(root, names[kind])
        if not path.is_file():
            raise HTTPException(404, "文件尚未生成")
        return FileResponse(path, filename=path.name if download or kind == "package" else None,
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/jobs/{job_id}/open-editor")
    def editor(job_id: str):
        # Returning to a draft preserves its last export. Do not let a new
        # confirmation race with opening/updating that previous revision.
        with jobs.lock:
            data = jobs.get(job_id)
            if (data["status"] not in {"completed", "needs_review"}
                    or not data.get("result") or job_id in jobs.active):
                raise HTTPException(409, "工程尚未生成或任务正在运行")
            try:
                opened = jobs.open_editor(jobs.directory(job_id))
            except Exception as exc:
                raise HTTPException(409, str(exc)) from exc
            result = data["result"]
            result["editor"] = opened
            write_json(jobs.directory(job_id) / "run-report.json", result)
            jobs.update(job_id, result=result)
            return opened

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="在浏览器打开工作台")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    runtime = pipeline.Runtime.configured()
    try:
        pipeline.start_ollama(runtime)
    except pipeline.PipelineError as exc:
        print(f"Qwen 尚未就绪：{exc}", flush=True)
    app = create_app(root=args.output_root, runtime=runtime)
    if args.open:
        import webbrowser
        timer = threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}"))
        timer.daemon = True
        timer.start()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

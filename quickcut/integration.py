"""C/E/D 编排；初测与有界 fit 返回使用不同的严格入口。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import voice_package
from .planning import PlanResult, validate_story_plan
from .planning.contracts import check_envelope, check_inputs, check_project, ref
from .timeline import TimelineError, compile_timeline
from .retiming import (compile_reply, content_hash, prepare_pending, validate_reply_identity,
                       validate_voice_content, verify_voice_media)

Document = dict[str, Any]


class IntegrationError(ValueError):
    """失败时保留 E 的原始反馈，供调用方保存或交回 C/E。"""

    def __init__(self, code: str, message: str, *, action_owner: str,
                 affected_ids: tuple[str, ...] = (), voice: Document | None = None,
                 fit_rounds: list[Document] | None = None):
        super().__init__(message)
        self.code = code
        self.action_owner = action_owner
        self.affected_ids = affected_ids
        self.voice_package = deepcopy(voice)
        self.fit_rounds = deepcopy(fit_rounds or [])

    def to_dict(self, project_id: str) -> Document:
        return {
            "status": "needs_retime" if self.code == "VOICE_NEEDS_RETIME" else "failed",
            "code": self.code, "stage": "integration", "project_id": project_id,
            "message": str(self), "retryable": False,
            "affected_ids": list(self.affected_ids), "action_owner": self.action_owner,
            "fit_rounds": deepcopy(self.fit_rounds),
        }


@dataclass
class IntegrationResult:
    story_plan: Document
    voice_package: Document
    timeline: Document
    assets: list[Document]
    fit_rounds: list[Document] = field(default_factory=list)


def _validate_plan(result: PlanResult, brief: Document, manifest: Document,
                   candidate_set: Document, expected_story_revision: int | None) -> None:
    story = result.story_plan
    check_envelope(story, expected_kind="StoryPlan", owner="C")
    if expected_story_revision is not None and (
        type(expected_story_revision) is not int or expected_story_revision < 1
        or story["revision"] != expected_story_revision
    ):
        raise IntegrationError("STALE_INPUT_REVISION", "StoryPlan 与请求版本不一致", action_owner="C")
    if story.get("status") != "ready_for_edit" or story.get("unresolved_gaps") or story.get("pending_reviews"):
        raise IntegrationError("STORY_NOT_READY", "C 仍有缺口或待审事项，不能合成或编排", action_owner="C")
    available = {"Brief": brief, "AssetManifest": manifest, "CandidateSet": candidate_set,
                 "RetrievalRequest": result.retrieval_request}
    for document in (result.retrieval_request, candidate_set, story):
        check_inputs(document, available)
    report = validate_story_plan(
        story, brief=brief, manifest=manifest, candidate_set=candidate_set,
        upstream={"RetrievalRequest": result.retrieval_request},
    )
    if not report["ok"]:
        raise IntegrationError("VLM_INVALID_PLAN", "；".join(report["errors"]), action_owner="C")


def _compile(result: PlanResult, brief: Document, manifest: Document, voice: Document,
             output: Document, document_id: str, revision: int, boundaries: Document | None = None) -> IntegrationResult:
    story = result.story_plan
    check_envelope(voice, expected_kind="VoicePackage", owner="E")
    check_project(story, voice)
    check_inputs(voice, {"StoryPlan": story})
    if voice.get("inputs") != [ref(story)]:
        raise IntegrationError("STALE_INPUT_REVISION", "初次编排只接受精确绑定 StoryPlan 的语音测量包", action_owner="E")
    if story["fixtures_only"] and not voice["fixtures_only"]:
        raise IntegrationError("INVALID_INPUT", "E 不得清除 C 的 fixture 标记", action_owner="E")
    if voice.get("status") != "ready" or voice.get("errors"):
        code = "VOICE_NEEDS_RETIME" if voice.get("status") == "needs_retime" else "VOICE_NOT_READY"
        affected = tuple(c["cue_id"] for c in voice.get("cues", [])
                         if c.get("fit_status") != "fits" or c.get("needs_retime") is not False)
        raise IntegrationError(code, f"E 返回 {voice.get('status')!r}，停止编排",
                               action_owner="D" if code == "VOICE_NEEDS_RETIME" else "E",
                               affected_ids=affected, voice=voice)
    for cue in voice.get("cues", []):
        if "script_input" in cue and (
            cue["script_input"] != ref(story) or type(cue["script_input"].get("revision")) is not int
        ):
            raise IntegrationError("STALE_INPUT_REVISION", "语音句子绑定了旧脚本", action_owner="E",
                                   affected_ids=(cue["cue_id"],), voice=voice)
        if cue.get("fit_status") != "fits" or cue.get("needs_retime") is not False:
            raise IntegrationError("VOICE_NOT_READY", "E 句子未通过时长检查", action_owner="E",
                                   affected_ids=(cue["cue_id"],), voice=voice)
    timeline = compile_timeline(brief, manifest, story, voice, output=output,
                                document_id=document_id, revision=revision, boundaries=boundaries)
    return IntegrationResult(
        deepcopy(story), deepcopy(voice), timeline,
        deepcopy(manifest["assets"] + voice["assets"]),
    )


def compile_from_voice(
    result: PlanResult, voice: Document, *, brief: Document, manifest: Document,
    candidate_set: Document, output: Document, expected_story_revision: int | None = None,
    timeline_document_id: str = "timeline-001", timeline_revision: int = 1,
    boundaries: Document | None = None,
) -> IntegrationResult:
    """把已有 E 包交给 D；拒绝非 ready、旧版本和时间线专用重合成包。

    C 校验需要本次实际 CandidateSet 和 result.retrieval_request，不重建审批。
    A/E 的 URI 均相对于同一项目根；assets 为两份清单的并集，不回写 A。
    PlanningError、TimelineError 原样上抛；桥接拒绝抛 IntegrationError。
    """
    _validate_plan(result, brief, manifest, candidate_set, expected_story_revision)
    return _compile(result, brief, manifest, voice, output, timeline_document_id, timeline_revision, boundaries)


def build_from_plan(
    result: PlanResult, *, brief: Document, manifest: Document, candidate_set: Document,
    root: str | Path, output: Document, fixture: bool = False, config: Document | None = None,
    mode: str = "measure", previous: Document | None = None, voice_revision: int = 1,
    expected_story_revision: int | None = None, timeline_document_id: str = "timeline-001",
    timeline_revision: int = 1,
    boundaries: Document | None = None,
) -> IntegrationResult:
    """消费 C 的真实 plan() 返回值，执行 E 合成并交给 D 初次编排。

    fixture=True 明确生成测试音调；False 使用 E 配置的本地合成后端。
    root 必须是 A/E 共用的项目根。只由 E 写音频/局部 SRT/缓存；本函数不
    保存 JSON、不导出、不声明听审或导入成功。非 ready 包在异常中保留。
    """
    _validate_plan(result, brief, manifest, candidate_set, expected_story_revision)
    voice = voice_package.build_voice_package(
        result.story_plan, root, fixture=fixture, config=config, mode=mode, previous=previous,
        revision=voice_revision, expected_revision=result.story_plan["revision"],
    )
    return _compile(result, brief, manifest, voice, output, timeline_document_id, timeline_revision, boundaries)


def prepare_voice_fit(
    result: PlanResult, measured_voice: Document, *, brief: Document, manifest: Document,
    candidate_set: Document, output: Document, project_root: str | Path,
    expected_story_revision: int, timeline_document_id: str = "timeline-001", timeline_revision: int = 1,
    iteration: int = 1, max_iterations: int = 2, previous_request: Document | None = None,
    boundaries: Document | None = None,
) -> Document:
    """Return {pending_timeline, request}; does not synthesize or export.

    request.voice_revision is the required E --revision. Every request binds the
    precise pending timeline, source documents, manual boundaries and prior reply.
    """
    if type(expected_story_revision) is not int or expected_story_revision < 1:
        raise IntegrationError("STALE_INPUT_REVISION", "必须提供本次请求锁定的 StoryPlan revision", action_owner="D")
    _validate_plan(result, brief, manifest, candidate_set, expected_story_revision)
    validate_voice_content(result.story_plan, measured_voice, allow_needs_retime=True)
    verify_voice_media(measured_voice, project_root)
    return prepare_pending(result.story_plan, measured_voice, brief, manifest, output,
        document_id=timeline_document_id, revision=timeline_revision, iteration=iteration,
        max_iterations=max_iterations, previous_request=previous_request, boundaries=boundaries)


def compile_voice_fit(
    result: PlanResult, fitted_voice: Document, *, pending_timeline: Document, request: Document,
    brief: Document, manifest: Document, candidate_set: Document, project_root: str | Path,
    expected_story_revision: int, boundaries: Document | None = None,
) -> IntegrationResult:
    """Accept only this outstanding request; preserve E's Timeline reference."""
    if type(expected_story_revision) is not int or expected_story_revision < 1:
        raise IntegrationError("STALE_INPUT_REVISION", "必须提供本次请求锁定的 StoryPlan revision", action_owner="D")
    _validate_plan(result, brief, manifest, candidate_set, expected_story_revision)
    validate_reply_identity(pending_timeline, request, fitted_voice)
    validate_voice_content(result.story_plan, fitted_voice, allow_needs_retime=True)
    verify_voice_media(fitted_voice, project_root)
    if fitted_voice["status"] == "needs_retime":
        raise IntegrationError("VOICE_NEEDS_RETIME", "完整语音仍超出指定区间，不能裁尾接受", action_owner="D",
            affected_ids=tuple(c["cue_id"] for c in fitted_voice["cues"] if c["needs_retime"]), voice=fitted_voice)
    timeline = compile_reply(result.story_plan, fitted_voice, pending_timeline, request, brief, manifest, boundaries)
    return IntegrationResult(deepcopy(result.story_plan), deepcopy(fitted_voice), timeline,
                             deepcopy(manifest["assets"] + fitted_voice["assets"]))


def complete_voice_timeline(
    result: PlanResult, measured_voice: Document, *, brief: Document, manifest: Document,
    candidate_set: Document, output: Document, project_root: str | Path, expected_story_revision: int,
    fit_provider: Callable[[Document], Document] | None = None, max_iterations: int = 2,
    timeline_document_id: str = "timeline-001", timeline_revision: int = 1,
    boundaries: Document | None = None,
    fit_observer: Callable[[Document], None] | None = None,
) -> IntegrationResult:
    """Compile actual measurements, then run at most two serial D→E→D rounds.

    fit_provider(request) may execute E in a separate Python environment/process;
    it returns the parsed VoicePackage. No model is instantiated by this entry.
    Success and failure both retain all pending/request/reply snapshots and hashes.
    Optional fit_observer(record) persists a defensive copy before dispatch and
    after each reply/state transition. Observer failure stops publication.
    Caller persists them and invalidates any obsolete user task before publishing.
    """
    if type(max_iterations) is not int or not 1 <= max_iterations <= 2:
        raise IntegrationError("INVALID_INPUT", "fit 最多允许两轮", action_owner="D")
    if type(expected_story_revision) is not int or expected_story_revision < 1:
        raise IntegrationError("STALE_INPUT_REVISION", "必须锁定 StoryPlan revision", action_owner="D")
    rounds: list[Document] = []
    voice = deepcopy(measured_voice)
    def observe(record):
        if fit_observer is not None:
            try:
                fit_observer(deepcopy(record))
            except Exception as exc:
                raise IntegrationError("TRACE_WRITE_FAILED", f"无法保存 D/E 适配记录: {exc}",
                    action_owner="D", voice=voice, fit_rounds=rounds) from exc
    try:
        _validate_plan(result, brief, manifest, candidate_set, expected_story_revision)
        validate_voice_content(result.story_plan, voice, allow_needs_retime=True)
        if voice.get("mode") != "measure":
            raise IntegrationError("STALE_INPUT_REVISION", "闭环起点必须是原始 measure 包", action_owner="E", voice=voice)
        verify_voice_media(voice, project_root)
        try:
            return _compile(result, brief, manifest, voice, output, timeline_document_id, timeline_revision, boundaries)
        except (IntegrationError, TimelineError) as exc:
            if exc.code != "VOICE_NEEDS_RETIME":
                raise
        if fit_provider is None:
            raise IntegrationError("VOICE_NEEDS_RETIME", "需要外部 E fit_provider 完成适配；当前未配置", action_owner="D", voice=voice)
        previous_request = None
        for iteration in range(1, max_iterations + 1):
            prepared = prepare_voice_fit(result, voice, brief=brief, manifest=manifest, candidate_set=candidate_set,
                output=output, project_root=project_root, expected_story_revision=expected_story_revision,
                timeline_document_id=timeline_document_id, timeline_revision=timeline_revision,
                iteration=iteration, max_iterations=max_iterations, previous_request=previous_request, boundaries=boundaries)
            pending, request = prepared["pending_timeline"], prepared["request"]
            record = {"iteration": iteration, "pending_timeline": deepcopy(pending), "request": deepcopy(request),
                "pending_hash": content_hash(pending), "request_hash": content_hash(request), "reply": None, "status": "requested"}
            rounds.append(record)
            observe(record)
            try:
                reply = fit_provider(deepcopy(request))
            except Exception as exc:
                cancelled = getattr(exc, "code", None) == "CANCELLED"
                code = "CANCELLED" if cancelled else "VOICE_PROVIDER_FAILED"
                record.update(status="cancelled" if cancelled else "provider_failed", error=str(exc), error_code=code)
                observe(record)
                raise IntegrationError(code, f"外部 E {'已取消' if cancelled else '执行失败'}: {exc}", action_owner="E", voice=voice) from exc
            if not isinstance(reply, dict):
                record.update(status="rejected", error="E provider did not return an object")
                observe(record)
                raise IntegrationError("VOICE_NOT_READY", "E provider 未返回 VoicePackage 对象", action_owner="E", voice=voice)
            record.update(reply=deepcopy(reply), status="received")
            try:
                record["reply_hash"] = content_hash(reply)
            except (ValueError, TypeError) as exc:
                record.update(status="rejected", error=str(exc))
                observe(record)
                raise IntegrationError("VOICE_NOT_READY", "E reply 不是可哈希的标准 JSON", action_owner="E", voice=voice) from exc
            observe(record)
            try:
                compiled = compile_voice_fit(result, reply, pending_timeline=pending, request=request,
                    brief=brief, manifest=manifest, candidate_set=candidate_set, project_root=project_root,
                    expected_story_revision=expected_story_revision, boundaries=boundaries)
                record["status"] = "accepted"
                observe(record)
                compiled.fit_rounds = deepcopy(rounds)
                return compiled
            except (IntegrationError, TimelineError) as exc:
                if not isinstance(exc, IntegrationError) or exc.code != "VOICE_NEEDS_RETIME":
                    record.update(status="rejected", error=str(exc), error_code=exc.code)
                    observe(record)
                    raise
                record["status"] = "needs_retime"
                observe(record)
                voice, previous_request = deepcopy(reply), deepcopy(request)
        raise IntegrationError("VOICE_NEEDS_RETIME", "两轮或配置轮数已耗尽；保留完整语音，交 C 改稿或人工调整画面",
            action_owner="C", affected_ids=tuple(c["cue_id"] for c in voice["cues"] if c["needs_retime"]), voice=voice)
    except (IntegrationError, TimelineError) as exc:
        if isinstance(exc, IntegrationError):
            exc.fit_rounds = deepcopy(rounds)
            if exc.voice_package is None:
                exc.voice_package = deepcopy(voice)
            raise
        raise IntegrationError(exc.code, str(exc), action_owner=exc.action_owner,
            affected_ids=exc.affected_ids, voice=voice, fit_rounds=rounds) from exc

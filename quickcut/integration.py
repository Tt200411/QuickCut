"""C → E → D 的初次编排桥接；输入审批和版本不由桥接补写。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import voice_package
from .planning import PlanResult, validate_story_plan
from .planning.contracts import check_envelope, check_inputs, check_project, ref
from .timeline import compile_timeline

Document = dict[str, Any]


class IntegrationError(ValueError):
    """失败时保留 E 的原始反馈，供调用方保存或交回 C/E。"""

    def __init__(self, code: str, message: str, *, action_owner: str,
                 affected_ids: tuple[str, ...] = (), voice: Document | None = None):
        super().__init__(message)
        self.code = code
        self.action_owner = action_owner
        self.affected_ids = affected_ids
        self.voice_package = deepcopy(voice)

    def to_dict(self, project_id: str) -> Document:
        return {
            "status": "needs_retime" if self.code == "VOICE_NEEDS_RETIME" else "failed",
            "code": self.code, "stage": "integration", "project_id": project_id,
            "message": str(self), "retryable": False,
            "affected_ids": list(self.affected_ids), "action_owner": self.action_owner,
        }


@dataclass
class IntegrationResult:
    story_plan: Document
    voice_package: Document
    timeline: Document
    assets: list[Document]


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
             output: Document, document_id: str, revision: int) -> IntegrationResult:
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
                                document_id=document_id, revision=revision)
    return IntegrationResult(
        deepcopy(story), deepcopy(voice), timeline,
        deepcopy(manifest["assets"] + voice["assets"]),
    )


def compile_from_voice(
    result: PlanResult, voice: Document, *, brief: Document, manifest: Document,
    candidate_set: Document, output: Document, expected_story_revision: int | None = None,
    timeline_document_id: str = "timeline-001", timeline_revision: int = 1,
) -> IntegrationResult:
    """把已有 E 包交给 D；拒绝非 ready、旧版本和时间线专用重合成包。

    C 校验需要本次实际 CandidateSet 和 result.retrieval_request，不重建审批。
    A/E 的 URI 均相对于同一项目根；assets 为两份清单的并集，不回写 A。
    PlanningError、TimelineError 原样上抛；桥接拒绝抛 IntegrationError。
    """
    _validate_plan(result, brief, manifest, candidate_set, expected_story_revision)
    return _compile(result, brief, manifest, voice, output, timeline_document_id, timeline_revision)


def build_from_plan(
    result: PlanResult, *, brief: Document, manifest: Document, candidate_set: Document,
    root: str | Path, output: Document, fixture: bool = False, config: Document | None = None,
    mode: str = "measure", previous: Document | None = None, voice_revision: int = 1,
    expected_story_revision: int | None = None, timeline_document_id: str = "timeline-001",
    timeline_revision: int = 1,
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
    return _compile(result, brief, manifest, voice, output, timeline_document_id, timeline_revision)

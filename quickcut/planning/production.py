"""Real local VLM drafts followed by an explicit, attributed confirmation.

The server owns the frozen draft. Clients submit edited narration, reviewer and
timestamp only; they cannot replace the model call or its input documents.
Assistant acceptance runs are explicitly distinguished from human approval.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (PlanningError, VLM_INVALID_PLAN, check_envelope, check_inputs,
                        check_project, content_hash)
from .planning import (PlanResult, THEMATIC_POLICY, MONTAGE_ROLES, build_outline, candidates_for_beat,
                       picture_policy, plan)
from .selection import (OllamaSelector, Selection, validate_selection,
                        narration_character_limit, narration_character_count, narration_review)
from .validator import assert_valid, validate_story_plan


def _fail(message: str) -> None:
    raise PlanningError(VLM_INVALID_PLAN, message, retryable=False)


def generate_draft(
    brief: dict[str, Any], manifest: dict[str, Any], candidate_set: dict[str, Any], *,
    retrieval_request: dict[str, Any], project_root: str | Path,
    model: str | None = None, base_url: str | None = None, timeout: float = 180,
    archive_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Call Qwen for each of three beats; never approve narration automatically."""
    inputs = copy.deepcopy({"Brief": brief, "AssetManifest": manifest,
                            "CandidateSet": candidate_set, "RetrievalRequest": retrieval_request})
    brief, manifest, candidate_set, retrieval_request = (
        inputs["Brief"], inputs["AssetManifest"], inputs["CandidateSet"], inputs["RetrievalRequest"])
    for kind, owner in (("Brief", "C"), ("AssetManifest", "A"), ("CandidateSet", "B"), ("RetrievalRequest", "C")):
        check_envelope(inputs[kind], expected_kind=kind, owner=owner)
        if inputs[kind].get("fixtures_only") is not False:
            _fail("生产草案不接受 fixture 输入")
    check_project(*inputs.values())
    check_inputs(retrieval_request, inputs)
    check_inputs(candidate_set, inputs)
    if picture_policy(brief) != THEMATIC_POLICY:
        _fail("生产主题草案要求 picture_policy=thematic_montage；素材来源保持原值")
    root = Path(project_root).resolve()
    selector = OllamaSelector(project_root=root, model=model, base_url=base_url, timeout=timeout,
                              archive_dir=archive_dir or root / "planning-evidence")
    assets = {asset["asset_id"]: asset for asset in manifest["assets"]}
    selected_segments: set[str] = set()
    beats, calls, model_attempts = [], [], []
    for beat in build_outline(brief):
        by_segment: dict[str, dict[str, Any]] = {}
        for candidate in candidates_for_beat(beat, retrieval_request, candidate_set, manifest):
            sid = candidate["segment_id"]
            if sid in selected_segments:
                continue
            # Different B queries may return aliases for the same physical clip.
            # Keep the highest-ranked representative; show each actual clip once.
            if sid in by_segment:
                by_segment[sid]["retrieval_aliases"].append({
                    "candidate_id": candidate["candidate_id"], "query_id": candidate["query_id"]})
            else:
                by_segment[sid] = {**candidate, "retrieval_aliases": []}
        candidates = list(by_segment.values())
        if not candidates:
            _fail(f"{beat['beat_id']} 没有未重复的真实候选，需增加素材或重新检索")
        model_candidates = candidates[:8]
        selection = selector.select(beat=beat, candidates=model_candidates, context={
            "project_id": brief["project_id"], "thematic_montage": True,
            "require_visual": True, "draft_narration": True, "contact_sheet": True, "short_candidate_ids": True,
            "story_theme": brief.get("requirements", {}).get("story", ""),
            "previous_selected_segment_ids": sorted(selected_segments), "keep_alive": "0s",
            "excluded_candidate_ids": [c["candidate_id"] for c in candidates[8:]],
        })
        if not selection.source.startswith("model:") or not (selection.raw_response or {}).get("inference_executed"):
            _fail("生产草案必须来自实际 Ollama 推理，不接受 rule 或固定响应")
        planned_shots, _ = validate_selection(selection, beat=beat, candidates=model_candidates, thematic_montage=True)
        if not isinstance(selection.narration_draft, str):
            _fail("真实模型未返回旁白草案")
        review = narration_review(selection.narration_draft, beat["duration_budget_us"])
        chosen = set(selection.ordered_candidate_ids)
        selected_segments.update(c["segment_id"] for c in candidates if c["candidate_id"] in chosen)
        views = [{**copy.deepcopy(c), "asset_uri": assets[c["asset_id"]]["uri"],
                  "selected": c["candidate_id"] in chosen} for c in candidates]
        record = selection.to_record()
        calls.append(record)
        for attempt in [*selection.raw_response.get("previous_attempts", []), selection.raw_response,
                        *selection.raw_response.get("narration_rewrites", [])]:
            model_attempts.append({"beat_id": beat["beat_id"], "kind": attempt.get("kind", "selection"),
                                   "rewrite_attempt": attempt.get("rewrite_attempt"), "archive_path": attempt.get("archive_path"),
                                   "status": attempt.get("status"), "model": attempt.get("model"),
                                   "started_at": attempt.get("request", {}).get("started_at"),
                                   "format_attempt": attempt.get("request", {}).get("format_attempt", 1),
                                   "inference_executed": attempt.get("inference_executed", False)})
        beats.append({"beat_id": beat["beat_id"], "purpose": beat["purpose"],
                      "duration_budget_us": beat["duration_budget_us"], "candidates": views,
                      "selected_candidate_ids": list(selection.ordered_candidate_ids),
                      "planned_shots": planned_shots,
                      "narration_draft": selection.narration_draft,
                      "narration_max_chars": narration_character_limit(beat["duration_budget_us"]),
                      "narration_char_count": narration_character_count(selection.narration_draft),
                      "narration_review": review, "review_warnings": copy.deepcopy(review["warnings"]),
                      "selection": record, "asserts_specific_user_event": False})
    draft = {
        "status": "needs_review", "project_id": brief["project_id"],
        "created_at": datetime.now(timezone.utc).isoformat(), "beats": beats,
        "model_calls": calls, "inputs": inputs,
        "model_attempts": model_attempts,
        "input_hashes": {kind: content_hash(doc) for kind, doc in inputs.items()},
        "actual_backend": "ollama", "human_review_performed": False,
        "review_warnings": [{"beat_id": beat["beat_id"], **warning} for beat in beats for warning in beat["review_warnings"]],
    }
    draft["draft_id"] = content_hash(draft)
    return draft


def _check_frozen_draft(draft: dict[str, Any]) -> None:
    original = copy.deepcopy(draft)
    draft_id = original.pop("draft_id", None)
    if draft_id != content_hash(original) or draft.get("status") != "needs_review":
        _fail("原始草案已变化，请重新生成并审阅")
    if draft.get("input_hashes") != {kind: content_hash(doc) for kind, doc in draft["inputs"].items()}:
        _fail("草案输入内容已经变化")


def _review_selection(beat: dict[str, Any], ids: list[str], *, source: str) -> Selection:
    record = copy.deepcopy(beat["selection"])
    if ids != beat["selected_candidate_ids"]:
        record.update(ordered_candidate_ids=list(ids), source=source,
                      reasons={cid: "确认时明确替换或重排的主题画面；非模型原始选择" for cid in ids},
                      role_overrides={cid: record["role_overrides"].get(cid, MONTAGE_ROLES.get(beat["beat_id"], "broll")) for cid in ids})
    return Selection(**record)


def preview_selections(
    draft: dict[str, Any], selection_overrides: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Recompute editable shot durations using only this frozen draft's candidates.

    Returns a beat_id mapping with selected_candidate_ids and planned_shots.
    This performs no model call and does not approve a picture or narration.
    """
    _check_frozen_draft(draft)
    overrides = {} if selection_overrides is None else selection_overrides
    beat_ids = {beat["beat_id"] for beat in draft["beats"]}
    if not isinstance(overrides, dict) or set(overrides) - beat_ids:
        _fail("选镜修改必须按当前草案 beat_id 提供")
    result, used_segments = {}, set()
    for beat in draft["beats"]:
        bid = beat["beat_id"]
        ids = overrides.get(bid, beat["selected_candidate_ids"])
        if not isinstance(ids, list) or not ids or any(not isinstance(cid, str) for cid in ids):
            _fail(f"{bid} 必须保留至少一个真实候选 ID")
        allowed = {c["candidate_id"]: c for c in beat["candidates"]}
        if set(ids) - set(allowed):
            _fail(f"{bid} 替代镜头不在该冻结草案的候选中")
        selection = _review_selection(beat, ids, source="review:pending")
        shots, _ = validate_selection(selection, beat=beat, candidates=beat["candidates"], thematic_montage=True)
        selected = {allowed[cid]["segment_id"] for cid in ids}
        if selected & used_segments:
            _fail("不同段落不能重复使用同一源片段，请重新选择")
        used_segments.update(selected)
        result[bid] = {"selected_candidate_ids": list(ids), "planned_shots": shots}
    return result


class _ConfirmedModelSelector:
    """Replay the actual archived selection, never a newly invented model result."""
    name = "ollama"

    def __init__(self, draft: dict[str, Any], selected: dict[str, dict[str, Any]], *, source: str):
        self.records = {beat["beat_id"]: _review_selection(
            beat, selected[beat["beat_id"]]["selected_candidate_ids"], source=source).to_record()
            for beat in draft["beats"]}

    def select(self, *, beat, candidates, context):
        record = self.records[beat["beat_id"]]
        allowed = {c["candidate_id"] for c in candidates}
        if set(record["ordered_candidate_ids"]) - allowed:
            _fail("确认时原始模型选镜已不在当前候选中")
        return Selection(**copy.deepcopy(record))


def confirm_draft(
    draft: dict[str, Any], *, approved_text_by_beat: dict[str, str],
    reviewer: str, reviewed_at: str,
    confirmation_kind: str = "human",
    plan_revision: int = 1,
    selection_overrides: dict[str, list[str]] | None = None,
) -> PlanResult:
    """Validate the server's frozen draft and bind explicit approval to its text."""
    if type(plan_revision) is not int or plan_revision < 1:
        _fail("plan_revision 必须为正整数")
    if not isinstance(reviewer, str) or not reviewer.strip():
        _fail("请记录实际确认者")
    if confirmation_kind not in {"human", "assistant_review"}:
        _fail("confirmation_kind 必须为 human 或 assistant_review")
    human = confirmation_kind == "human"
    try:
        timestamp = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timezone required")
    except (ValueError, AttributeError, TypeError):
        _fail("确认时间必须是含时区的 ISO 时间")
    selected = preview_selections(draft, selection_overrides)
    draft_id = draft["draft_id"]
    inputs = draft["inputs"]
    beat_ids = {beat["beat_id"] for beat in draft["beats"]}
    if (not isinstance(approved_text_by_beat, dict) or set(approved_text_by_beat) != beat_ids
            or any(not isinstance(text, str) or not text.strip() for text in approved_text_by_beat.values())):
        _fail("必须逐段确认三句非空旁白")
    reviews, approvals, texts, selection_edits = {}, {}, {}, []
    for beat in draft["beats"]:
        bid = beat["beat_id"]
        texts[bid] = approved_text_by_beat[bid].strip()
        review = narration_review(texts[bid], beat["duration_budget_us"])
        if review["warnings"]:
            _fail(f"{bid} 旁白需修改：" + "；".join(item["message"] for item in review["warnings"]))
        approvals[bid] = {"draft_id": draft_id, "reviewer": reviewer.strip(), "reviewed_at": reviewed_at,
                          "text_hash": content_hash(texts[bid]),
                          "source": "explicit_user_confirmation" if human else "assistant_review",
                          "confirmation_kind": confirmation_kind, "human_review_performed": human}
        chosen_ids = selected[bid]["selected_candidate_ids"]
        chosen = set(chosen_ids)
        if chosen_ids != beat["selected_candidate_ids"]:
            selection_edits.append({"beat_id": bid, "before": list(beat["selected_candidate_ids"]),
                                    "after": list(chosen_ids), "reviewer": reviewer.strip(), "reviewed_at": reviewed_at,
                                    "confirmation_kind": confirmation_kind, "human_review_performed": human,
                                    "source": "human_edit" if human else "assistant_edit",
                                    "original_model_call_hash": content_hash(beat["selection"])})
        for candidate in beat["candidates"]:
            if candidate["candidate_id"] in chosen:
                review = {key: copy.deepcopy(candidate[key]) for key in
                          ("candidate_id", "asset_id", "asset_revision", "segment_id", "source_range_us")}
                review.update(beat_id=bid, reviewer=reviewer.strip(), reviewed_at=reviewed_at,
                              source="human_montage_review" if human else "assistant_montage_review", draft_id=draft_id,
                              confirmation_kind=confirmation_kind, human_review_performed=human,
                              note=("用户确认主题画面与旁白" if human else "助手核对画面与草案的自动验收，非真人审阅") + "；不把画面作为用户具体经历证据")
                reviews[candidate["candidate_id"]] = review
    result = plan(inputs["Brief"], inputs["AssetManifest"], inputs["CandidateSet"],
                  retrieval_request=inputs["RetrievalRequest"], selector=_ConfirmedModelSelector(
                      draft, selected, source="human_edit" if human else "assistant_edit"),
                  approved_cues=texts, approval_records=approvals, reviewed_evidence=reviews,
                  plan_document_id=f"story-{draft_id}", plan_revision=plan_revision)
    if selection_edits:
        # Human changes must not rewrite what the model actually said or its call identity.
        original_calls = copy.deepcopy(draft["model_calls"])
        result.run["raw_responses"] = original_calls
        provenance = result.story_plan["decision_provenance"]
        provenance["model_call_ids"] = [content_hash(call) for call in original_calls]
        provenance["raw_response_count"] = len(original_calls)
        provenance["selection_edits"] = copy.deepcopy(selection_edits)
        edit_key = "human_edits" if human else "assistant_edits"
        descriptions = [f"{edit['beat_id']}: 确认者替换或重排主题镜头，保留模型原始选择" for edit in selection_edits]
        provenance[edit_key] = [*provenance[edit_key], *descriptions]
        result.run[edit_key] = list(provenance[edit_key])
        result.run["selection_edits"] = copy.deepcopy(selection_edits)
    report = validate_story_plan(result.story_plan, brief=inputs["Brief"], manifest=inputs["AssetManifest"],
                                 candidate_set=inputs["CandidateSet"], upstream=inputs)
    assert_valid(report)
    if result.story_plan["status"] != "ready_for_edit":
        _fail("确认后的故事仍有未解决缺口")
    result.run.update(validation=report, draft_id=draft_id, approval_records=approvals,
                      actual_backend="ollama", inference_executed_during_confirmation=False,
                      confirmation_kind=confirmation_kind, human_review_performed=human)
    if "model_attempts" in draft:
        result.run["model_attempts"] = copy.deepcopy(draft["model_attempts"])
        result.story_plan["decision_provenance"]["actual_model_attempt_count"] = len(draft["model_attempts"])
    result.story_plan["decision_provenance"].update(confirmation_kind=confirmation_kind, human_review_performed=human)
    return result

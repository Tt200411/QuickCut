"""校验 StoryPlan 的精确依赖、逐段候选、证据和待审状态。"""
from __future__ import annotations

from typing import Any

from .contracts import (
    PlanningError, VLM_INVALID_PLAN, check_envelope, check_inputs, check_project,
)
from .planning import build_outline, build_retrieval_request, candidates_for_beat, evidence_review


def validate_story_plan(
    plan: dict[str, Any], *, brief: dict[str, Any], candidate_set: dict[str, Any],
    manifest: dict[str, Any], upstream: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a rejection report for malformed JSON as well as semantic errors."""
    try:
        return _validate_story_plan(
            plan, brief=brief, candidate_set=candidate_set, manifest=manifest, upstream=upstream,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "status": plan.get("status") if isinstance(plan, dict) else None,
            "errors": [f"文档结构不合法：{exc}"],
            "warnings": [], "checks": [], "counts": {},
        }


def _validate_story_plan(
    plan: dict[str, Any], *, brief: dict[str, Any], candidate_set: dict[str, Any],
    manifest: dict[str, Any], upstream: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []
    selected_user_segments: set[str] = set()
    external_duration = 0
    nonuser_duration = 0
    beats = plan.get("beats", [])
    cues = plan.get("narration_cues", [])
    gaps = plan.get("unresolved_gaps", [])
    pending = plan.get("pending_reviews", [])

    def positive(value: Any, label: str) -> int:
        if type(value) is not int or value <= 0:
            errors.append(f"{label} 必须是正整数微秒")
            return 0
        return value

    try:
        for doc, kind, owner in ((plan, "StoryPlan", "C"), (brief, "Brief", "C"),
                                  (manifest, "AssetManifest", "A"), (candidate_set, "CandidateSet", "B")):
            check_envelope(doc, expected_kind=kind, owner=owner)
        available = {**(upstream or {}), "Brief": brief, "AssetManifest": manifest, "CandidateSet": candidate_set}
        outline = build_outline(brief)
        request = available.get("RetrievalRequest")
        if request is None:
            request = build_retrieval_request(brief, manifest, outline)
            warnings.append("未提供 RetrievalRequest，按默认检索规则核对逐段候选")
        check_envelope(request, expected_kind="RetrievalRequest", owner="C")
        check_project(plan, brief, manifest, candidate_set, request)
        check_inputs(request, available)
        if any(q.get("beat_id") not in {b["beat_id"] for b in outline} for q in request.get("queries", [])):
            raise PlanningError(VLM_INVALID_PLAN, "检索查询引用了当前大纲之外的 beat", retryable=False)
        check_inputs(candidate_set, {**available, "RetrievalRequest": request})
        check_inputs(plan, available)
        fixtures_only = any(d["fixtures_only"] for d in (brief, manifest, candidate_set, request))
        if fixtures_only and not plan["fixtures_only"]:
            errors.append("上游 fixtures_only 必须传播到 StoryPlan")
        candidate_views = {b["beat_id"]: candidates_for_beat(b, request, candidate_set, manifest) for b in outline}
        checks.append("信封、精确依赖和候选源区间检查通过")
    except (PlanningError, KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
        return {"ok": False, "status": plan.get("status"), "errors": errors, "warnings": warnings,
                "checks": checks, "counts": {"beats": len(beats), "cues": len(cues), "gaps": len(gaps)}}

    target = positive(brief["requirements"].get("target_duration_us"), "Brief.target_duration_us")
    outline_by_id = {b["beat_id"]: b for b in outline}
    beat_ids = [b.get("beat_id") for b in beats]
    if not beats or len(set(beat_ids)) != len(beat_ids):
        errors.append("beat_id 必须非空且唯一")
    if set(beat_ids) != set(outline_by_id):
        errors.append("故事段落与 Brief 大纲不一致")
    if [b.get("order") for b in beats] != list(range(len(beats))) or any(type(b.get("order")) is not int for b in beats):
        errors.append("段落 order 必须从 0 连续递增")
    budget_sum = 0
    shot_ids: set[str] = set()
    candidate_ids: set[str] = set()
    external_seen: set[str] = set()
    users_by_beat: dict[str, set[str]] = {}
    budgets_by_beat: dict[str, int] = {}
    beats_by_id = {b.get("beat_id"): b for b in beats}
    provenance = plan.get("decision_provenance") or {}
    reviews = provenance.get("evidence_reviews", [])

    for beat in beats:
        beat_id = beat.get("beat_id")
        users_by_beat[beat_id] = set()
        if not beat.get("purpose"):
            errors.append(f"{beat_id} 缺少叙事目的")
        budget = positive(beat.get("duration_budget_us"), f"{beat_id}.duration_budget_us")
        budget_sum += budget
        budgets_by_beat[beat_id] = budget
        support = beat.get("support")
        if support not in ("supported", "partially_supported", "unsupported"):
            errors.append(f"{beat_id}.support 必须是 supported / partially_supported / unsupported")
        allowed = {c["candidate_id"]: c for c in candidate_views.get(beat_id, [])}
        shot_sum = 0
        nonuser_beat_duration = 0
        for shot in beat.get("shots", []):
            cid = shot.get("candidate_id")
            if cid in candidate_ids:
                errors.append(f"重复镜头 candidate_id：{cid}")
            candidate_ids.add(cid)
            if "shot_id" in shot:
                sid = shot["shot_id"]
                if not isinstance(sid, str) or not sid.strip() or sid in shot_ids:
                    errors.append("shot_id 必须非空且唯一")
                shot_ids.add(sid)
            suggested = positive(shot.get("suggested_duration_us"), f"{cid}.suggested_duration_us")
            shot_sum += suggested
            candidate = allowed.get(cid)
            if candidate is None:
                errors.append(f"{beat_id} 选中了本段候选白名单之外的 ID：{cid}")
                continue
            if shot.get("segment_id") != candidate["segment_id"] or shot.get("origin") != candidate["origin"]:
                errors.append(f"{cid} 的 segment_id / origin 与素材不一致")
            if suggested > candidate["duration_us"]:
                errors.append(f"{cid} 建议时长超出素材区间")
            if shot.get("role") not in {"primary", "establishing", "transition", "atmosphere", "detail", "broll"}:
                errors.append(f"{cid} 镜头角色不合法")
            origin = candidate["origin"]
            if origin != "user":
                nonuser_beat_duration += suggested
                nonuser_duration += suggested
                if shot.get("role") == "primary":
                    errors.append(f"{cid} 非用户素材不能充当事件主镜头")
                if shot.get("event_review") is not None:
                    errors.append(f"{cid} 非用户素材不能携带用户事件审阅证据")
            if origin == "external":
                if candidate["segment_id"] in external_seen:
                    errors.append("外部空镜重复出现")
                external_seen.add(candidate["segment_id"])
                external_duration += suggested
            if origin != "user" and (not outline_by_id.get(beat_id, {}).get("broll_allowed") or not brief["requirements"].get("broll_policy", {}).get("enabled")):
                errors.append(f"{beat_id} 不允许空镜")
            if origin == "user":
                record = shot.get("event_review")
                review = evidence_review(candidate, outline_by_id[beat_id], {cid: record}, fixtures_only=fixtures_only)
                if not record or record != review or record not in reviews:
                    errors.append(f"{cid} 缺少绑定当前事件的有效审阅 provenance")
                else:
                    users_by_beat[beat_id].add(candidate["segment_id"])
                    selected_user_segments.add(candidate["segment_id"])
            for key in ("evidence", "transcript", "summary"):
                if shot.get(key) != candidate[key]:
                    errors.append(f"{cid} 的 {key} 与真实素材证据不一致")
        if shot_sum > budget:
            errors.append(f"{beat_id} 镜头建议时长超过段落预算")
        if not beat.get("shots") and not any(g.get("beat_id") == beat_id for g in gaps):
            errors.append(f"{beat_id} 没有镜头也没有登记缺口")
        if support == "supported" and not users_by_beat[beat_id]:
            errors.append(f"{beat_id} 声称 supported 却没有已核实用户证据")
        if (support in ("partially_supported", "unsupported") or not users_by_beat[beat_id]) and not any(
            g.get("beat_id") == beat_id for g in gaps
        ):
            errors.append(f"{beat_id} 事件证据不足，必须登记 unresolved gap")
        if nonuser_beat_duration * 5 > budget * 2:
            errors.append(f"{beat_id} 非用户空镜总时长超过段落预算 40%")
    if budget_sum > target:
        errors.append("段落预算之和超过 Brief 时长")
    if nonuser_duration * 5 > target * 2:
        errors.append("非用户空镜总时长超过成片预算 40%")

    cue_by_id = {c.get("cue_id"): c for c in cues}
    if len(cue_by_id) != len(cues) or any(not isinstance(cid, str) or not cid.strip() for cid in cue_by_id):
        errors.append("cue_id 必须非空且唯一")
    if (brief["requirements"].get("narration") or {}).get("enabled", True) is False and (cues or any(b.get("cue_ids") for b in beats)):
        errors.append("禁用旁白时不得生成 cues 或 cue_ids")
    cue_budgets_by_beat: dict[str, int] = {}
    for cue in cues:
        cid, bid = cue.get("cue_id"), cue.get("beat_id")
        owner = beats_by_id.get(bid) if isinstance(bid, str) else None
        owners = [b for b in beats if cid in b.get("cue_ids", [])]
        if owner is None or len(owners) != 1 or owners[0].get("beat_id") != bid:
            errors.append(f"{cid} 与段落 cue_ids 双向归属不一致")
        if not isinstance(cue.get("text"), str) or not cue["text"].strip():
            errors.append(f"{cid} 缺少文本")
        cue_budget = positive(cue.get("duration_budget_us"), f"{cid}.duration_budget_us")
        if owner is not None:
            cue_budgets_by_beat[bid] = cue_budgets_by_beat.get(bid, 0) + cue_budget
            if cue_budget > budgets_by_beat[bid]:
                errors.append(f"{cid} 旁白预算超过所属段落预算")
        evidence = cue.get("evidence_segment_ids", [])
        if set(evidence) - (users_by_beat[bid] if owner is not None else set()):
            errors.append(f"{cid} 必须引用同 beat 已选且已核实的用户证据")
        if type(cue.get("asserts_specific_user_event")) is not bool:
            errors.append(f"{cid} 必须显式声明是否叙述具体用户事件")
        if cue.get("asserts_specific_user_event") and not evidence:
            errors.append(f"{cid} 具体事件必须有非空用户证据")
        if owner is not None and owner.get("support") in ("partially_supported", "unsupported") and (
            evidence or cue.get("asserts_specific_user_event")
        ):
            errors.append(f"{cid} 事件证据不足的段落不得通过旁白声称用户事件证据")
        if cue.get("approved") is not True:
            if plan.get("status") == "ready_for_edit":
                errors.append(f"{cid} 未批准，不能 ready_for_edit")
            if not any(p.get("type") == "narration" and p.get("cue_id") == cid and p.get("beat_id") == bid for p in pending):
                errors.append(f"{cid} 未批准文案必须登记 pending_reviews")
    for beat in beats:
        bid = beat.get("beat_id")
        if cue_budgets_by_beat.get(bid, 0) > budgets_by_beat[bid]:
            errors.append(f"{bid} 旁白累计预算超过段落预算")
        ids = beat.get("cue_ids", [])
        if len(set(ids)) != len(ids):
            errors.append("段落 cue_ids 重复")
        for cid in ids:
            if cid not in cue_by_id or cue_by_id[cid].get("beat_id") != beat.get("beat_id"):
                errors.append(f"{beat.get('beat_id')} 引用的 cue 归属不一致")
    expected_status = "needs_input" if gaps or pending else "ready_for_edit"
    if plan.get("status") != expected_status:
        errors.append(f"缺口和待审事项要求 status={expected_status}")
    for gap in gaps:
        if not gap.get("suggestions"):
            errors.append(f"{gap.get('gap_id')} 没有解决建议")
    if not provenance.get("mode"):
        errors.append("decision_provenance 缺少 mode")
    if str(provenance.get("mode", "")).startswith("model:") and not provenance.get("model_call_ids"):
        errors.append("模型输出没有记录调用")
    return {
        "ok": not errors, "status": plan.get("status"), "errors": errors,
        "warnings": warnings, "checks": checks,
        "counts": {"beats": len(beats), "cues": len(cues), "gaps": len(gaps),
                   "pending_reviews": len(pending), "selected_user_segments": len(selected_user_segments),
                   "external_duration_us": external_duration, "nonuser_duration_us": nonuser_duration},
    }


def assert_valid(report: dict[str, Any]) -> None:
    if not report["ok"]:
        raise PlanningError(VLM_INVALID_PLAN, "StoryPlan 校验未通过：" + "；".join(report["errors"][:3]),
                            retryable=False, detail={"errors": report["errors"]})

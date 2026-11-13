"""C：故事与视听规划。

Brief + AssetManifest + CandidateSet -> RetrievalRequest / StoryPlan。
只生产 C 负责的两份文档；成片时序归 D，声音事实归 E。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .contracts import (
    NO_CANDIDATE,
    STALE_INPUT_REVISION,
    UNSUPPORTED_STORY_BEAT,
    VLM_INVALID_PLAN,
    ORIGINS,
    PlanningError,
    check_envelope,
    check_inputs,
    check_project,
    content_hash,
    envelope,
    ref,
    require_us,
    split_budget,
)
from .selection import RuleSelector, Selector, validate_selection

BEAT_SUFFIXES = ("open", "middle", "close")
# 2:3:1 对应黄金样例的 6/9/3 秒，且对任意总时长都只用整数微秒运算。
BUDGET_WEIGHTS = [2.0, 3.0, 1.0]
SPLIT_RE = re.compile(r"[，,。；;、]|然后|接着|最后|再|到|并")


@dataclass
class PlanResult:
    outline: list[dict[str, Any]]
    retrieval_request: dict[str, Any]
    story_plan: dict[str, Any]
    alignment: dict[str, Any]
    run: dict[str, Any] = field(default_factory=dict)


def _events(story: str) -> list[str]:
    pieces = [p.strip() for p in SPLIT_RE.split(story or "") if p.strip()]
    return pieces or ["建立环境", "推进过程", "安静收束"]


def build_outline(brief: dict[str, Any]) -> list[dict[str, Any]]:
    """把用户愿望拆成三段初始结构，愿望与已证实事实分开保存。"""
    check_envelope(brief, expected_kind="Brief", owner="C")
    requirements = brief.get("requirements", {})
    total = require_us(requirements.get("target_duration_us"), "Brief.target_duration_us")
    events = _events(requirements.get("story", ""))
    budgets = split_budget(total, BUDGET_WEIGHTS)
    if any(budget <= 0 for budget in budgets):
        raise PlanningError(VLM_INVALID_PLAN, "三段预算必须均为正整数微秒", retryable=False)
    roles = set((requirements.get("broll_policy") or {}).get("roles", []))
    narration = requirements.get("narration") or {}
    style = narration.get("style", "中性旁白")

    purposes = ["建立出发情境", "推进过程与环境过渡", "完成情绪收束"]
    needs = [
        ["primary"],
        ["primary"] + (["establishing"] if "establishing" in roles or "transition" in roles else []),
        ["primary"],
    ]
    outline: list[dict[str, Any]] = []
    for idx, suffix in enumerate(BEAT_SUFFIXES):
        if len(events) == 1:
            event = events[0] if idx == 1 else f"{events[0]}（{purposes[idx]}）"
        elif len(events) == 2:
            event = events[0] if idx == 0 else events[1]
        else:
            event = events[0] if idx == 0 else (events[-1] if idx == 2 else "、".join(events[1:-1]))
        outline.append({
            "beat_id": f"beat-{suffix}",
            "order": idx,
            "purpose": purposes[idx],
            # 这是用户想讲什么，不是素材里已经拍到的事实。
            "wish": {"event": event, "assertion": "user_wish", "verified": False},
            "shot_needs": needs[idx],
            "broll_allowed": idx == 1 and bool(roles) and bool(
                (requirements.get("broll_policy") or {}).get("enabled")
            ),
            "duration_budget_us": budgets[idx],
            "narration_style": style,
            "project_id": brief["project_id"],
        })
    return outline


def build_retrieval_request(
    brief: dict[str, Any],
    manifest: dict[str, Any],
    outline: list[dict[str, Any]],
    *,
    broll_queries: bool = True,
    document_id: str = "request-001",
    revision: int = 1,
    iteration: int = 1,
    max_iterations: int = 3,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    check_envelope(brief, expected_kind="Brief", owner="C")
    check_envelope(manifest, expected_kind="AssetManifest", owner="A")
    project_id = check_project(brief, manifest)
    broll_policy = (brief.get("requirements") or {}).get("broll_policy") or {}
    roles = [r for r in broll_policy.get("roles", []) if r in {"establishing", "transition", "atmosphere", "detail"}]
    queries: list[dict[str, Any]] = []
    for beat in outline:
        suffix = beat["beat_id"].removeprefix("beat-")
        queries.append({
            "query_id": f"query-{suffix}",
            "beat_id": beat["beat_id"],
            "semantic_text": beat["wish"]["event"],
            "role": "primary",
            "allowed_origins": ["user"],
            "duration_budget_us": beat["duration_budget_us"],
            "top_k": 4,
            "iteration": iteration,
            "beat_purpose": beat["purpose"],
        })
    if broll_queries and broll_policy.get("enabled") and roles:
        for suffix, role in (("middle", "establishing"), ("close", "atmosphere")):
            if role not in roles:
                continue
            beat = next(b for b in outline if b["beat_id"] == f"beat-{suffix}")
            queries.append({
                "query_id": f"query-{suffix}-{role}",
                "beat_id": beat["beat_id"],
                "semantic_text": f"用于{beat['purpose']}的环境或情绪画面",
                "role": role,
                "allowed_origins": ["external"],
                "duration_budget_us": min(beat["duration_budget_us"], 3_000_000),
                "top_k": 3,
                "iteration": iteration,
                "beat_purpose": beat["purpose"],
            })
    doc = envelope(
        "RetrievalRequest", document_id, project_id, revision,
        fixtures_only=any(d["fixtures_only"] for d in (brief, manifest)), owner="C",
        inputs=[ref(brief), ref(manifest)],
    )
    doc.update({
        "queries": queries,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "iteration_notes": list(notes or ["首轮按三段叙事目的发出检索请求"]),
        "status": "pending",
    })
    return doc


def next_retrieval_request(
    request: dict[str, Any], gaps: list[dict[str, Any]], *, reason: str
) -> dict[str, Any] | None:
    """同一缺口的下一轮检索请求。到达上限就返回 None，不再无限放宽。"""
    iteration = int(request["iteration"])
    if iteration >= int(request["max_iterations"]):
        return None
    updated = dict(request)
    updated["iteration"] = iteration + 1
    updated["revision"] = int(request["revision"]) + 1
    updated["iteration_notes"] = list(request.get("iteration_notes", [])) + [
        f"第 {iteration + 1} 轮：{reason}"
    ]
    gap_beats = {g["beat_id"] for g in gaps}
    queries = []
    for query in request["queries"]:
        q = dict(query)
        q["iteration"] = iteration + 1
        if query["beat_id"] in gap_beats and query["role"] == "primary":
            q["top_k"] = min(int(query["top_k"]) + 2, 8)
            q["relaxed_constraints"] = ["放宽非关键风格条件，仍不允许外部空镜证明用户事件"]
        queries.append(q)
    updated["queries"] = queries
    updated["status"] = "pending"
    return updated


def _index_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    assets = {a["asset_id"]: a for a in manifest.get("assets", [])}
    segments = {s["segment_id"]: s for s in manifest.get("segments", [])}
    if len(assets) != len(manifest.get("assets", [])) or len(segments) != len(manifest.get("segments", [])):
        raise PlanningError(VLM_INVALID_PLAN, "素材或片段 ID 重复", retryable=False)
    for segment in segments.values():
        asset = assets.get(segment["asset_id"])
        if asset is None:
            raise PlanningError(NO_CANDIDATE, "片段引用不存在的素材", retryable=False)
        if (type(segment.get("asset_revision")) is not int
                or type(asset.get("revision")) is not int
                or asset["revision"] < 1
                or segment["asset_revision"] != asset["revision"]):
            raise PlanningError(
                STALE_INPUT_REVISION, "片段 asset_revision 与素材当前版本不一致",
                project_id=manifest["project_id"], affected_ids=[segment["segment_id"]],
                action_owner="A", retryable=False,
            )
        source = segment["source_range_us"]
        begin = require_us(source.get("begin"), "source_range_us.begin")
        end = require_us(source.get("end"), "source_range_us.end")
        if begin >= end or ("duration_us" in asset and end > require_us(asset["duration_us"], "asset.duration_us")):
            raise PlanningError(VLM_INVALID_PLAN, "素材源区间必须满足 begin < end <= duration_us", retryable=False)
    return assets, segments


def candidates_for_beat(
    beat: dict[str, Any],
    request: dict[str, Any],
    candidate_set: dict[str, Any],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """把 B 的候选与 A 的素材事实拼成 C 可判断的证据视图。"""
    assets, segments = _index_manifest(manifest)
    results = {r["query_id"]: r for r in candidate_set.get("results", [])}
    query_ids = [q["query_id"] for q in request["queries"]]
    candidate_ids = [c["candidate_id"] for r in candidate_set.get("results", []) for c in r.get("candidates", [])]
    result_ids = [r["query_id"] for r in candidate_set.get("results", [])]
    if (len(set(query_ids)) != len(query_ids) or len(set(result_ids)) != len(result_ids)
            or set(result_ids) != set(query_ids) or len(set(candidate_ids)) != len(candidate_ids)):
        raise PlanningError(VLM_INVALID_PLAN, "检索结果必须逐一对应查询且候选 ID 唯一", retryable=False)
    collected: dict[str, dict[str, Any]] = {}
    for query in request["queries"]:
        if query["beat_id"] != beat["beat_id"]:
            continue
        result = results.get(query["query_id"])
        if not result:
            continue
        for candidate in result.get("candidates", []):
            segment = segments.get(candidate["segment_id"])
            if segment is None:
                raise PlanningError(
                    NO_CANDIDATE,
                    f"候选 {candidate['candidate_id']} 引用了不存在的片段 {candidate['segment_id']}",
                    project_id=manifest["project_id"],
                    affected_ids=[candidate["candidate_id"]],
                    action_owner="B",
                )
            asset = assets.get(segment["asset_id"])
            if asset is None:
                raise PlanningError(
                    NO_CANDIDATE,
                    f"片段 {segment['segment_id']} 引用了不存在的素材 {segment['asset_id']}",
                    project_id=manifest["project_id"],
                    affected_ids=[segment["segment_id"]],
                    action_owner="A",
                )
            if (type(candidate.get("asset_revision")) is not int
                    or type(asset.get("revision")) is not int or asset["revision"] < 1
                    or candidate.get("asset_revision") != asset.get("revision")):
                raise PlanningError(
                    STALE_INPUT_REVISION,
                    f"候选 {candidate['candidate_id']} 的 asset_revision 与素材清单不一致",
                    project_id=manifest["project_id"],
                    affected_ids=[candidate["candidate_id"]],
                    action_owner="B",
                    retryable=False,
                )
            source_range = segment["source_range_us"]
            begin = require_us(source_range.get("begin"), "source_range_us.begin")
            end = require_us(source_range.get("end"), "source_range_us.end")
            if begin >= end or ("duration_us" in asset and end > require_us(asset["duration_us"], "asset.duration_us")):
                raise PlanningError(VLM_INVALID_PLAN, "素材源区间必须满足 begin < end <= duration_us", retryable=False)
            if asset.get("origin") not in ORIGINS or asset["origin"] not in query.get("allowed_origins", []):
                raise PlanningError(VLM_INVALID_PLAN, "候选违反 allowed_origins 来源限制", retryable=False)
            if any("bad_evidence" in item.get("flags", []) for item in (candidate, segment, asset)):
                continue
            collected[candidate["candidate_id"]] = {
                "candidate_id": candidate["candidate_id"],
                "segment_id": candidate["segment_id"],
                "asset_id": segment["asset_id"],
                "origin": asset["origin"],
                "query_id": query["query_id"],
                "query_role": query["role"],
                "suggested_role": candidate.get("suggested_role", query["role"]),
                "ranking_score": candidate.get("ranking_score", 0.0),
                "score_profile": candidate.get("score_profile", ""),
                "flags": list(candidate.get("flags", [])),
                "source_range_us": source_range,
                "duration_us": end - begin,
                "asset_revision": asset["revision"],
                "evidence": candidate.get("evidence", []),
                "summary": segment.get("description") or segment.get("summary") or "",
                "transcript": (segment.get("transcript") or {}).get("text", ""),
                "keyframes": segment.get("keyframes", []),
                "keyframe_uris": [k["uri"] for k in segment.get("keyframes", [])],
            }
    return sorted(collected.values(), key=lambda c: c["ranking_score"], reverse=True)


def evidence_review(
    candidate: dict[str, Any], beat: dict[str, Any], reviewed_evidence: dict[str, Any], *, fixtures_only: bool,
) -> dict[str, Any] | None:
    """审阅精确绑定候选、素材版本、段落和具体目标；夹具来源单独标记。"""
    review = reviewed_evidence.get(candidate["candidate_id"])
    if isinstance(review, dict) and (
        review.get("beat_id") == beat["beat_id"]
        and review.get("event") == beat["wish"]["event"]
        and type(review.get("asset_revision")) is int
        and review["asset_revision"] == candidate["asset_revision"]
        and review.get("asset_id") == candidate["asset_id"]
        and review.get("segment_id") == candidate["segment_id"]
        and isinstance(review.get("source_range_us"), dict)
        and all(type(review["source_range_us"].get(key)) is int for key in ("begin", "end"))
        and review["source_range_us"] == candidate["source_range_us"]
        and all(isinstance(review.get(key), str) and review[key].strip() for key in ("reviewer", "note"))
    ):
        return {**review, "candidate_id": candidate["candidate_id"], "source": "human_review"}
    if fixtures_only and isinstance(candidate.get("evidence"), list) and "fixture_annotation" in candidate["evidence"]:
        return {
            "candidate_id": candidate["candidate_id"], "beat_id": beat["beat_id"],
            "asset_revision": candidate["asset_revision"], "event": beat["wish"]["event"],
            "source": "fixture_annotation",
        }
    return None


def classify_beat(beat: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """二次对齐的三态判断：可支持 / 部分支持 / 无素材支撑。

    只有外部空镜不算"部分支撑事件"，它只能支撑氛围段，因此单列 broll_only，
    让上层明确改写表达并登记缺口，而不是把空镜塞进主镜头位置。
    """
    usable_user = [c for c in candidates if c["origin"] == "user" and c.get("event_review") and "bad_evidence" not in c["flags"]]
    flagged_user = [c for c in candidates if c["origin"] == "user" and c not in usable_user]
    external = [c for c in candidates if c["origin"] == "external" and "bad_evidence" not in c["flags"]]
    common = {
        "beat_id": beat["beat_id"],
        "primary_candidate_ids": [],
        "external_candidate_ids": [c["candidate_id"] for c in external],
        "broll_only": False,
    }
    if usable_user:
        return {
            **common,
            "support": "supported",
            "reason": "存在可作为事件证据的用户主镜头",
            "primary_candidate_ids": [c["candidate_id"] for c in usable_user],
            "expression_rewrite": None,
        }
    if flagged_user:
        return {
            **common,
            "support": "partially_supported",
            "reason": "用户候选尚无与当前事件及素材版本匹配的合格审阅",
            "expression_rewrite": "暂缓叙述具体动作，等待证据复核",
        }
    if external:
        return {
            **common,
            "support": "partially_supported",
            "broll_only": True,
            "reason": "只有外部空镜，可承担环境或情绪，不能证明具体人物行为",
            "expression_rewrite": "把该段改为氛围描述，不叙述用户未拍到的具体事件",
        }
    return {
        **common,
        "support": "unsupported",
        "reason": "该段请求没有返回任何可用候选",
        "expression_rewrite": None,
    }


def _cue_text(beat: dict[str, Any], approved: dict[str, str] | None) -> tuple[str, str]:
    if approved and beat["beat_id"] in approved:
        return approved[beat["beat_id"]], "human_editorial"
    event = beat["wish"]["event"]
    return f"{event}。", "template_draft"


def _gap(beat: dict[str, Any], support: dict[str, Any], iteration: int, max_iterations: int) -> dict[str, Any]:
    exhausted = iteration >= max_iterations
    return {
        "gap_id": f"gap-{beat['beat_id']}",
        "beat_id": beat["beat_id"],
        "type": UNSUPPORTED_STORY_BEAT if support["support"] == "unsupported" else "PARTIAL_EVENT_EVIDENCE",
        "missing": beat["wish"]["event"],
        "reason": support["reason"],
        "iteration": iteration,
        "status": "exhausted" if exhausted else "open",
        "suggestions": [
            "补充能证明该事件的用户素材",
            "改写为主观感受或氛围表达，不虚构具体动作",
            "缩减三段结构，把预算让给有证据的段落",
        ],
        "must_not_assert_user_events": True,
    }


def plan(
    brief: dict[str, Any],
    manifest: dict[str, Any],
    candidate_set: dict[str, Any] | None = None,
    *,
    retrieval_request: dict[str, Any] | None = None,
    selector: Selector | None = None,
    approved_cues: dict[str, str] | None = None,
    reviewed_evidence: dict[str, Any] | None = None,
    broll_queries: bool = True,
    max_iterations: int = 3,
    plan_document_id: str = "story-001",
    plan_revision: int = 1,
    iteration_history: list[dict[str, Any]] | None = None,
) -> PlanResult:
    """执行一次规划。candidate_set 为空时只产出大纲与首轮检索请求。"""
    check_envelope(brief, expected_kind="Brief", owner="C")
    check_envelope(manifest, expected_kind="AssetManifest", owner="A")
    project_id = check_project(brief, manifest)
    if candidate_set is not None:
        check_envelope(candidate_set, expected_kind="CandidateSet", owner="B")
        check_project(brief, candidate_set)
    outline = build_outline(brief)
    request = retrieval_request if retrieval_request is not None else build_retrieval_request(
        brief, manifest, outline, broll_queries=broll_queries, max_iterations=max_iterations,
    )
    check_envelope(request, expected_kind="RetrievalRequest", owner="C")
    check_project(brief, manifest, request)
    check_inputs(request, {"Brief": brief, "AssetManifest": manifest})
    beat_ids = {beat["beat_id"] for beat in outline}
    if any(query.get("beat_id") not in beat_ids for query in request.get("queries", [])):
        raise PlanningError(VLM_INVALID_PLAN, "检索查询引用了当前大纲之外的 beat", retryable=False)
    fixtures_only = any(d["fixtures_only"] for d in (brief, manifest, request, *([candidate_set] if candidate_set is not None else [])))
    request = {**request, "fixtures_only": fixtures_only}
    if reviewed_evidence is not None and not isinstance(reviewed_evidence, dict):
        raise PlanningError(VLM_INVALID_PLAN, "reviewed_evidence 必须是候选 ID 到审阅记录的映射", retryable=False)
    if approved_cues is not None and (not isinstance(approved_cues, dict) or any(
        not isinstance(text, str) or not text.strip() for text in approved_cues.values()
    )):
        raise PlanningError(VLM_INVALID_PLAN, "approved_cues 必须包含非空文案", retryable=False)
    narration_enabled = (brief.get("requirements", {}).get("narration") or {}).get("enabled", True)

    if candidate_set is None:
        empty_plan = envelope(
            "StoryPlan", plan_document_id, project_id, plan_revision,
            fixtures_only=fixtures_only, owner="C",
            inputs=[ref(brief), ref(manifest)],
        )
        empty_plan.update({
            "status": "awaiting_candidates",
            "theme": (brief.get("requirements") or {}).get("story", ""),
            "narration_language": (brief.get("requirements") or {}).get("language", ""),
            "beats": [],
            "narration_cues": [],
            "unresolved_gaps": [],
            "decision_provenance": {"mode": "outline_only"},
        })
        return PlanResult(outline, request, empty_plan, {"stage": "outline_only"})

    check_inputs(candidate_set, {
        "AssetManifest": manifest,
        "RetrievalRequest": request,
    })
    selector = selector or RuleSelector()
    iteration = int(request.get("iteration", 1))
    max_iter = int(request.get("max_iterations", max_iterations))
    beats: list[dict[str, Any]] = []
    cues: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    alignment_rows: list[dict[str, Any]] = []
    raw_responses: list[dict[str, Any]] = []
    rule_adjustments: list[str] = []
    human_edits: list[str] = []
    model_call_ids: list[str] = []
    selected_segments: set[str] = set()
    reviews: list[dict[str, Any]] = []
    pending_reviews: list[dict[str, Any]] = []

    for beat in outline:
        candidates = candidates_for_beat(beat, request, candidate_set, manifest)
        for candidate in candidates:
            if candidate["origin"] != "user":
                continue
            review = evidence_review(candidate, beat, reviewed_evidence or {}, fixtures_only=fixtures_only)
            if review:
                candidate["event_review"] = review
                reviews.append(review)
            else:
                pending_reviews.append({
                    "type": "event_evidence", "candidate_id": candidate["candidate_id"],
                    "beat_id": beat["beat_id"], "event": beat["wish"]["event"],
                    "asset_revision": candidate["asset_revision"],
                })
        support = classify_beat(beat, candidates)
        alignment_rows.append(support)
        if support["support"] == "unsupported":
            gaps.append(_gap(beat, support, iteration, max_iter))
            beats.append({
                "beat_id": beat["beat_id"],
                "order": beat["order"],
                "purpose": beat["purpose"],
                "duration_budget_us": beat["duration_budget_us"],
                "support": support["support"],
                "shots": [],
                "alternative_candidate_ids": [],
                "cue_ids": [],
            })
            continue

        if support["support"] == "partially_supported":
            # 没有合格的用户事件证据时，不调用选镜器硬凑主镜头。空镜最多承担
            # 一个氛围位置，旁白退回非事件性留白，人工批准文案也不在这里套用。
            gaps.append(_gap(beat, support, iteration, max_iter))
            atmosphere_shots: list[dict[str, Any]] = []
            if support["broll_only"] and beat["broll_allowed"]:
                external = sorted(
                    [c for c in candidates if c["origin"] == "external"],
                    key=lambda c: c["ranking_score"], reverse=True,
                )
                if external and int(beat["duration_budget_us"] * 0.4) > 0:
                    best = external[0]
                    role = best["suggested_role"] if best["suggested_role"] in {
                        "establishing", "transition", "atmosphere", "detail", "broll",
                    } else "broll"
                    atmosphere_shots.append({
                        "candidate_id": best["candidate_id"],
                        "segment_id": best["segment_id"],
                        "origin": "external",
                        "role": role,
                        "suggested_duration_us": min(
                            best["duration_us"], int(beat["duration_budget_us"] * 0.4)
                        ),
                        "decided_by": "rule:gap_policy",
                        "evidence": best["evidence"],
                        "transcript": best["transcript"],
                        "summary": best["summary"],
                    })
            cue_id = f"cue-{beat['order'] + 1:02d}"
            if narration_enabled:
                cues.append({
                    "cue_id": cue_id,
                    "beat_id": beat["beat_id"],
                    "text": "画面先安静一会儿。",
                    "text_source": "template_draft",
                    "approved": False,
                    "content_review": "pending_human_review",
                    "duration_budget_us": max(1, beat["duration_budget_us"] - 200_000),
                    "voice_style": beat["narration_style"],
                    "expression": "氛围留白，等待素材补齐或人工改写；不得叙述具体人物动作",
                    "evidence_segment_ids": [],
                    "asserts_specific_user_event": False,
                })
            if approved_cues and beat["beat_id"] in approved_cues:
                human_edits.append(
                    f"{cue_id}: 人工文案未采用，该段没有事件证据，改为非事件性留白"
                )
            rule_adjustments.append(f"{beat['beat_id']}: {support['expression_rewrite']}")
            beats.append({
                "beat_id": beat["beat_id"],
                "order": beat["order"],
                "purpose": beat["purpose"],
                "duration_budget_us": beat["duration_budget_us"],
                "support": support["support"],
                "shots": atmosphere_shots,
                "alternative_candidate_ids": [c["candidate_id"] for c in candidates],
                "cue_ids": [cue_id] if narration_enabled else [],
            })
            continue

        context = {
            "project_id": project_id,
            "broll_enabled": bool(beat["broll_allowed"]) and bool(
                (brief.get("requirements") or {}).get("broll_policy", {}).get("enabled")
            ),
        }
        candidates = [c for c in candidates if c["origin"] != "user" or c["candidate_id"] in support["primary_candidate_ids"]]
        selection = selector.select(beat=beat, candidates=candidates, context=context)
        if selection.raw_response:
            raw_responses.append(selection.to_record())
            model_call_ids.append(content_hash(selection.to_record()))
        try:
            shots, corrections = validate_selection(selection, beat=beat, candidates=candidates)
        except PlanningError as exc:
            # 模型错误不静默替换事件角色：保留原始响应，作为修正请求返回。
            raise PlanningError(
                exc.code, exc.message, project_id=project_id,
                affected_ids=exc.affected_ids, retryable=True, action_owner="C",
                detail={**exc.detail, "beat_id": beat["beat_id"], "support": support},
            ) from exc
        rule_adjustments.extend(f"{beat['beat_id']}: {line}" for line in corrections)

        cue_id = f"cue-{beat['order'] + 1:02d}"
        text, text_source = _cue_text(beat, approved_cues)
        if text_source == "human_editorial":
            human_edits.append(f"{cue_id}: 文案来自人工审阅，不记为模型输出")
        cue_budget = max(1, beat["duration_budget_us"] - 200_000)
        if narration_enabled:
            cues.append({
                "cue_id": cue_id,
                "beat_id": beat["beat_id"],
                "text": text,
                "text_source": text_source,
                "approved": text_source == "human_editorial",
                "content_review": "pending_human_review" if text_source != "human_editorial" else "human_editorial",
                "duration_budget_us": cue_budget,
                "voice_style": beat["narration_style"],
                "expression": "与画面动作、角色和先后顺序对照检查后再批准",
                "evidence_segment_ids": [s["segment_id"] for s in shots if s["origin"] == "user"],
                "asserts_specific_user_event": support["support"] == "supported",
            })
        for shot in shots:
            selected_segments.add(shot["segment_id"])
            candidate = next(c for c in candidates if c["candidate_id"] == shot["candidate_id"])
            shot["evidence"] = candidate["evidence"]
            shot["transcript"] = candidate["transcript"]
            shot["summary"] = candidate["summary"]
            if candidate.get("event_review"):
                shot["event_review"] = candidate["event_review"]
            shot.pop("candidate_duration_us", None)
            shot.pop("reason", None)
        beats.append({
            "beat_id": beat["beat_id"],
            "order": beat["order"],
            "purpose": beat["purpose"],
            "duration_budget_us": beat["duration_budget_us"],
            "support": support["support"],
            "shots": shots,
            "alternative_candidate_ids": [
                c["candidate_id"] for c in candidates
                if c["candidate_id"] not in {s["candidate_id"] for s in shots}
            ],
            "cue_ids": [cue_id] if narration_enabled else [],
        })

    # 只有用户事件证据的句子才能引用选中片段；空镜不进入事实引用。
    for cue in cues:
        cue["evidence_segment_ids"] = [s for s in cue["evidence_segment_ids"] if s in selected_segments]

    for cue in cues:
        if cue.get("approved") is not True:
            pending_reviews.append({"type": "narration", "cue_id": cue["cue_id"], "beat_id": cue["beat_id"]})
    provenance = {
        "mode": selector.name,
        "model_call_ids": model_call_ids,
        "selected_segment_ids": sorted(selected_segments),
        "evidence_reviews": reviews,
        "reviewed_anchor_segment_ids": sorted({
            c["segment_id"] for b in beats for c in b["shots"]
            if c.get("event_review", {}).get("source") == "human_review"
        }),
        "rule_adjustments": rule_adjustments,
        "human_edits": human_edits,
        "iterations": list(iteration_history or []) + [{
            "iteration": iteration,
            "request_revision": request["revision"],
            "candidate_set_revision": candidate_set["revision"],
            "reason": request.get("iteration_notes", [""])[-1],
        }],
        "raw_response_count": len(raw_responses),
        "input_hashes": {
            "Brief": content_hash(ref(brief)),
            "AssetManifest": content_hash(ref(manifest)),
            "CandidateSet": content_hash(ref(candidate_set)),
        },
    }
    status = "needs_input" if gaps or pending_reviews else "ready_for_edit"
    doc = envelope(
        "StoryPlan", plan_document_id, project_id, plan_revision,
        fixtures_only=fixtures_only, owner="C",
        inputs=[ref(brief), ref(candidate_set), ref(manifest)],
    )
    doc.update({
        "status": status,
        "theme": (brief.get("requirements") or {}).get("story", ""),
        "narration_language": (brief.get("requirements") or {}).get("language", ""),
        "beats": beats,
        "narration_cues": cues,
        "unresolved_gaps": gaps,
        "pending_reviews": pending_reviews,
        "decision_provenance": provenance,
    })
    run = {
        "project_id": project_id,
        "stage": "C",
        "revision": plan_revision,
        "selector": selector.name,
        "raw_responses": raw_responses,
        "validation": {"status": status, "gap_count": len(gaps)},
        "human_edits": human_edits,
        "rule_adjustments": rule_adjustments,
    }
    alignment = {
        "iteration": iteration,
        "max_iterations": max_iter,
        "rows": alignment_rows,
        "next_request": (
            next_retrieval_request(request, gaps, reason="按缺口放宽非关键条件后再检索")
            if gaps else None
        ),
    }
    return PlanResult(outline, request, doc, alignment, run)


def compare_plans(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """比较两个故事方案，用于 P0 第 7 条的方案留存。"""
    def shots_of(doc: dict[str, Any]) -> dict[str, list[str]]:
        return {
            b["beat_id"]: [f"{s['candidate_id']}:{s['role']}" for s in b.get("shots", [])]
            for b in doc.get("beats", [])
        }

    def cues_of(doc: dict[str, Any]) -> dict[str, str]:
        return {c["cue_id"]: c["text"] for c in doc.get("narration_cues", [])}

    left_shots, right_shots = shots_of(left), shots_of(right)
    left_cues, right_cues = cues_of(left), cues_of(right)
    return {
        "status": [left.get("status"), right.get("status")],
        "mode": [
            (left.get("decision_provenance") or {}).get("mode"),
            (right.get("decision_provenance") or {}).get("mode"),
        ],
        "changed_beats": [
            beat_id for beat_id in sorted(set(left_shots) | set(right_shots))
            if left_shots.get(beat_id) != right_shots.get(beat_id)
        ],
        "changed_cues": [
            cue_id for cue_id in sorted(set(left_cues) | set(right_cues))
            if left_cues.get(cue_id) != right_cues.get(cue_id)
        ],
        "gap_counts": [len(left.get("unresolved_gaps", [])), len(right.get("unresolved_gaps", []))],
    }

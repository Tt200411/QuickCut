"""StoryPlan 审查回归：缺口不可抹除，旁白和全部非用户空镜受预算约束。"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from quickcut.planning import (
    build_outline, build_retrieval_request, load_bundle, plan, read_json,
    validate_story_plan,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def docs():
    _, data = load_bundle(ROOT / "examples/team_contracts/bundle.json")
    data["CandidateSet"] = read_json(ROOT / "examples/team_contracts/c/candidate_set.json")
    data["RetrievalRequest"] = build_retrieval_request(
        data["Brief"], data["AssetManifest"], build_outline(data["Brief"]),
    )
    return data


def generate(docs):
    return plan(
        docs["Brief"], docs["AssetManifest"], docs["CandidateSet"],
        retrieval_request=docs["RetrievalRequest"],
        approved_cues=read_json(ROOT / "examples/team_contracts/c/approved_cues.json"),
    ).story_plan


def validate(story, docs):
    return validate_story_plan(
        story, brief=docs["Brief"], manifest=docs["AssetManifest"],
        candidate_set=docs["CandidateSet"], upstream=docs,
    )


def register_gap(story, beat):
    story["unresolved_gaps"].append({
        "gap_id": f"gap-{beat['beat_id']}", "beat_id": beat["beat_id"],
        "type": "PARTIAL_EVENT_EVIDENCE", "suggestions": ["补充用户事件素材"],
        "must_not_assert_user_events": True,
    })
    story["status"] = "needs_input"


def assert_rejected(report, message):
    assert report["ok"] is False, report
    assert any(message in error for error in report["errors"]), report["errors"]
    assert report["counts"], "语义错误应返回完整报告，而非退回结构异常"


@pytest.mark.parametrize("support", ["partially_supported", "unsupported"])
def test_disabled_narration_external_only_cannot_drop_gap_and_become_ready(docs, support):
    docs["Brief"]["requirements"]["narration"]["enabled"] = False
    result = next(r for r in docs["CandidateSet"]["results"] if r["query_id"] == "query-middle")
    result["candidates"] = []
    story = generate(docs)
    middle = story["beats"][1]
    assert middle["shots"] and all(s["origin"] == "external" for s in middle["shots"])
    middle["support"] = support
    assert validate(story, docs)["ok"]
    story["unresolved_gaps"] = []
    story["pending_reviews"] = []
    story["status"] = "ready_for_edit"
    assert_rejected(validate(story, docs), "beat-middle 事件证据不足，必须登记 unresolved gap")


@pytest.mark.parametrize("support", [None, "", "partial", "SUPPORTED", "rewritten", True, [], {}])
def test_support_is_a_strict_enum(docs, support):
    story = generate(docs)
    story["beats"][0]["support"] = support
    assert_rejected(validate(story, docs), ".support 必须是")


def test_missing_support_is_rejected(docs):
    story = generate(docs)
    del story["beats"][0]["support"]
    assert_rejected(validate(story, docs), ".support 必须是")


@pytest.mark.parametrize("support", ["partially_supported", "unsupported"])
def test_partial_and_unsupported_still_require_gap_with_verified_user_shots(docs, support):
    story = generate(docs)
    beat = story["beats"][0]
    beat["support"] = support
    story["narration_cues"][0]["evidence_segment_ids"] = []
    story["narration_cues"][0]["asserts_specific_user_event"] = False
    assert_rejected(validate(story, docs), "必须登记 unresolved gap")
    register_gap(story, beat)
    assert validate(story, docs)["ok"]


@pytest.mark.parametrize("support", ["partially_supported", "unsupported"])
def test_insufficient_support_cannot_claim_event_evidence_via_cues(docs, support):
    story = generate(docs)
    beat = story["beats"][0]
    beat["support"] = support
    register_gap(story, beat)
    assert_rejected(validate(story, docs), "不得通过旁白声称用户事件证据")


@pytest.mark.parametrize("mutation", ["supported", "rewrite", "wrong_gap"])
def test_no_valid_user_evidence_requires_own_gap_even_without_narration(docs, mutation):
    docs["Brief"]["requirements"]["narration"]["enabled"] = False
    story = generate(docs)
    middle = story["beats"][1]
    middle["shots"] = [s for s in middle["shots"] if s["origin"] != "user"]
    middle["support"] = "supported" if mutation == "supported" else "partially_supported"
    if mutation == "rewrite":
        middle["expression_rewrite"] = "经人工改为纯氛围表达"
        story["decision_provenance"]["human_edits"].append("同意改写该段")
    if mutation == "wrong_gap":
        register_gap(story, story["beats"][0])
    assert_rejected(validate(story, docs), "beat-middle 事件证据不足，必须登记 unresolved gap")


def test_invalid_user_review_cannot_remove_gap(docs):
    docs["Brief"]["requirements"]["narration"]["enabled"] = False
    story = generate(docs)
    user = next(s for s in story["beats"][1]["shots"] if s["origin"] == "user")
    user["event_review"]["event"] = "另一个事件"
    assert_rejected(validate(story, docs), "beat-middle 事件证据不足，必须登记 unresolved gap")


def test_external_shot_cannot_carry_user_event_review(docs):
    story = generate(docs)
    middle = story["beats"][1]
    user = next(s for s in middle["shots"] if s["origin"] == "user")
    external = next(s for s in middle["shots"] if s["origin"] == "external")
    external["event_review"] = copy.deepcopy(user["event_review"])
    assert_rejected(validate(story, docs), "非用户素材不能携带用户事件审阅证据")


def test_cue_cannot_exceed_its_owner_budget(docs):
    story = generate(docs)
    story["narration_cues"][0]["duration_budget_us"] = story["beats"][0]["duration_budget_us"] + 1
    assert_rejected(validate(story, docs), "旁白预算超过所属段落预算")


@pytest.mark.parametrize("extra", [0, 1])
def test_same_beat_cue_budgets_are_summed(docs, extra):
    story = generate(docs)
    beat = story["beats"][0]
    first = story["narration_cues"][0]
    second = copy.deepcopy(first)
    second["cue_id"] = "cue-open-second"
    first["duration_budget_us"] = beat["duration_budget_us"] // 2
    second["duration_budget_us"] = beat["duration_budget_us"] - first["duration_budget_us"] + extra
    assert all(0 < c["duration_budget_us"] <= beat["duration_budget_us"] for c in (first, second))
    story["narration_cues"].append(second)
    beat["cue_ids"].append(second["cue_id"])
    report = validate(story, docs)
    if extra:
        assert_rejected(report, "beat-open 旁白累计预算超过段落预算")
    else:
        assert report["ok"], report["errors"]


@pytest.mark.parametrize("owner", ["unknown-beat", None, [], {}, True])
def test_invalid_cue_owner_reports_semantic_error_without_crashing(docs, owner):
    story = generate(docs)
    story["narration_cues"][0]["beat_id"] = owner
    assert_rejected(validate(story, docs), "双向归属不一致")


def test_multiple_cue_owners_report_error_without_crashing(docs):
    story = generate(docs)
    story["beats"][1]["cue_ids"].append(story["narration_cues"][0]["cue_id"])
    assert_rejected(validate(story, docs), "双向归属不一致")


def isolate_middle_broll(story):
    """比例边界只计 middle 空镜，隔离默认结尾策略对全局计数的影响。"""
    for beat in story["beats"]:
        if beat["beat_id"] != "beat-middle":
            beat["shots"] = [shot for shot in beat["shots"] if shot["origin"] == "user"]


def add_broll(docs, story, *, origin, duration):
    middle = story["beats"][1]
    template = next(s for s in middle["shots"] if s["origin"] == "external")
    manifest = docs["AssetManifest"]
    segment = copy.deepcopy(next(s for s in manifest["segments"] if s["segment_id"] == template["segment_id"]))
    asset = copy.deepcopy(next(a for a in manifest["assets"] if a["asset_id"] == segment["asset_id"]))
    asset.update(asset_id="asset-review-broll", origin=origin, duration_us=duration)
    segment.update(segment_id="seg-review-broll", asset_id=asset["asset_id"],
                   source_range_us={"begin": 0, "end": duration}, keyframes=[])
    manifest["assets"].append(asset)
    manifest["segments"].append(segment)
    result = next(r for r in docs["CandidateSet"]["results"] if r["query_id"] == "query-middle-establishing")
    candidate = copy.deepcopy(result["candidates"][0])
    candidate.update(candidate_id="candidate-review-broll", segment_id=segment["segment_id"])
    result["candidates"].append(candidate)
    query = next(q for q in docs["RetrievalRequest"]["queries"] if q["query_id"] == result["query_id"])
    if origin not in query["allowed_origins"]:
        query["allowed_origins"].append(origin)
    shot = {**copy.deepcopy(template), "candidate_id": candidate["candidate_id"],
            "segment_id": segment["segment_id"], "origin": origin, "suggested_duration_us": duration}
    shot.pop("shot_id", None)
    middle["shots"].append(shot)
    return shot


@pytest.mark.parametrize("origin", ["external", "generated"])
@pytest.mark.parametrize("extra", [0, 1])
def test_nonuser_broll_exactly_forty_percent_and_one_microsecond_over(docs, origin, extra):
    story = generate(docs)
    isolate_middle_broll(story)
    middle = story["beats"][1]
    external = next(s for s in middle["shots"] if s["origin"] == "external")
    user = next(s for s in middle["shots"] if s["origin"] == "user")
    user["suggested_duration_us"] = 1_000_000
    external["suggested_duration_us"] = 1_800_000
    add_broll(docs, story, origin=origin, duration=1_800_000 + extra)
    report = validate(story, docs)
    assert report["counts"]["external_duration_us"] == (3_600_000 + extra if origin == "external" else 1_800_000)
    assert report["counts"]["nonuser_duration_us"] == 3_600_000 + extra
    if extra:
        assert_rejected(report, "beat-middle 非用户空镜总时长超过段落预算 40%")
    else:
        assert report["ok"], report["errors"]


@pytest.mark.parametrize("origin", ["external", "generated"])
def test_all_nonuser_shots_are_summed_per_beat(docs, origin):
    story = generate(docs)
    middle = story["beats"][1]
    for shot in middle["shots"]:
        shot["suggested_duration_us"] = 2_000_000 if shot["origin"] == "external" else 1_000_000
    add_broll(docs, story, origin=origin, duration=2_000_000)
    assert_rejected(validate(story, docs), "beat-middle 非用户空镜总时长超过段落预算 40%")


@pytest.mark.parametrize("origin", ["external", "generated"])
def test_global_nonuser_budget_includes_generated_and_preserves_external_count(docs, origin):
    story = generate(docs)
    isolate_middle_broll(story)
    middle = story["beats"][1]
    for shot in middle["shots"]:
        shot["suggested_duration_us"] = 3_000_000 if shot["origin"] == "external" else 1_000_000
    add_broll(docs, story, origin=origin, duration=5_000_000)
    report = validate(story, docs)
    assert_rejected(report, "非用户空镜总时长超过成片预算 40%")
    assert report["counts"]["nonuser_duration_us"] == 8_000_000
    assert report["counts"]["external_duration_us"] == (8_000_000 if origin == "external" else 3_000_000)


def test_large_integer_broll_boundary_does_not_round_away_one_microsecond(docs):
    story = generate(docs)
    middle = story["beats"][1]
    unit = 2**54 + 1
    middle["duration_budget_us"] = unit * 5
    docs["Brief"]["requirements"]["target_duration_us"] = unit * 5 + 9_000_000
    for shot in middle["shots"]:
        shot["suggested_duration_us"] = 1
    added = add_broll(docs, story, origin="generated", duration=unit * 2 - 1)
    report = validate(story, docs)
    assert report["ok"], report["errors"]
    added["suggested_duration_us"] += 1
    segment = next(s for s in docs["AssetManifest"]["segments"] if s["segment_id"] == added["segment_id"])
    asset = next(a for a in docs["AssetManifest"]["assets"] if a["asset_id"] == segment["asset_id"])
    segment["source_range_us"]["end"] += 1
    asset["duration_us"] += 1
    assert_rejected(validate(story, docs), "beat-middle 非用户空镜总时长超过段落预算 40%")


@pytest.mark.parametrize("unit", [3_600_000, 2**54 + 1])
@pytest.mark.parametrize("extra", [0, 1])
def test_global_forty_percent_boundary_uses_integer_arithmetic(docs, unit, extra):
    story = generate(docs)
    isolate_middle_broll(story)
    target = unit * 5
    docs["Brief"]["requirements"]["target_duration_us"] = target
    middle = story["beats"][1]
    middle["duration_budget_us"] = target - 9_000_000
    for shot in middle["shots"]:
        shot["suggested_duration_us"] = 1
    add_broll(docs, story, origin="generated", duration=unit * 2 - 1 + extra)
    report = validate(story, docs)
    # 故意集中到一个 beat 以单独触发全局边界；逐段上限仍必须报告。
    assert_rejected(report, "beat-middle 非用户空镜总时长超过段落预算 40%")
    assert report["counts"]["nonuser_duration_us"] == unit * 2 + extra
    global_error = "非用户空镜总时长超过成片预算 40%"
    assert (global_error in report["errors"]) is bool(extra)


def test_exhausted_user_material_need_not_fill_sixty_percent(docs):
    sid = "seg-walk"
    segment = next(s for s in docs["AssetManifest"]["segments"] if s["segment_id"] == sid)
    segment["source_range_us"]["end"] = segment["source_range_us"]["begin"] + 1_000_000
    story = generate(docs)
    middle = story["beats"][1]
    user_duration = sum(s["suggested_duration_us"] for s in middle["shots"] if s["origin"] == "user")
    assert user_duration == 1_000_000
    assert user_duration * 5 < middle["duration_budget_us"] * 3
    report = validate(story, docs)
    assert report["ok"], report["errors"]


def test_golden_bundle_story_remains_valid():
    _, golden = load_bundle(ROOT / "examples/team_contracts/bundle.json")
    story = plan(
        golden["Brief"], golden["AssetManifest"], golden["CandidateSet"],
        retrieval_request=golden["RetrievalRequest"], broll_queries=False,
        approved_cues=read_json(ROOT / "examples/team_contracts/c/approved_cues.json"),
    ).story_plan
    report = validate(story, golden)
    assert report["ok"], report["errors"]
    assert story["status"] == "ready_for_edit"

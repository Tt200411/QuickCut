"""规划质量、事件审核与空镜角色策略的离线回归测试。"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from quickcut.planning import (
    PlanningError,
    RuleSelector,
    build_outline,
    build_retrieval_request,
    load_bundle,
    plan,
    read_json,
)
from quickcut.planning.planning import candidates_for_beat

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def docs():
    _, documents = load_bundle(ROOT / "examples/team_contracts/bundle.json")
    documents["CandidateSet"] = read_json(ROOT / "examples/team_contracts/c/candidate_set.json")
    documents["RetrievalRequest"] = build_retrieval_request(
        documents["Brief"], documents["AssetManifest"], build_outline(documents["Brief"]),
    )
    return documents


def _quality_item(docs, level):
    candidate = docs["CandidateSet"]["results"][0]["candidates"][0]
    segment = next(s for s in docs["AssetManifest"]["segments"] if s["segment_id"] == candidate["segment_id"])
    asset = next(a for a in docs["AssetManifest"]["assets"] if a["asset_id"] == segment["asset_id"])
    return {"candidate": candidate, "segment": segment, "asset": asset}[level]


def _open_candidates(docs):
    return candidates_for_beat(
        build_outline(docs["Brief"])[0], docs["RetrievalRequest"],
        docs["CandidateSet"], docs["AssetManifest"],
    )


@pytest.mark.parametrize("level", ["candidate", "segment", "asset"])
@pytest.mark.parametrize("marker", ["flags", "quality.flags", "quality.usable"])
def test_quality_rejects_bad_evidence_at_every_level(docs, level, marker):
    item = _quality_item(docs, level)
    if marker == "flags":
        item["flags"] = ["bad_evidence"]
    elif marker == "quality.flags":
        item["quality"] = {"usable": True, "flags": ["bad_evidence"]}
    else:
        item["quality"] = {"usable": False, "flags": []}
    assert "candidate-seg-packing" not in {c["candidate_id"] for c in _open_candidates(docs)}
    assert "candidate-seg-city" in {c["candidate_id"] for c in _open_candidates(docs)}


@pytest.mark.parametrize("level", ["candidate", "segment", "asset"])
@pytest.mark.parametrize("quality", [None, {}, {"flags": None}, {"usable": True, "flags": ["soft_warning"]}])
def test_optional_quality_and_flags_allow_usable_evidence(docs, level, quality):
    item = _quality_item(docs, level)
    item["quality"] = quality
    item["flags"] = None
    candidate = next(c for c in _open_candidates(docs) if c["candidate_id"] == "candidate-seg-packing")
    assert candidate["flags"] == []


@pytest.mark.parametrize("level", ["candidate", "segment", "asset"])
@pytest.mark.parametrize("malformed", [
    {"quality": []},
    {"quality": "bad_evidence"},
    {"quality": False},
    {"flags": "bad_evidence"},
    {"quality": {"flags": {"bad_evidence": True}}},
    {"quality": {"flags": [False]}},
])
def test_malformed_quality_returns_planning_error(docs, level, malformed):
    _quality_item(docs, level).update(malformed)
    with pytest.raises(PlanningError) as exc:
        _open_candidates(docs)
    assert exc.value.code == "VLM_INVALID_PLAN"
    assert exc.value.retryable is False


@pytest.mark.parametrize("level", ["candidate", "segment", "asset"])
def test_quality_filter_applies_to_external_candidates(docs, level):
    candidate = next(r for r in docs["CandidateSet"]["results"] if r["query_id"] == "query-close-atmosphere")["candidates"][0]
    segment = next(s for s in docs["AssetManifest"]["segments"] if s["segment_id"] == candidate["segment_id"])
    asset = next(a for a in docs["AssetManifest"]["assets"] if a["asset_id"] == segment["asset_id"])
    {"candidate": candidate, "segment": segment, "asset": asset}[level]["quality"] = {"usable": False}
    candidates = candidates_for_beat(
        build_outline(docs["Brief"])[2], docs["RetrievalRequest"], docs["CandidateSet"], docs["AssetManifest"],
    )
    assert {c["candidate_id"] for c in candidates} == {"candidate-seg-rest"}


@pytest.mark.parametrize("role,suffix", [
    ("establishing", "middle"), ("transition", "middle"),
    ("detail", "middle"), ("atmosphere", "close"),
])
def test_each_declared_role_drives_outline_and_request(docs, role, suffix):
    brief = docs["Brief"]
    brief["requirements"]["broll_policy"] = {"enabled": True, "roles": [role]}
    outline = build_outline(brief)
    request = build_retrieval_request(brief, docs["AssetManifest"], outline)
    assert [q["query_id"] for q in request["queries"]] == [
        "query-open", "query-middle", "query-close", f"query-{suffix}-{role}",
    ]
    query = request["queries"][-1]
    assert query["role"] == role
    assert query["beat_id"] == f"beat-{suffix}"
    assert query["allowed_origins"] == ["external"]
    for beat in outline:
        expected = beat["beat_id"] == f"beat-{suffix}"
        assert beat["shot_needs"] == (["primary", role] if expected else ["primary"])
        assert beat["broll_allowed"] is expected
    assert [b["duration_budget_us"] for b in outline] == [6_000_000, 9_000_000, 3_000_000]
    assert query["duration_budget_us"] == 3_000_000


def test_default_declared_roles_have_independent_queries_and_budgets(docs):
    outline = build_outline(docs["Brief"])
    queries = docs["RetrievalRequest"]["queries"]
    assert [q["query_id"] for q in queries] == [
        "query-open", "query-middle", "query-close", "query-middle-establishing",
        "query-middle-transition", "query-close-atmosphere",
    ]
    assert [q["role"] for q in queries] == [
        "primary", "primary", "primary", "establishing", "transition", "atmosphere",
    ]
    assert [b["shot_needs"] for b in outline] == [
        ["primary"], ["primary", "establishing", "transition"], ["primary", "atmosphere"],
    ]
    assert [b["broll_allowed"] for b in outline] == [False, True, True]
    assert [q["duration_budget_us"] for q in queries] == [
        6_000_000, 9_000_000, 3_000_000, 3_000_000, 3_000_000, 3_000_000,
    ]


def test_role_order_and_duplicates_do_not_change_query_order(docs):
    brief = docs["Brief"]
    brief["requirements"]["broll_policy"]["roles"] = [
        "atmosphere", "detail", "transition", "establishing", "detail",
    ]
    outline = build_outline(brief)
    request = build_retrieval_request(brief, docs["AssetManifest"], outline)
    assert [q["query_id"] for q in request["queries"]] == [
        "query-open", "query-middle", "query-close", "query-middle-establishing",
        "query-middle-transition", "query-middle-detail", "query-close-atmosphere",
    ]
    assert outline[1]["shot_needs"] == ["primary", "establishing", "transition", "detail"]


@pytest.mark.parametrize("roles", [[], ["establishing"], ["transition"], ["detail"], ["atmosphere"]])
def test_disabled_policy_has_no_broll_needs_queries_or_permission(docs, roles):
    brief = docs["Brief"]
    brief["requirements"]["broll_policy"] = {"enabled": False, "roles": roles}
    outline = build_outline(brief)
    request = build_retrieval_request(brief, docs["AssetManifest"], outline)
    assert all(b["shot_needs"] == ["primary"] and b["broll_allowed"] is False for b in outline)
    assert [q["query_id"] for q in request["queries"]] == ["query-open", "query-middle", "query-close"]


def test_enabled_empty_roles_has_no_broll_needs_or_queries(docs):
    docs["Brief"]["requirements"]["broll_policy"] = {"enabled": True, "roles": []}
    outline = build_outline(docs["Brief"])
    request = build_retrieval_request(docs["Brief"], docs["AssetManifest"], outline)
    assert all(b["shot_needs"] == ["primary"] and not b["broll_allowed"] for b in outline)
    assert [q["role"] for q in request["queries"]] == ["primary"] * 3


def test_broll_queries_false_keeps_only_primary_queries(docs):
    request = build_retrieval_request(
        docs["Brief"], docs["AssetManifest"], build_outline(docs["Brief"]), broll_queries=False,
    )
    assert [q["role"] for q in request["queries"]] == ["primary"] * 3


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("roles", [["unknown"], ["establishing", "unknown"], [None], "detail"])
def test_unknown_roles_raise_in_outline_and_request(docs, enabled, roles):
    brief = docs["Brief"]
    outline = build_outline(brief)
    brief["requirements"]["broll_policy"] = {"enabled": enabled, "roles": roles}
    for build in (
        lambda: build_outline(brief),
        lambda: build_retrieval_request(brief, docs["AssetManifest"], outline),
    ):
        with pytest.raises(PlanningError) as exc:
            build()
        assert exc.value.code == "VLM_INVALID_PLAN"
        assert "角色" in exc.value.message


def _human_reviews(docs):
    for document in docs.values():
        document["fixtures_only"] = False
    reviews = {}
    for beat in build_outline(docs["Brief"]):
        for candidate in candidates_for_beat(
            beat, docs["RetrievalRequest"], docs["CandidateSet"], docs["AssetManifest"],
        ):
            if candidate["origin"] == "user":
                reviews[candidate["candidate_id"]] = {
                    **{key: copy.deepcopy(candidate[key]) for key in (
                        "asset_id", "segment_id", "asset_revision", "source_range_us",
                    )},
                    "beat_id": beat["beat_id"], "event": beat["wish"]["event"],
                    "reviewer": "测试审阅人", "note": "已核对当前片段与事件",
                }
    return reviews


def _story(docs, reviews, **kwargs):
    return plan(
        docs["Brief"], docs["AssetManifest"], docs["CandidateSet"],
        retrieval_request=docs["RetrievalRequest"], reviewed_evidence=reviews,
        approved_cues=read_json(ROOT / "examples/team_contracts/c/approved_cues.json"), **kwargs,
    ).story_plan


@pytest.mark.parametrize("unreviewed", ["candidate-seg-city", "candidate-seg-packing", "candidate-walk-alt"])
def test_unused_unreviewed_user_candidate_does_not_block_ready(docs, unreviewed):
    reviews = _human_reviews(docs)
    del reviews[unreviewed]

    class RecordingSelector(RuleSelector):
        def __init__(self):
            self.seen = []

        def select(self, *, beat, candidates, context):
            self.seen.extend(c["candidate_id"] for c in candidates)
            return super().select(beat=beat, candidates=candidates, context=context)

    selector = RecordingSelector()
    story = _story(docs, reviews, selector=selector)
    assert story["status"] == "ready_for_edit"
    assert story["unresolved_gaps"] == []
    assert story["pending_reviews"] == []
    assert unreviewed not in selector.seen
    assert unreviewed not in {s["candidate_id"] for b in story["beats"] for s in b["shots"]}
    provenance = story["decision_provenance"]
    assert {r["candidate_id"] for r in provenance["evidence_reviews"]} == set(reviews)
    assert all(r["source"] == "human_review" for r in provenance["evidence_reviews"])
    assert provenance["reviewed_anchor_segment_ids"]


@pytest.mark.parametrize("field,value", [
    ("segment_id", "seg-rest"), ("asset_id", "other-asset"),
    ("source_range_us", {"begin": 1, "end": 2}), ("asset_revision", 2),
])
def test_invalid_review_excludes_candidate_but_qualified_alternative_is_ready(docs, field, value):
    reviews = _human_reviews(docs)
    reviews["candidate-seg-packing"][field] = value
    story = _story(docs, reviews)
    assert story["status"] == "ready_for_edit"
    assert story["pending_reviews"] == []
    assert {s["candidate_id"] for s in story["beats"][0]["shots"]} == {"candidate-seg-city"}
    assert "candidate-seg-packing" not in {
        r["candidate_id"] for r in story["decision_provenance"]["evidence_reviews"]
    }


def test_reviewed_unused_alternative_keeps_provenance(docs):
    reviews = _human_reviews(docs)
    story = _story(docs, reviews)
    assert "candidate-seg-city" not in {s["candidate_id"] for b in story["beats"] for s in b["shots"]}
    recorded = {r["candidate_id"]: r for r in story["decision_provenance"]["evidence_reviews"]}
    assert recorded["candidate-seg-city"] == {
        **reviews["candidate-seg-city"], "candidate_id": "candidate-seg-city", "source": "human_review",
    }


def test_pending_event_reviews_only_block_beats_without_qualified_evidence(docs):
    reviews = _human_reviews(docs)
    for candidate_id in ("candidate-seg-city", "candidate-seg-walk", "candidate-walk-alt"):
        del reviews[candidate_id]
    story = _story(docs, reviews)
    pending = [r for r in story["pending_reviews"] if r["type"] == "event_evidence"]
    assert story["status"] == "needs_input"
    assert {r["candidate_id"] for r in pending} == {"candidate-seg-walk", "candidate-walk-alt"}
    assert {r["beat_id"] for r in pending} == {"beat-middle"}
    assert {g["beat_id"] for g in story["unresolved_gaps"]} == {"beat-middle"}
    assert story["beats"][1]["shots"] == []


@pytest.mark.parametrize("level", ["candidate", "segment", "asset"])
def test_review_cannot_override_unusable_quality(docs, level):
    reviews = _human_reviews(docs)
    _quality_item(docs, level)["quality"] = {"usable": False}
    story = _story(docs, reviews)
    assert story["status"] == "ready_for_edit"
    assert "candidate-seg-packing" not in {
        s["candidate_id"] for b in story["beats"] for s in b["shots"]
    }
    assert "candidate-seg-packing" not in {
        r["candidate_id"] for r in story["decision_provenance"]["evidence_reviews"]
    }
    assert story["pending_reviews"] == []


def test_close_atmosphere_keeps_event_gap_and_non_event_narration(docs):
    for result in docs["CandidateSet"]["results"]:
        if result["query_id"] == "query-close":
            result["candidates"] = []
    story = _story(docs, {})
    close = story["beats"][-1]
    assert story["status"] == "needs_input"
    assert {g["beat_id"] for g in story["unresolved_gaps"]} == {"beat-close"}
    assert len(close["shots"]) == 1
    shot = close["shots"][0]
    assert shot["origin"] == "external"
    assert shot["role"] == "atmosphere"
    assert shot["decided_by"] == "rule:gap_policy"
    cue = next(c for c in story["narration_cues"] if c["beat_id"] == "beat-close")
    assert cue["asserts_specific_user_event"] is False
    assert cue["evidence_segment_ids"] == []
    assert cue["approved"] is False


def test_unapproved_narration_still_blocks_ready(docs):
    reviews = _human_reviews(docs)
    story = plan(
        docs["Brief"], docs["AssetManifest"], docs["CandidateSet"],
        retrieval_request=docs["RetrievalRequest"], reviewed_evidence=reviews,
    ).story_plan
    assert story["status"] == "needs_input"
    assert story["unresolved_gaps"] == []
    assert len(story["pending_reviews"]) == 3
    assert {r["type"] for r in story["pending_reviews"]} == {"narration"}

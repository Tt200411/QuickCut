"""C 模块（故事与视听规划）回归测试：全部不需要媒体、模型或网络。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from quickcut.planning import (
    FixedResponseSelector,
    PlanningError,
    build_outline,
    build_retrieval_request,
    classify_beat,
    compare_plans,
    load_bundle,
    next_retrieval_request,
    plan,
    read_json,
    validate_selection,
    validate_story_plan,
)
from quickcut.planning.planning import candidates_for_beat

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "examples/team_contracts/bundle.json"
C_CANDIDATES = ROOT / "examples/team_contracts/c/candidate_set.json"
C_CUES = ROOT / "examples/team_contracts/c/approved_cues.json"
C_FIXED = ROOT / "examples/team_contracts/c/fixed_model_response.json"


@pytest.fixture
def docs():
    _raw, golden = load_bundle(BUNDLE)
    golden["CandidateSet"] = read_json(C_CANDIDATES)
    golden["RetrievalRequest"] = build_retrieval_request(golden["Brief"], golden["AssetManifest"], build_outline(golden["Brief"]))
    return golden


@pytest.fixture
def request_doc(docs):
    return build_retrieval_request(
        docs["Brief"], docs["AssetManifest"], build_outline(docs["Brief"]),
        broll_queries=True, document_id="request-001",
    )


def test_outline_splits_wish_from_fact_and_keeps_integer_microseconds(docs):
    brief = docs["Brief"]
    outline = build_outline(brief)
    assert [b["order"] for b in outline] == [0, 1, 2]
    assert all(b["wish"]["assertion"] == "user_wish" and b["wish"]["verified"] is False for b in outline)
    assert all(type(b["duration_budget_us"]) is int for b in outline)
    assert sum(b["duration_budget_us"] for b in outline) == brief["requirements"]["target_duration_us"]


def test_retrieval_request_has_roles_origins_and_finite_loop(request_doc):
    roles = {q["role"] for q in request_doc["queries"]}
    assert {"primary", "establishing", "atmosphere"} <= roles
    for query in request_doc["queries"]:
        assert {"query_id", "beat_id", "semantic_text", "role", "allowed_origins",
                "duration_budget_us", "top_k"} <= set(query)
        assert type(query["duration_budget_us"]) is int
        if query["role"] in {"establishing", "atmosphere"}:
            assert query["allowed_origins"] == ["external"]
    assert request_doc["iteration"] == 1 and request_doc["max_iterations"] == 3


def test_complete_story_passes_c_validator(docs, request_doc):
    result = plan(
        docs["Brief"], docs["AssetManifest"], docs["CandidateSet"],
        retrieval_request=request_doc, approved_cues=read_json(C_CUES),
    )
    story = result.story_plan
    report = validate_story_plan(
        story, brief=docs["Brief"], candidate_set=docs["CandidateSet"],
        manifest=docs["AssetManifest"], upstream=docs,
    )
    assert report["ok"], report["errors"]
    assert story["status"] == "ready_for_edit" and story["unresolved_gaps"] == []
    assert story["decision_provenance"]["mode"] == "rule"
    assert story["decision_provenance"]["human_edits"], "人工文案必须留痕，不能记成模型输出"
    assert [b["beat_id"] for b in story["beats"]] == ["beat-open", "beat-middle", "beat-close"]
    assert sum(len(b["shots"]) for b in story["beats"]) == 4
    assert all(c["text_source"] == "human_editorial" for c in story["narration_cues"])


def test_every_selected_shot_and_cue_is_traceable(docs, request_doc):
    story = plan(
        docs["Brief"], docs["AssetManifest"], docs["CandidateSet"],
        retrieval_request=request_doc, approved_cues=read_json(C_CUES),
    ).story_plan
    candidate_ids = {
        c["candidate_id"] for r in docs["CandidateSet"]["results"] for c in r["candidates"]
    }
    selected_user = {
        s["segment_id"] for b in story["beats"] for s in b["shots"] if s["origin"] == "user"
    }
    for beat in story["beats"]:
        for shot in beat["shots"]:
            assert shot["candidate_id"] in candidate_ids
            assert shot["evidence"]
            if shot["origin"] == "external":
                assert shot["role"] != "primary"
                assert shot["evidence"] == ["fixture_annotation"]
    for cue in story["narration_cues"]:
        assert set(cue["evidence_segment_ids"]) <= selected_user
        assert cue["approved"] is True


def test_fixed_model_response_mode_is_labelled_as_fixture(docs, request_doc):
    selector = FixedResponseSelector.from_path(C_FIXED)
    story = plan(
        docs["Brief"], docs["AssetManifest"], docs["CandidateSet"],
        retrieval_request=request_doc, selector=selector, approved_cues=read_json(C_CUES),
    ).story_plan
    report = validate_story_plan(
        story, brief=docs["Brief"], candidate_set=docs["CandidateSet"],
        manifest=docs["AssetManifest"], upstream=docs,
    )
    assert report["ok"], report["errors"]
    provenance = story["decision_provenance"]
    assert provenance["mode"] == "fixed_response"
    assert provenance["model_call_ids"] and provenance["raw_response_count"] == 3
    assert all(s["decided_by"].startswith("fixture:") for b in story["beats"] for s in b["shots"])


def test_invalid_model_plan_keeps_raw_response(docs, request_doc):
    beat = build_outline(docs["Brief"])[0]
    candidates = candidates_for_beat(beat, request_doc, docs["CandidateSet"], docs["AssetManifest"])
    bad = FixedResponseSelector({"beats": {"beat-open": {"shot_order": ["candidate-not-real"]}}})
    with pytest.raises(PlanningError) as exc:
        validate_selection(
            bad.select(beat=beat, candidates=candidates, context={}),
            beat=beat, candidates=candidates,
        )
    assert exc.value.code == "VLM_INVALID_PLAN"
    assert exc.value.to_envelope()["detail"]["raw_response"] is not None


def test_external_broll_cannot_be_primary(docs, request_doc):
    beat = build_outline(docs["Brief"])[0]
    candidates = candidates_for_beat(beat, request_doc, docs["CandidateSet"], docs["AssetManifest"])
    candidates.append({**copy.deepcopy(candidates[0]),
                       "candidate_id": "candidate-seg-forest", "segment_id": "seg-forest",
                       "origin": "external"})
    bad = FixedResponseSelector({"beats": {"beat-open": {
        "shot_order": ["candidate-seg-forest"], "roles": {"candidate-seg-forest": "primary"},
    }}})
    with pytest.raises(PlanningError) as exc:
        validate_selection(
            bad.select(beat=beat, candidates=candidates, context={}),
            beat=beat, candidates=candidates,
        )
    assert exc.value.code == "VLM_INVALID_PLAN"


def test_alignment_three_states_and_limited_retrieval(docs, request_doc):
    beat = build_outline(docs["Brief"])[0]
    assert classify_beat(beat, [{"origin": "user", "flags": [], "candidate_id": "x"}])["support"] == "partially_supported"
    assert classify_beat(beat, [{"origin": "user", "flags": [], "candidate_id": "x", "event_review": {"source": "human_review"}}])["support"] == "supported"
    assert classify_beat(beat, [{"origin": "external", "flags": [], "candidate_id": "y"}])["support"] == "partially_supported"
    assert classify_beat(beat, [])["support"] == "unsupported"

    gaps = [{"beat_id": "beat-open", "gap_id": "gap-beat-open"}]
    r2 = next_retrieval_request(request_doc, gaps, reason="放宽非关键风格条件")
    r3 = next_retrieval_request(r2, gaps, reason="再放宽一次")
    assert r2["iteration"] == 2 and r2["queries"][0]["top_k"] > request_doc["queries"][0]["top_k"]
    assert r3["iteration"] == 3
    assert next_retrieval_request(r3, gaps, reason="不应再检索") is None


def test_insufficient_case_reports_gap_without_inventing_events(docs, request_doc):
    candidate_set = copy.deepcopy(docs["CandidateSet"])
    for result in candidate_set["results"]:
        if result["query_id"] == "query-close":
            result["status"] = "empty"
            result["reason"] = "人工构造：结尾事件没有素材"
            result["candidates"] = []
    result = plan(
        docs["Brief"], docs["AssetManifest"], candidate_set,
        retrieval_request=request_doc, approved_cues=read_json(C_CUES),
    )
    story = result.story_plan
    report = validate_story_plan(
        story, brief=docs["Brief"], candidate_set=candidate_set,
        manifest=docs["AssetManifest"],
        upstream={"Brief": docs["Brief"], "AssetManifest": docs["AssetManifest"], "CandidateSet": candidate_set},
    )
    assert report["ok"], report["errors"]
    assert story["status"] == "needs_input"
    assert len(story["unresolved_gaps"]) == 1
    gap = story["unresolved_gaps"][0]
    assert gap["type"] in {"UNSUPPORTED_STORY_BEAT", "PARTIAL_EVENT_EVIDENCE"}
    assert gap["suggestions"] and gap["must_not_assert_user_events"] is True
    assert story["beats"][-1]["shots"] == []
    close_cues = [c for c in story["narration_cues"] if c["beat_id"] == "beat-close"]
    assert len(close_cues) == 1
    assert close_cues[0]["asserts_specific_user_event"] is False
    assert close_cues[0]["evidence_segment_ids"] == []
    assert close_cues[0]["approved"] is False
    assert result.alignment["next_request"] is not None


def test_partially_supported_beat_keeps_broll_to_atmosphere_only(docs, request_doc):
    candidate_set = copy.deepcopy(docs["CandidateSet"])
    for result in candidate_set["results"]:
        if result["query_id"] == "query-middle":
            result["status"] = "empty"
            result["reason"] = "人工构造：中段主镜头没有素材"
            result["candidates"] = []
    story = plan(
        docs["Brief"], docs["AssetManifest"], candidate_set,
        retrieval_request=request_doc, approved_cues=read_json(C_CUES),
    ).story_plan
    report = validate_story_plan(
        story, brief=docs["Brief"], candidate_set=candidate_set,
        manifest=docs["AssetManifest"],
        upstream={"Brief": docs["Brief"], "AssetManifest": docs["AssetManifest"], "CandidateSet": candidate_set},
    )
    assert report["ok"], report["errors"]
    assert story["status"] == "needs_input"
    middle = story["beats"][1]
    assert len(middle["shots"]) == 1
    assert middle["shots"][0]["origin"] == "external"
    assert middle["shots"][0]["role"] != "primary"
    assert middle["shots"][0]["decided_by"] == "rule:gap_policy"
    middle_cue = next(c for c in story["narration_cues"] if c["beat_id"] == "beat-middle")
    assert middle_cue["asserts_specific_user_event"] is False
    assert middle_cue["evidence_segment_ids"] == []
    assert middle_cue["approved"] is False
    assert any(g["beat_id"] == "beat-middle" and g["type"] == "PARTIAL_EVENT_EVIDENCE"
               for g in story["unresolved_gaps"])


def test_stale_input_revision_is_rejected(docs, request_doc):
    candidate_set = copy.deepcopy(docs["CandidateSet"])
    candidate_set["inputs"][0]["revision"] = 999
    with pytest.raises(PlanningError) as exc:
        plan(docs["Brief"], docs["AssetManifest"], candidate_set, retrieval_request=request_doc)
    assert exc.value.code == "STALE_INPUT_REVISION"


def test_compare_two_plans(docs, request_doc):
    first = plan(docs["Brief"], docs["AssetManifest"], docs["CandidateSet"],
                 retrieval_request=request_doc, approved_cues=read_json(C_CUES)).story_plan
    second = copy.deepcopy(first)
    second["beats"][0]["shots"] = []
    second["unresolved_gaps"] = [{"gap_id": "gap-x", "beat_id": "beat-open", "suggestions": ["补充素材"]}]
    second["status"] = "needs_input"
    diff = compare_plans(first, second)
    assert diff["changed_beats"] == ["beat-open"]
    assert diff["gap_counts"] == [0, 1]


def test_generated_story_plan_passes_official_bundle_validator(tmp_path):
    """把 C 生成的 StoryPlan 注入黄金 bundle，进程内调用仓库自带校验器与五个负例。"""
    import importlib.util

    from quickcut.planning import RuleSelector, write_json

    spec = importlib.util.spec_from_file_location(
        "team_contract_validate", ROOT / "examples/team_contracts/validate.py",
    )
    official = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(official)

    raw, docs = load_bundle(BUNDLE)
    brief, manifest, candidate_set = docs["Brief"], docs["AssetManifest"], docs["CandidateSet"]
    outline = build_outline(brief)
    request = docs["RetrievalRequest"]
    result = plan(
        brief, manifest, candidate_set, retrieval_request=request,
        selector=RuleSelector(), approved_cues=read_json(C_CUES),
        broll_queries=False, plan_document_id="story-001", plan_revision=1,
    )
    report = validate_story_plan(result.story_plan, brief=brief, candidate_set=candidate_set,
                                 manifest=manifest, upstream=docs)
    assert report["ok"], report["errors"]
    for doc in raw["documents"]:
        if doc["kind"] == "StoryPlan":
            doc.clear()
            doc.update(result.story_plan)
    official.validate(raw)
    official.self_test(raw)
    output = write_json(tmp_path / "injected_bundle.json", raw)
    injected = json.loads(output.read_text(encoding="utf-8"))
    story = next(d for d in injected["documents"] if d["kind"] == "StoryPlan")
    assert story["owner"] == "C" and story["status"] == "ready_for_edit"
    assert story["decision_provenance"]["mode"] == "rule"

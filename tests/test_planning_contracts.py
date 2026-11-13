"""规划契约与证据边界回归，使用本地夹具且不调用模型。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from quickcut.planning.contracts import PlanningError, check_envelope, check_inputs, envelope, load_bundle
from quickcut.planning.planning import build_outline, build_retrieval_request, plan
from quickcut.planning.validator import validate_story_plan
from quickcut.planning import cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def docs():
    _, data = load_bundle(ROOT / "examples/team_contracts/bundle.json")
    data["CandidateSet"] = json.loads((ROOT / "examples/team_contracts/c/candidate_set.json").read_text())
    data["RetrievalRequest"] = build_retrieval_request(data["Brief"], data["AssetManifest"], build_outline(data["Brief"]))
    return data


def approved():
    return json.loads((ROOT / "examples/team_contracts/c/approved_cues.json").read_text())


def generate(docs, **kwargs):
    return plan(docs["Brief"], docs["AssetManifest"], docs["CandidateSet"],
                retrieval_request=docs["RetrievalRequest"], **kwargs).story_plan


def validate(story, docs):
    return validate_story_plan(story, brief=docs["Brief"], manifest=docs["AssetManifest"],
                               candidate_set=docs["CandidateSet"], upstream=docs)


def reviews_for(docs):
    assets = {a["asset_id"]: a for a in docs["AssetManifest"]["assets"]}
    segments = {s["segment_id"]: s for s in docs["AssetManifest"]["segments"]}
    queries = {q["query_id"]: q for q in docs["RetrievalRequest"]["queries"]}
    beats = {b["beat_id"]: b for b in build_outline(docs["Brief"])}
    reviews = {}
    for result in docs["CandidateSet"]["results"]:
        bid = queries[result["query_id"]]["beat_id"]
        for candidate in result["candidates"]:
            asset = assets[segments[candidate["segment_id"]]["asset_id"]]
            if asset["origin"] == "user":
                reviews[candidate["candidate_id"]] = {
                    "beat_id": bid, "event": beats[bid]["wish"]["event"],
                    "reviewer": "测试审阅者", "note": "已核对片段与该段目标", "asset_revision": asset["revision"],
                    "asset_id": asset["asset_id"], "segment_id": candidate["segment_id"],
                    "source_range_us": copy.deepcopy(segments[candidate["segment_id"]]["source_range_us"]),
                }
    return reviews


def production(docs):
    for doc in docs.values():
        doc["fixtures_only"] = False


@pytest.mark.parametrize("mutation", ["segment", "asset", "range"])
def test_review_is_bound_to_exact_source_identity(docs, mutation):
    production(docs)
    reviews = reviews_for(docs)
    review = reviews["candidate-seg-packing"]
    if mutation == "segment":
        review["segment_id"] = "seg-rest"
    elif mutation == "asset":
        review["asset_id"] = "another-asset"
    else:
        review["source_range_us"]["begin"] += 1
    story = generate(docs, approved_cues=approved(), reviewed_evidence=reviews)
    assert story["status"] == "needs_input"
    assert "candidate-seg-packing" not in {
        shot["candidate_id"] for beat in story["beats"] for shot in beat["shots"]
    }
    assert validate(story, docs)["ok"]


def test_replacing_candidate_segment_invalidates_old_review(docs):
    production(docs)
    reviews = reviews_for(docs)
    for result in docs["CandidateSet"]["results"]:
        for candidate in result["candidates"]:
            if candidate["candidate_id"] == "candidate-seg-packing":
                candidate["segment_id"] = "seg-rest"
    docs["CandidateSet"]["revision"] += 1
    story = generate(docs, approved_cues=approved(), reviewed_evidence=reviews)
    assert story["status"] == "needs_input"
    assert "candidate-seg-packing" not in {
        shot["candidate_id"] for beat in story["beats"] for shot in beat["shots"]
    }
    assert validate(story, docs)["ok"]


def test_manifest_rejects_stale_segment_revision(docs):
    segment = docs["AssetManifest"]["segments"][0]
    segment["asset_revision"] += 1
    with pytest.raises(PlanningError) as error:
        generate(docs, approved_cues=approved())
    assert error.value.code == "STALE_INPUT_REVISION"


def test_load_single_document_and_bundle_structure(tmp_path, docs):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(docs["Brief"]))
    raw, loaded = load_bundle(path)
    assert loaded == {"Brief": raw}
    for bad in ([], None, {}, {"documents": {}}, {"documents": []}, {"documents": [None]},
                {"documents": [{"kind": "unknown"}]}, {"documents": [docs["Brief"], docs["Brief"]]}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_bundle(path)


@pytest.mark.parametrize("field,value", [("revision", True), ("revision", 0), ("revision", 1.0),
    ("fixtures_only", 1), ("fixtures_only", None), ("document_id", " "), ("project_id", ""), ("project_id", 7)])
def test_envelope_rejects_invalid_types(docs, field, value):
    doc = copy.deepcopy(docs["Brief"])
    doc[field] = value
    with pytest.raises(PlanningError):
        check_envelope(doc, expected_kind="Brief", owner="C")


@pytest.mark.parametrize("field,value", [("revision", True), ("fixtures_only", 1), ("document_id", ""), ("project_id", " ")])
def test_envelope_constructor_is_strict(field, value):
    args = dict(kind="StoryPlan", document_id="s", project_id="p", revision=1, fixtures_only=False, owner="C", inputs=[])
    args[field] = value
    with pytest.raises(ValueError):
        envelope(**args)


@pytest.mark.parametrize("kind", ["RetrievalRequest", "CandidateSet", "StoryPlan"])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "bool_revision", "wrong_id", "wrong_revision", "project"])
def test_required_exact_dependencies(docs, kind, mutation):
    doc = generate(docs, approved_cues=approved()) if kind == "StoryPlan" else copy.deepcopy(docs[kind])
    if mutation == "missing":
        doc["inputs"].pop()
    elif mutation == "duplicate":
        doc["inputs"].append(copy.deepcopy(doc["inputs"][0]))
    elif mutation == "bool_revision":
        doc["inputs"][0]["revision"] = True
    elif mutation == "wrong_id":
        doc["inputs"][0]["document_id"] = "other"
    elif mutation == "wrong_revision":
        doc["inputs"][0]["revision"] = 2
    else:
        doc["project_id"] = "other"
    with pytest.raises(PlanningError):
        check_inputs(doc, docs)


@pytest.mark.parametrize("field,value", [("owner", "B"), ("revision", True), ("project_id", "other"), ("fixtures_only", "true")])
def test_request_envelope_is_checked(docs, field, value):
    docs["RetrievalRequest"][field] = value
    with pytest.raises(PlanningError):
        generate(docs)


@pytest.mark.parametrize("upstream", ["Brief", "AssetManifest", "CandidateSet", "RetrievalRequest"])
def test_fixture_taint_propagates_from_every_upstream(docs, upstream):
    production(docs)
    docs[upstream]["fixtures_only"] = True
    result = plan(docs["Brief"], docs["AssetManifest"], docs["CandidateSet"], retrieval_request=docs["RetrievalRequest"])
    assert result.story_plan["fixtures_only"] is True
    assert result.retrieval_request["fixtures_only"] is True
    if upstream in {"Brief", "AssetManifest"}:
        assert build_retrieval_request(docs["Brief"], docs["AssetManifest"], build_outline(docs["Brief"]))["fixtures_only"] is True


def test_templates_wait_for_approval_without_material_gaps(docs):
    story = generate(docs)
    assert story["status"] == "needs_input" and not story["unresolved_gaps"]
    assert len(story["pending_reviews"]) == 3
    assert validate(story, docs)["ok"]
    story["status"] = "ready_for_edit"
    assert not validate(story, docs)["ok"]


def test_disabled_narration_has_no_cues(docs):
    docs["Brief"]["requirements"]["narration"]["enabled"] = False
    story = generate(docs)
    assert story["narration_cues"] == []
    assert all(b["cue_ids"] == [] for b in story["beats"])
    assert story["status"] == "ready_for_edit"
    assert validate(story, docs)["ok"]


def test_nonfixture_candidates_need_event_reviews(docs):
    production(docs)
    story = generate(docs, approved_cues=approved())
    assert story["status"] == "needs_input" and len(story["unresolved_gaps"]) == 3
    assert not [s for b in story["beats"] for s in b["shots"] if s["origin"] == "user"]
    assert any(p["type"] == "event_evidence" for p in story["pending_reviews"])
    assert validate(story, docs)["ok"]
    story = generate(docs, approved_cues=approved(), reviewed_evidence=reviews_for(docs))
    assert story["status"] == "ready_for_edit"
    assert all(r["source"] == "human_review" for r in story["decision_provenance"]["evidence_reviews"])
    assert validate(story, docs)["ok"]


@pytest.mark.parametrize("field,value", [("beat_id", "beat-close"), ("event", "旧事件"), ("asset_revision", 2),
    ("asset_revision", True), ("reviewer", ""), ("note", " ")])
def test_invalid_review_cannot_verify_candidate(docs, field, value):
    production(docs)
    reviews = reviews_for(docs)
    for review in reviews.values():
        if review["beat_id"] == "beat-open":
            review[field] = value
    story = generate(docs, approved_cues=approved(), reviewed_evidence=reviews)
    assert story["beats"][0]["shots"] == []
    assert any(g["beat_id"] == "beat-open" for g in story["unresolved_gaps"])


def test_brief_event_change_invalidates_previous_review(docs):
    production(docs)
    reviews = reviews_for(docs)
    docs["Brief"]["requirements"]["story"] = "新的出发事件，然后途中步行，最后休息"
    story = generate(docs, approved_cues=approved(), reviewed_evidence=reviews)
    assert story["beats"][0]["shots"] == []


def test_fixture_annotation_is_explicit_and_not_human_review(docs):
    story = generate(docs, approved_cues=approved())
    provenance = story["decision_provenance"]
    assert not provenance["reviewed_anchor_segment_ids"]
    assert all(r["source"] == "fixture_annotation" and "reviewer" not in r for r in provenance["evidence_reviews"])
    for result in docs["CandidateSet"]["results"]:
        for candidate in result["candidates"]:
            candidate["evidence"] = ["retrieval_match"]
    assert generate(docs)["status"] == "needs_input"


def test_shot_keeps_actual_evidence_not_wish(docs):
    story = generate(docs, approved_cues=approved())
    shot = story["beats"][0]["shots"][0]
    segment = next(s for s in docs["AssetManifest"]["segments"] if s["segment_id"] == shot["segment_id"])
    assert shot["evidence"] == ["fixture_annotation"]
    assert shot["summary"] == segment["description"]
    assert shot["transcript"] == segment.get("transcript", {}).get("text", "")
    shot["evidence"] = docs["Brief"]["requirements"]["story"]
    assert not validate(story, docs)["ok"]


def test_unreviewed_user_cannot_be_selected_by_model(docs):
    from quickcut.planning.selection import FixedResponseSelector
    production(docs)
    reviews = reviews_for(docs)
    del reviews["candidate-walk-alt"]
    selector = FixedResponseSelector({"beats": {
        "beat-open": {"shot_order": ["candidate-seg-packing"]},
        "beat-middle": {"shot_order": ["candidate-walk-alt"]},
    }})
    with pytest.raises(PlanningError):
        generate(docs, reviewed_evidence=reviews, selector=selector)


@pytest.mark.parametrize("begin,end", [(True, 10), (0.0, 10), (-1, 10), (0, "10"), (10, 10), (20, 10), (0, 999_000_000)])
def test_invalid_source_range_rejected_by_planner_and_validator(docs, begin, end):
    story = generate(docs, approved_cues=approved())
    docs["AssetManifest"]["segments"][0]["source_range_us"] = {"begin": begin, "end": end}
    with pytest.raises(PlanningError):
        generate(docs)
    assert not validate(story, docs)["ok"]


def test_origin_limits_and_bad_evidence_are_enforced(docs):
    story = generate(docs, approved_cues=approved())
    docs["RetrievalRequest"]["queries"][0]["allowed_origins"] = ["external"]
    with pytest.raises(PlanningError):
        generate(docs)
    assert not validate(story, docs)["ok"]
    docs["RetrievalRequest"]["queries"][0]["allowed_origins"] = ["user"]
    docs["CandidateSet"]["results"][0]["candidates"][0]["flags"] = ["bad_evidence"]
    assert all(s["candidate_id"] != docs["CandidateSet"]["results"][0]["candidates"][0]["candidate_id"]
               for s in generate(docs)["beats"][0]["shots"])
    assert not validate(story, docs)["ok"]


@pytest.mark.parametrize("duration", [True, 0, -1, 1.5, "100"])
@pytest.mark.parametrize("location", ["beat", "shot", "cue"])
def test_validator_requires_positive_integer_durations(docs, duration, location):
    story = generate(docs, approved_cues=approved())
    target = {"beat": story["beats"][0], "shot": story["beats"][0]["shots"][0], "cue": story["narration_cues"][0]}[location]
    target["suggested_duration_us" if location == "shot" else "duration_budget_us"] = duration
    assert not validate(story, docs)["ok"]


@pytest.mark.parametrize("mutation", ["beat_id", "shot_id", "candidate_id", "cross_beat_shot", "cross_beat_cue", "cue_owner", "empty_evidence", "unapproved"])
def test_validator_rejects_invalid_story_links(docs, mutation):
    story = generate(docs, approved_cues=approved())
    first, middle = story["beats"][:2]
    if mutation == "beat_id":
        middle["beat_id"] = first["beat_id"]
    elif mutation == "shot_id":
        first["shots"][0]["shot_id"] = middle["shots"][0]["shot_id"] = "duplicate"
    elif mutation == "candidate_id":
        first["shots"].append(copy.deepcopy(first["shots"][0]))
    elif mutation == "cross_beat_shot":
        first["shots"] = copy.deepcopy(middle["shots"])
    elif mutation == "cross_beat_cue":
        story["narration_cues"][0]["evidence_segment_ids"] = [middle["shots"][0]["segment_id"]]
    elif mutation == "cue_owner":
        middle["cue_ids"].append(first["cue_ids"][0])
    elif mutation == "empty_evidence":
        story["narration_cues"][0]["evidence_segment_ids"] = []
    else:
        story["narration_cues"][0]["approved"] = False
    assert not validate(story, docs)["ok"]


def test_generated_origin_never_counts_as_user_evidence(docs):
    story = generate(docs, approved_cues=approved())
    sid = story["beats"][0]["shots"][0]["segment_id"]
    segment = next(s for s in docs["AssetManifest"]["segments"] if s["segment_id"] == sid)
    asset = next(a for a in docs["AssetManifest"]["assets"] if a["asset_id"] == segment["asset_id"])
    asset["origin"] = "generated"
    docs["RetrievalRequest"]["queries"][0]["allowed_origins"] = ["generated"]
    story["beats"][0]["shots"][0]["origin"] = "generated"
    assert not validate(story, docs)["ok"]


def write_bundle(tmp_path, docs):
    path = tmp_path / "custom.json"
    path.write_text(json.dumps({"documents": list(docs.values())}))
    return path


def test_custom_cli_bundle_does_not_get_default_cues(tmp_path, docs, capsys):
    path = write_bundle(tmp_path, docs)
    out = tmp_path / "out"
    assert cli.main(["run", "--bundle", str(path), "--out-dir", str(out)]) == 0
    story = json.loads((out / "story_plan.json").read_text())
    assert story["status"] == "needs_input"
    assert all(c["approved"] is False for c in story["narration_cues"])
    assert cli.main(["run", "--bundle", str(path), "--out-dir", str(out), "--approved-cues", str(cli.DEFAULT_APPROVED_CUES)]) == 0
    assert json.loads((out / "story_plan.json").read_text())["status"] == "ready_for_edit"


def test_cli_reviewed_evidence_and_insufficient_guard(tmp_path, docs, capsys):
    production(docs)
    path = write_bundle(tmp_path, docs)
    review_path = tmp_path / "reviews.json"
    review_path.write_text(json.dumps(reviews_for(docs)))
    out = tmp_path / "out"
    args = ["run", "--bundle", str(path), "--out-dir", str(out), "--approved-cues", str(cli.DEFAULT_APPROVED_CUES),
            "--reviewed-evidence", str(review_path)]
    assert cli.main(args) == 0
    assert json.loads((out / "story_plan.json").read_text())["status"] == "ready_for_edit"
    assert cli.main(args + ["--case", "insufficient"]) == 2
    assert "fixtures_only" in capsys.readouterr().err


def test_custom_candidate_override_preserves_request_contract(tmp_path, docs, capsys):
    path = write_bundle(tmp_path, docs)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps(docs["CandidateSet"]))
    out = tmp_path / "out"
    args = ["run", "--bundle", str(path), "--candidate-set", str(candidates), "--out-dir", str(out)]
    assert cli.main(args) == 0
    story = json.loads((out / "story_plan.json").read_text())
    assert story["status"] == "needs_input"
    docs["RetrievalRequest"]["inputs"][0]["revision"] = True
    path = write_bundle(tmp_path, docs)
    assert cli.main(args) == 2


def test_insufficient_rejects_mixed_fixture_inputs(tmp_path, docs, capsys):
    docs["Brief"]["fixtures_only"] = False
    path = write_bundle(tmp_path, docs)
    assert cli.main(["run", "--bundle", str(path), "--case", "insufficient", "--out-dir", str(tmp_path / "out")]) == 2


def test_inject_rejects_ignored_plan_argument():
    with pytest.raises(SystemExit) as exc:
        cli.main(["inject", "--plan", "ignored.json"])
    assert exc.value.code == 2


@pytest.mark.parametrize("level", ["candidate", "segment", "asset"])
def test_bad_external_evidence_is_filtered(docs, level):
    candidate = next(c for r in docs["CandidateSet"]["results"] for c in r["candidates"]
                     if c["candidate_id"] == "candidate-seg-forest")
    segment = next(s for s in docs["AssetManifest"]["segments"] if s["segment_id"] == candidate["segment_id"])
    asset = next(a for a in docs["AssetManifest"]["assets"] if a["asset_id"] == segment["asset_id"])
    {"candidate": candidate, "segment": segment, "asset": asset}[level]["flags"] = ["bad_evidence"]
    story = generate(docs, approved_cues=approved())
    assert not any(s["candidate_id"] == candidate["candidate_id"] for b in story["beats"] for s in b["shots"])
    assert validate(story, docs)["ok"]


def test_review_mapping_cannot_be_reused_for_another_candidate(docs):
    production(docs)
    reviews = reviews_for(docs)
    for cid in list(reviews):
        reviews["unknown-" + cid] = reviews.pop(cid)
    story = generate(docs, approved_cues=approved(), reviewed_evidence=reviews)
    assert not any(s["origin"] == "user" for b in story["beats"] for s in b["shots"])


def test_validator_rechecks_event_and_fixture_provenance(docs):
    production(docs)
    story = generate(docs, approved_cues=approved(), reviewed_evidence=reviews_for(docs))
    docs["Brief"]["requirements"]["story"] = "改变的事件，然后步行，最后休息"
    assert not validate(story, docs)["ok"]
    for doc in docs.values():
        doc["fixtures_only"] = True
    story = generate(docs, approved_cues=approved())
    story["fixtures_only"] = False
    assert not validate(story, docs)["ok"]


def test_empty_explicit_request_is_not_ignored(docs):
    with pytest.raises(PlanningError):
        plan(docs["Brief"], docs["AssetManifest"], docs["CandidateSet"], retrieval_request={})


def test_insufficient_cli_fixture_stays_valid(tmp_path, capsys):
    assert cli.main(["run", "--case", "insufficient", "--out-dir", str(tmp_path / "insufficient")]) == 0
    story = json.loads((tmp_path / "insufficient/story_plan.json").read_text())
    assert story["status"] == "needs_input" and story["unresolved_gaps"]


def test_default_cli_and_official_injection(tmp_path, capsys):
    out = tmp_path / "default"
    assert cli.main(["run", "--out-dir", str(out)]) == 0
    assert json.loads((out / "story_plan.json").read_text())["status"] == "ready_for_edit"
    assert cli.main(["inject", "--output", str(tmp_path / "injected.json")]) == 0


@pytest.mark.parametrize("field,value", [
    ("beats", None), ("beats", [None]), ("narration_cues", [None]),
    ("decision_provenance", []), ("unresolved_gaps", [None]),
])
def test_validator_returns_report_for_malformed_nested_json(docs, field, value):
    story = generate(docs, approved_cues=approved())
    story[field] = value
    report = validate(story, docs)
    assert report["ok"] is False
    assert report["errors"]


def test_validator_rejects_unhashable_ids_without_crashing(docs):
    story = generate(docs, approved_cues=approved())
    story["beats"][0]["shots"][0]["candidate_id"] = []
    assert not validate(story, docs)["ok"]
    assert not validate(None, docs)["ok"]


def test_disabled_broll_stays_disabled_when_only_external_candidates_remain(docs):
    # Keep the old query response to exercise the gap branch with stale policy.
    docs["Brief"]["requirements"]["broll_policy"]["enabled"] = False
    for result in docs["CandidateSet"]["results"]:
        if result["query_id"] == "query-middle":
            result["candidates"] = []
    story = generate(docs, approved_cues=approved())
    assert story["status"] == "needs_input"
    assert story["beats"][1]["shots"] == []
    assert validate(story, docs)["ok"]


def test_manifest_keyframe_times_reach_selector(docs):
    from quickcut.planning.planning import candidates_for_beat
    beat = build_outline(docs["Brief"])[0]
    candidates = candidates_for_beat(beat, docs["RetrievalRequest"], docs["CandidateSet"], docs["AssetManifest"])
    segment = next(s for s in docs["AssetManifest"]["segments"] if s["segment_id"] == candidates[0]["segment_id"])
    assert candidates[0]["keyframes"] == segment["keyframes"]
    assert candidates[0]["keyframes"][0]["source_time_us"] == 6_000_000


def test_custom_inject_requires_explicit_approval(tmp_path, capsys):
    raw, docs = load_bundle(cli.DEFAULT_BUNDLE)
    path = tmp_path / "custom.json"
    path.write_text(json.dumps(raw))
    output = tmp_path / "injected.json"
    assert cli.main(["inject", "--bundle", str(path), "--output", str(output)]) == 1
    assert not output.exists()
    assert cli.main(["inject", "--bundle", str(path), "--output", str(output),
                     "--approved-cues", str(cli.DEFAULT_APPROVED_CUES)]) == 0

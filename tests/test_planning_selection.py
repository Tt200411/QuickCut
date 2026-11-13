"""选镜边界回归：图片均临时生成，Ollama 调用完全离线。"""
from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from quickcut.planning.selection import (
    FixedResponseSelector,
    OllamaSelector,
    PlanningError,
    RuleSelector,
    Selection,
    validate_selection,
)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("测试不得访问真实 Ollama 或网络")
    monkeypatch.setattr("urllib.request.urlopen", unexpected)


@pytest.fixture
def beat():
    return {
        "beat_id": "beat-test", "project_id": "project-test", "purpose": "推进故事",
        "duration_budget_us": 10_000_000, "shot_needs": ["primary", "atmosphere"],
        "wish": {"event": "走入树林", "verified": False, "assertion": "user_wish"},
    }


def candidate(candidate_id="user-1", origin="user", **kwargs):
    return {
        "candidate_id": candidate_id, "segment_id": f"seg-{candidate_id}",
        "origin": origin, "duration_us": 10_000_000, "flags": [],
        "source_range_us": {"begin": 2_000_000, "end": 12_000_000},
        "keyframe_uris": [], **kwargs,
    }


def valid_entry():
    return {"shot_order": ["user-1"], "reasons": {"user-1": "用户事件证据"},
            "roles": {"user-1": "primary"}}


def mock_ollama(monkeypatch, body, *, raw=False):
    requests = []

    def respond(request, timeout):
        requests.append(json.loads(request.data))
        data = body if raw else json.dumps(body)
        return io.BytesIO(data.encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", respond)
    return requests


def assert_invalid(exc, beat, raw):
    error = exc.value
    assert error.code == "VLM_INVALID_PLAN"
    assert error.project_id == beat["project_id"]
    assert beat["beat_id"] in error.affected_ids
    assert error.detail["beat_id"] == beat["beat_id"]
    assert error.detail["raw_response"] == raw


@pytest.mark.parametrize("kind", ["absolute", "parent", "internal_parent", "symlink", "directory_symlink"])
def test_images_refuse_path_escape(tmp_path, kind):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.png"
    Image.new("RGB", (2, 2), "red").save(outside)
    if kind == "absolute":
        uri = str(outside)
    elif kind == "parent":
        uri = "../outside.png"
    elif kind == "internal_parent":
        Image.new("RGB", (2, 2)).save(root / "inside.png")
        (root / "subdir").mkdir()
        uri = "subdir/../inside.png"
    elif kind == "symlink":
        (root / "linked.png").symlink_to(outside)
        uri = "linked.png"
    else:
        (root / "linked").symlink_to(tmp_path, target_is_directory=True)
        uri = "linked/outside.png"
    selector = OllamaSelector(project_root=root)
    mapping = []
    assert selector._images([candidate(keyframe_uris=[uri])], image_mapping=mapping) == []
    assert mapping == []


def test_images_require_decodable_image_and_keep_internal_symlink(tmp_path):
    image = tmp_path / "real.png"
    Image.new("RGB", (3, 3), "blue").save(image)
    (tmp_path / "fake.png").write_text("不是图片", encoding="utf-8")
    (tmp_path / "truncated.png").write_bytes(image.read_bytes()[:40])
    (tmp_path / "link.png").symlink_to(image)
    uris = ["missing.png", "fake.png", "truncated.png", "link.png"]
    images = OllamaSelector(project_root=tmp_path)._images([candidate(keyframe_uris=uris)])
    assert images == [base64.b64encode(image.read_bytes()).decode("ascii")]


def test_partial_missing_images_have_correct_candidate_and_time_mapping(tmp_path, monkeypatch, beat):
    for name in ("first.png", "last.png"):
        Image.new("RGB", (2, 2)).save(tmp_path / name)
    candidates = [
        candidate(keyframe_uris=["missing.png"]),
        candidate("user-2", keyframe_uris=["missing.png", "first.png"],
                  keyframes=[{"uri": "first.png", "source_time_us": 4_000_000}]),
        candidate("user-3", keyframe_uris=["last.png"]),
    ]
    requests = mock_ollama(monkeypatch, {"response": json.dumps(valid_entry())})
    selection = OllamaSelector(project_root=tmp_path).select(
        beat=beat, candidates=candidates, context={"project_id": beat["project_id"]},
    )
    assert selection.ordered_candidate_ids == ["user-1"]
    payload = requests[0]
    prompt = json.loads(payload["prompt"])
    evidence = prompt["candidates"]
    assert [c["visual_evidence"] for c in evidence] == [False, True, True]
    assert [c["image_indices"] for c in evidence] == [[], [0], [1]]
    assert prompt["beat"]["wish"] == beat["wish"]
    assert prompt["image_mapping"] == [
        {"image_index": 0, "candidate_id": "user-2", "uri": "first.png",
         "source_time_us": 4_000_000, "source_range_us": candidates[1]["source_range_us"]},
        {"image_index": 1, "candidate_id": "user-3", "uri": "last.png",
         "source_time_us": None, "source_range_us": candidates[2]["source_range_us"]},
    ]
    assert len(payload["images"]) == 2


def test_without_root_or_valid_images_no_visual_evidence(monkeypatch, beat):
    requests = mock_ollama(monkeypatch, {"response": valid_entry()})
    OllamaSelector().select(beat=beat, candidates=[candidate(keyframe_uris=["fake.png"])], context={})
    assert "images" not in requests[0]
    evidence = json.loads(requests[0]["prompt"])["candidates"][0]
    assert evidence["visual_evidence"] is False
    assert evidence["image_indices"] == []


INVALID_ENTRIES = [
    None, [], "text", 42, True, {},
    {"shot_order": "user-1"}, {"shot_order": {}}, {"shot_order": None},
    {"shot_order": [1]}, {"shot_order": [[]]}, {"shot_order": [{}]},
    {"shot_order": [None]},
    {"shot_order": ["user-1"], "reasons": []},
    {"shot_order": ["user-1"], "reasons": None},
    {"shot_order": ["user-1"], "reasons": {"user-1": 1}},
    {"shot_order": ["user-1"], "roles": [["user-1", "primary"]]},
    {"shot_order": ["user-1"], "roles": {"user-1": []}},
]


@pytest.mark.parametrize("entry", INVALID_ENTRIES)
@pytest.mark.parametrize("backend", ["fixed", "ollama"])
def test_response_structure_is_strict(monkeypatch, beat, entry, backend):
    if backend == "fixed":
        raw = {"beats": {beat["beat_id"]: entry}}
        selector = FixedResponseSelector(raw)
    else:
        raw = json.dumps(entry)
        mock_ollama(monkeypatch, {"response": raw})
        selector = OllamaSelector()
    with pytest.raises(PlanningError) as exc:
        selector.select(beat=beat, candidates=[candidate()], context={"project_id": beat["project_id"]})
    assert_invalid(exc, beat, raw)


@pytest.mark.parametrize("payload", [[], None, "text", {"beats": []}, {"beats": None}, {"beats": {}}])
def test_fixed_requires_objects_at_each_level(beat, payload):
    with pytest.raises(PlanningError) as exc:
        FixedResponseSelector(payload).select(beat=beat, candidates=[], context={})
    assert_invalid(exc, beat, payload)


@pytest.mark.parametrize("field", ["reasons", "roles"])
def test_fixed_rejects_non_string_dictionary_keys(beat, field):
    entry = {**valid_entry(), field: {1: "text"}}
    payload = {"beats": {beat["beat_id"]: entry}}
    with pytest.raises(PlanningError) as exc:
        FixedResponseSelector(payload).select(beat=beat, candidates=[], context={})
    assert_invalid(exc, beat, payload)


def test_fixed_malformed_json_keeps_context_and_raw(tmp_path, beat):
    path = tmp_path / "response.json"
    path.write_text('{"beats":', encoding="utf-8")
    selector = FixedResponseSelector.from_path(path)
    with pytest.raises(PlanningError) as exc:
        selector.select(beat=beat, candidates=[], context={"project_id": beat["project_id"]})
    assert_invalid(exc, beat, path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("body", ["{broken", "[]", "null", '{"response":"{broken"}', "{}",
                                  '{"response":[["shot_order",["user-1"]]]}'])
def test_ollama_malformed_json_and_envelopes(monkeypatch, beat, body):
    mock_ollama(monkeypatch, body, raw=True)
    with pytest.raises(PlanningError) as exc:
        OllamaSelector().select(beat=beat, candidates=[candidate()], context={"project_id": beat["project_id"]})
    error = exc.value
    assert error.code == "VLM_INVALID_PLAN"
    assert error.project_id == beat["project_id"]
    assert error.detail["beat_id"] == beat["beat_id"]
    assert "raw_response" in error.detail


@pytest.mark.parametrize("origin", ["external", "generated"])
def test_non_user_primary_is_rejected(beat, origin):
    selection = Selection(beat["beat_id"], ["user-1", "other"], raw_response=valid_entry(),
                          role_overrides={"other": "primary"})
    with pytest.raises(PlanningError) as exc:
        validate_selection(selection, beat=beat, candidates=[candidate(), candidate("other", origin)])
    assert_invalid(exc, beat, selection.raw_response)


@pytest.mark.parametrize("origin", ["external", "generated"])
def test_non_user_alone_does_not_prove_event(beat, origin):
    with pytest.raises(PlanningError) as exc:
        validate_selection(Selection(beat["beat_id"], ["other"]), beat=beat,
                           candidates=[candidate("other", origin)])
    assert exc.value.code == "UNSUPPORTED_STORY_BEAT"


@pytest.mark.parametrize("origin", ["user", "external", "generated"])
def test_bad_evidence_is_rejected_for_all_origins(beat, origin):
    selection = Selection(beat["beat_id"], ["user-1", "other"])
    with pytest.raises(PlanningError) as exc:
        validate_selection(selection, beat=beat,
                           candidates=[candidate(), candidate("other", origin, flags=["bad_evidence"])])
    assert_invalid(exc, beat, None)


@pytest.mark.parametrize("order", [[[]], [{}], [1], [None], "user-1", None, ["user-1", "user-1"], ["unknown"]])
def test_validate_defends_candidate_ids(beat, order):
    with pytest.raises(PlanningError) as exc:
        validate_selection(Selection(beat["beat_id"], order), beat=beat, candidates=[candidate()])
    assert_invalid(exc, beat, None)


@pytest.mark.parametrize("duration", [-1, 0, True, 1.5, "100", None])
@pytest.mark.parametrize("target", ["candidate", "beat"])
def test_duration_must_be_strict_positive_integer(beat, duration, target):
    item = candidate()
    if target == "candidate":
        item["duration_us"] = duration
    else:
        beat["duration_budget_us"] = duration
    with pytest.raises(PlanningError) as exc:
        validate_selection(Selection(beat["beat_id"], ["user-1"]), beat=beat, candidates=[item])
    assert_invalid(exc, beat, None)


@pytest.mark.parametrize("ratio", [-0.1, 1.1, float("nan"), float("inf"), True, "0.4", None])
def test_invalid_broll_ratio_is_rejected(beat, ratio):
    with pytest.raises(PlanningError) as exc:
        validate_selection(Selection(beat["beat_id"], ["user-1"]), beat=beat,
                           candidates=[candidate()], max_broll_ratio=ratio)
    assert_invalid(exc, beat, None)


@pytest.mark.parametrize("origin", ["external", "generated"])
def test_non_user_broll_stays_non_factual_and_respects_budget(beat, origin):
    selection = Selection(beat["beat_id"], ["other", "user-1"],
                          role_overrides={"other": "atmosphere"})
    records, _ = validate_selection(selection, beat=beat,
                                   candidates=[candidate(), candidate("other", origin)])
    assert [r["role"] for r in records] == ["atmosphere", "primary"]
    assert all(type(r["suggested_duration_us"]) is int and r["suggested_duration_us"] > 0 for r in records)
    assert records[0]["suggested_duration_us"] <= beat["duration_budget_us"] * 0.4
    assert sum(r["suggested_duration_us"] for r in records) <= beat["duration_budget_us"]


@pytest.mark.parametrize("policy", ["enabled", "context_allowed", "beat_allowed"])
@pytest.mark.parametrize("backend", ["rule", "fixed", "ollama"])
def test_selectors_enforce_disabled_broll(monkeypatch, beat, policy, backend):
    context = {"project_id": beat["project_id"], "broll_enabled": True}
    if policy == "enabled":
        context["broll_enabled"] = False
    elif policy == "context_allowed":
        context["broll_allowed"] = False
    else:
        beat["broll_allowed"] = False
    candidates = [candidate(), candidate("other", "external")]
    entry = {"shot_order": ["user-1", "other"], "roles": {"other": "atmosphere"}}
    if backend == "rule":
        assert RuleSelector().select(beat=beat, candidates=candidates, context=context).ordered_candidate_ids == ["user-1"]
        return
    if backend == "fixed":
        selector = FixedResponseSelector({"beats": {beat["beat_id"]: entry}})
    else:
        requests = mock_ollama(monkeypatch, {"response": json.dumps(entry)})
        selector = OllamaSelector()
    with pytest.raises(PlanningError) as exc:
        selector.select(beat=beat, candidates=candidates, context=context)
    assert exc.value.code == "VLM_INVALID_PLAN"
    if backend == "ollama":
        prompt = json.loads(requests[0]["prompt"])
        assert prompt["beat"]["broll_allowed"] is False
        assert prompt["candidates"][1]["selectable"] is False


def test_legacy_fixed_response_optional_maps_still_work(beat):
    selector = FixedResponseSelector({"beats": {beat["beat_id"]: {"shot_order": ["user-1"]}}})
    selection = selector.select(beat=beat, candidates=[candidate()], context={})
    records, _ = validate_selection(selection, beat=beat, candidates=[candidate()])
    assert selection.source == "fixture:fixed_model_response"
    assert records[0]["role"] == "primary"


def test_ollama_empty_candidates_avoids_network(beat):
    selection = OllamaSelector().select(beat=beat, candidates=[], context={})
    assert selection.ordered_candidate_ids == []

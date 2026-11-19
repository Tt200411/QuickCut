"""Offline contract tests; real model acceptance is a separate recorded run."""
import copy
import hashlib
import io
import json

import pytest
from PIL import Image

from quickcut.planning import build_outline, build_retrieval_request, plan, OllamaSelector
from quickcut.planning.contracts import envelope, ref, PlanningError, content_hash
from quickcut.planning.production import generate_draft, confirm_draft, preview_selections
from quickcut.planning.validator import validate_story_plan


@pytest.fixture
def inputs(tmp_path):
    brief = envelope("Brief", "brief-live", "project-live", 1, fixtures_only=False, owner="C", inputs=[])
    brief["requirements"] = {
        "story": "林间光影，沿路风景，轻松收束", "target_duration_us": 30_000_000,
        "language": "zh-CN", "picture_policy": "thematic_montage",
        "narration": {"enabled": True}, "broll_policy": {"enabled": False, "roles": []},
    }
    manifest = envelope("AssetManifest", "assets-live", "project-live", 1, fixtures_only=False, owner="A", inputs=[])
    manifest.update(assets=[], segments=[])
    for i, origin in enumerate(["user", "external", "user"]):
        Image.new("RGB", (8, 8), ["red", "blue", "green"][i]).save(tmp_path / f"frame-{i}.png")
        transcript = {"status": "success", "text": f"原始转写{i}",
                      "segments": [{"source_range_us": {"begin": 0, "end": 1_000_000}, "text": f"原始转写{i}"}],
                      "review_status": "needs_review", "uri": f"asr-{i}.json"}
        manifest["assets"].append({"asset_id": f"asset-{i}", "revision": 1, "origin": origin,
                                   "uri": f"source-{i}.mp4", "duration_us": 15_000_000,
                                   "transcript": {"review": {"status": "needs_review", "reasons": ["low_confidence"]}}})
        manifest["segments"].append({"segment_id": f"segment-{i}", "asset_id": f"asset-{i}", "asset_revision": 1,
                                     "source_range_us": {"begin": 0, "end": 15_000_000}, "transcript": transcript,
                                     "keyframes": [{"uri": f"frame-{i}.png", "source_time_us": 100_000}]})
    request = build_retrieval_request(brief, manifest, build_outline(brief), broll_queries=False)
    candidates = envelope("CandidateSet", "candidates-live", "project-live", 1, fixtures_only=False, owner="B",
                          inputs=[ref(manifest), ref(request)])
    candidates["status"] = "success"
    candidates["results"] = [
        {"query_id": q["query_id"], "status": "ok", "candidates": [{
            "candidate_id": f"candidate-{i}", "segment_id": f"segment-{i}", "asset_revision": 1,
            "ranking_score": 0.9, "evidence": ["actual_keyframe"],
            "transcript_evidence": copy.deepcopy(manifest["segments"][i]["transcript"]),
        }]} for i, q in enumerate(request["queries"])
    ]
    return brief, manifest, candidates, request


@pytest.fixture
def model(monkeypatch):
    requests = []
    def respond(request, timeout):
        payload = json.loads(request.data)
        prompt = json.loads(payload["prompt"])
        requests.append(payload)
        cid = next(c["candidate_id"] for c in prompt["candidates"] if c["selectable"])
        answer = {"shot_order": [cid], "roles": {cid: "atmosphere"}, "reasons": {cid: "与主题一致"},
                  "narration_draft": "光影在路边缓缓流动。"}
        return io.BytesIO(json.dumps({"model": payload["model"], "done": True, "response": json.dumps(answer)}).encode())
    monkeypatch.setattr("quickcut.planning.selection.open_ollama", respond)
    return requests


def draft_for(inputs, tmp_path):
    brief, manifest, candidates, request = inputs
    return generate_draft(brief, manifest, candidates, retrieval_request=request, project_root=tmp_path)


def confirm(draft):
    return confirm_draft(draft, approved_text_by_beat={b["beat_id"]: b["narration_draft"] for b in draft["beats"]},
                         reviewer="测试确认者", reviewed_at="2026-11-16T10:00:00+08:00")


def _rehash_draft(draft):
    draft.pop("draft_id", None)
    draft["draft_id"] = content_hash(draft)


def test_legacy_single_sentence_draft_keeps_frozen_three_beats(inputs, tmp_path, model, monkeypatch):
    brief, manifest, candidates, _ = inputs
    # Historical C used three beats even for this one-clause brief. Its actual
    # request, candidate documents and model responses remain the confirmation inputs.
    legacy_outline = build_outline(brief)
    brief["requirements"]["story"] = "林间风景"
    for beat, event in zip(legacy_outline, ["林间风景（建立出发情境）", "林间风景", "林间风景（完成情绪收束）"]):
        beat["wish"]["event"] = event
    request = build_retrieval_request(brief, manifest, legacy_outline, broll_queries=False)
    with monkeypatch.context() as patch:
        patch.setattr("quickcut.planning.production.build_outline", lambda _: copy.deepcopy(legacy_outline))
        draft = draft_for((brief, manifest, candidates, request), tmp_path)
    draft.pop("outline")  # Old files did not have the new explicit outline field.
    for beat, limit in zip(draft["beats"], [23, 35, 10]):
        beat["narration_max_chars"] = limit
    _rehash_draft(draft)
    frozen = copy.deepcopy(draft)
    assert [b["beat_id"] for b in build_outline(brief)] == ["beat-01"]

    def no_regeneration(*args, **kwargs):
        raise AssertionError("Confirmation must not rebuild a frozen outline or rerun the model")
    monkeypatch.setattr("quickcut.planning.planning.build_outline", no_regeneration)
    monkeypatch.setattr("quickcut.planning.validator.build_outline", no_regeneration)
    monkeypatch.setattr("quickcut.planning.selection.open_ollama", no_regeneration)
    texts = {beat["beat_id"]: "林间风景静静展开。" for beat in draft["beats"]}
    result = confirm_draft(draft, approved_text_by_beat=texts, reviewer="离线回放审核",
                           reviewed_at="2026-11-19T10:00:00+08:00", confirmation_kind="assistant_review",
                           plan_revision=2)
    story = result.story_plan
    assert story["status"] == "ready_for_edit"
    assert story["revision"] == 2
    assert [beat["beat_id"] for beat in story["beats"]] == ["beat-open", "beat-middle", "beat-close"]
    assert [beat["wish"] for beat in story["decision_provenance"]["frozen_outline"]] == [beat["wish"] for beat in legacy_outline]
    assert story["decision_provenance"]["input_hashes"] == frozen["input_hashes"]
    assert result.run["raw_responses"] == frozen["model_calls"]
    assert draft == frozen
    assert len(model) == 3
    assert validate_story_plan(story, brief=brief, manifest=manifest, candidate_set=candidates,
                               upstream=draft["inputs"])["ok"]
    texts["beat-close"] = "这句话比原来保存的十字预算更长。"
    longer = confirm_draft(draft, approved_text_by_beat=texts, reviewer="离线回放审核",
                           reviewed_at="2026-11-19T10:00:00+08:00")
    assert longer.story_plan["narration_cues"][-1]["text"] == texts["beat-close"]
    assert draft == frozen  # Legacy estimated limits remain frozen, but are advisory.


@pytest.mark.parametrize("field,value", [("beat_id", "beat-forged"), ("retrieval_query_id", "query-forged"),
                                         ("duration_budget_us", 10_000_001), ("order", 1),
                                         ("wish", {"event": "伪造主题", "assertion": "user_wish", "verified": False})])
def test_frozen_outline_cannot_override_upstream_even_with_new_draft_hash(inputs, tmp_path, model, field, value):
    draft = draft_for(inputs, tmp_path)
    draft["outline"][0][field] = value
    _rehash_draft(draft)
    with pytest.raises(PlanningError, match="冻结大纲"):
        confirm(draft)


def test_validator_checks_frozen_outline_budget_and_request_identity(inputs, tmp_path, model):
    draft = draft_for(inputs, tmp_path)
    result = confirm(draft)
    for field, value in [("duration_budget_us", 10_000_001), ("beat_id", "beat-forged")]:
        story = copy.deepcopy(result.story_plan)
        story["decision_provenance"]["frozen_outline"][0][field] = value
        report = validate_story_plan(story, brief=inputs[0], manifest=inputs[1], candidate_set=inputs[2],
                                     upstream=draft["inputs"])
        assert not report["ok"]
        assert any("冻结大纲" in error for error in report["errors"])
    # Matching outline/query edits cannot grant extra time beyond the frozen Brief.
    draft["outline"][0]["duration_budget_us"] += 1
    draft["inputs"]["RetrievalRequest"]["queries"][0]["duration_budget_us"] += 1
    draft["input_hashes"]["RetrievalRequest"] = content_hash(draft["inputs"]["RetrievalRequest"])
    _rehash_draft(draft)
    with pytest.raises(PlanningError, match="冻结大纲"):
        confirm(draft)


@pytest.mark.parametrize("story_text,durations,expected_limits,expected_counts", [
    ("林间风景", [10, 10, 10, 10], [3], [3]),
    ("林间光影，沿路风景，轻松收束", [10, 10, 10, 10], [2, 2, 1], [1, 2, 1]),
    ("林间风景", [30, 10, 10, 10], [3], [3]),
])
def test_selector_capacity_covers_thirty_seconds_without_changing_short_beats(
        inputs, tmp_path, monkeypatch, story_text, durations, expected_limits, expected_counts):
    brief, manifest, _, _ = inputs
    brief["requirements"]["story"] = story_text
    fourth_asset, fourth_segment = copy.deepcopy(manifest["assets"][0]), copy.deepcopy(manifest["segments"][0])
    fourth_asset.update(asset_id="asset-3", uri="source-3.mp4")
    fourth_segment.update(segment_id="segment-3", asset_id="asset-3")
    manifest["assets"].append(fourth_asset)
    manifest["segments"].append(fourth_segment)
    for asset, duration in zip(manifest["assets"], durations):
        asset["duration_us"] = duration * 1_000_000
    for segment, duration in zip(manifest["segments"], durations):
        segment["source_range_us"] = {"begin": 0, "end": duration * 1_000_000}
    request = build_retrieval_request(brief, manifest, build_outline(brief), broll_queries=False)
    candidates = envelope("CandidateSet", "candidates-live", "project-live", 1, fixtures_only=False, owner="B",
                          inputs=[ref(manifest), ref(request)])
    candidates.update(status="success", results=[
        {"query_id": query["query_id"], "status": "ok", "candidates": [
            {"candidate_id": f"q{i}-candidate-{j}", "segment_id": segment["segment_id"], "asset_revision": 1,
             "ranking_score": 0.9 - j * .01, "evidence": ["actual_keyframe"]}
            for j, segment in enumerate(manifest["segments"])]}
        for i, query in enumerate(request["queries"])])
    payloads = []
    def respond(request, timeout):
        payload = json.loads(request.data)
        payloads.append(payload)
        prompt = json.loads(payload["prompt"])
        count = (prompt["beat"]["duration_budget_us"] + 9_999_999) // 10_000_000
        # The model prefers the shorter clips even when one long clip could
        # cover the entire beat. The schema must permit that genuine choice.
        ids = [c["candidate_id"] for c in prompt["candidates"] if c["selectable"]
               and c["source_range_us"]["end"] - c["source_range_us"]["begin"] == 10_000_000][:count]
        answer = {"shot_order": ids, "roles": {cid: "atmosphere" for cid in ids},
                  "reasons": {cid: "画面符合主题" for cid in ids}, "narration_draft": "林间风景静静展开。"}
        return io.BytesIO(json.dumps({"model": payload["model"], "done": True, "response": json.dumps(answer)}).encode())
    monkeypatch.setattr("quickcut.planning.selection.open_ollama", respond)
    draft = draft_for((brief, manifest, candidates, request), tmp_path)
    assert [p["format"]["properties"]["shot_order"]["maxItems"] for p in payloads] == expected_limits
    assert [len(b["selected_candidate_ids"]) for b in draft["beats"]] == expected_counts
    if durations[0] == 30:
        assert draft["beats"][0]["selected_candidate_ids"] == ["q0-candidate-1", "q0-candidate-2", "q0-candidate-3"]
    result = confirm(draft)
    assert result.story_plan["status"] == "ready_for_edit"
    assert sum(shot["suggested_duration_us"] for beat in result.story_plan["beats"] for shot in beat["shots"]) == 30_000_000
    assert all(shot["decided_by"].startswith("model:") for beat in result.story_plan["beats"] for shot in beat["shots"])
    assert len(payloads) == len(expected_counts)  # Confirmation makes no extra calls.


def test_live_draft_requires_confirmation_and_preserves_model_evidence(inputs, tmp_path, model):
    draft = draft_for(inputs, tmp_path)
    assert draft["status"] == "needs_review"
    assert draft["human_review_performed"] is False
    assert len(model) == len(draft["model_calls"]) == 3
    assert draft["beats"][1]["planned_shots"][0]["suggested_duration_us"] == 15_000_000
    call = draft["model_calls"][0]["raw_response"]
    assert call["actual_backend"] == "ollama"
    assert call["request"]["image_mapping"][0]["source_time_us"] == 100_000
    assert call["request"]["source_frame_sha256"] == [hashlib.sha256((tmp_path / "frame-0.png").read_bytes()).hexdigest()]
    assert call["request"]["visual_mode"] == "numbered_contact_sheet"
    assert len(call["request"]["payload"]["images"]) == 1
    assert call["request"]["payload"]["options"]["num_ctx"] == 8192
    assert call["request"]["payload"]["options"]["num_predict"] == 1024
    schema = call["request"]["payload"]["format"]
    assert schema["required"] == ["shot_order", "reasons", "roles", "narration_draft"]
    assert schema["properties"]["shot_order"]["items"]["enum"] == ["C01"]
    assert call["request"]["candidate_aliases"] == {"C01": "candidate-0"}
    assert call["parsed"]["shot_order"] == ["C01"]
    assert call["canonicalized"]["shot_order"] == ["candidate-0"]
    assert json.loads(call["body"]["response"])["shot_order"] == ["C01"]
    assert model[0]["format"]["properties"]["shot_order"]["maxItems"] == 1  # Only one supplied candidate.
    assert model[2]["format"]["properties"]["shot_order"]["maxItems"] == 1  # Five-second closing beat.
    prompt = json.loads(call["request"]["payload"]["prompt"])
    assert prompt["candidates"][0]["transcript_evidence"]["segments"][0]["source_range_us"]["end"] == 1_000_000
    assert prompt["candidates"][0]["transcript_evidence"]["review_status"] == "needs_review"
    assert len(list((tmp_path / "planning-evidence").glob("*.json"))) == 3
    result = confirm(draft)
    assert len(model) == 3  # Confirmation does not rerun or invent inference.
    assert result.story_plan["status"] == "ready_for_edit"
    shots = [b["shots"][0] for b in result.story_plan["beats"]]
    assert [s["origin"] for s in shots] == ["user", "external", "user"]
    assert all(s["role"] != "primary" and s["decided_by"].startswith("model:") for s in shots)
    assert shots[0]["transcript_evidence"]["review_status"] == "needs_review"
    assert all(not c["asserts_specific_user_event"] for c in result.story_plan["narration_cues"])
    assert len(result.story_plan["decision_provenance"]["model_call_ids"]) == 3
    assert result.run["approval_records"]["beat-open"]["text_hash"] == content_hash("光影在路边缓缓流动。")


def test_confirmation_rejects_tampered_draft_and_missing_reviewer(inputs, tmp_path, model):
    draft = draft_for(inputs, tmp_path)
    altered = copy.deepcopy(draft)
    altered["inputs"]["Brief"]["requirements"]["story"] = "changed without revision bump"
    with pytest.raises(PlanningError, match="草案已变化"):
        confirm(altered)
    with pytest.raises(PlanningError, match="确认者"):
        confirm_draft(draft, approved_text_by_beat={}, reviewer="", reviewed_at="2026-11-16T10:00:00+08:00")


def test_validator_rejects_changed_approved_text_and_missing_picture_confirmation(inputs, tmp_path, model):
    result = confirm(draft_for(inputs, tmp_path))
    story = copy.deepcopy(result.story_plan)
    story["narration_cues"][0]["text"] = "确认后偷偷替换"
    story["beats"][0]["shots"][0].pop("montage_review")
    brief, manifest, candidates, request = inputs
    report = validate_story_plan(story, brief=brief, manifest=manifest, candidate_set=candidates,
                                 upstream={"Brief": brief, "AssetManifest": manifest, "CandidateSet": candidates,
                                           "RetrievalRequest": request})
    assert not report["ok"]
    assert any("用户确认" in e for e in report["errors"])
    assert any("当前文本" in e for e in report["errors"])


def test_missing_visuals_and_retrieval_errors_are_not_rule_fallback(inputs, tmp_path, model):
    (tmp_path / "frame-0.png").unlink()
    with pytest.raises(PlanningError, match="没有可读取的关键帧"):
        draft_for(inputs, tmp_path)
    assert not model
    inputs[2]["status"] = "error"
    with pytest.raises(PlanningError, match="B 检索失败"):
        draft_for(inputs, tmp_path)


def test_explicit_ollama_external_montage_calls_model(inputs, tmp_path, model):
    brief, manifest, candidates, request = inputs
    brief["requirements"]["picture_policy"] = "authorized_external_montage"
    for asset in manifest["assets"]:
        asset["origin"] = "external"
    for query in request["queries"]:
        query["allowed_origins"] = ["external"]
    result = plan(brief, manifest, candidates, retrieval_request=request,
                  selector=OllamaSelector(project_root=tmp_path),
                  approved_cues={b["beat_id"]: "确认后的主题旁白" for b in build_outline(brief)})
    assert len(model) == 3
    assert len(result.run["raw_responses"]) == 3
    assert all(b["shots"][0]["decided_by"].startswith("model:") for b in result.story_plan["beats"])
    assert result.story_plan["decision_provenance"]["input_hashes"]["Brief"] == content_hash(brief)
    assert result.story_plan["decision_provenance"]["input_hashes"]["Brief"] != content_hash(ref(brief))


def test_assistant_confirmation_is_not_recorded_as_human(inputs, tmp_path, model):
    draft = draft_for(inputs, tmp_path)
    result = confirm_draft(draft, approved_text_by_beat={b["beat_id"]: b["narration_draft"] for b in draft["beats"]},
                           reviewer="Codex visual acceptance", reviewed_at="2026-11-16T10:00:00+08:00",
                           confirmation_kind="assistant_review")
    assert result.run["human_review_performed"] is False
    assert result.story_plan["decision_provenance"]["human_review_performed"] is False
    assert result.story_plan["beats"][0]["shots"][0]["montage_review"]["source"] == "assistant_montage_review"
    assert result.story_plan["narration_cues"][0]["text_source"] == "assistant_review"


def test_close_narration_estimate_does_not_block_complete_text_before_tts(inputs, tmp_path, model):
    draft = draft_for(inputs, tmp_path)
    assert draft["beats"][-1]["narration_max_chars"] == 10
    texts = {b["beat_id"]: b["narration_draft"] for b in draft["beats"]}
    texts["beat-close"] = "这是一段远远超过五秒钟朗读预算的过长中文旁白。"
    result = confirm_draft(draft, approved_text_by_beat=texts, reviewer="测试者", reviewed_at="2026-11-16T10:00:00+08:00")
    assert result.story_plan["narration_cues"][-1]["text"] == texts["beat-close"]


def test_failed_real_request_keeps_replayable_input_evidence(inputs, tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("local inference timed out")
    monkeypatch.setattr("quickcut.planning.selection.open_ollama", timeout)
    with pytest.raises(PlanningError, match="本地 Ollama"):
        draft_for(inputs, tmp_path)
    archived = list((tmp_path / "planning-evidence").glob("*.json"))
    assert len(archived) == 1
    record = json.loads(archived[0].read_text())
    assert record["status"] == "failed"
    assert record["inference_executed"] is False
    assert record["request"]["payload"]["images"]
    assert record["request"]["source_frame_sha256"]
    assert "local inference timed out" in record["error"]


def test_local_ollama_transport_bypasses_system_and_environment_proxy(monkeypatch):
    from quickcut.planning import selection
    import urllib.request
    captured = {}
    class Opener:
        def open(self, request, *, timeout):
            captured.update(url=request.full_url, timeout=timeout)
            return "direct"
    def build(handler):
        captured["proxies"] = handler.proxies
        return Opener()
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:9999")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:9999")
    monkeypatch.setattr(urllib.request, "build_opener", build)
    assert selection.open_ollama(urllib.request.Request("http://127.0.0.1:11434/api/generate"), timeout=120) == "direct"
    assert captured == {"proxies": {}, "url": "http://127.0.0.1:11434/api/generate", "timeout": 120}


def test_production_deduplicates_same_segment_from_multiple_queries(inputs, tmp_path, model):
    brief, manifest, candidates, request = inputs
    primary = request["queries"][1]
    extra = {**primary, "query_id": "query-middle-detail", "role": "detail"}
    request["queries"].append(extra)
    alias = copy.deepcopy(candidates["results"][1]["candidates"][0])
    alias.update(candidate_id="candidate-middle-alias", ranking_score=0.8)
    candidates["results"].append({"query_id": extra["query_id"], "status": "ok", "candidates": [alias]})
    draft = draft_for(inputs, tmp_path)
    middle = draft["beats"][1]
    assert len(middle["candidates"]) == 1
    assert middle["candidates"][0]["retrieval_aliases"] == [{"candidate_id": "candidate-middle-alias", "query_id": "query-middle-detail"}]
    sent = json.loads(model[1]["prompt"])["candidates"]
    assert [c["candidate_id"] for c in sent] == ["C01"]
    assert draft["model_calls"][1]["raw_response"]["request"]["candidate_aliases"] == {"C01": "candidate-1"}
    assert confirm(draft).story_plan["status"] == "ready_for_edit"


def test_montage_selection_rejects_different_ids_for_same_source_segment(inputs):
    from quickcut.planning.selection import Selection, validate_selection
    from quickcut.planning.planning import candidates_for_beat
    brief, manifest, candidates, request = inputs
    beat = build_outline(brief)[1]
    item = candidates_for_beat(beat, request, candidates, manifest)[0]
    alias = {**item, "candidate_id": "same-source-alias"}
    selection = Selection(beat["beat_id"], [item["candidate_id"], alias["candidate_id"]], source="model:qwen2.5vl:3b")
    with pytest.raises(PlanningError, match="重复使用同一源片段"):
        validate_selection(selection, beat=beat, candidates=[item, alias], thematic_montage=True)


def test_reconfirm_frozen_draft_produces_new_story_revision_without_inference(inputs, tmp_path, model):
    draft = draft_for(inputs, tmp_path)
    first = confirm(draft)
    texts = {b["beat_id"]: b["narration_draft"] for b in draft["beats"]}
    texts["beat-close"] = "光影渐渐收束。"
    revised = confirm_draft(draft, approved_text_by_beat=texts, reviewer="测试确认者",
                            reviewed_at="2026-11-16T10:05:00+08:00", plan_revision=2)
    assert revised.story_plan["revision"] == revised.run["revision"] == 2
    assert revised.story_plan["document_id"] == first.story_plan["document_id"]
    assert revised.story_plan["narration_cues"][-1]["text"] == "光影渐渐收束。"
    assert revised.run["approval_records"]["beat-close"]["text_hash"] == content_hash("光影渐渐收束。")
    assert len(model) == 3


@pytest.mark.parametrize("revision", [0, -1, True, False, 1.0, "2", None])
def test_confirmation_rejects_invalid_story_revision(inputs, tmp_path, model, revision):
    draft = draft_for(inputs, tmp_path)
    with pytest.raises(PlanningError, match="plan_revision 必须为正整数"):
        confirm_draft(draft, approved_text_by_beat={b["beat_id"]: b["narration_draft"] for b in draft["beats"]},
                      reviewer="测试确认者", reviewed_at="2026-11-16T10:00:00+08:00", plan_revision=revision)


def test_http_failure_keeps_ollama_error_body(inputs, tmp_path, monkeypatch):
    import urllib.error
    body = b'{"error":"request exceeds available context"}'
    def reject(request, *, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(body))
    monkeypatch.setattr("quickcut.planning.selection.open_ollama", reject)
    with pytest.raises(PlanningError, match="request exceeds available context"):
        draft_for(inputs, tmp_path)
    record = json.loads(next((tmp_path / "planning-evidence").glob("*.json")).read_text())
    assert record["raw_body"] == body.decode()
    assert record["request"]["payload"]["options"]["num_ctx"] == 8192


def add_alternative(inputs):
    _, manifest, candidates, _ = inputs
    asset = copy.deepcopy(manifest["assets"][0])
    asset.update(asset_id="asset-alternative", origin="external", uri="alternative.mp4")
    segment = copy.deepcopy(manifest["segments"][0])
    segment.update(segment_id="segment-alternative", asset_id="asset-alternative")
    candidate = copy.deepcopy(candidates["results"][0]["candidates"][0])
    candidate.update(candidate_id="candidate-alternative", segment_id="segment-alternative", ranking_score=0.7)
    manifest["assets"].append(asset)
    manifest["segments"].append(segment)
    candidates["results"][0]["candidates"].append(candidate)


@pytest.mark.parametrize("kind,source", [("human", "human_edit"), ("assistant_review", "assistant_edit")])
def test_replacement_and_reorder_preserve_actual_model_call(inputs, tmp_path, model, kind, source):
    add_alternative(inputs)
    draft = draft_for(inputs, tmp_path)
    before = copy.deepcopy(draft)
    overrides = {"beat-open": ["candidate-alternative", "candidate-0"]}
    preview = preview_selections(draft, overrides)
    assert [s["suggested_duration_us"] for s in preview["beat-open"]["planned_shots"]] == [5_000_000, 5_000_000]
    result = confirm_draft(draft, approved_text_by_beat={b["beat_id"]: b["narration_draft"] for b in draft["beats"]},
                           reviewer="实际确认者", reviewed_at="2026-11-16T11:00:00+08:00", plan_revision=2,
                           confirmation_kind=kind, selection_overrides=overrides)
    shots = result.story_plan["beats"][0]["shots"]
    assert [s["candidate_id"] for s in shots] == overrides["beat-open"]
    assert [s["origin"] for s in shots] == ["external", "user"]
    assert all(s["decided_by"] == source for s in shots)
    assert result.run["raw_responses"] == before["model_calls"]
    assert result.story_plan["decision_provenance"]["model_call_ids"] == [content_hash(c) for c in before["model_calls"]]
    assert result.run["selection_edits"][0]["source"] == source
    assert result.run["selection_edits"][0]["before"] == ["candidate-0"]
    assert draft == before
    assert len(model) == 3


@pytest.mark.parametrize("overrides", [[], {"unknown": ["candidate-0"]}, {"beat-open": []},
                                      {"beat-open": ["candidate-1"]}, {"beat-open": ["candidate-0", "candidate-0"]}])
def test_selection_override_rejects_unfrozen_invalid_or_duplicate_ids(inputs, tmp_path, model, overrides):
    draft = draft_for(inputs, tmp_path)
    with pytest.raises(PlanningError):
        preview_selections(draft, overrides)


def test_selection_override_rejects_segment_used_by_another_beat(inputs, tmp_path, model):
    # The same alternative is returned by B for two beats but was not selected by Qwen.
    add_alternative(inputs)
    candidates = inputs[2]
    candidate = copy.deepcopy(candidates["results"][0]["candidates"][-1])
    candidate["candidate_id"] = "alternative-middle"
    candidates["results"][1]["candidates"].append(candidate)
    draft = draft_for(inputs, tmp_path)
    with pytest.raises(PlanningError, match="不同段落不能重复"):
        preview_selections(draft, {"beat-open": ["candidate-alternative"], "beat-middle": ["alternative-middle"]})


def test_incomplete_model_format_retries_once_with_two_real_request_records(inputs, tmp_path, model, monkeypatch):
    from quickcut.planning import selection
    original = selection.open_ollama
    attempts = []
    def first_incomplete(request, *, timeout):
        attempts.append(json.loads(request.data))
        if len(attempts) == 1:
            return io.BytesIO(json.dumps({"model": "qwen2.5vl:3b", "done": True,
                                          "response": json.dumps({"narration_draft": "林间光影。"})}).encode())
        return original(request, timeout=timeout)
    monkeypatch.setattr(selection, "open_ollama", first_incomplete)
    draft = draft_for(inputs, tmp_path)
    assert len(attempts) == 4
    assert len(draft["model_attempts"]) == 4
    assert draft["model_attempts"][0]["status"] == "invalid_model_format"
    assert draft["model_attempts"][1]["format_attempt"] == 2
    assert "format_repair" in json.loads(attempts[1]["prompt"])
    assert len(list((tmp_path / "planning-evidence").glob("*.json"))) == 4
    assert confirm(draft).story_plan["decision_provenance"]["actual_model_attempt_count"] == 4


def test_repeated_bad_model_format_stops_after_two_requests(inputs, tmp_path, monkeypatch):
    requests = []
    def always_incomplete(request, *, timeout):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"done": True, "response": '{"narration_draft":"林间光影。"}'}).encode())
    monkeypatch.setattr("quickcut.planning.selection.open_ollama", always_incomplete)
    with pytest.raises(PlanningError) as caught:
        draft_for(inputs, tmp_path)
    assert len(requests) == 2
    assert len(caught.value.detail["attempts"]) == 2
    assert len(list((tmp_path / "planning-evidence").glob("*.json"))) == 2


def test_short_alias_prompt_bounds_text_but_keeps_full_asr_archive(inputs, tmp_path, model):
    transcript = inputs[1]["segments"][0]["transcript"]
    transcript["text"] = "真实语音转写" * 100
    transcript["segments"] = [{"source_range_us": {"begin": i * 1_000_000, "end": (i + 1) * 1_000_000},
                               "text": "真实语音转写" * 30} for i in range(10)]
    inputs[2]["results"][0]["candidates"][0]["transcript_evidence"] = copy.deepcopy(transcript)
    draft = draft_for(inputs, tmp_path)
    call = draft["model_calls"][0]["raw_response"]
    prompt = json.loads(call["request"]["payload"]["prompt"])
    compact = prompt["candidates"][0]["transcript_evidence"]
    assert len(compact["text"]) == 240
    assert len(compact["segments"]) == 3
    assert compact["omitted_span_count"] == 7
    assert call["request"]["candidate_evidence"][0]["transcript_evidence"] == transcript
    assert "candidate-0" not in call["request"]["payload"]["prompt"]


def test_narration_prompt_separates_speech_budget_from_decoding_bound(inputs, tmp_path, model):
    draft = draft_for(inputs, tmp_path)
    for beat, request in zip(draft["beats"], model):
        limit = beat["narration_max_chars"]
        prompt = json.loads(request["prompt"])
        assert "maxLength" not in request["format"]["properties"]["narration_draft"]
        assert prompt["narration_length_includes_punctuation"] is False
        assert prompt["narration_target_chars"] == {"min": 1, "max": max(16, limit)}
        assert prompt["narration_budget_is_advisory"] is True
        assert "不要复述ASR" in prompt["task"] and "不要堆叠地名" in prompt["task"]
        assert f"粗估参考为 {limit} 个朗读字" in prompt["task"]
        assert "不设最低字数" in prompt["task"]
        assert "单张关键帧不能证明" in prompt["task"]
        assert "禁止截词、截句" in prompt["task"]
        assert "猴子坐在台阶旁。" in {example["prefer"] for example in prompt["narration_examples"]}
    assert draft["beats"][0]["narration_max_chars"] == 23
    assert draft["beats"][2]["narration_max_chars"] == 10


def test_full_and_short_candidate_ids_share_narration_rules(inputs, tmp_path, model):
    draft = draft_for(inputs, tmp_path)
    selector = OllamaSelector(project_root=tmp_path)
    beat = build_outline(inputs[0])[0]
    for short_ids in (False, True):
        selector.select(beat=beat, candidates=draft["beats"][0]["candidates"], context={
            "thematic_montage": True, "require_visual": True, "draft_narration": True,
            "contact_sheet": True, "short_candidate_ids": short_ids,
        })
    full, short = (json.loads(request["prompt"]) for request in model[-2:])
    for field in ("narration_max_chars", "narration_target_chars", "narration_counting_rule",
                  "narration_length_includes_punctuation", "narration_examples"):
        assert full[field] == short[field]
    assert full["task"].split("同时返回 narration_draft：")[1] == short["task"].split("同时返回 narration_draft：")[1]


@pytest.mark.parametrize("text", [
    "餐厅里热闹的氛围。", "我们先去超市买些。", "也别忘了多带几。", "猴子在树上嬉戏，展示。", "车窗外，。",
])
def test_period_does_not_hide_obvious_unfinished_phrase(text):
    from quickcut.planning.selection import narration_review
    review = narration_review(text, 15_000_000)
    assert review["status"] == "requires_edit"
    assert "NARRATION_INCOMPLETE_SENTENCE" in {warning["code"] for warning in review["warnings"]}


@pytest.mark.parametrize("text", [
    "猴子坐在台阶旁。", "货架上摆满了商品。", "店里坐着不少食客。", "先把雨伞带上。",
    "走走停停。", "还想再看几眼。", "这是模型展示。", "路上没有但是。",
    "衣服多带几件。", "行李少带些。", "饮料先拿几瓶。", "我们衣服多带几件。",
])
def test_complete_short_sentences_are_not_rejected_by_tail_heuristics(text):
    from quickcut.planning.selection import narration_review
    assert narration_review(text, 5_000_000)["warnings"] == []


def test_speech_estimate_excludes_punctuation_and_is_only_advisory():
    from quickcut.planning.selection import narration_review
    assert narration_review("猴子，坐在台阶旁。", 4_300_000)["warnings"] == []
    review = narration_review("猴子坐在台阶旁边休息。", 4_300_000)
    assert not review["warnings"]
    assert review["status"] == "pending_confirmation"
    assert review["advisories"][0]["code"] == "NARRATION_DURATION_ESTIMATE"
    assert review["estimated_duration_us"] > 4_300_000


def test_unknown_short_alias_is_never_silently_mapped(inputs, tmp_path, monkeypatch):
    requests = []
    def invalid_alias(request, *, timeout):
        requests.append(request)
        return io.BytesIO(json.dumps({"done": True, "response": json.dumps({
            "shot_order": ["C99"], "reasons": {"C99": "画面"}, "roles": {"C99": "broll"},
            "narration_draft": "林间光影。"})}).encode())
    monkeypatch.setattr("quickcut.planning.selection.open_ollama", invalid_alias)
    with pytest.raises(PlanningError, match="短ID非法"):
        draft_for(inputs, tmp_path)
    assert len(requests) == 2


@pytest.mark.parametrize("bad_text,warning_code", [
    ("餐厅里热闹的氛围。", "NARRATION_INCOMPLETE_SENTENCE"),
    ("也别忘了多带几", "NARRATION_INCOMPLETE_SENTENCE"),
    ("我们先去超市买些。", "NARRATION_INCOMPLETE_SENTENCE"),
    ("猴子在树上嬉戏，展示。", "NARRATION_INCOMPLETE_SENTENCE"),
    ("A peaceful forest.", "NARRATION_NON_CHINESE"),
    ("", "NARRATION_EMPTY"),
])
def test_bad_narration_preserves_real_selection_but_blocks_confirmation(inputs, tmp_path, model, monkeypatch, bad_text, warning_code):
    from quickcut.planning import selection
    original = selection.open_ollama
    requests = []
    def bad_first(request, *, timeout):
        requests.append(json.loads(request.data))
        if set(requests[-1]["format"]["properties"]) == {"narration_draft"}:
            return io.BytesIO(json.dumps({"done": True, "response": json.dumps({"narration_draft": bad_text})}).encode())
        if len(requests) == 1:
            answer = {"shot_order": ["C01"], "reasons": {"C01": "主题一致"},
                      "roles": {"C01": "atmosphere"}, "narration_draft": bad_text}
            return io.BytesIO(json.dumps({"model": "qwen2.5vl:3b", "done": True,
                                          "response": json.dumps(answer)}).encode())
        return original(request, timeout=timeout)
    monkeypatch.setattr(selection, "open_ollama", bad_first)
    draft = draft_for(inputs, tmp_path)
    assert len(requests) == 5  # Three selections, two text-only repairs.
    assert draft["status"] == "needs_review"
    assert draft["beats"][0]["narration_draft"] == bad_text
    assert draft["beats"][0]["narration_review"]["status"] == "requires_edit"
    assert warning_code in {item["code"] for item in draft["beats"][0]["review_warnings"]}
    assert any(item["beat_id"] == "beat-open" and item["code"] == warning_code for item in draft["review_warnings"])
    first = draft["model_calls"][0]["raw_response"]
    assert json.loads(first["body"]["response"])["narration_draft"] == bad_text
    assert first["canonicalized"]["shot_order"] == ["candidate-0"]
    assert not first.get("previous_attempts")
    with pytest.raises(PlanningError):
        confirm(draft)
    original_draft = copy.deepcopy(draft)
    texts = {beat["beat_id"]: beat["narration_draft"] for beat in draft["beats"]}
    texts["beat-open"] = "林间光影。"
    result = confirm_draft(draft, approved_text_by_beat=texts, reviewer="助手验收", confirmation_kind="assistant_review",
                           reviewed_at="2026-11-16T10:00:00+08:00")
    assert result.story_plan["status"] == "ready_for_edit"
    assert result.story_plan["narration_cues"][0]["text"] == "林间光影。"
    assert result.story_plan["narration_cues"][0]["text_source"] == "assistant_review"
    assert len(requests) == 5 and draft == original_draft


@pytest.mark.parametrize("initial", ["餐厅内部热闹，吉", "餐厅里热闹的氛围。", "店里坐着许多正在用餐的食客，远处也摆着好几张餐桌，门口还有几位客人。"])
def test_narration_only_rewrite_preserves_selection_and_original_evidence(inputs, tmp_path, model, monkeypatch, initial):
    from quickcut.planning import selection
    original = selection.open_ollama
    requests = []
    def respond(request, *, timeout):
        payload = json.loads(request.data)
        requests.append(payload)
        if set(payload["format"]["properties"]) == {"narration_draft"}:
            prompt = json.loads(payload["prompt"])
            assert prompt["previous_text"] == initial
            assert prompt["previous_tail"] == initial[-16:]
            assert prompt["selected_candidate_ids"] == ["candidate-0"]
            assert {item["candidate_id"] for item in prompt["image_mapping"]} == {"candidate-0"}
            assert "maxLength" not in payload["format"]["properties"]["narration_draft"]
            answer = {"narration_draft": "店里坐着不少食客。"}
            return io.BytesIO(json.dumps({"done": True, "response": json.dumps(answer)}).encode())
        response = original(request, timeout=timeout)
        if len(requests) == 1:
            body = json.loads(response.read())
            answer = json.loads(body["response"])
            answer["narration_draft"] = initial
            body["response"] = json.dumps(answer)
            return io.BytesIO(json.dumps(body).encode())
        return response
    monkeypatch.setattr(selection, "open_ollama", respond)
    draft = draft_for(inputs, tmp_path)
    first = draft["beats"][0]
    raw = first["selection"]["raw_response"]
    assert len(requests) == len(draft["model_attempts"]) == 4
    assert first["narration_draft"] == "店里坐着不少食客。"
    assert json.loads(raw["body"]["response"])["narration_draft"] == initial
    assert raw["canonicalized"]["narration_draft"] == initial
    assert first["selected_candidate_ids"] == ["candidate-0"]
    assert raw["narration_rewrites"][0]["accepted"] is True
    assert raw["narration_result"]["approved"] is False
    assert len(list((tmp_path / "planning-evidence").glob("*.json"))) == 4
    assert confirm(draft).story_plan["narration_cues"][0]["text"] == first["narration_draft"]
    assert len(requests) == 4


def test_rewrite_service_failure_keeps_valid_selection_and_complete_long_text(inputs, tmp_path, model, monkeypatch):
    from quickcut.planning import selection
    original = selection.open_ollama
    long_text = "店里坐着许多正在用餐的食客，远处也摆着好几张餐桌，门口还有几位客人。"
    def respond(request, *, timeout):
        payload = json.loads(request.data)
        if set(payload["format"]["properties"]) == {"narration_draft"}:
            raise TimeoutError("rewrite unavailable")
        response = original(request, timeout=timeout)
        body = json.loads(response.read())
        answer = json.loads(body["response"])
        answer["narration_draft"] = long_text
        body["response"] = json.dumps(answer)
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr(selection, "open_ollama", respond)
    draft = draft_for(inputs, tmp_path)
    for beat in draft["beats"]:
        assert beat["narration_draft"] == long_text
        assert not beat["narration_review"]["warnings"]
    raw = draft["model_calls"][0]["raw_response"]
    assert raw["narration_rewrites"][0]["status"] == "failed"
    assert raw["narration_result"]["text"] == long_text
    assert confirm(draft).story_plan["status"] == "ready_for_edit"


def test_each_incomplete_draft_sentence_is_reviewable_without_rerunning_real_selection(inputs, tmp_path, monkeypatch):
    requests = []
    def incomplete(request, *, timeout):
        requests.append(request)
        answer = {"shot_order": ["C01"], "reasons": {"C01": "主题"}, "roles": {"C01": "atmosphere"},
                  "narration_draft": "也别忘了多带几"}
        return io.BytesIO(json.dumps({"done": True, "response": json.dumps(answer)}).encode())
    monkeypatch.setattr("quickcut.planning.selection.open_ollama", incomplete)
    draft = draft_for(inputs, tmp_path)
    assert len(requests) == 9  # Two bounded format retries per beat; never reselect.
    assert all(beat["narration_review"]["status"] == "requires_edit" for beat in draft["beats"])
    with pytest.raises(PlanningError, match="句末标点"):
        confirm(draft)


@pytest.mark.parametrize("done", [False, None])
def test_incomplete_inference_stops_before_narration_rewrite(inputs, tmp_path, model, monkeypatch, done):
    from quickcut.planning import selection
    original = selection.open_ollama
    def respond(request, *, timeout):
        body = json.loads(original(request, timeout=timeout).read())
        body["done"] = done
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr(selection, "open_ollama", respond)
    with pytest.raises(PlanningError, match="推理响应未完成") as exc:
        draft_for(inputs, tmp_path)
    assert len(model) == 1
    assert not exc.value.detail["attempt_record"]["inference_executed"]
    archives = list((tmp_path / "planning-evidence").glob("*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())["status"] == "incomplete_response"


@pytest.mark.parametrize("failure_at", ["initial_write", "rewrite_request", "rewrite_response", "final_selection"])
def test_archive_write_failure_is_structured_and_stops_draft(inputs, tmp_path, model, monkeypatch, failure_at):
    from pathlib import Path
    from quickcut.planning import selection
    original_call = selection.open_ollama
    original_write = Path.write_text
    def respond(request, *, timeout):
        payload = json.loads(request.data)
        if set(payload["format"]["properties"]) == {"narration_draft"}:
            answer = {"narration_draft": "店里坐着不少食客。"}
            return io.BytesIO(json.dumps({"done": True, "response": json.dumps(answer)}).encode())
        body = json.loads(original_call(request, timeout=timeout).read())
        answer = json.loads(body["response"])
        answer["narration_draft"] = "餐厅内部热闹，吉"
        body["response"] = json.dumps(answer)
        return io.BytesIO(json.dumps(body).encode())
    def write(path, data, *args, **kwargs):
        if path.parent.name == "planning-evidence":
            record = json.loads(data)
            fail = (
                failure_at == "initial_write" and record.get("status") == "requested"
                or failure_at == "rewrite_request" and record.get("kind") == "narration_rewrite" and record.get("status") == "requested"
                or failure_at == "rewrite_response" and record.get("kind") == "narration_rewrite" and record.get("status") == "reviewed"
                or failure_at == "final_selection" and "narration_result" in record
            )
            if fail:
                raise OSError("simulated archive disk failure")
        return original_write(path, data, *args, **kwargs)
    monkeypatch.setattr(selection, "open_ollama", respond)
    monkeypatch.setattr(Path, "write_text", write)
    with pytest.raises(PlanningError, match="证据归档读写失败") as exc:
        draft_for(inputs, tmp_path)
    assert exc.value.detail["archive_io_error"] is True
    assert "simulated archive disk failure" in exc.value.detail["error"]
    assert len(model) == (0 if failure_at == "initial_write" else 1)


@pytest.mark.parametrize("bad_response", ["not-json", {"done": False},
    {"done": True, "response": {"narration_draft": "短句", "shot_order": []}}])
def test_rewrite_format_failure_retries_text_only_with_feedback(inputs, tmp_path, model, monkeypatch, bad_response):
    from quickcut.planning import selection
    original = selection.open_ollama
    rewrites = []
    def respond(request, *, timeout):
        payload = json.loads(request.data)
        if set(payload["format"]["properties"]) == {"narration_draft"}:
            rewrites.append(json.loads(payload["prompt"]))
            answer = bad_response if len(rewrites) == 1 else {"done": True, "response": {"narration_draft": "树影落在路边。"}}
            return io.BytesIO((answer if isinstance(answer, str) else json.dumps(answer)).encode())
        body = json.loads(original(request, timeout=timeout).read())
        if len(model) == 1:
            answer = json.loads(body["response"])
            answer["narration_draft"] = "餐厅内部热闹，吉"
            body["response"] = json.dumps(answer)
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr(selection, "open_ollama", respond)
    draft = draft_for(inputs, tmp_path)
    assert len(model) == 3 and len(rewrites) == 2
    assert rewrites[1]["previous_rewrite_feedback"]["error"]
    assert rewrites[0]["selected_candidate_ids"] == rewrites[1]["selected_candidate_ids"]
    assert draft["beats"][0]["narration_draft"] == "树影落在路边。"
    assert len(draft["model_attempts"]) == 5


@pytest.mark.parametrize("missing_visual", [False, True])
def test_rewrite_images_follow_selection_order_and_require_all_selected_evidence(tmp_path, monkeypatch, missing_visual):
    from quickcut.planning.selection import Selection
    selector = OllamaSelector(project_root=tmp_path)
    chosen = Selection(beat_id="beat-test", ordered_candidate_ids=["b", "a"],
                       narration_draft="餐厅内部热闹，吉", raw_response={})
    calls = []
    def images(candidates, *, image_mapping):
        assert [c["candidate_id"] for c in candidates] == ["b", "a"]
        image_mapping.extend({"candidate_id": cid, "image_index": i} for i, cid in enumerate(["b"] if missing_visual else ["b", "a"]))
        return ["fixture"]
    def respond(request, *, timeout):
        calls.append(request)
        return io.BytesIO(json.dumps({"done": True, "response": {"narration_draft": "树影落在路边。"}}).encode())
    monkeypatch.setattr(selector, "_images", images)
    monkeypatch.setattr("quickcut.planning.selection.open_ollama", respond)
    selector._repair_narration(chosen, beat={"purpose": "开场", "duration_budget_us": 10_000_000},
        candidates=[{"candidate_id": cid, "origin": "user"} for cid in ["a", "b"]],
        context={"require_visual": True})
    assert len(calls) == (0 if missing_visual else 1)
    assert chosen.ordered_candidate_ids == ["b", "a"]
    if missing_visual:
        assert chosen.narration_draft == "餐厅内部热闹，吉"
        assert chosen.raw_response["narration_rewrite_skip_reason"] == "selected_visual_evidence_unavailable"
        assert chosen.raw_response["narration_result"]["review"]["warnings"]
    else:
        assert chosen.narration_draft == "树影落在路边。"

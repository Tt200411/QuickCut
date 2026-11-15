"""用 C 的真实 plan() 和 E 的实际 WAV 测量覆盖桥接及 D 的通用回归。"""
from copy import deepcopy
from pathlib import Path

import pytest

from quickcut import integration as bridge
from quickcut import voice_package as voice_module
from quickcut.planning import build_outline, build_retrieval_request, load_bundle, plan, read_json
from quickcut.planning.contracts import PlanningError, ref
from quickcut.timeline import TimelineError, compile_timeline

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def case():
    _, docs = load_bundle(ROOT / "examples/team_contracts/bundle.json")
    brief, manifest = docs["Brief"], docs["AssetManifest"]
    candidates = read_json(ROOT / "examples/team_contracts/c/candidate_set.json")
    result = plan(brief, manifest, candidates,
                  approved_cues=read_json(ROOT / "examples/team_contracts/c/approved_cues.json"),
                  plan_revision=7)
    assert result.story_plan["status"] == "ready_for_edit"
    return dict(result=result, brief=brief, manifest=manifest, candidate_set=candidates,
                output=docs["EditTimeline"]["output"])


def build(case, root, **kwargs):
    return bridge.build_from_plan(**case, root=root, fixture=True, **kwargs)


def compile_voice(case, voice, **kwargs):
    return bridge.compile_from_voice(**case, voice=voice, **kwargs)


def tracks(timeline):
    return {track["kind"]: track for track in timeline["tracks"]}


def direct(case, voice):
    return compile_timeline(case["brief"], case["manifest"], case["result"].story_plan,
                            voice, output=case["output"])


def test_real_plan_to_measured_fixture_to_timeline(case, tmp_path):
    before = deepcopy(case)
    result = build(case, tmp_path, expected_story_revision=7, voice_revision=3,
                   timeline_document_id="integration-cut", timeline_revision=9)
    assert case == before
    assert result.story_plan == case["result"].story_plan
    assert result.voice_package["status"] == "ready"
    assert result.voice_package["measurement_source"] == "fixture_tone"
    assert result.voice_package["synthesis_profile"]["voice"] == "tone-not-speech"
    assert result.timeline["status"] == "ready_for_export"
    assert result.timeline["fixtures_only"] is True
    assert result.timeline["duration_us"] == 18_000_000
    assert result.timeline["document_id"] == "integration-cut"
    assert result.timeline["revision"] == 9
    assert {r["kind"]: r for r in result.timeline["inputs"]} == {
        doc["kind"]: ref(doc) for doc in (result.story_plan, result.voice_package,
                                         case["brief"], case["manifest"])
    }
    assert result.assets == case["manifest"]["assets"] + result.voice_package["assets"]
    assets = {a["asset_id"]: a for a in result.assets}
    assert len(assets) == len(result.assets)
    by_kind = tracks(result.timeline)
    narration = {c["cue_id"]: c for c in by_kind["narration"]["clips"]}
    for cue in result.voice_package["cues"]:
        asset = assets[cue["asset_id"]]
        assert voice_module.duration(voice_module.read_pcm(tmp_path / asset["uri"])) == cue["duration_us"]
        assert voice_module.digest(tmp_path / asset["uri"]) == asset["content_hash"]
        assert cue["script_input"] == ref(result.story_plan)
        clip = narration[cue["cue_id"]]
        assert clip["source_range_us"] == {"begin": 0, "end": cue["duration_us"]}
        subtitle = next(s for s in by_kind["subtitle"]["cues"] if s["cue_id"] == cue["cue_id"])
        offset = clip["timeline_range_us"]["begin"]
        assert subtitle["timeline_range_us"] == {
            key: value + offset for key, value in cue["local_subtitles"][0]["range_us"].items()
        }
        assert subtitle["timeline_range_us"]["end"] == clip["timeline_range_us"]["end"] - 180_000
        assert (tmp_path / cue["srt_uri"]).read_text().endswith(cue["text"] + "\n")
    for clip in by_kind["video"]["clips"]:
        segment = next(s for s in case["manifest"]["segments"] if s["segment_id"] == clip["segment_id"])
        assert clip["source_range_us"]["begin"] == segment["source_range_us"]["begin"]
        assert clip["source_range_us"]["end"] <= segment["source_range_us"]["end"]
    assert any(c["source_range_us"]["begin"] > 0 for c in by_kind["video"]["clips"])
    pictures = {c["clip_id"]: c for c in by_kind["video"]["clips"]}
    for clip in by_kind["source_audio"]["clips"]:
        linked = pictures[clip["linked_video_clip_id"]]
        assert all(clip[k] == linked[k] for k in ("asset_id", "asset_revision", "source_range_us", "timeline_range_us"))
    assert not (tmp_path / "voice_package.json").exists()


@pytest.mark.parametrize("mode", ["outline", "draft", "missing_evidence"])
def test_actual_nonready_c_output_is_rejected(case, tmp_path, mode):
    candidates = deepcopy(case["candidate_set"])
    if mode == "missing_evidence":
        for row in candidates["results"]:
            row["candidates"] = []
    case["result"] = plan(case["brief"], case["manifest"],
                           None if mode == "outline" else candidates)
    assert case["result"].story_plan["status"] != "ready_for_edit"
    before = deepcopy(case["result"])
    with pytest.raises(bridge.IntegrationError) as error:
        build(case, tmp_path / "not-created")
    assert error.value.code == "STORY_NOT_READY"
    assert case["result"] == before
    assert not (tmp_path / "not-created").exists()


@pytest.mark.parametrize("state", ["awaiting_candidates", "needs_input", "pending_review"])
def test_nonready_c_rejected_before_e(case, tmp_path, monkeypatch, state):
    case["result"].story_plan["status"] = state
    monkeypatch.setattr(voice_module, "build_voice_package", lambda *a, **kw: pytest.fail("不应运行 E"))
    with pytest.raises(bridge.IntegrationError) as error:
        build(case, tmp_path / "not-created")
    assert error.value.code == "STORY_NOT_READY"
    assert not (tmp_path / "not-created").exists()


@pytest.mark.parametrize("field,value", [
    ("pending_reviews", [{"type": "narration", "cue_id": "cue-01"}]),
    ("unresolved_gaps", [{"gap_id": "gap-test"}]),
])
def test_ready_label_does_not_override_pending_work(case, tmp_path, field, value):
    case["result"].story_plan[field] = value
    with pytest.raises(bridge.IntegrationError, match="待审"):
        build(case, tmp_path)


def test_unapproved_cue_not_promoted(case, tmp_path):
    story = case["result"].story_plan
    story["narration_cues"][0]["approved"] = False
    before = deepcopy(story)
    with pytest.raises(bridge.IntegrationError) as error:
        build(case, tmp_path)
    assert error.value.code == "VLM_INVALID_PLAN"
    assert story == before


@pytest.mark.parametrize("revision", [6, 8, True, 0])
def test_expected_story_revision_is_exact(case, tmp_path, revision):
    with pytest.raises(bridge.IntegrationError) as error:
        build(case, tmp_path, expected_story_revision=revision)
    assert error.value.code == "STALE_INPUT_REVISION"


@pytest.mark.parametrize("kind", ["Brief", "AssetManifest", "CandidateSet"])
def test_stale_c_dependencies_rejected_before_synthesis(case, tmp_path, kind):
    link = next(r for r in case["result"].story_plan["inputs"] if r["kind"] == kind)
    link["revision"] += 1
    with pytest.raises(PlanningError) as error:
        build(case, tmp_path)
    assert error.value.code == "STALE_INPUT_REVISION"


@pytest.mark.parametrize("failure", ["partial", "failed", "needs_retime"])
def test_real_e_nonready_fails_closed_and_preserves_feedback(case, tmp_path, monkeypatch, failure):
    synthesize = voice_module.synthesize
    text = case["result"].story_plan["narration_cues"][1]["text"]
    if failure == "needs_retime":
        config = {"sentence_pause_us": 4_000_000}
    else:
        config = {}
        def broken(value, output, profile):
            if failure == "failed" or value == text:
                raise OSError("测试合成失败")
            synthesize(value, output, profile)
        monkeypatch.setattr(voice_module, "synthesize", broken)
    monkeypatch.setattr(bridge, "compile_timeline", lambda *a, **kw: pytest.fail("非 ready 不应交给 D"))
    with pytest.raises(bridge.IntegrationError) as error:
        build(case, tmp_path, config=config)
    package = error.value.voice_package
    assert package["status"] == failure
    assert package["errors"]
    assert error.value.action_owner == ("D" if failure == "needs_retime" else "E")
    assert package["inputs"] == [ref(case["result"].story_plan)]
    assert error.value.code == ("VOICE_NEEDS_RETIME" if failure == "needs_retime" else "VOICE_NOT_READY")
    assert error.value.to_dict(case["brief"]["project_id"])["status"] != "ready_for_export"
    assert package["assets"] or failure == "failed"


@pytest.mark.parametrize("mutation", ["revision", "document_id", "cue_revision", "project", "timeline_request", "boolean_revision"])
def test_existing_voice_requires_exact_script(case, tmp_path, mutation):
    voice = build(case, tmp_path).voice_package
    if mutation == "cue_revision":
        voice["cues"][0]["script_input"]["revision"] -= 1
    elif mutation == "project":
        voice["project_id"] = "other-project"
    elif mutation == "timeline_request":
        voice["inputs"].append({"kind": "EditTimeline", "document_id": "old-timeline", "revision": 1})
    elif mutation == "boolean_revision":
        case["result"].story_plan["revision"] = 1
        voice["inputs"][0]["revision"] = True
    else:
        voice["inputs"][0][mutation] = 99 if mutation == "revision" else "old-story"
    with pytest.raises((PlanningError, bridge.IntegrationError)) as error:
        compile_voice(case, voice)
    assert error.value.code == "STALE_INPUT_REVISION"


@pytest.mark.parametrize("mutation", ["status", "errors", "cue_fit", "cue_retime"])
def test_ready_voice_label_cannot_hide_failures(case, tmp_path, monkeypatch, mutation):
    voice = build(case, tmp_path).voice_package
    if mutation == "status":
        voice["status"] = "ok"
    elif mutation == "errors":
        voice["errors"] = [{"code": "TTS_FAILED"}]
    elif mutation == "cue_fit":
        voice["cues"][0]["fit_status"] = "failed"
        del voice["cues"][0]["asset_id"]
    else:
        voice["cues"][0]["needs_retime"] = True
    monkeypatch.setattr(bridge, "compile_timeline", lambda *a, **kw: pytest.fail("应先拒绝 E"))
    with pytest.raises(bridge.IntegrationError) as error:
        compile_voice(case, voice)
    assert error.value.code == "VOICE_NOT_READY"


def test_asset_union_rejects_collisions_without_changing_a(case, tmp_path):
    voice = build(case, tmp_path).voice_package
    original = deepcopy(case["manifest"])
    collision = deepcopy(case["manifest"]["assets"][0])
    voice["assets"].append(collision)
    with pytest.raises(TimelineError, match="duplicate asset_id"):
        compile_voice(case, voice)
    assert case["manifest"] == original


def test_multiple_local_subtitles_use_narration_origin_not_video_source(case, tmp_path):
    voice = build(case, tmp_path).voice_package
    cue = voice["cues"][1]
    text = cue["text"]
    cue["local_subtitles"] = [
        {"text": text[:4], "range_us": {"begin": 70_000, "end": 400_000}},
        {"text": text[4:], "range_us": {"begin": 500_000, "end": 930_000}},
    ]
    before = deepcopy(voice)
    result = compile_voice(case, voice)
    assert voice == before
    by_kind = tracks(result.timeline)
    narration = next(c for c in by_kind["narration"]["clips"] if c["cue_id"] == cue["cue_id"])
    offset = narration["timeline_range_us"]["begin"]
    assert offset == 6_200_000
    subtitles = [c for c in by_kind["subtitle"]["cues"] if c["cue_id"] == cue["cue_id"]]
    assert len({s["subtitle_id"] for s in subtitles}) == 2
    assert "".join(s["text"] for s in subtitles) == cue["text"]
    assert [s["timeline_range_us"] for s in subtitles] == [
        {k: v + offset for k, v in s["range_us"].items()} for s in cue["local_subtitles"]
    ]
    assert all(s["editable"] is True for s in subtitles)


@pytest.mark.parametrize("mutation", ["text", "range", "overlap", "duration", "source"])
def test_d_validation_not_weakened(case, tmp_path, mutation):
    voice = build(case, tmp_path).voice_package
    cue = voice["cues"][0]
    if mutation == "text":
        cue["local_subtitles"][0]["text"] = "没有播出的文字"
    elif mutation == "range":
        cue["local_subtitles"][0]["range_us"]["end"] = cue["duration_us"] + 1
    elif mutation == "overlap":
        cue["local_subtitles"] = [
            {"text": cue["text"][:2], "range_us": {"begin": 0, "end": 600_000}},
            {"text": cue["text"][2:], "range_us": {"begin": 500_000, "end": 900_000}},
        ]
    elif mutation == "duration":
        cue["duration_us"] += 1
    else:
        case["manifest"]["segments"][0]["source_range_us"]["end"] = 999_999_999
    with pytest.raises((TimelineError, bridge.IntegrationError)):
        compile_voice(case, voice)


def test_fixture_tone_with_nonfixture_c_propagates_without_relabeling_inputs(case, tmp_path):
    # 全部是测试证据；用于覆盖非 fixture 输入的传播分支，不声明真实素材审阅。
    brief, manifest, candidates = case["brief"], case["manifest"], case["candidate_set"]
    for doc in (brief, manifest, candidates):
        doc["fixtures_only"] = False
    request = build_retrieval_request(brief, manifest, build_outline(brief))
    segments = {s["segment_id"]: s for s in manifest["segments"]}
    assets = {a["asset_id"]: a for a in manifest["assets"]}
    outline = {b["beat_id"]: b for b in build_outline(brief)}
    queries = {q["query_id"]: q for q in request["queries"]}
    reviews = {}
    for row in candidates["results"]:
        beat = outline[queries[row["query_id"]]["beat_id"]]
        for candidate in row["candidates"]:
            segment = segments[candidate["segment_id"]]
            asset = assets[segment["asset_id"]]
            if asset["origin"] == "user":
                reviews[candidate["candidate_id"]] = {
                    "beat_id": beat["beat_id"], "event": beat["wish"]["event"],
                    "asset_id": asset["asset_id"], "asset_revision": asset["revision"],
                    "segment_id": segment["segment_id"], "source_range_us": deepcopy(segment["source_range_us"]),
                    "reviewer": "integration-test", "note": "仅用于单元测试的审阅记录",
                }
    case["result"] = plan(brief, manifest, candidates, retrieval_request=request,
                           reviewed_evidence=reviews,
                           approved_cues=read_json(ROOT / "examples/team_contracts/c/approved_cues.json"))
    before = deepcopy(case)
    result = build(case, tmp_path)
    assert result.story_plan["fixtures_only"] is False
    assert result.voice_package["fixtures_only"] is True
    assert result.timeline["fixtures_only"] is True
    assert case == before


@pytest.mark.parametrize("mutation", ["fixture_erased", "invalid_fixture", "voice_failed", "story_pending"])
def test_direct_d_regressions(case, tmp_path, mutation):
    voice = build(case, tmp_path).voice_package
    if mutation == "fixture_erased":
        voice["fixtures_only"] = False
    elif mutation == "invalid_fixture":
        voice["fixtures_only"] = "true"
    elif mutation == "voice_failed":
        voice["status"] = "partial"
        del voice["cues"][0]["asset_id"]
    else:
        case["result"].story_plan["pending_reviews"] = [{"type": "narration"}]
    with pytest.raises(TimelineError):
        direct(case, voice)


def test_no_narration_real_c_plan_is_supported(case, tmp_path):
    case["brief"]["requirements"]["narration"]["enabled"] = False
    case["result"] = plan(case["brief"], case["manifest"], case["candidate_set"])
    result = build(case, tmp_path)
    assert result.voice_package["status"] == "ready"
    assert not result.voice_package["assets"]
    assert not tracks(result.timeline)["narration"]["clips"]
    assert not tracks(result.timeline)["subtitle"]["cues"]


def test_new_voice_and_timeline_revisions_remain_independent(case, tmp_path):
    first = build(case, tmp_path)
    case["result"] = plan(case["brief"], case["manifest"], case["candidate_set"],
                           approved_cues=read_json(ROOT / "examples/team_contracts/c/approved_cues.json"),
                           plan_revision=8)
    second = build(case, tmp_path, previous=first.voice_package, voice_revision=2, timeline_revision=11)
    assert second.voice_package["inputs"] == [ref(second.story_plan)]
    assert second.story_plan["revision"] == 8
    assert second.voice_package["revision"] == 2
    assert second.timeline["revision"] == 11
    assert all(c["cache_hit"] for c in second.voice_package["cues"])
    assert second.voice_package["alignment_invalidated"] is True
    assert first.voice_package["inputs"][0]["revision"] == 7


def test_actual_local_synthesis_to_d_when_available(case, tmp_path):
    import shutil

    if not shutil.which("ffmpeg") or not any(shutil.which(e) for e in ("say", "espeak-ng", "espeak")):
        pytest.skip("本机缺少本地语音引擎或 ffmpeg")
    info = voice_module.capabilities()
    voices = [v["voice"] for v in info["voices"] if v["language"].lower() == "zh-cn"]
    if not voices:
        pytest.skip("本机没有已安装中文音色")
    result = bridge.build_from_plan(**case, root=tmp_path, fixture=False,
                                    config={"voice": voices[0], "rate": 220})
    assert result.voice_package["measurement_source"] == "measured_pcm_samples"
    assert result.voice_package["synthesis_profile"]["engine"] != "fixture-tone"
    assert result.timeline["status"] == "ready_for_export"
    for cue in result.voice_package["cues"]:
        asset = next(a for a in result.assets if a["asset_id"] == cue["asset_id"])
        assert voice_module.duration(voice_module.read_pcm(tmp_path / asset["uri"])) == cue["duration_us"]
        assert cue["quality"]["text_verification"] == "not_asr_verified"

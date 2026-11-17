from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import wave

import pytest

from quickcut import integration, voice_package
from quickcut.media import digest
from quickcut.planning import load_bundle, plan, read_json
from quickcut.planning.contracts import ref
from quickcut.retiming import content_hash, validate_voice_fit_receipt
from quickcut.timeline import TimelineError, compile_timeline
from quickcut.timeline_preview import PreviewError, _validate as validate_preview


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def case():
    _, docs = load_bundle(ROOT / "examples/team_contracts/bundle.json")
    candidates = read_json(ROOT / "examples/team_contracts/c/candidate_set.json")
    result = plan(docs["Brief"], docs["AssetManifest"], candidates,
        approved_cues=read_json(ROOT / "examples/team_contracts/c/approved_cues.json"), plan_revision=7)
    result.story_plan["narration_cues"][0]["duration_budget_us"] = 1_100_000
    return dict(result=result, brief=docs["Brief"], manifest=docs["AssetManifest"], candidate_set=candidates,
                output=docs["EditTimeline"]["output"], expected_story_revision=7)


def measure(case, root):
    return voice_package.build_voice_package(case["result"].story_plan, root, fixture=True)


def provider(case, root):
    def fit(request):
        return voice_package.build_voice_package(case["result"].story_plan, root, fixture=True,
            mode="fit", request=request, revision=request["voice_revision"])
    return fit


def prepare(case, root, voice):
    return integration.prepare_voice_fit(**case, measured_voice=voice, project_root=root)


def accept(case, root, prepared, reply, **extra):
    kwargs = {k: v for k, v in case.items() if k != "output"}
    return integration.compile_voice_fit(**kwargs, fitted_voice=reply, project_root=root,
        pending_timeline=prepared["pending_timeline"], request=prepared["request"], **extra)


def test_measure_fit_reply_compiles_and_preserves_exact_receipt(case, tmp_path):
    measured = measure(case, tmp_path)
    assert measured["status"] == "needs_retime"
    before = deepcopy((case, measured))
    completed = integration.complete_voice_timeline(**case, measured_voice=measured, project_root=tmp_path,
        fit_provider=provider(case, tmp_path))
    assert (case, measured) == before
    assert len(completed.fit_rounds) == 1
    round_ = completed.fit_rounds[0]
    assert round_["status"] == "accepted" and round_["reply_hash"] == content_hash(completed.voice_package)
    assert round_["pending_timeline"]["status"] == "pending_voice_fit"
    assert completed.voice_package["revision"] == measured["revision"] + 1
    assert completed.voice_package["inputs"] == [ref(completed.story_plan), ref(round_["pending_timeline"])]
    assert completed.timeline["status"] == "ready_for_export"
    assert 1 < completed.voice_package["cues"][0]["tempo_ratio"] <= 1.15
    validate_voice_fit_receipt(completed.timeline, completed.voice_package, manifest=case["manifest"])
    validate_preview(completed.timeline, case["manifest"], completed.voice_package)
    for clip in completed.timeline["tracks"][2]["clips"]:
        cue = next(c for c in completed.voice_package["cues"] if c["cue_id"] == clip["cue_id"])
        assert clip["source_range_us"] == {"begin": 0, "end": cue["duration_us"]}
        assert clip["timeline_range_us"]["begin"] == round_["request"]["cue_ranges"][cue["cue_id"]]["begin"]
    # Original initial-compilation entry remains closed to Timeline-specific replies.
    with pytest.raises(TimelineError, match="Initial compilation"):
        compile_timeline(case["brief"], case["manifest"], completed.story_plan, completed.voice_package, output=case["output"])


def test_fit_provider_can_be_an_independent_python_process(case, tmp_path):
    measured = measure(case, tmp_path)
    story_path = tmp_path / "approved-story.json"
    story_path.write_text(json.dumps(case["result"].story_plan))
    calls = []
    def external(request):
        calls.append(request["request_id"])
        path = tmp_path / f"request-{request['iteration']}.json"
        path.write_text(json.dumps(request))
        output = f"external-voice-r{request['voice_revision']}.json"
        proc = subprocess.run([sys.executable, "-m", "quickcut.voice_package", "--fixture", "--mode", "fit",
            "--story", str(story_path), "--root", str(tmp_path), "--request", str(path),
            "--revision", str(request["voice_revision"]), "--output", output], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return json.loads((tmp_path / output).read_text())
    completed = integration.complete_voice_timeline(**case, measured_voice=measured, project_root=tmp_path, fit_provider=external)
    assert len(calls) == 1 and completed.timeline["voice_fit"]["request"]["request_id"] == calls[0]


@pytest.mark.parametrize("change", ["reply_revision", "reply_timeline", "echo_request", "pending", "same_revision_content", "bool_ref"])
def test_rejects_stale_or_modified_request_reply(case, tmp_path, change):
    prepared = prepare(case, tmp_path, measure(case, tmp_path))
    reply = provider(case, tmp_path)(prepared["request"])
    if change == "reply_revision":
        reply["revision"] += 1
    elif change == "reply_timeline":
        reply["inputs"][1]["revision"] += 1
    elif change == "echo_request":
        reply["request"]["cue_ranges"]["cue-01"]["begin"] += 1
    elif change == "pending":
        prepared["pending_timeline"]["output"]["width"] += 2
    elif change == "same_revision_content":
        case["brief"]["requirements"]["tone"] = "changed without revision"
    else:
        reply["inputs"][1]["revision"] = True
    with pytest.raises((TimelineError, integration.IntegrationError)) as exc:
        accept(case, tmp_path, prepared, reply)
    assert exc.value.code == "STALE_INPUT_REVISION"


def test_two_round_limit_keeps_complete_overlong_audio_and_history(case, tmp_path):
    case["result"].story_plan["narration_cues"][0]["duration_budget_us"] = 500_000
    measured = measure(case, tmp_path)
    calls = []
    def fit(request):
        calls.append(request)
        return provider(case, tmp_path)(request)
    with pytest.raises(integration.IntegrationError) as exc:
        integration.complete_voice_timeline(**case, measured_voice=measured, project_root=tmp_path, fit_provider=fit)
    error = exc.value
    assert error.code == "VOICE_NEEDS_RETIME" and error.action_owner == "C"
    assert len(calls) == len(error.fit_rounds) == 2
    assert calls[1]["previous_request_id"] == calls[0]["request_id"]
    assert calls[1]["timeline"]["revision"] == calls[0]["timeline"]["revision"] + 1
    assert error.voice_package["cues"][0]["duration_us"] == measured["cues"][0]["duration_us"]
    assert error.voice_package["cues"][0]["tempo_ratio"] == 1
    assert all(r["status"] == "needs_retime" and r["reply"] for r in error.fit_rounds)
    with pytest.raises(integration.IntegrationError, match="最多"):
        integration.complete_voice_timeline(**case, measured_voice=measured, project_root=tmp_path, fit_provider=fit, max_iterations=3)


def test_previous_reply_cannot_satisfy_new_pending_revision(case, tmp_path):
    case["result"].story_plan["narration_cues"][0]["duration_budget_us"] = 500_000
    first = prepare(case, tmp_path, measure(case, tmp_path))
    reply = provider(case, tmp_path)(first["request"])
    second = integration.prepare_voice_fit(**case, measured_voice=reply, project_root=tmp_path,
        iteration=2, previous_request=first["request"])
    with pytest.raises(TimelineError) as exc:
        accept(case, tmp_path, second, reply)
    assert exc.value.code == "STALE_INPUT_REVISION"


@pytest.mark.parametrize("damage", ["missing", "hash", "lying_duration", "silence"])
def test_external_voice_media_is_verified_before_any_fit(case, tmp_path, damage):
    measured = measure(case, tmp_path)
    asset = measured["assets"][0]
    path = tmp_path / asset["uri"]
    if damage == "missing":
        path.unlink()
    elif damage == "hash":
        path.write_bytes(b"not a wav")
    else:
        with wave.open(str(path), "rb") as wav:
            params, pcm = wav.getparams(), wav.readframes(wav.getnframes())
        with wave.open(str(path), "wb") as wav:
            wav.setparams(params)
            wav.writeframes(bytes(len(pcm)) if damage == "silence" else pcm + bytes(4800 * 2))
        asset["content_hash"] = digest(path)
    with pytest.raises(integration.IntegrationError) as exc:
        integration.complete_voice_timeline(**case, measured_voice=measured, project_root=tmp_path,
            fit_provider=lambda _: pytest.fail("bad media must not reach provider"))
    assert exc.value.code == "VOICE_MEDIA_INVALID"


def test_downstream_rejects_missing_or_changed_fit_receipt(case, tmp_path):
    completed = integration.complete_voice_timeline(**case, measured_voice=measure(case, tmp_path), project_root=tmp_path,
        fit_provider=provider(case, tmp_path))
    for change in ("remove", "move", "hash"):
        timeline = deepcopy(completed.timeline)
        if change == "remove":
            timeline.pop("voice_fit")
        elif change == "move":
            timeline["tracks"][2]["clips"][0]["timeline_range_us"]["begin"] += 1
        else:
            timeline["voice_fit"]["reply_hash"] = "sha256:" + "0" * 64
        with pytest.raises(PreviewError):
            validate_preview(timeline, case["manifest"], completed.voice_package)


@pytest.mark.parametrize("disguise", ["strip_refs", "delete_mode", "forge_measure", "remove_request"])
def test_fit_cannot_be_disguised_as_measure_by_stripping_refs_and_receipt(case, tmp_path, disguise):
    completed = integration.complete_voice_timeline(**case, measured_voice=measure(case, tmp_path), project_root=tmp_path,
        fit_provider=provider(case, tmp_path))
    timeline, voice = deepcopy(completed.timeline), deepcopy(completed.voice_package)
    timeline.pop("voice_fit")
    voice["inputs"] = [ref(completed.story_plan)]
    if disguise == "delete_mode":
        voice.pop("mode")
    elif disguise in ("forge_measure", "remove_request"):
        voice["mode"] = "measure"
    if disguise == "remove_request":
        voice.pop("request")  # Per-cue requested ranges still prove this was a fit reply.
    with pytest.raises(PreviewError, match="初测 VoicePackage"):
        validate_preview(timeline, case["manifest"], voice)


def test_provider_failure_keeps_outstanding_request(case, tmp_path):
    def failed(_):
        raise RuntimeError("separate Python process failed")
    with pytest.raises(integration.IntegrationError) as exc:
        integration.complete_voice_timeline(**case, measured_voice=measure(case, tmp_path), project_root=tmp_path, fit_provider=failed)
    assert exc.value.code == "VOICE_PROVIDER_FAILED"
    assert exc.value.fit_rounds[0]["status"] == "provider_failed"
    assert exc.value.fit_rounds[0]["pending_timeline"]["status"] == "pending_voice_fit"


def test_observer_persists_pending_before_provider_and_copies_each_state(case, tmp_path):
    observations = []
    def observe(record):
        observations.append(deepcopy(record))
        record["request"]["request_id"] = "observer-cannot-mutate-the-live-request"
    def fit(request):
        assert observations[-1]["status"] == "requested"
        assert observations[-1]["request"]["request_id"] == request["request_id"]
        return provider(case, tmp_path)(request)
    result = integration.complete_voice_timeline(**case, measured_voice=measure(case, tmp_path), project_root=tmp_path,
                                                 fit_provider=fit, fit_observer=observe)
    assert [r["status"] for r in observations] == ["requested", "received", "accepted"]
    assert observations[0]["reply"] is None and observations[-1]["reply"] == result.voice_package
    assert result.fit_rounds[-1] == observations[-1]


def test_fit_cancellation_keeps_cancelled_code_and_pending_trace(case, tmp_path):
    from quickcut.production_pipeline import PipelineError
    observed = []
    def cancelled(request):
        raise PipelineError("CANCELLED", "user closed service", stage="voice")
    with pytest.raises(integration.IntegrationError) as exc:
        integration.complete_voice_timeline(**case, measured_voice=measure(case, tmp_path), project_root=tmp_path,
                                             fit_provider=cancelled, fit_observer=observed.append)
    assert exc.value.code == "CANCELLED"
    assert observed[-1]["status"] == "cancelled" and observed[-1]["error_code"] == "CANCELLED"
    assert exc.value.fit_rounds == [observed[-1]]


def test_observer_failure_prevents_external_inference(case, tmp_path):
    def disk_full(record):
        raise OSError("disk full")
    with pytest.raises(integration.IntegrationError) as exc:
        integration.complete_voice_timeline(**case, measured_voice=measure(case, tmp_path), project_root=tmp_path,
            fit_provider=lambda request: pytest.fail("unrecorded request reached E"), fit_observer=disk_full)
    assert exc.value.code == "TRACE_WRITE_FAILED" and exc.value.fit_rounds[0]["status"] == "requested"


@pytest.mark.parametrize("damage", ["missing_cue_id", "null_cues", "null_subtitles", "bad_subtitle"])
def test_malformed_external_reply_is_rejected_with_full_history(case, tmp_path, damage):
    def fit(request):
        reply = provider(case, tmp_path)(request)
        if damage == "missing_cue_id":
            reply["cues"][0].pop("cue_id")
        elif damage == "null_cues":
            reply["cues"] = None
        elif damage == "null_subtitles":
            reply["cues"][0]["local_subtitles"] = None
        else:
            reply["cues"][0]["local_subtitles"][0]["range_us"] = None
        return reply
    with pytest.raises(integration.IntegrationError) as exc:
        integration.complete_voice_timeline(**case, measured_voice=measure(case, tmp_path), project_root=tmp_path,
                                            fit_provider=fit)
    assert len(exc.value.fit_rounds) == 1
    assert exc.value.fit_rounds[0]["status"] == "rejected"
    assert exc.value.fit_rounds[0]["reply"] is not None
    assert exc.value.fit_rounds[0]["error_code"] == exc.value.code


def test_fitting_preserves_manual_hard_boundary_and_rejects_later_edit(case, tmp_path):
    story, manifest = case["result"].story_plan, case["manifest"]
    beat = story["beats"][0]
    segment = next(s for s in manifest["segments"] if s["segment_id"] == beat["shots"][0]["segment_id"])
    start = segment["source_range_us"]["begin"]
    boundaries = {"story_plan": ref(story), "asset_manifest": ref(manifest), "edits": [{
        "beat_id": beat["beat_id"], "shot_index": 0, "segment_id": segment["segment_id"],
        "source_range_us": {"begin": start, "end": start + 1_100_000},
        "method": "manual", "reviewer": "test-reviewer", "reason": "Confirmed exact shot"}]}
    prepared = integration.prepare_voice_fit(**case, measured_voice=measure(case, tmp_path), project_root=tmp_path,
                                             boundaries=boundaries)
    reply = provider(case, tmp_path)(prepared["request"])
    completed = accept(case, tmp_path, prepared, reply, boundaries=boundaries)
    assert completed.timeline["tracks"][0]["clips"][0]["source_range_us"] == boundaries["edits"][0]["source_range_us"]
    assert completed.timeline["tracks"][2]["clips"][0]["timeline_range_us"]["end"] <= 1_100_000
    changed = deepcopy(boundaries)
    changed["edits"][0]["reason"] = "Changed after fitting was requested"
    with pytest.raises(TimelineError) as exc:
        accept(case, tmp_path, prepared, reply, boundaries=changed)
    assert exc.value.code == "STALE_INPUT_REVISION"


def test_independent_fit_package_cannot_replace_initial_measurement(case, tmp_path):
    voice = measure(case, tmp_path)
    voice["mode"] = "fit"
    with pytest.raises(integration.IntegrationError) as exc:
        integration.complete_voice_timeline(**case, measured_voice=voice, project_root=tmp_path)
    assert exc.value.code == "STALE_INPUT_REVISION"

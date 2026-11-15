from copy import deepcopy

import pytest

from examples.team_contracts.demo_d import OUTPUT, load_case, run_case
from quickcut.timeline import TimelineError, compile_timeline


def _compile(bundle):
    docs = {doc["kind"]: doc for doc in bundle["documents"]}
    return compile_timeline(
        docs["Brief"], docs["AssetManifest"], docs["StoryPlan"], docs["VoicePackage"], output=OUTPUT,
    )


def test_generated_timeline_validates_without_reading_example_tracks():
    bundle = load_case("normal")
    expected = next(doc for doc in bundle["documents"] if doc["kind"] == "EditTimeline")
    expected["tracks"] = []
    result = run_case(bundle)
    picture, original, narration, subtitles = result["tracks"]
    assert result["duration_us"] == 18_000_000
    assert [clip["asset_id"] for clip in picture["clips"]] == [
        "asset-short-01", "asset-broll-01", "asset-long-01", "asset-short-03",
    ]
    assert len(original["clips"]) == 3
    cue = narration["clips"][1]["timeline_range_us"]
    assert cue == subtitles["cues"][1]["timeline_range_us"] == {"begin": 6_200_000, "end": 14_400_000}
    assert cue["begin"] < picture["clips"][1]["timeline_range_us"]["end"] < cue["end"]


def test_retime_uses_only_approved_source_headroom():
    bundle = load_case("retime")
    before = deepcopy(bundle)
    timeline = _compile(bundle)
    assert bundle == before
    picture, _, narration, _ = timeline["tracks"]
    assert picture["clips"][2]["source_range_us"] == {"begin": 40_000_000, "end": 46_000_000}
    assert narration["clips"][1]["timeline_range_us"] == {"begin": 6_200_000, "end": 15_000_000}
    assert narration["clips"][2]["timeline_range_us"]["begin"] == 15_200_000
    assert timeline["duration_us"] == 18_000_000
    run_case(bundle)


def test_changing_only_voice_duration_updates_only_its_cue():
    baseline, longer = load_case("normal"), load_case("longer")
    baseline_inputs = {doc["kind"]: doc for doc in baseline["documents"]}
    longer_inputs = {doc["kind"]: doc for doc in longer["documents"]}
    assert baseline_inputs["AssetManifest"] == longer_inputs["AssetManifest"]
    assert baseline_inputs["StoryPlan"] == longer_inputs["StoryPlan"]
    first, second = run_case(baseline), run_case(longer)
    assert first["duration_us"] == second["duration_us"] == 18_000_000
    assert first["tracks"][:2] == second["tracks"][:2]
    for index, key in ((2, "clips"), (3, "cues")):
        before = first["tracks"][index][key]
        after = second["tracks"][index][key]
        assert len(before) == len(after)
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        assert len(changed) == 1
        a, b = changed[0]
        assert a["cue_id"] == b["cue_id"] == "cue-02"
        assert a["timeline_range_us"] == {"begin": 6_200_000, "end": 14_400_000}
        assert b["timeline_range_us"] == {"begin": 6_200_000, "end": 15_000_000}


@pytest.mark.parametrize(("case", "code", "affected"), [
    ("overlong", "VOICE_NEEDS_RETIME", "cue-02"),
    ("stale", "STALE_INPUT_REVISION", "voice-001"),
])
def test_rejects_unusable_input(case, code, affected):
    with pytest.raises(TimelineError) as error:
        _compile(load_case(case))
    assert error.value.code == code
    assert affected in error.value.affected_ids


def test_refuses_to_extend_beyond_selected_segment():
    bundle = load_case("retime")
    manifest = next(doc for doc in bundle["documents"] if doc["kind"] == "AssetManifest")
    segment = next(seg for seg in manifest["segments"] if seg["segment_id"] == "seg-walk")
    segment["source_range_us"]["end"] = 45_500_000
    with pytest.raises(TimelineError) as error:
        _compile(bundle)
    assert error.value.code == "VOICE_NEEDS_RETIME"

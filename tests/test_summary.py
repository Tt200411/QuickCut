"""Offline regression tests for the reproducible Chinese-summary workflow."""
from __future__ import annotations

import io
import json
import wave
from argparse import Namespace
from pathlib import Path

import pytest

from quickcut.types import IndexManifest, Shot
from quickcut.core import _require_binary
from scripts import chinese_summary, plan_summary_acts
from scripts.verify_chinese_summary import audio_regression
from scripts.check_narration_coverage import closing_phrase_end


@pytest.fixture
def planning_fixture(tmp_path: Path):
    image = pytest.importorskip("PIL.Image")
    shots = []
    for index in range(9):
        path = tmp_path / f"frame-{index}.png"
        image.new("RGB", (80, 45), (index * 25, 80, 150)).save(path)
        shots.append(Shot(f"shot-{index:04d}", index * 3, (index + 1) * 3, keyframe=str(path)))
    manifest = IndexManifest("fixture", "unused.mp4", 1, 1.0, 27, 3, 3, "now", "fallback", shots=shots)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.to_dict()))
    config = {"theme": "林间故事", "acts": [
        {"id": name, "start": index * 9, "end": (index + 1) * 9, "count": 2,
         "instruction": "根据画面选择开头和收束。"}
        for index, name in enumerate(["hook", "journey", "close"])
    ]}
    config_path = tmp_path / "acts.json"
    config_path.write_text(json.dumps(config))
    return manifest, manifest_path, config_path, tmp_path / "qwen_plan.json"


def test_index_stage_builds_fresh_visual_asr_evidence_without_planning(planning_fixture, monkeypatch):
    manifest, _, _, output = planning_fixture
    calls = []
    monkeypatch.setattr(chinese_summary, "build_index", lambda *args, **kwargs: calls.append(kwargs) or manifest)
    monkeypatch.setattr(chinese_summary, "contact", lambda *args, **kwargs: None)
    monkeypatch.setattr(chinese_summary, "plan_with_ollama", lambda *args, **kwargs: pytest.fail("index must not call the planner"))
    monkeypatch.setattr("sys.argv", ["chinese_summary.py", "index", "unused.mp4", "--output-dir", str(output.parent), "--start", "0", "--end", "27"])
    chinese_summary.main()
    assert calls[0]["force"] is True
    assert calls[0]["asr_mode"] == "faster-whisper"
    assert (output.parent / "planning_manifest.json").is_file()


def test_narration_end_uses_closing_words_not_hallucinated_tail():
    words = [{"word": "终于", "end": 57.82}, {"word": "笑了。", "end": 58.24},
             {"word": "字幕by某人", "end": 58.48}]
    assert closing_phrase_end(words, "终于笑了") == 58.24
    assert closing_phrase_end(words, "不存在的话") is None


def test_act_planner_saves_actual_visual_selections_without_padding(planning_fixture, monkeypatch):
    manifest, manifest_path, config_path, output = planning_fixture
    requests = []

    def local_response(request, **kwargs):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({
            "response": json.dumps({"selected_positions": [1, 3], "description": "兔子走过树林。"}),
            "prompt_eval_count": 100, "eval_count": 30, "done": True,
        }).encode())

    monkeypatch.setattr(plan_summary_acts, "_open_ollama", local_response)
    monkeypatch.setattr("sys.argv", ["plan_summary_acts.py", str(manifest_path), str(config_path), "--output", str(output)])
    plan_summary_acts.main()
    result = json.loads(output.read_text())
    assert result["shot_order"] == [manifest.shots[index].shot_id for index in [0, 2, 3, 5, 6, 8]]
    assert [beat["id"] for beat in result["beats"]] == ["hook", "journey", "close"]
    assert [shot for beat in result["beats"] for shot in beat["source_shot_ids"]] == result["shot_order"]
    assert len(requests) == 3
    assert all(len(request["images"]) == 1 for request in requests)
    assert len(result["inference"]["grounded_shot_ids"]) == 9
    assert all(call["model_plan"]["selected_positions"] == [1, 3] for call in result["inference"]["calls"])
    assert result["inference"]["normalization_changed"] is False


@pytest.mark.parametrize("positions", [[1, 1], [3, 1]])
def test_act_planner_rejects_duplicate_or_reversed_model_choices(planning_fixture, monkeypatch, positions):
    _, manifest_path, config_path, output = planning_fixture

    def local_response(request, **kwargs):
        return io.BytesIO(json.dumps({"response": json.dumps({
            "selected_positions": positions, "description": "兔子走过树林。",
        })}).encode())

    monkeypatch.setattr(plan_summary_acts, "_open_ollama", local_response)
    monkeypatch.setattr("sys.argv", ["plan_summary_acts.py", str(manifest_path), str(config_path), "--output", str(output)])
    with pytest.raises(ValueError):
        plan_summary_acts.main()
    assert not output.exists()
    assert output.with_name("qwen_hook_raw.json").is_file()


@pytest.mark.parametrize("positions", [[0, 1], [-1, 1], [True, 3], [1, 4], [1.0, 3]])
def test_act_planner_rejects_noninteger_or_out_of_range_positions(planning_fixture, monkeypatch, positions):
    _, manifest_path, config_path, output = planning_fixture

    def local_response(request, **kwargs):
        return io.BytesIO(json.dumps({"response": json.dumps({
            "selected_positions": positions, "description": "兔子走过树林。",
        })}).encode())

    monkeypatch.setattr(plan_summary_acts, "_open_ollama", local_response)
    monkeypatch.setattr("sys.argv", ["plan_summary_acts.py", str(manifest_path), str(config_path), "--output", str(output)])
    with pytest.raises(ValueError, match="integer"):
        plan_summary_acts.main()
    assert not output.exists()


def test_model_order_repair_is_saved_and_used_without_editorial_sort(planning_fixture, monkeypatch):
    manifest, manifest_path, config_path, output = planning_fixture
    requests = []

    def local_response(request, **kwargs):
        payload = json.loads(request.data)
        requests.append(payload)
        result = {"selected_positions": [3, 1], "description": "兔子走过树林。"} if payload.get("images") else {"selected_positions": [1, 3]}
        return io.BytesIO(json.dumps({"response": json.dumps(result)}).encode())

    monkeypatch.setattr(plan_summary_acts, "_open_ollama", local_response)
    monkeypatch.setattr("sys.argv", ["plan_summary_acts.py", str(manifest_path), str(config_path), "--output", str(output)])
    plan_summary_acts.main()
    result = json.loads(output.read_text())
    assert len(requests) == 6
    assert result["shot_order"] == [manifest.shots[index].shot_id for index in [0, 2, 3, 5, 6, 8]]
    for call in result["inference"]["calls"]:
        assert call["model_plan"]["selected_positions"] == [3, 1]
        assert json.loads(call["order_correction"]["raw_payload"]["response"])["selected_positions"] == [1, 3]


def test_model_order_repair_cannot_substitute_bool_for_integer(planning_fixture, monkeypatch):
    _, manifest_path, config_path, output = planning_fixture

    def local_response(request, **kwargs):
        payload = json.loads(request.data)
        result = {"selected_positions": [3, 1], "description": "兔子走过树林。"} if payload.get("images") else {"selected_positions": [True, 3]}
        return io.BytesIO(json.dumps({"response": json.dumps(result)}).encode())

    monkeypatch.setattr(plan_summary_acts, "_open_ollama", local_response)
    monkeypatch.setattr("sys.argv", ["plan_summary_acts.py", str(manifest_path), str(config_path), "--output", str(output)])
    with pytest.raises(ValueError, match="temporal order"):
        plan_summary_acts.main()
    assert not output.exists()


def test_model_anchor_repair_is_saved_and_used(planning_fixture, monkeypatch):
    manifest, manifest_path, config_path, output = planning_fixture
    config = json.loads(config_path.read_text())
    for index, act in enumerate(config["acts"]):
        act["required_ids"] = [manifest.shots[index * 3 + 2].shot_id]
    config_path.write_text(json.dumps(config))
    request_count = 0

    def local_response(request, **kwargs):
        nonlocal request_count
        request_count += 1
        positions = [1, 2] if request_count % 2 else [1, 3]
        return io.BytesIO(json.dumps({"response": json.dumps({
            "selected_positions": positions, "description": "兔子走过树林。",
        })}).encode())

    monkeypatch.setattr(plan_summary_acts, "_open_ollama", local_response)
    monkeypatch.setattr("sys.argv", ["plan_summary_acts.py", str(manifest_path), str(config_path), "--output", str(output)])
    plan_summary_acts.main()
    result = json.loads(output.read_text())
    assert request_count == 6
    assert result["shot_order"] == [manifest.shots[index].shot_id for index in [0, 2, 3, 5, 6, 8]]
    assert all(call["anchor_correction"]["model_plan"]["selected_positions"] == [1, 3]
               for call in result["inference"]["calls"])


@pytest.mark.parametrize("order,credits_start,error", [
    (["shot-0001", "shot-0000"], 27, "chronological"),
    (["shot-0000", "shot-0000"], 27, "distinct"),
    (["shot-0000", "shot-0001"], 5, "credit"),
])
def test_render_rejects_invalid_timeline_before_tts(planning_fixture, monkeypatch, order, credits_start, error):
    manifest, _, _, output = planning_fixture
    output.write_text(json.dumps({"shot_order": order, "inference": {
        "grounded_shot_ids": [shot.shot_id for shot in manifest.shots],
    }}))
    monkeypatch.setattr(chinese_summary, "load_manifest", lambda _: manifest)

    def unexpected_tts(*args, **kwargs):
        pytest.fail("Invalid source selection reached TTS")

    monkeypatch.setattr(chinese_summary, "synthesize_timed_narration", unexpected_tts)
    with pytest.raises(ValueError, match=error):
        chinese_summary.render(Namespace(credits_start=credits_start), output.parent)


def test_render_rejects_english_narration_before_tts(planning_fixture, monkeypatch):
    manifest, _, _, output = planning_fixture
    ids = [shot.shot_id for shot in manifest.shots[:3]]
    output.write_text(json.dumps({
        "shot_order": ids, "narration": "兔子开始 adventure。",
        "inference": {"candidate_shot_ids": ids, "grounded_shot_ids": ids},
    }))
    monkeypatch.setattr(chinese_summary, "load_manifest", lambda _: manifest)

    def unexpected_tts(*args, **kwargs):
        pytest.fail("Mixed-language narration reached TTS")

    monkeypatch.setattr(chinese_summary, "synthesize_timed_narration", unexpected_tts)
    with pytest.raises(ValueError, match="Latin"):
        chinese_summary.render(Namespace(credits_start=27, cues=None), output.parent)


def test_render_rejects_existing_but_unseen_source_shots(planning_fixture, monkeypatch):
    manifest, _, _, output = planning_fixture
    output.write_text(json.dumps({
        "shot_order": ["shot-0000", "shot-0008"],
        "inference": {"grounded_shot_ids": ["shot-0000"]},
    }))
    monkeypatch.setattr(chinese_summary, "load_manifest", lambda _: manifest)
    with pytest.raises(ValueError, match="shown to the local model"):
        chinese_summary.render(Namespace(credits_start=27, cues=None), output.parent)


def test_source_audio_regression_requires_sample_aligned_stereo_evidence(tmp_path: Path):
    np = pytest.importorskip("numpy")
    rate = 12000
    time = np.arange(rate) / rate
    source = np.column_stack([0.15 * np.sin(2 * np.pi * 440 * time),
                              0.12 * np.sin(2 * np.pi * 660 * time)])
    voice = np.repeat((0.12 * np.sin(2 * np.pi * 887 * time))[:, None], 2, axis=1)

    def write(name, samples):
        path = tmp_path / name
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes((samples * 32767).astype("<i2").tobytes())
        return path

    original = write("original.wav", source)
    narration = write("narration.wav", voice)
    mixed = write("mixed.wav", 0.3 * source + 0.6 * voice)
    positive = audio_regression(_require_binary("ffmpeg"), mixed, original, narration, 1, "")
    assert positive["source_retained"] is True
    assert positive["source_gain"] == pytest.approx(0.3, abs=0.001)
    assert positive["narration_gain"] == pytest.approx(0.6, abs=0.001)
    assert positive["source_correlation_after_removing_narration"] > 0.999
    # A narration-only track can have similar energy and the same duration;
    # neither property should be mistaken for retained original sound.
    absent = audio_regression(_require_binary("ffmpeg"), narration, original, narration, 1, "")
    assert absent["source_retained"] is False
    swapped = write("swapped.wav", 0.3 * source[:, ::-1] + 0.6 * voice)
    wrong_channels = audio_regression(_require_binary("ffmpeg"), swapped, original, narration, 1, "")
    assert wrong_channels["source_retained"] is False

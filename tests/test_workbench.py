"""HTTP boundary and orchestration tests; real model acceptance runs separately."""
from pathlib import Path
from array import array
import io
import wave

import pytest
from fastapi.testclient import TestClient

from quickcut.media import write_json
from quickcut.workbench import create_app
from quickcut.planning.contracts import content_hash
from quickcut.planning.selection import Selection


def reference_wav(*, duration=1.25, rate=8000, width=2, silent=False):
    buffer = io.BytesIO()
    count = round(rate * duration)
    pcm = array("h", [0 if silent else (1200 if index % 2 else -1200) for index in range(count)]).tobytes()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm if width == 2 else bytes([128]) * count)
    return buffer.getvalue()


@pytest.fixture
def browser(tmp_path, monkeypatch):
    from quickcut.production_pipeline import Runtime
    default_reference = tmp_path / "default.wav"
    default_reference.write_bytes(reference_wav())
    config_path = tmp_path / "voice.json"
    write_json(config_path, {"engine": "indextts2", "model_dir": "outputs/test-model",
                             "reference_audio": str(default_reference), "voice_label": "默认测试男声"})
    app = create_app(root=tmp_path / "jobs", runtime=Runtime(voice_config=str(config_path)))
    submitted = []
    monkeypatch.setattr(app.state.jobs, "submit", lambda *args: submitted.append(args))
    with TestClient(app) as client:
        token = client.get("/api/config").json()["token"]
        client.headers["x-quickcut-token"] = token
        yield client, app.state.jobs, submitted


def upload(client):
    response = client.post("/api/jobs", data={"prompt": "真实公园画面", "duration": "30", "origin": "external"},
                           files=[("files", ("../原片.mp4", b"unit-test-input", "video/mp4"))])
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def frozen_draft():
    candidate = {"candidate_id": "c1", "asset_id": "a1", "asset_revision": 1, "segment_id": "s1",
                 "origin": "external", "duration_us": 15_000_000, "source_range_us": {"begin": 1_000_000, "end": 16_000_000}}
    draft = {"status": "needs_review", "inputs": {}, "input_hashes": {}, "beats": [{"beat_id": "beat-open",
               "purpose": "开场", "duration_budget_us": 10_000_000, "narration_draft": "看看公园的风景。",
               "narration_max_chars": 10, "selected_candidate_ids": ["c1"], "candidates": [candidate],
               "selection": Selection(beat_id="beat-open", ordered_candidate_ids=["c1"], source="model:test").to_record()}]}
    draft["draft_id"] = content_hash(draft)
    return draft


def review_ready(client, jobs):
    job_id = upload(client)
    root = jobs.directory(job_id)
    write_json(root / "draft.json", frozen_draft())
    jobs.update(job_id, status="needs_review")
    return job_id


def confirmation():
    return {"draft_id": frozen_draft()["draft_id"], "reviewer": "测试确认者", "confirmed": True,
            "approved_text_by_beat": {"beat-open": "看看公园。"}, "boundaries": [
                {"beat_id": "beat-open", "shot_index": 0, "segment_id": "s1",
                 "source_range_us": {"begin": 1_000_000, "end": 11_000_000}}]}


def test_home_uses_real_workflow_and_upload_keeps_origin(browser):
    client, jobs, submitted = browser
    assert "本地剪辑工作台" in client.get("/").text
    job_id = upload(client)
    settings = __import__("json").loads((jobs.directory(job_id) / "settings.json").read_text())
    assert settings["sources"][0]["origin"] == "external"
    assert Path(settings["sources"][0]["path"]).parent == jobs.directory(job_id) / "uploads"
    assert submitted == [(job_id, "analyze")]


def test_external_origin_and_missing_token_cannot_start_jobs(browser):
    client, _, submitted = browser
    payload = {"prompt": "test"}
    files = {"files": ("x.mp4", b"x", "video/mp4")}
    response = client.post("/api/jobs", data=payload, files=files, headers={"origin": "https://unrelated.example"})
    assert response.status_code == 403
    response = client.post("/api/jobs", data=payload, files=files, headers={"x-quickcut-token": "wrong"})
    assert response.status_code == 403
    assert client.get("/api/config", headers={"host": "attacker.example"}).status_code == 403
    assert not submitted


@pytest.mark.parametrize("filename,duration", [("x.txt", "30"), ("x.mp4", "nan"), ("x.mp4", "5"), ("x.mp4", "121")])
def test_bad_inputs_do_not_create_jobs(browser, filename, duration):
    client, jobs, submitted = browser
    response = client.post("/api/jobs", data={"prompt": "剪辑", "duration": duration},
                           files={"files": (filename, b"x", "video/mp4")})
    assert response.status_code == 422
    assert not list(jobs.root.iterdir()) and not submitted


def test_empty_and_oversized_uploads_are_cleaned(browser, monkeypatch):
    client, jobs, submitted = browser
    monkeypatch.setattr("quickcut.workbench.MAX_FILE_BYTES", 3)
    for content, code in ((b"", 422), (b"1234", 413)):
        response = client.post("/api/jobs", data={"prompt": "剪辑"}, files={"files": ("x.mp4", content, "video/mp4")})
        assert response.status_code == code
        assert not list(jobs.root.iterdir())
    assert not submitted


def test_review_exposes_only_frozen_candidate_view(browser):
    client, jobs, _ = browser
    job_id = review_ready(client, jobs)
    data = client.get(f"/api/jobs/{job_id}").json()
    shot = data["draft"]["beats"][0]["shots"][0]
    assert shot["source_range_us"] == {"begin": 1_000_000, "end": 11_000_000}
    assert shot["available_range_us"]["end"] == 16_000_000
    assert "inputs" not in data["draft"] and "model_calls" not in data["draft"]


@pytest.mark.parametrize("text", ["我们沿着公园小路慢慢走，看看身边的花草。", "我们沿着公园小路慢慢走，看看身边的花草。" * 2])
def test_duration_advisory_is_public_and_does_not_block_confirmation(browser, text):
    client, jobs, submitted = browser
    job_id = review_ready(client, jobs)
    body = {"draft_id": frozen_draft()["draft_id"], "review_revision": 0,
            "selection_overrides": {"beat-open": ["c1"]}, "draft_text_edits": {"beat-open": text}}
    response = client.post(f"/api/jobs/{job_id}/selection", json=body)
    assert response.status_code == 200, response.text
    beat = response.json()["draft"]["beats"][0]
    review = beat["narration_review"]
    count = sum(char.isalnum() for char in text)
    assert count > beat["narration_max_chars"] == review["max_chars"] == 10
    assert review["character_count"] == count
    assert review["estimated_duration_us"] == round((count / 2.5 + 0.8) * 1_000_000)
    assert "以配音实测为准" in review["estimate_basis"]
    assert review["advisories"][0]["code"] == "NARRATION_DURATION_ESTIMATE"
    assert beat["review_warnings"] == review["warnings"] == []
    assert review["status"] == "pending_confirmation" and review["approved"] is False
    assert client.get(f"/api/jobs/{job_id}").json()["draft"]["beats"][0]["narration_review"] == review
    assert len(submitted) == 1
    approve = confirmation()
    approve.update(review_revision=1, approved_text_by_beat={"beat-open": text})
    response = client.post(f"/api/jobs/{job_id}/confirm", json=approve)
    assert response.status_code == 200, response.text
    assert submitted[-1][2]["approved_text_by_beat"] == {"beat-open": text}


@pytest.mark.parametrize("length", [160, 161])
def test_narration_editor_limit_counts_punctuation_and_spaces(browser, length):
    client, jobs, submitted = browser
    job_id = review_ready(client, jobs)
    prefix, suffix = "我们走进公园", "慢慢看风景。"
    text = prefix + ("， " * length)[:length - len(prefix) - len(suffix)] + suffix
    assert len(text) == length
    body = {"draft_id": frozen_draft()["draft_id"], "review_revision": 0,
            "selection_overrides": {"beat-open": ["c1"]}, "draft_text_edits": {"beat-open": text}}
    response = client.post(f"/api/jobs/{job_id}/selection", json=body)
    if length == 161:
        assert response.status_code == 422
        assert not jobs.get(job_id).get("draft_text_edits")
    else:
        assert response.status_code == 200, response.text
    approve = confirmation()
    approve.update(review_revision=1 if length == 160 else 0, approved_text_by_beat={"beat-open": text})
    response = client.post(f"/api/jobs/{job_id}/confirm", json=approve)
    if length == 161:
        assert response.status_code == 422
        assert "字符" in response.json()["detail"]
        assert len(submitted) == 1
    else:
        assert response.status_code == 200, response.text
        assert submitted[-1][2]["approved_text_by_beat"]["beat-open"] == text


def test_confirmation_records_real_actor_and_server_time(browser):
    client, jobs, submitted = browser
    job_id = review_ready(client, jobs)
    body = confirmation()
    body.update(confirmation_kind="assistant_review", reviewed_at="forged", model_calls=["forged"])
    response = client.post(f"/api/jobs/{job_id}/confirm", json=body)
    assert response.status_code == 200, response.text
    approved = submitted[-1][2]
    assert approved["confirmation_kind"] == "assistant_review"
    assert approved["reviewed_at"] != "forged"
    assert approved["boundaries"][0]["method"] == "manual"
    assert "model_calls" not in approved


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(draft_id="stale"),
    lambda p: p.update(confirmed=False),
    lambda p: p.update(reviewer=[]),
    lambda p: p.update(boundaries=[None]),
    lambda p: p["boundaries"][0].update(shot_index=True),
    lambda p: p["boundaries"][0]["source_range_us"].update(begin=-1),
    lambda p: p["boundaries"][0]["source_range_us"].update(end=16_000_000),
    lambda p: p["approved_text_by_beat"].update({"beat-open": ""}),
    lambda p: p["approved_text_by_beat"].update({"beat-open": "   "}),
    lambda p: p["approved_text_by_beat"].update({"beat-open": "林间枝条"}),
    lambda p: p["approved_text_by_beat"].update({"beat-open": "我们买些。"}),
    lambda p: p["approved_text_by_beat"].update({"beat-open": "Nature."}),
])
def test_unapproved_stale_or_invalid_boundaries_never_start_voice(browser, mutation):
    client, jobs, submitted = browser
    job_id = review_ready(client, jobs)
    body = confirmation()
    mutation(body)
    response = client.post(f"/api/jobs/{job_id}/confirm", json=body)
    assert response.status_code in {409, 422}, response.text
    assert len(submitted) == 1


def test_media_and_artifacts_cannot_escape_project(browser, tmp_path):
    client, jobs, _ = browser
    job_id = upload(client)
    root = jobs.directory(job_id)
    secret = tmp_path / "outside.mp4"
    secret.write_bytes(b"private")
    write_json(root / "asset-manifest.json", {"assets": [{"asset_id": "a1", "uri": "../outside.mp4"}]})
    assert client.get(f"/api/jobs/{job_id}/media/a1").status_code == 400
    assert client.get(f"/api/jobs/{job_id}/files/settings").status_code == 404
    assert client.post(f"/api/jobs/{job_id}/open-editor").status_code == 409


def test_worker_errors_are_persisted_with_owner(browser, monkeypatch):
    client, jobs, _ = browser
    job_id = upload(client)
    from quickcut.production_pipeline import PipelineError
    def fail(*args, **kwargs):
        raise PipelineError("RETRIEVAL_FAILED", "图像损坏", stage="B")
    monkeypatch.setattr("quickcut.production_pipeline.analyze", fail)
    jobs.run(job_id, "analyze", None)
    data = client.get(f"/api/jobs/{job_id}").json()
    assert data["status"] == "failed" and data["error"]["stage"] == "B"
    assert data["error"]["code"] == "RETRIEVAL_FAILED"
    assert client.get(f"/api/jobs/{job_id}/files/error").status_code == 200


def test_failed_render_returns_to_review_without_reupload(browser):
    client, jobs, submitted = browser
    job_id = review_ready(client, jobs)
    jobs.update(job_id, status="failed", can_review=True, render_attempt=1,
                last_confirmation={"approved_text_by_beat": {"beat-open": "慢慢看风景。"}})
    response = client.post(f"/api/jobs/{job_id}/return-review")
    assert response.status_code == 200
    view = client.get(f"/api/jobs/{job_id}").json()
    assert view["draft"]["beats"][0]["narration_draft"] == "慢慢看风景。"
    assert client.post(f"/api/jobs/{job_id}/confirm", json=confirmation()).status_code == 200
    assert submitted[-1][2]["revision"] == 2


def test_selection_change_invalidates_stale_confirmation(browser):
    client, jobs, _ = browser
    job_id = review_ready(client, jobs)
    body = {"draft_id": frozen_draft()["draft_id"], "review_revision": 0,
            "selection_overrides": {"beat-open": ["c1"]}, "draft_text_edits": {"beat-open": "慢慢看风景。"}}
    response = client.post(f"/api/jobs/{job_id}/selection", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["draft"]["review_revision"] == 1
    assert client.post(f"/api/jobs/{job_id}/confirm", json=confirmation()).status_code == 409
    body["review_revision"] = 1
    body["selection_overrides"] = {"beat-open": ["not-in-draft"]}
    assert client.post(f"/api/jobs/{job_id}/selection", json=body).status_code == 422


def test_saved_trims_follow_candidate_through_reorder_and_replacement(browser):
    import copy
    client, jobs, submitted = browser
    job_id = review_ready(client, jobs)
    draft = frozen_draft()
    beat = draft["beats"][0]
    for number in (2, 3):
        candidate = copy.deepcopy(beat["candidates"][0])
        candidate.update(candidate_id=f"c{number}", segment_id=f"s{number}")
        candidate["source_range_us"] = {"begin": number * 20_000_000, "end": number * 20_000_000 + 15_000_000}
        beat["candidates"].append(candidate)
    beat["selected_candidate_ids"] = ["c1", "c2"]
    beat["selection"] = Selection(beat_id="beat-open", ordered_candidate_ids=["c1", "c2"], source="model:test").to_record()
    draft.pop("draft_id")
    draft["draft_id"] = content_hash(draft)
    write_json(jobs.directory(job_id) / "draft.json", draft)
    ranges = {"c1": {"begin": 2_000_000, "end": 5_000_000}, "c2": {"begin": 42_000_000, "end": 46_000_000}}
    body = {"draft_id": draft["draft_id"], "review_revision": 0,
            "selection_overrides": {"beat-open": ["c2", "c1"]},
            "draft_text_edits": {"beat-open": "修改未确认。"}, "draft_boundaries": [
                {"beat_id": "beat-open", "candidate_id": cid, "segment_id": cid.replace("c", "s"), "source_range_us": interval}
                for cid, interval in ranges.items()]}
    response = client.post(f"/api/jobs/{job_id}/selection", json=body)
    assert response.status_code == 200, response.text
    shots = response.json()["draft"]["beats"][0]["shots"]
    assert [(shot["candidate_id"], shot["source_range_us"]) for shot in shots] == [("c2", ranges["c2"]), ("c1", ranges["c1"])]
    assert jobs.get(job_id)["status"] == "needs_review"
    assert not jobs.get(job_id).get("last_confirmation") and len(submitted) == 1
    body.update(review_revision=1, selection_overrides={"beat-open": ["c3", "c1"]}, draft_boundaries=[])
    response = client.post(f"/api/jobs/{job_id}/selection", json=body)
    assert response.status_code == 200, response.text
    shots = client.get(f"/api/jobs/{job_id}").json()["draft"]["beats"][0]["shots"]
    assert shots[0]["candidate_id"] == "c3" and shots[0]["source_range_us"]["begin"] == 60_000_000
    assert shots[1]["source_range_us"] == ranges["c1"]
    assert "c2" not in jobs.get(job_id)["draft_boundary_edits"]["beat-open"]


def test_latest_confirmation_restores_text_and_manual_range_after_failure(browser):
    client, jobs, _ = browser
    job_id = review_ready(client, jobs)
    jobs.update(job_id, draft_text_edits={"beat-open": "旧草稿。"})
    body = confirmation()
    body["approved_text_by_beat"]["beat-open"] = "新的确认旁白。"
    body["boundaries"][0]["source_range_us"] = {"begin": 3_000_000, "end": 8_000_000}
    assert client.post(f"/api/jobs/{job_id}/confirm", json=body).status_code == 200
    jobs.update(job_id, status="failed", can_review=True)
    assert client.post(f"/api/jobs/{job_id}/return-review").status_code == 200
    beat = client.get(f"/api/jobs/{job_id}").json()["draft"]["beats"][0]
    assert beat["narration_draft"] == "新的确认旁白。"
    assert beat["shots"][0]["source_range_us"] == {"begin": 3_000_000, "end": 8_000_000}


def test_legacy_confirmation_recovers_boundary_by_frozen_selection(browser):
    client, jobs, _ = browser
    job_id = review_ready(client, jobs)
    body = confirmation()
    body["boundaries"][0]["source_range_us"] = {"begin": 4_000_000, "end": 9_000_000}
    jobs.update(job_id, status="failed", can_review=True, last_confirmation=body)
    assert client.post(f"/api/jobs/{job_id}/return-review").status_code == 200
    shot = client.get(f"/api/jobs/{job_id}").json()["draft"]["beats"][0]["shots"][0]
    assert shot["source_range_us"] == body["boundaries"][0]["source_range_us"]


@pytest.mark.parametrize("mutation", [
    lambda p: p["draft_boundaries"][0].update(candidate_id="not-in-draft"),
    lambda p: p["draft_boundaries"][0].update(segment_id="wrong-segment"),
    lambda p: p["draft_boundaries"][0]["source_range_us"].update(begin=True),
    lambda p: p["draft_boundaries"][0]["source_range_us"].update(end=17_000_000),
    lambda p: p["draft_text_edits"].update({"wrong-beat": "text"}),
])
def test_invalid_draft_edit_is_atomic_and_does_not_approve(browser, mutation):
    client, jobs, submitted = browser
    job_id = review_ready(client, jobs)
    before = jobs.get(job_id)
    body = {"draft_id": frozen_draft()["draft_id"], "review_revision": 0,
            "selection_overrides": {"beat-open": ["c1"]}, "draft_text_edits": {"beat-open": "草稿。"},
            "draft_boundaries": [{"beat_id": "beat-open", "candidate_id": "c1", "segment_id": "s1",
                                  "source_range_us": {"begin": 2_000_000, "end": 5_000_000}}]}
    mutation(body)
    assert client.post(f"/api/jobs/{job_id}/selection", json=body).status_code == 422
    assert jobs.get(job_id) == before and len(submitted) == 1


def test_unfinished_narration_can_be_saved_with_warning_but_never_confirmed(browser):
    client, jobs, submitted = browser
    job_id = review_ready(client, jobs)
    body = {"draft_id": frozen_draft()["draft_id"], "review_revision": 0,
            "selection_overrides": {"beat-open": ["c1"]}, "draft_text_edits": {"beat-open": "林间枝条"}}
    response = client.post(f"/api/jobs/{job_id}/selection", json=body)
    assert response.status_code == 200
    beat = response.json()["draft"]["beats"][0]
    assert beat["narration_draft"] == "林间枝条" and beat["narration_review"]["status"] == "requires_edit"
    assert beat["review_warnings"][0]["code"] == "NARRATION_INCOMPLETE_SENTENCE"
    approve = confirmation()
    approve.update(review_revision=1, approved_text_by_beat={"beat-open": "林间枝条"})
    assert client.post(f"/api/jobs/{job_id}/confirm", json=approve).status_code == 422
    assert len(submitted) == 1 and not jobs.get(job_id).get("last_confirmation")
    approve["approved_text_by_beat"]["beat-open"] = "枝条轻轻摇动。"
    assert client.post(f"/api/jobs/{job_id}/confirm", json=approve).status_code == 200
    assert len(submitted) == 2


@pytest.mark.parametrize("position", ["top", "bottom", "invalid"])
def test_subtitle_position_is_explicit_and_validated(browser, position):
    from quickcut.production_pipeline import read
    client, jobs, submitted = browser
    response = client.post("/api/jobs", data={"prompt": "字幕测试", "subtitle_position": position},
                           files=[("files", ("video.mp4", b"unit-test-input", "video/mp4"))])
    if position == "invalid":
        assert response.status_code == 422 and not submitted
    else:
        assert response.status_code == 200
        assert read(jobs.directory(response.json()["job_id"]) / "settings.json")["subtitle_position"] == position


def test_job_freezes_default_voice_config_and_reference_bytes(browser):
    import hashlib
    from quickcut.production_pipeline import REPO, read
    client, jobs, _ = browser
    assert client.get("/api/config").json()["default_voice"]["label"] == "默认测试男声"
    job_id = upload(client)
    root = jobs.directory(job_id)
    settings = read(root / "settings.json")
    frozen = read(root / settings["voice_config"])
    reference = Path(frozen["reference_audio"])
    expected = reference_wav()
    assert reference == root / "reference/voice.wav" and reference.read_bytes() == expected
    assert frozen["model_dir"] == str((REPO / "outputs/test-model").resolve())
    assert settings["reference_audio"]["kind"] == "default"
    assert settings["reference_audio"]["sha256"] == hashlib.sha256(expected).hexdigest()
    source = Path(jobs.runtime.voice_config)
    assert settings["voice_config_source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    original = read(source)
    Path(original["reference_audio"]).write_bytes(reference_wav(duration=2))
    original.update(model_dir="different-model", voice_label="新的全局音色")
    write_json(source, original)
    assert read(root / "voice-config.json") == frozen
    assert reference.read_bytes() == expected


def test_uploaded_wav_is_frozen_only_for_its_job(browser):
    import hashlib
    from quickcut.production_pipeline import read
    client, jobs, submitted = browser
    original = Path(jobs.runtime.voice_config).read_bytes()
    audio = reference_wav(duration=2.5, rate=24000)
    response = client.post("/api/jobs", data={"prompt": "使用上传参考音色"}, files=[
        ("files", ("video.mp4", b"unit-test-input", "video/mp4")),
        ("reference_audio", ("../我的男声.wav", audio, "audio/wav"))])
    assert response.status_code == 200, response.text
    root = jobs.directory(response.json()["job_id"])
    config = read(root / "voice-config.json")
    metadata = read(root / "settings.json")["reference_audio"]
    assert Path(config["reference_audio"]).read_bytes() == audio
    assert Path(config["reference_audio"]) == root / "reference/voice.wav"
    assert metadata["kind"] == "upload" and metadata["filename"] == "我的男声.wav"
    assert metadata["sha256"] == hashlib.sha256(audio).hexdigest()
    assert metadata["duration_us"] == 2_500_000 and metadata["sample_rate"] == 24000
    assert metadata["format"] == "PCM16" and metadata["channels"] == 1
    assert config["voice_label"] == "我的男声"
    assert Path(jobs.runtime.voice_config).read_bytes() == original
    assert submitted == [(response.json()["job_id"], "analyze")]


def test_per_cue_reference_cannot_bypass_the_jobs_single_frozen_voice(browser):
    from quickcut.production_pipeline import read
    client, jobs, _ = browser
    source = read(jobs.runtime.voice_config)
    source["cue_overrides"] = {"cue-a": {"reference_audio": "mutable-old-voice.wav", "seed": 17}, "cue-b": {"seed": 21}}
    write_json(Path(jobs.runtime.voice_config), source)
    job_id = upload(client)
    root = jobs.directory(job_id)
    frozen = read(root / "voice-config.json")
    assert frozen["cue_overrides"] == {"cue-a": {"reference_audio": frozen["reference_audio"], "seed": 17}, "cue-b": {"seed": 21}}
    assert read(root / "settings.json")["voice_config_source"]["normalized_cue_reference_ids"] == ["cue-a"]
    assert read(jobs.runtime.voice_config) == source


@pytest.mark.parametrize("filename,audio", [
    ("not-a-wave.mp3", reference_wav()),
    ("empty.wav", b""),
    ("invalid.wav", b"this is not a WAV"),
    ("short.wav", reference_wav(duration=0.99)),
    ("long.wav", reference_wav(duration=30.01)),
    ("silent.wav", reference_wav(silent=True)),
    ("pcm8.wav", reference_wav(width=1)),
    ("truncated.wav", reference_wav()[:-4]),
])
def test_invalid_reference_never_starts_analysis_or_keeps_partial_job(browser, filename, audio):
    client, jobs, submitted = browser
    response = client.post("/api/jobs", data={"prompt": "参考声音"}, files=[
        ("files", ("video.mp4", b"unit-test-input", "video/mp4")),
        ("reference_audio", (filename, audio, "audio/wav"))])
    assert response.status_code == 422, response.text
    assert not submitted and not list(jobs.root.iterdir())


def test_oversized_reference_is_rejected_before_analysis(browser, monkeypatch):
    client, jobs, submitted = browser
    monkeypatch.setattr("quickcut.workbench.MAX_REFERENCE_BYTES", 128)
    response = client.post("/api/jobs", data={"prompt": "参考声音"}, files=[
        ("files", ("video.mp4", b"unit-test-input", "video/mp4")),
        ("reference_audio", ("oversized.wav", reference_wav(), "audio/wav"))])
    assert response.status_code == 413
    assert not submitted and not list(jobs.root.iterdir())


@pytest.mark.parametrize("duration", [1, 30])
def test_reference_duration_boundaries_are_inclusive(browser, duration):
    client, _, _ = browser
    response = client.post("/api/jobs", data={"prompt": "参考声音"}, files=[
        ("files", ("video.mp4", b"unit-test-input", "video/mp4")),
        ("reference_audio", ("voice.wav", reference_wav(duration=duration), "audio/wav"))])
    assert response.status_code == 200, response.text


def test_cancel_worker_stops_actual_child_process(tmp_path, monkeypatch):
    import subprocess
    import sys
    import threading
    from quickcut import production_pipeline as pipeline
    real_popen = subprocess.Popen
    started = []
    def sleeping_child(command, **kwargs):
        child = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        started.append(child)
        return child
    monkeypatch.setattr(pipeline.subprocess, "Popen", sleeping_child)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(pipeline.PipelineError) as error:
        pipeline.worker(pipeline.Runtime(python=sys.executable), "ingest", {}, tmp_path, "cancel-check", cancel_event=cancel)
    assert error.value.code == "CANCELLED"
    assert len(started) == 1 and started[0].poll() is not None


def test_editor_install_tracks_export_revision_without_reusing_old_draft(tmp_path, monkeypatch):
    from quickcut import production_pipeline as pipeline
    installed = []
    def install(package, *, name):
        target = tmp_path / name
        target.mkdir()
        installed.append((package, name))
        return {"draft_name": name, "draft_directory": str(target)}
    monkeypatch.setattr("quickcut.export.install_jianying_draft", install)
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **kw: None)
    for revision in (1, 2):
        write_json(tmp_path / "run-report.json", {"revision": revision, "package_directory": f"export-r{revision}",
            "export": {"document_id": "export", "revision": revision}})
        opened = pipeline.open_editor(tmp_path)
        assert opened["draft_name"].endswith(f"-r{revision}")
        assert pipeline.open_editor(tmp_path) == opened
    assert len(installed) == 2
    assert all(Path(path).is_dir() for path in [tmp_path / name for _, name in installed])


def test_restart_recovers_completed_draft_during_first_voice_run(tmp_path):
    root = tmp_path / "jobs"
    job = root / "123456abcdef"
    job.mkdir(parents=True)
    write_json(job / "draft.json", frozen_draft())
    write_json(job / "job.json", {"job_id": job.name, "status": "rendering", "stage": "E"})
    app = create_app(root=root)
    with TestClient(app) as client:
        client.headers["x-quickcut-token"] = client.get("/api/config").json()["token"]
        state = client.get(f"/api/jobs/{job.name}").json()
        assert state["status"] == "interrupted" and state["can_review"] is True
        assert client.post(f"/api/jobs/{job.name}/return-review").status_code == 200
        assert client.get(f"/api/jobs/{job.name}").json()["draft"]["draft_id"] == frozen_draft()["draft_id"]

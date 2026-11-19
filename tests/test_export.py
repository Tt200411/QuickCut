"""F package tests use audible inputs, frozen contracts and a private registry."""
from __future__ import annotations

import array
from copy import deepcopy
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest

from quickcut import core, export, jianying, timeline_preview
from quickcut.media import digest
from quickcut.timeline import compile_timeline


def _envelope(kind, identifier):
    return dict(kind=kind, schema_version="0.1.0", document_id=identifier,
                project_id="export-test", revision=1, fixtures_only=False, inputs=[])


def _documents():
    brief = _envelope("Brief", "brief")
    brief.update(requirements={"target_duration_us": 1_600_000}, constraints={"preserve_source_audio": True})
    manifest = _envelope("AssetManifest", "manifest")
    manifest["assets"] = [dict(asset_id=aid, revision=1, media_kind="video", uri=f"{aid}.mp4",
        duration_us=1_600_000, content_hash="sha256:" + "0" * 64, audio={"present": True}) for aid in ("red", "blue")]
    manifest["segments"] = [dict(segment_id=aid, asset_id=aid, asset_revision=1,
        source_range_us={"begin": begin, "end": begin + 800_000}) for aid, begin in (("red", 200_001), ("blue", 400_001))]
    story = _envelope("StoryPlan", "story")
    story.update(status="ready_for_edit", unresolved_gaps=[], inputs=[timeline_preview._ref(d) for d in (manifest, brief)],
        narration_cues=[dict(cue_id="cue", beat_id="beat", text="可编辑字幕", duration_budget_us=500_000)],
        beats=[dict(beat_id="beat", order=1, duration_budget_us=1_600_000, cue_ids=["cue"],
                    shots=[dict(segment_id=aid, role="source", suggested_duration_us=800_000) for aid in ("red", "blue")])])
    voice = _envelope("VoicePackage", "voice")
    voice.update(status="ready", mode="measure", errors=[], inputs=[timeline_preview._ref(story)],
        assets=[dict(asset_id="speech", revision=1, media_kind="audio", uri="speech.wav",
                     content_hash="sha256:" + "0" * 64, duration_us=400_000, audio={"present": True})],
        cues=[dict(cue_id="cue", beat_id="beat", asset_id="speech", text="可编辑字幕", fit_status="fits", needs_retime=False,
                   duration_us=400_000, duration_budget_us=500_000,
                   local_subtitles=[dict(text="可编辑字幕", range_us={"begin": 50_001, "end": 300_001})])])
    timeline = compile_timeline(brief, manifest, story, voice, output={"width": 160, "height": 120,
        "fps": {"numerator": 25, "denominator": 1}, "sample_rate": 48000})
    return timeline, manifest, voice


@pytest.fixture(scope="module")
def source_media(tmp_path_factory):
    try:
        ffmpeg = core._ffmpeg_binary()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    if not timeline_preview._ffprobe_binary():
        pytest.skip("导出需要 ffprobe")
    root = tmp_path_factory.mktemp("f-audible-sources")
    for aid, color, frequency in (("red", "red", 400), ("blue", "blue", 600)):
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=25:d=1.6",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=1.6", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / f"{aid}.mp4")], check=True, capture_output=True)
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=0.4",
        "-c:a", "pcm_s16le", str(root / "speech.wav")], check=True, capture_output=True)
    return root


@pytest.fixture
def docs(source_media):
    result = _documents()
    for asset in result[1]["assets"] + result[2]["assets"]:
        asset["content_hash"] = digest(source_media / asset["uri"])
    return result


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _amplitude(path, frequency):
    with wave.open(str(path), "rb") as wav:
        samples = array.array("h", wav.readframes(wav.getnframes()))
        channels, rate = wav.getnchannels(), wav.getframerate()
    if sys.byteorder != "little":
        samples.byteswap()
    mono = samples[::channels]
    n = len(mono)
    real = sum(v * math.cos(2 * math.pi * frequency * i / rate) for i, v in enumerate(mono))
    imag = sum(v * math.sin(2 * math.pi * frequency * i / rate) for i, v in enumerate(mono))
    return 2 * math.hypot(real, imag) / n / 32768


@pytest.fixture
def webm_source(tmp_path):
    ffmpeg = core._ffmpeg_binary()
    if not timeline_preview._ffprobe_binary():
        pytest.skip("WebM measurement needs ffprobe")
    path = tmp_path / "short-video-long-audio.webm"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=160x120:r=25:d=0.8",
        "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=48000:duration=1.6",
        "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", str(path)],
        check=True, capture_output=True)
    return path


def test_webm_measures_playable_stream_instead_of_longer_container(webm_source):
    probe = timeline_preview._ffprobe_binary()
    info = timeline_preview._probe(probe, webm_source)
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    assert video.get("duration") in (None, "N/A")
    measured = timeline_preview._measured_stream(probe, webm_source, video)
    assert timeline_preview._duration(info["format"]) > 1_500_000
    assert timeline_preview._duration(measured) == 800_000
    assert measured["duration_measurement"]["method"] == "decoded_frames"
    tracks = {"video": {"kind": "video", "clips": [
        {"asset_id": "webm", "source_range_us": {"begin": 0, "end": 700_000}}]},
        "narration": {"kind": "narration", "clips": []}}
    measurement = export._inspect_media(webm_source, {"asset_id": "webm"}, tracks, core._ffmpeg_binary(), probe)
    assert measurement["duration_us"] == 800_000
    tracks["video"]["clips"][0]["source_range_us"]["end"] = 1_200_000
    with pytest.raises(export.ExportError, match="真实 video 流不能覆盖"):
        export._inspect_media(webm_source, {"asset_id": "webm"}, tracks, core._ffmpeg_binary(), probe)


@pytest.mark.parametrize("video_start,audio_start,source_begin", [
    (0, 0.069, 0), (0, 1.2, 0), (0.4, 1.6, 200_001),
])
def test_late_audio_start_is_padded_with_silence(tmp_path, video_start, audio_start, source_begin):
    ffmpeg = core._ffmpeg_binary()
    if not timeline_preview._ffprobe_binary():
        pytest.skip("导出需要 ffprobe")
    root = tmp_path / "project"
    root.mkdir()
    source = root / "late.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-copyts", "-f", "lavfi", "-i", "color=c=green:s=160x120:r=25:d=2",
        "-itsoffset", str(audio_start), "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1.93",
        "-vf", f"setpts=PTS+{video_start}/TB", "-fps_mode", "passthrough",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True, capture_output=True)
    probe = timeline_preview._probe(timeline_preview._ffprobe_binary(), source)
    audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert timeline_preview._start_time(audio) > 20_000
    speech = root / "speech.wav"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=0.4",
        "-c:a", "pcm_s16le", str(speech)], check=True, capture_output=True)
    _, _, voice = _documents()
    brief = _envelope("Brief", "brief")
    brief.update(requirements={"target_duration_us": 800_000}, constraints={"preserve_source_audio": True})
    manifest = _envelope("AssetManifest", "manifest")
    manifest["assets"] = [dict(asset_id="late", revision=1, media_kind="video", uri="late.mp4",
        duration_us=2_000_000, content_hash=digest(source), audio={"present": True})]
    manifest["segments"] = [dict(segment_id="late", asset_id="late", asset_revision=1,
        source_range_us={"begin": source_begin, "end": source_begin + 800_000})]
    story = _envelope("StoryPlan", "story")
    story.update(status="ready_for_edit", unresolved_gaps=[], inputs=[timeline_preview._ref(d) for d in (manifest, brief)],
        narration_cues=[dict(cue_id="cue", beat_id="beat", text="可编辑字幕", duration_budget_us=500_000)],
        beats=[dict(beat_id="beat", order=1, duration_budget_us=800_000, cue_ids=["cue"],
                    shots=[dict(segment_id="late", role="source", suggested_duration_us=800_000)])])
    voice["assets"][0]["content_hash"] = digest(speech)
    timeline = compile_timeline(brief, manifest, story, voice, output={"width": 160, "height": 120,
        "fps": {"numerator": 25, "denominator": 1}, "sample_rate": 48000})
    report = export.export_project(timeline, manifest, voice, root, tmp_path / "export", preview=True)
    assert report["validation"]["preview"] == "passed"
    derived = next(item for item in report["resources"] if item.get("derived_for_clip_id"))
    assert derived["leading_silence_samples"] > 900
    wav_path = tmp_path / "export" / derived["packaged_uri"]
    with wave.open(str(wav_path), "rb") as wav:
        assert abs(wav.getnframes() - 800_000 * wav.getframerate() / 1_000_000) <= 1
        samples = array.array("h", wav.readframes(wav.getnframes()))
    mono = samples[::2]
    head = mono[: int(0.02 * 48000)]
    tone = mono[int(0.12 * 48000): int(0.2 * 48000)]
    assert max(abs(v) for v in head) == 0
    if audio_start - video_start > 1:
        assert derived["leading_silence_samples"] == 38_400
        assert max(abs(v) for v in mono) == 0
        # The empty original-audio slice must not remove or replace the narration.
        mixed = root / "preview-audio.wav"
        subprocess.run([ffmpeg, "-v", "error", "-i", str(tmp_path / "export" / "preview.mp4"),
            "-map", "0:a:0", "-c:a", "pcm_s16le", str(mixed)], check=True, capture_output=True)
        assert _amplitude(mixed, 1000) > 0.03
    else:
        assert max(abs(v) for v in tone) > 1000


def test_audio_continuity_accepts_short_frames_sharing_a_container_tick(tmp_path):
    source = tmp_path / "coarse-audio-timestamps.mkv"
    subprocess.run([core._ffmpeg_binary(), "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=0.4", "-af", "asetnsamples=n=32:p=0",
        "-c:a", "pcm_s16le", str(source)], check=True, capture_output=True)
    probe = timeline_preview._ffprobe_binary()
    audio = next(stream for stream in timeline_preview._probe(probe, source)["streams"]
                 if stream["codec_type"] == "audio")
    frames = json.loads(subprocess.check_output([probe, "-v", "error", "-select_streams", "a:0",
        "-show_frames", "-show_entries", "frame=pts,nb_samples", "-of", "json", str(source)]))["frames"]
    assert audio["time_base"] == "1/1000"
    assert len(frames) == 600
    assert sum(frame["nb_samples"] for frame in frames) == 19200
    assert any(a["pts"] == b["pts"] for a, b in zip(frames, frames[1:]))
    timeline_preview._require_audio_continuity(probe, source, audio,
        [{"begin": 0, "end": 400_000}], origin_us=0)


@pytest.mark.parametrize("damage", ["interior_gap", "all_selected_audio_missing", "short_tail"])
def test_known_leading_silence_does_not_allow_missing_original_audio(tmp_path, source_media, docs, damage):
    root = tmp_path / "project"
    shutil.copytree(source_media, root)
    source = root / "bad-original.mkv"
    ffmpeg = core._ffmpeg_binary()
    length = 0.4 if damage == "short_tail" else 1.6
    command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=160x120:r=25:d=1.6",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={length}"]
    if damage != "short_tail":
        window = "0.4,0.6" if damage == "interior_gap" else "0.1,1.2"
        # Ten-millisecond PCM frames align with Matroska's timestamp ticks.
        # Resampler rounding must not accidentally reject an otherwise padded gap.
        command += ["-af", f"asetnsamples=n=480:p=0,aselect='not(between(t,{window}))'"]
    subprocess.run(command + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(source)],
                   check=True, capture_output=True)
    if damage != "short_tail":
        frames = json.loads(subprocess.check_output([timeline_preview._ffprobe_binary(), "-v", "error",
            "-select_streams", "a:0", "-show_frames", "-show_entries", "frame=pts_time,nb_samples",
            "-of", "json", str(source)]))["frames"]
        gaps = [(float(a["pts_time"]) + a["nb_samples"] / 48000, float(b["pts_time"]))
                for a, b in zip(frames, frames[1:])
                if float(b["pts_time"]) - float(a["pts_time"]) - a["nb_samples"] / 48000 > .1]
        assert len(gaps) == 1, "Fixture must contain a real decoded audio timestamp gap"
        assert sum(frame["nb_samples"] for frame in frames) < 1.6 * 48000
        if damage == "interior_gap":
            assert .2 < gaps[0][0] < gaps[0][1] < 1.0
        else:
            assert gaps[0][0] < .2 < 1.0 < gaps[0][1]
        audio = next(stream for stream in timeline_preview._probe(timeline_preview._ffprobe_binary(), source)["streams"]
                     if stream["codec_type"] == "audio")
        timeline_preview._require_audio_continuity(timeline_preview._ffprobe_binary(), source, audio,
            [{"begin": 20_000, "end": 80_000}], origin_us=0)  # An unused later gap must not reject this trim.
    docs[1]["assets"][0].update(uri=source.name, content_hash=digest(source))
    for consumer in ("preview", "export"):
        destination = tmp_path / ("rejected.mp4" if consumer == "preview" else "rejected-package")
        with pytest.raises((export.ExportError, timeline_preview.PreviewError),
                           match="原声采样|真实音频采样|真实 audio 流"):
            if consumer == "preview":
                timeline_preview.render_timeline_preview(*docs, root, destination)
            else:
                export.export_project(*docs, root, destination, preview=True)
        assert not destination.exists()


def test_export_and_preview_accept_webm_without_declared_stream_duration(tmp_path, source_media, docs):
    root = tmp_path / "project"
    shutil.copytree(source_media, root)
    webm = root / "red.webm"
    subprocess.run([core._ffmpeg_binary(), "-v", "error", "-i", str(root / "red.mp4"),
        "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", str(webm)],
        check=True, capture_output=True)
    asset = next(asset for asset in docs[1]["assets"] if asset["asset_id"] == "red")
    asset.update(uri="red.webm", content_hash=digest(webm))
    docs[0]["render_policy"]["preview_burn_subtitles"] = False
    report = export.export_project(*docs, root, tmp_path / "export", preview=True)
    assert report["status"] == "generated"
    assert export.verify_package(tmp_path / "export")[0] == report


@pytest.mark.parametrize("position,expected_y", [(None, -0.8), ("bottom", -0.8), ("top", 0.8)])
def test_editable_caption_position_matches_render_policy(tmp_path, source_media, docs, position, expected_y):
    if position is not None:
        docs[0]["render_policy"]["subtitle_position"] = position
    package = tmp_path / "caption-package"
    export.export_project(*docs, source_media, package)
    draft = _load(package / "draft/draft_info.json")
    texts = next(track for track in draft["tracks"] if track["type"] == "text")
    assert all(segment["clip"]["transform"] == {"x": 0.0, "y": expected_y} for segment in texts["segments"])
    assert all(segment["clip"]["transform"]["y"] == 0.0
               for track in draft["tracks"] if track["type"] == "video" for segment in track["segments"])
    assert "可编辑字幕" in (package / "captions.srt").read_text()
    assert export.verify_package(package)[0]["status"] == "generated"


def test_export_has_four_independent_tracks_exact_ranges_gain_and_audible_sources(tmp_path, source_media, docs):
    docs[0]["tracks"][1]["clips"][0]["gain"] = 0.17
    docs[0]["tracks"][2]["clips"][0]["gain"] = 0.73
    # Unused material must not be copied, and does not need to exist locally.
    docs[1]["assets"].append(dict(asset_id="unused", revision=1, media_kind="video", uri="missing.mp4",
                                 duration_us=1_600_000, content_hash="sha256:" + "a" * 64, audio={"present": True}))
    frozen = deepcopy(docs)
    before = {p.name: digest(p) for p in source_media.iterdir()}
    package = tmp_path / "export"
    report = export.export_project(*docs, source_media, package)
    assert docs == frozen
    assert before == {p.name: digest(p) for p in source_media.iterdir()}
    assert report["status"] == "generated"
    assert report["validation"]["editor_import"] == "not_run"
    assert report["inputs"] == [timeline_preview._ref(d) for d in docs]
    assert len(report["resources"]) == 5
    assert "unused" not in {r["asset_id"] for r in report["resources"]}
    native = _load(package / "draft/draft_info.json")
    assert [t["type"] for t in native["tracks"]] == ["video", "audio", "audio", "text"]
    assert len({t["id"] for t in native["tracks"]}) == 4
    video, source, narration, captions = native["tracks"]
    assert video["segments"][0]["source_timerange"] == {"start": 200_001, "duration": 800_000}
    assert video["segments"][1]["source_timerange"] == {"start": 400_001, "duration": 800_000}
    assert video["segments"][0]["volume"] == 0  # embedded original is not mixed twice
    assert source["segments"][0]["volume"] == 0.17
    assert narration["segments"][0]["volume"] == 0.73
    assert source["segments"][0]["source_timerange"] == {"start": 0, "duration": 800_000}
    assert captions["segments"][0]["target_timerange"] == {"start": 250_001, "duration": 250_000}
    assert json.loads(native["materials"]["texts"][0]["content"])["text"] == "可编辑字幕"
    assert native["new_version"] == "75.0.0"
    for resource in report["resources"]:
        path = package / resource["packaged_uri"]
        assert digest(path) == resource["content_hash"]
        if resource.get("derived_for_clip_id"):
            hz = 400 if resource["asset_id"] == "red" else 600
            assert _amplitude(path, hz) > 0.07
            assert _amplitude(path, 1000) < 0.004
        elif resource["asset_id"] == "speech":
            assert _amplitude(path, 1000) > 0.1
    assert export.verify_package(package)[0] == report
    original_manifest = (package / "export-manifest.json").read_bytes()
    with pytest.raises(export.ExportError, match="拒绝覆盖"):
        export.export_project(*docs, source_media, package)
    assert (package / "export-manifest.json").read_bytes() == original_manifest


@pytest.mark.parametrize("mutate", [
    lambda t, m, v: m.update(revision=2),
    lambda t, m, v: v.update(revision=2),
    lambda t, m, v: t["tracks"][0]["clips"][0].update(effects=[{"type": "fade"}]),
    lambda t, m, v: t["tracks"][1]["clips"][0].update(gain_envelope=[{"time_us": 0, "value": 1}]),
    lambda t, m, v: t["tracks"][0]["clips"][0]["playback_rate"].update(numerator=2),
    lambda t, m, v: t["render_policy"].update(editable_export_burn_subtitles=True),
    lambda t, m, v: t["tracks"].append({"track_id": "bgm", "kind": "music", "clips": []}),
])
def test_rejects_stale_contracts_and_unsupported_features_without_publishing(tmp_path, source_media, docs, mutate):
    mutate(*docs)
    with pytest.raises(export.ExportError):
        export.export_project(*docs, source_media, tmp_path / "no-export")
    assert not (tmp_path / "no-export").exists()
    assert not list(tmp_path.glob(".quickcut-export-*"))


@pytest.mark.parametrize('damage', ['missing_mode', 'fit_mode', 'request', 'cue_range'])
def test_export_rejects_disguised_initial_voice_before_media_access(tmp_path, damage, monkeypatch):
    docs = _documents()
    voice = docs[2]
    if damage == 'missing_mode':
        voice.pop('mode')
    elif damage == 'fit_mode':
        voice['mode'] = 'fit'
    elif damage == 'request':
        voice['request'] = {'kind': 'VoiceFitRequest'}
    else:
        voice['cues'][0]['requested_timeline_range_us'] = {'begin': 0, 'end': 400000}
    monkeypatch.setattr(export, '_inspect_media', lambda *args, **kwargs: pytest.fail('Invalid handoff reached media access'))
    package = tmp_path / 'rejected'
    with pytest.raises(export.ExportError) as exc:
        export.export_project(*docs, tmp_path, package)
    assert exc.value.code == 'TIMELINE_INVALID'
    assert not package.exists()
    assert not list(tmp_path.glob('.quickcut-export-*'))


def test_fixtures_require_explicit_opt_in_and_preserve_marker(tmp_path, source_media, docs):
    for doc in docs:
        doc["fixtures_only"] = True
    with pytest.raises(export.ExportError) as raised:
        export.export_project(*docs, source_media, tmp_path / "rejected")
    assert raised.value.code == "FIXTURE_EXPORT_REJECTED"
    report = export.export_project(*docs, source_media, tmp_path / "fixture", allow_fixtures=True)
    assert report["fixtures_only"] is True
    with pytest.raises(export.ExportError) as raised:
        export.verify_package(tmp_path / "fixture")
    assert raised.value.code == "FIXTURE_EXPORT_REJECTED"
    assert export.verify_package(tmp_path / "fixture", allow_fixtures=True)[0] == report


@pytest.mark.parametrize("failure", ["missing", "hash", "decode", "version"])
def test_bad_media_or_unknown_editor_never_publish(tmp_path, source_media, docs, failure):
    root = tmp_path / "inputs"
    shutil.copytree(source_media, root)
    options = {}
    if failure == "missing":
        (root / "red.mp4").unlink()
    elif failure in ("hash", "decode"):
        (root / "red.mp4").write_bytes(b"This is not a movie")
        if failure == "decode":
            docs[1]["assets"][0]["content_hash"] = digest(root / "red.mp4")
    else:
        options["target_version"] = "99.0.0"
    with pytest.raises(export.ExportError):
        export.export_project(*docs, root, tmp_path / "rejected", **options)
    assert not (tmp_path / "rejected").exists()
    assert not list(tmp_path.glob(".quickcut-export-*"))


def test_relocation_rebuilds_all_paths_preserves_hashes_and_existing_registry(tmp_path, source_media, docs, monkeypatch):
    package = tmp_path / "package"
    report = export.export_project(*docs, source_media, package)
    moved = tmp_path / "moved 中文 space" / "package"
    moved.parent.mkdir()
    package.rename(moved)
    drafts = tmp_path / "editor-drafts"
    drafts.mkdir()
    prior = {"all_draft_store": [{"draft_id": "unrelated-user-draft", "draft_name": "用户原项目", "unknown": [1, 2]}],
             "draft_ids": 1, "root_path": str(drafts), "unknown_root": "keep"}
    registry = drafts / "root_meta_info.json"
    registry.write_text(json.dumps(prior, ensure_ascii=False), encoding="utf-8")
    before = registry.read_bytes()
    monkeypatch.setattr(export, "is_editor_running", lambda: False)
    installed = export.install_jianying_draft(moved, drafts, name="搬家验证")
    target = Path(installed["draft_directory"])
    assert target.name == "QuickCut-搬家验证"
    assert Path(installed["registry_backup"]).read_bytes() == before
    after = _load(registry)
    assert after["all_draft_store"][1:] == prior["all_draft_store"]
    assert after["draft_ids"] == 2 and after["unknown_root"] == "keep"
    native = _load(target / "draft_info.json")
    paths = [m["path"] for k in ("videos", "audios") for m in native["materials"][k]]
    assert paths and all(Path(p).is_relative_to(target) and Path(p).is_file() for p in paths)
    assert all(str(package) not in p and str(moved) not in p for p in paths)
    for resource in report["resources"]:
        assert digest(target / resource["draft_uri"]) == resource["content_hash"]
    assert installed["editor_import_verified"] is False
    snapshot = registry.read_bytes()
    with pytest.raises(export.ExportError, match="拒绝覆盖"):
        export.install_jianying_draft(moved, drafts, name="搬家验证")
    assert registry.read_bytes() == snapshot
    assert not (drafts / ".quickcut-install.lock").exists()


def test_install_refuses_running_editor_and_tampered_or_missing_package(tmp_path, source_media, docs, monkeypatch):
    package = tmp_path / "package"
    report = export.export_project(*docs, source_media, package)
    monkeypatch.setattr(export, "is_editor_running", lambda: True)
    with pytest.raises(export.ExportError) as raised:
        export.install_jianying_draft(package, tmp_path / "drafts")
    assert raised.value.code == "EDITOR_RUNNING"
    assert not (tmp_path / "drafts").exists()
    media = package / report["resources"][0]["packaged_uri"]
    original = media.read_bytes()
    media.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(export.ExportError) as raised:
        export.verify_package(package)
    assert raised.value.code == "MEDIA_HASH_MISMATCH"
    media.unlink()
    with pytest.raises(export.ExportError) as raised:
        export.verify_package(package)
    assert raised.value.code == "PACKAGE_MISSING_MEDIA"


def test_resource_mapping_is_checked_against_frozen_inputs(tmp_path, source_media, docs):
    package = tmp_path / "package"
    report = export.export_project(*docs, source_media, package)
    report["resources"][0]["asset_revision"] += 1
    (package / "export-manifest.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(export.ExportError, match="资源版本"):
        export.verify_package(package)


def test_optional_preview_is_packaged_and_hash_verified(tmp_path, source_media, docs):
    docs[0]["render_policy"]["preview_burn_subtitles"] = False
    package = tmp_path / "package"
    result = export.export_project(*docs, source_media, package, preview=True)
    assert result["preview"] == "preview.mp4"
    assert result["validation"]["preview"] == "passed"
    assert result["validation"]["editor_import"] == "not_run"
    preview_report = _load(package / "preview.preview.json")
    assert preview_report["full_decode_verified"] is True
    assert all(Path(path).is_file() for path in preview_report["files"].values())
    assert export.verify_package(package)[0] == result


def test_failed_registry_update_rolls_back_only_new_draft(tmp_path, source_media, docs, monkeypatch):
    package = tmp_path / "package"
    export.export_project(*docs, source_media, package)
    root = tmp_path / "drafts"
    prior_dir = root / "原工程"
    prior_dir.mkdir(parents=True)
    (prior_dir / "user.txt").write_text("keep")
    registry = root / "root_meta_info.json"
    registry.write_text(json.dumps({"all_draft_store": [{"draft_name": "原工程"}], "draft_ids": 1}))
    before = registry.read_bytes()
    actual_write = export.write_json
    def failed_write(path, value):
        if path == registry:
            raise OSError("disk full")
        actual_write(path, value)
    monkeypatch.setattr(export, "is_editor_running", lambda: False)
    monkeypatch.setattr(export, "write_json", failed_write)
    with pytest.raises(OSError, match="disk full"):
        export.install_jianying_draft(package, root, name="new")
    assert registry.read_bytes() == before
    assert (prior_dir / "user.txt").read_text() == "keep"
    assert not (root / "QuickCut-new").exists()
    assert not (root / ".quickcut-install.lock").exists()
    assert len(list(root.glob("root_meta_info.quickcut-backup-*.json"))) == 1


def test_running_check_distinguishes_editor_from_helpers(monkeypatch):
    monkeypatch.setattr(export.sys, "platform", "darwin")
    monkeypatch.setattr(export.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=
        "/Applications/VideoFusion-macOS.app/Contents/Frameworks/JianyingHelper.app/Contents/MacOS/JianyingHelper\n"))
    assert export.is_editor_running() is False
    monkeypatch.setattr(export.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=
        "/Applications/VideoFusion-macOS.app/Contents/MacOS/VideoFusion-macOS\n"))
    assert export.is_editor_running() is True


def _resize_wav(path, difference):
    with wave.open(str(path), "rb") as wav:
        params = wav.getparams()
        pcm = wav.readframes(wav.getnframes())
    frame_size = params.nchannels * params.sampwidth
    pcm = pcm + bytes(difference * frame_size) if difference > 0 else pcm[:difference * frame_size]
    with wave.open(str(path), "wb") as wav:
        wav.setparams(params)
        wav.writeframes(pcm)


@pytest.mark.parametrize("difference", [-9600, -2, -1, 1, 2, 9600])
def test_narration_complete_wav_duration_matches_e_with_one_sample_tolerance(
        tmp_path, source_media, docs, difference):
    root = tmp_path / "sources"
    shutil.copytree(source_media, root)
    path = root / "speech.wav"
    _resize_wav(path, difference)
    docs[2]["assets"][0]["content_hash"] = digest(path)
    target = tmp_path / "export"
    if abs(difference) <= 1:
        export.export_project(*docs, root, target)  # default preview=False
        export.verify_package(target)
    else:
        with pytest.raises(export.ExportError):
            export.export_project(*docs, root, target)
        assert not target.exists()


def test_verify_package_rejects_coherent_hashes_but_false_narration_duration(tmp_path, source_media, docs):
    package = tmp_path / "package"
    report = export.export_project(*docs, source_media, package)
    resource = next(r for r in report["resources"] if r["asset_id"] == "speech")
    wav = package / resource["packaged_uri"]
    _resize_wav(wav, 9600)  # complete real WAV becomes 600ms; frozen duration is still 400ms.
    resource["content_hash"], resource["size_bytes"] = digest(wav), wav.stat().st_size
    voice_path = package / report["input_files"]["VoicePackage"]
    voice = _load(voice_path)
    voice["assets"][0]["content_hash"] = digest(wav)
    voice_path.write_text(json.dumps(voice), encoding="utf-8")
    report["input_hashes"]["VoicePackage"] = export._hash_json(voice)
    for item in report["files"]:
        if item["uri"] in {resource["packaged_uri"], report["input_files"]["VoicePackage"]}:
            path = package / item["uri"]
            item.update(content_hash=digest(path), size_bytes=path.stat().st_size)
    (package / "export-manifest.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(export.ExportError) as raised:
        export.verify_package(package)
    assert raised.value.code == "NARRATION_DURATION_MISMATCH"


def test_native_ids_stay_unique_when_contract_names_overlap(tmp_path, source_media, docs):
    # Contract clip and subtitle identifiers are separate sets; native IDs are global.
    docs[0]["tracks"][3]["cues"][0]["subtitle_id"] = docs[0]["tracks"][0]["clips"][0]["clip_id"]
    docs[0]["tracks"][0]["track_id"] = "red"  # also a material asset ID
    package = tmp_path / "package"
    export.export_project(*docs, source_media, package)
    native = _load(package / "draft/draft_info.json")
    ids = [t["id"] for t in native["tracks"]]
    ids += [s["id"] for t in native["tracks"] for s in t["segments"]]
    ids += [m["id"] for values in native["materials"].values() for m in values if "id" in m]
    assert len(ids) == len(set(ids))
    materials = {m["id"] for values in native["materials"].values() for m in values if "id" in m}
    assert all(s["material_id"] in materials for t in native["tracks"] for s in t["segments"])

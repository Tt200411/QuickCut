from __future__ import annotations

import array
from copy import deepcopy
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quickcut import core, timeline_preview as preview
from quickcut.media import digest
from quickcut.timeline import compile_timeline


def _envelope(kind, identifier):
    return dict(kind=kind, schema_version="0.1.0", document_id=identifier,
                project_id="preview-test", revision=1, fixtures_only=True, inputs=[])


def _documents():
    brief = _envelope("Brief", "brief")
    brief.update(requirements={"target_duration_us": 1_600_000},
                 constraints={"preserve_source_audio": True})
    manifest = _envelope("AssetManifest", "manifest")
    manifest["assets"] = [dict(asset_id=aid, revision=1, media_kind="video", uri=f"{aid}.mp4",
                               duration_us=1_600_000, content_hash="sha256:" + "0" * 64,
                               audio={"present": True}) for aid in ("red", "blue")]
    manifest["segments"] = [dict(segment_id=aid, asset_id=aid, asset_revision=1,
                                 source_range_us={"begin": begin, "end": begin + 800_000})
                            for aid, begin in (("red", 200_000), ("blue", 400_000))]
    story = _envelope("StoryPlan", "story")
    story.update(status="ready_for_edit", unresolved_gaps=[],
                 inputs=[preview._ref(d) for d in (manifest, brief)],
                 narration_cues=[dict(cue_id="cue", beat_id="beat", text="可编辑字幕", duration_budget_us=500_000)],
                 beats=[dict(beat_id="beat", order=1, duration_budget_us=1_600_000, cue_ids=["cue"],
                             shots=[dict(segment_id=aid, role="source", suggested_duration_us=800_000)
                                    for aid in ("red", "blue")])])
    voice = _envelope("VoicePackage", "voice")
    voice.update(status="ready", errors=[], inputs=[preview._ref(story)],
                 assets=[dict(asset_id="speech", revision=1, media_kind="audio", uri="speech.wav",
                              content_hash="sha256:" + "0" * 64, duration_us=400_000, audio={"present": True})],
                 cues=[dict(cue_id="cue", beat_id="beat", asset_id="speech", text="可编辑字幕",
                            fit_status="fits", needs_retime=False, duration_us=400_000, duration_budget_us=500_000,
                            local_subtitles=[dict(text="可编辑字幕", range_us={"begin": 50_000, "end": 300_000})])])
    timeline = compile_timeline(brief, manifest, story, voice, output={
        "width": 160, "height": 120, "fps": {"numerator": 25, "denominator": 1}, "sample_rate": 48000})
    return timeline, manifest, voice


@pytest.fixture
def ffmpeg():
    try:
        binary = core._ffmpeg_binary()
    except RuntimeError as exc:
        pytest.skip(f"需要 FFmpeg: {exc}")
    result = subprocess.run([binary, "-version"], capture_output=True)
    if result.returncode:
        pytest.skip("需要可执行的 FFmpeg")
    return binary


def _run(command):
    return subprocess.run(command, check=True, capture_output=True).stdout


@pytest.fixture
def media(tmp_path, ffmpeg):
    timeline, manifest, voice = _documents()
    for aid, size, color, hz, fps in (("red", "80x120", "red", 400, 20), ("blue", "160x80", "blue", 600, 30)):
        _run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r={fps}:d=1.6",
              "-f", "lavfi", "-i", f"sine=frequency={hz}:sample_rate=48000:duration=1.6",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(tmp_path / f"{aid}.mp4")])
    _run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=0.4",
          "-c:a", "pcm_s16le", str(tmp_path / "speech.wav")])
    for asset in manifest["assets"] + voice["assets"]:
        asset["content_hash"] = digest(tmp_path / asset["uri"])
    return timeline, manifest, voice


def test_accepts_current_d_without_mutating_inputs():
    docs = _documents()
    before = deepcopy(docs)
    assets, tracks = preview._validate(*docs)
    assert set(assets) == {"red", "blue", "speech"}
    assert set(tracks) == {"video", "source_audio", "narration", "subtitle"}
    assert docs == before


@pytest.mark.parametrize("mutate", [
    lambda t, m, v: t.update(duration_us=True),
    lambda t, m, v: t.update(duration_us=1.6),
    lambda t, m, v: t.update(revision=False),
    lambda t, m, v: t.update(status="draft"),
    lambda t, m, v: v.update(status="needs_retime"),
    lambda t, m, v: v.update(errors=["失败"]),
    lambda t, m, v: t.update(fixtures_only=False),
    lambda t, m, v: t.update(fixtures_only=1),
    lambda t, m, v: m.update(project_id="other"),
    lambda t, m, v: m.update(revision=2),
    lambda t, m, v: t["inputs"][1].update(revision=True),
    lambda t, m, v: v["inputs"][0].update(revision=2),
    lambda t, m, v: t["inputs"].append(deepcopy(t["inputs"][0])),
    lambda t, m, v: t["tracks"][0]["clips"][0].update(asset_revision=True),
    lambda t, m, v: t["tracks"][0]["clips"][0].update(asset_revision=2),
    lambda t, m, v: t["tracks"][0]["clips"][0]["source_range_us"].update(begin=True),
    lambda t, m, v: t["tracks"][0]["clips"][0]["source_range_us"].update(end=9_000_000),
    lambda t, m, v: t["tracks"][0]["clips"][1]["timeline_range_us"].update(begin=900_000),
    lambda t, m, v: t["tracks"][0]["clips"][1]["timeline_range_us"].update(begin=700_000),
    lambda t, m, v: t["tracks"][0]["clips"][0]["playback_rate"].update(numerator=2),
    lambda t, m, v: t["tracks"][0]["clips"][0].update(effects=[{"kind": "fade"}]),
    lambda t, m, v: t.update(transitions=[{"kind": "dissolve"}]),
    lambda t, m, v: t["tracks"][0]["clips"][0].update(speed=2),
    lambda t, m, v: t["tracks"][1]["clips"][0].update(gain=float("nan")),
    lambda t, m, v: t["tracks"][1]["clips"][0].update(gain=True),
    lambda t, m, v: t["tracks"][1]["clips"][0].update(linked_video_clip_id="unknown"),
    lambda t, m, v: t["tracks"][1]["clips"][0]["source_range_us"].update(begin=0),
    lambda t, m, v: t["tracks"][3]["cues"][0].update(editable=False),
    lambda t, m, v: t["tracks"][3]["cues"][0].update(text="改写了旁白"),
    lambda t, m, v: t["tracks"][3]["cues"][0]["timeline_range_us"].update(begin=0),
    lambda t, m, v: v["cues"][0].update(needs_retime=True),
    lambda t, m, v: v["cues"][0].update(duration_us=500_000),
    lambda t, m, v: v["cues"][0].update(script_input={"kind": "StoryPlan", "document_id": "story", "revision": 2}),
    lambda t, m, v: t["output"].update(width=159),
    lambda t, m, v: t["output"].update(sample_rate=1),
    lambda t, m, v: t["output"]["fps"].update(numerator=1000),
    lambda t, m, v: v["assets"].append(deepcopy(m["assets"][0])),
    lambda t, m, v: m["segments"][0].update(asset_revision=True),
    lambda t, m, v: m["segments"][0]["source_range_us"].update(end=900_000),
    lambda t, m, v: t["tracks"][0]["clips"][0].update(segment_id="unknown"),
    lambda t, m, v: m.update(processing={"status": "partial", "errors": ["未完成"]}),
    lambda t, m, v: t["tracks"][1]["clips"][1]["timeline_range_us"].update(begin=700_000),
    lambda t, m, v: t["tracks"][3]["cues"][0].update(text="第一行\n\n第二行"),
])
def test_rejects_invalid_contract(mutate):
    docs = _documents()
    mutate(*docs)
    with pytest.raises(preview.PreviewError):
        preview._validate(*docs)


def test_global_srt_uses_d_offsets_and_rejects_collapsed_intervals():
    timeline, _, _ = _documents()
    assert preview._srt(timeline["tracks"][3]["cues"]) == "1\n00:00:00,250 --> 00:00:00,500\n可编辑字幕\n"
    with pytest.raises(preview.PreviewError, match="毫秒"):
        preview._srt([dict(text="短", timeline_range_us={"begin": 10, "end": 20})])


def _pixels(ffmpeg, movie, position):
    return _run([ffmpeg, "-v", "error", "-i", str(movie), "-ss", str(position),
                 "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])


def _amplitude(samples, hz, begin, end):
    part = samples[round(begin * 48000):round(end * 48000)]
    n = len(part)
    real = sum(x * math.cos(2 * math.pi * hz * i / 48000) for i, x in enumerate(part))
    imag = sum(x * math.sin(2 * math.pi * hz * i / 48000) for i, x in enumerate(part))
    return 2 * math.hypot(real, imag) / n


def test_render_decodable_hard_cuts_scale_pad_audio_offsets_and_gains(tmp_path, media, ffmpeg):
    movie = tmp_path / "preview.mp4"
    report = preview.render_timeline_preview(*media, tmp_path, movie)
    assert report["full_decode_verified"] is True
    assert report["fixtures_only"] is True
    assert report["subtitles_burned"] is False
    assert report["editor_project_exported"] is False
    assert report["f_acceptance_completed"] is False
    assert report["measured_duration_us"] == 1_600_000
    assert report["source_measurements_us"] == {
        "blue": {"video": 1_600_000, "audio": 1_600_000},
        "red": {"video": 1_600_000, "audio": 1_600_000}, "speech": {"audio": 400_000}}
    assert report["duration_validation"] == "ffprobe"
    assert json.loads(movie.with_suffix(".preview.json").read_text()) == report
    assert "00:00:00,250 --> 00:00:00,500" in movie.with_suffix(".srt").read_text()
    all_frames = _run([ffmpeg, "-v", "error", "-i", str(movie), "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    assert len(all_frames) == 40 * 160 * 120 * 3
    red, blue = _pixels(ffmpeg, movie, 0.76), _pixels(ffmpeg, movie, 0.84)
    center = (60 * 160 + 80) * 3
    assert red[center] > 180 and red[center + 2] < 30
    assert blue[center + 2] > 180 and blue[center] < 30
    assert max(red[:3]) < 10
    assert max(blue[:3]) < 10
    pcm = array.array("f")
    pcm.frombytes(_run([ffmpeg, "-v", "error", "-i", str(movie), "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"]))
    if sys.byteorder != "little":
        pcm.byteswap()
    assert abs(len(pcm) / 48000 - 1.6) < 0.03
    assert _amplitude(pcm, 1000, 0.04, 0.14) < 0.004
    assert 0.09 < _amplitude(pcm, 1000, 0.3, 0.5) < 0.15
    assert _amplitude(pcm, 1000, 0.65, 0.75) < 0.004
    assert 0.02 < _amplitude(pcm, 400, 0.04, 0.14) < 0.055
    assert 0.02 < _amplitude(pcm, 600, 1.0, 1.2) < 0.055
    assert _amplitude(pcm, 400, 1.0, 1.2) < 0.004
    original = movie.read_bytes()
    with pytest.raises(preview.PreviewError, match="拒绝覆盖"):
        preview.render_timeline_preview(*media, tmp_path, movie)
    assert movie.read_bytes() == original


def test_ffprobe_validates_real_streams_and_duration(tmp_path, media):
    if not preview._ffprobe_binary():
        pytest.skip("需要 ffprobe 或 QUICKCUT_FFPROBE")
    report = preview.render_timeline_preview(*media, tmp_path, tmp_path / "probed.mp4")
    assert report["duration_validation"] == "ffprobe"
    assert abs(report["measured_duration_us"] - 1_600_000) <= report["duration_tolerance_us"]
    assert report["measured_stream_durations_us"]["audio"] == pytest.approx(1_600_000, abs=2000)


@pytest.mark.parametrize("uri", ["../red.mp4", "/tmp/red.mp4", "https://host/red.mp4", "a\\red.mp4"])
def test_unsafe_media_uri(tmp_path, media, uri):
    media[1]["assets"][1]["uri"] = uri
    with pytest.raises(ValueError, match="Unsafe|escapes"):
        preview.render_timeline_preview(*media, tmp_path, tmp_path / "unsafe.mp4")
    assert not (tmp_path / "unsafe.mp4").exists()


def test_symlink_escape(tmp_path, media):
    # 指向项目根之外的路径，解析阶段即拒绝，不需要读取目标。
    (tmp_path / "escape.mp4").symlink_to(tmp_path.parent / "outside.mp4")
    media[1]["assets"][1]["uri"] = "escape.mp4"
    with pytest.raises(ValueError, match="escapes"):
        preview.render_timeline_preview(*media, tmp_path, tmp_path / "unsafe.mp4")


@pytest.mark.parametrize("bad_hash", ["fixture:blue", "sha256:" + "f" * 64])
def test_rejects_placeholder_or_mismatched_hash(tmp_path, media, bad_hash):
    media[1]["assets"][1]["content_hash"] = bad_hash
    with pytest.raises(preview.PreviewError, match="sha256|SHA-256"):
        preview.render_timeline_preview(*media, tmp_path, tmp_path / "bad.mp4")
    assert not (tmp_path / "bad.mp4").exists()


@pytest.mark.parametrize("real_duration", [0.2, 0.6])
def test_incorrect_real_narration_is_not_padded_or_truncated(tmp_path, media, ffmpeg, real_duration):
    short = tmp_path / "short.wav"
    _run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=1000:sample_rate=48000:duration={real_duration}", str(short)])
    media[2]["assets"][0].update(uri="short.wav", content_hash=digest(short))
    with pytest.raises(preview.PreviewError, match="采样不足|真实 audio 流不足"):
        preview.render_timeline_preview(*media, tmp_path, tmp_path / "short.mp4")
    assert not (tmp_path / "short.mp4").exists()
    assert not (tmp_path / "short.srt").exists()


def test_probe_failure_does_not_fall_back_or_publish(tmp_path, media, ffmpeg, monkeypatch):
    monkeypatch.setattr(preview, "_ffprobe_binary", lambda: ffmpeg)

    def fail(*args):
        raise preview.PreviewError("ffprobe 原始失败")

    monkeypatch.setattr(preview, "_probe", fail)
    with pytest.raises(preview.PreviewError, match="ffprobe 原始失败"):
        preview.render_timeline_preview(*media, tmp_path, tmp_path / "failed.mp4")
    assert not (tmp_path / "failed.mp4").exists()


def test_silent_timeline_and_empty_subtitles(tmp_path, media):
    timeline, manifest, voice = deepcopy(media)
    for clip in timeline["tracks"][0]["clips"]:
        clip["audio_policy"] = "muted"
    timeline["tracks"][1]["clips"] = []
    timeline["tracks"][2]["clips"] = []
    timeline["tracks"][3]["cues"] = []
    voice.update(assets=[], cues=[])
    report = preview.render_timeline_preview(timeline, manifest, voice, tmp_path, tmp_path / "silent.mp4")
    assert report["full_decode_verified"] is True
    assert (tmp_path / "silent.srt").read_text() == ""


def test_existing_sidecar_is_not_overwritten(tmp_path):
    movie = tmp_path / "present.mp4"
    srt = movie.with_suffix(".srt")
    srt.write_text("已有字幕")
    with pytest.raises(preview.PreviewError, match="拒绝覆盖"):
        preview.render_timeline_preview(*_documents(), tmp_path, movie)
    assert srt.read_text() == "已有字幕"
    assert not movie.exists()


def test_command_failure_keeps_stderr():
    with pytest.raises(preview.PreviewError, match="原始错误"):
        preview._run([sys.executable, "-c", "import sys; sys.stderr.write('原始错误'); sys.exit(7)"])


def test_cli_entrypoint(tmp_path, media):
    script = Path(__file__).resolve().parents[1] / "scripts" / "render_timeline_preview.py"
    spec = importlib.util.spec_from_file_location("render_preview_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = []
    for flag, document in zip(("--timeline", "--manifest", "--voice-package"), media):
        path = tmp_path / f"{document['kind']}.json"
        path.write_text(json.dumps(document))
        args.extend([flag, str(path)])
    args += ["--project-root", str(tmp_path), "--output", str(tmp_path / "cli.mp4")]
    assert module.main(args) == 0
    assert module.main(args) == 1


def _video_documents(shots, *, source_audio=False):
    timeline, manifest, voice = _documents()
    video_template = timeline["tracks"][0]["clips"][0]
    audio_template = timeline["tracks"][1]["clips"][0]
    videos, audios = [], []
    position = 0
    for index, (aid, begin, duration) in enumerate(shots):
        clip = deepcopy(video_template)
        clip.update(clip_id=f"video-{index}", asset_id=aid, segment_id=aid,
                    source_range_us={"begin": begin, "end": begin + duration},
                    timeline_range_us={"begin": position, "end": position + duration},
                    audio_policy="separate_track" if source_audio else "muted")
        videos.append(clip)
        if source_audio:
            audio = deepcopy(audio_template)
            audio.update(clip_id=f"audio-{index}", asset_id=aid, linked_video_clip_id=clip["clip_id"],
                         source_range_us=deepcopy(clip["source_range_us"]),
                         timeline_range_us=deepcopy(clip["timeline_range_us"]), gain=1)
            audios.append(audio)
        position += duration
    timeline["duration_us"] = position
    timeline["tracks"][0]["clips"] = videos
    timeline["tracks"][1]["clips"] = audios
    timeline["tracks"][2]["clips"] = []
    timeline["tracks"][3]["cues"] = []
    voice.update(assets=[], cues=[])
    for segment in manifest["segments"]:
        segment["source_range_us"] = {"begin": 0, "end": 1_600_000}
    return timeline, manifest, voice


@pytest.mark.parametrize("video_start,audio_start,source_begin", [(0.4, 0, 200_000), (1.2, 0.8, 200_000), (0.4, 0.7, 0)])
def test_nonzero_video_origin_preserves_trim_content_and_audio_offset(
        tmp_path, ffmpeg, video_start, audio_start, source_begin):
    timeline, manifest, voice = _video_documents([("red", source_begin, 800_000)], source_audio=True)
    source = tmp_path / "offset.mov"
    _run([ffmpeg, "-v", "error", "-copyts", "-f", "lavfi", "-i", "color=red:s=160x120:r=25:d=0.4",
          "-f", "lavfi", "-i", "color=blue:s=160x120:r=25:d=1.2",
          "-f", "lavfi", "-i", "aevalsrc=sin(2*PI*if(lt(t\\,0.6)\\,400\\,1000)*t)/8:s=48000:d=2.4",
          "-filter_complex", f"[0:v][1:v]concat=n=2:v=1:a=0,setpts=PTS+{video_start}/TB[v];"
          f"[2:a]asetpts=PTS+{audio_start}/TB[a]", "-map", "[v]", "-map", "[a]",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-c:a", "pcm_s16le", str(source)])
    manifest["assets"][0].update(uri=source.name, content_hash=digest(source))
    info = preview._probe(preview._ffprobe_binary(), source)
    starts = {stream["codec_type"]: preview._start_time(stream) for stream in info["streams"]}
    assert starts == {"video": round(video_start * 1_000_000), "audio": round(audio_start * 1_000_000)}
    movie = tmp_path / "offset-preview.mp4"
    report = preview.render_timeline_preview(timeline, manifest, voice, tmp_path, movie)
    assert report["measured_video_frames"] == 20
    center = (60 * 160 + 80) * 3
    cut = 0.4 - source_begin / 1_000_000
    before_cut = _pixels(ffmpeg, movie, cut - 0.04)
    after_cut = _pixels(ffmpeg, movie, cut + 0.04)
    assert before_cut[center] > 180 and before_cut[center + 2] < 30
    assert after_cut[center + 2] > 180 and after_cut[center] < 30
    pcm = array.array("f")
    pcm.frombytes(_run([ffmpeg, "-v", "error", "-i", str(movie), "-map", "0:a:0", "-ac", "1",
                        "-ar", "48000", "-f", "f32le", "-"]))
    if sys.byteorder != "little":
        pcm.byteswap()
    if audio_start < video_start:
        assert _amplitude(pcm, 1000, 0.04, 0.14) > 0.08
        assert _amplitude(pcm, 400, 0.04, 0.14) < 0.004
    else:
        assert max(abs(x) for x in pcm[2400:9600]) < 0.004
        assert _amplitude(pcm, 400, 0.4, 0.5) > 0.08


@pytest.mark.parametrize("fps_numerator,fps_denominator", [(25, 1), (30000, 1001)])
def test_global_frame_boundaries_prevent_many_short_clip_drift(tmp_path, media, ffmpeg, fps_numerator, fps_denominator):
    shots = [("red" if index % 2 == 0 else "blue", 0, 100_000) for index in range(20)]
    timeline, manifest, voice = _video_documents(shots)
    for asset, actual in zip(manifest["assets"], media[1]["assets"]):
        asset["content_hash"] = actual["content_hash"]
    timeline["output"]["fps"] = {"numerator": fps_numerator, "denominator": fps_denominator}
    movie = tmp_path / "short-clips.mp4"
    report = preview.render_timeline_preview(timeline, manifest, voice, tmp_path, movie)
    # 独立计算每帧归属，逐个切点检查，不能仅检查最终总长。
    denominator = 1_000_000 * fps_denominator
    boundaries = [(index * 100_000 * fps_numerator + denominator - 1) // denominator for index in range(21)]
    frames = _run([ffmpeg, "-v", "error", "-i", str(movie), "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    stride = 160 * 120 * 3
    center = (60 * 160 + 80) * 3
    assert len(frames) == boundaries[-1] * stride
    assert report["measured_video_frames"] == boundaries[-1]
    assert abs(report["measured_stream_durations_us"]["video"] - 2_000_000) < 34_000
    for index in range(20):
        assert report["video_frame_ranges"][index] == {
            "clip_id": f"video-{index}", "begin_frame": boundaries[index], "end_frame": boundaries[index + 1]}
        for frame in range(boundaries[index], boundaries[index + 1]):
            pixel = frames[frame * stride + center:frame * stride + center + 3]
            assert pixel[0 if index % 2 == 0 else 2] > 180, (index, frame, pixel)
            assert pixel[2 if index % 2 == 0 else 0] < 30, (index, frame, pixel)


def test_short_real_video_cannot_pass_declared_source_bounds(tmp_path, ffmpeg):
    docs = _video_documents([("red", 0, 800_000)])
    source = tmp_path / "red.mp4"
    _run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=red:s=160x120:r=25:d=0.4",
          "-c:v", "libx264", str(source)])
    docs[1]["assets"][0].update(duration_us=800_000, content_hash=digest(source))
    docs[1]["segments"][0]["source_range_us"]["end"] = 800_000
    with pytest.raises(preview.PreviewError, match="真实 video 流不足"):
        preview.render_timeline_preview(*docs, tmp_path, tmp_path / "rejected.mp4")
    assert not list(tmp_path.glob("rejected.*"))


def test_missing_ffprobe_rejects_short_media_without_publishing(tmp_path, ffmpeg, monkeypatch):
    docs = _video_documents([("red", 0, 800_000)])
    source = tmp_path / "red.mp4"
    _run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=red:s=160x120:r=25:d=0.4",
          "-c:v", "libx264", str(source)])
    docs[1]["assets"][0].update(duration_us=800_000, content_hash=digest(source))
    docs[1]["segments"][0]["source_range_us"]["end"] = 800_000
    monkeypatch.delenv("QUICKCUT_FFPROBE", raising=False)
    monkeypatch.setattr(preview.shutil, "which", lambda name: None)
    with pytest.raises(preview.PreviewError) as error:
        preview.render_timeline_preview(*docs, tmp_path, tmp_path / "missing.mp4")
    assert error.value.code == "ffprobe_required"
    assert not list(tmp_path.glob("missing.*"))


def test_cli_subprocess_success_and_missing_ffprobe_exit(tmp_path, media, ffmpeg):
    script = Path(__file__).resolve().parents[1] / "scripts" / "render_timeline_preview.py"
    args = []
    for flag, doc in zip(("--timeline", "--manifest", "--voice-package"), media):
        path = tmp_path / f"{doc['kind']}.json"
        path.write_text(json.dumps(doc))
        args += [flag, str(path)]
    args += ["--project-root", str(tmp_path), "--output", str(tmp_path / "subprocess.mp4")]
    result = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["measured_video_frames"] == 40
    args[-1] = str(tmp_path / "missing.mp4")
    env = {**os.environ, "PATH": str(tmp_path), "QUICKCUT_FFMPEG": ffmpeg}
    env.pop("QUICKCUT_FFPROBE", None)
    result = subprocess.run([sys.executable, str(script), *args], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert not result.stdout
    assert json.loads(result.stderr)["errors"][0]["code"] == "ffprobe_required"
    assert not list(tmp_path.glob("missing.*"))


def test_quickcut_ffmpeg_override(tmp_path, media, ffmpeg, monkeypatch):
    monkeypatch.setenv("QUICKCUT_FFMPEG", ffmpeg)
    calls = []
    original = core._ffmpeg_binary

    def binary():
        calls.append(os.getenv("QUICKCUT_FFMPEG"))
        return original()

    monkeypatch.setattr(core, "_ffmpeg_binary", binary)
    preview.render_timeline_preview(*media, tmp_path, tmp_path / "configured.mp4")
    assert calls == [ffmpeg]

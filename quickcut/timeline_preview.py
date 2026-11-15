"""D 的 0.1.0 时间线预览：MP4、可编辑全局 SRT；不导出剪辑工程。"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import core
from .media import digest, resolve_uri

Document = dict[str, Any]


class PreviewError(ValueError):
    """契约或媒体检查失败；外部命令失败保留 stderr。"""

    def __init__(self, message: str, *, code: str = "preview_validation_failed"):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> Document:
        return {"status": "preview_rejected", "errors": [{"code": self.code, "message": str(self)}]}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PreviewError(message)


def _integer(value: Any, label: str, minimum: int = 1) -> int:
    _require(type(value) is int and value >= minimum, f"{label} 必须是 >= {minimum} 的整数")
    return value


def _text(value: Any, label: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{label} 必须是非空字符串")
    return value


def _index(records: Any, key: str) -> dict[str, Document]:
    _require(isinstance(records, list), f"{key} 集合必须是列表")
    result = {}
    for record in records:
        _require(isinstance(record, dict), f"{key} 记录必须是对象")
        identifier = _text(record.get(key), key)
        _require(identifier not in result, f"重复 {key}: {identifier}")
        result[identifier] = record
    return result


def _range(value: Any, maximum: int, label: str) -> tuple[int, int]:
    _require(isinstance(value, dict), f"{label} 必须是区间对象")
    begin = _integer(value.get("begin"), f"{label}.begin", 0)
    end = _integer(value.get("end"), f"{label}.end")
    _require(begin < end <= maximum, f"{label} 为空、倒序或越界")
    return begin, end


def _refs(doc: Document) -> dict[str, Document]:
    links = _index(doc.get("inputs"), "kind")
    for link in links.values():
        _require(set(link) == {"kind", "document_id", "revision"}, "引用必须包含且仅包含 kind/document_id/revision")
        _text(link.get("document_id"), "引用 document_id")
        _integer(link.get("revision"), "引用 revision")
    return links


def _ref(doc: Document) -> Document:
    return {key: doc[key] for key in ("kind", "document_id", "revision")}


def _no_effects(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"effects", "effect", "transitions", "transition", "filters", "transform"}:
                _require(item in (None, [], {}), f"不支持 {key}")
            if key in {"speed", "time_remap"}:
                raise PreviewError(f"不支持 {key}，仅支持 playback_rate 1×")
            _no_effects(item)
    elif isinstance(value, list):
        for item in value:
            _no_effects(item)


def _validate(timeline: Document, manifest: Document, voice: Document) -> tuple[dict, dict]:
    docs = {"EditTimeline": timeline, "AssetManifest": manifest, "VoicePackage": voice}
    for kind, doc in docs.items():
        _require(isinstance(doc, dict), f"{kind} 必须是对象")
        _require(doc.get("kind") == kind and doc.get("schema_version") == "0.1.0", f"需要 {kind} 0.1.0")
        _text(doc.get("document_id"), f"{kind}.document_id")
        _text(doc.get("project_id"), f"{kind}.project_id")
        _integer(doc.get("revision"), f"{kind}.revision")
        _require(type(doc.get("fixtures_only")) is bool, f"{kind}.fixtures_only 必须是布尔值")
        _require(doc["project_id"] == timeline["project_id"], "文档 project_id 不一致")
    _require(len({d["document_id"] for d in docs.values()}) == 3, "文档 ID 重复")
    links = {kind: _refs(doc) for kind, doc in docs.items()}
    tl = links["EditTimeline"]
    _require(set(tl) == {"Brief", "StoryPlan", "AssetManifest", "VoicePackage"}, "timeline 缺少或包含未知输入引用")
    for kind in ("AssetManifest", "VoicePackage"):
        _require(tl[kind] == _ref(docs[kind]), f"timeline 引用了旧版本 {kind}")
    _require(links["VoicePackage"].get("StoryPlan") == tl["StoryPlan"], "VoicePackage 的 StoryPlan 引用过期")
    _require(set(links["VoicePackage"]) == {"StoryPlan"}, "仅支持 D 初次编排的 VoicePackage 引用")
    for kind, link in links["AssetManifest"].items():
        _require(kind in tl and link == tl[kind], "AssetManifest 的上游引用不一致")
    _require(not (manifest["fixtures_only"] or voice["fixtures_only"]) or timeline["fixtures_only"], "timeline 丢失 fixture 来源标记")
    _require(timeline.get("status") == "ready_for_export" and not timeline.get("errors"), "timeline 必须 ready_for_export 且无错误")
    _require(voice.get("status") == "ready" and not voice.get("errors"), "VoicePackage 必须 ready 且无错误")
    if "processing" in manifest:
        _require(manifest["processing"].get("status") == "success" and not manifest["processing"].get("errors"), "AssetManifest 处理尚未成功")
    _require(not timeline.get("unresolved_gaps") and not timeline.get("pending_reviews"), "timeline 仍有缺口或待审项")
    _no_effects(timeline)
    duration = _integer(timeline.get("duration_us"), "duration_us")
    output = timeline.get("output", {})
    for key in ("width", "height"):
        value = _integer(output.get(key), key)
        _require(16 <= value <= 3840 and value % 2 == 0, f"{key} 必须是 16..3840 内的偶数")
    _require(output["width"] * output["height"] <= 8_294_400, "预览分辨率超过 4K 像素预算")
    fps = output.get("fps", {})
    rate = Fraction(_integer(fps.get("numerator"), "fps.numerator"),
                    _integer(fps.get("denominator"), "fps.denominator"))
    _require(1 <= rate <= 120, "fps 必须在 1..120 范围")
    _require(_integer(output.get("sample_rate"), "sample_rate") in {16000, 22050, 24000, 32000, 44100, 48000, 96000}, "不支持的 sample_rate")
    assets = _index(manifest.get("assets", []) + voice.get("assets", []), "asset_id")
    for asset in assets.values():
        _integer(asset.get("revision"), "asset.revision")
        _integer(asset.get("duration_us"), "asset.duration_us")
        _require(asset.get("media_kind") in {"video", "audio"}, "仅支持 video/audio 素材")
        if "fixtures_only" in asset:
            _require(type(asset["fixtures_only"]) is bool, "asset.fixtures_only 必须是布尔值")
            _require(not asset["fixtures_only"] or timeline["fixtures_only"], "timeline 丢失素材 fixture 标记")
    segments = _index(manifest.get("segments"), "segment_id")
    tracks = _index(timeline.get("tracks"), "kind")
    _index(timeline["tracks"], "track_id")
    _require(set(tracks) == {"video", "source_audio", "narration", "subtitle"}, "需要 D 的四种独立轨道")
    clips = _index([c for t in tracks.values() for c in t.get("clips", [])], "clip_id")
    for kind, track in tracks.items():
        records = track.get("cues" if kind == "subtitle" else "clips")
        _require(isinstance(records, list), f"{kind} 记录必须是列表")
        previous_end = 0
        for record in records:
            begin, end = _range(record.get("timeline_range_us"), duration, "timeline_range_us")
            _require(begin >= previous_end, f"{kind} 轨道重叠或乱序")
            if kind == "video":
                _require(begin == previous_end, "不支持视频 gaps")
            previous_end = end
            if kind == "subtitle":
                _require(record.get("editable") is True, "字幕必须保持 editable")
                text = _text(record.get("text"), "字幕 text")
                normalized = text.replace("\r\n", "\n").replace("\r", "\n")
                _require("\x00" not in text and not re.search(r"\n\s*\n", normalized), "字幕含 SRT 空块或 NUL")
                continue
            asset = assets.get(record.get("asset_id"))
            _require(asset is not None, "clip 引用了未知素材")
            revision = _integer(record.get("asset_revision"), "clip.asset_revision")
            _require(revision == asset["revision"], "clip 素材 revision 过期")
            lo, hi = _range(record.get("source_range_us"), asset["duration_us"], "source_range_us")
            playback = record.get("playback_rate", {})
            n = _integer(playback.get("numerator"), "playback_rate.numerator")
            d = _integer(playback.get("denominator"), "playback_rate.denominator")
            _require(n == d and hi - lo == end - begin, "仅支持 1× 且源时长等于 timeline 时长")
            if kind == "video":
                _require(asset["media_kind"] == "video", "视频轨道必须引用 video")
                segment = segments.get(record.get("segment_id"))
                _require(segment is not None and segment.get("asset_id") == record["asset_id"], "视频引用了未知或不匹配的 segment")
                _require(_integer(segment.get("asset_revision"), "segment.asset_revision") == asset["revision"], "segment 素材版本过期")
                segment_lo, segment_hi = _range(segment.get("source_range_us"), asset["duration_us"], "segment.source_range_us")
                _require(segment_lo <= lo < hi <= segment_hi, "视频超出选定 segment source bounds")
                _require(record.get("audio_policy") in {"muted", "separate_track"}, "不支持的 audio_policy")
            else:
                gain = record.get("gain")
                _require(type(gain) in (int, float) and math.isfinite(gain) and 0 <= gain <= 16, "gain 必须是 0..16 的有限数值")
                _require(asset.get("audio", {}).get("present") is True, "音频轨道素材未声明音频")
                if kind == "narration":
                    _require(asset["media_kind"] == "audio", "旁白必须引用 audio")
                else:
                    linked = clips.get(record.get("linked_video_clip_id"))
                    _require(linked in tracks["video"]["clips"], "source_audio 未绑定视频")
                    _require(linked["audio_policy"] == "separate_track" and all(record[k] == linked[k] for k in (
                        "asset_id", "asset_revision", "source_range_us", "timeline_range_us", "playback_rate")), "source_audio 与视频未对齐")
        if kind == "video":
            _require(bool(records) and previous_end == duration, "视频未连续覆盖完整 timeline")
    for clip in tracks["video"]["clips"]:
        linked = [c for c in tracks["source_audio"]["clips"] if c["linked_video_clip_id"] == clip["clip_id"]]
        _require(len(linked) == (1 if clip["audio_policy"] == "separate_track" else 0), "视频原声轨道缺失或重复")
    voices = _index(voice.get("cues"), "cue_id")
    narrations = _index(tracks["narration"]["clips"], "cue_id")
    _require(set(voices) == set(narrations), "旁白未覆盖 VoicePackage cues")
    expected_subtitles = []
    for cue_id, cue in voices.items():
        clip = narrations[cue_id]
        if "script_input" in cue:
            _integer(cue["script_input"].get("revision"), "cue.script_input.revision")
            _require(cue["script_input"] == tl["StoryPlan"], "旁白句子绑定了旧 StoryPlan")
        measured = _integer(cue.get("duration_us"), "cue.duration_us")
        _require(cue.get("fit_status") == "fits" and cue.get("needs_retime") is False, "旁白仍需 retime")
        _require(measured <= _integer(cue.get("duration_budget_us"), "cue.duration_budget_us"), "旁白超过预算")
        _require(cue.get("asset_id") == clip["asset_id"] and measured == assets[clip["asset_id"]]["duration_us"]
                 and clip["source_range_us"] == {"begin": 0, "end": measured}, "不得截断旁白或替换测量素材")
        offset = clip["timeline_range_us"]["begin"]
        previous = 0
        texts = []
        for local in cue.get("local_subtitles", []):
            lo, hi = _range(local.get("range_us"), measured, "local_subtitle")
            _require(lo >= previous, "局部字幕重叠或乱序")
            previous = hi
            texts.append(_text(local.get("text"), "local_subtitle.text"))
            expected_subtitles.append((cue_id, offset + lo, offset + hi, local["text"]))
        _require("".join(texts) == cue.get("text") and bool(texts), "局部字幕与旁白文本不一致")
    subtitles = tracks["subtitle"]["cues"]
    _index(subtitles, "subtitle_id")
    actual = [(c.get("cue_id"), c["timeline_range_us"]["begin"], c["timeline_range_us"]["end"], c["text"]) for c in subtitles]
    _require(sorted(actual) == sorted(expected_subtitles), "全局字幕未保留 D/E 的时间偏移与文本")
    return assets, tracks


def _seconds(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 1_000_000}.{value % 1_000_000:06d}"


def _srt(cues: list[Document]) -> str:
    def stamp(ms: int) -> str:
        hours, ms = divmod(ms, 3_600_000)
        minutes, ms = divmod(ms, 60_000)
        seconds, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"

    blocks = []
    for index, cue in enumerate(cues, 1):
        interval = cue["timeline_range_us"]
        begin, end = interval["begin"] // 1000, interval["end"] // 1000
        _require(end > begin, "字幕区间无法无歧义地表示为 SRT 毫秒")
        text = cue["text"].replace("\r\n", "\n").replace("\r", "\n")
        blocks.append(f"{index}\n{stamp(begin)} --> {stamp(end)}\n{text}\n")
    return "\n".join(blocks)


def _run(command: list[str]) -> bytes:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise PreviewError(f"媒体命令失败（{result.returncode}）: {result.stderr.decode('utf-8', errors='replace')}")
    return result.stdout


def _ffprobe_binary() -> str | None:
    configured = os.getenv("QUICKCUT_FFPROBE")
    if configured:
        _require(Path(configured).is_file() and os.access(configured, os.X_OK), "QUICKCUT_FFPROBE 不可执行")
        return configured
    return shutil.which("ffprobe")


def _probe(binary: str, path: Path) -> Document:
    return json.loads(_run([binary, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))


def _duration(stream: Document) -> int:
    value = stream.get("duration")
    _require(value is not None and value != "N/A", "ffprobe 未能测量流时长")
    result = round(Fraction(str(value)) * 1_000_000)
    return _integer(result, "实测时长")


def _start_time(stream: Document) -> int:
    value = stream.get("start_time")
    _require(value is not None and value != "N/A", "ffprobe 未能测量流 start_time")
    return round(Fraction(str(value)) * 1_000_000)


def _video_origin(binary: str, path: Path) -> int:
    info = json.loads(_run([
        binary, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
        "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path),
    ]))
    frames = info.get("frames", [])
    _require(bool(frames), "ffprobe 未能测量视频首帧")
    return _start_time({"start_time": frames[0].get("best_effort_timestamp_time")})


def _decode(ffmpeg: str, path: Path, kinds: set[str]) -> None:
    command = [ffmpeg, "-v", "error", "-xerror", "-i", str(path)]
    for kind in sorted(kinds):
        command += ["-map", "0:v:0" if kind == "video" else "0:a:0"]
    _run(command + ["-f", "null", "-"])


def render_timeline_preview(
    timeline: Document, manifest: Document, voice: Document,
    project_root: str | Path, output_path: str | Path, *,
    srt_path: str | Path | None = None, report_path: str | Path | None = None,
) -> Document:
    """消费 D JSON 和 A/E 文档，并集素材 URI 均相对 project_root。

    默认输出同名 .srt/.preview.json。返回可 JSON 序列化的预览报告。
    任一目标已存在则拒绝；先在同目录完成渲染、全量解码和校验，再以
    不覆盖的原子硬链接发布各文件。三文件发布不具备跨文件事务性。
    ffprobe 是发布的必需依赖；缺失时以结构化错误拒绝生成预览。
    """
    assets, tracks = _validate(timeline, manifest, voice)
    subtitle_text = _srt(tracks["subtitle"]["cues"])
    root = Path(project_root).expanduser().resolve(strict=True)
    _require(root.is_dir(), "project_root 必须是目录")
    destination = Path(output_path).expanduser().absolute()
    _require(destination.suffix.lower() == ".mp4", "预览输出必须是 .mp4")
    destinations = [destination, Path(srt_path).expanduser().absolute() if srt_path else destination.with_suffix(".srt"),
                    Path(report_path).expanduser().absolute() if report_path else destination.with_suffix(".preview.json")]
    _require(destinations[1].suffix.lower() == ".srt" and destinations[2].suffix.lower() == ".json", "SRT/报告扩展名必须为 .srt/.json")
    _require(len({p.resolve() for p in destinations}) == 3, "输出路径必须互不相同")
    for path in destinations:
        _require(not os.path.lexists(path), f"拒绝覆盖现有输出: {path}")
        _require(path.parent.is_dir(), f"输出目录不存在: {path.parent}")
    configured = os.getenv("QUICKCUT_FFMPEG")
    if configured:
        _require(Path(configured).is_file() and os.access(configured, os.X_OK), "QUICKCUT_FFMPEG 不可执行")
    ffmpeg = core._ffmpeg_binary()
    probe = _ffprobe_binary()
    if not probe:
        raise PreviewError("发布预览必须安装 ffprobe 或配置 QUICKCUT_FFPROBE", code="ffprobe_required")
    used = {c["asset_id"] for t in tracks.values() for c in t.get("clips", [])}
    paths = {}
    measurements = {}
    starts = {}
    for asset_id in sorted(used):
        asset = assets[asset_id]
        uri = _text(asset.get("uri"), "asset.uri")
        path = resolve_uri(root, uri)
        _require(path.is_file(), f"素材文件不存在: {uri}")
        expected = asset.get("content_hash")
        _require(isinstance(expected, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", expected) is not None, "素材必须提供真实 sha256 content_hash，不能使用占位 hash")
        _require(digest(path) == expected, f"素材 SHA-256 不一致: {asset_id}")
        _require(path not in {p.resolve() for p in destinations}, "输出不得覆盖输入素材")
        paths[asset_id] = path
        kinds = {"video" if t["kind"] == "video" else "audio" for t in tracks.values()
                 if any(c["asset_id"] == asset_id for c in t.get("clips", []))}
        _decode(ffmpeg, path, kinds)
        info = _probe(probe, path)
        streams = {kind: next((s for s in info["streams"] if s["codec_type"] == kind), None)
                   for kind in kinds | ({"video"} if asset["media_kind"] == "video" else set())}
        measured = {}
        starts[asset_id] = {}
        for kind, stream in streams.items():
            _require(stream is not None, f"素材缺少 {kind} 流: {asset_id}")
            # WAV 的采样从零开始，容器不提供 start_time；带视频素材必须读取真实流起点。
            starts[asset_id][kind] = (_video_origin(probe, path) if kind == "video" else
                                     0 if info["format"].get("format_name") == "wav" else _start_time(stream))
        for kind in kinds:
            stream = streams[kind]
            length = _duration(stream)
            measured[kind] = length
            offset = starts[asset_id][kind] - starts[asset_id].get("video", starts[asset_id][kind])
            tolerance = 2 if kind == "video" else max(2, math.ceil(1_000_000 / int(stream["sample_rate"])))
            required_end = max(c["source_range_us"]["end"] for t in tracks.values()
                               if (t["kind"] == "video") == (kind == "video")
                               for c in t.get("clips", []) if c["asset_id"] == asset_id)
            _require(required_end <= offset + length + tolerance,
                     f"真实 {kind} 流不足以覆盖 source bounds: {asset_id}")
        measurements[asset_id] = measured
    output = timeline["output"]
    sample_rate = output["sample_rate"]
    fps = Fraction(output["fps"]["numerator"], output["fps"]["denominator"])
    duration = timeline["duration_us"]
    with tempfile.TemporaryDirectory(prefix=".quickcut-preview-", dir=destination.parent) as directory:
        temp = Path(directory)
        command = [ffmpeg, "-v", "error", "-xerror", "-nostdin", "-n", "-copyts"]
        filters = []
        video_labels = []
        frame_ranges = []
        input_index = 0
        rate = f"{fps.numerator}/{fps.denominator}"
        time_base = f"{fps.denominator}/{fps.numerator}"
        # 全局半开区间按帧时间戳向上量化，邻接片段共用同一个边界，避免逐段取整累积漂移。
        for index, clip in enumerate(tracks["video"]["clips"]):
            bounds = clip["timeline_range_us"]
            begin_frame, end_frame = (math.ceil(Fraction(bounds[key], 1_000_000) * fps) for key in ("begin", "end"))
            frame_count = end_frame - begin_frame
            frame_ranges.append({"clip_id": clip["clip_id"], "begin_frame": begin_frame, "end_frame": end_frame})
            if not frame_count:
                continue
            command += ["-i", str(paths[clip["asset_id"]])]
            interval = clip["source_range_us"]
            label = f"v{index}"
            filters.append(f"[{input_index}:v:0]setpts=PTS-STARTPTS,"
                           f"trim=start={_seconds(interval['begin'])}:end={_seconds(interval['end'])},"
                           f"setpts=PTS-STARTPTS,fps={rate}:start_time=0,"
                           f"tpad=stop_mode=clone:stop=-1,trim=end_frame={frame_count},"
                           f"settb=expr={time_base},setpts=N,"
                           f"scale={output['width']}:{output['height']}:force_original_aspect_ratio=decrease,"
                           f"pad={output['width']}:{output['height']}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[{label}]")
            video_labels.append(f"[{label}]")
            input_index += 1
        total_frames = math.ceil(Fraction(duration, 1_000_000) * fps)
        filters.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0,"
                       f"settb=expr={time_base},setpts=N[picture]")
        audio_labels = ["[silence]"]
        total_samples = round(Fraction(duration * sample_rate, 1_000_000))
        filters.append(f"anullsrc=r={sample_rate}:cl=stereo,atrim=end_sample={total_samples}[silence]")
        for kind in ("source_audio", "narration"):
            for index, clip in enumerate(tracks[kind]["clips"]):
                interval = clip["source_range_us"]
                pcm = temp / f"{kind}-{index}.wav"
                # 旁白必须整段解码后核对测量值，不能按错误声明裁短语音。
                audio_filter = "asetpts=PTS-STARTPTS"
                if kind == "source_audio":
                    origin = starts[clip["asset_id"]]["video"]
                    audio_filter = (f"asetpts=PTS-({_seconds(origin)})/TB,"
                                    f"atrim=start={_seconds(interval['begin'])}:end={_seconds(interval['end'])},"
                                    f"asetpts=PTS-({_seconds(interval['begin'])})/TB,"
                                    f"aresample={sample_rate}:async=1:first_pts=0")
                _run([ffmpeg, "-v", "error", "-xerror", "-nostdin", "-n", "-copyts", "-i", str(paths[clip["asset_id"]]),
                      "-map", "0:a:0", "-af", audio_filter,
                      "-ar", str(sample_rate), "-ac", "2", "-c:a", "pcm_s16le", str(pcm)])
                with wave.open(str(pcm), "rb") as wav:
                    samples = wav.getnframes()
                expected_samples = Fraction((interval["end"] - interval["begin"]) * sample_rate, 1_000_000)
                _require(abs(samples - expected_samples) <= 1, f"真实音频采样不足或超长: {clip['clip_id']}；不补静音或截音凑时长")
                command += ["-i", str(pcm)]
                label = f"a{input_index}"
                offset = round(Fraction(clip["timeline_range_us"]["begin"] * sample_rate, 1_000_000))
                filters.append(f"[{input_index}:a:0]asetpts=PTS-STARTPTS,volume={clip['gain']},adelay={offset}S:all=1[{label}]")
                audio_labels.append(f"[{label}]")
                input_index += 1
        filters.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0:dropout_transition=0[mix]")
        movie = temp / "preview.mp4"
        command += ["-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
                    "-map", "[picture]", "-map", "[mix]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-r", rate, "-fps_mode", "cfr",
                    "-c:a", "aac", "-b:a", "192k", "-ar", str(sample_rate), "-ac", "2",
                    "-movflags", "+faststart", str(movie)]
        _run(command)
        _decode(ffmpeg, movie, {"video", "audio"})
        tolerance = math.ceil(1_000_000 / fps) + 2000
        info = _probe(probe, movie)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        _require((video["width"], video["height"]) == (output["width"], output["height"]), "预览尺寸不一致")
        _require(Fraction(video["avg_frame_rate"]) == fps and int(audio["sample_rate"]) == sample_rate, "预览帧率或采样率不一致")
        _require(int(video["nb_frames"]) == total_frames, "实测视频帧数与全局 timeline 量化结果不一致")
        stream_durations = {"video": _duration(video), "audio": _duration(audio)}
        measured_duration = _duration(info["format"])
        _require(abs(stream_durations["video"] - duration) <= tolerance, "实测视频时长与 timeline 不一致")
        _require(abs(stream_durations["audio"] - duration) <= max(2000, math.ceil(1_000_000 / sample_rate)), "实测音频时长与 timeline 不一致")
        _require(abs(measured_duration - duration) <= tolerance, "实测 MP4 时长与 timeline 不一致")
        report = {
            "kind": "TimelinePreviewReport", "schema_version": "0.1.0", "status": "preview_rendered",
            "project_id": timeline["project_id"], "inputs": [_ref(d) for d in (timeline, manifest, voice)],
            "fixtures_only": timeline["fixtures_only"], "expected_duration_us": duration,
            "measured_duration_us": measured_duration, "measured_stream_durations_us": stream_durations,
            "source_measurements_us": measurements, "duration_tolerance_us": tolerance,
            "duration_validation": "ffprobe",
            "video_frame_quantization": "ceil_global_timeline_boundaries",
            "video_frame_ranges": frame_ranges, "measured_video_frames": int(video["nb_frames"]),
            "full_decode_verified": True, "subtitles_burned": False, "subtitles_editable": True,
            "srt_time_quantization": "floor_to_millisecond",
            "audio_mix": "source_audio_and_narration_with_timeline_offsets_and_gains",
            "editor_project_exported": False, "editor_import_verified": False, "f_acceptance_completed": False,
            "notes": ["字幕未烧录，独立全局 SRT 保留编辑性。", "这是媒体预览，不是剪辑工程导出，未完成 F 验收。"],
            "files": dict(zip(("preview", "subtitles", "report"), map(str, destinations))),
        }
        subtitle_file = temp / "captions.srt"
        subtitle_file.write_text(subtitle_text, encoding="utf-8")
        report_file = temp / "report.json"
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        # 每个临时文件放到目标同目录，再用 link 排他发布，避免检查后覆盖的竞态。
        for source, target in zip((movie, subtitle_file, report_file), destinations):
            with tempfile.NamedTemporaryFile(prefix=".quickcut-publish-", dir=target.parent) as staged:
                with source.open("rb") as stream:
                    shutil.copyfileobj(stream, staged)
                staged.flush()
                os.fsync(staged.fileno())
                os.link(staged.name, target)
    return report

"""F: immutable media packages, editable Jianying drafts and safe relocation.

A generated package is not an editor acceptance result. Only an observed
open/edit/save/reopen test can establish that result, recorded separately.
"""
from __future__ import annotations

from copy import deepcopy
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from fractions import Fraction

from . import core, jianying, timeline_preview as renderer
from .media import digest, fingerprint, resolve_uri, write_json


class ExportError(ValueError):
    def __init__(self, code: str, message: str, affected_ids: tuple[str, ...] = ()):
        super().__init__(message)
        self.code, self.affected_ids = code, affected_ids

    def to_dict(self) -> dict:
        return {"status": "failed", "stage": "export", "code": self.code, "message": str(self),
                "retryable": False, "affected_ids": list(self.affected_ids), "action_owner": "F"}


def _require(ok: bool, code: str, message: str) -> None:
    if not ok:
        raise ExportError(code, message)


def _hash_json(value: dict) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _validate(timeline: dict, manifest: dict, voice: dict, allow_fixtures: bool) -> tuple[dict, dict]:
    try:
        assets, tracks = renderer._validate(timeline, manifest, voice)
    except (renderer.PreviewError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ExportError("TIMELINE_INVALID", str(exc)) from exc
    _require(allow_fixtures or not any(d["fixtures_only"] for d in (timeline, manifest, voice)),
             "FIXTURE_EXPORT_REJECTED", "生产工程拒绝 fixture 输入；测试包必须显式允许并保留标记")
    policy = timeline.get("render_policy", {})
    _require(policy.get("editable_export_burn_subtitles") is False and
             policy.get("retain_separate_audio_tracks") is True and
             policy.get("unsupported_effect_policy") == "error", "EDITOR_FEATURE_UNSUPPORTED",
             "工程须保留独立字幕和声音，并对未支持效果明确报错")
    def inspect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"gain_envelope", "volume_envelope", "ducking", "fade_in", "fade_out",
                           "fade_in_us", "fade_out_us", "audio_fade", "animation", "animations"}:
                    _require(item in (None, False, 0, [], {}), "EDITOR_FEATURE_UNSUPPORTED", f"暂不支持 {key}")
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    inspect(timeline)
    return assets, tracks


def _target(version: str) -> None:
    _require(version == jianying.TARGET_VERSION, "EDITOR_VERSION_UNSUPPORTED",
             f"此适配器仅生成 macOS 剪映 {jianying.TARGET_VERSION} 草稿，其他版本尚未验证")


def _publish_directory(source: Path, target: Path) -> None:
    """Atomic, no-replace directory publication on macOS, Linux and Windows."""
    if sys.platform == "win32":
        os.rename(source, target)  # Windows rename fails if destination exists.
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = rename(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
    else:
        raise ExportError("ATOMIC_PUBLISH_UNSUPPORTED", "当前系统缺少不覆盖的目录原子发布接口")
    if result:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise ExportError("OUTPUT_EXISTS", f"拒绝覆盖已有目录: {target}")
        raise OSError(code, os.strerror(code), str(target))


def _inspect_media(path: Path, asset: dict, tracks: dict, ffmpeg: str, probe: str) -> dict:
    kinds = {"video" if track["kind"] == "video" else "audio" for track in tracks.values()
             if any(c["asset_id"] == asset["asset_id"] for c in track.get("clips", []))}
    try:
        renderer._decode(ffmpeg, path, kinds)
        info = renderer._probe(probe, path)
        # Native editors and the extraction command both select the first
        # stream of each kind; do not validate a later language/alternate track.
        streams = {}
        for stream in info["streams"]:
            if stream["codec_type"] in {"video", "audio"}:
                streams.setdefault(stream["codec_type"], stream)
        for kind in kinds | ({"video"} if "video" in streams else set()):
            if kind in streams:
                streams[kind] = renderer._measured_stream(probe, path, streams[kind])
        video = streams.get("video")
        origin = renderer._video_origin(probe, path) if video else 0
        if video:
            rotations = [float(s.get("rotation", 0)) for s in video.get("side_data_list", [])]
            rotations.append(float(video.get("tags", {}).get("rotate", 0)))
            _require(all(v % 360 == 0 for v in rotations), "EDITOR_FEATURE_UNSUPPORTED",
                     "首版工程拒绝带旋转元数据的素材；请由 A 规范方向并更新素材版本")
        for kind in kinds:
            _require(kind in streams, "MEDIA_UNREADABLE", f"素材没有 {kind} 流: {asset['asset_id']}")
            stream = streams[kind]
            start = (origin if kind == "video" else 0 if info["format"].get("format_name") == "wav"
                     else renderer._start_time(stream))
            length = renderer._duration(stream)
            ends = [c["source_range_us"]["end"] for track in tracks.values()
                    if (track["kind"] == "video") == (kind == "video")
                    for c in track.get("clips", []) if c["asset_id"] == asset["asset_id"]]
            tolerance = 2 if kind == "video" else max(2, math.ceil(1_000_000 / int(stream["sample_rate"])))
            _require(max(ends) <= start - origin + length + tolerance, "MEDIA_UNREADABLE",
                     f"真实 {kind} 流不能覆盖源区间: {asset['asset_id']}")
        renderer._require_audio_continuity(probe, path, streams.get("audio", {}),
            [clip["source_range_us"] for clip in tracks.get("source_audio", {}).get("clips", [])
             if clip["asset_id"] == asset["asset_id"]], origin_us=origin)
        if any(c["asset_id"] == asset["asset_id"] for c in tracks["narration"]["clips"]):
            _verify_narration_wav(path, asset)
        result = {"origin_us": origin, "duration_us": renderer._duration(video or streams["audio"])}
        if "audio" in kinds:
            # WAV start is defined as 0, matching the coverage check above.
            result["audio_start_us"] = 0 if info["format"].get("format_name") == "wav" else renderer._start_time(streams["audio"])
            result["audio_duration_us"] = renderer._duration(streams["audio"])
        if video:
            result.update(width=video["width"], height=video["height"])
        return result
    except (renderer.PreviewError, KeyError, ValueError) as exc:
        if isinstance(exc, ExportError):
            raise
        raise ExportError("MEDIA_UNREADABLE", f"{asset['asset_id']}: {exc}", (asset["asset_id"],)) from exc


def _ceil_samples(microseconds: int, sample_rate: int) -> int:
    return math.ceil(Fraction(max(0, microseconds) * sample_rate, 1_000_000))


def _nearest_sample_count(expected: Fraction) -> int:
    whole = int(expected)
    return whole if expected - whole <= Fraction(1, 2) else whole + 1


def _write_padded_wav(path: Path, head: int, tail: int) -> int:
    """Insert silence so a late or short source stream matches the picture slot."""
    with wave.open(str(path), "rb") as wav:
        channels, rate, width = wav.getnchannels(), wav.getframerate(), wav.getsampwidth()
        payload = wav.readframes(wav.getnframes())
    silence = b"\x00" * (width * channels)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(silence * head + payload + silence * tail)
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes()


def _extract_source_audio(ffmpeg: str, source: Path, target: Path, bounds: dict, *,
                          origin_us: int, audio_start_us: int, audio_duration_us: int,
                          sample_rate: int, clip_id: str) -> dict:
    """Cut original audio to the picture range, padding gaps that contain no samples.

    Phone videos often start the AAC stream tens of milliseconds after the first
    picture. The missing head is silence, not a broken file.
    """
    command = [ffmpeg, "-v", "error", "-xerror", "-nostdin", "-n", "-copyts", "-i", str(source),
        "-map", "0:a:0", "-af",
        f"atrim=start={renderer._seconds(bounds['begin'] + origin_us)}:end={renderer._seconds(bounds['end'] + origin_us)},asetpts=PTS-STARTPTS",
        "-ar", str(sample_rate), "-ac", "2", "-c:a", "pcm_s16le", str(target)]
    try:
        renderer._run(command)
        with wave.open(str(target), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
    except (renderer.PreviewError, wave.Error) as exc:
        raise ExportError("MEDIA_UNREADABLE", str(exc), (clip_id,)) from exc
    expected = Fraction((bounds["end"] - bounds["begin"]) * rate, 1_000_000)
    head_pad = tail_pad = 0
    if abs(frames - expected) > 1:
        available_begin = audio_start_us - origin_us
        available_end = available_begin + audio_duration_us
        head_budget = _ceil_samples(available_begin - bounds["begin"], rate)
        tail_budget = _ceil_samples(bounds["end"] - available_end, rate)
        deficit = _nearest_sample_count(expected) - frames
        if deficit < 0 or deficit > head_budget + tail_budget:
            raise ExportError("MEDIA_UNREADABLE", f"原声采样时长不符: {clip_id}", (clip_id,))
        head_pad = min(deficit, head_budget)
        tail_pad = deficit - head_pad
        frames = _write_padded_wav(target, head_pad, tail_pad)
        if abs(frames - expected) > 1:
            raise ExportError("MEDIA_UNREADABLE", f"原声采样时长不符: {clip_id}", (clip_id,))
    return {"frames": frames, "rate": rate, "expected": expected,
            "leading_silence_samples": head_pad, "trailing_silence_samples": tail_pad}


def _verify_narration_wav(path: Path, asset: dict) -> None:
    """E duration must describe the complete PCM, including trailing pauses."""
    try:
        with wave.open(str(path), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
            measured = Fraction(frames * 1_000_000, rate)
            _require(abs(measured - asset["duration_us"]) <= Fraction(1_000_000, rate),
                     "NARRATION_DURATION_MISMATCH", f"旁白完整 WAV 时长不匹配 E 声明: {asset['asset_id']}")
            declared = asset.get("audio", {})
            _require(declared.get("sample_rate", rate) == rate and
                     declared.get("channels", wav.getnchannels()) == wav.getnchannels(),
                     "MEDIA_UNREADABLE", f"旁白 WAV 格式不匹配 E 声明: {asset['asset_id']}")
    except wave.Error as exc:
        raise ExportError("MEDIA_UNREADABLE", f"旁白必须是可读取的 PCM WAV: {asset['asset_id']}") from exc


def export_project(timeline: dict, manifest: dict, voice: dict, project_root: str | Path,
                   output_dir: str | Path, *, preview: bool = False, allow_fixtures: bool = False,
                   target_version: str = jianying.TARGET_VERSION, name: str | None = None) -> dict:
    """Export without mutating inputs or overwriting any existing package.

    Preview is optional; SHA256, stream probing and complete source decoding
    are mandatory. A failed export never publishes a partial package.
    """
    _target(target_version)
    timeline, manifest, voice = deepcopy((timeline, manifest, voice))
    assets, tracks = _validate(timeline, manifest, voice, allow_fixtures)
    root = Path(project_root).expanduser().resolve(strict=True)
    destination = Path(output_dir).expanduser().absolute()
    _require(root.is_dir() and destination.parent.is_dir(), "PACKAGE_PATH_INVALID", "项目根与输出父目录须存在")
    _require(not os.path.lexists(destination), "OUTPUT_EXISTS", f"拒绝覆盖已有目录: {destination}")
    export_id = str(uuid.uuid4()).upper()
    display_name = name or f"QuickCut-{timeline['project_id']}"
    _require(isinstance(display_name, str) and bool(display_name.strip()), "INVALID_NAME", "草稿名称不能为空")
    ffmpeg = core._ffmpeg_binary()
    from .music import validate_music
    music_path = validate_music(timeline, root)
    probe = renderer._ffprobe_binary()
    _require(bool(probe), "FFPROBE_REQUIRED", "工程导出需要 ffprobe 进行真实媒体校验")
    used = {clip["asset_id"] for track in tracks.values() for clip in track.get("clips", [])}
    _require(not used.intersection(f"source_audio:{c['clip_id']}" for c in tracks["source_audio"]["clips"]),
             "TIMELINE_INVALID", "素材 ID 与导出原声资源 ID 冲突")
    with tempfile.TemporaryDirectory(prefix=".quickcut-export-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "package"
        resource_dir = stage / "draft" / "Resources"
        resource_dir.mkdir(parents=True)
        (stage / "inputs").mkdir()
        resources, measurements, paths = [], {}, {}
        for asset_id in sorted(used):
            asset = assets[asset_id]
            try:
                path = resolve_uri(root, asset["uri"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ExportError("PACKAGE_PATH_INVALID", str(exc), (asset_id,)) from exc
            _require(path.is_file(), "PACKAGE_MISSING_MEDIA", f"素材不存在: {asset_id}: {asset.get('uri')}")
            expected = asset.get("content_hash")
            _require(isinstance(expected, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", expected) is not None,
                     "MEDIA_HASH_INVALID", f"素材缺少有效 SHA256: {asset_id}")
            _require(digest(path) == expected, "MEDIA_HASH_MISMATCH", f"素材内容已变化: {asset_id}")
            measurement = _inspect_media(path, asset, tracks, ffmpeg, probe)
            suffix = path.suffix.lower()
            _require(re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) is not None, "MEDIA_UNREADABLE", "媒体扩展名无效")
            filename = expected.removeprefix("sha256:") + suffix
            copied = resource_dir / filename
            if not copied.exists():
                shutil.copyfile(path, copied)
            _require(digest(copied) == expected, "MEDIA_HASH_MISMATCH", f"拷贝期间素材发生变化: {asset_id}")
            resources.append({"resource_key": asset_id, "asset_id": asset_id, "asset_revision": asset["revision"],
                "original_uri": asset["uri"], "draft_uri": f"Resources/{filename}",
                "packaged_uri": f"draft/Resources/{filename}", "content_hash": expected,
                "name": path.name, "media_kind": asset["media_kind"], "size_bytes": copied.stat().st_size,
                **measurement})
            paths[asset_id], measurements[asset_id] = copied, measurement
        for clip in tracks["source_audio"]["clips"]:
            key = f"source_audio:{clip['clip_id']}"
            filename = f"source-audio-{fingerprint(clip)}.wav"
            target = resource_dir / filename
            bounds = clip["source_range_us"]
            measurement = measurements[clip["asset_id"]]
            extracted = _extract_source_audio(
                ffmpeg, paths[clip["asset_id"]], target, bounds,
                origin_us=measurement["origin_us"], audio_start_us=measurement["audio_start_us"],
                audio_duration_us=measurement["audio_duration_us"],
                sample_rate=timeline["output"]["sample_rate"], clip_id=clip["clip_id"])
            frames, rate = extracted["frames"], extracted["rate"]
            resources.append({"resource_key": key, "asset_id": clip["asset_id"], "asset_revision": clip["asset_revision"],
                "source_range_us": deepcopy(bounds), "derived_for_clip_id": clip["clip_id"],
                "draft_uri": f"Resources/{filename}", "packaged_uri": f"draft/Resources/{filename}",
                "name": f"原声-{clip['clip_id']}.wav", "media_kind": "audio", "content_hash": digest(target),
                "size_bytes": target.stat().st_size, "duration_us": round(Fraction(frames * 1_000_000, rate)),
                "sample_rate": rate, "sample_rounding_error_us": float(Fraction(frames * 1_000_000, rate) - (bounds["end"] - bounds["begin"])),
                "leading_silence_samples": extracted["leading_silence_samples"],
                "trailing_silence_samples": extracted["trailing_silence_samples"]})
        if music_path:
            music = timeline["render_policy"]["background_music"]
            _require(music["asset_id"] not in used and music["asset_id"] not in {
                resource["resource_key"] for resource in resources},
                "TIMELINE_INVALID", "Music resource ID conflicts with an input or extracted audio resource")
            filename = music["content_hash"].removeprefix("sha256:") + ".wav"
            target = resource_dir / filename
            shutil.copyfile(music_path, target)
            _require(digest(target) == music["content_hash"], "MEDIA_HASH_MISMATCH", "Music changed during packaging")
            resources.append({"resource_key": music["asset_id"], "asset_id": music["asset_id"], "asset_revision": 1,
                "original_uri": music["uri"], "draft_uri": f"Resources/{filename}",
                "packaged_uri": f"draft/Resources/{filename}", "content_hash": music["content_hash"],
                "name": music["title"] + ".wav", "media_kind": "audio", "size_bytes": target.stat().st_size,
                "duration_us": music["duration_us"], "sample_rate": music["sample_rate"]})
            (stage / "MUSIC_CREDITS.txt").write_text(music["attribution"] + "\n" + music["source_url"] + "\n" +
                music["license_url"] + "\nMusic was excerpted, normalized, faded, and attenuated during narration.\n"
                "When publishing, include these credits in the video description.\n", encoding="utf-8")
        input_files = {"EditTimeline": "inputs/timeline.json", "AssetManifest": "inputs/asset-manifest.json",
                       "VoicePackage": "inputs/voice-package.json"}
        for doc in (timeline, manifest, voice):
            write_json(stage / input_files[doc["kind"]], doc)
        content, meta = jianying.build_draft(timeline, resources, destination / "draft",
            name=display_name, draft_id=export_id, target_version=target_version)
        write_json(stage / "draft" / "draft_info.json", content)
        write_json(stage / "draft" / "draft_meta_info.json", meta)
        (stage / "captions.srt").write_text(renderer._srt(tracks["subtitle"]["cues"]), encoding="utf-8")
        first = tracks["video"]["clips"][0]
        renderer._run([ffmpeg, "-v", "error", "-nostdin", "-n", "-ss", renderer._seconds(first["source_range_us"]["begin"]),
                       "-i", str(paths[first["asset_id"]]), "-frames:v", "1", str(stage / "draft" / "draft_cover.jpg")])
        preview_report = None
        if preview:
            preview_report = renderer.render_timeline_preview(timeline, manifest, voice, root, stage / "preview.mp4")
            preview_report["files"] = {k: str(destination / Path(v).name) for k, v in preview_report["files"].items()}
            write_json(stage / "preview.preview.json", preview_report)
        result = {"kind": "ExportManifest", "schema_version": "0.1.0", "document_id": f"export-{export_id}",
            "project_id": timeline["project_id"], "revision": 1, "owner": "F", "status": "generated",
            "fixtures_only": timeline["fixtures_only"], "inputs": [renderer._ref(d) for d in (timeline, manifest, voice)],
            "input_files": input_files, "input_hashes": {d["kind"]: _hash_json(d) for d in (timeline, manifest, voice)},
            "target": {"editor": "JianyingPro", "platform": "macOS", "version": target_version,
                       "adapter_version": jianying.ADAPTER_VERSION},
            "draft_name": display_name, "draft_id": export_id, "project_root": ".", "editor_project": "draft/draft_info.json",
            "preview": "preview.mp4" if preview else None, "subtitles": "captions.srt", "resources": resources,
            "capabilities": deepcopy(jianying.CAPABILITIES), "unsupported_features": [],
            "validation": {"source_hashes": "passed", "source_full_decode": "passed", "timeline_contract": "passed",
                           "source_audio_extraction": "passed", "editor_import": "not_run", "editor_edit_save_reopen": "not_run",
                           "preview": "passed" if preview else "not_run"},
            "warnings": ["工程已生成；剪映实际打开、编辑、保存和重开尚未由此命令验证。",
                         "包移动后请用 install_jianying_draft 重新定位媒体；不要直接打开旧的绝对路径草稿。"],
            "files": [{"uri": p.relative_to(stage).as_posix(), "content_hash": digest(p), "size_bytes": p.stat().st_size}
                      for p in sorted(stage.rglob("*")) if p.is_file()]}
        write_json(stage / "export-manifest.json", result)
        _publish_directory(stage, destination)
        return result


def _verify_package(package_dir: str | Path, *, allow_fixtures: bool = False) -> tuple[dict, dict, list[dict]]:
    """Verify every declared file plus frozen inputs before installation."""
    root = Path(package_dir).expanduser().resolve(strict=True)
    report = json.loads((root / "export-manifest.json").read_text(encoding="utf-8"))
    _require(report.get("kind") == "ExportManifest" and report.get("schema_version") == "0.1.0" and
             report.get("status") == "generated", "PACKAGE_INVALID", "需要当前 generated ExportManifest")
    target = report.get("target", {})
    _target(target.get("version"))
    if (target.get("adapter_version") == "quickcut-jianying-mac-0.1.0"
            and target.get("editor") == "JianyingPro" and target.get("platform") == "macOS"):
        raise ExportError("PACKAGE_INVALID", "该工程包使用旧版字幕布局（0.1.0）。请重新导出工程包后再打开剪映；"
                          "新版字幕布局不会直接重建旧包，避免改变原工程的字幕。")
    _require(target.get("adapter_version") == jianying.ADAPTER_VERSION and
             target.get("editor") == "JianyingPro" and target.get("platform") == "macOS",
             "PACKAGE_INVALID", "未知工程适配器版本")
    seen = set()
    for item in report["files"]:
        uri = item["uri"]
        _require(uri not in seen, "PACKAGE_INVALID", f"重复文件记录: {uri}")
        seen.add(uri)
        path = resolve_uri(root, uri)
        _require(path.is_file() and path.stat().st_size == item["size_bytes"], "PACKAGE_MISSING_MEDIA", f"包文件缺失或大小变化: {uri}")
        _require(digest(path) == item["content_hash"], "MEDIA_HASH_MISMATCH", f"包文件内容变化: {uri}")
    docs = []
    for kind in ("EditTimeline", "AssetManifest", "VoicePackage"):
        uri = report["input_files"][kind]
        _require(uri in seen, "PACKAGE_INVALID", f"输入未列入文件清单: {kind}")
        doc = json.loads(resolve_uri(root, uri).read_text(encoding="utf-8"))
        _require(_hash_json(doc) == report["input_hashes"][kind], "PACKAGE_INVALID", f"输入快照已变化: {kind}")
        docs.append(doc)
    assets, tracks = _validate(*docs, allow_fixtures)
    _require(report["inputs"] == [renderer._ref(d) for d in docs] and report["project_id"] == docs[0]["project_id"]
             and report["fixtures_only"] is docs[0]["fixtures_only"], "PACKAGE_INVALID", "导出清单与冻结输入不一致")
    resources = report["resources"]
    keys = {r["resource_key"] for r in resources}
    expected = {c["asset_id"] for t in tracks.values() for c in t.get("clips", [])} | {
        f"source_audio:{c['clip_id']}" for c in tracks["source_audio"]["clips"]}
    music = docs[0].get("render_policy", {}).get("background_music")
    if music:
        expected.add(music["asset_id"])
    _require(len(keys) == len(resources) and keys == expected, "PACKAGE_INVALID", "资源集合不匹配冻结时间线")
    clips = {f"source_audio:{c['clip_id']}": c for c in tracks["source_audio"]["clips"]}
    for resource in resources:
        uri = resource["packaged_uri"]
        _require(uri in seen and uri == "draft/" + resource["draft_uri"], "PACKAGE_INVALID", "资源路径不一致")
        _require(digest(resolve_uri(root, uri)) == resource["content_hash"], "MEDIA_HASH_MISMATCH", "资源映射hash不一致")
        if music and resource["resource_key"] == music["asset_id"]:
            from .music import validate_music
            _require(resource["asset_id"] == music["asset_id"] and resource["content_hash"] == music["content_hash"]
                     and resource["original_uri"] == music["uri"] and resource["media_kind"] == "audio"
                     and resource["asset_revision"] == 1 and resource["duration_us"] == music["duration_us"]
                     and resource["sample_rate"] == music["sample_rate"]
                     and resource["size_bytes"] == resolve_uri(root, uri).stat().st_size,
                     "PACKAGE_INVALID", "Background music resource differs from the frozen policy")
            validate_music(docs[0], root, packaged_path=resolve_uri(root, uri))
            _require("MUSIC_CREDITS.txt" in seen, "PACKAGE_INVALID", "Music attribution is missing")
            continue
        asset = assets[resource["asset_id"]]
        _require(resource["asset_revision"] == asset["revision"] and resource["size_bytes"] == resolve_uri(root, uri).stat().st_size,
                 "PACKAGE_INVALID", "资源版本或大小不匹配")
        if resource["resource_key"] in clips:
            clip = clips[resource["resource_key"]]
            _require(resource["derived_for_clip_id"] == clip["clip_id"] and resource["asset_id"] == clip["asset_id"] and
                     resource["source_range_us"] == clip["source_range_us"] and resource["media_kind"] == "audio",
                     "PACKAGE_INVALID", "原声资源映射不匹配冻结片段")
            with wave.open(str(resolve_uri(root, uri)), "rb") as wav:
                _require(wav.getframerate() == docs[0]["output"]["sample_rate"] and wav.getnchannels() == 2 and
                         wav.getsampwidth() == 2 and abs(Fraction(wav.getnframes() * 1_000_000, wav.getframerate()) -
                         (clip["source_range_us"]["end"] - clip["source_range_us"]["begin"])) <= Fraction(1_000_000, wav.getframerate()),
                         "PACKAGE_INVALID", "原声 PCM 不匹配时间线采样范围")
        else:
            _require(resource["resource_key"] == asset["asset_id"] and resource["original_uri"] == asset["uri"] and
                     resource["media_kind"] == asset["media_kind"] and resource["content_hash"] == asset["content_hash"],
                     "PACKAGE_INVALID", "原始素材映射不匹配冻结输入")
            if any(c["asset_id"] == asset["asset_id"] for c in tracks["narration"]["clips"]):
                _verify_narration_wav(resolve_uri(root, uri), asset)
    _require({"draft/draft_cover.jpg", "draft/draft_info.json", "draft/draft_meta_info.json", "captions.srt"} <= seen,
             "PACKAGE_INVALID", "工程必要文件未列入校验清单")
    return report, docs[0], resources


def verify_package(package_dir: str | Path, *, allow_fixtures: bool = False) -> tuple[dict, dict, list[dict]]:
    try:
        return _verify_package(package_dir, allow_fixtures=allow_fixtures)
    except ExportError:
        raise
    except (OSError, KeyError, TypeError, ValueError, AttributeError, wave.Error) as exc:
        raise ExportError("PACKAGE_INVALID", f"工程包无效: {exc}") from exc


def is_editor_running() -> bool:
    """Inspect only executable names, not arguments or unrelated helper apps."""
    if sys.platform != "darwin":
        return False
    result = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, check=True)
    return any(Path(line.strip()).name in {"VideoFusion-macOS", "JianyingPro"} for line in result.stdout.splitlines())


def install_jianying_draft(package_dir: str | Path, draft_root: str | Path | None = None, *,
                          name: str | None = None, allow_fixtures: bool = False) -> dict:
    """Add a new QuickCut draft with regenerated paths; never replace a draft.

    The editor must be closed. Registry is backed up before an atomic update.
    Only the new directory is removed on rollback; existing projects are untouched.
    """
    package = Path(package_dir).expanduser().resolve(strict=True)
    report, timeline, resources = verify_package(package, allow_fixtures=allow_fixtures)
    _require(not is_editor_running(), "EDITOR_RUNNING", "请先完全退出剪映，再安装新草稿，避免覆盖其内存中的草稿列表")
    root = Path(draft_root).expanduser().absolute() if draft_root else Path.home() / "Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    draft_name = name or f"QuickCut-{timeline['project_id'][:32]}-{uuid.uuid4().hex[:8]}"
    _require(isinstance(draft_name, str) and bool(draft_name.strip()) and draft_name not in {".", ".."}
             and not any(c in draft_name for c in ("/", "\\", "\0")), "INVALID_NAME", "草稿名称不能包含路径分隔符")
    if not draft_name.startswith("QuickCut-"):
        draft_name = "QuickCut-" + draft_name
    target = root / draft_name
    _require(not os.path.lexists(target), "OUTPUT_EXISTS", f"拒绝覆盖已有草稿: {draft_name}")
    lock = root / ".quickcut-install.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ExportError("INSTALL_BUSY", "已有安装锁；确认没有安装进程后再移除 .quickcut-install.lock") from exc
    with os.fdopen(fd, "w") as stream:
        stream.write(f"{os.getpid()}\n")
    try:
        registry_path = root / "root_meta_info.json"
        _require(not registry_path.is_symlink(), "REGISTRY_INVALID", "草稿注册表不能是符号链接")
        before = registry_path.read_bytes() if registry_path.exists() else None
        registry = json.loads(before) if before is not None else {"all_draft_store": [], "draft_ids": 0, "root_path": str(root)}
        _require(isinstance(registry, dict) and isinstance(registry.get("all_draft_store"), list)
                 and all(isinstance(item, dict) for item in registry["all_draft_store"])
                 and type(registry.get("draft_ids", 0)) is int, "REGISTRY_INVALID", "剪映草稿注册表格式不受支持")
        _require(not any(r.get("draft_fold_path") == str(target) or r.get("draft_name") == draft_name
                         for r in registry["all_draft_store"]), "OUTPUT_EXISTS", "注册表已有同名草稿")
        new_id = str(uuid.uuid4()).upper()
        with tempfile.TemporaryDirectory(prefix=".quickcut-install-", dir=root) as temporary:
            stage = Path(temporary) / "draft"
            stage.mkdir()
            for resource in resources:
                source = resolve_uri(package, resource["packaged_uri"])
                destination = resolve_uri(stage, resource["draft_uri"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copyfile(source, destination)
                _require(digest(destination) == resource["content_hash"], "MEDIA_HASH_MISMATCH", "安装期间资源发生变化")
            shutil.copyfile(package / "draft" / "draft_cover.jpg", stage / "draft_cover.jpg")
            content, meta = jianying.build_draft(timeline, resources, target,
                name=draft_name, draft_id=new_id, target_version=report["target"]["version"])
            write_json(stage / "draft_info.json", content)
            write_json(stage / "draft_meta_info.json", meta)
            write_json(stage / "quickcut-provenance.json", {"export_document_id": report["document_id"],
                "inputs": report["inputs"], "fixtures_only": report["fixtures_only"], "resources": resources,
                "editor_import": "not_run"})
            _require(not is_editor_running(), "EDITOR_RUNNING", "安装期间剪映已启动，请退出后重试")
            _require((registry_path.read_bytes() if registry_path.exists() else None) == before,
                     "REGISTRY_CHANGED", "安装期间草稿列表已变化，请重试")
            backup = None
            if before is not None:
                backup = root / f"root_meta_info.quickcut-backup-{time.time_ns()}.json"
                with backup.open("xb") as stream:
                    stream.write(before)
            registry["all_draft_store"].insert(0, jianying.registry_entry(meta))
            registry["draft_ids"] = len(registry["all_draft_store"])
            _publish_directory(stage, target)
            try:
                write_json(registry_path, registry)
            except Exception:
                shutil.rmtree(target)
                raise
        return {"status": "installed", "draft_name": draft_name, "draft_id": new_id,
                "draft_directory": str(target), "registry_backup": str(backup) if backup else None,
                "editor_import_verified": False, "fixtures_only": report["fixtures_only"],
                "next_step": "启动剪映，打开新草稿并验证独立轨道、编辑、保存和重新打开。"}
    finally:
        lock.unlink(missing_ok=True)

"""Import video assets and build a project manifest with per-stage caches."""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import shutil
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from .asr import FasterWhisperASR
from .asr_quality import source_diagnostics, transcript_review
from .core import _ffmpeg_binary
from .media import (MediaError, create_proxy, digest, extract_frames, ffprobe, fingerprint,
                    frame_timestamps, probe_media, read_json, resolve_uri, run_media,
                    us, valid_png, write_json)


@dataclass(frozen=True)
class SourceSpec:
    path: str | Path
    origin: str = "user"
    provenance_note: str = ""
    source_key: str | None = None


@dataclass(frozen=True)
class IngestConfig:
    window_us: int = 3_000_000
    coarse_window_us: int = 30_000_000
    proxy_width: int = 640
    asr: str = "none"
    asr_model: str = "tiny"
    asr_language: str | None = None
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_beam_size: int = 5
    asr_cpu_threads: int = 2
    asr_num_workers: int = 1
    asr_vad: bool = True
    asr_condition_on_previous_text: bool = False
    asr_initial_prompt: str | None = None
    asr_hotwords: str | None = None
    asr_model_revision: str | None = None
    max_attempts: int = 1

    def validate(self):
        for name in ("window_us", "coarse_window_us", "proxy_width", "asr_beam_size", "asr_cpu_threads", "asr_num_workers", "max_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.coarse_window_us < self.window_us:
            raise ValueError("coarse_window_us must be >= window_us")
        if self.proxy_width < 2 or self.proxy_width % 2:
            raise ValueError("proxy_width must be even and >= 2")
        if self.asr not in {"none", "faster-whisper"}:
            raise ValueError("asr must be none or faster-whisper (no silent auto fallback)")
        if not self.asr_model.strip():
            raise ValueError("asr_model must be nonempty")
        for name in ("asr_vad", "asr_condition_on_previous_text"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name in ("asr_initial_prompt", "asr_hotwords"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be text or None")
        if self.max_attempts > 3:
            raise ValueError("max_attempts must be <= 3")


class StageFailure(RuntimeError):
    def __init__(self, stage, cause, attempts):
        super().__init__(str(cause))
        self.stage, self.attempts = stage, attempts
        self.cause = cause


def _package_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


def _tool_profile():
    return {name: run_media([binary, "-version"]).decode(errors="replace").splitlines()[0]
            for name, binary in (("ffmpeg", _ffmpeg_binary()), ("ffprobe", ffprobe()))}


def _model_profile(config):
    profile = {key: value for key, value in asdict(config).items() if key.startswith("asr")}
    profile["packages"] = {name: _package_version(name) for name in (
        "faster-whisper", "ctranslate2", "av", "tokenizers", "onnxruntime", "huggingface-hub")}
    profile["vad_filter"] = config.asr_vad
    profile["audio_preprocessing"] = "video-origin-mono-16k-v1"
    profile["diagnostics_version"] = 1
    return profile


def _resolve_asr_model(config):
    path = Path(config.asr_model).expanduser()
    if not path.is_dir():
        from faster_whisper.utils import download_model
        options = {"revision": config.asr_model_revision} if config.asr_model_revision else {}
        path = Path(download_model(config.asr_model, **options))
    if not (path / "model.bin").is_file():
        raise MediaError("ASR model directory has no model.bin")
    return path.resolve()


def _error(project_id, asset_id, stage, error, attempts=1):
    cause = error.cause if isinstance(error, StageFailure) else error
    return {"status": "failed", "code": {"keyframes": "KEYFRAME_MISSING", "asr": "ASR_FAILED"}.get(stage, "MEDIA_UNREADABLE"),
            "stage": stage, "project_id": project_id, "affected_ids": [asset_id],
            "message": str(error), "retryable": not isinstance(cause, (ValueError, FileNotFoundError)),
            "attempts": attempts, "action_owner": "A"}


def _run_stage(root, record, name, profile, action, valid, report, config):
    key = fingerprint({"stage": name, "profile": profile, "asset_revision": record["revision"],
                       "content_hash": record["content_hash"], "implementation": 1})
    cache = record.setdefault("stages", {}).get(name)
    if cache and cache["key"] == key:
        try:
            intact = all(digest(resolve_uri(root, uri)) == expected for uri, expected in cache["files"].items())
            if intact and valid(cache["result"]):
                report.append({"asset_id": record["asset_id"], "stage": name, "cache_hit": True, "attempts": 0})
                return copy.deepcopy(cache["result"])
        except (OSError, ValueError, KeyError, MediaError):
            pass
    for attempt in range(1, config.max_attempts + 1):
        try:
            directory = resolve_uri(root, f"derived/{record['asset_id']}/r{record['revision']}/{name}-{key}")
            directory.mkdir(parents=True, exist_ok=True)
            result, paths = action(directory)
            if not valid(result):
                raise MediaError(f"Invalid {name} output")
            files = {p.relative_to(root).as_posix(): digest(p) for p in paths}
            record["stages"][name] = {"key": key, "result": result, "files": files}
            report.append({"asset_id": record["asset_id"], "stage": name, "cache_hit": False, "attempts": attempt})
            return copy.deepcopy(result)
        except Exception as exc:
            if attempt == config.max_attempts or isinstance(exc, (ValueError, FileNotFoundError)):
                raise StageFailure(name, exc, attempt) from exc


def _register(root, state, source):
    if source.origin not in {"user", "external"}:
        raise ValueError("Video origin must be user or external")
    path = Path(source.path).expanduser().resolve()
    alias = source.origin + ":" + (source.source_key or str(path))
    asset_id = state["aliases"].get(alias)
    if not path.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")
    content_hash = digest(path)
    if asset_id is None:
        asset_id = next((key for key, item in state["assets"].items()
                         if item["origin"] == source.origin and item["content_hash"] == content_hash), None)
    previous = state["assets"].get(asset_id)
    changed = previous and previous["content_hash"] != content_hash
    asset_id = asset_id or "asset-" + uuid4().hex
    revision = previous["revision"] + 1 if changed else previous["revision"] if previous else 1
    suffix = path.suffix.lower()
    if not suffix or not suffix[1:].isalnum():
        suffix = ".video"
    uri = f"media/{source.origin}/{asset_id}/r{revision}{suffix}"
    if previous and not changed:
        uri = previous["uri"]
    target = resolve_uri(root, uri)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or digest(target) != content_hash:
        temporary = target.with_name(target.name + ".tmp")
        shutil.copyfile(path, temporary)
        if digest(temporary) != content_hash:
            raise MediaError("Input changed during import; retry after copying finishes")
        temporary.replace(target)
    record = previous if previous and not changed else {
        "asset_id": asset_id, "revision": revision, "origin": source.origin,
        "content_hash": content_hash, "uri": uri, "stages": {},
    }
    record["provenance_note"] = source.provenance_note or record.get("provenance_note", "")
    state["assets"][asset_id] = record
    state["aliases"][alias] = asset_id
    return asset_id


def _segments(record, duration, config):
    coarse, shots = [], []
    base = f"{record['asset_id']}-r{record['revision']}"
    for lo in range(0, duration, config.coarse_window_us):
        hi = min(lo + config.coarse_window_us, duration)
        parent = f"{base}-coarse-{lo}-{hi}"
        coarse.append({"segment_id": parent, "asset_id": record["asset_id"], "asset_revision": record["revision"],
                       "source_range_us": {"begin": lo, "end": hi}, "granularity": "coarse_event",
                       "parent_segment_id": None, "keyframes": [],
                       "quality": {"usable": False, "flags": ["CONTAINER_ONLY"]},
                       "segmentation": {"method": "fixed_window", "window_us": config.coarse_window_us}})
        for begin in range(lo, hi, config.window_us):
            end = min(begin + config.window_us, hi)
            shots.append({"segment_id": f"{base}-shot-{begin}-{end}", "asset_id": record["asset_id"],
                          "asset_revision": record["revision"], "source_range_us": {"begin": begin, "end": end},
                          "granularity": "coarse_shot", "parent_segment_id": parent, "keyframes": [],
                          "quality": {"usable": True, "flags": []},
                          "segmentation": {"method": "fixed_window", "window_us": config.window_us}})
    return coarse + shots


def _process(root, record, config, tools, model_context, report):
    model_profile = model_context["profile"]
    source = resolve_uri(root, record["uri"])
    if digest(source) != record["content_hash"]:
        raise StageFailure("probe", MediaError("Packaged original content hash mismatch; reimport source"), 1)

    def media_action(directory):
        metadata = probe_media(source)
        frames = frame_timestamps(source, metadata)
        path = directory / "source-pts.json"
        write_json(path, {"video": metadata["video"], "frames": frames})
        return {"metadata": metadata, "frames_uri": path.relative_to(root).as_posix()}, [path]

    media = _run_stage(root, record, "probe", tools, media_action, lambda x: bool(x["metadata"]), report, config)
    metadata = media["metadata"]
    frames = read_json(resolve_uri(root, media["frames_uri"]))["frames"]
    segments = _segments(record, metadata["duration_us"], config)
    errors = []

    def frame_action(directory):
        shots = [s for s in copy.deepcopy(segments) if s["granularity"] == "coarse_shot"]
        extract_frames(source, frames, shots, directory, metadata["video"]["stream_index"])
        paths = []
        for segment in shots:
            for frame in segment["keyframes"]:
                path = Path(frame.pop("_path"))
                frame["uri"] = path.relative_to(root).as_posix()
                paths.append(path)
        return shots, paths

    try:
        shots = _run_stage(root, record, "keyframes", {**tools, "window_us": config.window_us,
                          "coarse_window_us": config.coarse_window_us, "pillow": _package_version("pillow")},
                          frame_action, lambda shots: all(valid_png(resolve_uri(root, frame["uri"]))
                          for shot in shots for frame in shot["keyframes"]), report, config)
    except StageFailure as exc:
        errors.append(_error(report.project_id, record["asset_id"], exc.stage, exc, exc.attempts))
        shots = []
        for segment in segments:
            segment["quality"] = {"usable": False, "flags": ["KEYFRAME_MISSING"]}
    by_id = {s["segment_id"]: s for s in shots}
    segments = [by_id.get(s["segment_id"], s) for s in segments]
    asset = {key: record[key] for key in ("asset_id", "revision", "origin", "uri", "content_hash", "provenance_note")}
    asset.update({"media_kind": "video", **metadata, "source_pts_uri": media["frames_uri"]})

    def proxy_action(directory):
        path = directory / "proxy.mp4"
        mapping = create_proxy(source, path, frames, config.proxy_width, metadata["video"]["stream_index"])
        mapping["source_time_base"] = metadata["video"]["time_base"]
        mapping["source_origin_pts"] = metadata["video"]["source_origin_pts"]
        map_path = directory / "proxy-map.json"
        write_json(map_path, mapping)
        return {"proxy_uri": path.relative_to(root).as_posix(),
                "proxy_mapping_uri": map_path.relative_to(root).as_posix()}, [path, map_path]

    try:
        asset.update(_run_stage(root, record, "proxy", {**tools, "width": config.proxy_width}, proxy_action,
                               lambda x: bool(x["proxy_uri"]), report, config))
        asset["proxy_status"] = "success"
    except StageFailure as exc:
        asset["proxy_status"] = "failed"
        errors.append(_error(report.project_id, record["asset_id"], exc.stage, exc, exc.attempts))

    def asr_action(directory):
        diagnostics = {}
        if not metadata["audio"]["present"]:
            status, transcript = "no_audio", []
        elif config.asr == "none":
            status, transcript = "disabled", []
        else:
            backend = FasterWhisperASR(model=str(model_context["path"]), device=config.asr_device,
                                      compute_type=config.asr_compute_type, language=config.asr_language or "",
                                      beam_size=config.asr_beam_size, allow_missing_audio=False,
                                      cpu_threads=config.asr_cpu_threads, num_workers=config.asr_num_workers,
                                      vad_filter=config.asr_vad, condition_on_previous_text=config.asr_condition_on_previous_text,
                                      initial_prompt=config.asr_initial_prompt, hotwords=config.asr_hotwords,
                                      word_timestamps=True, collect_diagnostics=True)
            # ASR decodes an extracted WAV starting at the video origin, so transcript times
            # share the exact normalized source coordinate even for offset audio streams.
            wav = directory / "asr.wav"
            run_media([_ffmpeg_binary(), "-v", "error", "-y", "-copyts", "-i", str(source), "-map", "0:a:0",
                       "-af", f"asetpts=PTS-({metadata['video']['source_origin_us']}/1000000)/TB,aresample=16000:async=1:first_pts=0",
                       "-t", str(metadata["duration_us"] / 1_000_000), "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
            recognized = backend.transcribe(wav)
            diagnostics = source_diagnostics(getattr(backend, "last_diagnostics", {}), metadata["duration_us"])
            transcript = []
            for item in recognized:
                if not math.isfinite(item.start) or not math.isfinite(item.end):
                    raise MediaError("ASR returned non-finite timestamps")
                begin, end = max(0, us(item.start)), min(metadata["duration_us"], us(item.end))
                if end > begin and item.text.strip():
                    transcript.append({"source_range_us": {"begin": begin, "end": end}, "text": item.text.strip()})
            status = "success" if transcript else "no_speech"
        result = {"status": status, "profile": model_profile, "segments": transcript, "diagnostics": diagnostics,
                  "source_content_hash": record["content_hash"],
                  "review": transcript_review(status, transcript, diagnostics, metadata["duration_us"])}
        path = directory / "transcript.json"
        write_json(path, result)
        return {**result, "uri": path.relative_to(root).as_posix()}, [path]

    try:
        if config.asr != "none" and metadata["audio"]["present"]:
            if "path" not in model_context and "error" not in model_context:
                try:
                    model_path = _resolve_asr_model(config)
                    weights = {p.relative_to(model_path).as_posix(): digest(p)
                               for p in sorted(model_path.rglob("*"))
                               if p.is_file() and ".cache" not in p.relative_to(model_path).parts}
                    model_context["profile"] = {**model_profile, "weight_hashes": weights}
                    model_context["path"] = model_path
                except Exception as exc:
                    model_context["error"] = exc
            if "error" in model_context:
                raise StageFailure("asr", model_context["error"], 1)
            model_profile = model_context["profile"]
        transcript = _run_stage(root, record, "asr", {**tools, **model_profile}, asr_action,
                                lambda x: x["status"] in {"no_audio", "disabled", "success", "no_speech"}, report, config)
    except StageFailure as exc:
        transcript = {"status": "failed", "profile": model_profile, "segments": []}
        transcript["review"] = transcript_review("failed", [], {}, metadata["duration_us"])
        errors.append(_error(report.project_id, record["asset_id"], exc.stage, exc, exc.attempts))
    asset["transcript"] = transcript
    for segment in segments:
        lo, hi = segment["source_range_us"]["begin"], segment["source_range_us"]["end"]
        overlapping = [t for t in transcript["segments"] if t["source_range_us"]["begin"] < hi and t["source_range_us"]["end"] > lo]
        segment["transcript"] = {"status": transcript["status"], "text": " ".join(t["text"] for t in overlapping),
                                 "segments": overlapping, "uri": transcript.get("uri"),
                                 "review_status": transcript["review"]["status"]}
    asset["processing_status"] = "partial" if errors else "success"
    return asset, segments, errors


class RunReport(list):
    def __init__(self, project_id):
        super().__init__()
        self.project_id = project_id


def ingest_assets(project_root: str | Path, sources: list[SourceSpec], *,
                  config: IngestConfig | None = None, project_id: str | None = None) -> dict:
    """Import/update sources and rebuild the aggregate manifest using per-stage caches.

    Original files are copied into the package. No retrieval embeddings are produced.
    A project is single-writer; different projects may run concurrently.
    """
    config = config or IngestConfig()
    config.validate()
    root = Path(project_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    internal = resolve_uri(root, ".quickcut")
    internal.mkdir(exist_ok=True)
    lock = internal / "ingest.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("Another ingestion owns this project (or a previous process left ingest.lock)") from exc
    os.close(descriptor)
    try:
        registry = internal / "registry.json"
        state = read_json(registry) if registry.exists() else {
            "version": 1, "project_id": project_id or "project-" + uuid4().hex,
            "document_id": "manifest-" + uuid4().hex, "assets": {}, "aliases": {},
        }
        if state.get("version") != 1 or project_id and state["project_id"] != project_id:
            raise ValueError("Registry version or project_id mismatch")
        if not sources and not state["assets"]:
            raise ValueError("A new project requires at least one input video")
        report = RunReport(state["project_id"])
        errors, blocked = [], set()
        for source in sources:
            alias = source.origin + ":" + (source.source_key or str(Path(source.path).expanduser().resolve()))
            asset_id = state["aliases"].get(alias, "input-" + fingerprint(alias))
            try:
                _register(root, state, source)
                write_json(registry, state)
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(_error(state["project_id"], asset_id, "import", exc))
                blocked.add(asset_id)
        tools = _tool_profile()
        model = {"profile": _model_profile(config)}
        assets, segments = [], []
        for asset_id, record in state["assets"].items():
            if asset_id in blocked:
                continue
            try:
                asset, parts, failures = _process(root, record, config, tools, model, report)
                assets.append(asset)
                segments.extend(parts)
                errors.extend(failures)
            except (StageFailure, OSError, ValueError, RuntimeError) as exc:
                errors.append(_error(state["project_id"], asset_id, getattr(exc, "stage", "probe"), exc,
                                     getattr(exc, "attempts", 1)))
            write_json(registry, state)
        manifest = {"schema_version": "0.1.0", "kind": "AssetManifest", "document_id": state["document_id"],
                    "project_id": state["project_id"], "revision": 1, "fixtures_only": False, "owner": "A", "inputs": [],
                    "assets": assets, "segments": segments,
                    "embedding_profile": {"status": "not_indexed", "owner": "B"},
                    "processing": {"status": "partial" if errors and assets else "failed" if errors else "success",
                                   "errors": errors}}
        path = resolve_uri(root, "asset-manifest.json")
        if path.exists():
            previous = read_json(path)
            manifest["revision"] = previous["revision"]
            if manifest != previous:
                manifest["revision"] += 1
        validate_asset_manifest(manifest, root)
        write_json(path, manifest)
        write_json(internal / "run-report.json", {"project_id": state["project_id"],
                   "manifest_revision": manifest["revision"], "stages": list(report), "errors": errors,
                   "config": asdict(config), "tools": tools})
        return manifest
    finally:
        lock.unlink()


def validate_asset_manifest(manifest: dict, root: Path) -> None:
    """Check manifest references, media hashes and source intervals."""
    if manifest["schema_version"] != "0.1.0" or manifest["kind"] != "AssetManifest" or manifest["fixtures_only"] is not False:
        raise ValueError("Expected a production AssetManifest 0.1.0")
    assets = {a["asset_id"]: a for a in manifest["assets"]}
    if len(assets) != len(manifest["assets"]):
        raise ValueError("Duplicate asset ID")
    segments = {s["segment_id"]: s for s in manifest["segments"]}
    if len(segments) != len(manifest["segments"]):
        raise ValueError("Duplicate segment ID")
    for asset in assets.values():
        if digest(resolve_uri(root, asset["uri"])) != asset["content_hash"]:
            raise ValueError("Original media hash mismatch")
        for key in ("proxy_uri", "proxy_mapping_uri", "source_pts_uri"):
            if asset.get(key) and not resolve_uri(root, asset[key]).is_file():
                raise ValueError(f"Missing {key}")
    for segment in segments.values():
        asset = assets[segment["asset_id"]]
        lo, hi = segment["source_range_us"]["begin"], segment["source_range_us"]["end"]
        if type(lo) is not int or type(hi) is not int or not 0 <= lo < hi <= asset["duration_us"]:
            raise ValueError("Invalid source interval")
        if segment["asset_revision"] != asset["revision"]:
            raise ValueError("Stale asset revision")
        parent = segments.get(segment["parent_segment_id"])
        if segment["parent_segment_id"] and (not parent or parent["asset_id"] != asset["asset_id"] or
                not parent["source_range_us"]["begin"] <= lo < hi <= parent["source_range_us"]["end"]):
            raise ValueError("Invalid segment parent")
        for frame in segment["keyframes"]:
            if not lo <= frame["source_time_us"] < hi or not valid_png(resolve_uri(root, frame["uri"])):
                raise ValueError("Missing/invalid keyframe evidence")
        if segment["quality"]["usable"] and not segment["keyframes"]:
            raise ValueError("Usable visual segment has no keyframe evidence")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Import a multi-source project and emit AssetManifest 0.1.0")
    parser.add_argument("videos", nargs="*")
    parser.add_argument("--external", nargs="*", default=[])
    parser.add_argument("--sources", help="JSON list of SourceSpec objects (paths relative to this JSON)")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--window", type=float, default=3)
    parser.add_argument("--coarse-window", type=float, default=30)
    parser.add_argument("--proxy-width", type=int, default=640)
    parser.add_argument("--asr", choices=["none", "faster-whisper"], default="none")
    parser.add_argument("--asr-model", default="tiny")
    parser.add_argument("--asr-language")
    parser.add_argument("--asr-model-revision")
    parser.add_argument("--asr-device", default="cpu")
    parser.add_argument("--asr-compute-type", default="int8")
    parser.add_argument("--asr-beam-size", type=int, default=5)
    parser.add_argument("--asr-cpu-threads", type=int, default=2)
    parser.add_argument("--asr-num-workers", type=int, default=1)
    parser.add_argument("--asr-vad", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--asr-condition-on-previous-text", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--asr-initial-prompt")
    parser.add_argument("--asr-hotwords")
    parser.add_argument("--max-attempts", type=int, default=1)
    args = parser.parse_args(argv)
    sources = [SourceSpec(p) for p in args.videos] + [SourceSpec(p, "external") for p in args.external]
    try:
        if args.sources:
            path = Path(args.sources).expanduser().resolve()
            items = read_json(path)
            if not isinstance(items, list):
                raise ValueError("Sources JSON must contain a list")
            for item in items:
                item = dict(item)
                item["path"] = path.parent / item["path"]
                sources.append(SourceSpec(**item))
        config = IngestConfig(window_us=us(args.window), coarse_window_us=us(args.coarse_window),
                              proxy_width=args.proxy_width, asr=args.asr, asr_model=args.asr_model,
                              asr_language=args.asr_language, asr_device=args.asr_device,
                              asr_compute_type=args.asr_compute_type, asr_model_revision=args.asr_model_revision,
                              asr_beam_size=args.asr_beam_size, asr_cpu_threads=args.asr_cpu_threads,
                              asr_num_workers=args.asr_num_workers, asr_vad=args.asr_vad,
                              asr_condition_on_previous_text=args.asr_condition_on_previous_text,
                              asr_initial_prompt=args.asr_initial_prompt, asr_hotwords=args.asr_hotwords,
                              max_attempts=args.max_attempts)
        result = ingest_assets(args.project_root, sources, config=config, project_id=args.project_id)
    except (ValueError, RuntimeError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": result["processing"]["status"], "revision": result["revision"],
                      "assets": len(result["assets"]), "segments": len(result["segments"]),
                      "transcripts_needing_review": sum(a["transcript"]["review"]["status"] == "needs_review" for a in result["assets"]),
                      "manifest": str(Path(args.project_root) / "asset-manifest.json")}, ensure_ascii=False))
    return {"success": 0, "partial": 2, "failed": 1}[result["processing"]["status"]]


if __name__ == "__main__":
    raise SystemExit(main())

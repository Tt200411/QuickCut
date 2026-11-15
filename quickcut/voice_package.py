"""E's versioned, local sentence audio. D alone owns global timeline positions.

Run ``python -m quickcut.voice_package --help`` for the independent entry point.
No model or media dependencies are imported until real synthesis is requested.
"""
from __future__ import annotations

import argparse
import array
import copy
import hashlib
import json
import math
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SCHEMA = "0.1.0"
SAMPLE_RATE = 48000


class VoiceError(ValueError):
    def __init__(self, code, message, ids=()):
        super().__init__(message)
        self.code, self.ids = code, list(ids)


def integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise VoiceError("INVALID_INPUT", f"{name} must be an integer >= {minimum}")
    return value


def reference(document):
    return {k: document[k] for k in ("kind", "document_id", "revision")}


def digest(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)


def capabilities():
    """Discover actual installed voices. Never silently substitute a voice."""
    if shutil.which("say"):
        voices = []
        for line in run(["say", "-v", "?"]).stdout.splitlines():
            match = re.match(r"^(.*?)\s+([a-z]{2}_[A-Z]{2})\s+#", line)
            if match:
                voices.append({"voice": match[1].strip(), "language": match[2].replace("_", "-")})
        return {"engine": "say", "version": platform.mac_ver()[0], "voices": voices,
                "local": True, "supported_parameters": ["voice", "rate"], "emotion_supported": False}
    engine = shutil.which("espeak-ng") or shutil.which("espeak")
    if engine:
        voices = []
        for line in run([engine, "--voices"]).stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4:
                voices.append({"voice": parts[1], "language": parts[1]})
        return {"engine": Path(engine).name, "version": run([engine, "--version"]).stdout.strip(),
                "voices": voices, "local": True, "supported_parameters": ["voice", "rate"],
                "emotion_supported": False}
    raise VoiceError("TTS_FAILED", "No local say/espeak engine available")


def select_profile(config, language, fixture):
    if fixture:
        return {"engine": "fixture-tone", "version": "1", "voice": "tone-not-speech",
                "language": language, "rate": 145, "local": True, "emotion_supported": False}
    info = capabilities()
    voice = config.get("voice")
    if not voice:
        preferred = "Reed (中文（中国大陆）)" if info["engine"] == "say" and language == "zh-CN" else None
        matches = [v for v in info["voices"] if v["language"].lower() == language.lower()]
        voice = next((v["voice"] for v in matches if v["voice"] == preferred), None)
        voice = voice or (matches[0]["voice"] if matches else None)
    matching = [v for v in info["voices"] if v["voice"] == voice]
    if not matching or matching[0]["language"].lower() != language.lower():
        raise VoiceError("UNSUPPORTED_VOICE", f"Voice {voice!r} is unavailable for {language}; use --capabilities")
    if config.get("emotion"):
        raise VoiceError("UNSUPPORTED_PARAMETER", "Installed engine has no verified emotion control")
    return {"engine": info["engine"], "version": info["version"], "voice": voice,
            "language": language, "rate": integer(config.get("rate", 145), "rate"),
            "local": True, "emotion_supported": False}


def synthesize(text, output, profile):
    """Engine adapter; all subprocess arguments are passed without a shell."""
    if profile["engine"] == "fixture-tone":
        samples = array.array("h", [round(4000 * math.sin(i * 2 * math.pi * 220 / SAMPLE_RATE))
                                   for i in range(SAMPLE_RATE)])
        write_pcm(output, samples)
        return
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "speech.aiff"
        script = Path(directory) / "text.txt"
        script.write_text(text, encoding="utf-8")
        if profile["engine"] == "say":
            run(["say", "-v", profile["voice"], "-r", str(profile["rate"]), "-f", str(script), "-o", str(source)])
        else:
            run([profile["engine"], "-v", profile["voice"], "-s", str(profile["rate"]),
                 "-f", str(script), "-w", str(source)])
        normalize_wav(source, output)


def normalize_wav(source, output):
    run(["ffmpeg", "-v", "error", "-i", str(source), "-ar", str(SAMPLE_RATE),
         "-ac", "1", "-c:a", "pcm_s16le", "-y", str(output)])


def read_pcm(path):
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            raise VoiceError("TTS_FAILED", "Expected mono PCM16 48000 Hz WAV")
        samples = array.array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples


def write_pcm(path, samples):
    pcm = array.array("h", samples)
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def duration(samples):
    return round(len(samples) * 1_000_000 / SAMPLE_RATE)


def trim_boundaries(samples):
    # Only remove boundary silence. Preserve 40ms handles and ALL internal pauses.
    active = [i for i, v in enumerate(samples) if abs(v) > 104]  # approximately -50 dBFS
    if not active:
        raise VoiceError("NO_SPEECH", "TTS returned silence, not usable speech")
    guard = 1920
    return samples[max(0, active[0] - guard):min(len(samples), active[-1] + 1 + guard)]


def safe_file(root, uri):
    if not isinstance(uri, str) or Path(uri).is_absolute() or "\\" in uri:
        raise VoiceError("INVALID_INPUT", "Media URI must be project relative")
    target = (root / uri).resolve()
    if not target.is_relative_to(root.resolve()):
        raise VoiceError("INVALID_INPUT", "Media URI escapes project root")
    return target


def validate_story(story, expected_revision=None):
    if story.get("kind") != "StoryPlan" or story.get("schema_version") != SCHEMA:
        raise VoiceError("INVALID_INPUT", "Expected StoryPlan schema 0.1.0")
    for key in ("document_id", "project_id"):
        if not isinstance(story.get(key), str) or not story[key]:
            raise VoiceError("INVALID_INPUT", f"Missing {key}")
    integer(story.get("revision"), "StoryPlan.revision")
    if expected_revision is not None and story["revision"] != expected_revision:
        raise VoiceError("STALE_INPUT_REVISION", "StoryPlan revision differs from requested revision")
    ids = set()
    beats = {b["beat_id"] for b in story.get("beats", [])}
    for cue in story.get("narration_cues", []):
        if not isinstance(cue.get("cue_id"), str) or not cue["cue_id"] or cue["cue_id"] in ids:
            raise VoiceError("INVALID_INPUT", "Missing/duplicate cue_id")
        ids.add(cue["cue_id"])
        if not cue.get("text", "").strip() or cue.get("beat_id") not in beats:
            raise VoiceError("INVALID_INPUT", "Empty text or unknown beat", [cue["cue_id"]])
        integer(cue.get("duration_budget_us"), "duration_budget_us")


def srt_time(us):
    ms = us // 1000
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def build_voice_package(story, root, *, config=None, mode="measure", request=None,
                        previous=None, fixture=False, revision=1, expected_revision=None):
    """Return a new VoicePackage. Never mutate StoryPlan or EditTimeline.

    Optional request carries exact StoryPlan/EditTimeline references, project_id,
    iteration/max_iterations and cue_ranges (global ranges echoed, never applied).
    """
    story = copy.deepcopy(story)
    validate_story(story, expected_revision)
    integer(revision, "VoicePackage.revision")
    config = dict(config or {})
    if mode not in ("measure", "fit"):
        raise VoiceError("INVALID_INPUT", "mode must be measure or fit")
    pause_us = integer(config.get("sentence_pause_us", 180000), "sentence_pause_us", 0)
    max_tempo = config.get("max_tempo", 1.15)
    if not isinstance(max_tempo, (int, float)) or not 1 <= max_tempo <= 1.25:
        raise VoiceError("INVALID_INPUT", "max_tempo must be between 1 and 1.25")
    ranges = {}
    if request is not None:
        if request.get("project_id") != story["project_id"] or request.get("story_plan") != reference(story):
            raise VoiceError("STALE_INPUT_REVISION", "Request project/script does not match StoryPlan")
        ref = request.get("timeline", {})
        if ref.get("kind") != "EditTimeline" or not ref.get("document_id"):
            raise VoiceError("INVALID_INPUT", "Request requires exact EditTimeline reference")
        integer(ref.get("revision"), "timeline revision")
        iteration = integer(request.get("iteration"), "iteration")
        if iteration > 2:
            raise VoiceError("VOICE_NEEDS_RETIME", "Two adaptation rounds exhausted", [c['cue_id'] for c in story.get('narration_cues',[])])
        ranges = request.get("cue_ranges", {})
        if not set(ranges).issubset({c['cue_id'] for c in story.get('narration_cues', [])}):
            raise VoiceError("INVALID_INPUT", "Request contains unknown cue IDs")
        for r in ranges.values():
            integer(r.get("begin"), "range begin", 0)
            integer(r.get("end"), "range end")
            if r["end"] <= r["begin"]:
                raise VoiceError("INVALID_INPUT", "Empty/reversed requested interval")
    if previous:
        if previous.get("project_id") != story["project_id"] or previous.get("kind") != "VoicePackage":
            raise VoiceError("STALE_INPUT_REVISION", "Previous voice package belongs to another project")
        old_script = next((r for r in previous.get("inputs", []) if r["kind"] == "StoryPlan"), None)
        if old_script and (old_script["document_id"] != story["document_id"] or old_script["revision"] > story["revision"]):
            raise VoiceError("STALE_INPUT_REVISION", "Cannot reuse a newer/different script package")
        if revision <= previous["revision"]:
            raise VoiceError("STALE_INPUT_REVISION", "New VoicePackage revision must increase")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    language = story.get("narration_language", "zh-CN")
    engine = config.get("engine", "auto")
    if not fixture and engine not in ("auto", "indextts2"):
        raise VoiceError("INVALID_INPUT", "Voice engine must be auto or indextts2")
    index_backend = None
    if not fixture and engine == "indextts2":
        from .indextts2 import IndexTTS2Backend

        try:
            index_backend = IndexTTS2Backend(config)
            _, profile = index_backend.cue_settings(config, language)
            if any(set(overrides) - {"reference_audio", "seed"}
                   for overrides in config.get("cue_overrides", {}).values()):
                raise ValueError("IndexTTS2 cue overrides support only reference_audio and seed")
        except (OSError, ValueError) as exc:
            raise VoiceError("INVALID_INPUT", str(exc)) from exc
    else:
        profile = select_profile(config, language, fixture)
    inputs = [reference(story)] + ([request["timeline"]] if request else [])
    package = dict(schema_version=SCHEMA, kind="VoicePackage", document_id="voice-" + story["document_id"],
                   project_id=story["project_id"], revision=revision, fixtures_only=fixture or story.get("fixtures_only", False),
                   owner="E", inputs=inputs, status="ready", timing_authority="D", mode=mode,
                   measurement_source="fixture_tone" if fixture else "measured_pcm_samples",
                   synthesis_profile=profile, assets=[], cues=[], errors=[],
                   request=copy.deepcopy(request), invalidated_cue_ids=[], alignment_invalidated=False)
    old_cues = {c["cue_id"]: c for c in (previous or {}).get("cues", [])}
    for cue in story.get("narration_cues", []):
        cid = cue["cue_id"]
        budget = ranges[cid]["end"] - ranges[cid]["begin"] if cid in ranges else cue["duration_budget_us"]
        cue_config = {**config, **config.get("cue_overrides", {}).get(cid, {})}
        record = {k: cue[k] for k in ("cue_id", "beat_id", "text")}
        record.update(duration_budget_us=budget, script_input=reference(story),
                      requested_timeline_range_us=ranges.get(cid))
        try:
            if index_backend is not None:
                speaker_audio, cue_profile = index_backend.cue_settings(cue_config, language)
            else:
                cue_profile = select_profile(cue_config, language, fixture)
            key = fingerprint({"text": cue["text"], "profile": cue_profile, "trim_version": 1})
            clean = safe_file(root, f"audio/cache/{key}.wav")
            clean.parent.mkdir(parents=True, exist_ok=True)
            record.update(synthesis_profile=cue_profile, cache_key=key)
            cache_meta = clean.with_suffix(".json")
            hit = clean.exists() and cache_meta.exists() and json.loads(cache_meta.read_text()).get("content_hash") == digest(clean)
            if not hit:
                with tempfile.TemporaryDirectory() as directory:
                    raw = Path(directory) / "raw.wav"
                    if index_backend is not None:
                        native = raw.with_name("native.wav")
                        index_backend.synthesize(cue["text"], native, speaker_audio, cue_profile["seed"])
                        normalize_wav(native, raw)
                    else:
                        synthesize(cue["text"], raw, cue_profile)
                    original = read_pcm(raw)
                    trimmed = trim_boundaries(original)
                    write_pcm(clean, trimmed)
                    cache_meta.write_text(json.dumps({"content_hash": digest(clean), "engine_duration_us": duration(original)}))
            samples = read_pcm(clean)
            raw_us = duration(samples)
            tempo = 1.0
            # Speed up only within the explicitly allowed limit; never trim speech.
            available_us = budget - pause_us
            if mode == "fit" and available_us > 0 and raw_us > available_us and raw_us / available_us <= max_tempo:
                tempo = raw_us / available_us
            processed = samples
            if tempo != 1:
                with tempfile.TemporaryDirectory() as directory:
                    fitted = Path(directory) / "fitted.wav"
                    run(["ffmpeg", "-v", "error", "-i", str(clean), "-af", f"atempo={tempo:.9f}", "-c:a", "pcm_s16le", "-y", str(fitted)])
                    processed = read_pcm(fitted)
            speech_us = duration(processed)
            final = array.array("h", processed)
            final.extend([0] * round(pause_us * SAMPLE_RATE / 1_000_000))
            output_key = fingerprint({"key": key, "tempo": tempo, "pause_us": pause_us})
            uri = f"audio/narration/{output_key}.wav"
            output = safe_file(root, uri)
            output.parent.mkdir(parents=True, exist_ok=True)
            if not output.exists() or read_pcm(output) != final:
                write_pcm(output, final)
            total_us = duration(final)
            needs = total_us > budget
            aid = "generated-voice-" + fingerprint([story["project_id"], cid, output_key])[:32]
            package["assets"].append(dict(asset_id=aid, revision=1, media_kind="audio", origin="generated", uri=uri,
                content_hash=digest(output), duration_us=total_us, audio=dict(present=True, sample_rate=SAMPLE_RATE, channels=1, codec="pcm_s16le")))
            srt_uri = f"subtitles/{output_key}.srt"
            srt = safe_file(root, srt_uri)
            srt.parent.mkdir(parents=True, exist_ok=True)
            srt.write_text(f"1\n00:00:00,000 --> {srt_time(speech_us)}\n{cue['text']}\n", encoding="utf-8")
            record.update(asset_id=aid, raw_duration_us=raw_us,
                          engine_duration_us=json.loads(cache_meta.read_text())["engine_duration_us"],
                          duration_us=total_us, speech_end_us=speech_us, sentence_pause_us=pause_us,
                          tempo_ratio=tempo, fit_status="needs_retime" if needs else "fits", needs_retime=needs,
                          cache_hit=hit, srt_uri=srt_uri,
                          quality={"peak_dbfs": round(20 * math.log10(max(abs(v) for v in processed) / 32768), 2),
                                   "clipped_samples": sum(abs(v) >= 32767 for v in processed),
                                   "text_verification": "not_asr_verified", "naturalness": "requires_listening"},
                          local_subtitles=[dict(range_us=dict(begin=0, end=speech_us), text=cue["text"])])
            if needs:
                package["errors"].append(error("VOICE_NEEDS_RETIME", "Measured audio exceeds budget; D retime or C shorten text", story, [cid], "D"))
        except (subprocess.SubprocessError, OSError, ValueError, wave.Error, ImportError, RuntimeError) as exc:
            code = exc.code if isinstance(exc, VoiceError) else "TTS_FAILED"
            record.update(fit_status="failed", needs_retime=False, error_code=code)
            package["errors"].append(error(code, str(exc), story, [cid]))
        package["cues"].append(record)
        old = old_cues.get(cid)
        if previous and (not old or old.get("asset_id") != record.get("asset_id") or old.get("duration_budget_us") != budget or record["fit_status"] == "failed"):
            package["invalidated_cue_ids"].append(cid)
    if previous:
        package["invalidated_cue_ids"] += sorted(set(old_cues) - {c["cue_id"] for c in package["cues"]})
        package["alignment_invalidated"] = bool(package["invalidated_cue_ids"] or previous.get("inputs") != inputs)
    failed = sum(c["fit_status"] == "failed" for c in package["cues"])
    package["status"] = ("failed" if failed == len(package["cues"]) and failed else "partial" if failed else
                         "needs_retime" if any(c["needs_retime"] for c in package["cues"]) else "ready")
    return package


def error(code, message, story, ids=(), owner="E"):
    return dict(status="failed", code=code, stage="voice", project_id=story.get("project_id"),
                message=message, retryable=False, affected_ids=list(ids), action_owner=owner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story", type=Path)
    parser.add_argument("--root", type=Path, default=Path("outputs/voice"))
    parser.add_argument("--output", help="Path relative to --root; default voice_package-rREVISION.json")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--expect-story-revision", type=int)
    parser.add_argument("--mode", choices=["measure", "fit"], default="measure")
    parser.add_argument("--fixture", action="store_true", help="Generate test tones, NOT speech")
    parser.add_argument("--capabilities", action="store_true")
    args = parser.parse_args()
    if args.capabilities:
        print(json.dumps(capabilities(), ensure_ascii=False, indent=2))
        return 0
    if not args.story:
        parser.error("--story is required")
    load = lambda path: json.loads(path.read_text(encoding="utf-8")) if path else None
    story = load(args.story)
    if "documents" in story:
        story = next(d for d in story["documents"] if d["kind"] == "StoryPlan")
    try:
        output = safe_file(args.root.resolve(), args.output or f"voice_package-r{args.revision}.json")
        if output.exists():
            raise VoiceError("OUTPUT_EXISTS", "Keep old revisions: choose a new --revision or --output")
        package = build_voice_package(story, args.root, config=load(args.config), mode=args.mode,
            request=load(args.request), previous=load(args.previous), fixture=args.fixture,
            revision=args.revision, expected_revision=args.expect_story_revision)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{package['status']}: {output}")
        return 0 if package["status"] == "ready" else 2
    except VoiceError as exc:
        print(json.dumps(error(exc.code, str(exc), story, exc.ids), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

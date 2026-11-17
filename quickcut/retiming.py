"""D-owned voice fitting requests and their exact, auditable acceptance.

No synthesizer is loaded here. Pending timelines contain requested slots, not
fictional measured audio. Only a matching E reply can produce exportable tracks.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import wave

from .media import digest, resolve_uri
from .timeline import LEAD_IN_US, TimelineError, _index, _make_clip, _picture_plans, _range, _ref


def content_hash(value) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _require(ok, message, code="FIT_REQUEST_INVALID", owner="D"):
    if not ok:
        raise TimelineError(message, code=code, action_owner=owner)


def _exact_ref(link, doc):
    return isinstance(link, dict) and type(link.get("revision")) is int and link == _ref(doc)


def verify_voice_media(voice: dict, project_root: str | Path) -> None:
    """Check actual complete mono PCM16 WAVs, including their final pause."""
    root = Path(project_root).expanduser().resolve(strict=True)
    assets = _index(voice.get("assets", []), "asset_id")
    for cue in voice.get("cues", []):
        if cue.get("fit_status") == "failed":
            continue
        asset = assets.get(cue.get("asset_id"))
        _require(asset is not None, "Voice cue has no complete audio asset", "VOICE_MEDIA_INVALID", "E")
        try:
            path = resolve_uri(root, asset["uri"])
            _require(path.is_file() and digest(path) == asset.get("content_hash"),
                     "Voice media is missing or its SHA256 changed", "VOICE_MEDIA_INVALID", "E")
            with wave.open(str(path), "rb") as wav:
                frames, rate = wav.getnframes(), wav.getframerate()
                _require((wav.getnchannels(), wav.getsampwidth(), rate) == (1, 2, 48000),
                         "Voice media must be normalized mono PCM16 / 48kHz", "VOICE_MEDIA_INVALID", "E")
                declared = asset.get("duration_us")
                _require(type(declared) is int and declared > 0 and declared == cue.get("duration_us")
                         and abs(frames * 1_000_000 - declared * rate) <= 1_000_000,
                         "Complete WAV sample duration differs from the voice declaration", "VOICE_MEDIA_INVALID", "E")
                size, nonzero = 0, False
                while block := wav.readframes(16384):
                    size += len(block)
                    nonzero = nonzero or any(block)
                _require(size == frames * 2 and nonzero, "Voice WAV is truncated or entirely silent",
                         "VOICE_MEDIA_INVALID", "E")
        except (OSError, ValueError, wave.Error, KeyError) as exc:
            if isinstance(exc, TimelineError):
                raise
            raise TimelineError(f"Cannot verify complete voice audio: {exc}", code="VOICE_MEDIA_INVALID", action_owner="E") from exc


def validate_voice_content(story: dict, voice: dict, *, allow_needs_retime: bool) -> None:
    _require(isinstance(voice, dict), "Voice package must be an object", "VOICE_NOT_READY", "E")
    _require(voice.get("kind") == "VoicePackage" and voice.get("schema_version") == "0.1.0"
             and isinstance(voice.get("document_id"), str) and bool(voice["document_id"])
             and voice.get("project_id") == story["project_id"] and voice.get("owner") == "E"
             and type(voice.get("revision")) is int and voice["revision"] > 0
             and type(voice.get("fixtures_only")) is bool,
             "Voice package identity is invalid", "VOICE_NOT_READY", "E")
    _require(not story["fixtures_only"] or voice["fixtures_only"], "Voice lost fixture provenance", "VOICE_NOT_READY", "E")
    states = {"ready", "needs_retime"} if allow_needs_retime else {"ready"}
    _require(voice.get("status") in states, "Voice package is not usable for fitting", "VOICE_NOT_READY", "E")
    errors = voice.get("errors", [])
    _require(isinstance(errors, list) and (not errors or allow_needs_retime and
             all(isinstance(e, dict) and e.get("code") == "VOICE_NEEDS_RETIME" for e in errors)),
             "Voice package contains a non-timing error", "VOICE_NOT_READY", "E")
    approved = _index(story["narration_cues"], "cue_id")
    cues = _index(voice.get("cues", []), "cue_id")
    assets = _index(voice.get("assets", []), "asset_id")
    _require(set(cues) == set(approved), "Voice reply does not cover the exact approved cue set", "VOICE_NOT_READY", "E")
    for cid, cue in cues.items():
        source, asset = approved[cid], assets.get(cue.get("asset_id"))
        _require(source.get("approved") is True and cue.get("text") == source["text"]
                 and cue.get("beat_id") == source["beat_id"] and _exact_ref(cue.get("script_input"), story),
                 f"{cid}: reply is not bound to the approved script", "STALE_INPUT_REVISION", "E")
        duration, budget = cue.get("duration_us"), cue.get("duration_budget_us")
        _require(type(duration) is int and duration > 0 and type(budget) is int and budget > 0
                 and asset is not None and asset.get("duration_us") == duration and asset.get("media_kind") == "audio"
                 and type(asset.get("revision")) is int and asset["revision"] > 0,
                 f"{cid}: incomplete measured voice", "VOICE_NOT_READY", "E")
        needs = duration > budget
        _require(cue.get("needs_retime") is needs and cue.get("fit_status") == ("needs_retime" if needs else "fits"),
                 f"{cid}: fit status contradicts measured duration", "VOICE_NOT_READY", "E")
        previous, texts = 0, []
        local_subtitles = cue.get("local_subtitles")
        _require(isinstance(local_subtitles, list), f"{cid}: local subtitles must be an array", "VOICE_NOT_READY", "E")
        for subtitle in local_subtitles:
            _require(isinstance(subtitle, dict) and isinstance(subtitle.get("range_us"), dict)
                     and isinstance(subtitle.get("text"), str),
                     f"{cid}: invalid local subtitle object", "VOICE_NOT_READY", "E")
            interval = subtitle.get("range_us", {})
            begin, end = interval.get("begin"), interval.get("end")
            _require(type(begin) is int and type(end) is int and previous <= begin < end <= duration,
                     f"{cid}: invalid local subtitle timing", "VOICE_NOT_READY", "E")
            previous = end
            texts.append(subtitle.get("text", ""))
        _require(texts and "".join(texts) == source["text"], f"{cid}: subtitle text differs from approved speech",
                 "VOICE_NOT_READY", "E")
        if not allow_needs_retime:
            _require(not needs, f"{cid}: fitted voice still exceeds its slot", "VOICE_NEEDS_RETIME", "C")
    any_needs = any(c["needs_retime"] for c in cues.values())
    _require(voice["status"] == ("needs_retime" if any_needs else "ready") and bool(errors) == any_needs,
             "Voice package status does not match its cue results", "VOICE_NOT_READY", "E")


def request_identity(request: dict) -> str:
    return "voice-fit-" + content_hash({k: v for k, v in request.items() if k != "request_id"}).split(":", 1)[1]


def prepare_pending(story: dict, measured_voice: dict, brief: dict, manifest: dict, output: dict, *,
                    document_id: str, revision: int, iteration: int, max_iterations: int,
                    boundaries: dict | None, previous_request: dict | None = None) -> dict:
    validate_voice_content(story, measured_voice, allow_needs_retime=True)
    _require(type(revision) is int and revision > 0 and type(max_iterations) is int and 1 <= max_iterations <= 2
             and type(iteration) is int and 1 <= iteration <= max_iterations, "At most two fitting rounds are allowed")
    if iteration == 1:
        _require(previous_request is None and measured_voice.get("mode") == "measure"
                 and content_hash(measured_voice.get("inputs")) == content_hash([_ref(story)])
                 and not measured_voice.get("request"), "Initial fitting requires the original measurement package",
                 "STALE_INPUT_REVISION", "E")
    else:
        _require(isinstance(previous_request, dict) and previous_request.get("iteration") == iteration - 1
                 and previous_request.get("max_iterations") == max_iterations
                 and previous_request.get("request_id") == request_identity(previous_request)
                 and content_hash(measured_voice.get("request")) == content_hash(previous_request)
                 and _exact_ref(previous_request.get("expected_voice"), measured_voice)
                 and previous_request.get("story_content_hash") == content_hash(story)
                 and previous_request.get("manifest_content_hash") == content_hash(manifest)
                 and previous_request.get("boundaries_hash") == content_hash(boundaries),
                 "Retry does not continue the immediately preceding request/reply", "STALE_INPUT_REVISION", "D")
    assets = _index(manifest["assets"] + measured_voice["assets"], "asset_id")
    plans, decisions = _picture_plans(brief, manifest, story, assets, boundaries)
    approved = _index(story["narration_cues"], "cue_id")
    cues = _index(measured_voice["cues"], "cue_id")
    remaining = brief["requirements"]["target_duration_us"] - sum(p[2] for p in plans)
    picture, original, ranges, beat_lengths = [], [], {}, {}
    cursor = 0
    for beat, shots, planned, budget in plans:
        ids = beat["cue_ids"]
        wanted = [min(cues[cid]["duration_us"], approved[cid]["duration_budget_us"]) for cid in ids]
        total = sum(wanted)
        capacity = min(budget - planned, remaining, sum(s[4] for s in shots))
        extension = min(capacity, max(0, total + (LEAD_IN_US if ids else 0) - planned))
        length = planned + extension
        remaining -= extension
        beat_lengths[beat["beat_id"]] = length
        if total > length:
            _require(length >= len(ids), "No positive voice slot remains after the picture edit", "VOICE_NEEDS_RETIME", "C")
            # Proportional bounded slots use actual measured needs, never increase
            # an approved cue budget, and assign integer-microsecond remainder.
            slots = [max(1, value * length // total) for value in wanted]
            difference = length - sum(slots)
            for index in reversed(range(len(slots))):
                if difference >= 0:
                    extra = min(difference, wanted[index] - slots[index])
                else:
                    extra = -min(-difference, slots[index] - 1)
                slots[index] += extra
                difference -= extra
            _require(difference == 0, "Cannot allocate bounded voice slots", "VOICE_NEEDS_RETIME", "C")
        else:
            slots = wanted
        voice_cursor = cursor + min(LEAD_IN_US, length - sum(slots)) if ids else cursor
        for cid, slot in zip(ids, slots):
            ranges[cid] = _range(voice_cursor, voice_cursor + slot)
            voice_cursor += slot
        extras = [0] * len(shots)
        for index in reversed(range(len(shots))):
            extras[index] = min(extension, shots[index][4])
            extension -= extras[index]
        for index, (shot, asset, begin, duration, _) in enumerate(shots):
            number = len(picture) + 1
            duration += extras[index]
            clip = _make_clip(f"clip-video-{number}", asset, begin, cursor, duration)
            has_audio = brief["constraints"]["preserve_source_audio"] and asset.get("audio", {}).get("present") is True
            picture.append({**clip, "segment_id": shot["segment_id"], "beat_id": beat["beat_id"], "role": shot["role"],
                            "audio_policy": "separate_track" if has_audio else "muted"})
            if has_audio:
                original.append({**clip, "clip_id": f"clip-original-{number}", "linked_video_clip_id": clip["clip_id"],
                                 "gain": 0.3 if ids else 1.0})
            decisions[number - 1].update(clip_id=clip["clip_id"], source_range_us=deepcopy(clip["source_range_us"]))
            cursor += duration
    pending = {"schema_version": "0.1.0", "kind": "EditTimeline", "owner": "D", "project_id": story["project_id"],
        "document_id": document_id + "-pending-fit", "revision": revision + iteration - 1,
        "fixtures_only": any(d["fixtures_only"] for d in (brief, manifest, story, measured_voice)),
        "status": "pending_voice_fit", "duration_us": cursor, "output": deepcopy(output),
        "inputs": [_ref(d) for d in (story, manifest, measured_voice, brief)],
        "input_content_hashes": {d["kind"]: content_hash(d) for d in (story, manifest, measured_voice, brief)},
        "target_timeline": {"kind": "EditTimeline", "document_id": document_id, "revision": revision},
        "tracks": [{"track_id": "track-picture", "kind": "video", "clips": picture},
                   {"track_id": "track-original", "kind": "source_audio", "clips": original}],
        "cue_ranges": ranges, "beat_lengths_us": beat_lengths, "boundary_decisions": decisions,
        "boundaries": deepcopy(boundaries), "render_policy": {"preview_burn_subtitles": True,
            "editable_export_burn_subtitles": False, "retain_separate_audio_tracks": True, "unsupported_effect_policy": "error"}}
    request = {"kind": "VoiceFitRequest", "schema_version": "0.1.0", "owner": "D", "project_id": story["project_id"],
        "story_plan": _ref(story), "story_content_hash": content_hash(story), "timeline": _ref(pending),
        "timeline_content_hash": content_hash(pending), "manifest_content_hash": content_hash(manifest),
        "brief_content_hash": content_hash(brief), "boundaries_hash": content_hash(boundaries),
        "measured_voice": _ref(measured_voice), "measured_voice_content_hash": content_hash(measured_voice),
        "expected_voice": {"kind": "VoicePackage", "document_id": measured_voice["document_id"], "revision": measured_voice["revision"] + 1},
        "voice_revision": measured_voice["revision"] + 1, "iteration": iteration, "max_iterations": max_iterations,
        "previous_request_id": previous_request["request_id"] if previous_request else None,
        "cue_ranges": deepcopy(ranges)}
    request["request_id"] = request_identity(request)
    return {"pending_timeline": pending, "request": request}


def validate_reply_identity(pending: dict, request: dict, voice: dict) -> None:
    _require(all(isinstance(doc, dict) for doc in (pending, request, voice)),
             "Fitting request, pending timeline and reply must be objects", "STALE_INPUT_REVISION")
    _require(request.get("kind") == "VoiceFitRequest" and request.get("schema_version") == "0.1.0"
             and request.get("owner") == "D" and request.get("request_id") == request_identity(request)
             and type(request.get("max_iterations")) is int and 1 <= request["max_iterations"] <= 2
             and type(request.get("iteration")) is int and 1 <= request["iteration"] <= request["max_iterations"],
             "Unknown or changed fitting request", "STALE_INPUT_REVISION")
    _require(pending.get("status") == "pending_voice_fit" and pending.get("owner") == "D"
             and _exact_ref(request.get("timeline"), pending) and request.get("timeline_content_hash") == content_hash(pending)
             and request.get("cue_ranges") == pending.get("cue_ranges"),
             "Pending timeline changed after the fitting request", "STALE_INPUT_REVISION")
    _require(voice.get("mode") == "fit" and content_hash(voice.get("request")) == content_hash(request)
             and _exact_ref(request.get("expected_voice"), voice)
             and voice["revision"] == request.get("voice_revision")
             and content_hash(voice.get("inputs")) == content_hash([request["story_plan"], request["timeline"]])
             and voice.get("project_id") == pending.get("project_id") == request.get("project_id"),
             "Voice reply belongs to a different request, timeline, project or revision", "STALE_INPUT_REVISION", "E")
    if pending.get("fixtures_only"):
        _require(voice.get("fixtures_only") is True, "Fit reply lost fixture provenance", "VOICE_NOT_READY", "E")
    for cue in _index(voice.get("cues", []), "cue_id").values():
        interval = request["cue_ranges"].get(cue.get("cue_id"))
        _require(interval is not None and cue.get("requested_timeline_range_us") == interval
                 and cue.get("duration_budget_us") == interval["end"] - interval["begin"],
                 "Voice reply changed a requested global interval", "STALE_INPUT_REVISION", "E")


def compile_reply(story: dict, voice: dict, pending: dict, request: dict, brief: dict, manifest: dict,
                  boundaries: dict | None) -> dict:
    validate_reply_identity(pending, request, voice)
    for key, doc in (("story", story), ("manifest", manifest), ("brief", brief)):
        _require(request.get(key + "_content_hash") == content_hash(doc),
                 f"{key} content changed while fitting, even if its revision was reused", "STALE_INPUT_REVISION")
    _require(request.get("boundaries_hash") == content_hash(boundaries), "Manual bounds changed while fitting",
             "STALE_INPUT_REVISION")
    validate_voice_content(story, voice, allow_needs_retime=False)
    assets = _index(manifest["assets"] + voice["assets"], "asset_id")
    narration, subtitles = [], []
    for cue in voice["cues"]:
        cid, duration = cue["cue_id"], cue["duration_us"]
        bounds = request["cue_ranges"][cid]
        _require(duration <= bounds["end"] - bounds["begin"], "Complete fitted voice does not fit its slot",
                 "VOICE_NEEDS_RETIME", "C")
        clip = _make_clip(f"clip-narration-{cid}", assets[cue["asset_id"]], 0, bounds["begin"], duration)
        narration.append({**clip, "cue_id": cid, "gain": 1.0})
        for index, local in enumerate(cue["local_subtitles"], 1):
            subtitles.append({"subtitle_id": f"sub-{cid}" if len(cue["local_subtitles"]) == 1 else f"sub-{cid}-{index}",
                "cue_id": cid, "text": local["text"], "editable": True,
                "timeline_range_us": {key: value + bounds["begin"] for key, value in local["range_us"].items()}})
    narration.sort(key=lambda c: c["timeline_range_us"]["begin"])
    subtitles.sort(key=lambda c: c["timeline_range_us"]["begin"])
    final = {key: deepcopy(pending[key]) for key in ("schema_version", "kind", "owner", "project_id", "fixtures_only",
        "duration_us", "output", "tracks", "boundary_decisions", "render_policy")}
    final.update(document_id=pending["target_timeline"]["document_id"], revision=pending["target_timeline"]["revision"],
        status="ready_for_export", inputs=[_ref(d) for d in (story, manifest, voice, brief)])
    final["tracks"].extend([{"track_id": "track-narration", "kind": "narration", "clips": narration},
                            {"track_id": "track-subtitles", "kind": "subtitle", "cues": subtitles}])
    final["voice_fit"] = {"request": deepcopy(request), "pending_timeline": deepcopy(pending),
                          "accepted_voice": _ref(voice), "reply_hash": content_hash(voice)}
    validate_voice_fit_receipt(final, voice, manifest=manifest)
    return final


def validate_voice_fit_receipt(timeline: dict, voice: dict, *, manifest: dict | None = None) -> None:
    """Independent downstream check; never accept a bare extra Timeline reference."""
    try:
        receipt = timeline["voice_fit"]
        pending, request = receipt["pending_timeline"], receipt["request"]
        validate_reply_identity(pending, request, voice)
        _require(_exact_ref(pending["target_timeline"], timeline)
                 and _exact_ref(receipt["accepted_voice"], voice) and receipt["reply_hash"] == content_hash(voice),
                 "Fit receipt is not bound to this exact timeline/reply", "STALE_INPUT_REVISION")
        links, prior = _index(timeline["inputs"], "kind"), _index(pending["inputs"], "kind")
        _require(all(links.get(k) == prior.get(k) for k in ("StoryPlan", "AssetManifest", "Brief"))
                 and links["StoryPlan"] == request["story_plan"] and _exact_ref(links["VoicePackage"], voice),
                 "Final timeline changed fitting dependencies", "STALE_INPUT_REVISION")
        if manifest is not None:
            _require(request["manifest_content_hash"] == content_hash(manifest), "Fit receipt refers to another asset manifest",
                     "STALE_INPUT_REVISION")
        by_kind = _index(timeline["tracks"], "kind")
        _require([by_kind["video"], by_kind["source_audio"]] == pending["tracks"]
                 and timeline["duration_us"] == pending["duration_us"] and timeline["output"] == pending["output"]
                 and timeline.get("boundary_decisions") == pending["boundary_decisions"],
                 "Picture timing changed after voice fitting", "STALE_INPUT_REVISION")
        cues = _index(voice["cues"], "cue_id")
        clips = _index(by_kind["narration"]["clips"], "cue_id")
        _require(set(cues) == set(clips) == set(request["cue_ranges"]), "Fitted cue set changed", "STALE_INPUT_REVISION")
        for cid, cue in cues.items():
            slot, clip = request["cue_ranges"][cid], clips[cid]
            _require(clip["source_range_us"] == _range(0, cue["duration_us"])
                     and clip["timeline_range_us"] == _range(slot["begin"], slot["begin"] + cue["duration_us"])
                     and clip["timeline_range_us"]["end"] <= slot["end"],
                     "Fitted voice was moved or truncated after acceptance", "STALE_INPUT_REVISION")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, TimelineError):
            raise
        raise TimelineError(f"Invalid fitting receipt: {exc}", code="STALE_INPUT_REVISION") from exc

"""Compile QuickCut's 0.1.0 multi-source editing contract."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

Document = dict[str, Any]
SCHEMA_VERSION = "0.1.0"
LEAD_IN_US = 200_000


class TimelineError(ValueError):
    def __init__(self, message: str, *, code: str = "TIMELINE_INVALID",
                 affected_ids: tuple[str, ...] = (), action_owner: str = "D"):
        super().__init__(message)
        self.code = code
        self.affected_ids = affected_ids
        self.action_owner = action_owner

    def to_dict(self, project_id: str) -> Document:
        return {
            "status": "needs_retime" if self.code == "VOICE_NEEDS_RETIME" else "failed",
            "code": self.code, "stage": "timeline", "project_id": project_id,
            "message": str(self), "retryable": False,
            "affected_ids": list(self.affected_ids), "action_owner": self.action_owner,
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TimelineError(message)


def _index(records: list[Document], key: str) -> dict[str, Document]:
    result = {}
    for record in records:
        value = record[key]
        _require(isinstance(value, str) and bool(value) and value not in result,
                 f"Missing or duplicate {key}: {value}")
        result[value] = record
    return result


def _positive_us(value: int, label: str) -> int:
    _require(type(value) is int and value > 0, f"{label} must be positive integer microseconds")
    return value


def _range(begin: int, end: int) -> Document:
    return {"begin": begin, "end": end}


def _ref(doc: Document) -> Document:
    return {key: doc[key] for key in ("kind", "document_id", "revision")}


def _check_refs(docs: dict[str, Document]) -> None:
    required = {"StoryPlan": ("AssetManifest", "Brief"), "VoicePackage": ("StoryPlan",)}
    for kind, source in docs.items():
        links = _index(source["inputs"], "kind")
        if kind == "VoicePackage" and "EditTimeline" in links:
            raise TimelineError(
                "Initial compilation requires voice measurements without a timeline-specific request",
                code="STALE_INPUT_REVISION", affected_ids=(source["document_id"],), action_owner="E",
            )
        for dependency in required.get(kind, ()):
            links.setdefault(dependency, {})
        for dependency, link in links.items():
            if dependency in docs and link != _ref(docs[dependency]):
                raise TimelineError(
                    f"{kind} must reference the current {dependency}",
                    code="STALE_INPUT_REVISION", affected_ids=(source["document_id"],),
                    action_owner=source["owner"],
                )


def _make_clip(clip_id: str, asset: Document, source_begin: int,
               timeline_begin: int, duration: int) -> Document:
    return {
        "clip_id": clip_id, "asset_id": asset["asset_id"],
        "asset_revision": asset["revision"],
        "source_range_us": _range(source_begin, source_begin + duration),
        "timeline_range_us": _range(timeline_begin, timeline_begin + duration),
        "playback_rate": {"numerator": 1, "denominator": 1},
    }


def compile_timeline(
    brief: Document, manifest: Document, story: Document, voice: Document, *,
    output: Document, document_id: str = "timeline-001", revision: int = 1,
) -> Document:
    """Build four tracks from approved selections and measured voice durations.

    Picture uses hard cuts at 1×. Optional headroom comes from the uncut tail
    of a selected segment, within its beat budget and the overall brief.
    """
    project_id = brief["project_id"]
    fixtures_only = any(doc["fixtures_only"] for doc in (brief, manifest, story, voice))
    _positive_us(revision, "Timeline revision")
    _require(bool(document_id), "Timeline document ID is required")
    docs = dict(zip(("Brief", "AssetManifest", "StoryPlan", "VoicePackage"),
                    (brief, manifest, story, voice)))
    for kind, doc in docs.items():
        _require(doc["kind"] == kind and doc["schema_version"] == SCHEMA_VERSION
                 and doc["project_id"] == project_id and type(doc["fixtures_only"]) is bool,
                 f"{kind}: wrong kind, project, fixture flag, or schema version")
        _positive_us(doc["revision"], f"{kind} revision")
    _check_refs(docs)
    _require(not any(doc["fixtures_only"] for doc in (brief, manifest)) or story["fixtures_only"],
             "StoryPlan must preserve upstream fixture provenance")
    _require(not story["fixtures_only"] or voice["fixtures_only"],
             "VoicePackage must preserve StoryPlan fixture provenance")
    _require(story["status"] == "ready_for_edit" and not story["unresolved_gaps"]
             and not story.get("pending_reviews"),
             "StoryPlan must be approved and have no unresolved gaps")
    _require(voice.get("status") == "ready" and not voice.get("errors"),
             "VoicePackage must be ready and have no unresolved errors")
    for key in ("width", "height", "sample_rate"):
        _positive_us(output[key], f"Output {key}")
    for key in ("numerator", "denominator"):
        _positive_us(output["fps"][key], f"Output fps {key}")

    limit = _positive_us(brief["requirements"]["target_duration_us"], "Brief duration")
    preserve_audio = brief["constraints"]["preserve_source_audio"]
    assets = _index(manifest["assets"] + voice["assets"], "asset_id")
    segments = _index(manifest["segments"], "segment_id")
    story_cues = _index(story["narration_cues"], "cue_id")
    voice_cues = _index(voice["cues"], "cue_id")
    _require(set(story_cues) == set(voice_cues), "VoicePackage must cover the approved cues")
    beats = sorted(_index(story["beats"], "beat_id").values(), key=lambda beat: beat["order"])
    _require(bool(beats) and len({beat["order"] for beat in beats}) == len(beats),
             "Story must have at least one beat and an unambiguous order")
    cue_order = [cue_id for beat in beats for cue_id in beat["cue_ids"]]
    _require(len(cue_order) == len(story_cues) and set(cue_order) == set(story_cues),
             "Each approved cue must appear in exactly one beat")

    plans = []
    planned_total = 0
    for beat in beats:
        beat_id = beat["beat_id"]
        budget = _positive_us(beat["duration_budget_us"], f"{beat_id} budget")
        shots = []
        for shot in beat["shots"]:
            segment = segments[shot["segment_id"]]
            asset = assets[segment["asset_id"]]
            source = segment["source_range_us"]
            begin, end = source["begin"], source["end"]
            duration = _positive_us(shot.get("suggested_duration_us", end - begin), "Shot duration")
            _require(asset["media_kind"] == "video" and segment["asset_revision"] == asset["revision"],
                     f"{segment['segment_id']}: video asset missing or stale")
            _require(type(begin) is int and type(end) is int and 0 <= begin < end <= asset["duration_us"]
                     and duration <= end - begin, f"{segment['segment_id']}: source interval out of bounds")
            shots.append((shot, asset, begin, duration, end - begin - duration))
        planned = sum(shot[3] for shot in shots)
        _require(0 < planned <= budget, f"{beat_id}: picture is empty or exceeds its beat budget")
        planned_total += planned
        plans.append((beat, shots, planned, budget))
    _require(planned_total <= limit, "Planned picture exceeds the Brief budget")

    picture, original, narration, subtitles = [], [], [], []
    timeline_cursor = 0
    remaining = limit - planned_total
    for beat, shots, planned, budget in plans:
        beat_id = beat["beat_id"]
        cues = [voice_cues[cue_id] for cue_id in beat["cue_ids"]]
        for cue in cues:
            cue_id = cue["cue_id"]
            approved = story_cues[cue_id]
            asset = assets[cue["asset_id"]]
            _require(cue["beat_id"] == approved["beat_id"] == beat_id and cue["text"] == approved["text"],
                     f"{cue_id}: voice does not match the approved text and beat")
            _require(cue["fit_status"] in {"fits", "needs_retime"},
                     f"{cue_id}: no usable voice synthesis result")
            duration = _positive_us(cue["duration_us"], f"{cue_id} measured duration")
            _require(asset["media_kind"] == "audio" and duration == asset["duration_us"],
                     f"{cue_id}: measured duration does not match the audio asset")
            locals_ = cue["local_subtitles"]
            _require("".join(item["text"] for item in locals_) == approved["text"],
                     f"{cue_id}: local subtitle text does not match approved text")
            previous_end = 0
            for item in locals_:
                begin, end = item["range_us"]["begin"], item["range_us"]["end"]
                _require(type(begin) is int and type(end) is int and previous_end <= begin < end <= duration,
                         f"{cue_id}: invalid local subtitle range")
                previous_end = end
            cue_budget = min(
                _positive_us(cue["duration_budget_us"], f"{cue_id} voice budget"),
                _positive_us(approved["duration_budget_us"], f"{cue_id} story budget"),
            )
            if cue["needs_retime"] or cue["fit_status"] == "needs_retime" or duration > cue_budget:
                raise TimelineError(
                    f"{cue_id}: voice is {duration} us, exceeding or requiring review of its approved budget",
                    code="VOICE_NEEDS_RETIME", affected_ids=(cue_id,), action_owner="C",
                )

        speech = sum(cue["duration_us"] for cue in cues)
        capacity = min(budget - planned, remaining, sum(shot[4] for shot in shots))
        preferred = speech + (LEAD_IN_US if cues else 0)
        extension = min(capacity, max(0, preferred - planned))
        length = planned + extension
        if speech > length:
            raise TimelineError(
                f"{beat_id}: voice needs {speech} us, but selected picture can provide {length} us",
                code="VOICE_NEEDS_RETIME", affected_ids=tuple(beat["cue_ids"]), action_owner="C",
            )
        remaining -= extension
        voice_cursor = timeline_cursor + min(LEAD_IN_US, length - speech)
        # Extend from the end of the beat first to preserve earlier cut points.
        extra = [0] * len(shots)
        for index in reversed(range(len(shots))):
            extra[index] = min(extension, shots[index][4])
            extension -= extra[index]
        for index, (shot, asset, begin, duration, _) in enumerate(shots):
            number = len(picture) + 1
            duration += extra[index]
            clip = _make_clip(f"clip-video-{number}", asset, begin, timeline_cursor, duration)
            has_original = preserve_audio and asset.get("audio", {}).get("present") is True
            picture.append({
                **clip, "segment_id": shot["segment_id"], "beat_id": beat_id, "role": shot["role"],
                "audio_policy": "separate_track" if has_original else "muted",
            })
            if has_original:
                original.append({
                    **clip, "clip_id": f"clip-original-{number}", "linked_video_clip_id": clip["clip_id"],
                    "gain": 0.3 if cues else 1.0,
                })
            timeline_cursor += duration
        for cue in cues:
            cue_id, duration = cue["cue_id"], cue["duration_us"]
            clip = _make_clip(f"clip-narration-{cue_id}", assets[cue["asset_id"]], 0, voice_cursor, duration)
            narration.append({**clip, "cue_id": cue_id, "gain": 1.0})
            # E 的字幕相对于句子音频起点；句尾静音不延长字幕。
            for index, local in enumerate(cue["local_subtitles"], 1):
                subtitles.append({
                    "subtitle_id": f"sub-{cue_id}" if len(cue["local_subtitles"]) == 1 else f"sub-{cue_id}-{index}",
                    "cue_id": cue_id, "text": local["text"],
                    "timeline_range_us": _range(voice_cursor + local["range_us"]["begin"],
                                                voice_cursor + local["range_us"]["end"]),
                    "editable": True,
                })
            voice_cursor += duration

    return {
        "schema_version": SCHEMA_VERSION, "kind": "EditTimeline", "document_id": document_id,
        "project_id": project_id, "revision": revision, "fixtures_only": fixtures_only, "owner": "D",
        "inputs": [_ref(doc) for doc in (story, manifest, voice, brief)],
        "status": "ready_for_export", "duration_us": timeline_cursor, "output": deepcopy(output),
        "tracks": [
            {"track_id": "track-picture", "kind": "video", "clips": picture},
            {"track_id": "track-original", "kind": "source_audio", "clips": original},
            {"track_id": "track-narration", "kind": "narration", "clips": narration},
            {"track_id": "track-subtitles", "kind": "subtitle", "cues": subtitles},
        ],
        "render_policy": {
            "preview_burn_subtitles": True, "editable_export_burn_subtitles": False,
            "retain_separate_audio_tracks": True, "unsupported_effect_policy": "error",
        },
    }

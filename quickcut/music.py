"""Local licensed-music selection and a shared, prepared preview/editor mix."""
from __future__ import annotations

import array
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import wave

from . import core
from .media import digest, resolve_uri, write_json


def catalog_path():
    return Path(os.getenv("QUICKCUT_MUSIC_CATALOG", str(Path(__file__).resolve().parents[1] /
                                                     "outputs/music-library/catalog.json")))


def load_catalog(path=None):
    path = Path(path or catalog_path()).resolve()
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("tracks"), list):
        raise ValueError("Invalid music catalog")
    tracks = []
    for entry in document["tracks"]:
        if not isinstance(entry, dict):
            raise ValueError("Music entries must be objects")
        item = dict(entry)
        filename = item.get("filename", "")
        if not filename or Path(filename).name != filename or "\\" in filename:
            raise ValueError("Music filename must stay inside the catalog directory")
        if item.get("license") not in {"CC0-1.0", "CC-BY-4.0"} or item.get("instrumental") is not True:
            raise ValueError("Music requires a supported free license and an instrumental declaration")
        if any(not isinstance(item.get(key), str) or not item[key].strip()
               for key in ("id", "title", "artist", "source_url", "license_url", "attribution")):
            raise ValueError("Music provenance or attribution is missing")
        media = resolve_uri(path.parent, filename)
        if not media.is_file() or digest(media) != "sha256:" + item.get("sha256", ""):
            raise ValueError("Music file is missing or its SHA256 has changed: " + filename)
        if not isinstance(item.get("tags"), list) or any(not isinstance(tag, str) for tag in item["tags"]):
            raise ValueError("Music tags must be strings")
        item["path"] = str(media)
        tracks.append(item)
    if not tracks or len({item["id"] for item in tracks}) != len(tracks):
        raise ValueError("Music catalog must contain unique playable tracks")
    return tracks


def catalog_status():
    try:
        tracks = load_catalog()
        return {"available": True, "track_count": len(tracks), "attribution_required": True}
    except (OSError, ValueError, KeyError, TypeError):
        return {"available": False, "track_count": 0, "attribution_required": True}


def select_music(query, tracks):
    query = str(query).casefold()
    ranked = []
    for index, track in enumerate(tracks):
        matches = [tag for tag in track["tags"] if (re.search(r"\b" + re.escape(tag.casefold()) + r"\b", query)
                   if tag.isascii() else tag.casefold() in query)]
        ranked.append((len(matches), -index, track, matches))
    _, _, track, matches = max(ranked, key=lambda row: row[:2])
    return track, {"method": "local_catalog_tag_match", "matched_tags": matches,
                   "fallback_to_catalog_default": not bool(matches)}


def gain_at(time_us, duration_us, speech, *, start_us=0, base_gain=.25,
            speech_gain=.06, rise_us=1_500_000, fall_us=3_000_000):
    """A deterministic envelope derived from the actual narration intervals."""
    if time_us < start_us or time_us >= duration_us:
        return 0.0
    gain = base_gain
    for begin, end in speech:
        if begin - 180_000 <= time_us < begin:
            level = base_gain + (speech_gain - base_gain) * (time_us - begin + 180_000) / 180_000
        elif begin <= time_us <= end:
            level = speech_gain
        elif end < time_us < end + 400_000:
            level = speech_gain + (base_gain - speech_gain) * (time_us - end) / 400_000
        else:
            continue
        gain = min(gain, level)
    return gain * min(1.0, (time_us - start_us) / max(1, rise_us),
                      (duration_us - time_us) / max(1, fall_us))


def validate_music(timeline, root, *, packaged_path=None):
    policy = timeline.get("render_policy", {}).get("background_music")
    if policy is None:
        return None
    if (not isinstance(policy, dict) or policy.get("version") != 1
            or policy.get("duration_us") != timeline["duration_us"]
            or policy.get("sample_rate") != timeline["output"]["sample_rate"]
            or policy.get("license") not in {"CC0-1.0", "CC-BY-4.0"}
            or any(not isinstance(policy.get(key), str) or not policy[key].strip()
                   for key in ("asset_id", "uri", "content_hash", "title", "artist", "attribution", "source_url", "license_url"))):
        raise ValueError("Invalid prepared background music metadata")
    original = resolve_uri(Path(root), policy["uri"])
    path = Path(packaged_path) if packaged_path is not None else original
    if digest(path) != policy["content_hash"]:
        raise ValueError("Prepared background music SHA256 mismatch")
    with wave.open(str(path), "rb") as wav:
        frames = round(Fraction(timeline["duration_us"] * timeline["output"]["sample_rate"], 1_000_000))
        if ((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (2, 2, policy["sample_rate"])
                or wav.getnframes() != frames or len(wav.readframes(frames)) != frames * 4):
            raise ValueError("Prepared music must be complete stereo PCM16 with the exact timeline duration")
    return path


def attach_music(timeline, project_root, query, *, catalog=None, start_us=0):
    """Return a copied timeline; keep D's timing and the four source tracks intact."""
    timeline = deepcopy(timeline)
    root = Path(project_root).resolve()
    track, selection = select_music(query, load_catalog(catalog))
    duration, rate = timeline["duration_us"], timeline["output"]["sample_rate"]
    if type(start_us) is not int or not 0 <= start_us < duration:
        raise ValueError("Music start must lie inside the timeline")
    speech = [(clip["timeline_range_us"]["begin"], clip["timeline_range_us"]["end"])
              for item in timeline["tracks"] if item["kind"] == "narration" for clip in item["clips"]]
    recipe = {"version": 1, "track_sha256": track["sha256"], "duration_us": duration,
              "sample_rate": rate, "speech_intervals_us": speech, "start_us": start_us,
              "base_gain": .25, "speech_gain": .06, "rise_us": 1_500_000, "fall_us": 3_000_000,
              "loudness_target_lufs": -18, "selection": selection}
    key = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
    uri = f"audio/music/{key}.wav"
    output = resolve_uri(root, uri)
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = output.with_suffix(".json")
    if output.exists():
        if not metadata.is_file() or json.loads(metadata.read_text("utf-8"))["content_hash"] != digest(output):
            raise ValueError("Prepared music cache is incomplete or corrupt")
    else:
        frames = round(Fraction(duration * rate, 1_000_000))
        with tempfile.TemporaryDirectory(prefix=".music-", dir=output.parent) as directory:
            raw = Path(directory) / "normalized.wav"
            subprocess.run([core._ffmpeg_binary(), "-v", "error", "-nostdin", "-stream_loop", "-1",
                "-i", track["path"], "-t", f"{duration / 1_000_000:.6f}", "-af",
                f"loudnorm=I=-18:TP=-2:LRA=10,aresample={rate},atrim=end_sample={frames},apad=whole_len={frames}",
                "-ar", str(rate), "-ac", "2", "-c:a", "pcm_s16le", str(raw)],
                check=True, capture_output=True, timeout=180)
            with wave.open(str(raw), "rb") as wav:
                samples = array.array("h", wav.readframes(wav.getnframes()))
            if digest(Path(track["path"])) != "sha256:" + track["sha256"]:
                raise ValueError("Music source changed during normalization")
            if sys.byteorder != "little":
                samples.byteswap()
            if len(samples) != frames * 2:
                raise ValueError("Music normalization returned an unexpected sample count")
            block = max(1, rate // 100)
            for begin in range(0, frames, block):
                end = min(begin + block, frames)
                left = gain_at(begin * 1_000_000 / rate, duration, speech, start_us=start_us)
                right = gain_at(end * 1_000_000 / rate, duration, speech, start_us=start_us)
                for index in range(begin, end):
                    gain = left + (right - left) * (index - begin) / (end - begin)
                    for channel in (0, 1):
                        offset = index * 2 + channel
                        samples[offset] = max(-32768, min(32767, round(samples[offset] * gain)))
            if sys.byteorder != "little":
                samples.byteswap()
            with output.open("xb") as stream, wave.open(stream, "wb") as wav:
                wav.setparams((2, 2, rate, 0, "NONE", "not compressed"))
                wav.writeframes(samples.tobytes())
        write_json(metadata, {"content_hash": digest(output), "recipe": recipe})
    policy = {"version": 1, "asset_id": "music-" + key[:32], "uri": uri,
              "content_hash": digest(output), "duration_us": duration, "sample_rate": rate,
              **{name: track[name] for name in ("title", "artist", "license", "license_url", "source_url", "attribution")}}
    timeline.setdefault("render_policy", {})["background_music"] = policy
    validate_music(timeline, root)
    write_json(root / "music-selection.json", {"track": policy, "selection": selection, "mix_recipe": recipe,
                                               "envelope_baked_into_separate_pcm": True})
    return timeline

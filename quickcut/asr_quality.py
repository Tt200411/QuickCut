"""Conservative transcript review flags; execution success is not accuracy approval."""
from __future__ import annotations

import math


def source_diagnostics(raw: dict, duration_us: int) -> dict:
    if not raw:
        return {}
    result = {key: raw.get(key) for key in ("language", "language_probability")}
    result["warnings"] = list(raw.get("warnings", []))
    for key in ("duration", "duration_after_vad"):
        value = raw.get(key + "_seconds")
        result[key + "_us"] = round(value * 1e6) if _number(value) else None
    result["speech_ranges_us"] = []
    for item in raw.get("speech_ranges_seconds", []):
        interval = _interval(item.get("begin"), item.get("end"), duration_us)
        if interval:
            result["speech_ranges_us"].append(interval)
    result["segments"] = []
    for item in raw.get("segments", []):
        normalized = {key: item.get(key) for key in ("segment_id", "avg_logprob", "no_speech_prob", "compression_ratio")}
        normalized["words"] = []
        for word in item.get("words", []):
            interval = _interval(word.get("start"), word.get("end"), duration_us)
            if interval:
                normalized["words"].append({"text": word["text"], "source_range_us": interval,
                                            "probability": word.get("probability")})
            else:
                result["warnings"].append({"code": "INVALID_WORD_TIMESTAMPS", "segment_id": item["segment_id"]})
        result["segments"].append(normalized)
    return result


def _interval(begin, end, maximum):
    if not _number(begin) or not _number(end):
        return None
    begin, end = max(0, round(begin * 1e6)), min(maximum, round(end * 1e6))
    return {"begin": begin, "end": end} if end > begin else None


def transcript_review(status: str, segments: list[dict], diagnostics: dict, duration_us: int) -> dict:
    policy = {"version": 1, "calibrated": False, "min_avg_logprob": -1.0,
              "max_no_speech_prob": 0.6, "max_compression_ratio": 2.4, "tail_tolerance_us": 500000}
    if status in {"disabled", "no_audio"}:
        return {"status": "not_applicable", "reasons": [], "policy": policy}
    if status == "failed":
        return {"status": "unavailable", "reasons": ["ASR_FAILED"], "policy": policy}
    reasons = ["UNVERIFIED_TRANSCRIPT"]
    if not diagnostics:
        reasons.append("DIAGNOSTICS_UNAVAILABLE")
    flags = []
    checks = (("avg_logprob", lambda x: x < policy["min_avg_logprob"], "LOW_AVG_LOGPROB"),
              ("no_speech_prob", lambda x: x > policy["max_no_speech_prob"], "HIGH_NO_SPEECH_PROB"),
              ("compression_ratio", lambda x: x > policy["max_compression_ratio"], "HIGH_COMPRESSION_RATIO"))
    for segment in diagnostics.get("segments", []):
        found = [code for name, test, code in checks if _number(segment.get(name)) and test(segment[name])]
        if found:
            flags.append({"segment_id": segment["segment_id"], "reasons": found})
    if flags:
        reasons.append("SUSPICIOUS_SEGMENTS")
    last_text = max((s["source_range_us"]["end"] for s in segments), default=0)
    last_speech = max((s["end"] for s in diagnostics.get("speech_ranges_us", [])
                       if _number(s.get("end"))), default=0)
    last_speech = min(duration_us, last_speech)
    if last_speech - last_text > policy["tail_tolerance_us"]:
        reasons.append("SPEECH_TAIL_REVIEW_RECOMMENDED")
    if diagnostics.get("warnings"):
        reasons.append("INCOMPLETE_DIAGNOSTICS")
    return {"status": "needs_review", "reasons": reasons, "flagged_segments": flags,
            "last_transcript_end_us": last_text, "last_vad_speech_end_us": last_speech, "policy": policy}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

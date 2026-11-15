"""Compare transcripts with explicitly verified references using character error rate."""
from __future__ import annotations

import argparse
import json
import unicodedata
from importlib.metadata import version
from pathlib import Path


def normalize(text: str, simplify_chinese: bool = False) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    if simplify_chinese:
        try:
            from opencc import OpenCC
        except ImportError as exc:
            raise RuntimeError("Chinese script normalization requires pip install -e '.[asr-eval]'") from exc
        text = OpenCC("t2s").convert(text)
    return "".join(char for char in text if unicodedata.category(char)[0] in {"L", "N"})


def character_errors(reference: str, hypothesis: str) -> dict:
    if not reference:
        raise ValueError("Reference must contain letters or numbers after normalization")
    # Each cell stores distance, substitutions, deletions and insertions.
    previous = [(i, 0, 0, i) for i in range(len(hypothesis) + 1)]
    for row, expected in enumerate(reference, 1):
        current = [(row, 0, row, 0)]
        for column, actual in enumerate(hypothesis, 1):
            diagonal = previous[column - 1]
            substitution = (diagonal[0] + (expected != actual), diagonal[1] + (expected != actual), diagonal[2], diagonal[3])
            deletion = (previous[column][0] + 1, previous[column][1], previous[column][2] + 1, previous[column][3])
            insertion = (current[-1][0] + 1, current[-1][1], current[-1][2], current[-1][3] + 1)
            current.append(min((substitution, deletion, insertion), key=lambda value: value[0]))
        previous = current
    distance, substitutions, deletions, insertions = previous[-1]
    return {"reference_characters": len(reference), "hypothesis_characters": len(hypothesis),
            "substitutions": substitutions, "deletions": deletions, "insertions": insertions,
            "edit_distance": distance, "cer": distance / len(reference)}


def select_text(transcript: dict, interval: dict | None = None) -> tuple[str, str]:
    segments = transcript["segments"]
    words = [word for segment in transcript.get("diagnostics", {}).get("segments", [])
             for word in segment.get("words", [])]
    # Partial word metadata must not silently omit a recognized segment.
    annotated_ids = {s["segment_id"] for s in transcript.get("diagnostics", {}).get("segments", []) if s.get("words")}
    use_words = bool(words) and len(annotated_ids) == len(segments)
    if interval is None:
        return " ".join(s["text"] for s in segments), "all_segments"
    begin, end = interval["begin"], interval["end"]
    if type(begin) is not int or type(end) is not int or not 0 <= begin < end:
        raise ValueError("Reference range must be an integer microsecond interval")
    records = words if use_words else segments
    selected = [s["text"] for s in records
                if begin * 2 <= s["source_range_us"]["begin"] + s["source_range_us"]["end"] < end * 2]
    return " ".join(selected), "word_midpoint" if use_words else "segment_midpoint"


def evaluate(reference: dict, transcript: dict, *, simplify_chinese=False, allow_unverified=False) -> dict:
    verified = (reference.get("verification", {}).get("status") == "human_verified"
                and bool(reference["verification"].get("reviewer")))
    if not verified and not allow_unverified:
        raise ValueError("Reference is not human-verified; use --allow-unverified only for a labeled reference comparison")
    expected_hash = reference.get("source_content_hash")
    if expected_hash and transcript.get("source_content_hash") != expected_hash:
        raise ValueError("Reference and transcript source hashes differ or are missing")
    if transcript.get("status") != "success":
        raise ValueError("A successful ASR transcript is required for evaluation")
    hypothesis, selection = select_text(transcript, reference.get("source_range_us"))
    expected = normalize(reference["text"], simplify_chinese)
    actual = normalize(hypothesis, simplify_chinese)
    return {"kind": "ASREvaluation", "reference_id": reference["reference_id"],
            "reference_verification": reference.get("verification"),
            "comparison_kind": "human_reference" if verified else "unverified_reference",
            "source_content_hash": expected_hash, "source_range_us": reference.get("source_range_us"),
            "selection_method": selection, "hypothesis": hypothesis,
            "normalization": {"unicode": "NFKC", "casefold": True, "letters_and_numbers_only": True,
                              "chinese_script": "t2s" if simplify_chinese else "unchanged",
                              "opencc_version": version("opencc-python-reimplemented") if simplify_chinese else None},
            "metrics": character_errors(expected, actual),
            "scope": "Only the referenced interval; not an overall model accuracy estimate"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference")
    parser.add_argument("transcript")
    parser.add_argument("--output", required=True)
    parser.add_argument("--simplify-chinese", action="store_true")
    parser.add_argument("--allow-unverified-reference", action="store_true")
    args = parser.parse_args(argv)
    try:
        reference = json.loads(Path(args.reference).read_text(encoding="utf-8"))
        transcript = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
        result = evaluate(reference, transcript, simplify_chinese=args.simplify_chinese,
                          allow_unverified=args.allow_unverified_reference)
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result["metrics"]))
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

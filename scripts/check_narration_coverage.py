"""Check a known closing phrase with local Whisper word timestamps.

This measures the end of the actual closing phrase, not the end of a possible
hallucinated credit line. Preserve the raw hypothesis so that distinction is
auditable. Example: --closing-phrase '终于笑了' --model small.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quickcut.asr import FasterWhisperASR


def closing_phrase_end(words: list[dict], phrase: str) -> float | None:
    chars, ends = [], []
    for word in words:
        normalized = re.sub(r"[^\u3400-\u9fffA-Za-z0-9]", "", word["word"])
        chars.extend(normalized)
        ends.extend([float(word["end"])] * len(normalized))
    phrase = re.sub(r"[^\u3400-\u9fffA-Za-z0-9]", "", phrase)
    if not phrase:
        raise ValueError("Closing phrase cannot be empty")
    position = "".join(chars).rfind(phrase)
    return ends[position + len(phrase) - 1] if position >= 0 else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("media")
    p.add_argument("--closing-phrase", required=True)
    p.add_argument("--model", default="small")
    p.add_argument("--report", required=True)
    p.add_argument("--asr-json", help="Reuse an actual saved word-timestamp inference, not a visual index")
    a = p.parse_args()
    if a.asr_json:
        data = json.loads(Path(a.asr_json).read_text())
    else:
        model = FasterWhisperASR(model=a.model, language="zh", beam_size=5)._load_model()
        segments, info = model.transcribe(a.media, language="zh", beam_size=5,
            word_timestamps=True, vad_filter=False, condition_on_previous_text=False,
            initial_prompt="大白兔，飞鼠，尖桩，树枝，风筝。")
        result = [asdict(s) for s in segments]
        data = {"file": str(Path(a.media).resolve()), "model": a.model, "duration": info.duration,
                "segments": result, "words": [w for s in result for w in s.get("words", [])]}
    end = closing_phrase_end(data["words"], a.closing_phrase)
    data["closing_phrase"] = a.closing_phrase
    data["verified_narration_end"] = end
    data["tail_gap"] = float(data["duration"]) - end if end is not None else None
    data["coverage_passed"] = end is not None and -0.05 <= data["tail_gap"] <= 1.5
    Path(a.report).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: data[k] for k in ["verified_narration_end", "tail_gap", "coverage_passed"]}))
    return 0 if data["coverage_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

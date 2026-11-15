from types import SimpleNamespace

import pytest

import quickcut.asr as asr
from quickcut.asr_quality import source_diagnostics, transcript_review
from quickcut.asr_eval import character_errors, evaluate, normalize, select_text


def test_backend_passes_options_and_retains_diagnostics(monkeypatch):
    captured = {}
    class Model:
        def transcribe(self, path, **options):
            captured.update(options)
            segment = SimpleNamespace(text="A word", start=0.1, end=0.9, avg_logprob=-1.3,
                no_speech_prob=0.1, compression_ratio=1.2,
                words=[SimpleNamespace(word="A word", start=0.1, end=0.9, probability=0.3)])
            return iter([segment]), SimpleNamespace(language="zh", language_probability=0.98, duration=1.0, duration_after_vad=0.8)
    backend = object.__new__(asr.FasterWhisperASR)
    backend.beam_size, backend.language = 5, "zh"
    backend.vad_filter, backend.condition_on_previous_text = False, False
    backend.initial_prompt, backend.hotwords = "Language hint", "Place name"
    backend.word_timestamps, backend.collect_diagnostics = True, True
    backend._load_model = lambda: Model()
    monkeypatch.setattr(asr, "_speech_ranges", lambda path: [{"begin": 0.1, "end": 1.0}])
    transcript = backend.transcribe("test.wav")
    assert transcript[0].text == "A word"
    assert captured == {"beam_size": 5, "vad_filter": False, "condition_on_previous_text": False,
                        "language": "zh", "initial_prompt": "Language hint", "hotwords": "Place name", "word_timestamps": True}
    assert backend.last_diagnostics["language_probability"] == 0.98
    assert backend.last_diagnostics["segments"][0]["avg_logprob"] == -1.3
    normalized = source_diagnostics(backend.last_diagnostics, 1000000)
    word = normalized["segments"][0]["words"][0]
    assert word["source_range_us"] == {"begin": 100000, "end": 900000}


def test_review_preserves_uncertainty_and_does_not_invent_tail_errors():
    segments = [{"source_range_us": {"begin": 0, "end": 1000000}, "text": "test"}]
    raw = {"segments": [{"segment_id": "s0", "avg_logprob": -1.2, "no_speech_prob": 0.05, "compression_ratio": 1}],
           "speech_ranges_us": [{"begin": 0, "end": 1200000}]}
    review = transcript_review("success", segments, raw, 10000000)
    assert review["status"] == "needs_review"
    assert review["flagged_segments"][0]["reasons"] == ["LOW_AVG_LOGPROB"]
    assert "SPEECH_TAIL_REVIEW_RECOMMENDED" not in review["reasons"]
    raw["speech_ranges_us"][0]["end"] = 3000000
    assert "SPEECH_TAIL_REVIEW_RECOMMENDED" in transcript_review("success", segments, raw, 10000000)["reasons"]
    assert transcript_review("success", segments, {}, 10000000)["status"] == "needs_review"
    assert transcript_review("no_audio", [], {}, 10000000)["status"] == "not_applicable"
    assert transcript_review("failed", [], {}, 10000000)["status"] == "unavailable"


def test_vad_diagnostic_failure_does_not_discard_transcript(monkeypatch):
    class Model:
        def transcribe(self, *args, **kwargs):
            return [SimpleNamespace(text="text", start=0, end=1)], SimpleNamespace()
    backend = object.__new__(asr.FasterWhisperASR)
    backend.beam_size, backend.language, backend.collect_diagnostics = 1, None, True
    backend._load_model = lambda: Model()
    def fail(path):
        raise RuntimeError("diagnostic failure")
    monkeypatch.setattr(asr, "_speech_ranges", fail)
    assert backend.transcribe("test.wav")[0].text == "text"
    assert backend.last_diagnostics["warnings"][0]["code"] == "VAD_DIAGNOSTICS_UNAVAILABLE"
    assert asr._finite(float("nan")) is None


def test_word_metadata_is_clamped_and_bad_intervals_flagged():
    raw = {"segments": [{"segment_id": "s", "words": [
        {"text": "word", "start": -0.1, "end": 0.8, "probability": 0.9},
        {"text": "bad", "start": float("nan"), "end": 0.9, "probability": 0.1}]}]}
    result = source_diagnostics(raw, 500000)
    assert result["segments"][0]["words"][0]["source_range_us"] == {"begin": 0, "end": 500000}
    assert result["warnings"][0]["code"] == "INVALID_WORD_TIMESTAMPS"


def test_cer_counts_edits_and_can_exceed_one():
    assert character_errors("abc", "axc")["substitutions"] == 1
    assert character_errors("abc", "ac")["deletions"] == 1
    assert character_errors("abc", "abxc")["insertions"] == 1
    assert character_errors("a", "aaaa")["cer"] == 3
    assert normalize("Ａ, B! 12") == "ab12"
    with pytest.raises(ValueError):
        character_errors("", "text")


def test_evaluation_requires_verification_and_matching_source():
    reference = {"reference_id": "r", "text": "a", "source_content_hash": "sha256:test"}
    transcript = {"status": "success", "source_content_hash": "sha256:test",
                  "segments": [{"text": "a", "source_range_us": {"begin": 0, "end": 1000000}}]}
    with pytest.raises(ValueError, match="human-verified"):
        evaluate(reference, transcript)
    assert evaluate(reference, transcript, allow_unverified=True)["comparison_kind"] == "unverified_reference"
    reference["verification"] = {"status": "human_verified", "reviewer": "test reviewer"}
    assert evaluate(reference, transcript)["metrics"]["cer"] == 0
    transcript["source_content_hash"] = "sha256:other"
    with pytest.raises(ValueError, match="hashes"):
        evaluate(reference, transcript)


def test_partial_word_metadata_falls_back_without_dropping_text():
    transcript = {"segments": [{"text": "first", "source_range_us": {"begin": 0, "end": 1000000}},
                               {"text": "last", "source_range_us": {"begin": 1000000, "end": 2000000}}],
        "diagnostics": {"segments": [{"segment_id": "s0", "words": [
            {"text": "first", "source_range_us": {"begin": 0, "end": 1000000}}]}]}}
    text, method = select_text(transcript, {"begin": 0, "end": 2000000})
    assert text == "first last" and method == "segment_midpoint"

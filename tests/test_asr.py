from __future__ import annotations

from pathlib import Path

from quickcut.asr import (
    assign_transcripts,
    load_transcript_sidecar,
    make_asr,
    save_transcript_sidecar,
)
from quickcut.core import _transcript_score, localize
from quickcut.types import IndexManifest, Shot, TranscriptSegment


def test_transcript_sidecar_round_trip_and_window_assignment(tmp_path: Path):
    shots = [
        Shot(shot_id="shot-0000", start=0.0, end=3.0),
        Shot(shot_id="shot-0001", start=3.0, end=6.0),
    ]
    segments = [
        TranscriptSegment(segment_id="segment-0000", start=0.5, end=2.5, text="blue ocean waves"),
        TranscriptSegment(segment_id="segment-0001", start=3.2, end=4.0, text="walking through a forest"),
    ]
    assign_transcripts(shots, segments)
    assert shots[0].transcript == "blue ocean waves"
    assert shots[1].transcript == "walking through a forest"

    path = save_transcript_sidecar(
        tmp_path / "index.transcript.json", segments, index_id="index", source_video="source.mp4",
        source_size=10, source_mtime=1.0, backend="test", model="tiny", language="en",
    )
    payload, loaded = load_transcript_sidecar(path)
    assert payload["schema_version"] == "1.0"
    assert payload["backend"] == "test"
    assert loaded == segments


def test_transcript_evidence_ranks_matching_shot():
    manifest = IndexManifest(
        index_id="abc", source_video="source.mp4", source_size=1, source_mtime=1.0,
        duration=6.0, window=3.0, stride=3.0, created_at="now", encoder="fallback",
        shots=[
            Shot(shot_id="shot-0000", start=0.0, end=3.0, transcript="a person walks across a bridge"),
            Shot(shot_id="shot-0001", start=3.0, end=6.0, transcript="a dog runs through the garden"),
        ],
    )
    timeline = localize(manifest, "dog runs 8 seconds", encoder_mode="fallback")
    assert timeline.coverage == "source"
    assert timeline.items
    matching = max(timeline.items, key=lambda item: item.score)
    assert matching.asset_id == "shot-0001"
    assert matching.transcript == "a dog runs through the garden"


def test_transcript_score_is_token_overlap():
    assert _transcript_score("dog runs", "a dog runs through the garden") == 1.0
    assert _transcript_score("dog runs", "a person walks") == 0.0
    assert make_asr("none").name == "none"

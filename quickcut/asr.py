"""Local automatic speech recognition backends and transcript sidecars.

The ASR dependency is intentionally optional.  A normal QuickCut install can
build a visual index without downloading model weights; selecting ``auto`` or
``faster-whisper`` enables local transcription when the extra is installed.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .types import TranscriptSegment


class NullASR:
    """No-op backend used when ASR is disabled or unavailable."""

    name = "none"
    model = None
    available = False

    def transcribe(self, video_path: str | Path) -> list[TranscriptSegment]:
        return []


class FasterWhisperASR:
    """Lazy local faster-whisper backend.

    Model construction is deferred until the first call to ``transcribe`` so
    importing QuickCut and using visual-only indexing never downloads weights.
    """

    name = "faster-whisper"
    available = True

    def __init__(
        self,
        *,
        model: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
        language: str | None = None,
        beam_size: int | None = None,
    ) -> None:
        try:
            import faster_whisper  # type: ignore
        except ImportError as exc:
            raise RuntimeError("faster-whisper backend requires: pip install -e '.[asr]'") from exc
        self._WhisperModel = faster_whisper.WhisperModel
        self.model = model or os.getenv("QUICKCUT_ASR_MODEL", "tiny")
        self.device = device or os.getenv("QUICKCUT_ASR_DEVICE", "cpu")
        self.compute_type = compute_type or os.getenv("QUICKCUT_ASR_COMPUTE_TYPE", "int8")
        self.language = language if language is not None else os.getenv("QUICKCUT_ASR_LANGUAGE")
        self.beam_size = beam_size or int(os.getenv("QUICKCUT_ASR_BEAM_SIZE", "1"))
        self._model = None

    def _load_model(self):
        if self._model is None:
            # faster-whisper versions released before PyAV 19 pass the removed
            # ``metadata_errors`` keyword. Keep the local tiny model usable on
            # current PyAV without changing the public faster-whisper API.
            try:
                import av  # type: ignore

                if not getattr(av.open, "_quickcut_compat", False):
                    original_open = av.open

                    def compat_open(*args, **kwargs):
                        kwargs.pop("metadata_errors", None)
                        return original_open(*args, **kwargs)

                    compat_open._quickcut_compat = True  # type: ignore[attr-defined]
                    av.open = compat_open
            except Exception:
                pass
            self._model = self._WhisperModel(self.model, device=self.device, compute_type=self.compute_type)
        return self._model

    def transcribe(self, video_path: str | Path) -> list[TranscriptSegment]:
        kwargs: dict[str, Any] = {
            "beam_size": self.beam_size,
            "vad_filter": True,
        }
        if self.language:
            kwargs["language"] = self.language
        try:
            segments, _info = self._load_model().transcribe(str(video_path), **kwargs)
        except IndexError:
            # A perfectly valid video-only source has no audio stream. Treat it
            # as an empty transcript so visual indexing still succeeds.
            return []
        result: list[TranscriptSegment] = []
        for index, segment in enumerate(segments):
            text = str(getattr(segment, "text", "") or "").strip()
            start = float(getattr(segment, "start", 0.0) or 0.0)
            end = float(getattr(segment, "end", start) or start)
            if not text or end <= start:
                continue
            result.append(TranscriptSegment(segment_id=f"segment-{index:04d}", start=start, end=end, text=text))
        return result


def make_asr(mode: str | None = None):
    """Create an ASR backend.

    ``none`` is the default to keep the base install deterministic.  ``auto``
    selects faster-whisper when importable and otherwise returns a no-op.
    Explicit faster-whisper requests raise a useful dependency error.
    """

    requested = (mode or os.getenv("QUICKCUT_ASR", "none")).strip().lower()
    if requested in {"", "none", "off", "disabled", "false", "0"}:
        return NullASR()
    if requested in {"auto", "faster-whisper", "faster_whisper", "whisper"}:
        try:
            return FasterWhisperASR()
        except Exception:
            if requested == "auto":
                return NullASR()
            raise
    raise ValueError(f"Unknown ASR backend: {mode}")


def save_transcript_sidecar(
    path: str | Path,
    segments: Iterable[TranscriptSegment],
    *,
    index_id: str,
    source_video: str,
    source_size: int,
    source_mtime: float,
    backend: str,
    model: str | None = None,
    language: str | None = None,
) -> Path:
    """Write a stable, inspectable transcript cache next to an index."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "index_id": index_id,
        "source_video": source_video,
        "source_size": source_size,
        "source_mtime": source_mtime,
        "backend": backend,
        "model": model,
        "language": language,
        "segments": [asdict(segment) for segment in segments],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_transcript_sidecar(path: str | Path) -> tuple[dict[str, Any], list[TranscriptSegment]]:
    """Read a transcript sidecar and normalize its segment records."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0":
        raise ValueError(f"Unsupported transcript sidecar schema: {payload.get('schema_version')!r}")
    segments = [TranscriptSegment(**segment) for segment in payload.get("segments", [])]
    return payload, segments


def assign_transcripts(shots: Iterable[Any], segments: Iterable[TranscriptSegment]) -> None:
    """Attach overlapping transcript text to each fixed-window shot in place."""

    segments = list(segments)
    for shot in shots:
        overlapping = [
            segment
            for segment in segments
            if segment.end > float(shot.start) and segment.start < float(shot.end)
        ]
        overlapping.sort(key=lambda segment: (segment.start, segment.end))
        shot.transcript = " ".join(segment.text.strip() for segment in overlapping if segment.text.strip())

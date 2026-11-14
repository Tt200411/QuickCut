"""Module B — Retrieval & external B-roll library.

Consumes an ``AssetManifest`` (from A) plus a ``RetrievalRequest`` (from C) and
returns a ``CandidateSet`` that preserves origin, provenance and empty-result
reasons.  This implementation deliberately runs without downloading media: when
real keyframe files are available the configured visual encoder is used, and
when they are only fixture paths the segment ``description`` text is used as a
stand-in.  Both branches share the same scoring envelope and ``score_profile``.

Design notes
------------
- The module is pure-function at the top level: ``retrieve(manifest, request)``
  takes dicts and returns a dict.  CLI/UI callers can read/write JSON around it.
- External B-roll is unified with user media by the AssetManifest format; the
  library is still visible via ``origin: "external"`` on both assets and
  candidates so C can tell them apart.
- Empty queries are a legal result.  Each query's ``status`` ∈
  {``ok``, ``empty``, ``error``} with ``reason`` filled when non-ok.
- Candidate IDs are stable and deterministic: ``candidate-{query_id}-{segment_id}``.
  Re-running the retrieval on the same inputs yields the same IDs so C can
  compare revisions.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .encoder import make_encoder


SCHEMA_VERSION = "0.1.0"

# Role → allowed origins when the request itself does not pin ``allowed_origins``.
# ``primary`` events must come from user media (external B-roll cannot stand in
# for things the user actually did); ``atmosphere``/``transition``/``establishing``
# may use either.
_ROLE_ORIGIN_DEFAULTS: dict[str, tuple[str, ...]] = {
    "primary": ("user",),
    "primary_or_broll": ("user", "external"),
    "establishing": ("user", "external"),
    "transition": ("user", "external"),
    "atmosphere": ("user", "external"),
    "detail": ("user", "external"),
    "broll": ("external",),
}

# Closed label set for external B-roll. The chosen label is a zero-shot
# prediction, not a human annotation, and the method is stored with the score.
TAG_LABELS = {
    "scene": ("小路", "树林", "田野", "夕阳风景", "室内"),
    "emotion": ("平静", "温暖", "紧张"),
    "light": ("日光", "暖色光线", "阴天"),
    "motion": ("人物步行", "航拍", "固定镜头"),
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)
_CJK_RE = re.compile(r"[一-鿿]", re.UNICODE)


# ----------------------------- data structures -----------------------------


@dataclass
class RetrievalConfig:
    """Tunable knobs for the retriever.

    ``threshold`` is not a hard filter.  Below-threshold queries still return
    candidates but are flagged ``low_confidence`` so C can decide whether to
    re-query, change brief phrasing or widen allowed origins.
    """

    encoder_mode: str | None = None
    transcript_weight: float = 0.35
    description_weight: float = 0.65
    low_confidence_threshold: float = 0.22
    # Scores below this are treated as no match. 0 keeps the old rule of only
    # dropping a ranking of zero. OpenCLIP cosine on a miss is a small positive
    # number, so a real visual run sets a floor above that noise.
    minimum_ranking_score: float = 0.0
    dedup_by_content_hash: bool = True


# -------------------------------- helpers ---------------------------------


def _tokens(text: str) -> set[str]:
    """Return matchable tokens.

    ASCII words are kept whole; CJK is split by character.  Chinese retrieval
    on the fixture bundle only needs 1-gram overlap (there is no segmenter
    installed), and whole-phrase matching missed obvious hits like 林 overlap
    between a '林间步行' query and a '林间光线' description.
    """
    text = text or ""
    words = {w.lower() for w in _TOKEN_RE.findall(text) if len(w) > 1}
    chars = {c for c in _CJK_RE.findall(text)}
    return words | chars


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a))


def _resolve_allowed_origins(query: dict[str, Any]) -> tuple[str, ...]:
    explicit = query.get("allowed_origins")
    if explicit:
        return tuple(explicit)
    return _ROLE_ORIGIN_DEFAULTS.get(query.get("role", ""), ("user", "external"))


def _usable(segment: dict[str, Any]) -> bool:
    quality = segment.get("quality") or {}
    return quality.get("usable", True)


def _segment_duration_us(segment: dict[str, Any]) -> int:
    rng = segment.get("source_range_us") or {}
    return int(rng.get("end", 0)) - int(rng.get("begin", 0))


def _hard_constraint_failure(segment: dict[str, Any], asset: dict[str, Any], query: dict[str, Any]) -> str | None:
    """Return why a segment fails an explicit hard constraint.

    ``duration_budget_us`` is the beat's planning budget. It is not a clip
    length limit; D trims inside the chosen segment later.
    """
    spec = query.get("aspect_ratio")
    if isinstance(spec, dict) and spec.get("width") and spec.get("height"):
        video = asset.get("video") or {}
        width, height = video.get("width"), video.get("height")
        if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
            return "asset has no pixel dimensions for the requested aspect"
        actual = width / height
        target = spec["width"] / spec["height"]
        if abs(actual - target) / target > 0.03:
            return f"aspect {width}x{height} does not match {spec['width']}:{spec['height']}"
    duration = _segment_duration_us(segment)
    minimum = query.get("min_duration_us")
    maximum = query.get("max_duration_us")
    if isinstance(minimum, int) and duration < minimum:
        return f"duration {duration}us is below min_duration_us {minimum}"
    if isinstance(maximum, int) and duration > maximum:
        return f"duration {duration}us is above max_duration_us {maximum}"
    return None


def _load_keyframe_bytes(uri: str, project_root: Path | None) -> bytes | None:
    """Return raw PPM bytes for a keyframe if a real file is reachable.

    Keyframes in the fixture are JPG paths that don't exist on disk, which is
    expected.  We only attempt to read when the file is actually there so the
    retriever stays runnable without media.
    """
    if not uri:
        return None
    path = Path(uri)
    if not path.is_absolute() and project_root is not None:
        path = project_root / uri
    if not path.exists():
        return None
    # The encoders currently consume raw PPM bytes.  A full visual pipeline
    # would decode JPG/PNG first; the fixture does not require that step and
    # upstream A is expected to pre-extract PPM-compatible frames.
    try:
        return path.read_bytes()
    except OSError:
        return None


# --------------------------------- core -----------------------------------


class Retriever:
    """Score a manifest's segments against a ``RetrievalRequest``.

    The retriever is stateless across requests but caches the encoder so
    repeated calls in the same process do not rebuild weights.  Scoring
    combines three signals, each already in [0, 1]:

      * ``visual``: cosine similarity between the query text embedding and the
        segment keyframe embedding, when a real keyframe is present.
      * ``transcript``: token overlap between the query and the segment ASR.
      * ``description``: token overlap between the query and the human/A-provided
        segment description.  This is what makes the retriever work on the
        fixture bundle where no real media exists.

    ``ranking_score`` is the max of the available signals rather than a linear
    sum: the fixture score distribution is neither calibrated nor Gaussian, so
    averaging a strong visual hit with a zero transcript score would wrongly
    demote it.  A segment whose ranking score is zero shares nothing with the
    query and is omitted; ``top_k`` is never padded with those segments.  C is
    expected to re-rank the remaining candidates with its own criteria.
    """

    def __init__(self, config: RetrievalConfig | None = None) -> None:
        self.config = config or RetrievalConfig()
        self._encoder = None
        self._image_cache: dict[str, list[float]] = {}

    # ---- scoring ----------------------------------------------------------

    def _encoder_lazy(self):
        if self._encoder is None:
            self._encoder = make_encoder(self.config.encoder_mode)
        return self._encoder

    def _image_embedding(self, segment: dict[str, Any], project_root: Path | None) -> list[float] | None:
        for kf in segment.get("keyframes") or []:
            uri = kf.get("uri", "")
            if uri in self._image_cache:
                return self._image_cache[uri]
            ppm = _load_keyframe_bytes(uri, project_root)
            if ppm is None:
                continue
            try:
                embedding = self._encoder_lazy().encode_image(ppm)
            except Exception:
                continue
            self._image_cache[uri] = embedding
            return embedding
        return None

    def _visual_score(self, query_text: str, segment: dict[str, Any],
                      project_root: Path | None) -> float:
        embedding = self._image_embedding(segment, project_root)
        if embedding is None:
            return 0.0
        try:
            return float(max(0.0, self._encoder_lazy().score_text_image(query_text, embedding)))
        except Exception:
            return 0.0

    def _broll_tags(self, segment: dict[str, Any], asset: dict[str, Any],
                    project_root: Path | None) -> dict[str, Any]:
        if asset.get("origin") != "external":
            return {"status": "not_applicable", "reason": "tags are recorded for external b-roll"}
        if self._encoder_lazy().name != "openclip":
            return {"status": "not_predicted", "method": "unavailable",
                    "reason": "fallback encoder has no semantic label head"}
        embedding = self._image_embedding(segment, project_root)
        if embedding is None:
            return {"status": "not_predicted", "method": "openclip-zero-shot",
                    "reason": "no readable keyframe"}
        info = self._encoder_lazy().profile()
        predicted = {}
        for group, labels in TAG_LABELS.items():
            score, label = max(
                (self._encoder_lazy().score_text_image(text, embedding), text) for text in labels
            )
            predicted[group] = {"label": label, "score": round(float(score), 4)}
        return {
            "status": "predicted",
            "method": "openclip-zero-shot",
            "model": info.get("model"),
            "pretrained": info.get("pretrained"),
            **predicted,
        }

    def _text_score(self, query_text: str, segment: dict[str, Any]) -> tuple[float, float]:
        q = _tokens(query_text)
        transcript = _tokens((segment.get("transcript") or {}).get("text", ""))
        description = _tokens((segment.get("description") or {}).get("text", ""))
        return _overlap(q, transcript), _overlap(q, description)

    # ---- orchestration ----------------------------------------------------

    def retrieve(
        self,
        manifest: dict[str, Any],
        request: dict[str, Any],
        *,
        project_root: str | Path | None = None,
        document_id: str = "candidates-001",
        owner: str = "B",
    ) -> dict[str, Any]:
        _validate_contract_envelope(manifest, "AssetManifest")
        _validate_contract_envelope(request, "RetrievalRequest")
        require(
            manifest["project_id"] == request["project_id"],
            "AssetManifest and RetrievalRequest belong to different projects",
        )

        root = Path(project_root) if project_root else None
        assets_by_id = {a["asset_id"]: a for a in manifest.get("assets", [])}
        segments = manifest.get("segments", [])

        encoder = self._encoder_lazy()
        encoder_name = encoder.name
        info = encoder.profile() if hasattr(encoder, "profile") else {"encoder": encoder_name}
        score_profile = (
            f"encoder={info.get('encoder', encoder_name)}; model={info.get('model')}; "
            f"pretrained={info.get('pretrained')}; dimension={info.get('dimension')}; "
            f"visual={info.get('visual_score')}; "
            f"transcript_weight={self.config.transcript_weight}; "
            f"description_weight={self.config.description_weight}; "
            "ranking_score is the max of separate signals, not a calibrated probability"
        )

        results: list[dict[str, Any]] = []
        for query in request.get("queries", []):
            results.append(self._run_query(
                query, segments, assets_by_id, root, score_profile,
            ))

        overall_status = "ok" if any(r["status"] == "ok" for r in results) else "empty"

        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "CandidateSet",
            "document_id": document_id,
            "project_id": manifest["project_id"],
            "revision": 1,
            "fixtures_only": manifest.get("fixtures_only", False) or request.get("fixtures_only", False),
            "owner": owner,
            "inputs": [
                {"kind": "AssetManifest", "document_id": manifest["document_id"], "revision": manifest["revision"]},
                {"kind": "RetrievalRequest", "document_id": request["document_id"], "revision": request["revision"]},
            ],
            "status": overall_status,
            "results": results,
            "retrieval_profile": {
                "score_profile": score_profile,
                "encoder": info,
                "dedup_by_content_hash": self.config.dedup_by_content_hash,
                "duration_budget_us_applied_as_filter": False,
                "tag_labels": TAG_LABELS if encoder_name == "openclip" else None,
                "tag_method": "openclip-zero-shot" if encoder_name == "openclip" else "unavailable",
            },
        }

    def _run_query(
        self,
        query: dict[str, Any],
        segments: list[dict[str, Any]],
        assets_by_id: dict[str, dict[str, Any]],
        project_root: Path | None,
        score_profile: str,
    ) -> dict[str, Any]:
        query_id = query["query_id"]
        semantic_text = query.get("semantic_text", "")
        allowed_origins = _resolve_allowed_origins(query)
        top_k = int(query.get("top_k", 4))
        role = query.get("role", "")

        pool: list[tuple[float, dict[str, Any], dict[str, Any], dict[str, float]]] = []
        seen_hashes: set[str] = set()
        reasons_dropped: list[str] = []
        constraint_rejected = 0
        eligible = 0

        for segment in segments:
            asset = assets_by_id.get(segment.get("asset_id"))
            if asset is None:
                reasons_dropped.append(f"orphan segment {segment.get('segment_id')} has no asset")
                continue
            if asset.get("origin") not in allowed_origins:
                continue
            if not _usable(segment):
                reasons_dropped.append(f"segment {segment['segment_id']} marked unusable")
                continue
            failure = _hard_constraint_failure(segment, asset, query)
            if failure:
                constraint_rejected += 1
                reasons_dropped.append(f"segment {segment['segment_id']} {failure}")
                continue
            if self.config.dedup_by_content_hash:
                content_hash = asset.get("content_hash")
                if content_hash and content_hash in seen_hashes:
                    # Keep the first occurrence; duplicates typically arise
                    # when the same B-roll clip is imported twice.
                    continue
                if content_hash:
                    seen_hashes.add(content_hash)

            eligible += 1
            visual = self._visual_score(semantic_text, segment, project_root)
            transcript_overlap, description_overlap = self._text_score(semantic_text, segment)
            weighted_text = max(
                self.config.transcript_weight * transcript_overlap,
                self.config.description_weight * description_overlap,
            )
            ranking = max(visual, weighted_text)
            # A zero score means the query shares nothing with this segment.
            # Leave the slot empty instead of padding top_k with it. A positive
            # floor does the same for weak OpenCLIP cosine that is not a match.
            floor = self.config.minimum_ranking_score
            if ranking <= 0 or (floor > 0 and ranking < floor):
                continue

            components = {
                "visual": round(visual, 4),
                "transcript": round(transcript_overlap, 4),
                "description": round(description_overlap, 4),
            }
            pool.append((ranking, segment, asset, components))

        pool.sort(key=lambda row: row[0], reverse=True)
        top = pool[:top_k]

        candidates: list[dict[str, Any]] = []
        flags_any_above_threshold = False
        for ranking, segment, asset, components in top:
            suggested_role = _suggest_role(role, asset["origin"])
            flags: list[str] = []
            if ranking < self.config.low_confidence_threshold:
                flags.append("low_confidence")
            else:
                flags_any_above_threshold = True
            candidates.append({
                "candidate_id": f"candidate-{query_id}-{segment['segment_id']}",
                "segment_id": segment["segment_id"],
                "asset_id": asset["asset_id"],
                "asset_revision": asset.get("revision", 1),
                "origin": asset["origin"],
                "visual_score": components["visual"],
                "transcript_score": components["transcript"],
                "description_score": components["description"],
                "ranking_score": round(ranking, 4),
                "score_profile": score_profile,
                "suggested_role": suggested_role,
                "source_range_us": segment.get("source_range_us"),
                "duration_us": _segment_duration_us(segment),
                "evidence": _evidence_tags(segment, components),
                "provenance_note": asset.get("provenance_note", ""),
                "flags": flags,
                "unmet_conditions": [],
                "tags": self._broll_tags(segment, asset, project_root),
            })

        constraints = {
            "aspect_ratio": query.get("aspect_ratio"),
            "min_duration_us": query.get("min_duration_us"),
            "max_duration_us": query.get("max_duration_us"),
            "duration_budget_us_applied_as_filter": False,
            "rejected": constraint_rejected,
        }
        if not candidates:
            if eligible:
                reason = "No segment had a positive match for the query"
            elif constraint_rejected:
                reason = "No segment satisfied aspect or duration constraints"
            else:
                reason = _empty_reason(allowed_origins, reasons_dropped)
            return {
                "query_id": query_id,
                "status": "empty",
                "reason": reason,
                "allowed_origins": list(allowed_origins),
                "constraints": constraints,
                "candidates": [],
            }

        return {
            "query_id": query_id,
            "status": "ok",
            "reason": "" if flags_any_above_threshold else "all candidates below low_confidence_threshold",
            "allowed_origins": list(allowed_origins),
            "constraints": constraints,
            "candidates": candidates,
        }


# ------------------------- envelope + validation helpers ------------------


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate_contract_envelope(doc: dict[str, Any], kind: str) -> None:
    require(doc.get("schema_version") == SCHEMA_VERSION,
            f"{kind}: schema_version must be {SCHEMA_VERSION}")
    require(doc.get("kind") == kind, f"document kind must be {kind}")
    for field_name in ("document_id", "project_id", "revision"):
        require(field_name in doc, f"{kind}: missing {field_name}")


def _suggest_role(requested_role: str, origin: str) -> str:
    # ``primary_or_broll`` is a blank cheque — tell C whether the actual pick
    # ended up being a primary clip (user) or a cover (external).  For the other
    # explicit roles we return them unchanged.
    if requested_role == "primary_or_broll":
        return "primary" if origin == "user" else "broll"
    return requested_role or ("primary" if origin == "user" else "broll")


def _evidence_tags(segment: dict[str, Any], components: dict[str, float]) -> list[str]:
    tags: list[str] = []
    if components["visual"] > 0:
        tags.append("visual_keyframe")
    if components["transcript"] > 0:
        tags.append("transcript_overlap")
    if components["description"] > 0:
        provider = (segment.get("description") or {}).get("provider") or "description"
        tags.append(provider)
    return tags or ["no_signal"]


def _empty_reason(allowed_origins: tuple[str, ...], dropped: list[str]) -> str:
    base = f"No segment matched allowed origins {list(allowed_origins)}"
    if dropped:
        return base + "; " + "; ".join(dropped[:3])
    return base


# ------------------------------ B-roll library ----------------------------


@dataclass
class BRollLibrary:
    """Thin wrapper around an on-disk external-clip library.

    The library is still an ``AssetManifest`` on disk — same schema, different
    origin — so A's indexer and B's retriever stay in one pipeline.  This class
    just makes the common ops (load, merge-into-project) explicit.
    """

    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "BRollLibrary":
        path = Path(path)
        import json
        manifest = json.loads(path.read_text(encoding="utf-8"))
        _validate_contract_envelope(manifest, "AssetManifest")
        for asset in manifest.get("assets", []):
            require(asset.get("origin") == "external",
                    f"B-roll library asset {asset.get('asset_id')} must have origin=external")
        return cls(path=path, manifest=manifest)

    def merge_into(self, project_manifest: dict[str, Any]) -> dict[str, Any]:
        """Return a new manifest that unions the library into the project.

        The project manifest is treated as authoritative on conflicts — a
        user-side ``asset_id`` with the same ID as a library asset wins, and we
        emit a flag the retriever will later drop through dedup.  We never
        mutate the caller's dict.
        """
        import copy as _copy
        merged = _copy.deepcopy(project_manifest)
        existing_ids = {a["asset_id"] for a in merged.get("assets", [])}
        existing_segs = {s["segment_id"] for s in merged.get("segments", [])}
        for asset in self.manifest.get("assets", []):
            if asset["asset_id"] in existing_ids:
                continue
            merged.setdefault("assets", []).append(asset)
        for segment in self.manifest.get("segments", []):
            if segment["segment_id"] in existing_segs:
                continue
            merged.setdefault("segments", []).append(segment)
        return merged


# ------------------------------ public API --------------------------------


def retrieve(
    manifest: dict[str, Any],
    request: dict[str, Any],
    *,
    config: RetrievalConfig | None = None,
    project_root: str | Path | None = None,
    document_id: str = "candidates-001",
) -> dict[str, Any]:
    return Retriever(config).retrieve(
        manifest, request, project_root=project_root, document_id=document_id,
    )

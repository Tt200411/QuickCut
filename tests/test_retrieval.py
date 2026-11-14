"""Tests for Module B (retrieval + external B-roll library)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from quickcut.retrieval import (
    BRollLibrary,
    RetrievalConfig,
    Retriever,
    retrieve,
)


FIXTURE_BUNDLE = Path(__file__).resolve().parents[1] / "examples" / "team_contracts" / "bundle.json"


@pytest.fixture(scope="module")
def bundle() -> dict:
    return json.loads(FIXTURE_BUNDLE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest(bundle) -> dict:
    return next(d for d in bundle["documents"] if d["kind"] == "AssetManifest")


@pytest.fixture(scope="module")
def request_doc(bundle) -> dict:
    return next(d for d in bundle["documents"] if d["kind"] == "RetrievalRequest")


def _result(candidates: dict, query_id: str) -> dict:
    for r in candidates["results"]:
        if r["query_id"] == query_id:
            return r
    raise KeyError(query_id)


# --------------------------------------------------------------------------
# Envelope / contract
# --------------------------------------------------------------------------


def test_retrieve_returns_well_formed_candidate_set(manifest, request_doc):
    out = retrieve(manifest, request_doc)
    assert out["kind"] == "CandidateSet"
    assert out["schema_version"] == "0.1.0"
    assert out["project_id"] == manifest["project_id"]
    inputs_kinds = {inp["kind"] for inp in out["inputs"]}
    assert inputs_kinds == {"AssetManifest", "RetrievalRequest"}
    # One result per query, in request order.
    assert [r["query_id"] for r in out["results"]] == [q["query_id"] for q in request_doc["queries"]]


def test_retrieve_rejects_cross_project(manifest, request_doc):
    bad = copy.deepcopy(request_doc)
    bad["project_id"] = "other-project"
    with pytest.raises(ValueError, match="different projects"):
        retrieve(manifest, bad)


def test_retrieve_rejects_wrong_kind(request_doc):
    not_a_manifest = {
        "schema_version": "0.1.0", "kind": "Brief",
        "document_id": "x", "project_id": "p", "revision": 1,
    }
    with pytest.raises(ValueError, match="AssetManifest"):
        retrieve(not_a_manifest, request_doc)


# --------------------------------------------------------------------------
# Origin filtering + role defaults
# --------------------------------------------------------------------------


def test_user_only_query_excludes_external_broll(manifest, request_doc):
    out = retrieve(manifest, request_doc)
    open_result = _result(out, "query-open")
    origins = {c["origin"] for c in open_result["candidates"]}
    # Request explicitly whitelists only "user".
    assert origins == {"user"}


def test_mixed_origin_query_can_include_external(manifest, request_doc):
    out = retrieve(manifest, request_doc)
    middle = _result(out, "query-middle")
    origins = {c["origin"] for c in middle["candidates"]}
    assert "user" in origins
    assert "external" in origins


def test_role_default_origin_when_not_specified(manifest):
    req = {
        "schema_version": "0.1.0",
        "kind": "RetrievalRequest",
        "document_id": "req-defaults",
        "project_id": manifest["project_id"],
        "revision": 1,
        "queries": [
            {"query_id": "q1", "beat_id": "b1", "semantic_text": "森林", "role": "primary", "top_k": 5},
        ],
    }
    out = retrieve(manifest, req)
    result = _result(out, "q1")
    for c in result["candidates"]:
        assert c["origin"] == "user", "primary role must default to user origin only"


# --------------------------------------------------------------------------
# Empty-result handling
# --------------------------------------------------------------------------


def test_empty_candidate_set_is_a_legal_result(manifest):
    req = {
        "schema_version": "0.1.0",
        "kind": "RetrievalRequest",
        "document_id": "req-empty",
        "project_id": manifest["project_id"],
        "revision": 1,
        "queries": [
            {
                "query_id": "q-empty",
                "beat_id": "b",
                "semantic_text": "任何东西",
                "role": "primary",
                "allowed_origins": ["generated"],  # no asset has this origin
                "top_k": 4,
            }
        ],
    }
    out = retrieve(manifest, req)
    result = _result(out, "q-empty")
    assert result["status"] == "empty"
    assert result["candidates"] == []
    assert "allowed origins" in result["reason"]


def test_overall_status_empty_when_all_queries_empty(manifest):
    req = {
        "schema_version": "0.1.0",
        "kind": "RetrievalRequest",
        "document_id": "req-all-empty",
        "project_id": manifest["project_id"],
        "revision": 1,
        "queries": [
            {"query_id": "q1", "beat_id": "b", "semantic_text": "x",
             "role": "primary", "allowed_origins": ["generated"], "top_k": 1},
        ],
    }
    out = retrieve(manifest, req)
    assert out["status"] == "empty"


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_description_signal_drives_fixture_scoring(manifest, request_doc):
    out = retrieve(manifest, request_doc)
    middle = _result(out, "query-middle")
    # The fixture has no real keyframes, so retrieval must still produce a
    # non-trivial ranking from the description/transcript signals.
    assert middle["candidates"], "retrieval produced no middle-beat candidates"
    top = middle["candidates"][0]
    assert top["ranking_score"] > 0


def test_candidate_includes_evidence_and_profile(manifest, request_doc):
    out = retrieve(manifest, request_doc)
    all_candidates = [c for r in out["results"] for c in r["candidates"]]
    assert all_candidates
    for c in all_candidates:
        assert c["score_profile"]
        assert c["evidence"], f"missing evidence tags on {c['candidate_id']}"
        assert "ranking_score" in c
        assert c["segment_id"]
        assert c["asset_id"]


def test_low_confidence_flag_fires_below_threshold(manifest):
    req = {
        "schema_version": "0.1.0",
        "kind": "RetrievalRequest",
        "document_id": "req-low",
        "project_id": manifest["project_id"],
        "revision": 1,
        "queries": [
            {"query_id": "q-city", "beat_id": "b",
             "semantic_text": "城市",
             "role": "primary", "top_k": 3},
        ],
    }
    out = retrieve(manifest, req, config=RetrievalConfig(low_confidence_threshold=0.9))
    result = _result(out, "q-city")
    assert result["candidates"]
    assert all(c["ranking_score"] > 0 for c in result["candidates"])
    assert all("low_confidence" in c["flags"] for c in result["candidates"])


def test_zero_score_segments_do_not_fill_top_k(manifest, request_doc):
    out = retrieve(manifest, request_doc)
    for result in out["results"]:
        assert all(c["ranking_score"] > 0 for c in result["candidates"])
        assert all("no_signal" not in c["evidence"] for c in result["candidates"])

    open_ids = [c["segment_id"] for c in _result(out, "query-open")["candidates"]]
    assert "seg-packing" in open_ids
    assert "seg-rest" not in open_ids
    assert "seg-walk" not in open_ids


def test_query_with_no_overlap_is_empty(manifest):
    req = {
        "schema_version": "0.1.0",
        "kind": "RetrievalRequest",
        "document_id": "req-noise",
        "project_id": manifest["project_id"],
        "revision": 1,
        "queries": [
            {"query_id": "q-noise", "beat_id": "b",
             "semantic_text": "xylophone unrelated topic nothing",
             "role": "primary_or_broll", "top_k": 4},
        ],
    }
    out = retrieve(manifest, req)
    result = _result(out, "q-noise")
    assert result["status"] == "empty"
    assert result["candidates"] == []
    assert "positive match" in result["reason"]
    assert out["status"] == "empty"


# --------------------------------------------------------------------------
# Dedup
# --------------------------------------------------------------------------


def test_duplicate_content_hashes_are_dropped(manifest, request_doc):
    doubled = copy.deepcopy(manifest)
    # Duplicate the forest B-roll under a new ID but same content hash.
    forest = next(a for a in doubled["assets"] if a["asset_id"] == "asset-broll-01")
    duplicate_asset = copy.deepcopy(forest)
    duplicate_asset["asset_id"] = "asset-broll-01-dup"
    doubled["assets"].append(duplicate_asset)
    forest_seg = next(s for s in doubled["segments"] if s["segment_id"] == "seg-forest")
    duplicate_seg = copy.deepcopy(forest_seg)
    duplicate_seg["segment_id"] = "seg-forest-dup"
    duplicate_seg["asset_id"] = "asset-broll-01-dup"
    doubled["segments"].append(duplicate_seg)

    out = retrieve(doubled, request_doc)
    middle = _result(out, "query-middle")
    segment_ids = [c["segment_id"] for c in middle["candidates"]]
    # Only one of the two identical forest clips should surface.
    assert segment_ids.count("seg-forest") + segment_ids.count("seg-forest-dup") == 1


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_candidate_ids_are_deterministic(manifest, request_doc):
    a = retrieve(manifest, request_doc)
    b = retrieve(manifest, request_doc)
    ids_a = [c["candidate_id"] for r in a["results"] for c in r["candidates"]]
    ids_b = [c["candidate_id"] for r in b["results"] for c in r["candidates"]]
    assert ids_a == ids_b
    for cid in ids_a:
        assert cid.startswith("candidate-")


# --------------------------------------------------------------------------
# B-roll library
# --------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_broll_library_rejects_non_external_assets(tmp_path, manifest):
    bad = {
        "schema_version": "0.1.0", "kind": "AssetManifest",
        "document_id": "bad-lib", "project_id": "any", "revision": 1,
        "assets": [{"asset_id": "x", "origin": "user", "revision": 1,
                     "media_kind": "video", "uri": "media/x.mp4"}],
        "segments": [],
    }
    path = _write(tmp_path, "bad.json", bad)
    with pytest.raises(ValueError, match="origin=external"):
        BRollLibrary.load(path)


def test_broll_library_merges_into_project_manifest(tmp_path, manifest):
    library_payload = {
        "schema_version": "0.1.0", "kind": "AssetManifest",
        "document_id": "lib-001", "project_id": "library",
        "revision": 1,
        "assets": [
            {"asset_id": "asset-lib-mountain", "revision": 1, "media_kind": "video",
             "origin": "external", "uri": "media/library/mountain.mp4",
             "content_hash": "fixture:mountain", "duration_us": 8000000,
             "video": {"width": 1920, "height": 1080, "fps": {"numerator": 30, "denominator": 1}},
             "audio": {"present": False, "sample_rate": None, "channels": 0},
             "provenance_note": "test library"}
        ],
        "segments": [
            {"segment_id": "seg-lib-mountain", "asset_id": "asset-lib-mountain",
             "asset_revision": 1,
             "source_range_us": {"begin": 1000000, "end": 5000000},
             "granularity": "coarse_shot", "parent_segment_id": None,
             "keyframes": [], "transcript": {"text": "", "segments": []},
             "description": {"text": "山景与远处的云", "provider": "fixture_annotation"},
             "quality": {"usable": True, "flags": []}}
        ],
    }
    lib_path = _write(tmp_path, "lib.json", library_payload)
    lib = BRollLibrary.load(lib_path)
    merged = lib.merge_into(manifest)

    assert any(a["asset_id"] == "asset-lib-mountain" for a in merged["assets"])
    assert any(s["segment_id"] == "seg-lib-mountain" for s in merged["segments"])
    # The caller's manifest must not be mutated.
    assert all(a["asset_id"] != "asset-lib-mountain" for a in manifest["assets"])

    req = {
        "schema_version": "0.1.0", "kind": "RetrievalRequest",
        "document_id": "req-mountain", "project_id": manifest["project_id"],
        "revision": 1,
        "queries": [
            {"query_id": "q-mountain", "beat_id": "b", "semantic_text": "山景",
             "role": "atmosphere", "allowed_origins": ["external"], "top_k": 3},
        ],
    }
    out = retrieve(merged, req)
    result = _result(out, "q-mountain")
    assert result["status"] == "ok"
    assert any(c["segment_id"] == "seg-lib-mountain" for c in result["candidates"])

"""Tests for Module B (retrieval + external B-roll library)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from quickcut.b_report import recall_against_expectations
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


def _ref(document):
    return {key: document[key] for key in ("kind", "document_id", "revision")}


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
    req["inputs"] = [_ref(manifest)]
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
    req["inputs"] = [_ref(manifest)]
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
    req["inputs"] = [_ref(manifest)]
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
    req["inputs"] = [_ref(manifest)]
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
    req["inputs"] = [_ref(manifest)]
    out = retrieve(manifest, req)
    result = _result(out, "q-noise")
    assert result["status"] == "empty"
    assert result["candidates"] == []
    assert "positive match" in result["reason"]
    assert out["status"] == "empty"


def _tiny_manifest():
    def asset(asset_id, origin, width, height, duration):
        return {
            "asset_id": asset_id, "revision": 1, "origin": origin,
            "content_hash": f"sha256:{asset_id}", "duration_us": duration,
            "video": {"width": width, "height": height},
        }

    def segment(asset_id, duration, text):
        return {
            "segment_id": f"seg-{asset_id}", "asset_id": asset_id, "asset_revision": 1,
            "source_range_us": {"begin": 0, "end": duration},
            "granularity": "coarse_shot", "quality": {"usable": True, "flags": []},
            "description": {"text": text, "provider": "test"},
            "transcript": {"text": ""},
        }

    document = {
        "schema_version": "0.1.0", "kind": "AssetManifest", "document_id": "manifest-filter",
        "project_id": "project-filter", "revision": 1, "fixtures_only": True, "owner": "A",
        "inputs": [],
        "assets": [
            asset("wide", "external", 1920, 1080, 10_000_000),
            asset("tall", "external", 1080, 1920, 10_000_000),
            asset("short", "external", 1920, 1080, 2_000_000),
        ],
        "segments": [
            segment("wide", 10_000_000, "小路行走"),
            segment("tall", 10_000_000, "小路行走"),
            segment("short", 2_000_000, "小路行走"),
        ],
    }
    return document


def _filter_request(**query_fields):
    query = {
        "query_id": "q-filter", "beat_id": "beat", "semantic_text": "小路行走",
        "role": "broll", "allowed_origins": ["external"], "top_k": 4,
        "duration_budget_us": 1_000_000,
    }
    query.update(query_fields)
    return {
        "schema_version": "0.1.0", "kind": "RetrievalRequest", "document_id": "request-filter",
        "project_id": "project-filter", "revision": 1, "fixtures_only": True,
        "inputs": [{"kind": "AssetManifest", "document_id": "manifest-filter", "revision": 1}],
        "queries": [query],
    }


def test_aspect_and_explicit_duration_are_hard_constraints():
    manifest = _tiny_manifest()
    landscape = retrieve(manifest, _filter_request(aspect_ratio={"width": 16, "height": 9}))
    ids = [item["asset_id"] for item in _result(landscape, "q-filter")["candidates"]]
    assert "wide" in ids and "short" in ids and "tall" not in ids
    capped = retrieve(manifest, _filter_request(max_duration_us=3_000_000))
    capped_ids = [item["asset_id"] for item in _result(capped, "q-filter")["candidates"]]
    assert capped_ids == ["short"]
    budget_only = retrieve(manifest, _filter_request())
    budget_ids = [item["asset_id"] for item in _result(budget_only, "q-filter")["candidates"]]
    assert "wide" in budget_ids
    assert budget_only["retrieval_profile"]["duration_budget_us_applied_as_filter"] is False


def test_score_profile_names_the_encoder_and_external_tags_stay_unpredicted():
    out = retrieve(_tiny_manifest(), _filter_request())
    profile = out["results"][0]["candidates"][0]["score_profile"]
    assert "model=fallback-color-histogram" in profile
    assert "dimension=72" in profile
    assert "not a calibrated probability" in profile
    assert out["results"][0]["candidates"][0]["tags"]["status"] == "not_predicted"


def test_recall_report_records_hits_misses_and_an_empty_gap():
    manifest = _tiny_manifest()
    request = _filter_request()
    request["queries"].append({
        "query_id": "q-gap", "beat_id": "beat", "semantic_text": "组员登顶后坐下休息",
        "role": "broll", "allowed_origins": ["external"], "top_k": 4,
    })
    candidates = retrieve(manifest, request)
    expectations = {"cases": [
        {"query": "小路行走", "expected_paths": ["media/wide.mp4", "media/missing.mp4"]},
        {"query": "组员登顶后坐下休息", "expected_result": "evidence_gap", "reason": "absent"},
    ]}
    report = recall_against_expectations(
        request, candidates, manifest, expectations,
        lambda asset: {"wide": "media/wide.mp4", "tall": "media/tall.mp4", "short": "media/short.mp4"}.get(asset.get("asset_id")),
    )
    walking = report["cases"][0]
    assert walking["status"] == "partial"
    assert walking["missed_paths"] == ["media/missing.mp4"]
    assert "media/wide.mp4" in walking["recalled_paths"]
    assert report["cases"][1]["status"] == "gap_confirmed"


def test_minimum_ranking_score_drops_weak_matches(manifest, request_doc):
    found = retrieve(manifest, request_doc)
    assert _result(found, "query-open")["candidates"]
    hidden = retrieve(manifest, request_doc, config=RetrievalConfig(minimum_ranking_score=0.99))
    assert all(result["status"] == "empty" and result["candidates"] == [] for result in hidden["results"])


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
        "revision": 1, "fixtures_only": True,
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
    req["inputs"] = [_ref(merged)]
    out = retrieve(merged, req)
    result = _result(out, "q-mountain")
    assert result["status"] == "ok"
    assert any(c["segment_id"] == "seg-lib-mountain" for c in result["candidates"])


def test_later_window_of_same_video_remains_retrievable():
    manifest = _tiny_manifest()
    manifest["assets"] = manifest["assets"][:1]
    first = manifest["segments"][0]
    first["description"]["text"] = "公路"
    second = copy.deepcopy(first)
    second.update(segment_id="later-window", source_range_us={"begin": 5_000_000, "end": 10_000_000})
    second["description"]["text"] = "树林行走"
    manifest["segments"] = [first, second]
    result = retrieve(manifest, _filter_request(semantic_text="树林行走"))
    assert result["results"][0]["candidates"][0]["segment_id"] == "later-window"


def test_duplicate_interval_retains_best_score_not_first_import():
    manifest = _tiny_manifest()
    first, second = manifest["segments"][:2]
    manifest["assets"][1]["content_hash"] = manifest["assets"][0]["content_hash"]
    first["description"]["text"] = "小路"
    second["description"]["text"] = "小路行走"
    manifest["segments"] = [first, second]
    result = retrieve(manifest, _filter_request())
    assert [c["segment_id"] for c in result["results"][0]["candidates"]] == [second["segment_id"]]


@pytest.mark.parametrize("reference", [None, [],
    [{"kind": "AssetManifest", "document_id": "manifest-filter", "revision": 99}],
    [{"kind": "AssetManifest", "document_id": "wrong-document", "revision": 1}],
    [{"kind": "AssetManifest", "document_id": "manifest-filter", "revision": True}],
])
def test_rejects_missing_or_stale_manifest_reference(reference):
    request = _filter_request()
    request["inputs"] = reference
    with pytest.raises(ValueError, match="AssetManifest"):
        retrieve(_tiny_manifest(), request)


def test_content_revisions_reuse_identical_result_and_advance_on_change():
    manifest, request = _tiny_manifest(), _filter_request()
    first = retrieve(manifest, request, revision=3)
    assert retrieve(manifest, request, previous=first) == first
    request["revision"] = 2
    request["queries"][0]["semantic_text"] = "夕阳"
    changed = retrieve(manifest, request, previous=first)
    assert changed["revision"] == 4 and changed["status"] == "empty"
    assert changed["document_id"] == first["document_id"]
    assert changed["inputs"][1]["revision"] == 2
    with pytest.raises(ValueError, match="old version"):
        retrieve(manifest, request, previous=first, revision=3)


def test_previous_candidate_set_cannot_be_from_another_project():
    manifest, request = _tiny_manifest(), _filter_request()
    first = retrieve(manifest, request)
    first["project_id"] = "somebody-elses-project"
    with pytest.raises(ValueError, match="different document or project"):
        retrieve(manifest, request, previous=first)


def test_model_profile_reuses_revision_after_json_roundtrip(monkeypatch):
    from quickcut.encoder import FallbackEncoder
    encoder = FallbackEncoder()
    encoder.name = "openclip"
    monkeypatch.setattr("quickcut.retrieval.make_encoder", lambda mode: encoder)
    first = retrieve(_tiny_manifest(), _filter_request())
    saved = json.loads(json.dumps(first))
    repeated = retrieve(_tiny_manifest(), _filter_request(), previous=saved)
    assert repeated == saved and repeated["revision"] == 1


def test_image_cache_binds_content_not_relative_filename(tmp_path):
    from quickcut.encoder import ppm_to_png
    pytest.importorskip("PIL")
    manifest, request = _tiny_manifest(), _filter_request(semantic_text="green", top_k=1)
    manifest["assets"] = manifest["assets"][:1]
    segment = manifest["segments"][0]
    segment["description"] = {"text": ""}
    segment["keyframes"] = [{"uri": "frame.png"}]
    manifest["segments"] = [segment]
    frame = tmp_path / "frame.png"
    frame.write_bytes(ppm_to_png(b"P6\n1 1\n255\n" + bytes([20, 180, 20])))
    retriever = Retriever()
    green = retriever.retrieve(manifest, request, project_root=tmp_path)
    assert green["results"][0]["candidates"][0]["visual_score"] > 0.9
    frame.write_bytes(ppm_to_png(b"P6\n1 1\n255\n" + bytes([180, 20, 20])))
    red = retriever.retrieve(manifest, request, project_root=tmp_path, previous=green)
    assert red["revision"] == 2
    assert red["results"][0]["candidates"][0]["visual_score"] < 0.6


def test_production_library_cannot_be_merged_without_registration(tmp_path):
    manifest = _tiny_manifest()
    manifest["fixtures_only"] = False
    library = BRollLibrary(tmp_path / "library.json", _tiny_manifest())
    with pytest.raises(ValueError, match="Register real B-roll with A"):
        library.merge_into(manifest)


def _cli_args(tmp_path, manifest, request):
    from argparse import Namespace
    return Namespace(
        manifest=str(_write(tmp_path, "manifest.json", manifest)),
        request=str(_write(tmp_path, "request.json", request)),
        output=str(tmp_path / "candidate-set.json"), broll_library=[],
        encoder="fallback", project_root=str(tmp_path), low_confidence_threshold=0.22,
        document_id="candidates-001", revision=None,
    )


def test_cli_persists_revisions_and_does_not_touch_identical_output(tmp_path):
    from quickcut.cli import _retrieve_to_file
    args = _cli_args(tmp_path, _tiny_manifest(), _filter_request())
    first = _retrieve_to_file(args)
    path = Path(args.output)
    mtime = path.stat().st_mtime_ns
    assert _retrieve_to_file(args) == first
    assert path.stat().st_mtime_ns == mtime
    args.low_confidence_threshold = 0.99
    second = _retrieve_to_file(args)
    assert second["revision"] == 2
    assert json.loads(path.read_text()) == second
    assert not path.with_name(path.name + ".lock").exists()


def test_cli_rejects_unregistered_library_without_output(tmp_path):
    from quickcut.cli import _retrieve_to_file
    args = _cli_args(tmp_path, _tiny_manifest(), _filter_request())
    args.broll_library = ["external-library.json"]
    with pytest.raises(ValueError, match="Import the external files"):
        _retrieve_to_file(args)
    assert not Path(args.output).exists()


def test_cli_rejects_another_writer_and_preserves_existing_output(tmp_path):
    from quickcut.cli import _retrieve_to_file
    args = _cli_args(tmp_path, _tiny_manifest(), _filter_request())
    _retrieve_to_file(args)
    path = Path(args.output)
    before = path.read_bytes()
    lock = path.with_name(path.name + ".lock")
    lock.write_text("other-writer")
    with pytest.raises(ValueError, match="locked by another writer"):
        _retrieve_to_file(args)
    assert path.read_bytes() == before and lock.read_text() == "other-writer"

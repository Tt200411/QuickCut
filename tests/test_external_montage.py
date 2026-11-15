"""公开素材可以串成蒙太奇，但不能被说成用户本人的经历。"""
from __future__ import annotations

import copy

from quickcut.planning import build_outline, build_retrieval_request, plan, validate_story_plan
from quickcut.planning.contracts import envelope
from quickcut.retrieval import retrieve

PROJECT = "project-montage"
CAPTIONS = [
    ("asset-short-01", "人物背影走在郊外小路上"),
    ("asset-short-02", "人物在林边行走"),
    ("asset-short-03", "人物走在乡间土路上"),
    ("asset-aerial-01", "无人机航拍郊外的土路"),
    ("asset-aerial-02", "无人机航拍郊外的土路"),
    ("asset-sunset", "郊外夕阳，暖色的光线和风景"),
]
APPROVED = {
    "beat-open": "沿着郊外的小路，看看路边的风景。",
    "beat-middle": "放慢脚步，留一点时间给身边的自然。",
    "beat-close": "让这段散步，在柔和的光线里收尾。",
}


def _manifest():
    assets, segments = [], []
    for index, (asset_id, caption) in enumerate(CAPTIONS):
        assets.append({
            "asset_id": asset_id, "revision": 1, "origin": "external",
            "content_hash": f"sha256:{index:064d}", "duration_us": 20_000_000,
            "media_kind": "video",
        })
        segments.append({
            "segment_id": f"seg-{asset_id}", "asset_id": asset_id, "asset_revision": 1,
            "source_range_us": {"begin": 0, "end": 15_000_000},
            "granularity": "coarse_shot", "quality": {"usable": True, "flags": []},
            "description": {"text": caption, "provider": "material_title"},
            "transcript": {"text": ""},
        })
    document = envelope("AssetManifest", "manifest-montage", PROJECT, 1, fixtures_only=False, owner="A", inputs=[])
    document.update(assets=assets, segments=segments)
    return document


def _brief():
    document = envelope("Brief", "brief-montage", PROJECT, 1, fixtures_only=False, owner="C", inputs=[])
    document.update(requirements={
        "story": "沿着郊外的小路看看路边的风景，放慢脚步留一点时间给身边的自然，让这段散步在柔和的光线里收尾。",
        "language": "zh-CN", "target_duration_us": 30_000_000,
        "picture_policy": "authorized_external_montage",
        "narration": {"enabled": True, "style": "轻松中文旁白"},
        "broll_policy": {"enabled": True, "roles": ["establishing", "atmosphere"], "must_not_assert_user_events": True},
    }, constraints={"preserve_source_audio": True})
    return document


def _request(brief, manifest):
    request = build_retrieval_request(brief, manifest, build_outline(brief), document_id="request-montage")
    texts = {"beat-open": "人物沿小路行走", "beat-middle": "人物沿小路行走", "beat-close": "夕阳和暖光风景"}
    broll = {"query-middle-establishing": "无人机拍摄道路或树林", "query-close-atmosphere": "夕阳和暖光风景"}
    for query in request["queries"]:
        if query["role"] == "primary":
            query["allowed_origins"] = ["external"]
            query["semantic_text"] = texts[query["beat_id"]]
        elif query["query_id"] in broll:
            query["semantic_text"] = broll[query["query_id"]]
    request["queries"].append({
        "query_id": "query-evidence-gap", "beat_id": "beat-middle",
        "semantic_text": "组员登顶后坐下休息", "role": "primary",
        "allowed_origins": ["external"], "duration_budget_us": 9_000_000,
        "top_k": 4, "iteration": 1,
    })
    return request


def _ids(result, query_id):
    row = next(item for item in result["results"] if item["query_id"] == query_id)
    return row["status"], [item["asset_id"] for item in row["candidates"]]


def test_captions_rank_the_described_clips_and_leave_the_missing_event_empty():
    manifest = _manifest()
    candidates = retrieve(manifest, _request(_brief(), manifest))
    assert _ids(candidates, "query-open")[1][:3] == ["asset-short-01", "asset-short-02", "asset-short-03"]
    assert _ids(candidates, "query-middle-establishing")[1][:2] == ["asset-aerial-01", "asset-aerial-02"]
    assert _ids(candidates, "query-close")[1] == ["asset-sunset"]
    assert _ids(candidates, "query-evidence-gap") == ("empty", [])


def test_authorized_montage_is_ready_without_claiming_a_user_event():
    brief, manifest = _brief(), _manifest()
    request = _request(brief, manifest)
    candidates = retrieve(manifest, request)
    story = plan(
        brief, manifest, candidates, retrieval_request=request, approved_cues=APPROVED,
    ).story_plan
    assert story["status"] == "ready_for_edit"
    assert story["unresolved_gaps"] == [] and story["pending_reviews"] == []
    shots = [shot for beat in story["beats"] for shot in beat["shots"]]
    assert [shot["origin"] for shot in shots] == ["external", "external", "external"]
    assert [shot["role"] for shot in shots] == ["establishing", "detail", "atmosphere"]
    assert len({shot["segment_id"] for shot in shots}) == 3
    assert all(cue["approved"] is True and cue["asserts_specific_user_event"] is False for cue in story["narration_cues"])
    report = validate_story_plan(
        story, brief=brief, manifest=manifest, candidate_set=candidates,
        upstream={"RetrievalRequest": request},
    )
    assert report["ok"], report["errors"]


def test_external_footage_without_montage_approval_still_stops():
    brief, manifest = _brief(), _manifest()
    brief["requirements"].pop("picture_policy")
    request = _request(brief, manifest)
    candidates = retrieve(manifest, request)
    story = plan(
        brief, manifest, candidates, retrieval_request=request, approved_cues=APPROVED,
    ).story_plan
    assert story["status"] == "needs_input"
    assert story["unresolved_gaps"]
    untouched = copy.deepcopy(story)
    untouched["status"] = "ready_for_edit"
    untouched["unresolved_gaps"] = []
    untouched["pending_reviews"] = []
    report = validate_story_plan(
        untouched, brief=brief, manifest=manifest, candidate_set=candidates,
        upstream={"RetrievalRequest": request},
    )
    assert not report["ok"]

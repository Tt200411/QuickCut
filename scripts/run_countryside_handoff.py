"""用 materials/A-countryside 的六条故事素材跑 A → B → C → E → D。

阳山导览和带声的空镜原片不进入这条故事。来源保持 external。
B 用 OpenCLIP 的 nllb-clip-base 直接比较中文查询和 A 抽出的 PNG 关键帧。
F 读取 timeline.json、voice-package.json 和 asset-manifest.json，项目根就是本目录。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from quickcut.b_report import recall_against_expectations
from quickcut.ingest import IngestConfig, SourceSpec, ingest_assets
from quickcut.integration import IntegrationError, build_from_plan
from quickcut.voice_package import VoiceError
from quickcut.media import write_json
from quickcut.planning import build_outline, build_retrieval_request, plan
from quickcut.planning.contracts import envelope, ref
from quickcut.retrieval import RetrievalConfig, retrieve

MATERIALS = REPO / "materials" / "A-countryside"
PROJECT_ID = "project-countryside"
STORY = "沿着郊外的小路看看路边的风景，放慢脚步留一点时间给身边的自然，让这段散步在柔和的光线里收尾。"
PRIMARY_TEXT = {
    "beat-open": "人物沿小路行走",
    "beat-middle": "人物沿小路行走",
    "beat-close": "夕阳和暖光风景",
}
BROLL_TEXT = {
    "query-middle-establishing": "无人机拍摄道路或树林",
    "query-close-atmosphere": "夕阳和暖光风景",
}
GAP_TEXT = "组员登顶后坐下休息"
APPROVED = {
    "beat-open": "沿着郊外的小路，看看路边的风景。",
    "beat-middle": "放慢脚步，留一点时间给身边的自然。",
    "beat-close": "让这段散步，在柔和的光线里收尾。",
}
OUTPUT = {"width": 1280, "height": 720, "fps": {"numerator": 30, "denominator": 1}, "sample_rate": 48000}
# nllb 的文本塔能读中文。真实命中大约 0.23–0.30；缺失事件的最高分大约 0.09。
CLIP_MODEL = "nllb-clip-base"
CLIP_PRETRAINED = "v1"
VISUAL_FLOOR = 0.18
VOICE = {"rate": 145}


def load_sources():
    specs = json.loads((MATERIALS / "story-sources.json").read_text(encoding="utf-8"))
    loaded = []
    for item in specs:
        path = (MATERIALS / item["path"]).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        loaded.append(SourceSpec(
            path, origin=item["origin"], provenance_note=item["provenance_note"],
            source_key=item["source_key"],
        ))
    return loaded


def make_brief():
    brief = envelope("Brief", "brief-countryside", PROJECT_ID, 1, fixtures_only=False, owner="C", inputs=[])
    brief.update(
        requirements={
            "story": STORY,
            "format": "thematic_montage",
            "language": "zh-CN",
            "target_duration_us": 30_000_000,
            "aspect_ratio": {"width": 16, "height": 9},
            "tone": "轻松",
            "narration": {"enabled": True, "style": "轻松中文旁白"},
            "picture_policy": "authorized_external_montage",
            "broll_policy": {
                "enabled": True,
                "roles": ["establishing", "atmosphere"],
                "must_not_assert_user_events": True,
            },
            "target_editor": "未验证",
        },
        constraints={
            "local_inference_only": True,
            "preserve_source_audio": True,
            "editable_tracks_required": True,
            "origin_policy": "公开素材保持 external；不改记为用户实拍",
        },
        source_note="旁白采用 brief-proposal.json 的三句可选文案，并标明不声称用户本人经历",
    )
    return brief


def countryside_request(brief, manifest, outline):
    request = build_retrieval_request(brief, manifest, outline, broll_queries=True, document_id="request-countryside")
    for query in request["queries"]:
        if query["role"] == "primary":
            query["allowed_origins"] = ["external"]
            query["semantic_text"] = PRIMARY_TEXT[query["beat_id"]]
        elif query["query_id"] in BROLL_TEXT:
            query["semantic_text"] = BROLL_TEXT[query["query_id"]]
    for query in request["queries"]:
        query["top_k"] = 6
        query["aspect_ratio"] = {"width": 16, "height": 9}
    request["queries"].append({
        "query_id": "query-evidence-gap",
        "beat_id": "beat-middle",
        "semantic_text": GAP_TEXT,
        "role": "primary",
        "allowed_origins": ["external"],
        "duration_budget_us": 9_000_000,
        "top_k": 6,
        "aspect_ratio": {"width": 16, "height": 9},
        "iteration": 1,
        "beat_purpose": "材料指定的缺失证据，不应召回成片镜头",
    })
    request["iteration_notes"] = [
        "六条故事素材均为 external。主镜头查询因此也搜索 external，而不是只搜索 user。",
        "查询文本来自 retrieval-expectations.json。",
    ]
    return request


def recall_report(manifest, request, candidates):
    inventory = json.loads((MATERIALS / "inventory.json").read_text(encoding="utf-8"))
    expectations = json.loads((MATERIALS / "retrieval-expectations.json").read_text(encoding="utf-8"))
    by_hash = {f"sha256:{item['sha256']}": item["path"] for item in inventory}
    return recall_against_expectations(
        request, candidates, manifest, expectations,
        lambda asset: by_hash.get(asset.get("content_hash")),
    )


def summarize(manifest, candidates, story):
    by_asset = {asset["asset_id"]: asset for asset in manifest["assets"]}
    segment_asset = {segment["segment_id"]: segment["asset_id"] for segment in manifest["segments"]}
    rows = []
    for result in candidates.get("results", []):
        hits = []
        for candidate in result.get("candidates", []):
            asset = by_asset.get(candidate.get("asset_id"), {})
            hits.append({
                "candidate_id": candidate["candidate_id"],
                "uri": asset.get("uri"),
                "ranking_score": candidate.get("ranking_score"),
                "visual_score": candidate.get("visual_score"),
                "origin": candidate.get("origin"),
            })
        rows.append({
            "query_id": result["query_id"],
            "status": result["status"],
            "reason": result.get("reason"),
            "candidate_count": len(hits),
            "candidates": hits,
        })
    return {
        "asset_count": len(manifest["assets"]),
        "segment_count": len(manifest["segments"]),
        "processing": manifest["processing"]["status"],
        "encoder": (candidates.get("retrieval_profile") or {}).get("encoder"),
        "candidate_status": candidates["status"],
        "queries": rows,
        "story_status": story["status"],
        "gap_ids": [gap["gap_id"] for gap in story.get("unresolved_gaps", [])],
        "pending_reviews": story.get("pending_reviews", []),
        "shots": [
            {
                "beat_id": beat["beat_id"],
                "role": shot["role"],
                "origin": shot["origin"],
                "uri": by_asset.get(segment_asset.get(shot["segment_id"]), {}).get("uri"),
                "summary": (shot.get("summary") or {}).get("text") if isinstance(shot.get("summary"), dict) else shot.get("summary"),
            }
            for beat in story.get("beats", []) for shot in beat.get("shots", [])
        ],
    }


def main(root: Path):
    root = root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} 必须是空目录或不存在")
    root.mkdir(parents=True, exist_ok=True)
    manifest = ingest_assets(
        root, load_sources(), project_id=PROJECT_ID,
        config=IngestConfig(window_us=15_000_000, coarse_window_us=30_000_000, asr="none"),
    )
    os.environ["QUICKCUT_CLIP_MODEL"] = CLIP_MODEL
    os.environ["QUICKCUT_CLIP_PRETRAINED"] = CLIP_PRETRAINED
    brief = make_brief()
    outline = build_outline(brief)
    request = countryside_request(brief, manifest, outline)
    candidates = retrieve(
        manifest, request, project_root=root, document_id="candidates-countryside",
        config=RetrievalConfig(encoder_mode="openclip", minimum_ranking_score=VISUAL_FLOOR),
    )
    result = plan(
        brief, manifest, candidates, retrieval_request=request,
        approved_cues=APPROVED, plan_document_id="story-countryside", plan_revision=1,
    )
    write_json(root / "brief.json", brief)
    write_json(root / "approved-cues.json", {"approval_source": "brief-proposal.optional_narration", "approved_cues": APPROVED})
    write_json(root / "retrieval-request.json", request)
    write_json(root / "candidate-set.json", candidates)
    write_json(root / "b-recall-report.json", recall_report(manifest, request, candidates))
    write_json(root / "story-plan.json", result.story_plan)
    write_json(root / "alignment.json", result.alignment)
    integration_error = None
    built = None
    try:
        built = build_from_plan(
            result, brief=brief, manifest=manifest, candidate_set=candidates,
            root=root, output=OUTPUT, fixture=False, config=VOICE, voice_revision=1,
            expected_story_revision=1, timeline_document_id="timeline-countryside",
        )
    except (IntegrationError, VoiceError) as exc:
        integration_error = exc.to_dict(PROJECT_ID) if isinstance(exc, IntegrationError) else {
            "status": "failed", "code": exc.code, "stage": "E", "project_id": PROJECT_ID,
            "message": str(exc), "retryable": False, "affected_ids": exc.ids, "action_owner": "E",
        }
    if built is not None:
        write_json(root / "voice-package.json", built.voice_package)
        write_json(root / "timeline.json", built.timeline)
        write_json(root / "assets-union.json", {
            "inputs": [ref(manifest), ref(built.voice_package)],
            "assets": built.assets,
        })
    ready = built is not None and result.story_plan["status"] == "ready_for_edit"
    f_inputs = {
        "project_root": str(root),
        "ready_for_f": ready,
        "required": {
            "EditTimeline": "timeline.json" if ready else None,
            "VoicePackage": "voice-package.json" if ready else None,
            "AssetManifest": "asset-manifest.json",
        },
        "also_written": [
            "brief.json", "approved-cues.json", "retrieval-request.json", "candidate-set.json",
            "b-recall-report.json", "story-plan.json", "alignment.json",
        ],
        "blocked_by": integration_error,
        "why": (
            "F 读取冻结 EditTimeline、VoicePackage，以及能解析到文件的素材引用。"
            if ready else
            "C 或 E/D 没有交出可冻结的时间线和配音，F 还不能开始。"
        ),
    }
    report = {
        "project_id": PROJECT_ID,
        "materials": "materials/A-countryside/story-sources.json",
        "excluded": ["media/long-01-mandarin-park-guide.webm", "media/broll-02-countryside-aerial.mp4"],
        "asr": "none",
        "clip_model": CLIP_MODEL,
        "clip_pretrained": CLIP_PRETRAINED,
        "visual_floor": VISUAL_FLOOR,
        "summary": summarize(manifest, candidates, result.story_plan),
        "f_inputs": f_inputs,
    }
    write_json(root / "f-inputs.json", f_inputs)
    write_json(root / "run-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ready and manifest["processing"]["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else REPO / "ABCDE")))

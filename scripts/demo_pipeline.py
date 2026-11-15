"""真实 A → B 检索 → C → E 测试音调 → D JSON；不执行渲染。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from dataclasses import asdict
import functools
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
import wave

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from quickcut import integration
from quickcut.core import _ffmpeg_binary
from quickcut.ingest import IngestConfig, SourceSpec, ingest_assets, validate_asset_manifest
from quickcut.media import digest, ffprobe, resolve_uri, write_json
from quickcut.planning import build_outline, build_retrieval_request, plan
from quickcut.planning.contracts import envelope, ref
from quickcut.retrieval import retrieve

OUTPUT = {"width": 320, "height": 180, "fps": {"numerator": 30, "denominator": 1},
          "sample_rate": 48000}
APPROVED = {"beat-open": "第一段测试画面。", "beat-middle": "第二段测试画面。",
            "beat-close": "第三段测试画面。"}
LIMITATIONS = [
    "合成测试画面没有对白。演示给每个窗口写上对应故事句，B 再按字面重合检索。",
    "候选审阅和 approved_cues 是预设夹具；不代表真人看片或听审。",
    "E fixture_tone 是固定一秒测试音调，不是真实语音；5 秒目标的收尾预算不足时停止 D。",
    "仅生成 D 多轨 JSON；未渲染、未导出编辑器工程、未验证编辑器导入。",
]


class DemoError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def prepare_root(path):
    root = Path(path).expanduser()
    if root.is_symlink():
        raise DemoError("ROOT_NOT_EMPTY", "root 不得是符号链接")
    root = root.resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise DemoError("ROOT_NOT_EMPTY", "root 必须不存在或为空；已有内容未修改")
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def stage(report, name):
    entry = {"stage": name, "status": "running"}
    report["stages"].append(entry)
    started = perf_counter()
    try:
        yield
    except Exception:
        entry["status"] = "failed"
        raise
    else:
        entry["status"] = "completed"
    finally:
        entry["elapsed_seconds"] = perf_counter() - started


def run_command(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise DemoError("MEDIA_COMMAND_FAILED", result.stderr[-4000:] or result.stdout[-4000:])
    return result.stdout


def capabilities():
    tools = {name: {"version": run_command([binary, "-version"]).splitlines()[0], "binary": binary}
             for name, binary in (("ffmpeg", _ffmpeg_binary()), ("ffprobe", ffprobe()))}
    return {"tools": tools, "A": {"ingest": True, "asr": "none"},
            "B": {"executed": True, "candidate_source": "retrieve"},
            "C": {"selector": "rule", "vlm_executed": False},
            "E": {"mode": "fixture_tone", "speech": False},
            "D": {"multitrack_json": True}, "render": {"executed": False}}


def git_state():
    def git(*args):
        result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                                text=True, timeout=10)
        if result.returncode:
            raise DemoError("GIT_METADATA_FAILED", result.stderr.strip())
        return result.stdout.strip()
    modules = ["quickcut/ingest.py", "quickcut/retrieval.py", "quickcut/planning/planning.py",
               "quickcut/voice_package.py", "quickcut/timeline.py", "quickcut/integration.py",
               "scripts/demo_pipeline.py"]
    return {"head": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "refs": git("show-ref").splitlines(), "status": git("status", "--porcelain").splitlines(),
            "source_hashes": {name: digest(REPO / name) for name in modules}}


def make_brief(project_id, target_seconds):
    brief = envelope("Brief", "brief-demo", project_id, 1, fixtures_only=True, owner="C", inputs=[])
    brief.update(requirements={
        "story": "展示第一段测试画面，展示第二段测试画面，展示第三段测试画面。",
        "format": "demo", "language": "zh-CN", "target_duration_us": target_seconds * 1_000_000,
        "aspect_ratio": {"width": 16, "height": 9}, "tone": "测试",
        "narration": {"enabled": True, "style": "fixture_tone，不是真实语音"},
        "broll_policy": {"enabled": False, "roles": [], "must_not_assert_user_events": True},
        "target_editor": "未验证",
    }, constraints={"local_inference_only": True, "preserve_source_audio": True,
                    "editable_tracks_required": True},
        fixture_provenance={"approval": "preset_fixture_only", "human_review_performed": False})
    return brief


def synthesize_video(root, window_us):
    target = root / "sources" / "synthetic-pattern.mp4"
    target.parent.mkdir()
    run_command([_ffmpeg_binary(), "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
                 "testsrc2=size=320x180:rate=30", "-f", "lavfi", "-i",
                 "sine=frequency=440:sample_rate=48000", "-t", str(window_us * 3 / 1_000_000),
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", str(target)])
    return target


def label_shot_descriptions(manifest, outline):
    """给合成窗口写上对应故事句，让没有对白的测试画面能被 B 的文字检索命中。"""
    labeled = copy.deepcopy(manifest)
    events = {beat["beat_id"]: beat["wish"]["event"] for beat in outline}
    shots = sorted((s for s in labeled["segments"] if s.get("granularity") == "coarse_shot"
                    and (s.get("quality") or {}).get("usable", True)),
                   key=lambda s: s["source_range_us"]["begin"])
    beat_ids = [beat["beat_id"] for beat in outline]
    if len(shots) < len(beat_ids):
        raise DemoError("INSUFFICIENT_MEDIA", "A 未提供三个可用测试窗口")
    for shot, beat_id in zip(shots, beat_ids):
        bounds = shot["source_range_us"]
        shot["description"] = {"text": events[beat_id], "provider": "demo_window_label"}
        shot["_demo_beat_id"] = beat_id
        if bounds["end"] - bounds["begin"] <= 0:
            raise DemoError("INSUFFICIENT_MEDIA", f"{beat_id} 的实际源窗口容量不足")
    return labeled


def preset_reviews(candidates, manifest, request, outline):
    """把 B 实际返回的候选绑定成演示用审阅，不改检索分数。"""
    segments = {segment["segment_id"]: segment for segment in manifest["segments"]}
    queries = {query["query_id"]: query for query in request["queries"]}
    beats = {beat["beat_id"]: beat for beat in outline}
    reviews = {}
    for row in candidates["results"]:
        beat = beats[queries[row["query_id"]]["beat_id"]]
        for candidate in row["candidates"]:
            segment = segments[candidate["segment_id"]]
            reviews[candidate["candidate_id"]] = {
                "beat_id": beat["beat_id"],
                "event": beat["wish"]["event"],
                "asset_revision": segment["asset_revision"],
                "asset_id": segment["asset_id"],
                "segment_id": segment["segment_id"],
                "source_range_us": segment["source_range_us"],
                "reviewer": "demo_preset",
                "note": "合成演示的预设审阅，只用于让 C 接受 B 返回的候选。",
            }
    return reviews


def retrieve_candidates(manifest, request, outline, root, *, unsupported=False):
    labeled = label_shot_descriptions(manifest, outline)
    candidates = retrieve(labeled, request, project_root=root, document_id="candidates-demo")
    if unsupported:
        queries = {query["query_id"]: query for query in request["queries"]}
        for row in candidates["results"]:
            if queries[row["query_id"]]["beat_id"] != "beat-middle":
                continue
            row["status"] = "empty"
            row["reason"] = "演示用例清空中段候选，验证 C 在 B 没有返回该段时停止"
            row["candidates"] = []
        if not any(row["candidates"] for row in candidates["results"]):
            candidates["status"] = "empty"
    return labeled, candidates


def validate_media(root, manifest, assets, voice=None):
    validate_asset_manifest(manifest, root)
    if manifest["processing"]["status"] != "success" or not manifest["assets"]:
        raise DemoError("INGEST_NOT_READY", "A 未成功完成真实媒体处理")
    ids = [a["asset_id"] for a in assets]
    if len(ids) != len(set(ids)):
        raise DemoError("DUPLICATE_ASSET", "A/E assets union 存在重复 ID")
    checked = set()

    def check_uris(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if item is not None and (key == "uri" or key.endswith("_uri")):
                    path = resolve_uri(root, item)
                    if not path.is_file() or not path.stat().st_size:
                        raise DemoError("MISSING_MEDIA", item)
                else:
                    check_uris(item)
        elif isinstance(value, list):
            for item in value:
                check_uris(item)

    check_uris(manifest)
    check_uris(assets)
    check_uris(voice or {})
    for asset in assets:
        path = resolve_uri(root, asset["uri"])
        if digest(path) != asset["content_hash"]:
            raise DemoError("MEDIA_HASH_MISMATCH", asset["uri"])
        if asset["media_kind"] == "audio":
            with wave.open(str(path), "rb") as audio:
                measured = round(audio.getnframes() * 1_000_000 / audio.getframerate())
                if measured != asset["duration_us"]:
                    raise DemoError("MEDIA_DURATION_MISMATCH", asset["uri"])
        else:
            for uri in (asset["uri"], asset["proxy_uri"]):
                run_command([_ffmpeg_binary(), "-v", "error", "-xerror", "-nostdin", "-i",
                             str(resolve_uri(root, uri)), "-f", "null", "-"])
                checked.add(uri)
            pts = json.loads(resolve_uri(root, asset["source_pts_uri"]).read_text())["frames"]
            mapping = json.loads(resolve_uri(root, asset["proxy_mapping_uri"]).read_text())
            if not pts or [{k: f[k] for k in ("source_pts", "source_time_us")}
                           for f in mapping["frames"]] != pts:
                raise DemoError("INVALID_PTS_MAPPING", "源 PTS 与代理映射不一致")
    for cue in (voice or {}).get("cues", []):
        text = resolve_uri(root, cue["srt_uri"]).read_text(encoding="utf-8")
        if not text.endswith(cue["text"] + "\n"):
            raise DemoError("INVALID_SUBTITLE", cue["cue_id"])
    return {"status": "passed", "asset_count": len(assets), "decoded_video_files": len(checked),
            "hashes": True, "source_pts_mapping": True, "A_manifest_validated": True}


@contextmanager
def observe_bridge(report, root, revision):
    # CLI 是单线程；临时包装真实调用以计时并保留 D 失败前 E 已产生的文档。
    original_voice = integration.voice_package.build_voice_package
    original_compile = integration.compile_timeline

    def observe(function, name, filename):
        @functools.wraps(function)
        def call(*args, **kwargs):
            with stage(report, name):
                report["calls"][name[0]] += 1
                value = function(*args, **kwargs)
                write_json(root / filename, value)
                report["files"][filename.split("-r")[0]] = filename
                if name.startswith("E-"):
                    manifest = json.loads((root / "asset-manifest.json").read_text(encoding="utf-8"))
                    union_name = f"assets-union-r{revision}.json"
                    write_json(root / union_name, {"fixtures_only": True,
                        "inputs": [ref(manifest), ref(value)], "assets": manifest["assets"] + value["assets"]})
                    report["files"]["assets_union"] = union_name
                return value
        return call

    integration.voice_package.build_voice_package = observe(
        original_voice, f"E-r{revision}", f"voice-package-r{revision}.json")
    integration.compile_timeline = observe(original_compile, f"D-r{revision}", f"timeline-r{revision}.json")
    try:
        yield
    finally:
        integration.voice_package.build_voice_package = original_voice
        integration.compile_timeline = original_compile


def save_plan(root, result, revision):
    for name, document in (("outline", result.outline), ("retrieval-request", result.retrieval_request),
                           ("story-plan", result.story_plan), ("alignment", result.alignment),
                           ("planning-run", result.run)):
        write_json(root / f"{name}-r{revision}.json", document)


def feedback(exc, report):
    if isinstance(exc, integration.IntegrationError):
        return exc.to_dict(report["project_id"])
    if hasattr(exc, "to_envelope"):
        return exc.to_envelope()
    return {"status": "failed", "code": getattr(exc, "code", "DEMO_FAILED"),
            "stage": report["stages"][-1]["stage"] if report["stages"] else "setup",
            "project_id": report["project_id"], "message": str(exc), "retryable": False,
            "affected_ids": list(getattr(exc, "affected_ids", ())),
            "action_owner": getattr(exc, "action_owner", "demo")}


def run_demo(root, case="supported", target_seconds=10):
    if case not in {"supported", "unsupported", "edit"} or target_seconds not in {5, 10, 15}:
        raise DemoError("INVALID_ARGUMENT", "case 或 target_seconds 无效")
    root = prepare_root(root)
    started = perf_counter()
    report = {"schema_version": "0.1.0", "project_id": "project-pipeline-demo", "case": case,
              "target_seconds": target_seconds, "fixturesOnly": True, "status": "running",
              "stages": [], "calls": {"A": 0, "B": 0, "E": 0, "D": 0}, "files": {},
              "render": None, "limitations": LIMITATIONS}
    try:
        with stage(report, "setup"):
            report["git"] = git_state()
            report["capabilities"] = capabilities()
            brief = make_brief(report["project_id"], target_seconds)
            outline = build_outline(brief)
            window_us = max(b["duration_budget_us"] for b in outline) + 1_000_000
            config = IngestConfig(window_us=window_us, coarse_window_us=window_us * 3,
                                  proxy_width=320, asr="none")
            report["ingest_config"] = asdict(config)
            write_json(root / "brief.json", brief)
            report["files"]["brief"] = "brief.json"
        with stage(report, "synthesize"):
            source = synthesize_video(root, window_us)
            report["files"]["source"] = source.relative_to(root).as_posix()
        with stage(report, "A-ingest"):
            report["calls"]["A"] += 1
            manifest = ingest_assets(root, [SourceSpec(source, provenance_note="实际合成测试图案；非实拍",
                                                      source_key="synthetic-demo")],
                                     config=config, project_id=report["project_id"])
            report["files"]["manifest"] = "asset-manifest.json"
            report["manifest_hash_before"] = digest(root / "asset-manifest.json")
        with stage(report, "A-validate"):
            report["media_validation"] = validate_media(root, manifest, manifest["assets"])
            report["manifestFixturesOnly"] = manifest["fixtures_only"]
        with stage(report, "B-retrieve"):
            report["calls"]["B"] += 1
            request = build_retrieval_request(brief, manifest, outline, broll_queries=False)
            labeled, candidates = retrieve_candidates(
                manifest, request, outline, root, unsupported=case == "unsupported")
            reviews = preset_reviews(candidates, labeled, request, outline)
            write_json(root / "candidate-set.json", candidates)
            write_json(root / "approved-cues-r1.json", {"fixtures_only": True,
                       "approval_source": "preset_fixture_only", "approved_cues": APPROVED})
            report["files"]["candidate_set"] = "candidate-set.json"
        approved = dict(APPROVED)
        previous = None
        for revision in range(1, 3 if case == "edit" else 2):
            if revision == 2:
                approved["beat-middle"] = "第二段测试画面的修改文案。"
                write_json(root / "approved-cues-r2.json", {"fixtures_only": True,
                           "approval_source": "preset_fixture_only", "approved_cues": approved})
            with stage(report, f"C-r{revision}"):
                result = plan(brief, labeled, candidates, retrieval_request=request,
                              approved_cues=approved, reviewed_evidence=reviews,
                              broll_queries=False, plan_revision=revision)
                save_plan(root, result, revision)
                report["files"]["story_plan"] = f"story-plan-r{revision}.json"
            if case == "unsupported":
                if result.story_plan["status"] != "needs_input" or not result.story_plan["unresolved_gaps"]:
                    raise DemoError("EXPECTED_GAP_MISSING", "C 没有按预期暴露素材缺口")
                report.update(status="needs_input", expected_stop=True, feedback={
                    "status": "needs_input", "code": "UNSUPPORTED_STORY_BEAT", "stage": "C",
                    "project_id": report["project_id"], "action_owner": "C", "retryable": False,
                    "affected_ids": [g["beat_id"] for g in result.story_plan["unresolved_gaps"]],
                    "message": "B 的中段候选被演示用例清空；未调用 E/D"})
                break
            with stage(report, f"integration-r{revision}"):
                with observe_bridge(report, root, revision):
                    built = integration.build_from_plan(
                        result, brief=brief, manifest=labeled, candidate_set=candidates, root=root,
                        output=OUTPUT, fixture=True, previous=previous, voice_revision=revision,
                        expected_story_revision=revision, timeline_revision=revision)
            with stage(report, f"media-validation-r{revision}"):
                report["media_validation"] = validate_media(root, manifest, built.assets, built.voice_package)
                if built.timeline["duration_us"] != target_seconds * 1_000_000:
                    raise DemoError("DURATION_MISMATCH", "D 成片时长未达到配置目标")
            if previous is not None:
                misses = [c["cue_id"] for c in built.voice_package["cues"] if not c["cache_hit"]]
                if misses != ["cue-02"] or built.voice_package["invalidated_cue_ids"] != ["cue-02"]:
                    raise DemoError("CACHE_INVALIDATION_MISMATCH", "编辑必须仅使 cue-02 缓存失效")
                report["edit"] = {"changed_cue_ids": ["cue-02"], "cache_misses": misses,
                                  "cache_hits": [c["cue_id"] for c in built.voice_package["cues"] if c["cache_hit"]],
                                  "story_revision": revision, "voice_revision": revision,
                                  "timeline_revision": revision, "A_reingested": False}
            previous = built.voice_package
            report["render"] = {"files": {"timeline": f"timeline-r{revision}.json",
                "manifest": "asset-manifest.json", "voice_package": f"voice-package-r{revision}.json",
                "assets_union": f"assets-union-r{revision}.json"},
                "output": built.timeline["output"], "uris_relative_to": "project_root", "executed": False}
            report["status"] = "ready_for_render"
        with stage(report, "manifest-unchanged"):
            report["manifest_hash_after"] = digest(root / "asset-manifest.json")
            if report["manifest_hash_before"] != report["manifest_hash_after"] or report["calls"]["A"] != 1:
                raise DemoError("MANIFEST_CHANGED", "规划或语音编辑改变了 A manifest")
            report["manifest_unchanged"] = True
    except Exception as exc:
        report.update(status="failed", feedback=feedback(exc, report), render=None)
    report["elapsed_seconds"] = perf_counter() - started
    report["files"]["report"] = "run-report.json"
    write_json(root / "run-report.json", report)
    if "feedback" in report:
        write_json(root / "feedback.json", report["feedback"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="必须不存在或为空的项目根")
    parser.add_argument("--case", choices=["supported", "unsupported", "edit"], default="supported")
    parser.add_argument("--target-seconds", type=int, choices=[5, 10, 15], default=10)
    args = parser.parse_args(argv)
    try:
        report = run_demo(args.root, args.case, args.target_seconds)
    except Exception as exc:
        report = {"status": "failed", "code": getattr(exc, "code", "DEMO_FAILED"), "stage": "setup",
                  "message": str(exc), "retryable": False}
    success = report["status"] == "ready_for_render"
    print(json.dumps(report, ensure_ascii=False, allow_nan=False), file=sys.stdout if success else sys.stderr)
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())

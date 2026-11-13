"""C 模块可运行入口。

    python -m quickcut.planning run --out-dir outputs/c
    python -m quickcut.planning run --case insufficient --out-dir outputs/c-insufficient
    python -m quickcut.planning inject --output outputs/c/injected_bundle.json
    python -m quickcut.planning selftest
"""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .contracts import (
    PlanningError,
    STALE_INPUT_REVISION,
    VLM_INVALID_PLAN,
    check_inputs,
    load_bundle,
    read_json,
    write_json,
)
from .planning import (
    PlanResult, build_outline, build_retrieval_request, candidates_for_beat, compare_plans, plan,
)
from .selection import FixedResponseSelector, OllamaSelector, RuleSelector, Selector, validate_selection
from .validator import assert_valid, validate_story_plan

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE = ROOT / "examples/team_contracts/bundle.json"
DEFAULT_C_CANDIDATES = ROOT / "examples/team_contracts/c/candidate_set.json"
DEFAULT_APPROVED_CUES = ROOT / "examples/team_contracts/c/approved_cues.json"
DEFAULT_FIXED_RESPONSE = ROOT / "examples/team_contracts/c/fixed_model_response.json"


def _docs(bundle_path: Path, candidate_path: Path | None = None) -> dict[str, dict[str, Any]]:
    _raw, docs = load_bundle(bundle_path)
    if candidate_path:
        docs["CandidateSet"] = read_json(candidate_path)
    return docs


def _selector(mode: str, fixed_path: Path | None) -> Selector:
    if mode == "rule":
        return RuleSelector()
    if mode == "fixed":
        return FixedResponseSelector.from_path(fixed_path or DEFAULT_FIXED_RESPONSE)
    if mode == "ollama":
        return OllamaSelector(project_root=ROOT)
    raise ValueError(f"unknown selector mode: {mode}")


def _insufficient(candidate_set: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    if candidate_set.get("fixtures_only") is not True or request.get("fixtures_only") is not True:
        raise PlanningError(VLM_INVALID_PLAN, "insufficient 仅允许 fixtures_only 输入", retryable=False)
    data = copy.deepcopy(candidate_set)
    target = next((q["query_id"] for q in request["queries"] if q["beat_id"] == "beat-close" and q["role"] == "primary"), None)
    for result in data["results"]:
        if result["query_id"] == target:
            result["status"] = "empty"
            result["reason"] = "人工构造：结尾事件没有任何可用素材"
            result["candidates"] = []
    data["status"] = "partial"
    data["document_id"] = "candidates-insufficient-001"
    return data


def _save(result: PlanResult, out_dir: Path) -> None:
    write_json(out_dir / "outline.json", result.outline)
    write_json(out_dir / "retrieval_request.json", result.retrieval_request)
    write_json(out_dir / "story_plan.json", result.story_plan)
    write_json(out_dir / "alignment.json", result.alignment)
    write_json(out_dir / "run.json", result.run)


def command_run(args: argparse.Namespace) -> int:
    default_fixture = Path(args.bundle).resolve() == DEFAULT_BUNDLE.resolve() and not args.candidate_set
    docs = _docs(Path(args.bundle), Path(args.candidate_set) if args.candidate_set else (DEFAULT_C_CANDIDATES if default_fixture else None))
    brief, manifest = docs["Brief"], docs["AssetManifest"]
    candidate_set = copy.deepcopy(docs["CandidateSet"])
    default_fixture = default_fixture and all(d.get("fixtures_only") is True for d in docs.values())
    approved = read_json(args.approved_cues) if args.approved_cues else (
        read_json(DEFAULT_APPROVED_CUES) if default_fixture and DEFAULT_APPROVED_CUES.is_file() else None
    )
    outline = build_outline(brief)
    request_link = next(
        (link for link in candidate_set.get("inputs", []) if link.get("kind") == "RetrievalRequest"),
        {"document_id": "request-001", "revision": 1},
    )
    request = build_retrieval_request(
        brief, manifest, outline,
        broll_queries=not args.no_broll_queries,
        document_id=request_link["document_id"],
        revision=request_link["revision"],
    ) if default_fixture else docs["RetrievalRequest"]
    if args.no_broll_queries and not default_fixture:
        raise PlanningError(VLM_INVALID_PLAN, "自定义 bundle 应显式提供与候选匹配的 RetrievalRequest", retryable=False)
    if args.case == "insufficient":
        if not all(d.get("fixtures_only") is True for d in (brief, manifest, request, candidate_set)):
            raise PlanningError(VLM_INVALID_PLAN, "insufficient 仅允许 fixtures_only 输入", retryable=False)
        candidate_set = _insufficient(candidate_set, request)
    result = plan(
        brief, manifest, candidate_set,
        retrieval_request=request,
        selector=_selector(args.mode, Path(args.fixed_response) if args.fixed_response else None),
        approved_cues=approved,
        reviewed_evidence=read_json(args.reviewed_evidence) if args.reviewed_evidence else None,
        broll_queries=not args.no_broll_queries,
        plan_document_id=args.document_id,
        plan_revision=args.revision,
    )
    report = validate_story_plan(
        result.story_plan, brief=brief, candidate_set=candidate_set, manifest=manifest,
        upstream={"Brief": brief, "CandidateSet": candidate_set, "AssetManifest": manifest, "RetrievalRequest": result.retrieval_request},
    )
    result.run["validation"] = report
    _save(result, Path(args.out_dir))
    print(json.dumps({
        "mode": args.mode,
        "case": args.case,
        "status": result.story_plan["status"],
        "beats": len(result.story_plan["beats"]),
        "cues": len(result.story_plan["narration_cues"]),
        "gaps": len(result.story_plan["unresolved_gaps"]),
        "validation_ok": report["ok"],
        "errors": report["errors"],
        "out_dir": str(Path(args.out_dir).resolve()),
    }, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def command_inject(args: argparse.Namespace) -> int:
    """用黄金 CandidateSet 生成 StoryPlan，替换进八文档 bundle 后跑官方校验器。"""
    docs = _docs(Path(args.bundle))
    brief, manifest, candidate_set = docs["Brief"], docs["AssetManifest"], docs["CandidateSet"]
    request = docs["RetrievalRequest"]
    default_fixture = Path(args.bundle).resolve() == DEFAULT_BUNDLE.resolve() and all(d.get("fixtures_only") is True for d in docs.values())
    approved = read_json(args.approved_cues) if args.approved_cues else (
        read_json(DEFAULT_APPROVED_CUES) if default_fixture and DEFAULT_APPROVED_CUES.is_file() else None
    )
    result = plan(
        brief, manifest, candidate_set, retrieval_request=request,
        selector=RuleSelector(), approved_cues=approved,
        reviewed_evidence=read_json(args.reviewed_evidence) if args.reviewed_evidence else None,
        broll_queries=False, plan_document_id="story-001", plan_revision=1,
    )
    report = validate_story_plan(
        result.story_plan, brief=brief, candidate_set=candidate_set, manifest=manifest,
        upstream=docs,
    )
    if not report["ok"] or result.story_plan["status"] != "ready_for_edit":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    raw, _ = load_bundle(Path(args.bundle))
    for doc in raw["documents"]:
        if doc["kind"] == "StoryPlan":
            doc.clear()
            doc.update(result.story_plan)
    target = Path(args.output)
    write_json(target, raw)
    validator = ROOT / "examples/team_contracts/validate.py"
    completed = subprocess.run(
        [sys.executable, str(validator), str(target), "--self-test"],
        capture_output=True, text=True, check=False,
    )
    print(completed.stdout.strip())
    if completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    print(json.dumps({
        "c_validator_ok": report["ok"],
        "injected_bundle": str(target.resolve()),
        "official_validator_exit": completed.returncode,
    }, ensure_ascii=False, indent=2))
    return completed.returncode


def command_validate(args: argparse.Namespace) -> int:
    docs = _docs(Path(args.bundle), Path(args.candidate_set) if args.candidate_set else None)
    plan_doc = read_json(args.plan)
    report = validate_story_plan(
        plan_doc, brief=docs["Brief"], candidate_set=docs["CandidateSet"],
        manifest=docs["AssetManifest"], upstream=docs,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def command_compare(args: argparse.Namespace) -> int:
    print(json.dumps(compare_plans(read_json(args.left), read_json(args.right)), ensure_ascii=False, indent=2))
    return 0


def command_selftest(args: argparse.Namespace) -> int:
    """固定响应测试校验器：五个负例必须被拒绝，一个正例必须通过。"""
    docs = _docs(DEFAULT_BUNDLE, DEFAULT_C_CANDIDATES)
    brief, manifest, candidate_set = docs["Brief"], docs["AssetManifest"], docs["CandidateSet"]
    outline = build_outline(brief)
    request = build_retrieval_request(brief, manifest, outline, broll_queries=True)
    beat = outline[0]
    candidates = candidates_for_beat(beat, request, candidate_set, manifest)
    checks: list[dict[str, Any]] = []

    def expect(label: str, payload: dict[str, Any], expect_code: str | None) -> None:
        selector = FixedResponseSelector(payload)
        try:
            selection = selector.select(beat=beat, candidates=candidates, context={"project_id": brief["project_id"]})
            validate_selection(selection, beat=beat, candidates=candidates)
            got = None
        except PlanningError as exc:
            got = exc.code
        checks.append({"case": label, "expected": expect_code, "got": got, "pass": got == expect_code})

    valid_ids = [c["candidate_id"] for c in candidates if c["origin"] == "user"][:1]
    expect("合法固定响应", {"beats": {"beat-open": {"shot_order": valid_ids, "reasons": {}, "roles": {}}}}, None)
    expect("非法候选 ID", {"beats": {"beat-open": {"shot_order": ["candidate-does-not-exist"]}}}, VLM_INVALID_PLAN)
    expect("重复候选 ID", {"beats": {"beat-open": {"shot_order": valid_ids + valid_ids}}}, VLM_INVALID_PLAN)
    expect("空镜冒充事件主镜头", {
        "beats": {"beat-open": {"shot_order": ["candidate-seg-forest"],
                                 "roles": {"candidate-seg-forest": "primary"}}},
    }, VLM_INVALID_PLAN)
    expect("空选择", {"beats": {"beat-open": {"shot_order": []}}}, VLM_INVALID_PLAN)

    stale = copy.deepcopy(docs["CandidateSet"])
    stale["inputs"][0]["revision"] = 999
    try:
        check_inputs(stale, {"AssetManifest": manifest, "RetrievalRequest": request})
        stale_code = None
    except PlanningError as exc:
        stale_code = exc.code
    checks.append({
        "case": "陈旧 revision", "expected": STALE_INPUT_REVISION,
        "got": stale_code, "pass": stale_code == STALE_INPUT_REVISION,
    })
    print(json.dumps({"checks": checks, "passed": sum(1 for c in checks if c["pass"]), "total": len(checks)},
                     ensure_ascii=False, indent=2))
    return 0 if all(c["pass"] for c in checks) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quickcut.planning", description="C：故事与视听规划")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="生成 RetrievalRequest 与 StoryPlan")
    run.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    run.add_argument("--candidate-set", default=None, help="覆盖 bundle 中的 CandidateSet")
    run.add_argument("--approved-cues", default=None)
    run.add_argument("--reviewed-evidence", default=None)
    run.add_argument("--mode", choices=["rule", "fixed", "ollama"], default="rule")
    run.add_argument("--fixed-response", default=None)
    run.add_argument("--case", choices=["complete", "insufficient"], default="complete")
    run.add_argument("--no-broll-queries", action="store_true")
    run.add_argument("--document-id", default="story-001")
    run.add_argument("--revision", type=int, default=1)
    run.add_argument("--out-dir", default="outputs/c")
    run.set_defaults(func=command_run)

    inject = sub.add_parser("inject", help="把 C 的 StoryPlan 注入八文档 bundle 并跑官方校验器")
    inject.add_argument("--approved-cues", default=None)
    inject.add_argument("--reviewed-evidence", default=None)
    inject.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    inject.add_argument("--output", default="outputs/c/injected_bundle.json")
    inject.set_defaults(func=command_inject)

    validate = sub.add_parser("validate", help="校验一份 StoryPlan")
    validate.add_argument("plan")
    validate.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    validate.add_argument("--candidate-set", default=None)
    validate.set_defaults(func=command_validate)

    compare = sub.add_parser("compare", help="比较两个故事方案")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.set_defaults(func=command_compare)

    selftest = sub.add_parser("selftest", help="固定响应与负例自检")
    selftest.set_defaults(func=command_selftest)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PlanningError as exc:
        print(json.dumps(exc.to_envelope(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (KeyError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "failed", "code": VLM_INVALID_PLAN, "message": str(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

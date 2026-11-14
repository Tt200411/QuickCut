"""Compare a CandidateSet with a labeled query list.

The report records which expected files were recalled and which labeled
queries stayed empty. It does not rewrite the ranking.
"""
from __future__ import annotations

from typing import Any, Callable


def recall_against_expectations(
    request: dict[str, Any],
    candidate_set: dict[str, Any],
    manifest: dict[str, Any],
    expectations: dict[str, Any],
    path_for_asset: Callable[[dict[str, Any]], str | None],
) -> dict[str, Any]:
    assets = {asset["asset_id"]: asset for asset in manifest.get("assets", [])}
    queries = {query["query_id"]: query for query in request.get("queries", [])}
    by_text: dict[str, list[dict[str, Any]]] = {}
    for result in candidate_set.get("results", []):
        query = queries.get(result["query_id"], {})
        by_text.setdefault(query.get("semantic_text", ""), []).append(result)

    cases = []
    for case in expectations.get("cases", []):
        text = case["query"]
        results = by_text.get(text, [])
        recalled: list[str] = []
        seen: set[str] = set()
        for result in results:
            for candidate in result.get("candidates", []):
                path = path_for_asset(assets.get(candidate["asset_id"], {}))
                if path and path not in seen:
                    seen.add(path)
                    recalled.append(path)
        if case.get("expected_result") == "evidence_gap":
            empty = bool(results) and all(result.get("status") == "empty" for result in results)
            cases.append({
                "query": text,
                "expected_result": "evidence_gap",
                "query_ids": [result["query_id"] for result in results],
                "status": "gap_confirmed" if empty else "gap_not_empty",
                "recalled_paths": recalled,
                "reason": case.get("reason", ""),
            })
            continue
        expected = list(case.get("expected_paths", []))
        excluded = list(case.get("exclude_as_event_evidence", []))
        missed = [path for path in expected if path not in recalled]
        unexpected = [path for path in recalled if path not in expected and path not in excluded]
        excluded_returned = [path for path in excluded if path in recalled]
        ranked_after_excluded = []
        for path in expected:
            if path not in recalled:
                continue
            ahead = [
                item for item in excluded
                if item in recalled and recalled.index(item) < recalled.index(path)
            ]
            if ahead:
                ranked_after_excluded.append({"path": path, "ranked_after": ahead})
        if not results:
            status = "untested"
        elif not missed and not unexpected and not ranked_after_excluded:
            status = "recalled"
        else:
            status = "partial"
        cases.append({
            "query": text,
            "expected_paths": expected,
            "recalled_paths": recalled,
            "missed_paths": missed,
            "unexpected_paths": unexpected,
            "excluded_as_event_evidence_but_returned": excluded_returned,
            "expected_ranked_after_excluded": ranked_after_excluded,
            "query_ids": [result["query_id"] for result in results],
            "status": status,
        })
    return {
        "status": "pass" if cases and all(
            item["status"] in {"recalled", "gap_confirmed"} for item in cases
        ) else "partial",
        "cases": cases,
    }

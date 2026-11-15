"""C 模块使用的 0.1.0 公共契约工具。

只依赖标准库，不修改 quickcut/types.py 或 core.py。六人共享的文档由 A-F
各自生产，这里只负责信封、精确版本引用、整数微秒时间与统一错误信封。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "0.1.0"
KINDS = {
    "Brief", "AssetManifest", "RetrievalRequest", "CandidateSet",
    "StoryPlan", "VoicePackage", "EditTimeline", "ExportManifest",
}
ORIGINS = {"user", "external", "generated"}
ROLES = {"primary", "establishing", "transition", "atmosphere", "detail", "broll"}

# 契约第 6 节的错误码。C 只会主动抛自己负责的几种。
NO_CANDIDATE = "NO_CANDIDATE"
UNSUPPORTED_STORY_BEAT = "UNSUPPORTED_STORY_BEAT"
VLM_INVALID_PLAN = "VLM_INVALID_PLAN"
STALE_INPUT_REVISION = "STALE_INPUT_REVISION"


class PlanningError(Exception):
    """可序列化成模块统一错误信封的规划期错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "planning",
        project_id: str = "",
        affected_ids: Iterable[str] = (),
        retryable: bool = True,
        action_owner: str = "C",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.project_id = project_id
        self.affected_ids = list(affected_ids)
        self.retryable = retryable
        self.action_owner = action_owner
        self.detail = detail or {}

    def to_envelope(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "code": self.code,
            "stage": self.stage,
            "project_id": self.project_id,
            "message": self.message,
            "retryable": self.retryable,
            "affected_ids": self.affected_ids,
            "action_owner": self.action_owner,
            "detail": self.detail,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_envelope()


def envelope(
    kind: str,
    document_id: str,
    project_id: str,
    revision: int,
    *,
    fixtures_only: bool,
    owner: str,
    inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown document kind: {kind}")
    if type(revision) is not int or revision < 1:
        raise ValueError("revision 必须是正整数")
    if type(fixtures_only) is not bool:
        raise ValueError("fixtures_only 必须是 bool")
    if any(not isinstance(value, str) or not value.strip() for value in (document_id, project_id)):
        raise ValueError("document_id 和 project_id 必须是非空字符串")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "document_id": document_id,
        "project_id": project_id,
        "revision": revision,
        "fixtures_only": fixtures_only,
        "owner": owner,
        "inputs": list(inputs),
    }


def ref(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": doc["kind"],
        "document_id": doc["document_id"],
        "revision": doc["revision"],
    }


def load_bundle(path: str | Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """读取八文档 bundle，返回 (原始 bundle, 按 kind 索引的文档)。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("bundle 必须是 JSON 对象")
    documents = raw.get("documents", [raw] if raw.get("kind") else None)
    if not isinstance(documents, list) or not documents:
        raise ValueError("bundle.documents 必须是非空列表")
    docs: dict[str, dict[str, Any]] = {}
    for doc in documents:
        if not isinstance(doc, dict) or not isinstance(doc.get("kind"), str) or doc["kind"] not in KINDS:
            raise ValueError("bundle 包含非法文档")
        kind = doc["kind"]
        if kind in docs:
            raise ValueError(f"duplicate document kind in bundle: {kind}")
        docs[kind] = doc
    return raw, docs


def check_envelope(doc: dict[str, Any], *, expected_kind: str, owner: str) -> None:
    if not isinstance(doc, dict):
        raise PlanningError(STALE_INPUT_REVISION, "文档必须是对象", retryable=False)
    if (type(doc.get("fixtures_only")) is not bool or any(
        not isinstance(doc.get(key), str) or not doc[key].strip()
        for key in ("document_id", "project_id")
    )):
        raise PlanningError(
            STALE_INPUT_REVISION, f"{expected_kind} 的标识或 fixtures_only 不合法", retryable=False,
        )
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise PlanningError(
            STALE_INPUT_REVISION,
            f"{expected_kind} 的 schema_version 不是 {SCHEMA_VERSION}",
            project_id=doc.get("project_id", ""),
            retryable=False,
            detail={"got": doc.get("schema_version")},
        )
    if doc.get("kind") != expected_kind or doc.get("owner") != owner:
        raise PlanningError(
            STALE_INPUT_REVISION,
            f"期望 {owner} 生产的 {expected_kind}，实际是 {doc.get('owner')}/{doc.get('kind')}",
            project_id=doc.get("project_id", ""),
            retryable=False,
        )
    if type(doc.get("revision")) is not int or doc["revision"] < 1:
        raise PlanningError(
            STALE_INPUT_REVISION,
            f"{expected_kind} 缺少合法 revision",
            project_id=doc.get("project_id", ""),
            retryable=False,
        )


def check_inputs(doc: dict[str, Any], available: dict[str, dict[str, Any]]) -> None:
    """消费者校验：每个输入必须带精确 document_id 与 revision。"""
    required = {
        "RetrievalRequest": {"Brief", "AssetManifest"},
        "CandidateSet": {"RetrievalRequest", "AssetManifest"},
        "StoryPlan": {"Brief", "AssetManifest", "CandidateSet"},
    }.get(doc.get("kind"), set())
    links = doc.get("inputs")
    if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
        raise PlanningError(STALE_INPUT_REVISION, "inputs 必须是引用列表", retryable=False)
    kinds = [link.get("kind") for link in links]
    if any(not isinstance(kind, str) for kind in kinds) or len(set(kinds)) != len(kinds) or not required <= set(kinds):
        raise PlanningError(STALE_INPUT_REVISION, "inputs 缺少必需依赖或包含重复引用", retryable=False)
    for link in links:
        kind = link.get("kind")
        target = available.get(kind)
        if target is None:
            raise PlanningError(
                STALE_INPUT_REVISION,
                f"缺少上游文档 {kind}",
                project_id=doc.get("project_id", ""),
                affected_ids=[doc.get("document_id", "")],
                retryable=False,
            )
        check_project(doc, target)
        if (target.get("kind") != kind or type(link.get("revision")) is not int
                or type(target.get("revision")) is not int or link["revision"] < 1
                or not isinstance(link.get("document_id"), str) or not link["document_id"].strip()
                or link.get("document_id") != target["document_id"]
                or link.get("revision") != target["revision"]):
            raise PlanningError(
                STALE_INPUT_REVISION,
                f"{kind} 已变更：本次引用 r{link.get('revision')}，当前 r{target['revision']}",
                project_id=doc.get("project_id", ""),
                affected_ids=[kind],
                retryable=False,
            )


def check_project(*docs: dict[str, Any]) -> str:
    if not docs or any(not isinstance(doc.get("project_id"), str) or not doc["project_id"].strip() for doc in docs):
        raise PlanningError(STALE_INPUT_REVISION, "project_id 必须是非空字符串", retryable=False)
    project_ids = {doc["project_id"] for doc in docs}
    if len(project_ids) != 1 or not next(iter(project_ids)):
        raise PlanningError(
            STALE_INPUT_REVISION,
            "输入文档不属于同一个 project_id",
            project_id=str(sorted(str(x) for x in project_ids)),
            retryable=False,
        )
    return str(next(iter(project_ids)))


def require_us(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise PlanningError(
            VLM_INVALID_PLAN,
            f"{label} 必须是非负整数微秒",
            retryable=False,
            detail={"value": value},
        )
    return value


def split_budget(total_us: int, weights: list[float]) -> list[int]:
    """把总预算按权重切成整数微秒，余数落在最后一段，保证总和精确。"""
    require_us(total_us, "target_duration_us")
    if not weights or sum(weights) <= 0:
        raise ValueError("weights must be positive")
    normalized = [w / sum(weights) for w in weights]
    parts = [int(total_us * w) for w in normalized[:-1]]
    parts.append(total_us - sum(parts))
    return parts


def content_hash(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))

"""镜头选择与严格校验。

模型只负责在候选白名单内提出顺序与理由；合法性、来源角色、事件证据和
时间预算由程序检查。模型输出非法时返回修正请求，不静默替换事件角色。
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .contracts import PlanningError, UNSUPPORTED_STORY_BEAT, VLM_INVALID_PLAN


@dataclass
class Selection:
    beat_id: str
    ordered_candidate_ids: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    source: str = "rule"
    raw_response: dict[str, Any] | None = None
    role_overrides: dict[str, str] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "ordered_candidate_ids": self.ordered_candidate_ids,
            "reasons": self.reasons,
            "source": self.source,
            "raw_response": self.raw_response,
        }


class Selector(Protocol):
    name: str

    def select(self, *, beat: dict[str, Any], candidates: list[dict[str, Any]], context: dict[str, Any]) -> Selection:
        ...


def _invalid_plan(
    message: str, *, beat: dict[str, Any], raw: Any,
    context: dict[str, Any] | None = None, problems: list[str] | None = None,
) -> PlanningError:
    return PlanningError(
        VLM_INVALID_PLAN, message,
        project_id=(context or {}).get("project_id") or beat.get("project_id", ""),
        affected_ids=[beat["beat_id"]] if "beat_id" in beat else [],
        detail={"beat_id": beat.get("beat_id", ""), "raw_response": raw,
                "problems": problems or [message]},
    )


def _plan_fields(entry: Any, *, beat: dict[str, Any], context: dict[str, Any], raw: Any) -> None:
    if not isinstance(entry, dict):
        raise _invalid_plan("模型选镜结果必须是 JSON object", beat=beat, context=context, raw=raw)
    order = entry.get("shot_order")
    if not isinstance(order, list) or any(not isinstance(item, str) for item in order):
        raise _invalid_plan("shot_order 必须是字符串列表", beat=beat, context=context, raw=raw)
    for name in ("reasons", "roles"):
        values = entry.get(name, {})
        if not isinstance(values, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()
        ):
            raise _invalid_plan(f"{name} 必须是字符串字典", beat=beat, context=context, raw=raw)


def _broll_allowed(beat: dict[str, Any], context: dict[str, Any]) -> bool:
    return all(bool(value) for value in (
        context.get("broll_enabled", True), context.get("broll_allowed", True),
        beat.get("broll_allowed", True),
    ))


def _check_broll(
    selection: Selection, *, beat: dict[str, Any], candidates: list[dict[str, Any]],
    context: dict[str, Any],
) -> None:
    if not _broll_allowed(beat, context):
        blocked = {c["candidate_id"] for c in candidates if c["origin"] != "user"}
        if blocked.intersection(selection.ordered_candidate_ids):
            raise _invalid_plan("该段禁止选用外部或生成空镜", beat=beat, context=context,
                                raw=selection.raw_response)


def _rank(candidate: dict[str, Any]) -> float:
    return float(candidate.get("ranking_score") or 0.0)


class RuleSelector:
    """无需模型的确定性选镜，也是模型不可用时的显式降级路径。

    它不会假装是 VLM 输出，selection.source 恒为 "rule"。
    """

    name = "rule"

    def select(self, *, beat: dict[str, Any], candidates: list[dict[str, Any]], context: dict[str, Any]) -> Selection:
        budget = beat.get("duration_budget_us")
        if type(budget) is not int or budget <= 0:
            raise _invalid_plan("beat.duration_budget_us 必须是正整数微秒", beat=beat,
                                context=context, raw=None)
        user = sorted(
            [c for c in candidates if c["origin"] == "user" and "bad_evidence" not in c.get("flags", [])],
            key=_rank, reverse=True,
        )
        external = sorted(
            [c for c in candidates if c["origin"] == "external" and "bad_evidence" not in c.get("flags", [])],
            key=_rank, reverse=True,
        )
        chosen: list[dict[str, Any]] = []
        reasons: dict[str, str] = {}
        needs = set(beat.get("shot_needs", ["primary"]))

        if user:
            chosen.append(user[0])
            reasons[user[0]["candidate_id"]] = "用户素材中排名最高的主镜头，作为该段事件证据"
        if external and _broll_allowed(beat, context) and context.get("broll_enabled") and needs & {"establishing", "transition", "atmosphere", "detail", "broll"}:
            if external[0]["candidate_id"] not in {c["candidate_id"] for c in chosen}:
                chosen.append(external[0])
                reasons[external[0]["candidate_id"]] = "授权空镜承担环境或情绪职责，不声明为用户实际经历"

        # 主镜头在前、空镜在后是规则默认；建立环境的空镜需要提前。
        if len(chosen) == 2 and "establishing" in needs:
            chosen.reverse()

        ids = [c["candidate_id"] for c in chosen]
        return Selection(
            beat_id=beat["beat_id"],
            ordered_candidate_ids=ids,
            reasons=reasons,
            source="rule",
            role_overrides={
                c["candidate_id"]: (
                    "primary" if c["origin"] == "user" else
                    c["suggested_role"] if c.get("suggested_role") in {
                        "establishing", "transition", "atmosphere", "detail", "broll",
                    } else "broll"
                )
                for c in chosen
            },
        )


class FixedResponseSelector:
    """读取固定模型响应，用来在接入真实推理之前测试校验器。

    响应格式：{"beats": {"<beat_id>": {"shot_order": [...], "reasons": {...},
    "roles": {...}}}, "mode": "fixed_model_response"}
    """

    name = "fixed_response"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self._parse_error: str | None = None

    @classmethod
    def from_path(cls, path: str | Path) -> "FixedResponseSelector":
        raw = Path(path).read_text(encoding="utf-8")
        try:
            return cls(json.loads(raw))
        except ValueError:
            selector = cls(raw)
            selector._parse_error = "固定响应不是合法 JSON"
            return selector

    def select(self, *, beat: dict[str, Any], candidates: list[dict[str, Any]], context: dict[str, Any]) -> Selection:
        if self._parse_error or not isinstance(self.payload, dict):
            raise _invalid_plan(self._parse_error or "固定响应必须是 JSON object",
                                beat=beat, context=context, raw=self.payload)
        beats = self.payload.get("beats")
        if not isinstance(beats, dict):
            raise _invalid_plan("固定响应 beats 必须是 JSON object", beat=beat,
                                context=context, raw=self.payload)
        entry = beats.get(beat["beat_id"])
        _plan_fields(entry, beat=beat, context=context, raw=self.payload)
        selection = Selection(
            beat_id=beat["beat_id"],
            ordered_candidate_ids=list(entry["shot_order"]),
            reasons=dict(entry.get("reasons", {})),
            source=f"fixture:{self.payload.get('mode', 'fixed_model_response')}",
            raw_response=dict(self.payload),
            role_overrides=dict(entry.get("roles", {})),
        )
        _check_broll(selection, beat=beat, candidates=candidates, context=context)
        return selection


class OllamaSelector:
    """把有限的真实候选、源时间、ASR 证据和段落目标交给本地 Qwen。

    用标准库 urllib 调用 Ollama /api/generate；模型名、地址、超时来自参数或
    环境变量。关键帧只有文件确实存在时才会附上，夹具路径不会被当成真实证据。
    """

    name = "ollama"

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        self.model = model or os.getenv("QUICKCUT_VLM_MODEL", "qwen2.5vl:3b")
        self.base_url = (base_url or os.getenv("QUICKCUT_OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.timeout = float(timeout or os.getenv("QUICKCUT_VLM_TIMEOUT", "120"))
        self.project_root = Path(project_root) if project_root else None

    def _images(
        self, candidates: list[dict[str, Any]], *, image_mapping: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        images: list[str] = []
        if not self.project_root:
            return images
        from PIL import Image

        root = self.project_root.resolve()
        for candidate in candidates:
            frames = {frame["uri"]: frame for frame in candidate.get("keyframes", [])}
            source_range = candidate.get("source_range_us")
            if "source_range_us" in candidate and (
                not isinstance(source_range, dict)
                or type(source_range.get("begin")) is not int
                or type(source_range.get("end")) is not int
                or not 0 <= source_range["begin"] < source_range["end"]
            ):
                continue
            # 显式 URI 列表决定附图顺序；旧调用也可只提供 keyframes。
            for uri in candidate.get("keyframe_uris", list(frames)):
                if not isinstance(uri, str):
                    continue
                frame = frames.get(uri, {})
                source_time = frame.get("source_time_us")
                if "source_time_us" in frame and (type(source_time) is not int or source_time < 0):
                    continue
                # 有区间时必须能定位到区间内；无区间的旧调用保留未知时间 null。
                if source_range is not None and (
                    source_time is None or not source_range["begin"] <= source_time < source_range["end"]
                ):
                    continue
                relative = Path(uri)
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                try:
                    path = (root / relative).resolve()
                    if not path.is_relative_to(root) or not path.is_file():
                        continue
                    data = path.read_bytes()
                    with Image.open(io.BytesIO(data)) as image:
                        image.verify()
                    with Image.open(io.BytesIO(data)) as image:
                        image.load()
                except (OSError, ValueError, SyntaxError, RuntimeError, Image.DecompressionBombError):
                    continue
                if image_mapping is not None:
                    image_mapping.append({
                        "image_index": len(images),
                        "candidate_id": candidate["candidate_id"],
                        "uri": uri,
                        "source_time_us": source_time,
                        "source_range_us": source_range,
                    })
                images.append(base64.b64encode(data).decode("ascii"))
        return images

    def select(self, *, beat: dict[str, Any], candidates: list[dict[str, Any]], context: dict[str, Any]) -> Selection:
        allowed = [c["candidate_id"] for c in candidates]
        if not allowed:
            return Selection(beat_id=beat["beat_id"], source=self.name)
        images_mapping: list[dict[str, Any]] = []
        images = self._images(candidates, image_mapping=images_mapping)
        evidence = [
            {
                "candidate_id": c["candidate_id"],
                "segment_id": c["segment_id"],
                "origin": c["origin"],
                "source_range_us": c.get("source_range_us"),
                "transcript": c.get("transcript", ""),
                "visual_evidence": any(m["candidate_id"] == c["candidate_id"] for m in images_mapping),
                "image_indices": [m["image_index"] for m in images_mapping
                                  if m["candidate_id"] == c["candidate_id"]],
                "selectable": c["origin"] == "user" or _broll_allowed(beat, context),
            }
            for c in candidates
        ]
        instruction = {
            "task": (
                "作为剪辑师，只从下面给出的真实候选中选镜头并排序。"
                "只有 user 来源可作为 primary 事件证据；external/generated 只能承担空镜职责。"
                "不要选择 selectable=false 的候选。visual_evidence=false 表示没有实际附图，不能臆测画面。"
                "beat.wish 仅是未验证的用户愿望，不能视为已发生的事实。"
                "image_indices 对应 images 的零基索引，image_mapping 给出已知源时间；null 表示未知。"
                "不要输出候选列表之外的 ID，不要重复 ID，不要编造动作或因果关系。"
            ),
            "beat": {
                "beat_id": beat["beat_id"],
                "purpose": beat["purpose"],
                "duration_budget_us": beat["duration_budget_us"],
                "shot_needs": beat.get("shot_needs", []),
                "wish": beat.get("wish"),
                "broll_allowed": _broll_allowed(beat, context),
            },
            "candidates": evidence,
            "image_mapping": images_mapping,
            "required_output": {
                "shot_order": ["candidate_id"],
                "reasons": {"candidate_id": "为什么选它"},
                "roles": {"candidate_id": "primary | establishing | transition | atmosphere | broll"},
            },
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": json.dumps(instruction, ensure_ascii=False),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        if images:
            payload["images"] = images
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        raw_body: Any = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read().decode("utf-8")
            body = json.loads(raw_body)
        except Exception as exc:
            raise _invalid_plan(
                f"本地 Ollama 调用或响应解析失败（{self.model} @ {self.base_url}）：{exc}",
                beat=beat, context=context, raw=raw_body,
            ) from exc
        if not isinstance(body, dict):
            raise _invalid_plan("Ollama 响应必须是 JSON object", beat=beat, context=context, raw=body)
        raw_text = body.get("response")
        try:
            parsed = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
        except ValueError as exc:
            raise _invalid_plan("本地模型返回内容不是合法 JSON", beat=beat,
                                context=context, raw=raw_text) from exc
        _plan_fields(parsed, beat=beat, context=context, raw=raw_text)
        selection = Selection(
            beat_id=beat["beat_id"],
            ordered_candidate_ids=list(parsed["shot_order"]),
            reasons=dict(parsed.get("reasons", {})),
            source=f"model:{self.model}",
            raw_response={"endpoint": "/api/generate", "model": self.model, "body": body, "parsed": parsed},
            role_overrides=dict(parsed.get("roles", {})),
        )
        _check_broll(selection, beat=beat, candidates=candidates, context=context)
        return selection


def validate_selection(
    selection: Selection,
    *,
    beat: dict[str, Any],
    candidates: list[dict[str, Any]],
    max_broll_ratio: float = 0.4,
) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (按角色与预算整理后的镜头记录, 规则修正说明)。

    任何非法 ID、重复、来源错配或超预算都抛 VLM_INVALID_PLAN，并把原始响应
    附在错误信封里，供 C 发起修正请求而非静默改写。
    """
    _plan_fields(
        {"shot_order": selection.ordered_candidate_ids, "reasons": selection.reasons,
         "roles": selection.role_overrides},
        beat=beat, context={}, raw=selection.raw_response,
    )
    if any(not isinstance(c.get("candidate_id"), str) for c in candidates):
        raise _invalid_plan("候选 ID 必须是字符串", beat=beat, raw=selection.raw_response)
    by_id = {c["candidate_id"]: c for c in candidates}
    budget = beat.get("duration_budget_us")
    if type(budget) is not int or budget <= 0:
        raise _invalid_plan("beat.duration_budget_us 必须是正整数微秒", beat=beat,
                            raw=selection.raw_response)
    if (type(max_broll_ratio) not in (int, float) or not math.isfinite(max_broll_ratio)
            or not 0 <= max_broll_ratio <= 1):
        raise _invalid_plan("max_broll_ratio 必须是 0 到 1 之间的有限数值", beat=beat,
                            raw=selection.raw_response)
    problems: list[str] = []

    if not selection.ordered_candidate_ids:
        problems.append("模型没有选出任何镜头")
    seen: set[str] = set()
    for candidate_id in selection.ordered_candidate_ids:
        if candidate_id not in by_id:
            problems.append(f"非法候选 ID：{candidate_id}")
        elif candidate_id in seen:
            problems.append(f"候选重复：{candidate_id}")
        seen.add(candidate_id)
    if problems:
        raise _invalid_plan("镜头选择含非法或重复 ID，需要重新规划", beat=beat,
                            raw=selection.raw_response, problems=problems)
    _check_broll(selection, beat=beat, candidates=candidates, context={})

    records: list[dict[str, Any]] = []
    for candidate_id in selection.ordered_candidate_ids:
        candidate = by_id[candidate_id]
        origin = candidate["origin"]
        declared = selection.role_overrides.get(candidate_id) or candidate.get("suggested_role") or ""
        if origin not in {"user", "external", "generated"}:
            problems.append(f"候选 {candidate_id} 来源非法：{origin}")
        if origin != "user":
            if declared == "primary":
                problems.append(f"非用户素材 {candidate_id} 被标为证明用户事件的主镜头")
            role = declared if declared in {"establishing", "transition", "atmosphere", "detail", "broll"} else "broll"
        else:
            role = "primary"
        if "bad_evidence" in candidate.get("flags", []):
            problems.append(f"关键帧不完整或证据被标记的候选 {candidate_id} 不能选用")
        duration = candidate.get("duration_us")
        if type(duration) is not int or duration <= 0:
            problems.append(f"候选 {candidate_id} duration_us 必须是正整数微秒")
        if problems:
            continue
        records.append({
            "candidate_id": candidate_id,
            "segment_id": candidate["segment_id"],
            "origin": origin,
            "role": role,
            "candidate_duration_us": candidate["duration_us"],
            "reason": selection.reasons.get(candidate_id, ""),
            "decided_by": selection.source,
        })
    if problems:
        raise _invalid_plan("镜头角色、来源或时长非法，需要重新规划", beat=beat,
                            raw=selection.raw_response, problems=problems)

    primary_count = sum(1 for r in records if r["origin"] == "user")
    if primary_count == 0:
        raise PlanningError(
            UNSUPPORTED_STORY_BEAT,
            f"段落 {beat['beat_id']} 缺少用户素材事件证据，空镜不能替代具体事件",
            project_id=beat.get("project_id", ""),
            affected_ids=[beat["beat_id"]],
            detail={"candidate_ids": selection.ordered_candidate_ids, "raw_response": selection.raw_response},
        )

    # 按比例上限分配空镜预算，默认给主镜头保留至少 60%。
    external_count = len(records) - primary_count
    if external_count > primary_count:
        raise _invalid_plan("空镜数量超过主镜头，故事可能被环境画面填满", beat=beat,
                            raw=selection.raw_response)
    external_total = min(int(budget * max_broll_ratio), external_count * 3_000_000)
    primary_total = budget - external_total
    for record in records:
        pool, count = (primary_total, primary_count) if record["origin"] == "user" else (external_total, external_count)
        suggested = min(record["candidate_duration_us"], pool // count) if count else 0
        record["suggested_duration_us"] = int(suggested)
    # 按选镜顺序补给主镜头，直到预算耗尽或所有用户素材容量用完。
    remaining = budget - sum(r["suggested_duration_us"] for r in records)
    for record in records:
        if remaining <= 0:
            break
        if record["origin"] == "user":
            headroom = max(0, record["candidate_duration_us"] - record["suggested_duration_us"])
            added = min(remaining, headroom)
            record["suggested_duration_us"] += added
            remaining -= added
    if (any(r["suggested_duration_us"] <= 0 for r in records)
            or sum(r["suggested_duration_us"] for r in records) > budget):
        raise _invalid_plan("镜头建议时长必须为正整数且总和不得超过段落预算", beat=beat,
                            raw=selection.raw_response)

    corrections: list[str] = []
    if selection.source.startswith("model:") or selection.source.startswith("fixture:"):
        for record in records:
            if record["origin"] == "external" and record["role"] == "broll" and not record["reason"]:
                corrections.append(f"{record['candidate_id']} 缺少空镜职责说明，由规则补写")
    return records, corrections

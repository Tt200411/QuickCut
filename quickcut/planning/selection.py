"""镜头选择与严格校验。

模型只负责在候选白名单内提出顺序与理由；合法性、来源角色、事件证据和
时间预算由程序检查。模型输出非法时返回修正请求，不静默替换事件角色。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from time import perf_counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .contracts import PlanningError, UNSUPPORTED_STORY_BEAT, VLM_INVALID_PLAN


def narration_character_limit(budget_us: int) -> int:
    """Legacy name: conservative speech-length reference, never a validity limit.

    Keep the estimator aligned with narration_review and the workbench display.
    Complete sentences may exceed this reference; actual TTS decides duration.
    """
    return max(2, int(max(0, budget_us / 1_000_000 - 0.8) * 2.5))


def narration_character_count(text: str) -> int:
    return sum(char.isalnum() for char in text)


def narration_review(text: str, budget_us: int, *, max_chars: int | None = None) -> dict[str, Any]:
    """Content issues require editing; only explicit confirmation can approve."""
    limit = narration_character_limit(budget_us) if max_chars is None else max_chars
    narration = text.strip()
    count = narration_character_count(narration)
    warnings, advisories = [], []
    estimated_us = round((count / 2.5 + 0.8) * 1_000_000) if count else 0
    if len(text) > 160:
        warnings.append({"code": "NARRATION_EDITOR_LIMIT", "message": "旁白超过编辑安全上限160字符，请重写完整短句，不要截字。"})
    if not narration:
        warnings.append({"code": "NARRATION_EMPTY", "message": "请填写完整中文旁白后再确认。"})
    else:
        if count > limit:
            advisories.append({"code": "NARRATION_DURATION_ESTIMATE", "message": f"当前{count}个朗读字，超过粗估参考{limit}字；可确认后配音实测，必要时完整改写或调整画面，不能截尾。"})
        if not any("\u4e00" <= char <= "\u9fff" for char in narration):
            warnings.append({"code": "NARRATION_NON_CHINESE", "message": "请将旁白改成与画面相符的中文完整句。"})
        sentence = narration.rstrip("\"'”’」』》）】)")
        if not sentence.endswith(("。", "！", "？", ".", "!", "?")):
            warnings.append({"code": "NARRATION_INCOMPLETE_SENTENCE", "message": "旁白缺少句末标点，可能是残句；请核对并改成完整句后确认。"})
        else:
            # Deliberately narrow heuristics, not a Chinese grammar or visual judge.
            # A period must not hide an obvious unfinished quantity/object phrase.
            content = sentence.rstrip("。！？.!?").rstrip()
            if (not content or content.endswith(("，", "、", "：", "；", ",", ":", ";", "…"))
                    or re.fullmatch(
                        r"(?:(?:我们|我|大家|咱们)(?:先|再|也)?(?:去(?:超市|商店))?(?:买|带|拿)"
                        r"|(?:也)?别忘了(?:多)?带)(?:了)?(?:些|几|一些|几个|几件|几瓶)", content)
                    or re.search(r"[，,](?:展示|体现|以及|并且|但是|从而)$", content)
                    or re.fullmatch(r"(?:餐厅|店|室|车厢)(?:里|内|内部)(?:热闹|温馨|安静)的氛围", content)):
                warnings.append({"code": "NARRATION_INCOMPLETE_SENTENCE", "message": "旁白疑似在数量、连接词或逗号后中断；请重写完整短句，仅补句号不能修复残句。"})
    return {"status": "requires_edit" if warnings else "pending_confirmation", "warnings": warnings,
            "advisories": advisories, "estimated_duration_us": estimated_us,
            "estimate_basis": "按2.5字/秒加0.8秒粗估；汉字、字母、数字逐字符计数，不含标点；以配音实测为准",
            "character_count": count, "max_chars": limit, "approved": False}


def open_ollama(request: urllib.request.Request, *, timeout: float):
    """Never send local source frames through environment/system HTTP proxies."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)


@dataclass
class Selection:
    beat_id: str
    ordered_candidate_ids: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    source: str = "rule"
    raw_response: dict[str, Any] | None = None
    role_overrides: dict[str, str] = field(default_factory=dict)
    narration_draft: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "ordered_candidate_ids": self.ordered_candidate_ids,
            "reasons": self.reasons,
            "source": self.source,
            "raw_response": self.raw_response,
            "role_overrides": self.role_overrides,
            "narration_draft": self.narration_draft,
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
    if not context.get("thematic_montage") and not _broll_allowed(beat, context):
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
        archive_dir: str | Path | None = None,
    ) -> None:
        self.model = model or os.getenv("QUICKCUT_VLM_MODEL", "qwen2.5vl:3b")
        self.base_url = (base_url or os.getenv("QUICKCUT_OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.timeout = float(timeout or os.getenv("QUICKCUT_VLM_TIMEOUT", "120"))
        self.project_root = Path(project_root) if project_root else None
        self.archive_dir = Path(archive_dir) if archive_dir else None

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
        attempts = []
        limit = 2 if context.get("draft_narration") else 1
        for number in range(1, limit + 1):
            attempt_context = {**context, "format_attempt": number}
            if attempts:
                attempt_context["repair_instruction"] = "上一次输出格式不完整。必须返回全部 required 字段，reasons/roles 只写所选短ID。错误：" + str(attempts[-1].get("validation_error", ""))[:250]
                attempt_context["previous_attempt_archive"] = attempts[-1].get("archive_path")
            try:
                selection = self._select_once(beat=beat, candidates=candidates, context=attempt_context)
                if attempts:
                    selection.raw_response["previous_attempts"] = attempts
                    path = selection.raw_response.get("archive_path")
                    if path:
                        Path(path).write_text(json.dumps(selection.raw_response, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                if context.get("draft_narration") and selection.ordered_candidate_ids:
                    self._repair_narration(selection, beat=beat, candidates=candidates, context=context)
                return selection
            except PlanningError as exc:
                if not exc.detail.get("model_format_error"):
                    raise
                attempts.append(exc.detail["attempt_record"])
                if number == limit:
                    exc.detail["attempts"] = attempts
                    raise
            except OSError as exc:
                # Evidence is required for production drafts. A failed write must
                # stop the run, rather than returning an unarchived approval input.
                error = _invalid_plan("规划证据归档读写失败，请检查目录权限和可用空间后重试",
                                      beat=beat, context=context, raw=None)
                error.detail.update(archive_io_error=True, archive_dir=str(self.archive_dir),
                                    error=str(exc))
                raise error from exc
        raise AssertionError("unreachable")

    def _repair_narration(self, selection: Selection, *, beat, candidates, context) -> None:
        """Rewrite text only, at most twice; keep the original selection/response intact."""
        current = selection.narration_draft
        review = narration_review(current, beat["duration_budget_us"])
        if not review["warnings"] and not review["advisories"]:
            return
        by_id = {c["candidate_id"]: c for c in candidates}
        selected = [by_id[cid] for cid in selection.ordered_candidate_ids]
        mapping = []
        images = self._images(selected, image_mapping=mapping)
        records = []
        original = current
        visual_missing = bool(context.get("require_visual")) and not set(selection.ordered_candidate_ids).issubset(
            {item["candidate_id"] for item in mapping})
        if visual_missing:
            # Never replace visually grounded narration with a blind rewrite.
            selection.raw_response["narration_rewrite_skip_reason"] = "selected_visual_evidence_unavailable"
        for number in range(1, 1 if visual_missing else 3):
            instruction = {
                "task": "只重写本段旁白，不重新选镜、不改变镜头顺序或角色。依据所附已选画面，写一句自然中文完整句。"
                        "优先说完一个有画面支持的信息，避免名词短语、虚构动作、人物关系或用户经历。"
                        "上一句可能在末尾中断，也可能只是过长；必须重写整句，不能截字或只补句号。"
                        "字数仅为时长粗估，不是硬上限。短句已经完整就不要凑字数；完整表达优先。以。！？结束。"
                        "只返回narration_draft；这是待审草稿，不代表批准或实测合格。",
                "purpose": beat["purpose"], "story_theme": context.get("story_theme", ""),
                "previous_text": current, "previous_tail": current[-16:],
                "issues": review["warnings"] + review["advisories"],
                "duration_budget_us": beat["duration_budget_us"],
                "reference_chars": narration_character_limit(beat["duration_budget_us"]),
                "selected_candidate_ids": selection.ordered_candidate_ids,
                "image_mapping": mapping,
                "evidence": [{"candidate_id": c["candidate_id"], "origin": c["origin"],
                              "source_range_us": c.get("source_range_us")} for c in selected],
            }
            if records:
                previous = records[-1]
                instruction["previous_rewrite_feedback"] = {
                    "error": previous.get("error"), "accepted": previous.get("accepted", False),
                    "rejected_text": previous.get("parsed", {}).get("narration_draft"),
                    "review": previous.get("review"),
                }
            payload = {"model": self.model, "prompt": json.dumps(instruction, ensure_ascii=False),
                       "stream": False, "format": {"type": "object", "additionalProperties": False,
                       "properties": {"narration_draft": {"type": "string", "minLength": 1}},
                       "required": ["narration_draft"]},
                       "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 512},
                       "keep_alive": context.get("keep_alive", "0s")}
            if images:
                payload["images"] = images
            record = {"kind": "narration_rewrite", "rewrite_attempt": number, "model": self.model,
                      "request": {"payload": payload, "started_at": datetime.now(timezone.utc).isoformat()},
                      "inference_executed": False, "status": "requested"}
            path = None
            if self.archive_dir:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                call_id = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
                path = self.archive_dir / f"narration-{call_id}.json"
                record["archive_path"] = str(path.resolve())
                path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            started = perf_counter()
            try:
                request = urllib.request.Request(f"{self.base_url}/api/generate",
                    data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
                with open_ollama(request, timeout=self.timeout) as response:
                    record["raw_body"] = response.read().decode("utf-8")
                body = json.loads(record["raw_body"])
                record["body"] = body
                record["inference_executed"] = isinstance(body, dict) and body.get("done") is True
                if not record["inference_executed"]:
                    raise ValueError("旁白重写响应未完成")
                parsed = json.loads(body["response"]) if isinstance(body.get("response"), str) else body.get("response")
                if (not isinstance(parsed, dict) or set(parsed) != {"narration_draft"}
                        or not isinstance(parsed["narration_draft"], str)):
                    raise ValueError("旁白重写只能返回narration_draft字符串")
                proposed = parsed["narration_draft"]
                proposed_review = narration_review(proposed, beat["duration_budget_us"])
                record.update(status="reviewed", parsed=parsed, review=proposed_review, accepted=False)
                # Never replace a complete sentence with a fragment or a longer over-budget version.
                score = lambda r: (bool(r["warnings"]), len(r["warnings"]), bool(r["advisories"]), r["character_count"])
                if not proposed_review["warnings"] and score(proposed_review) < score(review):
                    current, review = proposed, proposed_review
                    record["accepted"] = True
            except (ValueError, OSError) as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    try:
                        record["raw_body"] = exc.read().decode("utf-8", errors="replace")
                    except OSError:
                        pass
                record.update(status="failed", error=str(exc),
                              retryable=isinstance(exc, ValueError))
            record["elapsed_seconds"] = perf_counter() - started
            records.append(record)
            if path:
                path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            if (record["status"] == "failed" and not record.get("retryable")) or (not review["warnings"] and not review["advisories"]):
                break
        selection.narration_draft = current
        selection.raw_response["narration_rewrites"] = records
        selection.raw_response["narration_result"] = {"original_text": original, "text": current,
            "text_sha256": hashlib.sha256(current.encode("utf-8")).hexdigest(), "review": review,
            "approved": False}
        archive = selection.raw_response.get("archive_path")
        if archive:
            Path(archive).write_text(json.dumps(selection.raw_response, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def _select_once(self, *, beat: dict[str, Any], candidates: list[dict[str, Any]], context: dict[str, Any]) -> Selection:
        allowed = [c["candidate_id"] for c in candidates]
        if not allowed:
            return Selection(beat_id=beat["beat_id"], source=self.name)
        images_mapping: list[dict[str, Any]] = []
        images = self._images(candidates, image_mapping=images_mapping)
        if context.get("require_visual") and not images:
            raise _invalid_plan("真实视觉规划没有可读取的关键帧", beat=beat, context=context, raw=None)
        source_frame_hashes = [hashlib.sha256(base64.b64decode(data)).hexdigest() for data in images]
        contact_sheet = bool(context.get("contact_sheet") and images)
        if contact_sheet:
            from PIL import Image, ImageDraw
            # One numbered image keeps the local 3B model's visual context bounded.
            # Prefer at least one actual frame per candidate before extra frames.
            first, seen = [], set()
            for index, mapping in enumerate(images_mapping):
                if mapping["candidate_id"] not in seen:
                    first.append(index)
                    seen.add(mapping["candidate_id"])
            indices = (first + [i for i in range(len(images)) if i not in first])[:12]
            frame_images = [images[i] for i in indices]
            images_mapping = [dict(images_mapping[i], image_index=j, submitted_image_index=0, contact_cell_index=j)
                              for j, i in enumerate(indices)]
            source_frame_hashes = [source_frame_hashes[i] for i in indices]
            columns = min(4, len(frame_images))
            canvas = Image.new("RGB", (columns * 256, math.ceil(len(frame_images) / columns) * 172), "white")
            draw = ImageDraw.Draw(canvas)
            for index, data in enumerate(frame_images):
                with Image.open(io.BytesIO(base64.b64decode(data))) as frame:
                    thumb = frame.convert("RGB")
                    thumb.thumbnail((256, 144))
                    x, y = (index % columns) * 256, (index // columns) * 172
                    canvas.paste(thumb, (x, y + 24))
                    draw.text((x + 4, y + 4), f"#{index} t={images_mapping[index]['source_time_us']} us", fill="black")
            buffer = io.BytesIO()
            canvas.save(buffer, format="PNG")
            images = [base64.b64encode(buffer.getvalue()).decode("ascii")]
        evidence = [
            {
                "candidate_id": c["candidate_id"],
                "segment_id": c["segment_id"],
                "origin": c["origin"],
                "source_range_us": c.get("source_range_us"),
                "transcript": c.get("transcript", ""),
                "transcript_evidence": c.get("transcript_evidence", {}),
                "transcript_review": c.get("transcript_review", {}),
                "event_review": c.get("event_review"),
                "visual_evidence": any(m["candidate_id"] == c["candidate_id"] for m in images_mapping),
                "image_indices": [m["image_index"] for m in images_mapping
                                  if m["candidate_id"] == c["candidate_id"]],
                "selectable": (bool(context.get("thematic_montage")) or c["origin"] == "user" or _broll_allowed(beat, context))
                    and (not context.get("require_visual") or any(m["candidate_id"] == c["candidate_id"] for m in images_mapping)),
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
        if context.get("thematic_montage"):
            instruction["task"] = (
                "你是主题蒙太奇剪辑师。仅从 selectable=true 的真实候选选画面；保留素材来源。"
                "user 只表示用户上传，不证明故事发生在用户身上。所有镜头使用 establishing/detail/"
                "transition/atmosphere/broll 角色，禁止 primary，不虚构用户经历、人物关系或因果。"
                "参考实际附图、源时间和 ASR；review_status=needs_review 的 ASR 不可当已核实事实。"
                "不要重复或编造候选ID。用完整 candidate_id，不能写图片编号。"
            )
            instruction["required_output"]["roles"] = {
                "candidate_id": "establishing | transition | detail | atmosphere | broll"
            }
        if contact_sheet:
            instruction["image_layout"] = (
                "实际只附一张接触表。每格左上角 #编号 是 image_mapping/contact_cell_index 的零基编号；"
                "candidates.image_indices 指这些格子，而非多张图片。依照映射返回完整 candidate_id。"
            )
        narration_limit = narration_character_limit(beat["duration_budget_us"])
        narration_target_max = max(16, narration_limit)
        if context.get("draft_narration"):
            instruction["narration_max_chars"] = narration_limit
            instruction["narration_target_chars"] = {"min": 1, "max": narration_target_max}
            instruction["narration_length_includes_punctuation"] = False
            instruction["narration_counting_rule"] = "汉字、字母和数字逐字符计数；标点和空格不计。这是时长粗估参考，不是硬上限，不必填满。"
            instruction["narration_budget_is_advisory"] = True
            instruction["required_output"]["narration_draft"] = "待确认的中文旁白"
        instruction["story_theme"] = context.get("story_theme", "")
        instruction["previous_selected_segment_ids"] = context.get("previous_selected_segment_ids", [])
        if context.get("repair_instruction"):
            instruction["format_repair"] = context["repair_instruction"]
        aliases = {f"C{i + 1:02d}": c["candidate_id"] for i, c in enumerate(evidence)} if context.get("short_candidate_ids") else {}
        reverse_aliases = {cid: alias for alias, cid in aliases.items()}
        max_shots = 1 if beat["duration_budget_us"] <= 5_000_000 else 2
        if aliases:
            selectable_ids = {item["candidate_id"] for item in evidence if item["selectable"]}
            capacities = sorted((c["duration_us"] for c in candidates
                                 if c["candidate_id"] in selectable_ids
                                 and type(c.get("duration_us")) is int and c["duration_us"] > 0))[:8]
            # Size the allowance for shorter candidates too: the best-looking
            # clips need not be the longest. The model still chooses the shots.
            if sum(capacities[:max_shots]) < beat["duration_budget_us"]:
                capacity = 0
                for count, duration in enumerate(capacities, start=1):
                    capacity += duration
                    max_shots = max(max_shots, count)
                    if capacity >= beat["duration_budget_us"]:
                        break
            compact = []
            for item in evidence:
                transcript = item["transcript_evidence"] or {}
                spans = transcript.get("segments", [])
                compact.append({
                    "candidate_id": reverse_aliases[item["candidate_id"]], "origin": item["origin"],
                    "source_range_us": item["source_range_us"], "image_indices": item["image_indices"],
                    "visual_evidence": item["visual_evidence"], "selectable": item["selectable"],
                    "transcript_evidence": {
                        "status": transcript.get("status"), "review_status": transcript.get("review_status"),
                        "text": str(transcript.get("text", item["transcript"]))[:240],
                        "segments": [{"source_range_us": span.get("source_range_us"),
                                      "text": str(span.get("text", ""))[:100]} for span in spans[:3]],
                        "omitted_span_count": max(0, len(spans) - 3),
                        "review": (item.get("transcript_review") or {}).get("status"),
                    },
                })
            instruction["task"] = (
                f"看图，为本段主题择优选择1到{max_shots}个镜头；容量允许时让所选源区间总时长覆盖本段预算。候选用短ID C01等表示，"
                "图格编号不是候选ID。保留来源，不把用户上传或外部画面说成用户经历，不编造人物关系/因果。"
                "输出完整JSON：shot_order为所选短ID，reasons和roles只包含这些所选短ID，理由每项不超过12字；"
                "角色只能是establishing/detail/transition/atmosphere/broll。"
                "ASR仅作参考，needs_review不代表已证实；只选selectable=true。"
            )
            instruction["candidates"] = compact
            instruction["image_mapping"] = [{"cell": m["image_index"], "candidate_id": reverse_aliases[m["candidate_id"]],
                                               "source_time_us": m["source_time_us"]} for m in images_mapping]
            instruction["image_layout"] = "单张接触表；格子#编号对应image_mapping.cell。"
            instruction.pop("previous_selected_segment_ids", None)
            instruction.pop("required_output", None)
            instruction["beat"] = {"purpose": beat["purpose"], "duration_budget_us": beat["duration_budget_us"]}
            instruction["max_selected_shots"] = max_shots
        if context.get("draft_narration"):
            # Append after the compact-ID task so both production paths share rules.
            instruction["task"] += (
                "同时返回 narration_draft：面向听众的一句自然中文旁白，以。！？之一结束，待人工确认。"
                "先确定所选画面中可见的主体和状态，只说一个有画面支持的信息；"
                "不确定动作时描述静态状态，单张关键帧不能证明嬉戏、出发、购买等过程。"
                "主题和ASR只提供参考，不得盖过实际画面；不要复述ASR、不要堆叠地名。"
                "不虚构人物意图、地点、感受或用户经历；没有身份依据时不用我/我们。"
                "使用日常口语，避免画面标签、空泛氛围词和展示/体现/感受美好等套话。"
                f"本段粗估参考为 {narration_limit} 个朗读字，可在约 {narration_target_max} 字以内构思完整句（不计标点和空格）。"
                "字数仅为参考，完整表达优先；短段也要先成句，更短也可以，不设最低字数、不凑字数。"
                "超估算时删次要信息并整句改写，禁止截词、截句或只给残句补句号；以之后配音实测决定能否容纳。"
                "输出前默读一遍：是否说完，是否有画面依据。示例只示范改写，不能套用到无关画面。"
            )
            instruction["narration_examples"] = [
                {"visible": "猴子坐在台阶旁", "avoid": "猴子在树上嬉戏，展示", "prefer": "猴子坐在台阶旁。"},
                {"visible": "超市内的货架", "avoid": "我们先去超市买些", "prefer": "货架上摆满了商品。"},
                {"visible": "餐厅内有许多食客", "avoid": "餐厅里热闹的氛围", "prefer": "店里坐着不少食客。"},
            ]
        selectable = [reverse_aliases.get(c["candidate_id"], c["candidate_id"]) for c in evidence if c["selectable"]]
        count_limit = min(max_shots, len(selectable)) if aliases else max(1, len(selectable))
        role_names = ["establishing", "transition", "detail", "atmosphere", "broll"]
        if not context.get("thematic_montage"):
            role_names.insert(0, "primary")
        response_schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "shot_order": {"type": "array", "minItems": 1, "maxItems": count_limit,
                               "uniqueItems": True, "items": {"type": "string", "enum": selectable}},
                "reasons": {"type": "object", "additionalProperties": False,
                            "maxProperties": count_limit,
                            "properties": {cid: {"type": "string", "maxLength": 24} for cid in selectable}},
                "roles": {"type": "object", "additionalProperties": False,
                          "maxProperties": count_limit,
                          "properties": {cid: {"type": "string", "enum": role_names} for cid in selectable}},
            },
            "required": ["shot_order", "reasons", "roles"],
        }
        if context.get("draft_narration"):
            response_schema["properties"]["narration_draft"] = {
                # No decoding character cap: check the editor safety bound after
                # generation and request a complete rewrite rather than slicing.
                "type": "string", "minLength": 1,
            }
            response_schema["required"].append("narration_draft")
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": json.dumps(instruction, ensure_ascii=False),
            "stream": False,
            "format": response_schema,
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1024},
            "keep_alive": context.get("keep_alive", "0s"),
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
        started_at = datetime.now(timezone.utc).isoformat()
        started = perf_counter()
        request_archive = {
            "url": f"{self.base_url}/api/generate", "method": "POST", "payload": payload,
            "candidate_ids": allowed, "image_mapping": images_mapping,
            "candidate_aliases": aliases, "candidate_evidence": evidence,
            "excluded_candidate_ids": context.get("excluded_candidate_ids", []),
            "image_sha256": [hashlib.sha256(base64.b64decode(data)).hexdigest() for data in images],
            "source_frame_sha256": source_frame_hashes,
            "visual_mode": "numbered_contact_sheet" if contact_sheet else "individual_frames",
            "started_at": started_at,
            "format_attempt": context.get("format_attempt", 1),
            "previous_attempt_archive": context.get("previous_attempt_archive"),
        }
        archive_path = None
        attempt = {"endpoint": "/api/generate", "model": self.model, "request": request_archive,
                   "actual_backend": "ollama", "inference_executed": False, "status": "requested"}
        if self.archive_dir:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            call_id = hashlib.sha256(json.dumps(request_archive, sort_keys=True).encode()).hexdigest()
            archive_path = self.archive_dir / f"{call_id}.json"
            attempt["archive_path"] = str(archive_path.resolve())

        def save_attempt(record):
            if archive_path:
                archive_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")

        save_attempt(attempt)
        try:
            with open_ollama(request, timeout=self.timeout) as response:
                raw_body = response.read().decode("utf-8")
            body = json.loads(raw_body)
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    raw_body = exc.read().decode("utf-8", errors="replace")
                except OSError:
                    pass
            attempt.update(status="failed", error=str(exc), raw_body=raw_body, elapsed_seconds=perf_counter() - started)
            save_attempt(attempt)
            error = _invalid_plan(
                f"本地 Ollama 调用或响应解析失败（{self.model} @ {self.base_url}）：{exc}"
                + (f"；服务返回：{raw_body[:2000]}" if isinstance(exc, urllib.error.HTTPError) and raw_body else ""),
                beat=beat, context=context, raw=raw_body,
            )
            error.detail.update(request=request_archive, archive_path=str(archive_path) if archive_path else None)
            raise error from exc
        attempt.update(status="response_received", body=body, elapsed_seconds=perf_counter() - started,
                       inference_executed=isinstance(body, dict) and body.get("done") is True)
        save_attempt(attempt)

        def invalid_format(error):
            attempt.update(status="invalid_model_format", validation_error=str(error))
            save_attempt(attempt)
            error.detail.update(model_format_error=True, attempt_record=dict(attempt))
            return error

        if not isinstance(body, dict):
            raise _invalid_plan("Ollama 响应必须是 JSON object", beat=beat, context=context, raw=body)
        if body.get("done") is False or (context.get("draft_narration") and body.get("done") is not True):
            attempt.update(status="incomplete_response")
            save_attempt(attempt)
            error = _invalid_plan("Ollama 推理响应未完成，不能作为选镜或旁白草稿",
                                  beat=beat, context=context, raw=body)
            error.detail.update(attempt_record=dict(attempt))
            raise error
        raw_text = body.get("response")
        try:
            parsed = json.loads(raw_text) if isinstance(raw_text, str) else raw_text
        except ValueError as exc:
            raise invalid_format(_invalid_plan("本地模型返回内容不是合法 JSON", beat=beat,
                                               context=context, raw=raw_text)) from exc
        try:
            _plan_fields(parsed, beat=beat, context=context, raw=raw_text)
            if context.get("draft_narration") and any(key not in parsed for key in response_schema["required"]):
                raise _invalid_plan("模型响应缺少必填字段", beat=beat, context=context, raw=raw_text)
            canonical = parsed
            if aliases:
                selected_aliases = parsed["shot_order"]
                if (not 1 <= len(selected_aliases) <= max_shots or len(set(selected_aliases)) != len(selected_aliases)
                        or set(selected_aliases) - set(selectable)):
                    raise _invalid_plan("模型选镜短ID非法、重复或超过本段数量限制", beat=beat, context=context, raw=raw_text)
                if any(set(parsed.get(key, {})) != set(selected_aliases) for key in ("reasons", "roles")):
                    raise _invalid_plan("reasons/roles 必须且只能逐项包含 shot_order 中的短ID", beat=beat, context=context, raw=raw_text)
                canonical = {**copy.deepcopy(parsed), "shot_order": [aliases[cid] for cid in selected_aliases],
                             "reasons": {aliases[cid]: reason for cid, reason in parsed["reasons"].items()},
                             "roles": {aliases[cid]: role for cid, role in parsed["roles"].items()}}
        except PlanningError as exc:
            raise invalid_format(exc)
        selection = Selection(
            beat_id=beat["beat_id"],
            ordered_candidate_ids=list(canonical["shot_order"]),
            reasons=dict(canonical.get("reasons", {})),
            source=f"model:{self.model}",
            raw_response={**attempt, "parsed": parsed, "canonicalized": canonical, "status": "parsed_response",
                          "inference_executed": body.get("done") is True},
            role_overrides=dict(canonical.get("roles", {})),
            narration_draft=canonical.get("narration_draft"),
        )
        if context.get("draft_narration") and not isinstance(selection.narration_draft, str):
            raise invalid_format(_invalid_plan("模型没有返回待确认中文旁白 narration_draft", beat=beat, context=context, raw=selection.raw_response))
        # A structurally valid real selection survives a rough narration draft.
        # Production exposes content warnings and blocks confirmation until fixed.
        if context.get("require_visual"):
            pictured = {m["candidate_id"] for m in images_mapping}
            if set(selection.ordered_candidate_ids) - pictured:
                raise _invalid_plan("模型选择了没有实际附图的候选", beat=beat, context=context, raw=selection.raw_response)
        save_attempt(selection.raw_response)
        _check_broll(selection, beat=beat, candidates=candidates, context=context)
        return selection


def validate_selection(
    selection: Selection,
    *,
    beat: dict[str, Any],
    candidates: list[dict[str, Any]],
    max_broll_ratio: float = 0.4,
    thematic_montage: bool = False,
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
    if thematic_montage:
        segments = [by_id[cid]["segment_id"] for cid in selection.ordered_candidate_ids]
        if len(set(segments)) != len(segments):
            raise _invalid_plan("主题蒙太奇不能通过不同候选 ID 重复使用同一源片段", beat=beat,
                                raw=selection.raw_response)
    _check_broll(selection, beat=beat, candidates=candidates, context={"thematic_montage": thematic_montage})

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
        elif thematic_montage:
            if declared == "primary":
                problems.append(f"主题蒙太奇 {candidate_id} 不得冒充用户事件主镜头")
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
    if primary_count == 0 and not thematic_montage:
        raise PlanningError(
            UNSUPPORTED_STORY_BEAT,
            f"段落 {beat['beat_id']} 缺少用户素材事件证据，空镜不能替代具体事件",
            project_id=beat.get("project_id", ""),
            affected_ids=[beat["beat_id"]],
            detail={"candidate_ids": selection.ordered_candidate_ids, "raw_response": selection.raw_response},
        )

    # 按比例上限分配空镜预算，默认给主镜头保留至少 60%。
    external_count = len(records) - primary_count
    if external_count > primary_count and not thematic_montage:
        raise _invalid_plan("空镜数量超过主镜头，故事可能被环境画面填满", beat=beat,
                            raw=selection.raw_response)
    if thematic_montage:
        for record in records:
            record["suggested_duration_us"] = min(record["candidate_duration_us"], budget // len(records))
        remaining = budget - sum(r["suggested_duration_us"] for r in records)
        for record in records:
            added = min(remaining, record["candidate_duration_us"] - record["suggested_duration_us"])
            record["suggested_duration_us"] += added
            remaining -= added
        if any(r["suggested_duration_us"] <= 0 for r in records):
            raise _invalid_plan("主题镜头时长必须为正", beat=beat, raw=selection.raw_response)
        return records, []
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

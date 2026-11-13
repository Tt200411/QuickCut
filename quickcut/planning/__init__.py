"""QuickCut C 模块：故事与视听规划（协议 0.1.0）。

对外入口是 ``python -m quickcut.planning``；模块内部只生产 Brief 派生的
RetrievalRequest 与 StoryPlan，不写入成片时间线，也不修改多人共用的
quickcut/types.py、core.py。
"""
from .contracts import PlanningError, SCHEMA_VERSION, envelope, load_bundle, read_json, write_json
from .planning import (
    PlanResult,
    build_outline,
    build_retrieval_request,
    candidates_for_beat,
    classify_beat,
    compare_plans,
    next_retrieval_request,
    plan,
)
from .selection import FixedResponseSelector, OllamaSelector, RuleSelector, Selection, validate_selection
from .validator import assert_valid, validate_story_plan

__all__ = [
    "SCHEMA_VERSION",
    "PlanningError",
    "PlanResult",
    "Selection",
    "RuleSelector",
    "FixedResponseSelector",
    "OllamaSelector",
    "envelope",
    "load_bundle",
    "read_json",
    "write_json",
    "build_outline",
    "build_retrieval_request",
    "next_retrieval_request",
    "candidates_for_beat",
    "classify_beat",
    "plan",
    "compare_plans",
    "validate_selection",
    "validate_story_plan",
    "assert_valid",
]

"""用户选择的字幕字号、字重和颜色。缺省保持原来的白字、中号、常规。"""
from __future__ import annotations


SIZES = {"small": 0.75, "medium": 1.0, "large": 1.35}
WEIGHTS = {"regular": False, "bold": True}
COLORS = {
    "white": ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0)),
    "yellow": ((1.0, 1.0, 0.0), (0.0, 0.0, 0.0)),
    "black": ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
}
STYLE_CHOICES = {
    "subtitle_size": frozenset(SIZES),
    "subtitle_weight": frozenset(WEIGHTS),
    "subtitle_color": frozenset(COLORS),
}


def _ass(color: tuple[float, float, float]) -> str:
    red, green, blue = (round(channel * 255) for channel in color)
    return f"&H00{blue:02X}{green:02X}{red:02X}"


def resolve_subtitle_style(policy: dict | None) -> dict:
    """把 render_policy 里的枚举变成烧录和剪映都能用的样式。"""
    policy = policy or {}
    size = policy.get("subtitle_size", "medium")
    weight = policy.get("subtitle_weight", "regular")
    color = policy.get("subtitle_color", "white")
    if size not in SIZES or weight not in WEIGHTS or color not in COLORS:
        raise ValueError("字幕字号、字重或颜色无效")
    primary, outline = COLORS[color]
    return {"size_scale": SIZES[size], "bold": WEIGHTS[weight],
            "primary": primary, "outline": outline,
            "primary_ass": _ass(primary), "outline_ass": _ass(outline)}

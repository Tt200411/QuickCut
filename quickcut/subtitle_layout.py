"""字幕显示副本的确定性折行；不处理 cue 或时间，也不测量字体像素。"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


_OPEN = frozenset("（〔［｛《〈「『【〖〘〚“‘([{«")
_CLOSE = frozenset("，。！？；：、）〕］｝》〉」』】〗〙〛”’!?;:,.)]}»…％%")
_PUNCT = _CLOSE | frozenset("—–")


@dataclass(frozen=True)
class SubtitleLayout:
    # 原有换行符附在前一行；直接拼接这些片段能逐字符恢复输入。
    lines: tuple[str, ...]
    line_widths: tuple[int, ...]
    max_units: int
    overflow: bool

    @property
    def display_text(self) -> str:
        return "".join(line + ("\n" if index < len(self.lines) - 1
                              and not line.endswith(("\r", "\n")) else "")
                       for index, line in enumerate(self.lines))


def _extension(char: str) -> bool:
    point = ord(char)
    return (unicodedata.category(char).startswith("M")
            or 0xFE00 <= point <= 0xFE0F or 0xE0100 <= point <= 0xE01EF
            or 0x1F3FB <= point <= 0x1F3FF or 0xE0020 <= point <= 0xE007F)


def _hangul_type(char: str) -> str:
    point = ord(char)
    if 0x1100 <= point <= 0x115F or 0xA960 <= point <= 0xA97C:
        return "L"
    if 0x1160 <= point <= 0x11A7 or 0xD7B0 <= point <= 0xD7C6:
        return "V"
    if 0x11A8 <= point <= 0x11FF or 0xD7CB <= point <= 0xD7FB:
        return "T"
    if 0xAC00 <= point <= 0xD7A3:
        return "LVT" if (point - 0xAC00) % 28 else "LV"
    return ""


def _clusters(text: str) -> list[str]:
    """保护 Hangul GB6–8、组合符、变体、肤色、旗帜和 ZWJ；非完整 UAX #29。"""
    result = []
    regional_count = 0
    for char in text:
        regional = 0x1F1E6 <= ord(char) <= 0x1F1FF
        previous = _hangul_type(result[-1][-1]) if result else ""
        current = _hangul_type(char)
        hangul_join = (previous == "L" and current in ("L", "V", "LV", "LVT")
                       or previous in ("LV", "V") and current in ("V", "T")
                       or previous in ("LVT", "T") and current == "T")
        if result and (hangul_join or _extension(char) or char == "\u200d" or result[-1].endswith("\u200d")
                       or (regional and regional_count % 2)):
            result[-1] += char
        else:
            result.append(char)
        regional_count = regional_count + 1 if regional else 0
    return result


def _width(cluster: str) -> int:
    # 一个 Hangul 字素按一个全角音节估算，NFD 原文不做归一化。
    if _hangul_type(cluster[0]) and all(_hangul_type(char) or _extension(char) for char in cluster):
        return 2
    widths = [2 if unicodedata.east_asian_width(char) in "WF" else 1
              for char in cluster if not _extension(char)
              and unicodedata.category(char) not in ("Cf", "Cc")]
    if "\u200d" in cluster or "\ufe0f" in cluster or any(0x1F1E6 <= ord(c) <= 0x1F1FF for c in cluster):
        return max(2, max(widths, default=0))
    # 制表符仅作保守的四单元估计，实际播放器的制表位不在此承诺。
    return sum(widths) + 4 * cluster.count("\t")


def _word(cluster: str) -> bool:
    return bool(cluster) and (cluster[0].isalnum() or cluster[0] == "_") and unicodedata.east_asian_width(cluster[0]) not in "WF"


def _units(text: str) -> list[str]:
    clusters = _clusters(text)
    tokens: list[str] = []
    for index, cluster in enumerate(clusters):
        previous = clusters[index - 1] if index else ""
        following = clusters[index + 1] if index + 1 < len(clusters) else ""
        connector = cluster in ("'", "’", "-", ".", "/", ":") and _word(previous) and _word(following)
        after_connector = (previous in ("'", "’", "-", ".", "/", ":") and index > 1
                           and _word(clusters[index - 2]) and _word(cluster))
        if tokens and ((_word(previous) and _word(cluster)) or connector or after_connector):
            tokens[-1] += cluster
        else:
            tokens.append(cluster)
    # 把禁则边界合并为不可分单元；空格仍原样保留，并不绕开禁则。
    units: list[str] = []
    for token in tokens:
        left = units[-1].rstrip() if units else ""
        if units and (not left or not token.strip() or left[-1] in _OPEN
                      or token[0] in _CLOSE):
            units[-1] += token
        else:
            units.append(token)
    return units


def _wrap(text: str, budget: int, *, prefer_clauses: bool = False) -> tuple[list[str], list[int]]:
    units = _units(text)
    if not units:
        return [text], [0]
    widths = [sum(_width(cluster) for cluster in _clusters(unit)) for unit in units]
    count = len(units)
    # 依次最小化溢出、行数、参差及断点代价；固定遍历顺序保证确定性。
    costs = [(0, 0, 0, 0) if prefer_clauses else (0, 0, 0)] * (count + 1)
    ends = [count] * count
    for start in range(count - 1, -1, -1):
        best = None
        width = 0
        for end in range(start + 1, count + 1):
            width += widths[end - 1]
            if width > budget and end > start + 1:
                break
            tail = costs[end]
            boundary = units[end - 1]
            penalty = (0 if end == count or boundary.rstrip()[-1:] in _PUNCT
                       else 3 if boundary[-1:].isspace() else 24)
            if prefer_clauses:
                candidate = (tail[0] + max(0, width - budget) ** 2,
                             tail[1] + 1, tail[2] + penalty,
                             tail[3] + (budget - width) ** 2)
            else:
                candidate = (tail[0] + max(0, width - budget) ** 2,
                             tail[1] + 1, tail[2] + (budget - width) ** 2 + penalty)
            if best is None or candidate < best:
                best, ends[start] = candidate, end
        costs[start] = best
    lines, line_widths = [], []
    start = 0
    while start < count:
        end = ends[start]
        lines.append("".join(units[start:end]))
        line_widths.append(sum(widths[start:end]))
        start = end
    return lines, line_widths


def layout_subtitle(text: str, *, width: int, height: int, max_units: int | None = None,
                    prefer_clauses: bool = False) -> SubtitleLayout:
    """只插入显示换行，不删字、不截英文词、不限制总行数。

    默认横屏 40、竖屏 24、方屏 32 个 Unicode 估计单元：普通汉字为 2，
    窄字母/数字为 1。预算与分辨率无关，不能保证不同字体或播放器的像素布局。
    原有 CR/LF/CRLF 保持为硬换行；超长词或禁则单元可超宽并返回 overflow。
    """
    if width <= 0 or height <= 0:
        raise ValueError("画布宽高必须为正数")
    budget = max_units if max_units is not None else 40 if width > height else 24 if width < height else 32
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise ValueError("宽度预算必须为正整数")
    lines, widths = [], []
    parts = re.split(r"(\r\n|\r|\n)", text)
    for index in range(0, len(parts), 2):
        wrapped, measured = _wrap(parts[index], budget, prefer_clauses=prefer_clauses)
        if index + 1 < len(parts):
            wrapped[-1] += parts[index + 1]
        lines.extend(wrapped)
        widths.extend(measured)
    return SubtitleLayout(tuple(lines), tuple(widths), budget, any(w > budget for w in widths))


def layout_presentation_subtitle(text: str, *, width: int, height: int,
                                 policy: dict | None = None) -> SubtitleLayout:
    """Use the available caption width before wrapping a wide presentation.

    The estimate shares the preview's font size and margins. Actual glyph
    metrics still depend on the font; export frames must be checked visually.
    Legacy layouts retain their existing wrapping and serialized text.
    """
    from .subtitle_style import resolve_subtitle_style

    appearance = resolve_subtitle_style(policy)
    if appearance["layout"] != "wide":
        return layout_subtitle(text, width=width, height=height)
    if width <= 0 or height <= 0:
        raise ValueError("Canvas dimensions must be positive")
    short_side = min(width, height)
    font_size = short_side / 18 * appearance["size_scale"]
    outline = short_side / 360 * appearance["size_scale"]
    available = width - short_side / 10 - outline * 2
    budget = max(2, int(available / font_size) * 2)
    return layout_subtitle(text, width=width, height=height, max_units=budget,
                           prefer_clauses=True)

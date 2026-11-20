from __future__ import annotations

import random
import unicodedata

import pytest

from quickcut.subtitle_layout import _clusters, _width, layout_subtitle, layout_presentation_subtitle


def test_wide_presentation_uses_one_line_when_the_text_fits():
    text = "文艺复兴的故事，就藏在画里。"
    assert len(layout_subtitle(text, width=720, height=1280).lines) == 2
    wide = layout_presentation_subtitle(text, width=720, height=1280,
                                      policy={"subtitle_layout": "wide"})
    assert wide.lines == (text,)
    assert not wide.overflow


def test_wide_presentation_wraps_at_the_clause_without_losing_text():
    text = "走进安静的教堂，光从彩窗里慢慢落下来。"
    wide = layout_presentation_subtitle(text, width=720, height=1280,
                                      policy={"subtitle_layout": "wide"})
    assert wide.lines == ("走进安静的教堂，", "光从彩窗里慢慢落下来。")
    assert "".join(wide.lines) == text
    assert not wide.overflow


def test_wide_presentation_adapts_to_font_size_and_preserves_legacy_layout():
    text = "第一段旅途中的景色，第二段旅途中的回忆。"
    common = dict(width=720, height=1280)
    small = layout_presentation_subtitle(text, **common, policy={"subtitle_layout": "wide", "subtitle_size": "small"})
    large = layout_presentation_subtitle(text, **common, policy={"subtitle_layout": "wide", "subtitle_size": "large"})
    assert small.max_units > large.max_units
    assert layout_presentation_subtitle(text, **common) == layout_subtitle(text, **common)


@pytest.mark.parametrize("text", [
    "", "短字幕", "  保留  空格，不能删除。  ", "第一行\r\n第二行\n第三行\r末行\n",
    "\n\r\n", "今天使用 Python 3.12 处理 2026 年的视频，完成后保持原文。",
    "他说：“请看（这里）的画面。”接着继续介绍下一段内容，不要改写任何文字。",
    "家人\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466一起出发，cafe\u0301 很好。",
    "Supercalifragilisticexpialidocious" * 3,
])
@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1920), (1080, 1080)])
def test_lossless_deterministic_layout(text, size):
    result = layout_subtitle(text, width=size[0], height=size[1])
    assert "".join(result.lines) == text
    assert result == layout_subtitle(text, width=size[0], height=size[1])
    assert len(result.lines) == len(result.line_widths)
    assert result.overflow == any(width > result.max_units for width in result.line_widths)


def test_explicit_orientation_policy_and_balanced_tail():
    text = "天地山川风雨日月星河草木花鸟春夏秋冬田野森林海洋晨光晚霞清泉"
    landscape = layout_subtitle(text, width=1920, height=1080)
    portrait = layout_subtitle(text, width=1080, height=1920)
    square = layout_subtitle(text, width=1080, height=1080)
    assert (landscape.max_units, portrait.max_units, square.max_units) == (40, 24, 32)
    assert len(portrait.lines) > len(landscape.lines)
    assert max(landscape.line_widths) - min(landscape.line_widths) <= 2
    assert min(portrait.line_widths) >= portrait.max_units // 2


def test_prefers_punctuation_and_space_boundaries():
    result = layout_subtitle("山间微风，田野花香。", width=1920, height=1080, max_units=12)
    assert result.lines == ("山间微风，", "田野花香。")
    result = layout_subtitle("alpha beta gamma delta", width=1920, height=1080, max_units=12)
    assert result.lines == ("alpha beta ", "gamma delta")


def test_chinese_punctuation_never_stranded_at_inserted_break():
    text = "他说：“请看（这里）的画面。”接着介绍《山间故事》，再看【田野】！"
    for budget in range(4, 25):
        result = layout_subtitle(text, width=1920, height=1080, max_units=budget)
        for line in result.lines:
            assert line.lstrip()[0] not in "，。！？；：、）》」』】”’"
            assert line.rstrip()[-1] not in "（《「『【“‘"


@pytest.mark.parametrize("cluster", [
    "e\u0301", "a\u0308\u0301", "\u2764\ufe0f", "\U0001f44d\U0001f3fd",
    "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
    "\U0001f1e8\U0001f1f3", "1\ufe0f\u20e3",
])
def test_never_splits_combining_or_emoji_sequences(cluster):
    result = layout_subtitle("山" + cluster + "川" + cluster + "海", width=100, height=200, max_units=3)
    assert result.display_text.count(cluster) == 2
    assert "".join(result.lines) == "山" + cluster + "川" + cluster + "海"


@pytest.mark.parametrize("budget", [1, 2, 3, 4, 5, 8, 24])
def test_hangul_nfd_wraps_only_at_syllable_boundaries_without_normalizing(budget):
    text = unicodedata.normalize("NFD", "한글가나다각힣")
    syllables = [unicodedata.normalize("NFD", char) for char in "한글가나다각힣"]
    assert _clusters(text) == syllables
    result = layout_subtitle(text, width=720, height=1280, max_units=budget)
    assert "".join(result.lines) == text
    assert result.line_widths == tuple(2 * len(_clusters(line)) for line in result.lines)
    assert sum(result.line_widths) == 14
    assert result.overflow == (budget < 2)
    boundaries = set()
    offset = 0
    for syllable in syllables:
        offset += len(syllable)
        boundaries.add(offset)
    offset = 0
    for line in result.lines:
        offset += len(line)
        assert offset in boundaries


@pytest.mark.parametrize("cluster", [
    "\u1100\u1102", "\u1100\u1161", "\u1100가", "\u1100각",
    "가\u1161", "가\u11a8", "\u1161\u1162", "\u1161\u11a8",
    "각\u11a8", "\u11a8\u11a9", "\ua960\ud7b0\ud7cb",
    "\u115f\u1160\u11ff", "\ua97c\ud7c6\ud7fb", "\u1100\u1161\u11a8\u0301",
])
def test_hangul_gb6_to_gb8_including_extended_jamo(cluster):
    assert _clusters(cluster) == [cluster]
    assert _width(cluster) == 2
    assert _clusters(cluster + "山") == [cluster, "山"]


@pytest.mark.parametrize("text", ["\u1161\u1100", "\u11a8\u1161", "가나", "각가", "\u1100\u0301\u1161"])
def test_hangul_does_not_join_across_non_gb6_to_gb8_boundaries(text):
    assert len(_clusters(text)) == 2


def test_long_word_and_decimal_remain_whole_and_report_overflow():
    word = "ExtraordinarilyLongUnbrokenEnglishWord"
    result = layout_subtitle("中文 " + word + " 12345.6789 结束", width=100, height=200, max_units=8)
    assert word in result.display_text
    assert "12345.6789" in result.display_text
    assert result.overflow
    assert max(result.line_widths) == len(word) + 1


def test_leading_spaces_do_not_create_a_new_empty_srt_block():
    text = "      开场文字继续向前，保留空格。"
    result = layout_subtitle(text, width=100, height=200, max_units=6)
    assert "".join(result.lines) == text
    assert all(line.strip() for line in result.lines)
    assert result.overflow


def test_preserves_existing_line_endings_without_extra_breaks():
    text = " 山\r\n川 \n\n海\r"
    result = layout_subtitle(text, width=1920, height=1080)
    assert result.display_text == text
    assert result.lines == (" 山\r\n", "川 \n", "\n", "海\r", "")
    result = layout_subtitle("山川草木\r\n春夏秋冬", width=1920, height=1080, max_units=4)
    assert result.display_text == "山川\n草木\r\n春夏\n秋冬"


def test_seeded_mixed_text_preserves_every_character():
    rng = random.Random(19)
    atoms = ["山", "川", "a\u0301", "hello", "2026", " ", "，", "（", "）", "\r\n", "\U0001f469\u200d\U0001f4bb"]
    for _ in range(100):
        text = "".join(rng.choices(atoms, k=40))
        result = layout_subtitle(text, width=1080, height=1920)
        assert "".join(result.lines) == text
        assert result.overflow == any(w > 24 for w in result.line_widths)


@pytest.mark.parametrize("budget", [0, -1, True, 2.5])
def test_rejects_invalid_budget(budget):
    with pytest.raises(ValueError):
        layout_subtitle("字幕", width=1920, height=1080, max_units=budget)

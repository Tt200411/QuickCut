import pytest

from quickcut.production_pipeline import PipelineError, apply_presentation
from quickcut.subtitle_style import resolve_subtitle_style


def test_missing_style_keeps_the_original_white_medium_caption():
    style = resolve_subtitle_style({})
    assert style["size_scale"] == 1
    assert style["bold"] is False
    assert style["primary"] == (1, 1, 1)
    assert style["primary_ass"] == "&H00FFFFFF"


def test_selected_style_scales_size_and_maps_color():
    style = resolve_subtitle_style({"subtitle_size": "small", "subtitle_weight": "bold", "subtitle_color": "black"})
    assert style["size_scale"] == 0.75
    assert style["bold"] is True
    assert style["primary"] == (0, 0, 0)
    assert style["outline_ass"] == "&H00FFFFFF"


def test_unknown_style_is_rejected():
    with pytest.raises(ValueError, match="字幕"):
        resolve_subtitle_style({"subtitle_color": "purple"})


@pytest.mark.parametrize("key", ["subtitle_size", "subtitle_weight", "subtitle_color"])
@pytest.mark.parametrize("value", [None, [], {}, True, 1, "white,FontSize=100"])
def test_malformed_style_is_a_validation_error(key, value):
    with pytest.raises(ValueError, match="字幕"):
        resolve_subtitle_style({key: value})


@pytest.mark.parametrize("policy", [[], "", 0, False, ["subtitle_color"]])
def test_malformed_policy_is_not_silently_treated_as_default(policy):
    with pytest.raises(ValueError, match="字幕"):
        resolve_subtitle_style(policy)


def test_presentation_copies_only_styles_that_were_chosen():
    timeline = {"render_policy": {}}
    updated, recorded = apply_presentation(timeline, {"subtitle_position": "top", "subtitle_size": "large"})
    assert updated["render_policy"] == {"subtitle_position": "top", "subtitle_size": "large"}
    assert recorded == {"subtitle_position": "top", "subtitle_size": "large"}
    assert timeline == {"render_policy": {}}
    with pytest.raises(PipelineError, match="颜色"):
        apply_presentation(timeline, {"subtitle_color": "purple"})

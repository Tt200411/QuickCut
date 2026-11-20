"""Limited, explicit macOS Jianying draft mapping; no editor automation.

Serialization references: pyJianYingDraft (Apache-2.0); see the bundled
templates/jianying/NOTICE.txt and LICENSE.txt.
Private absolute media paths are regenerated on installation/relocation.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import time
import uuid

from .subtitle_layout import layout_subtitle
from .subtitle_style import resolve_subtitle_style

# Display wrapping changes serialized text; reject older packages on rebuild.
ADAPTER_VERSION = "quickcut-jianying-mac-0.2.0"
TARGET_VERSION = "6.1.0"
CAPABILITIES = {
    "video": "hard cuts, one picture track, 1x speed",
    "source_audio": "independent PCM clips with timeline gains",
    "narration": "independent unchanged WAV clips with timeline gains",
    "subtitle": "independent editable text segments",
    "media_relocation": "install regenerates private absolute paths from package URIs",
    "unsupported": ["music", "sound_effect", "transitions", "effects", "speed changes",
                    "gain envelopes", "ducking", "overlapping picture clips"],
}


def _template(name: str) -> dict:
    return json.loads((Path(__file__).parent / "templates" / "jianying" / name).read_text(encoding="utf-8"))


def _id(namespace: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"quickcut:{namespace}:{value}")).upper()


def _range(value: dict) -> dict:
    return {"start": value["begin"], "duration": value["end"] - value["begin"]}


def _clip_settings(*, subtitle: bool = False, subtitle_position: str = "bottom") -> dict:
    return {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False}, "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0}, "transform": {
                "x": 0.0, "y": (0.8 if subtitle_position == "top" else -0.8) if subtitle else 0.0}}


def build_draft(timeline: dict, resources: list[dict], draft_directory: Path, *,
                name: str, draft_id: str, target_version: str = TARGET_VERSION) -> tuple[dict, dict]:
    """Map already validated input; resources paths are relative to draft/.

    A source-audio derivative has resource_key='source_audio:<clip_id>'.
    Its PCM begins at the selected source range; the mapping is kept in the
    export manifest. Original videos remain complete and separately editable.
    """
    content = _template("draft_content.json")
    meta = _template("draft_meta_info.json")
    output = timeline["output"]
    subtitle_position = timeline.get("render_policy", {}).get("subtitle_position", "bottom")
    if subtitle_position not in ("top", "bottom"):
        raise ValueError("subtitle_position must be top or bottom")
    appearance = resolve_subtitle_style(timeline.get("render_policy"))
    now = time.time_ns() // 1000
    platform = {"app_id": 3704, "app_source": "lv", "app_version": target_version, "os": "mac"}
    content.update(id=draft_id, name=name, duration=timeline["duration_us"], new_version="75.0.0",
                   fps=output["fps"]["numerator"] / output["fps"]["denominator"],
                   create_time=now, update_time=now, platform=platform,
                   last_modified_platform=deepcopy(platform),
                   canvas_config={"width": output["width"], "height": output["height"], "ratio": "original", "background": None})
    content["config"]["maintrack_adsorb"] = False
    materials = content["materials"]
    materials.update(videos=[], audios=[], texts=[], speeds=[])
    by_key = {r["resource_key"]: r for r in resources}
    registry_media = []
    for resource in resources:
        mid = _id(draft_id, f"material:{resource['media_kind']}:{resource['resource_key']}")
        path = str(draft_directory / resource["draft_uri"])
        if resource["media_kind"] == "video":
            item = {"id": mid, "material_id": mid, "local_material_id": "", "type": "video",
                    "path": path, "media_path": "", "material_name": resource["name"],
                    "duration": resource["duration_us"], "width": resource["width"],
                    "height": resource["height"], "category_id": "", "category_name": "local",
                    "check_flag": 63487, "crop_ratio": "free", "crop_scale": 1.0, "audio_fade": None,
                    "crop": {"upper_left_x": 0.0, "upper_left_y": 0.0, "upper_right_x": 1.0,
                             "upper_right_y": 0.0, "lower_left_x": 0.0, "lower_left_y": 1.0,
                             "lower_right_x": 1.0, "lower_right_y": 1.0}}
            materials["videos"].append(item)
        else:
            materials["audios"].append({"id": mid, "local_material_id": mid, "music_id": mid,
                "type": "extract_music", "path": path, "name": resource["name"],
                "duration": resource["duration_us"], "app_id": 0, "category_id": "",
                "category_name": "local", "check_flag": 3, "copyright_limit_type": "none",
                "effect_id": "", "formula_id": "", "source_platform": 0, "wave_points": []})
        registry_media.append({"id": mid, "file_Path": path, "duration": resource["duration_us"],
            "extra_info": resource["name"], "width": resource.get("width", 0),
            "height": resource.get("height", 0), "metetype": "video" if resource["media_kind"] == "video" else "music",
            "type": 0, "item_source": 1, "md5": "", "create_time": now // 1_000_000,
            "import_time": now // 1_000_000, "import_time_ms": now,
            "roughcut_time_range": {"duration": -1, "start": -1},
            "sub_time_range": {"duration": -1, "start": -1}})
    names = {"video": "QuickCut 画面与空镜", "source_audio": "QuickCut 原声",
             "narration": "QuickCut 配音", "subtitle": "QuickCut 字幕"}
    content["tracks"] = []
    for order, track in enumerate(timeline["tracks"]):
        kind = track["kind"]
        native = {"id": _id(draft_id, f"track:{kind}:{track['track_id']}"), "type": "text" if kind == "subtitle" else
                  "video" if kind == "video" else "audio", "name": names[kind],
                  "is_default_name": False, "attribute": 0, "flag": 0, "segments": []}
        for item in track["cues" if kind == "subtitle" else "clips"]:
            identifier = item["subtitle_id"] if kind == "subtitle" else item["clip_id"]
            item_key = f"{kind}:{track['track_id']}:{identifier}"
            speed_id = _id(draft_id, f"speed:{item_key}")
            segment = {"id": _id(draft_id, f"segment:{item_key}"), "target_timerange": _range(item["timeline_range_us"]),
                "source_timerange": None, "speed": 1.0, "volume": 1.0, "last_nonzero_volume": 1.0,
                "reverse": False, "visible": True, "track_attribute": 0, "track_render_index": 0,
                "render_index": order, "common_keyframes": [], "keyframe_refs": [],
                "extra_material_refs": [speed_id], "is_tone_modify": False,
                "enable_adjust": True, "enable_color_correct_adjust": False, "enable_color_curves": True,
                "enable_color_match_adjust": False, "enable_color_wheels": True,
                "enable_lut": True, "enable_smart_color_adjust": False}
            materials["speeds"].append({"id": speed_id, "type": "speed", "mode": 0,
                                         "speed": 1.0, "curve_speed": None})
            if kind == "subtitle":
                mid = _id(draft_id, f"material:text:{item_key}")
                text = layout_subtitle(item["text"], width=output["width"],
                                       height=output["height"]).display_text
                style = {"fill": {"alpha": 1.0, "content": {"render_type": "solid", "solid": {
                    "alpha": 1.0, "color": list(appearance["primary"])}}},
                    "range": [0, len(text.encode("utf-16-le")) // 2], "size": 8.0 * appearance["size_scale"],
                    "bold": appearance["bold"], "italic": False, "underline": False, "strokes": []}
                materials["texts"].append({"id": mid, "content": json.dumps({"text": text, "styles": [style]}, ensure_ascii=False),
                    "typesetting": 0, "alignment": 1, "letter_spacing": 0.0, "line_spacing": 0.02,
                    "line_feed": 1, "line_max_width": 0.82, "force_apply_line_max_width": False,
                    "check_flag": 7, "type": "subtitle", "global_alpha": 1.0})
                segment.update(material_id=mid, clip=_clip_settings(subtitle=True, subtitle_position=subtitle_position),
                               uniform_scale={"on": True, "value": 1.0})
            else:
                key = f"source_audio:{item['clip_id']}" if kind == "source_audio" else item["asset_id"]
                if key not in by_key:
                    raise ValueError(f"Missing packaged resource for {key}")
                segment["material_id"] = _id(draft_id, f"material:{by_key[key]['media_kind']}:{key}")
                segment["source_timerange"] = (_range(item["source_range_us"]) if kind != "source_audio" else
                    {"start": 0, "duration": item["source_range_us"]["end"] - item["source_range_us"]["begin"]})
                segment["volume"] = 0.0 if kind == "video" else item["gain"]
                segment["last_nonzero_volume"] = item.get("gain", 1.0)
                if kind == "video":
                    segment.update(clip=_clip_settings(), uniform_scale={"on": True, "value": 1.0})
            native["segments"].append(segment)
        content["tracks"].append(native)
    meta.update(draft_id=draft_id, draft_name=name, draft_fold_path=str(draft_directory),
                draft_root_path=str(draft_directory.parent), tm_duration=timeline["duration_us"],
                tm_draft_create=now, tm_draft_modified=now, draft_cover=str(draft_directory / "draft_cover.jpg"),
                draft_timeline_materials_size_=sum(r["size_bytes"] for r in resources))
    for group in meta["draft_materials"]:
        if group["type"] == 0:
            group["value"] = registry_media
    return content, meta


def registry_entry(meta: dict) -> dict:
    """Minimal macOS 6.1 root_meta_info entry, preserving separate metadata."""
    keys = ("draft_id", "draft_name", "draft_fold_path", "draft_root_path", "draft_cover",
            "tm_duration", "tm_draft_create", "tm_draft_modified")
    entry = {key: meta[key] for key in keys}
    entry.update(draft_json_file=str(Path(meta["draft_fold_path"]) / "draft_info.json"),
                 draft_is_invisible=False, draft_is_ai_shorts=False, draft_type="",
                 draft_new_version="", draft_cloud_last_action_download=False,
                 draft_cloud_purchase_info="", draft_cloud_template_id="", draft_cloud_tutorial_info="",
                 draft_cloud_videocut_purchase_info="", tm_draft_cloud_completed="",
                 tm_draft_cloud_modified=0, tm_draft_removed=0,
                 draft_timeline_materials_size=meta["draft_timeline_materials_size_"])
    return entry

from __future__ import annotations

"""Gradio workbench for the local QuickCut pipeline.

The UI keeps editing as a thin layer over :func:`update_timeline`, so trim,
replacement, and reorder preserve the cached ``index_id`` and use the same
validation path as the API and CLI.
"""

from pathlib import Path
import os
from typing import Any

from .core import build_index, localize, render_timeline, update_timeline
from .types import IndexManifest, Shot, Timeline, TimelineItem
from .voice import render_story_timeline


def _timeline_from_payload(payload: Timeline | dict[str, Any] | None) -> Timeline:
    """Convert a Gradio state value back to a Timeline object."""
    if isinstance(payload, Timeline):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("Run index + localize before editing the timeline")
    data = dict(payload)
    data.pop("duration", None)
    items = [TimelineItem(**item) for item in data.pop("items", [])]
    return Timeline(items=items, **data)


def _manifest_from_payload(payload: IndexManifest | dict[str, Any] | None) -> IndexManifest | None:
    if payload is None:
        return None
    if isinstance(payload, IndexManifest):
        return payload
    if not isinstance(payload, dict):
        return None
    data = dict(payload)
    shots = [Shot(**shot) for shot in data.pop("shots", [])]
    return IndexManifest(shots=shots, **data)


def _item_choices(timeline: Timeline) -> list[str]:
    return [item.item_id for item in timeline.items]


def _shot_choices(manifest: IndexManifest | None) -> list[str]:
    return [shot.shot_id for shot in (manifest.shots if manifest else [])]


def _replacement_interval(item: TimelineItem, shot: Shot) -> tuple[float, float]:
    """Place an item on a replacement shot while retaining its requested length."""
    length = min(item.duration, shot.duration)
    if length <= 0:
        return shot.start, shot.end
    center = (shot.start + shot.end) / 2
    start = max(shot.start, center - length / 2)
    end = min(shot.end, start + length)
    return round(start, 3), round(end, 3)


def apply_edit(
    timeline_payload: Timeline | dict[str, Any] | None,
    manifest_payload: IndexManifest | dict[str, Any] | None,
    item_id: str | None,
    replacement_asset: str | None = None,
    trim_start: float | None = None,
    trim_end: float | None = None,
) -> Timeline:
    """Apply a replace/trim operation through the shared timeline updater.

    ``replacement_asset`` is a source shot ID from the indexed manifest. Its
    source interval is copied into the timeline item, so rendering reflects the
    replacement rather than merely changing provenance metadata.
    """
    timeline = _timeline_from_payload(timeline_payload)
    if not item_id:
        raise ValueError("Choose a timeline item to edit")
    selected = next((item for item in timeline.items if item.item_id == item_id), None)
    if selected is None:
        raise ValueError(f"Unknown timeline item: {item_id}")

    edit: dict[str, Any] = {"item_id": item_id}
    # The UI fills the selected item's in/out values for convenience. Treat
    # those unchanged values as defaults when replacing, allowing replacement
    # to move the item onto the target shot automatically.
    unchanged_trim = (
        trim_start is not None and trim_end is not None
        and abs(float(trim_start) - selected.start) < 1e-6
        and abs(float(trim_end) - selected.end) < 1e-6
    )
    if trim_start is not None and not (replacement_asset and unchanged_trim):
        edit["start"] = float(trim_start)
    if trim_end is not None and not (replacement_asset and unchanged_trim):
        edit["end"] = float(trim_end)
    if replacement_asset:
        edit["asset_id"] = str(replacement_asset)
        manifest = _manifest_from_payload(manifest_payload)
        replacement = next(
            (shot for shot in (manifest.shots if manifest else []) if shot.shot_id == replacement_asset),
            None,
        )
        if replacement is not None:
            start, end = _replacement_interval(selected, replacement)
            # Explicit trim values take precedence over the replacement shot's
            # interval, which lets a user replace and trim in one operation.
            edit.setdefault("start", start)
            edit.setdefault("end", end)

    return update_timeline(timeline, [edit])


def apply_reorder(
    timeline_payload: Timeline | dict[str, Any] | None,
    order_text: str | None,
) -> Timeline:
    """Reorder every current item using comma/newline separated item IDs."""
    timeline = _timeline_from_payload(timeline_payload)
    raw = (order_text or "").replace("\n", ",")
    order = [token.strip() for token in raw.split(",") if token.strip()]
    if not order:
        raise ValueError("Enter item IDs in the desired order")
    current = _item_choices(timeline)
    if len(order) != len(current) or set(order) != set(current):
        raise ValueError("Reorder must list each timeline item exactly once")
    edits = [{"item_id": item_id, "order": idx} for idx, item_id in enumerate(order)]
    return update_timeline(timeline, edits)


def _refresh_controls(
    timeline: Timeline,
    manifest: IndexManifest | None,
    selected_item: str | None = None,
) -> tuple[list[str], list[str], str, float | None, float | None]:
    """Return values for the editor controls after any timeline mutation."""
    choices = _item_choices(timeline)
    selected = next((item for item in timeline.items if item.item_id == selected_item), None)
    selected = selected or (timeline.items[0] if timeline.items else None)
    return choices, _shot_choices(manifest), ", ".join(choices), selected.start if selected else None, selected.end if selected else None


def build_demo():
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Gradio is optional. Install with: pip install -e '.[ui]'") from exc

    encoder_default = os.getenv("QUICKCUT_ENCODER", "fallback")
    asr_default = os.getenv("QUICKCUT_ASR", "auto")

    def dropdown_update(choices: list[str], value: str | None):
        # ``gr.update`` is available in Gradio 4 and 5.  The component return
        # keeps this workbench usable on releases where the helper is removed.
        if hasattr(gr, "update"):
            return gr.update(choices=choices, value=value)
        return gr.Dropdown(choices=choices, value=value)

    def index_and_search(video, prompt, duration, encoder_mode, asr_mode):
        if video is None:
            raise ValueError("Upload a video first")
        source = video if isinstance(video, str) else video.name
        manifest = build_index(source, "cache", window=3.0, encoder_mode=encoder_mode, asr_mode=asr_mode)
        timeline = localize(manifest, f"{prompt} {duration}s", encoder_mode=encoder_mode)
        item_choices, shot_choices, order, start, end = _refresh_controls(timeline, manifest)
        return (
            timeline.to_dict(), timeline.message, timeline.to_dict(), manifest.to_dict(),
            dropdown_update(item_choices, item_choices[0] if item_choices else None),
            dropdown_update(shot_choices, None), order, start, end,
        )

    def edit_from_ui(timeline_payload, manifest_payload, item_id, replacement_asset, trim_start, trim_end):
        timeline = apply_edit(timeline_payload, manifest_payload, item_id, replacement_asset, trim_start, trim_end)
        manifest = _manifest_from_payload(manifest_payload)
        item_choices, shot_choices, order, start, end = _refresh_controls(timeline, manifest, item_id)
        return (
            timeline.to_dict(), timeline.to_dict(),
            dropdown_update(item_choices, item_id if item_id in item_choices else (item_choices[0] if item_choices else None)),
            dropdown_update(shot_choices, None), order, start, end,
        )

    def reorder_from_ui(timeline_payload, manifest_payload, order_text):
        timeline = apply_reorder(timeline_payload, order_text)
        manifest = _manifest_from_payload(manifest_payload)
        item_choices, shot_choices, order, start, end = _refresh_controls(timeline, manifest)
        return (
            timeline.to_dict(), timeline.to_dict(),
            dropdown_update(item_choices, item_choices[0] if item_choices else None),
            dropdown_update(shot_choices, None), order, start, end,
        )

    def select_item(timeline_payload, selected_item):
        timeline = _timeline_from_payload(timeline_payload)
        item = next((entry for entry in timeline.items if entry.item_id == selected_item), None)
        return (item.start, item.end) if item else (None, None)

    def render(preset, timeline_payload, use_captions, use_narration):
        timeline = _timeline_from_payload(timeline_payload)
        timeline.preset = preset
        if use_captions or use_narration:
            result = render_story_timeline(timeline, Path("outputs") / "quickcut.mp4",
                                           captions=use_captions, narration=use_narration)
        else:
            result = render_timeline(timeline, Path("outputs") / "quickcut.mp4", preset=preset)
        return str(result)

    with gr.Blocks(title="QuickCut MVP") as demo:
        gr.Markdown("# QuickCut\nPrompt-guided source localization with an editable timeline.")
        timeline_state = gr.State(None)
        manifest_state = gr.State(None)
        with gr.Row():
            video = gr.Video(label="Source video")
            prompt = gr.Textbox(label="Prompt", value="scenic travel shots")
            duration = gr.Slider(5, 15, value=8, step=1, label="Duration (seconds)")
        with gr.Row():
            encoder_mode = gr.Dropdown(["fallback", "auto", "openclip"], value=encoder_default, label="Visual encoder")
            asr_mode = gr.Dropdown(["none", "auto", "faster-whisper"], value=asr_default, label="Transcript / Whisper")
        locate = gr.Button("Index + localize", variant="primary")
        timeline = gr.JSON(label="Validated timeline")
        message = gr.Textbox(label="Coverage")

        gr.Markdown("### Edit timeline\nChoose an item, optionally replace it with another indexed shot, and trim its in/out points.")
        with gr.Row():
            item_id = gr.Dropdown(label="Timeline item", choices=[])
            replacement = gr.Dropdown(label="Replace with source shot", choices=[], allow_custom_value=False)
        with gr.Row():
            trim_start = gr.Number(label="Trim start (seconds)", precision=3)
            trim_end = gr.Number(label="Trim end (seconds)", precision=3)
            apply = gr.Button("Apply replace / trim")
        order = gr.Textbox(label="Reorder item IDs (comma-separated)")
        reorder = gr.Button("Apply reorder")

        preset = gr.Dropdown(["clean", "warm"], value="clean", label="Preset")
        with gr.Row():
            use_captions = gr.Checkbox(label="Burn captions", value=True)
            use_narration = gr.Checkbox(label="Local narration", value=False)
        export = gr.Button("Render", variant="primary")
        output = gr.Video(label="Rendered clip")

        locate.click(
            index_and_search, [video, prompt, duration, encoder_mode, asr_mode],
            [timeline, message, timeline_state, manifest_state, item_id, replacement, order, trim_start, trim_end],
        )
        apply.click(
            edit_from_ui, [timeline_state, manifest_state, item_id, replacement, trim_start, trim_end],
            [timeline, timeline_state, item_id, replacement, order, trim_start, trim_end],
        )
        reorder.click(
            reorder_from_ui, [timeline_state, manifest_state, order],
            [timeline, timeline_state, item_id, replacement, order, trim_start, trim_end],
        )
        item_id.change(select_item, [timeline_state, item_id], [trim_start, trim_end])
        export.click(render, [preset, timeline_state, use_captions, use_narration], [output])
    return demo


def launch() -> None:
    demo = build_demo()
    demo.launch()


if __name__ == "__main__":
    launch()

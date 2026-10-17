from __future__ import annotations

import json
from pathlib import Path

from .core import build_index, localize, render_timeline


def build_demo():
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Gradio is optional. Install with: pip install -e '.[ui]'") from exc

    state: dict[str, object] = {}

    def index_and_search(video, prompt, duration):
        if video is None:
            raise ValueError("Upload a video first")
        source = video if isinstance(video, str) else video.name
        manifest = build_index(source, "cache", window=3.0, encoder_mode="fallback")
        timeline = localize(manifest, f"{prompt} {duration}s")
        state["timeline"] = timeline
        state["manifest"] = manifest
        return timeline.to_dict(), timeline.message

    def render(preset):
        timeline = state.get("timeline")
        if timeline is None:
            raise ValueError("Run localization first")
        result = render_timeline(timeline, Path("outputs") / "quickcut.mp4", preset=preset)
        return str(result)

    with gr.Blocks(title="QuickCut MVP") as demo:
        gr.Markdown("# QuickCut\nPrompt-guided source localization with an editable timeline.")
        with gr.Row():
            video = gr.Video(label="Source video")
            prompt = gr.Textbox(label="Prompt", value="scenic travel shots")
            duration = gr.Slider(5, 15, value=8, step=1, label="Duration (seconds)")
        locate = gr.Button("Index + localize", variant="primary")
        timeline = gr.JSON(label="Validated timeline")
        message = gr.Textbox(label="Coverage")
        preset = gr.Dropdown(["clean", "warm"], value="clean", label="Preset")
        export = gr.Button("Render")
        output = gr.Video(label="Rendered clip")
        locate.click(index_and_search, [video, prompt, duration], [timeline, message])
        export.click(render, [preset], [output])
    return demo


def launch() -> None:
    demo = build_demo()
    demo.launch()


if __name__ == "__main__":
    launch()

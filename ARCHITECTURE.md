# QuickCut MVP architecture

QuickCut is the local MVP for the EditForge proposal. It accepts one source video and one text prompt, caches a source index, returns a provenance-aware 5-60 second timeline, and renders an MP4. Basic Vlog defaults to a 30-second story and supports requests up to one minute so the hook, journey, and close can explain a complete event. Timeline edits reuse the cached index.

## Runtime

- **FastAPI + Uvicorn** provide `/health`, `/index`, `/localize`, `/basic`, `/edit`, `/render`, and `/render-story`.
- **FFmpeg** handles probing, keyframe extraction, trimming, caption burn-in, audio muxing, and final MP4 rendering. `imageio-ffmpeg` is an optional bundled-binary fallback when the host has no system FFmpeg.
- **Fixed windows** (three seconds by default) keep indexing deterministic and lightweight. PySceneDetect can replace this stage later without changing the timeline contract.
- **Fallback visual encoder** is a normalized RGB histogram plus prompt color prototypes. It needs no model download and is used for the smoke-test path.
- **Optional OpenCLIP/MobileCLIP** is loaded only when `QUICKCUT_ENCODER=openclip` or `auto`; normalized image embeddings are persisted in the cache and reused across prompts. Midpoint keyframes are also cached as PNG for optional local VLM review.
- **Optional faster-whisper tiny/int8** transcribes once per source. Transcript segments are assigned to overlapping shots and saved as a versioned sidecar, so visual-only and transcript indexes never collide.
- **Local narration** uses macOS `say` or `espeak`; captions are emitted as SRT and burned locally with FFmpeg.

## Data flow

1. `quickcut.core.build_index` probes the video, extracts one midpoint keyframe per fixed window, encodes it, optionally runs local Whisper, and writes `cache/<index_id>.json` plus a transcript sidecar.
2. `quickcut.core.localize` parses duration and beats, scores candidates as visual similarity plus transcript overlap, applies a confidence threshold, and emits a `Timeline`.
3. `quickcut.story.build_basic_vlog` first performs coarse source retrieval, then either creates a deterministic hook/journey/atmospheric-close plan or asks local Qwen to select and order a whitelisted candidate pool. The resulting beat, narration, and caption metadata stay in the same Timeline.
4. `quickcut.core.update_timeline` changes trim/order/replacement metadata without touching the index. Gradio exposes those operations with source-shot replacement and strict reorder validation.
5. `quickcut.voice.render_story_timeline` renders the montage, optionally burns SRT captions, and adds a locally synthesized narration track.

## Advanced extension path

The schema supports `external` and `generated` provenance. A licensed B-roll resolver can fill illustrative gaps while concrete events stay source-grounded. The local Ollama/Qwen planner reads transcript, cached keyframes, and candidate shot metadata, then returns three story beats plus an explicit source-shot order. IDs are constrained to the candidate pool and invalid plans fall back to deterministic retrieval. Longer requests expand the candidate pool and narration budget so all three beats have enough source coverage. Dense re-sampling or a short-window VLM can refine boundaries after coarse retrieval.

CLIP is used as an image-text alignment model, not as a conversion between modalities: it is the efficient coarse retrieval signal. Running a VLM over every frame of a five-minute source would add latency and memory pressure. The intended path is fixed-window CLIP plus Whisper for the full index, then a local Qwen2.5-VL/Ollama call only on a small set of cached keyframes and transcript evidence to choose, order, and structure the montage. `--planner auto` implements that boundary and falls back cleanly when the local VLM is absent.

## Acceptance checks

- Supported prompt: returns source items with total duration in the requested 5-60 second budget.
- Unsupported prompt: returns `coverage: "abstain"` with an actionable message.
- ASR query: transcript terms affect ranking and survive cache reload.
- Edit round trip: replace, trim, or reorder a Timeline and preserve the cached `index_id`.
- Basic Vlog: output contains hook/middle/close story metadata, captions, and optional local narration.
- Export: render the edited Timeline to a playable MP4.

The deterministic tests and generated fixture exercise these checks without requiring cloud services.

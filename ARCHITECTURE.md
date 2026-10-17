# QuickCut MVP architecture

QuickCut is the local MVP for the EditForge proposal. It accepts one source video and one text prompt, caches a source index, returns a provenance-aware 5-15 second timeline, and renders an MP4. Timeline edits reuse the cached index.

## Runtime

- **FastAPI + Uvicorn** provide the production-facing process and `/health`, `/index`, `/localize`, `/edit`, and `/render` endpoints.
- **FFmpeg** handles probing, keyframe extraction, trimming, and final MP4 rendering. `imageio-ffmpeg` is an optional bundled-binary fallback when the host has no system FFmpeg.
- **Fixed windows** (three seconds by default) keep indexing deterministic and lightweight. PySceneDetect can replace this stage later without changing the timeline contract.
- **Fallback visual encoder** is a normalized RGB histogram plus prompt color prototypes. It needs no model download and is used for the smoke-test path.
- **Optional OpenCLIP/MobileCLIP** is loaded only when `QUICKCUT_ENCODER=openclip` or `auto`; normalized image embeddings are persisted in the cache and reused across prompts.
- Whisper/ASR and Qwen2.5-VL remain optional extension points. A failed or absent ASR does not block visual retrieval.

## Data flow

1. `quickcut.core.build_index` probes the video, extracts one midpoint keyframe per fixed window, encodes it, and writes `cache/<index_id>.json`.
2. `quickcut.core.localize` parses the duration and beats, scores cached shots with visual and transcript terms, applies a confidence threshold, and emits a `Timeline`.
3. A source-backed Timeline contains `provenance`, `asset_id`, `start`, `end`, `license_note`, and the original `index_id`. Unsupported queries return `coverage: "abstain"` instead of fabricating a concrete event.
4. `quickcut.core.update_timeline` changes trim/order/replacement metadata without touching the index.
5. `quickcut.core.render_timeline` creates normalized temporary clips and concatenates them with FFmpeg. The current presets are `clean` and `warm`.

## Extension path

The schema already supports `external` and `generated` provenance. A licensed B-roll resolver can fill illustrative gaps while concrete events stay source-grounded. ASR segments can be added to each `Shot` and weighted by the existing multimodal score. Dense re-sampling or a short-window VLM can refine boundaries after coarse retrieval.

## Acceptance checks

- Supported prompt: returns source items with total duration in the requested 5-15 second budget.
- Unsupported prompt: returns `coverage: "abstain"` with an actionable message.
- Edit round trip: trim or replace a Timeline item and preserve the cached `index_id`.
- Export: render the edited Timeline to a playable MP4.

The deterministic tests and a generated fixture exercise these checks without downloading model weights.

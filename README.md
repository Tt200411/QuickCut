# QuickCut MVP

QuickCut is the local MVP for the EditForge proposal: a text prompt selects relevant moments from a source video, emits a provenance-aware 5–15 second timeline, and renders an edited clip. The index is built once and edit operations reuse it.

The first runnable stack is deliberately small and production-shaped:

- fixed-window FFmpeg ingestion and keyframes (three seconds by default);
- a deterministic local color-histogram encoder for zero-download smoke tests;
- optional MobileCLIP/OpenCLIP for semantic visual retrieval;
- optional local `faster-whisper` tiny/int8 ASR with cached transcript sidecars;
- cached JSON index and validated Timeline JSON;
- FastAPI/Uvicorn HTTP service and optional Gradio workbench;
- FFmpeg rendering with `clean` and `warm` presets, captions, and local narration.

The fallback encoder is useful for checking the entire pipeline on a fresh machine. For semantic retrieval, install the ML extra and set `QUICKCUT_ENCODER=openclip`; the backend reads `QUICKCUT_CLIP_MODEL` and `QUICKCUT_CLIP_PRETRAINED`, defaulting to the small MobileCLIP-S2 configuration exposed by OpenCLIP. For transcript retrieval, install the ASR extra and opt in with `--asr auto`; the default `tiny` model runs locally on CPU with int8 weights and writes `<index_id>.transcript.json` beside the index.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,runtime]'

# Index once. This prints the cache index id.
quickcut index ./my-trip.mp4 --cache-dir cache

# Or use local Whisper ASR as part of the one-time index.
pip install -e '.[asr]'
quickcut index ./my-trip.mp4 --cache-dir cache --asr auto

# Localize a 5–15 second clip and write a timeline.
quickcut search INDEX_ID 'scenic shots and group photos for an 8 second vlog opening' \
  --cache-dir cache --output outputs/timeline.json

# Render, then edit a trim without rebuilding the index.
quickcut render outputs/timeline.json --output outputs/quickcut.mp4
quickcut edit outputs/timeline.json '[{"item_id":"item-000","end":4.2}]' \
  --output outputs/timeline-edited.json
quickcut render outputs/timeline-edited.json --output outputs/quickcut-edited.mp4
```

Start the production inference service with:

```bash
uvicorn quickcut.api:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

The optional workbench is:

```bash
pip install -e '.[ui]'
python -m quickcut.ui
```

Run a fully self-contained fixture demo (no input media or model weights needed):

```bash
python scripts/smoke_demo.py
```

Build the Vision Basic Vlog opening, including a deterministic hook/middle/close story plan:

```bash
quickcut basic ./my-trip.mp4 'scenic shots and group moments' \
  --cache-dir cache --asr auto --duration 15 --render
```

`quickcut basic` stores the story plan and captions in the Timeline JSON. `--render` burns captions and adds narration through the local macOS `say` voice (or `espeak` when available); no cloud TTS is used. Render a saved story timeline later with `quickcut render-story ...`. The story planner is a deterministic local baseline: it uses transcript evidence when present and falls back to prompt-grounded hook, journey, and atmospheric-close beats. A local Ollama/Qwen or other VLM planner can replace this function later without changing the timeline schema.

For the Advanced direction, run a local Ollama server with a Qwen2.5-VL model and opt into its story planner:

```bash
ollama pull qwen2.5vl:3b
quickcut basic ./my-trip.mp4 'freedom inside a relationship' \
  --planner ollama --asr auto --duration 15 --render
```

`--planner auto` attempts this local endpoint and falls back to the deterministic planner when Ollama is unavailable. Basic does not require a VLM: CLIP/OpenCLIP is useful for shot-level visual retrieval, while a VLM is reserved for the Advanced story interpretation step where transcript, shot order, and short-window visual evidence need to be narrated into a new structure.

For a user-owned or openly licensed internet source, the optional downloader keeps the video under a duration limit and avoids playlists:

```bash
pip install -e '.[download]'
quickcut download 'VIDEO_URL' --output-dir inputs --max-duration 900
```

The project does not hard-code or silently copy a third-party video. Pass a URL whose use and license you control, then run `quickcut index` or `quickcut basic` on the downloaded file.

## API flow

1. `POST /index` uploads a video, extracts fixed-window keyframes, and returns an `index_id`.
   Pass the multipart `asr=auto` field to run local Whisper and attach transcript text to each window.
2. `POST /localize?index_id=...&prompt=...` returns a validated timeline. If no candidate clears the confidence threshold, the response has `coverage: "abstain"` and a user-facing message.
3. `POST /basic?index_id=...&prompt=...&duration=15` creates the hook/journey/close Basic Vlog story plan.
4. `POST /edit` applies numeric in/out or replacement edits to the timeline while preserving `index_id`.
5. `POST /render` returns an MP4 generated from the timeline; `/render-story` adds local captions and narration.

Every item records `provenance`, `asset_id`, `start`, `end`, and `license_note`, matching the proposal's source/external/generated contract. B-roll is represented in the schema, while the MVP abstains when no source candidate is sufficiently grounded; this avoids inventing concrete events. The next extension is a licensed `assets/broll` resolver for illustrative beats.

## Acceptance checks

- source-supported prompt returns source items and a total duration no longer than 15 seconds;
- unsupported prompt returns `coverage: "abstain"` instead of a fabricated source segment;
- trim/replace edits preserve the cached `index_id`;
- rendered output is produced by FFmpeg and can be opened as an MP4.

See `tests/` for deterministic checks covering core retrieval, ASR sidecars, Gradio edits, story planning, and caption timing.

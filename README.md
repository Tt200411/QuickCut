# QuickCut MVP

QuickCut is the local MVP for the EditForge proposal: a text prompt selects relevant moments from a source video, emits a provenance-aware 5–60 second timeline, and renders an edited clip. The index is built once and edit operations reuse it. Basic Vlog defaults to 30 seconds so the local planner has room to explain a complete hook→journey→close event; requests up to one minute are accepted.

The first runnable stack is deliberately small and production-shaped:

- fixed-window FFmpeg ingestion and keyframes (three seconds by default);
- a deterministic local color-histogram encoder for zero-download smoke tests;
- optional MobileCLIP/OpenCLIP for semantic visual retrieval;
- optional local `faster-whisper` tiny/int8 ASR with cached transcript sidecars;
- cached JSON index and validated Timeline JSON;
- FastAPI/Uvicorn HTTP service and optional Gradio workbench;
- FFmpeg rendering with `clean` and `warm` presets, captions, local narration, and preserved source audio.

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

# Localize a 5–60 second clip and write a timeline.
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
  --cache-dir cache --asr auto --duration 45 --render
```

`quickcut basic` stores the story plan and captions in the Timeline JSON. `--render` burns captions, keeps the source soundtrack, and mixes narration through the local macOS `say` voice (or `espeak` when available); no cloud TTS is used. Render a saved story timeline later with `quickcut render-story ...`. The deterministic story planner uses transcript evidence when present and falls back to prompt-grounded hook, journey, and atmospheric-close beats. With `--planner ollama`, local Qwen2.5-VL receives a small candidate pool of retrieval-relevant keyframes, chooses the source shots, orders them, and writes `shot_order` plus per-beat `source_shot_ids`; the renderer follows that order and safely falls back if the plan is invalid. Each cut is re-encoded with stable H.264/AAC parameters before joining so hardware players receive clean reference frames.

For the Advanced direction, run a local Ollama server with a Qwen2.5-VL model and opt into its story planner:

```bash
ollama pull qwen2.5vl:3b
quickcut basic ./my-trip.mp4 'freedom inside a relationship' \
  --planner ollama --asr auto --duration 45 --render
```

`--planner auto` attempts this local endpoint and falls back to the deterministic planner when Ollama is unavailable. Basic does not require a VLM: CLIP/OpenCLIP remains useful for shot-level visual retrieval, while Qwen is invoked only on the small candidate pool to make the higher-level shot selection, ordering, and hook/journey/close structure.

For 30–60 second CPU plans, QuickCut automatically requests a larger Ollama context and a longer local planning timeout. `QUICKCUT_VLM_NUM_CTX` and `QUICKCUT_VLM_TIMEOUT` can override those defaults on slower or memory-constrained machines.

Basic montage retrieval filters likely end-credit rolls from the final portion of long sources so a broad visual prompt does not insert a production credit card. A prompt containing `credits`, `片尾`, or `谢幕` explicitly opts into those windows.

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
3. `POST /basic?index_id=...&prompt=...&duration=45` creates the hook/journey/close Basic Vlog story plan. Duration is clamped to the supported 5–60 second range.
4. `POST /edit` applies numeric in/out or replacement edits to the timeline while preserving `index_id`.
5. `POST /render` returns an MP4 generated from the timeline; `/render-story` adds local captions and narration.

Every item records `provenance`, `asset_id`, `start`, `end`, and `license_note`, matching the proposal's source/external/generated contract. B-roll is represented in the schema, while the MVP abstains when no source candidate is sufficiently grounded; this avoids inventing concrete events. The next extension is a licensed `assets/broll` resolver for illustrative beats.

## Acceptance checks

- source-supported prompt returns source items and a total duration no longer than the requested 5–60 second budget;
- unsupported prompt returns `coverage: "abstain"` instead of a fabricated source segment;
- trim/replace edits preserve the cached `index_id`;
- rendered output is produced by FFmpeg and can be opened as an MP4.

See `tests/` for deterministic checks covering core retrieval, ASR sidecars, Gradio edits, story planning, and caption timing.

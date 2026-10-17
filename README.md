# QuickCut MVP

QuickCut is the local MVP for the EditForge proposal: a text prompt selects relevant moments from a source video, emits a provenance-aware 5–15 second timeline, and renders an edited clip. The index is built once and edit operations reuse it.

The first runnable stack is deliberately small and production-shaped:

- fixed-window FFmpeg ingestion and keyframes (three seconds by default);
- a deterministic local color-histogram encoder for zero-download smoke tests;
- optional MobileCLIP/OpenCLIP for semantic visual retrieval;
- cached JSON index and validated Timeline JSON;
- FastAPI/Uvicorn HTTP service and optional Gradio workbench;
- FFmpeg rendering with `clean` and `warm` presets.

The fallback encoder is useful for checking the entire pipeline on a fresh machine. For semantic retrieval, install the ML extra and set `QUICKCUT_ENCODER=openclip`; the backend reads `QUICKCUT_CLIP_MODEL` and `QUICKCUT_CLIP_PRETRAINED`, defaulting to the small MobileCLIP-S2 configuration exposed by OpenCLIP. Whisper/ASR is kept as an extension point because the minimum loop must remain runnable without a model download.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,runtime]'

# Index once. This prints the cache index id.
quickcut index ./my-trip.mp4 --cache-dir cache

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

## API flow

1. `POST /index` uploads a video, extracts fixed-window keyframes, and returns an `index_id`.
2. `POST /localize?index_id=...&prompt=...` returns a validated timeline. If no candidate clears the confidence threshold, the response has `coverage: "abstain"` and a user-facing message.
3. `POST /edit` applies numeric in/out or replacement edits to the timeline while preserving `index_id`.
4. `POST /render` returns an MP4 generated from the timeline.

Every item records `provenance`, `asset_id`, `start`, `end`, and `license_note`, matching the proposal's source/external/generated contract. B-roll is represented in the schema, while the MVP abstains when no source candidate is sufficiently grounded; this avoids inventing concrete events. The next extension is a licensed `assets/broll` resolver for illustrative beats.

## Acceptance checks

- source-supported prompt returns source items and a total duration no longer than 15 seconds;
- unsupported prompt returns `coverage: "abstain"` instead of a fabricated source segment;
- trim/replace edits preserve the cached `index_id`;
- rendered output is produced by FFmpeg and can be opened as an MP4.

See `tests/test_core.py` for deterministic checks that do not require a model download.

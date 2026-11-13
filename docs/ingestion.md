# A: Multi-source asset ingestion

Module A imports local videos into a project and produces an `AssetManifest` 0.1.0. It owns media metadata, source timestamps, initial segmentation, keyframes, preview proxies, and transcripts. Consumers are B for retrieval, D for timeline arrangement, and F for media resolution and export.

## Run

Requires Python 3.10+, FFmpeg, ffprobe, and Pillow. Install the dependencies and run:

```bash
python -m pip install -e '.[ingest,dev]'
python -m scripts.smoke_ingest
python -m quickcut.ingest --sources examples/ingest/sources.json --project-root outputs/my-project
```

FFmpeg and ffprobe must be on PATH, or set `QUICKCUT_FFMPEG` and `QUICKCUT_FFPROBE`. FFmpeg needs libx264, PNG, and `-fps_mode` support.

To check an existing project without adding sources:

```bash
python -m quickcut.ingest --project-root outputs/my-project
```

This reuses valid stage caches and retries failed stages using the current configuration. It does not rescan external source files for changes; pass the files or source list again to import updates.

Exit codes are 0 for success, 2 for partial success, and 1 for failure. Errors include the stage, asset ID, attempt count, and `retryable` status. A failed file or stage does not discard other successful results.

## Asset identity and storage

The first import assigns an `asset_id` and calculates a SHA-256 content hash. Identical files within the same origin share an identity; user and external assets are registered separately.

A source path or explicit `source_key` acts as an import alias. Changing the content behind a known alias retains its asset ID and increments its revision. A moved, unchanged file can be identified by hash. Use a stable `source_key` when changing both its location and content.

Originals are copied to `media/<origin>/<asset_id>/r<revision>.<ext>`. Older revisions are retained. Public URIs use `/` and resolve relative to the project root, so the package can be moved. External paths in `.quickcut/registry.json` are only import aliases.

An explicitly reimported missing or damaged file reports a failure rather than returning its old result as a current success. Asset deletion and automatic cleanup of old revisions are not implemented.

## Manifest fields

The document envelope follows the [team contract](team/contracts.md). Document revisions increase when manifest content changes. Cache-hit details are stored separately in `.quickcut/run-report.json`.

| Field | Meaning |
|---|---|
| `assets[]` | Identity, revision, origin, URI, hash, duration, video parameters, and audio parameters |
| `segments[]` | `coarse_event` windows and `coarse_shot` candidates, linked through `parent_segment_id` |
| `source_range_us` | Integer microseconds relative to the first decoded video frame; half-open `[begin,end)` intervals |
| `keyframes[]` | PNG URI and the selected frame's source timestamp |
| `quality` | `usable` means a keyframe is readable; containers, empty windows, and failed extractions are unusable |
| `transcript` | Recognition status, timestamped text, configuration, and sidecar URI |
| `proxy_uri` / `proxy_mapping_uri` | Video proxy and per-frame source/proxy PTS map; omitted when proxy generation fails |
| `source_pts_uri` | Decoded source-frame PTS records |
| `processing` | Overall status and errors |
| `embedding_profile` | `status: not_indexed, owner: B`; the retrieval module produces embeddings |

Retrieval should consume `coarse_shot` segments with `quality.usable: true`. Coarse windows are containers. The usable flag does not assess sharpness, composition, or semantic relevance.

Processing states and timestamp maps extend the current contract and need consumer confirmation with B/D/F. The new manifest cannot be passed directly to the existing single-source `quickcut search`, API, or UI, which still use `IndexManifest`.

## Source time and proxies

Source time is calculated from decoded PTS as `(source_pts - source_origin_pts) × timebase`, then converted to integer microseconds. Frame rates remain rational values; average FPS is not used to infer positions in variable-frame-rate video.

Proxies are scaled H.264 video without audio. Original sound remains a reference to the original asset. Proxy generation preserves frame timing with passthrough mode and rebases timestamps to zero. The frame counts must match, and each mapped timestamp must be within 2 microseconds of the source; otherwise the proxy stage fails. Final export should use the original media.

ASR extracts a mono 16 kHz WAV and accounts for audio offsets relative to the video origin. Transcript intervals use the same source coordinate and are bounded by the media duration.

## ASR and caching

ASR is disabled by default. Install `.[asr]` and pass `--asr faster-whisper` to enable it.

| Status | Meaning |
|---|---|
| `no_audio` | No audio stream |
| `disabled` | Audio exists, but recognition is disabled |
| `no_speech` | Recognition ran and returned no usable speech |
| `success` | A usable transcript was returned |
| `failed` | Dependency, weight, decoding, or inference failure |

The model can be a supported name such as `tiny` or a local CTranslate2 weight directory. Named models resolve to a concrete snapshot. Its weight files are hashed, then the same directory is used for inference. Initial download requires network access. `--asr-model-revision` selects a model-repository revision when supported by the installed faster-whisper version; a fixed local weight directory is also supported.

Probing, keyframes, proxies, and ASR have separate caches. Fingerprints include source content, processing parameters, tool versions, and library versions. ASR additionally records actual weight hashes, language, device, compute type, and preprocessing settings. Output hashes and PNG decoding detect missing or damaged cache files. Changing recognition settings invalidates ASR without rebuilding keyframes or proxies; adding assets reuses existing work. Failed results are not reused as successful cache entries.

Projects use a single-writer lock. Parallel jobs must use separate project directories. After an interrupted process, confirm that no ingestion task is running before removing `.quickcut/ingest.lock`. Stage-cache reuse is supported; full interrupted-job recovery is not implemented.

## Tests and limitations

```bash
python -m pytest tests/test_ingest.py tests/test_asr.py tests/test_encoder.py -q
python -m scripts.smoke_ingest
```

Tests cover identity, revisions, project relocation, cache invalidation, partial failures, variable-frame-rate mapping, and nonzero source origins. ASR tests use mocks for status and timestamp handling. The smoke test uses generated patterns, tones, and silence; it does not measure recognition accuracy.

Real Mandarin recognition, rotated/HDR phone footage, and integration with B/D/F still need validation. Per-frame PTS storage uses memory and JSON space proportional to frame count, and batched extraction decodes the source repeatedly. Long-video performance has not been optimized. Scene detection, speech-pause boundaries, and visual quality scoring are not implemented.

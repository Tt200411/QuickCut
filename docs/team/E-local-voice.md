# E: Local Voice and Subtitle Handoff

## Purpose and consumers

E converts C's approved sentences into independent WAV files, measured durations, local subtitles, and timing feedback. D consumes VoicePackage and owns global timing. F resolves generated assets against D's frozen timeline and mixes separate tracks. E does not change the story, shots, source audio, or global timeline.

This delivery covers all P0 requirements in E's task description. IndexTTS2 adds optional neural synthesis conditioned on a local speaker reference. The existing `voice.py` stays compatible. The new entry point explicitly returns VoicePackage instead of calling `synthesize_timed_narration`, which mutates the legacy Timeline.

## Protocol and entry points

- Input: StoryPlan, `schema_version: "0.1.0"`. The CLI can also extract StoryPlan from the golden bundle.
- Output: VoicePackage, `schema_version: "0.1.0"`, `owner: "E"`, `timing_authority: "D"`.
- Python API: `quickcut.voice_package.build_voice_package(story, project_root, ...)` returns a new dictionary without modifying its input.
- CLI: `python -m quickcut.voice_package`. Requires Python 3.10+. Real synthesis requires FFmpeg and either a system voice engine or a separately installed IndexTTS2 runtime. `--capabilities` lists installed voices, languages, and supported parameters.
- Verified on macOS: Reed, Mandarin Chinese (mainland China), rate 145, 48 kHz mono PCM16. The espeak adapter is implemented but has not undergone real platform listening acceptance.
- Synthesis runs locally. Optional Whisper verification requires an initial model download and can run offline afterward.

All media URIs are relative to `--root`, use `/`, and contain no personal absolute paths. Asset IDs hash the project, cue, and synthesis configuration and use the `generated-voice-` prefix. D/F must merge these with A's manifest and check ID uniqueness.

Documentation and descriptive metadata are in English. Mandarin test scripts, matching subtitles, raw ASR hypotheses, and exact system voice identifiers retain their original language so the recorded audio and evidence remain consistent.

## IndexTTS2 narration

Install the [official IndexTTS2 runtime](https://github.com/index-tts/index-tts/blob/main/docs/cli_v2_usage.md) in an isolated Python 3.11 environment. This integration was verified with upstream commit `d9e41aac89fd00b3d71497fddb287b7f24613712` and the [IndexTeam/IndexTTS-2 weights](https://huggingface.co/IndexTeam/IndexTTS-2/tree/740dcaf) at revision `740dcaf`. Download the pinned weights and prepare the auxiliary models under `hf_cache` with the upstream helper. The model and the speaker WAV stay in local ignored directories.

From the QuickCut repository root, the following layout matches the checked-in configuration:

```bash
git clone https://github.com/index-tts/index-tts.git outputs/indextts2-runtime/upstream
git -C outputs/indextts2-runtime/upstream checkout d9e41aac89fd00b3d71497fddb287b7f24613712
uv sync --locked --python 3.11 --project outputs/indextts2-runtime/upstream
outputs/indextts2-runtime/upstream/.venv/bin/hf download IndexTeam/IndexTTS-2 \
  --revision 740dcaf --local-dir outputs/indextts2-runtime/checkpoints \
  --include '*.pth' '*.pt' '*.yaml' '*.model'
outputs/indextts2-runtime/upstream/.venv/bin/python -c \
  'from indextts.utils.model_download import ensure_models_available; ensure_models_available("outputs/indextts2-runtime/checkpoints")'
mkdir -p inputs
cp /path/to/your/speaker.wav inputs/indextts2-reference.wav
```

Use the IndexTTS2 environment to launch QuickCut. The configuration uses `mps` on Apple Silicon. Set `device` to `cpu` or `cuda:0` for a matching runtime on another machine. Paths in the configuration are relative to the working directory.

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTORCH_ENABLE_MPS_FALLBACK=1 \
  outputs/indextts2-runtime/upstream/.venv/bin/python -m quickcut.voice_package \
  --story examples/voice_package/story.json \
  --config examples/voice_package/indextts2.config.json \
  --root outputs/indextts2-voice --mode measure
```

QuickCut loads the model on the first uncached cue, reuses it for the remaining cues, and converts its WAVs to 48 kHz mono PCM16 before the existing timing and subtitle pipeline. Each cue's synthesis profile records the checkpoint revision, runtime version, device, seed, and SHA-256 of the speaker reference. Update `model_revision` when changing weights. Cue overrides may set `reference_audio` and `seed`. The model uses the reference audio to condition its delivery. Keep actual reference recordings and downloaded checkpoints local.

Verified on Apple Silicon MPS with the same reference and seed as the listening test. Final WAVs measured approximately 0.90s, 2.73s, and 10.00s, including configured sentence-end padding. The third cue exceeds its one-second budget and returns `needs_retime` with complete audio and subtitles.

## Reproduce from the repository root

Contract mode generates test tones, not speech. Every output has `fixtures_only: true`:

```bash
python scripts/demo_voice_package.py --fixture --root outputs/e-fixture
```

Real mode produces three Mandarin WAVs, a single-sentence revision, a requested-interval fit, and verification results:

```bash
python scripts/demo_voice_package.py --root outputs/e-real
```

Single invocation:

```bash
python -m quickcut.voice_package --capabilities
python -m quickcut.voice_package --story examples/voice_package/story.json \
  --config examples/voice_package/config.json --root outputs/e-once \
  --mode measure --revision 1
```

The third sentence deliberately exceeds its budget, so exit code **2** is expected. Audio and JSON are still written in full. The demo checks this expected outcome and returns 0. The CLI returns 0 for `ready` and 2 for retiming, partial success, or failure; consumers must inspect the JSON too. Default output is `voice_package-r1.json`. Existing documents cannot be overwritten: use a new revision or output name. Audio is content addressed, preserving previous media.

Requested-interval fitting:

```bash
python -m quickcut.voice_package --story examples/voice_package/story.json \
  --config examples/voice_package/config.json --request examples/voice_package/fit-request.json \
  --root outputs/e-fit --mode fit --revision 3
```

The response echoes `project_id`, exact `story_plan` and `timeline` references, `cue_ranges`, and `iteration`. Initial measurement needs no EditTimeline. Adaptation iteration must be 1 or 2; a third round is rejected for D/C to resolve. D must match the echoed timeline reference against its current pending request. E cannot infer that D has created a newer timeline on another machine from an old request alone.

For local regeneration, update the StoryPlan revision, pass the previous VoicePackage through `--previous`, and increase `--revision`. Under the same `--root`, caching uses approved text, engine/system version, actual voice, language, rate, and silence-processing version. A hash mismatch regenerates the cache. `cue_overrides` can change voice/rate for one sentence.

## Audio processing and duration semantics

- Integer microsecond durations come from actual PCM sample counts, not character counts.
- `engine_duration_us` measures the complete engine WAV after conversion; `raw_duration_us` measures speech after boundary silence removal.
- Only boundary silence below approximately -50 dBFS is removed, with 40 ms protective handles. Internal pauses remain intact. All-silent output returns `NO_SPEECH`; engine errors return `TTS_FAILED`. Silence is never substituted for successful speech.
- Default sentence-end padding is 180000 microseconds and can be zero. `duration_us` measures the complete final WAV including padding; `speech_end_us` excludes added padding.
- `measure` never changes tempo and only reports fit. `fit` uses pitch-preserving atempo when necessary. Default permitted speed is 1–1.15, with a configuration ceiling of 1.25. Short speech is not slowed merely to fill space. Processed files are measured again; remaining overruns return `needs_retime`, without truncating final words or forcing duration with atrim.
- `duration_budget_us` is the current budget or D's requested interval length. Over-budget WAVs are retained in full. D should first extend valid footage/B-roll; if that is insufficient, C revises the text.
- `local_subtitles[].range_us` is a half-open interval relative to that sentence's audio origin. Subtitles preserve approved text and cover processed speech, excluding added sentence-end padding. Each sentence also has a local SRT. D maps these to global time; F preserves editable text tracks. Local SRTs cannot simply be concatenated into a global SRT.
- Durations are quantized at 48 kHz, with microsecond rounding of at most roughly half a sample. SRT provides millisecond preview timing; JSON microseconds remain authoritative for handoff.

## Status, errors, and invalidation

Package status is `ready` when every usable cue fits, `needs_retime` for overruns, `partial` when some synthesis fails, and `failed` when all synthesis fails. An empty narration list yields an empty `ready` package. Each cue retains its own `fit_status`; failed cues have no fabricated audio assets.

Error envelopes contain `stage: voice`, project ID, affected cue IDs, code, explanation, and next owner. Codes include `STALE_INPUT_REVISION`, `UNSUPPORTED_VOICE`, `UNSUPPORTED_PARAMETER`, `INVALID_INPUT`, and `OUTPUT_EXISTS`. Overruns return `VOICE_NEEDS_RETIME` to D; E handles synthesis failures.

Changing text, voice, or adaptation configuration changes the affected generated asset. `invalidated_cue_ids` lists changed, removed, or failed cues. `alignment_invalidated` tells D to re-align. Reusable WAVs do not make an old VoicePackage valid for a new script: the new package references the approved StoryPlan revision, and audio/subtitle records share that script reference.

## Compatibility with the existing contract

Core fields, time units, local subtitles, and references follow the golden fixture. Optional additions include request, mode, script_input, srt_uri, cache_key/cache_hit, speech_end_us, sentence_pause_us, engine_duration_us, quality, errors, invalidated_cue_ids, and alignment_invalidated. Consumers may ignore diagnostics but must recognize failures and needs_retime.

The golden `examples/team_contracts/validate.py` checks a fixed, fully arranged successful bundle. Its requirement that each cue fit its budget must not reject E's legitimate overrun feedback. This delivery does not modify the shared protocol or fixed success fixture. E's independent verifier supports real over-budget packages and checks media, hashes, subtitles, and script references:

```bash
python scripts/verify_voice_package.py --story examples/voice_package/story.json \
  --package examples/voice_package/real/voice_package.json \
  --root examples/voice_package/real
python examples/team_contracts/validate.py --self-test
```

Shared core fields are unchanged. D/F should review the optional extensions and block failed/partial/needs_retime packages from final export. D remains responsible for accepting exact cross-module revisions.

## Delivered examples and evidence

`examples/voice_package/` contains handwritten scripts, configuration, real WAVs/local SRTs, measurement/revision/fit packages, fit requests and responses, fixtures, and validation logs. Scripts test TTS and do not claim verified visual events. Real speech and synthetic test-tone fixtures are labeled separately.

Initial measurements: cue-01 is 904021 microseconds, cue-02 is 2862729 microseconds, and cue-03 is 11782479 microseconds. Cue-03 has a 1000000-microsecond budget and correctly returns needs_retime. The fitted first cue uses tempo approximately 1.08063 and measures 841667 microseconds within an 850000-microsecond interval. Editing the first sentence changes only cue-01; the other two hit the cache.

Tests cover boundary silence/internal pauses, full over-budget audio retention, bounded tempo, revision/project/request validation, text/voice edits, corrupt caches, partial/complete failure, silence, path traversal, and output protection. Full results and environment are in `evidence/pytest.txt` and `evidence/requirements-tested.txt`. Run:

```bash
python -m pip install -e '.[dev,runtime,server]' pillow
python -m pytest -q
```

Optional local ASR verification preserves raw hypotheses and never rewrites subtitles:

```bash
python -m pip install 'faster-whisper==1.2.1'
python scripts/check_voice_package_asr.py --model small \
  --root examples/voice_package/real --package examples/voice_package/real/voice_package.json \
  --report outputs/asr-small.json
```

Whisper base/small responses are saved. The long sentence has approximately 0.977 similarity with small and a detected closing phrase. Short/normal sentences include recognition errors and traditional characters; they are not labeled exact ASR passes, and approved subtitles remain unchanged. Sample peaks do not clip, and media/local-subtitle checks pass. Pronunciation and naturalness still require listening; ASR is not listening acceptance. D's real arrangement integration, F's source-audio mix, and actual Jianying import remain downstream/team acceptance work.

# Local Chinese summary workflow

The completed example condenses the forest confrontation into 58.5 seconds. Twenty Qwen-selected
source windows run chronologically from 381 to 493.5 seconds. The opening establishes the rabbit
and flying squirrel, the middle follows the glide and obstacles, and the ending shows the capture
and kite reveal. The source's credit text begins around 495 seconds; the reviewed exclusion boundary
is 494 seconds. All 212 index windows have freshly decoded keyframes. Local Whisper found no usable
spoken dialogue in the source.

The local 3B VLM initially reversed the animals and omitted the ending when given four contact
sheets together. Those plans were rejected. The accepted workflow sends one numbered contact sheet
per act, with a reviewed source interval and required causal anchors. Qwen selects 6 / 8 / 6 windows,
then corrects its own ordering through additional local calls. The exact responses, candidate images'
hashes, model counters, and corrections remain in the inference JSON. Chinese narration was edited
against the actual frames; it does not repeat the model's incorrect claim that the squirrel catches
the rabbit or catches the falling fruit.

## Reproduce

Requirements: local `qwen2.5vl:3b` in Ollama, locally downloaded faster-whisper `tiny` (source ASR)
and `small` (output validation), macOS Mandarin voices, FFmpeg, ffprobe, Pillow and NumPy.
Model inference and TTS run locally. No remote generation endpoint is used.

```bash
HF_HUB_OFFLINE=1 .venv/bin/python scripts/chinese_summary.py index \
  inputs/aqz-KE-bpKQ.mp4 --start 378 --end 493.5

.venv/bin/python scripts/plan_summary_acts.py \
  outputs/chinese_summary/planning_manifest.json \
  examples/chinese_summary/act_config.json \
  --output outputs/chinese_summary/qwen_plan.json

.venv/bin/python scripts/chinese_summary.py render \
  --cues examples/chinese_summary/editorial_cues.json \
  --voice 'Reed (中文（中国大陆）)' --rate 145

.venv/bin/python scripts/verify_chinese_summary.py \
  outputs/chinese_summary/final_chinese_summary.mp4 \
  --original-only outputs/chinese_summary/original_only_captioned.mp4 \
  --narration outputs/chinese_summary/narration.wav \
  --report outputs/chinese_summary/verification.json

.venv/bin/python -m pytest -q
.venv/bin/python scripts/smoke_demo.py

HF_HUB_OFFLINE=1 .venv/bin/python scripts/check_narration_coverage.py \
  outputs/chinese_summary/final_chinese_summary.mp4 --model small \
  --closing-phrase '终于笑了' --report outputs/chinese_summary/coverage.json
```

A new model run may select different windows. Review its contact sheet and adapt the editorial cues
before rendering a new selection. The checked example's actual model responses and order are in
`examples/chinese_summary/qwen_selection.json`; these are provenance, not a replacement visual cache.

## Timing and audio

Reed, Rocko and Grandpa Mandarin samples were synthesized at rate 145. Reed had the most stable
low-frequency periodicity in this comparison (median estimated fundamental about 109.7 Hz) and the
best local tiny-Whisper character accuracy among the three. This is an acoustic/ASR comparison,
not a claim of human listening. The final voice is **Reed (中文（中国大陆）), rate 145**.

Each sentence is synthesized once, its WAV duration is measured, boundary silence is trimmed, and
pitch-preserving tempo fitting places it into its reviewed visual interval. The JSON records each
raw duration, fitting ratio, cue and speech end. Eleven cues cross cuts continuously and exactly match
the spoken text. Subtitle burning always re-encodes audio.

Every clip and the concat stage reset video PTS and use `asetpts` plus
`aresample=async=1:first_pts=0`. PCM intermediate clips avoid accumulating AAC priming at every cut.
The final mix keeps the source stereo soundtrack at gain 0.30 and adds narration normalized to
−18 LUFS with peak limiting. The captioned original-only video is retained as the exact mix reference.

## Verified result

- H.264, 1280×720, 60 fps; AAC, stereo, 48 kHz.
- Video, audio and container duration: 58.500 seconds.
- Video/audio stream start and first decoded frame PTS: 0.
- FFmpeg full decode: exit 0, no error diagnostics.
- Eleven Chinese subtitle cues: no overlaps or zero lengths, ending at 58.500 seconds.
- Decoded stereo sample regression: source gain 0.298345; source correlation after removing narration
  0.952908; mix reconstruction correlation 0.997924. A narration-only negative control fails the
  same source-retention test.
- All 82 tests passed, including actual audiovisual render tests and planner rejection cases.
- Local Whisper small word alignment places the final spoken “了” at 58.24 seconds on the isolated
  narration track, 0.26 seconds before the end. Raw ASR can contain homophone errors and a spurious
  trailing credit phrase; the recorded TTS text, not the ASR hypothesis, supplies the subtitles.

The four delivery files and additional contact sheets, source-ASR sidecar, narration WAV, original-only
references, Whisper output checks, and verification report are under `outputs/chinese_summary/`.
Generated video/audio assets remain outside Git; the code and reproducible editorial configuration
are committed. Exact task-level input/output/tool token counters are unavailable in this platform.

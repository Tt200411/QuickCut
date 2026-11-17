# QuickCut MVP

完整共享交付见 [2026-11-17 交付目录](deliveries/quickcut-20261117/README.md)：参考音频、男声成片、剪映工程、历史运行和验收证据均已纳入。克隆后运行 `git lfs pull` 下载媒体；模型权重按部署配置下载。

## 本地网页工作台

双击 [QuickCut.command](QuickCut.command)，或执行 `./scripts/quickcut_web.sh`，打开 `http://127.0.0.1:8765`。
导入视频并输入剪辑要求，查看真实模型生成的选镜/旁白，确认后使用 IndexTTS2 配音，下载剪辑视频与四轨工程，并自动启动剪映。
缺模型或运行失败会明确提示，支持保留素材重试和返回草案改稿。

当前使用说明、环境和验收边界见 [HTML 工作台](docs/team/WORKBENCH.md)。下文保留的单视频原型和合成工程 demo 用于兼容与回归，不是网页的生产路径。

## Team implementation guide

Start with the [Chinese project guide](docs/PROJECT_GUIDE.zh-CN.md) for the shared product goal,
coarse-to-fine multi-source workflow, local model choices, and the six-person implementation plan.
It records the original prototype and multi-source plan; the maintained multi-source user entry point is now the HTML workbench above.

- [C → A 素材协作：需求卡、缺口诊断与交接清单](docs/team/C_A_HANDOFF.md)
- [全链路工程说明：入口、交接与已知限制](docs/team/PIPELINE_ENGINEERING.md)
- [验收操作手册：命令、退出码、CI 与证据](docs/team/ACCEPTANCE_RUNBOOK.md)
- [F：剪映可编辑工程导出、迁移安装与验收状态](docs/team/F-export.md)
- [E：IndexTTS2 本地部署、参考音频与配音交接](docs/team/E-local-voice.md#indextts2-narration)
- [MVP capabilities and reuse map](docs/team/mvp-baseline.md)
- [A-F workstreams, deliverables, and acceptance criteria](docs/team/workstreams.md)
- [Module contracts and revision rules](docs/team/contracts.md)
- [Contract fixtures and validator](examples/team_contracts/README.md)
- [视频素材的 Git LFS 上传、下载与故障处理](docs/team/git-lfs.md)
- [Editable workflow diagrams and PNGs](figures/README.md)
- [Project handbook PDF](output/pdf/QuickCut_%E9%A1%B9%E7%9B%AE%E6%8C%87%E5%AF%BC%E6%89%8B%E5%86%8C.pdf)

## 工程演示入口

从仓库根执行；需要 Python 3.10+、FFmpeg/ffprobe 和已安装的 `.[dev,ingest,runtime]`。严格 doctor 另检查系统 TTS（macOS say 或 espeak-ng/espeak）。demo 项目根必须不存在或为空，不能是符号链接。

```bash
python scripts/doctor_pipeline.py --require-media
python scripts/demo_pipeline.py --root outputs/pipeline-demo-10s \
  --case supported --target-seconds 10
python scripts/doctor_pipeline.py --require-media \
  --project-root outputs/pipeline-demo-10s
```

从项目根 `run-report.json` 的 `render.files` 读取 timeline、manifest、voice_package，再调用 [preview 脚本](scripts/render_timeline_preview.py)；可直接运行的映射示例、四种用例和退出码见[验收手册](docs/team/ACCEPTANCE_RUNBOOK.md)。`--manifest` 使用 A 清单，不能传 assets_union。preview 必需 ffprobe，生成真实 MP4、独立可编辑 SRT 和报告，并按 Timeline 的 `preview_burn_subtitles` 决定是否烧录中文字幕；剪映工程另用下述 F 入口导出。

`--case` 支持 supported/unsupported/edit，`--target-seconds` 支持 5/10/15。10 秒 supported/edit 预期退出 0；unsupported 退出 2 并返回 needs_input；5 秒固定音调触发 needs_retime，顶层 failed、退出 2，不生成 Timeline。当前 demo 已执行 B 检索，但使用合成画面上的测试文案做字面匹配，配合 rule C、预设批准和 E 音调；不代表真实视觉检索/Qwen/人审/自然语音、自动 fit 或剪映验收。C 的真实 Ollama 路径已支持 `python -m quickcut.planning run --project-root PROJECT_ROOT ...`。

IndexTTS2 使用 `IndexTeam/IndexTTS-2` 权重 `740dcaf`，通过 `python3 scripts/setup_indextts2.py` 安装到独立 Python 3.11 环境。准备参考 WAV 后，用 `scripts/indextts2_python.sh` 运行 E；doctor 和 demo 均支持 `--voice-config examples/voice_package/indextts2.config.json`。指定模型配置后，缺文件或推理失败会停止，不会回退系统语音。乡村真实链路默认选择 IndexTTS2，系统语音对照需显式传 `--system-voice`。具体命令和其他模块依赖见 [E 部署说明](docs/team/E-local-voice.md#indextts2-narration)。

2026-11-13 的历史集成包含 A PR #4 `cf988d0a0e65dbd63c4174fae83bda77acf86723`、D #2 `38dcb8b`、E #1 `d1c6c95` 和 C #5 `620b24f`。该轮本地全量回归为 **812 passed in 35.50s，无 skip（macOS / Python 3.13.12）**；旧 A 环境的 805 项结果也仅作历史记录。原始证据保存在仓库外 `../outputs/QuickCut-engineering-final-20261113/`（未随 Git 提交）：supported/edit 10 秒成功，unsupported 10 秒退出 2、`needs_input`（A/E/D=1/0/0）；5 秒退出 2，`VOICE_NEEDS_RETIME` 指向 cue-03、owner D（A/E/D=1/1/0）。该轮 supported 预览实测 **10 秒、300 帧、全量解码通过**，输出 MP4 和独立 SRT，未烧字幕。详细报告索引见[验收手册](docs/team/ACCEPTANCE_RUNBOOK.md#54-最终本地证据索引2026-11-13)。当时远端 CI 尚未运行；这份历史记录不代表当前分支或云端 CI 结果。

proposal 第 1 页 BACKGROUND 的短片验收为约 5 分钟源 → **5–15 秒**；约 45 秒 opening 是愿景，下述旧 MVP 的 5–60 秒（Basic 默认 30 秒）与 18 秒协议 fixture 分别保留，不替代短片验收。

## F：导出可编辑剪映工程

F 读取冻结的 `EditTimeline`、A `AssetManifest` 和 E `VoicePackage`，打包原片、独立原声、完整旁白 WAV、可编辑文字字幕及校验清单。首版适配 **macOS 剪映专业版 6.1.0**；成功生成记录为 `generated`，不会自动宣称完成实机验收。

```bash
mkdir -p outputs
python scripts/export_project.py \
  --timeline ABCDE/timeline.json --manifest ABCDE/asset-manifest.json \
  --voice-package ABCDE/voice-package.json --project-root ABCDE \
  --output outputs/countryside-f-new --preview

# 完全退出剪映后安装为新草稿；默认不会覆盖已有草稿。
python scripts/install_jianying_draft.py outputs/countryside-f-new
```

输出目录必须尚不存在。默认拒绝 fixture；仅测试时在导出和安装两步分别加 `--allow-fixtures`。迁移时复制完整包，再运行安装器重建媒体路径。`ABCDE/f-inputs.json` 和 `run-report.json` 内的 `project_root: "."` 均相对于这两份文档所在目录；从仓库根运行 CLI 时应传 `--project-root ABCDE`。

截至 2026-11-15，本机 6.1.0 的 fixture 与真实 30 秒 countryside 草稿均已打开并观察到独立四轨；fixture 字幕修改，以及真实片的字幕修改、旁白替换、画面移轨，均已观察到保存重开后保留。这些编辑操作针对 v1 包；修复后的 v2 包已搬迁、校验、重新安装，并在剪映打开后确认所有素材在线，保留干净草稿 `QuickCut-Countryside-30s-正式预览` 供用户查看。原始导出包保持 `generated`，各版本的验收范围记录在[独立验收记录](docs/team/F-acceptance-20261115.md)。Python API、支持边界、文件校验和迁移步骤见 [F 导出说明](docs/team/F-export.md)。

## A: Multi-source asset ingestion

Import user videos and external B-roll into a project with `quickcut-ingest` or
`python -m quickcut.ingest`. The output is an `AssetManifest` 0.1.0 with copied originals,
keyframes, preview proxies, timestamp maps and optional transcripts.

Requires Python 3.10+, FFmpeg and ffprobe on PATH, and `pip install -e '.[ingest]'`.
You can also set `QUICKCUT_FFMPEG` and `QUICKCUT_FFPROBE` to the binary paths.
FFmpeg needs libx264, PNG and `-fps_mode` support; the runtime extra does not include ffprobe.

```bash
# Generate test videos and check ingestion and cache reuse; no models required.
python -m scripts.smoke_ingest

# Import videos, keeping user and external sources separate.
python -m quickcut.ingest inputs/short.mp4 inputs/long.mp4 \
  --external inputs/forest.mp4 --project-root outputs/my-project

# Batch input; paths are relative to sources.json.
python -m quickcut.ingest --sources examples/ingest/sources.json \
  --project-root outputs/my-project

# Enable local speech recognition. Weights may be downloaded on the first run.
pip install -e '.[asr]'
python -m quickcut.ingest --project-root outputs/my-project \
  --asr faster-whisper --asr-model tiny --asr-language zh
```

On Windows PowerShell, run each command on one line. For a virtual environment, use
`py -m venv .venv`, then `.\.venv\Scripts\python.exe -m pip install -e '.[ingest,dev]'`.

Exit codes are 0 for success, 2 for partial success and 1 for failure. Check the manifest's
`processing` field for errors. Unchanged stages reuse cached outputs.

The existing `quickcut index/search/basic`, API and UI still use the single-source format.
Ingestion also supports ASR thread/worker limits, vocabulary hints, word diagnostics, and
review flags. ASR `status: success` 仅表示执行成功；`review.status: needs_review` 保留人工复核要求，启发式无标记也不等于准确率获批。范围与质量证据见 [A ASR 质量说明](docs/team/A-asr-quality.md)。Use
`python -m quickcut.asr_eval` to compare a sidecar with a human-verified reference.
See [Ingestion reference](docs/ingestion.md) for fields, cache rules and limitations, and
[Batch input example](examples/ingest/README.md) for batch configuration.

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

Basic montage retrieval filters likely end-credit rolls from the final portion of long sources so a broad visual prompt does not insert a production credit card. A prompt explicitly requesting credits opts into those windows.

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

## Local Chinese narrated summaries

The audited Chinese summary workflow is documented in [docs/chinese-summary.md](docs/chinese-summary.md).
It rebuilds real keyframes, runs local Whisper and Qwen2.5-VL, synthesizes sentence-timed Mandarin speech,
keeps the source soundtrack, and verifies decoded timestamps and source-audio retention. Qwen's raw
selection and order corrections are saved; invalid plans are rejected instead of silently padded.

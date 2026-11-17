# B 本地视觉模型部署

当前真实入口是仓库根目录的 `QuickCut.command`，或运行 `sh scripts/quickcut_web.sh` 打开工作台。工作台从 A 的真实关键帧生成 B 候选，B 使用独立的 `.venv-vision/bin/python`，与 A/C 主环境及 IndexTTS2 环境分开运行。

本说明记录安装资源的可复现方式。资源检查通过只表示模型可以进入加载阶段；真实检索是否成功，以本次任务生成的 `CandidateSet`、模型配置和日志为准。

## 安装与检查

在仓库根目录执行，准备好 `uv`（脚本也会查找 `~/.local/bin/uv`）：

```bash
# 已有部署的离线只读检查：不下载、不安装、不加载模型，不改写部署清单。
python3 scripts/setup_vision.py --check

# 首次安装，或补齐缺失资源。
python3 scripts/setup_vision.py

# 保持已有 Python 环境，仅校验并补齐模型文件；环境版本不符会报错。
python3 scripts/setup_vision.py --skip-install
```

完整安装固定 Python **3.11.15**，只操作 `.venv-vision`，不修改项目 `.venv` 或 IndexTTS2 环境。依赖及其传递依赖版本写在 `scripts/setup_vision.py`，核心版本为 OpenCLIP **2.32.0**、PyTorch **2.8.0**、torchvision **0.23.0**、transformers **4.57.6**、huggingface-hub **0.36.2**。本机环境为 macOS Apple Silicon；其他平台的包可用性需在目标机器验证。

安装时不要同时运行正在使用该环境的分析任务。已有完整、哈希正确的模型缓存直接复用；缺失或损坏的文件才会下载。下载损坏时最多强制重试一次，仍失败则退出。`--check` 校验全部文件的 SHA256，返回 JSON；退出码 `0` 表示检查通过，`1` 表示缺失、损坏、环境版本漂移或清单不一致。

默认资源目录是 `outputs/vision-runtime`，可用 `--runtime-root PATH` 改变安装和检查的位置。当前工作台读取默认目录；自定义目录主要用于独立部署与 CLI，若要接入工作台需同步其资源配置。

## 固定资源

| 资源 | 固定版本 | 下载范围 |
|---|---|---|
| `visheratin/nllb-clip-base-oc` | `f16398ba58d1d5cff27450ce2fe971b087969444` | `open_clip_pytorch_model.bin`、`open_clip_config.json` |
| `facebook/nllb-200-distilled-600M` | `f8d333a098d19b4fd9a8b18f94170487ad3f821d` | 配置、SentencePiece 和 tokenizer 文件，共 5 个 |

共 **7 个文件，2,029,864,328 字节**，另外需要 Python 依赖的磁盘空间。CLIP 主权重包含文本塔，不再下载独立的 NLLB 文本权重。

主权重大小为 **2,007,675,592 字节**，SHA256 为：

```text
55f4101efcb112643c0b62e650dd891dbf7f0b944a587bfd2e2c8e27663f098f
```

每个文件的预期大小和 SHA256 固定在安装脚本中。缓存采用 Hugging Face 的 `hf-cache/models--…/snapshots/<revision>/` 格式。验证成功后原子写入 `outputs/vision-runtime/deployment-manifest.json`，记录实际 Python、包版本、每个文件的路径/长度/哈希及运行环境变量。安装开始时状态为 `installing`；异常时为 `error`，不会留下成功标记。`inference_run: false` 表示安装脚本没有做模型推理。

## 独立运行真实 B

工作台会读取部署清单，将固定本地权重和文本配置传给独立 B 进程，并离线运行。独立 CLI 也应读取同一份清单，避免只写 `pretrained=v1` 而重新访问其他缓存：

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess

deployment = json.loads(Path("outputs/vision-runtime/deployment-manifest.json").read_text())
if deployment["status"] != "ready":
    raise SystemExit("请先完成 scripts/setup_vision.py --check")
environment = dict(os.environ, **deployment["environment"])
subprocess.run([
    deployment["python"], "-m", "quickcut.cli", "retrieve",
    "path/to/asset-manifest.json", "path/to/retrieval-request.json",
    "--project-root", "path/to/project",
    "--output", "path/to/candidate-set.json", "--encoder", "openclip",
], env=environment, check=True)
PY
```

将示例路径换成同一真实项目的 A 清单与 C 请求；请求必须精确引用清单版本。关键帧路径相对于 `--project-root`，外部素材必须先由 A 注册到同一个清单。当前实现使用 CPU；脚本不运行推理，也不宣称 GPU 性能验收。

WebM 与其他素材一样，通过 A 的真实关键帧进入 B，候选保留 A 的源时间范围。候选召回成功不代替最终媒体覆盖校验：缺少流时长的 WebM 会在 F 依据实际解码帧验证，不能借用较长音频或容器时长补足视频。素材已有烧录字幕时，B 不删除或移动原字幕；工作台可将新增旁白字幕设为上方，具体见 [F 的媒体校验与字幕策略](F-export.md)。

## 结果判读

- `status: error` 表示关键帧缺失/损坏、模型加载失败或评分失败；候选被阻断，错误记录包含查询、资产、片段和可用的路径信息。CLI 返回非零。这不是一次确认过的“没有匹配”。
- `status: empty` 表示输入和模型执行正常，但没有符合约束的正分候选。
- 没有声明关键帧但存在成功 ASR 文本的片段，可生成明确的 `text_only` 候选和 `VISUAL_NOT_AVAILABLE` 诊断；C 的真实视觉规划不把纯文本候选算作视觉验收。
- 候选的 `transcript_evidence` 保留 A 的 ASR 时间范围、分段和 `review_status`。B 不把待复核字幕变成已确认字幕。
- `retrieval_profile` 保留模型 revision、checkpoint SHA256、输入内容指纹和 B 实现指纹，供本次结果追溯。指定 `openclip` 失败时不会改用颜色编码器。

`examples/team_contracts` 与 `scripts/demo_pipeline.py` 的合成画面/人工描述用于协议和离线回归，不能作为真实图文检索验收。真实验收需要本次任务的实际图片、OpenCLIP 输出和后续 C 的选择证据。

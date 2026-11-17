# B 管线说明

B 负责检索与外部空镜库。输入是 A 的素材清单和 C 的检索请求，输出是一份候选集，交给 C 挑选。B 不决定故事顺序、精剪、配音或导出。

当前真实入口是 `QuickCut.command`，或在仓库根目录执行 `sh scripts/quickcut_web.sh` 打开工作台。工作台通过独立 `.venv-vision` 环境运行固定版本的 NLLB-CLIP。先执行 `python3 scripts/setup_vision.py --check` 检查已有部署；首次安装及独立 CLI 用法见 [B 本地视觉模型部署](docs/team/B-local-model.md)。下方协议样例和合成演示用于回归，不代表真实模型验收。

```text
AssetManifest（A）
RetrievalRequest（C）
        │
        ├─ 外部空镜先经 A 注册到同一项目素材清单
        ├─ 核对项目与请求引用的 Manifest 精确版本
        └─ 对每条查询：过滤来源与不可用片段 → 所有窗口打分 → 相同源区间去重 → 取前 top_k
        │
        ▼
CandidateSet（交给 C）
```

一条查询如果没有任何正分候选，返回 `status: "empty"` 和原因，不用零分片段凑满 `top_k`。有字面重合但低于 0.22 的片段仍会返回，并标上 `low_confidence`。`duration_budget_us` 是这一段故事的时长预算，不用来删除更长的原片。画面比例，以及查询里单独写出的 `min_duration_us` / `max_duration_us`，会在打分前剔除不符合的片段。

缺帧、坏图或模型执行失败返回 `status: "error"` 和可定位错误，CLI 以非零状态退出。明确指定 OpenCLIP 时不会静默改用颜色编码器。没有声明关键帧但有成功 ASR 的片段可以产生带诊断的 `text_only` 候选；C 的真实视觉规划会排除纯文本候选。

## 每个文件做什么

| 文件                                    | 作用                                                                       |
| ------------------------------------- | ------------------------------------------------------------------------ |
| `quickcut/retrieval.py`               | B 的管线。`retrieve()` 读取清单和请求，生成 `CandidateSet`；`BRollLibrary` 仅供协议夹具合并 |
| `quickcut/encoder.py`                 | 视觉打分。工作台使用固定版本 OpenCLIP；通用 CLI 默认颜色编码器供离线回归 |
| `scripts/setup_vision.py`             | 独立环境安装、固定模型下载和只读完整性检查 |
| `quickcut/cli.py` 的 `retrieve`        | 命令行入口                                                                    |
| `tests/test_retrieval.py`             | B 的自动化测试                                                                 |
| `examples/team_contracts/bundle.json` | 六人共用的协议样例。B 用其中的 `AssetManifest` 和 `RetrievalRequest`                    |
| `outputs/manifest.json`               | 从样例拆出的 A 输入                                                              |
| `outputs/request.json`                | 从样例拆出的 C 输入                                                              |
| `outputs/candidates.json`             | 本次运行交给 C 的 `CandidateSet`                                                |

## 输入和输出

两份输入的协议版本都是 `0.1.0`，而且 `project_id` 必须相同；请求必须精确引用实际传入的 Manifest document_id/revision。

CLI 重跑同一输出文件时，内容相同复用原 revision，输入、配置或结果变化则递增；写入使用独占锁和原子替换。API 持续更新同一文档时传入 `previous`，或由调用者显式维护 `revision`。输出不可覆盖输入文件。

外部空镜必须先通过 A 的 `quickcut.ingest --external ...` 导入同一项目，再由 C 针对新 Manifest revision 发出请求。`--broll-library` 会明确拒绝尚未注册的库，避免生成 C 找不到资产或片段的候选；不会静默复制、改写来源或假定库路径属于项目根。

`AssetManifest` 来自 A。每个片段包含 `segment_id`、所属 `asset_id`、来源（`user` 或 `external`）、源时间、关键帧路径、字幕和文字描述。

`RetrievalRequest` 来自 C。每条查询包含 `query_id`、`semantic_text`、镜头职责 `role`、允许的来源 `allowed_origins` 和 `top_k`。

`CandidateSet` 交给 C。每条查询都有一条结果。候选带回 `candidate_id`、`segment_id`、`asset_id`、`origin`、源时间、视觉分、文字分、`ranking_score`、`score_profile`、证据和 `flags`。外部空镜在使用 OpenCLIP 时带场景、情绪、光线和运镜标签，方法记为 `openclip-zero-shot`。颜色直方图没有语义标签，这些候选的标签状态是 `not_predicted`。

新候选还保留完整 `transcript_evidence`（含 ASR 时间范围和复核状态）、`visual_status`。`retrieval_profile` 记录模型与 checkpoint 指纹、输入内容指纹及 B 实现指纹，便于区分本次真实重跑和历史冻结输出。

打分取几路信号里的最大值：磁盘上有关键帧时比较画面和查询文字；同时比较查询与字幕、查询与描述的字面重合。`score_profile` 写明编码器、模型、权重和向量维度，并说明分数没有校准。颜色直方图和 CLIP 的分数不能直接互相比较。

## 如何测试

在仓库根目录执行：

```bash
python3 -m pip install -e '.[dev,ingest]'
python3 -m pytest tests/test_retrieval.py -q
```

用协议样例跑一遍检索：

```bash
python3 - <<'PY'
import json
from pathlib import Path
bundle = json.loads(Path("examples/team_contracts/bundle.json").read_text())
docs = {d["kind"]: d for d in bundle["documents"]}
Path("outputs").mkdir(exist_ok=True)
Path("outputs/manifest.json").write_text(json.dumps(docs["AssetManifest"], ensure_ascii=False, indent=2))
Path("outputs/request.json").write_text(json.dumps(docs["RetrievalRequest"], ensure_ascii=False, indent=2))
PY

python3 -m quickcut.cli retrieve outputs/manifest.json outputs/request.json --output outputs/candidates.json
```

样例里的视频和关键帧文件不在仓库中，且明确标为 `fixtures_only`，所以这次 `visual_score` 全是 `0.0`，排序来自字幕和描述。正式图文语义检索按 [部署说明](docs/team/B-local-model.md) 使用 `.venv-vision`、固定本地权重、`--encoder openclip` 和正确的 `--project-root`；仅安装 Python 包不代表模型权重已就绪。

## 测试结果

初版检索的历史记录为 **17 passed**。2026-11-15 的检索、编码器、蒙太奇与 pipeline demo 联合回归为 **63 passed**，新增长视频后续窗口、PNG关键帧、旧版本拒绝、输出修订及外部库注册边界验证。

2026-11-16 同组定向回归为 **103 passed**，覆盖缺图/坏图、模型和评分失败、显式模式禁止 fallback、ASR 证据与实现指纹等边界。这些自动化用例不计作真实模型推理验收。

样例检索生成的 `outputs/candidates.json` 是合法的 `CandidateSet`：

| 查询 | 留下的片段 | 说明 |
|---|---|---|
| `query-open` | `seg-packing`、`seg-city` | 只用用户素材，分数都低于 0.22 |
| `query-middle` | `seg-walk`（0.26）、`seg-forest`（0.13） | 同时有用户镜头和外部空镜，空镜职责为 `broll` |
| `query-close` | `seg-rest`、`seg-packing`、`seg-walk` | 都有字面重合，全部低于 0.22 |

三条查询的 `ranking_score` 都大于 0。整份文档 `status` 为 `ok`，`fixtures_only` 为 `true`。

## 交付给 C 

交给 C 的是 `outputs/candidates.json`。当前这份由样例输入生成，只能用来核对格式和召回行为。正式交接要换成 A 的真实 `AssetManifest` 和 C 的真实 `RetrievalRequest`，再重新运行 `retrieve`。

`scripts/demo_pipeline.py` 已经把 B 接进演示链路：A 生成清单后，C 写出 `RetrievalRequest`，B 的 `retrieve()` 返回 `CandidateSet`，再交给 C 的 `plan()` 和 `build_from_plan()`。合成测试画面没有对白，演示会先给每个窗口写上对应故事句，检索才能命中。

交给组长的 B 管线文件是：

- `quickcut/retrieval.py`
- `quickcut/encoder.py`
- `quickcut/cli.py` 中的 `retrieve` 命令
- `tests/test_retrieval.py`
- `outputs/manifest.json`、`outputs/request.json`、`outputs/candidates.json`

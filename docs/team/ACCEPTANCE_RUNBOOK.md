# QuickCut 验收操作手册

本手册用于复现模块、执行真实链路和记录交付证据；架构及失效规则见 [PIPELINE_ENGINEERING.md](PIPELINE_ENGINEERING.md)。所有命令从 repo 根目录执行，输出目录使用新的 run 标识，避免覆盖既有证据。**主代理已完成全仓回归：805 passed，33.55s，macOS / Python 3.13**；这是已有实测结果，不是本文编辑期间重新运行的结果。新增媒体演示仍在运行，尚无结果的 demo、preview 和远端 CI 只列命令及预期，不记为已通过。软件回归与真实产品质量、剪映实机验收分别记录。

## 1. 开始前先确认验收层级

| 层级 | 能证明什么 | 不足以证明什么 |
| --- | --- | --- |
| 协议 fixture | 字段、引用、预算、异常拒绝与编译关系 | 真实视觉语义、真实语音或可播放媒体、剪映实机 |
| 生成媒体 smoke | 真实解码、缓存、时间映射、旧渲染等技术路径 | 用户事件正确、普通话识别、故事自然或声音自然 |
| 真实模块样例 | 该素材、机器、模型和配置的模块效果 | 任意输入都可用，或全部 A–F 已串联 |
| 真实团队链路 | 冻结输入经真实检索、规划、声音、时间线和媒体执行 | 剪映可编辑性，除非另附实机验证 |
| 交付验收 | 同一冻结链路的媒体、内容、编辑闭环与目标软件证据 | 其他未测软件版本和平台的兼容性 |

执行结果只填 `通过/失败/阻塞/未执行`，每项附证据位置和 owner。协议预期负例被拒绝可以判该负例通过，但不得把它的 `needs_retime` 包标为最终成片成功。跳过可选模型时写“未执行”，不要用 rule/fixed 或音调结果代替真实模型分数。

现有入口包括 A 接入、C planning、E VoicePackage、D 编译、C→E→D 初编 API，以及 [doctor](../../scripts/doctor_pipeline.py)、[统一工程 demo](../../scripts/demo_pipeline.py) 和 [新 IR preview](../../scripts/render_timeline_preview.py)。demo 保存 JSON、验证合成媒体和改单句缓存；preview 生成 MP4、独立 SRT 与报告。它们尚不构成真实 B/Qwen/人审/自然语音/自动 fit/剪映的完整产品闭环。初编 API 本身不保存聚合 JSON，第 7 节保留真实模块逐步交接方法。

## 2. 版本和环境预检

### 2.1 建立可追溯记录

先在运行记录中填 run ID、操作者、日期、机器、素材授权范围，以及本次要验证的层级。保存以下只读命令输出；不要为了“干净”而 reset 或覆盖工作树。

```bash
git status --short
git rev-parse HEAD
git rev-parse origin/pr-4 origin/pr-2 origin/pr-1 HEAD
python --version
python -m pip freeze
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -encoders
ffmpeg -hide_banner -filters
```

以下 SHA 来自本地 PR 基线解析（A=`origin/pr-4`、D=`origin/pr-2`、E=`origin/pr-1`，C 为核对时 HEAD），并非用户本轮指定的 SHA。本次文档核对得到 A `3c6d4e99f9df6ffe87a1efb2808f66728d807ea3`、D `38dcb8b16ea5e0089da5a94142a171e32ff8abba`、E `d1c6c95105fe33432735a34071e5312b604eb63c`、C `620b24f277754d948ed69a8aba01d381a044e9f8`。今后远端 ref 若变化，保留本次与实际值，说明被测范围，不直接沿用旧结论。

运行目录建议为 `outputs/acceptance/<run-id>/`；项目媒体根单独固定，所有模块交接沿用同一根。该目录是操作建议，不表示已有自动证据收集器。运行清单可手工维护，先记录命令、stdout/stderr、退出码、输入输出 refs，再做人工判定。

### 2.2 最小 Python 与媒体环境

建议新建隔离环境；若已有可用环境，不必重复安装。以下是准备步骤，本轮未执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[ingest,dev]'
```

Python 要求 3.10+。FFmpeg、ffprobe 由系统提供并置于 PATH；A 也接受 `QUICKCUT_FFMPEG/QUICKCUT_FFPROBE`，但 E 当前直接调用 `ffmpeg`。`.[runtime]` 的 imageio-ffmpeg 不能代替 ffprobe 预检。

检查 libx264、PNG、`-fps_mode`、`atempo`；旧字幕预览需要 subtitles/libass 与中文字体。工具存在而缺编码器/滤镜仍会失败。把具体工具版本及缺能力记录下来，由 A/E/F 对口处理；不要用删字幕、无声输出或复制旧结果掩盖依赖缺失。

```bash
python -m quickcut.ingest --help
python -m quickcut.planning --help
python -m quickcut.voice_package --help
python -m quickcut.voice_package --capabilities
```

无系统 TTS 时 capabilities 或真实 E 测试可能受限；fixture 与真实引擎结果分开记。macOS 的已交付样例使用 Reed，其他平台的 espeak 实机试听仍需验证。

### 2.3 doctor：只读离线预检

```bash
python scripts/doctor_pipeline.py
python scripts/doctor_pipeline.py --require-media
python scripts/doctor_pipeline.py --require-media \
  --project-root outputs/acceptance/real-project
```

第一条为诊断模式：必需项失败时 `status=degraded` 仍退出 0。加 `--require-media` 后，必需项全部 ok 退出 0，否则退出 2；参数解析错误也可退出 2，应读 JSON 或 stderr 区分。检查项为 Python ≥3.10、A 的 ffmpeg/ffprobe 解析、E 的 PATH ffmpeg 和默认系统 TTS（say/espeak-ng/espeak）。传项目根后，`A.asset_manifest` 加入必需项：先递归检查所有 `uri/*_uri` 的路径安全，再执行 A 清单校验（引用、文件哈希、区间、PNG），并要求资产非空。未传根时该项 skipped，不要求已有项目。

报告看 `status/exit_code/required.checks/optional.checks/policy/evidence`；可选包只读安装元数据，缺失不会单独使严格媒体检查失败。doctor 不安装、不联网、不下载、不运行模型或全量解码；`evidence.real_inference=not_run`。工具版本和 TTS 可执行文件存在不证明编码器/滤镜、中文 voice、权重、声音自然度或成片已验收。

### 2.4 按需启用本地模型

| 能力 | 准备与核对 | 保存证据 |
| --- | --- | --- |
| A ASR | `python -m pip install -e '.[asr]'`；使用本地 CTranslate2 权重或预先下载的模型 | 实际权重哈希、语言、device、compute type、版本和下载/推理分段耗时 |
| B OpenCLIP | `python -m pip install -e '.[ml]'`；冻结实际 encoder 和权重 | 实际 backend；不能把 fallback 颜色直方图结果当中文语义模型结果 |
| C Qwen/Ollama | 先安装本地 Ollama 并准备指定模型；`ollama --version`、`ollama list` 核对 | 模型标识/摘要、参数、附图映射、原始响应、耗时 |
| E IndexTTS2 | 按 [E-local-voice.md](E-local-voice.md) 使用隔离 Python 3.11；固定 runtime、权重和授权参考声音 | runtime commit、权重 revision、参考 WAV SHA-256、seed、device、实际配置 |
| 服务/UI | 需要时安装 `.[server,ui]` | 仅对实际入口验收；旧 UI 能打开不等于已接新 pipeline |

C 使用 `QUICKCUT_VLM_MODEL`（默认 `qwen2.5vl:3b`）、`QUICKCUT_OLLAMA_URL`、`QUICKCUT_VLM_TIMEOUT`。这些是 C 当前配置项；不要假设其他模块全部使用相同变量。首次准备模型可联网，正式离线验收前确认所有权重与辅助模型缓存齐全。下载失败和本地推理失败分别记录，不合并成模型语义失败。

IndexTTS2 的可选命令沿用已交付配置：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTORCH_ENABLE_MPS_FALLBACK=1 \
  outputs/indextts2-runtime/upstream/.venv/bin/python -m quickcut.voice_package \
  --story examples/voice_package/story.json \
  --config examples/voice_package/indextts2.config.json \
  --root outputs/acceptance/indextts2-voice --mode measure
```

仅在该 runtime、权重、参考音频和设备配置确已准备好时执行。示例脚本有故意超预算的句子，退出 2 需查看 JSON，不能直接判安装失败，也不能直接判成片成功。

## 3. 数据集、时长与判定规则先冻结

### 3.1 区分短片验收、愿景与旧路径

已实际提取核对 `/Users/zicheng/Downloads/CV Proposal.pdf`：第 1 页 BACKGROUND 明确约 5 分钟源视频和 5–15 秒相关短片；VISION 的 Basic 愿景另为约 45 秒 Vlog opening。第 2 页 EVALUATION PROTOCOL 和 Minimum deliverable 是下述实验与交付要求的依据。

| 口径 | 本轮用途 | 验收方法 |
| --- | --- | --- |
| proposal 约 5 分钟源 → 5–15 秒 | 第 1 页 BACKGROUND；本次短片产品验收口径 | 准备真实 5、10、15 秒目标；检查最终长度、故事与末句完整性 |
| proposal 约 45 秒 opening | 第 1 页 VISION 的 Basic 愿景 | 与短片实验分别记录，不代替 5–15 秒验收 |
| 黄金 fixture 18 秒 | 协议与编译回归 | 保留原 fixture；不能拿通过结果抵消产品长度不符 |
| 旧 MVP 5–60 秒，Basic 默认30秒 | 兼容回归与已有范围 | 单独记旧路径结果；不据此宣布短片目标已验收 |

C/D 与需求方在真实验收前记录采用范围、总长度测量方式和帧/样本量化容差。不要通过截断最后一句把 18 秒 fixture 强行压成 15 秒；应使用新的 Brief 和有足够证据的短脚本。按表中用途分别报告；本次采用 5–15 秒短片范围，量化容差未冻结时不能判产品长度通过。5 秒合成 demo 的固定音调会触发 needs_retime，保留为负例，不算 5 秒成片通过。

### 3.2 5–10 视频、30–50 prompts 的执行安排

先给每个独立原片分配 source group ID，再切分开发集和 heldout。以下是可执行的**计划模板，不是已有数据或已测数量**：

| 计划规模 | 开发组 | heldout 组 | prompts 计划 |
| --- | --- | --- | --- |
| 最小：5个独立原片 | 3个 | 2个 | 开发18条＋heldout12条＝30条 |
| 推荐：8个独立原片 | 5个 | 3个 | 开发25条＋heldout15条＝40条 |
| 扩展：10个独立原片 | 6个 | 4个 | 开发30条＋heldout20条＝50条 |

多短片与长短混合项目可由同一 split 内多个原片组成，不能跨 split 混用。相同事件、同一拍摄连续段、原片的裁切/转码/代理、相邻镜头和由其改写的 prompt 都视为同组。外部空镜库配置也在评测前冻结，不能看过 heldout 后定向加入恰好匹配的空镜再报告原分数。

每个 split 的请求覆盖下列类别；类别可交叉，但每条请求只计入一次总数：

- 中文视觉定位与少量英文同义请求，覆盖环境、动作和对象。
- 原声/ASR 请求，包含有对白、无对白与无音轨样本。
- 环境建立、转场、细节和情绪空镜，至少包含不匹配空镜。
- 必须有用户证据的具体事件、候选不足和明确不存在的事件。
- 5/10/15 秒短片目标及不同节奏/旁白偏好。
- 编辑回归请求另加操作标签，不把同一结果重复算作新的独立视频。

单长视频、多短视频、长短混合三类输入都要在真实验收中覆盖；同时尽量包含横竖屏、VFR、非零源 PTS、含声/无声。若某 split 缺某类别，报告覆盖缺口，不能补写“全部覆盖”。

每条 prompt 预先填写以下标注表：

| 字段 | 内容 |
| --- | --- |
| `case_id / split / source_group_ids` | 唯一用例、开发或heldout、原片组 |
| `prompt / language / target_duration_us` | 原始请求与整数微秒目标 |
| `expected_events / forbidden_assertions` | 应覆盖事件和不得声称的事实 |
| `relevant_ranges` | 人工标注 asset revision 与源 `[begin,end)`；允许多个相关区间 |
| `expected_empty / allowed_broll_roles` | 是否应拒答/缺口，以及空镜允许承担什么 |
| `license / reviewers` | 来源授权和标注、复核人员 |
| `frozen_config / hashes` | 模型、规则、检索配置、媒体及标注指纹 |

先用开发集定 top-k、相关区间匹配标准、故事评分尺度、自然度及延迟预算，然后锁定配置。heldout 一次正式运行，失败也保留；若据其调参，则该集合降为开发数据，另建未见集合重新验收。固定 fixture、历史手调中文样例、合成色块都不能作为独立真实 heldout。

### 3.3 技术红线与质量指标

下列为交付红线：无依据具体事件不得通过；所需旁白未经批准不得播出；陈旧 refs、源越界、非法路径、缺媒体、静音伪成功、截断句尾、伪造导入成功均为阻断。不能用平均相关性或整体满意度抵消这些问题。

按 proposal 第 2 页执行并分 split/query 类别报告：

| 检查 | 操作与记录 |
| --- | --- |
| 时间定位 | 人工相关源区间作真值；R@1 at tIoU 0.5/0.7，multi-span mAP；匹配与 AP 聚合规则在开发集冻结，Recall@k 可补充 |
| 系统对比 | A：vision-only similarity + crop；B：vision + transcript + temporal refinement + timeline；C：B + gap fill + edit loop；这些是实验配置名，不是团队 owner |
| 消融 | 每次移除一个模块，在同一冻结 split、预算与标注下比较，记录配置差异 |
| 成片偏好 | 隐去系统标识、平衡展示顺序，盲评 relevance/coherence，保留人数、票数、平局与分歧 |
| 编辑任务 | replace/trim/caption；检查仅目标项改变，更新后仍为 5–15 秒，refs/声音/字幕一致 |
| 交付与成本 | export success rate 的分母、失败原因、每阶段耗时；另分冷/热缓存和资源峰值 |

CLIP Score 仅作辅助，不能代替人工 spans 和盲偏好结论。故事另查事件覆盖、角色/因果、空镜用途和无依据断言；声音查可懂度、自然度、末句完整、超预算率和 fit 次数。Minimum deliverable 必须包含 supported query、unsupported query 与完整 edit round-trip；合成 demo 的工程断言不等于这些真实产品任务已通过。除代码已明确的轮数、速度、路径和时间约束外，不捏造分数或吞吐保证；未冻结的阈值不能默认通过。

## 4. 无权重协议与模块检查

以下命令均为现存入口；示例输出目录可替换为自己的新 run 目录。每步保存完整命令、退出码和原始输出。

### 4.1 公共协议及 C

```bash
python examples/team_contracts/validate.py --self-test
python -m quickcut.planning selftest
python -m quickcut.planning run --mode rule --out-dir outputs/acceptance/c-rule
python -m quickcut.planning run --mode fixed --out-dir outputs/acceptance/c-fixed
python -m quickcut.planning run --case insufficient --out-dir outputs/acceptance/c-insufficient
python -m quickcut.planning inject --output outputs/acceptance/c-injected-bundle.json
```

检查步骤：

1. 公共校验器的正常 bundle 通过，陈旧 revision、越界路径/源范围、字幕文本不一致和虚假导入声明被拒绝。
2. C selftest 报告每个检查是否符合预期。记录实际检查数，不从本文推断本轮测试统计。
3. rule 和 fixed 输出 `outline/retrieval_request/story_plan/alignment/run/bundle.json`。确认 provenance 分别标 rule 与固定响应，没有被记为真实 Qwen。
4. insufficient 的 `needs_input` 与具体缺口是预期。**C run 在校验报告合法时可能退出0，即使 story status 为 needs_input**，因此还必须读状态。
5. inject 使用黄金协议关系；它能校验 C 替换后 bundle，但不生成真实声音、媒体或剪映工程。普通 run 输出 bundle 中其他 D/E/F 文档可能仍是旧 fixture，不能称完整联调。

C CLI 的一般语义为：合法报告返回0，校验失败1，PlanningError 或结构异常2；结合输出 JSON 判定，不单独依赖退出码。

### 4.2 D 编译与错误路径

```bash
python -m examples.team_contracts.demo_d --bundle-output outputs/acceptance/d-normal-bundle.json
python -m examples.team_contracts.demo_d --case longer --output outputs/acceptance/d-longer.json
python -m examples.team_contracts.demo_d --case retime --output outputs/acceptance/d-retime.json
python -m examples.team_contracts.demo_d --case overlong --output outputs/acceptance/d-overlong.json
python -m examples.team_contracts.demo_d --case stale --output outputs/acceptance/d-stale.json
```

normal/longer/retime 应验证 D 实际编译的时间线；overlong 和 stale 预期退出1，并输出 `VOICE_NEEDS_RETIME` 或 `STALE_INPUT_REVISION` 的结构化错误。逐个检查，不因一个预期非零退出跳过其余用例。

对照 normal 与 longer 的 cue-02：demo 文档约定末端由 `14400000` 变为 `15000000` 微秒；以实际输出核对旁白和字幕各自的范围。该 fixture 把局部字幕终点设为整句时长，与真实 E 排除新增 padding 的行为分开记录。retime 用例测试选中片段尾部容量扩展，**没有调用 E 的 fit**，不可当作 E–D 自动循环已跑通。

检查四轨存在、视频 1×硬切、原声音量政策、资产 revision、总时长与 Brief 上限。最新工作树 D 已逐条偏移 local subtitles：每条全局范围等于旁白 clip 起点加局部范围，保持分段与间隙。标准 E 包的字幕不含新增 padding；不要只凭 fixture 的单条字幕宣称多段映射或真实同步已验收。

### 4.3 E fixture

```bash
python scripts/demo_voice_package.py --fixture --root outputs/acceptance/e-fixture
```

应产生初测、改一句、fit 请求/回复和故意失败的验证记录。确认 `fixtures_only: true`；音调不是人声。检查只修改 cue-01 时其他句缓存复用、超预算 WAV 完整保留、全失败返回 failed 而非静音 ready。

此 demo 自己断言预期失败后可退出0；E 生产 CLI 则 ready 返回0，重定时/partial/failed 返回2。不能把二者退出码含义混用。E 默认拒绝覆盖已有输出，复跑使用新 root、输出名或 revision，保留旧证据。

### 4.4 选择模块回归

需要验证代码基线时运行现有相关测试；只执行需要的范围，保存实际结果：

```bash
python -m pytest tests/test_ingest.py tests/test_asr.py tests/test_encoder.py -q
python -m pytest tests/test_timeline.py tests/test_voice_package.py tests/test_integration.py -q
```

C 测试文件名先在 `tests/` 确认，再选 planning 相关测试或运行全量 `python -m pytest -q`。模型 mock 测试不计真实模型验收；环境缺失导致 skip/失败需原样记录。已有主代理的全仓结果为 805 passed、33.55s（macOS / Python 3.13），按来源留档；后续复跑记录自己的命令、代码状态和实际统计，不覆盖或冒用该结果。

## 5. 生成媒体与旧单源回归

### 5.1 A smoke

```bash
python -m scripts.smoke_ingest --root outputs/acceptance/a-smoke
```

脚本生成三个短片、一个较长片和两个 external 空镜，包含有声、静音、无声、VFR 与竖屏，随后两次接入验证缓存。它只用测试图案、音调与静音，不证明 ASR 正确率、视觉事件或真实授权库检索效果。

检查 `project/asset-manifest.json`、`.quickcut/run-report.json`、原片、代理、PTS/映射 sidecar 与关键帧。确认来源区分、公共 URI 相对路径、coarse_event 与 coarse_shot 层级、第二次有效阶段命中。A `quality.usable` 表示关键帧可读取，不表示清晰度、构图或语义已通过。

### 5.2 旧单源 smoke 与 prompt 定位演示

```bash
QUICKCUT_ASR=none python scripts/smoke_demo.py
```

该脚本使用固定 `outputs/smoke-demo/` 并重写其中生成的 fixture 和结果。运行前确认它是可重建 demo 目录；若需要保留旧证据，先复制到自己的 run 目录。它验证生成色块视频的索引、检索、剪辑点修改、渲染和不支持请求，实际 encoder 为 fallback，不测试真实语义。

真实 prompt 定位按 README 现有旧路径执行：

```bash
quickcut index /path/to/authorized-video.mp4 --cache-dir outputs/acceptance/single-cache
quickcut search INDEX_ID '适合开头的环境画面' \
  --cache-dir outputs/acceptance/single-cache --output outputs/acceptance/single-timeline.json
```

`INDEX_ID` 替换为上一步实际返回值；媒体路径替换为已有且有权使用的文件。另试一个明确不存在事件，检查 coverage/拒答情况并与标注对照。记录实际 encoder，不把 fallback 结果标成 CLIP。该演示用于旧定位能力；新多源 CandidateSet 必须另按第7节验收，不能把旧 Timeline 改文件名充当新 StoryPlan。

### 5.3 统一工程 demo → 新 IR preview

先执行第 2.3 节预检。四个 demo 使用各自不存在或为空的 root，不能是符号链接；目标参数只允许 5、10、15 秒，默认 10。以下是入口与预期，新增媒体演示运行结果仍待取得；在 `set -e` 环境下，两个预期退出 2 的命令应分别捕获退出码并核对报告，不用 `|| true` 吞掉任意错误。

```bash
python scripts/demo_pipeline.py --root outputs/acceptance/pipeline-10s \
  --case supported --target-seconds 10
python scripts/demo_pipeline.py --root outputs/acceptance/pipeline-edit-10s \
  --case edit --target-seconds 10
python scripts/demo_pipeline.py --root outputs/acceptance/pipeline-unsupported-10s \
  --case unsupported --target-seconds 10
python scripts/demo_pipeline.py --root outputs/acceptance/pipeline-retime-5s \
  --case supported --target-seconds 5
```

| 用例 | 预期退出 / 顶层状态 | A/E/D 调用 | 必须核对 |
| --- | --- | --- | --- |
| supported 10 秒 | 0 / ready_for_render | 1/1/1 | 四轨 Timeline、ready VoicePackage、媒体检查、render.files |
| edit 10 秒 | 0 / ready_for_render | 1/2/2 | r2 只改 cue-02，miss 仅 cue-02，hit 为 cue-01/03；A 未重接入、Manifest 哈希不变 |
| unsupported 10 秒 | 2 / needs_input | 1/0/0 | expected_stop=true，UNSUPPORTED_STORY_BEAT；有缺口，无声音包和 Timeline |
| supported 5 秒 | 2 / failed | 1/1/0 | feedback 为 VOICE_NEEDS_RETIME、status=needs_retime、owner D、cue-03；保留声音包，无 Timeline，render=null |

demo 用 testsrc2 + sine 生成实际 MP4 并执行 A，ASR=none；候选为人工 fixture、C 为 rule、批准为预设夹具、E 为固定一秒 tone。它验证真实文件解码/哈希/PTS、初编和缓存，不执行 B、Qwen、真人审批、自然语音、自动 fit 或渲染。为满足每 beat 容量，demo 显式扩大 A 窗口；默认 3 秒 shot 与 C 每 beat 单 user 的风险仍在。5 秒的固定 tone 加 padding 超出收尾预算，拒绝初编是负例预期。

根目录 `run-report.json` 含 `git/capabilities/stages/calls/files/media_validation/render/limitations`，编辑另有 `edit`，失败另写 `feedback.json`。保存 `brief.json/asset-manifest.json/candidate-set.json`、各 revision 的批准夹具、outline/request/story/alignment/planning-run、声音包、资产 union 和 Timeline（仅实际生成项）。`fixturesOnly=true` 不得改标；`manifestFixturesOnly=false` 只表示 A 处理实际编码文件。A 自己的 `.quickcut/run-report.json` 是阶段与缓存报告，不能拿它读取 demo 的 render 映射。

成功 demo 后运行以下映射示例，避免硬编码 r1/r2。输出父目录须存在，MP4/SRT/报告均不得已存在；重复运行使用新文件名。

```bash
python - <<'PY'
import json
from pathlib import Path
import subprocess
import sys

root = Path("outputs/acceptance/pipeline-10s").resolve()
report = json.loads((root / "run-report.json").read_text(encoding="utf-8"))
assert report["status"] == "ready_for_render", report
files = report["render"]["files"]
assert report["render"]["uris_relative_to"] == "project_root"
subprocess.run([
    sys.executable, "scripts/render_timeline_preview.py",
    "--timeline", str(root / files["timeline"]),
    "--manifest", str(root / files["manifest"]),
    "--voice-package", str(root / files["voice_package"]),
    "--project-root", str(root), "--output", str(root / "preview.mp4"),
    "--srt", str(root / "preview.srt"),
    "--report", str(root / "preview.preview.json"),
], check=True)
PY
```

`files.assets_union` 是 A+E 检查用合集，不是 A Manifest，不能用于 `--manifest`。preview 必需 ffmpeg 和 **ffprobe**，通过 PATH 或 `QUICKCUT_FFMPEG/QUICKCUT_FFPROBE` 指定；成功退出 0，读取/校验/媒体运行失败退出 1，argparse 参数错误退出 2。省略 `--srt/--report` 时默认 MP4 同名 `.srt/.preview.json`。已处理非零源 PTS 与原声对齐，并按全局时间线边界向上量化视频帧，避免逐片段取整漂移；字幕全局化后向下量化到毫秒，独立保存、不烧录。

核对 preview 报告 `status=preview_rendered`、`duration_validation=ffprobe`、`expected_duration_us/measured_duration_us/measured_stream_durations_us/duration_tolerance_us`、`video_frame_quantization=ceil_global_timeline_boundaries`、`video_frame_ranges/measured_video_frames` 和 `full_decode_verified=true`。10 秒、30 fps 的该 demo 预期 300 帧；真实输入依其输出规格核验，不固定套用。输入精确 refs、路径、资产 union/哈希、源范围、输入输出全量解码均须通过。报告 `files.preview/subtitles/report` 指向实际三项输出；`subtitles_editable=true/subtitles_burned=false` 仅说明独立 SRT，`editor_project_exported/editor_import_verified/f_acceptance_completed` 均为 false。MP4 内原声与旁白已混合，不是剪映可编辑工程。preview 另写报告，不会把原 demo 的 `render.executed=false` 改成 true。

## 6. 三种演示的操作与证据

| 演示 | 输入与现场操作 | 必须展示的产物 | 失败出口 |
| --- | --- | --- | --- |
| 1：prompt 定位 | 同一索引先查存在事件，再查缺失事件；回看源区间 | prompt、backend、命中/空集、asset/segment 或旧 shot、源时间和关键帧 | 无证据明确返回空集/缺口；错误命中记失败 |
| 2：5–15秒短片 | 在真实素材上冻结 Brief，依次完成检索、证据审核、文案批准、E/D/F | 真实StoryPlan、逐句WAV、四轨Timeline、MP4、素材包与工程验证 | 候选不足/未审/超时/导出不支持时阻断，展示归属 |
| 3：编辑闭环 | 替换或重排镜头，再改一句旁白；发布新 revision；在目标软件精修 | 修改前后refs、缓存与失效对照、新字幕/渲染、编辑保存重开记录 | 旧包引用、音画不一致、索引全量重建或工程不可编辑记失败 |

每次演示说明用的是单源旧能力、模块分演还是完整团队链路。三类真实输入（单长片、多短片、长短混合）与三种演示交叉安排，不能只演最容易的一条素材。第 5.3 节已有统一工程 demo 和 preview，可展示合成媒体与改单句缓存；真实 B/Qwen/人审/自然语音/自动 fit 或 F 实机证据未齐时，演示 2/3 的真实全链路仍为“阻塞/待验证”。

## 7. 真实 A–F 链路逐步验收

### 7.1 A：接入用户素材与授权空镜

从 `examples/ingest/sources.json` 复制结构准备自己的源清单，填写真实路径、origin、许可说明；不要原地修改 repo 示例。执行：

```bash
python -m quickcut.ingest --sources /path/to/sources.json \
  --project-root outputs/acceptance/real-project
```

如需 ASR，在权重准备好后显式加 `--asr faster-whisper` 和核实后的模型参数。只对 `--project-root` 重跑会复用/重试现有阶段，**不会重新扫描外部源文件变化**；检查源更新时必须再次传源清单。

验收逐项记录：

1. 同 origin 同内容去重；user 与 external 分开登记；相同 alias/source_key 内容变化时 asset ID 保留、revision 增加。
2. 原片复制到项目内；所有公开 URI 无个人绝对路径；搬迁 root 后仍能解析媒体。
3. 从 PTS 表选首帧、区间边界和 VFR 若干点，核对源时间与代理映射；帧数一致，误差≤2微秒。代理无音频，原声指向原片。
4. ASR no_audio/disabled/no_speech/success/failed 分开；真实普通话从原声对照转写与偏移，mock 不计准确率。
5. 加一个损坏/不可读源，确认其他成功资产保留，失败项有 stage/asset/attempt/retryable。A 退出0成功、2部分、1失败，不能把2当全成功。
6. 第二次同配置接入命中阶段缓存；改变 ASR 配置只失效 ASR；损坏一个缓存产物只补算相关阶段。

遇到 ingest.lock 先确认是否有接入进程；不要直接删锁继续。完整中断恢复未实现，恢复操作需留证。

### 7.2 B/C：请求、真实候选与双重审核

先由 C 的 `build_outline/build_retrieval_request` 产生当前 Request。B 对每个 query 返回当前 CandidateSet，包括合法空结果；冻结 encoder、模型和排序 profile。当前 repo 未给本轮统一 B CLI，不编造命令。

检查 CandidateSet 的 query IDs 与请求逐一对应、ID 唯一、asset revision 最新、来源符合 allowed_origins、segment 可用且不越界。外部空镜不能充当 primary 事件证据。关键帧文件必须真实存在，并保留帧到源时间的映射；图片缺失交 A，候选映射错误交 B。

事实审阅文件结构如下，键为真实 candidate ID；示例值必须替换成真实检查记录：

```json
{
  "candidate-actual": {
    "beat_id": "beat-open",
    "event": "与当前大纲完全一致的目标事件",
    "asset_id": "asset-actual",
    "segment_id": "segment-actual",
    "asset_revision": 1,
    "source_range_us": {"begin": 0, "end": 3000000},
    "reviewer": "实际审阅者",
    "note": "观看该源区间后的事实依据与限制"
  }
}
```

另建 `beat_id → 批准文本` 的 approved-cues JSON，保存审批人、时间、文本哈希与目标故事版本的旁证记录。不能由脚本把模板文案一律标批准。无用户事件依据时，不能仅靠批准一段文案绕过事实门槛。

已有 C CLI 可用于显式输入：

```bash
python -m quickcut.planning run \
  --bundle /path/to/current-input-bundle.json \
  --candidate-set /path/to/current-candidate-set.json \
  --retrieval-request /path/to/current-retrieval-request.json \
  --reviewed-evidence /path/to/reviewed-evidence.json \
  --approved-cues /path/to/approved-cues.json \
  --mode rule --document-id story-real --revision 1 \
  --project-root outputs/acceptance/real-project \
  --out-dir outputs/acceptance/c-real
```

先 rule 检查数据交接，再按验收计划改为 `--mode ollama` 做真实模型测试。C CLI 已将 `--project-root` 传入 `OllamaSelector(project_root=...)`，未传时才默认 repo root。使用与 A URI 相同的根，并保留实际附图、源时间映射及模型响应；参数正确不等于视觉质量已通过。

当前 A/C 风险必须单独验收，不能写成已修复：A 将跨窗口重叠 ASR spans 附到各 shot，C 候选视图仅取扁平文本，丢失 status 和 span/range；须对照原声确认句子实际范围。A 默认 3 秒 shot，而 rule 每 beat 只选一个 user（可另配 external），长 beat 可能缺容量；第 5.3 节 demo 扩窗口不证明默认路径已解决。C 结构 validator 不重读媒体、关键帧或真实哈希，JSON 合法不能证明文件存在、可解码或事件真实，仍需媒体检查和真人看片。

用输出 StoryPlan 与对应上游再次 validate；如果修改了 Request、CandidateSet 或源区间，需要重新审核对应证据。C 的 ready_for_edit 必须无 gaps/pending reviews，所有具体事件来自用户镜头，旁白获批；C 非用户空镜总时长有逐段和全局40%上限。检查“建立环境/转场/细节/气氛”用途是否与实际镜头一致，而不仅是比例合法。

候选不足时保存 alignment 的 next_request，交 B 执行后再调用 C；总计最多三轮检索。当前 C 单次 run 不会替你执行 B；达到上限后显示缺口并请求补素材/改叙述，不允许外部景物证明未拍到的具体事件。

### 7.3 E：真实逐句声音及独立验证

先用已交付真实声音 demo 检查本机引擎，再接批准的真实 StoryPlan：

```bash
python scripts/demo_voice_package.py --root outputs/acceptance/e-real-demo
python -m quickcut.voice_package --story /path/to/approved-story.json \
  --config /path/to/voice-config.json --root outputs/acceptance/real-project \
  --mode measure --revision 1
python scripts/verify_voice_package.py --story /path/to/approved-story.json \
  --package outputs/acceptance/real-project/voice_package-r1.json \
  --root outputs/acceptance/real-project
```

检查 StoryPlan ref、cue 集合、文本/beat、一句一个独立 WAV、资产 URI/哈希、48k PCM 实测、local subtitles 和预算。E 自身不独立强制全部 cue 审批，调用前仍由 C/D 执行批准门槛。单独 demo 的文字只是声音测试脚本，不声明对应真实画面事实。

逐句听审并记录：开头是否完整、发音/角色/语气、内部停顿、末尾最后几个字、额外 padding、爆音/削波。重点听最后一句；ASR 相似度或“识别到结尾”不能代替听审。批准字幕不被验证 ASR 改写。

超预算时 `duration_us` 必须仍对应完整 WAV，允许 `needs_retime`；独立 verifier 能检查合法超时反馈。黄金成功 bundle 校验器要求 fits，不能用它否定合法反馈，也不能把 verifier 通过当成整个成片已 fits。

### 7.4 D/E：三次处理上限与旧回复拒绝

真实初测后由 D 决定画面容量或修改预算。当前 `compile_timeline` 会拒绝超预算/needs_retime cue，并可能把 owner 指 C；它只会对已 fits 的声音在合法尾部余量内扩展画面。现有初编桥接和 demo 不生成或应用自动 fit 请求；该回路仍需集成与验收，不能用手动 E fit 或 D 尾部扩容代替。

有已核实请求后，E 现有命令为：

```bash
python -m quickcut.voice_package --story /path/to/approved-story.json \
  --config /path/to/voice-config.json --request /path/to/current-fit-request.json \
  --root outputs/acceptance/real-project --mode fit --revision 2
```

请求含 project、精确 StoryPlan/Timeline refs、cue ranges 和 iteration；D 校验返回回显与当前 pending request 完全一致。轮数口径为首次 measure＋iteration1＋iteration2，最多三次处理；第三次 fit（iteration3）必须拒绝。tempo 默认≤1.15、配置硬上限1.25，重新测量仍超时则停止并交 C 改词/用户改预算，保留失败链。

必测旧回复：发布新 pending Timeline 后送回旧 fit 响应，应拒绝为陈旧响应，不能更新当前全局时间。E 只能核验请求内部引用，无法知道远端 D 的最新状态；此项由集成层验收。不得删掉 fit 包中的 Timeline ref 以绕过当前编译器初测限制。

### 7.5 D：冻结时间线

D 现有函数签名为：

```python
compile_timeline(brief, manifest, story, voice,
                 output=output_spec, document_id="timeline-real", revision=1)
```

调用方先执行完整上游契约和审批检查；`output_spec` 指定 width/height、有理数 fps 和 sample_rate。不直接拿黄金 placeholder Timeline 当实际输出。直接 D 当前拒绝 pending reviews、非 ready 或带 errors 的声音包；逐 cue 审批仍交 C validator。

**已有初编桥接的用法**：若第7.2节直接调用 `plan()`，保留其 `PlanResult`，不要只把 `story_plan` 字典传给桥接。若使用 CLI，可从同一轮输出重建该容器；下面示例消费第7.3节已保存的初测包，不重复合成，也不自动执行 fit。本段为现有 Python API 的调用示例，本轮未执行：

```python
from pathlib import Path
from quickcut.integration import compile_from_voice
from quickcut.planning import PlanResult, read_json

c_dir = Path("outputs/acceptance/c-real")
project_root = Path("outputs/acceptance/real-project")
bundle = read_json("/path/to/current-input-bundle.json")
docs = {doc["kind"]: doc for doc in bundle["documents"]}
result = PlanResult(
    outline=read_json(c_dir / "outline.json"),
    retrieval_request=read_json(c_dir / "retrieval_request.json"),
    story_plan=read_json(c_dir / "story_plan.json"),
    alignment=read_json(c_dir / "alignment.json"),
    run=read_json(c_dir / "run.json"),
)
compiled = compile_from_voice(
    result, read_json(project_root / "voice_package-r1.json"),
    brief=docs["Brief"], manifest=docs["AssetManifest"],
    candidate_set=read_json("/path/to/current-candidate-set.json"),
    output={"width": 1920, "height": 1080,
            "fps": {"numerator": 30, "denominator": 1}, "sample_rate": 48000},
    expected_story_revision=result.story_plan["revision"],
    timeline_document_id="timeline-real", timeline_revision=1,
)
```

替换占位路径并固定当前项目根；`expected_story_revision` 在正式调度中应取任务创建时锁定的期望值，不能每次从新文件读值来掩盖任务过期。返回的 `compiled.story_plan/voice_package/timeline/assets` 需要由调用方保存到新的 run 目录并记录哈希；API 本身不保存 JSON。后续若由同一 `PlanResult` 直接执行声音，改用 `build_from_plan(result, brief=..., manifest=..., candidate_set=..., root=project_root, output=..., config=..., fixture=False, mode="measure", voice_revision=..., timeline_revision=...)`，不得与上例连续执行而意外重复合成。测试音调必须显式 `fixture=True`，不能用于听审。

桥接先运行 C validator，再检查声音包精确绑定当前脚本、ready/无 errors 和每 cue fits；带 Timeline ref 的 fit 包被拒绝。捕获 `IntegrationError` 时保存 `to_dict(project_id)` 和异常中的 `voice_package`（若有），超时归 D、合成非 ready 归 E；`PlanningError/TimelineError` 原样上抛，按各自错误信封记录。不要捕获后仍保存 ready 时间线。它不是完整统一 CLI，不执行 B、多轮 fit 或 F。

核对 A+E 资产 union 无重复 ID；每 clip 的 asset revision、源入出点、成片入出点、1×时长关系；画面顺序；原声独立轨及增益；旁白完整放入对应 beat；字幕文字与批准稿一致。按每条 local subtitle 验证偏移，特测非零局部起点、多个分段、分段间隙及标准 E 的180000微秒 padding；字幕不应被强行拉长到音频尾部。画面/声音改动后，后续 cue 全局时间即使使用原 WAV 也需重新核验。

冻结时记录 Brief/Manifest/StoryPlan/VoicePackage/Timeline refs 与媒体哈希，F 只能消费这一组输入。C run bundle 中混入的旧 E/D/F 占位文档不能进入真实冻结包。

### 7.6 F：媒体执行与可编辑工程实机

新 IR 已有 `scripts/render_timeline_preview.py`，完整命令、映射与退出码见第 5.3 节。真实链路使用第 7.5 节保存的同一组 Timeline、A Manifest 和 VoicePackage，传共同媒体根，生成 MP4、独立 SRT 与报告；不能将新 Timeline 传给旧单源 renderer。preview 必需 ffprobe，非零 PTS/全局帧量化已有修复；剪映工程导出与实机仍待验收。后续工程必须与预览共享冻结 Timeline ref。

对实际导出的 MP4 使用以下媒体检查，替换为真实文件：

```bash
ffprobe -v error -show_streams -show_format -of json /path/to/preview.mp4
ffmpeg -v error -i /path/to/preview.mp4 -f null -
```

全量解码无错误只是技术门槛；再完整观看和试听，检查黑帧、拉伸/旋转、原声位置与音量、旁白可懂、字幕对应、总长度和末句不截断。记录具体片段时间与故障，不只截封面。

剪映最小实验应提前进行，不必等真实模型稳定：

1. F 明确系统、剪映精确版本/build、工程放置或导入方法和支持矩阵。
2. 用至少两个不同源视频、可编辑文字、独立旁白和原声轨做最小工程；验证资产 union 与项目根路径。
3. 实际打开，逐个选中并修改画面入点、一个文字对象、一句声音及音量。只导入一个混合 MP4 不合格。
4. 保存、退出、重新打开，确认修改仍在且媒体在线；再导出并播放。
5. 将整个包复制到不同目录，必要时在软件内重定位；确认不依赖原个人绝对路径，再重复打开/保存/导出。
6. 保存操作记录或录屏、工程前后文件、软件版本、失败提示和验证人。目标版本仍为 UNVERIFIED 或没有实机证据时，只能声明 generated，不能填 verified_import。

不支持效果应返回明确差异给 D/F；不能静默删字幕、扁平化音轨或改故事后宣布兼容。

## 8. 编辑闭环与异常回归

### 8.1 编辑前后对照

先保存旧版本输入 refs、索引/embedding 指纹、A 阶段缓存、E cue cache keys 和 WAV 哈希、Timeline 与 Export。先用第 5.3 节 `--case edit --target-seconds 10` 检查改单句的工程回归：A 仅一次、Manifest 不变、r2 仅 cue-02 miss、其余 cue 命中，并编译新 Timeline。该脚本使用预设文案和测试音调；真实镜头替换/trim/caption、真人批准与剪映闭环仍按下表逐项执行。旧 `quickcut edit` 仅用于旧结构。

| 操作 | 必须看到的变化 | 必须保留与重新核验 |
| --- | --- | --- |
| 缩短/延长一个选中镜头 | 新 Timeline/Export；越边界时明确拒绝 | 素材索引不重建；事实区间改变需重审，原声/旁白/字幕重对齐 |
| 替换镜头并重排 | 新 StoryPlan/Timeline；必要的证据和文案审阅 | 已有资产索引复用；检查新的角色、事件顺序和后续全局时序 |
| 修改一句旁白 | 新批准 StoryPlan、VoicePackage；对应 cue 音频/字幕更新 | 其他句 WAV 哈希不变且有 cache_hit；聚合包必须引用新脚本 |
| 只换一句声音配置 | 该 cue 新资产/时长、VoicePackage/Timeline 更新 | 其他句与全素材索引复用；完整试听被变速的句尾 |
| 新增一个原片 | Manifest revision 更新，新资产阶段执行 | 旧资产阶段命中；重新检查依赖旧 Manifest 的文档 |
| 搬迁项目根 | 路径验证与导出验证记录 | 文档事实不必无故重写；媒体哈希与相对路径仍有效 |

每项对比实际日志与文件哈希；仅凭“代码应有缓存”不能判通过。E `invalidated_cue_ids/alignment_invalidated` 是通知，不表示 D 已执行；必须查看新 Timeline 与新导出。同项目接入使用单 writer，勿并发制造不确定证据。

### 8.2 负例、责任和恢复动作

只在验收输出副本中构造负例，不修改 repo 黄金输入或用户原片。

| 负例 | 预期 | owner / 恢复 |
| --- | --- | --- |
| 上游 revision 增加但下游仍旧 ref | 拒绝陈旧引用，保留旧结果为历史 | 生产者重建下游，D 协调；不只改数字 |
| 多源资产与 E 音频 ID 冲突 | 拒绝 union | A/E 修正身份；D/F 不覆盖 |
| 绝对路径、`..` 越界、符号链接出 root | 拒绝解析 | A/E/F 检查 URI 与 resolve；不得回退扫描用户磁盘 |
| segment/clip 超源范围、浮点微秒或非法重叠 | 阻断编译/导出 | A 修源事实或 D 修区间 |
| 事件 review 的区间、event 或 asset revision 不匹配 | C needs_input 或验证拒绝 | C/人工重新看片，不挪用旧审核 |
| 未批准旁白、模板草稿 | 阻断真实声音及最终交付 | C 审批；集成门槛不能依赖 E 自行检查 |
| 空镜冒充人物事件、重复/非法候选 ID | C 拒绝或登记缺口 | B/C 修召回或计划，保留原始响应 |
| 明确不存在事件且无候选 | 合法空集/needs_input，不虚构结局 | B/C 有限检索后请求用户输入 |
| TTS 无声、单句失败、全失败 | no_speech/partial/failed，不制造成功静音 | E 查能力/依赖，只重试受影响句 |
| 长句超时或 iteration3 fit | 完整超时音频/明确拒绝；停止循环 | D/C 改画面预算或经审批改词 |
| 字幕与批准文本不一致、局部字幕越 WAV | 拒绝 | E 修字幕事实，D 修全局映射 |
| 旧 fit 回应返回给新 Timeline | 拒绝陈旧回复，不污染当前结果 | D pending-request 校验 |
| 缓存损坏、缺音频/原片 | 检测并局部补算，不能算命中成功 | A/E；F 阻断缺媒体导出 |
| 工程 JSON 生成但软件打不开/不可编辑 | generated 或失败，不能 verified_import | F 最小实机复现与兼容修复 |

错误报告至少写故障阶段、实际 code、affected IDs、输入 refs、下一 owner、能否重试与证据。统一 code 尚未覆盖的模块保留原始错误，不擅自编造生产错误码。

## 9. 性能、日志与结果留档

每轮记录 `run_id → 各模块命令 → inputs refs → outputs refs → 媒体哈希 → 检查结果`。已有 A `.quickcut/run-report.json`、C `run/alignment.json`、E profile/request/cache/errors、D 输出与错误文件；统一 demo 的根 `run-report.json` 汇总其工程路径，preview 的 `*.preview.json` 记录实际媒体执行。三个报告层次分别留档；真实全组汇总仍由 D 负责。

性能分冷、热两次：先记录首次模型加载/下载和冷缓存，再在相同输入配置下热跑。比较 A 各阶段和 E 各句 hit/miss、重算数量、总耗时和峰值内存/显存；没有仪器采集的值填“未采集”。编辑时另报索引是否变化与实际新合成句数。不能从 fixture 耗时外推长片吞吐。

质量结果按开发/heldout、实际 backend、语言/请求类别、输入结构分栏；保留失败用例，不只挑成功 demo。对真实故事和听审采用标注表逐条判定，分歧由另一审阅者复核并保留原因。阈值变更、新增人工约束或人工改稿记入 provenance，不能仍称原始自动模型输出。

建议留档表：

| 字段 | 本轮执行时填写 |
| --- | --- |
| run / owner / 日期 / 层级 | 实际值 |
| 代码与环境 | HEAD、dirty diff说明、模块hash、Python/FFmpeg/依赖/模型指纹 |
| 数据与授权 | source groups、split、素材哈希、许可与参考声音授权 |
| 命令及结果 | 完整命令、退出码、stdout/stderr、状态JSON、实际测试数量/skip/失败 |
| 产物 | 精确文档 refs、项目相对URI、媒体哈希、播放/解码报告 |
| 人工证据 | 事件审核、文案批准、完整看片听审、实机编辑记录 |
| 缓存与成本 | 阶段/句子命中、失效理由、冷/热耗时、实际资源 |
| 结论 | 通过/失败/阻塞/未执行，已知限制，下一owner与复测条件 |

当前已知回归证据为主代理全仓 805 passed、33.55s（macOS / Python 3.13）；来源是用户提供的已完成实测，不能冒记为文档编辑者复跑或远端 CI 结果。新增媒体演示仍在运行，未取得报告前 demo/preview 不填通过。真实检索、Qwen、真人证据审核、自然语音、自动 fit 和剪映编辑仍按各自实验结果填写。历史 E 音频、中文样例或旧日志作为参考附件时注明原配置。

## 10. 交付门禁与 CI

| 门禁 | 负责人 | 关闭条件 |
| --- | --- | --- |
| M0 契约与准备 | D，各模块确认 | 冻结规格、refs、数据与环境；协议检查有真实输出；目标软件版本确定 |
| M1 模块独立 | A–F 各自 | 输入→输出或明确失败可复现；F 已做最小真实工程实验 |
| M2 真实串联 | D 汇总 | 同一冻结项目经真实 B/C/E/D/F；refs/资产/声音/预览一致；产品时长达标 |
| M3 编辑与交付 | D/F，全组配合 | 局部修改不重建索引；只重合成变更句；新字幕和导出正确；剪映编辑保存重开搬迁通过 |

P0/P1/P2 的 owner、依赖和验收条件以工程说明第13节为准。P0 阻断未解时不宣布端到端交付；P1 字幕同步及工程可编辑性、手机覆盖等是否阻断具体发布，由已冻结的本轮需求决定，不能事后降低标准。

### 10.1 已有 CI 矩阵、预期与产物

| Workflow | 触发与环境 | 配置中的检查 |
| --- | --- | --- |
| [pipeline-integration.yml](../../.github/workflows/pipeline-integration.yml) | pull_request、push、workflow_dispatch；ubuntu-latest × Python 3.10/3.13，fail-fast=false，30 分钟上限，contents:read | 安装 ffmpeg（含 ffprobe）、espeak-ng、fonts-noto-cjk 和 `.[dev,ingest,runtime]`；严格环境 doctor；全量 pytest；第 5.3 节四个 case；10 秒项目 doctor 与 preview |
| [c-planning.yml](../../.github/workflows/c-planning.yml) | Ubuntu × Python 3.10/3.13；PR 按 planning/相关测试/review script/contracts/pyproject/workflow 路径过滤；push 另限 main、feat/c-story-planning 及对应路径 | 安装 `.[dev]` 和 Pillow，运行 `python scripts/review_c_planning.py` |

集成 workflow 设置 `HF_HUB_OFFLINE=1/TRANSFORMERS_OFFLINE=1/QUICKCUT_ASR=none/QUICKCUT_ENCODER=fallback`，不运行真实模型或下载权重。pytest 命令为 `python -m pytest -q -ra --junitxml="$PIPELINE_OUTPUT/pytest.xml"`，同时留 `pytest.txt`。四个 case 逐一核对精确退出码、状态、calls、媒体校验与 fixture 标记；5 秒仅指定的 VOICE_NEEDS_RETIME 视为预期负例，任意失败不能混过。10 秒 preview 从 demo 的 `render.files` 取输入，断言 10,000,000 微秒目标、300 帧、ffprobe 流时长、全量解码、独立 SRT，且没有编辑器工程。

始终上传 `pipeline-integration-ubuntu-python-${{ matrix.python-version }}`，内容为 `$PIPELINE_OUTPUT/` 全目录、含隐藏文件，保留 7 天；包括 doctor JSON、pytest 文本/JUnit、每个 demo 的 stdout/stderr、项目媒体与阶段/根报告、preview MP4/SRT/报告。失败时也上传已有证据；产物缺失仍需查看失败步骤，不能把上传成功当验收成功。

以上是已读 CI 配置的行为与预期，**不是远端 workflow 已通过的运行结论**。取得真实 run 后另记 run ID、commit/工作树身份、矩阵结果和 artifact 位置；macOS 的 805 项回归不能替代 Ubuntu 两个矩阵的证据。

### 10.2 当前入口与剩余门禁

| 范围 | 当前入口 / 状态 | 尚需证据 |
| --- | --- | --- |
| 环境与项目预检 | doctor 已有，第 2.3 节 | 实际机器报告，不以版本检查代替模型/媒体验收 |
| 工程编排与改单句缓存 | demo 已有，第 5.3 节 | 新演示报告待取得；真实 B/Qwen/人审/自然声音和镜头编辑另验 |
| 新 IR 媒体执行 | preview 已有，第 5.3/7.6 节 | 新媒体实测与完整看片听审，不等于工程交付 |
| 自动 fit 与剪映 | 自动 fit 调度、剪映可编辑工程仍未验收 | 有界请求/旧回复拒绝；指定软件版本打开、编辑、保存、重开、搬迁、导出 |

仅有入口或配置不等于验收完成；结果必须绑定命令、输入 refs、代码状态和真实报告。文档修订不改源码、黄金 fixture 或历史证据。

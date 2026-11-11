# 多素材接入

接入模块将本地视频导入项目，生成 `AssetManifest` 0.1.0。它负责媒体信息、源时间、分段、关键帧、代理和转录；检索排名、故事规划、成片时间线及工程导出由下游模块处理。

## 运行

需要 Python 3.10+、FFmpeg、ffprobe 和 Pillow。安装依赖：

```bash
python -m pip install -e '.[ingest,dev]'
python -m scripts.smoke_ingest
python -m quickcut.ingest --sources examples/ingest/sources.json --project-root outputs/my-project
```

FFmpeg 和 ffprobe 需在 PATH 上，也可分别通过 `QUICKCUT_FFMPEG`、`QUICKCUT_FFPROBE` 指定。FFmpeg 需支持 libx264、PNG 和 `-fps_mode`。

已有项目可不传新文件，重新检查已注册的素材：

```bash
python -m quickcut.ingest --project-root outputs/my-project
```

该命令按当前配置复用缓存并重试失败阶段，不扫描外部源文件的变化。检查外部原片更新时，应重新传入文件或源列表。

退出码：成功为 0，部分成功为 2，失败为 1。每个错误记录阶段、素材 ID、重试次数和 `retryable` 状态。单个文件或处理阶段失败不会清除其他成功结果。

## 身份与文件存储

首次导入分配 `asset_id`，内容使用 SHA-256 校验。同一来源类别下的相同文件复用身份；用户素材和外部素材分别注册。

源路径或 `source_key` 作为导入别名。已知别名对应的内容变化时，素材 ID 保持不变，revision 递增。文件移动且内容不变时可通过哈希识别；位置和内容同时变化时，需使用稳定的 `source_key`。

原片复制到 `media/<origin>/<asset_id>/r<revision>.<ext>`，旧版文件保留。对外 URI 均相对于项目根目录，使用 `/`，项目整体移动后仍可解析。`.quickcut/registry.json` 中的外部源路径仅用于识别导入别名。

显式重导入时发现文件缺失或损坏，会报告失败，不返回旧结果作为本轮成功结果。当前不提供资产删除和旧版本自动清理。

## 输出字段

公共字段遵守 [团队契约](team/contracts.md)。清单内容改变时递增文档 revision，缓存命中信息单独保存在 `.quickcut/run-report.json`。

| 字段 | 含义 |
|---|---|
| `assets[]` | 素材身份、revision、来源、URI、哈希、时长、视频和音轨参数 |
| `segments[]` | `coarse_event` 粗窗口与 `coarse_shot` 镜头候选，通过 `parent_segment_id` 关联 |
| `source_range_us` | 相对视频第一张解码画面的整数微秒区间，左闭右开 `[begin,end)` |
| `keyframes[]` | PNG 路径与抽中画面的实际源时间 |
| `quality` | `usable` 表示关键帧可读取；容器、无帧窗口及抽帧失败的候选均不可用 |
| `transcript` | 识别状态、转录区间、配置和 sidecar 路径 |
| `proxy_uri` / `proxy_mapping_uri` | 视频代理及逐帧源/代理 PTS 映射路径；代理失败时不发布路径 |
| `source_pts_uri` | 原片逐帧 PTS 记录 |
| `processing` | 整体状态与错误列表 |
| `embedding_profile` | `status: not_indexed, owner: B`，向量由检索模块生成 |

检索应使用 `granularity: coarse_shot` 且 `quality.usable: true` 的片段。粗窗口仅作为容器，`usable` 也不表示画面清晰度、构图或语义质量已经评估。

这些状态及映射字段是对现有协议的扩展，需与 B/D/F 确认消费方式。新清单不能直接传给仍使用 `IndexManifest` 的旧 `quickcut search`、API 或 UI。

## 时间与代理

源时间根据解码 PTS 计算：`(source_pts - source_origin_pts) × timebase`，转换为整数微秒。帧率保留分数，不用平均帧率反推变帧率视频的位置。

代理为等比例缩小的 H.264 视频，不含音轨；原声引用原始素材。代理使用 passthrough 帧时序，从零开始计时。生成后逐帧核对：帧数必须一致，源时间与代理时间误差不超过 2 微秒，否则返回代理阶段失败。导出时应使用原片。

ASR 从原片抽取单声道 16 kHz WAV，按视频起点处理音频偏移，使转录和画面使用同一源时间坐标。转录区间限制在素材时长内。

## ASR 与缓存

ASR 默认禁用。启用时安装 `.[asr]`，传入 `--asr faster-whisper`。状态区分如下：

| 状态 | 含义 |
|---|---|
| `no_audio` | 素材无音轨 |
| `disabled` | 有音轨，未启用识别 |
| `no_speech` | 已运行识别，没有有效对白 |
| `success` | 获得有效转录 |
| `failed` | 依赖、权重、解码或推理失败 |

模型参数可以是 `tiny` 等名称或本地 CTranslate2 权重目录。模型名称先解析为具体快照，计算权重文件哈希，再使用该目录推理。首次下载需要网络。`--asr-model-revision` 用于指定模型仓库 revision，需要 faster-whisper 版本支持；也可直接使用固定的本地权重目录。

探测、抽帧、代理、ASR 分别缓存。指纹包括素材内容、处理参数、工具和库版本；ASR 额外包含实际权重哈希、语言、设备、计算类型及预处理配置。产物哈希和 PNG 解码检查用于发现缺失或损坏文件。识别配置变化只重算 ASR，新增素材复用旧素材的有效结果，失败结果不作为成功缓存使用。

项目采用单写入者锁，并行处理请使用不同项目目录。进程异常退出后，确认没有任务运行，才可移除 `.quickcut/ingest.lock`。当前支持阶段缓存复用，尚未实现完整中断恢复。

## 测试与限制

```bash
python -m pytest tests/test_ingest.py tests/test_asr.py tests/test_encoder.py -q
python -m scripts.smoke_ingest
```

自动化测试覆盖身份、修订、路径重定位、缓存失效、部分失败、变帧率映射及非零源起点。ASR 测试使用 mock 验证状态和时间关联；smoke 使用测试图案、音调和静音，不验证语音识别准确率。

真实中文识别、手机旋转/HDR 等复杂素材和 B/D/F 联调仍需验收。逐帧 PTS 列表的内存及 JSON 空间与帧数成正比，批次抽帧会重复解码，超长视频性能尚未优化。场景检测、停顿边界和画质评分未实现。

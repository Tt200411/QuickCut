# QuickCut 云端 PR 与 A–F 交付 GAP 审核

> 后续更新：用户随后授权合并与实现。本文保留首次只读审核快照；修复、F 实机验收和剩余 GAP 请看 [整合验收报告](../../quickcut-f-release-20261115/整合验收报告.md)。

审核日期：2026-11-15（Asia/Taipei）。本轮只审核、运行已有测试和验证脚本；没有修改实现代码，没有提交、推送、合并或发表 PR 评论。F 的实现工作尚未开始。

## 结论

当前不具备整体合并并宣布 A–F 完成的条件。主要问题是 B 的检索与版本交接存在可复现缺陷，D/E 重定时返回链未接通，集成分支缺少通过的跨平台检查，F 尚无剪映工程导出与实机验收。

F 已有可复用基础：#6 的多源四轨预览器。本轮用 #9 的真实素材包成功生成 30 秒、900 帧预览，视频与音频流均为 30 秒，全量解码通过；这证明原片、逐句 WAV 与初次编排的预览路径可执行，不证明剪映工程、自然度、事实表达或原声保留已验收。

## 审核依据与快照

已读取聊天「部署 QuickCut 本地 MVP Demo」中分工、指南及 PDF 交付记录，以仓库对应 Markdown 原文为验收依据：

- [总指南](https://github.com/Tt200411/QuickCut/blob/1e342b6fe910133c3c83b71ed5d34f86838b858c/docs/PROJECT_GUIDE.zh-CN.md)：职责、里程碑、迁移顺序、交付与大文件约定。
- [六人任务书](https://github.com/Tt200411/QuickCut/blob/1e342b6fe910133c3c83b71ed5d34f86838b858c/docs/team/workstreams.md)：各部分 P0/P1，F 的七项 P0 和实机验收。
- [接口契约](https://github.com/Tt200411/QuickCut/blob/1e342b6fe910133c3c83b71ed5d34f86838b858c/docs/team/contracts.md)：整数微秒、精确版本、相对路径、fixture 边界、ExportManifest。

审核时 main 为 `1e342b6`，本地工作区与云端基线一致。审核结束复查，以下 PR 头提交未变化。#3 已关闭且未合并，由 #4 接续。

| PR | 头提交 | 范围 | 本轮处理建议 |
|---|---|---|---|
| [#1](https://github.com/Tt200411/QuickCut/pull/1) | d1c6c95 | E 配音与字幕 | 可作为局部模块交付审核；补审批入口边界，D/E/F 联调另验 |
| [#2](https://github.com/Tt200411/QuickCut/pull/2) | 38dcb8b | D 多轨时间线 | 不作为最终对接基线；必须带上 #6 的状态与字幕修复 |
| [#4](https://github.com/Tt200411/QuickCut/pull/4) | cf988d0 | A 多源接入 | 基础可用；旋转手机素材需修复或明确拒绝/限制 |
| [#5](https://github.com/Tt200411/QuickCut/pull/5) | 620b24f | C 规划与校验 | 离线契约较完整；真实 Qwen 和输入证据留档尚缺 |
| [#6](https://github.com/Tt200411/QuickCut/pull/6) | a463e45 | A/C/E/D 集成与预览，Draft | 作为集成基础审核；检查须通过，不能按完整产品放行 |
| [#7](https://github.com/Tt200411/QuickCut/pull/7) | 2bc2772 | A 乡村素材包 | 来源和哈希可核对；调整大文件分发，补有声验收素材 |
| [#8](https://github.com/Tt200411/QuickCut/pull/8) | c0ae869 | B 检索 | 暂缓，先修下述 B 阻断项 |
| [#9](https://github.com/Tt200411/QuickCut/pull/9) | eb05bd0 | A–E 参考串联与冻结包 | 暂缓整包合并，拆清依赖、策略变化、素材与检查问题 |

#6 的提交历史包含 #1/#2/#4/#5；#9 包含 #1/#2/#4/#5/#6/#7/#8 的全部头提交。它们是叠加关系。保留祖先关系时 Git 不会简单重复应用同一提交，但主分支差异与验收范围高度重叠，不能把八个 PR 当成八份独立交付逐个批准。查询到的八个 PR 正式 review 列表均为空；其他渠道的消费者确认未在本轮得到证明。

## 已复现的阻断问题

### 1. B 错误跳过长视频后续片段（P1，#8/#9）

按整个文件的 content_hash 在打分前去重，导致同一视频最多检查第一个合格窗口。第一窗口不相关时，后面的正确命中也被跳过。

复现：同一视频第一窗口为“公路”、第二窗口为“树林行走”；查询“树林行走”，默认返回 empty，关闭去重后返回第二窗口。#9 保存的真实候选也全部来自各文件的 0–15 秒。

责任 B。应区分重复文件与同文件不同时间片段，保证所有合法窗口参与评分，再按合理片段规则去重。[代码位置](https://github.com/Tt200411/QuickCut/blob/eb05bd0de8b7415c9bf95dcdfe7e8188d313244f/quickcut/retrieval.py#L372)

### 2. B 外部库输出无法完整交给 C（P1，#8/#9）

`--broll-library` 只在内存合并清单，CLI 最终仅保存 CandidateSet，没有交付包含新资产/片段的新 Manifest，也没有对应的媒体复制或路径重定位。

复现：外部库增加 `seg-library-new`，B 能召回；C 读取原 Manifest 后拒绝“不存在的片段”。应由 A 注册库中入选资产，形成新 Manifest revision，并使请求和候选精确引用该版本。

责任 A/B，消费者 C/D/F。[CLI 位置](https://github.com/Tt200411/QuickCut/blob/eb05bd0de8b7415c9bf95dcdfe7e8188d313244f/quickcut/cli.py#L164)

### 3. B 的版本引用不能可靠标记旧结果（P1，#8/#9）

输出默认使用固定 document_id，revision 写死为 1；输入、查询或模型变化后仍可能覆盖同一身份和版本。输入请求引用 Manifest r99、实际传入 r1，也被接受。

这会使旧 StoryPlan 无法凭约定的文档引用识别候选内容变化。需要校验请求的精确输入引用，并提供内容变化时递增 revision 的机制。

责任 B，消费者 C/D/F。[输出版本位置](https://github.com/Tt200411/QuickCut/blob/eb05bd0de8b7415c9bf95dcdfe7e8188d313244f/quickcut/retrieval.py#L318)

### 4. D 独立 PR 有状态和字幕缺陷，#6 已修（P1，限 #2）

- 正常 cues 保持不变，只将 VoicePackage 改为 failed 并附 TTS_FAILED，#2 仍返回 ready_for_export；StoryPlan 增加 pending review 也未阻断。
- #2 忽略 E.local_subtitles，把整句字幕铺满完整 WAV。复现中，局部字幕 0.1–1.0 秒加上旁白 0.2 秒偏移，应输出 0.3–1.2 秒，实际输出 0.2–5.4 秒。

#6 已补包级状态、待审与逐条字幕偏移检查。不得把已修问题误报成 #6/#9 仍存在；也不能单独把 #2 当作 F 的稳定基线。

责任 D。[#2 旧状态检查](https://github.com/Tt200411/QuickCut/blob/38dcb8b16ea5e0089da5a94142a171e32ff8abba/quickcut/timeline.py#L109)；[#6 修复](https://github.com/Tt200411/QuickCut/blob/a463e4555fa0933cf898481ae0531eb5e30c8058/quickcut/timeline.py#L113)

### 5. 集成检查尚不能放行（P1，#6/#9）

- #5 的 Python 3.10/3.13 离线契约 CI 成功。
- #6 最新头提交的两项 Ubuntu 检查均超过 30 分钟后取消，不能当作通过。[检查记录](https://github.com/oran2e/QuickCut/actions/runs/43451495737)
- #9 最新头提交：Python 3.13 在全量 pytest 步骤失败，Python 3.10 超时取消。完整失败测试日志未获取，不能据本地成功断定远端失败原因。[检查记录](https://github.com/buluu97/QuickCut-B/actions/runs/43582543504)
- 另有独立可复现的 CI 断言过期：#9 demo 的 calls 包含 B=1，capabilities.B.executed 为 true，但 workflow 仍要求不含 B 的字典和 executed=false。即使前面的 pytest 修好，后续步骤仍会失败。

责任集成维护者与 B。[过期断言](https://github.com/Tt200411/QuickCut/blob/eb05bd0de8b7415c9bf95dcdfe7e8188d313244f/.github/workflows/pipeline-integration.yml#L99)

## 其他具体缺陷与交接缺口

| 项目 | 证据与影响 | 责任 |
|---|---|---|
| 旋转手机视频方向错误 | A 丢弃 Display Matrix，抽帧禁用 autorotate。160×90、旋转90°的样例仍被声明横屏，关键帧方向错误，影响 B 画幅过滤与 C 视觉证据 | A；[media.py](https://github.com/Tt200411/QuickCut/blob/cf988d0a0e65dbd63c4174fae83bda77acf86723/quickcut/media.py#L98) |
| B 默认 fallback 不读 A 的 PNG | 同一绿图，PNG 视觉分0，PPM为0.9925；异常被转换成零分。无对白/无人工描述时默认链路无法召回。OpenCLIP路径可读PNG，不是所有模式都坏 | B；[retrieval.py](https://github.com/Tt200411/QuickCut/blob/eb05bd0de8b7415c9bf95dcdfe7e8188d313244f/quickcut/retrieval.py#L153) |
| #9 代理声明与交付不符 | 六份资产声明 proxy 成功，Git树无对应proxy.mp4；实跑A校验报 Missing proxy_uri。原片存在，所以原片预览能成功，不能因此认定整个Manifest完整 | A/集成；[清单](https://github.com/Tt200411/QuickCut/blob/eb05bd0de8b7415c9bf95dcdfe7e8188d313244f/ABCDE/asset-manifest.json#L43) |
| #9 根目录写死作者机器 | f-inputs.json 的 project_root 是 /Users/buluu/...；实际媒体URI相对路径正确，显式覆盖 --project-root 可工作，但交接根目录规则须修正 | 集成/F；[f-inputs.json](https://github.com/Tt200411/QuickCut/blob/eb05bd0de8b7415c9bf95dcdfe7e8188d313244f/ABCDE/f-inputs.json#L2) |
| 字幕预览策略被忽略 | D写preview_burn_subtitles=true；新renderer始终不烧字幕，报告subtitles_burned=false。应实现或明确拒绝/约定该策略 | D/F；[timeline.py](https://github.com/Tt200411/QuickCut/blob/eb05bd0de8b7415c9bf95dcdfe7e8188d313244f/quickcut/timeline.py#L255)、[preview报告](https://github.com/Tt200411/QuickCut/blob/eb05bd0de8b7415c9bf95dcdfe7e8188d313244f/quickcut/timeline_preview.py#L461) |
| E 独立入口不检查审批 | needs_input、cue.approved=false，给足预算仍能产生ready包；集成build_from_plan在调用前有C校验，不能说整个集成都绕过审批 | E/C；[voice_package.py](https://github.com/Tt200411/QuickCut/blob/d1c6c95105fe33432735a34071e5312b604eb63c/quickcut/voice_package.py#L167) |
| C 模型输入记录不完整 | 运行记录保存模型响应，却未保存实际附图名单/哈希、image_mapping和完整调用参数；不能完整还原模型当次看到的证据 | C；[selection.py](https://github.com/Tt200411/QuickCut/blob/620b24f277754d948ed69a8aba01d381a044e9f8/quickcut/planning/selection.py#L359) |
| 大型媒体进入普通Git | #7媒体约194.8MB，#9又将约141MB项目根媒体路径加入Git。相同blob可被Git复用，但代码仓库仍承担大型媒体分发，违反总指南第9节约定 | A/集成；改为约定素材分发机制并保留hash和获取方式 |

## 尚未完成的产品与集成能力

### D/E 重定时与编辑闭环

E 接收 D 指定区间进行 fit 时，会把所消费的旧 EditTimeline 引用加入 VoicePackage.inputs；D、integration 与 preview 都只接受初次测量的 StoryPlan 引用，拒绝该合法返回包。当前路径能初次编排，不能完成“超预算→D调时间或C改稿→E适配→D接收并生成新版本→F导出”。

需要 D 统筹请求标识、请求所基于版本、次数限制、返回版本核对和重新编排；C负责改稿，E负责声音，F消费最终版本。F不能截掉句尾或自行重排画面来填这个缺口。[D拒绝位置](https://github.com/Tt200411/QuickCut/blob/a463e4555fa0933cf898481ae0531eb5e30c8058/quickcut/timeline.py#L61)；[E返回引用](https://github.com/Tt200411/QuickCut/blob/d1c6c95105fe33432735a34071e5312b604eb63c/quickcut/voice_package.py#L259)

replace/trim/reorder 后对故事证据、声音、字幕、预览和工程的失效传播，也不能由“改一句文本命中缓存”的样例代替验收。D的局部精剪目前主要使用给定区间与时长，不证明动作/对白边界细化已实现。

### #9 不是原始愿景的完整验收样例

#9正确保留全部公开素材origin=external；并增加authorized_external_montage模式，按规则选镜头、使用人工文案，同时放宽该模式的用户事件证据与空镜比例限制。这是新的公开素材蒙太奇模式，不能算“用户实拍事件＋外部空镜＋真实Qwen规划”的完成证明。C/D/F应明确确认模式语义与接口影响。

脚本将A的optional_narration草案写成APPROVED，来源记录仍指向草案；本轮未见可核对的C批准证据，需补证，不能仅凭布尔字段认定完成内容审批。

B真实召回报告为partial：行走查询中航拍排名高于一个正确人物镜头，航拍查询还召回人物镜头。需要固定评测集与消费者认可的质量门槛，不能只以有返回值验收。

#9实际入选的三份原片音轨实测mean/max均约-91dB，属于静音。预览有旁白、有音频流，不证明保留了可听原声。补充对白、环境声、无音轨和混合帧率素材，验证混音来源与最后一句。

## F 的明确工作清单

F首轮不必等待A–E全部完善，但不能替其他模块修改故事或时序。本机只读确认已安装剪映专业版6.1.0、macOS26.6.2；本轮未启动或验证其工程格式。

| 顺序 | F应完成的工作 | 完成证据 |
|---|---|---|
| F1 | 指定版本的最小导入实验 | 两段独立视频、独立配音、可编辑字幕；记录软件/系统版本与导入步骤 |
| F2 | 实机编辑与重开 | 移动镜头、修改文字、替换配音、保存并重开；不能仅是一个合成MP4 |
| F3 | 能力矩阵和输入门槛 | 接受冻结EditTimeline与精确VoicePackage；版本过期、fixture生产导出、缺媒体、unsupported效果均明确处理 |
| F4 | 剪映工程映射与素材包 | 保留画面/空镜片段、原声、旁白、文字的独立编辑性；A/E资产并集、哈希和可重定位引用 |
| F5 | 同一时间线的预览和工程 | 复用#6新preview；统一顺序、出入点、字幕策略、声音位置与gain，记录帧舍入 |
| F6 | 生产ExportManifest | 精确输入revision、目标软件/平台/适配器版本、文件清单、路径映射、支持/不支持项、验证记录；generated与verified_import分开 |
| F7 | 可复现的自动＋实机验收 | 解码、时长、PTS、非静音原声/旁白、字幕、预览工程一致；移包到新目录、重链接、重开和再导出 |

现在F5已有较扎实基础，F1–F4/F6/F7仍缺核心交付。普通SRT独立可编辑不等于剪映工程内已有可编辑字幕对象。

新preview明确拒绝未知轨道、非1倍速、未支持特效，并执行每clip的gain；不应为尚不支持的音乐/转场等能力虚报“已实现”。旧render_story_timeline会重新合成并回写时间线，不适合作为冻结版F入口。

## 按文档执行的建议合并路径

原文规定的是模块责任、协议变更须生产者/消费者确认、先最小工程后扩大能力，以及M0→M3的验收；没有规定具体PR编号的唯一合并顺序。下面是依据当前提交依赖推导的执行建议。

1. 冻结本轮审核SHA、共享契约和目标剪映版本；确认C新增蒙太奇模式、B资产注册与版本机制、D/E适配请求协议。
2. 独立审核A #4、E #1、C #5的交付边界；D #2必须连同#6已有修复交付。避免把已知有缺陷的#2独立当稳定接口。
3. 在#6明确保留依赖历史的基础上收敛集成，修复/解释远端测试和超时并取得通过结果；它仍只代表初次编排基础。
4. B修复去重、外部库注册、revision/引用检查和默认关键帧格式；将#9里的B增强和C模式变化按责任人审核，不能只合#8后宣称B已完成。
5. #7/#9素材分发符合指南，修复代理交付声明与绝对根目录；#9调整为清晰的增量联调范围并修正CI断言。
6. F最小实机导入与上述模块整改可以并行。F完成后再用真实上游替换手写输入，跑M2真实链路、M3编辑/异常/移包验收。

任何“Git可合并/无文本冲突”只证明机械合并条件，不代表这些交付门槛通过。

## 本轮验证记录与边界

- #9 `eb05bd0`，macOS/Python3.14.4：全仓库836项测试通过，29.11秒。使用隔离快照，未改实现代码。
- 独立审核者：#4相关40项；#8检索17项；#9检索21项；#6 C/D/E/集成相关538项；#6预览专项70项通过。它们与全套测试有重叠，不能相加作为测试总数。
- #7哈希清单26项匹配。#9六份视频、三份WAV的SHA-256均匹配；WAV采样时长与声明一致。
- #9真实素材直接渲染：30秒、900帧，音视频流各30秒，全量解码通过；未重新运行检索模型或Qwen，未进行人工试听/剪映操作。
- #9完整A清单验证失败：Missing proxy_uri；不是本地稀疏检出遗漏，Git树中确实没有对应代理视频。
- 复现了B后续窗口丢失、B外部库到C缺片段、B版本引用、默认PNG、A旋转、旧D状态/字幕以及CI断言问题。
- 本机通过不抵消Ubuntu失败/超时；真实预览通过不替代内容质量、可听原声与剪映可编辑性验收。

同目录保存复核产物：`countryside-preview.mp4`、`countryside-preview.srt`、`countryside-preview.preview.json`。报告中的原运行路径指向审核临时目录，复制后的媒体内容未变。

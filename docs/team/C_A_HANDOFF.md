# C → A 素材协作说明

团队交接版 · 2026-11-13。读者：A 素材接入、B 检索、C 故事与视听规划及项目负责人。

## 1. A 先看这一段

C 决定每段故事讲什么、需要什么画面，并反馈未满足的事件证据。A 可以据此寻找已有原片、修复接入或协调补充素材。**先查接入、检索和审核，再决定是否补拍。**

现有 `RetrievalRequest` 的直接消费者主要是 B；给 A 的需求卡是人工协作方式。当前没有 C→A 自动下载、采集或补拍派单系统。本说明未修改共享 schema。

协作顺序：A 接入已有素材 → C 提出故事与画面需求 → B 检索 → C 检查证据 → A/B/C 诊断缺口 → 必要时补素材或改写故事 → 重验通过后交 E/D。

## 2. C 能给 A 什么

| 信息 | 已有依据 | A 可以做什么 |
| --- | --- | --- |
| 故事段落与表达目的 | `beat_id`、`beat_purpose` | 按叙事任务找素材 |
| 所需动作、对象或环境 | `semantic_text`、`role` | 找到对应原片与连续可用时段 |
| 允许的来源 | `allowed_origins` | 区分用户事件证据和外部环境/氛围 |
| 画面时长预算 | `duration_budget_us` | 检查长度，保留剪辑余量 |
| 未满足的事件及原因 | `StoryPlan.unresolved_gaps` | 定位接入、检索、审核、容量或实际缺失问题 |
| 再检索限制 | `iteration`、`max_iterations`、`top_k` | 与 B 有限重试，耗尽后转人工 |

当前 `build_retrieval_request(brief, manifest, outline, ...)` 需要 A 的 `AssetManifest` 并检查项目身份。因此，素材接入前的拍摄讨论仍是人工活动，不能称为已完成的自动拍摄规划。

主体事件查询限定 `allowed_origins: ["user"]`。例如“用户完成登顶”需要用户素材支持；外部山景只能承担环境或氛围作用，不能证明该用户完成了动作。下一轮检索只能放宽非关键风格，不得取消这个限制。

## 3. 可直接采用的 12 秒素材清单

以下是假设“从城市出发、登上山顶、平静收尾”的协作示例，**不是已拍摄或已验收的 demo**。三段暂各 4 秒，最终全局时间由 D 决定。

| 段落 | 必需画面与用途 | 来源与收集建议 |
| --- | --- | --- |
| 开场 4 秒 | 用户实际出发，建立人物和情境 | 主体须 user；找出门、迈步或出发现场原片。城市空镜可另备 |
| 中段 4 秒 | 用户在山顶或完成登顶的可核验画面 | 主体须 user；人物与现场关系可判断。只有山景仍缺事件证据 |
| 结尾 4 秒 | 用户真实停留、远望，或不声称具体事件的环境氛围 | 动作须 user；氛围来源依对应查询，不能消除登顶缺口 |

拟用 4 秒时，建议优先提供约 6–8 秒连续稳定原片，前后留余量。此为拍摄建议，不是代码强制阈值。若有效片段过短，先查切分与其他候选，再由 C 调整故事或预算。

可复制给 A 的人工需求卡：

```text
需求：REQ-02 / beat-middle
目的：支撑“用户到达山顶”的主事件
画面：用户本人和现场环境能建立明确关联
来源：user；外部空镜不得作为该事件证据
拟用预算：4,000,000 us（4 秒）；原片另留剪辑余量
优先级：必须；确实缺失时允许讨论改写故事
A 回填：原片位置、asset_id/revision、segment_id、source_range_us、接入状态、来源说明
B/C 回填：查询、候选、事件审核状态、剩余缺口与原因
```

需求编号、优先级、景别、机位、画幅、分辨率、收音和命名规则均为人工补充项，不是当前 `RetrievalRequest` 新增字段。

## 4. 遇到缺口怎么处理

| 情况 | 先处理谁 | 下一步 |
| --- | --- | --- |
| 没返回可用候选 | B/C | 查查询和过滤；有限再检索，不能直接断言原片没有 |
| 解码、PTS、代理、路径或关键帧失败 | A | 修复接入，发布新 Manifest 后重验 |
| 片段太短 | A/B/C | 查连续原片、切分和其他候选；C 调整预算 |
| 人物、动作或源范围待审核 | C/人工 | 看原片；审核完成前不交下游 |
| 来源或授权不符合要求 | A/B/C | 补可追溯合法素材，保持来源约束 |
| 已核查而确实缺该事件 | 用户/A/C | 补素材，或改写/缩减故事；无法满足则保留缺口 |
| 旁白未批准 | C/用户 | 批准后再生成声音 |

`needs_input` 不等于“必须重拍”。现有 gap 包含 `gap_id/beat_id/type/missing/reason/iteration/status/suggestions/must_not_assert_user_events`。当前合成 unsupported 样例的原因为“该段请求没有返回任何可用候选”，不足以证明原片绝对没有该事件。

再检索到达 `max_iterations` 即停止；未耗尽时可增加缺口段落 primary 的 `top_k`，上限 8。A 修复或新增素材后应发布正确的新 revision，B 更新候选，C 重新绑定并审查事件证据，不能只改引用数字复用失效审核。

## 5. A 交回什么

正式机器交付仍是 `AssetManifest` 及其关联媒体。

| 类别 | 关键字段 |
| --- | --- |
| 身份和来源 | `asset_id`、`revision`、`content_hash`、`uri`、`origin`、`provenance_note` |
| 媒体规格 | `media_kind`、`duration_us`、`video`、`audio` |
| 时间和代理 | `source_pts_uri`、`proxy_uri`、`proxy_mapping_uri`、`proxy_status` |
| 可用片段 | `segment_id`、`asset_id/asset_revision`、`source_range_us`、`granularity`、`quality`、`keyframes` |
| 接入与转写 | `processing_status`、`transcript`；保留源范围及人工复核状态 |

所有 URI 按共同项目根解析。A 源时间以首个解码视频帧为零，使用整数微秒和半开区间；D 是成片时间，E 字幕是每句 WAV 的局部时间，不能混用。

ASR `status: success` 仅表示执行成功，`review.status: needs_review` 仍需人工复核。`CONTAINER_ONLY` 且 `usable=false` 的容器段不能当作可用选镜。

建议交原片、保留连续动作和环境声；统一项目目标画幅，说明静音与授权情况。文件名可用“项目_事件_take_序号”，机器身份始终以 Manifest 为准。这些是协作建议，不是额外 schema 要求。

## 6. 当前完成程度

| 范围 | 状态与边界 |
| --- | --- |
| C 核心 | 已实现大纲、画面需求、候选约束、事件审核、旁白审批、缺口与有限再检索。C 提交 `620b24f`，见 [PR #5](https://github.com/Tt200411/QuickCut/pull/5)；截至本次发布核对仍开放 |
| A/C/D/E 联调 | 集成了 A #4、D #2、E #1、C #5。本地基线 `4785e16` 全仓 812 passed in 35.50s |
| 媒体预览 | 合成 supported 10/15 秒与 edit r2 10 秒分别为 300/450/300 帧，全量解码成功；独立 SRT 未烧录 |
| 负例和增量编辑 | 缺口阻断 E/D；5 秒 tone 超预算阻断 D；仅改 cue-02 时其他两句缓存 hit，A 未重新接入、Manifest hash 不变 |
| 给 A 的交接 | 本说明可直接作为人工收集与补充规范；自动采集、下载和补拍派单尚未实现 |
| 正式产品验收 | 真实 B/Qwen、人审、自然语音、完整自动 fit、F 可编辑工程与剪映实机、课程评测仍待完成 |

合成 demo 中 A 处理真实编码视频，B 为人工候选，C 为 rule 与预设审核，E 为固定 tone；`fixtures_only` 向下传播。当前结果证明工程路径与门槛，不能代替真实语义和成片质量。MP4/SRT 也不代表剪映工程导入通过。

工程改动在 `feat/c-integration-engineering` 独立分支审查，原 C PR 范围保持独立。该分支包含尚未合并的队友 A/D/E 提交，团队应先协调依赖 PR，避免把全部集成差异视作 C 的新增贡献。

## 7. 下一步与核对依据

1. A/B/C 用真实授权原片检查素材支持、素材不支持、完整编辑往返；保留事件与源范围审核记录。
2. 接真实 B/Qwen 和自然语音，完成 D/E 时长适配；不沿用 fixture 的预设审批。
3. F 按冻结 Timeline 导出工程，验收剪映打开、编辑、保存、重开、搬迁和导出。
4. 按 proposal 验收约 5 分钟源→5–15 秒短片，以及 5–10 视频、30–50 prompts 的开发/held-out 评测。

- [C 交付说明](C_DELIVERY.md)
- [全链路工程说明](PIPELINE_ENGINEERING.md)
- [验收操作与证据索引](ACCEPTANCE_RUNBOOK.md#54-最终本地证据索引2026-11-13)
- [共享协议](contracts.md)
- [C 规划代码](../../quickcut/planning/planning.py)
- [检索请求示例](../../examples/team_contracts/c/retrieval_request.json)

原始本地运行证据在仓库外 `../outputs/QuickCut-engineering-final-20261113/`，未随 Git 提交；远端 workflow 的结果以实际 Actions run 为准。

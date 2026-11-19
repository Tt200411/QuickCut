# C 模块交付：故事与视听规划

## 2026-11-16 集成更新：生产草案与确认

原 PR 的提交、测试数字和夹具结果保留在下文，属于 **2026-11-13 的历史交付**。当前 HTML 入口与完整操作见 [本地工作台](WORKBENCH.md)，全链路边界见 [工程说明](PIPELINE_ENGINEERING.md)。截至本次更新，真实工作台已开始接入用户视频，Qwen → IndexTTS2 → D → F 的本轮完整链路正在验收；本节记录已实现的接口，不提前声明真实整链通过。

新增入口为 `quickcut.planning.production`，分为两个独立阶段：

```python
draft = generate_draft(
    brief, manifest, candidate_set,
    retrieval_request=request, project_root=project_root,
    model="qwen2.5vl:3b", archive_dir=project_root / "model-evidence",
)
result = confirm_draft(
    draft, approved_text_by_beat=reviewed_text,
    reviewer=reviewer, reviewed_at=timestamp_with_timezone,
    confirmation_kind="human", plan_revision=1,
    selection_overrides=None,
)
```

`generate_draft` 必须消费真实、非 fixture 输入，并实际调用本地 Ollama。每段先合并同一源片段在不同 B 查询中的候选别名，再用一张带编号接触表请求模型选镜与中文旁白；不使用固定 `APPROVED` 文案，也不降级到 rule。输出为 `needs_review`，包含候选与源区间、实际模型选镜、`planned_shots` 的建议时长、中文草稿、旁白字数预算、冻结输入及其内容哈希、模型调用记录。`planned_shots` 应用于 UI 默认边界，不能另按平均时长重算。

`confirm_draft` 使用服务器保存的冻结草案，不再次推理。确认必须逐段提供非空文本、实际确认者和含时区时间；记录绑定草案、镜头/素材版本/源区间及文本哈希，再通过现有 `plan` 和 StoryPlan validator。`plan_revision` 仅接受正整数，拒绝 bool、float、字符串；同草案改稿时保留文档 ID、发布新 revision，旧声音/时间线不能只修改引用后复用。2026-11-19 的旁白修复将字数/时长估计改为非阻断提示：按每秒 2.5 个朗读字加 0.8 秒停顿粗估，字母和数字逐字符计数，不含标点和空格。`narration_max_chars` 名称保留但只作参考；旧草案保存的参考值和冻结输入不改写。超过字数参考或粗估时长超过画面预算都会提示，完整句仍可明确确认；最终由 E 的全文合成和真实 PCM 时长决定，并由 D/E 有界适配，不截尾。160 字符编辑安全上限仍然阻断确认。

`selection_overrides={beat_id: [candidate_id, ...]}` 支持在冻结候选中替换或重排镜头；未传时保留原模型选择。`preview_selections(draft, selection_overrides=None)` 返回按 beat ID 索引的 `selected_candidate_ids/planned_shots`，用于 UI 重算时长，不推理、不批准。非法/重复 ID、跨段重复源片段及不合法容量会拒绝。修改后的镜头写 `decided_by=human_edit/assistant_edit`，`selection_edits` 记录原顺序、新顺序、确认者和时间；原始模型响应与调用身份保持原样，不能将人工选择写成模型输出。

`confirmation_kind="human"` 表示用户在界面实际确认。自动验收须使用 `assistant_review`，写入 `assistant_montage_review` / `assistant_review`、`human_review_performed=false`，修改记录放入 `assistant_edits`；不得记录成真人看片或听审。

生产 Brief 使用 `picture_policy="thematic_montage"`：`origin=user/external/generated` 原样保留，画面只承担主题、环境和情绪，不证明用户具体经历，不使用事件主镜头 `primary`。原 `authorized_external_montage` 路径若显式选择 Ollama，也必须调用模型或明确失败，不能先被规则分支跳过。真实事件叙事的 `event_review` 门槛仍适用于该路径以外的事实主镜头。

请求证据在发出前落盘：完整 prompt、模型与参数、实际附图、附图及源关键帧 SHA-256、候选/图片格子/源时间映射、开始时间；收到响应后补充原始响应、解析结果、耗时和实际执行标记。超时或错误保留请求与失败状态。Ollama 传输显式绕过系统和环境 HTTP 代理，避免本地素材被送入代理。文档 `input_hashes` 对完整文档内容计算，不再只哈希引用三元组。

真实运行暴露了默认 4096 上下文不足和模型只返回旁白两项问题。现显式设置 `num_ctx=8192`、`num_predict=1024`，每段最多 8 个去重候选送模型，完整候选仍在冻结输入中，未提交候选 ID 另记。`format` 使用 JSON Schema，要求 `shot_order/reasons/roles/narration_draft`，选镜 ID 来自实际候选 enum；这是 Ollama 官方 [Generate API](https://docs.ollama.com/api/generate) 和[结构化输出](https://docs.ollama.com/capabilities/structured-outputs)支持的方式。格式错误最多再调用真实模型一次，两个请求和原始响应分别保存并通过 `model_attempts` 关联；HTTP 错误正文归档并显示，不能以 rule 代填。上述是真实失败后的修复，**修复后的完整运行仍待验收**。

后续真实运行又发现模型全选长候选 ID，重复理由/角色键使输出预算耗尽。生产模型现使用 `C01/C02` 等短别名，`candidate_aliases` 保存其到完整 ID 的无损映射；容量允许时开场/中段最多选 2 镜头，预算不超过 5 秒时最多 1 镜头。当前版本按候选容量提高必要的选镜数量上限，最多 8 镜头，实际选择仍由模型决定。`reasons/roles` 只能对应实际所选短 ID。程序严格白名单还原，原始 `body/parsed` 保留模型短 ID，另存 `canonicalized`；这不是规则代选。模型只接收精简 ASR 摘要（文本最多 240 字、最多 3 个带源范围的 spans，标注省略数），完整 ASR/复核诊断仍保留于 `candidate_evidence` 及冻结输入。

真实运行出现过超预算和残句旁白。当前产品边界将结构与内容分开：候选 ID、理由、角色等结构不合法仍拒绝并最多追加一次真实模型格式修复；首次选镜响应未完成直接拒绝。结构合法的真实选镜保留，旁白缺少句末标点、空白、非中文、明显残句或超过 160 字符进入阻断 `warnings`，字数/粗估时长进入非阻断 `advisories`。初次生成和独立改写的 JSON Schema 不设 `maxLength`，避免解码器硬截句。存在内容或时长提示时，模型只重写整句旁白，最多两次，不重新选镜；只有无内容 warning 且优于当前文本的候选才会替换。改写图像按已选镜头顺序提供，生产模式缺少任一已选镜头的可读画面时跳过改写并留证。格式失败可在上限内重试，网络失败（包括 HTTP 响应中断）停止重试并保留当前文字；归档读写失败返回结构化错误并停止。原始响应、规范化选镜、独立改写请求/响应和最终文本哈希均保留，改写计入 `model_attempts`，不能自动批准。`confirm_draft` 与 HTTP 确认接口共同检查内容 warning；粗估超时不能替代真实配音测量。已有冻结草案和模型原始记录保持原样，用户或助手修改分别留证；这些文本启发式不等于语法、画面事实或听感已获批准。

B 的顶层或任一 query `status=error` 会阻断 C，不能作为合法空集继续。B 的 `transcript_evidence` 与 A 的完整 segment transcript 核对；C 把 ASR 状态、span/range、review 状态及资产级诊断保留给模型与 StoryPlan。`needs_review` 不会被解释为转写已准确。

本轮新增生产契约测试 **38 passed**，覆盖两阶段确认、请求归档与结构重试、短别名还原、失败证据、代理绕行、同片段候选去重、来源保留、助手确认、坏文案明确待改且确认拒绝、改稿 revision 和换镜/重排。这些测试模拟模型网络响应，**不替代正在进行的真实 Qwen 与成片验收**。

## 原 PR 交付背景（历史）

原交付基于 `1e342b6`，使用协议 `0.1.0`。负责模块 C，作者 Zhang Zicheng。B 接收检索请求，D 和 E 接收故事板；C 不编排最终时间线，也不生成音频。原 PR 未修改 `types.py`、`core.py` 或共享 `bundle.json`。以下章节保存原 PR 的交付范围与当时验证状态；生产新增能力以上文为准。

## 输入与输出

输入为 `Brief`、`AssetManifest`、`RetrievalRequest` 和 `CandidateSet`，输出为 `RetrievalRequest`、`StoryPlan`、对齐报告及运行记录。所有时间使用整数微秒，源区间为半开区间，`duration_budget_us` 是规划预算。

输入引用必须包含精确的 kind、document_id、revision，依赖必须齐全且项目一致。片段和候选引用的 asset_revision 必须匹配素材当前版本；版本过期返回 `STALE_INPUT_REVISION`。

## 原 PR 运行与验证（2026-11-13）

从仓库根目录执行，Python 3.10 以上：

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev,runtime,server]' 'pillow>=10.0'
.venv/bin/python -m quickcut.planning run --mode rule --out-dir examples/team_contracts/c
.venv/bin/python -m quickcut.planning run --mode fixed --out-dir examples/team_contracts/c/fixed
.venv/bin/python -m quickcut.planning run --case insufficient --out-dir examples/team_contracts/c/insufficient
.venv/bin/python -m quickcut.planning selftest
.venv/bin/python -m quickcut.planning inject --output outputs/c/injected_bundle.json
.venv/bin/python -m pytest -q
```

2026 年 11 月 13 日，本地 Python 3.13 环境验证结果：

- 第二轮审查后，全仓库 576 passed，其中 C 模块 494 passed；原有非 C 的 82 项测试未改动。
- `selftest` 为 6/6，一个合法正例与五个负例符合预期。
- 完整故事和固定响应输出 `ready_for_edit`，三段、三句旁白、零缺口。
- 素材不足输出 `needs_input`，一个事件证据缺口，并生成下一轮检索请求。
- 注入八文档 bundle 后，官方校验器通过，五个非法夹具全部拒绝。

`rule` 是确定性选镜，`fixed` 是固定响应夹具；两者均不代表模型推理。`ollama` 适配器支持本地 Qwen，使用离线模拟验证了响应解析、缺图、时间映射和路径边界，本次没有运行真实模型。

## 证据与人工审批

检索排名不能证明具体事件。真实输入必须显式传入 `--reviewed-evidence`，格式为以 candidate_id 为键的对象：

```json
{
  "candidate-seg-packing": {
    "beat_id": "beat-open",
    "event": "与 outline 中 wish.event 完全一致",
    "asset_id": "来自 AssetManifest",
    "segment_id": "seg-packing",
    "asset_revision": 1,
    "source_range_us": {"begin": 0, "end": 3000000},
    "reviewer": "实际审阅者",
    "note": "看片后记录动作、角色与该段目标的对应关系"
  }
}
```

以上仅说明字段，真实审阅须填实际值。更换候选片段、素材版本、源区间或事件目标后，旧审核失效。缺少有效审核的用户候选不参与事实主镜头选择。外部和生成素材不能作为用户事件的事实证据。

默认夹具允许 `fixture_annotation` 模拟证据，并显式记录该来源。`fixtures_only` 从任一上游传播；夹具通过不代表真人审阅过真实视频。

旁白批准与画面证据审阅分别处理。自定义 bundle 必须显式传 `--approved-cues` 才能批准旁白；默认黄金文案不会自动套到其他输入。模板句带 `approved: false`，写入 `pending_reviews` 并保持 `needs_input`。禁用旁白时不生成 cues。`ready_for_edit` 要求缺口清空、审阅完成且所有旁白获批。

## 审查中修复的问题

本轮审查修复了依赖缺失绕过版本检查、夹具标记未传播、未审批模板仍可交付、禁用空镜仍被选择，以及模型读取项目外图片等问题。关键帧现在保留候选 ID、图片索引和源时间映射，缺图不会宣称存在视觉证据。

最终复核又复现了两处证据链漏洞：候选换成其他片段后沿用旧审核，以及片段版本落后于素材版本。现在审核绑定具体素材、片段、源区间和事件，清单索引也会拒绝旧片段版本。相关回归加入 `test_planning_contracts.py`。

第二轮逐文件复核补上了以下约束：candidate、segment、asset 的嵌套质量标记统一过滤；无效或未审阅候选不进入事件选镜，有合法替代证据时不再被无关待审项阻塞；事件缺口不能靠禁用旁白、改写表达或删除 gap 绕过。旁白单项及同段累计预算受 beat 限制，external 和 generated 空镜合计受逐段及全局 40% 上限限制，边界使用整数运算。

规则选镜保留空镜具体职责，establishing、transition、detail、atmosphere 按配置产生独立请求。默认现在有六条查询；transition 的夹具结果显式为空，不虚构素材。结尾 atmosphere 可以补情绪，但不能消除事件证据缺口。多用户镜头补余遍历所有可用容量，关键帧必须有合法整数时间且位于候选半开源区间。

`run` 和 `validate` 统一解析上游输入，支持 `--retrieval-request`；`run` 保存同批输入的 `bundle.json` 供 C 重验。这个 bundle 中 D/E/F 文档仍来自原始输入，不能直接当作完整八文档联调输出；完整协议检查仍使用 `inject`。自定义输入不会自动得到默认文案批准。

一键复核命令为 `python scripts/review_c_planning.py`，加 `--all-tests` 可运行全仓库。脚本自动发现全部 `test_planning*.py`，并检查三种案例的生成及重验、selftest、八文档注入及官方五负例；输出放临时目录。Python 3.10/3.13 CI 使用同一入口。

## 接口扩展与接收方动作

新增字段均为可选字段，未改变共享协议必填字段与时间单位。合并前请 B、D、E 确认以下消费方式：

| 位置 | 扩展及用途 |
|---|---|
| RetrievalRequest | iteration、iteration_notes、beat_purpose、relaxed_constraints，用于有限重检索 |
| StoryPlan | pending_reviews、decision_provenance，记录审批与决策来源 |
| beats | support、alternative_candidate_ids，表示证据状态与替换候选 |
| shots | origin、decided_by、event_review，记录来源、选镜方式和审阅证据 |
| narration_cues | approved、text_source、content_review、expression、asserts_specific_user_event |
| unresolved_gaps | iteration、status、must_not_assert_user_events，供 D 阻止错误导出 |

B 按 query_id 回填 CandidateSet；空结果带原因，后续轮次必须返回匹配新请求 revision 的候选。D 检查 status、缺口和待审项后再编译时间线。E 仅使用已批准旁白。C 模块 CI 在 Python 3.10 和 3.13 上运行离线测试、selftest 和官方注入校验，无需模型和真实媒体。

## 原 PR 与手册要求的对应及剩余验收

Brief 拆段、分角色检索请求、三态二次对齐、候选白名单选镜、段落预算、逐句旁白、原始响应和方案比较已有代码及离线验证。预算、源区间、来源限制、同段 cue 证据及精确版本均有自动校验。

仍待完成：真实本地 Qwen 推理及输出留档、人工对照真实画面的角色动作审核、与 B 的多轮真实检索联调。三段 hook/journey/close 为固定结构；多风格、多方案自动生成、跨素材人物地点一致性属于尚未实现的 P1。当前 compare 仅比较给定故事板。Brief 事件拆分依赖标点与连接词，复杂叙事需要人工确认。

原 PR 可用于独立夹具交付和接口联调。上述历史夹具结果不能用于宣称全项目完成；最新生产接口与当前验收边界见本文开头和 [WORKBENCH.md](WORKBENCH.md)。

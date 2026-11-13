# C 模块交付：故事与视听规划

本次交付基于 `1e342b6`，使用协议 `0.1.0`。负责模块 C，作者 Zhang Zicheng。B 接收检索请求，D 和 E 接收故事板；C 不编排最终时间线，也不生成音频。未修改 `types.py`、`core.py` 或共享 `bundle.json`。

## 输入与输出

输入为 `Brief`、`AssetManifest`、`RetrievalRequest` 和 `CandidateSet`，输出为 `RetrievalRequest`、`StoryPlan`、对齐报告及运行记录。所有时间使用整数微秒，源区间为半开区间，`duration_budget_us` 是规划预算。

输入引用必须包含精确的 kind、document_id、revision，依赖必须齐全且项目一致。片段和候选引用的 asset_revision 必须匹配素材当前版本；版本过期返回 `STALE_INPUT_REVISION`。

## 运行与验证

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

- 全仓库 312 passed，其中 C 模块 230 passed，原有 82 项测试未改动。
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

## 与手册要求的对应及剩余验收

Brief 拆段、分角色检索请求、三态二次对齐、候选白名单选镜、段落预算、逐句旁白、原始响应和方案比较已有代码及离线验证。预算、源区间、来源限制、同段 cue 证据及精确版本均有自动校验。

仍待完成：真实本地 Qwen 推理及输出留档、人工对照真实画面的角色动作审核、与 B 的多轮真实检索联调。三段 hook/journey/close 为固定结构；多风格、多方案自动生成、跨素材人物地点一致性属于尚未实现的 P1。当前 compare 仅比较给定故事板。Brief 事件拆分依赖标点与连接词，复杂叙事需要人工确认。

本 PR 可用于独立夹具交付和接口联调。真实模型及媒体验收仍未完成，不能据此宣称全项目完成。

# D：人工边界与 D → E → D 有限适配

本说明对应 `quickcut.timeline`、`quickcut.integration` 和 `quickcut.retiming` 的新 IR 入口。D 消费 C 已批准的 StoryPlan、当前 A/B 依赖和 E 的真实测量包，生成四轨 `EditTimeline`。旧 `Timeline/TimelineItem` 不是这些入口的输入。

截至 **2026-11-16**，以下接口已有自动化回归，包括独立 Python 子进程返回 E 包、过期/损坏回复拒绝、完整 WAV 核验和 F 回执门禁。子进程回归使用显式 fixture 音调，不代表真实模型或听审通过。本轮真实 HTML UI 整链仍在验证，不能据此填写实测成功。

## 人工确认入出点

`compile_timeline`、初编桥接及下面三个适配 API 均接受可选 `boundaries`。对象绑定精确 StoryPlan 和 AssetManifest 引用；每个修改按 **`beat_id + shot_index`** 定位，`shot_index` 是该 beat 的 `shots` 中从 0 开始的索引。`segment_id` 用于交叉核对，不能单独当修改键：同一 segment 可多次入选。

```json
{
  "story_plan": {"kind": "StoryPlan", "document_id": "story-001", "revision": 7},
  "asset_manifest": {"kind": "AssetManifest", "document_id": "manifest-001", "revision": 2},
  "edits": [{
    "beat_id": "beat-open",
    "shot_index": 0,
    "segment_id": "seg-packing",
    "source_range_us": {"begin": 3500000, "end": 9000000},
    "method": "manual",
    "reviewer": "实际确认者",
    "reason": "已检查这一段的动作入点和出点"
  }]
}
```

以上 ID、版本和确认信息仅示意，调用方必须替换成当前真实值。区间使用整数微秒 `[begin, end)`，须落在 A 的入选 segment 内；重复修改同一 shot、陈旧引用、布尔 revision/index、缺少 reviewer/reason 或非 `manual` 方法均拒绝。人工区间的出点是硬边界；D/E 适配不会自动扩展它。确认后即使没有改变默认数值，也应保存实际 reviewer/reason。

未提交人工修改的 shot 仍采用 `segment_prefix_budget`：从入选 segment 起点取 C 的建议长度，允许在同一 segment 的剩余尾段、beat 预算和 Brief 总预算内延长。它没有读取局部画面或音频来判断动作、场景切换或停顿。当前实现不能称为模型动作精剪；是否保留动作/话语上下文仍需审阅。

D 在待适配和最终 Timeline 中保存 `boundary_decisions`，包括 beat/shot/segment、方法、reviewer/reason、A 原区间、实际源区间与 clip ID。画面和关联原声使用同一源区间。改动边界后，旧 fit 请求与旧成片均不能继续作为当前结果。

## 入口和职责

| 入口 | 接受与返回 |
| --- | --- |
| `compile_from_voice` / `build_from_plan` | 保留初编行为；只接受精确绑定 StoryPlan 的 ready 初测包，不接受带 Timeline 引用的 fit 回复，也不自动重试 |
| `prepare_voice_fit` | 校验当前 C/E 与实际 WAV，返回 `{"pending_timeline": ..., "request": ...}`；不调用模型、不导出 |
| `compile_voice_fit` | 接受指定 pending/request 的 E fit 回复；核验后返回 `IntegrationResult`，超时回复仍拒绝成为成片 |
| `complete_voice_timeline` | 先编译 `mode="measure"` 包；需要适配时顺序调用外部 provider，最多两轮；成功或失败均保留过程记录 |

三个适配入口共同要求 `result`（C 的 `PlanResult`）、`brief`、`manifest`、`candidate_set`、`project_root` 和 `expected_story_revision`。`project_root` 是 A/E 共享的媒体根，供核验实际 WAV；`expected_story_revision` 必须来自任务创建时锁定的版本，不能临时读新文件来掩盖过期任务。`complete_voice_timeline` 另接受可选 `fit_observer(record)`，在派发 E 前及收到回复、接受、拒绝、取消时同步通知；传入的是深拷贝，观察者不能修改实际请求。保存失败会报 `TRACE_WRITE_FAILED` 并停止派发或发布。

```python
from quickcut.integration import complete_voice_timeline, IntegrationError

# result/current_brief/current_manifest/current_candidates/measured_voice 来自同一任务。
# run_e_fit_in_separate_process(request) 返回已解析的 E VoicePackage 字典。
try:
    built = complete_voice_timeline(
        result, measured_voice,
        brief=current_brief, manifest=current_manifest,
        candidate_set=current_candidates, output=output_spec,
        project_root=project_root,
        expected_story_revision=locked_story_revision,
        fit_provider=run_e_fit_in_separate_process,
        max_iterations=2,
        timeline_document_id="timeline-001", timeline_revision=1,
        boundaries=confirmed_boundaries,
    )
except IntegrationError as exc:
    # 保存 exc.to_dict(project_id)、exc.voice_package 和 exc.fit_rounds 后报告失败。
    # 不把最后一个失败回复导出为 ready 时间线。
    raise
# 调用方保存 built.story_plan/voice_package/timeline/assets 和 built.fit_rounds。
```

`output_spec` 包含 width、height、有理数 fps 和 sample_rate。`boundaries=None` 表示仅采用已记录的默认区间方法，不表示完成了人工边界审阅。API 不保存聚合 JSON、不渲染、不安装剪映；发布前调用方还需确认任务仍是当前版本，并保存快照及哈希。

如果调度器需要分开派发任务，使用：

```python
prepared = prepare_voice_fit(
    result, measured_voice, brief=brief, manifest=manifest,
    candidate_set=candidate_set, output=output, project_root=project_root,
    expected_story_revision=locked_story_revision,
    timeline_document_id="timeline-001", timeline_revision=1,
    iteration=1, max_iterations=2, previous_request=None, boundaries=boundaries,
)
built = compile_voice_fit(
    result, reply, pending_timeline=prepared["pending_timeline"], request=prepared["request"],
    brief=brief, manifest=manifest, candidate_set=candidate_set, project_root=project_root,
    expected_story_revision=locked_story_revision, boundaries=boundaries,
)
```

这两个函数从 `quickcut.integration` 导入。第二轮须传前一份完整 reply 作为 `measured_voice`、前一份 request 作为 `previous_request`，并设 `iteration=2`；不能跳过上一轮或重用第一轮回复来满足第二轮请求。手动分派时由调度器保存所有 pending/request/reply；`complete_voice_timeline` 则返回完整 `fit_rounds`。

## 外部 E 进程约定

provider 的签名是 **`fit_provider(request: dict) -> VoicePackage dict`**。一次只执行一轮。请求有 `kind="VoiceFitRequest"`，这是任务元数据，不替代八种核心交接文档。其字段包括精确 StoryPlan/pending Timeline 引用、输入内容哈希、`request_id`、`iteration/max_iterations`、上一请求 ID、原测量包引用/哈希、`expected_voice`、`voice_revision` 和完整 `cue_ranges`。

外部进程使用同一批准 StoryPlan、声音配置和媒体根，传 `--mode fit --request <本轮请求文件> --revision <request.voice_revision>`。E CLI 的退出码 2 也会交付完整 JSON/WAV，调用方应读取其状态，再由 D 判定 `needs_retime`、partial 或 failed；不能把所有非零退出都当成没有结果。

IndexTTS 使用独立 Python 3.11 环境，例如通过 `scripts/indextts2_python.sh -m quickcut.voice_package ...` 启动。D 的编排进程不加载或驻留 TTS 模型。16 GB 机器应串行运行重模型阶段，完成前一阶段卸载后再启动下一阶段；环境和权重准备见 [E 说明](E-local-voice.md)。

## 适配规则与严格回执

1. D 校验逐 cue 批准、文本、script 引用、实际 WAV 的 SHA-256、mono PCM16 / 48 kHz、完整采样时长（容差一个采样）和非全零信号。非全零只排除纯静音，不证明发音或语义正确。
2. D 保持镜头顺序，仅利用默认区间可用尾段和现有预算。人工确认出点不延长，也不自动插入新空镜。需要 E 适配时生成 `status="pending_voice_fit"` 的不可导出 Timeline，并分配明确全局句子区间；请求不会增大 C 已批准的单句预算。
3. E 保留完整声音，用有界变速适配并回显完整 request。D 接受前同时核对内容哈希和严格整数 revision；相同 revision 下偷偷修改 StoryPlan、Brief、Manifest、边界或 pending 内容也拒绝。E 的 Timeline 输入引用保留，不删掉它绕过旧入口。
4. 最终 Timeline 只在完整音频适配成功后变为 `ready_for_export`。短于请求区间的声音保留实际长度，后续句子仍使用本轮冻结起点；不靠裁尾或拉长静音伪造适配。最多两轮后仍超预算，保留完整回复和受影响 cue，返回 `VOICE_NEEDS_RETIME` 交 C/人工处理。
5. 最终 `voice_fit` 保存完整 request、pending Timeline、accepted VoicePackage 引用和 `reply_hash`。`fit_rounds` 另保留每轮 pending/request/reply、哈希、状态及错误。仅有回显 Timeline ref 的旧样例不足以通过此门禁。

F 的预览、导出和包校验复用回执验证：检查最终目标引用、E 回复哈希、A 内容哈希、冻结画面/原声与边界、旁白完整源区间及全局起点。缺失/过期回执或接受后私改时序会被拒绝；修改后应重新走 D/E 冻结流程，不能手改哈希。详见 [F 导出说明](F-export.md)。

回归入口为 `tests/test_timeline_boundaries.py`、`tests/test_retiming.py`、`tests/test_voice_package.py`，并与 timeline/integration/preview/export 测试一起运行。本轮这组 **247 项通过**，包含剥离 fit 引用/回执后的拒绝、生产缺 mode 拒绝及旧黄金 fixture 的有限兼容；此前限定版本的剪映实机证据见 [F 验收记录](F-acceptance-20261115.md)，不据此推定本轮新整链已完成听审或编辑器验收。

## 工作台修订留档与取消

工作台每次确认创建新的 `revisions/r<attempt>/`，已有修订目录拒绝覆盖。归档包含当轮批准记录、草案与上游 JSON、声音配置、StoryPlan、边界、E 初测包、每轮 pending/request/reply、累计 `fit-rounds`、原始 D 输出及最终 Timeline；成功再保存导出清单和报告，失败保存错误及可用的完整 E 回复。顶层常用 JSON 继续供现有界面读取，历史追溯以修订目录和报告中的 `revision_archive` 为准。

`fit_observer` 在启动 E 子进程之前保存完整 pending 与 request，因此中断时也能找到尚未完成的请求。下一次确认使用新 StoryPlan/E/Timeline 版本，旧失败轮不会被新的 `fit-request-1.json` 或 `fit-rounds.json` 覆盖。运行时取消保留 `CANCELLED` 和轮次的 `cancelled` 状态，工作台显示 `interrupted`，不会把关闭服务误报为 TTS 合成失败；有效已完成草案可返回审阅继续生成。

字幕位置属于最终冻结前的显示策略。工作台保留 `timeline-d.json`，将 settings 的 top/bottom 写入最终 `timeline.json`，并用 `presentation-policy.json` 记录前后哈希。它不修改画面或声音时序，也不重写 fit 请求、pending 哈希或 E 回执。归档和取消的专项回归见 `tests/test_pipeline_revision_archive.py`；失败 r1 → 成功 r2 的测试逐字节检查 r1 不变。

## 2026-11-16 真实语音缓存 fit 验收

独立目录 `outputs/workbench-fit-acceptance-20261116/` 已复用真实工作台任务 `ac91f60c01ca` 的 `voice-measured-r1.json` 和原批准 StoryPlan，完成一次 D → E → D 适配。所有语音测量字段、批准文本、源引用及内容哈希保持真实值。为了触发适配，仅将验收副本的末镜头从源 35–40 秒缩到 35–37.427974 秒；这项测试边界不写回主任务，也不是新的真人审阅记录。

三句均 `cache_hit: true`；E 对第三句实际运行 FFmpeg `atempo=1.080000036`，完整 WAV 从 2.607812 秒变为 2.419062 秒，保留 0.18 秒句尾停顿，落入 2.427974 秒请求窗口。D 在第一轮接受，完整 WAV/hash、最终四轨合同及 F fit receipt 校验通过。该次验收禁止调用神经合成，实际调用数为 0，未导入 torch；读取的主任务文件哈希前后不变。

证据为该目录下的 `report.json`、`project/fit-rounds.json`、实际 WAV 与 `run_cached_fit.py`。复现时使用独立的新目标目录和包含 IndexTTS 元数据的 Python 环境；缓存/配置不一致会停止，不生成替代音频：

```bash
outputs/indextts2-runtime/upstream/.venv/bin/python \
  outputs/workbench-fit-acceptance-20261116/run_cached_fit.py \
  --source outputs/workbench/ac91f60c01ca \
  --measured voice-measured-r1.json \
  --destination outputs/workbench-fit-acceptance-repeat/project \
  --config examples/voice_package/indextts2.config.json
```

源目录须保留与指定初测包精确匹配的 StoryPlan；若主任务已切换到新修订，应先从对应修订归档和原媒体建立独立输入副本，不能把新 StoryPlan 拼给旧 E 包。此次结果验证真实缓存、真实变速及协议闭环；没有执行本验收副本的主观听审、MP4 渲染或剪映实机编辑，不替代主任务的新整链验收。

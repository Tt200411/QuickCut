# 2026-11-16：HTML 工作台与剪映实机验收记录

本记录只覆盖已经观察到的操作和落盘证据。旧任务 `ac91f60c01ca` 已完成真实模型链路、网页确认与修改、F 输出、限定版本剪映打开和字幕修改保存重开，并完成两次原生导出。使用用户最新 ZIP 中所选男声参考的新任务 `1c1bb824f8a9` 也已完成 A–F；2026-11-17 补记了其独立媒体核对、网页播放、新版剪映四轨打开和原生导出证据。新版未重复字幕修改保存重开，旧版该项结果不移记到新版。

操作方式为用户授权的 `assistant_review`，不是真人逐项审阅或真人听审。证据主要保存在本机忽略目录；仓库中的本说明不等于所有媒体已上传远端。

## 任务、输入与确认范围

旧任务目录：[outputs/workbench/ac91f60c01ca](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca)。目标为 30 秒、自然宁静的山林与风景主题短片。浏览器实际上传四份视频，来源登记为 external：

| 上传文件 | 工作台文件 |
| --- | --- |
| `long-01-mandarin-park-guide.webm` | `uploads/00.webm` |
| `short-01-trail-back-view.mp4` | `uploads/01.mp4` |
| `broll-02-countryside-aerial.mp4` | `uploads/02.mp4` |
| `long-02-countryside-sunset.mp4` | `uploads/03.mp4` |

实际 A/B/C 阶段分别使用 faster-whisper small、OpenCLIP nllb-clip-base 和本机 Qwen `qwen2.5vl:3b`。候选清单记录了模型版本、权重哈希与输入指纹；Qwen 调用保存在 `model-evidence/`。确认阶段使用已有模型草案，未把确认回放标成再次推理。

网页中实际替换了两个镜头：开场首镜换成路径素材，结尾换为另一段公园风景；随后确认五个镜头的源入出点和三句旁白。StoryPlan 的 `decision_provenance.selection_edits` 保留原模型选项、修改后选项和 `assistant_edit`；每个边界都有 `method: manual`、实际确认者与理由。r2 的确认时间为 `2026-11-16T13:14:59.582072+00:00`，记录明确 `confirmation_kind: assistant_review`、`human_review_performed: false`。

对应证据：[r2 批准记录](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/revisions/r2/approval.json)、[r2 StoryPlan](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/revisions/r2/story-plan.json)、[r2 边界](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/revisions/r2/boundaries.json)、[网页完成截图](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/review-evidence/workbench-completed.png)。镜头替换的本轮验收发生在 **HTML 工作台**，不据此声称在剪映原生编辑器中完成了替换镜头。

## 真实 IndexTTS 初测与失败后重试

旧任务使用项目中既有中文示例音频作为参考；参考音频 SHA-256 为 `da1f00d4ca84680174cad16b9db0c4d7a2f21e7545bacce583cf83be06a6ed95`。[参考来源记录](../../deliveries/quickcut-20261117/outputs/indextts2-runtime/reference-provenance.json) 标明该参考来自项目的 Reed 系统语音样本，用作部署验证的条件输入；本次 E 的三句输出仍由真实 IndexTTS 生成，没有回退到系统语音。它与用户后来从新 ZIP 中选定的男声是不同的验收输入。E 使用 IndexTTS2 2.0.0、模型修订 `740dcaf`、MPS、seed 42，实际生成三句完整 PCM WAV。

| cue | 批准文案 | 完整 WAV 时长 |
| --- | --- | ---: |
| cue-01 | 沿着林间小路，走进清新的绿意。 | 4.633521 秒 |
| cue-02 | 云雾掠过山头，草木在风中舒展，让脚步慢下来。 | 6.168625 秒 |
| cue-03 | 把宁静留在风景里。 | 2.607812 秒 |

`voice-measured-r1.json` 的三句 `cache_hit` 均为 false，`mode: measure`、`fixtures_only: false`、`tempo_ratio: 1.0`；不是测试音调或系统语音替代。时长包括 0.18 秒句尾停顿。独立 Whisper 复核保留了“绿蚁”“您敬”等识别差异，没有改写批准字幕，也未标成全部精确识别通过。原始结果见 [声音内容复核](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/review-evidence/voice-content-check.json)；该复核不代表真人对自然度和发音的听审。

r1 在 F 阶段因 WebM 的流时长字段缺失失败，错误为 `MEDIA_UNREADABLE` / “ffprobe 未能测量流时长”，没有将其当作成功导出。错误和当时产物已保存在 [r1 目录](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/revisions/r1)；该首次运行早于新的自动逐修订归档，留存的失败证据不应误称为当时已自动保存了所有新格式记录。

修复 WebM 时长测量后，任务从已确认草案进入 r2，无需重新上传或重跑已经完成的 A/B/C。r2 的 `voice-measured-r11.json` 三句全部 `cache_hit: true`，保留上述真实初测音频和时长；StoryPlan 修订为 2，VoicePackage / EditTimeline 修订为 11。主任务旁白均在预算内，`fit-rounds.json` 为空；主任务成功不冒充已经触发了自动 fit，另有独立真实 fit 验收见下文。

## r2 的 F 输出

r2 于 `2026-11-16T13:15:19.150662+00:00` 完成。F 预览为 **30 秒、1280×720、30 fps / 900 帧、48 kHz 双声道**，全量解码通过。最终时间线为 5 段画面、5 段独立原声、3 段旁白和 3 个字幕对象。

公园导览原片自带底部烧录字幕，因此新旁白字幕选择顶部。设置及理由保存于 [subtitle-placement.json](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/review-evidence/subtitle-placement.json)。r2 同时保留 `timeline-d.json` 和 [presentation-policy.json](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/revisions/r2/presentation-policy.json)，记录最终冻结前显示策略的前后哈希；没有改写 D/E 的声音位置或 fit 请求。

产物与检查：

- [MP4 预览](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/export-r2/preview.mp4)、[预览检查报告](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/export-r2/preview.preview.json)。
- [可迁移工程 ZIP](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/QuickCut-project-r2.zip)、[F 导出清单](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/export-r2/export-manifest.json)。
- [r2 运行报告](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/revisions/r2/run-report.json)、[r2 归档](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/revisions/r2)。

命令生成的 F 清单仍为 `generated`；预览报告、安装返回值不会因随后发生的桌面验收自动改写为 `verified_import`。以下原生操作单独记录。

## 剪映原生打开、修改、保存重开与导出

在 **macOS 剪映专业版 6.1.0 / build 309** 中实际打开 `QuickCut-ac91f60c01ca-r2`。界面显示独立文字、画面、原声、旁白四类轨道，媒体在线且字幕是文字对象。证据见 [四轨截图](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/review-evidence/jianying-four-tracks.png)。

本轮实际在剪映内修改了字幕标点，包含感叹号 **“！”**；保存、退出并重新打开工程后，修改文字仍保留。此项证明了该草稿的字幕可编辑和保存重开行为。此次原生精修只改标点，没有执行原生镜头替换、旁白替换或逐轨增益调整；不把网页中的镜头替换记作剪映操作，也不挪用 11 月 15 日其他工程的修改范围。

原生编辑及重开前后分别完成导出，文件已移到该任务的 `review-evidence/`：

| 文件 | 时长及格式 | SHA-256 |
| --- | --- | --- |
| [native-export-initial.mov](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/review-evidence/native-export-initial.mov) | 30 秒、1920×1080 H.264；44.1 kHz 双声道 AAC | `f8a5ac5cac718530f329243003cd9db1644cbf6308c54b7f604faf03623e20ef` |
| [native-export-reopened.mov](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/review-evidence/native-export-reopened.mov) | 30 秒、1920×1080 H.264；44.1 kHz 双声道 AAC | `cc2fe5b4092df2ef56b2f0c44c0a491dc67e407fcb6c12d9283bdd96a30a21f0` |

两文件全量解码通过，本记录核对了现存文件大小及 SHA-256 与 [native-exports.json](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/review-evidence/native-exports.json) 一致。对应桌面证据为 [首次导出完成截图](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/review-evidence/jianying-export-completed.png) 与 [重开后再次导出截图](../../deliveries/quickcut-20261117/outputs/workbench/ac91f60c01ca/review-evidence/jianying-export-after-reopen.png)。原生 1080p / 44.1 kHz 是这次剪映导出设置，不应误写成 F 预览的 720p / 48 kHz。

## 独立真实语音缓存 fit 验收

在独立目录 [outputs/workbench-fit-acceptance-20261116](../../deliveries/quickcut-20261117/outputs/workbench-fit-acceptance-20261116) 中，使用旧任务真实 `voice-measured-r1.json`、原批准 StoryPlan 和真实音频缓存，完成 D → E → D 一轮适配。批准文本、测量时长、引用和哈希未伪造；只将验收副本末镜头的源区间从 35–40 秒缩为 35–37.427974 秒，以明确触发时长不足。

三句全部命中真实缓存；第三句实际执行一次 FFmpeg atempo，比例约 **1.08×**，完整 WAV 从 **2.607812 秒** 变为 **2.419062 秒**，保留 0.18 秒句尾停顿，在 **2.427974 秒** 请求窗口内被 D 接受。完整 WAV / 哈希和 F 四轨合同、fit receipt 校验通过。神经合成调用数为 0，torch 未导入；读取的主任务文件哈希前后不变。

证据见 [fit 报告](../../deliveries/quickcut-20261117/outputs/workbench-fit-acceptance-20261116/report.json)、[完整往返记录](../../deliveries/quickcut-20261117/outputs/workbench-fit-acceptance-20261116/project/fit-rounds.json) 和 [D/E 接口与复现说明](D-retiming.md)。该副本没有单独进行主观听审、MP4 预览渲染或剪映编辑器验收；其成功范围是实际缓存、实际变速、完整声音和协议闭环。

## 用户新男声参考任务：已完成独立 A–F 与原生导出

新任务 [1c1bb824f8a9](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9) 于 `2026-11-16T13:52:26.093507+00:00` 完成 r1，`job.json` 和运行报告均为 `completed`。使用用户最新 ZIP 中选定的 [男声.wav](../../materials/reference-audio/20261117/男声.wav)，独立计算 SHA-256 为：

```text
f36cfc54d0d9ea8c99d102639913754b83c9ee22eb8fcb5deeb6b63ef0ad55cb
```

该哈希同时匹配任务内 `reference/voice.wav`、E 包的 synthesis_profile 和每句声音 profile，且不同于旧任务参考。E 实际运行 IndexTTS2，执行日志退出码 0、用时约 115.97 秒。三句 `cache_hit` 均为 **false**，各自生成了不同于旧声音的 WAV，`tempo_ratio` 均为 1.0，没有以旧语音缓存代替本次指定男声。

| cue | 文案与旧任务比较 | 新男声完整 WAV 时长 | 旧参考时长 |
| --- | --- | ---: | ---: |
| cue-01 | 一致 | 2.529812 秒 | 4.633521 秒 |
| cue-02 | 一致 | 4.118562 秒 | 6.168625 秒 |
| cue-03 | 一致 | 1.725938 秒 | 2.607812 秒 |

实际 WAV 均为 mono PCM16 / 48 kHz，文件哈希与 E 声明相符；完整采样数和声明时长在一个采样容差内一致。文案和旁白全局开始时间仍为 0.2、10.2、25.2 秒，声音及字幕结束时间随新男声的真实时长变化。不同任务的 asset ID 不同，因此比对采用源视频实际内容哈希，而不是只比 ID：五段画面的源区间、成片区间、速度与职责，以及五段原声的源区间、成片位置与增益均与旧任务一致。它证明同一文案和剪辑安排换用了新参考声音，不表示新旧旁白波形或时长相同。

新任务也实际触发过旁白确认门禁：缺句末标点的草案请求返回 HTTP 422，任务保留 `needs_review`、`render_attempt: 0`，未启动 E；证据见 [narration-gate-check.json](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/review-evidence/narration-gate-check.json)。最终确认仍明确为 `assistant_review`。

独立 ffprobe 和完整 FFmpeg 解码确认新预览为 **30 秒、1280×720、30 fps / 900 帧、48 kHz 双声道**，解码退出码 0。浏览器实际播放器 `readyState=4`，读到 30 秒、1280×720，视频和工程下载链接均指向新任务。产物与可复核证据：

- [新男声 MP4](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/export-r1/preview.mp4)、[新工程 ZIP](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/QuickCut-project-r1.zip)、[运行报告](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/run-report.json)。
- [独立男声核对报告](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/review-evidence/male-run-verification.json)：参考哈希、逐句 PCM/哈希、与旧任务的归一化镜头比对、预览完整解码和输入未改写记录。
- [网页完成截图](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/review-evidence/workbench-male-completed.png)。

在 macOS 剪映专业版 6.1.0 / build 309 中，已实际打开新草稿 `QuickCut-1c1bb824f8a9-r1`，四轨在线、总长 30 秒，并完成原生导出。文件 [native-export-male.mov](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/review-evidence/native-export-male.mov) 为 **30 秒、1920×1080、H.264、30 fps / 900 帧、44.1 kHz 双声道 AAC**，全量解码退出码 0，SHA-256 已独立复核为：

```text
73da9bbc871496148bce9d81a5e2c6725df41cadb231c0b69276a39c56687292
```

原生导出检查记录见 [native-export-male.json](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/review-evidence/native-export-male.json)，界面证据见 [新版四轨截图](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/review-evidence/jianying-male-four-tracks.png) 和 [新版原生导出完成截图](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/review-evidence/jianying-male-export-completed.png)。**新版没有再次做字幕修改、保存退出重开或原生镜头替换。** 旧任务的两个原生 MOV、字幕编辑重开和独立缓存 fit 各保留原声音与原工程范围。

上述文件/采样核对未运行神经模型。另已完成并保留[新版独立 ASR 复核](../../deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/review-evidence/voice-content-check.json)：首句识别为“沿着临间小路走进清新的律义。”，存在同音、近音差异；第二句为准确的繁体转写，第三句准确。原始识别结果不覆盖批准字幕，也不记成全部精确识别通过。本轮未进行真人听审；完整解码、声音非静音、哈希一致及 ASR 结果不等于自然度或发音已获主观认可。逐句 PCM 峰值及近满刻度采样数保存在独立报告中，没有把检查结果写成“完全无削波”。

## 本机完整回归

[final-regression-20261116.log](../../deliveries/quickcut-20261117/outputs/workbench-runtime/final-regression-20261116.log) 记录完整 pytest **1158 passed in 30.87s**、退出码 **0**，环境为本机 Python **3.14.4**。日志说明覆盖当时全部工作区改动，基线 HEAD 为 `0d4c2337580b7377825aa336fc1e9f8508c48f7d`；这不是“该 HEAD 已包含全部新增实现”的提交声明，也不冒充远端 CI 结果。回归不运行神经推理，真实模型与原生编辑器证据按上文分别留档。

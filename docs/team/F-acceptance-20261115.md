# F 实机验收记录：2026-11-15

本记录单独保存实际桌面操作结果，不修改冻结 A/D/E 输入，也不把导出命令的 `ExportManifest.status: generated` 改为 `verified_import`。生成、安装、媒体回归和编辑器内验收是不同的证据。

## 环境与范围

| 项目 | 已知信息 |
| --- | --- |
| 系统 | macOS 26.6.2，系统 build `25G83`，arm64 |
| 编辑器 | 剪映专业版 6.1.0，`CFBundleVersion: 309`，bundle ID `com.lemon.lvpro` |
| 编辑保存重开的包 | v1 fixture 与 v1 真实 countryside 30 秒包 |
| 真实片编辑验收副本 | `QuickCut-Countryside-30s-验收` |
| 搬迁安装打开的包 | v2 countryside，修复原生对象 ID 命名域与完整旁白时长校验 |
| 保留给用户的干净草稿 | `QuickCut-Countryside-30s-正式预览` |
| 代码提交 | `a88e8ba4cdf364a3a0d7260004d44f8d90cbed81`（`a88e8ba`） |

下述桌面结果来自当前任务中 Codex 对剪映的实际操作与观察。系统版本通过只读 `sw_vers` 核对，剪映版本和 build 读取自 `/Applications/VideoFusion-macOS.app/Contents/Info.plist`。原始导出包与冻结输入保持原样，v1 编辑发生在安装后的验收副本；v2 正式预览保持原时间线。

## 已观察到的操作结果

| 用例 | 操作与结果 |
| --- | --- |
| v1 fixture 打开 | 剪映 6.1.0 实际打开，识别独立画面、原声、配音、文本轨道 |
| v1 fixture 编辑 | 修改字幕，保存、关闭并重新打开后，修改保留 |
| v1 countryside 打开 | 实际打开 30 秒草稿；三个不同原片对应独立视频剪辑，原声、配音和文本轨均识别 |
| v1 countryside 字幕 | 首字幕追加 `（编辑验收）`；保存关闭重开后保留 |
| v1 countryside 配音 | 删除首配音，替换为第三句的复制；保存关闭重开后保留 |
| v1 countryside 画面 | 将第三个画面片段剪切粘贴到 0 秒处的单独画面轨；保存关闭重开后保留 |
| v2 搬迁 | 完整包从 `outputs/quickcut-f-export-20261115-countryside-v2` 重命名迁移至 `outputs/quickcut-f-release-20261115/乡村工程`；只读复查确认旧路径已不存在 |
| v2 校验与重装 | 搬迁后 `verify_package()` 通过，19 个清单文件、9 个资源映射通过校验；安装为新草稿 `QuickCut-Countryside-30s-正式预览` |
| v2 原生打开 | 实际打开后的原生界面显示 30 秒、3 个画面片段、3 段原声、3 段旁白、3 个独立字幕；素材全部在线，无需手工重新链接 |
| v2 原生成片 | 在剪映导出窗口完成本地 MOV 导出，界面确认“导出完成”；FFmpeg 完整解码通过，实测 H.264 1920×1080、30 FPS、900 帧、30 秒，AAC 44.1 kHz 音轨；未向外部平台发布 |

v1 结果覆盖对象独立编辑和保存重开；v2 结果覆盖完整包搬迁、文件校验、重新安装与原生打开，未对保留给用户的干净 v2 工程重复编辑。两组范围分别记录，不将 v1 的编辑保存结果自动继承给 v2。

## v2 交付包与冻结输入审计

以下路径相对于仓库根，交付包保存在本机 `outputs/`，不随 Git 提交。包目录为 `outputs/quickcut-f-release-20261115/乡村工程`，导出文档 ID 为 `export-DF58D17F-AD88-4D19-9D9E-FFA390A0429D`，`fixtures_only: false`，状态仍是 `generated`。安装草稿的 `quickcut-provenance.json` 引用了相同导出文档 ID。

| 冻结文档 | document_id | revision |
| --- | --- | --- |
| EditTimeline | `timeline-countryside` | 1 |
| AssetManifest | `manifest-1aebcf579acf476087b5110924b0862e` | 1 |
| VoicePackage | `voice-story-countryside` | 1 |

包内三份冻结文档的 JSON 内容与当前 `ABCDE` 对应文档一致。以下 SHA-256 针对**文件原始字节**计算；`export-manifest.json` 的 `input_hashes` 使用规范化 JSON 哈希，算法输入不同，不能与本表混用。

| 包内文件 | SHA-256 |
| --- | --- |
| `export-manifest.json` | `009519ea54996b4eea81241143e10a186cd61dd7e377cf13b3724a8771ec4d0a` |
| `inputs/timeline.json` | `ca1aaa9b92142e640597f3a1f1b03342cbff8cb66dbe44f54ebd73688676e3b7` |
| `inputs/asset-manifest.json` | `bf032a88926811ddbf8216492e3f9309c3d7d5ecd5f9e820acf736dffe219490` |
| `inputs/voice-package.json` | `360d8f945169a5540077678ea44aae904b54e71b84e6815939c6cb16269d9b30` |

本轮代码回归为 **902 passed in 29.29s**；wheel 构建产物核对包含 4 个模板资源；74 个 Python 文件通过 Python 3.10 语法检查。语法检查不代表在 Python 3.10 上执行过全部测试，云端 CI 状态另行记录。

剪映原生成片为 `outputs/quickcut-f-release-20261115/QuickCut-Countryside-30s-正式预览.mov`（45,477,492 字节），SHA-256 为 `a6367e299dca0e78b9006e83cf0f54af05082bada9b54130180c369e10f52616`；机器核验保存在同目录 `native-export-check.json`。1080p 与 44.1 kHz 为本次剪映导出窗口设置；它不改变冻结 Timeline 的 720p / 48 kHz，也不代表与 FFmpeg 预览逐像素一致。

## 尚待补齐的证据

- 将本轮原生界面截图或录屏整理为持久证据索引，并补齐 v1 包的路径与哈希。
- 按 [验收手册 7.6](ACCEPTANCE_RUNBOOK.md#76-f媒体执行与可编辑工程实机) 补录未在上表观察到的项目，例如画面入点与音量修改、完整人工播放听感检查；完整解码检查不替代人工内容审阅。

只有具体包、具体版本和具体操作有对应证据时，才能在验收记录中更新该项状态。安装器返回的 `editor_import_verified: false` 和导出清单的 `not_run` 保持对命令自身行为的准确描述。

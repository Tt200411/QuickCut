# F：剪映工程导出与迁移安装

F 消费同一冻结版本的 `EditTimeline`、A `AssetManifest` 和 E `VoicePackage`，生成媒体自包含的剪映工程包。当前适配器是 `quickcut-jianying-mac-0.1.0`，目标为 **macOS 剪映专业版 6.1.0**。入口是 Python API 和两个 CLI 脚本；没有新增 HTTP 导出接口。

## 当前能力与边界

| Timeline 轨道 | 生成的剪映内容 |
| --- | --- |
| `video` | 一个画面轨，保留原片与源入出点，1×速度、硬切；画面对象本身静音 |
| `source_audio` | 独立原声轨，按源区间提取 PCM WAV，保留全局位置与增益 |
| `narration` | 独立旁白轨，复制 E 的完整 WAV，保留全局位置与增益 |
| `subtitle` | 独立可编辑文字对象，保留字幕文本与微秒时间区间；另交付全局 SRT |

工程输入必须保持 `editable_export_burn_subtitles: false`、`retain_separate_audio_tracks: true` 和 `unsupported_effect_policy: "error"`。可选 MP4 预览按 `preview_burn_subtitles` 烧录中文字幕；这一设置不改变工程的独立文字轨。

首版拒绝背景音乐/音效轨、转场、滤镜/效果、动画、变速、音量包络、自动压低原声、淡入淡出、重叠画面及视频空隙。带非零旋转元数据的源视频也拒绝导出，应先由 A 规范方向、更新素材版本，再重新冻结 D/E 引用。未支持功能会明确报错，不会静默删除或扁平化。其他剪映版本、Windows 剪映及 CapCut 不在此适配器的兼容承诺内。

## 输入与生产门禁

三份输入均为 `0.1.0`，必须同项目、引用精确匹配，Timeline 为 `ready_for_export` 且无错误/未解决缺口，VoicePackage 为 `ready`，旁白已经适配预算。`--manifest` 使用 A 清单，不使用 A+E 的 `assets-union.json`；导出器自己合并并校验 A/E 资产。

默认拒绝 `fixtures_only: true` 的输入。导出、校验和安装各自要求显式 `allow_fixtures=True` 或 `--allow-fixtures` 才能处理测试包，并保留 fixture 标记。测试包可验证工程行为，不可据此声明真实检索、语音质量或生产验收通过。

源媒体 URI 相对于 `project_root`。绝对 URI、越界路径及指向项目外的符号链接被拒绝。`ABCDE/f-inputs.json` 与 `ABCDE/run-report.json` 的 `f_inputs` 中，`project_root: "."` 配合 `project_root_relative_to: "document_directory"` 表示交接文档所在目录；读取方不能把它误解为任意启动目录。从仓库根运行下述命令时，项目根就是 `ABCDE`。

需要 Python 3.10+、FFmpeg 与 ffprobe，可使用 `QUICKCUT_FFMPEG` / `QUICKCUT_FFPROBE` 指定路径。媒体资源先按 [Git LFS 说明](git-lfs.md)下载到本地，LFS 指针文件不构成有效视频。建议安装 `pip install -e '.[dev,ingest,runtime]'`。

导出会核对真实 SHA-256、完整解码、流起点与源区间覆盖。旁白必须是可读取的 PCM WAV，**包括尾部停顿在内的完整采样时长**须与 E 的声明一致，容差为一个采样；不会为了凑声明而截断旁白或补静音。独立原声提取也按完整目标采样数校验。包内资源复制后再次核对哈希，安装前重新检查文件大小、哈希、冻结输入和资源映射。

## CLI：导出、校验与安装

所有命令从仓库根运行。输出父目录必须存在，输出包目录必须尚不存在。

```bash
mkdir -p outputs
python scripts/export_project.py \
  --timeline ABCDE/timeline.json \
  --manifest ABCDE/asset-manifest.json \
  --voice-package ABCDE/voice-package.json \
  --project-root ABCDE \
  --output outputs/countryside-f-new \
  --target-version 6.1.0 --name countryside --preview
```

`--preview` 可省略。成功退出 0，并向 stdout 写 `ExportManifest`；失败退出 1，stderr 返回包含原因的 JSON。输出包结构如下：

```text
countryside-f-new/
  export-manifest.json       # 相对资源映射、输入 refs/hash、能力与校验状态
  inputs/                   # 冻结 Timeline、AssetManifest、VoicePackage 副本
  draft/
    draft_info.json         # 剪映原生四轨草稿
    draft_meta_info.json
    draft_cover.jpg
    Resources/              # 被引用原片、完整旁白与独立原声 PCM
  captions.srt              # 全局独立字幕
  preview.mp4               # 仅 --preview 时生成
  preview.srt
  preview.preview.json
```

预览烧字需要 libass 和中文字体。默认优先使用系统 Noto CJK、macOS Hiragino Sans GB 或 Windows 微软雅黑等已知字体；也可同时设置 `QUICKCUT_SUBTITLE_FONT` 与 `QUICKCUT_SUBTITLE_FONT_NAME`。若自动选择的系统 FFmpeg 缺 libass，可使用已安装的 imageio-ffmpeg；显式 `QUICKCUT_FFMPEG` 不会被静默替换。缺少所需能力会拒绝生成。烧字按全局帧边界显示；独立 SRT 按毫秒保存，工程对象保留微秒区间。

完全退出剪映后执行安装：

```bash
python scripts/install_jianying_draft.py outputs/countryside-f-new
```

安装器默认使用 `~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft`。可用 `--draft-root PATH` 指定实际草稿根；`--name NAME` 指定新草稿名称，安装器会加 `QuickCut-` 前缀。成功返回 `status: "installed"`、实际目录和注册表备份位置。安装只新增草稿；同名目录或同名注册表项存在时拒绝覆盖。安装前检查剪映已退出，使用独占安装锁，保留已有项目记录，并在更新注册表前备份；注册表写入失败会回滚本次新增草稿。

迁移时复制**整个导出包**到新目录或目标机器，在剪映关闭时重新运行安装器。安装器依据包内相对 URI 校验资源并重建目标机器的原生绝对路径；不要直接打开迁移前的 `draft_info.json`。安装会创建独立资源副本，不依赖原作者的项目路径。已在剪映内修改的安装草稿与冻结导出包是两份内容：迁移原导出包会重建冻结版本，不会携带安装后的手工修改；这类修改应通过剪映自己的项目备份另行保留。

## Python API

```python
import json
from pathlib import Path
from quickcut.export import export_project, verify_package, install_jianying_draft

root = Path("ABCDE")
read = lambda name: json.loads((root / name).read_text(encoding="utf-8"))
package = Path("outputs/countryside-f-api-new")
report = export_project(
    read("timeline.json"), read("asset-manifest.json"), read("voice-package.json"),
    root, package, preview=True, target_version="6.1.0", name="countryside",
)
manifest, frozen_timeline, resources = verify_package(package)
# 实际安装须在剪映完全退出后执行。
installed = install_jianying_draft(package)
```

API 不改写输入对象；导出失败不发布部分工程包。`verify_package()` 只校验文件和协议，不操作编辑器。捕获 `ExportError` 后用 `to_dict()` 获取错误信封。常见错误有 `FIXTURE_EXPORT_REJECTED`、`TIMELINE_INVALID`、`MEDIA_HASH_MISMATCH`、`NARRATION_DURATION_MISMATCH`、`EDITOR_FEATURE_UNSUPPORTED`、`EDITOR_VERSION_UNSUPPORTED`、`OUTPUT_EXISTS` 和 `EDITOR_RUNNING`。

## 验收状态与证据

代码生成成功始终记录 `status: "generated"`，其 `validation.editor_import` 与 `validation.editor_edit_save_reopen` 为 `not_run`。安装成功仍返回 `editor_import_verified: false`。这些值描述命令实际执行的检查，不会因为有一份可解析 JSON 或一段 MP4 而自动变为 `verified_import`。

截至 **2026-11-15**，macOS 剪映专业版 6.1.0 已实际打开 fixture 与真实 30 秒 countryside 草稿，观察到独立画面、原声、旁白和文本轨道。fixture 修改字幕后保存重开，文字仍保留。真实片验收副本 `QuickCut-Countryside-30s-验收` 完成了首字幕加字、首旁白替换为第三句复制、第三画面剪切到 0 秒的单独画面轨，保存退出重开后，三项修改均保留；原导出包与冻结输入未改。

上述编辑保存操作针对 **v1 包**。其后的 v2 修复原生对象 ID 命名域和完整旁白时长校验，已完整搬迁至 `outputs/quickcut-f-release-20261115/乡村工程`，确认旧路径不存在后通过 `verify_package()`，重新安装为 `QuickCut-Countryside-30s-正式预览`。原生界面已显示 30 秒、3 个画面片段、3 段原声、3 段旁白和 3 个独立字幕，素材全部在线，无需重新链接；该干净草稿保留原时间线供用户查看。

本机精确环境为 macOS **26.6.2 / build 25G83 / arm64**，剪映 **6.1.0 / build 309**；对应代码提交 `a88e8ba`。v1 的编辑保存重开与 v2 的搬迁安装打开分别记载，v2 未重复做修改验收。文件 SHA-256、冻结 refs 和回归结果见[独立验收记录](F-acceptance-20261115.md)。这些实机结果不改写命令生成的 `generated` 清单。

完整验收按 [验收手册 7.6](ACCEPTANCE_RUNBOOK.md#76-f媒体执行与可编辑工程实机) 记录：精确编辑器版本/build、包与输入哈希、打开时媒体在线、各轨对象可独立编辑、画面入点/字幕/声音及增益修改、保存退出重开、复制包后的重新安装、编辑器导出播放，以及对应截图或录屏与验证人。自动化回归和原生桌面操作证据分别保存；旧的 2026-11-13 预览及未烧字结果是历史记录，不替代本轮 F 验收。

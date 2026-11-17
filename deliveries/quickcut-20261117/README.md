# QuickCut 完整交付 · 2026-11-17

本目录是用户授权上传的项目快照，包含输入、生成结果、运行证据和五份 QuickCut 原生草稿备份。对应已验证的生产代码为 `e6cb16870e5fbb249040ab098998432a39d4888b`；本次发布另将默认参考音频路径改为仓库内共享目录。

## 最新男声版本

- [30 秒 MP4](outputs/workbench/1c1bb824f8a9/export-r1/preview.mp4)
- [可迁移剪映工程 ZIP](outputs/workbench/1c1bb824f8a9/QuickCut-project-r1.zip)
- [剪映原生 1080p 导出 MOV](outputs/workbench/1c1bb824f8a9/review-evidence/native-export-male.mov)
- [逐句参考哈希和成片检查](outputs/workbench/1c1bb824f8a9/review-evidence/male-run-verification.json)
- [参考音频和原始 ZIP](../../materials/reference-audio/20261117/README.md)
- [整体验收说明](../../docs/team/WORKBENCH-acceptance-20261116.md)

新男声任务使用真实 A/B/C/E 模型，方案经助手复核；三句声音均首次合成。完成 30 秒预览和原生剪映导出。本地 1158 项回归及该代码提交的两项云端工作流通过，证据见 [CI 记录](outputs/workbench-runtime/cloud-ci-e6cb168.json)。声音自然度未做真人听审。

## 下载与使用

媒体通过 Git LFS 保存。不要只下载 GitHub 的源码 ZIP 后把指针当作视频。

```sh
git lfs install
git clone https://github.com/Tt200411/QuickCut.git
cd QuickCut
git lfs pull
```

已克隆的仓库先同步分支，再运行 `git lfs pull`。从仓库根安装最新男声工程（先保存并完全退出剪映）：

```sh
python scripts/install_jianying_draft.py \
  deliveries/quickcut-20261117/outputs/workbench/1c1bb824f8a9/export-r1 \
  --name shared-male-20261117
```

安装器会检查完整媒体并重建目标机器上的路径。适配范围为 macOS 剪映专业版 6.1.0 / build 309；详见 [F 安装与迁移说明](../../docs/team/F-export.md)。重新运行生产流程按[工作台说明](../../docs/team/WORKBENCH.md)部署模型；两份归档 deployment manifest 是原机器的记录，不能直接作为新机器的运行配置。

## 快照范围

| 路径 | 内容 |
| --- | --- |
| `inputs/` | 当时的项目输入，含旧参考音频和新 ZIP 解出的三段参考 |
| `outputs/workbench/` | 新旧两个完整任务，含素材、原始模型响应、草案、确认、修订、真实旁白、成片、工程和截图 |
| `outputs/quickcut-f-*` | F 历史工程、原生导出和验收；名称含 abcde 的旧目录实际为 10 秒 fixture，以其报告标记为准 |
| `outputs/indextts2-acceptance-20261115/` | IndexTTS 单模块及乡村链路历史验收 |
| `outputs/workbench-fit-acceptance-20261116/` | 真实缓存声音的 D/E 适配复现，包括复现必需的 WAV/JSON 缓存输入 |
| `outputs/` 其余目录 | 历史演示、失败/负例、集成测试、审查和运行记录；保留原有成功或失败状态 |
| 两个模型 runtime 目录 | 仅顶层部署清单、脚本和日志等记录，不含权重、上游源码或虚拟环境 |
| `native-drafts/` | 本任务五份安装后的原生草稿快照，包含此前手工修改及保存备份；不含剪映全局配置或其他用户项目 |

`native-drafts/` 用于保全实际编辑结果。它们保留了原机器的绝对路径，不是已验证的跨机器安装包；在新机器使用上面的自包含导出包和安装器。原导出包不会自动携带安装后才发生的手工修改。

原始证据不改写，部分 JSON/日志保留原工作目录、当时日期和绝对路径。归档是只读复核材料，不应直接复制回活动工作台继续执行。`manifest.json` 逐文件记录来源、大小和 SHA-256。模型权重按用户选择通过配置下载；下载缓存、虚拟环境、锁、系统凭证和剪映全局偏好不在交付范围。

历史文档提到的仓库外 `../outputs/QuickCut-engineering-final-20261113/` 当前本机不存在，未能补入此次快照；已有历史记录没有被新版结果替换。

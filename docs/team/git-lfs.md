# 团队视频素材：Git LFS 与 fork / PR

共享视频放在 `materials/` 下，使用小写扩展名 `.mp4`、`.mov`、`.mkv` 或 `.webm`。仓库的 `.gitattributes` 会把这些文件交给 Git LFS；已冻结的团队交接样例 `ABCDE/media/**/*.mp4` 和 `ABCDE/derived/**/proxy.mp4` 也使用 LFS。源码、JSON 清单、来源/授权说明、哈希和预览图仍由普通 Git 保存。`inputs/`、`outputs/`、`cache/`、`tmp/` 继续用于本地数据，不上传。

## 首次上传为什么会被拒绝

GitHub 对公开 fork 的规则是：**整个仓库网络已存在 LFS 对象，或上传者对根仓库有写权限，满足任一项即可上传 LFS 对象。** 仅安装 Git LFS、增加 `.gitattributes` 或提交指针，并不代表远端已经有对象。

本仓库用 `materials/lfs-smoke.mp4` 作为首次 LFS 对象和下载检查样例。它是 FFmpeg 生成的 12 秒、320×180、25 FPS、无音轨颜色视频，没有第三方素材。维护者先上传该对象后，组员可以继续使用自己的 fork 和现有 PR。无需为此改变团队成员权限。

依据：[GitHub 的 LFS 协作与公开 fork 规则](https://docs.github.com/en/repositories/working-with-files/managing-large-files/collaboration-with-git-large-file-storage)。fork 的 LFS 存储与下载流量计入根仓库所有者的额度。

## 组员：同步配置后上传

先安装 [Git LFS](https://git-lfs.com/)，然后在自己的仓库目录执行。Windows 可使用 Git Bash；下面的 Git 命令也可在 PowerShell 执行。

```bash
git lfs version
git lfs install --local
git status
git remote -v
```

确认 `origin` 指向**自己的 fork**，`upstream` 指向 `Tt200411/QuickCut`。若还没有 `upstream`，执行一次：

```bash
git remote add upstream https://github.com/Tt200411/QuickCut.git
```

先提交或妥善保存未提交的改动，切换到用于 PR 的分支，再同步团队配置：

```bash
git fetch upstream
git merge upstream/main
git lfs pull upstream
```

把待共享素材放入 `materials/<素材包>/`，一并保留来源、授权、用途与 SHA-256。以 PR #7 的目录为例：

```bash
git add materials/A-countryside
# 已经作为普通 Git 文件跟踪的视频，需要重新应用 LFS 规则。
git add --renormalize materials/A-countryside
git lfs status
git lfs ls-files
git diff --cached --stat
```

确认视频出现在 `git lfs ls-files` 中，然后提交并推送**当前 PR 分支**：

```bash
git commit -m "Store shared video materials with Git LFS"
git push origin HEAD
```

正常的 `git push` 会通过 LFS 钩子先上传真实内容，再上传 Git 提交。已有 PR 会自动更新；不需要新建 PR。若没有暂存改动，跳过 `git commit`。

2026-11-15 整合时，PR #7 的八个共享视频及 `ABCDE` 的六份原片已转换为 LFS，六份缺失的代理也按冻结清单恢复并纳入 LFS；代理 SHA-256 与原登记值逐一一致。**旧提交里的普通视频仍保留在 Git 历史中**。这不会自动恢复此前压缩过的 4K 原文件；如需替换，提交新素材时也应更新清单、哈希和相关验证记录。不要为了此次上传修复对共享分支执行 `git lfs migrate --everything` 或强制推送。

## 下载与检查

新成员先安装 Git LFS，再克隆仓库：

```bash
git lfs install
git clone https://github.com/Tt200411/QuickCut.git
cd QuickCut
git lfs pull
git lfs fsck
```

已有克隆在同步分支后执行 `git lfs install --local` 和 `git lfs pull` 即可。需要查看尚未合并的素材时，先切换到对应 PR 分支，再拉取它的 LFS 对象。不要用是否能在 GitHub 网页播放视频判断上传成败；PR 中显示 LFS 指针是正常现象。

可用以下命令检查下载样例是否为真实视频：

```bash
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 materials/lfs-smoke.mp4
ffmpeg -v error -i materials/lfs-smoke.mp4 -f null -
```

第一条应显示 12 秒，第二条应成功退出且无解码错误。如果文件内容只是以 `version https://git-lfs.github.com/spec/v1` 开头的三行文本，实际内容还没有下载成功。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 公开 fork 无法上传首个 LFS 对象 | 先由根仓库维护者上传样例对象，再重试组员的上传；单独提交 `.gitattributes` 不够。 |
| 之前提交了指针，但对象上传失败 | 确认本地保有真实文件，执行下方补传命令；它仅补传当前分支可达的对象。 |
| `git add` 提示视频被忽略 | 同步主仓库的 `.gitignore`，把共享文件放到 `materials/`，使用上述小写扩展名。不要把整个 `inputs/` 或 `outputs/` 强行加入。 |
| GitHub 拒绝普通 Git 的大文件 | 先检查 `git lfs ls-files` 是否列出文件；新增规则不会自动转换旧提交。若超限文件已存在于尚未推送的提交中，仅追加一个转换提交仍会被拒绝，需要先整理这些本地提交。 |
| `missing object` / 本地对象丢失 | 从有完整素材的原机器或可用远端恢复真实文件，再上传；只有指针无法重建视频。 |
| `403`、认证失败或额度不足 | 检查 `origin` 是否为自己的 fork、当前账号认证和根仓库 LFS 额度；保留完整报错供维护者排查。 |

补传当前分支：

```bash
git lfs install --local
git lfs push --all origin HEAD
git push origin HEAD
```

`HEAD` 限定当前分支可达的提交，避免无意补传其他本地分支的全部素材。参见 [GitHub 的 LFS 上传故障处理](https://docs.github.com/en/repositories/working-with-files/managing-large-files/resolving-git-large-file-storage-upload-failures)。

## 维护者：样例与远端验证

样例生成命令如下（需要 FFmpeg，已有同名文件时会先提示）：

```bash
ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=skyblue:s=320x180:r=25:d=4 -f lavfi -i color=c=darkgreen:s=320x180:r=25:d=4 -f lavfi -i color=c=orange:s=320x180:r=25:d=4 -filter_complex "[0:v][1:v][2:v]concat=n=3:v=1:a=0[outv]" -map "[outv]" -c:v libx264 -pix_fmt yuv420p -movflags +faststart materials/lfs-smoke.mp4
```

本地 `git lfs fsck` 只能验证本地对象。确认上传成功需要用**全新目录和独立 LFS 缓存**克隆远端，拉取样例后比较 SHA-256 并完整解码。维护者完成这个检查后，组员再重试自己的素材上传；该检查本身不代表组员的素材已经上传。

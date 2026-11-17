# 参考音频

用户提供 `ref_audio.zip`，并指定本轮使用 `男声.wav`。2026-11-17 用户要求将完整项目交付物上传到本仓库，范围包括参考音频；本目录保留原始 ZIP、三段解压后的 WAV 及来源清单。

默认声音配置为仓库根的 `examples/voice_package/indextts2.config.json`。克隆后先运行 `git lfs pull`；只有 LFS 指针文本不能作为参考 WAV 使用。

| 文件 | 采样格式 | 时长 |
| --- | --- | ---: |
| 男声.wav | 24 kHz、单声道 PCM16 | 3.320000 秒 |
| 魅力游移型女1.wav | 24 kHz、单声道 PCM16 | 2.536875 秒 |
| 老人女性3.wav | 24 kHz、单声道 PCM16 | 4.696875 秒 |

男声 SHA-256：`f36cfc54d0d9ea8c99d102639913754b83c9ee22eb8fcb5deeb6b63ef0ad55cb`。原始来源和全部音频参数见 `source-manifest.json`，共享文件的大小与哈希见 `files.json`。

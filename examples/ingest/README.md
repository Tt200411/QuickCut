# 批量导入样例

`sources.json` 提供六个输入条目，不附带视频。将素材放入仓库 `inputs/`，修改路径、来源和 `provenance_note` 后运行：

```bash
python -m quickcut.ingest --sources examples/ingest/sources.json --project-root outputs/my-project
```

输入路径相对于 JSON 文件目录，输出 URI 相对于项目根目录。`source_key` 用于识别同一素材的后续版本，不同素材应使用不同 key。

没有素材时可运行 `python -m scripts.smoke_ingest`。脚本在 `outputs/ingest-smoke/inputs` 生成三个短片、一个 65 秒视频和两个空镜测试条目，结果写入 `outputs/ingest-smoke/project`。测试内容为图案、音调和静音，来源角色用于测试，不验证真实语音或画面语义。

`outputs/` 已被 Git 忽略。生成的视频、清单和缓存无需提交。字段与缓存说明见 [接入模块说明](../../docs/ingestion.md)。

# C 模块样例与证据边界

C 是「故事与视听规划」。这里放的是离线夹具、可复现命令和真实运行输出，供 B、D、E 直接取用。所有文档 `fixtures_only: true`，`media/...` 与 `keyframes/...` 沿用黄金样例的占位路径，仓库没有附带媒体文件。候选分数是人工填写的夹具值，不是 CLIP 实测，也不能跨模型比较。

## 文件

| 文件 | 来源 | 用途 |
|---|---|---|
| `candidate_set.json` | 人工整理 | 六条检索请求对应的结果，覆盖主内容、环境、转场和情绪；独立转场结果显式为空 |
| `approved_cues.json` | 人工审阅 | 三句已批准中文旁白，与黄金 StoryPlan 文案一致 |
| `fixed_model_response.json` | 人工固定 | 接入本地 Qwen 前测试校验器用的响应夹具 |
| `outline.json` | 本次运行 | Brief 拆出的三段初始结构，愿望与事实分开 |
| `retrieval_request.json` | 本次运行 | C 交给 B 的检索请求 |
| `story_plan.json` | 本次运行 | 完整故事板，交给 D 与 E |
| `alignment.json` | 本次运行 | 二次对齐结果，含缺口与下一轮请求 |
| `run.json` | 本次运行 | 原始响应、校验结果、人工修改、规则修正、版本号 |
| `comparison.json` | 本次运行 | 完整方案与素材不足方案的差异 |
| `fixed/` | 本次运行 | 固定响应模式的一套输出 |
| `insufficient/` | 本次运行 | 结尾没有主镜头的失败案例 |

## 复现

从仓库根目录运行，Python 3.10 以上。规则和固定响应入口仅依赖标准库；图片边界测试和 Ollama 图像处理需要 Pillow。

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev,runtime,server]' 'pillow>=10.0'
.venv/bin/python -m quickcut.planning run --mode rule --out-dir examples/team_contracts/c
.venv/bin/python -m quickcut.planning run --mode fixed --out-dir examples/team_contracts/c/fixed
.venv/bin/python -m quickcut.planning run --case insufficient --out-dir examples/team_contracts/c/insufficient
.venv/bin/python -m quickcut.planning selftest
.venv/bin/python -m quickcut.planning inject --output outputs/c/injected_bundle.json
.venv/bin/python scripts/review_c_planning.py
.venv/bin/python examples/team_contracts/validate.py --self-test
```

`inject` 会把 C 重新生成的 StoryPlan 替换进八文档 bundle，再调用仓库自带校验器和五个负例。这一步证明 C 的输出能被 D、E、F 的既有夹具接受，不代表真实媒体或剪映导入已验证。

三种选镜模式：

| 模式 | 是否需要模型 | 记录方式 |
|---|---|---|
| `rule`（默认） | 不需要 | `decision_provenance.mode = "rule"`，不冒充模型输出 |
| `fixed` | 不需要 | 读取固定响应，`mode = "fixed_response"`，保留 `raw_response_count` |
| `ollama` | 需要本地 Ollama 与 `qwen2.5vl:3b` | `mode = "ollama"`，记录端点、模型与原始响应 |

环境变量 `QUICKCUT_VLM_MODEL`、`QUICKCUT_OLLAMA_URL`、`QUICKCUT_VLM_TIMEOUT` 控制 Ollama 模式。本机没有运行 Ollama 服务，所以 `ollama` 模式这次没有实机跑过，只提供了代码路径和失败信封。

## 不能当作证据的东西

`approved_cues.json` 是人工文案。生成的 StoryPlan 会写 `text_source: "human_editorial"`，并在 `decision_provenance.human_edits` 留痕，不会把它记成「Qwen 自动写出来的」。`fixed_model_response.json` 同理，它是一次夹具，不是推理日志。

`candidate_set.json` 里的 `media/` 路径是占位，`fixture:` 前缀的哈希不是真实内容摘要。模板生成的旁白草稿一律带 `content_review: "pending_human_review"`，自动格式校验通过不等于旁白与画面角色、动作、先后顺序已经核对过。素材不足案例的 `status` 是 `needs_input`，D 不应拿它做最终导出。

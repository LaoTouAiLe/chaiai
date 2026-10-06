# 同题真实缓存对照与修复（2026-10-02）

这次实际运行了本项目的 Web HTTP 入口和本地 DSH 的 SDK/CLI 入口，先测量，再修改，再用新库复测。
修改已保留在当前工作区。缓存指标在本组三轮任务中达到 90% 以上；这不是完整 T43 或 v0.4 验收通过。

## 问题、环境与统计口径

两个项目连续询问以下三个问题，使用同一份冻结源码文件树：

1. 请你查看我们这个项目的架构
2. 请进一步说明一个用户消息从 HTTP 接口进入，到工具执行、审批和最终回答返回的完整链路。只读取源码和文档，给出关键文件与准确行号。
3. 请结合刚才核实的源码，总结哪些内容会进入下一次模型请求，哪些状态只保存在运行时；说明当前结构对缓存复用有什么影响。只读，不修改文件。

模型均为 `deepseek-flash`，上游均为 `https://api.deepseek.com`，输出上限均为 4096。
本项目窗口为 1,048,576 token，长期记忆开启。各组使用绝对路径的独立会话/记忆库。
工具集合与系统提示保留各自真实实现；DSH 没有传 temperature，本项目为 0.2。
没有为了匹配工具数、关闭记忆或增加读取而改变产品行为。

DSH 检出：`D:/ideaTest2/deepseek-harness`，提交 `b150a551b8`。
冻结文件树：`local-agent-web/eval/paired-live-20261002/workspace`，265 个文件的 SHA-256 清单见 `fixture-manifest.json`。
冻结的是开工时工作区真实内容，包含此前未提交改动；没有把这些内容重置为 Git HEAD。

命中率 = 实际 cache read /（实际 cache read + 实际未命中输入）。
合计先累加 token 再计算，不能平均三问百分比。DSH SDK 的 `inputTokens` 已经是未命中输入，
不能再减一次 `cacheReadTokens`。DSH 标题生成的 114 个未命中输入也计入下表。
本项目包括独立任务记忆提取，能力探测在用户提交前单独归档。

## 真实结果

| 项目 | 第一问 | 第二问 | 第三问 | 加权合计 | 未命中输入 | 缓存读取 |
|---|---:|---:|---:|---:|---:|---:|
| 修改前本项目 | 75.0% | 81.8% | 59.1% | **69.6%** | 1,210,588 | 2,768,000 |
| DSH | 84.0% | 97.5% | 97.5% | **95.5%** | 130,779 | 2,768,640 |
| 修复后本项目 | 88.7% | 96.2% | 97.6% | **96.0%** | 92,652 | 2,225,792 |

本次修复后未命中输入比本次基线少 92.35%，调用记录从 67 降到 35。
这也包含不同模型输出轨迹、重复读取减少等影响，不是纯缓存命中变化的受控因果估计。

| 项目 | 完成与质量边界 |
|---|---|
| 修改前本项目 | 三问终态显示 completed，回答均 finish_reason=stop；前两问任务记忆审核失败，第三问有工具失败，不能写成无错误通过。 |
| DSH | 第一问 completed；后两问明确为 max-tokens，回答截断。高命中率不代表输出质量验收通过。 |
| 修复后本项目 | 三问 completed，回答均 finish_reason=stop，错误列表为空；三轮长期记忆均 written。 |

原始透明代理用量与两边应用用量逐题校核一致。首次短任务仍为 88.7%，不保证冷缓存或任意任务达到 90%。
顺序运行共享供应商缓存温度，且模型轨迹不同；不能将这一组结果外推为稳定收益保证。

## 为什么原来只有约 70%

1. **跨轮删除旧快照。** 原组装器只注入当前轮快照，下一轮把旧快照从消息前部拿掉。
   实际请求由第一轮到第二轮，旧请求 24 条消息仅最前 1 条保持一致。新组装器按用户消息 ID
   保留窗口内每轮的冻结快照，新轮仅追加；复测两次续问分别保留了完整的 32/32、73/73 条旧消息。
   见 `nodes._with_snapshot`、`prefix-proof.json`。
2. **工具历史过早省略。** 160k 字符工具步预算在 1M 模型窗口远未满时就移动历史中段，已有前缀重新计费。
   新循环先按系统提示、全部快照/摘要、消息、工具定义估算模型空间，接近压缩阈值才启用原整步裁剪。
   消息窗口、已摘要边界、单条大输出压缩及原始审计历史均保留。见 `nodes._prompt_window`、`_snapshot_tokens`、
   `short_term.select_prompt_window`；回归同时验证充裕窗口与小窗口下的安全兜底。
3. **记忆阶段替换工具定义。** 原生结构化 schema 包装与主调用工具列表不同，记忆调用又重新计费历史。
   现在使用同样的工具绑定、尾部 JSON 指令和原 `TaskMemoryDecision` 校验。
   原候选审核与写入规则保留；记忆响应若请求工具，只拒绝并按原有限次数重试，绝不执行。
   见 `nodes.task_memory_node`、`reflector.extract_task_memory`、`protocol.call_structured`。

第一次候选复测暴露了流式协议缺陷：框架丢掉 DeepSeek 的 `reasoning_content`，沿用同一段工具历史的
记忆请求被 HTTP 400 拒绝。失败版本在 `variant` 单独保存，不作为成功结果。
`ChatOpenAIWithReasoning` 现在保留实际流式 delta 字段，消息聚合、Checkpoint 和下一次请求均原样往返；
普通回答与工具调用都覆盖，空串保留，缺失字段不伪造，用户正文不展示隐藏思考。
修复后的有效复测使用全新 `variant2` 库。

## 修改文件

- `local-agent-web/src/local_agent/graph/nodes.py`：历史快照重建、窗口压力判断、完整预算和共享工具的记忆提取。
- `local-agent-web/src/local_agent/memory/short_term.py`：允许新循环在空间充裕时保留工具步骤，默认旧行为兼容。
- `local-agent-web/src/local_agent/reflection/reflector.py`：记忆提取复用工具绑定并以 JSON 尾部指令校验。
- `local-agent-web/src/local_agent/llm/protocol.py`：支持绑定模型上的 JSON 校验，记忆工具请求不执行。
- `local-agent-web/src/local_agent/llm/compat.py`：保留流式思考扩展字段并原样回传。
- `local-agent-web/tests/test_cache_prefix_regressions.py`：跨轮字节前缀、模型窗口压力和记忆工具拒绝回归。
- `local-agent-web/tests/test_llm_compat.py`：流式字段聚合、空串、缺失字段与记忆接续请求回归。
- `local-agent-web/tests/conftest.py`、`tests/test_t43_main_loop.py`：假模型按实际 JSON 记忆请求响应。
- `local-agent-web/tests/test_runtime_snapshot.py`、`tests/test_graph_resume.py`：区分当前/历史快照及真实用户消息/注入数据块，保留原断言。
- `local-agent-web/scripts/paired_cache_relay.py`、`paired_cache_http_client.py`、`paired_cache_dsh_client.mjs`、
  `paired_cache_report.py`：实际双项目调用、供应商原始用量记录与逐题校核。
- `ARCHITECTURE.md`：同步快照、窗口、记忆及供应商字段契约。
- `V0_4_DEVELOPMENT_PLAN.md` 与本文：保存测量事实、失败候选和验收边界。

## 验证与复测

已完成：离线全量 **955 passed / 7 skipped**；真实精度层 **7 passed**（含四个真实任务）；
Ruff 通过、Mypy 的 95 个源码文件通过。新增供应商字段专项与原恢复测试共 80 passed。
最后仅同步注释、文档和报告脚本统计口径，未再改运行行为。

在 `E:/pylianxi/chaiai/local-agent-web` 运行：

```powershell
$env:UV_CACHE_DIR = 'E:/pylianxi/chaiai/.uv-cache'
uv run --no-sync ruff check src tests scripts/paired_cache*.py
uv run --no-sync mypy src
uv run --no-sync pytest -q -p no:cacheprovider --basetemp C:/Users/asus/.codex/visualizations/2026/10/02/01a0fad0-4355-72b2-a33d-a1dd2c05dba1/pytest-cache-review
$env:DS_PRECISION = '1'
$env:DS_PRECISION_MODEL = 'deepseek-flash'
uv run --no-sync pytest -q tests/test_agent_eval_precision.py -p no:cacheprovider --basetemp C:/Users/asus/.codex/visualizations/2026/10/02/01a0fad0-4355-72b2-a33d-a1dd2c05dba1/pytest-cache-real-review
uv run --no-sync python scripts/paired_cache_report.py --out eval/paired-live-20261002
```

最后一条只重算已保存真实数据，不调用模型。重新实跑会产生真实 API 用量：
启动 `paired_cache_relay.py --out eval/paired-live-20261002/wire --port 8019`，
用 `python -m local_agent.api --config .../config.yaml --env .env` 启动隔离的配置，
再运行 `paired_cache_http_client.py --out 隔离目录 --port 配置端口 --turns 3`。
隔离目录须放 `scenario.json`，配置的四类持久化路径均改为新的绝对路径，不能复用旧记忆库冒充空库。
DSH 三问由 `node --env-file=.env scripts/paired_cache_dsh_client.mjs eval/paired-live-20261002` 驱动。
SDK patch 与配置、原始事件、请求、回答均留在产物目录；基线代码的开工副本在 `starting-files`，
不能直接用已修复代码的另一个新会话冒充修改前基线。

## 仍待确认的质量与现场边界

- 本轮证明了前缀损失的具体位置和同题缓存改善，未证明所有开放问题的回答质量不下降。
  三份长回答仍有新旧循环解释混用；行号审计有一条 `models.py:514` 的缩写歧义，
  多个同名模块存在，不能仅凭行号范围把它认定为精确引用。其余直接或唯一文件名引用可映射到冻结源码。
- 固定精度层通过不替代 T43 原“引用总条数不下降”门槛。用户未确认修改门槛，本轮不擅自更改。
- 第一组诊断配置的相对路径连接到原记忆库，运行有实际记忆写入；该组单独保存并排除正式对照。
  随后两组本项目与 DSH 全部使用隔离绝对路径。本次未删除或伪称恢复原库。
- 原工作区有大量先前未提交改动，未重置；本次只修改上述范围。没有改 DSH/Codex 源码。

完整证据在 `local-agent-web/eval/paired-live-20261002`：`comparison.json/.md`、`wire`、
`prefix-proof.json`、`checkpoint-answer-audit.json`、`answer-reference-audit.json`、三方完整回答与开工备份。

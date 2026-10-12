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

## 2026-10-08：T43 源码调查质量重开与提示 A/B

用户重新要求修正“回答只读表面”。先用本地 DSH 真 CLI/SDK 与 chaiai 真 HTTP 同题对比，再仅调整 chaiai 稳定提示、修正架构中已确认的模式说明。没有恢复固定反思调用、改工具 Schema、替换检索算法或修改正式存储。

**方法**：deepseek-flash / api.deepseek.com / 输出上限 4096 / chaiai temperature=0.2。同一份 532 文件冻结树，每题每版独立会话、Checkpoint、长期记忆、Chroma 和模型设置库。旧提示通过隔离源码副本运行，保留冻结 prompt_context.py；新提示运行当前源码。原题分别为“这个项目是干什么的？只读，不修改文件。”与“我们这个项目中的检索用的什么？用了哪些方法？只读，不修改文件。”。为隔离提示效果，被调查树仍含旧架构文字，修正文档没有偷偷替换比较语料。

旧提示复测 Q1 → 候选 A Q1 → A Q2 → 旧提示 Q2；随后 B、C、D 各按 Q1/Q2 顺序运行。共享供应商缓存温度、每版每题单样本，并用这些问题迭代了提示，因此这既不是随机受控收益估计，也不是独立保留集验收。早先 DSH Q1/Q2 为 18.9/33.7s、78.6%/87.7%；本轮后续未重跑 DSH，不能据此宣称最终版胜过 DSH。

| 版本 | 题 | 时间 | 工具调用 | 主动读不同 .py | 命中率 | 未命中输入 | 缓存读取 | 输出 Token | 错误事件 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 旧提示复测 | 1 | 12.7s | 6 | 0 | 90.9% | 4,777 | 47,741 | 1,943 | 0 |
| 旧提示复测 | 2 | 65.0s | 20 | 9 | 89.6% | 35,660 | 307,072 | 9,595 | 0 |
| 候选 A | 1 | 37.9s | 18 | 4 | 90.2% | 23,379 | 214,656 | 5,020 | 0 |
| 候选 A | 2 | 78.1s | 38 | 13 | 94.7% | 38,577 | 686,208 | 9,037 | 1 |
| 候选 B | 1 | 31.4s | 16 | 3 | 87.8% | 20,055 | 144,256 | 3,560 | 0 |
| 候选 B | 2 | 84.5s | 28 | 10 | 93.5% | 53,295 | 765,952 | 10,937 | 1 |
| 候选 C | 1 | 40.7s | 19 | 5 | 89.3% | 28,720 | 238,976 | 5,261 | 0 |
| 候选 C | 2 | 45.1s | 18 | 8 | 89.9% | 27,780 | 246,528 | 6,204 | 0 |
| 保留候选 D（范围优先） | 1 | 58.0s | 20 | 6 | 90.7% | 40,911 | 400,768 | 6,891 | 0 |
| 保留候选 D（范围优先） | 2 | 70.6s | 32 | 10 | 91.1% | 51,973 | 531,072 | 9,855 | 0 |

供应商 cache hit / prompt tokens 累加，题内包括任务记忆提取；提交前能力探测单列，不并入题内。应用统计逐题与 wire 用量一致，模型响应 HTTP 全为 200，冻结文件哈希无变化。所有题终态 completed；候选 A/B 检索题各有一个已恢复的工具失败，不能用 completed 掩盖错误。

**候选结论**：A 增加源码读取并覆盖 Web，但检索模式超过 200 字符；B 压缩输出模板使小窗口收尾恢复，仍有参数超限/笼统免责声明；C 工具错误消失，检索题却又漏 Web，未通过覆盖标准。D 把范围发现放在通用模式与输出要求前：用途题读取入口/图构建/能力与工具注册等 6 个实现文件，检索题覆盖自动 RAG、主动 grep/glob、记忆（含 Embedding）与 Web；明确源码默认 max_files=200 与示例 2000、任务结论有效重要度 1.0、Web 需 researcher + 开关。两题错误列表为空，结尾不再否认已执行检索。

D 两题合计命中率 **90.9%**（旧提示复测 **89.8%**）；但未命中输入 **40,437 → 92,884（+129.7%）**，总耗时 **77.7 → 128.6s**。用途题 **12.7 → 58.0s**，检索题 **65.0 → 70.6s**。深入调查增加了真实成本，不能写成“缓存不受影响所以成本没增加”。D Q1 90.7% 比旧 Q1 90.9% 略低，也不承诺任意问题 90%。

**前缀检查**：D 两题系统消息哈希一致，每题全部调用也只有一个系统哈希；所有版本工具 Schema 哈希相同。发布会改变系统哈希并重建首次缓存，后续调用不因调查结果动态改写系统提示。提示要求本档有计划工具时先记录跨模块调查范围，但 D 实测没有调用 update_plan；不能声称显式计划遵守度已验收。

**边界**：本轮证明“用途题开始核实实现、检索题覆盖改善”的样本结果，不证明整体回答质量稳定通过。最终回答部分后续引用仍省略完整相对路径，引用格式未全项通过；未逐句做盲评，也未重开记忆漏召回/相关度回显。新增“一句话简介”外发最初被自动审批拒绝，用户明确批准后真实复测：9.1s、completed、错误为空，回复为一句话；单独记录，不并入两道原题缓存对比。

**回归验证**：最终候选相关 38 项（runtime_snapshot / cache_prefix_regressions / capability_boundary / 6144 小窗口收尾）通过；最终版本全量 **1159 passed / 7 skipped**（337.04s，仓库外 basetemp）；跳过项依照现有套件条件，仍有既有依赖弃用警告。Ruff 全项目检查曾通过，最终文件复检通过；Mypy 106 模块通过。第一版全量 10 个失败中，9 个源于把 basetemp 放入 Git 仓库使临时工作区识别为同一项目，1 个是提示增长挤占小窗口预算；已改为仓库外 basetemp，且压缩输出模板修复真实预算退化，中间版全量 1159 passed / 7 skipped。

**范围外已确认的文档漂移**：根 README 的“下一步从 T50 开工”仍未同步 v0.5 计划里 T50 的实现与 T53 的验收记录，这会让用途回答误报进度。本轮只记录，未调整 v0.5 排期或验收状态。

**原始证据**：`local-agent-web/eval/quality-paired-20261008/` 的 comparison.json/comparison.md、两边完整回答与 wire；本轮在其 `prompt-ab/` 下，results.json / scenario.json / summarize.py / run.py，baseline-1/2、updated-1/2、final-1/2、accepted-1/2、scope-1/2 的 turn JSON 与 answer.md，以及 wire 请求/状态/用量。目录名 accepted 仅为过程标签，不表示该候选验收通过。eval 被 Git 排除，正式报告只保留脱敏汇总；没有存授权 Header，也没有复制 .env 或正式库。

**复现**：在 local-agent-web 启动 `.venv/Scripts/python.exe scripts/paired_cache_relay.py --out eval/quality-paired-20261008/prompt-ab/wire --port 8199`；旧提示 API 使用 baseline-src 作 PYTHONPATH（端口 8196/8195），D 用当前源码及 scope-1/2 的隔离 config.yaml（端口 8204/8205），API 参数为 `--env .env --no-open-browser`。客户端 `run.py --variant scope-1 --question 1 --port 8204` / `--variant scope-2 --question 2 --port 8205`；`summarize.py` 校对原始用量、系统/Schema 指纹与冻结文件哈希。再次复测必须先生成新的目录和数据库，不能复用已写记忆的库冒充空库；真实调用会再次外发两道原题与冻结源码/工具输出并产生用量。临时服务验证结束后关闭。

## 2026-10-08：T43 工具证据投影修复与同题真实对照

**根因与落地**：原 `read_file` 的 17,281 字符在 Prompt 窗口被压到 3,902 字符，元数据仍声明第 1–578 行、`truncated=false`、`has_more=false`。本地复现与实际转发 JSON 的 SHA256 一致，排除了日志显示省略。DSH 的生产裁剪入口在上下文压力达标/超限后，本项目此前“无条件对标 DSH”的注释已纠正；这轮继续保留本项目首次出站即确定性处理的策略，避免之后改写前缀。

新增 `memory/tool_output.py`：仅投影送模型的文件结果，正文保留真实 1 基行号；按行保留头尾，显式 `visible_ranges` / `omitted_ranges` / `partial_lines`；裁剪后更新截断与续读字段，字符上限造成的半行从同一行补读。目录用列名 + 行数组去掉重复字段名/名称，保留路径、类型、大小、修改时间与截断说明；80 项测试输出小于原 JSON 的 70%。原工具 JSON、Checkpoint、页面、工具 Schema、主循环不变，不新增反思调用；关闭压缩仍原样透传。

**对照方法**：同一份 532 文件冻结树、deepseek-flash、temperature=0.2、输出上限 4096；四轮各使用全新独立库。旧实现使用当前源码副本，只还原 HEAD 的 `short_term.py`，提示与新实现完全相同。运行顺序：新 Q1→旧 Q1→旧 Q2→新 Q2。两题分别是“这个项目是干什么的？只读，不修改文件。”、“我们这个项目中的检索用的什么？用了哪些方法？只读，不修改文件。”。每题新旧首条消息与工具定义逐字一致，所有任务系统/Schema 各只有一个哈希，题内历史始终追加；应用用量逐题等于转发用量，66 次题内模型响应全为 HTTP 200，冻结文件哈希未变。共享供应商缓存、每题每版仅一个样本，不能据此作稳定收益或因果结论；本轮未重新跑 DSH。

| 实现 | 题 | 时间 | 模型调用 | 工具调用 | 不同 .py 读取 | 命中率 | 未命中输入 | 缓存读取 | 工具错误 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 旧投影 | 1 | 38.0s | 12 | 20 | 8 | 90.6% | 24,703 | 238,848 | 0 |
| 新投影 | 1 | 43.1s | 13 | 21 | 6 | 91.6% | 29,632 | 324,864 | 0 |
| 旧投影 | 2 | 92.0s | 23 | 38 | 10 | 95.0% | 47,393 | 906,880 | 1 |
| 新投影 | 2 | 63.1s | 18 | 33 | 13 | 93.3% | 52,249 | 731,648 | 0 |

**不能掩盖的退化**：合计命中率 **94.08%→92.81%**，未命中输入 **72,096→81,881（+13.6%）**；合计耗时 **130.0→106.2s**。Q1 更慢且未命中增加，Q2 更快但未命中仍增加。旧 Q2 的错误是 grep 模式 278 字符超过 200 上限，随后恢复；新两题没有工具错误，这只是样本结果，不证明参数错误永久消失。不同轨迹选择的目录数量不同，不能用跨题目录总字符宣称整体成本降低。

**证据与质量边界**：新实测有 31 次带行号读取，其中 4 次显式中段缺失；逐行与冻结源码核对，无行号/正文错配、无缺口元数据矛盾。例：search.py 可见 1–116、562–578，缺失 117–561，next_start_line=117。检索回答覆盖 RAG、grep/glob、中期向量/长期结构化召回、Web 四类，但仍有缩写路径，Web Profile 条件说明不完整；用途回答仍偏节点枚举与文档，不能宣称整体语义/引用质量通过。固定判官没有恢复，计划遵守度没有新增验收。

**门槛结果**：修复专项 234 passed / 3 skipped（22.67s）；完整 pytest **1175 passed / 7 skipped**（191.67s）；`ruff check src tests scripts packaging`、`mypy src`（107 模块）通过。新增 16 个测试案例覆盖真实工具到窗口、CRLF、缺失区间定点补读、极长/半行、配置阈值、无效 JSON、非文件/富内容、工具角色、审计不变与历史前缀。最初 CRLF 夹具在 Windows 写成 CRCRLF，已改按字节写并复验；最初 `ruff check .` 扫入前轮未清理的 `.pytest-*` 夹具，不能算源码失败，也不能写成该命令通过，实际源码/测试/脚本/打包目录检查已通过。完整测试的临时目录放在仓库外，正式数据未使用。

**状态**：文件证据一致性修复可复核；成本与整体回答质量门槛未通过，T43 保持开放。代码未提交，不作发布验收。升级后旧会话的工具历史会采用新投影，首次请求可能重建相关缓存，后续同一投影保持稳定。

原始四轮回答、配置、请求/响应、结果与进程清单在忽略目录 `local-agent-web/eval/quality-paired-20261008/evidence-ab/`；临时转发与四个 API 服务验证后均已关闭。重新复测应使用全新目录/数据库和相同原题，不复用已写记忆的库。离线复验：在 `local-agent-web` 下运行 `.venv/Scripts/python.exe -m pytest --basetemp "$env:TEMP/chaiai-evidence-check" -q`、`.venv/Scripts/python.exe -m ruff check src tests scripts packaging`、`.venv/Scripts/python.exe -m mypy src`；真实复测会再次外发原题与冻结源码并产生 API 用量。

## 2026-10-09：T43 继续验收（离线审计，未通过）

本轮只复核已有四轮真实回答/出站数据，并重放只读工具与兼容格式；**没有修改生产代码，没有新增 API 调用，没有重新运行 DSH，也没有重复完整 pytest**。上一轮的 1175 passed / 7 skipped、ruff、mypy 结果保持为此前记录，不能代替本轮新增门槛。

### 必须改 / 必须重新验收

1. `local-agent-web/src/local_agent/memory/short_term.py:374` → 旧格式 `read_file` 缺少可靠行区间时，仍由通用路径裁剪正文却不修改截断元数据 → 模型会把不完整证据当成完整文件 → 后续修复必须明确标记裁剪和坐标未知，不编造原始行号，并保留原始工具记录。`tool_output.py:19-29` 的校验拒绝该格式后，落入 `short_term.py:373-379`；现有 `ReadFileResult` 的 `first_line` / `last_line` 默认均为 0（`tools/result_types.py:60-63`），是合法兼容结构。离线复现：正文 12,000→3,901 字符，`truncated=false`、`has_more=false`、无 `visible_ranges`，原始消息未变。这是原有通用裁剪路径的漏修；未证明正式库存在这样的旧记录。
2. `local-agent-web/eval/quality-paired-20261008/evidence-ab/after-2/answer.md:23` → 把中期排序写成相似度直接乘重要度、置信度 → 不符合实现 → 重新验收必须检查公式中的系数。真实代码是相似度 × 作用域系数 × `(0.5+0.5×importance)` × 时间系数 × `(0.5+0.5×confidence)`（`memory/recall.py:73,146-152`）。同句的 `1-distance` 指向 351–352，而具体运算在 353；引用范围存在不表示引用准确支持结论。
3. `local-agent-web/eval/quality-paired-20261008/evidence-ab/after-2/answer.md:24` → 把长期召回统一描述成置信度、重要度双阈值 → 抹掉任务结论分支 → 重新验收必须区分事实与任务结论。事实走双阈值（`memory/recall.py:235-243`）；任务结论在来源可信后只按置信度过滤（244–248），没有 importance 列，固定有效重要度 1.0、分数等于 confidence（286–301）。新轨迹已读取覆盖该分支的源码，因此不能把错误简单归因于“没读实现”。旧回答明确提到任务结论的有效重要度，本次新样本遗漏了该区别。
4. `local-agent-web/eval/quality-paired-20261008/evidence-ab/after-1/answer.md:7` → 以 `builder.py:107-123` 的节点注册顺序解释接线，把检索后写成压缩→计划 → 流程说明误导 → 重新验收应对照边和条件分支；实际 Agent 边为检索→计划→压缩（`graph/edges.py:91-92`）。初始自动 RAG 已提供架构流程片段，不能声称模型完全没看到流程证据。图中仍有 reflection 节点名称，不能据名称断言存在独立模型反思调用。

### 建议改 / 验收缺口

- Web 说明（新检索回答第 29 行）只说启用开关，未说明 Profile 必须包含 web；两个条件由 `tools/tool_registry.py:42-44` 明确约束。这是说明不完整，默认关闭的判断本身正确。
- 引用格式：只统计明确的 `文件:行号/区间`，不统计逗号续接和孤立 `:行号`。新检索回答 37 处明确引用中，完整路径只有 3 处、缩写 34 处；这 37 处均可唯一解析到冻结文件且范围未越界，但不能因此判事实正确。新用途题 19 处中完整路径 14、缩写 5；旧两题分别为 21/12/9、42/22/20（明确引用/完整/缩写）。完整路径要求未通过。
- 两条新轨迹未调用 `update_plan`，计划步骤为空。提示要求已存在，遵守度尚未通过，不能仅凭提示文字验收。

### 成本隔离验证

用原始出站 tool_call 的参数，在同一冻结文件树重放 `read_file` / `list_dir`，分别送旧/新投影；只比较同轨迹同结果。下表为本项目 CJK/4 估算器的**局部工具输入估算**，不是供应商 token，也不是实际缓存反事实，不包括其他消息与重复发送成本。

| 固定轨迹 | 旧投影估算 | 新投影估算 | 差值 |
|---|---:|---:|---:|
| 旧 Q1 | 17,867 | 18,372 | +505 |
| 新 Q1 | 23,945 | 22,985 | -960 |
| 旧 Q2 | 30,743 | 31,239 | +496 |
| 新 Q2 | 36,097 | 33,815 | -2,282 |

文件投影局部增加，目录去重复在两条新轨迹上抵消了增加。实际四轮全部工具消息按 tool_call_id 去重后字符总数：Q1 66,674→76,080，Q2 132,301→143,968；这是字符，不是 token。新轨迹工具选择/目录范围不同，未发现前缀被改写；真实未命中输入 72,096→81,881（+13.6%）仍不符合成本门槛，但不能把上涨全部归因于行号投影。共享供应商缓存和每题每版单样本仍无法分离精确因果。

### 本次结论与复现

**T43 未通过，保持开放。** 新格式 31 次编号读取/4 次缺口与冻结源码一致的结果继续成立；兼容格式新增失败，撤回此前笼统的“证据一致性修复通过”。已读取实现、行号有效、离线测试通过，都不足以证明回答公式/分支正确。下一项优先修复兼容裁剪，再验证事实、分支、引用与调查成本；本轮不改提示/主循环、不恢复固定判官。

原始产物在 `local-agent-web/eval/quality-paired-20261008/evidence-ab/`：`acceptance-legacy.json`、`acceptance-citations.json`、`acceptance-offline.json`、`acceptance_verify.py`（eval 为本地忽略目录）。在 `local-agent-web` 执行：

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv/Scripts/python.exe eval/quality-paired-20261008/evidence-ab/acceptance_verify.py
```

当前脚本会打印兼容矛盾与已有审计统计，并退出 **1**；这是已确认的验收失败，不会调用网络、读正式库或写源码。语义核验可逐行对照 `after-2/answer.md:23-24` 与冻结 `workspace/local-agent-web/src/local_agent/memory/recall.py:146-152,235-248,286-301`。真实 API 重测应使用全新隔离库，不把本轮离线审计当作新增真实样本。


## 2026-10-09：DSH 读取与压力压缩链路移植（实现通过，整体未通过）

用户批准后，先更新 `ARCHITECTURE.md` §10.1，再移植机制。参考本地 DSH `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`（MIT）：`packages/fs/tool-fs/src/read-render.ts:152-169` 的行号/续读、`packages/compaction/compaction-basic/src/index.ts:279-312` 的压力→裁剪→重测→摘要，以及 `packages/compaction/compaction-tool-result-pruner/src/index.ts:143-173` 的实际替换持久化。移植行为和顺序，未搬入 TypeScript 插件框架。

### 本次实际改动

- `tools/file_tools.py:211-241`、`tools/result_types.py:65`：字符截断明确报告半行，从该行续读；恰好切到行边界则从下一行继续。参数 Schema 未改，结果增加默认空的 `partial_lines`。
- `memory/tool_output.py:81`：旧格式坐标不可靠时明确标记 `source_coordinates=unknown`；压力裁剪同步设置截断/续读字段，不编造行号。低压力保留全文。
- `memory/short_term.py:323-357`、`graph/state.py:451`：实际模型替换文本以消息身份及原始结果哈希绑定，存入 Checkpoint；重启、后续低压力请求仍重放相同文本。原始工具结果保留作审计。裁剪文本不比完整投影短时不保存无收益替换。
- `graph/nodes.py:506`、`:812`：版本 1 主循环在低压力下保留所有未覆盖历史，不按 100 条消息滑动丢证据；压力达标才裁剪，重测仍超限才摘要。长任务可以摘要已完成的早期工具步，边界不切开调用/结果配对。摘要失败不推进覆盖边界。
- 配置含义、架构、对应回归同步更新；审批、页面、记忆存储和调查提示未在本轮调整。没有新增固定独立反思调用。

### 六轮真实同题对照

原题仍为“这个项目是干什么的？只读，不修改文件。”与“我们这个项目中的检索用的什么？用了哪些方法？只读，不修改文件。”。改动前的 107 个 Python 文件先复制到 `baseline-src`；新旧提示完全相同。三套引擎使用同一份 532 文件冻结树，各题独立会话/数据库或 DSH_HOME；deepseek-flash，输出上限 4096。执行顺序为新 Q1→DSH Q1→旧 Q1→旧 Q2→DSH Q2→新 Q2。

| 实现 | 题 | 时间 | 模型调用（含辅助） | 工具调用 | 不同 .py 读取 | 命中率 | 未命中输入 | 缓存读取 | 工具错误 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 改前 chaiai | 1 | 29.2s | 11 | 18 | 7 | 89.46% | 27,441 | 232,960 | 0 |
| 移植后 chaiai | 1 | 30.5s | 10 | 15 | 6 | 85.63% | 40,969 | 244,096 | 0 |
| DSH | 1 | 19.0s | 8 | 11 | 1 | 78.25% | 33,234 | 119,552 | 0 |
| 改前 chaiai | 2 | 72.3s | 21 | 42 | 12 | 93.42% | 51,848 | 736,000 | 1 |
| 移植后 chaiai | 2 | 64.3s | 16 | 31 | 10 | 91.76% | 71,368 | 794,752 | 1 |
| DSH | 2 | 38.0s | 15 | 27 | 12 | 90.51% | 51,904 | 495,104 | 0 |

合计：改前命中 **92.44%**、未命中 **79,289**、耗时 **101.5s**；移植后 **90.24%**、**112,337**、**94.8s**；DSH **87.83%**、**85,138**、**57.0s**。移植后未命中增加 **33,048（41.7%）**，成本门槛失败。较高命中百分比不能抵消输入总量增加。主调查请求未命中从 78,579 增至 111,641；记忆提取从 710 降至 696，上涨集中在主调查链路。两版检索题各一次 grep 模式超过 200 字符，随后恢复，参数遵守问题未解决。

计数以供应商响应为准：DSH journal 的 `inputTokens` 已扣除缓存，不能再次相减；表中补计其标题请求。六轮事件用量均与 wire 一致，81 次 HTTP 响应均为 200，请求正文哈希无错配。新旧两题首条消息和工具 Schema 相同，各主调查系统前缀稳定；辅助记忆请求追加其专用指令，未发现前缀改写。532 份冻结文件哈希不变。新样本 24 次编号读取与冻结正文逐行核验一致，没有模型投影引入的中段缺口。

**限制**：每引擎每题仅一次、共享供应商缓存，不能作稳定收益或因果结论。chaiai temperature=0.2；DSH SDK 未提供同项配置，wire 未传 temperature。DSH Q1 在冻结目录内执行 Git 命令，额外读到了父仓库实时 Git 元数据，因此并非纯粹相同输入。部分 Q1 计时与本地完整测试并行。真实样本未达到 1M 上下文的压力阈值；压力裁剪、持久化与单任务摘要由本地测试验证，尚无真实端点高压力验收。

### 回答质量：有改善，仍未通过

以下路径相对于本轮忽略产物目录 `local-agent-web/eval/dsh-port-20261009/`；源码对照使用其中 `workspace/` 冻结树。

- `after-1/answer.md:15`：Streamlit 入口事实正确，但引用根 `README.md:102` 越界；应为 `local-agent-web/README.md:102`。用途题读取了 6 个实现文件，但本轮旧样本读取 7 个，不能声称读取覆盖比旧版提升。
- `after-2/answer.md:41`：本次用“重要度系数、置信度系数”表达排序，没有重现上一轮直接乘原值的错误；但这不能证明以后不会再错。
- `after-2/answer.md:43`：长期召回遗漏任务结论只按置信度过滤、有效重要度固定 1.0 的分支；冻结 `memory/recall.py:244-248,286-301` 明确实现该区别。旧样本 `before-2/answer.md:34` 提到了有效重要度，完整性门槛仍失败。
- `after-2/answer.md:48`：Web 的 Profile/开关说明正确，但 `capabilities.py:46` 支持的是 coder；包含 web 的 researcher 在 47。引用范围合法不表示支持结论。
- 旧样本 `before-2/answer.md:12` 错把 rg 退出码 2 排除在失败之外；冻结 `rag/search.py:365-368` 将其视作失败。本次新回答及 DSH 没有重现该错误。
- DSH Q2 写出带 0.5 下限的中期系数（`dsh-2/answer.md:34`），但也遗漏任务结论区别（36）及 Web 默认/Profile 条件（44）。DSH Q1 没有明确文件行号引用，不能把成熟项目当作自动满足本项目全部质量门槛的证明。
- 新检索回答 42 处明确文件行号引用中仅 1 处完整相对路径；新用途题 16 处中 14 处完整路径，1 处越界。统计不含逗号续接和孤立行号，仍需逐条核查语义。四条 chaiai 轨迹均未显式更新计划，计划遵守门槛没有通过。

### 本地验证、产物与状态

最终完整 pytest **1184 passed / 7 skipped**（192.25s）；`ruff check src tests scripts packaging` 与 `mypy src`（107 模块）通过。新增压力测试 9 个案例，覆盖低压力长历史、实际 SQLite 重启重放、旧格式坐标、复用 ID/不同正文、单任务完整工具前缀、摘要失败、行边界/半行与极小阈值不扩张。既有“临时省略整步”的断言按持久化摘要新契约更新；没有移除审计/恢复门槛。真实样本后补的极小阈值保护仅作用于压力裁剪，不影响六轮低压力样本。

原始请求响应、六份回答、独立配置/库、冻结清单、baseline 源码哈希、`results.json`、`purpose-breakdown.json`、进程/清理清单与复现脚本在上述 eval 目录；该目录被 Git 忽略，未当作仓库内可复核附件。临时 relay 和四个 API 服务已经关闭，正式数据和服务未用于测试。已有旧 pytest 夹具目录未清理。

在 `local-agent-web` 下离线复验（不发网络请求）：

```powershell
.venv/Scripts/python.exe -m pytest --basetemp "$env:TEMP/chaiai-dsh-port-check" -q
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe eval/dsh-port-20261009/summarize.py
```

**结论：机制移植与本地回归已完成；T43 整体仍未通过。** 旧格式矛盾已修复，但真实引用/分支完整性、计划遵守和成本仍有失败。代码保留为待验收改动，未提交或发布；后续优先解决输入总量与引用/结论错误，不能用本次 90.24% 宣称稳定达到 90% 或“无质量损失”。

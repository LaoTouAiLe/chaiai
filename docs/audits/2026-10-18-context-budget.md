# 工具预算重复计数与小窗口压缩修复（2026-10-18）

本轮只修正工具定义的预算估算，不调整压缩阈值、可用工具、模型窗口、记忆召回或摘要策略。
正式后端已刷新：健康检查 200，设置版本 5；任务仍为 65 completed / 2 cancelled，无活动任务。

## 根因

`src/local_agent/context/token_budget.py` 的旧 `_tool_schema_text` 拼接工具名称、工具说明和
`args_schema.model_json_schema()`。多数 `@tool` 的参数模型顶层 description 就是完整工具说明，
因此同一段说明在估算时出现两次。实际 ChatOpenAI 工具绑定使用 `convert_to_openai_tool`，
工具说明只保留在 function.description，参数模型的顶层重复说明不会发送。

当前默认工具集合在同一字符估算器下，旧口径为 **9515 token**，实际绑定结构为 **6068 token**，
差 **3447 token**。这不是供应商计费 token，也不表示实际请求变短；实际请求的工具结构没有改变。
错误的固定开销挤掉了摘要和最近工具证据的预算，引发压缩后虚报超限，或因证据余量不足而不生成摘要。

修复后 BaseTool 按同一转换器产生的出站结构计数；已转换的工具字典也按绑定结构计数。
保留实际发送的参数说明、嵌套字段、约束与包装，不把真实工具转换错误静默降成零开销。
既有非标准测试工具仍支持名称、说明和可取得参数 Schema 的兼容估算。

## 修改文件

| 路径 | 修改 |
| --- | --- |
| `src/local_agent/context/token_budget.py` | 修正工具定义序列化口径 |
| `tests/test_tool_budget.py` | 新增 11 项预算及实际出站结构回归 |
| 上级 `ARCHITECTURE.md` | 实现前补充预算计数契约 |
| 上级 `V1_0_DEVELOPMENT_PLAN.md` | 记录本轮修复及验收边界 |
| 上级 `README.md` | 增加当前结果和本记录入口 |

## 验证

修复前 `tests/test_pressure_compaction.py` 为 **16 passed / 3 failed**；仅替换工具计数后，
原测试全部 **19 passed**。新增预算测试 **11 passed**；合并专项 **30 passed**。

新增测试通过真实 `ChatOpenAIWithReasoning.bind_tools(...).invoke(...)` 构造 HTTP 请求，
由本地 `httpx.MockTransport` 截获实际 tools 数组；估算与实际发送结构一致。
该测试不联网、不调用真实供应商。还验证长说明只计一次、嵌套字段、注入参数、strict 字典、
转换错误、兼容回退、空清单，以及输入未达压力阈值时不购买摘要。

同一工作区、同一状态的离线 20k 窗口对照，预留输出 4096、输入预算 15904：

| 场景 | 修复前输入估算 | 修复后输入估算 | 修复后摘要字符数 | 审计消息 |
| --- | ---: | ---: | ---: | ---: |
| 尚未冻结运行快照 | 17171 | 15540 | 1036 | 17 条保留 |
| 已冻结运行快照 | 17054 | 15551 | 2072 | 17 条保留 |
| 冻结快照，保护任务及计划 | 28730，未推进覆盖 | 15505 | 2066 | 17 条保留 |

三种场景均恢复到输入预算以内，保留当前目标、完整计划标题及状态、最近完整工具配对。
原有 SQLite 重启、持久正文替换、摘要失败不推进覆盖及证据余量不足不调用摘要模型的测试通过。
对照脚本 `eval/verify_pressure_budget.py`；原始结果 `eval/pressure-20261018/comparison.json`。

扩展到预算、压力、记忆、快照、用量、压缩页面进度、文件投影、大文件及并行等 9 个测试文件：
**300 passed / 2 failed / 2 skipped**。两个跳过分别是已安装 Chroma 时不适用的缺依赖负例，
以及 Windows 上不适用的 POSIX 大小写测试。

两项失败均为现有删除审批断言：

- `tests/test_runtime_snapshot.py:834` 仍期待默认 delete_file 产生 pending_approval。
- `tests/test_model_usage.py:815` 仍期待同一默认删除动作产生 approval_requested_at。

独立进程恢复本轮前的估算函数后，相同两项仍失败，见 `eval/pressure-20261018/approval-baseline.txt`。
本轮未改审批实现或这些旧用例。

首次扩展回归把临时工作区建在本 Git 仓库中，6 项项目隔离测试因此识别为同一项目。
随后改用仓库之外的全新专用临时目录，这 6 项通过；没有更改 scope 实现或测试断言。
最终结果见 `eval/pressure-20261018/regression-outside-git.txt`，初次结果保留于 `regression.txt`。

复测命令，在 `local-agent-web` 执行：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_tool_budget.py tests/test_pressure_compaction.py -q -p no:cacheprovider
.venv/Scripts/python.exe eval/verify_pressure_budget.py
.venv/Scripts/python.exe -m ruff check src tests/test_tool_budget.py
.venv/Scripts/python.exe -m mypy src/local_agent/context/token_budget.py
```

src 和新测试 Ruff、修改模块 mypy 通过。全量 mypy 仍有原有 3 条诊断：
`application/projection.py:1544` 的两条、`context/prompt_context.py:248` 的一条，见 `eval/pressure-20261018/mypy.txt`。
本轮未重跑前端构建和全部项目测试，未宣称全库全绿。

## 边界

Token 数仍采用原有字符启发式估算，不是供应商 tokenizer 的精确结果。
真实超过预算、固定前缀本身装不下、超大受保护证据等情况仍须报告压力，不能由本次去重保证任意小窗口可用。
本轮未调用付费模型，未重新验收 T43 的真实多轮质量/缓存目标；此前该质量门仍开放。
正式服务检查记录为 `eval/pressure-20261018/formal-smoke.json`，没有改写用户设置或历史用量。

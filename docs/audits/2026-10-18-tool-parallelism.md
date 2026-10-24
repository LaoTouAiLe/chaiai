# 同批只读工具并行（2026-10-18）

用户在浏览器、持久进程与宿主适配完成后要求继续。本轮推进前述建议中的主工具并行，
不调整文件搜索预算、记忆策略或 Shell 写边界，不修改正式偏好，不提交或发布。

## 改动与边界

| 文件 | 本轮贡献 |
| --- | --- |
| `src/local_agent/graph/tool_parallel.py` | 新增明确只读工具契约、有界线程池、每调用 ContextVars 和实际执行起止时间 |
| `src/local_agent/graph/nodes.py` | 连续只读调用分组，工作线程只取工具结果，图线程按原顺序写消息、事件、错误和 Checkpoint |
| `src/local_agent/context/prompt_context.py` | 提示模型将独立读取/搜索/网页请求同批提交，依赖结果的动作等待返回后构造 |
| `tests/test_tool_parallel.py` | 新增 24 项并行、顺序、权限、取消、上下文和 SQLite 持久化验证 |
| `../ARCHITECTURE.md` §22.1、`../V1_0_DEVELOPMENT_PLAN.md`、两级 README | 记录本轮授权、执行边界和证据 |

可以并行：`list_dir/read_file/grep/glob/web_search/web_fetch/git_status/git_diff/git_log/skill_list/skill_load/view_image/inspect_system`。
有审批历史、已确认或不可见的调用不进入并行组，执行侧原有防御仍保留。

不能跨越顺序边界：文件写入、删除、下载、Git 提交、任意 Shell、浏览器、交互进程、委派、
计划与任务控制。搜索 `save_full=true` 会落盘，保留串行。用户配置了相关联网计数额度时，
相应网络工具继续按旧路径逐次检查，不为并行重复消费同一份剩余额度。

取消后排队的读取在真正调用工具前再检查令牌；已完成的读取仍保存真实结果，
不因合并时已经取消而改写成“未执行”。正在运行的工具沿用其已有取消与超时机制，
不承诺 Python 线程能立即打断任意网络调用。

并行只发生在同批连续独立读取。模型每轮只发一个调用时不能跨轮提前执行未来操作；
这次也没有自动推断任意 Shell 脚本的读写依赖。线程池使用标准库有界默认并发数，
没有增加模型决策次数或任务总调用次数上限。

## 验证

### 专项与回归

```powershell
# 在 local-agent-web/ 下执行
.venv/Scripts/python.exe -m pytest tests/test_tool_parallel.py -q -p no:cacheprovider
```

末次专项 **24 passed**。重点断言：

- Barrier 确认三个读取真正同时进入；按 C、B、A 完成时，结果仍按 A、B、C 保存。
- 两次读取 → 写入 → 两次读取，前组只得到旧值，后组只得到新值，写入始终在图线程执行。
- 取消时未开始的调用不触发实际读取，已经完成的两个读取保留成功内容。
- 每个调用继承父请求 ContextVars，修改不污染兄弟线程或父上下文。
- 单个读取失败不取消其它读取；确认记录去重和一次性审批仍有效。
- SQLite Checkpoint 中的工具消息与结果完整，下一次模型决策能使用这些内容。

扩大原生回归执行了主循环、网页预算、审批恢复、取消、故障注入、只读/代码/委派工作流、
交互工具、过程展示与投影：**431 passed / 7 failed**（首次专项为 23 项，后增加一项 Git 提交边界测试）。
JUnit：`eval/tool-parallel-tests.xml`。

关闭新并行调度后，对这七个失败项做相同串行基线，**七项仍全部失败**，失败身份完全一致：

| 位置 | 当前断言与实际行为 |
| --- | --- |
| `tests/test_fault_injection.py:260` | 预期旧 UTF-8 错误文案，实际已支持指定编码并提示实际解码失败 |
| `tests/test_readonly_workflow.py:218` | 预期 8 条事件，当前任务控制协议增加为 9 条 |
| `tests/test_code_workflow.py:275`（3 种选择）、`:413`、`:433` | 仍要求编辑等待审批，与用户此前取消工作区编辑审批的规则不一致 |

串行对照脚本与 JUnit：`eval/check_parallel_baseline.py`、`eval/parallel-serial-baseline.xml`。
本轮没有修改这些历史断言，不能宣称完整 pytest 套件全绿。

`ruff check src tests/test_tool_parallel.py` 通过；调度与节点两个模块定向 mypy 通过。
全项目 mypy 仍有前一轮已记录的三条诊断（`application/projection.py:1544` 两条、
`context/prompt_context.py:248` 一条），本轮未修改对应实现。
无前端实现、API Schema 或依赖变化，本轮未重复前端全套测试或构建。

### 成对 HTTP 对照

脚本：`eval/verify_tool_parallel.py`；产物：`eval/tool-parallel-1792316783646692000/result.json`。

使用真实生产 `web_fetch` 工具、解析与执行节点，注入测试 resolver/transport 到本地 HTTP 夹具；
三个实际 HTTP 请求每个固定等待 0.35 秒。生产网络地址校验没有修改，未声称访问公开网站的速度基准。
串行组仅在内存中关闭新增调度，其余输入一致；各运行三次。

| 指标 | 串行 | 并行 |
| --- | --- | --- |
| 三次总耗时中位数 | 1.1090 秒 | 0.3690 秒 |
| 同时在途 HTTP 请求 | 1 | 3 |
| 结果顺序与状态 | A/B/C，全部成功 | A/B/C，全部成功 |

这个固定延迟夹具约 **3.01 倍**；不能据此推断所有任务都会提速三倍。

### 真实模型与公开消息入口

脚本：`eval/verify_agent_parallel.py`；产物：`eval/parallel-agent-1792316875007291900/`。
使用当前配置真实模型、新工作区与聊天库，只读复制设置；仅在隔离副本关闭自动检索和记忆，
让读取证据明确来自工具。输入三个固定 JSON 数据文件的 value 分别为 13、29、47。

- 模型首次响应实际提交三个 `read_file` 调用；没有额外补救消息或审批。
- 三个实际读取区间重叠：最晚开始 `09:47:58.552815 UTC`，最早结束 `09:47:58.558792 UTC`。
- 随后写入 `result.json`，再读取核验，总和为 **89**，任务完成。
- 四次模型调用，约 **9.03 秒**；工具批次大小为 `[3,1,1,1]`（最后一批为任务控制）。

这些数据只代表这道验收任务，未测试任意模型或任意任务的整体成功率。

## 用户复验

新版已加载到正式本地服务，健康检查 200。正式偏好仍为版本 5，执行次数与默认命令超时仍为 null；
任务记录仍为 65 完成、2 取消，运行/排队/等待/复核均为 0。验证摘要为 `eval/parallel-formal-smoke.json`。

刷新新版后，在工作区放置三个 JSON 文件，分别含不同数字，要求：
“这三个文件独立，请同批调用读取，按真实值求和，写入汇总文件后重新读取核验。”
过程里应看到同批多个读取；轨迹中的起止区间可以重叠，写入与后续核验仍保持顺序。

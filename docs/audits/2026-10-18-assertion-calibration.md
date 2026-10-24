# 全库断言与离线评测校准（2026-10-18）

用户批准处理上一轮遗留的全库旧断言。本轮不恢复决策次数、机械完成判官或默认文件审批，
不修改生产权限、Graph、记忆映射、存储结构、依赖和前端。

## 根因与处理

完整原生基线：**2389 passed / 69 failed / 8 skipped**，2466 项，1344.74 秒。
原始日志和 JUnit 在 `eval/assertion-calibration/before.txt`、`before.xml`，
逐项原诊断在同目录 `failures.txt`。它们来自临时测试数据，不是正式会话数据。

| 原失败域 | 数量 | 校准依据 |
| --- | ---: | --- |
| Agent 评测 | 14 | task_control 与 update_plan 分开计业务动作；完成正确性独立核对调用状态、文件和来源 |
| 能力与提示边界 | 12 | 固定当前工具顺序、完整工具清单和已批准的自主收尾/Git 提交规则 |
| 检查点序列化 | 2 | 显式开启审批；通过真实旧图生成并重读历史 EvidenceReference，保留全部白名单 |
| 代码工作流 | 5 | 审批测试显式开启 large_batch_edit，产品默认保持免审批 |
| 外部写边界 | 3 | 直接文件后端调用提供内部并发版本 Hash，确保断言实际到达外部写授权检查 |
| 故障注入 | 1 | 核对实际解码错误文案，并新增断言半写入字节未变化 |
| 只读工作流与反思 | 2 | 控制调用保留在审计；失败作为反馈，不强制恢复旧机械红卡 |
| 发布契约基线 | 1 | 审查配置默认值、配置 schema 和 OpenAPI 三个变化指纹后更新候选基线 |
| 路由 | 13 | 主模型结束协议、真实 diff/Hash、可选审批及明确的旧协议兼容夹具 |
| 安全与流式 | 3 | 补完整工具清单；验证流式/非流式都只有一次结束动作和一次业务读取 |
| 子任务资源 | 1 | 请求结构不再硬限三个子任务/两个并发；空输入、非法路径、非正并发仍拒绝 |
| Streamlit 审批 | 3 | 三个审批场景单独开启删除审批，保留拒绝、批准、刷新和重启恢复 |
| 网页不可信来源 | 1 | 新 browser 与 web_search/web_fetch 同为不可信来源 |
| 工作流公开入口 | 4 | 审批夹具显式开启配置，保留等待时释放工作线程和重启后原请求语义 |
| 工作流评测及配对报告 | 4 | 固定脚本用 task_control 结束；生命周期调用仍完整计入审计，不再耗尽脚本 |

三个新增负例/对照在真实图上运行：拒绝读取却声明完成、编辑成功但最终文件错误，都被评测检出；
成功读取加结束动作保留两条原始审计，而业务动作只算一次。没有把主模型自称完成当作质量通过。

工作流基线现在准确记录代码任务 **6 次模型/6 次工具**、只读任务 **4 次模型/4 次工具**。
结束动作替代原普通文本回答，模型调用不增加；工具计数包含这个真实控制调用。
固定文件 Hash、三个 unittest 的真实输出、引用与实际读取对应、越权及重复写入负例均保留。

发布契约仅更新三个指纹：已批准的权限/资源默认值与能力配置反映在配置 schema/defaults；
图片附件、视觉声明、能力开关与技能/联网设置接口反映在 OpenAPI。
数据库版本、DTO 版本、事件类型及公共字段不变。缺失/损坏基线、任意契约漂移仍会失败，
检查命令不会自动覆盖基线；本次显式执行了审查后的 `--write`。

## 旧协议与现行行为的边界

只对确实验证旧协议的测试启用 legacy_loop，不在模块或全局启用。
旧夹具的假模型不发送新 task_control，与原有 build_legacy_graph 保持一致。
保留旧循环的空转保护、未确认副作用要求人工核对、机械证据及长期任务结论兼容测试。
新循环的读取和编辑测试直接检查结果和文件，不借旧图绕过现行协议。

本次发现一项现行记忆限制：新循环的 trust 为 uncertain，task_fragment 仅写中期，
不会触发原来要求机械 verified 的长期 task_conclusion 映射；entity_fact 的长期写入仍有现行图测试。
本轮没有改变这项产品策略，也没有把“长期结论已保存”写成当前新循环的验收结果。

## 本轮文件

| 文件 | 修改内容 |
| --- | --- |
| `tests/conftest.py` | 可脚本化新结束动作/多批工具，旧协议夹具关闭假模型的新结束动作 |
| `tests/test_agent_eval.py` | 业务与控制审计、独立完成质量判定、三个真实图负例/对照 |
| `tests/test_capabilities.py`、`test_capability_boundary.py`、`test_security.py`、`test_web_tools.py` | 现行工具清单、提示与不可信来源边界 |
| `tests/test_checkpoint_serde.py` | 可选审批与真实旧协议序列化覆盖 |
| `tests/test_code_workflow.py`、`test_web.py`、`test_workflow_application.py` | 场景内显式开启审批 |
| `tests/test_external_read_policy.py`、`test_fault_injection.py` | 抵达实际权限边界、明确解码错误与字节不变 |
| `tests/test_readonly_workflow.py`、`test_reflection.py`、`test_router.py`、`test_streaming.py` | 新结束协议、控制事件、实际读写结果及旧协议兼容边界 |
| `tests/test_v07_adversarial.py` | 已放宽资源额度的请求契约 |
| `src/local_agent/workflow_eval.py`、`workflow_comparison.py` | 仅离线评测脚本：显式结束、允许控制动作、保留完整计数和质量检查 |
| `tests/test_workflow_eval.py`、`test_workflow_comparison.py` | 当前固定脚本计数及原负例 |
| `docs/release/compatibility-baseline.json` | 三个已审查的候选指纹 |
| 上级 `ARCHITECTURE.md`、`V1_0_DEVELOPMENT_PLAN.md`、`README.md` | 现行评测口径、验证结果与边界入口 |

## 验证

完整后端复测：**2461 passed / 0 failed / 8 skipped**，2469 项，1378.38 秒。
日志和 JUnit 为 `eval/assertion-calibration/after.txt`、`after.xml`；
69 项原失败全部消除，新增 3 项真实图负例/对照通过。其余验证：

完整复测前后的 src/tests/scripts/packaging Python 源码指纹一致，退出码为 0；
计数和指纹汇总见 `eval/assertion-calibration/summary.json`。

- 固定工程评测：**402 passed / 0 failed / 0 skipped**，450.20 秒；
  `eval/assertion-calibration/fixed/report.json` 的 passed、source_unchanged 均为 true，pytest_exit_code 为 0。
  使用确定性测试替身，semantic_quality_reviewed 为 false。
- Agent 评测及能力清单：44 passed；检查点/代码工作流：73 passed；
  外部写/故障/只读工作流：62 passed；反思：54 passed。
- 路由/安全/流式/联网/子任务专项：首轮 297 passed / 2 failed / 2 skipped；
  两处旧提示/旧回答存储断言修正后，原失败两项均通过。
- 页面/工作流公开入口/配对评测/发布契约：124 passed。
- `ruff check src tests scripts` 通过；`mypy src` 160 个源码文件通过。

以上定向组有重叠，不相加当作独立总数。完整原始日志均在 `eval/assertion-calibration/`。

8 项跳过未计入通过：2 项真实模型测试未开启、2 项 POSIX 专属用例、
2 项本机无法创建符号链接的用例、1 项本机未启用 NTFS 8.3 短路径的用例，
以及 1 项仅适用于未安装可选 Chroma 依赖的负例（本机已安装）。38 条警告为测试运行所报，
包括依赖弃用提示；本轮不升级依赖消除警告。

复现完整后端测试，在 `local-agent-web` 执行：

```powershell
$testTemp = Join-Path $env:TEMP ('chaiai-calibration-' + [Guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $testTemp) { throw '临时路径已存在' }
.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp=$testTemp -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe scripts/check_release_contracts.py --check
.venv/Scripts/python.exe scripts/run_release_evaluation.py
```

临时路径须在项目 Git 根之外，否则项目级记忆隔离测试会把两个临时目录识别为同一 Git 项目。
Windows 原生进程/受限令牌测试需要可用的系统权限；本轮在 Codex 外层沙箱外运行这些测试。
正式服务、正式设置、会话及模型凭据未改，未调用真实模型，未提交、打包或发布。
源码检查采用明确的 src/tests/scripts 范围；没有删除历史 `.pytest-*` 产物来伪造全目录 Ruff 通过。

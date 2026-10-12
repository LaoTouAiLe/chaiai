# v0.7 T70 同题对照与工程交付记录

日期：2026-10-12。**T64–T70 已完成，v0.7 工程源码可交付，未提交 Git。** 范围：已定义的复杂工作流与受控子 Agent；本记录不重新审查旧版本，不进行语义质量或真实付费模型验收。

## 同题对照

原始报告：[v07-workflow-comparison-20261012.json](v07-workflow-comparison-20261012.json)。两题各重复三次，每次自由循环与 Subgraph 各跑一次，共 12 个样本；交替执行顺序，独立重建工作区种子。使用相同配置、Profile、脚本模型类与应答配置；工作流选择是两臂唯一显式运行选择差异。T64 的三份源码和机械校验器保持原 Hash，不修改任务或放宽验收。

| 固定任务 | 完成数：自由循环 / 工作流 | 每次模型调用 | 每次工具调用 | 写入 / 真实测试与证据 |
| --- | --- | --- | --- | --- |
| 代码修改 | 3/3 / 3/3 | 6 → 1 | 5 → 6 | 两臂均只修改源码一次，三个 unittest 真执行且退出 0，测试不改，修改后复读成立 |
| 只读调查 | 3/3 / 3/3 | 4 → 0 | 3 → 4 | 两臂均无写入，文件清单/Hash 不变，三项原文与行号由实际 read_file 输出证明 |

代码模型调用减少 83.33%，只读减少 100%；两者工具调用各增加 1 次，不声称所有指标都改善。所有尝试均保留；每次配对都通过完成、权限与副作用硬门，并在模型调用数上改善。缺样本、失败、配置/种子/模型不一致、越权、重复写入均阻止优势结论；图异常导致审计不完整时工具计数写 null，不用 0 掩盖已发生调用。

任务指纹：

- 代码：`1ad43f952bf399c389a23d7d0762d453ea4f08702735ae082489aae925ea7e70`
- 只读：`385c7fb42fd6f5428b9c6d513a5554e2216193f4d8c86adf7f4ff218aa23f2bc`

报告保存实际调用请求/响应摘要、工具事件与输出、文件 Hash、配置/模型指纹和执行源码 Hash。脚本适配器识别实际出站协议，没有评测臂开关；固定应答已知正确修复，所以仅证明离线编排省去模型调用，不证明真实模型完成率或回答质量。只读工作流是原文整理。耗时保留原始测量但不用于优势门；实际 Token 与缓存未知，写 null；预算预留单列。两臂配置一致，预算执行门仍各用原自由循环和工作流实现。

## 验证命令与证据

```powershell
Set-Location E:/pylianxi/chaiai/local-agent-web
$env:PYTHONIOENCODING='utf-8'
.venv/Scripts/python.exe scripts/run_workflow_comparison.py --repetitions 3 --output eval/v07-comparison-replay.json
.venv/Scripts/python.exe -m pytest tests/test_workflow_comparison.py -q -p no:cacheprovider
$taskFullRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("chaiai-v07-replay-" + [guid]::NewGuid().ToString("N"))
.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider --basetemp $taskFullRoot
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe scripts/export_openapi.py --check
Set-Location frontend
npm.cmd run check:api
```

全量临时目录必须位于仓库外、为本次验证专用且不含需保留数据；pytest 会清理指定 basetemp。本次实际由 `eval/v07-t70-20261012/run_full.py` 创建系统临时目录并记录命令、退出码和清理结果，需允许测试清理自建子进程树。评测 CLI 任一机械门或优势门失败返回 1，成功返回 0；原始结果先写入再返回退出码。

当前证据目录：`local-agent-web/eval/v07-t70-20261012/`。最终对照命令退出 0，定向 **28 passed / 0 failed，6.01 秒**；ruff、mypy（129 文件）、OpenAPI 与前端类型漂移均退出 0。冻结后完整回归 **1771 passed / 8 skipped / 0 failed / 38 warnings，470.51 秒**，退出 0；229 个冻结文件 Hash 不变，专用临时目录已清理，汇总为 `verification.json`。

8 项跳过：2 项真实付费模型、1 项只针对未安装可选依赖的分支、1 项真机凭据存储、2 项环境无法创建的符号链接、2 项 POSIX 大小写/权限位语义。跳过不计通过，38 条依赖弃用警告保留；没有升级依赖。

首轮全量为修正新跑器异常审计统计而受控中断，不计通过。`interrupted-first-*` 保留原日志、命令、冻结 Hash 与实际退出码 4294967295；源码在该轮中断前未改变。初次对照和定向结果也分别保留为 `comparison-first.json`、`comparison.log`、`focused-first.log`，最终结论只采用修正后源码的结果。

## 交付边界

生产工作流和前端源码相对 T69 不变，使用 Hash 核对其原始前端验收记录：39 文件 / 345 个单测，工作流真实 API 浏览器 5 个、原自由循环浏览器 2 个通过；本轮不将这些写成重新执行的结果。最终 Python 全量包含父子关闭重开、不重复执行、权限收窄、未知副作用人工核对、持久取消、审批恢复、子失败和超时等已有故障注入。

不新增付费调用，不生成 ZIP、不发布、不自动提交 Git。回答语义审查按用户要求继续暂停；T43 真实压力验收沿原记录保持开放。本阶段不把真实模型优势、未知 Token、跳过项或未验证平台写成已通过。

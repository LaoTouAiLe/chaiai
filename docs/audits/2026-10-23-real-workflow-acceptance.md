# 真实开发流程验收（2026-10-23）

用户确认先用一条真实任务检查日常使用链路。本轮只做验收、取证与问题定位，没有修改产品源码、原配置或原会话数据库。

## 环境与样本

- Windows；真实 FastAPI 服务、已构建前端、真实 `deepseek-reasoner` 连接；没有使用 fixture 或假模型。
- 使用 `local-agent-web/scripts/live_development_acceptance.py` 建立独立会话库和设置副本，记忆只在副本中关闭。
- 服务端口 `8147`；隔离根目录 `E:/tmp/chaiai-real-acceptance-20261023-01`。
- 副本原先继承 `danger-full-access`，通过 `/api/permissions` 切到 `workspace-write`，没有外部写授权。原设置未改。
- 样本 `workspaces/clamp-demo` 是一个无依赖 Node 项目：修复 clamp、保持测试不变、运行测试和语法构建、更新报告、清理测试草稿。
- 第一轮真实模型完成开发任务；第二轮专门验证 `run_shell` 删除测试文件时的人工审批。

## 已验证结果

| 项目 | 实测结果 |
| --- | --- |
| 修改前对照 | 3 个测试中 1 个通过、2 个失败 |
| 模型修改 | clamp 上下界修正；测试、package.json、README、ticker 脚本逐字核对未变 |
| 模型执行 | `npm test` 3/3 通过；`npm run build` 退出码 0 |
| 改动面板 | 列出源码、报告及删除的草稿；源码 diff 的删除行和新增行与磁盘一致 |
| 终端审批 | 启动前显示工作目录、Shell、写入边界和批准后语义；批准后才启动 |
| 终端真实执行 | 再运行测试及构建，3/3 通过，构建退出码 0 |
| 切页与刷新 | 切到文件页签再回来、刷新页面后，均重连同一个 terminal_id；长命令 PID 不变，心跳文件继续增长 |
| Ctrl+C | 长命令 PID 26380 退出，Shell 可继续接收命令 |
| 结束终端 | 新的长命令 PID 17464 随终端结束退出；页面显示“终端已结束” |
| 终端写入边界 | 向工作区外的隔离根目录写入被拒，终端显示“拒绝访问”，目标文件没有创建 |
| 产物 | 源码和报告出现在列表；报告弹窗内容与磁盘一致 |
| 会话恢复 | 从产物返回来源会话，再刷新，历史仍在 |
| 会话地图 | 真实数据加载出 1 轮问答，可见完成状态；已保存截图 |
| 命令审批 | `del /q approval-probe.txt` 进入待批；刷新后 approval_id 不变，文件仍在；点击“允许一次”后才删除，记录为 approved |
| 浏览器 | 验收中捕获的 console error 和 pageerror 均为 0；产物预览与地图截图已人工查看 |

终端边界检查不使用 CMD 的 `%errorlevel%` 作证明：该值保留了此前 Ctrl+C 的状态。证据是“拒绝访问”的实际输出和目标文件不存在。

## 确认的问题

### 1. 审批后的模型说明与执行事实相反——建议下一项优先处理

第二轮审批 ID 为 `a33a608e24f8494b968baa9bf942809f`，会话为 `450055a6aaaa`。

实际顺序：待批 → 刷新仍待批、文件存在 → 点击允许一次 → 工具执行 → 文件消失、审批状态 approved。页面也显示“已批准”。

但模型最终回答声称“没有出现审批卡，命令直接生效”，并把本轮审批验收说成失败。该错误同时存在于可见正文与 `final_result.summary`，不是仅凭页面猜测。

**结论**：本次执行层审批有效，结果说明不可信。不能根据模型这段话把它归因为审批绕过。

**根因未确定，未改代码**。下一步应检查审批恢复后模型实际收到的消息、工具结果及审批信息，并检查前一轮未审批的上下文是否影响回答。需要用同类任务复现并补充有针对性的验证后再修复。

证据：`evidence/11-approval-pending.json`、`12-approval-restored.png`、`approval-checks.json`、`13-final-snapshot.json`、`14-approved-but-wrong-summary.png`。

### 2. 当前审批配置与最初交接说明不一致

`local-agent-web/config.yaml:125` 的 `delete_file` 为 false；第 127、128 行的 `large_batch_edit`、`overwrite_existing_file` 也为 false。`destructive_shell` 为 true。

`local-agent-web/src/local_agent/security/policy.py:660` 明确在工作区内且关闭删除审批时免审执行。当前模型提示也在 `local-agent-web/src/local_agent/context/prompt_context.py:647` 描述工作区修改、覆盖、删除无需逐次审批。

因此第一轮 `delete_file` 清理测试草稿时没有弹审批，文件进入回收站。此行为有配置与代码依据，不是本次新增终端导致的。最初交接中的“所有能力必须走审批”不能据此认定为当前实际行为；本轮未替用户改动原配置或重定权限口径。

### 3. 一轮内出现重复的完成说明

第一轮快照保留了两段内容相近的完整交付说明，第二轮也出现两段。用户会读到重复结果。现象已确认，是否需要调整主循环收尾或展示规则尚未定位，本轮未修改。

此外，模型报告对原始 clamp 故障的文字解释不准确：在 `min <= max` 时原表达式始终返回 min，并非把越界值推得更远。修复代码和测试结果正确，说明仍需依据执行证据核对模型总结。

## 证据与复验

证据目录：[本次证据](E:/tmp/chaiai-real-acceptance-20261023-01/evidence)。其中包含输入、公开会话快照、终端检查结果、页面截图及浏览器错误记录；未将设置数据库或凭据复制进仓库。

复验样本的测试与构建：

```powershell
cd E:/tmp/chaiai-real-acceptance-20261023-01/workspaces/clamp-demo
npm test
npm run build
```

重做全流程时，应选择一个尚不存在的新隔离目录，并使用现有脚本启动：

```powershell
cd E:/pylianxi/chaiai/local-agent-web
.\.venv\Scripts\python.exe scripts/live_development_acceptance.py serve --root E:/tmp/chaiai-real-acceptance-new --port 8147
```

按 `seed.json` 建立样本，加上草稿文件；通过工作区选择接口选择样本，在页面提交 `evidence/prompt.txt`。补测审批时新建 `approval-probe.txt`，提交 `evidence/approval-prompt.txt`；人工核对命令和目标后批准。该流程调用真实模型，回答内容不保证逐字重复。

验收结束已关闭测试终端、浏览器和临时服务，保留本地证据。没有重跑全库测试，没有修改前端源码，所以未重新构建产品前端。本轮不能替代一周日常使用、其他模型、并发会话或其他操作系统验收。

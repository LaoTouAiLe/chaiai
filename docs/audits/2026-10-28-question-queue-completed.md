# 挂起提问与待发队列：修复及功能验收完成

本记录取代同日 `2026-10-28-question-queue-user-scenarios.md` 的未修复结论。没有重建 exe，没有改变队列“内存保存、刷新清空”和“图片不入队”的既定边界。

## 用户可用的操作

| 场景 | 结果 |
| --- | --- |
| 本轮执行中继续写消息并回车 | 文本进入队列，本轮结束后依次发送。 |
| 上移、下移、删除 | 修改排队顺序，删除仅影响指定消息；边界按钮禁用。 |
| 编辑消息正文 | 使用独立编辑窗口；保存保持消息标识与排队位置；取消不改变正文；空内容不能保存。 |
| 编辑时本轮结束 | 编辑窗口打开期间暂缓自动发送，保存或取消后再继续。 |
| 队列达到 20 条 | 拒绝新增并保留草稿，不静默丢消息。 |
| 后台接受提交、快照尚未推进 | 根据本次返回的任务 ID 与任务索引等待，不拿旧空闲快照继续发。 |
| 收尾短暂持锁、快照版本推进 | 明确未被接受的忙碌/版本冲突请求，刷新后延迟重试；沿用幂等规则。 |
| 分支发生变化 | 保留原分支意图并暂停，不能把消息悄悄发到新分支；用户检查后点击继续才解除。 |
| 普通网络/服务错误 | 消息恢复队首、队列暂停，处理后可继续。 |
| 停止本轮 | 队列暂停；用户点击继续后，等待原执行退出再发送。 |
| worker 尚未开始执行就取消 | 同时结清已取消任务的请求台账；下一条消息不会被错误挡在人工核对态。 |
| A 自动发送中切到 B，A 随后失败 | 消息恢复 A，B 不受影响；回到 A 可以继续发送。 |
| 提问题组翻页与刷新 | 保留已经填写的答案；可一次提交全部题目或全部跳过。 |
| 提问提交失败后刷新 | 答案保留，并显示失败提示；只有后端确认结局后才清理草稿。 |
| 提问事件到达 | 及时重新读取快照，避免卡在中间的“已失效”投影。 |
| 服务进程结束并重启 | 挂起提问恢复，答复后继续同一个 run，未新建一轮或重跑原提问。 |

## 文件清单

以下路径相对项目根目录；未列出的已有、并行改动不属于本轮。

| 文件 | 修改目的 |
| --- | --- |
| `local-agent-web/frontend/src/api/queries.ts` | 按会话保留后台接受的任务 ID，供发送门控核对任务索引。 |
| `local-agent-web/frontend/src/app/LiveApp.tsx` | 任务状态门控、发送前读取最新快照、原分支保护、确定未接受请求的延迟重试、编辑暂停、提问失败提示。 |
| `local-agent-web/frontend/src/components/Composer.tsx`、`Composer.module.css` | 队列编辑入口、窗口、保存/取消及手机布局。 |
| `local-agent-web/frontend/src/stores/outbox.ts` | 增加编辑操作，保持消息 ID、顺序与会话隔离。 |
| `local-agent-web/frontend/src/features/question/QuestionCard.tsx` | 将草稿清理从点击提交移至后端确认结局。 |
| `local-agent-web/frontend/src/api/useSessionStream.ts` | 消费提问 required/resolved 事件，更新权威快照。 |
| `local-agent-web/src/local_agent/application/resolution.py`、`service.py` | 对 worker 明确跳过、从未调用执行入口的取消结清台账，并支持安全幂等重放；已执行或不确定的请求仍保留原核对要求。 |
| `local-agent-web/frontend/src/app/LiveApp.test.tsx` | 后台任务等待、版本/分支保护与队列场景回归。 |
| `local-agent-web/frontend/src/components/Composer.test.tsx` | 编辑、取消、空内容校验和编辑期间暂停回调。 |
| `local-agent-web/frontend/src/stores/outbox.test.ts` | 编辑不改变标识/顺序，拒绝空文本与已发送条目。 |
| `local-agent-web/frontend/src/features/question/QuestionCard.test.tsx` | 未确认答复保留刷新草稿，确认后清理。 |
| `local-agent-web/frontend/src/api/useSessionStream.test.tsx` | 两种提问事件的快照刷新。 |
| `local-agent-web/tests/test_ask_user_flow.py` | 服务重建恢复同一 run；worker 开始前取消后仍可继续、重放不执行。 |
| `local-agent-web/scripts/serve_question_queue.py` | 独立临时目录、确定性假模型和慢生成的场景服务，不使用真实用户库或模型。 |
| `local-agent-web/frontend/e2e/question-queue-live.spec.mjs` | 11 个真实浏览器场景，含故障注入与实际请求内容核对。 |
| `local-agent-web/frontend/e2e/setup-question-queue.ts` | 构建并直接管理隔离 Python 子进程，结束测试时关闭自己启动的服务。 |
| `local-agent-web/frontend/playwright.question-queue.config.ts` | 可单独运行的场景测试配置。 |
| 本文件及前一份场景记录 | 最新结果和历史发现的对应关系。 |

## 验证

- 构建和 TypeScript 检查通过。
- 前端全量测试曾运行 70 个文件、686 项全部通过；最后的版本冲突/分支保护补充后，相关 6 个文件、117 项再次全部通过。
- 提问、任务和路由后端测试：163 项全部通过。
- 取消与后台 API 专项：38 项通过；保留依赖里的 1 条弃用警告。
- 仓库中的浏览器配置：11 个场景全部通过，测试命令正常结束（`11 passed`）。
- ESLint：0 错误、7 条已有 Fast Refresh 警告；本轮后端文件通过 Ruff。
- 真正结束隔离服务进程、重新启动并答复旧提问：`session_state=completed`、`question.state=answered`、`same_run=true`。
- 未运行真实付费模型，未验证 WebView2/exe 宿主；旧视觉基线未更新。

浏览器测试从 `local-agent-web/frontend` 运行：

```powershell
npx playwright test -c playwright.question-queue.config.ts
```

它先构建前端，再启动隔离服务；使用项目已有 `.venv` 和本机 Playwright 浏览器。截图、HTTP 时间线和服务日志默认写入 `frontend/test-results/`。可通过 `CHAIAI_SCENE_PROOF_DIR` 指定截图目录。

手动复核：运行源码服务，执行一轮任务时连续排入 A/B/C；编辑 C 并上移，删除 B，确认随后发送修改后的 C 与 A；重复一次并按停止、继续；让模型提问后故意断开提交连接，刷新检查已填答案仍在。

本轮现场截图与证据保存在 `C:/Users/asus/.codex/visualizations/2026/10/27/01a17d34-3311-7422-b8e9-515ae56bd639/`：`user-scenes-queue.png`、`user-scenes-question.png`、`user-scenes-queue-edit-mobile.png`、`user-scenes-queue-timeline.json`。隔离进程重启证明另存 `.playwright-cli/process-restart-result.json`。

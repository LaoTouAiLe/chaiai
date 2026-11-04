# 长任务用户场景验收

2026-11-02。分批验证 23 个场景：21 个通过，2 个确认失败。本轮只增加验收代码和记录，没有修改业务源码，没有重建 exe。

## 验收方式

使用生产前端构建、真实 FastAPI、Graph、SSE 和 SQLite，模型输出由可控假模型提供。临时配置、工作区与数据库全部放在项目 `.playwright-cli/long-task-*` 下，不连接收费模型，不使用用户真实会话数据。验收服务只监听回环地址 8140～8142，结束后关闭自己创建的进程。

## 必须改

1. `local-agent-web/frontend/src/components/ScrollToBottom.tsx:66`、`local-agent-web/frontend/src/features/conversation/Conversation.tsx:746`、`Conversation.tsx:770` → 生成期间向上阅读后点击“滑到最底”，只向点击时的高度做一次平滑滚动，没有明确恢复跟随状态 → 新内容继续增长时，滚动目标落后，未进入 80px 的跟随阈值，最终仍停在回答中间 → 会话的回到底部操作需要显式恢复跟随，并滚动到当前实际尾部；保留用户再次向上滚动时停止跟随的行为。
   - 已重复复现。最近一次最终距底部 2528px，前一次 2656px；前一次 319 个采样点中，新增正文 2875 字符，但滚动停在 856px。
   - 复现：闲聊发送 `LONG_STREAM`，第 16 段出现后向上滚动，点击“滑到最底”，等待生成结束。
   - 证据：`.playwright-cli/long-task-PbvqNt/browser-proof/long-task-reading-点击滑到最底后能重新跟随仍在生成的回答-reading/` 下的 `resume-following.json`、PNG、错误上下文；另一次证据在 `long-task-SqVUhd/browser-proof/`。

2. `local-agent-web/frontend/src/features/conversation/Conversation.tsx:806` → 成功任务的处理过程折叠条件排除了所有 `memory` 行 → 真实图执行会产生“检索与记忆 · 未命中”行，即使工具成功且已有最终回答，也不会出现默认收起的“处理过程”，工具记录继续占据正文 → 区分正常检索结果与失败记录，允许安全的成功/未命中记录随处理过程收起，同时保留检索失败、工具失败、审批和待输入等需要注意的内容。
   - 已用真实 `read_file` 调用重复复现：文件读取成功、任务完成、最终长回答完整出现，但“处理过程”按钮不存在。
   - 复现：新建 Agent 会话发送 `TOOL_LONG`，等待完成；检查正文上方仍显示未命中记录和读取文件按钮。
   - 证据：`.playwright-cli/long-task-PbvqNt/browser-proof/long-task-reading-最终回答出现后过程收起，展开和收起均保留正文与草稿-reading/` 的错误上下文和 `process-settled` 截图、滚动采样。
   - 该场景在折叠断言处失败，因此之后的展开/收起断言未执行，不能宣称已经验证。

## 建议改

本轮没有额外提出范围外的重构建议。

## 不用管

- 流式中断后仍成功完成：这是明确实现的降级策略，见 `local-agent-web/src/local_agent/graph/nodes.py:965`。服务端丢弃临时投影，再用普通调用重试一次。已验证补全后及刷新后均只有一份正文，不应按“必须失败”处理。
- 外层受限进程中的命令执行错误：第一次命令场景遇到 `windows-acl-run: CreateRestrictedToken 失败：87`。在正常宿主进程环境重新运行同一隔离验收后通过，应用自身 Windows ACL 限制保持开启；该次失败不计产品缺陷。
- 一次直接设置 `scrollTop = 0` 的测试与增量更新竞态：改为真实鼠标滚轮后，“向上阅读历史不拉回底部”通过，不计产品缺陷。

## 已通过的场景

| 类别 | 数量 | 覆盖内容 |
| --- | ---: | --- |
| 等待队列和挂载提问 | 11 | 队列编辑、上下排序、删除及真实发送顺序；20 条上限保留被拒绝草稿；发送失败暂停及复用请求标识；题组翻页和刷新保留答案；全部跳过丢弃填写内容；答复失败刷新恢复；编辑时阻止抢发、空内容校验、取消和保存；手机编辑/取消/删除；停止后继续等待原 worker 退出；A/B 会话隔离；停止后不重复执行下一条 |
| 普通流式 | 3 | 命令逐行输出及刷新续接；慢流停止保留部分回答、取消终态与继续发送；真实增量出现及刷新后不重复 |
| 后台执行 | 2 | 切会话不串正文或草稿、刷新恢复、停止和继续；地图追问抽屉、草稿保留、分支及刷新一致 |
| 长回答阅读 | 5 | 60 段回答持续增量跟随、结束及刷新不重复；生成期间向上阅读不被拉回；390px 手机无横向溢出、停止保留部分回答后可续发；流式失败普通调用补全后不重复；流式和普通调用均失败后显示原因，恢复后可重新发送同一问题并刷新 |

## 本轮新增文件

| 文件 | 用途 |
| --- | --- |
| `local-agent-web/scripts/serve_reading_acceptance.py` | 可控慢流、真实工具调用、流式故障及恢复，隔离配置和数据 |
| `local-agent-web/frontend/e2e/setup-long-task.ts` | 构建前端、启动和关闭自己拥有的三个验收服务 |
| `local-agent-web/frontend/playwright.long-task.config.ts` | 聚合普通流式、后台执行和长回答阅读场景 |
| `local-agent-web/frontend/e2e/long-task-reading.spec.mjs` | 七个阅读、滚动、故障恢复、手机场景及采样截图 |
| `docs/audits/2026-11-02-long-task-acceptance.md` | 本记录 |

## 复核命令与结果

从 `local-agent-web/frontend` 运行：

```powershell
# 现有提问和队列验收，本轮 11/11 通过
npx playwright test -c playwright.question-queue.config.ts

# 完整长任务验收；目前两个已确认问题会使测试失败
npx playwright test -c playwright.long-task.config.ts

# 单独复核失败降级与恢复，本轮 2/2 通过
npx playwright test -c playwright.long-task.config.ts --project=reading -g '流式中断|连续失败'

# 命令逐行输出应在允许创建受限令牌的正常 Windows 进程中运行
npx playwright test -c playwright.long-task.config.ts --project=streaming -g '命令运行时'
```

- 最后两个故障场景：2/2 通过，49.0s，数据及浏览器证据在 `.playwright-cli/long-task-OOwRfT/`。
- 命令场景正常宿主进程复核：1/1 通过，45.5s，数据在 `.playwright-cli/long-task-IaLM85/`。
- 最近一次阅读整组复核在 `.playwright-cli/long-task-PbvqNt/`：长回答跟随、真实滚轮阅读、手机停止续发通过；两个产品问题失败。该次额外失败是错误地期待流式中断必然导致失败，已按源码策略拆分为两个独立场景，最终均通过。
- 最初组合运行在 `.playwright-cli/long-task-jBgHqb/`：普通流式的另外两项、后台两项通过。最初 Agent 用例夹具未调用显式任务完成协议，导致该用例及其后续用例无效；夹具已修正，不把这些无效失败计入产品问题。
- 新增 Python 夹具 Ruff、新增浏览器脚本/配置 ESLint 均通过。前端生产构建及 TypeScript 检查在验收启动时通过，`git diff --check` 通过（既有文件有行尾提示）。

统计为分批验收结果，并非最后一次全量运行全部通过。新增的两个产品回归测试仍然保持失败，以便后续修复时验证。

## 未验证与边界

- 未验证真实模型供应商的断流、长时间工具运行和网络重连；本轮控制的是本地假模型输出。
- 未验证打包 exe/WebView2；没有重建安装包。
- 等待队列刷新清空是现有测试约定，本轮没有增加队列持久化。
- 流式模型失败的临时片段按源码策略丢弃，不等同于用户主动停止时保留部分回答；两个行为分别验收。
- 两个确认问题仅定位并保留证据，未修改业务源码；当前状态不能宣称整体验收通过。

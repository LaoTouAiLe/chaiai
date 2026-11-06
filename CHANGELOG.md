# 更新日志

本项目的重要变化记在这里。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

安装与下载见 [Releases](https://github.com/LaoTouAiLe/chaiai/releases)。逐次提交的完整对比链接
在各版本末尾。

## [0.3.1] - 2026-11-06

### 新增

- **数据位置可以整体搬家**：换数据目录时点一次「搬到这里」，系统复制 → 校验 → 切指针，再把旧数据
  移入系统回收站；不再需要用户自己复制文件。迁移在后台执行，准备、复制、校验、切换、清理五个阶段
  都有进度；期间拒绝新回合，完成后重启生效。
- **日志落盘**：`<数据根>/logs/app.log`（5 MB × 3 个滚动文件），root 与 uvicorn 的 logger 一起挂，
  启动横幅与 `--diagnose` 都会打印路径。此前服务只往 stdout 写，双击 exe 的用户报障时手里一条记录都没有。
- **渲染异常有兜底**：界面渲染抛错时显示可复制的错误信息与「重新加载」，不再整页白屏。
- 仓库内补 `CHANGELOG.md` 与 `SECURITY.md`：此前变更只散在 Release 与提交历史里，漏洞也没有
  私密报告入口。

### 变更

- **Python 要求改为 3.13+**（原声明 3.10+）。声明此前从未被验证过：代码用了 `enum.StrEnum`
  （3.11+ 才有），3.10 上根本无法导入。`pyproject.toml`、`uv.lock`、中英 README 与 CI 现在
  是同一个版本口径。
- CI 的 `portability`（Linux / macOS 核心用例）由观察期转为**必需闸门**：连绿多轮后摘掉
  `continue-on-error`，这一路红了就是真红。
- **删除审批卡说人话**：卡面不再出现 `dir:4096:…:parent-exists:True:files:12,dirs:3` 这种机器串，
  改成「整个目录，含 12 个文件、3 个子目录；删除后可从系统回收站还原」。
- 内置浏览器可以拿来预览本地前端（右栏「浏览器」→ 地址栏输入 `localhost:5173`），README 补了说明。

### 修复

- **文件预览会串味**：切换文件时，上一份文件还在途中的"加载更多"响应会把旧正文拼进新文件的
  内容里。现在换文件（或刷新）会让在途请求作废。
- **隧道下移动端 cookie 不带 `Secure`**：tailscale serve / funnel、cloudflared 都在本机把
  TLS 解成明文再转发，网关只看 `request.url.scheme` 会永远判成 http。现在认
  `X-Forwarded-Proto`，且只认本机隧道进程（回环对端）转发的值。
- **委派卡取消后无限轮询**：任务取消/失败时"进行中"的子任务列表不会被清空，页面会每 1.5 秒
  请求一次 `/api/delegation/live` 直到关页。现在按行状态在结束后停掉。
- **验证提醒会吃掉最后一格决策额度**：只剩最后一次决策时提醒会把额度用掉，下一轮撞上限，
  一次正常收尾被判成失败。现在剩余额度不足一格时不提醒。
- **浏览器启动失败会连环重试**：每次重试都再起一个浏览器进程，Windows 上连弹崩溃对话框，一轮能卡
  十几分钟。现在失败后 120 秒内直接拒绝，并把子进程输出与配置目录带进错误。
- **预览并发槽只护住 Office 转换**：3 个 32 MB 的 PDF 能同时整份读进内存，表格解析完全不受限。
  现在两类预览共用同两格槽。

## [0.3.0] - 2026-11-05

### 新增

- 移动访问：网关（配对码换设备令牌、反向代理）、局域网与 Tailscale 两条通道，手机浏览器
  通过配对码接入同一套网页。
- 屏幕能力：查看当前前台窗口、按窗口授权模拟输入，默认关闭。
- 文档预览与「打开方式」：PDF 用 `pdfjs-dist` 渲染，Office 由 `preview-runtime/` 转换。
- MCP 支持 Streamable HTTP（stdio 与 http 两种连接方式）。
- 会话轮次全文索引（v17），列表搜索覆盖回答正文；新增 `session_search` 工具与
  `session_history` 能力（默认关）。
- 浏览器截图按 vision 预算降采样；AI 回复正文补齐代码、表格、图片与工具结果的呈现。
- POSIX 沙箱：landlock 兜底、macOS seatbelt profile 分块，三平台 CI 冒烟。

### 变更

- 子 Agent 第二档可写，`run_shell` 保持只读边界。
- 沙箱边界不再静默失败，可配置为强制拒绝。
- 数据中台改用用量账；委派失败带真实原因，写盘次数上页面，压缩摘要失败重试一次。

### 修复

- 移动网关先合并设置快照再判断 `enabled`（设置页开了却不启动）。
- 浏览器跨源导航后 CDP 会话失效。
- 冻结包收集 `uiautomation` / `comtypes`；IPC 源文件哈希按 LF 重算。
- 终端 IPC 误扫描 CLR 管理的 IL-only 程序集导致加载重入。

## [0.2.0] - 2026-10-29

### 新增

- 结构化提问通道：任务挂起 → 答复 / 跳过，支持题组；页面有提问卡与待发消息队列。
- 官网补 GitHub 下载入口，18 个文档页顶栏同步。

### 变更

- README 拆为中英双语，补发布说明模板。
- 错误文案统一。

## [0.1.1] - 2026-10-27

仓库的第一个发布标签。此前的变化没有逐条整理，完整提交列表见上面的对比链接。

[未发布]: https://github.com/LaoTouAiLe/chaiai/compare/v0.3.1...master
[0.3.1]: https://github.com/LaoTouAiLe/chaiai/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/LaoTouAiLe/chaiai/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/LaoTouAiLe/chaiai/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/LaoTouAiLe/chaiai/commits/v0.1.1

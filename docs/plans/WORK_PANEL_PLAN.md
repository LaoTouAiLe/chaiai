# 右栏工作面板（多标签）架构与实施计划

> **状态**：2026-10-21 立项，**多标签容器尚未开工**。文件变更链路已独立实现并完成真实会话验收（见 §13）；其余架构方案和待决策项见 §12，不代表已批准实现。
> **需求来源**：用户 2026-10-21 提供的两张对照图（CodeBuddy 右栏形态）。要求：顶栏右上角一个开关按钮；右侧一块独立面板；面板内是**多个并列标签页**；起始页六项入口**顺序照抄**：文件、任务摘要、侧边对话、浏览器、终端、代码变更。
> **用户已明确的语义**：终端 = 启动真正的本地终端，体验对标 IDEA / VS Code 的「新建终端」；侧边对话 = 针对当前上下文另开一段对话（类似分支），**不是**独立新会话。
> **效力**：本文件负责该特性的架构与实施顺序。与 `ARCHITECTURE.md` 冲突时先改文档再编码；落地每个阶段前，先把该阶段涉及的稳定契约写进 `ARCHITECTURE.md`。

## 1. 范围

### 做什么

1. 顶栏右上角新增面板开关（对应截图中红框位置），面板可折叠/展开。
2. 面板支持**多标签**：`+` 新建、逐个关闭、切换；同一类型允许多开（终端、侧边对话必须支持）。
3. 起始页六个入口，顺序照抄：文件、任务摘要、侧边对话、浏览器、终端、代码变更。
4. 六类标签内容全部接真实数据，不做静态占位。

### 这次不做（明确边界）

- **不做**标签拖拽排序、拖拽分隔条调整宽度、标签跨设备/跨会话同步。
- **不做**把现有「对话 / 轨迹 / 会话地图」迁进右栏——它们仍是中栏页签，互不影响。
- **不做**文件编辑与保存：文件标签**只读**（预览）。写文件仍只能经 Agent 工具 + 审批。
- **不做** `git commit` / `git push`：代码变更标签**只读**（status + diff）。
- **不做**多终端分屏、终端录制回放、shell 集成（命令块识别）。
- **不做**把终端暴露到非回环地址或加远程访问：仍受 `LocalBoundaryMiddleware` 约束。

## 2. 现状核实（决定工作量的事实，全部已核对源码）

本节保留立项时的核实记录；2026-10-21 文件变更链路的当前实现与验收结果见 §13。

| 标签 | 现状 | 证据 |
|---|---|---|
| 任务摘要 | **有现成端点** `GET /api/tasks` | `local-agent-web/src/local_agent/api/routes.py:419` |
| 文件 | **无端点**。能力只存在于工具层 | `tools/file_tools.py:49`（list）、`:146`（read）、装配 `:505` |
| 代码变更 | **无端点**。`git_tools` 只在工具层 | `tools/git_tools.py:388`（status）、`:423`（diff）、`:593`（装配） |
| 终端 | **无端点，且无 pty**：现有会话是 subprocess 三管道 | `tools/terminal_tools.py:33`、`:159`（自述"不提供全屏终端仿真"）、`security/process_guard.py:198-201` |
| 浏览器 | **无端点**。`browser` 工具经 CDP 控制外部 Chromium | `tools/browser_tools.py`（capability `page_control`，`capabilities.py:79`） |
| 侧边对话 | 有分支能力，但 **fork 会抢 active**、锁是会话级 | `persistence/conversation_store.py:97`、`:102`；`application/service.py:3815-3821` |

HTTP 面只有两个 router（`api/app.py:98-100`：`routes.py:233` 业务路由 + `sse.py:124` 事件流），`api/` 目录**不 import 任何工具模块**。所以除任务摘要外，其余五类都要新增对外端点。

## 3. 与既有裁定的冲突（必须先承认）

| # | 既有裁定 | 本次要求 | 处理 |
|---|---|---|---|
| 1 | v1.0 明确「**不实现全屏终端**」 | 要 VSCode 式真终端 | 视为**撤销该限制**，需要用户显式确认（§12 决策 1） |
| 2 | 只有 SSE 一条单向流（`sse.py`），50ms 轮询泵（`:41`、`:112`） | 终端需要双向低延迟 | 新增 WebSocket 端点，见 §6.3 |
| 3 | 每条 shell 动作走 `prepare_shell` + 指纹 + 审批（`shell_tools.py:107-147`、`approval.py:105`） | 交互式终端无法逐条审批 | 终端改为「启动时判定一次 + 审计记录」，见 §6.4 |
| 4 | 分支 active 全表互斥（`conversation_store.py:97`），快照按 active 读（`service.py:3202-3204`） | 侧边对话不得抢走主对话视图 | 分支模型改造，见 §7 |
| 5 | 快照无分支列表，只有 `branch_id/branch_title`（`models.py:1044-1045`） | 侧边对话需要并行视图 | 新增 `view_branch_id`，见 §7 |

## 4. 状态归属

| 状态 | 归属 | 依据 |
|---|---|---|
| 面板开合、标签列表、顺序、激活标签 | 前端 Zustand（新增 `stores/panel.ts`） | 与 `stores/ui.ts:1-10` 同口径：刷新即忘的界面状态不进服务端 |
| 文件树、文件内容、git 变更 | React Query，key 带 `workspaceId` | 服务端权威 |
| 终端进程与输出缓冲 | **服务端**持有；前端只有 `terminal_id` + xterm 本地回滚 | 刷新页面不应杀掉 shell |
| 侧边对话分支 | 服务端（分支表 + 新 `view_branch_id`） | 见 §7 |

标签状态**不持久化**（刷新回起始页）；终端标签例外：刷新后按 `terminal_id` 重连，进程仍在则继续，否则显示"已结束"。

## 5. 前端结构

```
frontend/src/
  components/TopBar.tsx            # 右上角新增面板开关（现有 tabs 在 :27-56）
  features/panel/
    PanelShell.tsx                 # 容器：折叠、宽度、与中栏的 flex 关系
    PanelTabBar.tsx                # "+"、关闭、role=tablist
    StartPage.tsx                  # 六入口，顺序照抄
    registry.ts                    # kind -> { 标题, 图标, 懒加载组件, 依赖(workspace/session) }
    tabs/{Files,Tasks,SideChat,Browser,Terminal,Changes}Tab.tsx
  stores/panel.ts                  # tabs[{id,kind,params}]、activeId、collapsed
```

约定：

1. 标签身份是 `id`，不是 `kind`——同 kind 多开靠不同 `id` + `params`。
2. 六个标签组件全部 `lazy` + `Suspense`；`@xterm/xterm` 只在终端标签加载（不进首屏包）。
3. 每个标签带自己的 `adapters.ts`：DTO → view model，组件不直接认 `schema.gen.ts` 类型（沿用现有约定，见 `features/conversation/adapters.ts`）。
4. 布局：`app/App.module.css:1` 的 `.app` 目前是两栏 flex，改为三栏；右栏 `flex: 0 0 auto; width: clamp(320px, 28vw, 560px)`；`max-width: 1024px` 时改为覆盖式抽屉（与 `LiveApp.tsx:62` 的窄屏判定对齐），避免把中栏压出横向滚动。
5. 开关按钮状态与 `topbar` 现有 tab 并存，不合并。

## 6. 后端：文件 / 变更 / 终端

新增三个模块，均挂 `/api` 前缀，注册在 `api/app.py:98-100` 旁边：

### 6.1 文件（只读）

| 端点 | 返回 | 约束 |
|---|---|---|
| `GET /api/workspaces/{id}/files?path=.` | 条目数组：`name` / `kind`(file/dir) / `size` / `modified` / `ignored` | 目录深度单层，不递归；单目录上限 2000 条，超出标记 `truncated` |
| `GET /api/workspaces/{id}/file?path=&max_bytes=` | `content` / `encoding` / `truncated` / `binary` | 默认上限 256 KB；二进制（含 NUL 或非 UTF-8 可解码）返回 `binary: true` 且不带内容 |

- 路径校验走 `security/path_guard.py:332` 的 `resolve_target` + `ExternalPathPolicy`（`:270`），越界返回既有 `WorkspaceBoundaryError` 对应的错误码，不新造判断。
- 只读：不提供任何写接口，`file_tools.write_file_content/edit_file_content/delete_file_target` 不对外暴露。

### 6.2 代码变更（只读）

| 端点 | 返回 |
|---|---|
| `GET /api/workspaces/{id}/git/status` | 路径 / 状态码 / staged 标记，复用 `git_tools.status`（`git_tools.py:388`）的解析结果 |
| `GET /api/workspaces/{id}/git/diff?path=&staged=` | unified diff 文本 + 截断标记 |

- 复用 `git_tools.run_git`（`:139`）的白名单与 `_assert_safe`（`:182`），**不暴露 commit 分支**。
- 非 git 工作区返回 `available: false`，页面显示"此工作区不是 git 仓库"，而不是报错。
- 刷新时机：订阅现有 SSE 的 `task_completed`（`application/events.py:70-95`）后 invalidate 对应 query，不做定时轮询。

### 6.3 终端（WebSocket）

| 端点 | 说明 |
|---|---|
| `POST /api/terminal` | 建终端：`{workspace_id, cols, rows, shell?}` → `{terminal_id, cwd, shell}` |
| `WS /api/terminal/{terminal_id}` | 双向：客户端→服务端发输入字节、`resize` 控制帧、`ping`；服务端→客户端发原始输出字节 + `exit` 帧 |
| `DELETE /api/terminal/{terminal_id}` | 主动结束（关标签时调用） |

分层：

1. **pty 抽象** `src/local_agent/host/pty_backend.py`：`open(argv, cwd, env, cols, rows) -> PtyHandle`（`read/write/resize/poll/close`）。Windows 用 `pywinpty`（ConPTY），POSIX 用标准库 `pty`。**新增运行时依赖**（当前 `pyproject.toml:12-33` 无任何 pty 库）。
2. **帧协议**：JSON 控制帧（`resize` / `exit` / `error`）+ 二进制数据帧（原始字节，不做 UTF-8 假设）。解码交给前端 xterm，避免 GBK/UTF-8 混排破坏字节流（现有 `tools/output_decoding.py` 的启发式解码只适用于一次性文本结果）。
3. **生命周期**：会话登记在一个进程内注册表（与 `tools/terminal_tools.py:164` 的 `RESOURCES` 分开，避免 Agent 工具与用户终端互相取消）；空闲默认 30 分钟关闭；服务退出时全部 terminate 进程树。
4. **并发**：每终端一个读线程 → asyncio 队列（`run_in_executor` 或独立线程 + `call_soon_threadsafe`），不在事件循环里阻塞读。

### 6.4 终端安全（本次最大的语义变化）

交互式终端里用户敲的每条命令**无法逐条弹审批**。因此把"授权粒度"从命令级降到**终端级**：

1. 建终端时走一次 `shell_guard.prepare_shell`（`shell_guard.py:284-294`），决定 shell 可执行文件、cwd、env 白名单；
2. cwd 锁在会话工作区内；Windows 写侧沙箱沿用 `policy.shell_sandbox_request(...)`（与 `terminal_tools.py:58` 同路径）；
3. 启动参数用 `approval.action_fingerprint`（`approval.py:105`）记一条审计记录，含 shell、cwd、沙箱档位；
4. 危险可执行文件（`shell_guard` 已拦截的项）在启动时拦，运行期不再逐条拦。

两种模式待选（§12 决策 1）：

- **受控（建议默认）**：cwd 锁工作区 + 写侧沙箱 + 启动判定；
- **直通**：等同用户自己开 cmd/PowerShell，仅启动前确认一次。

## 7. 侧边对话（旁路分支）

事实链：`fork` 建分支时把旧分支 `active=0`、新分支 `active=1`（`conversation_store.py:97`、`:102`）；`_read()` 按 active 分支的 checkpoint 读快照（`service.py:3202-3204`）；执行锁按 `session_id`（`service.py:3815-3821`）；分支校验在 `conversation.py:136-152`，建分支在 `:155-167`。

因此直接复用 `parent_turn_id` 提交会**立刻把主对话切到新分支**，且两边不能同时跑（`SESSION_BUSY`）。要真"侧边"，需要四处改造：

1. `fork(..., activate=False)`：旁路分支建立后不切 active；
2. 快照读取参数化：新增 `view_branch_id`，`GET /api/sessions/{id}` 支持按分支读；主对话仍读 active，`branch_id/branch_title` 语义不变（`models.py:1044-1045`）；
3. 执行锁键从 `session_id` 细化为 `(session_id, branch_id)`，允许主/侧并行；会话地图"当前分支"仍以 active 为准（`models.py:1098-1112` 不变）；
4. 事件按分支过滤：`EventBus` 目前按会话广播给所有订阅者（`service.py:377-386`），订阅侧必须带 `branch_id` 过滤，否则侧边对话的 `message_delta` 会串进主对话。

降级选项（不做上述改造）：侧边对话退化为"切分支对话"——用户一提问主对话就切走，与需求不符。所以若坚持第 4 条语义，这四处是必需的。

## 8. 浏览器

最小可用（推荐）：URL 输入 + iframe 尝试渲染 + 被 `X-Frame-Options / CSP` 拒绝时给出可执行提示（"在系统浏览器打开"）+ 记住最近 URL。**无需后端**。

重量级选项：镜像 `browser` 工具控制的那个 Chromium（CDP `Page.captureScreenshot` → WS 推帧 + 点击坐标回传）。成本最高，且与"不扩展到桌面控制"的既有裁定相邻，建议单独立项。

## 9. 契约改动点（先写进 `ARCHITECTURE.md`，再编码）

| 节 | 要写进去的东西 |
|---|---|
| 新增 §23 | 右栏工作面板：六标签语义、标签状态归属、端点清单、终端帧协议、安全粒度 |
| §6.9 能力闭集 | 面板端点**不是 Agent 能力**，不进 `capabilities`（`capabilities.py:53-79`）；终端与 `terminal` 能力是两条独立通道，不得合并 |
| §15.3 HTTP 与 SSE | 增加 WebSocket 例外条款：只用于终端字节流，其他实时仍走 SSE |
| §16.4 Git 能力 | 明确 REST 只读与 `git_write` 档位的边界 |
| §19.2 断线语义 | 终端断线行为：进程保留、重连续传缓冲；与 SSE"重连不重发"区分开 |
| §20.3 脱敏口径 | 终端输出可能含密钥：审计只记启动参数，不落终端字节流到日志/长期记忆 |

## 10. 任务拆分（每阶段独立可验收）

### P0 面板骨架（纯前端）

- 目标：开关能用，标签能开能关，任务摘要可用，其余五类显示明确"未接入"空态。
- 落点：`TopBar.tsx`、`features/panel/*`、`stores/panel.ts`、`App.module.css`、`LiveApp.tsx`。
- 验收：点按钮面板开/合；`+` 开出同类两个标签且互不干扰；任务摘要数字与左侧角标一致；`npm run build` 后从服务访问可见（服务发的是 `frontend/dist`）。

### P1 文件与代码变更（只读）

- 落点：`api/workspace_files.py`、`api/workspace_git.py`、`schemas` 新模型、前端两个标签 + adapters。
- 验收：文件内容与磁盘一致；越界路径返回明确拒绝（非 500）；非 git 工作区给 `available:false`；Agent 跑完一轮后变更标签自动刷新。

### P2 终端（依赖 §12 决策 1）

- 落点：`host/pty_backend.py`、`api/terminal.py`（WS）、进程注册表、前端 xterm 标签。
- 验收：`dir` / `python -c "print(1)"` / 进入交互式 REPL 均正常；拖动窗口后 `resize` 生效（`stty size` 对得上）；关标签后进程树被回收，无残留；刷新页面后进程仍活且缓冲可续。

### P3 浏览器（前端）

- 验收：可输入 URL 并渲染；被拒时提示可照做；切换标签不重新加载同一 URL。

### P4 侧边对话（依赖 §12 决策 2）

- 落点：`conversation_store.fork(activate=False)`、快照 `view_branch_id`、锁粒度、事件分支过滤、右栏对话标签。
- 验收：侧边提问时主对话视图**不切换**；主/侧可同时运行且事件不串；会话地图的"当前分支"仍等于主对话分支。

## 11. 风险与未核实项

1. **WebSocket 可用性未核实**：`pyproject.toml:12-33` 依赖表里没有 `websockets`，`uvicorn` 是否带 ws 实现取决于安装 extras。落地 P2 前必须先实测一个 `WS /api/ping`，不通则改 `SSE 输出 + POST 输入` 方案（代价：回显延迟与字符吞并）。
2. **`pywinpty` 打包**：涉及 `pyproject.toml:57-59` 的 native 资产与 `packaging/` 流程，需在打包机上验证一次，不能只在本机源码模式通过。
3. **Windows 编码**：终端的字节流不能用现有 `OutputDecoder`（`tools/output_decoding.py:41-42`）的启发式解码，必须原样透传，否则 `chcp 65001` 前后会出现乱码。
4. **前端类型生成**：每个新端点都要重新生成 `frontend/src/api/schema.gen.ts`，否则 schema 漂移。
5. **终端与审批的一致性**：终端页签是新增的一条"不经逐条审批的本机执行通道"。这一点必须在安全章节写明，不能只在代码注释里。

## 12. 待拍板（决定后才开工对应阶段）

2026-10-23：用户已确认启动 P2，撤销「不做全屏终端」限制，采用受控模式：启动审批一次，后续手动输入不逐条审批，保留工作区写侧沙箱。具体冻结边界见 `ARCHITECTURE.md` §23；Agent 工具原审批不变。首版 Windows 使用系统 ConPTY 直接衔接既有受限令牌，无需 pywinpty；不支持沙箱的平台明确不可用。本次不实施 P3/P4。

| # | 决策 | 选项 | 影响 |
|---|---|---|---|
| 1 | 终端安全模式 | **受控**（cwd 锁工作区 + 写侧沙箱 + 启动判定，建议） / **直通**（等同本机 shell，仅启动确认一次） | P2 实现与安全章节 |
| 2 | 侧边对话模型 | **改造分支模型**（§7 四处，真并行） / **降级为切分支**（不改模型，但会打断主对话） | P4 是否存在 |
| 3 | 浏览器标签 | **iframe + 系统浏览器兜底**（无后端） / **CDP 截图镜像**（重量级） | P3 成本 |
| 4 | v1.0「不做全屏终端」限制 | 确认撤销 / 保留（保留则 P2 不做） | P2 是否存在 |

## 13. 文件变更链路真实会话验收（2026-10-21）

本节对应交接清单的 **P0 端到端验收**，与 §10 的原始阶段编号不同；多标签骨架仍按交接顺序放到最后。

### 范围与运行环境

- 使用当前源码、现有配置与 `frontend/dist` 启动本机服务 `http://127.0.0.1:8000`；启动前 8000 / 5173 均未监听，启动后健康检查通过且 `model_configured=true`。未重新构建前端、未替换 EXE。
- 使用真实已配置模型、独立工作区 `local-agent-web/data/acceptance/p0-file-changes-20261021/workspace` 和会话 `788849a4a6b9`；复用正式聊天库，沿用现有记忆设置，因此验收对话也产生了记忆写入。
- 当前界面为**对话中的逐轮文件列表 + 点击后右栏整文件 diff**；本轮未验收或补建独立右栏总览／按轮切换器。

### 已通过

1. 预置 `sample.txt` 三行 UTF-8 基线；第一轮真实模型通过 `edit_file` 将 `status=before` 改为 `status=after` 并追加一行。工具行和默认收起的摘要均显示 `已编辑 1 个文件 +2 −1`，展开只有一个文件条目，点击显示基线到当前内容。
2. 第二轮通过 `write_file` 完整覆盖同一文件，将状态改为 `final` 并追加一行。工具输出的 `diff` 非空，工具行和该轮摘要仍为 `+2 −1`。
3. 两轮汇总为 1 个文件、累计 `+4 −2`；右栏整文件 diff 为首次 `before` 到当前 `final` 的净差异 `+3 −1`，保留两轮新增内容，未把第二轮前状态覆盖成新基线。
4. 数据库只读核对：该会话 2 条 `file_change_ops`、1 条 `file_change_baselines`；基线 SHA-256 与 diff 的 `before_sha256` 相同，磁盘文件 SHA-256 与 `after_sha256` 相同，文件末尾换行保留。
5. 刷新后两轮摘要及工具增删行数恢复，两个摘要默认收起。当前窄窗的 diff 是覆盖式预览，关闭预览后可以继续发送消息。

### 证据与复验

原始证据保存在 `local-agent-web/data/acceptance/p0-file-changes-20261021/`（本机数据目录）：`session.json`、`final-snapshot.json`、`final-changes.json`、`final-diff.json`、`verification.json`、`round1.png`、`round2-diff.png`、`after-refresh.png` 及启动日志。

复验：打开本机服务 → 展开侧栏并选择 `workspace` 下标题以“这是独立 P0 文件变更验收”开头的会话 → 展开任一“已编辑 1 个文件”摘要 → 点击 `sample.txt` → 核对右栏 `status=before` 删除、`status=final` 新增及 `+3 −1` → 刷新后再次展开检查。接口可直接读取 `/api/sessions/788849a4a6b9/changes` 与 `/api/sessions/788849a4a6b9/changes/diff?path=sample.txt` 对照。

本轮没有修改业务源码，没有重跑单测或编译；上述结论来自真实模型、浏览器、接口和数据库核对。`delete_file`、无基线旧会话、大文件上限及长命令转后台未做本轮端到端验收。P1 检索优化仍需先决定分页／游标契约，未提前推进 P1–P6。

## 14. 简单改页耗时排查与首批修复（2026-10-21）

用户指定以当天“哈哈”会话 `6facc1152bca`、11:21 带参考图修改前端初始页的记录作为性能案例。
详细只读排查、原始快照、checkpoint 边界归因和修复探针保存在
`local-agent-web/data/diagnostics/haha-timing-20261021/REPORT.md` 及同目录证据。

### 本次改动与原因

| 文件（相对仓库根） | 改动 |
|---|---|
| `local-agent-web/src/local_agent/security/process_cleanup.py` | 新增只支持当前 Job 内 PID 的进程树清理，拒绝外部作业、自身/祖先和远程/进程名选择器 |
| `local-agent-web/src/local_agent/security/win_ipc.py` | 受保护组件缓存生成并锁定 taskkill.cmd |
| `local-agent-web/src/local_agent/security/process_guard.py` | 仅为沙箱命令前置兼容入口；源码直接用基础 Python 避免 venv 嵌套 Job |
| `local-agent-web/src/local_agent/startup.py` | EXE 的清理辅助入口在 API 装配前分流 |
| `local-agent-web/src/local_agent/performance.py` | 新增 monotonic span、并行去重分项与 JSONL 写入，日志失败不阻断任务 |
| `local-agent-web/src/local_agent/api/container.py` | 接线本地计时日志路径 |
| `local-agent-web/src/local_agent/application/service.py` | 记录图执行范围与运行上下文装配 |
| `local-agent-web/src/local_agent/graph/builder.py` | 保留节点边界计时，并标出压缩/工具阶段 |
| `local-agent-web/src/local_agent/graph/nodes.py` | 标出决策请求组装和估算区间，不改变压缩策略 |
| `local-agent-web/src/local_agent/llm/usage_recording.py` | 记录真实模型调用区间 |
| `local-agent-web/src/local_agent/persistence/checkpoint.py` | 记录 saver 读取、序列化/锁等待/写入的完整边界 |
| `local-agent-web/tests/test_process_cleanup.py` | 参数边界、真实子孙清理、外部作业与祖先拒绝 |
| `local-agent-web/tests/test_performance.py` | 重叠区间、checkpoint 后台线程、日志故障与新轮次归属 |
| `local-agent-web/tests/test_windows_ipc.py` | 补上兼容脚本不可改写的验收 |
| `local-agent-web/tests/test_startup.py` | 确认清理入口不启动 API |
| `local-agent-web/src/local_agent/native/windows/README.md`、`ARCHITECTURE.md`、`README.md`、本文件 | 同步兼容范围、计时口径与验证结果 |

根因对照：普通进程的 Playwright 清理探针 0.58 秒；受限令牌内系统 taskkill 返回拒绝访问，
即使工作区可写也等到子进程 10 秒自行退出。兼容入口使用现有进程访问权限，按当前 Job 校验目标，
没有退出沙箱、增加令牌权限或修改进程 ACL。修复后同一受限探针 0.89 秒完成。

### 验证与实际结果

- 原生进程/IPC/启动定向回归 60 passed；计时/用量/流式/checkpoint/启动定向回归 58 passed。
- 最终增加祖先拒绝、EXE 入口分流与轮次归属校验后，清理/计时/启动定向回归 28 passed。
- 原六个 Playwright 空页面用例：**6 passed**，构建、测试与收尾合计 **32.34 秒**；
  独立新进程的首次沙箱物化 **23.04 秒**另计，完整探针约 55.37 秒。webServer 收尾日志间隔约 **0.287 秒**。
  使用 `--update-snapshots=none`，没有更新截图基线。
- 正常宿主令牌下工具回归 **105 passed / 1 skipped**。必须从正常宿主令牌运行 Windows 原生测试；
  在 Codex 外层受限令牌中，嵌套创建受限令牌会导致 Shell 测试环境失败。
- 全量源码 mypy 检出 **14 条既有错误**；使用开工前工作区备份做 shadow-file 对照，错误完全相同，
  本轮没有新增。Ruff 对本轮源文件及测试通过。
- 后端组合回归为 351 passed / 1 skipped / 11 failed；其中 10 条是上述外层令牌环境问题，
  正常宿主下复验工具套件已通过。剩余 `test_openapi_surface_is_controlled` 的旧白名单缺少 P0 已有的
  changes、changes/diff、commands/detach 三个端点；用开工前模块备份复现相同失败，按范围要求只记录，不修改。

源码服务已重启，健康检查通过；会话 `1954b92be6de` 两轮只读验证生成
`data/metrics/turn-timings.jsonl`。第一轮 trace 8.07 秒；最终接线后的第二轮 trace 9.72 秒，
分项为 context_build 4.72、model 4.05、tool 0.016、checkpoint 0.296、other 0.632 秒，
五项之和与 trace 总时间一致。第二轮摘要使用新 run_id，未被输入 checkpoint 的上一轮 id 覆盖。
这些是图执行范围的计时样本，不是简单改页任务整体性能已经验收的结论。

### 边界与下一步

- 当前修复覆盖未限定路径的 taskkill PID 清理；显式系统 taskkill.exe 路径保留系统行为。
- EXE 清理分流有单测，**未重新打包或做 EXE 实测**；本轮交付为源码服务。
- 当前 search_tools.py 已有部分 rg 快速路径，本次没有修改它、没有简化分页/游标契约；
  后续检索优化需先验证原有预算、过滤、顺序和续页行为。
- 下一步使用新计时比较原慢案例的代表性轮次，再决定上下文重复计数与 grep/list_dir 的具体优化。

## 15. grep 契约修复与路径解析优化（2026-10-21）

保留分页、游标、过滤、同级轮转和预算契约。现有 rg 快速分支导致原有 72 项检索测试中
17 项失败，涉及编码、长行、续页及扫描顺序，本轮已撤下；RAG 使用 rg 的路径及打包定位不变。

| 文件 | 本轮改动 |
| --- | --- |
| `local-agent-web/src/local_agent/tools/search_tools.py` | 移除不兼容的 rg 分支；每页复用已解析的展示根及候选路径 |
| `local-agent-web/src/local_agent/security/path_guard.py` | 两处比较本次已解析的真实路径，避免内部再次 realpath |
| `README.md`、本计划 | 更新结果与验证边界 |

根因采样：第一条历史查询扫描 167 文件，Path.resolve 3018 次、累计约 1.40 / 2.50 秒；
修改后调用数 1345 次。没有跨请求路径缓存，I/O 与游标版本校验仍走策略后端。

对“哈哈”原始 15 个参数，以当前前端文件做三组改前/改后交替对照：完整契约的 Python
基线总耗时中位数 **13.36 → 7.06 秒（减少 47.12%）**；全部返回字段逐项一致。
该结果不能直接与中午历史的 48.46 秒作性能对照。

检索契约 72 passed；正常宿主兼容性与工具 201 passed / 1 skipped；路径策略专项
64 passed / 2 skipped / 5 deselected / 1 既有失败。既有失败为工具清单漏列后台命令工具，
已用改前备份复现，未顺手修改。本轮两份源码 Ruff / mypy 通过，未声称全量后端通过。

源码服务重启后，真实模型 run `a34a5cd634734cb2ba619b9e0397009a` 三页检索依次返回
1–2、3–4、5 行；checkpoint 核对游标逐页传递正确，文件未变，工具各耗时 15–18 ms。
图计时 17.08 秒，其中 model 8.62、context_build 6.58、checkpoint 0.62、tool 0.055、other 1.21 秒。

完整证据、复现命令、原文件备份和检查限制：
`local-agent-web/data/diagnostics/search-performance-20261021/REPORT.md`。
本轮没有修改 list_dir、没有重跑完整带图片的改页任务、没有更新 EXE。

## 16. 原图改页完整任务复测（2026-10-21）

用户明确将“改页并测试，10 分钟都顶天”作为验收目标。本轮用当前源码的完整独立副本，
新会话、原图、原请求与当前默认 deepseek-reasoner 连接跑完整任务。不是中午修改前工作区的精确重放。

**验收失败**：会话 `8c1c848d6d74`，run `1f0ebcf711dd4c39816df538d652bce9`，
HTTP 613.21 秒取消完成。截止检查约 15 秒粒度；未继续等待到 50 分钟。
已做部分前端修改及 typecheck，但完整构建、相关测试与最终视觉验收尚未完成。

| 图内互斥分项 | 耗时 |
| --- | ---: |
| 模型 | 240.43 秒 |
| 上下文构建 | 219.28 秒 |
| 工具 | 91.78 秒 |
| checkpoint | 24.16 秒 |
| 其它 | 37.34 秒 |

66 次模型调用、106 次工具事件；首次成功编辑在 4 分 37 秒。
18 次 edit_file 有 8 次格式／精确匹配失败；另有浏览器连接超时与两次 Playwright 路径配置失败。
模型生成的 CSS `.mainWelcome` 与 TSX `styles.welcomeMain` 不一致，导致背景没有生效，截止时仍未修好。
本轮最长工具仅 13.36 秒，未进入原六个 Playwright 用例，不能把此次失败归给此前的 900 秒测试收尾问题。

上下文成本集中于 compression_node（67 次，原始 span 累计 141.69 秒）与 decision_prompt（75.63 秒）；
估算输入从 17,767 增至 139,584 token。对最终消息的只读等价计数原型，中位数
0.5013 → 0.0081 秒，约快 61.7 倍；全部 Unicode 码点与真实文本计数一致。
这是局部操作的数据，**尚未接入正式源码，不代表完整任务已提速或验收通过**。

正式业务源码没有改动；679 份复制源文件哈希复核不变。七份未验收的页面改动只在测试副本中。
完整证据、脚本、文件清单和后续验证方案见：
`local-agent-web/data/diagnostics/page-task-20261021/REPORT.md`。
下一项优先建议为保持预算与出站语义不变的 token 估算加速，然后重新做完整任务验收。

## 17. 等价 token 估算加速与图片顺序修复（2026-10-22）

已接入等价宽字符计数加速；保持全部 Unicode 范围、预算公式、取整、模型与上下文阈值。
实际最终 177 条消息旧/新都为 111,809 token，三组交替计数中位数 **0.517007 → 0.007380 秒**。
这是局部操作约 70 倍提速，不能当作完整任务的速度比。

首次接入后会话 `ce337e9269cf` 在 27.74 秒失败，未编辑文件。服务端 400 根因为同批两次
view_image 的内部图片用户帧插在工具回复之间，违反工具调用配对顺序；与计数修改无关。
出站适配现将完整批次的图片帧排在全部工具回复之后，兼容旧历史，不修改 State 或图片内容，
不跨真实用户发言，不补造缺失回复。实际失败 checkpoint 只读回放已通过。

| 文件 | 改动 |
| --- | --- |
| `local-agent-web/src/local_agent/context/token_budget.py` | 编译原有范围，用正则引擎扫描宽字符 |
| `local-agent-web/src/local_agent/graph/nodes.py` | 出站副本中修正完整工具批次的内部图片帧顺序 |
| `local-agent-web/tests/test_token_budget.py` | 全 Unicode 码点、混合文本及预算边界 |
| `local-agent-web/tests/test_image_refs.py` | 双图复现、历史不变与真实用户/缺失回复边界 |
| `README.md`、本计划 | 同步接入与完整任务验收结果 |

定向回归 **209 passed / 2 skipped**；Ruff 通过，token_budget.py mypy 通过。
nodes.py 的 `_split_image_blocks` 一条既有 mypy 错误，用改前备份 shadow-file 对照相同。
较早组合回归 177 passed / 2 skipped / 1 failed；预算套件的一条失败已用改前估算器复现，未修改。
未声称全量检查通过。

源码服务已重启，健康检查通过。再次完整任务使用新会话 `2f27c1e8f2d8`、独立副本，
**679 份起始文件与前次逐一哈希相同**；原图、原请求与默认 deepseek-reasoner 连接不变。
该副本的页面修改不合并回正式前端，EXE 未重新打包。

本次单样本时间目标达到：**HTTP 455.50 秒（7 分 35 秒）完成**，首次页面写入在第 88.19 秒。
图内 model 190.57、tool 228.44、checkpoint 12.30、context_build **7.04**、other 16.04 秒。
6 次 grep 合计 1.758 秒，4 次 list_dir 合计 0.653 秒；本样本检索不是大头。
前端 typecheck、469 个单测、相关页面用例通过；全量 lint 有 3 条原文件错误，
模型扩大到整份 177 条页面回归后耗时约 115 秒并失败，未在原始副本对照所有失败根因。
因此本次完成是耗时与相关功能验收，不能声称全量检查通过。

另发现 Shell 脚本写入的实际页面文件未进入变更账本、completed 响应仍携带早期工具错误，
按范围要求仅记录。下一步先讨论验证范围与重复步骤，暂不改分页契约或推进多标签壳。

详细证据与复现命令：`local-agent-web/data/diagnostics/token-estimator-20261021/REPORT.md`。
完整任务结果：`local-agent-web/data/diagnostics/token-image-retest-20261022/REPORT.md`。

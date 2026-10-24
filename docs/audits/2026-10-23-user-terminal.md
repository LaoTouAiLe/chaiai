# 右栏受控终端验收（2026-10-23）

## 范围与实现

用户确认启动时审批一次，后续手动输入不逐条审批。契约见 `ARCHITECTURE.md` §23。
首版为 Windows cmd + ConPTY + xterm；进程挂起创建，接入受限令牌、IPC 兼容层和
独立 `KILL_ON_JOB_CLOSE` Job 后恢复。只读档保持只读；其他档只授予当前工作区及专用
临时目录的写权限，不携带外部目录授权。审批前不物化 ACL、不启动 PTY。

审批有效期五分钟，绑定工作区实际路径、Shell 和当前权限配置。重复批准只返回已有结果。
同一终端只允许一个输入连接；最多四个运行终端。每个终端输出最多 2 MiB，结束后最多保留
八份回放缓冲；输入每帧 16 KiB、队列十六帧。关闭面板/刷新不杀进程；结束、空闲三十分钟、
权限变更和服务关闭会清理进程树。内容不存入数据库、日志或模型记忆；审计表仅记启动元数据。

终端可读取本机文件并访问网络，审批卡明确提示。写沙箱保留既有保护范围与限制：没有扩大
保护能力的承诺。Agent 工具、无人值守审批、侧边对话与浏览器功能未改。

## 修改文件

以下路径相对仓库根目录；表内只描述本轮增量，工作区内既有修改没有回退。

| 文件 | 本轮用途 |
|---|---|
| `ARCHITECTURE.md` | 新增 §23 冻结审批、边界、生命周期和接口 |
| `docs/plans/V1_0_DEVELOPMENT_PLAN.md` | 加入用户确认的受控终端例外 |
| `docs/plans/WORK_PANEL_PLAN.md` | 记录 P2 授权和原生 ConPTY 方案 |
| `docs/plans/UI_PAGE_IMPLEMENTATION.md` | §9.22 结果与未做范围 |
| `docs/audits/2026-10-23-user-terminal.md` | 本验收记录 |
| `local-agent-web/README.md` | 使用入口与限制 |
| `local-agent-web/src/local_agent/security/windows_pty.py` | 受限 ConPTY、输入输出、尺寸、真实退出与 Job 清理 |
| `local-agent-web/src/local_agent/security/process_guard.py` | 统一终端启动出口，复用原有 IPC 清理环境 |
| `local-agent-web/src/local_agent/application/terminals.py` | 审批、指纹、容量、输出回放、输入队列、空闲回收 |
| `local-agent-web/src/local_agent/application/terminal_models.py` | 终端 HTTP DTO |
| `local-agent-web/src/local_agent/application/service.py` | 惰性管理器和权限变化清理 |
| `local-agent-web/src/local_agent/persistence/terminal_audit.py` | 只保存审批和状态元数据 |
| `local-agent-web/src/local_agent/persistence/migrations.py` | 追加聊天库 v13 审计表迁移 |
| `local-agent-web/src/local_agent/api/terminals.py` | HTTP 与 WebSocket 通道 |
| `local-agent-web/src/local_agent/api/security.py` | WebSocket Host/Origin 校验 |
| `local-agent-web/src/local_agent/api/app.py` | 注册路由，lifespan 启停，校验错误 no-store |
| `local-agent-web/src/local_agent/api/container.py` | 显式关闭运行时也清理终端 |
| `local-agent-web/pyproject.toml`、`local-agent-web/uv.lock` | 显式声明 WebSocket 服务端依赖 |
| `local-agent-web/docs/api/openapi.json` | 由导出脚本生成接口文档 |
| `local-agent-web/frontend/src/api/schema.gen.ts` | 由 generate:api 生成类型 |
| `local-agent-web/frontend/src/api/client.ts` | 终端请求、批准/拒绝、列表与结束 |
| `local-agent-web/frontend/src/features/panel/PanelShell.tsx`、`PanelShell.module.css` | 终端标签、按需加载和高度容器 |
| `local-agent-web/frontend/src/features/panel/TerminalPane.tsx`、`TerminalPane.module.css` | 审批卡、xterm、连接状态、尺寸与结束 |
| `local-agent-web/frontend/package.json`、`package-lock.json` | xterm 与 fit 插件 |
| `local-agent-web/frontend/vite.config.ts` | 开发代理启用 WebSocket |
| `local-agent-web/tests/test_user_terminals.py` | 审批、边界、协议、回放、容量、生命周期 |
| `local-agent-web/tests/test_windows_pty.py` | 真实 Windows 写边界、REPL、Ctrl+C、进程树 |
| `local-agent-web/tests/test_api.py` | 严格端点清单增加四个终端端点；收尾补齐十三个已实现旧端点 |
| `local-agent-web/frontend/src/features/panel/TerminalPane.test.tsx` | 不自动批准、拒绝、只读提示、失败与不可用 |
| `local-agent-web/frontend/e2e/terminal-live.spec.ts` | 真实服务审批、落盘文件、刷新同进程、结束、控制台 |
| `local-agent-web/frontend/playwright.terminal.config.ts` | 独立真实服务验收入口 |
| `local-agent-web/frontend/playwright.config.ts` | 静态 fixture 套件排除真实终端用例 |

## 验证与复现

在 `local-agent-web` 执行；原生 Windows 测试需正常用户环境，外层受限令牌中再次构造 LUA
令牌会返回错误 87。每次选择新的临时目录，避免残留 ACL/数据影响验证。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_user_terminals.py tests/test_api.py::test_the_committed_openapi_matches_the_real_application -q --basetemp=.pytest_cache/terminal-check-new
.\.venv\Scripts\python.exe -m pytest tests/test_windows_pty.py tests/test_process_guard.py tests/test_catalog_persistence.py tests/test_workspace_view.py::test_non_git_workspace_reports_unavailable -q -p no:cacheprovider --basetemp=E:/tmp/chaiai-terminal-check-new
.\.venv\Scripts\python.exe scripts/export_openapi.py
cd frontend
npm run generate:api
npm run check:api
npm run build
npx vitest run --maxWorkers=1
npx playwright test -c playwright.terminal.config.ts
```

- 前端全量：51 文件、485 用例通过；最终调整后终端四用例单独复核。
- 终端 API/WS 与生成 OpenAPI 一致性：14 通过。
- Windows 原生、原有进程管理、迁移与非 Git 目录：24 通过。原生三条覆盖工作区内写入、
  外部与 `.git` 写入拒绝、只读档、resize、Python REPL、Ctrl+C、子孙进程退出。
- 真实服务 Playwright：1 通过，使用新临时数据库和真实受限 ConPTY；仅模型为测试替身。
  浏览器输入创建文件，经真实文件预览 API 验证内容；刷新后 terminal_id 不变，结束成功。
  页面异常和浏览器 error 控制台均为空；审批与运行截图已打开复核。
- 修改模块 Ruff、六个核心模块 mypy、修改前端模块 ESLint、生成一致性、typecheck、build 通过。
  Vite 仍有既有大 chunk 提示；没有为此调整其他模块。

## 排查结果与未覆盖项

ConPTY 最初绕过 PTY，使用了父服务重定向的标准句柄，导致输入无响应。按
[Windows Terminal 的创建实现](https://github.com/microsoft/terminal/blob/main/src/cascadia/TerminalConnection/ConptyConnection.cpp)
设置 `STARTF_USESTDHANDLES` 并保持标准句柄为空后，原生和真实浏览器验证通过。
关闭顺序遵循 [ClosePseudoConsole 文档](https://learn.microsoft.com/en-us/windows/console/closepseudoconsole)，
先关闭输出管道以避免旧 Windows 版本等待输出排空。

初次验收的相关后端批次（API、终端、工作面板、定时任务、事件）为 **215 通过、2 失败**（收尾结果见下节）：

1. `test_non_git_workspace_reports_unavailable`：临时目录在仓库内，Git 向上发现本仓库。
   换到仓库外目录重跑通过，产品代码和该测试均未改。
2. `test_openapi_surface_is_controlled`：既有端点清单缺 13 条旧接口。本轮四条终端 HTTP
   接口已补进严格相等清单；没有放宽断言或顺带补旧功能的清单。
   缺项为：四个 cron 路径、两个 artifacts 路径、两个 session changes 路径、command detach、
   workspace files、files/search、git/status、git/diff。真实应用 OpenAPI 与生成文档一致性通过。

既有发布 compatibility-baseline 欠账未处理。没有运行全库 pytest、真实供应商调用、安装包发布、
实体触屏或其他操作系统验证；不支持的系统明确禁用。没有新增审批白名单或替代用户的自动批准。

## 接口清单收尾（2026-10-23）

用户确认先完成收尾。本次先复现 `test_openapi_surface_is_controlled` 失败，同时确认真实
应用与生成 OpenAPI 一致性测试通过。核对 `api/routes.py` 与架构契约后，确定根因为测试的
固定预期集合遗漏已实现路径，不是路由未注册或接口文档漂移。

在 `tests/test_api.py` 显式补齐十三条路径：工作区文件及搜索、Git 状态/diff、会话改动及
diff、命令转后台、四个定时任务路径、产物汇总及预览。仍使用 `set(schema["paths"]) == {...}`，
没有改成子集比较，也没有从实际路由自动推导预期值。新增或删除未登记接口仍会使测试失败。
同步更新本记录与 `UI_PAGE_IMPLEMENTATION.md` §9.23，生产代码、接口和生成文件没有变化。

复现命令（在 `local-agent-web` 执行；临时目录使用仓库外的新目录）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py::test_openapi_surface_is_controlled tests/test_api.py::test_the_committed_openapi_matches_the_real_application tests/test_user_terminals.py tests/test_workspace_view.py::test_non_git_workspace_reports_unavailable -q --tb=short -p no:cacheprovider --basetemp=E:/tmp/chaiai-api-closeout-new
.\.venv\Scripts\python.exe -m ruff check tests/test_api.py
cd frontend
npm run check:api
```

结果：**16 passed**，其中包含此前失败的接口清单与非 Git 目录检查；Ruff 与前端生成类型
一致性检查均通过。仅有第三方 `BlockingPortal` 弃用警告。本次收尾范围内没有遗留失败。
未重跑全库测试、前端构建或浏览器验收；这些运行代码均未在本次修改，上一轮验证结果仍见上文。

# 浏览器、持久交互进程与宿主适配（2026-10-18）

用户确认按前一轮建议优先处理前三项：浏览器操作、持续终端交互、已复现的 Windows 执行兼容问题。
本次没有修改主工具并行调度、文件搜索预算或记忆策略，没有提交 Git、打包或发布。

## 最终行为

- 浏览器：独立 Edge/Chrome/Chromium、标签页、导航、动态 DOM 快照、稳定控件引用、点击、中文填表、按键、
  页面/容器滚动、条件等待、截图、视口调整和下载。使用正文/控件分页，不等待持续连接页面的 networkidle。
- 持久进程：立即返回会话 ID；支持后续输入、EOF、增量日志、真实退出状态、列举与停止。
  普通回答结束保留，同一对话下一轮可复用；不同对话和应用实例隔离。
- 公开下载：Python 默认 TLS 保留证书验证，原子保存授权路径，输出 HTTP 状态、字节数和 SHA256。
  不替换有 Cookie、客户端证书、代理认证等语义的原请求，不执行下载内容。
- 系统查询：平台、CPU 数、工作区磁盘容量，以及进程 PID/父 PID/名称；Windows 使用原生只读 API，避免依赖 WMI。
- 清理：取消任务、正常关闭服务、改变目录权限或关闭对应能力会关闭持久资源；跨轮访问会重新绑定当前取消令牌。
  关闭执行命令同时禁用并清理交互进程，不能借新工具绕过原开关。运行目录自带 `.gitignore`。

## 权限与未完成边界

1. Shell 继续使用原 Windows 写沙箱。持久进程不提供退出沙箱选项；外部写入仍需目录授权，
   破坏性命令和输入复用现有判定。只读子 Agent 没有浏览器、持久进程或文件下载能力。
2. 浏览器是固定用途的受控宿主，保留 Chromium 自身沙箱，**不在 Windows Shell 写令牌内运行**。
   固定程序与启动参数、独立工作区配置目录、默认禁下载、显式输出路径检查是这里的边界，
   不能把它描述为与 Shell 相同的 OS 写隔离。它不开放任意宿主 JavaScript、可执行文件或启动 flags。
3. 当前终端是 stdio 会话，支持 REPL 与安装输入，**没有全屏 PTY/ConPTY 仿真**。
4. 原生 Node pipe spawn、WMI、Schannel 路径的兼容问题仍存在。
   原生对照中，移除 LUA_TOKEN 后三项仍失败，未将这个无效修改带入生产。
   新入口覆盖浏览器、公开下载和常用系统查询，不宣称全面修复任意第三方程序内部的 IPC。
5. 服务重启后原会话句柄不能恢复；历史 ID 会提示重新启动。强制结束或崩溃后的孤儿资源恢复未作为本轮验收。
   macOS/Linux 没有执行本轮真实浏览器与进程验收。

## 修改文件

| 文件 | 本轮改动 |
| --- | --- |
| `src/local_agent/tools/browser_tools.py` | 新增浏览器依赖探测、CDP 宿主与页面操作入口 |
| `src/local_agent/tools/terminal_tools.py` | 新增持久 stdio 进程、后台日志、输入与停止入口 |
| `src/local_agent/tools/runtime_sessions.py` | 新增应用/对话/工作区隔离及资源取消、权限与退出清理 |
| `src/local_agent/tools/host_tools.py` | 新增公开下载、原生系统与进程查询 |
| `src/local_agent/tools/tool_registry.py`、`src/local_agent/capabilities.py` | 注册、依赖探测、开关联动、子任务边界和不可信来源归属 |
| `src/local_agent/security/policy.py`、`security/approval.py` | 新入口的执行前复核、启动指纹、路径授权与破坏性输入判定 |
| `src/local_agent/security/process_guard.py`、`src/local_agent/cancellation.py` | 可选 stdin 管道与取消令牌弱引用支持 |
| `src/local_agent/graph/builder.py`、`graph/nodes.py` | 注入会话范围和浏览器可用性，登记新工具的恢复副作用语义 |
| `src/local_agent/application/service.py`、`api/container.py` | 应用命名空间、能力视图、权限/能力变更及正常关闭时清理 |
| `src/local_agent/application/projection.py`、`frontend/src/features/tool/display.ts` | 新工具中文标签与参数名称 |
| `src/local_agent/context/prompt_context.py` | 新能力使用说明，澄清回答结束与已启动服务的生命周期 |
| `tests/test_interactive_tools.py` | 21 项新增权限、隔离、取消、输入、日志、下载、依赖/开关联动验证 |
| `pyproject.toml`、`uv.lock`、`.gitignore` | 显式声明已有 websocket-client 依赖、自忽略运行目录；锁文件仅增加两行声明，没有依赖升级 |
| `../ARCHITECTURE.md` §22、`../V1_0_DEVELOPMENT_PLAN.md`、两级 README、本文件 | 同步现行边界与验证记录 |

以上仅列本轮贡献，工作树里此前已有的大量未提交改动保留，没有重置或冒认其改动量。

## 验证

### 原生与自动回归

工作目录为 `local-agent-web/`。Windows 进程树/受限令牌用例必须在原生进程中运行，
不把 Codex 外层受限进程的 `CreateRestrictedToken 87` 或 taskkill 失败当成本项目原生命令结论。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_interactive_tools.py tests/test_autonomy_policy.py tests/test_process_guard.py tests/test_shell_tls_recovery.py tests/test_delegated_workflow.py tests/test_tool_stream.py tests/test_projection.py tests/test_preferences.py -q -p no:cacheprovider
```

- 最终原生 **250 passed**，其中新增专项 **21 项**。JUnit：`eval/interactive-final-tests.xml`。
- 能力状态不泄漏工具名、工具覆盖与关闭命令联动另行冒烟 **3 passed**。
- 前端 **427 passed**；末次滚动参数补齐后重新构建通过。前端 lint 无错误，有已有热刷新 warning。
- `ruff check src tests/test_interactive_tools.py` 通过。
- 新增/主要改动 11 个模块定向 mypy 通过。**全项目 mypy 未通过**：
  `application/projection.py:1544` 的既有 object→int 与无效 ignore 两条，
  `context/prompt_context.py:248` 的可空时间一条；与前一轮已记录的三条相同，没有顺手修改。
- 初次扩大到历史能力/快照套件并非全绿：旧用例仍断言不含任务控制的工具顺序、旧提示词、删除必须审批等。
  本轮未修改这些旧测试，**不宣称完整 pytest 套件通过**。

本轮曾发现取消令牌不能被弱引用，造成新增取消绑定失败；已增加弱引用槽位，
随后专项和上述最终 250 项原生回归通过。测试残留的进程已按专属测试路径识别并清理。

原生工具独立验收：`eval/verify_interactive_tools.py`，产物
`eval/interactive-native-1792312926438987700/`。
实际完成 Python REPL 输入、未授权目录写入拒绝、中文填表、两次点击、PNG 截图、浏览器下载，
以及公共 HTTPS 文件下载。下载校验来自实际字节，未关闭证书验证。

### 两轮真实模型

脚本：`eval/verify_agent_interactive.py`。公开 FastAPI 消息入口、真实配置模型，
新工作区和聊天库，只读复制设置到忽略目录；不修改正式聊天或偏好。

| 轮次 | 实际完成 | 耗时 / 模型调用 |
| --- | --- | --- |
| 第一轮 | 自己编写 HTML；持久 HTTP 服务返回 200；浏览器中文填表并点到 2；截图；交互 REPL 算得 42 后关闭；公开 HTTPS 下载并核对 SHA256 | 45.39 秒 / 21 次 |
| 第二轮 | 复用原服务与浏览器标签；确认保留计数 2；改为“跨轮成功”后点到 3；截图；关闭浏览器和进程，核对端口拒绝连接及 PID 不存在 | 39.49 秒 / 16 次 |

两轮均完成，无中途补救消息、审批决定、预写实现或替 Agent 修页面。
两条验收任务本身分别提出第一轮执行与第二轮复用，不将第二条算作失败后的人工救援。
这些数字只证明本次验收，不代表任意复杂任务的成功率或性能基准。

证据：`eval/interactive-agent-1792313424793612500/summary.json`、两轮完整快照、工具消息与
`workspace/browser-round1.png`、`workspace/browser-round2.png`。

### 正式服务与页面

当前新版服务 `http://127.0.0.1:8000` 健康检查 200；能力页真实浏览器只读查看，
确认“交互进程与后台服务”“操作浏览器”已开启且可用，没有点击设置开关或提交消息。
正式设置版本仍为 **5**，任务仍为 **65 完成、2 取消、0 运行/等待/复核**。
次数和默认命令超时仍为 null。

前端服务实际返回 `/assets/index-hzOX81LW.js`，SHA256
`2f73577526f57f805c6b263a543f97f4d97f878336e312a972d7459fb909a277`，与本地构建相同。
截图：`eval/interactive-formal-browser/capabilities.png`，验证摘要在同目录 `result.json`。

用户复验：刷新正式页面 → “能力与档位” → 确认两项可用；
提交“创建计数网页，启动服务，浏览器输入中文并点击两次后截图，保留服务”，
下一轮要求“复用原服务再点一次，验证为 3 后关闭浏览器和服务”。

协议实现参考：[CDP Page](https://chromedevtools.github.io/devtools-protocol/tot/Page/)、
[websocket-client 官方示例](https://websocket-client.readthedocs.io/en/latest/examples.html)。

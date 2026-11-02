# 交付补充验收 · 2026-11-01

本记录覆盖 10 月 30 日审批后至 11 月 1 日的补充工作，优先于交付初稿及历史修复报告中的“未跑”状态。没有提交或发布，未清理其他会话的改动。

## 改动文件与原因

原审批修复文件清单及根因见 `../.review-artifacts/fixes-2026-10-30.md`。本次继续补充：

| 文件（相对仓库根） | 改动与原因 |
| --- | --- |
| `local-agent-web/src/local_agent/application/file_mentions.py`、`application/service.py` | Git 引用使用 ProcessRegistry 和会话取消令牌，符合受控进程要求 |
| `local-agent-web/src/local_agent/application/session_export.py` | 归档导出 guard 显式允许归档会话，修复前后两层检查不一致 |
| `local-agent-web/tests/test_file_mentions.py`、`tests/test_api.py` | 验证取消及两种格式的归档导出 |
| `local-agent-web/frontend/src/components/SidebarItemMenu.tsx` 及对应样式 | 菜单 portal 到 body，按视口限制位置和高度，修复菜单被裁切 |
| `local-agent-web/scripts/serve_fake.py`、`serve_workflow_fake.py`、`serve_browser_test.py`、`serve_mcp_acceptance.py` | 验收服务覆盖 CHAIAI_HOME，使用隔离数据；假模型不使用真实凭据或用户 PowerShell profile；审批场景使用当前文件数阈值 |
| `local-agent-web/tests/test_e2e_harness.py` | 验证隔离在创建 runtime 前生效 |
| `local-agent-web/frontend/playwright.config.ts` | 默认批次排除有独立服务配置的专用测试 |
| `local-agent-web/frontend/e2e/` 下相关测试 | 按当前可访问名称、折叠状态、工作流 API、终端生命周期及连接就绪条件修正旧断言，保留内容和副作用验证 |
| `local-agent-web/frontend/e2e/__screenshots__/` | 核对视觉变化并更新基线；其中剩余 17 张经用户逐项清单授权后更新，未放宽像素比较 |

## 验证结果

| 验证 | 结果与日志（位于 `.review-artifacts/`） |
| --- | --- |
| 全量 pytest | **3261 passed、12 skipped**，40 分 47 秒，`accept-pytest-final.txt` / XML；在普通 Windows 权限下运行，临时目录在仓库外 |
| 最终归档导出、引用及隔离回归 | **29 passed**，`accept-nov01-pytest.txt`；其后增加 profile 隔离断言，单例 **1 passed**，`accept-nov01-harness.txt` |
| 前端单测 | **714 passed / 73 文件**，`accept-frontend-unit.txt` |
| 类型、构建、API 生成检查 | 通过；11 月 1 日再跑 typecheck 通过 |
| 源码 lint | 0 errors、7 个已有 warnings；调整的审批/MCP/终端测试 lint 通过 |
| 规模、兼容与 OpenAPI | 11 月 1 日检查通过，没有放宽规模门槛 |
| 默认严格 e2e | **240 passed、3 skipped、0 flaky**，`accept-static-verified.json`；无截图差异、未启用 update-snapshots |
| 提问队列 | 11 passed，`accept-question-queue.json` |
| 权限 / 流式 / 后台 / 模型变体 | 分别 2 / 3 / 2 / 1 passed，`accept-recheck-permissions.json`、`accept-recheck-streaming.json`、`accept-recheck-background.json`、`accept-live-variants.json` |
| 基础对话 / 压缩 / 工作流 | 分别 3 / 1 / 5 passed，`accept-verified-live.json`、`accept-verified-compression.json`、`accept-last-workflow.json` |
| 审批 / MCP | 各 2 passed，`accept-nov01-final-approval.json`、`accept-nov01-final-mcp.json` |
| 内置浏览器 | **4 passed**，`accept-nov01-ready-browser.json`；覆盖中文、登录、历史、标签恢复、关闭、外网与双击 |

全量 pytest 在归档导出 guard 的最后一行修复之前完成；该修复随后经过专项测试和冻结包 HTTP 验证，未再次宣称最终树全量复跑。

最初受限进程中的 CreateRestrictedToken 87 及仓库内临时目录造成的失败分别通过普通 Windows 运行和仓库外临时目录验证；没有关闭产品安全机制。

## 冻结包与安装验收

- 最终目录包：`local-agent-web/build/packaging/out-delivery-final-20261030/chaiai`；DLL、自包含、敏感文件扫描及原生依赖检查通过，见 `accept-delivery-package.txt`。
- 安装程序：`local-agent-web/build/packaging/installer-delivery-final-20261030/chaiai-setup-0.2.0.exe`。
- 安装程序 SHA256：`C79F33DE038937C73793434FB20639B4B92039C1770B860B5A396FC0B09A380C`。
- 冻结包健康、前端、文档上传/提取/预览、markdown/json 导出及归档后两种导出均通过，见 `accept-delivery-smoke.json`。
- 隔离目录静默安装成功，安装后 `--diagnose` 返回 ok；卸载 exit 0，程序被移除、测试数据保留，见 `accept-installed-diagnose.json`、`accept-uninstall-result.json`。

## 复验方式与边界

在 `local-agent-web` 执行 `scripts/check_size.py`、`scripts/check_release_contracts.py`、`scripts/export_openapi.py --check`；用本项目 `.venv/Scripts/python.exe` 运行。pytest 使用仓库外全新 `--basetemp`，并在可创建 Windows 限制令牌的普通终端运行。

在 `local-agent-web/frontend` 执行 `npm run typecheck`、`npm run test`、`npm run build`、`npx playwright test --workers=2`。专用套件使用各自 `playwright.<名称>.config.ts`，例如 `npx playwright test -c playwright.approval.config.ts`。验收时设置 `CI=1`，确保使用新建服务。

尚未验证：真实模型/付费搜索的 web-search-live 专用套件，以及另一台干净 Windows 机器/VM 的安装。12 个后端和 3 个默认 e2e 的既有跳过保留。视觉清单见 `../.review-artifacts/visual-baseline-review.md`。

终端原阻塞已修复：IPC 组件误扫描 CLR 管理的 IL-only 程序集，在锁内引发加载重入；现识别 IL-only 映像并正确结束原始导入表扫描，CoreCLR 与 native/mixed-mode 模块仍保留拦截。没有切换产品默认 shell、自动降级或放宽沙箱。

- 新增回归在旧组件上 30 秒超时，修复后验证 pwsh 调用 Node 子进程、工作区内允许写入、工作区外拒绝写入。
- IPC/终端 **34 passed**，相关进程/PTY/命令 **61 passed**。
- 产品组件完整默认 pwsh 终端 e2e **1 passed**；新冻结包完整终端 e2e **1 passed**，均无 flaky。
- 完整根因、改动和复验命令见 [IPC 修复记录](IPC_FIX_2026-11-01.md)。临时 Playwright/native 探针已移除。

11 月 1 日最终交付包已重新构建和扫描，替代上面的 10 月 30 日历史包：`local-agent-web/build/packaging/out-ipc-final-20261101/chaiai`，4879 文件 / 286.4 MiB；安装程序为 `local-agent-web/build/packaging/installer-ipc-final-20261101/chaiai-setup-0.2.0.exe`，SHA256 为 `A9E6C19B83023CCD70A7A6F859D344140BBE71EE851814A3BE54B2EFFB6D6332`。日志见 `.review-artifacts/ipc-package-final.txt`、`ipc-installer-final.txt`、`terminal-frozen-final.json`。

新安装程序静默安装 exit 0，安装后诊断 ok，安装出的 IPC DLL 与修复包一致；卸载 exit 0、程序移除且测试数据保留，见 `.review-artifacts/ipc-install-result.json`、`ipc-installed-diagnose.json`、`ipc-uninstall-result.json`。本轮验收服务已关闭。

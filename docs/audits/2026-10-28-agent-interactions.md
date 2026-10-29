# Agent 页面层级与编辑交互修订

用户反馈：任务中心仍是弹窗，关闭入口不合适；编辑表单插在列表顶部导致跳动；会话模型列表挤压对话空间。

## 已完成

- 任务中心使用主工作区页面，保留状态筛选、查看结果、打开会话和人工核对结案。返回会话使用页面动作，取消旧遮罩和滚动锁；功能页显示期间隐藏右侧工作面板。
- 模型连接、MCP Server、MCP 工具名单和定时任务新建使用独立编辑窗口，支持窗口内部滚动、图标关闭、Escape、Tab 范围控制及返回来源按钮。保存中不能退出；失败保留输入，取消后不把该次错误插到原列表上方。
- 模型和上下文详情使用按钮锚定的浮层，通过 Portal 脱离布局。长模型列表在浮层内滚动；点击外部、键盘离开或 Escape 可收起，焦点返回不滚动页面。
- 窄屏模型连接表单改为单列。上下文只读悬浮提示不拦截邻近按钮的点击。
- 本轮未修改后端接口、模型调用、审批和权限契约，未重建 exe，未覆盖旧像素基线。

## 文件清单

以下路径相对仓库根目录，TSX 对应的同名 CSS 文件一并列出。

| 文件 | 用途 |
| --- | --- |
| `local-agent-web/frontend/src/app/LiveApp.tsx` | 将任务中心接入功能页分支、收敛命令面板导航和工作面板展示条件。 |
| `local-agent-web/frontend/src/app/App.tsx` | 原型任务场景直接展示页面，并选中任务导航。 |
| `local-agent-web/frontend/src/features/tasks/TasksPanel.tsx`、`TasksPanel.module.css`、`TasksPanel.test.tsx` | 页面结构、返回动作、响应布局和回归验证。 |
| `local-agent-web/frontend/src/components/ui/EditorDialog.tsx`、`EditorDialog.module.css`、`EditorDialog.test.tsx` | 通用编辑窗口、焦点恢复、键盘和提交期间退出限制。 |
| `local-agent-web/frontend/src/components/ui/AnchoredPopover.tsx`、`AnchoredPopover.module.css`、`AnchoredPopover.test.tsx` | 锚定定位、视口边界、独立滚动和退出交互。 |
| `local-agent-web/frontend/src/components/Composer.tsx` | 模型和上下文按钮提供锚点。 |
| `local-agent-web/frontend/src/components/ContextMeter.tsx`、`ContextMeter.module.css` | 上下文详情浮层及只读提示遮挡修复；保留本轮前已有的计量口径修改。 |
| `local-agent-web/frontend/src/features/settings/ModelMenu.tsx`、`ModelMenu.module.css` | 模型选择浮层和有界列表。 |
| `local-agent-web/frontend/src/features/settings/ModelSettingsDialog.tsx`、`SettingsDialog.module.css` | 模型编辑窗口、错误作用域及窄屏表单。 |
| `local-agent-web/frontend/src/features/mcp/McpSection.tsx` | Server 和工具名单编辑窗口。 |
| `local-agent-web/frontend/src/features/cron/CronPanel.tsx` | 定时任务新建窗口。 |
| `local-agent-web/frontend/src/features/capabilities/CapabilitiesPanel.tsx` | 独立原型弹窗关闭入口改为带可访问名称的图标。 |
| `docs/plans/UI_PAGE_IMPLEMENTATION.md` | 记录页面、编辑窗口和浮层的层级约定。 |
| 本文件 | 文件、验证和未验证范围。 |

## 验证结果

- `local-agent-web/frontend` 执行 `npm run build`：通过，包括 TypeScript 检查。保留现有 Shiki 分包及包体积提示。
- `npm run test -- --maxWorkers=2`：70 个文件、678 项测试全部通过。该数量包含工作区内已有及并行修改的测试。
- `npm run lint`：0 错误、7 条已有 Fast Refresh 警告。
- Playwright 交互验证：4 项全部通过，测试脚本和配置分别为仓库忽略目录内 `.playwright-cli/agent-interaction.e2e.cjs`、`.playwright-cli/interaction-playwright.config.cjs`。
- 四档视口：1440×900、1280×800、640×800、390×844。31 个模型打开前后对话区的 y、height、scrollTop 和输入区矩形完全相同；长列表末项可滚动到达。
- 滚到 31 条连接的末尾编辑：窗口打开、模拟保存失败、关闭后，来源列表 scrollTop 和编辑按钮矩形保持相同；保存中禁用关闭，失败后 Escape 正常退出并恢复焦点。
- 15 条 MCP Server 的末条编辑与工具名单窗口、新建定时任务、任务查看结果与返回会话通过浏览器操作验证。

截图与测量数据位于 `C:/Users/asus/.codex/visualizations/2026/10/27/01a17d34-3311-7422-b8e9-515ae56bd639/`：

- `agent-interaction-tasks-1440.png`、`agent-interaction-tasks-390.png`
- `agent-interaction-models-1440.png`、`agent-interaction-models-390.png`
- `agent-interaction-editor.png`、`agent-interaction-editor-390.png`
- `agent-interaction-mcp.png`、`agent-interaction-cron.png`
- `agent-interaction-layout.json`（四档浮层开关前后的测量记录）

浏览器验证使用隔离目录 `.playwright-cli/agent-interaction-data` 和确定性假模型服务，没有连接真实模型、运行 MCP 命令或创建真实定时任务。Windows WebView2 宿主及新 exe 未验证。

## 手动复核

启动源码服务并刷新页面：点击任务入口，应直接进入主工作区；进入会话打开模型列表，观察对话及输入区不移动；设置中滚动到较低的连接点击编辑，取消后仍停在原连接；能力中心的 MCP 编辑、定时任务的新建应在独立窗口出现。将窗口收窄到 390px 重复操作，并检查 Escape 和 Tab。

# Agent 工作台整体重构 · 2026-10-28

## 结果与边界

本轮按用户确认的 Craft Agents 方向重构应用壳和核心交互，替换旧的“单个侧栏堆全部入口”结构。桌面为功能导航轨、会话目录、主工作区；工作面板按需展开。没有重建 exe，没有修改后端接口、执行、权限或审批契约，没有覆盖本轮开始前已有的终端等并行改动。

## 参考源码

- 上游：<https://github.com/craft-ai-agents/craft-agents-oss>，Apache-2.0。
- 完整源码压缩包下载并解压于 `E:\pylianxi\craft-agents-reference\craft-agents-oss-main`；下载时桌面包版本为 `0.15.6`。
- 已查看应用壳 `apps/electron/src/renderer/components/app-shell/AppShell.tsx`、`SessionList.tsx`、`input/FreeFormInput.tsx`、`panel-constants.ts`，以及 `packages/ui/src/components/chat/TurnCard.tsx` 和共享设计变量。
- 借鉴层次、布局和交互组织，代码按 chaiai 现有视图模型重新实现；未引入 Craft 的 Electron、Agent SDK、Jotai、Tailwind 或 UI 包，保留 chaiai 品牌资源。

## 本轮文件

以下路径均相对项目根目录。

| 文件 | 改动 |
| --- | --- |
| `local-agent-web/frontend/src/components/NavigationRail.tsx` | 新增独立功能导航；目录收起后仍能访问会话、任务、产物、能力、定时任务、数据与设置。 |
| `local-agent-web/frontend/src/components/NavigationRail.module.css` | 导航轨、当前入口、角标、焦点与悬停样式。 |
| `local-agent-web/frontend/src/components/WorkbenchIcon.tsx` | 统一本轮导航、任务入口与标题栏所需的 SVG 图标。 |
| `local-agent-web/frontend/src/components/useCompactNavigation.ts` | 静态场景与真实应用共用小窗口监听。 |
| `local-agent-web/frontend/src/components/Sidebar.tsx` | 改为导航轨与会话目录组合；新增状态筛选，保留搜索、工作区管理、归档恢复。 |
| `local-agent-web/frontend/src/components/Sidebar.module.css` | 重建目录、会话卡片、状态和时间布局；窄屏手动展开采用覆盖层。 |
| `local-agent-web/frontend/src/components/Sidebar.test.tsx` | 状态筛选与搜索组合、收起目录后的功能入口回归测试。 |
| `local-agent-web/frontend/src/components/TopBar.tsx` | 工作区路径与会话标题分层，右侧统一组织视图与工作面板。 |
| `local-agent-web/frontend/src/components/TopBar.module.css` | 标题栏布局与窄屏换行。 |
| `local-agent-web/frontend/src/components/Composer.tsx` | 草稿输入自动增高至 200px，保留现有发送、停止、引用、附件、模式及审批行为。 |
| `local-agent-web/frontend/src/components/Composer.module.css` | 输入表面、焦点、操作条和快捷键提示；字号跟随偏好。 |
| `local-agent-web/frontend/src/features/conversation/WelcomePanel.tsx` | 新增任务起点；快捷任务只准备草稿，由用户发送。 |
| `local-agent-web/frontend/src/features/conversation/WelcomePanel.module.css` | 欢迎介绍、任务卡片与窄屏排列。 |
| `local-agent-web/frontend/src/features/conversation/WelcomePanel.test.tsx` | 快捷任务回调与无动作时的禁用行为。 |
| `local-agent-web/frontend/src/features/conversation/Conversation.tsx` | 抽出首屏；每轮分为提问和回复，回复显示真实工具操作总数。 |
| `local-agent-web/frontend/src/features/conversation/Conversation.module.css` | 回复内容区与只读工具分组表面；清理已迁移的旧欢迎区样式。 |
| `local-agent-web/frontend/src/features/settings/SettingsDialog.module.css` | 扩展设置工作区、导航分组、当前分区和阅读层级。 |
| `local-agent-web/frontend/src/features/settings/PreferenceSection.module.css` | 设置组标题、字段表面和间距。 |
| `local-agent-web/frontend/src/features/panel/PanelShell.module.css` | 独立工作面板画布、共享表面及手机窗口边距。 |
| `local-agent-web/frontend/src/styles/tokens.css` | 导航轨、画布、主题、强调色、公共边距与旧主操作变量映射。 |
| `local-agent-web/frontend/src/app/App.tsx` | 静态场景使用与真实应用相同的窄屏导航规则。 |
| `local-agent-web/frontend/src/app/App.module.css` | 工作区外壳与空会话组合布局。 |
| `local-agent-web/frontend/src/app/LiveApp.tsx` | 集中处理功能页切换；接通快捷草稿、会话返回和窄屏导航收口。 |
| `docs/plans/UI_PAGE_IMPLEMENTATION.md` | 记录本轮覆盖历史规格的结构和行为约定。 |
| `docs/audits/2026-10-27-agent-ui.md` | 标记历史记录已由本轮取代，并链接修复结果。 |
| `docs/audits/2026-10-28-agent-workbench.md` | 本次来源、修改与验证记录。 |

新建或选择会话会关闭旧功能页，修复上一轮记录的“定时任务页新建/选择会话后仍停留原页面”。根因是原处理器只清除数据中台状态，本次新导航统一清理互斥页面。窄窗口执行导航后收起临时展开的目录，保留主内容空间。

## 已验证

在 `local-agent-web/frontend` 执行：

```text
npm run build
npm run test -- --maxWorkers=2
npm run lint
```

- TypeScript 与前端生产构建通过。
- Vitest：65 个文件、641 个测试全部通过。
- ESLint：0 错误、7 条既有 React Fast Refresh 警告。
- 构建仍有 Shiki 大分块及静态/动态混用导入提示；没有改动依赖或打包架构。
- 本轮文件的 `git diff --check` 通过。

浏览器使用本机 Playwright Chromium、生产构建，以及现有 `serve_fake.py` 的确定性假模型服务；独立 `CHAIAI_HOME` 和数据目录均在 `.playwright-cli/agent-workbench-data` 中。未使用真实密钥、模型或用户会话。测试脚本与日志保留在本地 `.playwright-cli/`，没有替换旧视觉基线。

- 24 个静态场景 × 1440×900、1280×800、640×800、390×844，共 96 个组合，逐一确认实际场景挂载，无整页横向或纵向溢出。
- 空会话输入区与审批按钮在各档窗口完整位于视口内（浏览器断言交集比例为 1）；没有点击审批动作。
- 实测任务入口准备草稿、使用现有接口创建会话、切换模式、发送并收到假模型回复。
- 实测从定时任务页新建会话返回对话、从能力中心通过导航轨返回工作台，以及工作面板开合。
- 实测浅深主题保存，较大字号生效，多行草稿使输入框自动增长。
- 实测窄屏自动隐藏目录，手动展开目录后返回工作台会重新收起，设置入口始终可访问。
- 三个浏览器回归测试均通过；截图已逐张检查首屏、正常对话、执行工具、设置、深色主题、工作面板、640px 与 390px 页面。

收尾时检测到其他工作同时新增了 `awaiting-input` 状态和新会话上下文计量展示。本轮保留这些改动，将筛选统一命名为“待处理”，并在筛选回归中覆盖等待回答与等待审批；没有把其他功能改动归入本轮。随后重新完成最新工作区的构建、641 个测试、lint 和浏览器回归。问答通道本身由其独立功能验收负责。

## 效果文件

本地截图目录：`C:/Users/asus/.codex/visualizations/2026/10/27/01a17d34-3311-7422-b8e9-515ae56bd639/`。

- `agent-workbench-rebuild-home.png`：真实入口与快捷任务草稿。
- `agent-workbench-rebuild-shell.png`：普通问答和工具操作。
- `agent-workbench-rebuild-settings.png`：设置工作区。
- `agent-workbench-rebuild-dark.png`：深色界面。
- `agent-workbench-rebuild-panel.png`：工作面板。
- `agent-workbench-rebuild-mobile.png`、`agent-workbench-rebuild-390.png`：小窗口。
- `agent-workbench-layouts.json`：96 个场景与窗口组合的布局记录。

## 查看与尚未验证的部分

前端已构建。可在 `local-agent-web` 执行 `uv run python -m local_agent.api --window` 通过源码启动查看；已有安装包和 exe 中的界面没有更新。

真实模型、真实用户数据和 Windows WebView2 桌面宿主本轮未回归。现有像素基线按 `frontend/e2e/prototype.spec.ts` 的人工审阅约定保留，未把旧截图批量更新后当作像素测试通过。

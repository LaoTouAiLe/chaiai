# Agent 侧栏：图标导航与会话目录分列

## 改动与原因

用户提供 Codex 侧栏截图，要求把纵向功能导航移到独立窄栏，让会话列表占据主要高度。

- 左侧 56px 图标栏放会话、任务、产物、能力、定时、数据入口，设置固定在底部；按钮保留名称、悬停提示和当前页状态。
- 旁边 260px 会话栏仅保留品牌、新建会话、搜索与工作区目录。展开时整体宽 316px，收起时宽 56px。
- 常驻的状态筛选行改为 200px 浮层，筛选生效时按钮高亮。选中后关闭；Escape 关闭并恢复焦点，不撑开或挪动会话列表。
- 窄窗口展开为带遮罩的抽屉，遮罩或 Escape 可收起；切换展开状态后焦点落在仍可操作的开关上。搜索、筛选和会话草稿继续保留。

1440×900 源码页面实测：会话列表起点 y=186px，可滚动区域高 706px。测量场景为 `/?state=shell`，不使用真实会话数据。

## 文件

路径相对项目根目录：

| 文件 | 说明 |
| --- | --- |
| `local-agent-web/frontend/src/components/NavigationRail.tsx` | 独立图标导航；会话目录收起时提供展开与新建入口 |
| `local-agent-web/frontend/src/components/NavigationRail.module.css` | 窄栏、图标按钮、角标与底部设置布局 |
| `local-agent-web/frontend/src/components/Sidebar.tsx` | 精简会话头部、筛选浮层、展开/收起焦点交接 |
| `local-agent-web/frontend/src/components/Sidebar.module.css` | 两列宽度、会话头部、列表区域和移动抽屉 |
| `local-agent-web/frontend/src/components/WorkbenchIcon.tsx` | 增加筛选图标 |
| `local-agent-web/frontend/src/components/ui/AnchoredPopover.tsx` | 新增可选宽度；默认仍为 420px，筛选浮层使用 200px |
| `local-agent-web/frontend/src/components/Sidebar.test.tsx` | 更新状态筛选用例，经过真实浮层入口操作 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 验证列表空间、窄栏按钮位置、筛选浮层与焦点、抽屉和草稿保留 |
| `docs/audits/2026-11-02-dsh-agent-ui.md` | 标注后续侧栏方案 |
| `docs/audits/2026-11-02-compact-agent-sidebar.md` | 本记录 |

## 验证

- 生产构建、TypeScript 检查通过：浏览器验收启动时先执行 `npm run build`。
- 4 个相关单测文件、61 项通过，覆盖 Sidebar、SidebarItemMenu、AnchoredPopover、LiveApp。
- 12 项浏览器验收通过，覆盖 1440×900、1280×800、768×900、390×844。沿用的主题布局用例仍检查 48 组页面/主题/窗口组合。
- 新增空间断言：桌面列表起点低于 220px，列表容器高度大于窗口高度的 70%；功能按钮保持在 56px 窄栏内。
- 验证筛选浮层打开不改变列表起点，Escape 仅关闭浮层并将焦点还给触发按钮，选择状态后可恢复全部会话。
- 修改的组件、单测与浏览器用例 ESLint 通过，0 错误、0 警告。
- `git diff --check` 通过。
- 正在运行的 `http://127.0.0.1:5173/api/health` 返回 200。

复现，在 `E:/pylianxi/chaiai/local-agent-web/frontend` 执行：

```powershell
npm run test -- src/components/Sidebar.test.tsx src/components/SidebarItemMenu.test.tsx src/components/ui/AnchoredPopover.test.tsx src/app/LiveApp.test.tsx
npx playwright test -c playwright.dsh.config.ts
```

人工预览：`http://127.0.0.1:5173/`。先看会话列表，再点击筛选图标、收起/展开开关和左侧任务图标；窄窗口检查抽屉遮罩与 Escape。

截图与实测指标副本：

`C:/Users/asus/.codex/visualizations/2026/10/27/01a17d34-3311-7422-b8e9-515ae56bd639/compact-sidebar/`

## 边界

这次只调整侧栏，未修改后端接口和消息调度，也未重建 exe。WebView2 宿主下的实际显示未验证；原有截图基线未更新。

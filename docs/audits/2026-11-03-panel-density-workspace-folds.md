# 文件预览密度、面板顶部与工作区折叠

2026-11-03。针对用户提供的 chaiai / DSH 对比截图，补齐文件行距、面板顶部高度和工作区展开细节。本轮只改前端，没有修改业务接口、安装依赖或重建 exe。

## 根因和结果

1. 文件代码行距：高亮器输出的行间换行与块级 `.line` 同时参与 `pre` 的空白排版，形成额外的视觉行；纯文本行也额外渲染了换行。现在高亮容器处理行间空白，每个逻辑行独立保留源代码缩进；纯文本改为行号/正文网格。字号 12px、行高 19px，原文件空行仍占一行，长行换行仍按实际行数增加高度。
2. 面板顶部：原先工作面板标题 76px、页签 44px、文件工具栏 38px，累计约 158px。关闭面板按钮已并入页签栏，文件文本预览顶部改为 38px 页签栏 + 38px 工具栏，合计 76px；窄窗口不把工具栏挤成多行。复制改为图标，文件类型图标留在页签中，避免文件名重复占位；工具栏展示路径并保留完整悬停提示。
3. 工作区：原有“展示更多”只控制会话数量，无法折叠整组。现在工作区有独立箭头、`aria-expanded`、`aria-controls` 和键盘操作；折叠状态使用独立本地存储键保存。工作区选择和会话选择保持原语义，选择已折叠的工作区会展开该组；搜索/状态筛选临时展开匹配组，清空后恢复折叠。筛选时箭头禁用并解释原因，避免点击后没有可见反馈。

## 文件清单

路径相对项目根目录。

| 文件 | 说明 |
| --- | --- |
| `local-agent-web/frontend/src/features/panel/PreviewPane.tsx` | 去掉纯文本行末的重复换行、紧凑复制按钮和路径显示 |
| `local-agent-web/frontend/src/features/panel/PreviewPane.module.css` | 单倍代码行高、稳定行号列、固定 38px 文件工具栏 |
| `local-agent-web/frontend/src/features/panel/PanelShell.tsx` | 合并标题/页签控制，关闭与新建入口留在同一行 |
| `local-agent-web/frontend/src/features/panel/PanelShell.module.css` | 页签栏 38px、页签 28px，移除独立大标题栏样式 |
| `local-agent-web/frontend/src/features/panel/FileTree.module.css` | 目录和文件行由 32px 收至 28px |
| `local-agent-web/frontend/src/components/Sidebar.tsx` | 工作区整组折叠、状态保存、筛选临时展开及选择展开 |
| `local-agent-web/frontend/src/components/Sidebar.module.css` | 折叠箭头反馈、隐藏会话组及紧凑间距 |
| `local-agent-web/frontend/src/components/Sidebar.test.tsx` | 独立折叠、重新挂载恢复、搜索恢复、坏存储回退 |
| `local-agent-web/frontend/e2e/work-panel.spec.ts` | 测量高亮/纯文本实际行高、真实空行和顶部累计高度 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 键盘折叠、刷新恢复、筛选与当前会话不变 |
| `docs/audits/2026-11-03-panel-density-workspace-folds.md` | 本记录 |

## 验证

从 `local-agent-web/frontend` 执行：

```powershell
npm test -- src/components/Sidebar.test.tsx src/features/panel
npx playwright test -c playwright.preview.config.ts --project=1440x900 --project=390x844 --max-failures=1
npx playwright test -c playwright.dsh.config.ts --max-failures=1
```

- 相关单元测试：66 项全部通过。
- 文件面板浏览器验收：29 项通过、1 项按设计跳过手机鼠标拖宽；桌面/手机均包含行高新用例。
- 全局界面浏览器验收：36 项全部通过，覆盖 1440×900、1280×800、768×900、390×844。
- 浏览器实测：高亮及纯文本前八行相邻位置差为 19px，其中第二行是原文空行；自动换行关闭后纯文本行高不变。页签栏不超过 38px，页签栏与文件工具栏累计不超过 77px。
- 短行用于测量单倍行高；长行自然折行会增加物理高度，不将正常折行误报为额外行距。
- 设置、编辑弹窗、草稿、导航、Office/PDF、Markdown/HTML、工作簿等既有浏览器场景通过；未更新截图基线。
- 浏览器验收启动时生产构建和 TypeScript 检查通过。修改的 TSX、测试 ESLint 通过，`git diff --check` 通过。
- 最后去掉工具栏重复文件图标后，桌面/手机的行高与顶部高度用例再次复核，截图重新保存到本次会话可视化目录。

## 用户复核

刷新 `http://127.0.0.1:8731/`，打开 `pom.xml` 或其他长代码文件：查看行号连续性、文件原有空行和首屏可显示行数；切换自动换行，拖宽/缩窄面板并关闭/切换页签。左侧用工作区箭头收起会话组，刷新后确认状态保留；搜索组内会话，再清空搜索，确认恢复原折叠状态。

## 边界

本轮补齐截图指明的细节，不宣称所有 DSH 行为均已逐项复制。没有执行后端测试或触发真实模型调用；未重建 exe，WebView2 仍未单独验收。本地存储不可用时仍可折叠，但无法跨刷新保留状态。

# Agent 前端：DSH 样式与布局适配

后续用户确认采用图标窄栏与会话目录分列，最新侧栏结构与验收见 [精简侧栏记录](2026-11-02-compact-agent-sidebar.md)。本记录保留第一轮整体样式适配的范围与验证过程。

## 改动与原因

参考本地 `D:/ideaTest2/deepseek-harness`，读取时提交为 `f3803c96974ad4a94195fb97e5c637e9c2a3afe3`。直接适配 DSH 的主题与阴影变量，并按它的单侧栏、连续画布、页签和输入区结构重做本项目的页面骨架。应用继续使用 chaiai 的组件、品牌与接口。

本轮覆盖：展开/收起导航、窄屏导航抽屉、会话页头与页签、欢迎页、消息气泡、输入框与待发队列、设置页、二级编辑框、浮动菜单、工作面板和任务页标题。浅色与深色共享 DSH 语义变量。窄屏设置卡片的连接名称独占一行，操作按钮整体换行，避免汉字被压成纵排。

### 文件清单

以下路径均相对项目根目录：

| 文件 | 用途 |
| --- | --- |
| `local-agent-web/frontend/src/styles/dsh-platform.css` | DSH 主题源表，适配根元素和主题选择器 |
| `local-agent-web/frontend/src/styles/dsh-elevation.css` | DSH 描边和阴影变量 |
| `local-agent-web/frontend/src/styles/tokens.css` | 将现有组件变量映射至 DSH，调整布局尺寸和字体栈 |
| `local-agent-web/frontend/src/app/App.module.css` | 连续主画布，移除外套面板的边距和圆角 |
| `local-agent-web/frontend/src/components/NavigationRail.tsx` | 导航品牌、收起开关、文字入口与设置入口结构 |
| `local-agent-web/frontend/src/components/NavigationRail.module.css` | 单列导航和收起后的图标导航 |
| `local-agent-web/frontend/src/components/Sidebar.tsx` | 导航容器、窄屏遮罩和 Escape 收起 |
| `local-agent-web/frontend/src/components/Sidebar.module.css` | 单侧栏、会话列表和窄屏抽屉 |
| `local-agent-web/frontend/src/components/TopBar.tsx` | 视图页签独立成行 |
| `local-agent-web/frontend/src/components/TopBar.module.css` | 面包屑、标题和工具区排版 |
| `local-agent-web/frontend/src/components/Composer.module.css` | 圆角输入区、工具按钮和附着式待发列表 |
| `local-agent-web/frontend/src/components/ui/primitives.module.css` | 视图页签的下划线选中态 |
| `local-agent-web/frontend/src/components/ui/EditorDialog.module.css` | 编辑框背景、圆角、标题和阴影 |
| `local-agent-web/frontend/src/components/ui/AnchoredPopover.module.css` | 浮动菜单的圆角和阴影 |
| `local-agent-web/frontend/src/features/conversation/Conversation.module.css` | 消息列、滚动区和用户气泡 |
| `local-agent-web/frontend/src/features/conversation/WelcomePanel.tsx` | 居中品牌标题和快捷提示结构 |
| `local-agent-web/frontend/src/features/conversation/WelcomePanel.module.css` | 首屏标题、说明和快捷入口 |
| `local-agent-web/frontend/src/features/settings/SettingsDialog.module.css` | 设置框、导航、连接卡片和窄屏操作排版 |
| `local-agent-web/frontend/src/features/panel/PanelShell.module.css` | 右侧工作面板与主画布相接 |
| `local-agent-web/frontend/src/features/tasks/TasksPanel.module.css` | 任务页标题与其它功能页统一 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 主题加载、布局、导航和编辑交互验收 |
| `local-agent-web/frontend/e2e/setup-dsh.ts` | 构建并直接管理隔离的 Vite 预览进程 |
| `local-agent-web/frontend/playwright.dsh.config.ts` | 四档窗口的验收配置 |
| `docs/licenses/deepseek-harness-MIT.txt` | 保留 DSH 原始 MIT 许可证 |
| `docs/audits/2026-11-02-dsh-agent-ui.md` | 本记录 |

## 验证结果

- 前端构建、TypeScript 检查通过。最终浏览器验收的启动步骤会先重新构建生产包。
- 完整前端单测：73 个文件、714 项通过。
- 生产包浏览器验收：12 项通过，窗口分别为 1440×900、1280×800、768×900、390×844。
- 布局用例在每档窗口遍历浅/深色与首屏、会话、待发队列、提问、模型设置和模型菜单，共 48 组组合；检查主题变量实际生效、字体、画布颜色、输入框阴影、横向溢出和输入可达性。
- 导航用例验证桌面展开/收起、窄屏遮罩关闭、Escape 关闭、草稿保留、任务页以页面展示，以及返回会话。
- 设置用例验证主设置框和二级连接编辑框位于视口内、Escape 仅关闭二级编辑框，并将键盘焦点还给添加按钮。
- 窄屏连接操作按钮另有文本高度检查，防止逐字换行；已目视检查实际截图。
- `npx eslint src e2e/dsh-workbench.spec.ts e2e/setup-dsh.ts playwright.dsh.config.ts`：0 错误，7 条既有 Fast Refresh 警告。

`npm run lint` 的全目录扫描被既有 `test-results-stale/tmp` 权限限制阻断；另一次排除该目录的扫描读到了 `.tmp-e2e` 的生成缓存并报缓存文件错误。因此使用上面的源码与新增测试文件范围完成 ESLint 验证，没有删除旧测试目录或修改其权限。

## 复现

在 `E:/pylianxi/chaiai/local-agent-web/frontend` 运行：

```powershell
npm run test
npx playwright test -c playwright.dsh.config.ts
npx eslint src e2e/dsh-workbench.spec.ts e2e/setup-dsh.ts playwright.dsh.config.ts
```

浏览器验收会构建前端，在 4181 端口启动并结束自身的预览进程，业务请求使用测试响应，不调用真实模型。截图在 `frontend/test-results/dsh-workbench-*`。人工检查可运行 `npm run dev`，打开 `/?state=empty-session`、`/?state=shell`、`/?state=question`、`/?state=queue` 和 `/?state=settings-models`。

稳定预览副本位于：

`C:/Users/asus/.codex/visualizations/2026/10/27/01a17d34-3311-7422-b8e9-515ae56bd639/dsh-redesign/`

其中 `1440-light-empty-session.png`、`1440-dark-shell.png`、`390-dark-settings-models.png` 已从最终生产包验收截图复制。

## 验收边界

本轮是 DSH 视觉与布局在 chaiai 中的适配，没有迁移 DSH 的 Cordis 插件运行时，也没有承诺 DSH 每个插件页面、所有动效逐像素一致。后端接口与提问/队列的调度实现未改动。

未重建 exe；WebView2 宿主和安装包运行效果未验证。旧截图基线没有覆盖更新，需人工认可此次视觉变化后再单独更新。构建仍提示既有 Shiki 导入与大体积产物警告，本轮没有改动依赖或打包策略。

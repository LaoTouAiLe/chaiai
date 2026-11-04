# 全局界面对齐 DSH

2026-11-03。用户授权对比本地 DSH（3080）与 chaiai（8731），整体优化字体、布局和交互外观。本轮没有修改后端、业务流程或依赖，没有重建 exe。

## 对比依据

用 computer-use / 浏览器工具实际查看双方空白会话、历史正文、输入区及模型菜单，并读取可见元素的计算样式；同时查阅本地 DSH 的 `ui-theme/styles/base.css`、`ui-primitives/Button.module.css`、`Input.module.css`、`ui-sidebar/SidebarRoot.module.css`。DSH 的 MIT 声明沿用 `docs/licenses/deepseek-harness-MIT.txt`。访问用的认证信息不写入代码、记录或测试。

确认的主要差别：

- 正文字体栈原本已接近，但代码字体以通用 `monospace` 结尾。DSH 特意以中文无衬线字体收尾，避免 Windows 中文代码回退到宋体。
- 原侧栏总宽 316px；窄导航条、会话列不同底色，以及会话列单独的顶部圆角，让左栏比 DSH 更碎。
- 原页面中正文 14px、输入框 16px，部分控件继承 16px；DSH 正文为 14px/24px，工具控件通常更紧凑。
- 原回复含额外的品牌身份行、通栏处理过程分隔线；DSH 将注意力集中在正文和紧凑的过程入口。
- 只导入颜色与阴影令牌并不能统一组件外观：组件自身的圆角、描边、字体、内边距仍需要按角色协调。

## 改动文件

以下路径均相对项目根目录。

| 文件 | 改动 |
| --- | --- |
| `local-agent-web/frontend/src/styles/tokens.css` | 对齐中英文字体回退；UI 基准 14px；控件/卡片/浮层圆角 12/16/28px；侧栏 232px + 48px；统一内容宽度和留白 |
| `local-agent-web/frontend/src/styles/base.css` | 公共按钮颜色/阴影过渡、输入光标颜色；保留已有减少动效规则 |
| `local-agent-web/frontend/src/components/Sidebar.module.css` | 侧栏统一底色，取消会话列独立顶角，收紧品牌字号，整理新会话按钮、工作区行和选中反馈 |
| `local-agent-web/frontend/src/components/NavigationRail.module.css` | 配合 48px 导航宽度调整内边距，统一图标按钮圆角 |
| `local-agent-web/frontend/src/components/TopBar.module.css` | 标题区间距及细分隔线 |
| `local-agent-web/frontend/src/components/Composer.module.css` | 统一正文与输入区宽度，输入字号 14px、行高 24px、圆角 24px；轻描边、模式控件及蓝色发送/停止按钮 |
| `local-agent-web/frontend/src/features/conversation/MarkdownMessage.module.css` | 中文代码字体回退、行内代码与代码卡片圆角，减少代码工具栏的重复分隔 |
| `local-agent-web/frontend/src/features/conversation/Conversation.module.css` | 轮次留白、紧凑处理过程入口；品牌身份文字保留给辅助技术读取，视觉上减少额外身份条 |
| `local-agent-web/frontend/src/features/conversation/WelcomePanel.module.css` | 欢迎区标题字距、留白和快捷入口形状 |
| `local-agent-web/frontend/src/components/ui/primitives.module.css` | 公共按钮字重、默认 36px 高度、次要按钮边界；其他功能页通过共享令牌同步更新 |
| `local-agent-web/frontend/src/components/ui/AnchoredPopover.module.css` | 浮层细边界与柔和阴影，保留浮动定位和高度约束 |
| `local-agent-web/frontend/src/components/ui/EditorDialog.module.css` | 编辑弹窗底色、细边界与标题字距 |
| `local-agent-web/frontend/src/features/settings/SettingsDialog.module.css` | 设置搜索框、导航间距及公共圆角；保留小屏全屏布局 |
| `local-agent-web/frontend/src/features/tasks/TasksPanel.module.css` | 任务页留白、标题字距及返回控件圆角 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 更新侧栏尺寸验收；增加字体回退、圆角、聚焦和无横向溢出检查 |
| `docs/audits/2026-11-03-whole-workbench.md` | 本记录 |

## 验证

从 `local-agent-web/frontend` 执行：

```powershell
npm run build
npm test
npx playwright test -c playwright.dsh.config.ts --max-failures=2
npx playwright test -c playwright.preview.config.ts --project=1440x900 --project=390x844 -g '文件预览：|入口、文件预览|读取失败' --max-failures=1
npx eslint e2e/dsh-workbench.spec.ts
```

- 生产构建、TypeScript 检查通过；保留已有的大 chunk 和 Shiki 导入方式提示。
- 全量前端：73 个文件，727 项测试通过，52.7s。
- 界面验收：32 项通过，覆盖 1440×900、1280×800、768×900、390×844。
- 深色/浅色、空白会话、普通正文、队列、挂载提问、模型菜单、设置与嵌套编辑弹窗均检查了窗口边界；验证关闭嵌套编辑器后焦点恢复。
- 输入区同时验证 240px 窄列、模型选择、上下文帮助、发送可达性；导航切换和侧栏收起保留草稿。
- 文件面板补充回归：桌面和手机合计 10 项通过，包含 Office/PDF、Markdown/HTML、工作簿、文件入口与读取失败重试。
- 已刷新 8731 实际运行页面，检查真实会话与设置弹窗，确认加载新构建。
- `git diff --check` 通过；没有更新旧截图基线。

桌面和手机的干净验收截图已经复制到本次会话的可视化目录，避免被后续 Playwright 运行覆盖；原截图在 `frontend/test-results/dsh-workbench-shared-DSH-t-56978-and-focus-remain-consistent-*/dsh-whole-workbench.png`。

## 手动查看

刷新 `http://127.0.0.1:8731/`：查看历史正文与中文代码、输入框和模型菜单；进入设置后打开添加连接弹窗，按 Escape 返回；收起侧栏、切换任务页再返回，并将窗口缩到手机尺寸。

## 边界

这是对本项目全局视觉与公共组件的统一，没有宣称移植 DSH 全部插件、视图或操作。保留 chaiai 的功能入口与工作流。

没有改动或重测后端模型功能，没有触发真实模型请求。没有重建 exe，WebView2 打包环境仍需在页面定稿后的打包验收中检查。

# DSH 阅读尺度与细节对齐

> 后续更新：用户反馈本轮尺寸偏大，已在 `2026-11-04-dsh-standard-scale.md` 记录回调到正文 14px/24px 的结果。以下保留本轮实施时的历史尺度和验证记录。

2026-11-03 实施，2026-11-04 完成实际服务复核。针对用户反馈的字号偏小、工作区局促、会话边距和图片展示问题，比较 DSH 源码及运行页面后调整。本轮未重建 exe。

## 对比依据与改动

参考源码位于 `D:/ideaTest2/deepseek-harness`：`packages/client/ui-conversation/src/client/skeleton/ConversationRoot.module.css`、`InputBar.module.css`，以及 `ui-sidebar`、`ui-chat`、`ui-attachment` 的对应组件。两站比较时统一为 1440×900、设备比例 1，排除了初始窗口尺寸不同的影响。

| 项目 | 原页面 | 本轮结果 |
| --- | --- | --- |
| 正文 | 14px / 24px | 16px / 28px，小/标准/大为 14/16/18px |
| 工作区名称 | 12px | 15px，与主要会话文字统一尺度 |
| 侧栏内容区 | 232px | 272px，加功能轨后总宽 320px |
| 会话行 | 标题偏小 | 主要文字 15px、最小行高 40px，活动行保留状态说明 |
| 顶部操作 | 新建入口高 36px | 新建入口 44px，搜索、组标题和折叠图标协调放大 |
| 会话宽度 | 800px 上限中还包含左右内边距 | 正文按主区域的 64% 自适应，680–920px 上限范围内受实际可用宽度约束 |
| 输入框边界 | 与正文相同 | 参考 DSH 的宽度关系，左右各比正文宽 16px；分屏、窄屏均保持该关系 |
| 输入框 | 24px 圆角、14px 输入文字 | 桌面 28px 圆角、16px 输入文字，操作区间距及发送按钮同步调整 |
| 窄屏操作 | 底部控件挤在一行 | 按输入框自身宽度分行，保留完整模型名称及发送入口 |
| 图片 | 在蓝色文字气泡内，最多 220×160 | 独立于气泡；单张最长边 240px、多张 64px 缩略图、间距 10px、圆角 20px |
| 图片边界 | 固定宽高与窄屏约束可能改变比例 | 使用宽度和纵横比；小窗口等比缩小，极长图按 DSH 规则限制比例，原图仍可点开 |
| 图片失败 | 浏览器破图 | 可见失败文案和点击重试，保留原图查看、Esc 和焦点恢复 |

DSH 的标准正文实测也是 14px。本轮依据用户明确提出的“更大、更从容”，采用其布局关系并将本项目默认阅读字号增至 16px，不宣称所有像素与 DSH 相同。两站字体栈原本已经接近，本轮主要调整字号、行高、字重层次和布局比例，没有新增字体依赖。

文件预览仍保留前一轮紧凑代码行高，不用全局放大替代正文和代码的区分。

## 文件清单

以下路径相对项目根目录。

| 文件 | 作用 |
| --- | --- |
| `local-agent-web/frontend/src/styles/tokens.css` | 阅读字号、行高、侧栏尺度和统一窄屏边距 |
| `local-agent-web/frontend/src/app/App.module.css` | 以实际主区域宽度计算阅读宽度 |
| `local-agent-web/frontend/src/components/Sidebar.tsx` | 工作区文件夹及折叠图标尺寸 |
| `local-agent-web/frontend/src/components/Sidebar.module.css` | 工作区、会话、新建、搜索和悬停操作的间距尺度 |
| `local-agent-web/frontend/src/components/NavigationRail.module.css` | 品牌图标尺寸 |
| `local-agent-web/frontend/src/components/TopBar.tsx` | 标题栏文件夹图标尺寸 |
| `local-agent-web/frontend/src/components/TopBar.module.css` | 页签文字及窄屏标题字号 |
| `local-agent-web/frontend/src/components/Composer.module.css` | 比正文更宽的输入框、圆角、操作行、64px 待发图片及窄屏布局 |
| `local-agent-web/frontend/src/features/conversation/Conversation.tsx` | 图库与文字气泡分别展示，纯图片不产生空气泡 |
| `local-agent-web/frontend/src/features/conversation/Conversation.module.css` | 正文宽度、气泡和图库样式 |
| `local-agent-web/frontend/src/features/conversation/MarkdownMessage.module.css` | 正文、标题及回答内代码的字号层次 |
| `local-agent-web/frontend/src/features/conversation/WelcomePanel.module.css` | 新会话引导的按钮和说明字号 |
| `local-agent-web/frontend/src/features/conversation/MessageImages.tsx` | 单图比例、多图缩略图、失败重试及原图查看 |
| `local-agent-web/frontend/src/features/conversation/MessageImages.test.tsx` | 横图、竖图、长图、小图和失败重试 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 字号、阅读边界、图库、窄屏和分屏的实际浏览器检查 |
| `docs/audits/2026-11-04-dsh-reading-scale.md` | 本记录 |

## 验证结果

实施期间执行：

```powershell
cd local-agent-web/frontend
npm test -- --reporter=dot
npx playwright test -c playwright.dsh.config.ts --max-failures=1
npx playwright test -c playwright.preview.config.ts --max-failures=1
```

- 全量前端单元测试：74 个文件、740 项通过。最终图库比例修正后，相关图库和会话测试 45 项再次通过。
- 最终工作台浏览器测试：44 项通过，覆盖 1440×900、1280×800、768×900、390×844，并额外检查 320px 窄屏和 1910px 宽屏。
- 文件面板浏览器测试：63 项通过、1 项手机鼠标拖宽按设计跳过。包括代码 19px 单倍行高、长文档连续滚动、Office/PDF、Markdown/HTML、工作簿及页签交互。
- 浏览器实测正文与输入框左右边界差各为 16px；打开右栏后仍一致。1910px 窗口正文达到 920px 上限。
- 单张测试图为 240×160，多图为 64×64；320px 窗口中单图缩小后比例仍为 1.5。点开原图、Esc 关闭及返回缩略图焦点通过。
- 大字号正文 18px，无页面横向溢出；窄输入区的模型名称未被截掉，按钮没有相互覆盖。
- 浏览器验收启动时执行生产构建，包含 TypeScript 检查；本轮修改的 TSX、测试 ESLint 无错误，已跟踪修改文件的空白检查通过。没有更新截图基线。

2026-11-04 续做时，原本两个本地服务均已停止。重新用项目现有配置启动本项目后端，使用现有数据目录，打开真实既有会话，确认实际运行页面正文为 16px、行高 28px、侧栏内容宽 272px。服务保留运行于 `http://127.0.0.1:8731/`。没有发送模型请求或保存用户外观设置。

## 手动验收与边界

刷新 8731 页面，查看已有长回答；开关右侧工作面板，观察正文与输入框边界。查看单张和多张历史图片，点开原图后按 Esc 返回；在设置中切换文字大小，检查段落与按钮是否仍可读。

截图保存在本次会话可视化目录：`comfortable-workbench-desktop.png`、`comfortable-workbench-mobile.png`、`comfortable-image-gallery-desktop.png`、`comfortable-image-gallery-mobile.png` 为浏览器测试样本；`comfortable-workbench-live.png` 为 11 月 4 日实际服务截图。

未单独验收重新打包后的 exe/WebView2；本轮不修改后端业务、数据协议或真实模型调用行为。浏览器测试的图片为中性样本，不能据此声称所有用户图片格式、系统缩放和极端窗口尺寸均已验证。

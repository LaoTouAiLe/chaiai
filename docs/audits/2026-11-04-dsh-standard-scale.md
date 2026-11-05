# 回到 DSH 标准阅读尺度

2026-11-04。用户反馈上一轮整体偏大，本轮将字号及控件高度回调，保留已完成的阅读宽度、留白、图库和交互优化。本记录取代上一轮记录中的当前字号和高度口径；上一轮测试结果仍作为历史记录保留。

## 原因与结果

上一轮将“更从容”同时落实为字号、行高和控件尺寸放大，正文升至 16px/28px、侧栏主要文字 15px，输入框文字区最小高度 58px。这超出了 DSH 默认尺度。

本轮再次核对本地 DSH 源码中的 `UserMessageBubble.module.css`、`MarkdownText.module.css` 和 `InputBar.module.css`，按标准正文 14px、行高 24px、输入文字区最小高度 36px、发送按钮 34px回调。实机现有会话中确认正文 14px/24px、工作区文字 14px、文字输入区高 36px、输入卡片整体高约 99.6px；之前 DSH 同尺寸实测约 98px。

- 正文小/标准/大档为 12/14/16px，字号设置仍可用。
- 侧栏主要文字 14px，会话最小行高 32px；活动会话继续显示第二行状态。
- 品牌标题由 20px 收至 18px；新建入口由 44px 收至 36px，工作区组标题行收至约 34px。
- 用户文字气泡内边距回到 10px/16px；标题字号同步收回。
- 输入文字区由 58px 收至 36px，桌面发送/停止按钮从 40px 收至 DSH 的 34px，减少卡片内的额外留白。
- 保留正文自适应宽度、输入框两侧各宽 16px的关系、侧栏可用宽度、分组间距和悬停菜单。
- 保留单张图片等比、多张缩略图、失败重试、原图查看和焦点恢复。文件代码行高不变。
- 手机触摸输入继续保留至少 16px 字号和较大触摸目标，DSH 源码也有此规则；避免系统自动放大输入页面。

## 文件

以下路径相对项目根目录。

| 文件 | 改动 |
| --- | --- |
| `local-agent-web/frontend/src/styles/tokens.css` | 收回全局字号、行高和会话行高 |
| `local-agent-web/frontend/src/components/Sidebar.module.css` | 标题、组标题、新建入口、会话行的协调尺寸 |
| `local-agent-web/frontend/src/components/Composer.module.css` | 输入文字区、卡片内边距、模式与发送按钮高度 |
| `local-agent-web/frontend/src/features/conversation/Conversation.module.css` | 文字气泡内边距 |
| `local-agent-web/frontend/src/features/conversation/MarkdownMessage.module.css` | 回答标题的字号层级 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 标准、大小字号及工作区的浏览器预期 |
| `docs/audits/2026-11-04-dsh-standard-scale.md` | 本记录 |

## 验证

从 `local-agent-web/frontend` 执行：

```powershell
npm test -- --reporter=dot
npx playwright test -c playwright.dsh.config.ts --max-failures=1
npx playwright test -c playwright.preview.config.ts --project=1440x900 --project=390x844 --max-failures=1
```

- 全量单元测试：74 个文件、747 项通过。
- 工作台浏览器测试：44 项通过；含四种常用尺寸、320px 窄屏、1910px 宽屏、大小字号、分屏、模型选择、设置导航和图库。
- 生产构建与 TypeScript 检查随浏览器测试启动通过，修改文件空白检查通过。
- 已在 8731 实际服务中打开现有会话，核实字号与卡片高度，保存 `dsh-normalized-live.png`；测试样本截图为 `dsh-normalized-desktop.png` 和 `dsh-normalized-mobile.png`，均位于本次会话可视化目录。
- 文件面板桌面/手机回归：31 项通过，1 项手机鼠标拖宽按设计跳过；代码行高、Office/PDF 和长文档连续阅读通过。

## 复核与边界

刷新 `http://127.0.0.1:8731/`，查看已有回答和输入框；打开工作面板，观察两边留白。可在设置中切换文字大小，检查字号档位与分类操作。

本轮主要校正默认尺寸，未重建 exe，也未单独验收 WebView2；没有修改后端功能或调用真实模型。不宣称本产品所有结构与 DSH 一一相同：本项目仍有独立功能轨和模式切换控件。

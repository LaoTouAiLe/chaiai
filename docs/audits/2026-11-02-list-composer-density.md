# 会话列表与输入区减负

## 改动与原因

本轮落实会话列表和输入区的精简方案。原列表给每条空闲/已完成会话重复展示状态与时间，占用第二行；输入区同时展示模式、模型、附件、Token 数字和发送，窄窗下模型名称空间不足。

- 空闲、已完成会话以单行为主，保留标题、选中态、未读标记；完整名称、状态与时间放在悬停提示中，时间仍供辅助技术读取。
- 运行、等待审批、等待回答继续显示第二行说明；任务索引产生的“需人工核对”仍按已有状态映射保留。
- 输入区按模式、模型、操作区分组，保留两种模式的一键切换；模型按钮有下拉指示、长名称截断与完整标题提示。
- Token 数字从常驻操作栏收起，只保留上下文圆环。悬停、键盘聚焦和点击详情继续提供用量、窗口、比例及口径；未知值保持未知，超限/接近上限状态沿用现有规则。
- “未流式”“已停止”作为简短说明单独排布；附件、发送/停止的原有动作不变。
- 极窄输入列按自身宽度换行，将模式独立成行，避免挤压模型与发送。上下文帮助浮层的宽度也跟随输入列，修复窄列下被主画布裁切的问题。

## 修改文件

以下路径相对项目根目录：

| 文件 | 说明 |
| --- | --- |
| `local-agent-web/frontend/src/components/Sidebar.tsx` | 根据状态选择单行/双行展示，并补充完整悬停信息 |
| `local-agent-web/frontend/src/components/Sidebar.module.css` | 紧凑行高、行间距与状态点对齐 |
| `local-agent-web/frontend/src/components/Sidebar.test.tsx` | 验证普通会话精简、未读标记及待回答状态保留 |
| `local-agent-web/frontend/src/components/Composer.tsx` | 整理控件分组、模型提示、状态说明与上下文紧凑模式 |
| `local-agent-web/frontend/src/components/Composer.module.css` | 工具栏网格布局、窄列适配、按钮内距及状态说明样式 |
| `local-agent-web/frontend/src/components/ContextMeter.tsx` | 可选 compact 模式；独立使用时保留原来的数字展示 |
| `local-agent-web/frontend/src/components/ContextMeter.module.css` | 紧凑圆环按钮，以及跟随输入列宽度的帮助浮层 |
| `local-agent-web/frontend/src/components/ContextMeter.test.tsx` | 验证紧凑模式仍能读取上下文比例与精确用量 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 增加行高、控件位置、模型/上下文浮层、窄列与提示裁切检查 |
| `docs/audits/2026-11-02-list-composer-density.md` | 本记录 |

## 验证

- 生产构建、TypeScript 检查通过。
- 5 个相关单测文件、98 项通过：Sidebar、ContextMeter、Composer、Composer 附件、LiveApp。
- 完整界面浏览器验收 28 项通过，覆盖 1440×900、1280×800、768×900、390×844。
- 最后收紧模型按钮内距后，四档窗口的输入区验收再通过 4 项，验证“选择模型”占位文字完整可见。
- 会话行检查：空闲/已完成行高不超过 40px，运行中保留第二行；未读与时间信息没有丢失。
- 输入区检查：模式、模型、附件、上下文和发送不重叠、不越出窗口；模型菜单、上下文详情可打开，Escape 关闭后焦点恢复；帮助浮层不被主画布裁切。
- 额外将输入列限制为 240px，验证模式换行、模型和发送可达，以及帮助浮层边界。
- 修改的 TSX、单测与浏览器用例 ESLint：0 错误、0 警告；`git diff --check` 通过。
- 运行中的前端代理健康检查返回 200：`http://127.0.0.1:5173/api/health`。

复现，在 `E:/pylianxi/chaiai/local-agent-web/frontend` 执行：

```powershell
npm run test -- src/components/Sidebar.test.tsx src/components/ContextMeter.test.tsx src/components/Composer.test.tsx src/components/Composer.attachments.test.tsx src/app/LiveApp.test.tsx
npx playwright test -c playwright.dsh.config.ts
```

人工验收：刷新 `http://127.0.0.1:5173/`，查看会话列表；点击模型选择与上下文圆环，调整窗口宽度；检查运行中或待回答的会话仍显示状态说明。

截图副本位于：

`C:/Users/asus/.codex/visualizations/2026/10/27/01a17d34-3311-7422-b8e9-515ae56bd639/list-composer/`

`mobile-composer.png` 和 `desktop-composer.png` 来自最后一轮输入区复核；`session-list.png` 来自通过的完整界面验收。

## 边界

本轮只改会话列表和输入区展示。未改后端、未改变队列和提问调度、未重建 exe；WebView2 宿主实机显示未验证。原有像素截图基线未更新。浏览器验收使用固定响应，不修改真实会话数据、不调用真实模型。

# 整套 AI 回复展示补齐（2026-11-05）

本轮继续按用户授权对齐 DSH 的回复阅读与交互细节，覆盖正文、表格、公式、脚注、图片、工具结果、来源和执行状态。不是将模型正文强制改为 JSON；继续消费既有结构化工具记录。没有重建 exe，没有修改供应商协议、权限或执行规则。

## 用户可见变化与参照

| 部分 | 已实现行为 |
| --- | --- |
| 正文与思考 | 标题接列表的留白、嵌套列表、引用与紧凑思考分开排版；思考中的代码采用次级色，历史 Markdown 不因其他消息更新反复解析 |
| 表格 | 两、三列铺满正文列，四列起独立横向滚动；保留单元格对齐，支持键盘滚动、复制 TSV，可直接粘贴表格软件 |
| 公式 | 行内 `$...$`、块级 `$$...$$` 和 math 围栏通过 KaTeX 渲染；超宽公式在自身区域滚动，未完成的公式不让页面崩溃 |
| 脚注 | 中文标签、本页前后跳转；多条消息的引用、目标和说明标签 ID 独立，不打开额外标签页 |
| 正文图片 | 本地图片走已有右栏；HTTP(S) 图片点击后加载、失败可重试、可放大，Escape 关闭后恢复焦点；地址变化会重置加载决定与失败状态 |
| 文件与搜索结果 | 文件目标可直接打开右栏；复制结果与复制标题分开，正文支持自动换行；文件行号继续采用后端真实行号 |
| 命令结果 | 退出状态、stdout、stderr 分开表达；错误输出轻量强调，面板可滚动、复制本次返回原文、切换换行 |
| 通用 JSON 结果 | 完整对象/数组按键、值、类型和层级展开；每层先显示 20 项，可继续显示，深层保留文本；不完整 JSON 和超限结果回退原文 |
| 来源 | 默认前三条，展开可看全部；标题、域名、引用/读取事实和日期分层，不把搜索命中说成已读取 |
| 工具过程 | 过程标题收成轻量说明行；分组内运行、待执行、失败和结果未确认的操作默认展开，保留用户手动选择 |

视觉依据来自本机 DSH 的 `MarkdownText.module.css`、`render.tsx`、`katex.tsx`、`JsonBlock.tsx` 和 `ToolDetails.module.css`。沿用仓库已有的 DSH MIT 许可记录。公式管线依据 [remark-math 官方用法](https://github.com/remarkjs/remark-math)；可信命令、宏次数和尺寸限制参考 [KaTeX 安全配置](https://katex.org/docs/security.html)。

## 本轮文件清单

以下相对仓库根目录：

| 文件 | 作用 |
| --- | --- |
| `local-agent-web/frontend/src/features/conversation/MarkdownTable.tsx`、`.module.css`（新增） | 语义化表格、布局、滚动与 TSV 复制 |
| `local-agent-web/frontend/src/features/conversation/MarkdownImage.tsx`、`.module.css`（新增） | 图片按地址隔离的加载、重试、本地打开与放大 |
| `local-agent-web/frontend/src/features/conversation/MarkdownMessage.tsx`、`.module.css`、`.test.tsx` | 数学管线、表格元信息、脚注隔离、正文/思考排版与专项测试 |
| `local-agent-web/frontend/src/features/conversation/ImageLightbox.tsx` | 放大原图不发送 referrer |
| `local-agent-web/frontend/src/features/conversation/Conversation.tsx`、`.module.css`、`.test.tsx` | 轻量过程头及分组状态默认可见性 |
| `local-agent-web/frontend/src/features/conversation/WebSources.tsx`、`.module.css`、`.test.tsx` | 来源卡片和展开/收起 |
| `local-agent-web/frontend/src/features/tool/StructuredResult.tsx`、`.module.css`（新增） | 通用 JSON 展示和原文回退 |
| `local-agent-web/frontend/src/features/tool/ToolCards.tsx`、`.module.css`、`.test.tsx` | 结果复制、换行、搜索文件入口与统一详情面板 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 综合图文与多类型工具结果浏览器场景 |
| `local-agent-web/frontend/package.json`、`package-lock.json` | 新增并锁定 remark-math 6.0.0、rehype-katex 7.0.1、KaTeX 0.19.1；没有升级原有依赖 |
| `ARCHITECTURE.md` | 补充本轮展示契约与限制 |

本轮保留此前未提交的样式和后端展示阶段改动；没有清理其他任务的临时文件。

## 验证

- 前端全套：76 个测试文件，765 项通过。
- 浏览器：四种视口合计 60 项通过，包含新增综合图文、多类型工具结果的 8 项场景。
- TypeScript 与前端生产构建通过（浏览器全局准备执行 `npm run build`）；改动组件、测试与浏览器场景的 ESLint 通过。
- 桌面/手机、明暗主题截图已查看；没有用断言通过代替视觉检查。
- 8731 源码服务已按原配置启动；健康检查返回 `ok`，实际响应的首页与最新 `frontend/dist/index.html` 一致。继续使用 `E:\chaiai-data`。

上一轮后端展示阶段的测试记录保留在 `2026-11-04-dsh-response-presentation.md`，不将旧结果当成本轮重跑。本轮没有再改后端。

本轮浏览器场景覆盖 1440×900、1280×800、768×900、390×844：表格宽度与键盘横滚、公式局部布局、脚注标签、图片首次失败/重试/放大/焦点、文件行号与继续显示、命令失败、JSON 展示、来源展开、明暗主题，以及原有流式、断线、刷新和历史兼容回归。

可在 `local-agent-web/frontend` 运行：

```powershell
npm test
npm run typecheck
npx playwright test -c playwright.dsh.config.ts --max-failures=1
```

手动检查：刷新 8731，打开含表格、图片或工具调用的会话；试用表格横滚/复制、图片加载与放大、结果复制/换行、来源展开。长回复可上下阅读，辅助信息与最终回答应保持清楚的层级。

## 限制与未验证

- 未重建 exe，未验证打包 WebView2，也未逐家验证真实模型端点。
- 图片仍需用户点击才请求；不加载脚本协议，也不放开正文原始 HTML。
- JSON 只展示本次工具返回的脱敏预览，超过 60,000 字符不解析；六层后使用文本。复制不能取得后端未返回的部分。
- 数学支持 Markdown 公式语法，不是完整 LaTeX 文档引擎；没有更改模型返回格式来强制适配页面。
- 既有代码块超过 60,000 字符的高亮回退、非持久行标记、文件入口不跳具体行等限制保持原口径。

# v0.4 UI 参考基线（T25 产物）

本目录冻结 v0.4 独立 Web 页面的**视觉与交互参考基线**：参考来源、可复现的采集方法、页面状态矩阵、设计
Token 与截图资产。它是 `V0_4_DEVELOPMENT_PLAN.md` T25 与 T26 的输入，不是本项目自身的界面规范。

> 权威边界：页面**要做什么**由 `PRODUCT_EVOLUTION_ROADMAP.md` §6 与 `V0_4_DEVELOPMENT_PLAN.md` §6 决定；
> 本目录只回答**长什么样、交互怎么反馈**。冲突时以两份规划文档为准。
>
> 状态：参考资产已采集并冻结（2026-09-23）——**参考基线已冻结，本项目特有状态的原型待验收**。
> 这两件事不等价：`state-matrix.md` §3 中标 ⚠️ 的状态与 §5 的待补原型各自通过前，不得描述为"已高保真还原"。
> 本目录只包含参考截图与 Token 证据，**不含**本项目实现。

## 1. 参考来源（已冻结）

| 项 | 值 |
|---|---|
| 仓库 | `D:\ideaTest2\deepseek-harness`（本地检出，非本项目依赖） |
| 冻结 commit | `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e` |
| commit 日期 | 2026-08-21 20:03:37 +0800 |
| commit 主题 | `Merge pull request #2908 from deepseek-harness/release/dsh-0.1.1-rc.2` |
| 包版本 | `@deepseek-ai/dsh-root` 0.1.1-rc.2 |
| 采集时工作区 | 干净（仅有一个与本项目无关的未跟踪 `.codebuddy/`） |
| 页面来源 | `apps/web/dist`（Vite 构建产物），由 `apps/cli` 的 `dsh web`（= `--profile web`）托管于 `http://127.0.0.1:3080` |

## 2. 采集方法（可复现）

### 2.1 启动参考页面

```cmd
set DSH_HOME=E:\tmp\dsh-ref-home
cd /d D:\ideaTest2\deepseek-harness
pnpm dsh web --port 3080 --no-open
```

要点与理由：

- `apps/web` **不是独立应用**：其 `vite.config.ts` 的 `rejectStandaloneServe()` 会拒绝裸 `vite`，
  因为前端需要 `apps/cli` 侧注入的 `window.__DSH_BOOT__`（`packages/client/modules/src/index.ts`
  的 `bootInjections()`）。因此只能走 `dsh web`。
- 该仓库本地**依赖与构建产物都已存在**（`node_modules`、`packages/*/lib`、`apps/web/dist`），
  本次采集**没有**执行 `pnpm install` 或 `pnpm run build`。
- `DSH_HOME` 把全部用户数据从 `~/.dsh` 重定向到 E 盘：本机 C 盘已满，禁止任何写 C 盘的操作。
  该变量的语义见 `packages/util/home-paths/src/index.ts`（优先级：显式配置 > `$DSH_HOME` > `~/.dsh`）。

### 2.2 用假传输渲染页面状态

参考应用内置一个**客户端假服务器**：URL 带 `fixture` 查询参数时，`packages/client/connection/src/client/index.ts`
的 `apply()` 选择 `FixtureApiClient` 而不是真实 HTTP 传输。可用的查询参数由同包 `fixture.ts` 的
`fixtureOptionsFromLocation()` 定义：

| 参数 | 效果 |
|---|---|
| `?fixture` | 载入内置世界：2 个工作区、4 个会话（含一个 75 轮历史会话、一个子会话、一个持续“运行中”的会话） |
| `?fixture=empty` | 空世界：无工作区、无会话 → 首次启动/选择工作区状态 |
| `?fixturePrompt=reject` | 提交被拒绝 |
| `?fixtureAttach=fail` | 附加工作区失败 |
| `?fixtureSessionCreate=drop-response` | 新建会话响应丢失 |
| `?fixtureFrames=workspace-first` | 调整推送帧顺序 |

内置世界的内容（见 `fixture.ts` 的 `buildAlphaLog()` / `createFixtureWorld()`）覆盖：普通问答、可折叠
reasoning、上下文字段注入、工具调用（`fx-bash` / `fx-write` / `edit` / `write` / `read` / `run_code` /
`grep` / `glob` / `web_search` / `web_fetch` / `todo_write`）、工具失败（`read` 返回 ENOENT 子派发）、
终端输出（ANSI 颜色 + 非零退出码 + 高度折叠）、搜索卡（按文件分组与截断）、图片消息、输出上限截断提示、
常驻待审批请求与常驻待回答提问（两者都会接管输入区）。

### 2.3 截图脚本与浏览器

- 驱动：Playwright（来自 `D:\ideaTest2\deepseek-harness\apps\web\node_modules`）。
- 浏览器：本机已下载的 Chromium 1217，**显式指定** `executablePath`
  （`%LOCALAPPDATA%\ms-playwright\chromium-1217\chrome-win64\chrome.exe`），避免重新下载写 C 盘。
- 固定条件：`deviceScaleFactor=1`、浅色主题、`locale=zh-CN`、`timezoneId=Asia/Shanghai`、
  `reducedMotion=reduce`、视口 1440×900 与 1280×800。
- 等待策略：`waitUntil: 'domcontentloaded'`。**不能用 `networkidle`**——页面持有常驻事件流，永不空闲。

### 2.4 内测声明弹窗的处理（重要）

首次启动会弹出 `内测声明` 模态框。在 `?fixture` 下它**无法通过界面关闭**：确认状态写入 Host settings
scope（`packages/client/ui-settings-models/src/client/welcome-store.ts`），而假传输不提供该域，点击后
显示“暂时无法保存确认状态，请重试”。该弹窗同时给 `#root` 加 `inert` 并保留一层 `_mask_*` 遮罩，
会拦截全部指针事件。

因此：`00-first-run-beta-notice` 是它的原始截图；其余截图在采集前移除了该弹窗所在层
（连同其 `[role="presentation"]` 容器与 `inert` 属性）以露出下层页面。这是**采集手段**，不代表本项目
应该跳过首次启动声明——v0.4 的初始设置流程仍按 `V0_4_DEVELOPMENT_PLAN.md` §6.8 实现。

## 3. 目录结构

```text
docs/ui-reference/
├── README.md                       # 本文件：来源、采集方法、边界、缺口、验收方式
├── state-matrix.md                 # 页面状态矩阵：状态 → 截图 → 复现方式 → 参考实现位置 → 本项目任务
├── design-tokens.md                # 设计 Token 表（浅色）：调色板 / 语义 / 字体 / 阴影 / 动效
├── tokens/
│   ├── tokens-light-resolved.txt   # 350 个 `--dsw-*` 变量在浅色主题下的**解析后**取值（采集证据）
│   └── tokens-declared.txt         # 423 个变量的**声明原文**（含暗色分支、字体阶梯、阴影、渐变）
└── screenshots/
    ├── 1440x900/                   # 29 张
    └── 1280x800/                   # 11 张
```

截图总体积 2.64 MiB。

## 4. 借鉴与不借鉴的边界

**借鉴（视觉与交互结果）**：常驻会话壳、紧凑工具行与展开卡、输入区接管（审批/提问）、计划条、
diff 摘要卡、轨迹时间线与检查器、每轮统计行、圆形上下文计量器、设置页的组件语言与状态反馈。

**不复制**：源码、品牌与 Logo、鱼形标记、产品名称、专有图标资产、`dist` 产物、Cordis 插件运行时、
Slot/插件注册体系、Monorepo 包结构。本项目使用独立实现与自身标识。

参考页面里属于**其产品边界**、本项目 v0.4 不做的部分（插件配置页、Cordis 面板、Agent 预设、Skill、
Subagent、WebSocket 终端等）只用于理解其信息层级，不作为 v0.4 的实现范围。

## 5. 缺口（无法从参考复现的状态）

以下状态在冻结版本 + 离线假传输下**没有**产出截图。缺口如实列出，不用文字描述替代视觉验收。
**裁定结果（2026-09-23）**：前两项不索取参考截图，改用本项目自定基线并由 T26 早期视觉检查点产出原型；
**刷新恢复的归属见 `state-matrix.md` §3 / §5，留给 T32 做本项目验收**（它不在下表，故不占表格行）。
验收前不得把任一项描述为"已高保真还原"。

| 缺口 | 原因 | 裁定与归属 |
|---|---|---|
| 多行/多 hunk 的完整文件 diff | 内置世界的 `edit` / `write` 记录只产生单行变更摘要（`+1 -0 · 1 file`），没有多 hunk 样本 | **接受本项目自定基线**：T26 用自有 fixture 做至少双 hunk（含增删行与长行）的静态原型，两个视口截图并确认；T32 再验真实 diff 与审批行为。参考的 `07` 只用于卡片外观 |
| 真实供应商错误（鉴权失败、限流、超时/重试耗尽的界面） | 假传输不带这些分支，需要真实 provider 或 `DSH_SNAPSHOT=record` 且有 API Key（见 `apps/web/tests/scaffold.ts` 的模式解析） | **接受本项目自定基线**：T26 用确定性 fixture 出三种错误原型，沿用已冻结的颜色/排版/组件语言；必须有可执行提示，不得露出原始响应或密钥；不接真实 provider、不消耗 API Key |
| 后台任务/取消/队列 | 属于其插件能力，冻结版本的内置世界不含真实后台任务 | 本项目 v0.4 不做（属 v0.6），无需基线 |
| 记忆面板、MCP、Skill 面板 | 参考产品边界不同 | 本项目 v0.4 不做（属 v0.5+） |
| 深色主题 | T26 视觉基线固定浅色主题 | 如需要，另行采集并单独冻结 |

另有两处**只在参考项目中成立、本项目需要重新决定**的细节，已记录为待决，不当作基线：

1. 参考的模型设置页是“提供方 + 模型目录”双层结构（`ModelsSection.tsx` / `ProviderEditor.tsx` /
   `DeepSeekModelsEditor.tsx` / `ModelListEditor.tsx`）；本项目 v0.4 只需要“多个 OpenAI 兼容聊天连接”
   （`V0_4_DEVELOPMENT_PLAN.md` §6.8），需要**删减**而不是照搬。
2. 参考的 `模型与推理等级` 是同一个小菜单（模型 + 推理等级）；本项目 v0.4 的会话模型选择只切换连接。

## 6. 验收方式

T26 与 T37 的视觉验收按以下步骤执行，逐项留痕：

1. 在 1440×900 与 1280×800 分别生成本项目对应状态的截图（`state-matrix.md` 的“本项目截图名”列）。
2. 与 `screenshots/<viewport>/` 中的参考截图逐项对照：布局比例、尺寸、间距、颜色、字体层级、圆角、
   边框、阴影、图标尺寸、信息密度、悬停/选中/禁用状态、动画节奏。
3. 每一处差异写出**书面理由**（功能边界删减 / 安全边界差异 / 本机度量差异）。“主观上差不多”不是理由。
4. 不允许出现未经设计的浏览器默认控件、裸露原始 JSON、关键错位、文本溢出、低对比度状态或临时组件。

参考实现位置（组件路径）见 `state-matrix.md`，可用它定位“该状态在参考里由谁渲染”，但**只能读，不能复制代码**。

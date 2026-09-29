# local-agent-web 前端

v0.4 独立 Web 前端。技术栈与边界由
[V0_4_DEVELOPMENT_PLAN.md §2/§5](../../V0_4_DEVELOPMENT_PLAN.md) 与
[ARCHITECTURE.md §15](../../ARCHITECTURE.md) 冻结。

> 默认入口 `/` 是真实会话页面：通过应用服务 API 新建/打开会话、提交消息、接收 SSE 增量，
> 刷新后以服务端快照恢复；模型设置与会话级模型选择也已接线。`?state=` 只保留给 T26 的静态视觉基线。
> T31/T32 的过程详情与审批操作、T34 的真实用量仍未接完，v0.4 尚不可按正式发布口径验收。

## 命令

```text
npm ci                 # 按 package-lock.json 安装（.npmrc 已固定 legacy-peer-deps，见下）
npm run typecheck      # tsc --noEmit
npm run lint           # eslint
npm run test           # vitest run（单元测试）
npm run test:e2e       # playwright（先 build，再对 dist 截图比对）
npm run test:live      # 真 Python 服务 + 确定性假模型：两轮/刷新/模型选择
npm run test:live:streaming  # 真 SSE 增量、完成与刷新去重
npm run test:live:variants   # 长回答、空回答与模型异常
npm run build          # typecheck + vite build → dist/
npm run dev            # 开发服务器 127.0.0.1:5173，把 /api 代理到 127.0.0.1:8000
npm run generate:api   # 从受控 OpenAPI 生成 src/api/schema.gen.ts
npm run check:api      # 漂移检查：重新生成后必须与已入库的生成文件逐字节相同
```

## 传输类型与受控 OpenAPI（T29）

前端不手写接口形状，只从**受控 OpenAPI** 生成：

```text
后端契约（FastAPI 路由 + 应用层 DTO）
  → uv run python scripts/export_openapi.py        # 落盘 docs/api/openapi.json（入库，唯一来源）
  → npm run generate:api                           # 生成 src/api/schema.gen.ts（不得手改）
  → npm run check:api                              # 重新生成后逐字节比对；检查不留改动
```

- 改契约的顺序：改后端 → 重新导出 → `npm run generate:api`；两处都有漂移检查
  （后端 `pytest tests/test_api.py` 断言"落盘文档 == 真实应用"，前端 `npm run check:api`）。
- 生成文件只描述**传输**形状（路径、请求体、响应体、错误信封 `{"error": …}`）；业务语义仍以
  `ARCHITECTURE.md` §15 为准。真实页面经 `features/*/adapters.ts` 转成展示模型，`src/fixtures/`
  只给视觉原型使用；两者都不直接读取 Graph State（§6.1）。
- `.npmrc` 里的 `legacy-peer-deps=true` 是**有意**的：`openapi-typescript` 的 peer 只到 `typescript@^5.x`，
  而本项目用 TS 6。副作用见该文件注释——新增依赖后请顺手跑 `npm run typecheck`。

## 本机环境约定

- **Node / npm**：本机验证过 `node v22.20.0` + `npm 10.9.3`，`package.json` 的 `engines` 与之对齐；
  依赖的精确版本由 `package-lock.json` 锁定。
- **npm 缓存**：本机 C 盘已满，安装时必须把缓存指到数据盘，否则会写 C 盘：
  `$env:npm_config_cache='E:\npm-cache'`（不写 `~/.npmrc`）。
- **本机的批量删除拦截**（环境问题，不是项目问题）：`npm ci` 会先删 `node_modules`，本机拦截一次超过 500 个
  文件的删除 → `npm ci` 中途失败并留下残缺依赖树。本机请用 `npm install`（幂等）；万一失败，重跑一次即可恢复。
- **Playwright 浏览器**：1.63 需要 Chromium **1243**，本机原有的 1217 版本不匹配，已安装到
  `E:\pw-browsers`。该路径放在**被 gitignore 的本机文件** `playwright.local.json` 里由
  `playwright.config.ts` 读取，因此配置本身保持可移植：

  ```json
  { "browsersPath": "E:\\pw-browsers" }
  ```

  环境变量 `PLAYWRIGHT_BROWSERS_PATH` 优先级最高，CI 可用它覆盖。首次准备：把同一变量指向目标目录后执行
  `npx playwright install chromium`。

## 原型宿主与场景

`src/app/App.tsx` 默认进入真实的 `LiveApp`；只有传 `?state=` 时才选择 fixture 场景，供视觉基线复验。
原型标识条已删除，fixture 按钮不能当作真实操作入口。

```text
/?state=shell             常驻会话壳：侧栏、会话头、对话流、计划条、工具行、统计行、上下文计量器
/?state=diff              早期检查点 1：双 hunk 完整文件 diff（含增删行与超长行）
/?state=errors            早期检查点 2：供应商错误态三种（鉴权失败 / 限流 / 超时重试耗尽）
/?state=tools             其余工具卡：读取窗口 / 搜索两种形状 / 终端非零退出 / 待办 / 未知工具回退
/?state=approval          审批接管输入区：动作、目标、影响、风险 + 一次性批准或拒绝
/?state=approval-expired  审批失效态：不再提供按钮，只说明未执行任何操作
```

```text
/?state=settings-models  设置页：连接六态（已验证/未验证/检测中/检测失败/能力不支持/密钥缺失）+ 删除确认 + 下一轮生效
/?state=settings-empty   设置页空态：还没有连接，且系统凭据存储不可用
/?state=model-menu       输入区模型菜单：切换只对下一次提交生效，正在运行的一轮固定原连接
```

检索与记忆（只给来源定位，不给正文 / 向量 / 评分）：

```text
/?state=memory-hit      命中：来源类型 + 安全引用 + 用途，并标明不展示什么
/?state=memory-miss     未命中 + 写入长期记忆失败（明说不编造出处）
/?state=memory-failed   检索失败：与"未命中"分开陈述
```

空 / 加载 / 禁用：

```text
/?state=empty-no-workspace  未选工作区：输入区禁用并用真实元素说明原因，侧栏不高亮
/?state=empty-session       空会话（已选工作区）：空态标题 + 可用输入区
/?state=running-empty       运行中且无内容：阶段态 + 可写草稿但暂时禁用发送（不渲染停止按钮）
/?state=loading-history     读取事件中：骨架只有形状、无文字
```

计量器与侧栏（矩阵 22/23/24）与服务端冲突码：

```text
/?state=meter-open         上下文计量器展开：精确读数 + 三态口径 + 窗口与缓存基线规则
/?state=sidebar-collapsed  侧栏收起：窄轨只保留品牌、新建、展开与设置，不渲染会话树
/?state=error-codes        SESSION_BUSY / IDEMPOTENCY_CONFLICT / RECOVERY_REQUIRED 三种可执行性
```

矩阵 22 的**悬停**态没有独立场景：它由 `context-meter-hover.png` 这张 e2e 证据图覆盖
（真实 `hover()` 触发 CSS 弹出，断言弹层完整进入视口后截图）。

页面不再显示「原型」标识条；正式视觉基线也不带它。

审批接管**只在 `pending` 时替换输入区**。一旦审批失效或已作出决定，输入框立即恢复，审批结果以卡片留在输入区上方——
一张让用户"重新发起请求"的卡片，不能同时成为他无法输入的原因（见 `src/components/Composer.test.tsx`）。

侧栏里**选中行的运行状态取自当前场景**，未选中行才用侧栏 fixture 自己的状态；每个会话行的状态同时有可见文本
与隐藏状态词（空闲 / 运行中 / 等待审批 / 已完成），选中行带 `aria-current`（见 `src/app/App.test.tsx`）。

fixture 与视图模型在 `src/fixtures/`。它们是**展示用视图模型**，不是传输 DTO，也不镜像 Graph State
（V0_4_DEVELOPMENT_PLAN §6.1）。

## 视觉基线

- 截图由 `e2e/prototype.spec.ts` 生成，落在 `e2e/__screenshots__/{1440x900,1280x800}/`，
  固定视口、`deviceScaleFactor=1`、浅色主题、`zh-CN`、`Asia/Shanghai`、`reduced-motion`。
- 每次截图前都必须经过 `settle()`：等 `document.fonts.ready` 并等两帧 `requestAnimationFrame`。少了它，
  并行 worker 下截图可能落在字体就绪之前，同一个页面会产出两张不同图片（假回归）。
- 每个视口 27 张：19 个场景（`shell` / `diff` / `errors` / `tools` / `approval` / `approval-expired` /
  `settings-models` / `settings-empty` / `model-menu` / `memory-hit` / `memory-miss` / `memory-failed` /
  `empty-no-workspace` / `empty-session` / `running-empty` / `loading-history` / `meter-open` /
  `sidebar-collapsed` / `error-codes`）各一张，外加 8 张**证据图**：7 张滚动证据（`diff-bottom` /
  `errors-bottom` / `error-codes-bottom` / `tools-read` / `tools-bottom` / `settings-models-mid` /
  `settings-models-confirm`）+ 1 张悬停证据 `context-meter-hover`，共 54 张。
- 滚动证据用 `expect(...).toBeInViewport({ ratio: 1 })` 断言被折叠在首屏之下的内容在滚动后**完整进入视口**：
  第二个 hunk 末行、第三张错误卡的完整提示与"重新发送"按钮、读取卡的窗口与行号、尾部的待办/通用回退/成功终端卡、
  连接列表的中段（"检测中"+ 明文传输标记 / "检测失败" / "能力不支持"，把第三张卡对齐到滚动容器顶部后三张同时完整可见）、
  末张连接卡的删除确认按钮。因此"需要滚动"不等于"不可读/不可操作"，而"元素存在"也不等于"可以验收"。
- 截图比较是**严格**的（`maxDiffPixelRatio: 0`）。之前允许 0.002 的比例，在 1280×800 下放过约 2000 个像素的差异，
  于是输入区 chip 还写着旧模型的过期基线也能"通过"——`52/52 passed` 单独并不证明基线是最新的。
- 有意改动视觉后重采：`npx playwright test -g "<测试名>" --update-snapshots=all`。`=all` 会把**当前仍能通过**的
  基线也一并重写（默认的 `--update-snapshots` 只在差异超出容差时才写），因此要用 `-g` / `--project` 限定到确实改过的
  那几条测试，不要整目录刷。重采后**必须人工看新图**，不允许用批量更新掩盖回归（`V0_4_DEVELOPMENT_PLAN.md` §4.2）。
- 截图依赖本机字体（`Microsoft YaHei` 等），因此基线是**本机环境产物**；换机器需要重新确认。
- 参考资产的对照方法见 [docs/ui-reference/README.md](../docs/ui-reference/README.md) 与
  [state-matrix.md](../docs/ui-reference/state-matrix.md)。

## 后续任务与静态原型边界

T26 范围内的状态已全部出原型（进度账见
[docs/ui-reference/state-matrix.md](../docs/ui-reference/state-matrix.md) §6）。真实入口已经消费会话 API 与 SSE；仍待后续任务接线的有：

- 计划、工具、记忆/RAG 与轨迹详情（T31）、真实 diff 与审批接管（T32）、Streamlit 迁移（T33）、
  用量与缓存真实数据（T34）。

已知限制：

- 终端输出暂按纯文本渲染（ANSI 配色与 stdout/stderr 分流属 T31）。
- 设置页只有模型连接一节：v0.4 没有其他可设置的项，因此**不放惰性的分区导航**。
- `?state=` 的静态原型按钮仍可能没有行为；真实入口的设置页与模型菜单已接后端。
  错误恢复动作和计量器展开的真实交互由各自后续任务验收，不以 fixture 按钮冒充。
- **不渲染停止/取消控件**：v0.4 不公开 `cancel_task()`，架构禁止渲染一个没有后端语义的停止按钮。
  运行中的表达是**阶段态 + 可继续写草稿 + 暂时禁用发送并说明"任务进行中"**（`ARCHITECTURE.md` §15.2）。
- 检索/记忆行只显示来源定位与用途：正文、向量与内部评分不进入界面（`MemoryRow` 单测守住）。
- 空态与加载态是**替换**消息流，不与真实消息并列：骨架只有形状、不含任何占位内容。
- 侧栏收起态按本项目自定窄轨（56px，取自 T25 实测的 `--law-sidebar-width-collapsed`）实现：轨道里只有
  品牌标记、"新建会话"、"展开侧边栏"与"设置"，没有会话树的替代物。
- 样式只实现浅色主题。

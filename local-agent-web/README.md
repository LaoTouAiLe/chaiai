# chaiai 使用与开发指南

当前功能、状态和下一步统一见 [项目总览](../README.md)。本文件只说明如何启动、配置、使用及开发，不维护一份重复的任务表。

界面是 FastAPI + React；Python + LangGraph 执行业务。默认本机、单用户、单进程、单 Uvicorn worker；Streamlit 保留为调试入口。内部包名 `local-agent-web` / `local_agent` 未改名。

## 快速开始

所有 Python 命令在本 README 所在的 `local-agent-web/` 执行。

```powershell
uv sync
# 首次需要中期向量记忆或本地 ONNX：uv sync --extra memory
# 仅首次创建配置；已有 config.yaml 时不要覆盖。
Copy-Item config.yaml.example config.yaml
# 编辑 config.yaml，并在 .env 或指定环境变量中配置模型密钥。
cd frontend
npm ci
npm run build
cd ..
uv run python -m local_agent.api
```

浏览器打开 http://127.0.0.1:8000。前端构建后刷新；后端更新后重启服务。没有产物时根路径提供构建提示，API 仍可用。Python/Node/npm 版本要求以 `pyproject.toml` 与 `frontend/package.json` 为准，精确依赖由锁文件确定。

开发时另开终端，在 `frontend/` 执行 `npm run dev`，访问 http://127.0.0.1:5173；Vite 将 `/api` 代理到后端 :8000。生产运行只需要 Python 服务和前端产物，不启动 Node 服务。T40 EXE 试用包尚未交付。

## 配置与数据

- `config.yaml.example` 是完整字段参考；复制后的 `config.yaml` 与 `.env` 已被 Git 排除。
- `llm.api_key_env` 只填写变量名；密钥值放环境变量或 `.env`。`llm.context_window_tokens` 按实际连接填写，必须大于 `max_tokens`。
- 工作区是默认上下文、相对路径与检索根。会话绑定登记工作区；外部访问由“设置 → 权限”控制。
- 聊天/Checkpoint、设置、长期 SQLite 和中期 Chroma 的实际路径按配置解析；不要删除正式库来复现测试。
- 打开设置、浏览地图和数据中台不会因为浏览操作新增模型调用；提交追问和显式 Embedding 测试会实际执行并可能产生费用。
- AGENTS 规则从工作区加载；本仓库当前没有项目级 `AGENTS.md`，协作约定仍以用户提供的协议为准。新增关键约定应补充独立项目规则，不能把历史开发计划当作模型指令。

## 页面操作

侧栏提供新建/选择会话、搜索、会话改名/归档/恢复与工作区改名/移除。会话归档保留历史数据；移除工作区登记不删除磁盘目录。运行或待审批时遵守原切换限制。

输入区的 Agent／闲聊切换直接作用于当前会话，从下一轮生效，保留草稿、历史与地图分支，不新建会话。运行、审批或结果待核对时不可切换。模型调用失败明确结束本轮，可检查连接后继续提问；若工具或持久化异常导致执行结果不确定，刷新仍保留核对入口，先查看本轮记录与实际文件/命令结果，勾选确认后点击“结束中断任务”。这个操作不会重跑工具，也不会撤销已执行的操作。

对话与轨迹按轮展示模型实际返回的 Think、工具、计划、检索/记忆、审批和用量。隐藏或未返回的推理不补造。审批的批准、拒绝、修改参数及重启恢复由服务端契约决定，刷新不重新执行副作用。

### 会话地图

会话顶部打开“会话地图”：父节点在左，卡片向右展开；点击独立详情，搜索问答，拖空白平移、滚轮缩放，也可适应全图和定位当前分支。

“基于此轮继续提问”返回原输入框。末端追问延续分支，中间追问新增子分支；上下文继承根到所选轮次，原后续路径保留。分支可以改名和调整显示顺序。文件与共享记忆不随路径回滚；缺历史检查点、运行/待审批时相应入口禁用。

当前不支持自由布局、改父关系、智能分类、历史重生成和分支删除。[功能与证据](docs/ui-reference/CONVERSATION_MAP.md)。

### 数据中台

侧栏“数据中台”提供概览、中期记忆、长期事实、任务结论和 Token 用量。记忆可按工作区、状态、关键词筛选并分页查看；点击详情，关闭按钮或 Escape 返回。

用量可按工作区、模型、UTC 日期筛选；包括归档会话与各分支，共享调用去重。图表可点击进入对应日期/模型明细。未知不补零；上限或读取失败明确提示。返回保留会话、分支与未发送草稿，刷新页面不承诺保存草稿。

第一版只读，不含记忆写删、向量库重建和费用换算。真实存储/浏览器已验证；大规模性能、实体触屏和本轮真实缓存 A/B 未验。[统计口径与证据](docs/ui-reference/data-center-20261007.md)。

## 网页设置

侧栏“设置”包含模型连接、权限、通用与外观、任务执行、自动代码检索、长期记忆、中期记忆、向量库与 Embedding 和高级配置。
通用与外观支持深浅/系统主题、文字大小、减少动画、发送快捷键和 Think 默认展开，保存后立即生效。
执行、检索和记忆参数初值来自实际加载的 `config.yaml`；保存写入本地设置库，不修改 YAML，
从下一次新任务生效，运行中或等待审批的任务继续使用提交时的配置，重启后恢复也一样。
表单支持取消修改、恢复初始值（需再保存）；其他页面已保存新版本时会拒绝覆盖并保留草稿。
每次打开设置都会读取当前保存值；刷新共享缓存不会替换正在编辑的草稿及其基准版本。
自动代码检索参数只控制工具任务开始前的检索，不控制模型主动调用的 `grep` / `glob`。
记忆阈值接受 0–1 范围内的小数，不限于两位小数。
中期记忆可修改开关、召回条数、相似度阈值与时间衰减半衰期；启用前需配置 Embedding 并具备可用依赖。
“向量库与 Embedding”可配置本地 ONNX 或兼容 API 的模型、缓存目录、离线模式、端点与密钥环境变量名。
本地模型已导入 HuggingFace 缓存路径后，更换缓存目录需保存并重启后端；页面显示“需重启后端”，重启前测试不加载新目录。重启后请再次测试，静态准备状态不证明权重已缓存。
兼容 API 发送原文字符串数组并请求 float 向量，不按 OpenAI 模型窗口隐式切分；超长输入由供应商明确拒绝，不静默截断。测试失败显示安全分类及 HTTP 状态，区分请求格式/长度、鉴权、模型/端点、网络、超时与限流，不返回供应商原始报文。
向量库目录和集合名称只读展示，不从页面迁移、重建或清空。已有集合会校验模型/端点身份，实际运行和显式测试还校验向量维度及距离度量。
打开设置与保存不会加载模型或调用 Embedding 接口；“测试当前配置”会实际生成一条固定文本的向量，可能加载/下载本地模型或产生少量 API 费用，不保存草稿、不写入记忆。
远端密钥沿用本机 `.env` 或环境变量方式，页面只填写变量名、显示是否可用；修改 `.env` 后需重启后端。准备状态只验证静态条件，实际缓存及接口需通过测试确认。
高级配置只读展示压缩、历史保留与 AGENTS 规则，不开放修改。检索/记忆参数改变模型输入，缓存率需单独实测。

[设置技术与质量验收](docs/ui-reference/MEMORY_SETTINGS.md)。页面交付不等于召回质量遗留已解决。

## 安全与运行边界

- 默认只监听回环，单进程/单 worker；不提供公网、多用户、后台任务队列或真实取消能力。
- 外部路径采用 deny/whitelist/allow 与逐目录 read/read_write 授权；默认 whitelist 加空授权拒绝外部访问。
- 删除、覆盖已有文件、破坏性 Shell、大范围编辑仍需批准；受保护目标和敏感文件不因审批放行。
- 工具执行、作用域、安全判据和恢复真源在服务端；浏览器不直接操作文件或解释 Graph State。
- 记忆写入经过候选/作用域审核，普通聊天不自动写入。具体规则、配置和 API 见 [架构](../ARCHITECTURE.md) 与生成 `docs/api/openapi.json`；不能从历史示例反推当前默认值。

## 质量命令与调试

```powershell
uv lock --check
uv run pytest -q -p no:cacheprovider
uv run ruff check src tests
uv run mypy src
uv run python scripts/export_openapi.py --check
```

受限环境的 pytest `--basetemp` 使用本次可写、仓库外的新目录；不要复用真实数据。默认自动测试不依赖密钥或网络，真实供应商案例必须另用隔离库并标记费用及测量边界。

可选 Streamlit：`uv run streamlit run src/local_agent/web.py`。用于调试/对照，朋友试用不需要。提交规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

<a id="frontend"></a>

## 前端开发、契约与视觉参考

本节 npm 命令和前端源码路径以 `frontend/` 为根；Python 导出 OpenAPI 的命令从 `local-agent-web/` 执行。静态原型只用于参考，不能当真实操作验收。当前界面功能见本文件前文。

### 前端命令

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

### 传输类型与受控 OpenAPI（T29）

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

### 本机环境约定

- **Node / npm**：本机验证过 `node v22.20.0` + `npm 10.9.3`，`package.json` 的 `engines` 与之对齐；
  依赖的精确版本由 `package-lock.json` 锁定。
- **npm 缓存**：若遇到本机磁盘空间限制，安装时必须把缓存指到数据盘，否则会写 C 盘：
  `$env:npm_config_cache='E:\npm-cache'`（不写 `~/.npmrc`）。
- **本机的批量删除拦截**（环境问题，不是项目问题）：`npm ci` 会先删 `node_modules`，本机拦截一次超过 500 个
  文件的删除 → `npm ci` 中途失败并留下残缺依赖树。这是既有本机例外记录：曾使用 `npm install` 恢复残缺依赖。正常安装仍使用 `npm ci`，不能将该故障写成所有环境的要求。
- **Playwright 浏览器**：1.63 需要 Chromium **1243**，本机原有的 1217 版本不匹配，已安装到
  `E:\pw-browsers`。该路径放在**被 gitignore 的本机文件** `playwright.local.json` 里由
  `playwright.config.ts` 读取，因此配置本身保持可移植：

  ```json
  { "browsersPath": "E:\\pw-browsers" }
  ```

  环境变量 `PLAYWRIGHT_BROWSERS_PATH` 优先级最高，CI 可用它覆盖。首次准备：把同一变量指向目标目录后执行
  `npx playwright install chromium`。

### 原型宿主与场景

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

### 视觉基线（冻结参考）

- 截图由 `e2e/prototype.spec.ts` 生成，落在 `e2e/__screenshots__/{1440x900,1280x800}/`，
  固定视口、`deviceScaleFactor=1`、浅色主题、`zh-CN`、`Asia/Shanghai`、`reduced-motion`。
- 每次截图前都必须经过 `settle()`：等 `document.fonts.ready` 并等两帧 `requestAnimationFrame`。少了它，
  并行 worker 下截图可能落在字体就绪之前，同一个页面会产出两张不同图片（假回归）。
- 每个视口 33 张：22 个场景（`shell` / `diff` / `errors` / `tools` / `trace` / `long-text` / `approval` /
  `approval-expired` / `settings-models` / `settings-empty` / `model-menu` / `memory-hit` / `memory-miss` /
  `memory-failed` / `empty-no-workspace` / `empty-session` / `running-empty` / `loading-history` /
  `meter-open` / `sidebar-collapsed` / `error-codes` / `capabilities`）各一张，外加 11 张**证据图**：
  滚动证据（`diff-bottom` / `errors-bottom` / `error-codes-bottom` / `tools-read` / `tools-bottom` /
  `settings-models-mid` / `settings-models-confirm` / `capabilities-bottom`）、轨迹检查器
  （`trace-inspector` / `trace-inspector-bottom`）与悬停证据 `context-meter-hover`。三视口合计 **97** 张
  （`640x800` 少 2 张：`diff-bottom` / `tools-bottom` 在窄窗下元素本身就高于视口，按用例声明 skip）。
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
- 参考资产的对照方法见 [docs/ui-reference/README.md](docs/ui-reference/README.md) 与
  [state-matrix.md](docs/ui-reference/state-matrix.md)。



## 故障诊断

先看**服务端自己给的错误文本**：配置、端口、锁、静态产物这几类都是启动即失败并写明原因，不需要翻代码。

| 现象 | 常见原因 | 处理 |
|---|---|---|
| `uv run python -m local_agent.api` 直接拒绝启动 | `web.host` 被改成非回环地址，而 `web.allow_non_loopback` 仍为 `false` | 改回 `127.0.0.1`；确实要对外暴露时显式设 `web.allow_non_loopback: true`，并清楚 v0.4 **没有鉴权** |
| 启动报数据库锁被占 | 已经有同一个库的实例在跑（排他锁 `data/checkpoints.sqlite.api.lock`） | 用已在跑的那个进程，或先把它停掉——同一目录只能有一个 Python 服务（见「单进程、单 worker」） |
| 打开 `http://127.0.0.1:8000/` 只有一段提示 | 没有构建产物，服务只提供 API | `cd frontend; npm ci; npm run build`，然后刷新 |
| `npm run dev` 起不来（端口 5173） | Vite 配了 `strictPort` | 停掉占用 5173 的进程；不要靠改端口绕过，代理目标写死在 `vite.config.ts` |
| 页面出现「事件流提示：stream_idle_timeout / stream_overrun」 | 服务端主动结束了这条 SSE 连接（长时间无事件 / 客户端读得太慢） | 前端会自动重连并先取快照，**任务不会被取消**；偶发可忽略，持续出现看服务端日志 |
| 页面出现「事件流提示：snapshot_refresh_failed」 | 对账用的快照请求失败 | 切会话瞬间的取消是正常行为、不会再报；其他情况先确认后端进程还活着 |
| 启动即失败并点名某个配置键 | `config.yaml` 校验不通过 | 按提示补：`llm.context_window_tokens` **必填**；`security.require_approval` 四项不允许写 `false`；YAML 语法 / 字段拼错 / `api_key_env` 指的环境变量没设 |
| 中期记忆相关功能不可用 | 打开了 `memory.medium_term.enabled` 但没装可选项 | `uv sync --extra memory`；缺依赖给的是可读提示而不是 `ImportError`，embedding 缓存目录不可用时侧栏状态为 `not_ready` |
| `uv` / `npm` / `node` 提示"无法识别为 cmdlet" | 终端 PATH 没生效（shell 每次调用不共享环境） | 见上面的「本机环境备忘（Windows）」，或用全路径调用 |
| live 套件（`test:live*`）失败 | 缺 `uv`、并发太高、或本机没装 Chrome | 确认 `uv` 可用；`--workers=2`（8 会 OOM）；浏览器走本机 Chrome，不额外下载 headless shell |
| `npm run check:api` 失败 | `docs/api/openapi.json` 与后端代码漂移 | `uv run python scripts/export_openapi.py` 重新导出 → `npm run generate:api` → 再比对 |
| 报"数据库结构未就绪" | 库还没建或版本旧 | `uv run python scripts/init_db.py --check` 看原因，需要时跑一次 `scripts/init_db.py` |

旧 T01–T24 进度、底层实现展开及完整整理前 README 见 [阶段历史](../docs/archive/v0.1-v0.3-development.md#readme-snapshot)。

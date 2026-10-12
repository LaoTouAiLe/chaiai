# chaiai v0.4 当前实施与验收计划

> 更新：2026-10-07。当前总览见 [README.md](README.md)，架构真源见 [ARCHITECTURE.md](ARCHITECTURE.md)。阶段实施日志已移入 [历史](docs/archive/v0.4-implementation-history.md)，后续候选统一见 [后续规划](docs/planning/FUTURE_WORK.md)。本轮只整理文档，不代表新功能已实现或未验收门槛通过。

> **当前顺序：已有成果收口 → T38/T39 剩余交付门槛核对 → T40 Windows 包 → T37 朋友试用。** T44/T45 与品牌改名已完成；质量遗留单列暂缓，不重复开工。

## 1. 目标与成功口径

v0.4 同时解决以下影响真实使用的问题：

1. 交付一个朋友可以在本人机器上启动并正常使用的独立 Web 页面；视觉与交互以 DeepSeek Harness 当前 Web 页面为高保真基准。
2. 建立应用服务、结构化事件和 HTTP/SSE 边界，使 React 与保留的 Streamlit 都不再直接解释 LangGraph 内部 State、节点或 `Command`。
3. 记录供应商实际 Token 与缓存命中，随后把模型请求改造成稳定前缀、追加式动态上下文和可解释的缓存 epoch，降低未缓存输入成本。
4. 让朋友在 Web 设置页自行添加、测试和选择多个 OpenAI 兼容聊天模型连接；保存或切换后下一轮立即生效，无需重启服务。
5. 朋友试用前完成 T38 权限边界、T39 页面与浏览器数据收口、T40 Windows 可解压 EXE 试用包，再用实际分发包复验 T37。

v0.4 完成不等于后台任务平台完成。首版仍是本机、单用户、单进程，并且只支持单个 Uvicorn worker（`workers=1`）；任务在一次前端提交请求的生命周期内运行，Checkpoint 继续是执行恢复真源。多 worker、跨进程广播和分布式锁不属于本阶段。

v0.4 是增量演进，不是重写现有 Agent：Python + LangGraph 编排、SQLite Checkpoint、会话恢复、安全策略、人工审批、文件/Shell 工具、三层记忆、上下文压缩、反思、代码 RAG 和固定评测全部保留。允许替换 UI 到内核的直接调用路径，但必须先证明新旧路径业务等价；不得以“新前端暂时没展示”为理由删除或关闭底层能力。

## 2. 已冻结的技术决策

```text
Python 服务端     FastAPI + Uvicorn
前端              React + TypeScript + Vite
命令与查询        JSON HTTP API
流式文本与事件    SSE
服务端状态        TanStack Query
本地 UI 状态      Zustand
样式              CSS Modules + CSS Variables / Design Tokens
前端单元测试      Vitest + React Testing Library
端到端与视觉回归  Playwright
API 类型同步       FastAPI OpenAPI → openapi-typescript
生产发布          Vite 静态产物由 FastAPI 同源托管
```

补充决策：

- 前端采用单包目录，不建立 JavaScript Monorepo；默认使用随 Node 提供的 `npm` 和 `package-lock.json`，原因是当前只有一个前端包，不需要额外工作区工具。T26 开工时记录并锁定实际验证过的 Node/npm 版本。
- 开发时 Vite 代理 `/api`；生产构建由 FastAPI 同源托管，因此试用者不需要运行 Node 服务。
- 提交消息、审批和查询走普通 HTTP；会话级 SSE 连接推送增量事件。SSE 断开不等于取消任务。
- 官方开发和生产启动入口固定为单个 Uvicorn worker；进程内广播器、会话互斥和幂等记录都以此为支持边界。直接使用多 worker 启动属于不受支持配置，启动说明必须明确警告。
- 不采用 Next.js、Electron/Tauri、Redis、Celery、微服务、WebSocket、Cordis 或前端 Slot/插件运行时。
- 不使用带完整视觉语言的大型组件库作为页面基础；可以使用无样式基础组件和通用图标库，但默认外观不能覆盖设计 Token。

## 3. v0.4 明确不做

- 不实现公网访问、账号、多人协作、云端同步或远程工作区。
- 不实现后台持久任务队列、多任务并发调度、浏览器关闭后继续运行或协作式取消；这些属于 v0.6。
- 不实现持久终端和 WebSocket。
- 不增加 MCP、Agent Profile、Capability Registry、Subgraph 或子 Agent。
- 不删除 LangGraph、Checkpoint 或现有安全/审批真源，也不另建一套可写 UI 状态库。
- 不删减 v0.1–v0.3 已完成的聊天、工作区、会话、工具、安全审批、记忆、压缩、反思、RAG、恢复和评测能力；迁移入口必须先通过等价回归。
- 不为了缓存命中关闭记忆、RAG、摘要、错误承诺修正或安全上下文。
- 不复制 DeepSeek Harness 的源码、品牌、Logo、鱼形标记、专有图标、插件体系和包结构。
- 当前可编辑设置已扩展到聊天连接、权限、通用外观、任务、自动代码检索、长期/中期记忆与 Embedding；压缩等高级配置只读。设置入本地库，不支持任意 YAML 编辑；向量库迁移/重建、关闭强制审批和供应商私有协议仍不在交付范围。

## 4. 开工前基线与通用质量门槛

### 4.1 已知基线

v0.3 完成时的最近记录为：两种 Python 运行入口均 `608 passed, 5 skipped`，`ruff check .`、`mypy src`、`uv lock --check` 和 `git diff --check` 通过。这个数字只用于识别回归；新增测试后总数应增加，不能把精确用例数写成永久常量。

### 4.2 每个实现任务的共同门槛

Python 侧至少执行：

```text
python -m pytest <本任务相关测试>
python -m ruff check .
python -m mypy src
uv lock --check
git diff --check
```

触及 Graph、Checkpoint、应用服务、API 或跨层 DTO 的任务，在提交前还必须执行完整 `python -m pytest`。继续保留当前 venv 与 `uv run` 两种全量入口的阶段性对照；若两者结果不一致，停止提交并先定位环境差异。

前端从 T26 起至少执行：

```text
npm ci
npm run typecheck
npm run lint
npm run test
npm run build
```

触及关键用户路径、设计 Token、布局、审批或刷新恢复的任务还必须运行 Playwright。视觉基线只能在人工确认差异符合设计后更新，不能用批量更新快照掩盖回归。

### 4.3 测试边界

- 自动测试默认使用临时数据库、临时工作区、假模型和确定性事件，不依赖 API Key、外网或供应商缓存。
- 真实 DeepSeek 缓存验收是单独的带密钥手动门槛；未运行时必须标为未验证，不能用模拟字段替代。
- 前端自动测试使用稳定 fixture 状态覆盖全部视觉状态；真实后端端到端测试只覆盖关键纵向路径，不让模型随机输出决定截图结果。
- 日志、事件 fixture、截图和测试报告不得包含 API Key、隐藏思维链、私人路径或未脱敏的记忆正文。

## 5. 目标目录与职责

T25 必须先把最终目录和依赖方向同步到 `ARCHITECTURE.md`。默认目标如下；若审查时改变，先改架构与本计划再编码：

以下代码与测试路径均相对于 `local-agent-web/` 项目根目录；仓库文档路径从仓库根目录解析，具体入口见根 README。

```text
local-agent-web/
├── frontend/
│   ├── package.json
│   ├── package-lock.json
│   ├── vite.config.ts
│   ├── playwright.config.ts
│   └── src/
│       ├── api/             # HTTP/SSE 客户端与传输 DTO
│       ├── app/             # 页面壳、路由和全局 providers
│       ├── components/      # 通用无业务组件
│       ├── features/        # conversation/tool/plan/approval/diff/usage/model-settings
│       ├── stores/          # Zustand 本地 UI 状态
│       ├── styles/          # tokens、全局基础样式、字体与主题
│       └── test/            # fixtures、渲染工具与视觉状态
├── src/local_agent/
│   ├── application/
│   │   ├── models.py        # UI/API DTO；不得暴露 AgentState
│   │   ├── events.py        # 事件模型、稳定 id 与实时序列
│   │   ├── projection.py    # Checkpoint/State → DTO/Event 单向投影
│   │   ├── errors.py        # 稳定错误分类与安全消息
│   │   └── service.py       # 会话、提交、恢复、审批、读取入口
│   ├── model_settings/      # 聊天模型连接、密钥引用、配置版本与能力检测
│   ├── api/
│   │   ├── app.py           # FastAPI 构造、生命周期与静态托管
│   │   ├── routes.py        # JSON HTTP API
│   │   └── sse.py           # 会话级事件流与进程内广播
│   └── ...                  # 既有 graph/memory/tools/security/persistence
└── tests/
    ├── test_application.py
    ├── test_api.py
    ├── test_events.py
    ├── test_model_usage.py
    └── ...                  # 既有测试继续保留
```

依赖方向固定为：

```text
React → HTTP/SSE → application → LangGraph
                              → Checkpoint/Session Store

LangGraph 不导入 api、frontend 或 React DTO
application 不导入 Streamlit
frontend 不知道 AgentState、节点名、LangChain 消息类或 LangGraph Command
```

## 6. v0.4 原始边界与契约索引

本节保留初始应用边界，具体当前字段与路由以 `ARCHITECTURE.md` §15 及生成 OpenAPI 为准；T38–T45 的扩展不应由本节早期最小清单反推为未实现。

### 6.1 应用服务

应用服务至少提供以下用例，不以 Graph 节点名作为公开接口：

```text
list_workspaces()
select_workspace(path)
select_workspace_by_id(workspace_id)  # 已列出的工作区；新路径仍用 select_workspace(path)
list_sessions(workspace_id)
create_session(workspace_id)
get_session_snapshot(session_id)
list_model_connections()
save_model_connection(connection, optional_api_key)
test_model_connection(connection_id)
select_default_model(connection_id)
select_session_model(session_id, connection_id)
submit_message(session_id, text, client_request_id)
resolve_approval(session_id, approval_id, decision, client_request_id)
subscribe_events(session_id)
get_permissions()
update_permission_mode(mode, expected_version)
grant_external_path(path, access)
revoke_external_path(path)
```

v0.4 不公开真正的 `cancel_task()`。DTO 可以返回 `can_cancel=false`，页面不得渲染一个无后端语义的停止按钮。流式展示失败只能降级为最终完整结果，不能通过重调 Graph 制造第二次副作用。

所有会话**执行状态修改**采用**每会话单飞**语义：`submit_message` 与 `resolve_approval` 共用同一互斥边界。已有修改正在执行时，不同 `client_request_id` 的第二个执行请求返回稳定错误 `SESSION_BUSY`，不进入队列；相同 id、相同操作与相同规范化参数的重试返回原请求关联结果，相同 id 配不同参数返回 `IDEMPOTENCY_CONFLICT`。模型选择只写下一轮的设置，不修改当前运行的 Graph State；它也必须按版本校验避免两个页面互相覆盖。幂等请求台账必须跨服务重启保留到会话删除，至少记录请求 id、操作、参数摘要、`run_id`、状态和结果引用；它只保护命令执行，不复制 Graph 业务状态，也不成为第二套任务真源。

服务重启后若台账仍为“执行中”，应用服务先只读核对 Checkpoint：能够证明终态时补齐结果引用；无法证明工具副作用与终态时返回 `RECOVERY_REQUIRED` 并要求人工核对，绝不能自动重提同一 Graph 输入或再次执行工具。

### 6.2 HTTP 与 SSE

T25 冻结具体 OpenAPI 后，默认资源形态为：

```text
GET  /api/health
GET  /api/workspaces
POST /api/workspaces/select
GET  /api/sessions
POST /api/sessions
GET  /api/sessions/{session_id}
GET  /api/sessions/{session_id}/events
GET  /api/model-connections
POST /api/model-connections
PATCH /api/model-connections/{connection_id}
DELETE /api/model-connections/{connection_id}
POST /api/model-connections/{connection_id}/test
PUT  /api/model-connections/default
GET    /api/permissions
PATCH  /api/permissions/mode
POST   /api/permissions/external-paths
DELETE /api/permissions/external-paths
PUT  /api/sessions/{session_id}/model
POST /api/sessions/{session_id}/messages
POST /api/sessions/{session_id}/approvals/{approval_id}
```

- 浏览器先保持会话级 SSE，再提交消息；POST 请求本身可在服务端同步运行 Graph，SSE 只投影进度，不成为任务真源。POST 完成时还要返回稳定的命令结果引用，不能要求 SSE 才能确认命令结局。
- `client_request_id` 按 §6.1 的请求台账拒绝浏览器双击、重试或网络重发造成的重复提交；它不能绕过 Graph、Checkpoint 与工具副作用保护。
- v0.4 不提供持久 `after_cursor`。首次连接和重连时，SSE 先发送由当前 Checkpoint 投影的 `session_snapshot`，再发送连接建立后的实时增量；临时 `message_delta` 不永久保存，最终消息、审批、工具结果和任务状态必须能从后续快照恢复。持久游标、长时间断线补发和分页留到 v0.8。
- 服务默认只绑定 `127.0.0.1`。改变监听地址必须显式配置并被启动检查拒绝或警告；状态修改接口校验 `Host`/`Origin`，开发代理的跨源许可只在开发配置启用。

### 6.3 事件与实时序列

最小事件集合：

```text
session_snapshot
message_started / message_delta / message_completed
plan_updated / step_started / step_completed
tool_started / tool_output / tool_completed
approval_required / approval_resolved
memory_recalled / memory_written
model_call_started / model_usage_reported / model_call_completed
task_completed / task_failed
```

每个事件至少包含：

```text
schema_version
event_id
event_type
session_id
task_id                  # session_snapshot 无当前任务时可为 null
run_id                   # session_snapshot 无当前运行时可为 null
occurred_at
stream_sequence
payload
```

- v0.4 中一次用户提交就是一个任务；允许 `task_id` 与现有 `run_id` 使用同一底层标识，但两个字段都必须出现在 DTO 中，并在 v0.6 分离前保持这一映射有文档和测试。
- 除空会话或没有当前任务的 `session_snapshot` 外，所有任务级事件的 `task_id`、`run_id` 都必须非空；禁止用随机占位值填充会话级快照。
- Checkpoint/State 是真源，事件是单向投影。生成快照和事件不得写回 State、调用模型、恢复 Graph 或执行工具。
- `event_id` 必须由稳定来源标识和事件类型确定，不能使用每次渲染随机 UUID；同一 Checkpoint 重放得到相同事件 id。
- `stream_sequence` 只保证当前进程、当前会话实时流内的顺序，不是持久游标，服务重启后允许重新计数。进程内流式 delta 使用 `model_call_id + chunk_sequence` 形成稳定 `event_id` 并去重；最终消息由 Checkpoint 中稳定消息 id 接管。刷新后不重放每个历史 delta，但快照中的最终文本必须完全一致。
- `session_snapshot` 包含快照版本或 Checkpoint 标识。连接建立时先登记订阅者，再取得边界快照并发送该事件；边界之后产生的实时事件按 `stream_sequence` 投递，避免“先读快照、后建连接”之间丢失最终状态。

### 6.4 错误模型

公开错误分类固定为：

```text
configuration / model / tool / security / approval / recovery / system
```

错误 DTO 必须区分用户可执行提示、稳定错误码、是否可重试和安全的诊断关联 id。会话互斥与幂等至少固定 `SESSION_BUSY`、`IDEMPOTENCY_CONFLICT`、`RECOVERY_REQUIRED` 三个错误码；前者可在当前请求结束后重试，第二个要求客户端生成新 id 或修正请求，最后一个禁止自动重试副作用并给出人工核对入口。异常原文默认只进本地受控日志；API 不返回密钥、绝对私人路径、隐藏推理或未经脱敏的供应商响应。

### 6.5 模型用量与缓存

估算量和供应商实际量是两个不同对象，不得共用一个含糊的 `token_usage` 名称：

```text
estimated_prompt_tokens
actual_uncached_input_tokens
actual_cache_read_tokens
actual_cache_write_tokens
actual_output_tokens
actual_reasoning_tokens
cache_metrics_reported
cache_hit_ratio
turn_elapsed_ms
model_elapsed_ms
tool_elapsed_ms
time_to_first_token_ms
approval_wait_ms
context_used_tokens
context_window_tokens
context_usage_ratio
```

- DeepSeek 的 `prompt_tokens` 包含缓存命中，归一化时使用 `uncached = prompt_tokens - cache_read`；优先识别 `prompt_tokens_details.cached_tokens`，兼容 `prompt_cache_hit_tokens`。
- 供应商没返回某缓存字段时写 `null`/未知，不得写 0。只有明确返回零时才展示 0%。
- 命中率为 `cache_read / (uncached_input + cache_read + cache_write)`；分母为零或字段未知时不显示百分比。
- 事件只记录安全指标与不可逆指纹，不记录完整 Prompt 或隐藏思维链。
- Checkpoint 持久状态增加规范化的 `model_call_records` 与 `turn_records`，它们是刷新恢复和事件投影的唯一用量真源。`ModelCallRecord` 至少包含稳定 `model_call_id`、`run_id`、调用用途、连接 ID/版本、供应商/模型、端点安全指纹、重试关联、开始/首 Token/结束时间、状态、估算与实际 Token、缓存字段、上下文窗口和安全指纹；每次重试使用新的调用 id，不能覆盖失败尝试。
- `TurnRecord` 以 `run_id` 为稳定标识，至少包含提交接受时间、最终状态时间、审批等待区间、关联模型调用 id 和工具事件 id。运行中允许按同一 id 更新，进入完成/失败终态后不可改写；页面汇总必须由这些规范化记录确定性计算，不能把事件流或前端状态当成第二真源。
- 聊天、规划、工具决策、最终回答、反思、摘要和其他实际调用模型的路径都必须经过同一记录入口；不能只统计主回答模型。旧 Checkpoint 没有这些记录时显示“历史版本未记录”，不得伪造估算或零值。
- “本轮”指一次用户提交直到最终完成或失败的任务边界；等待审批只是同一轮的中间状态，统计可以暂时显示“进行中”，审批恢复后继续累计并更新同一轮，不能另建一条统计。轮次底部默认展示所有已结算模型调用的实际 Token 汇总、缓存命中率和端到端耗时，展开后按 `model_call_id` 展示明细。汇总值必须由明细确定性相加，不能从页面文本反推。
- 端到端耗时从应用服务接受提交开始，到本轮最终状态确定为止；等待人工审批的时间记录为 `approval_wait_ms` 并在明细中明确标记，不能伪装为纯模型耗时。`model_elapsed_ms`、`tool_elapsed_ms` 和 `time_to_first_token_ms` 只有在能够可靠测量时显示，未知保持空值。
- 圆形上下文计量器使用已经完成组装的模型请求之**估算输入占用**作为 `context_used_tokens`，使用配置/能力探测确认的模型上下文窗口作为 `context_window_tokens`；圆环比例为二者之比。执行中显示最新一次即将发送或已经发送的调用并标记“当前请求”，空闲时显示最后一次记录并标记“上一请求”，从未组装或窗口未知时显示未知。v0.4 不声称它随未提交草稿实时精确变化；悬停明细区分系统提示、工具 Schema、消息和输出预留，输出预留单列且不能重复计入已使用输入。

### 6.6 运行上下文快照

动态运行信息不再伪装成聊天消息。Checkpoint 持久状态增加独立的 `RuntimeContextSnapshot` 记录，至少包含：

```text
schema_version
snapshot_id
run_id
context_version
created_at
content_hash
canonical_text
source_versions
```

- `canonical_text` 以固定段落顺序和确定性序列化生成；`source_versions` 只记录记忆、RAG、摘要、任务、计划、模式理由、当前目录等来源的版本/安全引用，不复制密钥或未经脱敏的外部正文。
- 快照与 `messages` 分开持久化。请求组装器只把最新有效快照作为带明确来源标记的上下文块放在当前真实用户输入之前；具体供应商消息角色由兼容层统一映射，UI 和 Graph 节点不能各自选择。
- 快照不显示为用户气泡，不建立新轮次，不参与用户消息计数，不触发记忆写入，也不被摘要器或 RAG 索引当作用户陈述。内容未变化时不得追加新记录；新快照通过版本明确覆盖旧快照，但旧记录保留审计与重建能力。

### 6.7 页面可见执行过程

页面展示安全、可验证的执行状态，不展示模型隐藏思维链：

| 类别 | v0.4 展示 | 明确不展示 |
|---|---|---|
| 模型 | 回答增量、当前阶段、安全决策摘要、模型/用量/推理 Token 数值 | `reasoning_content`、原始 Chain-of-Thought、完整 Prompt、系统/开发提示 |
| 计划 | 目标、步骤、当前状态、完成/失败依据 | 节点私有临时变量、未脱敏内部异常 |
| 工具 | 工具名、等待/执行/成功/失败、脱敏参数、耗时、输出摘要与证据 | 密钥、私人绝对路径、无界 stdout/stderr、未经审批的危险操作入口 |
| 记忆/RAG | 是否命中、来源类型、安全引用、写入成功/失败 | 未经脱敏的长期记忆正文、向量或内部评分细节 |
| 审批 | 动作、参数、影响、风险、批准/拒绝结果 | 把前端按钮当成授权真源 |
| Web 查询 | v0.5 实现后复用通用工具时间线 | v0.4 伪造联网、搜索结果或进度 |

事件轨迹、日志和导出使用同一脱敏规则。前端可以用“正在分析”“正在调用工具”等产品状态表达当前阶段，但不能把生成的解释文本标成模型逐字思考过程。

### 6.8 可视化模型连接与免重启切换

v0.4 管理的是**聊天模型连接**。连接记录至少包含稳定 `connection_id`、显示名称、`provider=openai_compatible`、`base_url`、模型 ID、上下文窗口、输出上限、temperature、超时、重试、流式选项、鉴权模式、能力检测状态、配置版本和可空的密钥引用。无鉴权的本地兼容端点可明确选择“不需要 Key”；客户端适配层可以在内存中满足协议库所需的占位参数，但不能将占位值保存为用户密钥。上下文窗口必须由用户填写或由可信能力检测确认，不能猜测；`context_window_tokens > max_tokens` 沿用现有校验。页面支持新增、编辑、删除、主动测试连接、设置新会话默认模型，以及为当前会话选择下一轮模型；输入区始终显示选中模型与可用状态。

- 现有 `config.yaml` 存在时，首次启动从其 `llm` 和 `api_key_env` 幂等导入一个默认连接，保留旧环境变量取密钥路径。后续非秘密连接设置保存在应用自己的本地设置库，作为 Web/Streamlit 共同读取的聊天模型选择来源；不直接重写 `config.yaml` 或进程环境变量。没有可用设置库时继续使用原配置并明确告知页面降级原因；旧会话不因迁移失去模型或历史。
- 全新安装缺少 `config.yaml` 或缺少模型 Key 时，Web/API 必须先以安全的非模型默认配置进入初始设置模式；健康检查、设置页和模型连接 API 可用，Graph/模型调用在连接与必要工作区配置就绪前拒绝执行并给出明确提示。完成首个有效连接后立即建立客户端，不要求生成伪造的 YAML 模型段或重启服务；CLI 仍沿用原有显式配置要求。
- Windows 朋友试用环境的持久 API Key 存入操作系统凭据存储，通过不含明文的 `credential_ref` 引用；设置库、Checkpoint、请求台账、事件、日志、浏览器本地存储、导出和截图都不得保存明文 Key。读取设置仅返回“已配置/未配置”，不返回完整或部分密钥；更新 Key 只能覆盖写入。操作系统凭据存储不可用时允许本次进程内临时使用录入的 Key，并明确显示“重启后需重新录入”，不得悄悄写入明文文件。POSIX 持久凭据方案须先经过真机验证，否则也只提供临时模式。
- 新增连接可以保存但未通过检测时标记为“未验证”；用户点击“测试连接”前说明可能产生一次少量付费模型调用，测试只验证该连接的鉴权、聊天、工具调用和流式能力，并将各项显示为通过/失败/未知。测试失败保留输入及原选中模型，错误要可执行且脱敏；缺失工具能力时允许普通聊天，但进入需要工具的任务前明确拒绝，不能静默切回其他模型或降低安全策略。
- 对非回环端点默认要求 HTTPS；用户明确确认的局域网 HTTP 端点可以保存并标出明文传输风险。拒绝 URL 内嵌凭据、非 HTTP(S) scheme 和跨主机重定向；连接测试只对用户明确保存的端点发起，不在页面加载时自动探测。
- 新会话继承当前默认连接；旧会话保留自己的选择。会话切换或默认变更只影响**下一次新用户提交**，已开始、等待审批或恢复中的运行固定提交时的连接 ID、配置版本、模型参数和密钥引用，不能因设置变更而换模型。编辑产生新版本；旧版本及凭据引用保留到所有引用它的运行结束。删除是停用连接，历史模型标识与用量不删除；最后一个可用连接或正被运行引用的版本不能被物理清除。
- 应用服务在提交边界解析会话选择、构造对应模型客户端与能力快照；Graph 的安全策略、工具注册和 Checkpoint 不由前端直接修改。每次模型调用记录连接 ID/版本、模型 ID 与端点安全指纹，缓存基线只在相同连接版本和工具 Schema 下比较；模型切换后圆形上下文计量器按新模型窗口重新标记，旧轮次仍显示当时窗口。

## 7. 当前任务与交付状态

本节判断当前状态；原任务卡、逐轮执行、失败与回退见 [完整实施历史](docs/archive/v0.4-implementation-history.md)。新任务不能从历史“下一步”或备选条目直接推导授权。

| 项目 | 当前状态 | 下一动作 / 边界 |
| --- | --- | --- |
| T25–T36 | 主体已实现，历史证据已归档 | 不重新排期；测量只代表对应阶段 |
| T38 权限 | 实现及自动门槛通过；专门人工往返未在记录中关闭 | 试用前核对权限切档、外部读写与强制审批 |
| T39 页面/管理/设置 | 已实现并分阶段收口；整体人工矩阵仍待核对 | 审批恢复旧阻断已修复；不要重复作为待修缺陷 |
| T40 Windows 包 | 目录式构建脚本与实包已落地；图标、自动打开浏览器、数据目录口径已定；首次实包漏带 OpenSSL 已修（构建期自包含检查 + 启动兜底入口），新包在干净 PATH 下复验通过 | 用新包（`build/packaging/out-20261008b/chaiai`）重发朋友复验；按 §T40「尚未验收」清单补人工项；未提交改动仍待收口 |
| T37 朋友试用 | 朋友试用**已通过**（实际分发包 + 非开发者）；视觉基线已重拍；T38/T39 人工往返、真实缓存、轨迹导出经人工批准豁免 | 见 §T37 验收记录；补验条件已写明 |
| T41/T42 | 已有缓存/检索实现与阶段证据 | 不由旧测量承诺任意任务稳定 90% |
| T43 回答与召回质量 | 主循环保留；源码回答质量已重开并有样本改善，整体未通过 | 补独立样本、完整引用与调查成本；记忆质量另验 |
| T44 会话地图 | 横向卡片树及分支追问已交付 | 万级浏览器性能、实体触屏与自由布局未验 |
| T45 数据中台 | 第一版只读范围已交付，交互复验通过 | 不包含写删、库重建、费用换算；真实缓存 A/B 未做 |
| chaiai 品牌 | 名称、SVG、侧栏与 favicon 已完成 | 构建/类型/Lint及5项侧栏回归通过 |

### 执行顺序

1. 收口本次已有成果与文档，按用户授权另行提交；本轮没有提交 Git。
2. 核对 T38 人工权限往返与 T39 剩余矩阵，不重做已关闭的设置、地图、数据中台开发。未完成门槛列出结果。
3. 满足试用前提后做 T40 → T37。v0.5/MCP/多 Agent/后台任务暂不排到前面。

下列集中列出剩余交付门槛；T40/T37 保留任务边界，“目标”不代表已交付。

### T38｜权限模式与外部路径授权：剩余人工验收

- **当前状态**：实现和自动门槛已有通过记录；专门人工往返未在记录中关闭。原实现细节、测试矩阵和历史问题见 [历史归档](docs/archive/v0.4-implementation-history.md)。
- **人工步骤**：切到「放行」→ 读写工作区外目录并成功 → 请求删除其中的文件，必须出现审批卡；切回「手动审批」→ 同样的外部访问请求被拒，提示指向「设置 → 权限」中真实存在的入口。
- **核对边界**：默认空白名单拒绝外部访问；只读授权不放开写操作；读写授权不绕过删除、覆盖已有文件、破坏性 Shell 命令、大范围编辑的强制审批。路径规范化、模式切换和授权持久化保持原自动测试范围。
- **停止条件**：任何模式绕过四项强制审批；默认访问外部路径；配置与库形成授权的两处真源；旧键兼容没有冲突校验。

### T39｜页面、管理与设置：剩余整体交付验收

- **当前状态**：页面、管理、设置已实现并分阶段收口；完整人工矩阵仍待核对。早期审批恢复、工作区排序及惰性按钮问题的处理记录留在 [历史归档](docs/archive/v0.4-implementation-history.md)，不再把旧现象列为当前缺陷。
- **已确认边界**：会话创建时固定「闲聊」或「Agent」，刷新和切回保持模式；闲聊可只读文件，不获得写、删除或 Shell 工具。展示执行摘要及实际供应商思考的脱敏、有界副本，不能编造思考内容。
- **剩余矩阵**：在 1440×900 和 1280×800 核对空会话、普通问答、工具成功/失败、长输出、审批、diff、轨迹与刷新恢复；可见按钮可操作，刷新不重放工具；复核在途/等待审批时切换模型只作用于下一轮。
- **字段门槛**：按架构 §15.8 核对页面、展开详情、快照、SSE、导出和网络响应。页面不展示原始工具名、运行 ID、DTO 名或结果指纹；协议所需的不透明 ID 只服务请求和重连。密钥、完整 Prompt、私人绝对路径、未经脱敏记忆、原始供应商错误及无业务必要的扩展字段保持服务端白名单、裁剪与负例检查。
- **试用放行**：使用 T40 实际包由未参与开发者完成解释文件、定位问题和审批修改等任务，记录成功、卡点及口头解释需求；任务必须理解内部术语才能继续时，T37 不通过。若核对发现需要修复，改动后重跑相关回归。
- **本次不做**：Profile、Git、Web 搜索、Skill、后台任务、新插件体系；不重复开发已关闭的设置、地图和只读数据中台。

### T40｜Windows 可解压 EXE 试用包（T39 后执行）

- **目标**：交付一个可解压文件夹；朋友在目标 Windows 机器双击其中的 EXE 即能进入本机页面、完成初次设置和核心任务，不需要安装 Python、Node、uv 或运行命令。仅支持本机回环地址、单进程、单 worker；不把公网部署或安装器纳入本任务。
- **已定口径（2026-10-08，用户拍板）**：目录式（PyInstaller onedir），不做安装器与单文件 EXE；**可写数据目录 = `%LOCALAPPDATA%\chaiai`**（`config.yaml`、`.env`、SQLite、Chroma 都落这里），**只读程序资源 = 包内 `_internal`**（前端产物在 `_internal/frontend/dist`，按 `sys._MEIPASS` 解析）。理由：解压目录是会被整包替换的东西，数据放进去直接违背本任务"覆盖升级后保留设置、凭据引用、Checkpoint 和会话"的要求。实现落点：`src/local_agent/app_paths.py`（口径本身，含源码形态必须沉默的约束）、`api/__main__.py`（`--config`/`--env` 默认值与 `static_dir` 注入）。构建资产：`scripts/chaiai.spec`、`scripts/build_windows_package.py`、`scripts/requirements-build.txt`（钉 `pyinstaller==6.22.3` 与 `pyinstaller-hooks-contrib==2026.8`；只装进 venv，不写进 `uv.lock`）。可选记忆依赖（`chromadb`/`fastembed`/`onnxruntime`）**暂不随包**，缺依赖走既有降级提示；要用向量记忆时再评估体积与收集规则。
- **已确认入口与路径**：`src/local_agent/api/__main__.py` 已有服务入口和 `--pick-directory` 自启动分支，但目前只打印 URL；`api/container.py` 按配置文件所在目录解析数据库、设置和前端路径；`api/static.py` 从 `frontend/dist` 托管页面。打包时须明确区分只读程序/前端资源与可写配置、数据库、日志和用户工作区，不能让 `_internal` 或临时解压目录成为持久数据目录。
- **实现边界**：在 Windows 上用目录式打包方案做可复现构建，先以 PyInstaller onedir 作为验证候选，跑通后锁定工具版本与构建脚本；把前端生产构建和所需 Python 运行依赖随包交付，保留目录选择器的 `sys.executable --pick-directory` 分支。双击启动需给出可见的启动/失败提示并打开本机页面；重复启动、端口占用和退出行为要有可理解提示。试用包不包含开发者 API Key、真实数据库、日志、工作区或机器绝对路径；朋友自行在设置页配置模型。
- **数据与边界**：为包选定稳定的用户可写数据目录与迁移/备份口径，重启和覆盖升级后保留设置、凭据引用、Checkpoint 和会话。前端静态资源和 Python 字节码会随包送到用户机器，EXE 不是保密机制；项目设计的暴露控制依赖 T39 的服务端投影与界面收口，不能用“打包后看不到源码”替代验收。
- **自动验收**：构建产物含 EXE、静态页及全部运行资源；清洁解压后启动页面/API/SSE，验证重启持久化、单实例锁、目录选择器子进程、核心审批与配置流程；检查分发目录及前端资源中没有密钥、真实数据或开发机路径。保存构建版本、文件清单与校验值，并给出明确的压缩包制作步骤。
- **人工验收**：在没有开发工具的目标 Windows 环境，分别从普通路径及含空格/中文的路径解压，双击启动、设置模型、选工作区、执行只读任务及需审批的修改、刷新、退出、重启、覆盖升级；故障信息能指导朋友自行处理。T37 的朋友试用必须使用这份实际分发包。
- **首次构建与自检（2026-10-08）**：产物 `build/packaging/dist/chaiai`（1927 个文件、111.3 MiB、被 `.gitignore` 的 `build/` 覆盖）。已实测：`GET /api/health` 返回 200；`GET /` 返回标题 chaiai 且引用 `/assets/index-*.js`（证明前端产物确实从包内托管，不是降级成"只提供 API"）；首次启动在隔离数据目录创建 `data/checkpoints.sqlite`（证明可写目录口径生效，且配置确实从数据目录读取）；第二次启动被排他文件锁拒绝并给出"已经有一个 local-agent-web 服务在跑"；自动打开浏览器生效——控制台打出目标地址，服务端日志随即出现真实浏览器拉取 `/`、`/assets/*`、`/chaiai.svg`、`/api/*` 的记录；`_tkinter.pyd` 与 `_tcl_data` 随包（目录选择器具备运行条件）；exe 资源目录含 `RT_ICON` 与 `RT_GROUP_ICON`（图标已内嵌）；敏感数据扫描 0 命中，独立复核也未在包内找到本项目路径、其它参考项目路径或 conda 基路径；Python 全量 **1069 passed / 7 skipped**（基线 1057 + 新增 12 条路径口径与浏览器打开用例）。清单与校验值见产物内 `MANIFEST.txt`，构建环境与源树状态见 `BUILD_INFO.txt`（该次构建时 HEAD 为 `458a686`，**未提交改动 93 条**——包与源码的对应关系尚不能用提交号复现）。
- **已定事项（2026-10-08 用户拍板）**：① **保留控制台窗口**，由它承担"可见的启动/失败提示"，不做隐藏控制台 + 原生弹窗；② **启动后自动打开本机页面**已实现（`src/local_agent/browser_open.py`：后台线程等端口可连再打开，通配监听地址换成回环地址，超时或找不到浏览器只提示、不阻塞服务；入口新增 `--no-open-browser`，自动化与测试统一带这个开关）；③ **EXE 图标**已启用：由 `frontend/public/chaiai.svg` 经 Chromium（4 倍设备像素比截图，256×256 透明底）+ Pillow 生成 `frontend/public/chaiai.ico`（16/24/32/48/64/128/256 七种尺寸），spec 用 `icon=` 写入；④ **包内附使用说明**：源文件 `packaging/使用说明.txt`，由构建脚本复制到包**顶层**（不走 spec 的 `datas`——那会落进 `_internal`，朋友根本看不见），写出时统一为"UTF-8 + BOM + CRLF"以保证任何版本的记事本都能正常显示；内容只有用户视角的启动、填模型、数据位置、已知限制与故障提示，不含内部机制与开发机信息；该文件同样进入 `MANIFEST.txt` 校验。
- **T40 尚未验收（如实列出）**：目录选择器未人工点击验证（只确认 `_tkinter.pyd` / `_tcl_data` 随包）；未在**没有开发工具**的目标 Windows 机器解压验证；"含空格/中文路径解压"与"覆盖升级保留数据"未人工复验；未验证杀软拦截与其它 Windows 版本。
- **2026-10-08 追加：试用包缺 OpenSSL 运行库（已修，朋友实测暴露）**。现象：朋友解压后双击，cmd 窗口一闪而过。核实链：① 包内 `_internal/_ssl.pyd` 的导入表要求 `libssl-3-x64.dll` 与 `libcrypto-3-x64.dll`（pefile 读出）；② 这两个文件**整个包里都没有**（含子目录）；③ 启动链路确实会加载 `_ssl`（导入 `local_agent.api.container` 后 `_ssl in sys.modules` 为真，httpx 在启动时被导入），因此失败发生在任何输出之前——正是"一闪而过"的成因；④ 本机 `C:\Windows\System32` 恰好有这两个文件，所以"清空 PATH 只留 system32"仍能启动，**本机通过不代表干净机器通过**。修复分两处：构建脚本新增第 3 步「DLL 依赖补齐与自包含检查」——包内缺失的依赖，能在解释器环境（`Library/bin`、`site-packages/*.libs`）找到就复制进 `_internal`；只在 System32/SysWOW64 里能找到的算 Windows 自带；`api-ms-win-*` / `ext-ms-*` 属 API set 契约（磁盘上没有对应文件）不算缺失；**其余一律让构建失败**。本次补齐 4 个：`libssl-3-x64.dll`、`libcrypto-3-x64.dll`、numpy 的 `libscipy_openblas64_*.dll` 与 `msvcp140-*.dll`。另新增冻结形态启动入口 `src/local_agent/startup.py`：在导入官方入口**之前**装好兜底，导入阶段崩溃也会把 traceback 写进 `%LOCALAPPDATA%\chaiai\启动错误.log`（spec 入口随之从 `api/__main__.py` 换成它；源码入口仍是 `python -m local_agent.api`，开发路径行为不变）。复验：干净 PATH（仅 system32）下启动新包，`/api/health` 200、页面标题 chaiai，且运行时枚举模块显示 `_ssl.pyd`、`libssl-3-x64.dll`、`libcrypto-3-x64.dll` **全部来自包内 `_internal`**；Python 全量 **1075 passed / 7 skipped**（含新增 6 条启动兜底用例）。
- **构建产物目录与批量删除限制**：本机环境对"一次删除 500 个以上文件"有审批链，而产物目录有 1900+ 个文件，**原地重建会被拦下**。构建脚本因此支持用环境变量 `CHAIAI_OUT_DIR` 指向一个新目录（不删除任何东西）：本次产物在 `build/packaging/out-20261008b/chaiai`（1932 个文件、139.7 MiB，比修复前多 28 MiB 即新增的 DLL）。旧目录（`build/packaging/dist/*`、`build/packaging/out-20261008/*` 及各自的 `_work`）需人工清理，脚本不代删。
- **本次不做**：单文件 EXE、安装器、自动更新、跨平台发行或代码混淆；先让目录式试用包可靠可用。

### T37｜v0.4 集成、视觉验收、本地分发与朋友试用门槛（XL）

- **目标**：完成独立 Web、应用服务、缓存优化和文档的整体验收，形成可分享仓库和可执行试用说明。
- **主要文件**：`README.md`、`ARCHITECTURE.md`、配置样例、FastAPI 静态托管与启动入口、前端构建/Playwright 配置、全链路测试和无敏感信息的验收记录。
- **依赖**：T25–T35（含 T29A）；T36 按进入条件完成或有证据延期。
- **自动门槛**：Python 两种入口全量测试、ruff、mypy、`uv lock --check`、`git diff --check`；前端 `npm ci`、typecheck、lint、unit、build、Playwright；生产静态托管集成测试；无密钥事件轨迹导出。
- **视觉门槛**：T25 状态矩阵在 1440×900 与 1280×800 全部生成截图并逐项核对；差异有书面理由；无默认控件、裸 JSON、关键错位、遮挡、低对比度或临时占位组件。
- **功能门槛**：逐项走通“添加/测试两个兼容模型 → 设置默认与会话下一轮模型 → 选择工作区 → 新建会话 → 两轮对话 → 切换模型 → 工具 → 计划 → 审批/拒绝 → diff/结果 → 刷新 → 重启恢复”；模型切换和重连不重放副作用，在途/等待审批运行不被切换改写。
- **缓存门槛**：实际与估算 Token 分开；未知缓存字段正确显示；真实 DeepSeek 固定场景有优化前后记录，首轮后出现正数 cache read，且正确性评测不回退。若仍为 `blocked-external`，默认不得宣称 v0.4 缓存验收通过；确需发布时必须由人工在验收记录中明确批准豁免，写明证据、影响、未验证声明和补验条件。
- **分发门槛**：生产模式只启动一个 Python 服务、一个 Uvicorn worker 即可使用页面/API/SSE；README 给出开发、构建、启动、配置、故障诊断、单 worker 限制和已知限制；默认只监听回环地址。
- **朋友试用门槛**：在 T38、T39、T40 通过后，由未参与开发的人在一台目标 Windows 环境解压试用包、双击 EXE 完成首次启动和核心任务；观察问题只进入独立清单，阻断项修复后重跑相关自动与人工门槛。此前按 README 源码启动的验证属于 T37 初验记录，不替代打包试用复验。

### T37 验收记录：已通过项与豁免项（2026-10-08，项目负责人批准）

**已通过**

- **朋友试用门槛**：未参与开发的人在目标 Windows 机器上解压实际分发包（`build/packaging/out-20261008b/chaiai`）、双击 EXE 完成首次启动与核心流程；第一版包因缺 OpenSSL 运行库启动失败，修复后通过（见 §T40「2026-10-08 追加」）。卡点一条：模型连接把上下文窗口填 `10000`、输出上限填 `4096`，导致 prompt 预算只剩 5904 token、工具结果被大量裁剪，回答偏薄；按项目负责人要求暂不处理，仅记录。
- **自动门槛**：`ruff check .`、`mypy src`（104 个模块）、`pytest` **1075 passed / 7 skipped**；前端 `typecheck`、`lint`、`vitest` **307 passed**、生产构建；真后端 Playwright 四套（live / streaming / variants / approval）**6 条通过**；默认套件 **165 passed / 3 skipped**。
- **视觉门槛（本轮补齐）**：`prototype.spec.ts` 基线按当前外观重拍，`e2e/__screenshots__` 变更 **89 个文件**，重拍后连跑两次全绿。重拍前有 90 条失败，归因：82 张为有意变更（品牌名称与图标、T44/T45 新增入口、10-08 的折叠与文案改动）、3 条为过期交互断言（"执行过程 · N 个动作"聚合行已在 Think 那轮移除；检索详情改为默认折叠，`MemoryRow` 的 `<details open>` 只在失败态成立）。
  **归因方式的如实说明**：抽样人工查看差异图 + 按已知有意变更分类，**未逐张人工核对全部 89 张**；差异图另存于 `%TEMP%\chaiai-baseline-diffs-20261008`（仓库外，供抽查）。

**豁免（人工批准，写明影响）**

1. **T38 权限专门人工往返**：未做。影响：没有真机确认"放行 / 手动审批切档 + 工作区外读写 + 删除审批"的端到端表现；自动门槛已有通过记录。
2. **T39 剩余人工矩阵**：未做（两视口全状态人工核对、真实连接的在途/待审批模型切换）。影响：真实模型与双尺寸组合下的人工确认缺失；结构与交互已由自动断言覆盖。
3. **真实缓存对照**：未重新测量。影响：**不宣称 v0.4 缓存验收通过**（沿用 `blocked-external` 口径）。
4. **无密钥事件轨迹导出**：未找到面向用户的导出入口——`api/routes.py` 无导出路由、前端无下载/`Blob` 逻辑；v0.3 历史中该能力由评测脚本产出的 `eval/*.md` 实现，而 `eval/` 被 `.gitignore` 排除。影响：**这条门槛当前没有仓库内可复核证据**。

**补验条件**：下次改动触及外观时先重拍基线再改；进入 v0.5 前若新增工具或权限判定，必须先补 T38 的人工往返；缓存与轨迹导出在出现真实需求或发布前补测。

### T43 质量重开记录与后续门槛

保留已验证的缓存/主循环实现，不回退、不恢复固定独立反思调用。曾出现源码流程解释遗漏或混用、记忆漏召回与相关度回显；候选格式已修复，整体质量仍未证明通过。用户重新要求处理，或试用因此阻断时，核实当前源码、冻结新语料，分别测事实正确性、完整性、准确引用、泄漏、缓存读取和未命中输入；不凭引用数量或短样本命中率下整体结论。

证据：[记忆质量](local-agent-web/docs/ui-reference/MEMORY_SETTINGS.md#quality-fix)、[同题缓存对照](docs/benchmarks/cache-paired-20261002.md)。

**2026-10-08 用户重开源码回答质量**：已实际运行本地 DSH 与 chaiai 同题、冻结源码，修正稳定提示的调查范围/分支取证/收尾要求及架构中的过期模式原则。保留范围优先候选；用途题读取 6 个实现文件，检索题说明四条路径，两题无工具错误。合计命中 90.9%，但未命中输入 40,437→92,884、耗时 77.7→128.6s，不能声明无成本退化。既有主循环与工具 Schema 不变、不新增固定判官；显式计划工具有提示要求但实测未调用。引用格式、更多独立真实样本、调查成本仍待验，记忆漏召回不在本轮修复范围；T43 整体未结束。全部候选/失败/条件见同题缓存对照的 2026-10-08 章节。


**2026-10-08 工具证据修复开工**：已确认长文件模型输入被压中段，而 `truncated=false` / `has_more=false` 与可见正文不一致；DSH 裁剪入口为压力/超限，本项目“无条件对标 DSH”注释不准确。本轮按 §10.1 落模型输入投影：真实行号、可见/缺失区间、不完整行与准确续读位置；目录输出去重复字段并保留文件元数据。原始工具结果、审计、页面、Schema 和主循环不改，不新增反思调用。验收包含实际工具到 Prompt 窗口、长行/错误输出/工具边界、前缀稳定、本地质量门槛与原题真实对照；整体回答质量与成本未实测前不得宣称验收。

**本轮结果**：文件模型输入投影已实现，真实输入 31 次编号读取/4 次缺口均与冻结源码一致；1175 passed / 7 skipped，ruff 与 mypy 107 模块通过。四轮同提示/同原题/空库对照：合计命中 94.08%→92.81%、未命中 72,096→81,881、耗时 130.0→106.2s；引用缩写、Web Profile 条件与用途题节点枚举仍有缺口。因此当时只确认新格式样本证据一致性，整体质量和成本门槛未通过，T43 继续开放；不宣称整体验收、不提交发布。原始证据与边界见同题缓存对照的“工具证据投影修复”章节。


**2026-10-09 继续验收未通过**：只做离线审计，未修改生产代码、未新增 API 调用。合法旧格式读取结果正文 12,000→3,901 字符仍 `truncated=false` / `has_more=false`，通用回退裁剪漏修；撤回笼统的证据一致性通过结论，新格式 31 次编号读取核验仍成立。真实检索回答把重要度/置信度系数写成原值，并混用长期事实与任务结论门槛；新用途题误用节点注册顺序解释接线。检索题明确引用 37 处仅 3 处完整路径，两个新轨迹均未显式更新计划。固定轨迹离线投影估算显示目录压缩能抵消行号局部增加，不能把真实未命中上涨全归因于投影；成本门槛仍失败。优先补兼容裁剪，再复验事实/分支/引用/调查成本；不恢复固定判官、不作发布验收。复现脚本和范围见同题缓存对照的“继续验收（离线审计，未通过）”章节。


**2026-10-09 用户批准移植 DSH 链路**：先更新 §10.1 稳定契约，再实现读取半行/旧格式坐标、压力达标后裁剪与持久化重放、重测后摘要（含单个长任务的完整早期工具步）。保留审批/页面/原始审计/工具参数，不改记忆存储、不增加固定判官；真实质量与成本没有通过之前不宣布 T43 结束。现实现已复制到独立 baseline-src，后续同题新旧/DSH 使用同一冻结源码、独立数据库和脱敏 wire；不覆盖已有样本。


**移植结果与最终验收**：读取半行、旧格式坐标未知、压力后裁剪持久化/重放、重测后摘要（含单个任务完整工具前缀）已实现，原始审计保留；1184 passed / 7 skipped（192.25s）、ruff、mypy 107 模块通过。同冻结源码六轮真实对照：改前/新版/DSH 合计命中 92.44%/90.24%/87.83%，未命中 79,289/112,337/85,138，新版上涨 41.7%，成本门槛失败。新回答仍有根 README 引用越界、Web 引用错位、长期任务结论分支遗漏；四条 chaiai 轨迹均未显式更新计划。真实样本未触发压力，高压力路径仅本地测试；DSH temperature 未传且 Q1 额外观察父仓库 Git，单样本与共享供应商缓存不能证明稳定因果。机制落地不等于 T43 整体验收，保持开放，未提交发布。全部明细/复现见同题缓存对照“DSH 读取与压力压缩链路移植”章节。


**2026-10-09 上下文压缩：保留下限与窗口联动、压不动不再静默**：把 `preserve_recent_turns` 从“固定轮数下限”改为与窗口比例联动——新增 `memory.compression.retain_ratio`（默认 0.16，对齐 DSH `retainRatio`），实际保留轮数 = `preserve_recent_turns` 之内、且 token 总量不超过 `retain_ratio × context_window_tokens` 的**最近**轮数（下限 1 轮），并加配置校验 `retain_ratio < trigger_ratio`，避免小窗口下“保留下限大于窗口”导致压缩触发也压不动。当触发压缩但没有可摘要的旧消息、或压到极限仍超预算（`usage_ratio ≥ 1.0`）时，记一条 `CONTEXT_COMPRESSION_FAILED` 可见告警（按 run 去重），不再静默照发。窗口选择与摘要边界共用同一保留轮数计算，不额外丢历史；不改安全审批、不恢复固定判官。前端计量器圆环仍按上限钳制，数字可显示超过 100%，给“超预算”以可见表达。验收：全量 `1213 passed / 7 skipped`（285.08s）、ruff 与 mypy 通过；相关子集 212 passed / 2 skipped；前端 vitest 308 passed、tsc 与 eslint 干净。边界：本轮只做机制修复，未做真实多轮成本/质量对照，T43 真实门槛结论不变；`summary_max_chars` 仍为绝对上限、未与窗口联动（评估结论为非必要）。架构文档 §10.5/§11.4 本次未同步（仅在 `config.yaml.example` 补了 `retain_ratio` 注释）。


## 8. 实施顺序、提交与审查节奏

以下为原始建设依赖链，仅供追溯；当前执行顺序见 §7：

```text
T25 → T26 → T27 → T28 → T29 → T29A → T30 → T31 → T32
    → T33 → T34 → T35 → T36（证据触发）→ T37 初验 → T38 → T39 → T40 → T37 朋友试用复验
```

- T26 被刻意放在应用服务实现前：先把高保真页面壳做出来，尽早验证视觉方向；它只能使用 fixture，不能临时直连 Graph。
- T27/T28 冻结并实现边界后，T29–T32 用纵向切片逐步替换 fixture；T29A 先让用户配置和选择模型，再由 T30 接通真实对话；任何阶段都保持页面可启动。
- T34 先观测、T35 再优化、T36 最后按证据处理窗口；不得跳过基线直接大改 Prompt。
- 每个任务原则上一个独立 Conventional Commit；若任务跨前后端，可在同一任务内分两个提交，但第二个提交前必须保持第一提交可独立测试和回滚。
- 每个任务只做一轮集中常规审查和一轮集中修复；安全绕过、重复副作用、数据损坏、跨会话泄漏、错误审批或不可恢复迁移不受轮数限制，发现即重新打开门槛。
- 后续规划已合并到 `docs/planning/FUTURE_WORK.md`；合并文档必须先保留独有证据，再删除旧文件。提交范围仍须显式核对，是否提交由当轮用户指令决定。

## 9. 验收矩阵

| 场景 | 首次任务 | 自动验证 | 人工/真实验证 | 失败判据 |
|---|---|---|---|---|
| 高保真静态页面 | T26 | Vitest + Playwright snapshots | 两个固定视口逐项截图对照 | 临时样式、关键错位、状态缺失 |
| 会话互斥与幂等 | T28 | 并发、重试、冲突、重启恢复测试 | 两个页面同时提交与超时重试 | 两个 Graph 同时修改会话、重复副作用、重启后丢幂等 |
| SSE 快照与实时增量 | T29 | snapshot-first、顺序、慢消费者、重连测试 | 流式中刷新与服务重启 | 承诺持久游标、遗漏最终状态、重连重调 Graph |
| 模型设置与密钥 | T29A | 迁移、CRUD、版本、凭据脱敏、无效端点与能力测试 | 目标 Windows 机器录入两个独立端点并重启恢复 | Key 泄露、必须重启、设置丢失、失败时暗中回退 |
| 模型切换 | T29A；对话 T30 | 会话选择、在途/审批锁定、跨模型历史测试 | 页面切换后连续对话、刷新/重启 | 在途请求换模型、旧轮统计串到新模型 |
| 会话与消息 | T30 | application/API/frontend tests | 两轮对话、刷新、重启 | 重复消息、上下文丢失、重调模型 |
| 可见执行过程 | T31 | event projection + UI/redaction tests | 工具、计划、记忆/RAG 与错误路径 | 原始 JSON 才能理解、配对错误、泄露隐藏推理或记忆正文 |
| 审批与 diff | T32 | security/resume/API/e2e | 批准、拒绝、双击、刷新 | 绕过审批、重复执行、diff 不一致 |
| Streamlit 对照 | T33 | `tests/test_web.py` + contract tests | 调试入口基本操作 | 两个 UI 得出不同业务状态 |
| Token/缓存观测 | T34 | usage adapter tests | 真实 DeepSeek 优化前基线 | 未报告被显示为零、重复计数 |
| 每轮统计与耗时 | T34 | 聚合、计时、刷新归属测试 | 逐轮检查汇总与明细 | 串轮、等待时间冒充模型耗时、刷新丢失 |
| 圆形上下文计量 | T26；数据 T34 | 当前/上一/未知状态 + 预算口径测试 | `371k / 1000k` 类固定场景 | 旧值冒充当前草稿、已用/最大口径错误、比例与颜色不一致 |
| 稳定前缀 | T35 | byte/hash stability + full eval | 真实多轮 cache read | 正确性回退、system hash 随动态状态变化 |
| 动态上下文快照 | T35 | Checkpoint/消费者隔离测试 | 重启后请求字节重建 | 显示为用户气泡、触发记忆/摘要/RAG 或建立伪轮次 |
| 缓存 epoch | T36 | compression/projection regression | 长对话逐轮命中记录 | 为命中删除原文或放宽预算 |
| 生产分发 | T37 | build/static/API/e2e gates | 新环境按 README 启动 | 仍需单独 Node 服务、非回环默认监听 |
| 权限模式与外部路径 | T38 | 模式/路径矩阵与四项强制审批回归 | 切换档位、工作区外读写及删除审批 | 默认放行外部路径、敏感操作绕过审批 |
| 朋友可理解的展示 | T39 | 浏览器 DTO/SSE/导出字段负例、前端交互与截图 | 普通对话、轨迹、长输出、失败和刷新恢复 | 原始工具名/ID 出现在页面、不应暴露字段进入浏览器、惰性按钮 |
| Windows EXE 试用包 | T40 | onedir 构建、资源清单、无敏感数据检查、打包后 API/SSE/重启测试 | 无开发工具机器解压双击、初设、目录选择、审批、退出和升级 | 缺资源、需命令行、数据随更新丢失、包内带密钥或真实数据 |
| 完整试用 | T37 | 全量门槛 | 未参与开发者使用 T40 试用包完成核心任务 | 阻断操作、审批不可达、重复副作用 |

## 10. 风险与停止条件

1. **页面优先变成业务复制**：React fixture 只能存在于展示测试，接真实能力后所有业务状态来自应用服务。若页面开始自己推断任务成功、审批或工具结果，停止前端扩展。
2. **事件成为第二真源**：事件可以由快照重新投影并用于展示，但不能恢复 Graph 或驱动工具。出现 State 与事件可分别修改同一事实时停止。
3. **SSE 被误当后台运行时**：断线、刷新和服务重启语义必须由 Checkpoint 与快照解释；不能承诺 v0.4 未实现的后台继续运行和取消。
4. **高保真变成源码复制**：只复刻视觉与交互结果；发现复制品牌、专有资产或运行时架构时停止并替换。
5. **缓存优化损害正确性**：记忆、RAG、计划、安全、审批、错误承诺修正或固定评测任一回退，停止缓存改造并回到最近通过的阶段。
6. **供应商指标不可用**：缓存字段缺失必须显示未知。没有真实 usage 时只能完成观测链路的结构测试，不能宣称已提高命中率。
7. **本地服务被误开放**：默认监听非回环地址、宽松 CORS/Origin 或无提示远程访问均阻塞发布。
8. **前端工具链吞噬项目**：不引入 Monorepo、SSR、微前端或插件体系；新增基础设施必须服务至少两个已确认的 v0.4 用户场景。
9. **模型设置泄密或中途换模**：Key 进入可读取 API、日志、Checkpoint、浏览器持久存储，或已开始运行在审批恢复前更换模型版本，立即停止该任务并修复；设置页不能成为绕过工具/审批策略的入口。

## 11. v0.4 完成定义

只有同时满足以下条件，才能把 v0.4 标记为实现完成：

- T25–T35（含 T29A）全部通过；T36 已完成或根据真实证据明确延期；T38、T39、T40 通过后，T37 朋友试用门槛使用分发包复验通过。
- 独立 Web 达到确认过的 DeepSeek Harness 高保真目标，朋友不需要通过 Streamlit 或原始 JSON 才能使用核心能力。
- React 与 Streamlit 都只通过应用服务访问业务能力；Checkpoint 仍是唯一执行恢复真源。
- 页面刷新、SSE 重连和审批恢复不重复模型、工具或文件副作用。
- 用户能在 Web 中保存并测试多个兼容聊天模型连接、设置默认与会话下一轮模型，不重启服务即可切换；目标 Windows 环境的密钥安全保存、两端点实测、刷新/重启恢复和在途运行锁定全部通过。
- 实际 Token、缓存读取和未知状态能在事件与页面中正确表达；真实 DeepSeek 场景有可复核的优化前后数据，并在首轮后出现正数 cache read。若因外部原因无法验证，只有带证据且经人工明确批准的发布豁免才能关闭版本门槛，完成说明必须继续标注“缓存命中未验证”和补验条件。
- 每个已完成助手轮次都有优美、紧凑且可展开的 Token/缓存/耗时统计；输入区旁有与真实预算口径一致的圆形“已使用/最大上下文”计量器。
- 页面按 §6.7 展示安全、可验证的执行过程；工具、计划、记忆/RAG、审批和错误可理解，但不泄露隐藏思维链、完整 Prompt、密钥或未经脱敏的记忆正文。
- 现有 v0.1–v0.3 固定评测、全量测试、安全和恢复能力没有回退。
- README、架构、配置、代码、截图和已知限制一致；没有把尚未实现的后台任务、取消、后台并发调度、持久游标或远程访问描述为可用。


### T43 继续修复（2026-10-09）

用户要求继续修复，停止打包工作。已从真实失败确认四次摘要输出 4096 token 全为 reasoning、content 为空；先补 SDK wire 失败回归，再仅对 DeepSeek 摘要用途关闭 thinking，保留主循环与其它供应商语义。方法题的计划首步、收尾更新和公式括号要求放在稳定调查提示，不增加动态判官、不伪造已完成计划。先验证摘要和压力/预算回归，再在此前累计 ¥5 授权剩余额度内以独立数据库复验两题；旧失败保留，真实质量与成本通过前不关闭 T43。

本轮真实压力根因补充：摘要成功后仍使用 12000 字符上限，工具步只按 20000 字符留存，二者未扣除固定提示与工具定义；压缩后估算反而超过预算。修复须按实际 token 余量联动，最近完整步确实超限时仍告警。计划完成的同批保护保持，调查指引要求每次计划更新独立调用；grep 公开说明现有模式长度上限及 path/glob 区别。

T43 继续修复补充：真实压力记录证明摘要会丢失全任务目标和已完成计划；压缩时由代码保留目标/完整标题状态，过小配置不推进覆盖边界。另确认锚点覆盖后冻结快照移到末尾导致两次非追加改写，已固定到摘要之后并覆盖下一轮追加。原快照测试改为检查末块当前快照，保留系统前缀不变断言和历史块保留回归。累计预算保护未重置；仅按核实的闲时价格预留最坏费用，旧高峰预留拒绝保留。

T43 2026-10-10 离线续修：verified 压力记录确认程序将证据摘要压至 1 字符且推进覆盖，重复目标/计划包装消耗证据空间。须先去除受控包装再滚动归纳，保护项后不足 128 字符时不购买摘要或推进覆盖。连续整数行号列表须逐项补路径，验收器不得只检查首项而放过裸行号。历史原始压力失败保留，不追认为通过。

T43 verified 真实验收结果：普通/重复题命中率 85.91%/95.19%/93.42%，20k 压力 failed、46.11%。原始 Q2 的裸行号列表门失败；当前续修离线重放补齐 21 项，路径/区间有效。累计原生用量闲时费用估算 ¥4.87160888/¥5，未重置预算、未再付费、未生成新 ZIP。15 次真实坏摘要覆盖被当前源码离线阻止；真实全任务压力与语义质量门仍未收口，不能宣布整体交付通过。详见 docs/benchmarks/v05-review-20261009.md。

当前源码最终自动回归：1336 passed、8 skipped，Ruff 与 Mypy（112 个源文件）通过；日志 local-agent-web/eval/v05-delivery-20261009/pytest-final-source.log。未据自动回归追认真实压力/语义质量门通过。

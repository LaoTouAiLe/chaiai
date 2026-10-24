# v0.6 开发计划：后台任务与长时间运行

> **状态**：2026-10-11 **T57–T63 已实现，复审三项 P1 已修复，完整离线回归 1482 passed / 8 skipped / 0 failed，未提交**。回答语义质量审查暂不进行。
> **依据**：`docs/planning/FUTURE_WORK.md` §v0.6「后台任务与长时间运行」的六项主要工作、五条验收与「不做」，以及其中的 B 编号条目（B-03 / B-07 / B-12 / B-21）。
> **前置**：v0.5 六项主要工作全部落地、五条验收判据均有证据（见 `V0_5_DEVELOPMENT_PLAN.md` §T50–§T56）。v0.5 的**收口与测试审查由项目负责人单独整理**，本文件不代替它。
> **效力**：本文件只负责 v0.6 的实施顺序。方向与"不做项"以 `FUTURE_WORK.md` 为准；两者冲突时先改文档，不允许由代码自行选择一种解释。**实施任何任务前，必须先把该任务涉及的稳定契约同步进 `ARCHITECTURE.md`（v0.6 的契约载体是新增 §17）。**
> **与 v0.7 的边界**：Subgraph、确定性工作流、受控子 Agent、并发/Token 硬上限全部属 v0.7，v0.6 一律不建（`FUTURE_WORK.md:378-401`）。v0.6 只把"一次提交 = 一个同步 HTTP 请求"改成本机后台任务，并把取消、超时与恢复做成**可观察的状态机**。v0.7 的"重启后恢复主任务并识别已完成子任务"依赖本阶段的恢复策略，因此任务索引要为"子任务标记"留出扩展位，但本阶段**不加实现、不加配置项**。
> **外部对照**：§7 记录了 dsh（`D:\ideaTest2\deepseek-harness`）与 Codex（`E:\pylianxi\codex`）在后台任务 / 取消 / 恢复上的实测核验，本阶段的取舍以它为依据。两家**都不持久化任务状态**，而本阶段必须持久——这是本项目与它们的最大分歧，也是主要工作量所在。

## 1. 范围

### 1.1 做什么（路线图 §v0.6 的六项主要工作，逐项落成任务）

1. 建立持久任务索引，区分**会话、任务与单次运行**（并接上 B-03 的会话来源门，见 §T57）。
2. 建立本机后台 Job Runtime；任务状态至少覆盖**排队、运行、等待用户、完成、失败、取消**。
3. 把长任务进度转换为**结构化事件**（输入之一是 v0.5 §T56 未做③：能力状态目前只有 REST、没有事件）。
4. 支持**协作式取消、超时、资源上限与进程树清理**。
5. 明确"**副作用已发生但状态未提交**"的恢复策略，不自动假定恰好执行一次。
6. 页面提供**任务中心与恢复入口**。

### 1.2 这次不做（沿用路线图原话与既有裁定）

- 分布式队列、远程 Worker、多机调度（路线图明确不做）。
- Subgraph、受控子 Agent、确定性工作流（v0.7）；不建并发/委派层级。
- **B-12「dreaming 式后台晋升」**：已裁定**不做**；本阶段也不采集它的重开证据、不顺手做（复核见 §5.3）。
- **不引入第二套存储**：任务索引必须复用既有的**会话索引 + Checkpoint + 迁移机制**（同一库、同一迁移入口），不得新起一套库或第二套任务真源。
- **不引入第二套取消检查路径**：取消语义挂在既有的两道门上——能力可见性（`capabilities.is_visible`）与安全/审批门（`security/policy.py`），不在工具管线里另加一条取消分支（外部对照见 §7.1 / §7.3-1）。
- **不设能力式开关**：不新增"关闭即不可用"的 `jobs.enabled`；`jobs.background` 只决定**调度时机**（请求内 / 后台），两条路共用同一状态机与同一个图执行入口（§5.1-1）。
- **不改 §15.2 的每会话单飞与 `SESSION_BUSY`**：路线图要求的"排队"只作为**任务状态**存在（多会话竞争 worker 时出现），不引入同一会话的多请求队列（§5.1-3）。
- 持久事件游标、断线补发、分页、WebSocket（v0.8）；v0.6 的 SSE 仍是**进程内投影**，不是任务真源。
- 部署拓扑仍是**单进程、单 Uvicorn worker**（§15.1）；多 worker 仍是不受支持配置。

### 1.3 五条阶段验收 → 任务落点

| 路线图 §v0.6 验收 | 落在 |
| --- | --- |
| 关闭浏览器后任务可按策略继续，重新打开可查看状态 | T58（Runtime）+ T62（任务中心） |
| 服务重启后能区分可恢复、失败和需要人工核对的任务 | T61 |
| 取消后不会启动新的工具调用；已启动进程按平台策略终止并记录结果 | T60 |
| 一个任务等待审批时不阻塞其他会话的只读操作 | T58 + T60 |
| 任务状态变化有自动测试和页面演示 | T59（事件）+ T62（页面 + 截图基线） |

## 2. 契约改动点（先改契约，再编码）

| `ARCHITECTURE.md` 节 | 要写进去的东西 |
| --- | --- |
| §4 LangGraph State | 任务标识与运行标识的归属；后台任务由谁持有 `thread_id` / `run_id`；本轮字段与会话级审计字段的边界（沿用 T43/T52 既有划分，不新增第二套审计字段） |
| §10.6 会话管理 | **会话来源类型 `kind` 的闭集（`interactive` 默认 / `scheduled` / `heartbeat`）**，以及它与既有 `mode`（`chat`/`agent`）的区分；后台/心跳会话与记忆写入的关系（B-03） |
| §15.2 应用服务用例与每会话单飞 | **单飞语义不变**（`SESSION_BUSY` 与"不排队"原样保留，`:1672-1674`）；新增任务用例（提交 → 后台运行 → 查询 / 取消 / 恢复），并写明"排队是任务状态、不是会话提交队列" |
| §15.3 HTTP 与 SSE | 新增任务相关端点；重申 SSE 只投影、POST 仍须返回稳定命令结果引用 |
| §15.4 事件契约 | `job_queued / job_progress / job_waiting / job_completed / task_cancelled` **从 `RESERVED_FOR_V06` 转为正式**；`task_id` 与 `run_id` 分离的口径（v0.4 允许两者复用同一底层标识，`:1730-1731`） |
| §15.5 错误模型 | 取消不可用、状态非法迁移等新增/复用的错误码与可执行提示 |
| §6.7 / §6.8 `grep` / `glob`（**T63 新增**） | 两个工具各加 `save_full: bool = False`（**模型可见的 schema 变更**，因此必须走"先改契约"），并新增"**超限结果可恢复**"整段：locator 语义、落盘位置与命名、自忽略、保留策略、失败绝不变成错误、默认行为逐字节不变 |
| §6.7 / §6.8 跳过口径（**T63 第二片新增**） | 检索遍历**尊重 `.gitignore`**（被忽略的目录整个剪掉；嵌套规则深层优先、`!` 可反选；语义用 `pathspec`）；两条边界写死（只影响枚举、无 `.gitignore` 时行为不变）。**新增直接依赖 `pathspec`**（原本是传递依赖，现在显式声明） |
| §6.7 **超限时怎么选**（**T63 第三片新增**） | **同级轮转**：同目录条目按名整体排序后轮流各出一条、逐层递归 ⇒ 上限决定"看到多少"、不再决定"只看得到哪一个子树"（dsh 的 `sampleOverCapGlobResults=true` 语义）；四条边界写死（未触顶集合不变、顺序确定、**不开配置开关**、`note` 必须说明这是采样）。`grep` / `glob` / 按需 RAG 共用 `rag/walk.py` 一份实现 |
| §15.7 运行上下文快照 | 任务状态在快照里的呈现（有则写，无则明写"不进快照"） |
| §17.1 任务状态闭集（**T61 按"先改契约"修订**） | 由六值改为七值：追加 `needs_review`（终态）。触发者是 T61 的"需人工核对"——它必须与"失败 / 取消"可区分；事件仍沿用 `job_completed`（载荷 `state` 区分），**§15.4 的事件全集不动** |
| §8 配置契约 | 新增 `jobs` 段：`background`（默认 `false`）、`max_concurrent_jobs`（默认 `1`）。**`max_task_seconds` 不在 T58 落地**——没有行为对应的配置键就是空转项，它连"超时处置"一起归 T60（§17.2 已记这条裁定） |
| **新增 §17 v0.6 契约** | 任务索引、Job Runtime 状态机、取消语义、恢复策略、结构化任务事件、页面任务中心在本节冻结；本阶段结束时本节即验收依据 |

**同步文档（属"先改契约/文档"，与实现同批出现，不允许先落后实现）：**

- `local-agent-web/README.md:84` 现写着"单进程/单 worker；不提供公网、多用户、后台任务队列或真实取消能力"——v0.6 做的正是其中两项。**T57 不动它**（该任务只建索引，产品行为未变）："后台任务"半句随 **T58**、"真实取消"半句随 **T60**，与实现同批改。
- `local-agent-web/config.yaml.example`：新增 `jobs` 段（与 `web_access` / `git` / `skills` 同构）。
- `docs/planning/FUTURE_WORK.md` §13「当前下一步」在立项后指向本文件；B 条目的实施状态在开工后回填（B-03 / B-21）。
- `docs/api/openapi.json`：按仓库既有门槛**重新生成并逐字节比对**（不手改）。

## 3. 任务拆分（T57–T63，按顺序；每个都能独立验收）

> **T63 不在路线图原列的六项里**：它是 T57–T62 收口后与 dsh / codex 逐条对照时发现的**能力缺口**
> （`grep` / `glob` 超限即丢失）。放在本阶段做的理由与验收写在 T63 一节里。

### T57｜持久任务索引、会话来源类型与 B-03 前置门

- **目标**：在持久层区分**会话 / 任务 / 单次运行**，并给会话加来源类型；让后台/心跳会话**不产生可自动注入的持久记忆**（B-03，作为 v0.6 的任务前置条件）。
- **会话 `kind`（闭集三值，默认 `interactive`）**：`interactive` / `scheduled` / `heartbeat`；前两者与后者由后台任务产生，`interactive` 是既有会话。**不留 `subagent` 取值**（v0.7 开工时追加）。`sessions` 已有 `mode`（`chat`/`agent`，本轮怎么跑），§10.6 必须用一句话把 `mode` 与 `kind`（会话从哪来）切开。
- **B-03 的落法（不新造标记）**：`memory/schemas.py:73` 的 `ORIGIN_SYSTEM` 目前**没有任何生产者**；后台/心跳会话就是它的第一个真实生产者——这类会话产出的记忆候选按 `origin=system` 落库（`is_auto_injectable` 只认 `owner`，因此与 `untrusted` 一样**结构性不自动注入**）。
- **落点（已核实的真实位置）**
  - `local-agent-web/src/local_agent/persistence/migrations.py:24`（`CHAT_SCHEMA_VERSION = 6`；本任务后为 7）、`:260-310`（`_CHAT_MIGRATIONS`，**只在末尾追加**）、`:23-33`（三个库各自的版本常量；现状是聊天库 6 / 长期库 3 / 设置库 3）。
  - `local-agent-web/src/local_agent/persistence/session_store.py:49-53`（`_COLUMNS`）、`:57-71`（`Session`）、`:276-283`（`create`）——加 `kind` 列与字段，历史行由迁移默认值补齐。
  - 新增 `local-agent-web/src/local_agent/persistence/job_store.py`（任务索引，与会话索引、Checkpoint **共用同一个库与同一个迁移入口**，见 `migrations.py:3-5`）。
  - `local-agent-web/src/local_agent/memory/schemas.py:62-108`（`Origin` 闭集与 `is_auto_injectable`；**闭集扩项须先改 §16.2/§17，不在此任务随手加值**）、`local-agent-web/src/local_agent/reflection/memory_candidates.py`（候选打标校验）。
- **验收**
  1. 迁移 v7 幂等：旧库升级后历史会话 `kind` 为默认值，数据不丢；重复 `apply_migrations` 不重复执行（`migrations.py:367-384`）。
  2. 任务索引能按 `session_id` 列出该会话的多次运行，且与 `session_store` / Checkpoint 在同一库、同一事务边界内。
  3. 后台/心跳会话产出的候选以 `origin=system` 落库；这些记忆**无论召回频次多高都不进 Prompt**（沿用 `test_memory_origin.py` 既有断言口径）。
  4. 既有 `interactive` 会话的写入行为**逐项不变**（origin 仍为 `owner`）。
- **明确不做**：不为尚无生产者的来源（子 Agent，v0.7）预留 kind 取值或开关——按 §16.5 的既有裁定"不造空转的配置项"，v0.7 开工时再追加；不改 `is_auto_injectable` 的判定口径。
- **状态（2026-10-10，已完成，未提交）**：契约 §10.6、§4.2 与**新增 §17.1** 已冻结（§17.2–§17.7 标注未冻结）。实现落点：新增 `src/local_agent/session_kind.py`（会话来源的**叶子模块**：闭集 + `SessionKind` 字面量；计划原落点没列它，原因同 `capabilities.py`——`session_store` 与 `graph.nodes` 都要读同一份闭集，放任一侧都会多出一条依赖边）、新增 `src/local_agent/persistence/job_store.py`（任务索引读写 + 终态不可改写）、`persistence/migrations.py` 新增聊天库 v7（`sessions.kind` + `jobs` 表，`CHAT_SCHEMA_VERSION` 6→7）、`persistence/session_store.py`（列/占位符/dataclass/行解析/`create` 各加 `kind`）、`graph/state.py` 加 `session_kind`、`application/service.py` 提交时写入、`graph/nodes.py` 新增 `_candidate_origin` 三步优先级链（`untrusted` > `system` > `owner`）、`exceptions.py` 加 `JobStoreError`。
- **T57 的验收证据**：新增 `tests/test_session_kind.py` **8 条**（闭集与字面量同源、任务状态闭集与终态子集、v6→v7 迁移幂等且历史行 `kind='interactive'`、库层 `CHECK` 拒绝脏值（sessions 与 jobs 各一条）、`create` 默认值/闭集外拒绝/读写往返、任务索引与会话索引**同库同连接**且换连接仍可读、状态链保留 `run_id` 且终态不可改写 + 四类越界输入明确失败）；`tests/test_memory_origin.py` 新增 **6 条**（interactive 仍是 `owner` 的对照组、`scheduled`/`heartbeat` → `system`、两条件同时成立取 `untrusted`、旧 Checkpoint 缺字段按 `interactive`、失败的外部调用不升级为 `untrusted` 但会话门仍成立）。Python 全量 **1350 passed / 8 skipped**（4:48）；`ruff check src tests scripts` 与 `mypy src`（114 文件）通过。
- **T57 未做（如实列出）**：① `jobs` 表与 `JobStore` 已建好但**还没有生产者**——第一个消费者是 T58 的 Job Runtime（先建表是为了让运行期不需要第二次迁移）；② `README.md:84` 按上表未动；③ `docs/planning/FUTURE_WORK.md` 的 §13 与 B-03 / B-21 状态未回填。

### T58｜本机后台 Job Runtime 与任务状态机

- **目标**：把"提交 HTTP 请求 → 同步跑完整轮图"改成本机后台任务；任务状态覆盖**排队 / 运行 / 等待用户 / 完成 / 失败 / 取消**。
- **单一状态机、两种调度时机（§5.1-1）**：`jobs.background=false` 表示在提交请求内跑到终态，`true` 表示入队由 worker 跑；**图执行入口只有一处**，不得出现第二份 `_drive`。外部对照（codex 的"Session 内单活动 Turn + 取消是独立路由"）见 §7.2。
- **落点（已核实的真实位置）**
  - `local-agent-web/src/local_agent/application/service.py:1247-1250`（`submit_message` 主入口）、`:1402`（`_guarded`）、`:1431-1437`（`lock.acquire(blocking=False)` → 当前即 `SESSION_BUSY`）、`:1570`（`_drive`）、`:1609-1614`（`self._graph.stream(payload, config=..., stream_mode="updates")`——**当前在调用线程内跑完整轮图**）。
  - `local-agent-web/src/local_agent/api/routes.py:337-348`（提交处理器是**同步 `def`**，docstring 写"同步跑完这一轮"）。
  - `local-agent-web/src/local_agent/application/service.py:1-23`（模块 docstring 明写"第二个请求返回 `SESSION_BUSY`，**不排队**"）。
  - 新增 `local-agent-web/src/local_agent/application/jobs.py`（状态机 + 单进程 worker 池；**唯一**的图执行入口，不复制 `_drive`）。
- **验收**
  1. 状态机是闭集，非法迁移被拒并有测试；每次迁移写入任务索引，可重建。
  2. 关闭浏览器（断开 SSE）后任务继续；重新打开能从索引 + Checkpoint 读出状态（不依赖事件重放）。
  3. 一个任务等待审批时，**其他会话**的只读用例不被阻塞（沿用每会话单飞的边界，不扩大到全局锁；外部同构见 §7.1 / §7.2）。
  4. 同一 `client_request_id` 的幂等语义与 v0.5 一致（复用 `persistence/request_ledger.py`，**不复制台账**）。
  5. `jobs.background=false` 时提交路径与 v0.5 逐字节一致；`true` 时**图节点调用序列与事件序列逐项一致**，只差"POST 返回时任务是否已终态"（见 §4 硬验收第 9 条与 §5.1-1 的观测量）。
- **明确不做**：多 worker / 进程池 / 持久队列（单进程内 worker 即可满足路线图验收）；不把 `request_ledger` 改造成任务真源；**不改 §15.2 的每会话单飞与 `SESSION_BUSY`**（§5.1-3）。
- **状态（2026-10-10，已完成，未提交）**：契约 **§17.2 已冻结**；§15.2 写明"排队是任务状态、单飞不变"；§15.3 写明 `background` 两种取值的返回与一致口径；§8 登记 `jobs` 段；`CommandResultView` 增加 `status="running"` 与 `job_id`。实现落点：新增 `src/local_agent/application/jobs.py`（`LEGAL_TRANSITIONS` + `JobRuntime`：`begin` / `resume` / `run_now` / `dispatch` / `wait_for_idle` / `close` / `job_id_for_run`）、`config.py` 加 `JobsConfig`、`application/service.py`（`_guarded` 落台账后按调度时机分支、`_accepted`、`_release_after` 把执行锁交给 worker 释放、提交预检里的索引门、审批恢复 `resume_job=True`、`_replay` 复原 `job_id`）、`api/container.py` 装配（**同一把 Checkpoint 锁**）、`persistence/job_store.py` 加共享锁、`config.yaml.example` 加 `jobs` 段、`local-agent-web/README.md:84` 的"后台任务队列"半句与实现同批更新。`docs/api/openapi.json` 与 `frontend/src/api/schema.gen.ts` 按门槛重新生成（`export_openapi.py --check` 与 `npm run check:api` 均通过）。
- **T58 的验收证据**：新增 `tests/test_jobs.py` **10 条**（迁移表逐条对照契约、同会话第二个任务被拒且终态后可开新任务、`awaiting_approval` 停在 `waiting_user` 且恢复复用同一个任务、旧待审批会话按需补建行、内联路径在调用线程内落终态、**`queued` 是真状态**（单 worker 忙时第二个会话的任务停在排队）、`close` 幂等、异常后任务留在 `running` 且写 `detail`、已终态行不被改写）；`tests/test_api.py` 新增 **3 条**端到端（后台提交返回 `running` + `job_id`、索引收敛到 `completed` 且快照给出结局、同 id 重放拿到同一 `job_id` 且不新增任务；同一会话有非终态任务时第二个提交仍 `SESSION_BUSY`；**同一场景在两种调度时机下的可见结果与决策阶段序列逐项一致**）。Python 全量 **1363 passed / 8 skipped**（4:28）；`ruff check src tests scripts`、`mypy src`（115 文件）通过。
- **T58 抓到的两个真实缺陷（本轮最有价值的产出）**：① **死锁**：`JobStore` 在 `update_state` 里嵌套获取 Checkpoint 的锁，而 `langgraph` 的 `SqliteSaver.lock = threading.Lock()` **不可重入**——第一条走容器的 api 用例直接卡死（单测用 RLock 看不出来）。修法：`update_state` 改用不加锁的私有读取，并**把单测的锁换成不可重入的 `threading.Lock`**，让这类问题在单测层就能复现；② **重放丢任务标识**：`_replay` 不经过提交路径，返回的 `job_id` 为空，违反"同 id 重试拿原结果"。修法：按"会话 + 轮次"反查索引复原（台账只存 `result_ref` 与 `run_id`），并把这条写进 §17.2。
- **T58 未做（如实列出）**：① 取消与进程终止（T60）；② 崩溃恢复与"需人工核对"的判定（T61，本任务遇到异常时只把任务留在 `running` 并写 `detail`）；③ 页面任务中心（T62），因此 `status="running"` 目前只有 API 消费方能看到；④ `max_task_seconds` 与"执行中中止"一并归 T60（§5.1-4 的收敛）；⑤ `docs/planning/FUTURE_WORK.md` 与 B 条目状态未回填。
- **T58 的口径差异（如实列出）**：验收里的"**图节点调用序列**"用 Checkpoint 中 `decision_trace` 的 **`stage` 序列**作代理——节点名本身不落库；两种调度时机下它逐项一致，且事件序列由既有投影用例守住。

### T59｜结构化任务事件

- **目标**：把任务生命周期转成结构化事件；`RESERVED_FOR_V06` 五项**转正**。
- **落点（已核实的真实位置）**
  - `local-agent-web/src/local_agent/application/events.py:42-62`（`EventType` 闭集）、`:65-85`（`EVENT_TYPES`，测试据此对照 §15.4）、`:100-107`（`RESERVED_FOR_V06`：`job_queued / job_progress / job_waiting / job_completed / task_cancelled`）、`:110-120`（`event_id` 的稳定来源口径）。
  - `local-agent-web/src/local_agent/application/projection.py`（单向投影，不得写回 State）。
  - `local-agent-web/src/local_agent/api/sse.py:58-112`（`event_stream` 只轮询订阅队列，不跑图——事件由跑图线程 `publish`）。
- **验收**
  1. 事件仍是**单向投影**：生成事件不写 State、不调模型、不执行工具；`event_id` 由稳定来源派生（同 Checkpoint 重放得相同 id）。
  2. `RESERVED_FOR_V06` 清空，五项成为正式事件；`EventType` / `EVENT_TYPES` / §15.4 三处一致（既有守卫测试保持通过）。
  3. 任务状态**可从 Checkpoint + 索引重建**，事件只用于增量展示与审计（不因丢事件而丢终态；dsh / codex 的"日志与状态分离"同源，见 §7.1 / §7.2）。
  4. v0.5 §T56 遗留③（能力状态只有 REST、无事件）按"是否有真实消费者"判定：**无消费者则不补事件**，只在契约里写明现状——不得为对称而造事件。
- **明确不做**：持久游标 / `after_cursor` / 断线补发（v0.8）；用 WebSocket 替换 SSE。
- **状态（2026-10-10，已完成，未提交）**：契约 §15.4 的最小事件集合加入五项并写明信封例外；**§17.3 已冻结**（状态→事件类型表、信封与 `event_id` 规则、单向投影与"不是真源"、明确不做）。实现落点：`application/events.py`（`EventType` / `EVENT_TYPES` 扩到 **24** 项、`RESERVED_FOR_V06` 清空、新增 `_JobEnvelope` 与 `JobStatePayload` / `TaskCancelledPayload`、五个事件模型、`_JOB_EVENT_CLASSES` / `is_job_event_type` / `build_job_event`）、`application/jobs.py`（`JOB_EVENT_FOR_STATE`、`set_transition_listener`、`_notify`——**登记也算一次迁移**）、`application/service.py`（`_on_job_transition` 投影并广播）。`docs/api/openapi.json` 与 `frontend/src/api/schema.gen.ts` 重新生成，漂移检查通过。
- **T59 的验收证据**：新增 `tests/test_job_events.py` **6 条**（状态→类型映射不重不漏且五个类型都已转正、`build_job_event` 产物能被 `parse_event` 认回具体模型并满足信封规则、`run_id` 可空而 `task_id`/`job_id` 非空、非法类型被拒、**一次内联执行恰好三次通知**、只改 `detail` 的同状态写入**不发事件**、审批往返的完整通知序列）；`tests/test_api.py` 新增 **1 条** SSE 用例（同一条流上按序收到 `job_queued → job_progress → job_completed`，`job_queued` 的 `run_id` 为空、`job_progress` 仍为空、**终态才带 `run_id`**，且 `job_completed` 排在 `task_completed` 之后）；`tests/test_events.py` 的守卫更新到 24 项并断言 `RESERVED_FOR_V06 == ()`。Python 全量 **1370 passed / 8 skipped**（5:11）；`ruff check src tests scripts`、`mypy src`（115 文件）、`export_openapi.py --check`、`npm run check:api` 全部通过。
- **T59 抓到的两处口径修正（实测）**：① **登记也是一次迁移**——`job_queued` 最初根本没发出去（`JobStore.create` 不经过 `_advance`，因此没有通知）；修法是登记后显式通知，并把监听器签名改成 `before` 可为 `None`（含义是"任务诞生"）。② **`job_progress` 的 `run_id` 实测为空**——任务进入 `running` 早于图交出轮次；契约因此改成"**只有终态事件才一定带得到 `run_id`**"，测试改为断言这个事实，而不是按预设写一个"应当已存在"。
- **T59 未做（如实列出）**：① 取消的**触发**（T60）：`cancelled → task_cancelled` 的映射已成立、有测试，但没有产生这条迁移的入口；② 能力状态的事件（v0.5 §T56 未做③）按"没有真实消费者"**不补**（§17.3 已写明现状）；③ 持久游标与断线补发（v0.8）；④ `docs/planning/FUTURE_WORK.md` 与 B 条目状态未回填。

### T60｜协作式取消、超时、资源上限与进程树清理

- **目标**：取消能真的停住后续工具调用；已启动进程按平台策略终止并记录结果；超时与资源上限有硬边界。
- **取消挂在哪两道门（不另开检查路径）**
  - 能力可见性：`local-agent-web/src/local_agent/capabilities.py:312`（`is_visible`）。
  - 安全/审批门：`local-agent-web/src/local_agent/security/policy.py:94-100`（`SafetyDecision`）、`:51-91`（`READ_ONLY_TOOLS` / `WRITE_TOOLS` / `SHELL_TOOLS` / `ALWAYS_APPROVAL_TOOLS` / `GIT_WRITE_TOOLS` / `KNOWN_TOOLS`）。
  - 生效点：`local-agent-web/src/local_agent/graph/nodes.py:1385`（`safety_gate_node`）里的 `:1406`（`is_visible`）与 `:1418`（`classify_tool`），以及 `:1602`（`tool_execution_node`）的 `:1638`（纵深防御 `is_visible`）——**取消后不再启动新的工具调用由这两处统一保证**，不新增一条取消分支。
- **落点**：`local-agent-web/src/local_agent/application/service.py`（提交/取消用例）、`application/jobs.py`（状态迁移与取消信号）、`local-agent-web/src/local_agent/security/shell_guard.py`（子进程启动与终止）、`local-agent-web/src/local_agent/application/errors.py:150-153`（`_RETRY_CONDITION`）与 `:348-353`（`SESSION_BUSY` 文案中"v0.4 不公开取消接口"一句**随契约更新**）。
- **验收**
  1. 取消后**不启动新的工具调用**——用可注入的假 runner 证明（含"取消与工具启动竞态"用例），不依赖真实模型；**中断必须先让运行中的任务观察到取消、再清空待审批**（次序取自 codex `tasks/mod.rs:561-566,620-622`，见 §7.2）。
  2. 已启动进程按平台策略终止（**含子进程树**），终止结果与退出状态如实记录，不伪造"已停止"；Windows 下必须实测证明整棵树被终止——dsh 的 `taskkill /T` 与 codex"非 Unix 只杀直接子进程"是两条现成对照（§7.1 / §7.2）。
  3. 资源上限**复用既有项**（`security.shell.default_timeout_seconds=60`、`agent.max_tool_loops=60`、`agent.max_retries_per_step=2`、`security.shell.max_output_chars=30_000`），只新增 `max_concurrent_jobs`（默认 1）与 `max_task_seconds`（默认 `None`）；显式设上限后超限进入明确终态并记录原因（§5.1-4）。
  4. 取消不影响**其他会话**的运行与只读操作。
- **明确不做**：跨进程 / 分布式取消；把"取消"实现成"回滚已发生的副作用"（那是 v0.6 明确不承诺的东西）。
- **状态（2026-10-10，第一片已完成，未提交）**：契约 **§17.4 已冻结**（取消的两步与顺序、两道门、边界、取消与审批的次序、超时复用同一路径、"已启动进程的终止"明确列为**未冻结的第二片**）；§15.2 公开 `cancel_task` 并写明它**不接受排队、不撤销副作用、不释放执行锁**；§15.3 登记 `POST /api/sessions/{session_id}/cancel` 与 `status="cancelled"`；§8 把 `max_task_seconds` 接回（这次连实现一起）。实现落点：新增 `src/local_agent/cancellation.py`（叶子模块：线程安全、幂等的 `CancellationToken`）、`capabilities.is_visible(..., cancelled=False)`（**可见性门自己表达取消**）、`security/policy.py`（`SecurityPolicy.cancellation` + `classify_tool` 的**第一条判定**即取消，用的是它本来就有的"拒绝"出口）、`graph/state.py`（`RuntimeContext.cancellation`）、`graph/builder.py`（令牌同时进两道门）、`graph/nodes.py`（`_cancel_refusal` / `_is_cancelled`，两处 `is_visible` 传 `cancelled=`，拒绝话术区分"取消"与"档位不够"）、`application/service.py`（每会话一个令牌、新提交重置、`cancel_task`、`expire_overdue_tasks` 与看守线程）、`application/jobs.py`（`cancel()` 幂等 + `jobs_in_flight()` + **`_settle` 容忍已被取消的任务**）、`application/models.py` 与 `api/schemas.py` / `api/routes.py`（端点）。OpenAPI 与前端类型重新生成，`check:api` 通过。
- **T60 第一片的验收证据**：新增 `tests/test_cancellation.py` **5 条**（令牌幂等且不覆盖首次原因、无原因时的默认话术、**可见性门取消后一个工具都不可见**（含 chat 模式）、**安全门取消后四类工具全部被拒且不进审批**、没挂令牌时策略行为一字不变）；`tests/test_api.py` 新增 **3 条**（**取消后整轮一个工具都不执行**——用被取消的令牌跑一次真实工具决策，断言目标文件不存在且拒绝理由里带取消话术；取消命令标记任务并把理由写进 `detail`、再取消是空操作且不改写终态；超限任务被取消且 `detail` 写明 `max_task_seconds`，不限时长时什么都不做）。Python 全量 **1378 passed / 8 skipped**（5:04，含受控 API 面守卫的新端点）；前端 `typecheck` / `eslint` / 单测 **315 passed**；`ruff` / `mypy src`（116 文件）/ `check:api` 全绿。
- **T60 第一片当时未做（后续已在第二片补齐，见下）**：① ~~已启动进程的终止~~（第二片已完成）；② 取消的**页面**入口与恢复入口（T62），所以目前只有 API 消费方能取消；③ 取消对**审批恢复**路径的端到端验证只到"令牌复用"这一层（`resume` 复用同一个令牌），没有专门的往返用例；④ `docs/planning/FUTURE_WORK.md` 与 B-21 状态未回填。
- **状态（2026-10-10，第二片已完成，未提交）**：契约 §17.4 新增**"已启动进程的终止"**小节并冻结（覆盖范围只写实数、两道门与登记表的分工与线性化点、Windows `taskkill /F /T` 与 POSIX `killpg SIGTERM→SIGKILL`、如实记录、取消与超时同一条终止路径）；§6.2 的 shell 返回口径补上"进程树终止 + 如实记录"；"取消不做的事"改成与已落地能力一致的措辞。实现落点：新增 `src/local_agent/security/process_guard.py`（`ProcessRegistry` 登记表 + `RunningProcess` / `ProcessOutcome` + `_spawn_kwargs` / `_terminate_tree` / `_taskkill_tree` / `_killpg_tree` / `_reap` / `_drain`；`os.getpgid` 与 `os.killpg` 只在 POSIX 存在，改用动态取并给出可读失败原因，这样类型检查在 Windows 上也过）、`cancellation.py` 增加**取消清理挂点**（`add_cancel_hook`，已取消时立即执行）、`security/policy.py` 持有 `processes` 登记表、`tools/shell_tools.py` 从 `subprocess.run` 改走登记表（`ProcessCancelledError` → 如实报"命令未执行"）。
- **T60 第二片的验收证据**：新增 `tests/test_process_guard.py` **9 条**，其中**真跑两级进程树**（`python → python`，孙子进程每 50ms 往标记文件追加一个字节）：① 取消后标记文件**停止增长**且直接子进程退出、`termination` 里出现本平台手段（Windows `taskkill` / POSIX `killpg`）；② 超时同样终止整棵树、`termination` 以"未结束"原因开头、`exit_code` 是**真实非 0 值**（不伪造）；③ 正常命令 `exit_code==0` 且 `termination is None`；④ **启动前预检通过、登记前发生取消**的竞态被关闭（猴子补丁让 `Popen` 之后立刻取消，断言进程被当场终止、不留孤儿）；⑤ 已取消时 `spawn` 拒绝启动并带上用户原因；⑥ `add_cancel_hook` 在"先取消后注册"时立即执行；⑦ 策略的登记表与令牌绑定、不挂令牌时行为不变；⑧ 工具层取消后**不启动命令**且如实报"未执行"；⑨ 平台启动参数的差异（POSIX 必须 `start_new_session`）。Python 全量 **1387 passed / 8 skipped**（约 5 分钟）；`ruff check src tests`、`mypy src`（117 文件）通过。
- **T60 第二片未做（如实列出）**：① 取消的页面入口（T62）；② 登记表**只覆盖 `run_shell`**——`git_read` / `git_write` 仍是 `subprocess.run` + 自身 20 秒超时，契约里已如实声明不覆盖；③ 未使用 Job Object / cgroup（当前策略在目标平台上够用）；④ 审批恢复路径的取消仍只有"令牌复用"这一层验证。
  - **2026-10-12 补记（v0.9 T76）**：② 已**不成立**——`git` / `rg` / 目录选择器都收进登记表，超时与取消都按进程树终止；契约 §17.4 已改为修订后的实数，口径冻结在 `ARCHITECTURE.md` §20.1（本条是当时的记录，不改写）。

### T61｜"副作用已发生但状态未提交"的恢复策略

- **目标**：服务重启后能区分**可恢复 / 失败 / 需要人工核对**，且**绝不自动假定恰好执行一次**。
- **落点（复用既有口径，不另立一套）**
  - `local-agent-web/src/local_agent/persistence/request_ledger.py`（幂等台账，§15.2 的既有真源）。
  - `local-agent-web/src/local_agent/application/service.py`（`RECOVERY_REQUIRED` 现口径）、`src/local_agent/application/errors.py:150-153`。
  - `migrations.py` 里 v7 新建的任务索引（T57）。
  - `local-agent-web/src/local_agent/persistence/checkpoint.py`（只读核对终态）。
- **验收**
  1. 重启后：能证明终态 → 补齐结果；无法证明副作用 → 标"需人工核对"，**不自动重放**；明确失败 → 标失败。三类可区分，各有测试。
  2. 恢复路径不重提同一图输入、不重复执行工具（沿用 `graph/state.py:178-189` 的 `merge_turn_records`"终态不可改写"口径）。
  3. 中断任务的展示沿用 §15.2 的 `interrupted` / `unknown` 语义，不宣称"未执行"或"已撤销"；"可能仍有残留进程 / 可能已部分执行"如实进可见历史（codex `core/src/context/turn_aborted.rs:10-11` 与 dsh 的 `work may be orphaned`，见 §7.1 / §7.2）。
- **未知**：重启窗口内**进程树是否真的死掉**无法从台账证明（跨平台），这一点在契约里明写"不可证明"，由人工核对。
- **无现成参照（见 §7.3-5）**：「需要人工核对」这一档在 dsh 与 codex 的任务状态里**都不存在**，本项目要自己把"什么算需人工核对"写死；这是本阶段风险最高的一处。
- **状态（2026-10-10，已完成，未提交）**：契约 §17.5 冻结（四类判定与各自的可证依据、`needs_review` 的语义与"不可证明项"、不自动做的事、明确不做）；§17.1 的状态闭集由 6 值改为 7 值（`needs_review` 是终态，随 §17.1 的"先改契约"规则修订）；**§17.3 的事件全集不动**（`needs_review` 沿用 `job_completed`，由载荷的 `state` 区分、`detail` 带原因）。实现落点：新增 `src/local_agent/application/recovery.py`（`TASK_TERMINAL_STATUSES` 与判定纯函数 `classify_interrupted_job` + `TurnEvidence` / `JobVerdict`）、`persistence/request_ledger.py` 新增 `latest`（"该会话最后一条"）、`persistence/job_store.py`（闭集 + 终态子集 + 注释）、`application/jobs.py`（迁移表补 `queued→failed` 与 `running/waiting_user→needs_review`；新增 `recover` / `list_for_session`）、`persistence/migrations.py` 聊天库 **v8**（重建 `jobs` 表以放宽 CHECK）、`application/service.py`（`recover_interrupted_jobs` / `_adopt_interrupted_job` / `_turn_evidence`，并在 `_recover` 与 `_replay` 成功分支复用同一条判定）、`api/container.py`（**只在持单实例锁时**在装配期跑一次）。
- **T61 抓到的真实缺陷（本轮最有价值的产出）**：改完 Python 侧闭集后，写 `needs_review` 直接抛 `sqlite3.IntegrityError: CHECK constraint failed`——`jobs.state` 的 CHECK 是**写侧的最后一道守卫**，只改一半会让"索引允许的状态"与"实现能写出的状态"不一致。修法是按 SQLite 的标准四步做 v8 重建表迁移（建新表 → 逐列拷贝 → 删旧 → 改名，索引一并重建），并补一条"v7 旧库升 v8 后行**一列不差**、且新取值写得进去、索引还在"的用例。
- **顺带修掉的第二个真实缺陷**：崩溃后用户拿同一 `request_id` 重试时，v0.5/T58 只把**台账**收敛，索引里那条任务仍是非终态，于是这个会话的下一次新提交被"已有非终态任务"挡住，**只能等下一次重启**。修法：在 `_recover` 与 `_replay` 的成功分支调用同一个 `_adopt_interrupted_job`（证据用台账那条记录的 `run_id`），判定仍走同一个纯函数——不是第二条规则。这样"可恢复 / 可证明"不依赖重启。
- **T61 的验收证据**：新增 `tests/test_recovery.py` **13 条**穷举判定（`queued` 可证明未开始且不被上一轮的终态冒充；读不到 Checkpoint 不猜；活挂起→可恢复；`waiting_user` 已对就不写库；活挂起但 run 显式矛盾→需核对；终态 + 匹配 run→可证明；"`NULL` 不是通配符"的四种证不了；终态任务传入即报错；每条判定的目标状态都在迁移表内；状态→事件"不重不漏"）；`tests/test_api.py` 新增 **5 条**端到端（真索引 + 真 Checkpoint + 真台账，靠"关掉运行时再开一套"模拟重启）：① `queued` → `failed` 且 detail 说明"还没开始执行"；② `running` 且证明不了 → `needs_review`、detail 含"可能仍有残留进程"、且它是终态（会话不被永久卡住）；③ **崩溃窗口复现**（让 `_settle` 什么都不做：图跑完、Checkpoint 终态、台账记了轮次，只有索引没结算）→ 重启后按 `completed` 补齐，且消息条数不变（补的是索引，不是执行）；④ 停在审批上的任务重启后**一个字节都没改**，并当场走通审批恢复；⑤ 同一 id 重试**不必重启**即收口，且会话不再被旧任务挡住。另同步 4 处既有契约断言（迁移表、状态闭集、终态子集、`CHAT_SCHEMA_VERSION`）与受控 OpenAPI（重新生成 `docs/api/openapi.json` 与前端 `schema.gen.ts`，`check:api` 通过）。
- **T61 未做（如实列出）**：① 页面任务中心与恢复入口（T62）——因此 `needs_review` 目前只有索引/API 消费方能读到，**没有新的读取端点**；② 不做跨进程清查残留进程（契约明写"不可证明"）；③ 不做自动重放 / 自动重试 / 自动回滚；④ `docs/planning/FUTURE_WORK.md` 与 B 条目状态未回填。

### T62｜页面任务中心与恢复入口

- **目标**：用户能看到多任务状态（排队/运行/等待用户/完成/失败/取消），并走到恢复入口。
- **落点**：`local-agent-web/src/local_agent/application/models.py`（DTO，沿用 `*View` / `*Ref` 风格，如 `CommandResultView`）、`local-agent-web/src/local_agent/api/routes.py:192-194`（`build_router`，新增任务端点）、`frontend/`（新增任务中心容器，**不重写 v0.4 页面壳**）、Playwright 截图基线。
- **验收**
  1. 页面**不出现原始工具名、内部 id、DTO 名或指纹**（沿用 §15.8 与 v0.4 T39 的字段门槛）；缺依赖/不可用时给"缺什么、怎么装"，不是只灰掉。
  2. 任务状态变化有**页面演示**（截图基线按测试粒度重拍 + 人工抽查）。
  3. 恢复入口不给"自动重跑"以外的危险语义误导——文案与 T61 的三类判定一致。
  4. 运行中任务必须显示**已运行时长与当前在等什么**（这是"默认不限总时长"的配套条件，§5.1-4）。
- **明确不做**：通用任务调度 UI、任务优先级编辑、跨工作区任务编排（无消费者）。
- **状态（2026-10-10，已完成，未提交）**：契约 §17.6 冻结（只读端点与数据来源、字段门槛、"在等什么"是**状态的函数**、已运行时长的口径、恢复入口三类文案、入口计数、明确不做）；§15.3 登记 `GET /api/tasks` 并写明它**不读任何会话的 Checkpoint**。后端落点：`application/models.py`（`TaskView` / `TaskCenterView`，`state` 借用 `persistence.job_store.JobState`——闭集只有一处来源）、`persistence/job_store.py`（新增 `JobState` 类型别名、`Job.state` 改精确类型、新增 `list_for_sessions`）、`application/jobs.py`（`list_for_sessions` 透传）、`application/service.py`（`list_tasks` / `_task_view`）、`api/routes.py`（端点）。前端落点：`src/api/client.ts`（`getTasks`）、`src/api/queries.ts`（`queryKeys.tasks` + `useTasks`，5 秒轮询）、`src/api/viewModels.ts`（`toTasksView`：把 `counts` 补成**完整闭集**，缺键会让筛选栏显示成 `undefined`）、`src/fixtures/types.ts`（`TaskState` / `TaskRowView` / `TasksView`）、新增 `src/features/tasks/`（`adapters.ts` 闭集文案与派生、`TasksPanel.tsx` + `.module.css`、`TasksSection.tsx`）、`src/components/Sidebar.tsx` + `.module.css`（入口与"等你处理"角标，**可选 prop**：原型宿主不传 ⇒ 既有截图基线一字不变）、`src/app/LiveApp.tsx`（四个入口互斥 + 打开会话 + 角标）、`src/app/App.tsx`（原型宿主渲染面板）、`src/fixtures/scenarios.ts`（`tasks` 场景：**七个状态各一行**）、`src/features/conversation/adapters.ts`（抽出 `elapsedLabel`，与 `RunActivity` 共用同一处格式，不再各写一份）。
- **T62 抓到的两处真实问题**：① **死锁**：`list_tasks` 一开始把 `JobStore.list_for_sessions` 包在 `_sqlite_access` 里，而 JobStore 的每个公开方法已经在**同一把不可重入的锁**内执行——第一次跑用例直接卡死（T58 的同一个坑，已在代码里留下注释说明为什么不能"顺手加一层"）；② **e2e 的文本匹配语义**：`getByText` 的字符串匹配默认是**子串**，而 `exact: true` 比的是整段 `textContent`——"正在执行"同时出现在"需要人工核对"那行的原因里，第 4 行整段文本含"在等什么"，都会撞出 strict mode 的"2 个元素"或"找不到"。修法两条：把分隔符 `·` 从文案 span 里**拆出来**（让每个 span 的文本正好等于那句话），以及该用计数的地方用计数（"取消不撤销"按设计在运行时与已取消两行都出现，断言 `toHaveCount(2)`）。
- **T62 的验收证据**：后端 `tests/test_api.py` 新增 **6 条**（列表的键与顺序、**字段门槛**（`run_id`/`thread_id`/`request_id`/指纹都不出现）、`counts` 不受 `limit` 截断、`include_finished` 与"已归档会话不进列表"、**只读**（连读两次索引一字不改）、空工作区不是错误）；前端新增 **19 条**（11 条适配层纯函数：闭集不多不少、"在等什么"逐条对表、时长如实且不出现负数、筛选只筛列表而计数取全量；8 条面板组件：状态/时长/"在等什么"、**页面上不出现内部标识**、展示时钟每秒重算、恢复入口文案、点"打开会话"、两句不同的空态）；视觉基线：新增 `tasks` 场景与 3 个视口的 PNG（**钉住时钟**——面板要显示"已运行 X 分 Y 秒"，不钉时钟的基线每次都不一样），整份 `prototype.spec.ts` **174 passed / 3 skipped**，**既有基线未改一字**（新入口是可选 prop，原型不传）。后端全量 **1420 passed / 8 skipped**（5:43）；`ruff` / `mypy src`（119 文件）/ `export_openapi.py --check` / 前端 `typecheck` / `lint` / `vitest`（**339 passed**：T62 新增 19 条 + 任务事件驱动 5 条）/ `check:api` 全绿；prototype 套件 **174 passed / 3 skipped**（既有基线一字未改）。
- **T62 的第二轮（同批，未提交）**：① **任务中心改成事件驱动**——`useSessionStream` 消费 §17.3 的任务事件（`job_queued` / `job_progress` / `job_waiting` / `job_completed` / `task_cancelled`）后**失效任务查询**、重读索引，页面自己**不推导**状态；`useTasks` 的轮询从 5 秒降到 **15 秒**，只补两个缝：**别的会话**的后台任务（SSE 按会话订阅，本页收不到它的事件）与**断线期间漏掉的事件**（事件流没有持久游标）。§17.6 补记这条机制（"轮询不是主要机制"）。② **补上 `local-agent-web/README.md` 里"真实取消"那半句**——原文仍写着"真实取消能力仍未提供"，属 T60 的文档同步缺口（§2 的"同步文档"要求与实现同批改），现改为取消 + 进程树终止 + 重启三类收口 + 任务中心四句指向 §17.4 / §17.5 / §17.6。③ 我一度把"取消不撤销已经发生的副作用"从**正在跑**的那行删掉（觉得还没取消、多余），复查 §17.6 时发现契约**明确要求**这一行必须写（那正是用户要考虑取消的地方），**已按契约改回**——契约优先于我的排版偏好。
- **T62 未做（如实列出）**：① **没有"重跑旧输入"的按钮**——§17.6 明确不提供：重跑要么变成"替用户重放"，要么要求把用户原文存进任务索引（那是第二套业务真源，§17.1 禁止）。恢复入口只负责**打开会话** + 给出与 §17.5 三类判定一致的下一步；这一条比验收 3 的字面更保守，**是刻意的**。② 不做任务详情页（执行详情在会话里）、不做优先级 / 并发调参 UI、不做跨工作区编排。③ 不做**全局任务事件订阅**（SSE 仍是按会话订阅），跨会话即时性未提供，靠 15 秒兜底。④ `docs/planning/FUTURE_WORK.md` 与 B 条目状态未回填。
- **T62 第三轮（2026-10-10，未提交）：按 §17.6 逐条核对既有产物 + 补两条没覆盖的口径**。先说清口径：**T62 的实现与验收在第二轮就已完整**（见上两条），本轮只做**核对**与**补缺**，没有改任何实现、任何契约。
  - **核对**（逐条对 §17.6）：`TaskView` / `TaskCenterView`（`application/models.py`，字段门槛与"没有'在等什么'字段"都在注释里写明）、`GET /tasks`（`api/routes.py`：`workspace_id` / `include_finished` 默认真 / `limit` 默认 50 上限 200）、`service.list_tasks` 与 `_task_view`（只读两个索引；`counts` 覆盖全量、`pending_count` 是等审批 + 需核对；**不读 Checkpoint**）、前端 `features/tasks/`（`adapters.ts` 的状态名与"在等什么"**逐条对上** §17.6 那张表、时长如实且缺时间戳写"未记录"、`pendingBadge` 取服务端 `pending_count` 而不自己加总；`TasksPanel.tsx` 的恢复入口文案与三类判定一致）——**一致，无需改动**。
  - **补 2 条**（基础那 6 条没覆盖的）：① **不读任何会话的 Checkpoint**：把 `service._read` 封死后 `GET /api/tasks` 照常返回——§17.6 明写"数据只来自任务索引 + 会话索引"，这件事要有证据，不能只靠注释声明；② **`needs_review` 与 `waiting_user` 一样进角标**：T61 的三类判定在任务中心接通（plant 一个 `running` 再收口成 `needs_review`，断言它出现在列表、`detail` 原样透出、`pending_count >= 2`）。两条与前六条**同文件并列但注明了分工**，不重复。
  - **我的一次误判（如实记）**：上一轮我说"T62 缺 Python 侧用例"——那是**只按文件名找 `test*task*`** 得出的错误结论，6 条用例其实在 `test_api.py` 末尾。教训：**"有没有测试"要按符号 / 端点名搜，不能只看文件名**（与 §7 的"证据要能复现"同一口径）。
  - **当前门槛**：后端全量 **1451 passed / 8 skipped**（6:22）；`ruff` / `mypy src`（122 文件）/ `export_openapi.py --check` 全绿；前端 `features/tasks` 单测本轮**亲自复跑 19 passed**（2 个文件），与第二轮记的数字一致。

### T63｜超限检索结果可恢复（`save_full` + locator）

- **目标**：`grep` / `glob` 超过上限时不再"**截断即丢失**"——模型能拿到**完整**结果；写盘失败**绝不**把检索变成错误。
  这是对齐 dsh 的 spill + locator 语义，但**不引入**它的外部 spill 服务（见下"为什么不照搬"）。
- **触发它的事实（2026-10-10 实测）**：与 dsh 逐条对照时量的——本机工作区有 **12828** 个候选文件（4.4 GB），
  全量遍历 5.8 秒；`MAX_SCAN_FILES=2000` 会在 `local-agent-web/eval/`（**gitignored** 的产物目录，8138 个文件）里耗尽，
  `glob="*.py"` 下前 2000 个候选**全部**来自 `eval/`，`src/` / `tests/` 一个文件都扫不到。超限**必然发生**，
  而当时的语义是"只说明不完整、截掉的取不回来"。（**这是触发本任务时的数字**；遍历口径已在第二片修正，
  修正后的本机数字见下"第二片的验收证据"。）
- **契约（先改）**：§6.7 新增"**超限结果可恢复**"整段（`grep` 加 `save_full: bool = False`，§6.8 `glob` 同构并
  逐字引用本节）；四条口径写死：① locator = 落盘文件的**工作区相对路径**，**不新增读取通道**（既有 `read_file`
  与 `grep path=<locator>` 都能读它）；② 落盘位置 `.local-agent/results/`、确定性文件名（同一组参数复用同一个文件）、
  自忽略 `.gitignore`、保留最新 50 个、单文件 8 MB / 20000 行；③ **落盘失败只如实说明，不抛错、不谎称已保存**；
  ④ **默认 `false` 时行为逐字节不变、不写任何文件**。
- **与 dsh 的差别写在契约里（不是遗漏）**：dsh 在超限时**自动**保存完整结果，因为它的引擎是 ripgrep、扫描成本可忽略；
  我们的引擎**仍是 `os.walk` + Python `re` 逐行**（**没有**打包 ripgrep），"完整收集"要跑到文件/时间上限——
  所以**不做自动落盘**，改成模型显式要（`save_full=true`），并把"怎么拿完整"写进截断说明与工具描述。
  **成本口径要说准**：自动落盘的代价 = "**触顶时继续扫到 `MAX_SCAN_FILES` / `timeout` 上限**"，上限本身就是上界；
  §6.7 里原先引用的"整库 12828 个候选 / 5.8 秒"是**修遍历口径之前**的数字，第二片之后本机是
  **587 个候选 / 22.1 MB / 遍历 0.21 秒 + 逐行扫描 0.05 秒（`*.py`）**，一次整库检索约 **0.26 秒**——
  已同步修订（**不打包 ripgrep** 的裁定与量化重开条件见 `docs/planning/FUTURE_WORK.md` 的 B-34 复测段）。
  要改这个默认值须先修订 §6.7。
- **明确不做**：不引入外部 spill 后端 / 远程存储 / 会话级 spill 服务；不加"按 locator 分页取回"的新参数；
  不做落盘结果的生命周期管理（只有"保留最新 50 个"这一条）。
- **状态（2026-10-10，已完成，未提交）**：契约 §6.7 / §6.8 冻结（见上）；§2 的契约改动点表登记了这次
  **模型可见的 schema 变更**（两个工具各加一个布尔参数）。实现落点：新增
  `src/local_agent/tools/result_spill.py`（`SPILL_DIR` / 自忽略 / `spill_key` 确定性命名 / `save_result` 失败返回
  `None` / `prune` / `spill_note` / `SPILL_FAILED_NOTE`）、`tools/search_tools.py`（`grep_content` 与 `glob_files`
  把**收集**与**预览**解耦：`save_full` 时收集不受命中上限约束、预览仍受 `max_matches` / `max_files`；
  两个工具的 docstring 都写清"怎么拿完整结果"）。**顺带的好处**：`save_full=true` 与 `output_mode="count"`
  组合时，`count` 汇总是**完整**的（默认仍是受上限截断的那一份）。
- **T63 的验收证据**：新增 `tests/test_search_spill.py` **14 条**：① 落盘是**全部**命中而预览仍受上限；
  ② **locator 能被既有 `read_file` 读回**（`line_count == 20`、内容含命中行）；③ locator 还能被
  `grep path=<locator>` 再查；④ `count` + `save_full` 给出完整汇总；⑤ **写盘失败**（补丁让 `save_result` 返回
  `None`）时结果照常返回、`note` 如实说"未能保存"、且**没有任何文件被写出来**；⑥ `save_result` 在建不出目录时
  返回 `None` 而**不抛异常**；⑦ **默认一字不变**（省略与显式 `false` 的 `model_dump()` 相同，且不建目录）；
  ⑧ 收集触到扫描上限时 `note` 同时说"已达文件数上限"与"**落盘结果本身也未完整**"；⑨ 目录自带 `.gitignore`（内容 `*`）；
  ⑩ 落盘文件**不进**后续检索（隐藏目录）；⑪ 保留策略只留最新 N 个（用例把上限调成 2 验证）；  ⑫ 同一组参数 locator
  稳定、换参数就变；⑬ `glob` 同构；⑭ locator 是工作区内的相对路径（不越界、不含 `..`）。全量 **1434 passed / 8 skipped**（5:35）；
  `ruff` / `mypy src`（120 文件）/ `export_openapi.py --check` / 前端 `check:api` 全绿。
- **T63 的既存缺陷（同一批量到，**未修**，见下条）**：跳过清单不认识 `.gitignore`，于是 `eval/` 这类产物目录吃掉整个
  扫描额度。它与本任务互补——修好它，上限就不那么容易触；本任务保证触到上限时结果仍可取回。**要动它得先定口径**
  （静态补名单 vs 让遍历尊重 `.gitignore`），因此单列不夹带。
- **T63 第二片（同批，未提交）：三个决策按"成熟系统怎么做"拍板**（用户授权："按成熟系统来拍板"）：
  1. **落盘保留策略 → 按容量收口**（照 ccache 的 `max_size` / npm cacache 的 LRU-GC）：由"最新 50 个"改为
     **双封顶**——最新 **20** 个文件 + 目录总计 **50 MB**，谁先到按谁删、从 mtime 最旧删起。理由：只按个数封顶不够
     （20 × 8 MB = 160 MB）；而 dsh 的"永不清理"是它自己 README 里承认的未做项，不照抄。
  2. **默认是否自动落盘 → 保持显式**（不改 `save_full` 默认值）。理由（**不是省钱，而是语义**）：dsh 与 Claude Code
    能自动落盘，是因为它们的**生产者**（ripgrep / 命令本身）已经把全部结果产出来了，截断只是**显示**问题；
    我们的生产者就是扫描本身，自动落盘 = 每条触顶检索都**继续扫到 `MAX_SCAN_FILES` / `timeout` 上限**（上限即上界，
    与工作区多大无关）。取回路径已经写进 `note` 与工具描述，模型需要完整结果时一条参数就能拿到。
  3. **遍历尊重 `.gitignore` → 做**（与 ripgrep / ag / fd / Claude Code 的 Grep 默认一致），**而不是**往静态清单里
     补目录名：新增 `src/local_agent/rag/ignore.py`（`IgnoreRules`，目录栈 + 三态 `check_file`，深层优先、`!` 可反选、
     被忽略的目录整个剪掉——与 git 的"父目录被排除后无法再包含其中的文件"一致），语义交给 **`pathspec`**
     （black / pre-commit 用的同一套实现，**不自己实现 gitignore 语法**），`pyproject.toml` 显式声明它（它本来就在
     依赖图里，`uv lock --offline` 已刷新）。`grep` / `glob` / 按需 RAG 三处遍历共用这一套规则（§6.7 要求两处共用一套）。
     边界写进契约：**只影响枚举**——显式点名一个**文件**不受影响；点名一个**目录**时目录内部仍按工作区根的规则过滤
     （"规则只由路径本身决定"）；**没有 `.gitignore` 的工作区行为与从前逐字相同**。顺带修掉一处既有的错误引用
     （§6.7 原文写"与 §4.3 的按需代码 RAG 共用清单"，而 §4.3 是"关键嵌套结构"）。
- **T63 第二片的验收证据**：新增 `tests/test_search_ignore.py` **8 条**：被忽略目录整个剪掉（`grep` 与 `glob` 都验）；
  **实测盲区复现**（300 个产物文件 + 1 个 `src` 文件、额度只够几十个 → 没有规则时 `src` 会被挤掉，现在命中 `src/real.py`
  且 `truncated is False`）；嵌套规则的 `!` 反选；父目录被排除后无法再包含；**显式点名的文件照给**；**没有 `.gitignore`
  时结果与旧行为一字不差**；按需 RAG 的遍历用同一套规则；`.gitignore` 不是普通文件时不影响检索。`tests/test_search_spill.py`
  补 1 条"按总字节封顶且最新一个一定在"。**本机实测的前后对比（同一工作区、同一遍历规则）**：候选文件
  **12828 → 587**（遍历 5.8 秒 → **0.2 秒**），`glob="*.py"` 下 **3035 → 189**；此前"前 2000 个候选"是
  `eval/` 占 1854（`src/` 一个都没有），现在前 2000 个里 `local-agent-web/src` 有 121 个——**与 `mypy src`
  报的 121 个源文件逐个对上**。两个上限在这个工作区**都不再被触到**。回归：`tests/test_tools.py` 等检索相关
  **108 passed / 1 skipped**；全量 **1443 passed / 8 skipped**（5:47）。`ruff` / `mypy src`（121 文件）/
  `export_openapi.py --check` / `uv lock --check --offline` 全绿。
- **同批修正的三处"过时依据"（文档诚实性）**：① `ARCHITECTURE.md` §6.7 用"整库 12828 个候选 / 5.8 秒"作为
  "不做自动落盘"的代价依据——那是**修遍历之前**的数字，已换成复测值（587 候选 / 22.1 MB / 遍历 0.21 秒 +
  逐行扫描 0.05 秒），并把成本口径改为"**触顶时继续扫到 `MAX_SCAN_FILES` / `timeout` 上限**，上限即上界"；
  ② 本节 T63 的"与 dsh 的差别"同步；③ `docs/planning/FUTURE_WORK.md` B-34 追加 **2026-10-10 复测**：
  维持"**不打包 ripgrep**"的裁定（差距从 0.35 秒缩到约 0.2 秒、瓶颈从遍历转到逐行匹配）并写下**量化重开条件**
  （未被忽略的候选 ≳ `MAX_SCAN_FILES`、或单次检索 > 1 秒、或需要真引擎才好做的语义）。
- **同批登记、随即在第三片做掉的一条**：参照实现 dsh 的 `sampleOverCapGlobResults`——**超限时怎么选**
  （在顶层条目之间采样 vs 保前部）——此前本项目**仍没有**（超限时是"遍历顺序的前 N 个"）。第三片按
  **同级轮转**补齐，见下。
- **T63 第三片（同批，未提交）：超限时怎么选 = 同级轮转**。为什么单列一片：它与前两片治的是**同一个病的两面**
  ——第二片修的是"**扫不到**"（额度被 gitignored 的产物目录吃光），这一片修的是"**只能看到一条链**"
  （额度被名字最靠前的子树吃光）。两者都不改"上限是多少"，改的是"上限落在谁头上"。
  - **契约**：§6.7 新增并冻结"**超限时怎么选：同级轮转**"（dsh 的 `sampleOverCapGlobResults=true` 语义）
    + 四条边界：① 未触顶时结果**集合**与从前逐字相同；② 顺序确定可复现；③ **写死、不开配置开关**
    （dsh 做成必填配置，我们写进契约——多一个没人会翻的开关就是空转配置，同 §16.5 的口径）；
    ④ `note` 必须说明"**这是采样**"，不能只说"不完整"。
  - **实现**：新增 `src/local_agent/rag/walk.py`（`iter_candidate_files`：`os.scandir` 逐层、同级按名
    **整体排序**（文件与子目录混排）、`deque` 轮转、递归；跳过口径仍走 `rag/ignore.py`），
    `SKIPPED_DIRS` 从 `rag/search.py` **搬到** `rag/ignore.py`（口径与遍历同源），`tools/search_tools.py`
    与 `rag/search.py` 的遍历都改为调用它（**三处一份实现**，此前是两处各写一遍）。三条上限说明补上
    "（同级轮转采样：…）"。
  - **要留意的可见变化**：遍历顺序由"深度优先、文件先于子目录"变成"**同级按名轮转**"（未触顶时**集合**不变，
    顺序会变）——契约把顺序写成确定性的一部分，用例把它钉死，不再有"看起来是乱的"的问题。
- **T63 第三片的验收证据**：新增 `tests/test_search_fairness.py` **6 条**：① **命中上限**触顶时，目标文件在
  名字靠后的顶层条目（`zzz/target.py`）里**仍被看到**（老行为前 3 条命中全落在 `aaa/`）；② **文件数上限**
  （`MAX_SCAN_FILES=2`）不再被第一个子树吃光；③ 未触顶时**集合**与从前相同且两次调用一致；④ 顺序逐项
  对照"同级按名轮转"（`["d1/a.py","d2/c.py","f1.py","d1/b.py"]`）；⑤ 很深的子树不能饿死同级兄弟；
  ⑥ `glob` 与按需 RAG 在同一棵树上给出**同一批文件**（证明是一份实现）。另**同步 2 处既有用例的补丁点**：
  `tests/test_rag.py` 的"没有词项不碰文件系统""遍历失败要如实上报"原先打的是 `rag.search.os.walk`，
  现在改打共用入口 `rag.search.iter_candidate_files`（遍历实现搬走后那个属性不存在了——全量跑当场报
  `AttributeError` 抓到的）。**本机实测的形状**：588 个候选里
  `local-agent-web` 占 **562**、`docs` 只有 **20**——老顺序先走完根文件与 `docs/` 才进 `local-agent-web/`，
  默认 100 条命中很容易被 `docs/`、`frontend/` 吃光；新顺序**前 100 个候选覆盖 23 个顶层条目**
  （`src` 13、`tests` 13、`frontend` 13、`scripts` 13、`docs/lessons` 11…）。回归：检索相关
  **114 passed / 1 skipped**；`ruff` / `mypy src`（122 文件）通过。
- **T63 未做（如实列出）**：① ~~遍历/跳过清单不尊重 `.gitignore`~~（第二片已完成）；
  ② `output_mode=count` 的**默认**汇总仍受命中上限截断（`save_full=true` 才完整）；③ 单行展示窗口仍是 300 字符
  （dsh 是 2000 字节），未调整；④ 不引入打包 ripgrep；⑤ `docs/planning/FUTURE_WORK.md` 与 B 条目未回填。

## 4. 全局硬验收（每个任务都要过）

以下五条沿用 `V0_5_DEVELOPMENT_PLAN.md` §4，在 v0.6 **同样适用**：

1. **不可见的工具**：模型不能调用，**也不能绕过注册表直接执行**（注册表是"给谁看"，安全门仍是"能不能做"，两者都要挡住）。
2. **关闭新增能力后**，原基础 Agent 行为不变（回归全绿，不得以"新能力带来新路径"为由放宽）。
3. **稳定前缀**：同一配置下，系统提示与工具 Schema **字节一致**；变化必须能用事件指纹解释。
4. **不引入第二套实现**：截断、检索、记忆、审批各自只有一条路径。
5. **契约先行**：每个任务开工前先改 `ARCHITECTURE.md`，文档与实现**同批出现**，不允许"先写代码后补文档"。

v0.6 追加四条（本阶段特有）：

6. **不引入第二套任务真源**：任务状态由既有 Checkpoint + 会话索引 + 迁移机制承载；事件仍是**单向投影**，不得成为编排状态（§15.4）。
7. **取消不新开检查路径**：取消的生效点必须落在既有 `is_visible` 与 `policy.classify_tool` 两道门，不在工具管线里另加取消分支。
8. **不得假定恰好一次**：任何恢复路径都不得自动重放已发生的副作用（沿用 §15.2 的 `RECOVERY_REQUIRED` 口径）。
9. **`jobs.background=false` 时行为不变**：提交路径、错误码与事件序列与 v0.5 逐字节一致；`=true` 时，除"POST 返回时任务是否已终态"外，**图节点调用序列与事件序列逐项一致**。

## 5. 风险与未知项

### 5.1 已定口径（草案建议，随本计划一并请批）

> 下面四条是经"架构 / 可读性 / 可维护性"评估后的**具体取法**，不再是开放选项。任一条若在评审时被否决，受影响章节已在括号里标出。

1. **`jobs` 段与开关（影响 §2、§3 T58、§4-9）**
   - 新增 `jobs` 配置段，但**不设"关闭即不可用"的能力式开关**；字段是 `background: bool`（**默认 `false`**）、`max_concurrent_jobs`（**默认 `1`**）、`max_task_seconds`（**默认 `None`** = 不限）。
   - **理由（架构）**：`enabled` 在 v0.5 的语义是"能力是否可见/可用"（`web_access` / `git` / `skills` 都会让工具从模型视野消失）。后台化改的是**执行模型**，不是新增一项能力；沿用 `enabled` 会让配置读者以为"关了就不能用"。取 `background`，读作"是否后台执行"。
   - **理由（可维护性，最关键）**：**只有一条执行路径**。`background=false` = 在提交请求内跑到终态；`true` = 入队由 worker 跑。两者共用同一个状态机与同一个图执行入口，不产生第二份 `_drive`。若走"两套代码 + 开关"，会同时违反硬验收第 4 条并使第 9 条无法机械验证。
   - **默认 `false` 的理由**：不是防攻击面，而是**不让任务在没人在看时继续花钱**（与项目成本纪律一致）；代价是路线图"关闭浏览器后任务继续"这条验收需在开关打开后验证（与 v0.5 T53/T55 的做法一致）。
   - **可验收观测量**：同一次固定提交在 `background` 两种取值下，**图节点调用序列与事件序列逐项一致**，唯一差异是"POST 返回时任务是否已进入终态"。
2. **会话 `kind` 闭集（影响 §2、§3 T57）**
   - `interactive`（默认）/ `scheduled` / `heartbeat`，三值。`sessions` 已有 `mode`（`chat`/`agent`，执行模式），因此 §10.6 必须用一句话把两者切开：`mode` = 这一轮怎么跑，`kind` = 这个会话从哪来。名字沿用 B-03 的 `kind`，便于审计追溯。
   - **不留 `subagent` 取值**：按 §16.5 已裁定的"不造空转的配置项"，v0.7 开工时追加一个闭集取值本来就是要改契约的事，提前占位只会让"谁在用这个值"变成悬案。
3. **`SESSION_BUSY` 与"排队"（影响 §2、§3 T58）**
   - **不改 §15.2**：每会话单飞与 `SESSION_BUSY` 原样保留；路线图要求的"排队"只作为 **job 状态机的一个状态**（多会话竞争 worker 时真实出现），不引入"同一会话多请求队列"。
   - **理由**：改为同会话排队会顺带引入队列深度 / 排序 / 公平性 / "等待审批时又提交"一串新契约面，而路线图那句是对**任务状态**的要求，不是对会话提交队列的要求。codex 的"Session 内同时只有一个活动 Turn"也印证：后台执行**不需要**改这条（§7.2）。
   - **可验收观测量**：同一会话并发第二个提交**仍返回 `SESSION_BUSY`，且 v0.5 该回归用例一字不改**；同时用"两个会话抢一个 worker"的用例证明 `queued → running` 可达。
4. **超时与资源上限（影响 §2、§3 T60、§3 T62）**
   - 只新增两个量（`max_concurrent_jobs` 默认 1、`max_task_seconds` 默认不限），其余**全部复用既有项**：`security.shell.default_timeout_seconds=60`（`config.py:415,426`）、`agent.max_tool_loops=60`、`agent.max_retries_per_step=2`（`:496-497`）、`security.shell.max_output_chars=30_000`（`:427`）。
   - **不给任务总时长设默认硬上限**（设计取舍）：v0.6 已交付取消，此时再加默认硬超时会**误杀正常长任务**，而"长任务"正是本阶段目标。替代方案是**可见性 + 手动取消**——页面必须显示已运行时长与当前在等什么（T62 验收 4）。外部对照：codex 有命令级 10s / 后台终端 300s，dsh 明确**没有**任务总时长上限（§7.1 / §7.2），两者口径不同，我们只借"每个执行面都要有显式上限"这个形状，不照搬数值。
   - **可验收观测量**：`max_task_seconds=None` 时任务能到正常终态；显式设置后超限进入明确终态并记录原因；运行中任务的耗时在页面可读。
   - **实施时的收敛（2026-10-10）**：T58 只落 `background` 与 `max_concurrent_jobs`；`max_task_seconds` **连实现一起移到 T60**——T58 没有"执行中中止"的能力，先放一个不生效的键就是空转项（§16.5 的既有裁定）。T62 的"显示已运行时长"保留，但它的理由不再是"配合默认超时"，而是"任务可能跑很久，用户要知道它还在动"。

### 5.2 风险

- **契约面收敛是风险控制点**：我们**选择不动** §15.2 的"第二个请求 `SESSION_BUSY`、不排队"（`ARCHITECTURE.md:1672`），因为改它会连带引入队列语义（§5.1-3）。若开工后不得不改，先改 §15.2 与 §17，不加实现。
- **持久化是本阶段最大的新增面**：dsh 的任务状态在内存 Map、codex 的状态随线程生命周期（§7.3-3），**两家都没有**"重启后区分三类任务"的实现可抄；而路线图把它列为验收。因此 T57 / T61 是本阶段风险最高、也最需要独立测试的两块。
- **前台阻塞改后台后的并发与幂等**：`request_ledger` 是命令执行的幂等真源，v0.6 不得复制它、也不得把它升格为任务真源（否则就是第二套存储）。
- **SSE 仍是进程内投影（无持久游标）**：因此"重新打开可查看状态"**必须**靠 Checkpoint + 索引重建，不能依赖事件重放；这是 T59 与 T61 的共同约束。
- **进程树清理跨平台**：Windows 与 POSIX 的终止语义不同，且 codex 的 core 路径在 Windows 上**只杀直接子进程**（`utils/pty/src/process_group.rs:82-86` 非 Unix 为 no-op），dsh 才用 `taskkill /T`（§7.1 / §7.2）。**本机具体行为未核实**，T60 开工第一步先做本机实测，再定契约措辞。
- **真实模型验证要单独授权**：验证"关闭浏览器后任务继续""取消后不再调工具"的端到端行为需要真实调用；按成本纪律**先给批次规模估算再请授权**，能用假 model / 假 runner 证明的绝不花真钱。
- **本机环境事实（T57 开工时复核，不预先假设）**：当前迁移版本是**聊天库 6 / 长期库 3 / 设置库 3**（`migrations.py:24,27,30`）；T52 的 "v3 迁移" 指**长期库**的 `origin` 列（`:312-327`），不是聊天库。新增任务索引应是**聊天库 v7**。

### 5.3 B 编号条目复核（依据 `FUTURE_WORK.md` §0「条目里的'阶段建议'在立计划时必须重新判定——触发条件可能已经不成立」）

| B 条目 | 原阶段建议 | 复核结论（2026-10-10） |
| --- | --- | --- |
| **B-03** session-kind gating | 不单独立项，作为 v0.6 与 v0.7 的**任务前置条件**记入那两阶段的计划 | **采纳**，作为 §T57 的前置条件；落法复用 `ORIGIN_SYSTEM`，不新造标记。 |
| **B-21** 没有"停下来"的能力 | 完整取消归 **v0.6** | **采纳**，落 §T60。v0.4 已落地的最小版（`application/errors.py:151,351` 的"没有取消接口"话术 + `local-agent-web/README.md:84`）随契约一并更新。 |
| **B-07** MMR 召回多样性 | 阶段建议 **v0.6** | **不采纳**。它是**证据触发**条目，触发条件（同一轮召回里出现可重复的高度相近记忆）**尚未成立**；仅因"v0.6 会让记忆变多"就做属于猜测。若本阶段真实运行出现该证据，再单独裁定。 |
| **B-12** dreaming 式后台晋升 | **已裁定不做**；重开条件 = v0.6 引入 Job Runtime 后出现"写入质量明显受对话延迟限制"的证据 | **保持不做**。本阶段**不采集**该证据、不实现；重开条件成立前不得顺手做。 |

**未纳入本轮**（阶段建议未指向 v0.6，或与六项主要工作无直接交叠）：B-02、B-04、B-06、B-08、B-18、B-22、B-26、B-27、B-34。开工前若要用其中任何一条，需按同一口径重新判定触发条件。

## 6. 停止条件（命中任一，先改契约，不加实现）

1. 后台任务必须引入多进程 / 多 worker 才能满足"不占住 HTTP 请求"；
2. 取消无法挂在既有两道门（`is_visible` 与 `policy.classify_tool`）上，必须另开一条检查路径；
3. 任务状态无法由既有 Checkpoint + 会话索引重建，必须另立一套真源；
4. "副作用已发生但状态未提交"只能靠自动重放才能保证可用；
5. 后台 / 心跳会话的记忆写入无法用既有 `origin` 闭集（三值）表达，需要新增第四档；
6. 为了任务中心，页面必须复制 Graph 真源或触发副作用。

## 7. 外部对照（dsh / codex，2026-10-10 逐行核验）

> 用途：给本阶段的取舍提供**外部实证**，不表示要移植其框架。证据取自本机检出 `D:\ideaTest2\deepseek-harness`（dsh）与 `E:\pylianxi\codex`（Codex，`codex-rs/`）；行号为核验时读到的位置。**两家都不持久化任务状态**，而本阶段必须持久（§7.3-3）。

### 7.1 dsh（`ctx.jobs` 后台任务运行时）

- **任务状态闭集**：`packages/jobs/jobs/src/types.ts:17` —— `running | stopping | completed | killed | failed`，注释 `:14-16` 写明"running，可选 stopping，然后**恰好一个**终态"；终态判定只在 `packages/jobs/jobs-local/src/index.ts:66-68` 一处。→ 我们照此形状：**闭集 + 单点终态判定**（T58 验收 1）。
- **明确不做队列**：设计 note `.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md:28`——"provider does not queue, preempt…"。→ **我们不照抄**：路线图要求"排队"作为**任务状态**；但同样不引入多请求队列（§5.1-3）。
- **取消分两层**：在途用 `AbortSignal`；已发布的后台任务用 producer `cancel(reason)` 钩子（`packages/jobs/jobs/src/types.ts:72-77`：同步、幂等、必须最终 settle `done`）。
- **"取消后不再启动新工具调用"的检查点在派发循环**：`packages/core/agent-loop/src/tool-calls.ts:199,211,228`（`while (!aborted && …)`）；**未启动**的调用要补写 `tool/call` + 合成错误结果 `TOOL_ABORTED_BEFORE_DISPATCH`（`:237-259`）。→ 与 T60 同源：取消的生效点是**派发前的既有检查**，不是新增分支；差别是我们把这两处落在 `safety_gate_node` 的 `is_visible` / `classify_tool` 上。
- **进程树终止**：`docs/subsystems/subprocess.md:134,141-144`——"Termination is tree-scoped on every platform"，SIGTERM → grace → SIGKILL；POSIX 打进程组，**Windows 走 `taskkill /T`**。→ T60 的 Windows 实测照这个口径做。
- **崩溃恢复**：`docs/subsystems/persistence.md:13-17`——不为孤儿轮次截断历史，而是补一个合成的 `turn/end {reason: interrupted}`；`packages/jobs/jobs-local/src/index.ts:526-528` 在 teardown 取消失败时把任务强制记 `failed`，detail 写 `cancel threw during teardown; work may be orphaned`。→ **"副作用可能已发生"的如实表达**，与 T61 的"不假定恰好一次"同源。
- **真源**：会话的 append-only `SessionEvent` 日志是唯一真源（`docs/subsystems/persistence.md:5,7`，明写"no parallel persisted event type"）；但**任务状态在内存 Map**（`packages/jobs/jobs-local/src/index.ts:102`），note `:54` 明写跨重启存活"requires a separate durable-job design"。→ **我们与它相反**（§7.3-3）。
- **提交即回收据**：`packages/host/apiproxy/src/api-proxy.ts:2398-2414` 返回 `{ accepted: true }`，完成走事件流；`sessions.cancel` 是**独立一元路由**（`packages/host/apiproxy/src/fetch/handler.ts:102`）→ 取消不依赖提交请求的存续（对应 T58/T60 的端点设计）。
- **会话间并发是显式契约**：`.agents/notes/implemented/feature/2026-06-14-acp-multi-session.md:43`——N 个会话可并发提交、**请求权限**与跑后台任务，且"a cancel in one session does not affect its neighbors"。→ 正是路线图验收"一个任务等待审批时不阻塞其他会话"的外部同构。

### 7.2 codex（Session / Turn / Rollout）

- **一次提交 = 一个后台 Tokio 任务；Session 同时只有一个活动 Turn**：`codex-rs/core/src/tasks/mod.rs:171-203,364-419`；`codex-rs/docs/protocol_v1.md:23-33`。→ 与我们的"每会话单飞"同构，说明**后台执行不需要改 §15.2**（§5.1-3）。
- **提交立即返回、完成走事件**：`codex-rs/app-server/src/request_processors/turn_processor.rs:704-715`（返回 `TurnStatus::InProgress`）；完成在 `codex-rs/app-server/src/bespoke_event_handling.rs:1331-1346` 发 `TurnCompleted`。→ 与 T58 / T59 的目标形态一致（含"取消是独立命令、不靠提交请求存续"）。
- **取消分层 + 与审批的次序**：`codex-rs/core/src/tasks/mod.rs:920-963`（`CancellationToken.cancel()` → 100ms 宽限 → `abort()`）；工具派发处 `codex-rs/core/src/tools/parallel.rs:248-263` 用 `tokio::select!` 兜底返回 aborted。**中断时先让任务观察到取消、再清空待审批**：`tasks/mod.rs:561-566,620-622`。→ 次序写进 T60 的竞态用例。
- **进程终止的平台差异**：`codex-rs/core/src/exec.rs:1026-1082`（Unix：SIGTERM 进程组 → 50ms → SIGKILL）；`codex-rs/utils/pty/src/process_group.rs:82-86,249-253` **在非 Unix 上是 no-op**，因此 Windows 只杀直接子进程（Job Object 只出现在 `codex-rs/app-server-daemon/src/backend/windows.rs:37-58`，**core 的 exec 路径没用**）。→ 直接印证 T60 的未知项：**Windows 的进程树终止不能想当然**。
- **上限有默认值**：命令 `DEFAULT_EXEC_COMMAND_TIMEOUT_MS=10_000`（`core/src/exec.rs:63`）、输出 1 MiB（`utils/pty/src/lib.rs:25`）、后台终端 `background_terminal_max_timeout` 默认 300000（`core/src/config/mod.rs:1089-1090`）。→ **不照搬数值**（量级与本项目不同），只借"每个执行面都要有显式上限"的形状；本项目复用既有 `config.py` 口径（§5.1-4）。
- **"可能已部分执行"如实进入可见历史**：`codex-rs/core/src/context/turn_aborted.rs:10-11`——"Any running unified exec processes may still be running in the background. If any tools/commands were aborted, they may have partially executed."；`TurnStatus::{Completed,Interrupted,Failed,InProgress}`（`codex-rs/app-server-protocol/src/protocol/v2/turn.rs:33-38`）。→ 与 T61 验收 3 一致：不宣称"未执行"。
- **真源分离**：rollout JSONL 是历史、SQLite 存元数据，`codex-rs/thread-store/README.md:22-28,34-35` 明写"raw history appends remain history-only"。→ 与 §15.4"Checkpoint 是真源、事件是投影"同源，**我们照做**。
- **重启恢复**：`codex-rs/app-server/src/daemon_thread_recovery.rs:10-49`（snapshot 写盘 → 读回 → 后台恢复，"normal cold-resume semantics"）。

### 7.3 对照后的取舍（已写进任务的结论）

1. **照 dsh**：任务状态用**闭集 + 单点终态判定**；取消落在"派发前的既有检查"，未启动的调用要**补明确结果**而不是静默丢弃（T60）。
2. **照 codex**：**不改每会话单飞**就能得到后台执行（§5.1-3）；**中断先让任务观察到取消、再清待审批**（T60 竞态用例）；"可能已部分执行"要如实进可见历史（T61）。
3. **不照抄两家**：它们**都不持久化任务状态**（dsh 为内存 Map、codex 的状态随线程生命周期），而路线图验收要求"重启后区分三类任务"——这是本项目**必须自己新增**的一档，也是本阶段风险最高的部分（T57 / T61）。
4. **不照抄两家的默认值**（dsh 并发 10、无任务总时长上限；codex 命令 10s / 后台 300s）：本项目默认并发 **1**、任务总时长**不限**，其余复用既有 `config.py` 项（§5.1-4）。
5. **两家都缺、我们要补的**：「需要人工核对」这一档在**任务状态**里两家都没有——dsh 的 `JobStatus` 没有它（最近的是锁层面的 `packages/util/atomic-write/README.md:47` "Orphaned locks require operator recovery"），codex 的 `TurnStatus` 只有 `Completed / Interrupted / Failed / InProgress`。→ "什么算需人工核对"要自己写死（T61），没有现成参照，这也是 §6 停止条件 4 的来源。

## 8. 提交清单（v0.6 一次提交，**等并发会话落地后执行**）

**为什么不能现在提交**：同一个工作树里有另一个会话在并行改（2026-10-10 实测 `git status` 101 条），而我改的多数文件**与它混在一起**——`ARCHITECTURE.md` 总改动 `+391`、`graph/nodes.py` `+162`、`docs/api/openapi.json` `+761`、`frontend/src/api/schema.gen.ts` `+422`，其中只有一部分是我的。按路径整份提交会把它的未完成工作写进来；按 hunk 切分要面对几百个 hunk，风险和收益不成比例。**只提交我的新文件更糟**：那会留下一个"模块在、契约与接线不在"的自相矛盾历史，违反本项目"契约与实现同批出现"。

**判据（它落地的标志）**：下面「不提交」一列里的文件**不再出现在** `git status --porcelain` 里。落地之后，所有混合文件里剩下的 diff **正好就是 v0.6 的改动**，此时按路径提交是安全的。

**复核步骤（提交前必做）**：`git status --porcelain` → 逐条对照本清单；**清单外出现任何文件就停下来问**，不要"顺手带上"。

**要提交的路径**（我的 v0.6 改动；`ARCHITECTURE.md` 是唯一同时含契约与实现的文件）

```text
ARCHITECTURE.md
V0_6_DEVELOPMENT_PLAN.md
docs/planning/FUTURE_WORK.md                               # T63 第二片：B-34 的 2026-10-10 复测段（实测只 +5 行，未混入他人改动）
local-agent-web/README.md
local-agent-web/config.yaml.example
local-agent-web/docs/api/openapi.json
local-agent-web/src/local_agent/session_kind.py            # T57 新增（叶子）
local-agent-web/src/local_agent/job_state.py               # T62 新增（叶子）
local-agent-web/src/local_agent/cancellation.py            # T60 新增（叶子）
local-agent-web/src/local_agent/persistence/job_store.py   # T57 新增
local-agent-web/src/local_agent/persistence/migrations.py  # v7 / v8
local-agent-web/src/local_agent/persistence/session_store.py
local-agent-web/src/local_agent/persistence/request_ledger.py
local-agent-web/src/local_agent/application/jobs.py        # T58 新增
local-agent-web/src/local_agent/application/recovery.py    # T61 新增
local-agent-web/src/local_agent/application/service.py
local-agent-web/src/local_agent/application/models.py
local-agent-web/src/local_agent/application/events.py
local-agent-web/src/local_agent/api/routes.py
local-agent-web/src/local_agent/api/container.py
local-agent-web/src/local_agent/api/schemas.py
local-agent-web/src/local_agent/security/policy.py
local-agent-web/src/local_agent/security/process_guard.py  # T60 第二片新增
local-agent-web/src/local_agent/capabilities.py
local-agent-web/src/local_agent/config.py
local-agent-web/src/local_agent/exceptions.py
local-agent-web/src/local_agent/graph/state.py
local-agent-web/src/local_agent/graph/builder.py
local-agent-web/src/local_agent/graph/nodes.py             # 只差 origin 三步链那几行
local-agent-web/src/local_agent/tools/shell_tools.py
local-agent-web/src/local_agent/tools/search_tools.py      # T63：save_full + 落盘接线 + gitignore 遍历
local-agent-web/src/local_agent/tools/result_spill.py      # T63 新增
local-agent-web/src/local_agent/rag/ignore.py              # T63 第二片新增（静态清单 + .gitignore 规则）
local-agent-web/src/local_agent/rag/walk.py                # T63 第三片新增（三处共用的遍历）
local-agent-web/src/local_agent/rag/search.py              # T63：改用共用遍历，不再自带跳过清单
local-agent-web/tests/test_search_spill.py                 # T63 新增
local-agent-web/tests/test_search_ignore.py                # T63 第二片新增
local-agent-web/tests/test_search_fairness.py              # T63 第三片新增
local-agent-web/pyproject.toml                             # T63 第二片：显式声明 pathspec
local-agent-web/uv.lock                                    # 同上（`uv lock --offline` 已刷新）
local-agent-web/tests/test_session_kind.py                 # 新增
local-agent-web/tests/test_jobs.py                         # 新增
local-agent-web/tests/test_job_events.py                   # 新增
local-agent-web/tests/test_cancellation.py                 # 新增
local-agent-web/tests/test_process_guard.py                # 新增
local-agent-web/tests/test_recovery.py                     # 新增
local-agent-web/tests/test_api.py
local-agent-web/tests/test_tools.py
local-agent-web/tests/test_rag.py                         # T63 第三片：2 处补丁点（os.walk → iter_candidate_files）
local-agent-web/tests/test_memory_origin.py
local-agent-web/frontend/src/api/client.ts
local-agent-web/frontend/src/api/queries.ts
local-agent-web/frontend/src/api/viewModels.ts
local-agent-web/frontend/src/api/schema.gen.ts
local-agent-web/frontend/src/api/useSessionStream.ts
local-agent-web/frontend/src/api/useSessionStream.test.tsx
local-agent-web/frontend/src/app/App.tsx
local-agent-web/frontend/src/app/LiveApp.tsx
local-agent-web/frontend/src/app/LiveApp.test.tsx
local-agent-web/frontend/src/components/Sidebar.tsx
local-agent-web/frontend/src/components/Sidebar.module.css
local-agent-web/frontend/src/features/tasks/               # T62 新增整个目录
local-agent-web/frontend/src/features/conversation/adapters.ts
local-agent-web/frontend/src/features/conversation/RunActivity.tsx
local-agent-web/frontend/src/fixtures/scenarios.ts
local-agent-web/frontend/src/fixtures/types.ts
local-agent-web/frontend/e2e/prototype.spec.ts
local-agent-web/frontend/e2e/__screenshots__/1440x900/tasks.png
local-agent-web/frontend/e2e/__screenshots__/1280x800/tasks.png
local-agent-web/frontend/e2e/__screenshots__/640x800/tasks.png
```

**明确不提交**（并发会话或 v0.5 收口的产出，我一个字都没动）：

```text
README.md                                       # 根目录
V0_4_DEVELOPMENT_PLAN.md
docs/lessons/*            docs/benchmarks/*
local-agent-web/scripts/verify_delivery_evidence.py
local-agent-web/src/local_agent/context/citations.py
local-agent-web/src/local_agent/context/prompt_context.py
local-agent-web/src/local_agent/memory/tool_output.py
local-agent-web/src/local_agent/memory/{recall,short_term,summarizer}.py
local-agent-web/src/local_agent/llm/{compat,usage_recording}.py
local-agent-web/src/local_agent/tools/{file_tools,plan_tools,result_types,skill_tools,web_tools}.py
local-agent-web/frontend/src/components/ContextMeter{,.test}.tsx
local-agent-web/tests/{test_cache_prefix_regressions,test_capability_boundary,test_events,test_graph_resume,test_llm_compat,test_memory,test_model_usage,test_runtime_snapshot,test_web_tools,test_citations,test_delivery_evidence,test_pressure_compaction,test_tool_output_projection}.py
```

**提交方式**（提交信息按仓库习惯写清楚"做了什么 + 依据哪一节契约"）：

```powershell
git add -- <上面那串路径>
git --no-pager diff --cached --stat    # 复核：只有清单里的文件、且没有别人的新模块
git commit -m "v0.6 后台任务与长时间运行（T57–T62）"
```

**提交信息草案**：任务索引与会话来源（§17.1）→ 后台 Job Runtime 与状态机（§17.2）→ 结构化任务事件（§17.3）→ 取消、超时与进程树终止（§17.4）→ 重启三类收口与 `needs_review`（§17.5）→ 任务中心与恢复入口（§17.6）；`jobs.background` 默认关闭，五条全局硬验收见 `V0_6_DEVELOPMENT_PLAN.md` §4。


## 9. 2026-10-10 审查修复与复验

用户授权直接修改审查问题。后台队列将锁释放独立为清理回调，已取消任务的跳过、启动异常、执行异常和结算异常都能清理；启动、取消与结算迁移互斥，worker 继续服务后续会话。新提交全部预检通过后才重置取消令牌；审批恢复按 Checkpoint 轮次恢复取消意图，覆盖恢复登记后重启的窗口。T63 对落盘/清理校验解析链接后的工作区边界，拒绝链接目标覆盖，本次结果优先保留并串行清理。SESSION_BUSY 提示和状态机主表已同步。

最终完整 Python **1469 passed / 8 skipped / 0 failed**（539.26 秒）；ruff、mypy src（122 文件）、OpenAPI、前端 API 漂移检查通过。新增 17 个边界/竞态/重启回归实例。没有新增付费调用，不做回答语义质量验收，未提交 Git、未操作生产库。完整文件清单、原始审查证据与复跑命令见 `docs/benchmarks/v05-v06-review-20261010.md`。

## 10. 2026-10-11 必须改修复与升级边界

取消在同一任务迁移锁内核对目标、触发令牌、保存已核实的运行编号并写终态；迟到的旧超时/取消不触碰同会话下一轮令牌。终态仍不可改写，图已停在审批但任务尚未结算时也能保留取消身份。旧版本已有空绑定取消记录无法证明所属轮次时，旧审批不执行；后续有独立绑定的新轮次仍可正常提交。定向回归 **50 passed**，三个原始缺陷探针全部通过，最终完整回归 **1482 passed / 8 skipped / 0 failed**。没有新增数据库迁移、公共 DTO 或另一套恢复执行路径。详见 `docs/benchmarks/v07-readiness-fixes-20261011.md`。

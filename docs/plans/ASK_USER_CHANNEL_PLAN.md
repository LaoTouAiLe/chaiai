# 模型中途提问通道（ask_user）方案

> 状态：**方案已评估、四项决策已定；架构文档已按本方案修订，代码未开工**（2026-10-28）。裁决与依据见 §9。
> 目的：模型在同一轮执行中途需要用户补一句信息时，能**挂起等待答复并继续**，而不是把问题当成最终回答、结束这一轮。
> 相关：`docs/plans/QQ_CHANNEL_PLAN.md:224` 记录的缺口（"做不了『停下来的选择题』"）。

---

## 1. 现状（先看证据）

| 事实 | 证据 |
| --- | --- |
| 工具集是静态闭集，**没有** `ask_user` / `clarify` / `elicitation` 之类入口 | `local-agent-web/src/local_agent/capabilities.py:53-91` |
| 模型表达"需要用户给信息"只有两条路：`task_control(action="need_input")`（只能带一句话）与反思层 `ask_user`（只能带一句话） | `src/local_agent/tools/plan_tools.py:30-51`；`src/local_agent/reflection/reflector.py:143-148` |
| 两者都**不挂起**：`need_input` 被当成"收尾动作"，直接路由到 `final_response_node → END`，本轮终态记 `blocked` | `src/local_agent/graph/tool_runner.py:640-648`；`src/local_agent/graph/main_loop.py:42-53`；`src/local_agent/graph/edges.py:155` |
| `blocked` 的语义就是"本轮已结束、在等用户补充"，不是等待态 | `src/local_agent/application/models.py:978-984` |
| 唯一真正挂起的机制是**审批**：`interrupt()` + `Command(resume=...)` | `src/local_agent/graph/tool_runner.py:836-877`；`src/local_agent/application/service.py:3028-3092` |
| 审批卡只有按钮 + 一个 checkbox，**没有自由文本输入**；输入区接管位在类型上只有 `approval` 一种 | `frontend/src/features/approval/ApprovalCard.tsx:226-250`；`frontend/src/fixtures/types.ts:530` |

结论：**"停下来问一句再继续"目前不存在**。要做得新增一条通道，不是改一个提示词能解决的。

---

## 2. 三条候选路线

### 路线 A：把提问塞进审批通道

做法：`pending_approval` 加一个 `kind`，审批决定闭集加 `answer`，审批卡加一个文本框。

- 工作量：最小（不动 interrupt 节点结构、不新增状态字段）。
- **否决理由（三条，都硬）**：
  1. 审批的判定与记录全绑在"动作指纹 / 放行 / 二次确认"上——`action_fingerprint` 参与 `build_decision`、`requires_second_confirmation` 参与阶段推进（`tool_runner.py:864-877`）、放行标记写 `decision_trace`（`:901-914`）。提问要伪造一份"动作"才能进这条管道。
  2. 审批有"无人值守一律拒绝"的分支（`tool_runner.py:853-862`）。提问复用后，定时任务里的提问会变成一条"被拒绝的动作"，语义错。
  3. 审批的对外状态机是 `pending/executing/approved/rejected/expired/interrupted`（`models.py:758`、`projection.py:1345-1405`）。"已回答"不是其中任何一种，硬塞会把 5 个状态的含义全部稀释。

### 路线 B：独立提问通道，与审批同构但不共用字段 ★推荐

做法：新增工具 `ask_user` + 新增 `pending_question` 状态 + 新增 `question_interrupt_node` + 新增 `resolve_question` 接口 + 前端新增第二种 takeover 形态。

- 复用：LangGraph 的 `interrupt()` / checkpoint / `Command(resume=...)` / `_guarded` 幂等台账 / SSE 快照+增量。
- 不复用：审批的指纹、放行、二次确认、`awaiting_approval` 状态。
- 成本最高，但每条语义都对得上，且不污染既有审批审计。

### 路线 C：维持现状（只说一句文本）

零成本。代价：模型执行到一半缺一个参数（目录、分支名、要不要覆盖）时只能结束整轮，用户下一条消息进来是**新 run**，前面积累的中间结论要么重跑要么靠上下文压缩后残存。

### 路线 D：半挂起（不 interrupt，靠下一轮消息回填）

做法：模型调 `ask_user` 时本轮直接结束（复用现有 `blocked`），用户下一条消息进来时，服务层把它包成 `ToolMessage` 回填到上一轮的 `tool_calls` 上，再开新 run 继续。

- 看起来最省事（不需要新节点、新状态、新事件），但**否决**：
  1. `tool_calls` 缺 `ToolMessage` 就发不出去（`tool_runner.py:950-954` 的 400 实证）。也就是说用户**不发消息**时，历史一直处于残缺态；用户改了话题，那条悬空调用要么被伪造成"用户跳过了"，要么报错。
  2. 同一件事会被记成**两条 turn**（`TurnRecordView`，§15.6）：提问一条、回答一条，`run_id`/用量/产物/审批归属全部分裂，任务中心与审计要同时改。
  3. "用户是在回答提问还是在开新话题"没有可靠判据，只能靠猜测——与项目"不猜"的口径冲突。
- 所以它省下的是代码行数，付出的是一条**无法闭合的消息协议**。

**推荐 B，并分三期落地（§8）。**

---

## 3. 目标与非目标

**做到**
- 同一轮内：模型调用 `ask_user` → 图挂起 → 页面出现提问卡 → 用户答复 → 图继续，答复以工具结果形式回到模型。
- 未答复时：禁止在同一会话开新一轮（否则挂起被丢弃，与审批同一约束）。
- 刷新/重连后提问卡仍在（靠快照，不靠补发）。
- 无人值守会话（定时任务 / 心跳 / 子 Agent）**不会**挂起。

**这次不做（明确列出）**
- 不做 HTML / 富表单；P0 只做"一句问题 + 单行或多行文本框"。
- 不做超时自动跳过（P2 再说；审批目前也没有本机超时）。
- 不做"把提问当审批"（路线 A 已否决）。
- 不改 `need_input` 的既有语义（它仍是"本轮收尾"），只调整两者在提示词里的分工（§7）。
- 不做跨渠道（QQ）实现，但通道设计要让桥接层将来能复用（同 `QQ_CHANNEL_PLAN.md:224` 的诉求）。

---

## 4. 后端设计

### 4.1 工具

```
ask_user(question: str, options: list[str] | None = None,
         multiple: bool = False, allow_free_text: bool = True)
```

- 归属能力：`task_lifecycle`（与 `task_control` 同族——它是控制流，不是副作用）。追加在 `CAPABILITY_TOOLS["task_lifecycle"]` 末尾（`capabilities.py:54`）。
- 安全分类：进 `CONTROL_TOOLS`（`security/policy.py:90`），于是 `classify_tool` 走"免审批"分支（`:692-693`），`KNOWN_TOOLS` 自动含它（`:117-126`）。
- P0 参数只保留 `question`；`options`/`multiple`/`allow_free_text` 在 P1 加，避免先定义一个用不上的 schema。

### 4.2 图

- 工具执行前置节点（现在 defer 审批的地方，`tool_runner.py:238-254` 一带）识别 `ask_user` 调用：
  - 写入 `pending_question = {question_id, tool_call_id, question, options, asked_at, run_id}`；
  - 把该调用从 `pending_tool_calls` 移到现有的排队表 **`deferred_tool_calls`**。已核实它就是"还没结局的调用"的通用覆盖型通道，不需要新表：`tool_runner.py:153-155` 把排队项排在本批最前，`:334-338` 每批重算，路由由 `routers.py:309-322` 按"有没有未结算条目"（对照 `tool_results`）决定是否回到安全门。要改的只是 `_has_queued_approval` 的判据口径（从"待审批"扩成"未结算的排队调用"）与命名，不是新机制。
- 新节点 `question_interrupt_node`：
  - **只读 state、只调 `interrupt(payload)`，不做任何副作用**——`resume` 时整个节点会被重放（这条约束在 `tool_runner.py:841` 有原话）；
  - `payload` 必须可 JSON 序列化（会进 Checkpoint，同 `_approval_prompt` 的注释 `:993`）；
  - 无人值守判定与审批同源（`session_kind != interactive`，`tool_runner.py:857`）：**不 interrupt**，直接产出 `ToolMessage("当前会话无人值守，无法向用户提问……")`，让模型自行收尾。
- resume 后：清 `pending_question`、补一条 `ToolMessage(name="ask_user", content=<用户答复>)`、写 `tool_events` / `tool_results`。
  - **必须补 ToolMessage**：缺一条应答会在历史里留下"有调用、无结果"，下一轮发往 OpenAI 兼容端点会被 400 拒绝（`tool_runner.py:950-954` 记过这次的实证）。
  - 出口接 `tool_feedback_node`（既有把工具结果交回模型的汇聚点），由它回到 planner —— 具体连边在实现时以 `edges.py:140-180` 的既有汇聚点为准，不新造汇聚点。
- 互斥：`pending_approval` 与 `pending_question` **不允许同时非空**。实现时在写 state 的两处各断言一次；投影层若同时读到一个，按"先答提问"处理并记一条 `decision_trace`（失败闭合，不静默）。

### 4.3 状态与 DTO

- `AgentState` 新增 `pending_question: QuestionRequest | None`（`graph/state.py:513-517` 旁边）；`question_asked_at` 与 `approval_requested_at` 同理保留（等答复时长要用）。
- 新 DTO `QuestionView`（`application/models.py`，仿 `ApprovalView:761-788`）：

  ```
  question_id, run_id, tool_call_id, question,
  options: list[str], multiple: bool, allow_free_text: bool,
  state: Literal["pending","answered","skipped","expired"],
  answer: str | None,      # 只回显用户自己写的话，长度上限 2000
  created_at, note
  ```

- 会话详情视图加 `question: QuestionView | None`（`models.py:1205` 的 `approval` 旁边）。
- 投影函数 `project_question`（仿 `project_approval`，`projection.py:1345`）：`state` 全部从 `pending_question` + `answers` 记录推导，不加状态机字段。
- 会话状态：`project_session_state`（`projection.py:1941-1942`）在 `pending_question` 存在时返回 `awaiting_input`（§9-D2）。

### 4.4 接口

```
POST /api/sessions/{session_id}/questions/{question_id}
body: { answer: str | null, action: "answer" | "skip", client_request_id: str }
```

- `service.resolve_question(...)` 与 `resolve_approval` 同构：`_require_pending_question` 前置校验 → `Command(resume={"action": ..., "answer": ...})` → `_guarded(..., resume_job=True)`（`service.py:3028-3092` 是模板）。
- 幂等键：`client_request_id` + 规范化后的 answer。重复提交返回首次结果，不改写历史答复。
- `_require_idle`（`service.py:3096-3108`）已用 `has_live_interrupt` 拦新提交，提问挂起时**无需改动就**会拒新消息——这是期望行为，但要补一条用户可读的错因文案（现在说的是"审批还没决定"）。

### 4.5 恢复路径（易漏）

`recovery.classify_interrupted_job` 现在把"还挂着活 interrupt"直接当作"在等审批"（`application/recovery.py:152-159`）。新增提问后必须按 state 里的 `pending_question` 分流，否则重启后一条提问挂起会被当成审批去恢复，结果 resume 到错误的 interrupt。

### 4.6 事件

新增 `question_required` / `question_resolved`，在 `_turn_events` 里按 `announced.question` 去重发一次（照 `service.py:4638-4647` 的 `approval_required` 写法）。事件名要同步进 `application/events.py:47-59` 的闭集与 `tests/test_events.py:183-195` 的顺序断言。

---

## 5. 前端设计

| 改动 | 位置 |
| --- | --- |
| takeover 联合类型加一支 | `frontend/src/fixtures/types.ts:530` → `{kind:'question'; question: QuestionView}` |
| 新卡片组件 | `frontend/src/features/question/QuestionCard.tsx`（结构照 `ApprovalCard.tsx`：标题 + 问题正文 + 输入 + 两个按钮"提交"/"跳过"；`state !== 'pending'` 时只显示结果、不显示按钮） |
| Composer 分发 | `frontend/src/components/Composer.tsx:179-180` 一带：`takeover.kind === 'question'` 时渲染卡片并禁用原输入区 |
| 提交 mutation | `frontend/src/api/queries.ts:601-616` 旁新增 `useResolveQuestion`（同一 `client_request_id` 重试复用，成功/失败都重取快照） |
| 快照投影 | `frontend/src/app/LiveApp.tsx:359` 改为三选一（none / approval / question） |
| 事件订阅 | 收到 `question_required` 即重取快照（与 `approval_required` 同路径） |
| 会话状态映射 | `frontend/src/features/conversation/adapters.ts` 的 `mapSessionState` 增加 `awaiting_input`（`blocked` 归 `idle` 的既有规则不变） |
| 视觉基线 | 新增提问卡（pending / answered / skipped）三张，需更新既有基线数 |

文案要求：卡片里**不出现** interrupt / checkpoint / 工具名这类内部机制词（提问正文来自模型，卡片框架文案用"需要你补充一句"）。

---

## 6. 契约冲突清单（要动的地方）

| # | 冲突 | 证据 | 化解 |
| --- | --- | --- | --- |
| 1 | 期望用户输入的时长没有落点 | `state.py:513-517` 只有 `approval_requested_at` | 加 `question_asked_at`，并把 `turn_usages` 的 `approval_wait_ms` 扩成含提问等待（或单列 `question_wait_ms`） |
| 2 | `SessionState` 是闭集，且当前把"有活 interrupt"一律说成 `awaiting_approval` | `models.py:1130`；`projection.py:1931-1954`（`:1941-1942` 是唯一落点） | 追加 `awaiting_input` 这一个取值，投影按 `pending_question` 分流（§9-D2）。**不改** `TurnFinalState`：提问挂起不是轮次终态 |
| 3 | 任务状态机把"等待"映射成 `waiting_user` 且只认审批 | `application/jobs.py:86-98,104-112` | 不改映射：`running`/`waiting_user` 都能承载提问；但 `job_waiting` 事件的 detail 文案要能说"在等你回答" |
| 4 | 恢复把活 interrupt 当审批 | `application/recovery.py:152-159` | 按 `pending_question` 分流（§4.5） |
| 5 | 输入区接管位只有一种 | `types.ts:530` | 加一支（§5） |
| 6 | 事件闭集是白名单 | `events.py:47-59`、`tests/test_events.py:183` | 加两个事件名并同步断言 |
| 7 | 工具集变化会改系统前缀 | `capabilities.py:18-20` | 一次性前缀缓存失效，可接受；但**必须**让 `ask_user` 挂在默认开启的 `task_lifecycle` 里，否则关掉的能力会让"提问"时有时无 |

---

## 7. 提示词与模型行为

- 系统提示词里加一条分工（`context/prompt_context.py:671-678` 附近）：
  - `ask_user`：**执行中**缺一个无法从上下文/文件推断的输入（目标目录、分支名、二选一的方案）。
  - `task_control(need_input)`：任务**可以就此收尾**、剩下的判断只能由用户做。
  - 明确禁止："不要为了确认你自己能推断的默认值而提问"、"同一轮不要重复问同一件事"。
- 反思层 `ask_user`（`reflector.py:143-148`）保持原样，不接到新通道：反思发生在收尾判定阶段，那时不挂起是合理的（改动它会让"未验证就追问"的既有强制逻辑变复杂）。

---

## 8. 分期与验收（每条都要能观察）

### P0：一问一答（单文本框 + 跳过）

**进度（2026-10-28）：后端两步都已落地。**

1. **工具契约**：`ask_user` 登记进 `task_lifecycle`（排在 `task_control` 之后）、安全门按控制工具免审批、
   `tests/test_ask_user_tool.py` 12 例。
2. **图与服务链路**：`pending_question` / `question_asked_at` / `question_history` 三个 State 字段、
   `question_interrupt_node`（与审批同构，含无人值守不挂起）、安全门分流、`deferred_tool_calls` 排队、
   `QuestionView` + `project_question`、`POST .../questions/{id}` + `resolve_question`、
   `question_required` / `question_resolved` 事件、`SessionState` 与 `CommandStatus` 各扩一个
   `awaiting_input`。`tests/test_ask_user_flow.py` 4 例覆盖挂起、答复继续、跳过、答非所问被拒、无人值守。

3. **前端提问卡**（2026-10-28 完成）：`features/question/QuestionCard.tsx` + 样式、`Composer` 接管
   （`data-composer-mode="question"`）、`useResolveQuestion` 幂等 mutation、`LiveApp` 的快照派生与
   `awaiting-input` 状态接线、侧栏「等待回答」文案、7 例组件测试，并已 `npm run build` 出新产物。

**顺手做的两处拆分**（规模棘轮逼出来的）：`application/pending_views.py`（审批/提问两种投影）、
`application/resolution.py`（两种恢复命令），基线已收紧。

**P0 仍未做**：提问卡的 e2e 视觉基线（需跑 playwright 截图生成约 7–9 张），以及真实模型下的一次冒烟。

改动面：后端 8 个文件（capabilities / policy / state / tool_runner / edges+routers / models / projection / service+routes+events+recovery）、前端 6 个文件 + 约 7–9 张新基线（3 个视口，见 §10）。

验收：
1. 用**假模型**脚本化一次 `ask_user("工作目录是哪个？")` → 页面输入区变成提问卡；会话状态是 `awaiting_input`（`running` 之外的新取值，见 §9-D2）。
2. 输入 `src` 提交 → 卡片收起；下一轮模型调用的消息里存在 `ToolMessage(name="ask_user", content="src")`；该轮正常完成。
3. 刷新页面 → 提问卡仍在（快照恢复），且不能提交新消息（`session_busy`）。
4. 点"跳过" → 工具结果为"用户跳过，未提供信息"，模型不重复提问（提示词约束 + 断言不出现第二次 `ask_user`）。
5. 定时任务会话里触发 `ask_user` → **不出现**挂起、不出现卡片、该轮按无人值守收尾。
6. 重启服务后再答 → 恢复路径按 `pending_question` 走，不报 `approval_not_actionable`。
7. 同一 `client_request_id` 重复提交 → 返回首次结果，`answers` 只有一条。

### P1：题组与选项（2026-10-28 完成）

形态按用户要求改成"考试式"：**一次挂起一组题（1–5），用户翻页作答，答完点一次"提交"整组上送**——
不是"答一题提交一次"。

- 工具：`ask_user(questions=[{question, options?, allow_free_text?}])`，每题最多 3 个候选，单题写法保留。
- 状态：`pending_question.items` 存整组；`question_history` 一次记整组 `answers`（逐题 `index/question/answer/skipped`）。
- 图：一次 `interrupt()` 等整组，恢复时把整组问答写成**一条** `ToolMessage`（多条会破坏 `tool_calls` 配对）。
- 前端：`QuestionCard` 变成题组卡——右上角 `1/3`、选项按钮 + 自定义输入、上一题/下一题、最后一题"提交"、"全部跳过"、底部进度点；草稿存 `sessionStorage`（标签页内保活，不落服务端）。
- 提示词：加一条"缺多个就一次问清（最多 5 题，每题最多 3 个候选）"，并同步了 `FULL_PROFILE_PROMPT_FINGERPRINT`（有意的前缀变化）。
- 验收（真实模型已跑通）：模型一次生成 3 题并自带候选；整组提交后 `items[1].skipped=true`（该题留空），
  模型收到"1. 发布环境：测试环境 / 2. 分支：跳过（未提供） / 3. 先备份：要备份"并正确收尾。
- 视觉基线：`question` / `question-answered` 各 3 个视口重拍（含题组形态）。

### P2：超时与默认值
- `ask_user(timeout_seconds=...)`，超时按"跳过"续跑并注明"用户超时未答"；超时要与任务的 `max_task_seconds` 看守（`service.py:839-856`）区分，不能用取消来代替。

### 测试策略
- **全部用假模型**：`ask_user` 的调用序列可以完全脚本化，不需要花真实模型调用（纪律见项目既有做法）。
- 必测的单点：重放安全（resume 后 `pending_question` 不重复写入）、互斥断言（审批与提问不同时存在）、无人值守、幂等、恢复分流。

---

## 9. 决策（按项目规范与技术成熟度裁决）

四条都按"**既有契约优先、不新增可避免的概念、不复用语义不同的字段**"来定，不再抛回给用户。

### D0（前置）：架构文档先行，不是可选项

`CONTRIBUTING.md:11-12` 硬性要求："修改 State 字段、Graph 边、工具签名或存储字段，必须先改架构文档"。

**状态：已完成（2026-10-28），只改了 `ARCHITECTURE.md`，未动任何代码。** 实际修订的段落如下（含原表之外补的三处：§5.3 的会话状态取值、§15.8 的可见性行、§21.4 的无人值守验收）：

| 架构文档位置 | 要改什么 |
| --- | --- |
| §4.2 短期记忆 State | 新增 `pending_question` / `question_asked_at` |
| §5.1 节点定义表 | 新增 `question_interrupt_node` 一行 |
| §5.3 工具任务分支 | 图里加"安全门 → 提问挂起 → 答复 → tool_feedback"这条边 |
| §6.9 能力归属表 + 新工具小节 | `task_lifecycle` 追加 `ask_user`，并按 §6.1–6.8 的体例写一节工具定义 |
| §15.3 / §15.4 | 新增 `POST .../questions/{id}` 与 `question_required` / `question_resolved` |
| §17.5 判定表第 1 行 | 现文"还挂着活的 interrupt ⇒ 走既有审批恢复"（`ARCHITECTURE.md:2773`）必须改成按 `pending_question` 分流 |
| §17.6 "在等什么" | `awaiting_input` 的文案 |

### D1：`ask_user` 与 `need_input` **并存**

- **依据**：删 `need_input` 要同时改 `main_loop.py:42-53` 的终态判定、`edges.py:155` 的路由、`decision_streaming.py:150-156` 的流式投影、`reflector.py:143-148` 的枚举，以及 `blocked` 的文档语义（`models.py:978-984`）——为一个"概念更少"的收益去动四处已冻结契约，不划算。而两者语义本来就不同：`need_input` 是"本轮到此为止，等你开新话题"，`ask_user` 是"执行中缺一个输入，等你一句我就继续"。
- **代价与抑制**：并存有混用风险。用**可测的约束**压住——两个工具的 docstring 互相点名分工（`plan_tools.py:45-50` 与新的 `ask_user` 各写一句），并加一条构建期断言"两条 docstring 里都出现对方名字"，混用一改就会红。
- **不做**：不改反思层的 `ask_user` 枚举（那是收尾阶段的判定，不能挂起）。

### D2：新增 `awaiting_input`，**只扩 `SessionState`，不扩 `TurnFinalState`**

- **依据**：`projection.py:1932` 的原话是"会话状态只由是否有待决审批、`task_status`、是否已有 run 决定"，且 §17.6 规定"在等什么"是状态的函数、服务端不猜。若提问挂起期仍报 `running`，任务中心和侧栏会说"正在运行"——而事实是它在等用户，这是项目反复禁止的"说假话"。项目已有同类先例：`cancelled` 就是"三处闭集同时扩一个取值"（`ARCHITECTURE.md:1974-1977`）。
- **落点只有一个**：`project_session_state`（`projection.py:1941-1942`）改成先看 `pending_approval`、再看 `pending_question`。
- **代价**：前端 `mapSessionState`、"在等什么"文案、若干测试；**不需要**重做侧栏结构，也**不新增**既有场景的视觉基线（只有新提问卡需要新基线）。
- **不做**：不给 `TurnFinalState` 加值——提问挂起不是轮次终态，加了会让"本轮是否结束"变得含混。

### D3：挂起期间**不依赖"停止"**，出口只有 `answer` / `skip`（已实测）

**实测记录（2026-10-28；假模型 + 临时工作区 + 隔离库，`jobs.background=true`，临时用例跑完即删）**：

| 步骤 | 观察量 |
| --- | --- |
| 提交一次删除请求 | `status=running`，任务 `waiting_user`，审批 `pending`，`session_state=awaiting_approval` |
| 调 `cancel_task` | `status=cancelled`，任务离开非终态集合（`active_for_session → None`） |
| 取消后读会话 | `session_state` **仍是** `awaiting_approval`，审批卡**仍 pending**（没被取消掉） |
| 取消后再提交消息 | `SESSION_BUSY`（被拒） |
| 取消后点审批卡 | **成功**，`status=running` —— 挂起被正常恢复 |

- **结论一**：取消**不会**解除挂起。它只把任务索引落成 `cancelled`，于是"任务已取消"与"会话仍在等审批"同时成立——审计上一处不一致，但没有任何东西被破坏。
- **结论二**：这**不是死锁**。卡片还在，点它就恢复。所以提问通道不需要为取消另造机制，`skip` 足够。
- **结论三**：默认配置（`jobs.background=false`，`config.py:942/947`）下 `_jobs is None`，`cancel_task` 是空操作；前端在挂起时也不渲染停止按钮（`LiveApp.tsx:635` 的 `&& !awaitingApproval`，注释见 `:629-630`）。交互路径上根本不存在"点了停止"这件事。
- **本方案要补的一处**：把 `awaiting_input` 也并进那条 `canStop` 表达式（`LiveApp.tsx:635`），否则新状态挂起期间会冒出一个按了没效果的停止按钮。
- **范围外只报告**：取消一个 `waiting_user` 任务会让任务索引与轮次脱节（任务说 cancelled，这一轮却继续跑完）。审批同样有，修它要动 §17.4 的取消语义，不在本方案内。

### D4：答复**双轨**——模型侧原文，展示与落库侧脱敏

- **依据**：审批已有完全同构的口径——`arguments` 以原文进 Checkpoint（模型要真实参数才能执行），展示一律走 `redacted_arguments`（`_approval_prompt` 的载荷 `tool_runner.py:992-1008`、投影 `_display_arguments`）。
- **做法**：进 `messages` 的 `ToolMessage` 用原文；进 `tool_results` 的 preview、DTO、事件载荷一律过 `SecurityPolicy.redact`（同类先例是拒绝理由 `tool_runner.py:961-964`）。答复长度上限 2000，与拒绝理由同值。
- **不做的**：不做"答复写入记忆"的额外闸门——它天然会被现有的"本轮记忆候选"链路处理，另外加规则就是第二套真源。

### 各条的反证条件（什么情况下该反过来选）

| 决策 | 推翻它的观察量 |
| --- | --- |
| D1 并存 | 真实使用里出现"该提问却结束整轮"的高频误用（可从 `need_input` 与 `ask_user` 的调用分布看出），说明两个概念对模型不可区分，再合并 |
| D2 新增状态 | 前端出现"两个状态做同一件事"的第三处分支，说明状态值本身没带来表达力，退回复用 `awaiting_approval` |
| D3 只用 skip | 将来把挂起期取消改成 resume 驱动（动 §17.4），`skip` 可以降级为普通按钮之一 |
| D4 双轨脱敏 | 用户反馈"答复被遮挡后看不懂"（例如把路径打成 `[REDACTED]`），改成只脱敏落库、展示用原文 |

### 技术选型自述（回答"是否成熟"）

- **不引入任何新依赖**，`uv.lock` 不动。
- 挂起只用 **LangGraph 官方 `interrupt()` / `Command(resume=...)`**——同一机制在本仓库的审批链路上已经跑通"暂停 → 重启 → 恢复"（`service.py:3028-3092`、`recovery.py`），不是新发明。
- 前端只用**既有的 TanStack Query mutation + SSE 快照**，不引入状态机库，不自建挂起态缓存。
- P2 的超时复用既有的看守线程口径（`service.py:830-856`），不自建定时器体系。

### 规范符合性自检（提交前）

- `CONTRIBUTING.md:23-39` 的三道门槛：`pytest` / `ruff check .` / `mypy src` 全绿；默认套件用假模型、不联网、不花钱。
- 新模块三类用例（正常 / 错误 / 安全边界）：缺 `ToolMessage` 的历史、重复提交、无人值守、resume 重放。
- `scripts/check_size.py --check`：新节点函数受 300 行 / 圈复杂度 20 约束，提问节点保持薄（只 `interrupt` + 组装 payload）。

---

## 10. 仍未核实 / 留给实现期

已核实的三项（2026-10-28）：

- **排队表**：复用 `deferred_tool_calls`，只改判据口径，见 §4.2。
- **取消与挂起**：已实测，结论与后果见 §9-D3（不是死锁，但任务索引与会话状态会脱节）。
- **视觉基线**：`frontend/e2e/__screenshots__/` 共 **103 张**，三个视口：`1280x800` 35、`1440x900` 35、`640x800` 33（记忆里的"54 张"已过时）。新增提问卡按 3 个场景（pending / answered / skipped）计，约 **7–9 张**新基线。

**任务中心联动（2026-10-28，假模型实证，不花钱）**：

- 数据层**本来就通**：提问挂起时任务索引那行是 `waiting_user`、`pending_count` +1（入口角标会亮）；
  答复后回到 `completed`、`pending_count` 归零；`job_waiting` / `job_progress` 事件照常广播。
- 真正的缺口是**文案**：`waiting_user` 的行内说明写"（审批）"、按钮写"打开会话做决定"，
  提问时两句话都说不通。已改为"（审批或回答提问）"与"打开会话处理"，并同步架构 §17.6、
  `JobWaitingEvent` 的描述（进 OpenAPI）与 3 张 `tasks` 基线。
- **明确不做**：不给任务行加"在等的是哪一种"字段——那要求任务索引存业务语义，而 §17.6 定的是
  "在等什么是状态的函数，服务端不猜"；点进会话就能看见是哪张卡。

**真实冒烟（2026-10-28）**：本机服务（deepseek-flash）跑了 4 次模型调用，覆盖
"提交 → 挂起 → 答复 → 完成"与"提交 → 挂起 → 跳过 → 完成"两条真实链路，均按设计工作；
挂起时 `POST /messages` 返回 `awaiting_input`，答复后整轮继续到 `completed`。

冒烟当场抓到一个单测照不到的缺陷：`application/jobs.py` 的 `_STATE_FOR_RESULT` 少了
`awaiting_input`，同步提交在收尾处 `KeyError` → **HTTP 500**（图其实已挂起，页面却收到错误）。
单测的 harness 不装任务运行时（`_jobs is None` 时直接 `run()`），所以这条路径一直空着。
已补映射，并加一条"命令结局 → 任务状态必须齐全"的测试守住。

仍未核实：

- 长会话里答复永久留在 `messages` 的 token 成本；是否在 `compression_node` 特殊处理，未评估。
- `e2e` 基线是否还有"只跑部分视口"的约定（`640x800` 少 2 张 bottom 场景，原因未查）。

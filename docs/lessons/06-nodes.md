# L6 · 编排主干：`nodes.py`

> L5 讲了流程的**形状**（谁连着谁），这一篇讲流程里的**内容**——14 个节点各自在干什么。
>
> 读完你应该能回答：**"模型说'任务完成了'，系统凭什么信？""工具执行到一半进程崩了，重启后会不会重复删文件？""为什么闲聊模式连工具都不绑定？"**

文件：[nodes.py](../../local-agent-web/src/local_agent/graph/nodes.py)，**3470 行**。这是全项目最大的单个文件。

---

## 0. 先说读法：不要从头读到尾

3470 行里，14 个节点函数只占约 **1600 行**（平均 114 行），剩下的是**60 多个辅助函数和常量**。分布大致是：

| 区段 | 内容 | 行数 |
|---|---|---|
| L141-171 | 14 个节点名常量 + 3 个回复模板常量 | ~30 |
| L177-470 | 开场四节点 + 记忆库缓存 + 召回合并 | ~290 |
| L473-570 | 系统提示与窗口的 5 个辅助函数 | ~100 |
| L573-816 | 压缩、闲聊、规划三节点 + 拆步 | ~240 |
| L819-1320 | 决策、安全门、工具执行（**最重的三块**） | ~500 |
| L1323-1558 | **反思节点**（单函数 236 行） | ~236 |
| L1561-2275 | 反思与合成的辅助函数（机械压制、计划推进、证据渲染） | ~700 |
| L2282-2617 | 闲聊记忆的审核与收尾文案 | ~330 |
| L2620-2910 | 记忆写入节点 + 完成判定 | ~290 |
| L2914-2996 | 召回失败兜底、最终回答节点 | ~80 |
| L3002-3164 | 审批节点 + 审批辅助函数 | ~160 |
| L3167-3470 | 作用域、决策/错误构造、副作用追踪、工具调用 | ~300 |

**推荐的读法**（后面每一节都按这个来）：

1. 先读**节点函数的 docstring**——这个项目的 docstring 写得极其详细，很多设计决策和踩过的坑直接写在里面。
2. 再看它**读哪些 State 字段、写哪些 State 字段**（L4 §7.1 那张表）。
3. 中间的逻辑当黑盒，需要时再往下钻。
4. **辅助函数只在被节点调用时才看**，不要单独通读。

---

## 1. 文件里的三类函数

理解 `nodes.py` 的关键是分清这三类东西：

| 类型 | 命名 | 例子 | 特点 |
|---|---|---|---|
| **节点函数** | `xxx_node` 或 `xxx`（14 个） | `prepare_context_node`、`chat_node`、`last_ai_text` | 第一个参数是 `state`，返回 State 更新字典。**只有这 14 个会被 `builder.py` 注册** |
| **辅助函数** | `_xxx`（约 50 个） | `_system_prompt`、`_advance_plan`、`_merge_references` | 纯逻辑，不返回 State 更新，可单独测试 |
| **构造器** | `_decision` / `_error` | 见 §4.5 | 造 `DecisionRecord` / `ErrorEvent`，几乎所有节点都用 |

**注意一个命名细节**：有 3 个**公开**辅助函数不带下划线——`medium_term_store`、`long_term_store`、`current_task_scope`、`unconfirmed_calls`、`stale_side_effects`、`last_ai_text`。

**为什么公开？** 因为它们要被**别的模块**用（测试、`web.py`、其他包）。带下划线的只在文件内用。这是 Python 的约定：**下划线 = 文件私有**，不是 Java 的 `private`（Python 没有真正的访问控制，全靠约定 + 工具检查）。

---

## 2. 开场四节点：一轮的"前置条件"

```text
prepare_context → load_agents → recall_memory → (mode_router)
                                    ↑ tool 分支才有
                              retrieve_code_node
```

### 2.1 `prepare_context_node`（L177-216，40 行）

全图**第一个**执行的节点，也是最能体现"一次提交 = 一个任务"的地方。

它返回的字典里有 **17 个字段**（[L194-215](../../local-agent-web/src/local_agent/graph/nodes.py#L194-L215)），分成三组：

```python
update: AgentState = {
    # ① 标识：缺失就现造
    "thread_id":    state.get("thread_id") or uuid4().hex,
    "session_id":   state.get("session_id") or uuid4().hex,
    "run_id":       uuid4().hex,              # ← 每轮都新建，不复用
    "normalized_request": user_input.strip(),
    "task_status":  "running",
    # ② "本轮"字段：覆盖语义，新一轮必须回到初始值
    "task_goal": None,
    "plan": [],
    "current_step_index": 0,
    "pending_tool_calls": [],
    "pending_approval": None,
    "reflection_result": None,
    "retry_count": 0,
    "loop_count": 0,
    "memory_refs": [],
    "rag_refs": [],
    "final_response": None,
    "final_evidence": [],
}
if user_input.strip():
    update["messages"] = [HumanMessage(content=user_input)]
```

**第 ② 组那 12 行是这一节的重点。** 注释只写了一句（[L200](../../local-agent-web/src/local_agent/graph/nodes.py#L200)）：*"以下都是'本轮'字段（覆盖语义），新一轮必须回到初始值"*。但 docstring 交代了**不这么写会怎样**（[L184-186](../../local-agent-web/src/local_agent/graph/nodes.py#L184-L186)）：

> **一次提交 = 一个任务**：本节点同时把上一轮的"本轮作用域"字段全部复位（循环计数、任务目标、计划、待执行调用、挂起审批、反思结果等），否则多轮工具任务会**累计到 `agent.max_tool_loops`**，新任务还会**带着旧目标与旧审批继续跑**。

两个具体的 bug：

- **`loop_count` 不复位** → 用户第二轮问个新问题，`loop_count` 还是上一轮的 5，直接撞上循环上限被拦。
- **`pending_approval` 不复位** → 上一轮挂起的审批会带到新任务，用户可能批准一个**已经无关的动作**。

**第 ③ 个细节：`messages` 是条件写入**（[L214-215](../../local-agent-web/src/local_agent/graph/nodes.py#L214-L215)）——空输入不产生消息。这是为了让"用户直接回车"不污染历史。

**第 ④ 个细节，也是最重要的：为什么追加型字段不在这里清理？**

docstring 最后一句（[L189-191](../../local-agent-web/src/local_agent/graph/nodes.py#L189-L191)）：

> 追加型字段（`messages`、`tool_events`、`tool_results`、`decision_trace`、`approval_history`、`error_events`、`completed_steps`）属于**历史审计记录**，不在这里清理。

**判据很清晰**：

| 字段类别 | 例子 | 新一轮怎么办 | 为什么 |
|---|---|---|---|
| "本轮作用域" | `plan`、`loop_count`、`pending_approval` | **复位** | 新任务不该受旧任务影响 |
| "历史审计记录" | `messages`、`tool_events`、`decision_trace` | **保留** | 这是会话历史，删了就没法审计和恢复 |

**Java 类比**：像 `HttpServletRequest` 的 attributes（每次请求清空）与 Session（跨请求保留）的区别。**放错一边就会出 bug，而且症状很隐蔽。**

**最后一个关键点**（[L188-189](../../local-agent-web/src/local_agent/graph/nodes.py#L188-L189)）：

> 注意本节点只在**新请求**时执行：审批恢复（`Command(resume=...)`）从断点继续，**不会重跑这里**，所以复位不会打断同一任务的后续步骤。

这是 Checkpoint 恢复的一个直接后果：**恢复时不从 `START` 走**，而是从挂起的那个节点继续。所以 `prepare_context` 的复位**不会**在审批恢复时把任务状态清掉。

### 2.2 `load_agents_node`（L219-232，14 行）——唯一不写 State 的节点

```python
def load_agents_node(state, runtime) -> AgentState:
    """加载并合并 AGENTS 规则，写入 RuntimeContext（架构 §4.1、§7.1）。

    规则文本不放进 State：它属于单次运行的临时上下文，写入 Checkpoint 没有意义，
    也会让每轮都重复保存同一份规则。空结果写为 ``None``。
    """
    context = runtime.context
    loaded = load_agents(...)
    context.agents_context = loaded.text or None      # ← 写 RuntimeContext
    return {}                                          # ← 不写 State
```

三个要点：

1. **`return {}`** —— 空字典是合法的"无更新"（L5 §6.3 也见过）。这个节点**一个 State 字段都不碰**。
2. **写 `context.agents_context`** —— 它是 `RuntimeContext` 的字段（L4 §6.1 提过 `RuntimeContext` 没有 `frozen=True`，就是为这个）。所以 L4 说它是"可变"的，这里看到了具体用途。
3. **理由写在 docstring 里**：规则文本是"单次运行的临时上下文"。写进 Checkpoint 有两个坏处——没意义（每次运行都要重算）+ **每轮重复保存同一份规则**（浪费存储）。

**这是一条很好的判据**：一个数据该进 State 还是 RuntimeContext？问"**它在一次调用内会变吗**"。

- 一次调用内不变（AGENTS 规则、模型客户端、工作区路径）→ RuntimeContext
- 一次调用内会被多个节点改（计划、工具结果、循环计数）→ State

### 2.3 `recall_memory_node`（L331-397，67 行）——"互不牵连"

三层记忆里**中期（Chroma 向量）和长期（SQLite）在这里汇合**。docstring 的核心是四个字：**互不牵连**（[L333-340](../../local-agent-web/src/local_agent/graph/nodes.py#L333-L340)）：

> - 中期（Chroma 向量）与长期（SQLite 结构化）**各自判断启用、各自捕获异常**：一层打不开只丢这一层的结果并记一条错误事件，另一层照常召回；
> - 两层都关闭时**直接透传**：不打开任何库、不加载 embedding 模型；
> - 结果按 `memory_id` 去重、按分数倒序、限制总条数后写进 `memory_refs`。

**为什么"各自捕获异常"很重要？** 看它捕获的异常类型就知道分层的边界：

| 层 | 捕获 | 位置 |
|---|---|---|
| 中期 | `EmbeddingError` / `MemoryStoreError` | [L364](../../local-agent-web/src/local_agent/graph/nodes.py#L364) |
| 长期 | `MemoryStoreError` / `sqlite3.Error` / `OSError` | [L386](../../local-agent-web/src/local_agent/graph/nodes.py#L386) |

中期会抛 `EmbeddingError`（模型加载失败），长期不会——**异常类型本身就是分层的证据**。

**"两层都关时直接 `return {}`"**（[L346-347](../../local-agent-web/src/local_agent/graph/nodes.py#L346-L347)）是个性能决策：中期库要**加载 embedding 模型**（L3 §5.4 讲过"启动慢"），不关的话每次调用都要加载一次。所以：

```python
if not medium_enabled and not long_enabled:
    return {}                    # 一行都不多走
```

**`_merge_references`（L400-416）的排序口径值得单独记**（docstring [L402-405](../../local-agent-web/src/local_agent/graph/nodes.py#L402-L405)）：

> 两类分数量纲不同…**只做同一列表内排序，不把它们当成可比量互相校准**。

中期是向量相似度、长期是别的打分方式，**量纲不同**。合并时按 `(-score, memory_id)` 排序只是"各自内部有序"，**不能理解为"中期的 0.9 一定比长期的 0.8 更相关"**。

**Java 类比**：把两个不同来源的 `relevance` 直接比较，就像把"百分比"和"绝对分"混在一起排序——数字能排，但意义是假的。

### 2.4 `retrieve_code_node`（L419-470，52 行）——三个"不"

这个节点有三条容易忽略的规矩，docstring 全写了（[L421-432](../../local-agent-web/src/local_agent/graph/nodes.py#L421-L432)）：

**① `rag.enabled=false` 时什么都不做**

> 不访问文件系统、不探测 `rg`、不加载任何解析器，**也不记错误事件**，只写空的 `rag_refs` 透传。

注意"也不记错误事件"——**"功能关闭"不是"失败"**。这和第 2.5 节的"失败 vs 没命中"是同一类区分。

**② 查询素材只取当前轮的输入，绝不读 `task_goal`**

原因在注释里（[L426-428](../../local-agent-web/src/local_agent/graph/nodes.py#L426-L428)）：

> 这个节点在 `planner_node` **之前**，`task_goal` 此刻还没生成（`prepare_context` 每轮把它复位为 `None`），更不能读上一轮遗留的目标。

**这是"节点顺序决定可用字段"的一个实例**：`retrieve_code_node` 在 `planner_node` 之前（L5 的图里可以看到），所以它**只能**用 `normalized_request`。如果它去读 `task_goal`，读到的永远是 `None`（或更糟——上一轮的残留）。

**③ "检索失败"和"检索了但没命中"是两回事**

> 前者记一条可见错误事件，Prompt 里也用**不同的占位文字**。

因为这两件事对模型的意义完全不同：

- **没命中** → "工作区里没有相关内容" → 模型应该直接回答或问用户
- **失败**（rg 崩了、权限不够）→ "我没能搜" → 模型应该知道信息不完整

如果都写成"没找到"，模型会把"工具坏了"当成"确实没有"，然后基于不完整信息给出结论。

**还有一个防御性设计**（[L438-442](../../local-agent-web/src/local_agent/graph/nodes.py#L438-L442)）：

```python
# 正常路径下 context 一定带 security_policy；这里缺了就现建一个，
# 不能因为"没有策略"就跳过边界检查去扫文件
```

**安全边界不能因为"对象缺失"而失效**——缺失时要现造一个，而不是"没有策略就不检查"。这是 L7 会反复出现的原则。

---

## 3. 预算与规划：`compression_node` / `planner_node`

### 3.1 `compression_node`（L573-718，146 行）——只压缩"送模型的窗口"

这是全文件最"抠细节"的节点之一。docstring 里最该记住的一句（[L575-577](../../local-agent-web/src/local_agent/graph/nodes.py#L575-L577)）：

> **只压缩送模型的窗口：Checkpoint 里的 `messages` 一条都不删**（架构 §10.1）。

**这句话解决了一个容易搞混的问题**："上下文压缩"到底压的是什么？

| 位置 | 压不压 | 为什么 |
|---|---|---|
| Checkpoint 里的 `messages` | **不压** | 审计与恢复用的原始记录（L4 §2.7 的 `reply_overrides` 同源理由） |
| 送模型的窗口 | **压** | 省 token |
| 页面展示 | 用投影 | 原文留存、读时替换 |

**三个副本，三种用途。** 你在 `nodes.py` 里会反复看到 `_prompt_window` 这个辅助函数（[L473-497](../../local-agent-web/src/local_agent/graph/nodes.py#L473-L497)），它就是"生成送模型的那一份"。

**触发口径**（[L579-583](../../local-agent-web/src/local_agent/graph/nodes.py#L579-L583)）：

```text
estimated_prompt_tokens / (context_window_tokens - max_tokens) >= memory.compression.trigger_ratio
```

注意分子"覆盖实际出站的**完整** Prompt"——包括三部分：

1. 系统提示（含 AGENTS、会话摘要、召回记忆）
2. 消息窗口
3. **工具模式下绑定给模型的工具定义**（`_tool_definition_tokens`，[L508-515](../../local-agent-web/src/local_agent/graph/nodes.py#L508-L515)）

**第 3 项最容易漏**：工具定义也是 Prompt 的一部分，会真的占 token。工具多的时候（L8 会讲有 6 个工具）这一块不小。**估算口径必须和实际出站一致**，否则预算永远算不准。

**`covered` 边界：防重复摘要**

摘要成功后，把"覆盖到哪条消息"记进 `context_budget.summary_covered_messages`（[L692-718](../../local-agent-web/src/local_agent/graph/nodes.py#L692-L718)）。注释解释了为什么（[L658-659](../../local-agent-web/src/local_agent/graph/nodes.py#L658-L659)）：

> 既不重复摘要同一批历史，也**不会出现"既没进窗口、也没进摘要"的消息**。

**这两句话是两个不同的 bug**：

- 没有边界 → 每轮都摘要同一批历史，**摘要越滚越长**
- 边界算错 → 有一段消息既被裁掉又没进摘要，**永久丢失**

**降级的四种情况**（docstring [L585-593](../../local-agent-web/src/local_agent/graph/nodes.py#L585-L593)）：

| 情况 | 处理 |
|---|---|
| `compression.enabled=false` | 只统计预算，不裁剪、不摘要 |
| 没有"更早的旧消息"（典型是第一轮） | **静默跳过**，不记错误事件、不改计数 |
| 摘要生成失败 | **只记 ErrorEvent**，不更新摘要/版本/计数，本轮继续用裁剪后的窗口 |
| 窗口裁剪 | 按**完整轮次**裁，不切开工具调用对 |

**第三行是"失败要可见"的实现**：摘要失败不阻断本轮，但必须留下痕迹（`error_events`）。

**第四行呼应 L1 §2**：一个"工具步"是 `AIMessage(tool_calls)` + `ToolMessage` 的组合，从中间切开会让模型看到残缺序列。

**一个很容易忽略的细节**（[L669-672](../../local-agent-web/src/local_agent/graph/nodes.py#L669-L672)）：

> 摘要输入也必须**投影**，否则"被纠正的那句'我记住了'"会进摘要，再经 `conversation_summary` 影响**之后每一轮的 Prompt**。

**这是个"污染放大"的 bug**：投影只用在"当前展示"上还不够——如果不投影摘要输入，假承诺会被固化进摘要，然后每轮都带上。**修法：所有"送模型"的路径都要投影。**

### 3.2 `planner_node`（L762-804，43 行）——计划只在"没有计划"时生成

```python
if not plan:
    steps = _draft_steps(context, goal)      # 请模型拆步
    plan = [PlanStep(id=f"{run_id}:step-{i+1}", ..., status=...) for ...]
    # 第一步立刻 running，其余 pending
```

**这一句 `if not plan` 是全部的重点**（docstring [L768-771](../../local-agent-web/src/local_agent/graph/nodes.py#L768-L771)）：

> 计划只在**没有计划**时生成：同一任务内的后续轮次沿用已有计划，**重启恢复（重入）也不会重新拆步**，所以已完成步骤不会被新计划覆盖。

**为什么必须这样？** 因为 L5 §7.2 讲过 `completion_router --continue--> planner_node` —— **规划节点在环里**，一个多步任务会反复经过它。如果不加这个判断：

```text
第 1 次经过：拆成 3 步，第 1 步已完成
第 2 次经过：重新拆成 3 步 → 第 1 步变回 pending → 又做一遍 → 死循环
```

**这就是 L4 §7.1 说"`planner_node` 是 `max_loop_count` 的写入方"的原因**：上限只在真正规划时设定一次。

**`id` 里的 `run_id`**（[L781](../../local-agent-web/src/local_agent/graph/nodes.py#L781)）：

```python
id=f"{run_id}:step-{i+1}"        # 形如 "a1b2c3:step-1"
```

docstring 解释了为什么（[L770-771](../../local-agent-web/src/local_agent/graph/nodes.py#L770-L771)）：

> 步骤 id 形如 `<run_id>:step-N`，`completed_steps` 里留下的**历史任务步骤因此不会让新任务误判"这一步已完成"**。

**这是 L4 §3.1 / §4.3 那个"防串味"主题的第三次出现**：`tool_scope_key` 防跨轮次串味、`PlanStep.retry_count` 防跨步骤串味、这里防跨任务串味。**三个地方防的是同一件事。**

**拆步失败是"合法退化"**（[L807-816](../../local-agent-web/src/local_agent/graph/nodes.py#L807-L816)）：

```python
def _draft_steps(context, goal) -> tuple[str, ...]:
    """拆步失败**合法退化**为单步（只进决策轨迹，不记错误事件）。"""
```

**"合法退化"和"失败"的区别**：模型不会拆步不是错误，是能力边界。所以只记决策轨迹（供审计），不记错误事件（那是给"真出问题了"用的）。

---

## 4. 决策 → 安全 → 执行：最重的三块

### 4.1 `agent_decision_node`（L819-856，38 行）——模型提出，不代表会执行

docstring 只有两句（[L820-823](../../local-agent-web/src/local_agent/graph/nodes.py#L820-L823)）：

> 决策节点：把当前模式允许的工具绑定给模型，取回工具调用或回答。
>
> 工具调用由模型提出，**不代表会被执行**：是否放行由 `safety_gate_node` 判定。

**第二句是整个安全设计的核心。** 模型输出 `tool_calls` 只是**"申请"**，真正的"批准"在下一个节点。

流程（[L826-856](../../local-agent-web/src/local_agent/graph/nodes.py#L826-L856)）：

```text
① registry 缺失 → 抛 LocalAgentError          （不能没注册表还往下走）
② 取当前模式 + Profile 的工具列表 registry.tools_for(mode, capabilities)（T50 起）
③ 有工具但模型不支持 Tool Calling → 失败闭合 _tool_calling_unavailable
④ 有工具 → bind_tools(tools)；没工具 → 裸模型
⑤ model.invoke(...)
⑥ 结果不是 AIMessage → 抛 LocalAgentError      （类型守卫）
⑦ 写回 messages / pending_tool_calls / loop_count+1 / decision_trace
```

**第 ③ 步的"失败闭合"值得单独讲**（[L831-833](../../local-agent-web/src/local_agent/graph/nodes.py#L831-L833)）：

```python
# 架构 §11.7：不支持 Tool Calling 时不得静默退化成闲聊，要明确告知
```

**为什么不"降级成闲聊"？** 因为用户要的是"帮我改文件"，你说"我不会用工具，那我们聊聊天吧"——**用户会以为任务在做，实际什么都没发生**。

正确做法是明确告知"当前模型不支持工具调用"。**降级必须让用户知道**——这和 L5 §9 的"成本类可以 fail-open、安全类必须 fail-closed"是同一族原则：**降级的可见性不能省。**

**第 ⑦ 步的 `loop_count + 1`** 是循环上限的计数点。所以 `loop_count` 的语义是"**模型被调用的次数**"，不是"反思轮数"。

### 4.2 `safety_gate_node`（L879-1075，197 行）——把"待执行"收敛成覆盖语义

docstring 有一句关键的设计说明（[L881-884](../../local-agent-web/src/local_agent/graph/nodes.py#L881-L884)）：

> 被拒绝的调用立刻回填 `ToolMessage` 与工具事件，让模型看到拒绝原因；`pending_tool_calls` 是**覆盖语义**，这里把它收敛为"仍需执行"的调用，这样**后续条件边无须再访问策略对象**即可判断有没有要执行的动作。

**这句话解释了 L5 §5.3 的一个疑问**：`route_after_safety` 只读 `state.get("pending_tool_calls")`，为什么不用再查一遍策略？

因为**安全门已经把它收敛好了**：

```text
进入安全门：pending_tool_calls = [A, B, C]     ← 模型提出的全部
出去后：    pending_tool_calls = [B]           ← 只剩"已放行、等着执行"的
            A → 回填了拒绝的 ToolMessage
            C → 变成了 pending_approval
```

**所以"待执行列表"的语义是"已经通过安全审查的动作"**——条件边只要看它空不空，就知道有没有活要干。

**这是个很好的分层实践**：把"判断"集中在安全门，把"结果"用覆盖语义暴露给路由。路由保持成一个纯读 State 的函数（L5 §5.3 强调过它们可以单独测）。

**"只在确实要审批时才算指纹"**（[L903-906](../../local-agent-web/src/local_agent/graph/nodes.py#L903-L906)）：

```python
# T14C 复审：指纹只在确实要进审批时才算……对用户主目录这类目标会把页面卡死
```

**指纹计算（`action_fingerprint`，L7 会细讲）要读文件、算 Hash、扫目录树。** 如果对**每一个**工具调用都算，那"列出用户主目录"这种硬拒绝的动作会先被递归扫描一遍——**页面直接卡死**。

**所以判断顺序是**：先分类（要不要审批）→ 只有"要审批"的才算指纹。

**同一回复里多个审批动作只提交第一个**（[L950-992](../../local-agent-web/src/local_agent/graph/nodes.py#L950-L992)）：

```python
# 否则模型会看到"有 tool_call 却没有 ToolMessage"的残缺序列
```

**这是 L1 §2 那个"工具步不能切开"规则的应用**：一个 `AIMessage` 里的 3 个 `tool_calls`，如果只给 1 个 `ToolMessage`，序列就是残缺的。所以**其余两个要回填"等下一轮审批"的 `ToolMessage`**，让模型知道它们没被丢弃、只是排队。

**放行标记写在安全门结束时**（[L995-996](../../local-agent-web/src/local_agent/graph/nodes.py#L995-L996)）：

> 放行标记在本节点结束时就写入 Checkpoint……恢复时能识别出"已放行但结果未知"。

这句话是 §4.3 那个"副作用追踪"的前置条件。**先记"我放行了"，再去执行**——如果执行中崩溃，重启时能看到"放行了但没结果"。

### 4.3 `tool_execution_node`（L1078-1320，243 行）——最难的幂等设计

docstring 很短（[L1079-1081](../../local-agent-web/src/local_agent/graph/nodes.py#L1079-L1081)）：

> 执行放行的工具调用，并把结果回填成 `ToolMessage`（架构 §6）。
>
> 工具失败不中断图：异常被转换成给模型看的说明，同时记录工具事件与结果。

真正的难点在**副作用的重放保护**，这条链有四环：

#### 第一环：`(run_id, tool_call_id)` 判"已执行过"

```python
confirmed = {...}   # 本次运行已确认的调用
if (run_id, call_id) in confirmed:
    continue        # 跳过重复执行
```

**为什么键要带 `run_id`？** L4 §3.1 讲过：模型经常在不同轮次给出相同的 `tool_call_id`（每轮都从 `call_1` 开始编号）。只按 `tool_call_id` 去重，第二轮会被误判成"上一轮执行过"而**静默跳过**。

#### 第二环：`stale_side_effects` —— 检查必须在**执行前**

这是最值得琢磨的一环。注释写得很明确（[L1103-1105](../../local-agent-web/src/local_agent/graph/nodes.py#L1103-L1105)）：

> 检查放在这里而不是反思里：反思发生时**副作用已经又跑过一次了**……按**工具名**匹配而不是动作指纹——副作用可能已经改过目标文件，指纹必然已经变了。

**两个判断，都反直觉，都有理由**：

**① 为什么不在反思节点检查？**

因为图的顺序是 `tool_execution → reflection`。等反思跑的时候，**工具已经执行完了**。要在执行**之前**拦住，就必须在 `tool_execution_node` 的开头。

**② 为什么按工具名匹配，而不是按动作指纹？**

因为**指纹已经变了**。假设上一次运行执行了"删掉 `a.txt`"但结果未知（崩了）。这次模型又提出"删掉 `a.txt`"：

- 按**指纹**匹配：如果文件真被删了，这次算出的指纹（含文件 Hash）**和上次不同** → 匹配不上 → 直接执行 → **重复删除**（这次会报"文件不存在"，但如果是 `rm -rf` 这种就更糟）
- 按**工具名**匹配：只要是 `delete_file`，就拦住提醒 → **保守**

**这是"指纹在某些场景下不可靠"的一个实例。** 指纹的价值在于"同一个动作"的判定，但当**动作本身会改变指纹的输入**时，指纹就自相矛盾了。

#### 第三环：批准后目标变了 → 批准失效

`_approval_target_changed`（[L3167-3189](../../local-agent-web/src/local_agent/graph/nodes.py#L3167-L3189)）——用户批准的是"删 `a.txt`"，但等到执行时 `a.txt` 已经变了。**批准的语义是"批准这个具体动作"，动作变了批准就无效。**

**Java 类比**：乐观锁。你读的时候 version=5，提交的时候发现 version=7 了——**拒绝提交**，而不是"反正你批准过"。

#### 第四环：`grant` / `revoke` 一次性授权

```python
policy.grant(...)          # 执行前授权
try:
    ...执行...
finally:
    policy.revoke(...)     # 无论成败都立即撤销
```

**`finally` 里的 `revoke` 是关键**：授权是**一次性的**，用完立刻收回。这样即使执行抛异常，授权也不会泄漏给后续调用。

**Java 类比**：`try { lock.lock(); ... } finally { lock.unlock(); }`。

#### 防永久阻断：`SIDE_EFFECT_RECHECK_STAGE`

最后一环是"不要过度保护"。注释（[L1290-1291](../../local-agent-web/src/local_agent/graph/nodes.py#L1290-L1291)）：

> 没有这一步，一次异常结束会让**之后每一次运行都拒绝执行副作用**（永久阻断，用户没有任何解除手段）。

**这是一个"安全机制反噬"的经典案例**：为了保护用户而加的"未确认副作用"拦截，如果没有"已提醒过"的标记，就会变成**永久瘫痪**——因为那条"未确认"的记录永远在 State 里。

修法：对每个遗留项写一条 `side_effect_recheck` 决策记录（[L3290-3292](../../local-agent-web/src/local_agent/graph/nodes.py#L3290-L3292)），**提醒过一次就不再重复拦**。

**给你的经验**：任何"基于历史状态拒绝操作"的机制，都必须有一个**退出路径**。否则一次异常 = 永久不可用。

### 4.4 `reflection_node`（L1323-1558，236 行）——三层顺序不能颠倒

这是全文件**最长的节点函数**，也是最核心的。docstring 直接给出三层结构（[L1324-1338](../../local-agent-web/src/local_agent/graph/nodes.py#L1324-L1338)）：

> 三层职责，**顺序不能颠倒**：
>
> 1. **机械校验**（`reflection/validators`）：证据是否存在、是否 `verified`、有哪些异常——`result_trust` **只由它得出**；
> 2. **LLM 决策**（`reflection/reflector`）：完成度、异常解释、计划调整与下一步动作；结构化输出失败时按机械结论兜底（`verified` → `finish`，否则 `ask_user`）并记一条 ErrorEvent，让"解析失败"可见；
> 3. **机械压制**：证据等级不是 `verified` 时 `goal_achieved` 一律压回 `False` 并改用 `ask_user`；`goal_achieved=False` 时不允许用 `finish` 收尾；重试超过 `agent.max_retries_per_step` 时同样改为 `ask_user`。规范化说明写进 `decision_trace`。

**"顺序不能颠倒"这五个字是整个项目的信任模型**：

```text
机械校验（代码算）  ──→  得出 result_trust        ← 模型碰不到
      ↓
LLM 决策（模型说）  ──→  得出 goal_achieved / next_action
      ↓
机械压制（代码改）  ──→  归一矛盾组合              ← 模型说了也不算
```

**`result_trust` 只由机械校验得出**——模型**无法**影响"证据是否可信"。这是"不信任模型的自我评价"的具体实现。

**三层压制各拦什么**（docstring 第 3 条）：

| 压制规则 | 拦住的矛盾 |
|---|---|
| 证据非 `verified` → `goal_achieved` 压回 `False`，改 `ask_user` | 模型说"完成了"但没有可验证的证据 |
| `goal_achieved=False` → 不许 `finish` | 模型自己都说没完成，却想收尾 |
| 重试超 `max_retries_per_step` → 改 `ask_user` | 同一个步骤反复失败还在重试 |

**三层拦的都是"模型自相矛盾"**。压制结果写进 `decision_trace`，所以**你能在审计里看到"模型本来想说什么、被改成了什么"**。

**三个实测踩过的坑，都写成了注释**：

**坑 1：证据只取前 N 个字符会让模型误判**（[L1380-1382](../../local-agent-web/src/local_agent/graph/nodes.py#L1380-L1382)）：

> 工具输出是 JSON（元数据在前、正文在后），只取前 N 个字符会让模型看到 `path` / `line_count` 就以为"**正文被截断、任务没完成**"（**实测因此连续空转并反过来问用户**）。

**这是个特别值得学的 bug**：截断本身没问题，问题是**截断的位置让模型误解了内容**。修法是"按可判读摘要渲染"（`tool_evidence_text`），截断时带明确标注。

**坑 2：按工具名取最后一次，而不是汇总全量**（[L1392-1395](../../local-agent-web/src/local_agent/graph/nodes.py#L1392-L1395)）：

> `tool_events` 覆盖整个任务，所以直接汇总全量会让**第二步的失败永久留在集合里**：即使重试或换了做法已经成功，"第二步失败、调整后成功"也**一辈子判不成完成**（复审实证）。
>
> 因此按**工具名**取最后一次结果——它表达的正是"后续尝试有没有修复这个工具的失败"。

**`latest_by_tool` 这个字典（[L1354](../../local-agent-web/src/local_agent/graph/nodes.py#L1354)、[L1368](../../local-agent-web/src/local_agent/graph/nodes.py#L1368)）就是干这个的。** 但注释特别强调（[L1395](../../local-agent-web/src/local_agent/graph/nodes.py#L1395)）：

> 注意只影响整体可信度：`final_evidence` 仍按 §4.2 **全量重算**，异常清单也照旧**全量记录**。

**"可信度取最后一次，证据和异常全量"**——这是个精细的区分：判定"现在可信吗"看最后一次，但**审计要看全部**。

**坑 3：`final_evidence` 按当前任务重算**（docstring [L1337-1338](../../local-agent-web/src/local_agent/graph/nodes.py#L1337-L1338)）：

> `final_evidence` 按架构 §4.2 从**当前任务**的工具事件重算后**整体覆盖**：历史任务的事件与错误不再混进本轮证据。

注意是**整体覆盖**（`final_evidence` 是覆盖语义，L4 §2 的 26 个之一），不是追加。所以每轮都重算一份，历史任务的东西不会混进来。

### 4.5 `_advance_plan`（L1772-1870，99 行）——三条硬语义

反思节点调它推进计划（[L1437](../../local-agent-web/src/local_agent/graph/nodes.py#L1437)）。docstring 标题就写着"**T20 的三条硬语义，复审定稿**"（[L1779-1791](../../local-agent-web/src/local_agent/graph/nodes.py#L1779-L1791)）：

**语义 1：完成一步 ≠ 完成整个任务**

> 当前步完成、但计划里还有未走完的步骤时，即使模型给了 `finish` 也**改成 `continue`**——这是机械规则，不接受"第一步做完就宣布收尾"。

代码（[L1812-1825](../../local-agent-web/src/local_agent/graph/nodes.py#L1812-L1825)）：

```python
if goal_achieved:
    if step.id not in known:
        completed = (step.id,)
    remaining = [item for item in plan[index + 1:] if item.status != "completed"]
    if remaining:
        action = "continue"      # ← 模型说的 finish 被覆盖
        achieved = False         # ← 且不算"任务完成"
        note = f"步骤 {index+1}「{step.description}」已完成，计划还有 {len(remaining)} 步，继续执行（完成一步不等于完成整个任务）"
```

**注意 `achieved = False` 那一行**——注释（[L1818](../../local-agent-web/src/local_agent/graph/nodes.py#L1818)）解释了：

> 计划没走完就不是"任务完成"：否则 `task_status` 会显示 `completed`，与"仍在继续"矛盾。

**这是 L5 §5.3 那个"半截任务"bug 的同族**：状态显示和实际行为必须一致。

**语义 2：重规划必须真的改计划**

> `plan_adjustment` 非空且当前步失败时，把该步标为 `failed`，并**在它之后插入一步**（描述即调整内容），`current_step_index` 指向新步。**只把调整写进 `result_summary` 不算重规划**——那样模型以为改了、执行仍按老计划走。

**"只写摘要不算重规划"**——这句话点出了一个很隐蔽的失败模式：模型说"我打算换个方法"，你把这句话记下来了，但**计划没变**，下一步还是老方法。**用户看到的和实际发生的脱节。**

**然后是一个"实测"出来的上限**（[L1830-1833](../../local-agent-web/src/local_agent/graph/nodes.py#L1830-L1833)）：

> 计划已达 `MAX_PLAN_STEPS` 时**不再插入**：改为原地按调整重做当前步骤并计一次重试……否则计划会**无限增长，任务永远收不了尾**（实测）。
>
> （实测真实模型把一句问句拆成 4 步后又连续插入 5 个调整步骤，一直"继续"到循环上限也走不完）

**这段注释很有价值**：它记录了真实模型的行为——**模型会不断"插入调整步骤"**。如果没有这个上限，`plan` 会无限长，`loop_limit` 每次都撞上，任务永远收不了尾。

**语义 3：已完成步骤不可重写、不可重跑**

> 只更新当前步及其之后；`completed_steps` 只追加、不清空。

**这条保证"进度不可逆"**。想想如果不加：模型说"我改主意了，重新做第 1 步"——那已经做完的工作白费，而且可能重复产生副作用。

---

## 5. 审批节点：`approval_interrupt_node`（L3008-3111）

单独一节，因为它是**全项目唯一会"挂起"的节点**，也是重放语义最微妙的地方。

### 5.1 四条注意事项（docstring 原文）

[L3011-3018](../../local-agent-web/src/local_agent/graph/nodes.py#L3011-L3018)：

> - 本节点在 resume 时会被**整体重放**，因此 `interrupt()` **之前不能有任何副作用**；
> - 递归删除需要**两次独立恢复**：第一次恢复无论传入什么都不构成授权，只有第二次独立的 `interrupt()` 返回"批准 + 显式二次确认"才授权（架构 §6.5）。**阶段不能用"决策内容"推断**，否则一次伪造的带标记恢复就能跳过二次确认；
> - 第一阶段选择拒绝 / 修改参数时立即按该决策结束，不再要求第二次确认；
> - 决策写入 `approval_history`，条件是"**同一次审批只记一次**"，重放不会重复追加。

### 5.2 代码结构（L3020-3038）

```python
del runtime                                    # ← 审批不访问运行上下文
request = state.get("pending_approval")
if not isinstance(request, Mapping):
    return {}                                  # ← 类型守卫

first = _as_mapping(interrupt(_approval_prompt(request, stage=1)))       # 第一次挂起
decision_input = first
if bool(request.get("requires_second_confirmation")) and (
    _normalize_choice(first.get("decision")) == "approve"
):
    # 第一次只负责推进阶段：必须由第二次独立恢复给出授权
    second = _as_mapping(interrupt(_approval_prompt(request, stage=2)))  # 第二次挂起
    if _normalize_choice(second.get("decision")) == "approve" and not _second_confirmed(second):
        decision_input = {**second, "decision": "reject"}                # 失败闭合
    else:
        decision_input = second
```

**四个值得学的点：**

**① `del runtime`** —— 一行显式声明"这个节点不用运行上下文"。**这是种"主动声明依赖"的写法**，比什么都不写更能表达意图。

**② 两次 `interrupt()` 是"两次独立的恢复"**

关键在于：**图会在每次 `interrupt()` 处分别挂起**。所以：

```text
第一次挂起 → 用户点"批准" → 图恢复 → 走到第二个 interrupt() → 又挂起
第二次挂起 → 用户勾"确认删除" → 图恢复 → 才真的授权
```

**为什么必须两次？** 因为"批准递归删除"是个不可逆的破坏性操作。两次独立确认**在机制上**排除了"一次误点"。

**③ "阶段不能用决策内容推断"** —— 这是防御性设计里最精妙的一句。

假设你用"第一次恢复的内容里有没有 `confirm_recursive` 标记"来判断阶段。那么**一次伪造的带标记恢复就能同时满足两次检查**，直接跳过二次确认。

用**独立的 `interrupt()` 调用**表示阶段，则阶段由**图的执行位置**决定，不受恢复数据影响。**这是"用控制流而不是数据来表达状态"的经典例子。**

**Java 类比**：你不会用"请求体里有没有 `step=2` 字段"来判断走到第几步，而是让框架的调用栈决定。数据可以被伪造，控制流不能。

**④ 第二次没勾确认 → 一律按拒绝**（[L3035-3036](../../local-agent-web/src/local_agent/graph/nodes.py#L3035-L3036)）：

```python
# 第二次没有显式勾选"确认删除"→ 一律按拒绝处理（失败闭合）
```

**又是 fail-closed。** L5 §9 汇总过这个原则，这是它最严肃的一次应用。

### 5.3 放行标记：一个复审发现的漏洞

[L3060-3073](../../local-agent-web/src/local_agent/graph/nodes.py#L3060-L3073) 是全文件最值得读的注释之一：

```python
if choice == "approve":
    # 人工批准也是一次**放行**，必须在这里留下放行标记：批准后走的是
    # approval_interrupt_node → tool_execution_node，**不经过安全门**，
    # 所以以前整条审批路径都没有放行标记——批准后执行中崩溃时无从判断
    # "副作用可能已经发生"，重启后会把同一个副作用再执行一次（复审实证）。
    records.append(_decision(state, stage="tool_start", ..., salt=approved_name))
```

**看懂这个 bug**：

```text
正常路径：  agent_decision → safety_gate（写放行标记）→ tool_execution
审批路径：  agent_decision → safety_gate → approval_interrupt（挂起）
                                    ↑ 这里只写了"要审批"，没写"放行"
            ... 用户批准 ...
            approval_interrupt → tool_execution     ← 跳过了 safety_gate！
```

**`safety_gate_node` 是写放行标记的地方**（§4.2 末尾），但**审批路径从 `approval_interrupt_node` 直接到 `tool_execution_node`，不经过安全门**（L5 的图里能看出来）。所以：

1. 用户批准 → 执行中崩溃 → **没有放行标记**
2. 重启 → 系统看不到"这个动作可能已经执行过"
3. → **把同一个副作用再执行一次**（比如再删一次文件、再发一次请求）

**修法**：在审批节点里补一条 `stage="tool_start"` 的记录。

**这个 bug 的教训**：**"所有路径都经过 X"这个假设必须验证**。加一条边（`approval_interrupt → tool_execution`）就绕过了原本"唯一"的标记点。L5 §10.3 讲的"入边/出边表"就是验证这类假设的工具。

**注意 `salt=approved_name`**（[L3071](../../local-agent-web/src/local_agent/graph/nodes.py#L3071)）——L4 §3.2 讲过：没有 `salt` 时同轮多条记录会 ID 撞车被 `merge_appended` **静默丢弃**。这里必须区分"审批决策"和"放行标记"两条记录。

### 5.4 恢复时"只替换被审批的那一个调用"

[L3093-3102](../../local-agent-web/src/local_agent/graph/nodes.py#L3093-L3102)：

```python
# 只替换被审批的这一个调用：同批次里已经放行的免审调用必须留在待执行列表里，
# 否则它们既不会执行、也没有 ToolMessage，模型会收到"有调用无结果"的消息。
```

**场景**：模型一次提出两个调用——`read_file`（免审）和 `delete_file`（要审批）。

```text
安全门后：pending_tool_calls = [read_file]      ← 免审的留着
          pending_approval   = delete_file      ← 要审批的移出去
```

如果审批恢复时**整个替换** `pending_tool_calls = [delete_file]`，那 `read_file` **永远不会执行，也没有 `ToolMessage`** → 模型看到残缺序列（L1 §2 的规则又出现了）。

---

## 6. 收尾三节点：`memory_write_node` / `chat_node` / `final_response_node`

### 6.1 `memory_write_node`（L2620-2801，182 行）——五条硬语义

docstring 标题写着"**四条硬语义（复审定稿）**"，但正文列了 5 条（[L2624-2641](../../local-agent-web/src/local_agent/graph/nodes.py#L2624-L2641)）。逐条看：

**第 1 条：两层各自打开、各自降级，且"启用但打不开"≠"配置关闭"**

> "**启用但打不开**"必须与"配置关闭"区分开——前者按 `failed` 处理，后者才是 `disabled`。

**为什么要区分？** 因为它们对用户的意义完全不同：

| 情况 | 用户该知道什么 |
|---|---|
| 配置关闭 | "我没打算用记忆" —— 正常 |
| 启用但打不开 | **"我本来要记，但没记住"** —— 必须告知 |

注释里有个复审实证（[L2712](../../local-agent-web/src/local_agent/graph/nodes.py#L2712)）：

> 混为一谈会让**零写入的候选也登记完成**（复审实证：长期库打不开时仍标成完成）。

**这是"把失败伪装成成功"的典型**——库打不开，但候选被登记成"已处理"，于是**永远不会重试**。

**第 2 条：按候选判定完成，要求**双重条件**

> 只有该候选的**所有**目标都 `satisfied` **且至少有一个目标真正落库**（`written` / `duplicate` / `conflict`）才登记进 `memory_write_candidates`；**部分成功或零写入都不登记**，下一轮还能补写。

**"至少有一个真正落库"这个附加条件就是修上面的 bug**（[L2633-2634](../../local-agent-web/src/local_agent/graph/nodes.py#L2633-L2634)）：

> 复审实证：只按 satisfied 判会让"库打不开"的候选以零写入登记完成。

**"不登记"意味着"下轮还能补写"**——这是个**可重试**的设计：宁可多试几次，也不能假装成功。

**第 3 条：真实失败写错误事件，`salt` 防 ID 撞车**

> `kind == "failed"` 的结果进 `error_events`，未启用与幂等跳过只进决策轨迹。事件 ID 带 `salt`，同一轮的多条记录不会因 ID 相同被 `merge_appended` 丢掉。

**这里的三分类值得记**：

| 结果 | 去哪 | 为什么 |
|---|---|---|
| `failed` | `error_events` | 真出问题了 |
| 未启用 | 只进 `decision_trace` | 不是错误 |
| 幂等跳过（已存在） | 只进 `decision_trace` | 正常行为 |

**第 4 条：闲聊必须说实话**

> `mode == "chat"` 时把写入结果交给 `_chat_memory_final_text` 生成**展示文本**（写进 `final_response`）——全部成功才说"已记住"，有未保存必须如实说明；**`messages` 保持原始记录不动**（架构 §10.1：删改历史会破坏审计与"原文恢复"，复审实证）。

**这就是 L4 §2.7 那个 `reply_overrides` 的用途现场**：

- `messages` 里模型说的"我记住了"**原样保留**（审计要）
- `final_response` 里换成**真实结果**（"只保存了 2 条"）
- 下一轮送模型时用投影修正后的文本

**第 5 条：同一轮内重复候选只处理一次**

> 按写入判重键去重，否则中期稳定 id 只会留下一条，**确认文案却会报"保存 N 条"**。

**症状很有意思**：实际只存了 1 条，但文案说"已保存 3 条"。**因为去重发生在写库层（稳定 id 相同会合并），但计数发生在别处。**

**这类 bug 的通用形态**：**"我处理了几个"和"实际存了几个"用两套计数**。修法是**在进入处理前就去重**（`_distinct_candidates`，[L2804-2819](../../local-agent-web/src/local_agent/graph/nodes.py#L2804-L2819)）。

### 6.2 `chat_node`（L721-756，36 行）——不绑定工具

docstring（[L722-726](../../local-agent-web/src/local_agent/graph/nodes.py#L722-L726)）：

> 架构 §1.3-3 要求闲聊模式**不注册文件与 Shell 工具**，因此本节点**不调用 `bind_tools`**；模型实例来自 RuntimeContext（架构 §4.1），不在图内持有。

**这是"安全靠结构而不是靠 Prompt"的又一个实例**：闲聊模式不给模型工具，**它连"提出工具调用"的能力都没有**。

**Java 类比**：不是"告诉调用方别调这个方法"，而是**这个类上根本没有这个方法**。

**它用 `stream_answer` 而不是 `model.invoke`**（[L734-739](../../local-agent-web/src/local_agent/graph/nodes.py#L734-L739)），因为闲聊回答可以直接流式展示给用户。注释（[L733](../../local-agent-web/src/local_agent/graph/nodes.py#L733)）：

> 闲聊回答就是"最终回答"之一：开了流式就增量展示（**不逐块写 Checkpoint**，见 `ui.streaming`）。

**"不逐块写 Checkpoint"是个重要细节**：流式只影响**页面显示**，最终回答仍然是**一条** AI 消息写进 State。这样 Checkpoint 里不会有一堆 `AIMessageChunk`。

### 6.3 `final_response_node`（L2952-2986，35 行）——`task_status` 的四条规则

docstring 直接给了规则表（[L2954-2961](../../local-agent-web/src/local_agent/graph/nodes.py#L2954-L2961)）：

> `task_status` 的更新规则（架构 §5.4：**有回答不等于任务完成**）：
>
> - 没有可用的 AI 文本 → `failed`，由页面给出明确提示，**而不是编造回答**；
> - 已经是 `failed`（例如超出循环上限）→ **保持 `failed`，不改写成 `completed`**；
> - 反思结论存在但 `goal_achieved=False`（未达成目标就结束，例如需要用户补充）→ **保持 `running`，不冒充 `completed`**；
> - 其余（闲聊、模型直接回答、反思判定完成）→ `completed`。

**四条规则的核心是"不许美化状态"**：

| 规则 | 拦住的"美化" |
|---|---|
| 无文本 → `failed` | 不许编造回答 |
| 已 `failed` → 保持 | 不许把失败改写成成功 |
| `goal_achieved=False` → 保持 `running` | 不许冒充完成 |
| 其余 → `completed` | — |

**第 2 条特别值得注意**：`final_response_node` 是**唯一**写 `task_status` 终值的地方，如果它无条件写 `completed`，那 `completion_router_node` 辛苦标记的 `failed`（L5 §6.3 的循环上限）就白费了。

**一个精确比较的细节**（[L2972-2974](../../local-agent-web/src/local_agent/graph/nodes.py#L2972-L2974)）：

> 用精确比较：没有 `mode` 的 State（直接调用节点的单测）按**工具语义**处理，避免"默认按闲聊"把 `running` 变成 `completed`。

**这是 L5 §5.3 那个 `mode == "chat"` 精确比较的同族写法**——两次都选了"缺失时按工具语义（更严格）处理"。**又一次保守兜底。**

---

## 7. 贯穿全文件的 8 个模式

看完 14 个节点，有 8 个模式反复出现。**记住它们比记住任何单个节点更有用。**

### 模式 1：`_decision` / `_error` 的 `salt` —— 每条记录都要问"会不会撞车"

[L3216-3236](../../local-agent-web/src/local_agent/graph/nodes.py#L3216-L3236) 和 [L3239-3255](../../local-agent-web/src/local_agent/graph/nodes.py#L3239-L3255) 两个构造器。

**同一个节点、同一轮次产生多条记录时，必须给 `salt`**，否则 `merge_appended` 会**静默丢弃**后面的（L4 §3.2）。

实测出现 `salt` 的地方：

| 位置 | salt 值 |
|---|---|
| `safety_gate_node` 放行标记 | `name`（工具名） |
| `approval_interrupt_node` 放行标记 | `approved_name` |
| `memory_write_node` 各条记录 | `reject:{i}`、`open:medium`、`open:long`、`write:{i}` |

**规律：`salt` 描述"这条记录和同轮其他记录的区别"**——工具名、候选序号、库名。

### 模式 2：失败闭合（fail-closed）在安全相关处一律生效

| 位置 | 输入异常 | 结果 |
|---|---|---|
| `_normalize_choice` | 未知的人工选择 | `reject` |
| `_as_mapping` | 非法恢复数据 | 空决策 |
| `approval_interrupt_node` | 二次确认未勾选 | `reject` |
| `tool_execution_node` | 批准后目标变了 | 拒绝执行 |

**对照 L5 §9**：安全类 fail-closed，成本类可 fail-open。

### 模式 3："失败"与"没命中/未启用"必须分开

| 场景 | 失败 | 没命中/未启用 |
|---|---|---|
| 代码检索 | `error_events` + 不同占位文字 | 空 refs，不记事件 |
| 记忆召回 | `error_events` | 不打开库 |
| 记忆写入 | `kind == "failed"` → `error_events` | 未启用/幂等跳过 → 只进轨迹 |
| 拆步 | 不适用（不会失败） | 合法退化为单步 |

**判据**：**"我做不到"要报，"这里没有"不用报。** 混在一起会让模型把"工具坏了"当成"确实没有"。

### 模式 4：鸭子类型避免硬依赖

```python
# _invoke_tool（L3411-3412）
invoke = getattr(tool, "invoke", None)      # ← 不 import BaseTool
```

**为什么？** 因为 `nodes.py` 已经 import 了 `langchain_core.messages`，再加 `BaseTool` 会让"改 langchain 版本"的影响面变大。用 `getattr` 检查"有没有这个方法"，比 `isinstance` 检查"是不是这个类"更宽松。

**代价**：类型检查器帮不上忙，写错了要到运行时才发现。

### 模式 5：估算与实际共用同一个函数

`_system_prompt`（[L548-570](../../local-agent-web/src/local_agent/graph/nodes.py#L548-L570)）被**两处**调用：

1. `compression_node` 里**估算 token**（L599）
2. `agent_decision_node` / `chat_node` 里**真的发给模型**（L835、L728）

**如果估算用一份逻辑、发送用另一份，预算就永远算不准。** 这是 L4 §6 讲 `RuntimeContext` 时提过的同一原则。

### 模式 6：所有"送模型"的路径都要投影

`project_messages` + `reply_overrides` 出现在**四处**：

| 位置 | 用途 |
|---|---|
| `_prompt_window`（L481-483） | 拼送模型的窗口 |
| `compression_node`（L669-672） | 摘要的输入 |
| `_chat_memory_final_text`（L2471-2473） | 闲聊收尾重写 |
| `web.py:236` | 页面渲染 |

**漏掉任何一处，假承诺就会从那个口子漏进去**——尤其摘要那条，漏了会**影响之后每一轮**（§3.1）。

### 模式 7：机械规则优先于模型结论

三处压制：

| 函数 | 压制什么 |
|---|---|
| `_apply_mechanical_guard`（[L1678-1700](../../local-agent-web/src/local_agent/graph/nodes.py#L1678-L1700)） | 三条自相矛盾组合 |
| `_apply_retry_limit`（[L1703-1717](../../local-agent-web/src/local_agent/graph/nodes.py#L1703-L1717)） | 重试超上限 |
| `_apply_stall_guard`（[L1894-1927](../../local-agent-web/src/local_agent/graph/nodes.py#L1894-L1927)） | 连续空转 |

加上 `_advance_plan` 的"完成一步 ≠ 完成整个任务"——**四处都是"模型说了不算"**。

**共同点：模型输出的都是"意图"，代码决定的是"事实"。**

### 模式 8：作用域隔离 —— 三处防"串味"

| 位置 | 键 | 防什么 |
|---|---|---|
| `tool_scope_key` | `run_id:tool_call_id` | 跨轮次 |
| `PlanStep.id` | `{run_id}:step-N` | 跨任务 |
| `current_task_scope`（[L3196-3203](../../local-agent-web/src/local_agent/graph/nodes.py#L3196-L3203)） | `run_id` | 跨任务 |

`_in_current_task`（[L3206-3213](../../local-agent-web/src/local_agent/graph/nodes.py#L3206-L3213)）在 `reflection_node` 里用了 **3 次**（L1350、L1360、L1398）——**每条读历史记录的地方都要过滤作用域**。

**规律：凡是从"追加型字段"里读数据，都要问一句"这条是这一轮的吗"。**

---

## 8. 读 `nodes.py` 的三个技巧

### 技巧 1：先看 docstring，它比代码信息量大

这个项目的 docstring 里有大量**"为什么"和"踩过的坑"**，而这些在代码里看不出来。举几个例子：

- L184-186：不复位"本轮字段"会累计到 `max_tool_loops`
- L669-671：摘要输入不投影会污染之后每一轮
- L1380-1382：只取前 N 字符让模型以为正文被截断
- L1392-1395：全量汇总会让"第二步失败"永久留存
- L1831-1833：真实模型会连续插入调整步骤直到走不完
- L3060-3064：审批路径没有放行标记 → 重复执行副作用
- L1290-1291：没有 recheck 标记 → 永久阻断

**这些都是"复审实证"——是被测试或人工复核抓出来的真实 bug。** 读它们等于免费获得一份"这个项目踩过的坑"清单。

### 技巧 2：grep `复审` / `实证` / `实测` 找坑

```powershell
rg -n "复审|实证|实测" src/local_agent/graph/nodes.py
```

这个项目用这三个词标记"经过验证的真实结论"。**这是最快获得"设计意图"的方法。**

### 技巧 3：用 L4 §7.1 的表当索引

`nodes.py` 的 3470 行不该通读。正确的用法是：

```text
"我想知道 plan 是谁改的"     → 查 L4 §7.1 的写入方 → planner_node / reflection_node
"我想知道 pending_approval 谁读的" → 查 L4 §7.2 的读取方 → safety_gate / route_after_safety
```

**然后跳到那个行号，读 docstring，完事。**

---

## 9. 自查清单

1. `prepare_context_node` 复位多少字段？哪些字段**不复位**？为什么？ → §2.1（17 个；7 个追加型审计字段不复位）
2. 哪个节点完全不写 `AgentState`？它写什么？ → §2.2（`load_agents_node` 写 `RuntimeContext.agents_context`）
3. 中期记忆和长期记忆，一层打不开会怎样？ → §2.3（互不牵连，只丢这一层）
4. `retrieve_code_node` 为什么不能读 `task_goal`？ → §2.4（它在 `planner_node` 之前，`task_goal` 还是 `None`）
5. 上下文压缩压的是什么？Checkpoint 里的 `messages` 会变吗？ → §3.1（压送模型的窗口；不变）
6. `planner_node` 为什么用 `if not plan` 判断？ → §3.2（它在环里，不判断会反复重新拆步）
7. 模型不支持 Tool Calling 时怎么处理？为什么不能"降级成闲聊"？ → §4.1（明确告知；降级会让用户以为任务在做）
8. 为什么动作指纹要"延迟到确实要审批时"才算？ → §4.2（对硬拒绝动作算指纹会递归扫目录树，卡死页面）
9. `stale_side_effects` 为什么必须在**执行前**检查？为什么按工具名而不是指纹匹配？ → §4.3（反思时已经执行过了；副作用会改变指纹输入）
10. `reflection_node` 的三层顺序是什么？`result_trust` 由谁得出？ → §4.4（机械校验 → LLM → 机械压制；只由机械校验）
11. `_advance_plan` 的三条硬语义？ → §4.5（完成一步≠完成整个任务 / 重规划必须真改计划 / 已完成步骤不可重写）
12. 递归删除为什么要**两次独立恢复**？"阶段不能用决策内容推断"是什么意思？ → §5.2（一次误点不够；数据可伪造，控制流不能）
13. 审批路径为什么需要自己写放行标记？ → §5.3（它不经过 `safety_gate_node`，那里是唯一标记点）
14. 记忆写入"按候选判定完成"的两个条件是什么？ → §6.1（所有目标 satisfied **且**至少一个真正落库）
15. `final_response_node` 的 `task_status` 有四条规则，核心是什么？ → §6.3（不许美化状态）

---

## 下一步

主干讲完了。到目前为止你已经走完：**地基**（L0-L2）→ **地图**（L3）→ **数据**（L4）→ **流程**（L5）→ **节点**（L6）。

接下来进入**支撑模块**，可以按兴趣挑：

- **L7 · 安全层**（`security/`，2001 行，6 文件）——最推荐接着看。因为 L6 里反复出现"安全门""指纹""审批""放行标记"，但**具体怎么判定**全在 `security/` 里。讲清 `classify_tool` 的三类判定、`action_fingerprint` 到底算了什么、`shell_guard` 怎么拦危险命令、递归删除的二次确认怎么触发。
- **L8 · 能力层**（`tools/`，1203 行）——6 个工具怎么做出来的。
- **L9 · 记忆·反思·上下文·检索**（`memory/` + `reflection/` + `context/` + `rag/`）——L6 里调用最多的那些能力。
- **L10 · 启动与界面**（`web.py` + `ui/` + `persistence/` + `llm/`）——从点击到落盘的完整一次调用。

建议接着写 **L7**——它能把 L6 里所有"安全门说不行"的悬念一次性结清。

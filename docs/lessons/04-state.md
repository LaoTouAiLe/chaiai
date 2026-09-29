# L4 · 数据模型：`graph/state.py`

> L3 说过：`state.py` 是全员依赖的枢纽（L3 §3.1 结论 2）。这一篇就是把那个枢纽拆开看。
>
> 读完你应该能回答：**State 里为什么有的字段用 `Annotated`、有的不用？节点往 State 里写东西时，为什么有的直接返回、有的要先 `merge_appended`？**

文件：[state.py](../../local-agent-web/src/local_agent/graph/state.py)，394 行，四块内容：

| 行范围 | 内容 | 数量 |
|---|---|---|
| [35-92](../../local-agent-web/src/local_agent/graph/state.py#L35-L92) | 类型别名 + 三个 TypedDict | 4 个 `Literal` + `RAGReference` / `DecisionRecord` / `ErrorEvent` |
| [98-219](../../local-agent-web/src/local_agent/graph/state.py#L98-L219) | reducer 与去重键工厂函数 | 2 个 reducer + 5 个 key + 2 个构造器 |
| [222-300](../../local-agent-web/src/local_agent/graph/state.py#L222-L300) | 5 个 `@dataclass(frozen=True)` | 计划步骤、工具事件、证据、记忆候选、反思结果 |
| [303-394](../../local-agent-web/src/local_agent/graph/state.py#L303-L394) | **`AgentState`（35 字段）+ `RuntimeContext`（13 字段）** | 本文重点 |

---

## 1. `AgentState` 到底是什么东西

```python
class AgentState(TypedDict, total=False):
```

### 1.1 `TypedDict` 是"编译期声明"，运行时就是个 `dict`

Java 类比：它像**一份没有实现类的接口**，或者说像你写给同事看的字段清单——**只在类型检查器眼里存在**。

```python
from local_agent.graph.state import AgentState
AgentState()          # 报错！TypedDict 不能实例化
```

运行时节点返回的就是普通 `dict`，LangGraph 按你声明的字段名去匹配。所以：

**→ 字段名写错不会有任何运行时报错，只会静默丢掉。** 这是本文件最容易踩的坑。

### 1.2 `total=False` 意味着"所有字段都可选"

实测：

```python
>>> AgentState.__required_keys__
frozenset()
>>> AgentState.__optional_keys__
frozenset({'thread_id', 'session_id', ... 共 35 个})
```

为什么必须这样？因为 `total=True` 时，LangGraph 会要求**每个节点返回完整的 35 个字段**——那就没法"只提交局部更新"了。

`state.py:304` 的注释原话：*"`total=False` 允许节点只提交局部更新"*。

### 1.3 实测：State 是**稀疏**的

建一个只有一句话的图，让它只写 `user_input`，看返回了哪些键：

```python
g = StateGraph(AgentState)
g.add_node("w", lambda state: {"user_input": "x"})
...
g.compile().invoke({"user_input": "hi"})
```

```text
返回的键 = ['approval_history', 'completed_steps', 'decision_trace', 'error_events',
            'memory_write_candidates', 'messages', 'reply_overrides', 'tool_events',
            'tool_results', 'user_input']
'task_goal' in out -> False
```

两个结论，都很重要：

1. **没被写过的"覆盖语义"字段根本不在结果里。** `task_goal` 不在——所以你读代码时不能假设 `state["task_goal"]` 一定存在，必须 `state.get("task_goal")`。
2. **带 reducer 的通道一定在结果里，值是空值**（`[]` / `{}`）。上面 7 个 `[]` 加 1 个 `{}` 全是这种。

这解释了一个你会在 `nodes.py` 里反复看到的写法差异：

```python
state["messages"]            # 带 reducer → 通道已初始化 → 敢直接下标
state.get("task_goal")       # 覆盖语义 → 可能不存在 → 必须 get
```

**Java 类比**：这像 `Map<String, Object>` 与 POJO 的区别。POJO 的字段永远存在（默认值 `null`）；这里的 State 是 Map，**键不存在 ≠ 值是 null**。

---

## 2. 三种更新语义（本文件的核心）

35 个字段实测分桶：

| 语义 | 数量 | 字段 | 声明方式 |
|---|---|---|---|
| **覆盖**（后写覆盖先写） | **26** | `thread_id` `session_id` `run_id` `user_input` `normalized_request` `mode` `route_reason` `conversation_summary` `context_version` `task_goal` `task_status` `plan` `current_step_index` `pending_tool_calls` `reflection_result` `pending_approval` `memory_refs` `rag_refs` `retry_count` `loop_count` `max_loop_count` `token_usage` `context_budget` `compression_count` `final_response` `final_evidence` | 裸类型 |
| **追加** | **7** | `completed_steps` `tool_events` `tool_results` `decision_trace` `approval_history` `memory_write_candidates` `error_events` | `Annotated[list[X], operator.add]` |
| **消息合并** | **1** | `messages` | `Annotated[list[AnyMessage], add_messages]` |
| **字典合并** | **1** | `reply_overrides` | `Annotated[dict[str, str], merge_mapping]` |

`Annotated[X, f]` 的读法是：**类型是 X，但更新时用 f 合并**。L2 §3 讲过 `Annotated` 本身只是"给类型贴标签"，LangGraph 是那个读标签的人。

### 2.1 覆盖语义：为什么 26 个字段敢用裸类型

因为它们**在图上永远不会被并发写**。这不是猜测，LangGraph 会强制：

实测两个并列节点同时写 `task_goal`：

```text
InvalidUpdateError: At key 'task_goal': Can receive only one value per step.
                    Use an Annotated key to handle multiple values.
```

也就是说：**如果你将来改了 `edges.py`，让两个节点在同一超步里并行、又都写同一个覆盖字段，图会直接报错**。`InvalidUpdateError` 是一个"架构保护"——它逼你回头想清楚这两个值到底该合并还是该二选一。

**Java 类比**：像并发容器与普通 `HashMap` 的区别——普通字段就是普通字段，多线程同时写没定义行为；这里 LangGraph 选择了"直接抛异常"而不是"悄悄丢失"。

### 2.2 `operator.add` 的真相：**输入值也走 reducer**

这是个反直觉点，实测：

```python
g.add_node("w1", lambda s: {"completed_steps": ["a", "b"]})
g.add_node("w2", lambda s: {"completed_steps": ["c"]})
g.compile().invoke({"completed_steps": ["init"]})
```

```text
['init', 'a', 'b', 'c']
```

**首次 invoke 传进去的 `["init"]` 不是"初始值"，而是一次更新**——它被 reducer 追加进去，而不是替换掉。

**Java 类比**：`invoke(payload)` 相当于一次 `list.addAll(payload)`，而不是 `list = payload`。所有 reducer 通道都是这个语义。

判断你写的 reducer 属于哪种语义，只要问一句：**"这个字段的更新是 addAll 还是 set？"**

### 2.3 `operator.add` 的问题：**它不去重**

这是整个 `state.py` 后半部分存在的唯一理由。

`state.py:10` 的注释写得很直接：

> 追加型列表使用 `operator.add`，**写入方需按事件 ID 去重**（`operator.add` 本身不去重）。

为什么必须去重？**因为 Checkpoint 会重放节点**（L0 §9）：

```text
用户提交 → 节点 A 追加了 3 条 tool_results → 进程在节点 B 崩溃
进程重启 → Checkpoint 恢复到节点 A 之后 → 但如果 A 被重放…
        → operator.add 会把同样的 3 条再追加一遍 → 6 条，其中 3 条是假的
```

**Java 类比**：这就是"消息队列的 at-least-once 投递"。消费者必须自己做幂等，不能指望队列只送一次。`merge_appended` 就是那个幂等过滤器。

### 2.4 `merge_appended`：幂等追加

[state.py:109-128](../../local-agent-web/src/local_agent/graph/state.py#L109-L128)，实测：

```python
existing = ["evt-1", "evt-2"]
incoming = ["evt-1", "evt-2", "evt-3"]
merge_appended(existing, incoming, key=lambda s: s)
# -> ['evt-3']
```

读源码就三行逻辑（[120-128](../../local-agent-web/src/local_agent/graph/state.py#L120-L128)）：

```python
seen = {key(item) for item in existing}   # 已有的事件 ID 集合
for item in incoming:
    if item_key in seen: continue         # 出现过就跳过
    seen.add(item_key); fresh.append(item)
```

注意 `seen` 在循环里**持续增长**：`incoming` 自己内部的重复项也会被滤掉。这是正确的——同一次调用里重复的事件 ID 同样不该进 State。

**Java 类比**：像一个带 `Set` 去重的 `BatchInserter`，或者数据库的 `INSERT ... ON CONFLICT DO NOTHING`。

### 2.5 7 个追加通道里，只有 5 个用 `merge_appended`

我把 7 个通道的写入点全查了一遍，实测结果：

| 通道 | 去重方式 | 写入位置 |
|---|---|---|
| `tool_events` | `merge_appended(..., tool_event_key)` | [nodes.py:1065](../../local-agent-web/src/local_agent/graph/nodes.py#L1065)、[1310](../../local-agent-web/src/local_agent/graph/nodes.py#L1310) |
| `tool_results` | `merge_appended(..., tool_result_key)` | [nodes.py:1068](../../local-agent-web/src/local_agent/graph/nodes.py#L1068)、[1313](../../local-agent-web/src/local_agent/graph/nodes.py#L1313) |
| `decision_trace` | `merge_appended(..., decision_record_key)` | 9 处，见 §3.4 |
| `error_events` | `merge_appended(..., error_event_key)` | 9 处 |
| `approval_history` | `merge_appended(..., approval_key)` | [nodes.py:3077](../../local-agent-web/src/local_agent/graph/nodes.py#L3077) |
| **`completed_steps`** | **写入方自己算增量** | [nodes.py:1493-1495](../../local-agent-web/src/local_agent/graph/nodes.py#L1493-L1495) |
| **`memory_write_candidates`** | **写入方自己算增量** | [nodes.py:2781-2782](../../local-agent-web/src/local_agent/graph/nodes.py#L2781-L2782) |

后两个不是偷懒，是**必须那样**——看代码：

`completed_steps`（[nodes.py:1807-1814](../../local-agent-web/src/local_agent/graph/nodes.py#L1807-L1814)）：

```python
known = set(state.get("completed_steps") or ())
...
if goal_achieved:
    if step.id not in known:          # 只有没记过的才追加
        completed = (step.id,)
```

`memory_write_candidates`（[nodes.py:2742-2754](../../local-agent-web/src/local_agent/graph/nodes.py#L2742-L2754)）：

```python
# 判重键含候选身份（不是只看来源事件）：同一事件下的其它候选不会被误伤（复审实证）
seen = {writer.candidate_write_key(item) for item in state.get("memory_write_candidates", [])}
...
completed = _completed_candidates(intents, outcomes)
```

为什么不用 `merge_appended`？因为这两个通道的去重键**不是"事件 ID"**：

- `completed_steps` 存的是 `PlanStep.id` 的字符串，本身就是键。
- `memory_write_candidates` 的键要算（`candidate_write_key` 含候选身份），而且**写入前还要先过滤掉已见过的**——判重发生在写库阶段，不只是写 State 阶段。

**给你的判据**：新的追加通道该用哪种？

| 情况 | 用 |
|---|---|
| 元素自带唯一 ID，且"已存在就跳过"足够 | `merge_appended` + 一个 key 函数 |
| 判重键要计算，或判重要和副作用（写库）绑在一起 | 写入方自己算增量 |

### 2.6 `add_messages`：唯一的"有智能"的 reducer

它来自 LangGraph（[state.py:24](../../local-agent-web/src/local_agent/graph/state.py#L24)），不在这里定义。行为在 L1 §2 讲过，这里只补一句关键区别：

`add_messages` 遇到**同 ID** 的消息是**替换**（L1 实测：输入 1 条 → 输出 1 条，内容是改写后的），而 `operator.add` 是**无脑追加**。

**为什么消息必须用替换语义？** 因为流式输出会不断产生 `AIMessageChunk`，它们的 id 相同，需要"拼起来"而不是"堆成一摞"。这是 LangGraph 专门给消息写的 reducer（源码里那个 `_add_messages_wrapper` 就是干这个的）。

### 2.7 `merge_mapping`：为什么单独造一个

[state.py:98-106](../../local-agent-web/src/local_agent/graph/state.py#L98-L106)：

```python
merged = dict(existing or {})
merged.update(incoming or {})
return merged
```

只有 6 行，但它和 `merge_appended` 的区别是本质的（[state.py:101-102](../../local-agent-web/src/local_agent/graph/state.py#L101-L102) 的原话）：

> 这里要的是**覆盖语义**（同一条消息被再次修正时以最新为准），而不是"只追加没出现过的"。

实测：

```python
g.add_node("w1", lambda s: {"reply_overrides": {"m1": "修正1"}})
g.add_node("w2", lambda s: {"reply_overrides": {"m1": "修正2"}})
g.compile().invoke({"reply_overrides": {"m0": "原始"}})
# -> {'m0': '原始', 'm1': '修正2'}
```

`m0` 保留、`m1` 被后写的覆盖。**这是对的**：`reply_overrides` 是"消息 id → 修正文本"，同一条消息被修正两次，当然以最后一次为准。

`reply_overrides` 到底是什么、为什么需要它，看 [state.py:361-367](../../local-agent-web/src/local_agent/graph/state.py#L361-L367) 那段注释，值得逐字读一遍：

> `messages` 必须保留模型原文（架构 §10.1：审计与恢复用的原始记录），但**用户看到的内容**与**下一轮送模型的窗口**要用修正后的文本——否则记忆保存失败时页面仍显示"我记住了"，下一轮模型也照着假承诺继续对话（复审实证）。

这段话是整份文档里最有信息量的一段之一。它讲的是一个**真实发生过的 bug**：模型说"我记住了"，但实际写入失败。修法不是"改掉模型的话"（那会破坏审计），而是**保留原文 + 加一层投影**。

**Java 类比**：事件溯源里的"原始事件 + 读时投影"。存储层永远是 append-only 的事实，展示层可以有自己的视图。谁读它（实测）：[nodes.py:494](../../local-agent-web/src/local_agent/graph/nodes.py#L494)（拼下一轮窗口）、[nodes.py:672](../../local-agent-web/src/local_agent/graph/nodes.py#L672)（压缩时投影）、[web.py:236](../../local-agent-web/src/local_agent/web.py#L236)（页面渲染）。

---

## 3. 事件 ID：为什么这里有一整套 key 函数

[state.py:141-167](../../local-agent-web/src/local_agent/graph/state.py#L141-L167) 有 5 个几乎一样的函数：

```python
def tool_event_key(event: ToolEvent) -> str:      return tool_scope_key(event.run_id, event.tool_call_id)
def tool_result_key(record: ToolResultRecord) -> str: return tool_scope_key(record.get("run_id") or "", record["tool_call_id"])
def decision_record_key(record: DecisionRecord) -> str: return record["record_id"]
def error_event_key(record: ErrorEvent) -> str:    return record["event_id"]
def approval_key(record: ApprovalDecision) -> str: return record["approval_id"]
```

看起来冗余（为什么不直接 `lambda x: x.id`）？三个理由：

1. **`merge_appended` 需要 `Callable[[T], str]`**，而这些 key 要跨文件调用，写成具名函数才有 docstring 和类型检查。
2. **每个 key 的语义不同**，注释里写了为什么（比如 `tool_result_key` 用 `get("run_id")` 是为了兼容升级前的 Checkpoint）。
3. **改去重口径时只改一处**——这正是你想要的（Java 里你会把它抽成 `Function<T,String>` 常量）。

### 3.1 `tool_scope_key`：轮次 + 调用 ID

[state.py:131-138](../../local-agent-web/src/local_agent/graph/state.py#L131-L138)，实测：

```text
tool_scope_key('run-1', 'call-1') = run-1:call-1
tool_scope_key('run-2', 'call-1') = run-2:call-1
```

**注意第二行**：同一个 `tool_call_id`，不同轮次 → 不同的键。这是刻意的，注释解释了为什么要两个都算：

> 不同轮次即使复用了同一个 `tool_call_id`，也是不同的键：既不会被误判成"已执行"而跳过，也不会把新结果当成重复项丢掉。

**这是个真实的坑**：模型经常在不同轮次给出相同的 `tool_call_id`（比如每轮都从 `call_1` 开始编号）。如果只用 `tool_call_id` 做去重键，第二轮的工具调用会被当成"上一轮已经执行过"而**静默跳过**——模型会收到"工具没结果"，然后开始胡编。

**Java 类比**：数据库复合主键。单列做主键会撞，`(run_id, tool_call_id)` 才是真正唯一的。

### 3.2 `make_decision_record` 与 `salt`

[state.py:170-192](../../local-agent-web/src/local_agent/graph/state.py#L170-L192)。实测：

```text
make_decision_record("run-1", stage="reflection", summary="x", loop_count=2, tool_call_id="call-1")
  -> run-1:decision:reflection:2:call-1
同参数再调一次 -> run-1:decision:reflection:2:call-1   # 完全相同 = 幂等
加 salt="cand#0" -> run-1:decision:reflection:2:call-1:cand#0
加 salt="cand#1" -> run-1:decision:reflection:2:call-1:cand#1
```

**后半段是这个文件里最阴险的坑**，docstring 原话（[state.py:182-183](../../local-agent-web/src/local_agent/graph/state.py#L182-L183)）：

> 否则同 stage + 同轮次 + 同 `tool_call_id` 的记录会得到同一个 ID，`merge_appended` 只保留第一条、**其余静默消失**。

**静默消失**——不是报错、不是警告，是少了三条记录而你根本不知道。这个 bug 只在"一个节点内要产生多条同阶段记录"时才暴露，比如一批记忆候选各自的结果。

**这就是 `merge_appended` 的代价**：它保护你免受重放影响，但也把"ID 撞车"变成了静默数据丢失。所以每个调用点都要问一句：**"这个节点在这次执行里会不会产生两条 ID 相同的记录？"**

顺带记住这个模式：`_record_scope`（[216-219](../../local-agent-web/src/local_agent/graph/state.py#L216-L219)）把 ID 拼成

```text
{run_id}:{kind}:{stage}:{loop_count}:{tool_call_id}:{salt}
```

用 `:` 分段的字符串 ID，而不是对象或 UUID。Java 类比：你在 Java 里可能用 `Objects.hash(...)` 或者一个 `record EventId(...)`；这里选了最土但最好排查的方案——**肉眼可读、能直接 grep、Checkpoint 里看得懂**。

### 3.3 为什么不用 UUID

如果每条记录都 `uuid4()`，重放时会产生**新 ID** → `merge_appended` 认为都是新的 → 重复追加。**确定性 ID 是幂等的前提**。

规则：**ID 必须是"这件事的身份"的函数，不能是"这次执行"的函数。**

### 3.4 谁在写 `decision_trace`（9 处）

`decision_trace` 是最热闹的通道，实测写入方：

| 节点 | 位置 |
|---|---|
| `mode_router_node` | [routers.py:142](../../local-agent-web/src/local_agent/graph/routers.py#L142) |
| `agent_decision_node` | [nodes.py:801](../../local-agent-web/src/local_agent/graph/nodes.py#L801) |
| `_tool_calling_unavailable` | [nodes.py:853](../../local-agent-web/src/local_agent/graph/nodes.py#L853)、[866](../../local-agent-web/src/local_agent/graph/nodes.py#L866) |
| `safety_gate_node` | [nodes.py:1059](../../local-agent-web/src/local_agent/graph/nodes.py#L1059) |
| `tool_execution_node` | [nodes.py:1304](../../local-agent-web/src/local_agent/graph/nodes.py#L1304) |
| `reflection_node` | [nodes.py:1489](../../local-agent-web/src/local_agent/graph/nodes.py#L1489) |
| `_chat_memory_reflection` | [nodes.py:2392](../../local-agent-web/src/local_agent/graph/nodes.py#L2392) |
| `memory_write_node` | [nodes.py:2655](../../local-agent-web/src/local_agent/graph/nodes.py#L2655)、[2767](../../local-agent-web/src/local_agent/graph/nodes.py#L2767) |
| `approval_interrupt_node` | [nodes.py:3080](../../local-agent-web/src/local_agent/graph/nodes.py#L3080) |

它就是这个项目的"日志表"。§8 会回到这一点。

---

## 4. 五个 `@dataclass(frozen=True)`

| 类 | 行 | 用途 | 谁在用 |
|---|---|---|---|
| `PlanStep` | [222-241](../../local-agent-web/src/local_agent/graph/state.py#L222-L241) | 计划的一步 | `plan: list[PlanStep]` |
| `ToolEvent` | [244-259](../../local-agent-web/src/local_agent/graph/state.py#L244-L259) | 一次工具调用的事件 | `tool_events` |
| `EvidenceReference` | [262-270](../../local-agent-web/src/local_agent/graph/state.py#L262-L270) | 证据引用 | `final_evidence` / `ReflectionResult.evidence` |
| `MemoryCandidate` | [273-287](../../local-agent-web/src/local_agent/graph/state.py#L273-L287) | 记忆候选 | `memory_write_candidates` |
| `ReflectionResult` | [290-300](../../local-agent-web/src/local_agent/graph/state.py#L290-L300) | 反思结论 | `reflection_result` |

### 4.1 为什么是 `dataclass` 而不是 `TypedDict` 或 pydantic

三种结构在这个文件里**同时存在**，分工很清楚：

| 类型 | 语法 | 运行时行为 | 用在哪 | 为什么 |
|---|---|---|---|---|
| `TypedDict` | `class X(TypedDict)` | **就是个 dict**，无校验 | `RAGReference` `DecisionRecord` `ErrorEvent` `AgentState` | 进 Checkpoint 要能被 JSON 序列化；不需要校验 |
| `@dataclass(frozen=True)` | `@dataclass(frozen=True) class X` | **真对象**，有 `__eq__`/`__hash__`，**不可变** | 5 个内嵌结构 | 需要"值语义"：复制、比较、当集合元素；不可变才敢在 State 里传来传去 |
| `BaseModel`（pydantic） | `class X(BaseModel)` | 真对象 + **运行时校验** + 序列化 | `tools/result_types.py` 的工具返回 | 数据来自"外部"（模型、文件系统），必须校验 |

**判断口诀**：

- 数据来自**外部**（模型输出、文件内容、用户输入）→ pydantic（校验边界）
- 数据在**内部**流转、要不可变 + 能比较 → dataclass
- 数据要**进 Checkpoint**、且结构简单 → TypedDict

### 4.2 为什么必须 `frozen=True`

因为它进 `operator.add` 的列表。如果可变，一个节点改了 `state["plan"][0].status`，就**直接改了 State 里的对象**——而 LangGraph 依赖"节点返回新值"来记录变更，原地修改会让 Checkpoint 认为什么都没发生。

**Java 类比**：`record` 与普通 POJO 的区别。你已经在 Java 里熟悉"不可变值对象"的好处了，这里是同一个理由，加上一条框架约束。

那怎么改字段值？用 `dataclasses.replace`——实测出现在 [nodes.py:1825](../../local-agent-web/src/local_agent/graph/nodes.py#L1825)：

```python
plan[next_index] = replace(plan[next_index], status="running")
```

**这是 `frozen=True` 的标准操作**：复制一份、改一个字段、放回去。Java 里你会用 `withStatus(...)` 或者 Builder 重造一个。

### 4.3 逐字段要点（只讲会导致 bug 的）

**`PlanStep`**（[222-241](../../local-agent-web/src/local_agent/graph/state.py#L222-L241)）——六个 `status` 取值，docstring 里有：

`pending` / `running` / `completed` / `failed` / `skipped` / `blocked`

注意它**不是** `Literal`，就是个 `str`（`status: str = "pending"`）。这与 §5.4 的 `MemoryCandidate.memory_type` 不同——那里用了 `Literal`。为什么这里不用？因为 `status` 会被 reflection 层按字符串处理，而且取值集合还在演进（docstring 里写"T20 定稿"）。**这是一处不一致，属于实现演进的痕迹**，读代码时别指望它被类型系统保护。

还有一句值得记住的设计（[230-232](../../local-agent-web/src/local_agent/graph/state.py#L230-L232)）：

> `retry_count` 是**本步骤**的重试次数：进入下一步后从 0 重新开始（`agent.max_retries_per_step` 按步骤判定，`agent.max_tool_loops` 仍是整个任务的总上限）。
> `id` 含 `run_id`，所以 `completed_steps` 里留下的历史任务步骤不会让新任务误判"这一步已完成"。

**"ID 含 run_id"是这里第二个"防历史串味"的设计**（第一个是 §3.1 的 `tool_scope_key`）。三个地方都在防同一件事：**上一个任务的数据污染这一个任务**。

**`MemoryCandidate`**（[273-287](../../local-agent-web/src/local_agent/graph/state.py#L273-L287)）——`memory_type` 用了 `Literal`，三个取值：`conversation_summary` / `task_fragment` / `entity_fact`。

docstring 特别强调（[277-279](../../local-agent-web/src/local_agent/graph/state.py#L277-L279)）：

> **字段集合与架构 §4.3 逐字一致**（架构 §10.4 明确"`MemoryCandidate` 不新增作用域字段"）：作用域不是候选的属性，而是写入时按"已验证意图 + 候选类型 + 当前用户/项目"推导的结果。

**这是"架构文档约束代码"的一个实例**——注意这个项目里 `架构 §x.x` 这样的引用到处都是，你可以顺着它们回到 `ARCHITECTURE.md` 验证。这也是你作为架构设计者该有的读法：**代码是否偏离了你的设计**。

**`ReflectionResult.next_action`**（[298](../../local-agent-web/src/local_agent/graph/state.py#L298)）——四个取值 `continue` / `retry` / `ask_user` / `finish`，默认 `finish`。

**这个默认值是"保守"还是"危险"？** 想一想：如果反思节点构造 `ReflectionResult` 时忘了传 `next_action`，默认 `finish` 会让任务直接收尾。而 `reflection_result.result_trust` 是必填的（无默认），所以至少模型说的"可信度"必须显式给。这是个**刻意的偏保守设计**——宁可早收尾，也不无限循环（后面 `loop_limit_reached` 还有一道兜底，见 L5）。

---

## 5. 几个 `Literal` 类型别名

[state.py:35-46](../../local-agent-web/src/local_agent/graph/state.py#L35-L46)：

```python
AgentMode    = Literal["chat", "tool"]
TaskStatus   = Literal["pending", "running", "waiting_approval", "completed", "failed"]
EvidenceType = Literal["file_hash", "diff", "exit_code", "stdout", "test_result"]
RAGMatchType = Literal["path", "filename", "text", "ast"]
RAGSource    = Literal["rg", "python_scan", "ast"]
```

### 5.1 为什么不用 `Enum`（L2 §3 说过，这里是它的理由）

**因为 State 要进 SQLite Checkpoint。** `Literal` 的值就是普通 `str`，序列化零成本；`Enum` 需要在序列化白名单里登记转换器。L3 §6 说过：**全仓库零 `import enum`**。

### 5.2 `TaskStatus` 里有 `waiting_approval`

这个值是状态机的一部分：任务在等待人工审批时**不是 `running` 也不是 `failed`**，而是一个独立状态。因为此时图已经 `interrupt` 挂起了——它既没在跑，也没失败。L7 会详细讲这条链。

### 5.3 `RAGMatchType` / `RAGSource` 里的 `ast`

`ast` 是**预留值**：[state.py:43](../../local-agent-web/src/local_agent/graph/state.py#L43) 明确说"`ast` 是局部 AST 解析预留的取值，T21 的基础路径不产生它"。L3 §5.3 讲过这个决定。

**这是"契约先行"的一个实例**：类型定义里先把未来的取值留出来，实现分阶段填。好处是升级时不用改类型；风险是**读到 `Literal` 里有个值不等于这个值真的会产生**——查一下代码里谁产生它，别假设。

### 5.4 `RAGReference` 的三条硬约束

[state.py:49-64](../../local-agent-web/src/local_agent/graph/state.py#L49-L64) 的 docstring 值得单独列（它把三件事讲死了）：

1. `path` 是**相对当前工作区**的规范化路径；`line_start` / `line_end` 是 **1 基闭区间**，且必须与实际返回的片段一致（**截断后也要如实收窄**）。
2. `rank` 只是本轮结果的顺序（从 1 开始），**不是相似度或置信度**——这里没有向量。
3. 相同 `path + line_start + line_end` 的结果在写入 `rag_refs` 前**必须去重**。

三句话对应三个 Java 程序员熟悉的坑：坐标口径不统一（0 基 vs 1 基）、字段名暗示了不该有的语义（`rank` 不是 score）、写入前的去重责任归属。

---

## 6. `RuntimeContext`：不进 Checkpoint 的那一半

[state.py:371-394](../../local-agent-web/src/local_agent/graph/state.py#L371-L394)，13 个字段：

| 字段 | 类型 | 有默认值 | 作用 |
|---|---|---|---|
| `workspace_root` | `Path` | 否 | 工作区根（安全策略的边界） |
| `current_cwd` | `Path` | 否 | 当前工作目录 |
| `model_adapter` | `BaseChatModel` | 否 | 模型客户端 |
| `app_config` | `AppConfig` | 否 | 完整配置，节点按需读取 |
| `user_id` | `str \| None` | 是 | 多用户作用域 |
| `config_profile` | `str` | 是 | 配置档名，默认 `"default"` |
| `agents_context` | `str \| None` | 是 | **`load_agents` 节点写入**的合并规则文本 |
| `model_capabilities` | `ModelCapabilities \| None` | 是 | 启动时探测的能力 |
| `tool_registry` | `ToolRegistry \| None` | 是 | 工具注册表 |
| `security_policy` | `SecurityPolicy \| None` | 是 | 安全策略 |
| `approval_channel` | `object \| None` | 是 | 审批通道（**类型还是 `object`**） |
| `current_time` | `datetime \| None` | 是 | 注入的时间（可测性） |
| `request_id` | `str` | 是 | 本次请求 ID |

三个必须记住的点：

### 6.1 它是**可变**的（没有 `frozen=True`）

注意上面 5 个类是 `@dataclass(frozen=True)`，而 `RuntimeContext` 是 `@dataclass` **裸的**——因为它要**被节点改**：`load_agents_node` 会把合并好的 `AGENTS.md` 文本写进 `rt.agents_context`（[state.py:386-387](../../local-agent-web/src/local_agent/graph/state.py#L386-L387) 的注释明说）。

**这也是它不能进 Checkpoint 的原因之一**：它的生命周期是"一次调用"，State 的生命周期是"一个会话"。

### 6.2 它是**依赖注入容器**

L0 §7 讲过：节点函数第二个参数名叫 `runtime` 时，LangGraph 注入这个对象。所以节点拿模型、拿配置、拿安全策略，全都走 `runtime`，**而不是从 State 里拿**。

**为什么模型不能放 State 里？** 两个理由：

1. **State 要序列化**——`BaseChatModel` 里可能有 HTTP 客户端、API Key，不能进 Checkpoint。
2. **同一张图要能用不同模型跑**——[builder.py:1-5](../../local-agent-web/src/local_agent/graph/builder.py#L1-L5) 的 docstring 原话："同一张编译好的图既能用真实模型，也能用测试假模型与假策略。"

**Java 类比**：这就是 Spring 的 `@RequestScope` bean 与 Session 存储的区别。请求级的东西放请求里；跨请求的事实放会话里。

### 6.3 `workspace_root` 是安全根基

它不在 State 里，意味着**模型无法通过改 State 来改变文件访问边界**。如果你把 `workspace_root` 放进 State，一个幻觉的模型理论上可以"写一个字段"来扩大自己的权限面。

**这是"安全边界不放在被控对象能触及的地方"的一个实例。** L7 会讲 `SecurityPolicy` 怎么用它。

### 6.4 `approval_channel: object | None` 是个待办

类型是 `object`，注释说"由 T10 给出具体类型，当前保持为空"。这是一个**已知的类型缺口**——读代码时如果看到 `cast` 或者 `getattr` 在这里兜底，原因就是这个。

---

## 7. 全字段读写索引（AST 实测）

这张表是我用 `ast` 解析 `nodes.py` / `routers.py`，抓每个函数里的 `state["x"]` / `state.get("x")` 和返回字典的键**实测**出来的。读代码时它比 grep 快。

### 7.1 按节点看（写什么）

| 节点 | 写的字段 |
|---|---|
| `prepare_context_node` | 17 个：`thread_id` `session_id` `run_id` `normalized_request` `task_status` `task_goal` `plan` `current_step_index` `pending_tool_calls` `pending_approval` `reflection_result` `retry_count` `loop_count` `memory_refs` `rag_refs` `final_response` `final_evidence` |
| `load_agents_node` | **一个都不写 State**（只写 `RuntimeContext.agents_context`） |
| `recall_memory_node` | `memory_refs` |
| `retrieve_code_node` | `rag_refs` `error_events` |
| `compression_node` | `token_usage` `context_budget` `conversation_summary` `context_version` `compression_count` `error_events` |
| `mode_router_node` | `mode` `route_reason` `decision_trace` |
| `planner_node` | `task_goal` `max_loop_count` `task_status` `plan` |
| `agent_decision_node` | `messages` `pending_tool_calls` `loop_count` `decision_trace` |
| `safety_gate_node` | `pending_tool_calls` `pending_approval` `decision_trace` |
| `tool_execution_node` | `tool_events` `tool_results` `error_events` `decision_trace` |
| `reflection_node` | `final_evidence` `reflection_result` `plan` `current_step_index` `retry_count` `completed_steps` `decision_trace`（+ 条件写 `messages` `token_usage` `context_budget`） |
| `memory_write_node` | `decision_trace`（+ 条件写 `memory_write_candidates` `error_events` `reply_overrides` `final_response`） |
| `final_response_node` | `final_response` `task_status` |
| `approval_interrupt_node` | `pending_approval` `approval_history` `decision_trace` |
| `completion_router_node` | `messages` `task_status` |
| `chat_node` | `messages` |

**从这张表能看出两件事**：

1. **`prepare_context_node` 写 17 个字段**——它在"清空上一轮"。看一下它的读（只有 `session_id` `thread_id` `user_input`），对比它的写，你能直接看到"一轮开始时要复位哪些状态"。这是 L6 讲这个节点时的主线。
2. **`load_agents_node` 完全不碰 State**——它是唯一一个纯"写 RuntimeContext"的节点。为什么？因为 AGENTS 规则**在一次调用内是常量**，不需要进会话状态。

### 7.2 按字段看（谁读）

只列**读**的位置（写的位置见 §7.1 与 §2.5）：

| 字段 | 被谁读 |
|---|---|
| `messages` | `_prompt_window` `compression_node` `reflection_node` `_last_answer_text` `_stalled_rounds` `_wrap_evidence` `_deterministic_summary` `_chat_memory_final_text` `final_response_node` |
| `plan` | `_system_prompt` `planner_node` `_reflect_decision` `_updated_plan` `_current_step_retry_count` `_plan_has_unfinished_steps` |
| `tool_results` | `safety_gate_node` `tool_execution_node` `reflection_node` `_recent_records` `unconfirmed_calls` `stale_side_effects` |
| `tool_events` | 同上（`_source_event_ids` 而非 `_recent_records`） |
| `error_events` | `compression_node` `chat_node` `safety_gate_node` `reflection_node` `_recall_failed` `_rag_search_failed` `_chat_memory_reflection` … |
| `decision_trace` | 12 处，几乎所有节点 |
| `pending_tool_calls` | `safety_gate_node` `tool_execution_node` `approval_interrupt_node` `route_after_decision` `route_after_safety` |
| `approval_history` | `safety_gate_node` `tool_execution_node` `approval_interrupt_node` `_approval_target_changed` `stale_side_effects` `route_after_approval` |
| `reflection_result` | `memory_write_node` `final_response_node` `_has_memory_candidates` `_next_action` |
| `reply_overrides` | `_prompt_window` `compression_node` `web.py:236` |
| `loop_count` / `max_loop_count` | `agent_decision_node` / `_reflect_decision` / `loop_limit_reached` / `mode_router_node` |
| `context_budget` | `_covered_messages` |

**一个空档值得注意**：`final_evidence` 只有写入方（3 处），没有任何节点读它。它是**纯粹的输出产物**——给页面/审计看的，不参与决策。`final_response` 同理（只被 `final_response_node` 自己读一次）。

---

## 8. 为什么 `decision_trace` 取代了日志

回到 L3 §6 的取舍 A。现在可以看得更具体了：`decision_trace` 有 9 个写入方、11 个读取方——**它是这个项目的日志表**。区别在于：

| 传统 Java 服务 | 这个项目 |
|---|---|
| `log.info(...)` 追加到文件 | `merge_appended(decision_trace, ...)` 写进 State |
| 日志不可回放 | Checkpoint 可以恢复到任意时刻 |
| 日志格式靠约定 | `DecisionRecord` 是 `TypedDict` |
| 只能人看 | 节点可以读它做决策（比如 `_started_call_ids`、`_approval_target_changed`） |
| 幂等性无所谓 | **必须幂等**，所以 ID 是确定性派生的 |

最后一行是真正的分野：**日志是"给未来的人看的"，State 是"给未来的自己用的"**。给未来的自己用，就必须能重放、能去重。

代价是 §3.2 那个"静默消失"的坑。

---

## 9. 读 `state.py` 的六条建议

1. **先看 §7.1 的表，别通读。** 你要找某个字段的值从哪来，就去那张表搜。
2. **看到 `Annotated[list[X], operator.add]` 就去找 `merge_appended`。** 如果某个写入方直接返回裸列表而没过滤，那是一个 bug 或一处待补的技术债——可以对照 §2.5 的表核查。
3. **看到 `state.get("x")` 是正常写法，不是防御性编程。** 覆盖语义的字段真的可能不存在（§1.3 实测）。
4. **看到 `run_id` / `tool_call_id` / `salt` 出现在 ID 里，先问"它在防哪种串味"**：跨轮次串味（§3.1）、跨任务串味（§4.3）还是同轮次撞车（§3.2）。
5. **改一个字面量字段名不会有任何报错。** `TypedDict` 只在类型检查器里存在（§1.1）。项目有 `mypy` 配置（`disallow_untyped_defs=true`），跑一次 typecheck 比跑测试更快发现这类错。
6. **`RuntimeContext` 与 `AgentState` 的分界线是"这次调用" vs "这个会话"。** 拿不准一个东西该放哪边，就用这条问自己。

---

## 10. 自查清单

1. `AgentState` 有几个字段？三种更新语义各多少个？ → §2（35 / 26 / 7 / 1 / 1）
2. 首次 `invoke` 时传入的列表，是被"设为初始值"还是"追加进去"？ → §2.2（追加）
3. `operator.add` 为什么不够用？ → §2.3（重放 + 不去重）
4. 7 个追加通道里，哪两个不走 `merge_appended`？为什么？ → §2.5
5. 为什么 `tool_scope_key` 要带 `run_id`，只用 `tool_call_id` 会怎样？ → §3.1（第二轮被误判"已执行"，静默跳过）
6. `make_decision_record` 的 `salt` 参数是干什么的？不加会怎样？ → §3.2（ID 撞车 → 记录静默消失）
7. 为什么 `RuntimeContext` 没有 `frozen=True`？ → §6.1（`load_agents` 要写 `agents_context`）
8. `MemoryCandidate` 为什么没有"作用域"字段？ → §4.3（架构 §10.4 明确不新增）
9. `PlanStep.status` 为什么不是 `Literal` 而 `MemoryCandidate.memory_type` 是？ → §4.3（前者取值还在演进）
10. 哪个节点完全不碰 `AgentState`？ → §7.1（`load_agents_node`）

---

## 下一步

State 讲完了，接下来是它的**骨架**——节点怎么被连起来、什么条件下走哪条路。

- **L5 · 流程骨架**：[edges.py](../../local-agent-web/src/local_agent/graph/edges.py)（143 行，11 条固定边 + 6 组条件边）+ [routers.py](../../local-agent-web/src/local_agent/graph/routers.py)（350 行，7 个路由判断函数）。
- 你会看到 L0 讲过的"条件边不能改 State"这个限制，是怎么被"节点 + 条件边两段式"绕过去的（`completion_router_node` + `completion_route`）。

建议接着写 **L5**。
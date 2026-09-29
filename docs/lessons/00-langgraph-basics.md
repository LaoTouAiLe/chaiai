# L0 · LangGraph 核心 API

> 对应代码：[`src/local_agent/graph/`](../../local-agent-web/src/local_agent/graph)
> 前置要求：会写 Python 函数、知道什么是 dict。
> 读完你能：看懂 [`builder.py`](../../local-agent-web/src/local_agent/graph/builder.py) 的每一行；理解节点函数为什么长成那个样子；明白"为什么要有 reducer"。

---

## 0. 为什么要先读这篇

这个项目的流程编排全部由 LangGraph 承担。如果不认识它的 API，读 [`nodes.py`](../../local-agent-web/src/local_agent/graph/nodes.py) 时会遇到一堆无法解释的写法：

```python
def mode_router_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    ...
    return {"mode": result.mode, "decision_trace": merge_appended(...)}
```

疑问会堆积成：`runtime` 是谁传进来的？为什么只返回两个字段而不是整个 State？`merge_appended` 为什么要手动调用？

**答案全在 LangGraph 的模型里**，这篇就是讲这个模型。

### 本篇的版本前提

本机实测（`importlib.metadata.version`）：

```
langgraph                    1.2.11
langgraph-checkpoint         4.2.0
langgraph-checkpoint-sqlite  3.1.1
langchain-core               1.6.3
langchain-openai             1.6.2
```

**注意这是 LangGraph 1.x。** 1.x 与 0.x 有断裂性变化（例如 0.x 里节点的第二参数是 `config`，1.x 里是 `Runtime` 且额外有 `context_schema` 这一层）。**网上大量教程还是 0.x，照着抄会踩坑。** 本篇所有签名都是从本机安装的包源码里 dump 出来的，不是凭记忆写的。

---

## 1. LangGraph 是什么

一句话：**它把"一个大函数"拆成"若干个只做一件事的节点 + 一张有向图"，框架负责按边执行，并在每个节点执行前后自动保存状态。**

### 它解决的三个真实痛点

| 痛点 | 用普通代码写会怎样 | LangGraph 怎么解 |
|---|---|---|
| 流程要能**中断和恢复** | 审批挂起 → 进程退出 → 状态全丢，只能从头再来 | Checkpoint：每一步的状态都落库，可以按 `thread_id` 恢复到断点 |
| 流程要**循环** | 写 `while True` + 一堆状态变量，越写越乱，还容易死循环 | 图可以有环；循环次数是显式状态字段，可以设上限 |
| 流程要**可测试** | 模型、数据库、安全策略全写死在函数里，测试必须打桩 | 节点函数只从 `Runtime` 取依赖，测试时注入假对象即可 |

**Java 类比**：把它想成一个**带持久化的事务日志的状态机引擎**。
`Node` ≈ `Step`（或一个 `@Component` 的处理方法），`State` ≈ `JobExecutionContext`，`Edge` ≈ 下一步的转移条件，`Checkpointer` ≈ 每一步提交一次的事务日志。

需要注意一处**不一致**：Spring Batch 的 Step 是"读—处理—写"三段式，而 LangGraph 的节点是纯函数式的——**节点不持有状态，状态在节点之间传递**。这是设计上的根本差别，别把 `Step` 的直觉直接套过来。

### 五个核心概念

| 概念 | 是什么 | 本项目里的位置 |
|---|---|---|
| **State** | 流程中携带的数据（一个 dict） | [`graph/state.py`](../../local-agent-web/src/local_agent/graph/state.py) 的 `AgentState` |
| **Node** | 一个"只做一件事"的函数 | [`graph/nodes.py`](../../local-agent-web/src/local_agent/graph/nodes.py) 里 16 个 |
| **Edge** | 节点之间的连接（顺序或条件） | [`graph/edges.py`](../../local-agent-web/src/local_agent/graph/edges.py) |
| **Reducer** | 同一字段被多次更新时的合并规则 | `state.py` 里的 `Annotated[...]` |
| **Runtime** | 一次调用的依赖容器（模型、策略、配置） | `state.py` 的 `RuntimeContext` |

前四个是 LangGraph 的概念，第五个是**本项目的做法**（LangGraph 只提供 `Runtime` 这个容器，装什么由你决定）。

---

## 2. 建图三步

### 2.1 第一步：声明图的类型

```python
# 本项目的真实写法：src/local_agent/graph/builder.py:76
graph: ChatStateGraph = StateGraph(AgentState, context_schema=RuntimeContext)
```

`StateGraph.__init__` 的真实签名（本机 dump）：

```python
__init__(
    self,
    state_schema: type[StateT],
    context_schema: type[ContextT] | None = None,
    *,
    input_schema: type[InputT] | None = None,
    output_schema: type[OutputT] | None = None,
    **kwargs,
)
```

| 参数 | 作用 | 本项目 |
|---|---|---|
| `state_schema` | State 的类型定义。**必填**，第一个参数 | `AgentState`（在 `state.py` 里定义） |
| `context_schema` | 注入给节点的"依赖容器"的类型 | `RuntimeContext` |
| `input_schema` | 流进图的数据类型，和 State 不同才要写 | 不用 |
| `output_schema` | 流出图的数据类型，和 State 不同才要写 | 不用 |

`state_schema` 传进去的类，是**用来决定"每个字段怎么合并"的**（见第 4 节 reducer），它不是一个运行时对象。

### 2.2 第二步：加节点 `add_node`

```python
# 本项目的真实写法：builder.py:77-92
graph.add_node(PREPARE_CONTEXT, prepare_context_node)   # 名字在前，函数在后
```

真实签名（本机 dump，只挑对初学者有用的部分）：

```python
add_node(
    self,
    node: str | StateNode,              # 节点名，或直接给函数
    action: StateNode | None = None,    # 节点函数
    *,
    defer: bool = False,
    retry_policy: RetryPolicy | Sequence[RetryPolicy] | None = None,
    cache_policy: CachePolicy | None = None,
    error_handler: StateNode | None = None,
    timeout: float | timedelta | TimeoutPolicy | None = None,
    ...
) -> Self
```

**两种写法都合法**：

```python
graph.add_node("my_node", my_node)   # 显式指定名字（本项目用这种，名字是常量）
graph.add_node(my_node)              # 省略名字，自动取函数名 "my_node"
```

本项目统一用第一种，因为节点名是**常量**（在 `nodes.py` 里定义，`edges.py` 也 import 同一批常量）。好处是**连边时不可能拼错名字**——写错一个字母会在 import 时就报错，而不是运行时才发现图接不上。

`add_node` 还支持 `retry_policy`（失败重试）、`timeout`（超时）、`cache_policy`（节点级缓存）等。**本项目一个都没用**，但知道它们存在，以后想加就查得到。

> 注意返回类型是 `Self`——所以可以链式调用：`StateGraph(S).add_node(...).add_edge(...).compile()`。

### 2.3 第三步：连边

#### 固定边 `add_edge`

```python
add_edge(self, start_key: str | list[str], end_key: str) -> Self
```

```python
# 本项目的真实写法：edges.py:75-78
graph.add_edge(START, PREPARE_CONTEXT)
graph.add_edge(PREPARE_CONTEXT, LOAD_AGENTS)
graph.add_edge(LOAD_AGENTS, RECALL_MEMORY)
graph.add_edge(RECALL_MEMORY, MODE_ROUTER)
```

- 语义：`start_key` 执行完，**无条件**去 `end_key`。
- `start_key` 可以是 `list[str]`（多个节点都指向同一个下游，用于汇聚）。
- `START` 和 `END` 是 LangGraph 的内置常量（`from langgraph.graph import START, END`），不是普通字符串。`add_edge(START, "first_node")` 的意思是"图从这里开始"。

#### 条件边 `add_conditional_edges`

```python
add_conditional_edges(
    self,
    source: str,
    path: Callable[..., Hashable | Sequence[Hashable]],
    path_map: dict[Hashable, str] | list[str] | None = None,
) -> Self
```

**三个参数，记住"从哪出发 / 怎么判断 / 去哪"**：

```python
# 本项目的真实写法：edges.py:80-84
graph.add_conditional_edges(
    MODE_ROUTER,                                              # ① 从哪个节点出发
    route_by_mode,                                            # ② 用哪个函数判断
    {ROUTE_CHAT: COMPRESSION_NODE, ROUTE_TOOL: PLANNER_NODE}, # ③ 返回值 → 去哪
)
```

判断函数要做的事非常简单——**返回一个字符串**：

```python
# edges.py 引用的判断函数，定义在 routers.py:223-225
def route_by_mode(state: AgentState) -> str:
    return ROUTE_TOOL if state.get("mode") == "tool" else ROUTE_CHAT
```

三个必须记住的点：

1. **判断函数只能读 State，不能改 State。** 它返回的是"选择结果"，框架拿这个结果去查映射表。
2. **映射表就是"这张图上所有可能的下一步"**。如果判断函数返回了一个表里没有的字符串，LangGraph 会直接报错——相当于穷尽性检查。
3. **`path_map` 可以省略**（传 `None`），此时判断函数的返回值必须**直接是节点名**。本项目传了 map，用常量做键，更清晰。

### 2.4 第四步：编译 `compile`

```python
compile(
    self,
    checkpointer: Checkpointer = None,
    *,
    cache: BaseCache | None = None,
    store: BaseStore | None = None,
    interrupt_before: All | list[str] | None = None,
    interrupt_after: All | list[str] | None = None,
    debug: bool = False,
    name: str | None = None,
    ...
) -> CompiledStateGraph
```

```python
# 本项目的真实写法：builder.py:94
return graph.compile(checkpointer=checkpointer)
```

| 参数 | 作用 |
|---|---|
| `checkpointer` | 传进去就开启持久化；不传则图**无状态**（跑完就忘） |
| `interrupt_before` / `interrupt_after` | 在指定节点前后强制暂停（静态中断） |
| `store` | 跨会话的长期存储（本项目用的是自己的 `memory/`，没用它） |

**"编译"发生在程序启动时，不是每次对话时。** 本项目在 `web.py` 里启动时编译一次，之后每轮对话都调用同一张编译好的图。

---

## 3. 节点函数的契约

### 3.1 能收几个参数（有一条**实测**出来的规则）

节点函数的第一个参数永远是 **State**。第二个参数是可选的，**按参数名识别**：

| 第二个参数的名字 | 注入什么 | 本项目用不用 |
|---|---|---|
| `runtime` | 一个 `Runtime` 对象（依赖容器，见第 5 节） | ✅ 所有节点都用 |
| `config` | 一个 dict（RunnableConfig，含 `configurable` 等） | ❌ |
| 其他名字 | **不注入** → 函数会因为缺参数报 `TypeError` | — |

这条规则我实测过（下面是完整可运行的验证代码，输出是真实结果）：

```python
from typing import TypedDict
from dataclasses import dataclass
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime

@dataclass
class Ctx:
    tag: str

class S(TypedDict, total=False):
    out: str

def a(state: S, runtime: Runtime[Ctx]) -> S:
    return {"out": "有注解 + 名叫 runtime: " + runtime.context.tag}

def b(state: S, runtime) -> S:
    return {"out": "无注解 + 名叫 runtime"}

def c(state: S, config) -> S:
    return {"out": "名叫 config: " + type(config).__name__}

for name, fn in [("a", a), ("b", b), ("c", c)]:
    g = StateGraph(S, context_schema=Ctx).add_node(name, fn).add_edge(START, name).add_edge(name, END).compile()
    print(name, "->", g.invoke({}, context=Ctx(tag="T")))
```

实测输出：

```
a -> {'out': '有注解 + 名叫 runtime: T'}
b -> {'out': '无注解 + 名叫 runtime'}
c -> {'out': '名叫 config: dict'}
```

结论：**必须叫 `runtime` 或 `config`**；`runtime` 那个参数**写不写类型注解都能注入**（写了注解只是为了让 mypy 和 IDE 知道 `runtime.context` 是什么类型）。

> 反过来验证也做过：把参数名写成 `rt`、`whatever`、`x`，三种全部报 `TypeError: missing 1 required positional argument`。
> 所以**不要顺手给第二个参数起别的名字**，这是纯粹的浪费时间的坑。

### 3.2 返回值：只写你改过的字段

```python
# 本项目的真实写法：nodes.py 里的 mode_router_node（routers.py:139-145）
return {
    "mode": result.mode,
    "route_reason": result.reason,
    "decision_trace": merge_appended(...),
}
```

**节点不需要返回完整 State，只返回它改动的那几个键。** 没提到的字段保持原值。

这是为什么 `AgentState` 要声明成 `total=False`（所有字段可选）—— 节点只提交自己那份"补丁"。

三种合法返回值：

```python
return {"mode": "tool"}      # 改了 mode
return {}                    # 什么都没改（合法，见 routers.py:276）
return {"a": 1, "b": 2}      # 改多个
```

**为什么要 `return {}` 而不是 `return None`？** 本项目 [`completion_router_node`](../../local-agent-web/src/local_agent/graph/routers.py#L264) 就是用 `return {}` 表示"这次判断结果是不需要改任何状态"。写 None 语义上不自明。

### 3.3 节点里**不能**做的事

| 不要做 | 原因 |
|---|---|
| 直接改传入的 `state` 对象（`state["x"] = 1`） | 状态更新要靠**返回值**，直接改不会生效/会破坏框架的合并逻辑 |
| 假设节点只执行一次 | **节点可能被重放**（见第 7 节），所以有副作用的操作要有幂等性 |
| 把"依赖"写死在函数里（自己 new 一个模型客户端） | 那样就没法测试了；依赖应该从 `runtime.context` 取 |

---

## 4. Reducer：State 字段的合并规则

### 4.1 问题

节点返回 `{"messages": [新消息]}`，框架是**替换**整个 `messages` 列表，还是**追加**一条？

答案：**看你有没有给字段加"合并规则"标注。没标注就是替换。**

### 4.2 四种情况

```python
from typing import Annotated
import operator
from langgraph.graph.message import add_messages
from local_agent.graph.state import merge_mapping

class 示例(TypedDict, total=False):
    a: int                                              # ① 无标注 → 覆盖
    b: Annotated[list[str], operator.add]               # ② → 拼接（不去重）
    c: Annotated[list, add_messages]                    # ③ → 消息专用追加
    d: Annotated[dict[str, str], merge_mapping]         # ④ → 自定义合并（本项目自己写的）
```

| 规则 | 谁提供 | 行为 | 项目里的字段 |
|---|---|---|---|
| 无标注 | — | 新值覆盖旧值 | `mode`、`loop_count`、`task_status`… |
| `operator.add` | Python 标准库 | 列表拼接，**不查重** | `tool_results`、`decision_trace`、`completed_steps`… |
| `add_messages` | LangChain 提供 | 追加消息，带消息级语义 | `messages` |
| 自定义函数 | 本项目 | 本项目写了 `merge_mapping` / `merge_appended` | `reply_overrides` 等 |

### 4.3 `operator.add` 的真实行为（实测）

注意一个**容易误判**的点：传入的**初始值也会走 reducer**。

```python
from typing import TypedDict, Annotated
import operator
from langgraph.graph import StateGraph, START, END

class S(TypedDict, total=False):
    n: int
    log: Annotated[list[str], operator.add]

def a(state: S) -> S:
    return {"n": 1, "log": ["a"]}

def b(state: S) -> S:
    return {"log": ["b"]}

g = StateGraph(S)
g.add_node("a", a)
g.add_node("b", b)
g.add_edge(START, "a")
g.add_edge("a", "b")
g.add_edge("b", END)
print(g.compile().invoke({"n": 0, "log": ["init"]}))
```

实测输出：

```
result = {'n': 1, 'log': ['init', 'a', 'b']}
```

两个结论：
- `log` 是 `['init', 'a', 'b']`——**输入值 `["init"]` 被当成了第一次追加**，所以列表开头有它。
- `n` 是 `1`——没有标注，被覆盖。

### 4.4 `add_messages` 的特殊之处（实测）

它不是普通的"列表拼接"，它带**消息 ID 语义**：同 ID 的消息会被**替换**，不是追加。

```python
from langchain_core.messages import AIMessage

def dup(state) -> dict:
    return {"messages": [AIMessage(content="改写后", id="m1")]}

# 输入里已有 id="m1" 的消息
r = g2.invoke({"messages": [AIMessage(content="原始", id="m1")]})
```

实测输出：

```
同ID消息 -> 条数: 1 | 内容: ['改写后']
```

**这个行为是理解本项目 `messages` 字段的关键**：模型的流式输出/修正回答时，可以用"同一个消息 ID 覆盖旧内容"的方式更新，而不用自己维护列表下标。

### 4.5 为什么本项目还要自定义 reducer

因为 `operator.add` **只负责拼接，不负责去重**。而本项目里有两个必须去重的现实原因（详见 L4）：

1. **节点会被重放**（恢复 Checkpoint 时同一个节点可能再跑一次），追加型字段会被重复写入。
2. **模型的 `tool_call_id` 是可预测的**，不同轮次可能拿到同一个 ID。

所以项目里配套提供了 `merge_appended(existing, incoming, key)`——先算出已有条目的 ID 集合，只追加没见过的。

---

## 5. Runtime：依赖注入

### 5.1 它是什么

`Runtime` 是 LangGraph 提供的"运行期上下文"对象，节点的第二个参数拿到的就是它。本机 dump 出来的字段（源码原文摘录）：

| 字段 | 是什么 |
|---|---|
| `context` | **你自己定义的东西**（`context_schema` 的实例）。本项目把模型、安全策略、工具表全放这儿 |
| `store` | LangGraph 的长期存储（本项目未使用） |
| `stream_writer` | 往自定义流里写数据的函数 |
| `previous` | 上一次的返回值（仅函数式 API + checkpointer 时可用） |
| `execution_info` | 本次节点执行的元信息（只读） |

### 5.2 本项目的用法

```python
# state.py 里定义容器的形状（简化）
class RuntimeContext(TypedDict, total=False):
    workspace_root: Path
    model_adapter: BaseChatModel
    security_policy: SecurityPolicy
    tool_registry: ToolRegistry
    app_config: AppConfig
    ...

# builder.py:137-149 在每次调用前构造它
return RuntimeContext(
    workspace_root=workspace_root,
    model_adapter=model_adapter,
    security_policy=policy,
    tool_registry=registry,
    ...
)

# 节点里取用（routers.py:127-132）
def mode_router_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    result = decide_mode(user_text=..., context=runtime.context)
```

**为什么要把这些放进 `RuntimeContext`，而不是让节点自己 new？** [`builder.py` 开头的 docstring](../../local-agent-web/src/local_agent/graph/builder.py#L1-L5) 把这个设计讲得很直接：

> 模型、配置与安全策略都不放在图里，而是随每次调用的 `RuntimeContext` 注入（架构 §4.1）：**同一张编译好的图既能用真实模型，也能用测试假模型与假策略。**

**Java 类比**：这就是 `@Autowired` 的构造函数注入版。区别是 Java 靠容器（Spring）解析依赖，这里是**每次 `invoke` 时显式传一个对象进去**——更啰嗦，但也更透明：依赖从哪来，一眼看得到，不需要理解容器的作用域规则。

> 顺带解释一个疑问：**为什么 `RuntimeContext` 用 `TypedDict` 而不是普通类？** 因为它只是个"装依赖的袋子"，不需要方法、不需要校验。L2 会展开三种数据类（`TypedDict` / `dataclass` / `pydantic`）的取舍。

---

## 6. 跑图：`invoke` 的三个参数

```python
# 本项目的真实写法：src/local_agent/web.py:213-217
result = app.graph.invoke(
    payload,                                              # ① 输入：初始 State 的一部分
    config={"configurable": {"thread_id": thread_id}},     # ② 配置：会话键在这里
    context=context,                                      # ③ 依赖：RuntimeContext 实例
)
```

| 参数 | 传什么 | 说明 |
|---|---|---|
| 第 1 个（位置） | dict | 初始 State。**只需要给你关心的字段**，其余留空 |
| `config` | dict | `{"configurable": {"thread_id": ...}}` 是**Checkpoint 的分区键**——同一个 `thread_id` 的多次 invoke 共享同一份状态 |
| `context` | `context_schema` 的实例 | 节点里 `runtime.context` 拿到的就是它 |

### `thread_id` 是什么

**它是"会话"的唯一标识，是 Checkpoint 的分区键。**
换一个 `thread_id` = 换一个全新的会话；沿用同一个 `thread_id` = 接着上次的状态继续跑。

所以本项目所有涉及状态的读写都带着它：

```python
# builder.py:162 读某个会话当前的状态
snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
return dict(snapshot.values or {})      # .values 就是当前的 State
```

`get_state` 返回的 `snapshot` 有两个常用属性：`values`（当前 State）和 `config`（含 `checkpoint_id`，见 `web.py:389-391`）。

### 本项目为什么用 `invoke` 而不是 `stream`

`invoke` 是"跑完整张图，一次性拿到最终 State"。本项目图层面用 `invoke`；需要在页面上**逐字显示**的流式效果发生在**模型层**（每次 LLM 调用的内部），不是图层。两者不要混淆。

---

## 7. Checkpoint：为什么会"重放节点"

`compile(checkpointer=...)` 之后，LangGraph 会在**每个节点执行前后**都保存一次状态。本项目用的是 `SqliteSaver`（[`persistence/checkpoint.py`](../../local-agent-web/src/local_agent/persistence/checkpoint.py)）。

这带来一个**必须知道的副作用**：

> **恢复时，节点可能会被重新执行一次。**

因为"保存状态"和"节点产生副作用"之间不是原子的。如果在节点跑到一半时中断，恢复时框架会用最近一次干净的检查点重跑那个节点。

这直接决定了两条项目约束：

1. **有副作用的节点必须幂等**：所以项目里到处是 `merge_appended` 去重，而不是简单 `operator.add`。
2. **`interrupt()` 之前不能有任何副作用**：[`nodes.py:3013`](../../local-agent-web/src/local_agent/graph/nodes.py#L3013) 的注释原话是"本节点在 resume 时会被整体重放，因此 `interrupt()` 之前不能有任何副作用"。

**Java 类比**：像消息队列的"至少一次投递"——消费者必须幂等。这里同理：节点是"至少执行一次"的。

---

## 8. 人工中断：`interrupt()` + `Command(resume=...)`

这是 LangGraph 里"人在回路"（human-in-the-loop）的标准做法，本项目用它实现审批。

### 8.1 两个 API

本机 dump 的真实签名：

```python
# from langgraph.types import interrupt, Command
interrupt(value: Any) -> Any

Command(*, graph=None, update=None, resume=None, goto=())
```

| API | 在哪用 | 作用 |
|---|---|---|
| `interrupt(value)` | **节点函数内部** | 把 `value` 抛给调用方并**挂起**整个图。下次恢复时，它的**返回值**就是你传进来的 `resume` 值 |
| `Command(resume=...)` | 调用 `invoke` 时，**代替输入** | 通知图"从挂起点继续，并把这份数据作为 `interrupt()` 的返回值" |

### 8.2 本项目怎么用

```python
# nodes.py:3025-3031（简化）
first = _as_mapping(interrupt(_approval_prompt(request, stage=1)))
if _approved(first) and request["requires_second_confirmation"]:
    second = _as_mapping(interrupt(_approval_prompt(request, stage=2)))
```

**递归删除要问两次**：第一次 `interrupt()` 挂起，用户点"批准"后图恢复，`first` 拿到用户的回答；如果这个动作需要二次确认，就**再挂起一次**。第一次确认本身不构成授权。

恢复侧的代码：

```python
# web.py:366-370
result = app.graph.invoke(
    Command(resume=dict(resume_value)),                    # 注意：输入位置放的是 Command
    config={"configurable": {"thread_id": thread_id}},
    context=context,
)
```

### 8.3 挂起状态下不能干的事

[`web.py:10`](../../local-agent-web/src/local_agent/web.py#L10) 的注释说明了这条约束：

> 待审批的会话只能经 `Command(resume=...)` 恢复，不能用普通用户输入覆盖挂起状态。

判断某个会话是否处于挂起状态，用的是 `get_state` 里有没有中断信息（`web.py:329` 的 `thread_interrupt`）。**这是"状态机"思维：挂起是一种显式状态，不是"没反应"。**

---

## 9. 一个能跑的最小例子

把前面所有概念串起来，20 行。**下面这段是实测跑过的**：

```python
from typing import TypedDict, Annotated
import operator
from langgraph.graph import StateGraph, START, END

class S(TypedDict, total=False):
    n: int                                        # 覆盖语义
    log: Annotated[list[str], operator.add]       # 追加语义

def a(state: S) -> S:
    return {"n": 1, "log": ["a"]}

def b(state: S) -> S:
    return {"log": ["b"]}

g = StateGraph(S)                                 # ① 声明 State 类型
g.add_node("a", a)                                # ② 加节点
g.add_node("b", b)
g.add_edge(START, "a")                            # ③ 连边
g.add_edge("a", "b")
g.add_edge("b", END)
app = g.compile()                                 # ④ 编译

print(app.invoke({"n": 0, "log": ["init"]}))      # ⑤ 跑
```

实测输出：

```
{'n': 1, 'log': ['init', 'a', 'b']}
```

跑它的命令（用本项目的虚拟环境）：

```powershell
& "e:\pylianxi\chaiai\local-agent-web\.venv\Scripts\python.exe" your_script.py
```

---

## 10. API 速查表

| 想干的事 | 写法 | 本项目位置 |
|---|---|---|
| 声明图 | `StateGraph(State类, context_schema=Context类)` | `builder.py:76` |
| 加节点 | `add_node("名字", 函数)` | `builder.py:77-92` |
| 固定跳转 | `add_edge(A, B)` ／ `add_edge(START, 第一个节点)` | `edges.py:75-78` |
| 条件跳转 | `add_conditional_edges(A, 判断函数, {返回值: 目标})` | `edges.py:80-84` |
| 编译 | `graph.compile(checkpointer=...)` | `builder.py:94` |
| 节点读 State | 第一个参数 `state`，用 `state.get("字段")` | 所有节点 |
| 节点读依赖 | 第二个参数必须**叫 `runtime`**，用 `runtime.context.xxx` | `routers.py:127` |
| 节点改 State | `return {"字段": 新值}` | 所有节点 |
| 跑一次 | `graph.invoke(输入, config=..., context=...)` | `web.py:213` |
| 读状态 | `graph.get_state({"configurable": {"thread_id": ...}}).values` | `builder.py:162` |
| 挂起问人 | 节点内 `interrupt(数据)` | `nodes.py:3025` |
| 恢复 | `graph.invoke(Command(resume=数据), config=...)` | `web.py:366` |
| 消息追加 | `Annotated[list[AnyMessage], add_messages]` | `state.py:24` |
| 列表追加 | `Annotated[list[X], operator.add]` | `state.py` 多处 |

---

## 11. 常见误解

| 误解 | 事实 |
|---|---|
| "节点返回的 dict 会替换整个 State" | 只替换你写出的那几个键 |
| "`operator.add` 会去重" | 不会，只拼接。要自己去重（本项目用 `merge_appended`） |
| "节点只会执行一次" | 恢复 Checkpoint 时可能重放，所以要有幂等设计 |
| "条件边函数可以顺便改 State" | 不可以。要改状态就得拆出一个节点（见 `completion_router` 的节点 + 条件边两段式） |
| "第二个参数随便起名" | 必须叫 `runtime`（注入 Runtime）或 `config`（注入 dict），其他名字不注入 |
| "`context` 和 `config` 是一回事" | 不是。`context` 是**你定义的依赖对象**；`config` 是框架的配置（`thread_id` 在里面） |
| "1.x 和 0.x 只是版本号差别" | API 有断裂性变化，抄 0.x 的教程会报错 |

---

## 12. 以后遇到不认识的 API，怎么 30 秒查清楚

**不用搜，直接问装在你机器上的包。** 这是本篇最重要的一个习惯。

```powershell
# ① 看方法签名（参数名、类型、默认值）
& "e:\pylianxi\chaiai\local-agent-web\.venv\Scripts\python.exe" -c "import inspect; from langgraph.graph import StateGraph; print(inspect.signature(StateGraph.add_conditional_edges))"

# ② 看类的完整源码（含官方 docstring 和示例）
& "e:\pylianxi\chaiai\local-agent-web\.venv\Scripts\python.exe" -c "import inspect; from langgraph.runtime import Runtime; print(inspect.getsource(Runtime))"

# ③ 看装的是什么版本（决定你该看哪一版文档）
& "e:\pylianxi\chaiai\local-agent-web\.venv\Scripts\python.exe" -c "from importlib.metadata import version; print(version('langgraph'))"

# ④ 实测某个行为（比读文档更可信）
& "e:\pylianxi\chaiai\local-agent-web\.venv\Scripts\python.exe" -c "你的验证代码"
```

**为什么这比搜索引擎好**：官方文档描述的是最新版，博客描述的是作者当时那一版，**只有本机源码描述的是你正在用的这一版**。本项目用 `langgraph>=1.0,<2` 区间约束，任何一次 `uv sync` 都可能改变细节行为。

---

## 下一篇

**L1 · LangChain 消息与工具调用 API**：`HumanMessage` / `AIMessage` / `ToolMessage` 的区别、模型怎么"提出"工具调用（`tool_calls`）、`bind_tools` 干了什么、结构化输出怎么把 JSON 变成对象、以及为什么"工具调用"本质上是模型返回的一段结构化数据而不是模型真的去执行了工具。

这是读懂 [`nodes.py`](../../local-agent-web/src/local_agent/graph/nodes.py) 里 `agent_decision_node` 和 `tool_execution_node` 的前提。
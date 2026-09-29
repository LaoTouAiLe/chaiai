# L1 · LangChain 消息与工具调用 API

> 对应代码：[`graph/state.py`](../../local-agent-web/src/local_agent/graph/state.py) 的 `messages` 字段、
> [`graph/nodes.py`](../../local-agent-web/src/local_agent/graph/nodes.py) 的 `agent_decision_node` / `tool_execution_node`、
> [`memory/short_term.py`](../../local-agent-web/src/local_agent/memory/short_term.py)、[`llm/`](../../local-agent-web/src/local_agent/llm)
> 前置：L0（LangGraph 核心 API）
> 读完你能：看懂"模型提出工具调用 → 系统执行 → 结果回填"这条链路；知道 `ToolMessage` 为什么必须带 `tool_call_id`。

---

## 0. 本篇解决的问题

这个项目里最核心的一句话是：

> **模型提出工具调用 → 安全门 → 工具执行 → 结果回填 → 循环直到给出最终回答**

L0 讲清了"谁在调度这条循环"（LangGraph）。**本篇讲"每一次循环里来回传的是什么"**——答案是**消息对象（Message）**，而不是字符串。

如果你把 `messages` 当成 `list[str]` 去理解，后面一定会卡住：`tool_calls` 存在哪里？工具结果怎么和调用对上？为什么裁剪历史时"不能从中间切开"？

---

## 1. 消息不是字符串，是对象

LangChain 定义了一套消息类，项目直接从 `langchain_core.messages` 导入：

```python
# state.py:23、web.py:30、short_term.py:37 等
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
```

### 四种基本类型（字段实测）

| 类 | `type` 的值（实测） | 谁产生的 | 用途 |
|---|---|---|---|
| `SystemMessage` | `system` | **你**（程序） | 系统提示词：规则、工具说明、AGENTS.md |
| `HumanMessage` | `human` | **用户** | 用户输入 |
| `AIMessage` | `ai` | **模型** | 模型的回答；**也可能是工具调用请求** |
| `ToolMessage` | `tool` | **你的代码**（执行完工具后） | 工具执行结果，回填给模型 |

实测代码与输出：

```python
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

print(HumanMessage(content="hi").type)          # human
print(SystemMessage(content="sys").type)        # system
print(AIMessage(content="").type)               # ai
print(ToolMessage(content="{}", tool_call_id="call_1").type)  # tool
```

上面四个 `type` 的值是我实跑出来的。**`type` 字段很重要**：项目里 `memory/summarizer.py:92` 的 `_role_of()` 就是靠它把消息渲染成 `"human: ..."` 这样的文本。

### `AnyMessage` 是什么

```python
# state.py:318
messages: Annotated[list[AnyMessage], add_messages]
```

`AnyMessage` 是上面几种消息类型的**联合类型**（union），等价于"任意一种消息"。它的作用只是让类型标注写得简短——运行时它就是这些类的实例，没有任何包装。

> **Java 类比**：`AnyMessage` ≈ 一个接口类型，`HumanMessage`/`AIMessage` 是它的实现类。区别是 Python 里它只是一个 `Union[...]` 别名，没有真正的继承体系约束。

### `AIMessage` 的实际字段（实测 dump）

```
['additional_kwargs', 'content', 'id', 'invalid_tool_calls', 'name',
 'response_metadata', 'tool_calls', 'type', 'usage_metadata']
```

初学者只需要关心其中四个：

| 字段 | 是什么 |
|---|---|
| `content` | 正文文本（可能是 `str`，也可能是分块列表） |
| `tool_calls` | **模型提出的工具调用请求列表**（下面重点讲） |
| `id` | 消息 ID（`add_messages` 靠它判断"替换还是追加"，见 L0 第 4.4 节） |
| `usage_metadata` | token 用量（项目用它记账） |

另外有个 `text` 属性（实测存在）：它是"把 `content` 里的文本部分取出来"的便捷写法，项目里出现过 `getattr(response, "text", response)` 这种写法（[`ui/streaming.py:183`](../../local-agent-web/src/local_agent/ui/streaming.py#L183)）。

---

## 2. 工具调用的本质：模型**不执行**工具

**这是初学者最容易误解的一点。**

模型能做的只有一件事：**返回一段结构化数据，说"我建议调用 read_file，参数是 {path: a.txt}"**。它没有能力碰你的磁盘。

真正执行的是你的代码。所以整条链路是：

```
① 你：把"工具说明书"交给模型（bind_tools）
② 模型：返回 AIMessage，其中 tool_calls = [{name: "read_file", args: {...}, id: "call_1"}]
③ 你：解析 tool_calls，过安全门，执行工具
④ 你：造一条 ToolMessage(content=执行结果, tool_call_id="call_1") 追加到消息列表
⑤ 你：把整个消息列表再发给模型 → 模型看到结果，继续回答或再提一个调用
```

### `tool_calls` 的真实结构（实测）

```python
m = AIMessage(content="", tool_calls=[
    {"name": "read_file", "args": {"path": "a.txt"}, "id": "call_1", "type": "tool_call"}
])
print(m.tool_calls)
```

实测输出：

```python
[{'name': 'read_file', 'args': {'path': 'a.txt'}, 'id': 'call_1', 'type': 'tool_call'}]
```

每个元素是 `ToolCall`（`state.py:23` 从 `langchain_core.messages` 导入），四个键：

| 键 | 含义 |
|---|---|
| `name` | 工具名（必须和 `bind_tools` 时给的清单里的名字**完全一致**） |
| `args` | 参数字典（**由模型生成**，所以要校验合法性） |
| `id` | 这次调用的唯一编号——**回填结果时靠它配对** |
| `type` | 固定是 `"tool_call"` |

> **`args` 是模型生成的**，这句话的含义比看起来重：模型完全可能给出 `{"path": 123}` 或者缺字段。所以项目里每个工具函数开头都要自己解析参数并返回参数错误（参见 [`tools/file_tools.py`](../../local-agent-web/src/local_agent/tools/file_tools.py)）。
>
> `AIMessage` 还有一个 `invalid_tool_calls` 字段，是"参数不合法、连解析都失败了"的调用的落点——模型给的 JSON 坏了，不会直接抛异常，而是被放到这里。

### 为什么必须有 `id`

因为**模型一轮可以提多个工具调用**，而结果要按顺序回填。没有 `id` 就无法把"第 2 个调用的结果"对应到"第 2 个调用"上。

更重要的是：`id` 会参与本项目**安全层**的逻辑。审批指纹里**故意不包含 `tool_call_id`**——因为 `tool_call_id` 是模型生成的、**可以预测和复用**，如果把它放进指纹，模型换个 ID 重新提交就能绕过"拒绝记录"（详见 L7 安全层）。

---

## 3. `bind_tools`：把工具说明书交给模型

```python
# 本项目的真实用法：llm/models.py:82（探测模型是否支持工具调用）
model.bind_tools([probe_tool])
```

`[probe_tool]` 就是"工具说明书"列表。它的语义是：

> **给这个模型绑定一份可用工具清单；此后每次调用，这份清单都会随请求一起发给模型。**

三个必须理解的点：

1. **`bind_tools` 不执行任何工具**，它只是**附加了一份声明**（工具名、参数 schema、用途描述）。
2. **它返回一个新的模型对象**（绑定后的），不是原地修改。这跟 Java 里 `builder` 模式返回新对象是一个思路。
3. **模型知道有哪些工具，完全来自这份清单**。所以"模型调了一个不存在的工具名"这种情况，根因通常是清单和实际注册表不一致。

本项目的设计是：**图里不放工具清单**，工具注册表由 `RuntimeContext` 注入（`builder.py:133` 的 `ToolRegistry.build(policy)`），节点在决策时再把它交给模型。这样测试时可以换一份工具表。

---

## 4. `ToolMessage`：把结果回填给模型

实测：

```python
t = ToolMessage(content='{"ok": true}', tool_call_id="call_1")
print(t.type, t.tool_call_id)     # tool call_1
```

要点：

| 要点 | 说明 |
|---|---|
| **必须带 `tool_call_id`** | 它把这条结果和某个 `tool_calls[i].id` 配对 |
| `content` 是**字符串** | 通常放 JSON 文本。模型读的是文本，不是对象 |
| 由**你的代码**产生 | 模型永远不会生成 `ToolMessage` |

**每一个 `tool_calls[i]` 都必须有一条对应的 `ToolMessage`。** 如果漏了，模型会看到一个"要调用工具但没有结果"的残缺序列，它的下一步行为是不可预测的（可能重复调用、可能直接编造结果）。

这正是本项目在"一轮有多个待审批动作"时，**对未处理的调用也要回填一条说明性消息**的原因（见 [`nodes.py:783-806`](../../local-agent-web/src/local_agent/graph/nodes.py#L783) 附近）。

### 项目里的"工具步"定义

[`memory/short_term.py:20`](../../local-agent-web/src/local_agent/memory/short_term.py#L20) 的注释给出了一个很实用的概念：

> "工具步"的定义：一条 `AIMessage(tool_calls)` 及其紧随其后的 `ToolMessage`。**只按轮次裁剪**……（不能从 `AIMessage(tool_calls)` 与对应 `ToolMessage` 中间切开）

**白话**：消息列表是一条**有配对的线性历史**，裁剪历史（为了省 token）时不能把"调用"和"结果"拆散——否则模型会看到一个"我调用过、但没有结果"的悬空记录，比不裁剪更糟。

**这是"消息列表不是普通列表"的最好例证。** 后面读 `context/`（上下文压缩）和 `memory/short_term.py` 时，这个约束是理解它们全部逻辑的前提。

---

## 5. 结构化输出：把模型的 JSON 变成对象

除了"工具调用"，还有一种让模型输出结构化数据的方式：**结构化输出**。

```python
# 本项目的真实用法：llm/protocol.py:78
result = model.with_structured_output(schema, method="json_schema").invoke(payload)
```

| | `bind_tools` | `with_structured_output` |
|---|---|---|
| 目的 | 让模型**提出动作** | 让模型**回答一个格式固定的问题** |
| 输出落在哪 | `AIMessage.tool_calls` | 直接是一个对象（如 pydantic 实例） |
| 项目里的例子 | 决定要调哪个文件工具 | `routers.py` 里的意图分类 `RouteDecision` |

本项目的做法是把它包成一个函数 `call_structured(...)`（[`llm/protocol.py`](../../local-agent-web/src/local_agent/llm/protocol.py)），里面是一条**降级链**：优先用模型原生的 JSON Schema 能力，不支持就退化为"用提示词要求它输出 JSON，然后自己解析"，再不行就重试一次并带上错误信息。

> 原因很实际：**不是所有 OpenAI 兼容服务都实现了完整的结构化输出能力**。这条"降级链"就是为兼容性服务的，细节在 L9 讲 `llm/` 时展开。

---

## 6. 项目里两个真实的坑

这两个坑都不是"理论上应该注意"，而是**已经踩过并写进注释**的。

### 6.1 供应商扩展字段必须原样回传

[`llm/compat.py`](../../local-agent-web/src/local_agent/llm/compat.py) 的开头写着：

> 为什么必须接：DeepSeek 的思考模式下，**带 `tool_calls` 的 assistant 消息在下一轮请求里必须**（原样回传它给过的字段）……实测两级踩坑

**白话**：某些模型（如 DeepSeek 的思考模式）在下一次请求时，要求把上一轮返回的扩展字段（例如 `reasoning_content`）原封不动带回去，否则服务端直接返回 400。

这类坑的通用教训是：**消息对象里那些你不关心的字段，不代表可以丢**。它们存在 `additional_kwargs`、`response_metadata` 这些"杂项"字典里，项目为此专门写了 `compat.py` 处理。

### 6.2 token 预算必须算上 `tool_calls.args`

[`context/token_budget.py:151-159`](../../local-agent-web/src/local_agent/context/token_budget.py#L151) 的注释：

> 用鸭子类型读取 `content` 与 `tool_calls`（不导入 langchain 的消息类）：`str` 直接估算……
> `tool_calls[].args` 里，而它们同样是**出站** Prompt 的一部分——漏算会让大参数的工具调用……

**白话**：估算上下文长度时，不能只数 `content` 的字符——**`tool_calls[].args` 也是要发给模型的**。写一个 500 行的 `write_file` 调用，光参数就可能上千 token。漏算它会导致"以为没超限、实际上超了"。

顺便注意那句"不导入 langchain 的消息类"——它用 `getattr(message, "tool_calls", None)` 这种**鸭子类型**（不管对象是什么类型，只要有这个属性就用），目的是让这个模块不依赖 langchain。这是**降低耦合**的常见手法（Java 里相当于"面向接口而非具体类"，只不过 Python 连接口都不声明）。

---

## 7. 速查表

| 想干的事 | 写法 | 项目位置 |
|---|---|---|
| 造用户消息 | `HumanMessage(content=text)` | `web.py:780` |
| 造系统提示 | `SystemMessage(content=prompt)` | `memory/summarizer.py:70`、`llm/protocol.py:89` |
| 读模型的工具调用 | `message.tool_calls` → `[{name, args, id, type}]` | `nodes.py` 的 `agent_decision_node` |
| 取消息类型 | `message.type` → `human`/`ai`/`system`/`tool` | `memory/summarizer.py:92` |
| 回填工具结果 | `ToolMessage(content=..., tool_call_id=calls[i]["id"])` | `nodes.py` 的 `tool_execution_node` |
| 给模型工具清单 | `model.bind_tools([...])` | `llm/models.py:82` |
| 结构化输出 | `model.with_structured_output(模型类).invoke(...)` | `llm/protocol.py:78` |
| 消息列表类型 | `Annotated[list[AnyMessage], add_messages]` | `state.py:318` |
| 取消息 ID | 见 `graph/projection.py` 的 `message_id()` | `projection.py:24` |

---

## 8. 常见误解

| 误解 | 事实 |
|---|---|
| "模型会自己执行工具" | 不会。模型只返回"建议调用"的结构化数据，执行永远是你的代码 |
| "`messages` 是一个字符串列表" | 是消息对象列表，每条带 `type`/`content`/`tool_calls` 等字段 |
| "`ToolMessage` 可以不写 `tool_call_id`" | 必须写，否则无法与调用配对 |
| "`tool_calls` 里的 `args` 是可信的" | 不是，它由模型生成，所以每个工具都要校验参数 |
| "裁剪历史就是切列表" | 不能把 `AIMessage(tool_calls)` 和它的 `ToolMessage` 切开 |
| "消息里不认识的字段可以丢掉" | 某些供应商要求原样回传，否则 400 |
| "算 token 只看 `content`" | 还要算 `tool_calls[].args` |

---

## 9. 下一篇

**L2 · Python 语法与 Java 对照手册**：`TypedDict` / `@dataclass(frozen=True)` / `pydantic.BaseModel` 三者的取舍、`Annotated`、海象运算符、`Path` 与 `difflib` 等本项目高频出现的标准库、以及"为什么 Python 的循环 import 比 Java 危险"。

这一篇是**查字典用的**，不需要一次读完；建议先扫一遍目录，以后遇到看不懂的语法再回来查。
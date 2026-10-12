# L2 · Python 语法与 Java 对照手册

> 这是**查字典**，不是教程。不要一次读完，先扫一遍目录，以后读代码遇到看不懂的语法再回来查。
> 所有例子都来自本项目真实代码，并标了 `文件:行号`。
> 前置：不需要。可以在任何阶段回来翻。

---

## 0. 目录

| 节 | 内容 |
|---|---|
| 1 | 四个根本差别（先建立正确的心智模型） |
| 2 | 数据类三兄弟：`TypedDict` / `@dataclass` / `pydantic` |
| 3 | 类型标注进阶：联合类型、`Annotated`、`Literal`、`Protocol`、`TypeVar` |
| 4 | 装饰器 ≈ Java 注解（但会真的执行代码） |
| 5 | 函数签名的花样：`*`、`**`、默认参数的坑 |
| 6 | 那些看起来奇怪的语法（海象、推导式、真值判断……） |
| 7 | 标准库速查（项目里真的用到的） |
| 8 | 模块与包：为什么循环 import 在 Python 更危险 |
| 9 | 错误处理：Python 没有 checked exception |
| 10 | Java 程序员最容易写错的 12 件事 |
| 11 | 速查对照表 |

---

## 1. 四个根本差别

### 1.1 类型标注**不参与运行**，只是给工具看的

| | Java | Python |
|---|---|---|
| 类型何时检查 | **编译期**，错了编译不过 | **从不检查**。`mypy` 是外部工具，不跑它就没检查 |
| 类型错了会怎样 | 编译失败 | 运行时才炸（或者悄悄错下去） |
| 类型的作用 | 语言的一部分 | **文档 + IDE 提示 + mypy 检查** |

**这不是"差不多"，是本质不同。** 一个类型标了 `int`、实际传了字符串，Python 会照常跑，直到那一行出问题。

所以本项目才把 `mypy` 当成必跑项（见 `pyproject.toml` 的 `[tool.mypy]` 段，配置了 `disallow_untyped_defs = true`：**所有函数都必须写类型**）。这是在用工具补语言的短板。

```python
def loop_limit_reached(state: AgentState) -> bool:   # 标注只是给人看和给 mypy 看的
    limit = state.get("max_loop_count")
    if not limit:
        return False
    return int(state.get("loop_count", 0)) >= int(limit)   # 注意这里为什么要 int()
```

上面这段来自 [`routers.py`](../../local-agent-web/src/local_agent/graph/routers.py)。注意最后一行**为什么要包 `int()`**：`state` 是 `total=False` 的 dict，值可能来自反序列化的 JSON，**类型标注不保证运行时真的是 int**。这就是"类型不参与运行"的直接后果——**在边界上要自己转**。

### 1.2 函数是对象，可以当值传来传去

```python
# edges.py:85-89：把函数本身当参数传进去
graph.add_conditional_edges(MODE_ROUTER, route_by_mode, {...})
```

Java 里这需要 `Function<T,R>` 或方法引用；Python 里**函数就是普通对象**，直接写名字（不加括号）就传过去了。加括号 `route_by_mode(state)` 是**调用并传返回值**，不加括号才是**传函数本身**——这个区别是本项目里最常见的读代码障碍之一。

### 1.3 没有 `private` / `interface`，靠**约定**和**结构化类型**

| Java | Python | 本项目 |
|---|---|---|
| `private void helper()` | `def _helper()`（下划线开头） | 到处可见，如 `policy.py` 的 `_classify_edit` |
| `interface AnswerSink` | `class AnswerSink(Protocol)` | [`llm/stream_context.py:11`](../../local-agent-web/src/local_agent/llm/stream_context.py#L11) |
| `final` | 没有（`Final` 只是给 mypy 看的） | — |

下划线约定**没有任何强制力**：`_foo` 照样能被外部调用。它只是在说"别碰，这是内部实现"。

`Protocol` 更值得注意（第 3.4 节详讲）：它相当于 Java 的接口，但**不需要实现类显式声明 implements**——只要有对应的方法，就算实现了。这叫**结构化类型**。

### 1.4 缩进就是语法

没有花括号，没有分号。**缩进错了代码就错了**（不是"格式不好看"，是语法错误）。本项目的缩进统一是 4 空格（`line-length = 100` 由 ruff 把关）。

---

## 2. 数据类三兄弟：`TypedDict` / `@dataclass` / `pydantic`

这是**本项目最重要的一个取舍**，三种都大量使用。先给结论表：

| | `TypedDict` | `@dataclass(frozen=True)` | `pydantic.BaseModel` |
|---|---|---|---|
| 运行时是什么 | **就是 dict** | 一个普通对象 | 一个带校验的对象 |
| 运行时校验 | ❌ 完全没有 | ❌ 完全没有 | ✅ **有**（类型不对会抛异常） |
| 能加方法 | ❌ | ✅ | ✅ |
| 能不可变 | ❌（dict 永远可改） | ✅ (`frozen=True`) | ✅（可配置） |
| 能序列化成 JSON | ✅ 直接可以 | 需要转换 | ✅ 自带 `.model_dump()` |
| 性能 | 最好 | 好 | 最差（每次构造都校验） |
| Java 近似 | `Map<String,Object>` + 注释 | `record` | DTO + Bean Validation |
| 本项目用在哪 | **State、工具结果记录、审批记录** | **内部流转的小对象** | **模型输出、工具返回给模型的 JSON** |

### 2.1 `TypedDict`：运行时就是 dict

```python
# graph/state.py
class AgentState(TypedDict, total=False):
    thread_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    ...
```

**为什么 State 必须用 `TypedDict`？** 因为它要能被序列化进 SQLite（Checkpoint），而且 LangGraph 的 State 就是 dict 语义。用 pydantic 会带来不必要的校验开销，而且"只返回改动的字段"这种局部更新也更自然。

`total=False` 的含义：**所有键都可选**。所以节点可以只返回自己改的那几个键（见 L0 第 3.2 节）。

对比一下 `result_types.py:208` 的 `ToolResultRecord(TypedDict)`——**没有** `total=False`，表示所有字段都必填。

```python
class ToolResultRecord(TypedDict):     # 全部必填
    tool_call_id: str
    tool_name: str
    status: ToolResultStatus
    ...
```

**这类"必填 vs 可选"的差别，是类型标注里信息量最大的一处**，读代码时值得留意。

### 2.2 `@dataclass(frozen=True)`：不可变的 record

```python
# security/policy.py:90-96
@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    requires_approval: bool
    reason: str
```

`@dataclass` 自动生成 `__init__`、`__repr__`、`__eq__`——相当于 Java 的 `record`。**本项目有 30 多处用它**，几乎全是 `frozen=True`。

`frozen=True` 的效果：

```python
d = SafetyDecision(allowed=True, requires_approval=False, reason="只读工具")
d.allowed = False        # ❌ 抛 FrozenInstanceError
```

**为什么项目里几乎全都不可变？** 因为这些对象会被放进 State、被多个节点读取。不可变就排除了"某个节点偷偷改了别人正在用的对象"这类问题——这是**并发/共享数据的基本功**，和 Java 里做不可变值对象是同一个道理。

还有两个变体：

```python
# llm/embedding.py:564、reflection/memory_candidates.py:78
@dataclass(frozen=True, kw_only=True)     # 构造时必须写参数名
```

`kw_only=True` 强制调用方写 `字段名=值`。好处是**参数字典顺序变了也不会错**——三个都是 `str` 的字段最容易搞混，强制写名字就杜绝了。

**默认值要小心**（第 5.3 节详讲）：

```python
# reflection/validators.py:32
reasons: tuple[str, ...] = field(default_factory=tuple)
```

### 2.3 `pydantic.BaseModel`：带运行时校验

```python
# tools/result_types.py:19-36
class DirEntry(BaseModel):
    name: str
    path: str
    kind: EntryKind                                    # Literal["file","dir","symlink","other"]
    size_bytes: int | None = None
    modified_at: str | None = None

class ListDirResult(BaseModel):
    path: str
    entries: list[DirEntry] = Field(default_factory=list)
    total_entries: int = 0
    truncated: bool = False
```

**为什么工具返回结构要用 pydantic？** 因为它的内容是**给模型看的 JSON**，必须保证字段真的存在、类型真的对。用 `TypedDict` 的话，少写一个字段不会有任何提示，模型就会收到残缺的 JSON。

pydantic 的核心价值：**构造时校验，不合规立刻抛异常**。

```python
DirEntry(name="a", path="/x", kind="乱七八糟")   # ❌ 抛 ValidationError（kind 不在 Literal 里）
DirEntry(name="a", path="/x", kind="file")      # ✅
```

这跟 Java 的 Bean Validation（`@NotNull` / `@Valid`）是一个位置的东西，但 **pydantic 是主动校验，不需要额外注解**。

### 2.4 一句话选型规则

> **要进 Checkpoint / 要当 State 字段 → `TypedDict`；只在代码内部传 → `@dataclass(frozen=True)`；要校验外部来的数据（模型输出、工具返回）→ `pydantic`。**

---

## 3. 类型标注进阶

### 3.1 联合类型 `X | None`

```python
def find_rejection(...) -> ApprovalDecision | None:      # 可能是对象，也可能是 None
    ...
```

等价于 `Optional[ApprovalDecision]`（老写法）。Java 对应 `@Nullable`，但 Python 的 `|` 是**语言语法**，不依赖注解库。

**项目里的读法**：看到 `| None` 就说明"这个函数可能失败/可能没有结果"，调用方必须判空。比如 `state.get("x")` 可能返回 `None`。

### 3.2 `Annotated[X, 元数据]`：给类型挂附加信息

```python
# graph/state.py
messages: Annotated[list[AnyMessage], add_messages]
```

`Annotated[X, V]` 的字面意思是"类型是 X，另外附带一个 V"。**V 是什么由使用它的框架决定**：

| 使用者 | V 的含义 | 项目例子 |
|---|---|---|
| **LangGraph** | 字段的**合并规则**（reducer） | `Annotated[list[AnyMessage], add_messages]` |
| **pydantic** | 字段的**校验/元信息** | `Annotated[int, Field(ge=0)]` |
| 一般情况 | 元数据，没人用就没人用 | — |

**关键理解**：`Annotated` 本身不做任何事，**它是给别人的信号**。你写 `Annotated[list, add_messages]`，LangGraph 才知道"这个列表用追加语义"（详见 L0 第 4 节）。

### 3.3 `Literal`：只能是这几个值之一

```python
# tools/result_types.py:15-16
EntryKind = Literal["file", "dir", "symlink", "other"]
ToolResultStatus = Literal["ok", "error", "denied"]
```

Java 对应 `enum`。**但这个项目里完全没有用 `Enum`**——全部用 `Literal` 字符串。

原因很实际：这些值要**序列化成 JSON 给模型看**。`Literal` 运行时就是普通字符串，`"ok"` 就是 `"ok"`；而 `Enum` 到 JSON 需要额外的转换配置。**用字符串换取了序列化的无痛。**

代价是：**mypy 世外有效，运行时毫无约束**——你传 `Literal` 之外的值，Python 不会拦（pydantic 用 `Literal` 才会拦）。

### 3.4 `Protocol`：不用 implements 的接口

```python
# llm/stream_context.py:11
class AnswerSink(Protocol):
    def push(self, text: str) -> None: ...     # 只有签名，没有实现
```

**语义**：任何有 `push(self, text: str) -> None` 方法的对象，**都算 `AnswerSink`**——不需要显式继承。

| | Java 接口 | Python `Protocol` |
|---|---|---|
| 实现方怎么写 | `class X implements AnswerSink` | 什么都不写，有方法就行 |
| 检查时机 | 编译期 | **只有 mypy 检查**（运行时不做） |
| 好处 | 明确 | 解耦：实现方不需要依赖接口所在的模块 |

**为什么这在本项目里重要？** 看 [`llm/embedding.py:235`](../../local-agent-web/src/local_agent/llm/embedding.py#L235) 的 `EmbeddingFunction(Protocol)`：它是为了满足 **chromadb 的期望**。chromadb 要求传入的 embedding 函数必须有特定名字的方法，用 `Protocol` 描述这个"期望的形状"，项目就不用去 import chromadb 的类型。

> 这也解释了为什么 `Protocol` 的定义经常出现在"为了适配外部库"的地方——**它天生就是用来解耦的**。

### 3.5 `TypeVar`：泛型

```python
# graph/state.py:105
T = TypeVar("T")

# llm/protocol.py:51 —— 带约束的泛型
SchemaT = TypeVar("SchemaT", bound=BaseModel)
```

Java 里写 `<T>`，Python 里要先"声明"一个 `T = TypeVar("T")`，然后就能：

```python
def merge_appended(existing: list[T], incoming: list[T], key: Callable[[T], str]) -> list[T]:
    ...
```

`bound=BaseModel` 相当于 Java 的 `<T extends BaseModel>`：限定 T 必须是 BaseModel 的子类。

**下标泛型**也是同样的写法：

```python
# graph/builder.py:88
ChatGraph = CompiledStateGraph[AgentState, RuntimeContext, AgentState, AgentState]
```

就是"给这个类型填上四个类型参数"。

### 3.6 `from __future__ import annotations`：几乎每个文件都有

本项目**几乎所有文件**第一行都是它。作用是：

> **让所有类型标注变成"字符串"，延迟到需要时才解析。**

为什么需要？因为它解决了两个问题：

1. **前向引用**：A 类的方法要标注返回 A 类本身，此时 A 还没定义完。Java 里不存在这个问题（编译器先扫全文件）。
2. **省性能**：注解不参与运行，没必要每次导入时都真的去解析这些类型。

**副作用（值得知道）**：因为注解变成了字符串，运行时如果想读注解必须用 `typing.get_type_hints()`，直接 `func.__annotations__` 拿到的是字符串。**LangGraph 注入 `runtime` 参数时用的是参数名而不是注解**（L0 第 3.1 节实测过），所以它不受这个影响。

---

## 4. 装饰器 ≈ Java 注解（但会真的执行代码）

**这是最容易误解的一处**：Java 的注解默认什么都不做（靠处理器扫描），而 **Python 的装饰器是一个真的会被调用的函数**。

```python
@dataclass(frozen=True)
class SafetyDecision:
    ...
```

等价于：

```python
class SafetyDecision:
    ...
SafetyDecision = dataclass(frozen=True)(SafetyDecision)   # 把类传给函数，拿返回值覆盖原名
```

**装饰器 = "把下面的东西传给一个函数，用返回值替换它"**。理解了这句，所有装饰器都能读。

本项目用到的装饰器：

| 装饰器 | 作用 | 项目例子 |
|---|---|---|
| `@dataclass` | 自动生成 `__init__`/`__eq__` 等 | 30+ 处 |
| `@property` | **把方法变成"属性"**，访问时不用写括号 | [`session_store.py:73`](../../local-agent-web/src/local_agent/persistence/session_store.py#L73) |
| `@classmethod` | 第一个参数是**类**而不是实例，常用于替代构造器 | [`policy.py:125`](../../local-agent-web/src/local_agent/security/policy.py#L125) 的 `from_config` |
| `@pytest.fixture` | 声明测试夹具 | `tests/` 目录（见 L11） |
| `@abstractmethod` 等 | 本项目未使用 | — |

### `@property` 的真正用途

```python
# ShellPlan 里（security/shell_guard.py）
@property
def uses_shell_adapter(self) -> bool:
    return self.executor != "argv"
```

调用时写 `plan.uses_shell_adapter`（**不加括号**）。这跟 Java 的 `getUsesShellAdapter()` 目标一样（调用方看不出是字段还是计算值），但**没有 `get` 前缀**。

**读代码时的坑**：在 Python 里看到一个"字段"，它可能是属性、可能真的是字段，从调用处看不出来。要确认就去类定义里找 `@property`。

### `@classmethod` 替代构造器

```python
# security/policy.py:124-132（简化）
@classmethod
def from_config(cls, app_config, *, workspace_root, environ=None) -> SecurityPolicy:
    ...
    return cls(workspace_root=..., security=..., ...)
```

`cls` 就是类本身（类似 Java 的静态方法，但**能拿到类**，所以子类继承时也正确）。**Java 里这叫"静态工厂方法"**（`SecurityPolicy.fromConfig(...)`），Python 里用 `@classmethod` 实现，是惯用写法。

**为什么项目爱用 `from_config` 而不是在 `__init__` 里做所有事？** 因为构造函数应该只做"赋值"，而"从配置推导"这种带逻辑的构造适合拆出来——这也是"依赖显式化"的一部分。

---

## 5. 函数签名的花样

### 5.1 `*`：强制关键字参数

```python
# security/policy.py（简化）
def __init__(
    self,
    *,
    workspace_root: Path,
    security: SecurityConfig,
    workspace: WorkspaceConfig,
    secret_values: tuple[str, ...] = (),
) -> None:
```

`*` 之后的参数**必须写名字**调用：

```python
SecurityPolicy(workspace_root=p, security=s, workspace=w)   # ✅
SecurityPolicy(p, s, w)                                     # ❌ TypeError
```

**本项目大量使用这个写法。** 原因是：当有 3-4 个参数、其中好几个类型相同（都是 `Path` 或都是 `str`）时，位置参数极易写错且不会报错。强制写名字 = **把"靠记忆"变成"靠语法"**。

> 读代码时看到孤零零一个 `*`，含义就是这个——**不是通配符，是"以下必须写关键字"**。

### 5.2 `**kwargs` / `*args`

| 写法 | 含义 |
|---|---|
| `def f(*args)` | 收集多余的位置参数成一个元组 |
| `def f(**kwargs)` | 收集多余的关键字参数成一个字典 |
| `f(*my_list)` | **解包**：把列表铺开当位置参数传 |
| `f(**my_dict)` | **解包**：把字典铺开当关键字参数传 |

L0 里 `StateGraph.__init__(..., **kwargs)` 的 `**kwargs` 是用来接"废弃参数"的（`Unpack[DeprecatedKwargs]`）——这是库作者为了**给出更友好的报错**而留的口子。

### 5.3 默认参数的坑：`=[]` 只求值一次

**Java 里不存在这个问题**，所以 Java 程序员几乎一定踩：

```python
def f(x=[]):        # ❌ 危险！这个列表在所有调用之间共享
    x.append(1)
    return x

f()   # [1]
f()   # [1, 1]      ← 不是 [1]！默认值只创建了一次
```

正确写法：

```python
def f(x=None):
    x = [] if x is None else x
```

本项目在 dataclass 里的处理方式是 `field(default_factory=...)`：

```python
# reflection/validators.py:32
reasons: tuple[str, ...] = field(default_factory=tuple)

# security/shell_guard.py:128
env_keys: tuple[str, ...] = field(default_factory=tuple)
```

`default_factory=tuple` 的意思是"**每次构造这个对象时调用一次 `tuple()`**"，而不是共享同一个值。**语义上等价于 Java 的构造函数里写 `this.reasons = new ArrayList<>()`。**

> 顺便注意：项目里用的是 `tuple` 而不是 `list`。因为 `frozen=True` 的对象如果含可变字段（list），"不可变"就是假的——**用 tuple 才真的不可变**。这是个很讲究的细节。

---

## 6. 那些看起来奇怪的语法

### 6.1 海象运算符 `:=`（Python 3.8+）

**语义：在表达式里顺便赋值，并把这个值作为表达式的值。**

本项目只用了 4 次，全是经典用法：

```python
# security/policy.py:156-158 —— 在推导式里算一次、用一次
secret_values = tuple(
    value for name in secret_names if (value := env.get(name, "").strip())
)

# security/approval.py:290、tools/file_tools.py:684 —— 边读边判
while chunk := handle.read(1024 * 1024):
    ...    # 读到空就结束循环

# llm/embedding.py:147
if isinstance(value := getattr(constants, name, None), str):
    ...
```

**读法**：`while chunk := handle.read(...)` = "读一块，赋给 `chunk`，然后判断它是不是空"。
**为什么需要它**：没有它就得写成"先赋值、再判断、循环体里再赋值一次"，重复代码。

**Java 类比**：Java 没有对应语法。`(line = reader.readLine()) != null` 是最接近的写法——Python 的海象就是这个，但**必须写括号**才不会和 `=` 语句混淆。

### 6.2 推导式（comprehension）

```python
[c * 2 for c in items if c > 0]                    # 列表
{k: v for k, v in pairs}                            # 字典
{x for x in items}                                  # 集合
tuple(v for name in names if (v := env.get(name)))  # 生成器 → 转元组
```

**Java 对应**：`stream().filter().map().collect()`。Python 的推导式是这个的语法糖，但**没有惰性求值**（列表推导立刻算完；用圆括号才是惰性生成器）。

项目里 `policy.py:157` 就是"过滤 + 映射"一步完成，等价于 Java：

```java
Arrays.stream(secretNames).map(n -> env.get(n)).filter(v -> v != null && !v.isBlank())...
```

### 6.3 三元表达式：**顺序和 Java 相反**

```python
mode = "tool" if is_tool else "chat"        # Python
```

```java
String mode = isTool ? "tool" : "chat";     // Java
```

Python 是"**值 如果 条件 否则 值**"，Java 是"**条件 ? 值 : 值**"。读了三个小时代码之后这个顺序错误很常见。

项目里的真实例子（`routers.py`）：

```python
return ROUTE_TOOL if state.get("mode") == "tool" else ROUTE_CHAT
```

### 6.4 真值判断：`if not x` 的含义比 Java 广

Python 里这些值**都为假**：`None`、`False`、`0`、`0.0`、`""`、`[]`、`{}`、`()`、`set()`。

```python
# graph/routers.py —— 一行同时处理"没配"和"配成 0"
limit = state.get("max_loop_count")
if not limit:
    return False
```

**Java 里 `if (!limit)` 编译不过（类型不对）**，Python 里既可以，语义还刚好是这里想要的"没启用上限"。

`and` / `or` 也返回**操作数本身**，不是布尔值：

```python
# graph/state.py —— 取不到就当空串
return tool_scope_key(record.get("run_id") or "", record["tool_call_id"])
```

`a or b` 的语义是"a 为假就取 b"，所以这里 `None or ""` 得到 `""`。**Java 的 `||` 只能返回 boolean，这是本质区别。**

### 6.5 解包

```python
a, b = b, a                       # 交换（Java 需要临时变量）
first, *rest = [1, 2, 3]          # first=1, rest=[2,3]
f(*args, **kwargs)                # 铺开传参
```

项目里 `main` 附近的参数解析、`shell_tools` 拼 argv 时都用得到。

### 6.6 f-string

```python
f"工作区外路径：{target.as_posix()}；{verb}工作区外目标必须人工审批"
```

前缀 `f` = 格式化。常用修饰符：

| 写法 | 效果 |
|---|---|
| `{name!r}` | 用 `repr()` 而不是 `str()`（字符串会带引号，调试时看清类型） |
| `{value:.2f}` | 保留两位小数 |
| `{value:>8}` | 右对齐占 8 位 |
| `{value:,}` | 千分位 |

`!r` 在项目里出现得多（日志/错误信息里区分 `"abc"` 和 `abc`）。

### 6.7 `with`：等价于 Java 的 try-with-resources

```python
with sqlite3.connect(path) as conn:      # 退出时自动 close（或 commit）
    ...
```

**但有个 Python 特有的坑**：`with` 到底做什么，取决于对象实现的 `__enter__` / `__exit__`。同一个 `with`，对文件是"自动关"，对数据库连接是"自动提交事务"，对锁是"自动释放"。**看代码时不要假设。**

### 6.8 `yield`：生成器（≈ 惰性迭代器）

```python
def iter_lines(path):
    with open(path) as handle:
        for line in handle:
            yield line            # 产出一个值，然后"暂停"在这里
```

调用 `iter_lines(p)` **不执行任何代码**，只是得到一个可迭代对象；每次 `for` 取下一个值时，函数才从上次 `yield` 的地方继续。

**Java 类比**：`Iterator` + 内部状态，或者 Stream 的惰性求值。好处是**不必一次把所有内容读进内存**（本项目读大文件、逐行扫描时用到）。

**测试里更重要**：`pytest` 的 fixture 用 `yield` 来**划分 setup 和 teardown**（yield 之前是准备、之后是清理）——见 L11。

### 6.9 异常语法

```python
try:
    ...
except WorkspaceBoundaryError as exc:        # 捕获，as 绑定异常对象
    return SafetyDecision(allowed=False, ...)
except (ValueError, TypeError):              # 一次捕多种
    ...
finally:
    policy.revoke(fingerprint)               # 无论成败都执行
```

**没有 `catch`，是 `except`；没有 `throws` 声明。** `raise ... from exc` 保留原始异常链（相当于 Java 的 `initCause`）：

```python
raise SecurityError("...") from exc
```

### 6.10 下划线约定

| 写法 | 含义 |
|---|---|
| `_helper()` | 内部函数，别从外面调 |
| `__init__` / `__eq__` / `__call__` | 双下划线开头结尾 = 语言级别的钩子（不叫"私有"，叫"魔法方法"） |
| `_`（单个下划线变量） | 约定俗成的"我不关心这个值" |

`__call__` 值得单独说：**实现了它的对象可以像函数一样被调用**。`llm/embedding.py` 里的 `EmbeddingFunction` 就要求实现 `name()` 和 `__call__` —— 这是 chromadb 的约定（见 `pyproject.toml` 里那段关于 chromadb 的注释）。

---

## 7. 标准库速查（项目里真的用到的）

### 7.1 `pathlib.Path`（替代 `os.path` 拼字符串）

| 写法 | 结果 | Java 对应 |
|---|---|---|
| `Path("a/b") / "c"` | `a/b/c`（自动处理分隔符） | `Paths.get(a,b)` |
| `Path("x/../y.txt").resolve()` | 展开成真实绝对路径 | `toRealPath()` |
| `.parts` | `('a', '.git', 'config')` 逐段元组 | — |
| `.name` / `.parent` / `.parents` | 文件名 / 父目录 / **所有祖先目录** | `getFileName()` / `getParent()` |
| `.is_file()` / `.is_dir()` / `.exists()` | 类型判断 | `Files.isRegularFile()` 等 |
| `.rglob("*")` | 递归列出全部内容 | `Files.walk()` |
| `.as_posix()` | 转成 `a/b/c` 风格字符串（**Windows 上也是 `/`**） | — |
| `.read_text()` / `.write_text()` | 读写文本 | `Files.readString()` |

**为什么项目到处用 `as_posix()`？** 因为它出现在**对外展示和指纹计算**里：Windows 会得到 `E:\x\y`，而 `\` 在 JSON 和指纹字符串里是转义字符，统一转成 `/` 才不会出现"同一路径算出两个指纹"。

### 7.2 `collections.abc`

```python
from collections.abc import Mapping    # security/policy.py:20
```

| 抽象类型 | 相当于 Java |
|---|---|
| `Mapping[K, V]` | `Map<K,V>`（只读视图） |
| `Sequence[T]` | `List<T>` |
| `Iterable[T]` | `Iterable<T>` |
| `Callable[[A], B]` | `Function<A,B>` |

**为什么参数标注写 `Mapping` 而不是 `dict`？** 因为它表示"我只需要只读能力"，调用方传 dict、传自定义映射都可以。**这和 Java 里"参数类型尽量用接口"是同一条原则。**

### 7.3 `difflib.SequenceMatcher`（`security/diff_guard.py`）

它算的是**两个序列的最长匹配**，用来做 diff。核心是 `get_opcodes()`：

```python
SequenceMatcher(None, "abcd", "axcd").get_opcodes()
```

实测输出：

```python
[('equal', 0, 1, 0, 1), ('replace', 1, 2, 1, 2), ('equal', 2, 4, 2, 4)]
```

每个元组是 `(操作, 旧起始, 旧结束, 新起始, 新结束)`，五元组的后四个是**区间**（Python 的区间是**左闭右开**：`[start, end)`）：

| 操作 | 含义 |
|---|---|
| `equal` | 这段没变 |
| `replace` | 这段被替换 |
| `delete` | 旧有、新没有 |
| `insert` | 旧没有、新有 |

**这是"用标准库解决具体问题"的典型例子**：`count_changed_lines()` 只统计非 `equal` 的区间长度之和，不用自己写 diff 算法。

> **左闭右开**是 Python 的普遍约定（切片 `s[1:3]`、`range(1,3)`、这些区间都遵守）。**这是 Java 程序员最容易算错的地方**：`[1,2)` 只有 1 个元素，不是 2 个。

### 7.4 其他小工具

| 模块 | 项目里的用途 |
|---|---|
| `json` | `json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)` 算指纹——`sort_keys` 保证同样内容永远得到同样字符串 |
| `hashlib` | `sha256(...).hexdigest()` 算文件指纹 |
| `uuid` | `uuid4().hex` 生成请求 ID、审批 ID |
| `datetime` | `datetime.now(timezone.utc)`（**带时区**，不要用不带时区的 `now()`） |
| `fnmatch` | 通配符匹配敏感文件名（`.env.*`） |
| `shutil.which` | 在 PATH 里找可执行文件（等价于 `where`） |
| `os.environ` | 读环境变量（API Key 只从环境变量读） |
| `re` | 正则：`re.compile(r"\$\(|%[A-Za-z_]\w*%")` 找命令替换 |
| `subprocess` | 真正执行命令（`tools/shell_tools.py`） |
| `logging` | 日志（本项目用得**极少**，见下） |

**关于日志值得一提**：本项目几乎不用 `logging` 打日志，而是**把过程写成 State 里的结构化记录**（`tool_events`、`decision_trace`、`error_events`）。因为它是个 Agent——**这些记录既是排障信息，也是要喂给模型和展示给用户的内容**，写进日志里就浪费了。

---

## 8. 模块与包：为什么循环 import 在 Python 更危险

### 8.1 基本规则

```python
from local_agent.security.path_guard import is_within     # 绝对导入（项目约定）
from .path_guard import is_within                          # 相对导入（同一个包内可用）
```

`__init__.py` 的作用有三种（本项目三种都用到了）：

| 用法 | 例子 |
|---|---|
| 空标记文件（1 行 docstring） | `graph/__init__.py`、`tools/__init__.py` |
| **刻意保持空** | `memory/__init__.py`——为了让"没装 chromadb"也能启动（一旦 import 就会连带 import 可选依赖） |
| 门面 re-export | `llm/__init__.py`——把常用类从子模块提到包一级 |

### 8.2 循环 import：Java 免疫，Python 危险

**Java**：`A.java` 里 `new B()`、`B.java` 里 `new A()` 完全没问题。因为编译期先解析全部类，类加载是**用到时才加载**。

**Python**：`import` 是一条**会被执行的语句**，模块是按行执行的。

```python
# a.py
from b import B_class         # → 开始执行 b.py
class A_class: ...

# b.py
from a import A_class         # ❌ a.py 还没执行到 class A_class 那行，A_class 不存在 → ImportError
```

**一句话：Python 的 import 顺序敏感，因为它不是"声明"，是"执行"。**

本项目的真实情况：`memory/writer.py` import 了 `graph/state.py`，属于**能力层反向依赖编排层**（违反设计上的分层）。它今天不出问题，仅仅因为 `graph/state.py` **没有** import `memory/writer.py`。**一旦哪天 state.py 需要 writer 里的东西，就是循环 import，报错点会很隐蔽。**

**你应该记住的自保做法**：

```python
from __future__ import annotations      # 只用于类型标注时，能缓解一部分循环 import
import typing
if typing.TYPE_CHECKING:                # 只在 mypy 检查时导入，运行时完全跳过
    from local_agent.graph.state import AgentState
```

### 8.3 `if __name__ == "__main__":`

```python
if __name__ == "__main__":
    main()
```

含义：**"这个文件是被直接运行的，而不是被别人 import 的"**。Java 没有这个问题（`main` 是静态方法，不需要保护）。在 Python 里不加这层的后果是：别人一 import 你的模块，你的代码就自己跑起来了。

---

## 9. 错误处理：Python 没有 checked exception

| | Java | Python |
|---|---|---|
| 受检异常 | 有（`throws` 必须处理） | **没有** |
| 方法可能抛什么 | 签名里写着 | 只写在 docstring 的 `Raises:` 里（本项目都写了） |
| 编译器强制 | 是 | 否 |

**后果**：**你必须主动去读 docstring 才知道一个函数会不会抛异常。** 本项目的 docstring 有一致的 `Raises:` 段：

```python
"""...
Raises:
    WorkspaceBoundaryError: 空路径，或路径在工作区之外且不被允许。
"""
```

### 本项目的一套显式约定（很重要）

读过 `security/` 的话你会注意到一个反复出现的模式：**同一个模块里，有的函数抛异常，有的返回 `(值, 原因)` 元组。**

| 风格 | 什么时候用 | 例子 |
|---|---|---|
| **抛异常** | 这是"不该发生/不可恢复"的违规 | `resolve_path()` 越界 → `raise WorkspaceBoundaryError` |
| **返回 `(值, 错误说明)`** | 这是"正常业务流程里的一种结果" | shell_guard 的 `_validate_env_keys` → `(allowed, error)` |
| **返回状态对象** | 需要区分"允许/需要审批/拒绝"三种以上结果 | `SafetyDecision` |

**三种风格混用不是随意，而是按"调用方需要怎么处理"来选的**：越界是硬拒绝（调用方只能上报），环境变量名不合法是"这条命令不行、换一条"（调用方要拿原因展示）。

**Java 类比**：类似"该抛 `IllegalArgumentException` 还是返回 `Result<T>`"的取舍。Python 里因为没有受检异常，这种显式约定**必须靠文档和一致性**来维持。

---

## 10. Java 程序员最容易写错的 12 件事

| # | 陷阱 | 正确理解 |
|---|---|---|
| 1 | 三元表达式顺序 | Python 是 `值 if 条件 else 值`，**和 Java 相反** |
| 2 | `1 / 2` | 等于 `0.5`（Python 的 `/` 永远是浮点除法）。要整除写 `1 // 2` |
| 3 | `if (!x)` | Python 里空容器、`0`、`""`、`None` **全都是假** |
| 4 | `a || b` 的返回值 | Python 的 `or` 返回**操作数本身**，不是 bool |
| 5 | 默认参数 `def f(x=[])` | 默认值**只求值一次**，会被所有调用共享 → 用 `field(default_factory=...)` |
| 6 | 切片/区间的边界 | Python 是**左闭右开**：`s[1:3]` 是 2 个元素，`range(1,3)` 是 1、2 |
| 7 | 块作用域 | Python **没有块作用域**：`for` 循环变量在循环结束后依然存在 |
| 8 | 类型标注 | **运行时完全不检查**，别指望它拦住错误参数 |
| 9 | `private` | **不存在**，`_foo` 只是约定，外部照样能调 |
| 10 | 循环 import | Python 的 import 是**执行语句**，顺序敏感；Java 免疫 |
| 11 | 传参语义 | 变量装的是**引用**，函数内改可变对象会影响外部；但重新赋值不会 |
| 12 | `is` 与 `==` | `is` 比"是不是同一个对象"，`==` 比值。**判断 None 用 `is None`**（项目里 `if history else ""`、`is not None` 都是这个习惯） |

---

## 11. 速查对照表

| 想做 | Java | Python |
|---|---|---|
| 定义数据类 | `record Foo(int a) {}` | `@dataclass(frozen=True) class Foo: a: int` |
| 字典/Map | `Map<String,Object>` | `dict[str, Any]` |
| 只读 Map | `Map<K,V>`（接口） | `Mapping[K, V]` |
| 可空 | `@Nullable Foo` | `Foo \| None` |
| 枚举 | `enum Status { OK, ERROR }` | `Literal["ok", "error"]` |
| 接口 | `interface Sink { void push(String s); }` | `class Sink(Protocol): def push(self, s: str) -> None: ...` |
| 静态工厂 | `static Foo fromConfig(...)` | `@classmethod def from_config(cls, ...)` |
| getter | `getX()` | `@property def x(self)` |
| 泛型 | `<T extends Base>` | `T = TypeVar("T", bound=Base)` |
| 遍历过滤 | `stream().filter().map().collect()` | `[f(x) for x in xs if cond(x)]` |
| 资源管理 | `try (var r = open()) {}` | `with open() as r:` |
| 惰性序列 | `Stream<T>` | 生成器（`yield`） |
| 受检异常 | `throws IOException` | 无此概念（写在 docstring 的 `Raises:`） |
| 包/命名空间 | `package com.x;` | 目录 + `__init__.py` |
| 私有 | `private` | `_` 前缀（**纯约定**） |
| 主入口 | `public static void main` | `if __name__ == "__main__":` |
| 依赖声明 | `pom.xml` | `pyproject.toml` |
| 依赖隔离 | 本地 `~/.m2` + classpath | `.venv` 虚拟环境（见 L4 旧稿/环境速查） |

---

## 12. 下一步

地基三篇（L0 LangGraph / L1 消息与工具调用 / L2 语法）到这里补齐了。接下来进入项目正题：

**阶段二 · 地图**

- **L3 · 项目总览与技术选型**：13 个包、111 个文件的分工；为什么选 LangGraph / FastAPI+React / SQLite / Chroma；哪些是"刻意不用"的（如不用 LangChain 的 Chain、不用 `Enum`、不用 `logging`）
- **L4 · 数据模型**：`graph/state.py` 逐个字段讲透

建议接着写 **L3**——它能把前面三篇的孤立知识接到这个项目的具体位置上。
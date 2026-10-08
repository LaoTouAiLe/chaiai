# 从零读懂 local-agent-web

> 本目录是源码学习资料，文件行号与流程属于编写时快照；当前版本已增加 FastAPI/React 和新主循环，不能把教程当作下一步排期或现行运行契约。当前总览见 [项目入口](../../README.md)，正式启动见 [使用指南](../../local-agent-web/README.md)。

> 一套写给「有 Java 基础、但没写过 Agent」的人的代码导读。
> 目标：读完能自己看懂这个项目的每一层，能改、能扩展。

---

## 这套文档是什么

`local-agent-web` 是一个用 **LangGraph + Streamlit** 写的本地 Agent：它真的会读写你机器上的文件、真的会执行命令，并且在动手前要过一道道安全关卡。

代码本身不算多（60 个 `.py`，约 18461 行），但**看懂它的门槛不在 Python，而在三样东西**：

| 门槛 | 表现 | 对应文档 |
|---|---|---|
| LangGraph 的 API 没见过 | 不知道 `add_conditional_edges` 三个参数是什么意思、`Runtime` 从哪来 | L0 |
| LangChain 的消息与工具调用体系没见过 | 不知道 `AIMessage.tool_calls`、`ToolMessage`、`bind_tools` 的关系 | L1 |
| 分布式/框架思维用不上 | Java 的 `@Autowired`、`ExecutorService` 在这里都没有对应物 | L2 |

**先把这三样垫平，再读项目，会快很多。** 这就是本文档把 L0–L2 放在最前面的原因。

---

## 和仓库里已有文档的关系

| 文档 | 定位 | 什么时候看 |
|---|---|---|
| [`ARCHITECTURE.md`](../../ARCHITECTURE.md) | **设计文档（权威）**：代码里到处出现的 `架构 §6.3` 就指它 | 想知道"为什么这么设计"时查对应章节 |
| [`DEVELOPMENT_PLAN.md`](../archive/v0.1-v0.3-development.md) | 开发任务分解（T01、T05…），代码注释里的 `开发文档 T05` 指它 | 想知道某个模块是在哪一步加进来的 |
| [`local-agent-web/README.md`](../../local-agent-web/README.md) | 安装、配置、运行 | 要跑起来时看 |
| [`local-agent-web/CONTRIBUTING.md`](../../local-agent-web/CONTRIBUTING.md) | 提交规范 | 要改代码并提交时看 |
| **本目录** | **学习导读**：把上面的设计文档"翻译"成可逐步阅读的代码讲解 | 想弄懂代码怎么读时看 |

一句话分工：**ARCHITECTURE.md 讲设计，本目录讲"怎么读懂实现"。**

---

## 学习路线图

分四个阶段共 L0–L11，**必须按顺序**（后一篇都用前一篇的概念）。

### 阶段一 · 地基（先垫平框架 API）

| # | 标题 | 内容 | 状态 |
|---|---|---|---|
| L0 | [LangGraph 核心 API](00-langgraph-basics.md) | StateGraph / 节点 / 边 / reducer / Runtime / Checkpoint / interrupt | ✅ |
| L1 | [LangChain 消息与工具调用 API](01-langchain-messages.md) | `HumanMessage`/`AIMessage`/`ToolMessage`、`tool_calls`、`bind_tools`、结构化输出 | ✅ |
| L2 | [Python 语法与 Java 对照手册](02-python-for-java-devs.md) | `TypedDict` / `dataclass` / pydantic 的区别、`Annotated`、海象运算符、`Path`、`difflib` 等 | ✅ |

### 阶段二 · 地图（知道有哪些东西、坐在哪）

| # | 标题 | 内容 | 状态 |
|---|---|---|---|
| L3 | [项目总览与技术选型](03-project-map.md) | 五层架构、11 个包 60 个文件的分工、真实的 import 依赖图、为什么选 LangGraph/Streamlit/SQLite/Chroma、刻意不用清单 | ✅ |
| L4 | [数据模型：`graph/state.py`](04-state.md) | 全项目的"数据字典"：35 个字段逐个讲 + 三种更新语义（覆盖 / `operator.add` / `add_messages` / `merge_mapping`）+ 去重与事件 ID | ✅ |

### 阶段三 · 主干（流程怎么跑起来）

| # | 标题 | 内容 | 状态 |
|---|---|---|---|
| L5 | [流程骨架](05-edges-routers.md) | [`edges.py`](../../local-agent-web/src/local_agent/graph/edges.py) 11 条固定边 + 6 组条件边（展开 17 条）/ [`routers.py`](../../local-agent-web/src/local_agent/graph/routers.py) 三组判定 / 环路与循环上限 / 条件边为什么不能改 State | ✅ |
| L6 | [编排主干](06-nodes.md)：[`nodes.py`](../../local-agent-web/src/local_agent/graph/nodes.py) | 3470 行、14 个节点函数逐个过：读哪些 State、写哪些 State、调了什么；决策→安全门→执行→反思这条环路，以及贯穿全文件的 8 个模式 | ✅ |

### 阶段四 · 支撑模块（可以按兴趣挑着看）

| # | 标题 | 内容 | 状态 |
|---|---|---|---|
| L7 | [安全层](07-security.md)：[`security/`](../../local-agent-web/src/local_agent/security) | 2001 行、6 文件：路径守卫（边界 / 不可审批目标 / 敏感名）→ Shell 判定（切词 / 语法提级 / 黑白名单 / 执行器）→ `classify_tool` 总入口 → 动作指纹与审批作用域 → 纵深防御的十道关 | ✅ |
| L8 | [能力层](08-tools.md)：[`tools/`](../../local-agent-web/src/local_agent/tools) | 1203 行、5 文件：`ToolRegistry` 与 `MODE_TOOLS` → 模型看到的 JSON Schema → `list_dir`/`read_file`/`edit_file`/`write_file`/`delete_file`/`run_shell` 逐个拆解（有界遍历、整文件 Hash、乐观锁、patch 严格定位、原子写、`shell=False`）→ 工具结果怎么进 State | ✅ |
| L9 | [上下文与反思](09-context-reflection.md)：[`context/`](../../local-agent-web/src/local_agent/context) + [`reflection/`](../../local-agent-web/src/local_agent/reflection) | 约 1838 行：提示词十段组装与三态占位 → Token 估算与压缩阈值 → 窗口裁剪（轮次 + 工具步）→ 回答投影 → 反思三层职责 → 机械证据校验 → 记忆候选与写入意图 | ✅ |
| L10 | 记忆与检索：`memory/`（除 `short_term`）+ `rag/` | 约 3300 行：短期/中期/长期三层记忆的读写、五因子打分召回、遗忘策略、Chroma 向量库、按需代码检索 | ⬜ |
| L11 | 启动与界面：`web.py` + `ui/` + `persistence/` + `llm/` | 从点击到落盘的一次完整调用 | ⬜ |

---

## 怎么用这套文档（三条建议）

**1. 先读文档，再看代码，不要反过来。**
每篇文档里都会给出「项目里的真实文件 : 行号」。读完后立刻打开对应代码扫一眼——**文档只是地图，代码才是地形**。

**2. 每个 API 都动手跑一次。**
所有 Python 片段都能用本项目的虚拟环境直接跑（见下面的"环境速查"）。LangGraph 的概念光看文字记不住，跑一次 `invoke` 就懂了。

**3. 遇到不认识的 API，自己查，别搜。**
安装在你机器上的包源码就是最权威的文档。文档最后一节「怎么 30 秒查清一个 API」给了一条通用命令，比搜索引擎准。

---

## 环境速查

解释器与虚拟环境（本文档所有片段都按这个来）：

```
项目根：   e:\pylianxi\chaiai\local-agent-web
虚拟环境： e:\pylianxi\chaiai\local-agent-web\.venv
解释器：   e:\pylianxi\chaiai\local-agent-web\.venv\Scripts\python.exe
源码根：   e:\pylianxi\chaiai\local-agent-web\src\local_agent
```

四条常用命令（都要在 `local-agent-web` 目录下执行）：

```powershell
# 1. 装依赖（读完 pyproject.toml 后同步环境）
uv sync

# 2. 跑测试（必须 python -m，原因见 L2）
python -m pytest

# 3. 代码风格检查
uv run ruff check src tests

# 4. 类型检查
uv run mypy src
```

启动界面：

```powershell
uv run python -m local_agent.api
# 可选调试入口：uv run streamlit run src/local_agent/web.py
```

---

## 本文档的写作约定

- **每个概念配一个 Java 类比**，但不假设 Java 类比一定成立——不一致的地方会明说。
- 所有 Python 片段都是**能跑的**，实测输出会原样贴出并标注「实测」。
- 引用代码时给 `文件 : 行号`，例如 `src/local_agent/graph/builder.py:76`。
- 不确定的地方会明确写「未验证」，不用推测冒充事实。

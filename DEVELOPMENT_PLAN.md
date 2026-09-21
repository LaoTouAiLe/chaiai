# 本地网页 Agent 完整开发文档

> 架构基线：[ARCHITECTURE.md](ARCHITECTURE.md)。本文件负责“怎么实现、按什么顺序、如何验收”；架构文件负责“系统是什么、必须遵守什么契约”。两者不重复维护 State、Graph、工具签名或安全语义；出现冲突时先核对架构文档，再在单独提交中同步修订。本文件是计划，不代表功能已实现。

## 1. 范围与交付方式

交付范围是架构 §12 的 v0.1a、v0.1b、v0.2、v0.3；§14 的多模态、插件系统、知识蒸馏只预留接口，不列作必须实现。网页只在本机启动，文件与 Shell 操作发生在服务端 Python 进程（架构 §1.4）。技术栈 Python + Streamlit + LangGraph；不使用旧版 LangChain Agent。每一任务独立提交、独立验收；未满足前一阶段退出门槛，不开始依赖它的下一阶段任务。T19 首次在线下载的外部网络阻断仅按 §6 的明确例外处理，不等于宣布 v0.2 全部验收通过。

**目标产物**：可安装的开源仓库、可复现的本机演示、自动测试、README 中的已知限制与安全边界，以及各阶段的演示记录。不要把“目录存在”或 `pass` / `NotImplementedError` 算作交付。

## 2. 通用实施与质量门槛

- `src/local_agent/` 包布局；`pyproject.toml` 写 `requires-python`、运行依赖和开发依赖；`requirements.txt` 只保留基础安装入口。用 `uv lock` 生成并提交 `uv.lock`，用 `uv lock --check` 与 `uv sync` 验证可复现环境（架构 §3、§9）。
- 所有公开函数/方法有类型标注与 docstring；每次提交通过 `python -m pytest`、`python -m ruff check .`、`python -m mypy src`。每个新增模块至少覆盖正常、错误和安全边界测试，测试目录见 §3。
- `config.yaml` 只存非秘密配置；聊天模型的 API Key 由 `llm.api_key_env` 指定的环境变量读取，只有 `embedding.provider=openai_compatible` 时才使用 `embedding.api_key_env`；`onnx_local` 不读取 API Key。日志、页面、Checkpoint 不泄露密钥。
- 每任务一个 Conventional Commits 提交，提交里附测试命令与结果；任务不能只创建空文件。不同阶段可以只创建已实现的模块，§3 是最终目录，不要求 v0.1a 全部落盘。
- 对 LangGraph、langchain-core、Streamlit、Chroma 等具体构造参数或调用签名，先以安装版本的官方文档与包内签名核实，再写最小契约测试。本计划不把不确定 API 写成既定事实。
- 默认自动测试使用临时工作区、隔离 SQLite 和假模型；真实 API 的端到端测试作为手动验收，不能让 PR 测试依赖密钥、网络或模型输出稳定性。
- Windows PowerShell 与 POSIX 的 Shell 分支分别测试；最小安全策略失败闭合，不靠 Prompt 代替代码校验。UI 重新运行不得重复执行工具（架构 §1.4、§6.6）。

## 3. 项目目录与阶段归属

```text
local-agent-web/
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── AGENTS.md
├── requirements.txt
├── pyproject.toml
├── uv.lock
├── .gitignore
├── config.yaml.example
├── .env.example
│
├── src/
│   └── local_agent/
│       ├── __init__.py
│       ├── web.py
│       ├── config.py
│       ├── exceptions.py
│       ├── scope.py                 # 作用域与稳定标识（project_id / scope_key），只依赖标准库
│       │
│       ├── graph/
│       │   ├── __init__.py
│       │   ├── state.py
│       │   ├── builder.py
│       │   ├── nodes.py
│       │   ├── edges.py
│       │   └── routers.py
│       │
│       ├── llm/
│       │   ├── embedding.py
│       │   ├── factory.py
│       │   ├── models.py
│       │   └── protocol.py
│       │
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── file_tools.py
│       │   ├── shell_tools.py
│       │   ├── tool_registry.py
│       │   └── result_types.py
│       │
│       ├── security/
│       │   ├── policy.py
│       │   ├── path_guard.py
│       │   ├── shell_guard.py
│       │   ├── diff_guard.py        # diff 预览、变更行数和批量范围校验；v0.3 扩展 Patch 应用
│       │   └── approval.py
│       │
│       ├── context/
│       │   ├── agents_loader.py
│       │   ├── prompt_context.py
│       │   └── token_budget.py
│       │
│       ├── memory/
│       │   ├── short_term.py
│       │   ├── medium_term.py
│       │   ├── long_term.py
│       │   ├── summarizer.py
│       │   ├── recall.py
│       │   ├── writer.py
│       │   ├── forgetting.py
│       │   └── schemas.py
│       │
│       ├── reflection/
│       │   ├── reflector.py
│       │   ├── validators.py
│       │   └── memory_candidates.py
│       │
│       ├── rag/
│       │   ├── indexer.py
│       │   ├── chunker.py
│       │   ├── retriever.py
│       │   └── symbols.py
│       │
│       ├── persistence/
│       │   ├── checkpoint.py
│       │   ├── session_store.py
│       │   └── migrations.py
│       │
│       └── ui/
│           ├── renderer.py
│           ├── approval_ui.py
│           └── streaming.py
│
├── tests/
│   ├── test_web.py
│   ├── test_router.py
│   ├── test_tools.py
│   ├── test_security.py
│   ├── test_memory.py
│   ├── test_reflection.py
│   ├── test_graph_resume.py
│   ├── test_rag.py
│   └── test_agent_eval.py
│
├── data/
│   ├── checkpoints.sqlite
│   ├── memories.sqlite
│   └── chroma/
│
└── scripts/
    ├── init_db.py
    └── inspect_memory.py
```

### 3.1 目录职责

- `graph/`：只负责 LangGraph 编排。
- `tools/`：只负责工具接口和具体动作。
- `security/`：集中处理路径、Shell、diff 和审批策略。
- `memory/`：实现三层记忆和记忆生命周期。
- `reflection/`：实现独立反思逻辑。
- `llm/`：隔离聊天模型、Embedding provider 和 API 差异；`embedding.py` 在 T14A 落地本地 ONNX 与 OpenAI 兼容 Embedding 适配。
- `persistence/`：处理 Checkpoint 和会话隔离。
- `scope.py`：作用域与稳定标识（路径规范化、稳定 Hash、`project_id`、`scope_key`），被 `persistence` 与 `memory` 共用，只依赖标准库。T11 时这份规则在 `persistence/session_store.py`，T13 归位到这里：放进 `memory` 或 `persistence` 任一侧都会造成包之间的反向依赖，而复制两份会让 §10.2 的规则漂移。
- `rag/`：可选代码检索，不影响核心 Agent。
- `web.py`：Streamlit 网页入口，只负责把页面操作转成 Graph 调用并展示结果。
- `ui/`：负责网页聊天、工具结果与 diff 展示、流式输出和审批交互。

---

目录按使用阶段创建：v0.1a 先落地 `web.py`、`graph/`、`llm/`、`tools/`、`security/`、`context/`、`persistence/checkpoint.py` 和最小 `reflection/validators.py`；v0.1b 加 `security/approval.py`、`ui/approval_ui.py`、`persistence/session_store.py` 与迁移；v0.2 加 `memory/` 的实际存储、召回、压缩和完整 `reflection/`；v0.3 才启用 `rag/`、轨迹与评测。`tests/test_web.py`、`tests/test_router.py`、`tests/test_tools.py`、`tests/test_security.py`、`tests/test_graph_resume.py` 从 v0.1a 随模块落地，`tests/test_memory.py` / `tests/test_reflection.py` 分别在 v0.2 / v0.1b 后扩展。§7 的评测数据可放在 `tests/` 下，不要求提前新增运行依赖。

## 4. v0.1a：图跑通

**阶段退出门槛**：浏览器可完成闲聊、读文件、精确串替换、安全 Shell；`delete_file` 等审批动作固定拒绝；单会话重启恢复、多轮消息保留、页面重绘无重放（架构 §12）。`recall_memory`、`compression_node`、`memory_write_node` 只做可测试的透传，不访问记忆库；`reflection_node` 只做确定性检查（架构 §5.2–§5.4）。这一阶段不以流式 UI、审批和多会话为验收条件。

每个 T01–T07 都继承 §2 的质量门槛；以下保留可直接开工的任务粒度。

原 v0.1a 清单中的未加前缀章节号：§1.4、§4.2、§5.x、§6.x、§10.6、§12 指架构文档；§3、§8、§9 指本开发文档。同名配置键与工具签名以架构契约和本文件样例核对。

### T01｜可安装、可打开的本地网页壳（M）

- **目标**：建立 src layout 和只监听本机的 Streamlit 页面，能提交文字并收到明确的“模型尚未配置”配置错误；交付实际可运行页面与错误处理，不交付 `pass` 或空函数。
- **涉及文件**：`pyproject.toml`、`requirements.txt`、`uv.lock`、`.gitignore`、`src/local_agent/__init__.py`、`src/local_agent/web.py`、`src/local_agent/ui/renderer.py`、`tests/test_web.py`、`README.md`、`LICENSE`、`CONTRIBUTING.md`（§3、§9）。
- **关键接口**：`web.py` 的 Streamlit 页面入口和 `ui/renderer.py` 的消息展示函数；具体函数签名由实现时在这两个文件确定，不改变 §4.2 State。Streamlit 组件 API 按安装版本官方文档核实。
- **前置任务**：无。
- **验收方式**：安装开发工具 `uv` 后执行 `uv lock`、`uv lock --check`、`uv sync`；在项目虚拟环境运行 `python -m streamlit run src/local_agent/web.py --server.address=127.0.0.1 --server.port=8501`，打开本机页面后提交文字可见缺少模型配置提示且不调用工具；`python -m pytest tests/test_web.py` 与 §2 命令通过。检查 `.gitignore` 排除 `.env`、`config.yaml`、`data/`、`.venv/` 和缓存，但不排除示例配置及 `uv.lock`；README 包含用途、安装、配置、启动命令和已知限制，MIT 许可与贡献说明存在。

### T02｜模型配置与最小闲聊闭环（L）

- **目标**：从 YAML + 环境变量构造模型，以 `prepare_context` → `chat_node` → `final_response_node` 最小子图完成单轮网页闲聊；后续任务按 §5.2 补齐其余节点，闲聊始终不绑定文件或 Shell 工具。
- **涉及文件**：`config.yaml.example`、`.env.example`、`src/local_agent/config.py`、`src/local_agent/llm/factory.py`、`src/local_agent/graph/state.py`、`src/local_agent/graph/builder.py`、`src/local_agent/graph/nodes.py`、`src/local_agent/graph/edges.py`、`src/local_agent/web.py`、`tests/test_web.py`（§3）。
- **关键接口**：`AgentState` 字段原样按 §4.2，尤其 `messages: Annotated[list[AnyMessage], add_messages]`；最小子图的节点名严格按 §5.1。Graph 编译、模型构造和图调用的确切 API/签名先对已安装 §9 版本查官方文档和包内签名，记录在实现 PR 中；不在计划里臆造。
- **前置任务**：T01。
- **验收方式**：`python -m pytest tests/test_web.py`；浏览器输入“你好”收到模型回答，测试断言闲聊调用的工具数为零；移除 API Key 时有明确配置错误，日志与网页均不显示密钥；共同验收命令通过。跨轮恢复在 T03 验收。

### T03｜SQLite Checkpoint 与刷新恢复（M）

- **目标**：将同一网页会话的 `thread_id` 绑定到 `data/checkpoints.sqlite`，刷新和重启后继续多轮闲聊，不靠 `st.session_state` 保存业务状态（§1.4、§4.2、§10.6）。
- **涉及文件**：`src/local_agent/persistence/checkpoint.py`、`src/local_agent/graph/builder.py`、`src/local_agent/web.py`、`tests/test_graph_resume.py`、`tests/test_web.py`（§3）。
- **关键接口**：`thread_id`、`session_id`、`messages` 使用 §4.2 字段；SQLite Checkpointer 构造、连接管理及读取状态的签名待核实，核实方法为 §9 固定版本的官方文档和运行一项跨进程恢复测试。
- **前置任务**：T02。
- **验收方式**：`python -m pytest tests/test_graph_resume.py tests/test_web.py`；在浏览器完成两轮对话，刷新页面并重启服务后历史仍可见且新回答包含上文；页面普通重绘不会再次调用模型或工具；异常中断后未获 Checkpoint 确认的待执行副作用不能自动重放，需提示人工核对；共同验收命令通过。

### T04｜AGENTS 加载与模式切换（M）

- **目标**：补齐 `load_agents`、`recall_memory` 透传、`mode_router` 与 §5.2 闲聊目标图；按层级和优先级加载工作区规则、超限截断，并区分纯闲聊与工具任务（§5、§7）。
- **涉及文件**：`src/local_agent/context/agents_loader.py`、`src/local_agent/context/prompt_context.py`、`src/local_agent/context/token_budget.py`、`src/local_agent/graph/nodes.py`、`src/local_agent/graph/routers.py`、`src/local_agent/graph/edges.py`、`src/local_agent/graph/builder.py`、`tests/test_router.py`、`tests/test_security.py`（§3）。
- **关键接口**：`load_agents`、`mode_router` 使用 §5.1 原名；配置键仅用 `agents.enabled`、`agents.filenames`、`agents.max_file_chars`、`agents.max_total_chars`、`agents.precedence`（§8）。合并后的 AGENTS 文本写入 RuntimeContext 的 `agents_context` 字段，节点通过 RuntimeContext 的 `app_config` 读取配置（两个字段均已在架构 §4.1 同步），不新增 State 字段。具体函数签名以对应模块实现和测试确定。
- **前置任务**：T03。
- **验收方式**：`python -m pytest tests/test_router.py tests/test_security.py`；测试覆盖 Git 根目录边界、Git 根目录位于 `workspace.root` 外时停止扫描、多层规则、`AGENTS.override.md` 优先级、截断和无文件情况；网页纯闲聊不出现文件/Shell 调用，工具请求进入工具分支；共同验收命令通过。

### T05｜只读工具的第一次工具闭环（L）

- **目标**：完成 `planner_node` → `agent_decision_node` → `safety_gate_node` → `tool_execution_node` → 最小 `reflection_node` → `completion_router` → `final_response_node`，实际执行 `list_dir`、`read_file` 并展示结果（§5.3、§6.1–6.2）。
- **涉及文件**：`src/local_agent/tools/file_tools.py`、`src/local_agent/tools/tool_registry.py`、`src/local_agent/tools/result_types.py`、`src/local_agent/security/path_guard.py`、`src/local_agent/security/policy.py`、`src/local_agent/graph/nodes.py`、`src/local_agent/graph/edges.py`、`src/local_agent/graph/builder.py`、`src/local_agent/reflection/validators.py`、`src/local_agent/ui/renderer.py`、`tests/test_tools.py`、`tests/test_security.py`、`tests/test_router.py`（§3）。
- **关键接口**：`list_dir(path: str = ".", recursive: bool = False, include_hidden: bool = False, max_entries: int = 200)`；`read_file(path: str, start_line: int | None = None, end_line: int | None = None, max_chars: int = 50000)`，用 `@tool`（§6.1–6.2）。节点名按 §5.1，Tool Calling 的绑定与返回数据 API 待核实。
- **前置任务**：T04。
- **验收方式**：`python -m pytest tests/test_tools.py tests/test_security.py tests/test_router.py`；浏览器请求“列出当前目录”和“读取指定测试文件”均显示真实结果，工作区外路径和二进制读取被拒绝/标注，模型未实际调用工具时不声称完成；共同验收命令通过。

### T06｜精确串替换、新文件写入与拒绝删除（L）

- **目标**：接通 `edit_file`、`write_file`、`delete_file`：前者精确串替换并展示 diff，中者只创建新文件，后者固定拒绝且不删除（§6.3–6.5、§12）。
- **涉及文件**：`src/local_agent/tools/file_tools.py`、`src/local_agent/security/path_guard.py`、`src/local_agent/security/diff_guard.py`、`src/local_agent/reflection/validators.py`、`src/local_agent/graph/nodes.py`、`src/local_agent/ui/renderer.py`、`tests/test_tools.py`、`tests/test_security.py`（§3）。
- **关键接口**：`edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False, expected_sha256: str | None = None, reason: str = "")`；`write_file(path: str, content: str, overwrite: bool = False, reason: str = "")`；`delete_file(path: str, recursive: bool = False, reason: str = "")`，均以 `@tool` 暴露（§6.3–6.5）。
- **前置任务**：T05。
- **验收方式**：`python -m pytest tests/test_tools.py tests/test_security.py tests/test_graph_resume.py`；浏览器编辑后内容正确、展示非空 diff、反思校验前后 Hash 改变；`expected_sha256` 缺失或不匹配、重复匹配、覆盖已有文件、大范围修改均被拒绝；请求删除后文件仍存在且页面显示原因；刷新不重复修改；共同验收命令通过。

### T07｜安全 Shell 与 v0.1a 完整验收（L）

- **目标**：接通 `run_shell`，只允许符合 §8 结构化白名单和工作区边界的安全命令免审批，其他需要审批/破坏性命令在 v0.1a 明确拒绝（§6.6、§12）。
- **涉及文件**：`src/local_agent/tools/shell_tools.py`、`src/local_agent/security/shell_guard.py`、`src/local_agent/security/policy.py`、`src/local_agent/graph/nodes.py`、`src/local_agent/ui/renderer.py`、`tests/test_tools.py`、`tests/test_security.py`、`tests/test_graph_resume.py`、`tests/test_web.py`、`README.md`（§3）。
- **关键接口**：`run_shell(command: str, cwd: str = ".", timeout_seconds: int | None = None, env_keys: list[str] | None = None)`，使用 `@tool`（§6.6）；`timeout_seconds` 省略或传 `None` 时使用 `security.shell.default_timeout_seconds`（出厂默认值 60），显式值不能超过该配置；工作目录与白名单分别使用 §8 的 `workspace.root`、`security.shell.platform`、`security.shell.executor`、`security.shell.whitelist`、`security.shell.default_timeout_seconds`、`security.shell.max_output_chars`。
- **前置任务**：T06。
- **验收方式**：`python -m pytest`、`python -m ruff check .`、`python -m mypy src`；浏览器执行白名单中的 `python --version` 并显示退出码/输出，危险 Shell 与工作区外 `cwd` 被拒绝；手动走完 §12 的闲聊、读、改、Shell、拒删、刷新/重启恢复且不重复工具操作；README 已写明已知限制和安全边界。

### v0.1a 手动演示

T01 → T02 → T03 可演示本机网页两轮闲聊和重启恢复；T04 → T05 可演示模式路由与只读工具；T06 → T07 补齐安全编辑、拒删与安全 Shell。每个操作前后记录工作区文件 Hash，刷新网页后确认 Hash 和工具事件数不变。此时仍不开放审批动作。

## 5. v0.1b：安全、兼容性与会话闭环

前置：v0.1a 全部验收通过。退出门槛：审批拒绝不循环、批准恰好执行一次；无 Tool Calling 的模型工具模式失败闭合；多会话隔离；机械证据不过关不得标记 verified。每项任务都执行 §2 质量命令。

### T08｜追加状态与机械证据（M）

- 目标：落实架构 §4.2 全部追加 reducer、事件 ID 幂等，以及架构 §11.2 的 ToolEvidence；final_evidence 从当前有效 tool_events/tool_results 重算后整体覆盖。事件去重之外，tool_execution_node 执行前检查已有成功结果；崩溃造成结果不确定时不自动重放副作用。
- 涉及文件：src/local_agent/graph/state.py、src/local_agent/reflection/validators.py、src/local_agent/graph/nodes.py、tests/test_reflection.py、tests/test_graph_resume.py。
- 关键接口：只用架构 §4.2 的 messages、tool_events、tool_results、decision_trace、approval_history、error_events、final_evidence；不增 State 字段。
- 前置：T07。验收：`python -m pytest tests/test_reflection.py tests/test_graph_resume.py`；重试/恢复不重复事件或已确认副作用；模拟“副作用已发生但 Checkpoint 未提交”时提示人工核对，不自动重放；Shell 非零退出、空输出、编辑 Hash 未变不得 verified；多步骤有效证据不丢；§2 命令通过。

### T09｜模型能力探测与降级（M）

- 目标：探测并记录架构 §11.7 的 ModelCapabilities；结构化输出按原生 JSON Schema → Prompt JSON → Pydantic 校验 → 一次重试 → 明确失败。
- 涉及文件：src/local_agent/llm/factory.py、src/local_agent/llm/models.py、src/local_agent/llm/protocol.py、src/local_agent/config.py、tests/test_router.py、tests/test_web.py。
- 关键接口：ModelCapabilities 字段按架构 §11.7；模型 API 的构造和签名以安装版本官方文档、包内签名及最小契约测试核实。
- 前置：T07。验收：`python -m pytest tests/test_router.py tests/test_web.py`；模拟 Tool Calling、JSON Schema、Streaming 分别不支持及两次解析失败，工具模式失败闭合、非流式输出可用、日志不泄密；§2 命令通过。

### T10｜三出口人工审批（L）

- 目标：接通 approval_interrupt_node 和网页批准/修改/拒绝控件，保护 delete_file、覆盖写、大范围编辑与破坏性 Shell；执行前复核批准指纹和目标状态，递归删除需二次确认，修改回规划/决策，拒绝结束不重试（架构 §5.3、§6）。
- 涉及文件：src/local_agent/security/approval.py、src/local_agent/security/policy.py、src/local_agent/graph/nodes.py、src/local_agent/graph/edges.py、src/local_agent/graph/builder.py、src/local_agent/ui/approval_ui.py、src/local_agent/web.py、tests/test_security.py、tests/test_graph_resume.py、tests/test_web.py。
- 关键接口：pending_approval、approval_history、pending_tool_calls 按架构 §4.2；action_fingerprint 按 §5.3 分工具计算。interrupt()/Command(resume=...) 的精确签名先核实。
- 前置：T08、T09。验收：`python -m pytest tests/test_security.py tests/test_graph_resume.py tests/test_web.py`；三出口及重启后挂起状态均通过；批准后目标/参数变化必须重新审批或拒绝；递归删除仅第一次确认不得执行，第二次确认后才恢复 Graph，刷新后不得沿用第一次确认；换 tool_call_id 仍不能绕过拒绝；按钮双击不重复执行；§2 命令通过。

### T11｜会话索引与侧栏（M）

- 目标：按架构 §10.6 的 sessions 表实现新建、列表、切换、恢复、重命名；浏览器状态不是会话事实来源。
- 涉及文件：src/local_agent/persistence/session_store.py、src/local_agent/persistence/migrations.py、src/local_agent/persistence/checkpoint.py、src/local_agent/web.py、src/local_agent/ui/renderer.py、tests/test_graph_resume.py、tests/test_web.py。
- 关键接口：session_id、thread_id、title、workspace_root、project_id、last_checkpoint_id 按架构 §10.6；待审批线程只经 Command(resume=...) 恢复。
- 前置：T10。验收：`python -m pytest tests/test_graph_resume.py tests/test_web.py`；两个会话各自消息与审批状态隔离，刷新不新建重复会话，重启可恢复；§2 命令通过。

### T12｜跨平台 Shell 与阶段回归（L）

- 目标：按平台区分 argv 直执行、cmd.exe、pwsh；白名单按可执行名、参数结构、cwd 和平台匹配，黑名单只兜底（架构 §6.6）。
- 涉及文件：src/local_agent/security/shell_guard.py、src/local_agent/tools/shell_tools.py、src/local_agent/security/policy.py、tests/test_security.py、tests/test_tools.py、README.md。
- 关键接口：run_shell 按架构 §6.6；security.shell.platform、executor、whitelist、blocked_patterns 按本文件 §8。
- 前置：T10。验收：`python -m pytest tests/test_security.py tests/test_tools.py`；覆盖绝对路径、别名、大小写、管道、重定向、超时、输出截断；`pytest` 带任意测试参数和 `git diff` 带额外参数不得命中免审白名单；当前平台真执行，另一平台解析单测；§2 命令及 T08–T11 回归通过。

阶段演示：提交删除 → 网页挂起并展示影响 → 重启 → 拒绝后文件未删且不再次弹窗；另一个会话批准动作并恰好执行一次；不支持 Tool Calling 的模型收到能力错误。

## 6. v0.2：三层记忆、压缩与完整反思

前置：v0.1b 安全/会话回归通过。新增存储按 user_id、project_id、workspace_root 与全局作用域隔离，压缩不得删除 Checkpoint 原始 messages（架构 §10）。每项任务都执行 §2 质量命令；自动测试用临时库与假 Embedding。

### T13｜作用域和记忆库初始化（M）

- 目标：按架构 §10.2–§10.3 建 project_id 稳定规则、Chroma 中期库、SQLite 长期库及迁移，启停记忆不破坏旧 Checkpoint。
- 涉及文件：src/local_agent/memory/schemas.py、src/local_agent/memory/medium_term.py、src/local_agent/memory/long_term.py、src/local_agent/persistence/migrations.py、scripts/init_db.py、tests/test_memory.py。
- 关键接口：中期记录的 scope/scope_key/user_id/project_id/workspace_root 与 entities.scope_key、facts、task_conclusions 字段按架构 §10；无 Git 根目录时 project_id 用 workspace.root 稳定 Hash；不把未验证对话直接入库。
- 前置：T11、T12。验收：`python -m pytest tests/test_memory.py tests/test_graph_resume.py`；初始化幂等、旧 Checkpoint 可恢复、跨项目隔离、记忆关闭不建向量索引；§2 命令通过。

### T14A｜本地 Embedding 适配器与配置（M）

- 目标：交付显式 Embedding 适配器和 provider 条件配置；默认 `onnx_local` 可在本机 CPU 推理，同时保留 `openai_compatible`，且不让 Chroma 隐式选择或下载模型（架构 §10.2）。
- 涉及文件：src/local_agent/llm/embedding.py、src/local_agent/config.py、src/local_agent/exceptions.py、config.yaml.example、pyproject.toml、uv.lock、tests/test_memory.py、tests/test_web.py（既有 embedding 校验用例随 provider 口径调整）。
- 关键接口：embedding.* 按 §8。`onnx_local` 使用 FastEmbed 和 `BAAI/bge-small-zh-v1.5`，模型实例按应用进程惰性创建并复用；`openai_compatible` 保留。FastEmbed 的具体构造参数、缓存参数和模型元数据读取方式在安装并锁定版本后核实，以契约测试固定，不提前编造 API。
- 前置：T13。验收：`python -m pytest tests/test_memory.py`；自动测试使用假 Embedding 且不访问网络；两种 provider 的必填字段分别校验，YAML 显式提供当前 provider 不适用的字段时失败；关闭时不导入 FastEmbed、不下载模型；`config.yaml.example` 必须保持 `embedding.enabled=false` 和 `memory.medium_term.enabled=false`，可以预填 `onnx_local`、模型和缓存字段，但加载示例配置不得初始化 FastEmbed、访问网络或下载模型，启用状态测试使用独立临时配置；§2 命令通过。手动验收首次下载只写入显式 `cache_dir`、断网后可复用缓存、`local_files_only=true` 不发起网络请求、缓存缺失/损坏时后端返回可读状态与失败原因、输出为 512 维有限数值，并记录首次与复用加载耗时。T14A 不负责网页展示，页面状态和错误展示由 T19 验收。

### T14B｜Chroma 中期向量记忆与召回（L）

- 目标：保存已验证对话摘要、任务片段与近期偏好；recall_memory 按作用域、top_k、min_score 召回，并只将相关记忆注入 Prompt（架构 §10.2）。
- 涉及文件：src/local_agent/memory/medium_term.py、src/local_agent/memory/recall.py、src/local_agent/memory/schemas.py、src/local_agent/graph/nodes.py、src/local_agent/graph/state.py、src/local_agent/context/prompt_context.py、src/local_agent/llm/embedding.py（补齐 Chroma EF 协议成员）、src/local_agent/config.py、config.yaml.example、pyproject.toml、uv.lock、tests/test_memory.py、tests/test_router.py。
- 关键接口：memory_refs 按架构 §4.2 覆盖并去重；memory.medium_term.* 按 §8；显式使用 T14A 的 Embedding 适配器。Chroma 的具体构造参数、自定义 Embedding 协议、距离语义和 collection 元数据读写方式在锁定版本后核实，以契约测试固定。
- 前置：T14A。验收：`python -m pytest tests/test_memory.py tests/test_router.py`；自动测试使用假 Embedding 且不访问网络；相关问题召回，跨项目/过期/低分记忆不召回，关闭时节点透传，Prompt 不超预算；`min_score` 使用“越大越相关”的统一分数；collection 的 provider、模型、维度、归一化或距离度量不匹配时拒绝复用并提示重建；§2 命令通过。手动验收使用本地中文模型时，中文近义内容排在无关内容之前。

### T14C｜网页工作区选择与外部路径策略（L）

- 目标：允许用户在网页选择运行服务的本机目录作为当前工作区，并在不改变记忆、AGENTS 和 RAG 作用域的前提下，通过 `workspace.allow_external_paths` 受控访问工作区外的显式绝对路径（架构 §1.4–§1.5、§6、§10.6）。
- 涉及文件：src/local_agent/web.py、src/local_agent/ui/renderer.py、src/local_agent/config.py、src/local_agent/scope.py、src/local_agent/persistence/session_store.py、src/local_agent/persistence/migrations.py、src/local_agent/security/path_guard.py、src/local_agent/security/policy.py、src/local_agent/security/shell_guard.py、src/local_agent/security/approval.py、src/local_agent/tools/file_tools.py、src/local_agent/tools/shell_tools.py、src/local_agent/graph/builder.py、src/local_agent/graph/nodes.py、config.yaml.example、tests/test_web.py、tests/test_tools.py、tests/test_security.py、tests/test_graph_resume.py、README.md。
- 关键接口：只使用既有 `workspace.root`、`workspace.allow_external_paths`、RuntimeContext 的 `workspace_root` / `current_cwd` 与 sessions.workspace_root/status，不增加 AgentState 字段。`workspace.root` 是启动默认值；页面使用“绝对路径输入框 + 已使用工作区列表”，不引入原生目录选择器依赖。规范化后的工作区与会话绑定，`sessions.status=active` 从 T14C 起表示整个聊天库唯一的当前会话；聊天库迁移将历史多 active 归一为 `updated_at` 最新的一条并增加唯一约束。普通提交与 `Command(resume=...)` 都从当前 Session 取得 `workspace_root`，默认 `current_cwd=workspace_root`，不能继续使用缓存 ChatApp 的启动默认值。相对路径始终以当前工作区解析；允许外部路径时，只有原始输入本身是绝对路径才进入外部路径策略。
- 前置：T14B。验收：`python -m pytest tests/test_web.py tests/test_tools.py tests/test_security.py tests/test_graph_resume.py`；页面可输入并选择已存在且可读的本机目录，也可从去重后的历史工作区列表切换；非法/无权限目录失败且不改变当前会话。等价路径（Windows 大小写与分隔符、尾部分隔符、可解析的符号链接入口）不得生成重复工作区身份；刷新和重启恢复全局唯一的 active 会话及其工作区，旧库存在多 active 时迁移后只保留最新一条。切换工作区会切换或新建会话，不污染原会话的 AGENTS、`project_id`、记忆召回和工具上下文；在工作区 A 挂起相对路径审批、切到 B、再回 A 恢复时，判定、指纹和执行仍以 A 为根目录。`allow_external_paths=false` 时路径穿越、软链接逃逸和绝对外部路径均被拒绝；设为 `true` 时，原始输入为绝对路径的外部目标可 `list_dir` / `read_file`，但 `~`、相对路径逃逸和 Windows 盘符相对路径仍拒绝。外部 `edit_file`、`write_file`、`delete_file` 与工作区外 `run_shell.cwd` 必须在安全判定、动作指纹和执行三处使用同一规范化目标，进入人工审批且不得命中免审白名单；文件系统/驱动器根、用户主目录根、工作区根、Git 元数据和系统关键目录不可通过审批删除。外部路径不自动加载 AGENTS、不改变作用域、不进入 RAG 索引；页面突出展示外部路径及审批原因。关闭短期记忆时只承诺页面生命周期内选择，服务重启回到 `workspace.root`；§2 命令通过。

### T15｜长期实体事实 CRUD 与冲突（L）

- 目标：实现 entities/facts/task_conclusions 的写入、查询、更新、版本记录、冲突检测、过期处理及人工删除（架构 §10.3）。
- 涉及文件：src/local_agent/memory/long_term.py、src/local_agent/memory/schemas.py、src/local_agent/memory/recall.py、src/local_agent/persistence/migrations.py、tests/test_memory.py、README.md。
- 关键接口：表和字段名按架构 §10.3，不新增 AgentState 字段。
- 前置：T14C。验收：`python -m pytest tests/test_memory.py`；同键更新保留版本、冲突不静默覆盖、删除后不召回、跨项目隔离、旧库迁移通过；§2 命令通过。

### T16｜Prompt 窗口与摘要压缩（M）

- 目标：达到 memory.compression.trigger_ratio 时生成 conversation_summary，只压缩模型输入窗口，Checkpoint 原始 messages 保持不变（架构 §10.1、§10.5）。
- 涉及文件：src/local_agent/context/token_budget.py、src/local_agent/memory/summarizer.py、src/local_agent/memory/short_term.py、src/local_agent/graph/nodes.py、src/local_agent/context/prompt_context.py、src/local_agent/config.py、src/local_agent/graph/state.py、config.yaml.example、ARCHITECTURE.md（§7.2「会话摘要」段落）、tests/test_memory.py、tests/test_graph_resume.py、tests/test_web.py、README.md。
- 关键接口：conversation_summary、context_version、compression_count、context_budget 按架构 §4.2；prompt_window_messages、compression.* 按 §8。
- 前置：T14B。验收：`python -m pytest tests/test_memory.py tests/test_graph_resume.py`；闲聊和工具循环均实际经过 `compression_node`，压缩后原始消息数不减，Prompt 窗口缩小但保留目标/约束，关闭时透传；§2 命令通过。

### T17｜完整反思与动态重规划（L）

- 目标：在机械证据基础上加入 LLM 完成度判断、异常解释、继续/重试/追问/结束与计划更新；是否 verified 仍由 validators.py 决定（架构 §5.4、§11.2）。
- 涉及文件：src/local_agent/reflection/reflector.py、src/local_agent/reflection/validators.py、src/local_agent/graph/nodes.py、src/local_agent/graph/edges.py、src/local_agent/context/prompt_context.py（「当前任务」「当前计划」段落）、tests/conftest.py、tests/test_reflection.py、tests/test_router.py、tests/test_security.py、README.md。
- 关键接口：reflection_result、plan、current_step_index、final_evidence、loop_count、max_loop_count 按架构 §4.2；结构化输出沿用 T09。
- 前置：T08、T09、T16。验收：`python -m pytest tests/test_reflection.py tests/test_router.py`；工具失败不得声称完成，可重规划且不超 agent.max_tool_loops，无证据不 verified，解析失败降级明确；§2 命令通过。

### T18｜记忆写入审核与遗忘（L）

- 目标：任务完成、闲聊中固定偏好变化或用户要求记住时先形成 memory_write_intent，再经 reflection_node 验证为 MemoryCandidate，最后由 memory_write_node 写入；实现时间/重要度降权、过期淘汰、人工删除（架构 §5.2、§10.2–§10.4）。
- 涉及文件：src/local_agent/reflection/memory_candidates.py、src/local_agent/memory/writer.py、src/local_agent/memory/forgetting.py、src/local_agent/memory/medium_term.py、src/local_agent/memory/long_term.py、src/local_agent/graph/nodes.py、tests/test_memory.py、tests/test_reflection.py。
- 关键接口：memory_write_candidates 按架构 §4.2；候选携来源事件、置信度和重要度，写入作用域由已验证意图与 RuntimeContext 推导，不向 MemoryCandidate 或 State 增加字段。memory_write_intent 是触发事件，不新增 State 字段。
- 前置：T14B、T15、T17。验收：`python -m pytest tests/test_memory.py tests/test_reflection.py tests/test_router.py`；普通闲聊不每轮写，用户在闲聊中明确要求记住时经过反思节点后可写，作用域不确定则拒绝写入；未验证输入不入长期库、重复事件不重写、过期/人工删除不召回、低重要度随时间降权；§2 命令通过。

### T19｜三层记忆集成与阶段验收（M）

- 目标：接通 Checkpoint、Chroma、长期 SQLite、召回、压缩与反思，并在网页展示本地 Embedding 模型准备状态和错误；任意层关闭不影响其他层（架构 §5、§10）。
- 涉及文件：src/local_agent/graph/builder.py、src/local_agent/graph/nodes.py、src/local_agent/context/prompt_context.py、src/local_agent/web.py、src/local_agent/ui/renderer.py、tests/test_memory.py、tests/test_graph_resume.py、tests/test_web.py、README.md。
- 关键接口：保持架构 §4.2 State、§8 memory.*.enabled / rag.enabled 配置。
- 前置：T16、T18。验收：`python -m pytest` 与 §2 命令；跨会话同项目可召回已验证偏好，跨项目不可，Checkpoint 原文恢复，关闭各层不访问相应库；真实模型手动演示写入与下一会话召回。首次准备本地模型时，页面可见模型名称、目标缓存目录和当前状态；下载、缓存或加载失败时展示明确原因，不能表现为无响应。

阶段演示：完成项目任务 → 审核后写入中期摘要/长期结论 → 新会话提问召回相关记忆 → 人工删除后不再召回；压缩前后 Checkpoint 消息完整。

**T18/T19 收尾记录（2026-09-21）**：已实现部分与真实模型/本地 Embedding 的逐项实测结果见 README「v0.2 退出条件」。离线预置权重的加载与复用、对话写入后跨会话召回、跨项目隔离、人工删除后不召回及召回失败的可见降级已有实测记录；全新空缓存的首次在线下载成功仍**未验收通过**，现有尝试因 `WinError 10060` 网络连接超时失败。网络恢复后用 `python -m local_agent.cli memory probe` 补验并记录结果；不能把离线预置成功写成在线下载成功。该外部环境待补验项不阻塞 T20 的开发与 T18/T19 实现代码提交，但在补验通过或获得明确的发布豁免前，不得宣称 v0.2 全部验收通过或完成发布门槛。

**当前执行顺序**：先冻结 T18/T19 范围，正式提交前运行一轮 §2 质量命令、`uv lock --check` 与 `git diff --check`（失败修复后复验）；由于共享文件的改动交织，T18/T19 合并为一个提交，未跟踪的 `PRODUCT_EVOLUTION_ROADMAP.md` 不纳入，也不默认推送。之后单工作区依次做 T20 → T22 → T21 → T23 → T24；每项独立验收、独立提交。T19 不再因非阻断性的回答措辞开启无上限复审；只有误写/误删、跨作用域泄漏、虚假保存承诺、重复副作用、崩溃或数据损坏等可复现的硬性问题才重新打开其退出判定。每项任务开工前固定验收场景，常规复审集中一轮、修复集中一轮；发现硬性问题不受轮数限制。

## 7. v0.3：复杂任务、可选代码 RAG 与评测

前置：v0.2 验收通过；仅 T19 首次在线下载因外部网络阻断时，可按 §6 的收尾记录先开展 T20 等后续开发，但该项仍是待补验，不构成 v0.2 完整验收或发布豁免。RAG 可关闭，关闭后基本工具任务仍能完成。架构 §12 的可选子 Agent 只有在基础任务评测达标并另行批准范围后启动，不阻塞本阶段退出。每项任务都执行 §2 质量命令。

### T20｜多步骤规划与恢复（L）

- 目标：planner_node、agent_decision_node、completion_router 在复杂任务中维护 plan、current_step_index、completed_steps，支持动态重规划及中断恢复（架构 §4.2、§5.3）。
- 涉及文件：src/local_agent/graph/nodes.py、src/local_agent/graph/edges.py、src/local_agent/reflection/reflector.py、tests/test_router.py、tests/test_graph_resume.py。
- 关键接口：只用架构 §4.2 已有字段，步骤上限用 agent.max_tool_loops / max_retries_per_step；final_evidence 全量重算。
- 前置：T19。验收：`python -m pytest tests/test_router.py tests/test_graph_resume.py`；三步任务第二步失败可调整后续计划，重启后不重做已有副作用步骤，超限明确失败；§2 命令通过。

### T21｜AST 索引与可关闭代码 RAG（L）

- 目标：为工作区代码建立增量索引；代码问题按 rag.enabled 检索带文件路径和位置的片段，禁用时不访问索引（架构 §2、§12）。
- 涉及文件：src/local_agent/rag/indexer.py、src/local_agent/rag/chunker.py、src/local_agent/rag/retriever.py、src/local_agent/rag/symbols.py、src/local_agent/context/prompt_context.py、src/local_agent/graph/nodes.py、tests/test_rag.py。
- 关键接口：rag_refs 按架构 §4.2 覆盖语义；rag.enabled/backend/index_path/max_results 按 §8；Tree-sitter API 先按已安装版本核实，未知语言以带来源的文本切片降级。
- 前置：T19。验收：`python -m pytest tests/test_rag.py tests/test_router.py`；新增/修改/删除文件后索引更新，禁用时无索引访问，工作区外文件不进入结果，片段可定位；§2 命令通过。

### T22｜编辑预览与 Patch 冲突（L）

- 目标：增强 edit_file 的 diff 预览与冲突处理；架构 §6.3 的 unified diff 输入模式只有在 hunk、上下文、旧 Hash、空 Patch 均校验后才能启用，失败时原文件不变。
- 涉及文件：src/local_agent/tools/file_tools.py、src/local_agent/security/diff_guard.py、src/local_agent/ui/renderer.py、tests/test_tools.py、tests/test_security.py。
- 关键接口：old_text/new_text 精确替换保持默认；新增 patch 入参前先更新架构 §6.3，不能暗改现有签名。采用自定义 Patch 应用器可不加依赖；若改用 unidiff，列为待批准并同步 §9 与锁文件。
- 前置：T20。验收：`python -m pytest tests/test_tools.py tests/test_security.py`；合法修改有预览，冲突/错 hunk/旧 Hash 不匹配/空 Patch 均拒绝且原 Hash 不变，精确替换回归通过；§2 命令通过。

### T23｜本地轨迹与固定评测集（M）

- 目标：把工具、审批、反思与记忆写入轨迹按 thread_id/run_id 关联，用固定任务集测成功率、错误宣称、重复副作用和循环次数；网页可选流式输出在模型不支持时退化为完整结果（架构 §4.2、§11、§12）。
- 涉及文件：src/local_agent/graph/nodes.py、src/local_agent/tools/result_types.py、src/local_agent/ui/renderer.py、src/local_agent/ui/streaming.py、tests/test_agent_eval.py、tests/test_reflection.py、tests/test_web.py、README.md。
- 关键接口：复用 tool_events、decision_trace、error_events、approval_history、final_evidence、run_id；先给本地可读 Trace，不新增外部可观测性供应商。
- 前置：T20、T21、T22。验收：`python -m pytest tests/test_agent_eval.py tests/test_reflection.py tests/test_web.py`；评测覆盖闲聊、读、改、Shell、拒绝、恢复、记忆、RAG 开/关；流式可用时增量展示、不可用时完整展示，均不重复工具调用；每例保留结构化结果和失败原因，轨迹无密钥或隐藏思维链；§2 命令通过。

### T24｜全功能回归与开源发布准备（M）

- 目标：按 §10 验收矩阵完成安装、文档、测试和真实模型演示；关闭高级功能后不破坏 v0.1a 使用。
- 涉及文件：README.md、CONTRIBUTING.md、LICENSE、config.yaml.example、requirements.txt、pyproject.toml、tests/test_web.py、tests/test_security.py、tests/test_graph_resume.py、tests/test_memory.py、tests/test_rag.py、tests/test_agent_eval.py。
- 关键接口：不新增业务接口，只核对架构 §4–§10 契约、配置与启动命令。
- 前置：T23。验收：在新环境执行 `uv lock --check`、`uv sync --extra memory --extra rag`，激活该环境后运行 `python -m pytest`、`python -m ruff check .`、`python -m mypy src`；再用仅基础依赖的环境确认关闭高级功能时 Web/工具仍可用。按 README 从空配置到本机网页演示，分别记录 Windows/POSIX 的实际测试覆盖范围与至少两种兼容端点的结果和限制，§10 矩阵逐项通过。

阶段演示：三步任务跨重启继续、没有重复编辑；打开 RAG 定位代码片段，关闭后基本任务照常；导出无密钥的轨迹与评测结果。

## 8. 配置文件与密钥

下面展示启用 v0.2 记忆能力后的目标配置。T14A / T14B 落地后该结构已可用；仓库里的 `config.yaml.example` 仍保持 `embedding.enabled=false` 与 `memory.medium_term.enabled=false`（加载示例配置不得初始化 FastEmbed、访问网络或下载模型），启用前请按 §8.1 逐项验证，并先确认 `embedding.cache_dir` 指向本机可写的绝对路径。v0.1a / v0.1b 将 `memory.medium_term.enabled`、`memory.long_term.enabled` 和 `memory.compression.enabled` 设为 `false`，对应节点按架构 §5 透传。

```yaml
llm:
  provider: openai_compatible
  base_url: "https://api.example.com/v1"
  api_key_env: "OPENAI_API_KEY"
  model: "your-model-name"
  context_window_tokens: 128000   # 必填：模型上下文窗口（token），按所连模型填写；压缩触发比的分母
  temperature: 0.2
  max_tokens: 4096                # 输出预留：输入 Prompt 可用预算 = context_window_tokens − max_tokens
  timeout_seconds: 120
  retry_count: 2

embedding:
  enabled: true
  provider: onnx_local
  model: "BAAI/bge-small-zh-v1.5"
  cache_dir: "E:/hf-cache/local-agent-web"  # 替换为本机可写的绝对目录
  local_files_only: false

workspace:
  root: "."                    # 启动默认工作区；T14C 后可在网页选择并按会话保存
  allow_external_paths: false   # false=严格工作区；true=允许受控的显式外部绝对路径
  max_file_size_bytes: 2000000

agents:
  enabled: true
  filenames:
    - "AGENTS.md"
    - "AGENTS.override.md"
  max_file_chars: 20000
  max_total_chars: 60000
  precedence:
    closer_directory_wins: true
    override_wins: true

memory:
  short_term:
    enabled: true
    backend: sqlite_checkpoint
    path: "data/checkpoints.sqlite"
    preserve_checkpoint_messages: true
    prompt_window_messages: 100

  medium_term:
    enabled: true
    backend: chroma
    path: "data/chroma"
    collection: "agent_memory"
    top_k: 8
    # 召回的最低语义相似度（真实余弦，越大越相关）。
    # 0.53 是 BAAI/bge-small-zh-v1.5 在当前小样本上的**初始值**：相关记忆 0.560~0.772、
    # 无关记忆 0.242~0.497（该模型相关性基线偏高，照 OpenAI 系量级写的 0.72 会把结果全筛掉）。
    # 换 embedding 模型或接入真实语料后必须重新标定。
    min_score: 0.53
    decay_half_life_days: 30
    write_on:
      - task_completed
      - important_fact_changed
      - explicit_user_preference

  long_term:
    enabled: true
    backend: sqlite
    path: "data/memories.sqlite"
    confidence_threshold: 0.75
    importance_threshold: 0.5
    aging_enabled: true
    forget_after_days: 180

  compression:
    enabled: true
    trigger_ratio: 0.8
    preserve_recent_turns: 8
    summary_max_chars: 12000

security:
  require_approval:
    delete_file: true
    destructive_shell: true
    large_batch_edit: true
    overwrite_existing_file: true

  large_edit:
    max_files_without_approval: 3
    max_changed_lines_without_approval: 200

  shell:
    platform: "auto"
    executor: "argv_preferred"
    default_timeout_seconds: 60
    max_output_chars: 30000
    whitelist:
      - platform: ["windows", "posix"]
        executable: "python"
        args: ["--version"]
        match: "exact"
      - platform: ["windows", "posix"]
        executable: "pytest"
        args: ["--version"]
        match: "exact"
      - platform: ["windows", "posix"]
        executable: "git"
        args: ["status"]
        match: "exact"
      - platform: ["windows", "posix"]
        executable: "git"
        args: ["diff"]
        match: "exact"
      - platform: ["windows", "posix"]
        executable: "ruff"
        args: ["check"]
        match: "exact"
    blocked_patterns:
      - "rm -rf"
      - "del /s"
      - "format "
      - "shutdown"
      - "chmod"
      - "curl "
      - "wget "

  secrets:
    redact_environment_values: true
    block_known_secret_files: true

rag:
  enabled: false
  backend: tree_sitter
  index_path: "data/code_index"
  max_results: 8

agent:
  max_tool_loops: 20
  max_retries_per_step: 2
  require_evidence_for_completion: true
```

上述配置以 `onnx_local` 作为 v0.2 默认方案。`cache_dir` 必须是本机可写的绝对路径；模型文件不进入 Git，也不放在项目源码目录。`local_files_only=false` 允许首次缺失时下载，设为 `true` 后只读本地缓存，缺失或损坏必须明确失败。

如需改用 OpenAI 兼容 Embedding API，只替换 `embedding` 段：

```yaml
embedding:
  enabled: true
  provider: openai_compatible
  base_url: "https://api.example.com/v1"
  api_key_env: "EMBEDDING_API_KEY"
  model: "your-embedding-model"
```

配置校验按 provider 区分，判定口径是“**显式提供且给出了实际内容**”：空字符串与 `false` 属于无效占位（`model_dump()` 后再 `model_validate()` 就会这样出现），不算使用该字段，不算冲突；填写了真实内容才校验失败。`onnx_local` 要求 `model`、`cache_dir`（启用时必须是绝对路径），填了 `base_url` 或 `api_key_env` 时失败；`openai_compatible` 要求 `model`、`base_url`、`api_key_env`，填了 `cache_dir` 或 `local_files_only=true` 时失败。不得静默忽略当前 provider 不适用的字段。所有 API Key 都不得直接写入 YAML，必须通过对应的环境变量提供。

---

### 8.1 分阶段启用与配置验证

- v0.1a：以示例 YAML 为起点，memory.medium_term.enabled、memory.long_term.enabled、memory.compression.enabled、embedding.enabled 均设为 false；rag.enabled 维持 false。llm.api_key_env 指向环境变量名，绝不在 YAML 写入值。security.require_approval 的四项始终为 true；v0.1a 对这些动作固定拒绝，v0.1b 才进入审批。任一项改为 false 应在配置校验时失败。
- v0.1b：启用网页审批和 sessions 索引，保持记忆/压缩/RAG 关闭；逐项验证 shell.whitelist 的平台与参数结构。
- v0.2：先配置并验证 embedding provider 再打开 medium_term；默认 `onnx_local`，首次运行允许下载模型，严格离线环境先准备缓存并设置 `local_files_only=true`。T14C 后 `workspace.root` 只作为启动默认值，网页选择结果在启用短期记忆时按会话保存；关闭短期记忆时只在当前页面生命周期内生效，服务重启回到配置默认值。`allow_external_paths` 保持严格边界，改为 `true` 只允许受控的显式外部绝对路径，外部副作用仍强制审批。long_term 和 compression 可分别打开，关闭任一层仍须可运行。`context_window_tokens` 必填且必须大于 `max_tokens`：压缩触发比 `trigger_ratio` 以 `context_window_tokens − max_tokens` 为分母，换模型时必须同步修改；prompt_window_messages 限制 Prompt 窗口而非 Checkpoint 消息数，且只在 `compression.enabled=true` 时生效（关闭时窗口原样返回全部消息）。
- v0.3：rag.enabled 可选打开；若没有语言解析器或索引损坏，必须给出可见错误或降级检索，不得伪称已检索。
- 配置缺字段、超出工作区路径、缺少环境变量或模型能力不匹配，在启动/提交时给出可理解的错误；不允许用默认值掩盖危险配置。具体配置键以上方 YAML 为准。


## 9. 依赖与环境

以 `pyproject.toml` 作为依赖唯一来源，`uv.lock` 记录可复现版本；`requirements.txt` 只提供基础安装入口。开发工具 `uv` 是项目环境的前置工具，不是运行时依赖；`uv lock` 创建锁文件，`uv lock --check` 验证，`uv sync` 安装默认开发组。见 [uv 锁定与同步说明](https://docs.astral.sh/uv/concepts/projects/sync/)、[Python Packaging User Guide](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)。

`pyproject.toml` 起步示例；实际创建时依已安装版本验证依赖范围：

```toml
[build-system]
requires = ["setuptools>=77.0.3"]
build-backend = "setuptools.build_meta"

[project]
name = "local-agent-web"
version = "0.1.0"
description = "Local learning-oriented web Agent"
readme = "README.md"
requires-python = ">=3.10,<4"
dependencies = [
  "langchain-core>=1.0,<2",
  "langchain-openai>=1.0,<2",
  "langgraph>=1.0,<2",
  "langgraph-checkpoint-sqlite>=3.0,<4",
  "streamlit",
  "pydantic",
  "pydantic-settings",
  "pyyaml",
  "python-dotenv",
  "filelock",
]

[project.optional-dependencies]
memory = ["chromadb>=1.5,<1.6", "tiktoken", "fastembed>=0.8,<0.9"]
rag = ["tree-sitter", "tree-sitter-language-pack", "langchain-text-splitters"]

[dependency-groups]
dev = ["pytest", "pytest-asyncio", "ruff", "mypy"]

[tool.setuptools.packages.find]
where = ["src"]
```

本项目不直接使用旧版 LangChain Agent，因此不需要为了 Agent 编排额外安装顶层 `langchain` 包；`langchain-core`、`langchain-openai` 和 LangGraph 已覆盖核心需求。

```text
-e .
```

v0.2 安装记忆可选组：`uv sync --extra memory`；v0.3 全功能验收：`uv sync --extra memory --extra rag`。可选依赖安装失败不应影响未开启这些功能的基础网页/工具模式。`unidiff` 不在默认依赖内，T22 采用自定义 Patch 应用器；若改变选择，先取得批准并更新此节。

---

### 9.1 依赖分层与锁定

- v0.1a 基础组包含上方 `[project].dependencies`；`filelock` 仅在需要进程间文件协调时调用，不得替代 SQLite 事务或安全审批。每新增运行依赖同步 `pyproject.toml`、`uv.lock` 和安装说明；`requirements.txt` 保持 `-e .` 入口，无需复制依赖名单。
- v0.2 安装 `memory` 可选组，其中 FastEmbed 只服务于 `onnx_local`；版本范围已在 T14A / T14B 用目标 Python 版本实际安装、核实 API 后锁定为 `chromadb>=1.5,<1.6` 与 `fastembed>=0.8,<0.9`（实现依赖这两版实测到的具体行为：`hnsw:space` 距离度量键、自定义 Embedding 函数的 `name()` 与 `__call__(self, input)` 协议、`cache_dir` / `local_files_only` 构造参数），不直接依赖其传递依赖。v0.3 根据 `rag.enabled` 安装 `rag` 组；关掉 RAG 时不应强制安装 Tree-sitter。运行全量测试必须同步两个可选组。
- 开发工具 pytest、pytest-asyncio、ruff、mypy 归 `[dependency-groups].dev`，随 `uv sync` 安装；T01 和 T24 都必须检查 `uv lock --check`。用新虚拟环境实际安装，并记录 Python 版本、`uv` 版本和依赖冲突；本文版本范围是方案约束，安装后的实际 API 仍需以该版本核实。
- 待批准：unidiff。默认 T22 采用自定义 Patch 应用器，不偷偷引入 unidiff；若选择它，先批准并补版本、测试与许可检查。

## 10. 全链路验收矩阵

| 场景 | 首次验收 | 自动验证 | 手动观察与失败判据 |
|---|---|---|---|
| 网页闲聊不调用工具 | T02 | tests/test_web.py、tests/test_router.py | 两轮问答保留上下文；任何文件/Shell 事件即失败 |
| 只读/编辑/新建/安全 Shell | T05–T07 | tests/test_tools.py、tests/test_security.py | 实际内容/Hash/退出码与页面一致，刷新无重复副作用 |
| 删除、覆盖、大改、危险 Shell | T07 拒绝；T10 审批 | tests/test_security.py、tests/test_graph_resume.py | 未批准不执行；批准后目标变化重新审批；递归删除二次确认；拒绝不循环 |
| Checkpoint 与多会话 | T03；T11 | tests/test_graph_resume.py、tests/test_web.py | 重启恢复，跨会话消息/审批/工具状态不串线 |
| 工作区选择与外部路径 | T14C | tests/test_web.py、tests/test_tools.py、tests/test_security.py、tests/test_graph_resume.py | 全局当前会话与工作区可恢复；严格模式不可越界；允许模式只接受显式绝对外部路径，外部只读受限、外部副作用强制审批，根/系统目录删除硬拒绝，且不改变 AGENTS/记忆/RAG 作用域 |
| 模型能力与错误输出 | T09 | tests/test_router.py、tests/test_web.py | 不支持 Tool Calling 不能伪装完成；JSON 错误有界重试 |
| 本地 Embedding 与模型缓存 | T14A；页面 T19 | tests/test_memory.py（假 Embedding）+ 手动真实模型验收 | 自动测试不联网；首次下载只写显式缓存目录；断网可复用；准备状态与失败原因在 T19 页面可见 |
| 中长期记忆召回和写入 | T14B–T19 | tests/test_memory.py、tests/test_reflection.py | 普通闲聊不写；闲聊中明确“记住”经反思后可写；仅相关且作用域匹配的已验证记忆进入 Prompt，模型/维度/距离契约不匹配时拒绝旧索引，删除后不召回 |
| 摘要压缩 | T16 | tests/test_memory.py、tests/test_graph_resume.py | Prompt 窗口变短，Checkpoint 原消息不减 |
| 反思、重规划和循环上限 | T17；T20 | tests/test_reflection.py、tests/test_router.py | 错误工具不能 verified；失败可调整计划；循环有明确终点 |
| 可选 RAG 与 AST | T21 | tests/test_rag.py | 开启可定位来源，关闭时无索引依赖/访问 |
| 轨迹与评测 | T23–T24 | tests/test_agent_eval.py | 固定任务集产出可读指标、失败原因，无密钥和隐藏思维链 |

每阶段留存：测试命令/通过结果、一次真实模型演示记录、已知限制、回归列表。真实 API 不通过时先区分“模型能力不足”“配置错误”“Graph/工具缺陷”，不能只给笼统失败截图。

## 11. 关键风险与停止条件

1. **安全失败闭合**（架构 §1.5、§6、§11.1）：路径穿越、软链接逃逸、外部路径被误纳入作用域、Windows/POSIX 命令歧义或审批被跳过，任一复现即阻止阶段交付；仅在隔离临时目录复现外部路径与破坏性测试。
2. **模型与 API 差异**（架构 §11.7）：兼容端点不等于支持 Tool Calling/严格 JSON/Streaming。T09 之前只承诺已验证模型；探测结果需可见，不能自动“假执行”工具。
3. **恢复与幂等**（架构 §4.2、§5.3）：刷新、重试和人工审批按钮重复提交可能重放写入/删除/Shell。用事件 ID、Checkpoint 和执行前后证据测试；“副作用已发生但 Checkpoint 未提交”的崩溃窗口无法仅凭 State 保证恰好一次，必须暂停自动重放并要求用户核对。未通过不得开放危险操作。
4. **记忆污染与召回噪声**（架构 §10、§11.3/§11.6）：写入需来源与反思审核；召回需作用域、相似度、时间和重要度过滤。跨用户/项目串线、已删除记忆再召回、混用不兼容 Embedding 向量或把 Chroma distance 反向当成 score，即停止发布。本地模型下载还需记录来源与锁定版本，缓存损坏不得静默换模型。
5. **上下文超限与幻觉**（架构 §10.5、§11.2/§11.4）：Prompt 窗口与 Checkpoint 分离；证据必须来自工具事件，模型判断不能覆盖机械失败。
6. **无限循环与成本**（架构 §11.5）：每步重试和总循环上限可配置；同一错误重复出现应停止并询问用户，不无限烧 Token。
7. **跨平台和可选依赖**：不把 Windows 实测当作 POSIX 已验证；Tree-sitter/Chroma 等可选能力缺包或关开关时给清晰错误或退化路径。`unidiff` 未批准前不得偷偷加入依赖。

## 12. 实施顺序与变更控制

- 严格依 T01→T07 形成可运行 Web 基线；T08/T09 可并行，T10 依赖二者，T11/T12 之后进入记忆。
- T13 后按 T14A→T14B→T14C→T15→T16 推进；T17 依赖 T16，T18 依赖 T14B/T15/T17，T19 做网页与三层记忆集成。T14A、T14B、T14C 各对应一个独立 Conventional Commits 提交和独立验收结果。T14C 紧接已完成的 T14B，不推迟到 v0.3 或发布阶段。
- 依赖关系允许 T20/T21 并行，T22 依赖 T20，T23 汇总，T24 发布回归；当前单工作区为避免 State/Graph 改动交织，采用 T20→T22→T21→T23→T24 的串行顺序。以后若在隔离工作树中并行 T20/T21，合并时须重跑全量测试。
- 修改架构 §4 State、§5 边、§6 工具契约或 §10 存储字段，先改架构文档并说明迁移/兼容策略，再改本开发文档任务和测试；不要在实现中悄悄新增字段或配置键。
- 每阶段退出前确认文档、配置样例、依赖清单和真实运行结果一致；用户未确认的范围扩张（例如多 Agent、外部 Trace 服务）单独立项，不夹带进任务。

## 13. 学习成果与简历证据

| 阶段 | 学习知识点 | 可展示的真实证据 |
|---|---|---|
| v0.1a | StateGraph 路由、Tool Calling、`add_messages`、Checkpointer、文件/Shell 安全、Web rerun 幂等 | 浏览器读改测录屏、Checkpoint 恢复测试、拒删用例 |
| v0.1b | `interrupt()` / `Command(resume=...)`、三出口 HITL、事件幂等、模型能力协商、跨平台策略 | 审批拒绝不循环、按钮重复不重放、多会话隔离与能力降级测试 |
| v0.2 | 短/中/长期记忆分层、向量召回、工作区与会话作用域、受控外部路径、SQLite 实体、摘要压缩、反思、遗忘 | 跨会话召回与删除测试、双工作区隔离、外部副作用审批、压缩前后 Prompt/Checkpoint 对照、反思证据链 |
| v0.3 | Plan-and-Execute、动态重规划、可选代码 RAG/AST、轨迹与评测 | 三步任务恢复演示、RAG 开关对照、固定评测集指标和失败分析 |

简历只陈述已完成且有测试/演示证据的能力；不要把架构预留的多模态、插件、知识蒸馏或未实现的多 Agent 写成已交付功能。

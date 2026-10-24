# MCP 客户端接入实施计划

> **状态**：2026-10-20 立项（项目负责人批准开工：方案 A / server 默认关 / Direct 配额 20 / 官方 SDK / `mcp.json` 独立文件）。
> **依据**：本机检出 Codex `E:\pylianxi\codex` 的逐行核验、CodeBuddy 本机配置与界面行为、Trae 官方文档（`docs.trae.cn/ide/add-mcp-servers`）。外部对照结论见 §7。
> **前置**：v0.5 T50–T55 已完成（能力闭集、开关运行期真源、`UNTRUSTED_CAPABILITIES`）。本计划不改这些语义，只在其上开口。
> **效力**：本文件负责本特性的实施顺序与验收口径。与 `ARCHITECTURE.md` 冲突时先改文档，不允许代码自行选择一种解释。**实施每个任务前，先把该任务涉及的稳定契约写进 `ARCHITECTURE.md`。**

## 1. 范围

### 做什么

1. 作为 **MCP 客户端**连接外部 MCP Server（第一版只做 `stdio` 传输）。
2. 配置载体 `mcp.json`，顶层 `mcpServers`，与 CodeBuddy 同格式，可整份粘贴。
3. server 的工具注册为模型可见工具，命名为 `mcp__<server>__<tool>`。
4. 设置里的「MCP」分区：server 卡片、连接状态、工具数、工具名、server 级开关。
5. **规模控制**：Direct 配额 20、每 server allow/deny、超量走检索（`mcp_tool_search`）。

### 这次不做（明确边界）

- **不做 `mcp_call` 网关**。方案 A 已覆盖调用路径，再留一个网关等于第二套调用方式（本项目明令避免）。检索不到就让模型换词重搜。
- **不做 HTTP/SSE 传输**。第一版只 stdio；配置里出现 `type: "http"` 时明确报"本版不支持"，不静默忽略。
- **不做 MCP prompts / resources / elicitation / sampling**。只做 `tools/list` + `tools/call`。
- **不做 MCP 市场**（需要联网目录与审核，且与"不做插件市场"的既有裁定同类）。
- **不给子 Agent MCP 工具**（`delegated_workflow` 不传），理由：不可信外部内容 + 额外模型调用成本。
- **工具级独立开关已追加实现**（M5 补充；本轮继续补齐白名单编辑，见 §10）。
- **不做按 server 分组的自定义审批策略编辑器**（沿用既有两个档位 + 审批）。

## 2. 核心决策（已拍板，不再讨论）

| 决策 | 取值 | 理由 |
|---|---|---|
| 工具暴露形态 | **A 动态注入**：`mcp_tool_search` 返回 top-8，当轮加入工具表 | 体验最好；代价是前缀失效一轮，Codex 同 |
| 新 server 默认 | **关** | 与"新能力默认关"同一口径；打开才连、才下发 |
| Direct 配额 | **20** | 20 × 约 300 token ≈ 6k token，可接受 |
| 配置载体 | 独立 `mcp.json`，与 `config.yaml` 同目录 | `config.yaml` 是 `extra="forbid"`，加结构会让旧配置升级直接失效 |
| 开关与 allow/deny 存哪 | **设置库**（`settings_meta`），不写 `mcp.json` | 开关是用户状态不是部署配置；`mcp.json` 可原样从 CodeBuddy 复制不改一字 |
| SDK | 官方 `mcp`（版本在 `uv add` 时确定） | 握手/分页/通知/错误码自己写是高成本重复 |

## 3. 规模处理逻辑（写进契约，实施与验收都按这个）

四个不等于：

> 配置 N 个 ≠ 连 N 个；连 N 个 ≠ 给模型 N×K 个；给模型 N×K 个 ≠ 每轮都发；每轮都发 ≠ 全量进提示。

| 阶段 | 规则 | 数值 |
|---|---|---|
| 解析 | 只校验，**不 spawn** | — |
| 连接 | 只连 `enabled`；并发闸 | 4 |
| 超时 | 启动 / 调用 | 30s / 60s |
| 回收 | 空闲断开，保留目录缓存 | 600s |
| 目录 | `tools/list` 结果落 sqlite 缓存，**不进系统提示** | — |
| 分档 | `Direct` / `Deferred` / `Hidden` 三档 | — |
| Direct 配额 | 稳定排序后取前 N | 20 |
| 字节闸 | 单条 schema 超限直接 `Hidden` | 8192 B |
| 检索 | top-k | 8 |

判定顺序（固定，重启后结果一致）：

1. server 开关关 → 整台 `Hidden`
2. 命中 `disabled_tools` → `Hidden`
3. 配了 `enabled_tools` 而工具不在其中 → `Hidden`
4. 单条 schema 字节超限 → `Hidden`
5. 剩余按「server 配置顺序 → 工具名升序」稳定排序，前 20 → `Direct`
6. 其余 → `Deferred`

**稳定排序不是洁癖**：顺序一变，工具名序列就变，系统前缀缓存整段失效（架构 §6.7 记过同类代价）。

## 4. 契约改动点（先改 `ARCHITECTURE.md`，再编码）

| 节 | 要写进去的东西 |
|---|---|
| §6.9 能力闭集 | `mcp` 能力的成员**来自运行期**：静态表只登记两个固定工具，动态成员必须是「已连接 server 的 `tools/list` 结果」且带 `mcp__` 前缀。这是闭集**唯一**开口点，不得有第二个 |
| §6.9 可见性 | `is_visible` 增加动态工具名参数；执行侧拦截仍走同一入口，不允许 MCP 工具绕过 |
| §6.10 外部内容 | MCP 工具描述与调用结果都是外部内容：描述进工具 schema（否则模型不会用），但结果按 `untrusted` 处置、不进长期记忆 |
| 新增 §6.13 | MCP 工具契约：命名、三档暴露、Direct 配额、字节闸、检索 top-k、超时、失败文案 |
| §16.1 开关 | `mcp` 能力默认关；关闭时行为与接入前**逐字一致**（工具不绑、提示不提） |
| §17 安全 | MCP Server 的 `command` 是本机任意命令（`npx -y <pkg>` 等于联网下载并执行第三方包）：首次连接必须展示完整命令行并确认一次；之后按 server 级信任 |

## 5. 任务拆分（M1–M6，按顺序，每个都能独立验收）

### M1｜配置、目录与数据模型

- **目标**：能把 `mcp.json` 读进来并解释，不启动任何进程。
- **落点**
  - 新增 `local-agent-web/src/local_agent/mcp/config.py`：`load_mcp_config(path)` → `McpConfig(servers: dict[str, McpServerSpec])`。字段 `type`（仅 `stdio`）、`command`、`args`、`env`、`enabled_tools`、`disabled_tools`。重名、空 `command`、未知 `type`、非字符串 `env` 一律报错并指出是哪一条。
  - 新增 `local-agent-web/src/local_agent/mcp/catalog.py`：`ToolEntry(server, name, canonical_name, description, schema_bytes, input_schema)`、`McpCatalog`（稳定排序 + 三档标注 + 缓存读写）。
  - `config.py:893-895` 附近新增 `mcp: McpConfig`（`enabled` / `max_direct_tools=20` / `max_connected=4` / `idle_timeout_seconds=600` / `startup_timeout_seconds=30` / `call_timeout_seconds=60` / `max_tool_schema_bytes=8192`）；`config.py:998-1039` 的加载链路带上 `mcp.json` 路径（与 `config.yaml` 同目录；冻结形态走 `app_paths.default_config_path()` 的同级）。
- **验收**
  1. 合法 `mcp.json`（含 CodeBuddy 原样格式）解析成功，字段逐项对得上。
  2. 五类错误各自给出可照做的文案，并指明是哪个 server。
  3. 解析阶段**不产生任何子进程**（用测试断言，不靠肉眼）。
  4. 缺文件时返回空配置，行为与未接入前一致。
- **明确不做**：`http`/`sse` 字段的兼容解析（直接报不支持）。
- **配置示例**（`mcp.json`，与 `config.yaml` 同目录；`env` 里是明文凭据，不要提交）
  ```json
  {
    "mcpServers": {
      "lanhu": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-lanhu"],
        "env": { "LANHU_COOKIE": "..." },
        "enabled_tools": null,
        "disabled_tools": []
      }
    }
  }
  ```
- **状态（2026-10-20，已完成）**：新增 `local-agent-web/src/local_agent/mcp/__init__.py`、`mcp/config.py`（`McpServerSpec` / `McpServersFile` / `load_mcp_servers` / `dump_mcp_servers`）、`mcp/catalog.py`（`ToolEntry` / `ServerCatalog` / `build_server_catalog` / `canonical_tool_name` / `expose_all` / `search`）；`config.py` 新增 `McpConfig` 并挂进 `AppConfig`；`config.yaml.example` 加 `mcp` 段。**`mcp.json` 不提供 `.example`**：空配置与缺文件等价（`allow_missing`），示例写在本文档，避免用户复制一份带假凭据的文件。
- **M1 的验收证据**：新增 `tests/test_mcp_config.py` 15 条（CodeBuddy 原样格式、缺文件/空配置、JSON 错误带行列、顶层非对象、未知顶层键、非法 server 名、`type: http` 专门文案、空 command、空 arg、env 键非法、未知字段 `commnd`、白黑名单重叠、名单重复项、顺序保持、dump 往返）与 `tests/test_mcp_catalog.py` 22 条（命名形状与规范化、超长截断后互不相同、目录按名排序、无名工具跳过、规范化冲突加后缀、字节数口径、六类分档与原因、配额跨 server 累计、配额 0、重复分档逐项一致、绑定顺序、检索命中/未命中/空查询/限额）。**37 passed**；`ruff check` 与 `mypy`（4 个源文件）通过；`tests/test_capabilities.py` 与 `tests/test_config_paths.py` 共 28 条回归通过。
- **M1 未做（如实列出）**：① 目录缓存（sqlite）尚未实现，属 M2；② 界面尚无任何 MCP 入口（属 M5）；③ 一份 `mcp.json` 里的 server 名与设置库里的旧名字不同时如何处置未定义（改名即视为新 server，旧状态残留，M4 再定清理口径）。

### M2｜连接管理与生命周期

- **目标**：连得上、断得开、挂一个不拖垮全部。
- **落点**
  - 新增 `local-agent-web/src/local_agent/mcp/client.py`：单 server 的 `initialize` → `tools/list`（含分页 cursor）→ `tools/call` → `close`，全部带超时。失败分类：可执行文件找不到 / 启动超时 / 握手失败 / 调用超时 / 协议错误。
  - 新增 `local-agent-web/src/local_agent/mcp/manager.py`：连接池（启动**串行**，见状态里的偏离说明）、空闲回收（600s）、失败隔离与冷却、请求驱动的预热 worker（照 Codex `core/src/session/mcp_prewarm.rs:1-4,21-67` 的形状）、`snapshot()` 给出当轮目录。
  - 子进程登记进既有 `security/process_guard.py:148` 的 `ProcessRegistry`，退出时统一回收（Windows 上只杀直接子进程这件事要如实记录，照 `V0_6_DEVELOPMENT_PLAN.md:397` 的结论）。
- **验收**
  1. 假 stdio server（本地 python 脚本）能完成 list/call。
  2. 启动超时、调用超时、进程中途崩溃三种情况各自给出明确失败，且**不重试三轮**。
  3. 并发闸生效：20 个 server 同时请求连接时，同时在连的不超过 4。
  4. 空闲回收后目录缓存仍在，重连是懒连接。
  5. 一个 server 崩溃不影响其他 server 的调用。
- **明确不做**：自动重启（崩溃后等下一次调用再懒连接，避免重启风暴）。
- **状态（2026-10-20，已完成）**：依赖 `mcp==2.3.0`（`uv add` 解析结果；SDK 2.x 把 `FastMCP` 改名为 `MCPServer`、客户端在 `mcp.ClientSession`）。新增 `local-agent-web/src/local_agent/mcp/errors.py`（失败分类闭集 + `McpFailure`）、`mcp/client.py`（`ServerWorker`，一 server = 一线程 = 一 event loop）、`mcp/manager.py`（`McpManager`：连接池、目录、淘汰、回收、预热、冷却）。
- **两处与计划的偏离（如实记录）**
  1. **启动并发 4 → 串行**。原计划用信号量控制"同时握手 4 台"。实现改为**后台预热线程串行连接**：预热在后台跑，用户不等待；串行还避免了 4 个 `npx` 同时下载包把网络打满。并发收益（更快就绪）在本场景不值得额外复杂度。
  2. **`_Slot` 不持有工具目录**。目录与连接解耦：空闲回收/淘汰后连接没了，界面仍要显示上次的 3 个工具。目录存在 `McpManager._catalogs`，只有重连成功才更新。
- **M2 的验收证据**：新增 `tests/test_mcp_client.py` 9 条（真实子进程 + 真实 stdio：工具清单与描述、正常调用文本、工具内异常 → `is_error`、调用超时、命令不存在（文案含 Node.js 提示）、启动即崩、启动超时、重复 start、close 后 `alive` 为假）与 `tests/test_mcp_manager.py` 7 条（未启用从不 spawn、启用后可调、Direct 配额把其余推进检索、一台崩不影响另一台、冷却期内不重复 spawn、容量淘汰最久未用、空闲回收后回 `idle` 且目录仍在）。加上 M1 共 **53 passed**；`ruff check` 与 `mypy`（6 个源文件）通过。测试用 `tests/mcp_fake_server.py`（SDK 自带 server 端），**不联网、不花钱**。
- **M2 未做（如实列出）**：① 子进程回收**没有**接 `ProcessRegistry`——`close()` 只 join 线程，Windows 上 `npx` 派生的 node 子进程是否残留**未实测**；② `npx` 在 Windows 是 `npx.cmd`，现在只做 `shutil.which` 解析，**未经真实 `npx` 验证**（M6 实测）；③ 调用被超时取消后连接是否仍可用（MCP 协议允许并发请求，但本实现串行），未单独验证；④ 目录未落 sqlite（进程重启后要重新 `tools/list`），属 M4。

### M3｜能力表开口、注册表与安全门（风险最高，单独一批）

- **目标**：让动态工具名进入既有三张表，且**不稀释失败闭合**。
- **落点**
  - `capabilities.py:40-67`：`CAPABILITY_TOOLS` 加 `"mcp": ("mcp_tool_search",)`；`:106-123` 加 `"mcp": False`；`:330-347` 加标签「MCP 外部工具」；`:73` `UNTRUSTED_CAPABILITIES` 加 `mcp`；`:379-391` 加关闭时的解释文案。
  - `capabilities.py:522-547` `tool_names_for` 增加 `discovered: Sequence[str] = ()`，只在 `mcp` 能力位置插入（**唯一开口点**）。
  - `capabilities.py:505-519` `is_visible` 同步增加该参数。
  - `tools/tool_registry.py:46-62` `build()` 增加 `mcp_tools`；`:92-117` 追加外部工具；`:125-147` `assert_capability_coverage` 允许 `mcp__` 前缀归 `mcp`，但**必须**同时满足：名字带前缀、来源在注册时给出、不与其他能力重名。
  - `security/policy.py:92-100` 增加 `mcp__` 前缀识别（不穷举）；`:469` `classify_tool` 在兜底拒绝（`:558-562`）之前加 MCP 分支：默认需审批；`:117-183` `action_fingerprint` 加 MCP 分支（server + tool + 参数摘要）。
  - `graph/nodes.py:1642-1659`（`is_visible` 调用处）传入 registry 的动态集。
- **验收**
  1. 未配置的 `mcp__foo__bar` 调用被拒（安全门 + 执行侧两处）。
  2. 已连接的 server 的工具可被安全门识别，且默认进入审批。
  3. `assert_capability_coverage` 仍能抓出"实现未登记""登记未实现""重复归属"三类问题（既有用例必须全绿）。
  4. `mcp` 能力关闭时，`mcp_tool_search` 不绑、提示不提、`mcp__*` 调用被拒。
  5. 同一份目录重复构建，工具名序列**字节一致**。
- **明确不做**：让 MCP 工具跳过审批（那等于把外部工具当本机工具信任）。
- **状态（2026-10-20，已完成）**
  - `capabilities.py`：新增 `EXTERNAL_TOOL_PREFIX = "mcp__"`（**唯一**开口标识，三处共用）；`CAPABILITY_TOOLS["mcp"] = ("mcp_tool_search",)`（只登记固定的检索工具，外部工具名不进静态表）；默认 `False`；标签「MCP 外部工具」；进 `UNTRUSTED_CAPABILITIES`；补关闭文案。
  - `capabilities.is_visible`：新增 `external_names` 参数。判据是"名字带前缀 **且** `mcp` 在**本轮能力集**里 **且** 名字在本轮已知集合里"；`chat` 模式恒不可见；`cancelled` 仍然一票否决。
  - `tools/tool_registry.py`：`build()` 新增 `mcp_tools` / `mcp_direct` / `mcp_search_provider`；构造时校验外部工具名前缀；`__init__` 检测重名（外部与内置同名会静默覆盖）；`assert_capability_coverage` 把 `mcp__` 归到 `mcp` 能力并额外要求"能力表里确实有 mcp"；`tools_for` 只追加 `Direct` 档；新增 `external_names()`（**全部**已知外部工具，含 `Deferred`）。
  - `tools/mcp_tools.py`：`build_mcp_tool`（schema 原样用 server 那份）、`build_mcp_tool_search`（返回工具名 + 参数结构）、`build_mcp_tools`。`McpManager` 用 `TYPE_CHECKING` 导入——主路径不为"MCP 工具存在"付 SDK 的导入开销。
  - `security/policy.py`：`classify_tool` 在兜底拒绝前加 `mcp__` 分支 → `_classify_mcp`（**免审批**，2026-10-21 按用户决定改，理由见下；只读子任务直接拒绝）。
  - `graph/nodes.py`：`safety_gate_node` 传入 `external_names`；`_invoke_tool` 增加 `McpFailure` 分支（不再套"工具执行失败（类型）"，直接用其可展示文案）。
- **一处与计划的偏离（如实记录）**：**`security/approval.py` 没有改**。计划里写"指纹加 MCP 分支"，实测不必：`action_fingerprint` 的 `parts` 首项就是 `tool_name`，而外部工具名是 `mcp__<server>__<tool>`，server 已进名字；兜底分支的 `json.dumps(args)` 也已覆盖参数。多写一个分支只会多一份要维护的口径。
- **M3 的验收证据**：新增 `tests/test_mcp_policy.py` 10 条（外部工具必须审批、只读子任务拒绝、**未登记的普通工具仍然失败闭合**（前缀分支没有顺手放宽兜底）、能力开关决定可见性、名字不在目录里不可见、chat 恒不可见、取消一票否决、静态工具不受影响、指纹区分 server、指纹随参数变化）与 `tests/test_mcp_registry.py` 7 条（外部工具已注册且 `external_names` 只列外部、能力关两者都不可见、能力开绑检索 + Direct、Deferred 不绑但可执行、`mcp_direct` 为空时只绑检索、无前缀拒绝、同名拒绝、检索工具总是被构造）。回归：`test_security.py` + `test_capability_boundary.py` + `test_prefix_cache_report.py` **91 passed / 2 skipped**；`test_api.py` **114 passed**；`test_capabilities.py` **22 passed**；本次改动范围 `ruff check` 全绿；`mypy` 10 个源文件无问题。
- **M3 未做（如实列出）**：① 装配层还没接（`builder.py` 不传 `mcp_tools`，所以运行期 MCP 工具实际注册数为 0），属 M4；② 检索命中后**跨轮**保留（`state` 记录已发现集合）未做——同一轮内模型搜到工具后，下一轮要再搜一次；③ ~~server 级信任（免审）未做~~ → 2026-10-21 已改为**免审批**，见"决策变更"；④ `mcp.json` 热重载未做。

### M4｜装配与开关存储

- **目标**：开关能改、下一轮生效、不需要重启。
- **落点**
  - `graph/builder.py:430-552`（`build_runtime_context`）：从 manager 取目录快照传给 `ToolRegistry.build`；`:525-539` 增加 `mcp_tools` 与 `mcp` 配置。
  - `graph/delegated_workflow.py:207`：**不**传（有意为之，文档写清）。
  - `model_settings/store.py:80` 新增 meta 键 `mcp.server_states`（JSON：`server → {enabled, enabled_tools, disabled_tools}`），读写入口照 `CapabilityStore`（`store.py:253-331`）的形状。
  - `application/service.py:1515-1552` 附近新增 `mcp_view()` / `set_mcp_server_enabled()` / `set_mcp_server_tools()`；`:2996-3023` 的 `_context` 带上目录快照。
- **验收**
  1. 改 server 开关后**下一轮**工具表随之变化，不需重启。
  2. 关掉某 server 后其工具立刻从工具表与检索索引消失。
  3. allow/deny 生效，且 deny 优先于 allow。
  4. 设置库不可用时退回"全部 server 关"，并给出可见原因（不静默当成开着）。
- **状态（2026-10-20，已完成）**
  - `mcp/state.py`（新）：`ServerState` 从 `manager.py` 移出——设置库要用它，不该为此导入 `mcp.client`（→ SDK）。
  - `model_settings/store.py`：新增 `META_MCP_SERVERS = "mcp.servers"` 与 `McpServerStore`（`states` / `put` / `forget`），挂进 `SettingsDatabase.mcp`，随 `open_settings_database` 一起构造。脏行（`enabled` 非布尔、名单类型不对）一律**按关**处理。
  - `mcp/binding.py`（新）：`McpBinding`（manager + states + discovered）与 `McpManagerLike` Protocol。装配层因此只依赖一个结构类型，不导入 SDK。
  - `graph/builder.py`：`build_runtime_context` 新增 `mcp: McpBinding | None`；`mcp` 能力开着且未注入注册表时，从 `manager.snapshot()` 取目录 → `mcp_tools`，并把 `snapshot.direct` + `McpBinding.discovered` 合成 `mcp_direct`（发现的工具可能超出 Direct 配额，所以单独补）；检索来源用闭包读**当前**目录。
  - `capabilities.enabled_capabilities` 新增 `mcp_available`（默认 `True`，保持既有调用点语义不变）：没有连接管理器的路径（CLI、委派子图、多数测试）即使开关写着真也不把 `mcp` 放进能力集——否则模型会看到一个搜不到东西的检索工具。
  - `config.py`：`capability_default_layer` 里 `mcp` 取下 `mcp.enabled`（MCP 不在任何内置档位里）；`apply_capability_gates` 把 `mcp.enabled` 同步为开关值。
  - `application/service.py`：新增 `mcp` / `mcp_manager` 两个可选注入位与 `_mcp_states()` / `_mcp_binding(session)`；`_context` 传 `mcp=`。
  - `api/container.py`：新增 `build_mcp_manager(...)`（开关关着时**完全不建**，连 SDK 都不导入；`mcp.json` 解析失败只记警告、不阻断启动），`ApiRuntime.mcp_manager` 在 `close()` 与装配失败路径里都会被关掉。
  - `graph/state.py` + `graph/nodes.py`：新增会话级字段 `discovered_mcp_tools`；`tool_execution_node` 在 `mcp_tool_search` 成功后从返回文本里提取工具名，写"旧集合 ∪ 新发现"（**覆盖语义**，不用追加 reducer——否则每次搜索都重复累加）。
  - `graph/delegated_workflow.py`：补注释说明"子 Agent 有意不给 MCP"（能力集与注册表两层都不给）。
- **M4 的验收证据**：新增 `tests/test_mcp_store.py` 6 条（默认关、写入读回、互不影响、残留条目保留、脏行按关、forget）与 `tests/test_mcp_binding.py` 7 条（无绑定不绑任何 mcp 工具、能力关时即便有绑定也不绑、Direct 绑而 Deferred 不绑、`discovered` 跨轮保留、失效名字被忽略、gate 跟随开关、`ServerState` 默认关）。全部 MCP + 能力用例 **105 passed**；`test_application.py` **67 passed**；`test_api.py` + `test_graph_resume.py` **189 passed**；改动范围 `ruff check` 与 `mypy`（21 files）通过。
- **M4 未做（如实列出）**：① 页面/接口还没有任何 MCP 入口（属 M5）；② `_mcp_binding` 每轮多读一次 Checkpoint（拿 `discovered`），仅在 MCP 管理器存在时发生；③ 预热线程通过 `settings.mcp.states()` 读设置库，**跨线程复用同一条 sqlite 连接未加锁**——风险已记，M5 接 API 时一并评估；④ `mcp.json` 解析失败只记日志，错误详情要等 M5 的读取接口才能展示给用户。

### M5｜API 与前端

- **目标**：界面能看、能开、能看明白为什么不可用。
- **落点**
  - `application/models.py:259` 附近新增 `McpServerView` / `McpToolView` / `McpView`；`api/schemas.py:329` 附近新增请求体。
  - `api/routes.py:616-637` 附近新增 `GET /api/mcp`、`PUT /api/mcp/servers/{name}`、`PUT /api/mcp/servers/{name}/tools`、`POST /api/mcp/reload`；`api/container.py:227-231` 装配依赖。
  - 前端新增 `features/mcp/McpSection.tsx`、`McpServerCard.tsx`、`useMcp.ts`；挂到 `ModelSettingsDialog.tsx:113-128` 的 `extraSections`（与 `skills` 并列）。
  - 页面文案只讲能力不讲工具名（§16.6 口径），但 MCP 例外：工具名是用户自己配的外部资产，必须展示。
- **验收**
  1. 50 个 server 的极端情况下，页面显示「已配置 50 · 已启用 12 · 已连接 10 · 失败 2 · 工具 380 · 直接可见 20 · 其余 360 按需检索」。
  2. 每个失败项后面跟可照做的原因，而不是一个红点。
  3. `mcp.json` 含 `env` 时页面提示"该文件含密钥，请勿外传"。
  4. 开关切换后不刷新整页即可看到状态变化。
- **明确不做**：在页面里编辑 `env`（避免把密钥写进接口与日志）；编辑请直接改 `mcp.json`。

## M5 完成记录

- **状态（2026-10-20，已完成）**
  - `application/models.py`：`McpToolView` / `McpServerView` / `McpSummaryView` / `McpView`。**工具名在列**（与 §16.6 相反，理由见 M5 落点最后一条）；`env` 永不出现在任何 DTO 里。
  - `api/schemas.py`：`McpServerRequest`（`enabled` / `enabled_tools` / `disabled_tools`，按"有没有传这个键"区分"不改"与"取消白名单"）。
  - `api/routes.py`：`GET /api/mcp`、`PUT /api/mcp/servers/{name}`、`POST /api/mcp/reload`；`_MCP_WRITE_RESPONSES`（422 配置/名字不合法、503 无设置库）。
  - `application/service.py`：`mcp_view()`（每次重读 `mcp.json` 现算 `config_error`）、`set_mcp_server()`（合并式写入 + 名单重叠校验）、`reload_mcp()`、`_mcp_known_servers()`、`_require_mcp_store()`；新增构造参数 `mcp` / `mcp_manager` / `mcp_config_path`。
  - `mcp/manager.py`：新增 `replace_servers()`（重载：断开并清掉新配置里已删除的 server）；`snapshot()` 改为"**有目录就参与分档**"——未连接但有历史目录的 server 不再让工具突然消失（工具对象在，调用时懒连接）。
  - 前端：`api/client.ts` 三个函数、`api/queries.ts` 三个 hook + `queryKeys.mcp`、`features/mcp/McpSection.tsx` + `McpSection.module.css` + `McpSection.test.tsx`（5 条）、`SettingsDialog` 的 `SettingsSection` 增加 `'mcp'` 与分区项（归入"任务与工具"组）、`ModelSettingsDialog` 挂载 `mcp: <McpSection />`。
  - OpenAPI 重新导出（`scripts/export_openapi.py`）+ `npm run generate:api` 重新生成 `schema.gen.ts`。
- **实施中修掉的一个真实缺陷**：能力开关 `mcp` 打开后，管理器只在**进程启动时**按当时开关创建，用户点开开关必须重启才生效——与"保存即生效、作用于下一轮"的既有承诺矛盾。修法：`service._ensure_mcp_manager()` 懒建（开关打开的那一刻才导入 SDK 并建管理器，之后复用）。这条是 M5 最重要的产出，比三个接口本身更关键。
- **M5 的验收证据**：后端新增 3 条 API 用例（默认关闭的零状态、写坏配置的 422 带"不是合法 JSON"、未声明 server 的 422 + 声明后可写）。后端**全量分批**跑：497 + 927 + 738 passed（另有 `test_api` 189、`test_application` 67、MCP+能力 105、`test_security` 等 91 已单独跑过）。前端 **440 passed / 48 files**，`typecheck` 与 `build` 通过（`dist` 已重新产出）。
- **全量 pytest 的一次失败说明**：一次跑完 113 个文件时进程被环境终止（退出码 `0x40010004`，不是测试失败），改为分批后正常。这是执行环境限制，不是仓库问题。
- **两个既有失败（与本次改动无关，未修）**：`test_release_contracts.py` 找不到 `docs/release/compatibility-baseline.json`、`test_packaging_portability.py` 找不到随包 `skills/` 目录——两者在工作区与 `git ls-files` 里都不存在，属于仓库目录重组的遗留。
- **M5 未做（如实列出）**：① 工具级开关只有接口，页面只画了"直接可见/按需检索"标签，没有逐工具勾选 UI（`enabled_tools` 的编辑入口未做）；② 没有"添加 server"表单，仍是手改 `mcp.json` + 点「重读配置」；③ 预热线程读设置库仍跨线程复用连接（未加锁，风险同 M4）；④ 前端未做 e2e（`e2e/` 里没有 MCP 用例）。

### M6｜真实联调与收口

- **目标**：用真实 server 跑通，且成本与风险可控。
- **验收**
  1. 用本机 `lanhu`（`npx -y mcp-lanhu`）连接成功，`tools/list` 得到 3 个工具（`lanhu_design` / `lanhu_page` / `lanhu_resolve_invite_link`）。
  2. 一次真实调用（用户指定要查的内容），**只跑 1–2 次**，报出实际花费。
  3. 结果按 `untrusted` 落库（不进长期记忆）。
  4. 断开网络/杀掉 server 后，失败文案可照做。
  5. 全量 pytest 收口"通过才提交"。

## 6. 安全与不可信内容

- `npx -y <pkg>` 等于**联网下载并执行第三方包**：首次连接显示完整命令行，要求确认一次。
- `mcp.json` 里的 `env` 是明文密钥：文件位置不放仓库，页面提示不外传，**不进任何 DTO / 事件 / 日志**（沿用 `policy.redact`）。
- MCP 工具描述进工具 schema（模型需要它才能用对），因此**描述本身是提示注入面**：不进系统提示、单条字节上限 8192、结果标 `untrusted`。
- 调用失败一律如实报告，不伪造"稍后可用"。

## 7. 外部对照（已核验）

| 维度 | Codex（源码级） | CodeBuddy（本机行为级） | Trae（官方文档级） |
|---|---|---|---|
| 命名 | `mcp__<server>__<tool>`（`codex-mcp/src/tools.rs:57-61`） | MCP 工具以 `mcp__` 开头（其自身工具描述原文） | 未核 |
| 暴露 | `Direct`/`Deferred`/`Hidden` 三档（`tools/src/tool_executor.rs:49-56`），开 search 时一律 Deferred（`core/src/mcp_tool_exposure.rs:85-89`） | 未核 | 未核 |
| 检索 | 内置 `tool_search`，top-8，默认开（`tools/src/tool_discovery.rs:6-7`） | 未核 | 未核 |
| 每 server 筛选 | `enabled_tools` / `disabled_tools`（`config/src/mcp_types.rs:292-298`） | 未核 | 未核 |
| 字节预算 | 每 server schema 上限；插件工具单条 8KB、合计 64KB（`core/src/mcp_tool_exposure.rs:18-19`） | 未核 | 未核 |
| 生命周期 | 会话级预热 worker（`core/src/session/mcp_prewarm.rs:1-4,21-67`） | 未核 | 未核 |
| 配置作用域 | 用户级 | 用户级 `~/.codebuddy/mcp.json` | 用户级 + 项目级 `.trae/mcp.json`（项目级需确认） |

「未核」= 闭源或无源码可读，不猜测。

## 8. 风险

| 风险 | 触发条件 | 处置 |
|---|---|---|
| 闭集开口被稀释 | `assert_capability_coverage` 的例外写宽 | 例外只认 `mcp__` 前缀 + 注册时给出的来源，两条同时满足 |
| 前缀缓存反复失效 | 目录顺序不稳定 | server 配置序 → 工具名升序，用例钉住 |
| 50 个 server 吃光内存 | 全部常驻 | 空闲 600s 回收 + 并发闸 4 |
| 提示预算被工具定义吃满 | 全量 Direct | 配额 20 + 三档 + 字节闸 |
| 第三方包在本机执行 | 用户粘了不可信的 `mcp.json` | 首次确认展示完整命令行；项目级配置不做 |
| Windows 进程树残留 | `npx` 派生的 node 子进程 | 登记 `ProcessRegistry`；如实记录"只杀直接子进程"的已知限制 |

### M5 补充：三项遗留做完（2026-10-20）

用户要求把 M5 列出的三项"未做"补齐，已全部落地。

1. **工具级勾选**（写 `disabled_tools`，只记"禁用了哪些"）
   - `McpToolView` 新增 `disabled: bool`（由 `ExposureRow.reason == "in_disabled_tools"` 得出）。为什么不复用 `exposure`：停用是**用户可撤销的选择**，超配额是**系统行为**，页面要分开画。
   - 前端把工具 chip 变成按钮：点一下停用/启用，只发 `{disabled_tools: [...]}`；停用态打删除线并显示「已停用」。
   - 回归：`McpSection.test.tsx` 两条（停用只发 `disabled_tools`、重新启用时从列表移除）。

2. **添加 server 表单**（替代手改 `mcp.json`）
   - `mcp/config.py` 新增 `check_server_name` / `spec_to_body` / `save_mcp_servers`。写入用**同目录临时文件 + `os.replace`**：写到一半退出不该让用户拿到一份截断的 JSON（里面通常还有凭据）。
   - `POST /api/mcp/servers`：只支持 stdio；同名拒绝（**不允许"添加"顺手覆盖一份带凭据的配置**）；现有文件解析失败时拒绝写入（先修好再添加）；写完立即 `replace_servers` 重载。
   - `env` 允许传但**只写不回读**：`McpView` 没有任何 env 字段，日志不记。前端按行 `KEY=VALUE` 解析，**只按第一个等号切**（cookie 本身就带等号）。
   - 回归：`test_api.py` 三条（写入成功且响应不含凭据值、同名 422、非法名 422）、`test_mcp_config.py` 三条（原子写往返、不留临时文件、写失败时原文件不变）、`McpSection.test.tsx` 一条（参数按空白切、env 按第一个等号切）。

3. **跨线程 sqlite 加锁**
   - `McpServerStore` 自带 `threading.RLock`（`states` / `put` / `forget` / `_read` / `_write` 全部串行）。这把锁只保护本 store 的那一行；主线程读写「能力开关」走的是另一个 store，两者都靠 sqlite 自身的连接级串行——**这里只把"MCP 预热线程读开关"这条我们自己引入的并发消掉**，不声称覆盖全部跨线程访问。
   - 内部拆出 `_read_unlocked` / `_write_unlocked` / `_states_unlocked`，避免 RLock 重入时重复加锁。

- **验证**：后端 `test_mcp_* + test_model_settings + test_security` **239 passed / 2 skipped**；前端 **443 passed / 48 files**，`typecheck` 与 `build` 通过（`dist` 已重出）。
- **仍不做**：编辑与删除已有 server（只能手改文件）；`enabled_tools` 白名单的 UI（只有逐工具停用）。

## M6 联调记录（2026-10-20，用户授权真实调用）

- **连接（不花钱的一步先做）**：`npx -y mcp-lanhu` 在本机连通——`lanhu: status=connected tools=3`，三个工具全部进 `Direct`（配额 20 足够）。**M2 遗留的 Windows `npx.cmd` 疑点就此排除**：`shutil.which` 解析成绝对路径后 SDK 能直接 spawn。
- **绑定**：`tools_for("tool", 能力集)` 里出现 `mcp_tool_search` + `mcp__lanhu__lanhu_design` / `mcp__lanhu__lanhu_page` / `mcp__lanhu__lanhu_resolve_invite_link` 四个名字。
- **schema 真实传递**：真实模型（`deepseek-flash`）在没有被喂任何提示的情况下，准确复述了 `lanhu_design` 的参数（`url` / `mode: list|analyze|slices|tokens` / `design_names` / `include` / `layer_depth` 默认 4）。这些字段只可能来自 server 的 `tools/list`，不是我们编的。
- **没有实际执行 lanhu 工具**：模型缺一个真实项目链接，选择如实说"缺参数、没法读"，并明确拒绝编造（`task_status=blocked`）。**MCP 调用通路**本身由 `test_mcp_client.py` 的真实 stdio 用例守住（echo/超时/崩溃），这里只是缺一个可用的业务参数。
- **既有降级（与本次无关）**：能力探测报 `structured_json_schema -> False`（`OpenAIInvalidRequestError`），`deepseek-flash` 不支持结构化输出，走了 JSON 提示降级——这是既有行为。
- **验证中发现并修掉的安全缺陷（本次最重要的一条）**：模型**确实用 `read_file` 打开过 `local-agent-web/mcp.json`**（回答里引用了 `mcp.json:1-12`）。虽然它没把内容说出去，但文件里是明文 cookie。修法：`path_guard.SENSITIVE_NAME_PATTERNS` 增加 `mcp.json`，读入口**直接拒绝**（不是脱敏后放行）；回归见 `tests/test_mcp_secret_protection.py`（3 条：文件名命中、读被拒且错误消息不带内容、其它 `*.json` 不受影响）。同时 `.gitignore` 增加 `mcp.json`。
- **成本未记录（如实说明）**：这次脚本没有挂 `usage_recorder`，所以 `state.usage` 为空、查不到 token 数。按调用结构估：能力探测 1 次 + 主循环若干次决策（工具结果 8 条）+ 收尾，量级是"几厘到几分钱"。下次要报准 token，必须在脚本里接录音机。

### M6 追加：真实图形链路跑通，并抓出第二个与第三个缺陷

- **真实执行成功**：用用户给的真实项目链接，Agent 模式下模型调用 `mcp__lanhu__lanhu_design`（两次）与 `mcp__lanhu__lanhu_page`（一次），`tool_results` 状态均为 **`ok`**。这是 MCP 外部工具在本系统里第一次真正执行并回填结果。
- **缺陷 2（执行侧漏传动态集合）**：第一轮真实运行里，安全门放行了（弹了审批卡），**批准之后执行阶段却判 `denied`**。根因：`tool_execution_node` 里还有**两处** `is_visible`（并行只读预取过滤、审批恢复后的纵深防御），我只改了安全门那一处，没给它们传 `external_names` —— 默认空集合 = 外部工具一律不可见。审批恢复不经过安全门，所以执行节点是唯一一道门，漏传表现为"批准了也不执行"。修法：函数入口算一次 `external_names = registry.external_names()`，两处都带上。回归：新增 `tests/test_mcp_graph_e2e.py`（假模型 + 本地假 server，走完整图：安全门 → 审批 → 执行 → 结果回填，断言 `status == "ok"`）。
- **缺陷 3（检索工具漏白名单）**：`mcp_tool_search` 被安全门兜底拒绝（"尚未接入或不在放行清单内"）——它有 `mcp_` 前缀但**不是**外部工具（外部是 `mcp__`），而 `KNOWN_TOOLS` 白名单是硬编码元组，加能力时漏了它。后果是检索入口本身不可用，整个 `Deferred` 档失效。修法：加进 `READ_ONLY_TOOLS`（它只查本机已拿到的目录，不发外部请求）；回归见 `tests/test_mcp_policy.py` 的 `test_search_tool_is_read_only`。
- **这三条的共同点**：单测各自覆盖的层面都看不到它们——需要"整图 + 真实 server"或"真实模型"才会显形。M6 用 3 次真实调用换来这三个真实缺陷，是这一阶段最值钱的部分。
- **仍未跑完的一处**：脚本给审批往返设了 3 轮上限，模型在第三轮后仍在继续调用（`task_status=running`），所以那一次没有跑到终态。这不是产品行为，是脚本上限；不影响"执行通路已通"的结论。

## 决策变更（2026-10-21）

### MCP 工具调用改为**免审批**

原先 `_classify_mcp` 返回 `allowed=False, requires_approval=True`（一律弹卡）。改成
`allowed=True, requires_approval=False`。理由：

- **实测代价**：真实 lanhu 联调里模型一趟任务连着调 3~5 个工具，用户就要点 3~5 次；而人很快
  就不再读卡片内容直接点批准——那比不审批更糟，因为它把"审批"变成了仪式。
- **授权本来在别处**：一台 server 的 `command` 是用户亲手写进 `mcp.json` 的，那就是授权；
  调用它暴露的工具是**使用已授权的东西**，不是一次新决定。
- **不属于四项硬约束**：§6.5 要求人工批准的是删除文件、覆盖已有文件、破坏性 Shell、大范围
  编辑——都发生在**本机**。MCP 的副作用在远端系统，那个系统有自己的权限与审计。

仍保留的两条边界：

1. `allow_result_spill=False`（只读子任务）**直接拒绝**（不是审批）——子 Agent 没人盯着；
2. 名字必须来自本轮已知集合（`is_visible` 的 `external_names`），幻觉出来的名字执行不了。

**代价说清楚**：模型在 Agent 模式下可以连续调用外部工具而无人过目。若以后需要更强控制，
方向是"每台 server 一个 `approval` 模式（`auto` / `always`，默认 `auto`）"——**未实现**。

## 9. 未核实项（不写进结论）

1. `mcp` SDK 的版本号与协议版本——`uv add` 时以实际解析结果为准。
2. 本机 `lanhu` server 的实际工具清单与参数结构——M6 实测后才写进文档。
3. CodeBuddy 内部是否也做延迟加载、Trae 是否有工具级开关——闭源，不猜。


## 10. 代码审查与补全（2026-10-20）

本节覆盖前述历史“未做/未验证”记录的当前状态。范围仅 MCP；未改动已有真实 mcp.json、未提交 Git、未打包或替换正式 EXE、未调用真实模型或真实账号。

### 10.1 审查结论

【必须改】以下均已修复，位置为修复后代码行号（不是原始缺陷行号）：

- `local-agent-web/src/local_agent/application/service.py:1585` → 总开关关时不返回配置、停用状态依赖隐藏原因 → 不能管理配置且名单显示失真 → 独立投影配置、设置和缓存，停用状态直接读名单。
- `local-agent-web/src/local_agent/application/service.py:1719` → 缺少编辑/删除闭环，残留同名设置 → 无法管理连接、重建继承旧名单 → 添加 spec 编辑和 DELETE，按键合并 env、显式 remove_env，删除清状态和缓存；改连接后默认关闭。
- `local-agent-web/src/local_agent/mcp/manager.py:173` → 只清仍在池中的已删除连接、同名配置变动不换连接 → 旧命令/目录持续生效 → 按完整配置比较清理，重读配置显式刷新目录和连接。
- `local-agent-web/src/local_agent/mcp/manager.py:356` → 同台首次连接没有互斥，回收没有使用中标志 → 重复 spawn 或杀掉忙碌工具 → 串行建连去重、active 计数、容量满且全忙时返回错误。
- `local-agent-web/src/local_agent/api/container.py:307`、`application/service.py:2163` → 后台直接读共享设置连接，仅 MCP store 有锁 → 真实浏览器复现 sqlite InterfaceError/HTTP 500 → 后台和页面共用 Checkpointer 的应用层锁，装配完成后才启动预热。
- `local-agent-web/src/local_agent/mcp/client.py:237` → close 排队等待当前调用且丢弃线程引用 → 假性“已关闭”、退出拖延 → AnyIO CancelScope 取消，SDK 上下文负责回收进程树，仍保留线程引用。
- `local-agent-web/src/local_agent/mcp/client.py:290` → tools/list 不在启动时限内；运行期连接关闭被当成普通请求错误 → 无限等目录、坏连接反复复用 → initialize+list 共用时限；退出失败关闭连接并冷却，协议错误单独分类。
- `local-agent-web/src/local_agent/api/app.py:87`、`application/service.py:1719` → 校验异常含 input_value → env 凭据可能进入错误响应 → 只返回字段位置和安全错误说明，第三方 stderr 不直通宿主日志，连接错误移除已配置 env 值。
- `local-agent-web/src/local_agent/graph/builder.py:434`、`graph/nodes.py:1444` → 检索后只保存名字，当前任务的模型绑定未更新，后台新目录也不在注册表 → Deferred 工具当次任务不可用 → 检索同步动态成员，State 中已发现工具在下一次模型调用立即绑定，预算使用同一份工具表。
- `local-agent-web/src/local_agent/graph/builder.py:553` → Hidden 也进入注册表，历史 discovered 可重新绑定 → 停用/超大 schema 工具继续对模型可见 → 注册阶段排除 Hidden，执行阶段再次核对当前名单和目录。
- `local-agent-web/src/local_agent/graph/nodes.py:1855`、`mcp/catalog.py:112` → 发现解析漏连字符和长名；server/tool 分隔符歧义、规范化冲突依赖返回顺序 → 漏发现或工具重名 → 扩展解析，歧义加摘要，排序后去重及消歧。
- `local-agent-web/scripts/build_windows_package.py:152`、`src/local_agent/security/path_guard.py:146` → mcp.json 与原子保存临时文件未被完整保护 → 凭据误读/误分发 → 名称禁入及临时文件模式一并覆盖，调用真实扫描函数验证。
- `local-agent-web/frontend/src/features/mcp/McpSection.tsx:22` → 参数按空白切分、名单从当前工具清单重建 → 含空格参数损坏、已消失工具的停用记录丢失 → JSON 数组参数，完整名单来自 DTO；补编辑、删除确认、白名单、总开关、隐藏原因、不可写状态、连接状态刷新及窄窗布局。

【建议改】本轮已完成：`mcp/cache.py:24` → 重启丢目录 → 每台重新 tools/list → 独立 SQLite 缓存和配置指纹；`app_paths.py:81` → 源码默认配置随 CWD → 从别处启动丢 mcp.json → 固定源码项目目录并保留显式路径优先；`mcp/client.py:114` → 大返回仅靠通用处理、纯 structured_content 丢失 → 上下文膨胀或结果为空 → 文本上限 60,000 字符、明确原长度/截断提示，保留纯结构化结果。

【不用管】stdio-only、prompts/resources/sampling 暂不支持、子 Agent 不接 MCP 均保留原边界；页面已明确提示子 Agent 限制。

【存疑/未验证】本轮未重跑真实 lanhu 账号与真实模型。Windows 回收验证使用真实 `.cmd → Python → 子进程`，不等于测试所有第三方 npx 包的异常行为；60,000 字符截断已用 200,000 字符假返回验证，未重新测量 lanhu_design 的业务结果规模。

### 10.2 完成的功能

总结里的八项缺口均已落实：编辑、删除、打包禁入、白名单 UI、目录落 SQLite、源码路径稳定、子 Agent 提示、浏览器 e2e。

编辑规则：名字保持不变；命令/参数可编辑；env 留空不变，填写同名键更新，勾选键删除；连接方式变化后关闭 Server，需核对命令并重新启用。新加和同名重建默认关闭。原始 mcp.json 与环境变量值不会进入 MCP 查询响应。

缓存位于配置目录的 `data/mcp_catalog.sqlite`，仅工具元数据和配置摘要，不写入原始连接配置。重启可直接恢复目录；工具真实调用时懒连接并重新列举。升级 Server 后可以“重读配置”刷新缓存。

### 10.3 本轮文件清单

- `README.md`、`ARCHITECTURE.md`、`docs/plans/MCP_CLIENT_PLAN.md`：补当前总览、稳定契约、审查根因、边界和验证记录。
- `local-agent-web/src/local_agent/mcp/cache.py`（新增）、`client.py`、`manager.py`、`catalog.py`、`config.py`：缓存、连接生命周期、连接去重/回收、错误分类、结果处理、目录命名、配置校验。
- `local-agent-web/src/local_agent/application/service.py`、`models.py`：配置管理、状态投影、共享锁、生命周期及安全 DTO。
- `local-agent-web/src/local_agent/api/app.py`、`container.py`、`routes.py`、`schemas.py`、`__main__.py`：安全校验错误、装配和退出、两条管理路由、请求模型及稳定启动路径。
- `local-agent-web/src/local_agent/graph/builder.py`、`nodes.py`、`tools/tool_registry.py`、`tools/mcp_tools.py`：Hidden 过滤、同次任务检索绑定、动态目录同步及注释校准。
- `local-agent-web/src/local_agent/app_paths.py`、`security/path_guard.py`、`scripts/build_windows_package.py`：源码路径、临时凭据文件保护、分发扫描。
- `local-agent-web/frontend/src/features/mcp/McpSection.tsx`、`.module.css`、`.test.tsx`：页面管理功能、布局、错误反馈及组件回归。
- `local-agent-web/frontend/src/api/client.ts`、`queries.ts`、`schema.gen.ts`、`docs/api/openapi.json`：API 客户端、状态刷新、生成契约。
- `local-agent-web/scripts/serve_mcp_acceptance.py`、`frontend/playwright.mcp.config.ts`、`frontend/e2e/mcp-live.spec.ts`、`frontend/e2e/mcp-teardown.ts`（新增），`frontend/playwright.config.ts`：隔离的真实 API/stdio 浏览器验收，默认视觉套件排除新真服务用例。
- `local-agent-web/tests/test_api.py`、`test_app_paths.py`、`test_packaging_portability.py`、`test_mcp_client.py`、`test_mcp_manager.py`、`test_mcp_catalog.py`、`test_mcp_binding.py`、`test_mcp_config.py`、`test_mcp_graph_e2e.py`、`test_mcp_secret_protection.py`、`mcp_fake_server.py`：回归与真实进程证据。
- `local-agent-web/frontend/dist/`：重新构建的本地产物（gitignore）。

### 10.4 验证与复现

使用项目内 `.venv/Scripts/python.exe`，避免 uv 缓存目录权限；pytest 的临时目录显式放在工作区 `data/`。

```powershell
# local-agent-web 目录；每次换一个未使用过的 basetemp 名
.\.venv\Scripts\python.exe -m pytest tests/test_mcp_config.py tests/test_mcp_catalog.py tests/test_mcp_client.py tests/test_mcp_manager.py tests/test_mcp_store.py tests/test_mcp_binding.py tests/test_mcp_registry.py tests/test_mcp_policy.py tests/test_mcp_graph_e2e.py tests/test_mcp_secret_protection.py tests/test_api.py tests/test_app_paths.py tests/test_config_paths.py tests/test_packaging_portability.py -k "not test_the_shipped_skills_folder_exists_and_is_copied_into_the_package" --basetemp=data/pytest-mcp-check -q
# frontend 目录
npm test
npm run build
npm run check:api
npx playwright test -c playwright.mcp.config.ts
```

最终专项回归 **260 passed / 1 deselected**（排除已单独确认的随包 skills 缺失用例）；前端 **451 passed / 48 files**，typecheck/build/API 漂移检查通过；真实后端浏览器 **2 passed**（1440×900、640×800），截图已逐张检查。涉及源文件 Ruff、16 个源文件的定向 mypy 通过；整图 Deferred 用例跑到 completed，包含检索、工具入表、审批、执行。

未全绿的检查如实保留：完整打包便携测试仍缺 `packaging/skills`（1 failed；排除该条之前的广集为 249 passed / 1 failed）；扩展工具/图回归 **304 passed / 1 skipped / 11 failed**，失败均为当前受限环境 `CreateRestrictedToken 87`；全量 mypy 的 4 条既有图片类型诊断在 `tools/image_tools.py:111/112/115` 与 `graph/nodes.py` 的 `_split_image_blocks` 返回值，未修改其逻辑。本轮未重跑全量后端所有用例、未重跑全仓前端 lint；涉及前端文件 ESLint 通过。


## 11. 真实复验（2026-10-20，用户要求“继续跑真实”）

### 已实际完成

- 使用已有 `mcp.json` 在内存中加载 lanhu 凭据，真实启动 `npx -y mcp-lanhu`。连接 **1.36 秒**，`tools/list` 返回 `lanhu_design`、`lanhu_page`、`lanhu_resolve_invite_link`，随后关闭管理器。未输出凭据，未改原配置、数据库、开关和正式 EXE。
- 按设置库当前默认 `deepseek-reasoner` 构造真实客户端；供应商返回的每条 AI 消息 `response_metadata.model_name` 均为 **`deepseek-flash`**。这说明本次实际响应名与保存的请求模型名不同，不据此推断所有旧模型名的映射规则。
- Direct 配额强制为 **0**，三个真实工具全部 Deferred；真实模型先调用 `mcp_tool_search` 检索 `design page`，发现两个工具，读取其真实参数定义并正确完成目录核对，调用 `task_control` 后达到 **`task_status=completed`**；无 `error_events`。
- 总耗时 **8.68 秒**，模型调用 **3 次**。供应商实报 token：未缓存输入 **8,444**、缓存命中输入 **13,568**、输出 **852**（其中 reasoning **32**）；缓存写入字段未报告，不记作 0。
- 根据实际响应名 `deepseek-flash`、运行时北京时间 21:44 的非高峰费率估算约 **US$0.001818504**。价格已核对 [DeepSeek 官方当前价格页](https://api-docs.deepseek.com/quick_start/pricing/)：每百万缓存命中 US$0.003、未命中 US$0.15、输出 US$0.6。该数为实报 token × 公布价格的估算，不是账户扣费凭证。
- 可复现 runner：`local-agent-web/eval/mcp-real-20261020/run.py`；脱敏结果与逐次用量：`local-agent-web/eval/mcp-real-20261020/discover-1792503877150832000/{submission,report,answer,tool_results,model_calls}.json`。这些是本地 gitignore 验收产物，未提交。

### 收到链接后的真实设计读取

用户提供 stage 项目链接（tid `02194bc6-fa68-4fcb-8bd9-ee7d2ab034aa`、pid `78ea765f-ea28-49f6-a806-9712adff83f2`）。使用同一 runner 的 `--case read --url '<链接>'`，凭据仅在内存读取，原设置库以 mode=ro 打开；仅自动批准本次授权的三个蓝湖工具，其他审批会停止验收。

- 首次业务运行 `read-1792504825953114000`：真实模型检索 → 两次审批 → `lanhu_design(list)` → `analyze` → `task_control` → **completed**，14.97 秒、4 次模型调用。项目「南岸区+生态环境监测」，47 份设计稿，第一份「专家档案」，750×1644，实际 HTML 包含 36px 标题和 PingFang 字体。list 仅收到 36 字符汇总；analyze 收到 43,474 字符。未缓存输入 26,795、缓存命中 22,528、输出 1,396（其中 reasoning 172）；估算 US$0.004924434。
- 为核对名称遗漏，再直接捕获一次真实 list 的原始 SDK 结果，保存脱敏 `raw-list-response.json`：`content` 只有一句汇总，`structuredContent.designs` 实际有 47 份名称与元数据，紧凑 JSON 56,996 字符。不是蓝湖没返回，是本项目漏传。
- 修复后业务运行 `read-1792505100424844800`：真实模型读到完整名称清单（57,033 字符、未截断），按名称分析「专家档案」，最终 **completed**，27.37 秒、6 次模型调用。模型首次将 `layer_depth` 写为字符串 `"1"`，服务端返回 `isError=true`；模型读取原始错误后改成数字 1，第二次成功。未缓存输入 59,149、缓存命中 144,384、输出 3,730（其中 reasoning 856）；估算 US$0.011543502。
- 两处修复全部落地后的最终业务运行 `read-1792505273617333700`：检索 → 两次审批 → list → 按名称 analyze → `task_control` → **completed**；18.56 秒，5 次模型调用，两次业务调用均 `is_error=false`、图记录 `ok`，无错误事件。未缓存输入 57,963、缓存命中 105,856、输出 1,745（其中 reasoning 368），估算 US$0.010059018。list 保留完整 47 份名称，analyze 仍受统一上限截断；模型根据真实文本给出 36px 标题和底部圆角按钮样式。源码最终版本已实际复验，非仅修复前证据。
- 第二次 analyze 的文本 43,474 字符、结构化 JSON 47,639 字符，共 91,114 字符（含分隔换行），经过 60,000 字符统一上限返回 60,027 字符（含截断提示）。已证明保护生效；不能声称完整结构化结果全部进入模型。文本与结构化内容部分重叠，仍有上下文成本。
- 两次业务运行响应模型名均为 `deepseek-flash`。费用按本节已核对的非高峰费率估算，不是实扣账单。逐次用量和结果在对应目录 `{report,answer,tool_results,model_calls}.json`；`answer.json` 是模型原文，出现“未联网”表示未调用网络搜索工具，MCP 本身实际访问了蓝湖服务。

### 真实运行发现并修复的缺陷

**【必须改】已修复**

1. `local-agent-web/src/local_agent/mcp/client.py` 的 `_outcome` 仅在没有文本内容时保留 `structuredContent` → 设计稿名称清单被丢弃 → 文本和结构化数据同时传递，使用紧凑 JSON，并对相同 JSON 及 SDK 的单 `result` 包装去重；两部分共用长度上限。
2. `local-agent-web/src/local_agent/tools/mcp_tools.py` 的 `_call` 将 `is_error` 转成普通字符串 → 图记录错误调用为 `ok`，页面与机械结果失真 → 抛出 `ToolException`，沿现有执行节点错误通道记录 `error`，保留原服务端错误供模型修正。该问题由第二次真实运行的错误参数触发确认，随后修复并补真实 stdio 整图错误用例。

**【建议改】** 本次未增加额外产品行为；无独立建议项。

**【不用管】** 模型把数字写成字符串属于该次模型参数错误，schema 已正确说明类型；服务端拒绝、模型修正，未放宽输入类型。

**存疑 / 未覆盖**：本链接无 docId，未执行 `lanhu_page`；未遍历其他 46 份设计稿、未下载图片和切图。未替换已打包 EXE；以上真实验证使用当前源码。

本轮改动范围：`client.py`、`mcp_tools.py`、`tests/test_mcp_client.py`、`tests/test_mcp_graph_e2e.py` 与本实施记录；本地验收 runner 加入原始文本/结构化长度及实际响应模型名记录。最终专项回归 **53 passed**，涉及四文件 Ruff 与两个源文件 mypy 均通过；未重复全仓测试与前端构建。复现回归：

```powershell
# local-agent-web 目录；每次换未使用的 basetemp 名
.\.venv\Scripts\python.exe -m pytest tests/test_mcp_client.py tests/test_mcp_graph_e2e.py tests/test_mcp_manager.py tests/test_mcp_registry.py tests/test_mcp_binding.py --basetemp=data/pytest-mcp-real-results -q
.\.venv\Scripts\python.exe -m ruff check src/local_agent/mcp/client.py src/local_agent/tools/mcp_tools.py tests/test_mcp_client.py tests/test_mcp_graph_e2e.py
.\.venv\Scripts\python.exe -m mypy src/local_agent/mcp/client.py src/local_agent/tools/mcp_tools.py
```

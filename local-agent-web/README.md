# local-agent-web

本机运行的学习型网页 Agent：浏览器里自然聊天、读写本地代码、执行受控命令。
技术栈 Python + Streamlit + LangGraph + LangChain Core。

- 架构契约：[../ARCHITECTURE.md](../ARCHITECTURE.md)
- 实施计划与验收标准：[../DEVELOPMENT_PLAN.md](../DEVELOPMENT_PLAN.md)

> 这是学习项目，不是商用成品。默认只监听本机回环地址，不包含账号体系、多人并发或公网部署。

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| v0.1a / T01 | 项目骨架、只监听本机的网页壳、模型配置检查 | 已完成 |
| v0.1a / T02 | 配置文件与 `.env`、模型工厂、最小闲聊子图 | 已完成 |
| v0.1a / T03 | SQLite Checkpoint 会话恢复 | 已完成 |
| v0.1a / T04 | AGENTS 规则加载、系统提示组装、闲聊/工具模式路由 | 已完成 |
| v0.1a / T05 | 只读工具闭环（`list_dir`、`read_file`）+ 路径边界 + 机械证据 | 已完成 |
| v0.1a / T06 | `edit_file`（精确串替换 + diff）、`write_file`（只建新文件） | 已完成 |
| v0.1a / T07 | 安全 Shell（`run_shell`，结构化白名单） | 已完成 |
| v0.1b / T08 | 追加 reducer 去重、事件幂等、完整机械证据 | 已完成 |
| v0.1b / T09 | 模型能力探测与结构化输出降级链 | 已完成 |
| v0.1b / T10 | 三出口人工审批（批准 / 修改 / 拒绝）+ 动作指纹 | 已完成 |
| v0.1b / T11 | 会话索引与侧栏（新建 / 切换 / 恢复 / 重命名） | 已完成 |
| v0.1b / T12 | 跨平台 Shell（argv 直执行 / cmd.exe / pwsh / sh）+ 阶段回归 | 已完成 |
| v0.2 及以后 | 三层记忆与压缩、完整反思、代码 RAG | 未开始 |

**现在能做什么**：本机网页里和模型多轮闲聊；刷新/重启后会话仍在，可新建、切换、重命名会话；加载 AGENTS 规则；列目录、读文件、精确串替换改文件、新建文件、执行安全命令（白名单内免审直执行，其余批准后执行，含管道/重定向的走平台 Shell 适配器）；删除、覆盖写、大范围编辑、白名单外命令与危险命令会**暂停等待你在页面上批准 / 修改参数 / 拒绝**。
**现在不能做什么**：工作区外的路径、密钥文件、未获批准的命令一律拒绝；没有三层记忆与代码检索（v0.2/v0.3）、没有会话删除或归档、没有公网部署与账号体系。

## 环境要求

- Python 3.10 – 3.13
- [uv](https://docs.astral.sh/uv/)（项目依赖与虚拟环境的唯一管理工具）

## 安装

在项目目录内执行：

```powershell
uv lock          # 按 pyproject.toml 生成/更新 uv.lock
uv lock --check  # 校验 uv.lock 与 pyproject.toml 一致
uv sync          # 按 uv.lock 创建 .venv 并安装依赖（含 dev 组）
```

`uv.lock` 必须提交到版本库：它是“换台机器能装出同样环境”的唯一依据。

## 配置

两个文件，都已被 `.gitignore` 排除，不会提交：

| 文件 | 内容 |
|---|---|
| `config.yaml` | 非秘密配置；可复制 `config.yaml.example` 后修改 |
| `.env` | API Key；可复制 `.env.example` 后填入，变量名要与 `config.yaml` 的 `llm.api_key_env` 一致 |

默认对接 DeepSeek（`base_url: https://api.deepseek.com`）。该端点上实际可用的模型名以
`curl -H "Authorization: Bearer $KEY" https://api.deepseek.com/models` 返回为准，
当前为 `deepseek-flash` 与 `deepseek-v4-pro`。

`config.yaml` 的关键字段：

```yaml
llm:
  base_url: "https://api.deepseek.com"
  api_key_env: "DEEPSEEK_API_KEY"   # 只写变量名，Key 的值放 .env
  model: "deepseek-flash"
```

配置缺失、字段拼错、YAML 语法错误、环境变量没设，都会在页面上给出具体原因；
`security.require_approval` 的四项不允许关闭，写成 `false` 会直接校验失败。

`security.shell` 决定命令怎么判定与执行（架构 §6.6）：

```yaml
security:
  shell:
    platform: "auto"            # auto / windows / posix
    executor: "argv_preferred"  # 也可显式写 cmd / pwsh / sh，指定 Shell 语法命令用哪个适配器
    default_timeout_seconds: 60
    max_output_chars: 30000
    whitelist:                  # 免审条目：平台 + 可执行名 + 参数完全一致（可选 cwd 限定目录）
      - platform: ["windows", "posix"]
        executable: "python"
        args: ["--version"]
        match: "exact"
    blocked_patterns: ["rm -rf", "del /s", "format ", "shutdown", "chmod", "curl ", "wget "]
```

## 项目规则（AGENTS.md）

每次提问都会扫描规则文件并注入系统提示（架构 §7.1）：

- 从当前目录向上扫描到最近的 `.git`，但**绝不越过 `workspace.root`**；
- 合并顺序为 Git 根 → 当前目录（越近的目录优先级越高，排在后面）；
- 同一目录存在 `AGENTS.override.md` 时覆盖该目录的 `AGENTS.md`；
- 单个文件超过 `agents.max_file_chars`、累计超过 `agents.max_total_chars` 时保留头尾并插入截断标记；
- `agents.enabled: false` 时完全不读取规则文件。

工作区之外的规则文件不会被读取（有测试覆盖）。

## 模式路由

每轮提问会先做一次意图分类（LLM 分类 + 关键词兜底）：

- 判为 `chat`：闲聊分支，**不绑定任何工具**给模型；
- 判为 `tool`：进入工具分支，模型可以提出工具调用。

## 工具

工具签名与架构 §6.1–6.6 一致：

| 工具 | 参数 | 本版本行为 |
|---|---|---|
| `list_dir` | `path="."`、`recursive=False`、`include_hidden=False`、`max_entries=200` | 只读，放行 |
| `read_file` | `path`、`start_line=None`、`end_line=None`、`max_chars=50000` | 只读，放行 |
| `edit_file` | `path`、`old_text`、`new_text`、`replace_all=False`、`expected_sha256=None`、`reason=""` | 免审上限内放行；超出改动行数上限进入人工审批 |
| `write_file` | `path`、`content`、`overwrite=False`、`reason=""` | 只允许创建新文件；覆盖已有文件进入人工审批 |
| `run_shell` | `command`、`cwd="."`、`timeout_seconds=None`、`env_keys=None` | 白名单内免审直执行；其余人工批准后执行，Shell 语法走平台适配器 |
| `delete_file` | `path`、`recursive=False`、`reason=""` | 人工批准后才执行；递归删除需二次确认 |

`run_shell` 的 `timeout_seconds` 省略（或传 `None`）时使用 `security.shell.default_timeout_seconds`
（出厂默认值 60）；显式值不能超过该配置。安全判定、动作指纹与工具执行取同一个有效值。

`edit_file` 的额外约束：必须先 `read_file` 拿到 `expected_sha256` 并在调用时原样带上；
`old_text` 必须唯一匹配（多处匹配要显式 `replace_all=true`）；不接受用整份文件内容做替换
（单行文件例外）；改动行数超过 `security.large_edit.max_changed_lines_without_approval`
时进入人工审批（批准后才写入）。写入用"同目录临时文件 + 原子替换"，改完重新计算 Hash，并把 unified diff
作为审计产物返回。

一次工具调用的完整链路：

```text
模型提出 tool_calls → safety_gate_node 判放行（未放行的当场回填拒绝原因）
                   → tool_execution_node 执行 → ToolMessage 回填给模型
                   → reflection_node 机械校验（状态、是否有输出）→ 记录证据
                   → completion_router 决定继续 / 结束（超上限强制结束）
```

保证：

- **工作区边界**：路径解析（含 `..`、`~`、符号链接）后仍必须在 `workspace.root` 内，否则拒绝；
- **敏感文件**：`.env`、私钥等一律拒绝读取（`security.secrets.block_known_secret_files`）；
- **二进制**：只返回类型说明，不把内容注入提示；
- **截断**：单文件大小、返回字符数与目录条数都有上限，超出会标注；
- **失败不隐藏**：工具报错会作为 `ToolMessage` 回填给模型，同时记入工具事件；
- **写入前校验版本**：改文件必须带 `expected_sha256`，与服务端当前 Hash 不一致就拒绝；
- **原子写入**：先写同目录临时文件再替换，避免写到一半留下损坏文件；
- **不伪造证据**：`final_evidence` 只来自真实执行的工具，`verified` 由机械校验决定；
  编辑类还要额外通过"Hash 确实变了""diff 非空"两项检查才算 `verified`。

## 启动

```powershell
python -m streamlit run src/local_agent/web.py --server.address=127.0.0.1 --server.port=8501
```

浏览器打开 <http://127.0.0.1:8501>。`.streamlit/config.toml` 已把地址与端口固定为
`127.0.0.1:8501`，即使用户忘记加命令行参数也不会把服务暴露到局域网。

必须**在项目根目录启动**：`config.yaml`、`.env` 与 `workspace.root` 都按进程当前目录解析。

### 本机环境备忘（Windows）

- `uv` 装在 `E:\tools\uv\uv.exe`；若终端 PATH 还没生效，用全路径调用 `E:\tools\uv\uv.exe`。
- 本机 C 盘空间紧张，uv 缓存与临时目录已指向 E 盘。在**新开的**终端里已自动生效；
  旧终端需要手动设置：
  ```powershell
  $env:UV_CACHE_DIR='E:\uv-cache'; $env:TEMP='E:\tmp'; $env:TMP='E:\tmp'
  ```
- 已避免使用系统环境变量存放密钥：Key 只从项目根目录的 `.env` 读取。

## 质量命令

```powershell
python -m pytest
python -m ruff check .
python -m mypy src
```

运行前请先 `uv sync`，或使用 `uv run python -m pytest` 等命令。

## 已知限制与安全边界

- 只监听 `127.0.0.1`。本地网页不是安全边界，将来若开放远程访问，必须另行设计身份认证与隔离。
- 网页只是客户端：工作区、文件读写和命令执行都发生在运行 Streamlit 的本机 Python 进程中。
- Shell 判定基于**解析后的命令结构**，不是字符串包含：
  - `security.shell.whitelist` 命中（平台 + 归一化可执行名 + 参数完全一致 + 可选 `cwd`）的命令
    免审直执行（`shell=False` + argv 数组）；
  - 其余命令、带路径的可执行文件、命中 `blocked_patterns` 的破坏性命令一律需要人工审批，
    批准后才执行；
  - 含 Shell 语法（管道、重定向、命令替换、后台执行）的命令由平台适配器执行：Windows 默认
    `cmd.exe /d /s /c`、POSIX 默认 `/bin/sh -c`，可用 `security.shell.executor: pwsh` 改为
    `pwsh -NoProfile -NonInteractive -Command`；适配器与运行平台不匹配时直接拒绝；
  - 大小写与 `.exe` / `.cmd` 后缀会被归一化，但**别名不共享免审资格**：`python3`、`py` 不等于 `python`。
- 跨平台覆盖：Windows 与 POSIX 两套解析规则都有单元测试；**真执行只在本机平台验证过**
  （自动测试在当前平台真跑一条管道命令，另一平台只做解析断言）。
- 需要审批的动作在网页上暂停：批准按**动作指纹**执行前复核，目标变化则失效；拒绝按指纹匹配，
  换一个调用 ID 也绕不过；递归删除必须二次确认，刷新不会沿用第一次确认。
- 页面审批控件与会话侧栏都有自动化测试（AppTest），仍建议人工点一遍。
- 编辑不做过行尾自动转换：文件是 CRLF 时，`old_text` 必须照原样带上 `\r\n`，否则匹配不上。
- 同一输入不会因页面重绘而重复执行工具；单轮的工具循环上限由 `agent.max_tool_loops` 兜底。
- 敏感文件一律拒绝读取，**包括 `.env.example`**（模式 `.env.*`）；如希望放行示例文件，需要调整 `security.secrets` 相关策略。
- 会话索引在 `data/checkpoints.sqlite` 的 `sessions` 表：同一工作区至多一个“当前会话”，
  刷新与重启都会回到它；第一个会话沿用 v0.1a 的 `thread_id = local-default`，升级不会丢历史。
  没有会话删除/归档，也没有"多个浏览器标签各自独立选会话"（本地单用户场景）。
- 会话数据存在 `data/checkpoints.sqlite`（首次启动自动创建，已被 `.gitignore` 排除）。
  SQLite 是单文件数据库，不适合高并发写入，本机单用户足够。
- 模型能力（Tool Calling、结构化输出）在启动时探测；不支持 Tool Calling 的模型在工具模式下
  失败闭合，并给出明确提示。

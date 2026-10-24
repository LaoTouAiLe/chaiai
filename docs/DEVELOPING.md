# chaiai 使用与开发指南

当前功能、状态和下一步统一见 [项目总览](../README.md)。本文件只说明如何启动、配置、使用及开发，不维护一份重复的任务表。

> **路径基准**：本文件原先是 `local-agent-web/README.md`，2026-10-25 移到 `docs/`。
> 文中出现的 `frontend/`、`src/`、`tests/`、`scripts/`、`pyproject.toml` 等相对路径，
> **都是相对于仓库里的 `local-agent-web/` 目录**，不是相对于本文件。

大文件可用 `read_file(start_line=...)` 定点读，长单行按 `next_start_char` 加 `expected_sha256` 续读。
`grep` / `glob` 返回 `next_cursor` 时可继续分页；扫描/时间/单行预算支持按任务调整，跳过原因明确返回。
具体参数与验证边界见 [大文件验证记录](../docs/audits/2026-10-18-large-file-search.md)。

界面是 FastAPI + React；Python + LangGraph 执行业务。默认本机、单用户、单进程、单 Uvicorn worker；Streamlit 保留为调试入口。内部包名 `local-agent-web` / `local_agent` 未改名。

## 快速开始

所有 Python 命令在本 README 所在的 `local-agent-web/` 执行。

```powershell
uv sync
# 首次需要中期向量记忆或本地 ONNX：uv sync --extra memory
# 首次启动可不建 config.yaml/.env，在网页设置中添加模型连接。
# 需要自定义配置时才复制示例；已有 config.yaml 时不要覆盖：
# Copy-Item config.yaml.example config.yaml
cd frontend
npm ci
npm run build
cd ..
uv run python -m local_agent.api
```

浏览器打开 http://127.0.0.1:8000。前端构建后刷新；后端更新后重启服务。没有产物时根路径提供构建提示，API 仍可用。Python/Node/npm 版本要求以 `pyproject.toml` 与 `frontend/package.json` 为准，精确依赖由锁文件确定。

开发时另开终端，在 `frontend/` 执行 `npm run dev`，访问 http://127.0.0.1:5173；Vite 将 `/api` 代理到后端 :8000。生产运行只需要 Python 服务和前端产物，不启动 Node 服务。T40 旧 EXE 试用包已交付，不能作为 v1.0 新代码的分发验收。

v1.0 候选提供显式诊断：`.venv/Scripts/python.exe -m local_agent.api --diagnose`，
不启动服务或读取 Key；摘要检查 YAML、静态产物和已有 SQLite，不读取网页配置覆盖。
兼容检查：`.venv/Scripts/python.exe scripts/check_release_contracts.py --check`。
固定工程评测：`.venv/Scripts/python.exe scripts/run_release_evaluation.py`，只用测试替身。
升级/卸载/模型缓存见 [生命周期指南](../docs/release/LIFECYCLE.md)，
联网与本地原文边界见 [隐私和限制](../docs/release/PRIVACY_AND_LIMITS.md)，
演示与新机器记录见 [验收模板](../docs/release/DEMO_AND_ACCEPTANCE.md)。

## 浏览器与持久进程（2026-10-18）

2026-10-19：Windows x64/x86 命令已接入原生管道兼容组件，保留外部写入授权。
本机已验证 npm 安装脚本、esbuild 同步/异步构建、Vite 构建与持久服务。
正常更新后重启后端生效；无需安装原生编译器。构建、分发、测试和已知范围见
[Windows IPC 验收](../docs/audits/2026-10-19-windows-ipc.md)。

Agent 现在可以直接操作独立 Edge/Chrome 浏览器，并启动可持续输入、读取日志和停止的交互进程。
例如：“创建一个计数网页，启动服务，在浏览器填中文姓名并点击两次，截图验证；下一轮继续操作原页面。”
在能力面板可查看“操作浏览器”“交互进程与后台服务”；浏览器缺依赖时会说明安装方法。

Agent 的交互进程使用标准输入/输出，不提供 Vim 这类全屏终端仿真。正常回答结束保留服务，
取消任务、改变目录权限、关闭对应能力或正常关闭服务会清理。运行文件在工作区 `.agent-runtime/`
自忽略目录；浏览器使用独立登录状态，不借用个人浏览器配置。
公开文件下载可直接返回 HTTP 状态和 SHA256；系统与进程查询有无 WMI 的专用入口。
Shell 外部写入仍需目录授权；浏览器宿主与命令写沙箱的边界、验收和已知限制见
[实现记录](../docs/audits/2026-10-18-interactive-runtime.md)。

## 右栏受控终端（2026-10-23）

Windows 上打开右上角「工作面板」→「终端」→「新建」，核对工作目录、Shell 和权限后点击
「批准并启动」。首版使用系统 cmd 与 ConPTY；支持交互输入、Ctrl+C、调整尺寸和重连。
批准后手动命令不逐条审批，只读档保持只读，其余档仅允许写当前工作区与专用临时目录，
不会带入外部目录写授权；已有保护目录仍受限制。终端可读取本机文件和访问网络。

切换标签、关闭面板或刷新页面保留进程，回到终端标签会重连；「结束」回收整棵进程树。
权限变更、30 分钟无输入和服务退出也会结束终端。最多同时运行 4 个，输出仅在内存中保留
最近 2 MiB，不写数据库或模型记忆。服务重启不恢复终端。其他平台不提供无沙箱降级。
Agent 工具与无人值守审批保持原规则。[完整契约](../ARCHITECTURE.md#23-右栏受控终端2026-10-23-用户确认)。

## 配置与数据

独立文件读取、搜索和网页请求在同一批连续提交时可并行执行，工具结果仍按提交顺序展示。
写入、浏览器、持久进程和 Shell 保持顺序；配置了联网次数额度时仍逐次检查。
详见 [并行验证记录](../docs/audits/2026-10-18-tool-parallelism.md)。

- `config.yaml.example` 是完整字段参考；复制后的 `config.yaml` 与 `.env` 已被 Git 排除。
- `llm.api_key_env` 只填写变量名；密钥值放环境变量或 `.env`。`llm.context_window_tokens` 按实际连接填写，必须大于 `max_tokens`。
- 工作区是默认上下文、相对路径与检索根。会话绑定登记工作区；本机外部读取可用显式绝对路径，外部写目录由“设置 → 权限”控制。
- 聊天/Checkpoint、设置、长期 SQLite 和中期 Chroma 的实际路径按配置解析；不要删除正式库来复现测试。
- 打开设置、浏览地图和数据中台不会因为浏览操作新增模型调用；提交追问和显式 Embedding 测试会实际执行并可能产生费用。
- AGENTS 规则从工作区加载；本仓库当前没有项目级 `AGENTS.md`，协作约定仍以用户提供的协议为准。新增关键约定应补充独立项目规则，不能把历史开发计划当作模型指令。

联网搜索默认 `web_access.provider: auto`：配置了独立的 `TAVILY_API_KEY` 就使用 Tavily，否则使用 Bing HTML 兼容搜索。
Tavily 密钥从 [Tavily 控制台](https://app.tavily.com/) 注册获取，放入本目录 `.env` 的 `TAVILY_API_KEY=你的密钥` 后重启服务；它与模型密钥不同。
显式指定 `provider: tavily` 而未配置密钥会明确报错，不静默换服务。HTML 兼容搜索不支持精确时间筛选，且可能出现无关结果或站点限制。
页面按轮展示“搜索结果”“已读取”“回答引用”及实际来源；动态页面读取尚未实现，失败会交给模型说明。
本批实现、复验命令与真实问题结果见 [联网实施记录](../docs/ui-reference/web-search-implementation-20261015.md)。

## 页面操作

侧栏提供新建/选择会话、搜索、会话改名/归档/恢复与工作区改名/移除。会话归档保留历史数据；移除工作区登记不删除磁盘目录。正在运行或等待审批的任务继续绑定原会话，切到其他会话不会批准或取消它。

新会话在首次通过预检并开始执行时，以首个问题的第一行自动命名；后续提问不覆盖标题。每个工作区默认显示 5 条会话并保留当前选中项，点击“展示更多”展开，搜索覆盖全部会话。会话列表独立滚动，底部设置保持可见。错误提示可关闭并重新查看诊断，关闭状态在本次页面访问内保留，不删除历史记录；顶部信息与错误横幅也可关闭。

输入区的 Agent／闲聊切换直接作用于当前会话，从下一轮生效，保留草稿、历史与地图分支，不新建会话。运行、审批或结果待核对时不可切换。模型调用失败明确结束本轮，可检查连接后继续提问；若工具或持久化异常导致执行结果不确定，刷新仍保留核对入口，先查看本轮记录与实际文件/命令结果，勾选确认后点击“结束中断任务”。这个操作不会重跑工具，也不会撤销已执行的操作。

模型选择面板右上角可直接关闭，也可在面板内按 Escape；关闭后焦点回到模型入口，保留草稿与模型选择。在地图追问里按 Escape 先关闭模型面板，不会同时关闭外层追问抽屉。

上下文压缩按本轮模型连接中配置的上下文窗口自动触发：预计输入占用达到“窗口减去回答预留”的 80%（默认阈值）后处理可压缩历史。执行时输入区上方显示“正在压缩…”，成功后保留一行“上下文已自动压缩”，刷新也可恢复；失败给出错误提示。圆环按重新组装的上下文更新，节省后会下降，保留的摘要、近期消息和系统提示仍占空间，后续内容会继续增长。高级设置中的压缩参数目前只读。


对话与轨迹按轮展示模型实际返回的 Think、工具、计划、检索/记忆、审批和用量。隐藏或未返回的推理不补造。审批的批准、拒绝、修改参数及重启恢复由服务端契约决定，刷新不重新执行副作用。

### 会话地图

会话顶部打开“会话地图”：父节点在左，卡片向右展开；点击独立详情，搜索问答，拖空白平移、滚轮缩放，也可适应全图和定位当前分支。

“基于此轮继续提问”在地图右侧打开对话面板，直接提交并显示回答，地图随快照更新；关闭按钮或 Escape 返回地图，保留分支与草稿。“回到聊天位置”仍可显式跳转主对话。末端追问延续分支，中间追问新增子分支；上下文继承根到所选轮次，原后续路径保留。分支可以改名和调整显示顺序。文件与共享记忆不随路径回滚；缺历史检查点、运行/待审批时相应入口禁用。

当前不支持自由布局、改父关系、智能分类、历史重生成和分支删除。[功能与证据](../docs/ui-reference/CONVERSATION_MAP.md)。

### 数据中台

侧栏“数据中台”提供概览、中期记忆、长期事实、任务结论和 Token 用量。记忆可按工作区、状态、关键词筛选并分页查看；点击详情，关闭按钮或 Escape 返回。

用量可按工作区、模型、UTC 日期筛选；包括归档会话与各分支，共享调用去重。图表可点击进入对应日期/模型明细。未知不补零；上限或读取失败明确提示。返回保留会话、分支与未发送草稿，刷新页面不承诺保存草稿。

第一版只读，不含记忆写删、向量库重建和费用换算。真实存储/浏览器已验证；大规模性能、实体触屏和本轮真实缓存 A/B 未验。[统计口径与证据](../docs/ui-reference/data-center-20261007.md)。

## 网页设置

侧栏“设置”包含模型连接、权限、通用与外观、任务执行、自动代码检索、长期记忆、中期记忆、向量库与 Embedding 和高级配置。
通用与外观支持深浅/系统主题、文字大小、减少动画、发送快捷键和 Think 默认展开，保存后立即生效。
执行、检索和记忆参数初值来自实际加载的 `config.yaml`；保存写入本地设置库，不修改 YAML，
从下一次新任务生效，运行中或等待审批的任务继续使用提交时的配置，重启后恢复也一样。
表单支持取消修改、恢复初始值（需再保存）；其他页面已保存新版本时会拒绝覆盖并保留草稿。
每次打开设置都会读取当前保存值；刷新共享缓存不会替换正在编辑的草稿及其基准版本。
自动代码检索参数只控制工具任务开始前的检索，不控制模型主动调用的 `grep` / `glob`。
记忆阈值接受 0–1 范围内的小数，不限于两位小数。
中期记忆可修改开关、召回条数、相似度阈值与时间衰减半衰期；启用前需配置 Embedding 并具备可用依赖。
“向量库与 Embedding”可配置本地 ONNX 或兼容 API 的模型、缓存目录、离线模式、端点与密钥环境变量名。
本地模型已导入 HuggingFace 缓存路径后，更换缓存目录需保存并重启后端；页面显示“需重启后端”，重启前测试不加载新目录。重启后请再次测试，静态准备状态不证明权重已缓存。
兼容 API 发送原文字符串数组并请求 float 向量，不按 OpenAI 模型窗口隐式切分；超长输入由供应商明确拒绝，不静默截断。测试失败显示安全分类及 HTTP 状态，区分请求格式/长度、鉴权、模型/端点、网络、超时与限流，不返回供应商原始报文。
向量库目录和集合名称只读展示，不从页面迁移、重建或清空。已有集合会校验模型/端点身份，实际运行和显式测试还校验向量维度及距离度量。
打开设置与保存不会加载模型或调用 Embedding 接口；“测试当前配置”会实际生成一条固定文本的向量，可能加载/下载本地模型或产生少量 API 费用，不保存草稿、不写入记忆。
远端密钥沿用本机 `.env` 或环境变量方式，页面只填写变量名、显示是否可用；修改 `.env` 后需重启后端。准备状态只验证静态条件，实际缓存及接口需通过测试确认。
高级配置只读展示压缩、历史保留与 AGENTS 规则，不开放修改。检索/记忆参数改变模型输入，缓存率需单独实测。

[设置技术与质量验收](../docs/ui-reference/MEMORY_SETTINGS.md)。页面交付不等于召回质量遗留已解决。

## 安全与运行边界

- 默认只监听回环，单进程/单 worker；不提供公网与多用户。v0.6 起可把一次提交交给本机后台任务执行
  （`jobs.background`，**默认关闭**，见 [架构 §17.2](../ARCHITECTURE.md)），并提供**取消**
  （`POST /api/sessions/{id}/cancel`，见 [架构 §17.4](../ARCHITECTURE.md)）：取消只中止当前会话在跑的
  那一个任务、**不撤销已经发生的副作用**，已经启动的命令按平台策略**终止整棵进程树**。
  运行中点击“停止生成”可中断生产模型的异步流等待，保留部分回答并标“已停止”；关闭页面增量展示也可停止。
  停止请求失败显示可关闭提示，并可刷新状态重试；取消收敛后会话回到空闲，轮次与地图保留 `cancelled`。
  非流式降级按调用边界停止，仅同步流适配器按块边界停止，进程内工具不硬杀。
  同一会话的第二个提交仍然返回 `SESSION_BUSY`（不排队）。服务重启后遗留的任务会被分成
  "可恢复 / 失败 / 需要人工核对"三类收口，**不会自动重跑**（[架构 §17.5](../ARCHITECTURE.md)）。
  任务与它们到哪一步了在页面「任务中心」里看（[架构 §17.6](../ARCHITECTURE.md)）。
- 权限采用 **read-only / workspace-write / danger-full-access** 三档（出厂默认 `workspace-write`），回答的是
  "能写到哪"：三档均允许显式本机外部路径的读取、列目录与搜索；只读档连工作区都不许写，其余档位允许工作区写入。
  外部写入使用逐目录 read_write 授权（含子目录、可撤销；撤销后仍可读），Windows 命令内部写入也受该边界约束。
  旧的 `deny` / `whitelist` / `allow` 取值会自动归一；旧 read 记录保留兼容，不再决定读取权限。
  搜索默认仍在工作区，外部须显式指定本机绝对路径，结果保留完整路径；不沿目录链接扩展搜索，敏感文件仍受限。
- 文件读取、目录枚举、搜索内容读取、新建、编辑和删除共用本轮 `SecurityPolicy.files` 后端。
  它在可信服务进程内检查路径、档位和批准；实际变更前复核目标，原子替换前复核版本。
  只读档拒绝检索 `save_full` 落盘；递归删除会先检查全部子目标，保护目录不能靠批准父目录连带删除。
  这是与 Shell 共用策略的文件后端，不新增 OS 级读隔离，也没有把所有文件操作搬进受限子进程。
- 联网检索与抓取是**常态能力**：出厂即开、不可关闭，闲聊与 Agent 两种模式都可用。安全边界不靠开关，
  而靠 SSRF 校验（每个解析出的地址都查、重定向前后都校验）与"外部内容一律按不可信数据处理"。
  档位同时决定**常规 Shell 命令要不要逐条批准**：常规命令免批准，破坏性命令与越界请求仍要人工批准；
  工作区外绝对路径的 `run_shell` 工作目录免审批，外部写入仍需授权。
- Windows 各档位均约束命令写入：只读档不授予写权限，其余档位可写工作区、专属临时目录与显式读写授权目录，保护凭据子树。
  非 Windows **照常运行**，只是没有 OS 级写边界——界面与工具结果会如实说明这一点。
  只读档只补读取 ACL，不授予写 SID。普通目录无法枚举会持续提示保护范围未核实；受保护子树的拒绝项
  建立失败则阻止命令执行。首次扫描后新建的凭据子树不实时受保护，需下次进程启动扫描。
- 工作区文件工具删除、覆盖、编辑默认免审批；只读档直接拒绝变更。
  Shell 中已识别的破坏性命令需要批准（`blocked_patterns` 只能追加），但命令模式不能完整识别任意脚本内部的删除或覆盖。
  工作区可写意味着脚本可以在沙箱允许范围内修改文件；受保护目标和敏感文件不因文件工具审批放行。
- 工具执行、作用域、安全判据和恢复真源在服务端；浏览器不直接操作文件或解释 Graph State。
- 主循环、默认命令时长和联网次数默认不限；主模型自行声明继续、完成或需要输入。完整裁定、修改文件和真实验证见[执行限制调整](../docs/audits/2026-10-18-autonomy-policy.md)。
- 记忆写入经过候选/作用域审核，普通聊天不自动写入。具体规则、配置和 API 见 [架构](../ARCHITECTURE.md) 与生成 `docs/api/openapi.json`；不能从历史示例反推当前默认值。

## 质量命令与调试

### 数据库自检、备份与恢复（v0.9 T79）

聊天库启动会在迁移之前执行 `quick_check`；失败就停止，并提示保留原库及 `-wal`/`-shm`、从
已验证备份恢复到新路径。不要删除损坏库后建空库。聊天库实际已由 SQLite Saver 开启 WAL，本轮
保留这一模式；**不要只复制运行中的主 `.sqlite` 文件**，已提交内容可能还在 WAL 中。

先停止本项目服务，再运行以下命令（在 `local-agent-web` 下；路径按实际配置修改，输出用新名字）：

```powershell
.venv/Scripts/python.exe scripts/database_maintenance.py check --database data/checkpoints.sqlite
.venv/Scripts/python.exe scripts/database_maintenance.py backup --database data/checkpoints.sqlite --output backups/checkpoints-20261012.sqlite
.venv/Scripts/python.exe scripts/database_maintenance.py restore --backup backups/checkpoints-20261012.sqlite --output data/checkpoints-restored.sqlite
.venv/Scripts/python.exe scripts/database_maintenance.py check --database data/checkpoints-restored.sqlite
```

确认恢复副本自检通过后，将 `config.yaml` 的 `memory.short_term.path` 改为
`data/checkpoints-restored.sqlite`，按原启动命令启动，核对历史并继续一轮会话。原库和备份保留，
不直接覆盖原库；已有目标路径会被拒绝。恢复出的未结束任务仍需人工核对，不自动重跑副作用。
Windows 本机已通过真实 CLI 备份、恢复、读取历史与继续会话测试。

脚本使用 SQLite 一致备份接口，包含已提交 WAL 数据，产出自检通过的独立单文件；它不读取 `.env`
或凭据，也不生成 ZIP。可以逐个备份 `model_settings.path` 和 `memory.long_term.path` 指定的 SQLite
文件，但**必须先手动停止服务**：CLI 能通过聊天库的 API 锁识别服务占用，不能自动识别设置/长期
记忆库是否正在使用。单个 SQLite 快照不包含工作区文件、Chroma、配置或 OS 密钥；换机器需重新
配置密钥，多个库也不承诺同时快照。文件系统不支持硬链接发布时明确失败，不覆盖已有文件。

### 本机平台能力（v0.9 T82）

| 能力 | 本机 Windows | Linux / macOS 与边界 |
| --- | --- | --- |
| 自建子进程统一登记、超时/取消 | run_shell、git、rg、目录选择器已有真实回归 | POSIX killpg 已实现，本机未验证；第三方 SDK 自建进程不在登记范围 |
| 路径与授权 | 通用边界、大小写、尾点空格、设备/UNC 拒绝已测 | 非 Windows 路径语义未跨平台验证；symlink 依赖权限，8.3 缺短名时跳过 |
| Shell 环境收敛、单次输出/超时上限 | 已实现并有回归 | 平台适配未跨平台验；git/目录选择器仍继承环境 |
| SQLite/崩溃恢复 | 自检、WAL 一致快照、新路径恢复与故障核对有实跑 | SQLite API 跨平台，恢复发布依赖硬链接；未跨平台、未断电实测 |
| 脱敏 | 页面、HTTP、SSE、日志有负例验证 | 本机可信数据库/检索文件允许原文，不保证过滤未知密钥 |
| OS 凭据 | 已有人工实测；本轮默认跳过真机写入 | 非 Windows 持久凭据未验证；内存凭据不能跨重启 |
| CPU/内存/进程数、网络出口、工作区总磁盘 | 未限制 | T78 已撤销；输出上限不是磁盘配额 |
| OS 安全沙箱、文件系统强隔离 | **Windows 各档位均约束写入**：真实登录 SID + 令牌 DefaultDacl + 逐可写根 v3 SID；可写档位的范围含工作区、专属临时目录和显式读写授权目录，保护凭据子树（物化时已存在的那些）。根上一条可继承 ACE，新目录立即继承可写；runner 以 Python 隔离模式启动，正常返回可保留后台服务，异常退出仍由 Job 清理。本机 ctypes/ssl/hashlib 导入已通过；读侧/网络未隔离，Everyone/登录 SID 既有权限、硬链接及并发改名仍有边界 | 未做：没有 Landlock 或容器；非 Windows **照常运行但没有 OS 级写边界**（界面与工具结果如实说明） |

详细证据、隔离方案评估与重开条件见 [架构 §20.7](../ARCHITECTURE.md#207-平台能力矩阵与阶段门t822026-10-12-冻结)。

```powershell
uv lock --check
uv run pytest -q -p no:cacheprovider
uv run ruff check src tests
uv run mypy src
uv run python scripts/export_openapi.py --check
```

受限环境的 pytest `--basetemp` 使用本次可写、仓库外的新目录；不要复用真实数据。默认自动测试不依赖密钥或网络，真实供应商案例必须另用隔离库并标记费用及测量边界。

可选 Streamlit：`uv run streamlit run src/local_agent/web.py`。用于调试/对照，朋友试用不需要。提交规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

<a id="frontend"></a>

## 前端开发、契约与视觉参考

本节 npm 命令和前端源码路径以 `frontend/` 为根；Python 导出 OpenAPI 的命令从 `local-agent-web/` 执行。静态原型只用于参考，不能当真实操作验收。当前界面功能见本文件前文。

### 前端命令

```text
npm ci                 # 按 package-lock.json 安装（.npmrc 已固定 legacy-peer-deps，见下）
npm run typecheck      # tsc --noEmit
npm run lint           # eslint
npm run test           # vitest run（单元测试）
npm run test:e2e       # playwright（先 build，再对 dist 截图比对）
npm run test:live      # 真 Python 服务 + 确定性假模型：两轮/刷新/模型选择
npm run test:live:streaming  # 真 SSE 增量、完成与刷新去重
npm run test:live:variants   # 长回答、空回答与模型异常
npm run build          # typecheck + vite build → dist/
npm run dev            # 开发服务器 127.0.0.1:5173，把 /api 代理到 127.0.0.1:8000
npm run generate:api   # 从受控 OpenAPI 生成 src/api/schema.gen.ts
npm run check:api      # 漂移检查：重新生成后必须与已入库的生成文件逐字节相同
```

### 传输类型与受控 OpenAPI（T29）

前端不手写接口形状，只从**受控 OpenAPI** 生成：

```text
后端契约（FastAPI 路由 + 应用层 DTO）
  → uv run python scripts/export_openapi.py        # 落盘 docs/api/openapi.json（入库，唯一来源）
  → npm run generate:api                           # 生成 src/api/schema.gen.ts（不得手改）
  → npm run check:api                              # 重新生成后逐字节比对；检查不留改动
```

- 改契约的顺序：改后端 → 重新导出 → `npm run generate:api`；两处都有漂移检查
  （后端 `pytest tests/test_api.py` 断言"落盘文档 == 真实应用"，前端 `npm run check:api`）。
- 生成文件只描述**传输**形状（路径、请求体、响应体、错误信封 `{"error": …}`）；业务语义仍以
  `ARCHITECTURE.md` §15 为准。真实页面经 `features/*/adapters.ts` 转成展示模型，`src/fixtures/`
  只给视觉原型使用；两者都不直接读取 Graph State（§6.1）。
- `.npmrc` 里的 `legacy-peer-deps=true` 是**有意**的：`openapi-typescript` 的 peer 只到 `typescript@^5.x`，
  而本项目用 TS 6。副作用见该文件注释——新增依赖后请顺手跑 `npm run typecheck`。

### 本机环境约定

- **Node / npm**：本机验证过 `node v22.20.0` + `npm 10.9.3`，`package.json` 的 `engines` 与之对齐；
  依赖的精确版本由 `package-lock.json` 锁定。
- **npm 缓存**：若遇到磁盘空间限制，安装时把缓存指到**你自己的可写目录**，否则会写系统盘：
  `$env:npm_config_cache='<数据盘>:\npm-cache'`（不写 `~/.npmrc`）。
  刻意**不给具体盘符**：这类值随机器而变，写死一个会让别人照抄到不存在的路径上（2026-10-16）。
- **本机的批量删除拦截**（环境问题，不是项目问题）：`npm ci` 会先删 `node_modules`，本机拦截一次超过 500 个
  文件的删除 → `npm ci` 中途失败并留下残缺依赖树。这是既有本机例外记录：曾使用 `npm install` 恢复残缺依赖。正常安装仍使用 `npm ci`，不能将该故障写成所有环境的要求。
- **Playwright 浏览器**：浏览器版本要与 `playwright.config.ts` 期望的一致（1.63 对应 Chromium **1243**）。
  浏览器目录**自己挑一个可写位置**；该路径放在**被 gitignore 的本机文件** `playwright.local.json` 里由
  `playwright.config.ts` 读取，因此配置本身保持可移植：

  ```json
  { "browsersPath": "<你的浏览器目录>" }
  ```

  环境变量 `PLAYWRIGHT_BROWSERS_PATH` 优先级最高，CI 可用它覆盖。首次准备：把同一变量指向目标目录后执行
  `npx playwright install chromium`。

### 原型宿主与场景

`src/app/App.tsx` 默认进入真实的 `LiveApp`；只有传 `?state=` 时才选择 fixture 场景，供视觉基线复验。
原型标识条已删除，fixture 按钮不能当作真实操作入口。

```text
/?state=shell             常驻会话壳：侧栏、会话头、对话流、计划条、工具行、统计行、上下文计量器
/?state=diff              早期检查点 1：双 hunk 完整文件 diff（含增删行与超长行）
/?state=errors            早期检查点 2：供应商错误态三种（鉴权失败 / 限流 / 超时重试耗尽）
/?state=tools             其余工具卡：读取窗口 / 搜索两种形状 / 终端非零退出 / 待办 / 未知工具回退
/?state=approval          审批接管输入区：动作、目标、影响、风险 + 一次性批准或拒绝
/?state=approval-expired  审批失效态：不再提供按钮，只说明未执行任何操作
```

```text
/?state=settings-models  设置页：连接六态（已验证/未验证/检测中/检测失败/能力不支持/密钥缺失）+ 删除确认 + 下一轮生效
/?state=settings-empty   设置页空态：还没有连接，且系统凭据存储不可用
/?state=model-menu       输入区模型菜单：切换只对下一次提交生效，正在运行的一轮固定原连接
```

检索与记忆（只给来源定位，不给正文 / 向量 / 评分）：

```text
/?state=memory-hit      命中：来源类型 + 安全引用 + 用途，并标明不展示什么
/?state=memory-miss     未命中 + 写入长期记忆失败（明说不编造出处）
/?state=memory-failed   检索失败：与"未命中"分开陈述
```

空 / 加载 / 禁用：

```text
/?state=empty-no-workspace  未选工作区：输入区禁用并用真实元素说明原因，侧栏不高亮
/?state=empty-session       空会话（已选工作区）：空态标题 + 可用输入区
/?state=running-empty       运行中且无内容：阶段态 + 可写草稿 + 主按钮变「停止生成」（§17.4）
/?state=loading-history     读取事件中：骨架只有形状、无文字
```

计量器与侧栏（矩阵 22/23/24）与服务端冲突码：

```text
/?state=meter-open         上下文计量器展开：精确读数 + 三态口径 + 窗口与缓存基线规则
/?state=sidebar-collapsed  侧栏收起：窄轨只保留品牌、新建、展开与设置，不渲染会话树
/?state=error-codes        SESSION_BUSY / IDEMPOTENCY_CONFLICT / RECOVERY_REQUIRED 三种可执行性
```

矩阵 22 的**悬停**态没有独立场景：它由 `context-meter-hover.png` 这张 e2e 证据图覆盖
（真实 `hover()` 触发 CSS 弹出，断言弹层完整进入视口后截图）。

页面不再显示「原型」标识条；正式视觉基线也不带它。

审批接管**只在 `pending` 时替换输入区**。一旦审批失效或已作出决定，输入框立即恢复，审批结果以卡片留在输入区上方——
一张让用户"重新发起请求"的卡片，不能同时成为他无法输入的原因（见 `src/components/Composer.test.tsx`）。

侧栏里**选中行的运行状态取自当前场景**，未选中行才用侧栏 fixture 自己的状态；每个会话行的状态同时有可见文本
与隐藏状态词（空闲 / 运行中 / 等待审批 / 已完成），选中行带 `aria-current`（见 `src/app/App.test.tsx`）。

fixture 与视图模型在 `src/fixtures/`。它们是**展示用视图模型**，不是传输 DTO，也不镜像 Graph State
（V0_4_DEVELOPMENT_PLAN §6.1）。

### 视觉基线（冻结参考）

- 截图由 `e2e/prototype.spec.ts` 生成，落在 `e2e/__screenshots__/{1440x900,1280x800}/`，
  固定视口、`deviceScaleFactor=1`、浅色主题、`zh-CN`、`Asia/Shanghai`、`reduced-motion`。
- 每次截图前都必须经过 `settle()`：等 `document.fonts.ready` 并等两帧 `requestAnimationFrame`。少了它，
  并行 worker 下截图可能落在字体就绪之前，同一个页面会产出两张不同图片（假回归）。
- 每个视口 33 张：22 个场景（`shell` / `diff` / `errors` / `tools` / `trace` / `long-text` / `approval` /
  `approval-expired` / `settings-models` / `settings-empty` / `model-menu` / `memory-hit` / `memory-miss` /
  `memory-failed` / `empty-no-workspace` / `empty-session` / `running-empty` / `loading-history` /
  `meter-open` / `sidebar-collapsed` / `error-codes` / `capabilities`）各一张，外加 11 张**证据图**：
  滚动证据（`diff-bottom` / `errors-bottom` / `error-codes-bottom` / `tools-read` / `tools-bottom` /
  `settings-models-mid` / `settings-models-confirm` / `capabilities-bottom`）、轨迹检查器
  （`trace-inspector` / `trace-inspector-bottom`）与悬停证据 `context-meter-hover`。三视口合计 **97** 张
  （`640x800` 少 2 张：`diff-bottom` / `tools-bottom` 在窄窗下元素本身就高于视口，按用例声明 skip）。
- 滚动证据用 `expect(...).toBeInViewport({ ratio: 1 })` 断言被折叠在首屏之下的内容在滚动后**完整进入视口**：
  第二个 hunk 末行、第三张错误卡的完整提示与"重新发送"按钮、读取卡的窗口与行号、尾部的待办/通用回退/成功终端卡、
  连接列表的中段（"检测中"+ 明文传输标记 / "检测失败" / "能力不支持"，把第三张卡对齐到滚动容器顶部后三张同时完整可见）、
  末张连接卡的删除确认按钮。因此"需要滚动"不等于"不可读/不可操作"，而"元素存在"也不等于"可以验收"。
- 截图比较是**严格**的（`maxDiffPixelRatio: 0`）。之前允许 0.002 的比例，在 1280×800 下放过约 2000 个像素的差异，
  于是输入区 chip 还写着旧模型的过期基线也能"通过"——`52/52 passed` 单独并不证明基线是最新的。
- 有意改动视觉后重采：`npx playwright test -g "<测试名>" --update-snapshots=all`。`=all` 会把**当前仍能通过**的
  基线也一并重写（默认的 `--update-snapshots` 只在差异超出容差时才写），因此要用 `-g` / `--project` 限定到确实改过的
  那几条测试，不要整目录刷。重采后**必须人工看新图**，不允许用批量更新掩盖回归（`V0_4_DEVELOPMENT_PLAN.md` §4.2）。
- 截图依赖本机字体（`Microsoft YaHei` 等），因此基线是**本机环境产物**；换机器需要重新确认。
- 跑 e2e 时控制台会出现 `vite http proxy error: /api/preferences`（`ECONNREFUSED`）：这是**测试环境**的现象——
  `vite preview` 是静态服务、没有后端，而页面最外层的 `PreferencesProvider` 会读一次偏好来设置
  `data-theme` / `data-font-size` / `data-motion`。真实产品由 Python 后端提供该端点；Playwright 又已用
  `colorScheme: 'light'` + `reducedMotion: 'reduce'` 固定了这些取值，因此**不影响像素与判定**，不需要为它改生产代码。
- 参考资产的对照方法见 [docs/ui-reference/README.md](../docs/ui-reference/README.md) 与
  [state-matrix.md](../docs/ui-reference/state-matrix.md)。



## 故障诊断

先看**服务端自己给的错误文本**：配置、端口、锁、静态产物这几类都是启动即失败并写明原因，不需要翻代码。

| 现象 | 常见原因 | 处理 |
|---|---|---|
| `uv run python -m local_agent.api` 直接拒绝启动 | `web.host` 被改成非回环地址，而 `web.allow_non_loopback` 仍为 `false` | 改回 `127.0.0.1`；确实要对外暴露时显式设 `web.allow_non_loopback: true`，并清楚 v0.4 **没有鉴权** |
| 启动报数据库锁被占 | 已经有同一个库的实例在跑（排他锁 `data/checkpoints.sqlite.api.lock`） | 用已在跑的那个进程，或先把它停掉——同一目录只能有一个 Python 服务（见「单进程、单 worker」） |
| 启动报“数据库自检失败” | 数据库损坏或无法完成检查，迁移尚未执行 | 停服务并保留原库及 WAL；按上面的自检/备份恢复步骤恢复到新路径，不删除原库或直接建空库 |
| 打开 `http://127.0.0.1:8000/` 只有一段提示 | 没有构建产物，服务只提供 API | `cd frontend; npm ci; npm run build`，然后刷新 |
| `npm run dev` 起不来（端口 5173） | Vite 配了 `strictPort` | 停掉占用 5173 的进程；不要靠改端口绕过，代理目标写死在 `vite.config.ts` |
| 页面出现「事件流提示：stream_idle_timeout / stream_overrun」 | 服务端主动结束了这条 SSE 连接（长时间无事件 / 客户端读得太慢） | 前端会自动重连并先取快照，**任务不会被取消**；偶发可忽略，持续出现看服务端日志 |
| 页面出现「事件流提示：snapshot_refresh_failed」 | 对账用的快照请求失败 | 切会话瞬间的取消是正常行为、不会再报；其他情况先确认后端进程还活着 |
| 启动即失败并点名某个配置键 | `config.yaml` 校验不通过 | 按提示补：`llm.context_window_tokens` **必填**；`security.require_approval` 四项不允许写 `false`；YAML 语法 / 字段拼错 / `api_key_env` 指的环境变量没设 |
| 中期记忆相关功能不可用 | 打开了 `memory.medium_term.enabled` 但没装可选项 | `uv sync --extra memory`；缺依赖给的是可读提示而不是 `ImportError`，embedding 缓存目录不可用时侧栏状态为 `not_ready` |
| `uv` / `npm` / `node` 提示"无法识别为 cmdlet" | 终端 PATH 没生效（shell 每次调用不共享环境） | 见上面的「本机环境备忘（Windows）」，或用全路径调用 |
| live 套件（`test:live*`）失败 | 缺 `uv`、并发太高、或本机没装 Chrome | 确认 `uv` 可用；`--workers=2`（8 会 OOM）；浏览器走本机 Chrome，不额外下载 headless shell |
| `npm run check:api` 失败 | `docs/api/openapi.json` 与后端代码漂移 | `uv run python scripts/export_openapi.py` 重新导出 → `npm run generate:api` → 再比对 |
| 报"数据库结构未就绪" | 库还没建或版本旧 | `uv run python scripts/init_db.py --check` 看原因，需要时跑一次 `scripts/init_db.py` |

旧 T01–T24 进度、底层实现展开及完整整理前 README 见 [阶段历史](../docs/archive/v0.1-v0.3-development.md#readme-snapshot)。

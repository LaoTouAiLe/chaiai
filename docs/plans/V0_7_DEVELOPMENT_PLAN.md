# v0.7 开发计划：复杂工作流、Subgraph 与受控子 Agent

> **状态**：2026-10-12 **T64–T70 已完成，v0.7 工程源码可交付，未提交 Git**。各任务均先维护执行/架构文档再编码；两题固定对照只证明离线编排优势，真实模型效果与回答语义审查未验收。
> **依据**：`docs/planning/FUTURE_WORK.md` §v0.7 的六项主要工作、五条验收与不做项。结构沿用 `V0_6_DEVELOPMENT_PLAN.md`。
> **前置**：用户已接受 v0.5/v0.6 工程源码交付；已确认的必须改项收口，1482 passed / 8 skipped / 0 failed，证据见 `docs/benchmarks/v07-readiness-fixes-20261011.md`。本阶段不重新审查旧版本。T43 真实压力验收仍待补，回答语义审查按用户要求暂缓，这两项不写成已通过。
> **效力**：本文件负责执行顺序。每个任务先更新 `ARCHITECTURE.md` §18 对应契约，再编码；未开工的小节不冻结字段、配置或接口。文档中的未来落点是计划，不是已实现行为。
> **授权与验证**：用户授权先写文档再开始编写。默认验证使用假模型、隔离工作区与临时数据库；不新增付费调用，不生成 ZIP，不自动提交 Git，不操作正式数据库。

## 1. 范围

### 1.1 做什么

1. 固定两道复杂任务及机械验收标准，记录当前自由工具循环基线。
2. 构建两个确定性 LangGraph Subgraph：代码修改闭环与只读调查闭环。
3. 工作流声明输入、输出、步骤、能力、资源上限与停止条件；只从显式登记的工作流中选择。
4. 对适合拆分的只读调查提供受控子 Agent，独立上下文、收窄权限、预算与结构化证据报告。
5. 约束总 Token、工具调用、循环次数、委派深度、并发数，失败或证据缺失不能伪装成功。
6. 复用 v0.6 取消、审批、任务索引和恢复；重启后识别已完成子任务，避免重复副作用。

### 1.2 这次不做

- 自治 Agent 社会、任意生成控制流、无限递归委派、默认并行执行所有工具。
- 分布式队列、远程 Worker、多进程部署、跨工作区自动扩权。
- 新建第二套工具执行、安全审批、用量、幂等或持久存储真源。
- v0.8 的实时交互深化、WebSocket、页面重写；仅接入本阶段必要的可见结果。
- 旧版本全面复审、T43 新的付费压力实验、回答语义质量审查、打包和发布。
- 修改无关代码、依赖升级；没有实际消费者的配置和扩展点。

### 1.3 阶段验收 → 任务落点

| 路线图验收 | 落点 |
| --- | --- |
| 两个 Subgraph 在固定复杂任务上优于自由循环并有对照指标 | T64 基线 + T65/T66 实现 + T70 对照 |
| 子 Agent 失败、超时、无证据时主任务不伪装成功 | T67 + T69 |
| 子 Agent 无法获得 Profile 未授予的工具和工作区 | T67 |
| 重启可恢复主任务，识别已完成子任务 | T68 |
| 深度、并发、Token、工具和循环都有硬上限 | T65/T67 + T69 |

## 2. 契约改动点（先改契约，再编码）

| 架构章节 | 改动内容与时机 |
| --- | --- |
| 新增 §18.1 | T64 固定任务、证据校验、基线报告与真假指标区分 |
| 新增 §18.2；§4/§5 | T65 工作流状态、固定边、选择与返回；不复制父图真源 |
| §6.9/§16.1 | T65/T66 如需模型可见入口，先声明能力归属与稳定 Schema；Profile 原有权限不自动扩大 |
| §8；§18.2 | T65 首个消费者落地时才添加有实际用途的预算配置，明确默认值与执行检查点 |
| 新增 §18.3；§10.6/§16.2 | T67 子 Agent 身份、权限交集、来源 `subagent` 与记忆排除门；生产者落地时同步聊天库 v9 CHECK |
| 新增 §18.4；§17 | T68 父子任务身份、Checkpoint 归属、恢复证明与不重复执行边界 |
| §15.2–§15.8 | T69 必要的应用服务、API、事件与结果投影；公共 DTO 变化按现有生成流程检查 |

同步维护根 `README.md` 当前入口；历史版本计划与历史失败证据保持原样。稳定契约只按开工任务分段冻结，不一次制造未来所有字段。

## 3. 任务拆分（T64–T70，按顺序、独立验收）

### T64｜固定复杂任务与自由循环基线

- **目标**：先有同题、同工作区种子、同 Profile 的可复跑基线，防止后续用不同任务宣称改进。
- **固定任务 A：代码修改闭环**。合成 Python 项目中，`clamp` 未执行边界裁剪；读取实现和测试、改实现、运行三个标准库 unittest 用例、读取修改后文件并报告结果。正确产物由代码校验；不能用一段“测试通过”文本冒充执行。
- **固定任务 B：只读调查闭环**。在三份合成文档中查明取消、恢复与权限的约定；报告必须带三个机械可核对的事实和对应文件行号。任何种子文件变动或新增文件均判失败。
- **落点**：新增 `local-agent-web/src/local_agent/workflow_eval.py`（固定数据、校验和现有图的离线跑器）、`scripts/run_workflow_baseline.py`（命令行）、`tests/test_workflow_eval.py`（正常、错误和边界）。不改生产 Graph、工具 Schema、API 或数据库。
- **指标**：任务指纹、Profile、运行模式、终态、校验失败原因、模型调用数、工具调用数、循环数、耗时。假模型不提供实际 Token/缓存指标时写 `null`，不能伪造供应商数据。
- **验收**：两道题在现有主循环里实际执行工具；A 产物正确且真实测试退出码为 0，B 所有文件哈希与清单不变且引用覆盖实际读取行区间；漏测试、假完成、失败工具、错误引用、只读写入均能被校验拒绝；重复执行任务指纹与操作计数一致。
- **解释边界**：脚本化模型只验证协议与机械产物，不证明真实模型回答质量或 Subgraph 优势。报告保留此边界。
- **状态（2026-10-11，已完成，未提交）**：已实现固定任务、离线跑器和机械校验；两题在当前主图完成，修改题 6 次模型 / 5 次工具 / 6 轮，只读题 4 / 3 / 4。新增 32 个测试实例（含两次真实图运行的一致性检查）全部通过，完整回归 **1514 passed / 8 skipped / 0 failed / 38 warnings，351.30 秒**；ruff 与 mypy（123 源文件）已通过。本任务不实现 Subgraph，不声明工作流优势。

### T65｜工作流契约、资源边界与只读调查 Subgraph

- **目标**：首个真实消费者落地：有明确输入与证据输出的只读工作流；固定步骤为定位、读取、证据核对、报告。
- **落点**：`graph/` 下工作流实现；复用现有能力注册表、工具节点、用量记录和安全门。具体字段和入口在 §18.2 冻结后实现。
- **验收**：输入非法、缺能力、无证据、预算耗尽均明确失败；仅使用父 Profile 已授予的只读能力；工具集在轮次内稳定；T64 任务 B 同题可跑；关闭或未选择工作流时自由循环行为保留。
- **不做**：由模型创建节点和边；为工作流另起工具执行链；现在实现子 Agent。
- **实现取法（2026-10-11）**：先实现程序驱动的原文证据整理，输入为相对 glob 与字面关键词，报告不作模型语义归纳；模型调用/Token 固定为 0。资源上限先覆盖实际工具、步骤、文件、证据与报告消费者；T66 有模型调用时再冻结 Token 总预算，不提前放无用配置。入口先在 RuntimeContext，HTTP/UI 在 T69。
- **状态（2026-10-11，已完成，未提交）**：§18.2 已先冻结契约；已实现注册的 LangGraph Subgraph、严格输入、资源预留、权限交集、原文/哈希证据与父图收口。新增 37 个实例与 T64/主循环/恢复相关回归合计 **155 passed，45.54 秒**；原图节点集合断言同步新节点后单独复跑通过。完整回归 **1551 passed / 8 skipped / 0 failed / 38 warnings，371.44 秒**；ruff、mypy（125 源文件）、OpenAPI 与前端类型漂移检查通过。

### T66｜代码修改 Subgraph

- **目标**：固定“理解→修改→测试→复盘”闭环；失败测试不进入成功终态。
- **落点**：复用 T65 的工作流入口与预算，在 `graph/` 实现第二条工作流。
- **验收**：T64 任务 A 同题可跑；写入与 Shell 仍经既有审批，拒绝后不执行；测试失败、未运行或输出缺失明确失败；取消后不启动后续工具；测试和产物双重校验成立才完成。
- **不做**：自动回滚用户文件、未经授权 Git 提交、自动安装依赖、无限“改到成功”。
- **范围冻结（2026-10-11）**：§18.2.1 已先记录首版单源码文件、显式测试清单/数量、固定 unittest 命令、一次模型精确替换、保守 Token 预留、复用父审批与双重产物验证。复杂多文件 Patch、其他测试框架、自动重试/回滚不在本任务。
- **状态（2026-10-11，已完成，未提交）**：已实现注册的 Subgraph、严格范围、单次模型提议、调用前 Token 预留、原父图审批与真实测试/复读校验。新增 60 个实例；与 T65/T64/图恢复/原路由回归合计 **305 passed，84.93 秒**。底层 SDK 重试已用离线 503 实际报文验证关闭，父模型设置不变。冻结后完整回归 **1611 passed / 8 skipped / 0 failed / 38 warnings，393.44 秒**；ruff、mypy（126 源文件）、OpenAPI 与前端类型一致性检查通过。

### T67｜受控只读子 Agent 与结构化报告

- **目标**：只对调查的独立证据收集允许委派，主 Agent 核对有界结果。
- **权限**：子能力必须是父有效能力的子集；工作区只能在父允许范围内收窄；子上下文不复制父全部历史或隐藏推理。
- **限制**：第一版禁止子 Agent 再委派；明确子任务数量与并发上限，全部子任务共享父总预算；具体数值随消费者冻结，不依赖提示词自觉遵守。
- **落点**：`graph/`、`capabilities.py`、`session_kind.py`、记忆来源门；需要持久字段时走同一迁移入口，不另建数据库。
- **验收**：越权工具与路径真实执行侧拒绝；无证据、超时、异常不升级为成功；报告体积受限，引用可回溯；子会话记忆不能自动注入；串行与允许的并发路径都受同一总额度约束。
- **不做**：子 Agent 写文件/Shell、自主递归、全工具默认并行、继承父审批作为自己的授权。
- **范围冻结（2026-10-11）**：§18.3 已先冻结一次模型检索计划 + T65 只读执行、1–3 子任务/最多 2 并发、目录与上下文隔离、总额度均分预留、超时收口、父原文/Hash 核对与 subagent 来源迁移。持久父子索引和重启恢复留给 T68。
- **状态（2026-10-11，已完成，未提交）**：隔离只读子计划/执行、总预算份额、串行/并发、超时取消、父证据校验与来源迁移均已实现；补齐 save_full 落盘拒绝。新增 78 个实例，相关回归 **516 passed / 2 skipped / 0 failed，171.18 秒**；最终冻结后全量 **1689 passed / 8 skipped / 0 failed / 40 warnings，718.16 秒**，退出码 0。ruff、mypy（128 文件）、OpenAPI 与前端类型漂移检查通过；持久父子关联及重启恢复留给 T68。

### T68｜父子任务持久化与重启恢复

- **目标**：与 v0.6 的恢复契约合并，恢复父任务时识别子任务已完成的证据。
- **落点**：现有 `persistence/migrations.py`、任务索引、Checkpoint、`application/recovery.py`；依据实际身份需求扩展，禁止第二套可写真源。
- **验收**：子任务完成后重启不重复执行；父任务等待审批沿既有入口恢复；副作用与检查点之间崩溃无法证明时进入人工核对；取消意图跨重启保留；旧库迁移和旧会话仍可读取。
- **不做**：自动重放不明副作用、恰好一次承诺、跨进程取消。
- **状态（2026-10-11，已完成，未提交）**：§18.4 已先冻结；同 saver 独立子图终态复用、冻结父请求/份额、v10 身份索引、未知结果人工核对与持久取消均通过实际测试。新增 30 个实例；冻结后全量 **1719 passed / 8 skipped / 0 failed / 39 warnings，750.26 秒**，退出码 0。ruff、mypy（128 文件）、OpenAPI 与前端类型一致性通过。下一项 T69。

### T69｜应用接入、结果可见性与故障收敛

- **目标**：用户可在既有提交/任务链路使用工作流，并看到已验证的阶段和最终结果。
- **落点**：`application/service.py` 与现有投影；确有必要才改 API/前端，保持任务中心与会话共用真源。
- **验收**：等待审批不占用其他会话；取消、超时、预算耗尽、子失败、证据缺失均在主任务可见；同请求重放保持幂等；公共契约有变化则生成 OpenAPI/前端类型并通过漂移检查。
- **不做**：第二套任务中心或审批界面、v0.8 断线事件重放。
- **状态（2026-10-11，已完成，未提交）**：§18.5 已先冻结；三类工作流接入原提交/审批/任务链路与同轮快照，幂等、取消、失败、跨会话与刷新恢复均通过实际测试。新增 24 个后端实例；最终冻结后全量 **1743 passed / 8 skipped / 0 failed / 38 warnings，496.43 秒**，退出码 0；前端 345 passed，工作流真实 API 浏览器 5 passed，原自由循环浏览器 2 passed。静态、类型与漂移检查通过。下一项 T70。
- **失败记录**：首轮全量发现普通轮次扩展参数与既有捕获上下文接口不兼容，已停止并保留原始片段/冻结哈希；修正后完整重跑通过，旧审批与取消测试未被放宽。首轮浏览器创建会话竞态也已修正并全套复验。

### T70｜固定任务对照与交付收口

- **目标**：冻结实现，完成两条工作流与自由循环的同题对照及全量回归。
- **对照方法**：使用相同任务指纹、Profile、模型、预算、配置与隔离种子；记录所有失败，不删除不利样本。完成率和越权/重复副作用先作硬门，再比较调用数、耗时与有来源的 Token。
- **优势判据**：两条工作流各有可复现对照；正确性不下降，在模型/工具调用、耗时或实际 Token 至少一项改善。脚本化模型的改进只写为离线工程指标，不自动外推真实模型。
- **验收**：完整 Python 回归、ruff、mypy；若前端变化则相应单测/浏览器回归；父子恢复与取消故障注入；留任务指纹、命令、退出码与原始结果。真实模型实验如需新增费用或改变已暂停范围，另行取得授权。
- **停止条件**：无优势则记录未满足路线图该项，不用“测试全绿”替代它；本阶段未完成项如实列出。
- **范围冻结（2026-10-12）**：§18.6 已先冻结；新增独立离线对照模块、CLI 与验收测试，复用且不修改 T64 数据/校验/基线。相同脚本模型按出站协议应答，每题三次配对、交替顺序；保留失败、核对配置/种子/模型指纹与实际审计。生产与前端实现冻结，不新增付费调用或语义审查。
- **状态（2026-10-12，已完成，未提交）**：两题各三次配对共 12 次运行全部通过机械验收；模型调用 6 → 1、4 → 0，工具分别 5 → 6、3 → 4，仅声明离线编排优势。新增 28 个实例；冻结后最终完整回归 **1771 passed / 8 skipped / 0 failed / 38 warnings，470.51 秒**，退出码 0，229 个冻结文件不变；ruff、mypy（129 文件）及 API/前端类型漂移通过。T69 接入文件 Hash 不变，前端沿用其原始验收，本轮未重跑前端用例。T64–T70 工程阶段收口，可交付源码；真实模型质量、语义审查和 T43 开放项保持原边界。

## 4. 硬验收与停止条件

1. 能力只收窄，不因工作流/委派隐式扩大；审批与取消仍由原执行门裁决。
2. 模型回复不是成功证明；产物、工具退出码与证据都必须核对。
3. 子 Agent 的异常、超时、无证据、额度耗尽不得变成主任务成功。
4. 预算在动作启动前检查；父子共同计数，已发生调用仍记账，重试不能绕额度。
5. 缺实际 Token 时保守估算/预留，无法安全预留则停止；不得把未知实际量当作 0。
6. 副作用不明时人工核对，不自动重放；已完成子任务有持久证明才复用。
7. 工作流关闭或未选中时保留既有自由循环；轮次中途不换工具 Schema。
8. 不为接入创建第二套 Graph 状态、工具执行、取消或用量记录入口。
9. 命中契约冲突、证据不能证明、隔离需突破现有授权范围时停止该任务，先修订契约。
10. 每个任务完成后更新本文件中的状态、验证命令、结果和未做项；不每轮重新扩大旧版本审查。

## 5. 实施取舍

- **先离线基线，再 Subgraph，再子 Agent**：让每一步都有真实消费者和同题校验，避免先建调度平台。
- **先只读委派**：并发写入、Shell 与批准继承不属于第一版子 Agent 的边界。
- **复用现有任务运行期**：后台与内联仍是两种调度时机，不增加另一份图驱动入口。
- **真假指标分开**：假模型、估算 Token、供应商实际用量分别标注；不以假数据证明缓存或语义质量。
- **不引入外部对照项目依赖**：本计划基于已读路线图与现有实现，不把未经核实的外部 API 写成契约。

## 6. 验证与证据维护

T64 复跑命令（先跑基线创建证据父目录，再运行指定临时目录的 pytest）：

```powershell
Set-Location E:/pylianxi/chaiai/local-agent-web
.venv/Scripts/python.exe scripts/run_workflow_baseline.py --output eval/v07-t64-20261011/baseline.json
.venv/Scripts/python.exe -m pytest tests/test_workflow_eval.py -q -p no:cacheprovider --basetemp eval/v07-t64-20261011/focused-tmp
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
```

完整阶段门沿用 v0.6：`python -m pytest -q -rs -p no:cacheprovider`、ruff、mypy；API 变化时追加 `scripts/export_openapi.py --check` 与 `npm.cmd run check:api`。后续任务只运行与改动有关的定向验证，独立任务收口再全量回归。

基线输出位于被忽略的 `local-agent-web/eval/`；通过后在本文件记录摘要与指纹。原始失败结果保留，报告不得含密钥、正式会话或隐藏推理。

### 6.1 T64 当前证据（2026-10-11）

| 固定任务 | 任务指纹 | 当前自由循环 |
| --- | --- | --- |
| 代码修改 | `1ad43f952bf399c389a23d7d0762d453ea4f08702735ae082489aae925ea7e70` | completed，产物/真实三个 unittest/复读通过 |
| 只读调查 | `385c7fb42fd6f5428b9c6d513a5554e2216193f4d8c86adf7f4ff218aa23f2bc` | completed，三处事实/实际读取/文件不变通过 |

原始报告：`local-agent-web/eval/v07-t64-20261011/baseline.json`。报告同时保存跑器源码指纹与评测配置指纹；耗时依机器负载变化，不是本轮优势结论。假模型实际 Token/缓存仍为 `null`。

定向首次运行因本机默认 pytest 临时目录访问受限未完成；第二次指定临时目录时父目录未创建，也未完成。两次环境错误不计为通过；创建本轮证据父目录后，31 个当时已有用例全部通过。随后补充第 32 个引用路径负例并纳入最终完整回归，不改旧版本产品代码解决测试环境问题。

最终定向日志 `eval/v07-t64-20261011/focused-final.log`：32 passed，3.74 秒。初次完整执行的 `pytest-full.log` 因临时目录位于本仓库，9 个项目作用域测试继承了同一个 Git 根；另有 2 个进程树终止测试在受限环境失败（11 failed / 1503 passed / 8 skipped）。因此完整套件改用仓库外的临时目录与允许清理自建子进程树的执行环境；切换环境前的中断日志 `pytest-outside-interrupted.log` 也不计作通过，最终结果以 `pytest-final.log` 为准。

最终完整日志汇总：**1514 passed / 8 skipped / 38 warnings，351.30 秒**。相对 v0.5/v0.6 工程基线新增 32 个通过实例，跳过数仍为 8。付费真实模型、真机凭据、可选依赖与平台条件的跳过不记成已验证。测试已完成、临时目录已清理；外层命令在打印含中文的汇总时发生 GBK 编码异常，另行读取完整日志并检查无失败汇总通过，不把外层打印错误当作测试失败或隐瞒它。

本轮改动仅六个文件：本计划、根 `README.md`、`ARCHITECTURE.md` §18.1，以及新建 `src/local_agent/workflow_eval.py`、`scripts/run_workflow_baseline.py`、`tests/test_workflow_eval.py`。未修改生产图、工具、配置示例、数据库、API 或前端，因此本轮不重复前端矩阵。文档本地链接和差异空白检查通过。

完整回归的临时目录须在仓库外；用户可在普通开发终端复跑：

```powershell
Set-Location E:/pylianxi/chaiai/local-agent-web
$taskTempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("chaiai-v07-t64-" + [guid]::NewGuid().ToString("N"))
.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider --basetemp $taskTempRoot
```

完整执行需能终止测试自己创建的子进程树。不得通过修改项目作用域或进程清理实现来迁就受限测试环境。

### 6.2 T65 复跑与边界（2026-10-11）

```powershell
Set-Location E:/pylianxi/chaiai/local-agent-web
$taskTempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("chaiai-v07-t65-" + [guid]::NewGuid().ToString("N"))
.venv/Scripts/python.exe -m pytest tests/test_readonly_workflow.py tests/test_workflow_eval.py tests/test_t43_main_loop.py tests/test_graph_resume.py -q -p no:cacheprovider --basetemp $taskTempRoot
.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider --basetemp ($taskTempRoot + "-full")
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe scripts/export_openapi.py --check
Set-Location frontend
npm.cmd run check:api
```

- 当前图入口：在已装配的 `build_runtime_context` 传入 `readonly_research={"pattern": "docs/*.md", "keywords": ["取消", "执行结果", "能力"]}`，且 `config.workflows.enabled=true`；输入为空（`None`）走原自由循环。模式必须为 `agent`。此入口不是已发布的网页按钮或 REST 字段。
- 固定任务 B 仍使用 T64 原输入、三份种子、`reviewer` 与任务指纹；已通过同一个 `validate_outcome`。观察到 **0 次模型调用、4 次工具调用、11 个工作流步骤**（1 次 glob + 3 次 read_file）；默认 8 个源文件的完整流程也已测试，9 次工具调用可到达完成。
- 实际调用的工具仍由原安全门与执行节点执行。父 Profile 可写时子图交集仍拒绝写入；取消发生在最终读取之后仍阻止成功；历史结果、缺正文和失败读取不能通过。两轮子图工具事件恰为 8 条、身份互不重复；随后无工作流请求的普通轮次清空选择，保留旧审计且正常返回原自由循环回答。
- 只输出带真实行号的原文引用，不声称完成真实模型语义归纳。报告成功时同时写既有 `final_evidence` 与反思证据；源文中的“执行 write_file”等文字只被引用，不产生控制指令。
- 证据日志目录 `local-agent-web/eval/v07-t65-20261011/`：`focused.log`、完整 `pytest-final.log`、`pytest-exit.json`（退出码 0）与 `verification.json`（最终文件 SHA-256、检查结果与 T64 基线源码完整性）。首次完整回归的唯一失败为旧节点集合断言未列入新节点；已同步，原日志保留为 `pytest-node-contract-before.log` 与对应退出码，不覆盖历史失败证据。未新增付费调用、数据库迁移、公共 DTO 或前端代码，本轮不重复完整前端截图矩阵；网页接入、子 Agent 与跨重启证明仍分别在 T69、T67、T68。

本轮文件清单：

| 路径 | 改动 |
| --- | --- |
| `ARCHITECTURE.md`、`V0_7_DEVELOPMENT_PLAN.md`、`README.md` | 冻结 T65 契约、记录执行与当前阶段 |
| `local-agent-web/src/local_agent/workflow_contracts.py` | 严格输入与可序列化工作流状态 |
| `local-agent-web/src/local_agent/graph/readonly_workflow.py` | 固定子图、权限交集、额度与证据报告 |
| `local-agent-web/src/local_agent/graph/builder.py`、`graph/edges.py` | 注册 Subgraph，显式请求才选择；复用原用量装饰器 |
| `local-agent-web/src/local_agent/graph/state.py`、`graph/nodes.py` | 工作流状态/上下文与新轮次清空 |
| `local-agent-web/src/local_agent/config.py`、`config.yaml.example` | 默认关闭开关与有实际消费者的资源上限 |
| `local-agent-web/tests/test_readonly_workflow.py` | 37 个正常、失败、取消、额度、历史与权限实例 |
| `local-agent-web/tests/test_router.py` | 原节点集合断言同步已登记的 `readonly_research` 节点 |

子图本身不调用模型。这里观察到整轮 0 次调用的固定验收配置关闭了记忆/Embedding；父图原有召回不会被这个子图关闭，不把该数字外推到用户任意配置。

### 6.3 T66 复跑与边界（2026-10-11）

```powershell
Set-Location E:/pylianxi/chaiai/local-agent-web
$taskTempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("chaiai-v07-t66-" + [guid]::NewGuid().ToString("N"))
.venv/Scripts/python.exe -m pytest tests/test_code_workflow.py tests/test_readonly_workflow.py tests/test_workflow_eval.py tests/test_graph_resume.py tests/test_router.py -q -p no:cacheprovider --basetemp $taskTempRoot
.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider --basetemp ($taskTempRoot + "-full")
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe scripts/export_openapi.py --check
Set-Location frontend
npm.cmd run check:api
```

- 图入口为 `build_runtime_context(code_change={"source_path": "bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 3})`，配置 `workflows.enabled=true` 与 Agent 模式；不增加 REST 字段或网页按钮。
- T64 固定题 A 的种子、输入、Profile、指纹与 `validate_outcome` 均保持原样。实际观察 **1 次假模型调用 / 6 次工具调用 / 16 个工作流步骤**；执行真实 edit_file 与 Shell unittest，测试后再读取源码及测试文件。未宣称真实模型语义质量或完成 T70 的对照优势验收。
- 60 个新增实例覆盖请求/模型协议非法、配置关闭、冲突选择、缺能力、五类资源/模型额度、真实测试失败与数量不符、前后版本变更、BOM/CRLF、执行结果缺失/旧轮/截断/篡改、五个取消检查点、等待审批时取消、两次连续批准及各拒绝/编辑路径。审批后的写入与模型均恰执行一次；随后未选中工作流的普通轮次正常返回，自由循环用量和历史工具记录无重复。
- 仅使用离线假模型与隔离源码/数据库；实际供应商 Token/缓存未上报仍为未知，保守预留独立记录为正数。未新增付费调用、ZIP、Git 提交、公共 DTO、数据库迁移或前端源码。网页接入与跨重启副作用证明分别留在 T69/T68。
- 日志目录为 `local-agent-web/eval/v07-t66-20261011/`，专项日志 `focused-final.log`；首次专项日志 `focused-first.log` 保留（已修正模型切换时的测试夹具与用量断言）。离线 SDK 503 的失败注入 `sdk-retry-before.log` 实证：仅复制字段时请求 3 次；改为零重试 SDK 客户端资源后只请求 1 次、出站 max_tokens=2048，验证于 `sdk-fixed.log`（97 passed）。SDK 修复前的专项/完整日志另存 `focused-sdk-before.log`、`pytest-sdk-before.log`，不作为最终版本验收。完整日志/退出码为 `pytest-final.log`、`pytest-exit.json`，最终校验值在 `verification.json`。
- 最终完整回归退出码为 0；运行期间源码/配置/测试的 10 个冻结校验值保持一致，T64 三个基线源码校验值保持原样；测试自建的完整回归临时目录已清理。

本轮文件清单：

| 路径 | 改动 |
| --- | --- |
| `ARCHITECTURE.md`、`V0_7_DEVELOPMENT_PLAN.md`、`README.md` | 先冻结 §18.2.1，记录范围、验证与当前任务 |
| `local-agent-web/src/local_agent/graph/code_workflow.py` | 单文件固定闭环、模型预留、原工具/安全门和产物核对 |
| `local-agent-web/src/local_agent/workflow_contracts.py` | 代码输入、受限提议及可序列化状态 |
| `local-agent-web/src/local_agent/graph/builder.py`、`graph/edges.py` | 注册第二条子图、选择与原父审批衔接 |
| `local-agent-web/src/local_agent/graph/state.py`、`graph/readonly_workflow.py` | 扩展工作流状态与上下文；只读子图仅收窄联合类型 |
| `local-agent-web/src/local_agent/config.py`、`config.yaml.example` | 实际模型消费者的总预留与输出额度 |
| `local-agent-web/tests/test_code_workflow.py`、`tests/test_router.py` | 60 个实例与注册节点断言 |

### 6.4 T67 复跑与边界（2026-10-11）

```powershell
Set-Location E:/pylianxi/chaiai/local-agent-web
$taskTempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("chaiai-v07-t67-" + [guid]::NewGuid().ToString("N"))
.venv/Scripts/python.exe -m pytest tests/test_delegated_workflow.py tests/test_code_workflow.py tests/test_readonly_workflow.py tests/test_session_kind.py tests/test_router.py tests/test_model_usage.py tests/test_memory_origin.py -q -p no:cacheprovider --basetemp $taskTempRoot
.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider --basetemp ($taskTempRoot + "-full")
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe scripts/export_openapi.py --check
Set-Location frontend
npm.cmd run check:api
```

- 图入口为 `build_runtime_context(delegated_research={"tasks":[{"scope":"docs","question":"核对取消、恢复与权限原文"}],"concurrency":1})`，配置 `workflows.enabled=true`、Agent 模式、父有效 `files_read/code_search` 能力。问题是显式委派内容，子模型只选择严格相对 pattern 与字面关键词，不接收父历史。HTTP/UI 入口留给 T69。
- 一子固定调查实际执行 **1 次假模型 / 4 次真实工具 / 15 个计数步骤**；二子独立目录实际执行 **2 次假模型 / 4 次真实工具 / 20 个计数步骤**，三子为 **3 / 6 / 29**。串行与显式并行均验证，Barrier 使两次模型调用真实重叠，全部受同一父份额约束；这些是离线工程指标，不外推供应商速度或语义质量。
- 78 个新增实例覆盖真实文件不变、单/双/三子及并发、非法输入/递归字段、父缺能力/关闭/冲突/取消、总预算耗尽、模型异常/超额/工具调用、缺证据、两个超时检查点、父取消、上下文隔离、实际执行侧写入/Shell/外部/敏感路径拒绝、跨子/失败终态/正文/行号/Hash/磁盘篡改、BOM/CRLF、原用量投影和跨轮 Checkpoint 不重复。假模型失败与原 SDK 503 均保留用量；MockTransport 实际请求一次、max_tokens=2048、read timeout≤1 秒，父 SDK 重试设置仍为 2。
- 聊天库版本升到 v9，同入口事务扩展 subagent CHECK，外键开/关两种迁移、旧会话/任务/分支/索引保留、幂等与失败回滚均实际测试；只操作临时库。记忆 origin 闭集与自动注入规则不变，子 State 来源为 subagent，候选仍判 system；子图没有记忆节点。父子会话索引、独立 Checkpoint 与重启复用证明仍由 T68 实现，不把当前跨轮测试写成重启验收。
- 补齐检索落盘边界后的相关回归 `focused-spill-final.log` 为 **516 passed / 2 skipped / 0 failed，171.18 秒**，平台跳过不写成通过。之前的 `focused-with-checkpoint.log`（335 passed，80.04 秒）和 `new-final.log`（74 passed，25.18 秒）保留。最终完整回归为 **1689 passed / 8 skipped / 0 failed / 40 warnings，718.16 秒**，`pytest-exit.json` 退出码 0；同时运行过定向测试，该耗时不作为 T70 性能对照。ruff、mypy（128 文件）、OpenAPI 与前端类型漂移已通过，分别留原始日志；冻结值在 `frozen-source-sha256.json`。
- 实际注入 save_full 负例确认：原子策略让 grep 写出 `.local-agent/results` 两个文件，父失败不等于没有写入。根因是只读检索工具支持显式落盘参数，子策略未限制；现于原策略判定与原工具函数各加一门，子策略固定关闭。四个新增实例覆盖 grep/glob 执行门被跳过和直接调用工具；复测工具 denied、落盘文件数为 0，前后证据在 `readonly-spill-before.json` / `readonly-spill-after.json`。原工具 Schema 与父默认行为保持，未引入用户配置。
- 落盘修复前完整回归 **1685 passed / 8 skipped / 38 warnings，479.13 秒**，另存 `pytest-spill-before.log`、`pytest-spill-before-exit.json`，不冒充最终验收。额外隔离手工八文件边界同样完成：**1 模型 / 9 工具 / 25 计数步骤**，证据 `eight-files-capacity.json`。
- 初轮 T67 为 65 passed / 1 failed，失败是 SDK 夹具用了通用模型名却断言 DeepSeek 的字段，改用实际 DeepSeek 模型名后复测通过；原终端失败摘要留在 `first-run-summary.json`。之前的 `focused-final.log`（332 passed）保留，不冒充最终源码验收。
- 无新增付费调用、ZIP、提交、生产数据库操作或 API/前端源码改动。回答语义审查仍暂停；本轮不宣称整个 v0.7 可交付。
- 最终运行的 18 个源码/配置/测试冻结值未变，T64 三个基线源码仍与原验收校验值一致；本轮两次完整回归的自建临时目录均已清理。最终 21 文件校验值、命令退出码、原始日志和边界由 `verification.json` 汇总。8 条跳过涵盖付费真实模型、真机凭据、可选依赖条件和平台语义，不记为通过；40 条为依赖弃用告警，未升级依赖。

本轮文件清单（21 个产品/测试/文档文件）：

| 路径 | 改动 |
| --- | --- |
| `ARCHITECTURE.md`、`V0_7_DEVELOPMENT_PLAN.md`、`README.md` | §18.3 范围、来源修订与验证结果 |
| `local-agent-web/src/local_agent/graph/delegated_workflow.py` | 隔离子计划/只读图、固定总份额、并发/超时、父证据核对与报告 |
| `local-agent-web/src/local_agent/workflow_contracts.py` | 严格父输入、子任务、结构化子报告与第三种工作流状态 |
| `local-agent-web/src/local_agent/graph/builder.py`、`local-agent-web/src/local_agent/graph/edges.py` | 注册/显式路由、子用量合并原明细与父整轮记录 |
| `local-agent-web/src/local_agent/graph/state.py`、`local-agent-web/src/local_agent/graph/readonly_workflow.py` | 上下文/状态与独立子 State 来源传入 |
| `local-agent-web/src/local_agent/capabilities.py`、`local-agent-web/src/local_agent/session_kind.py` | 父只读能力交集与 subagent 真实来源 |
| `local-agent-web/src/local_agent/config.py`、`local-agent-web/config.yaml.example` | 有消费者的子数、并发数、超时配置 |
| `local-agent-web/src/local_agent/llm/workflow_model.py`、`local-agent-web/src/local_agent/graph/code_workflow.py` | 共用实际零重试 SDK 克隆，T66 调用行为保持原样 |
| `local-agent-web/src/local_agent/persistence/migrations.py` | 同入口 v9 事务重建 sessions 与约束/外键复核 |
| `local-agent-web/src/local_agent/security/policy.py`、`local-agent-web/src/local_agent/tools/search_tools.py` | 子策略限制 save_full 落盘，原执行门与工具函数双重拒绝 |
| `local-agent-web/tests/test_delegated_workflow.py`、`local-agent-web/tests/test_session_kind.py`、`local-agent-web/tests/test_router.py` | 78 个新实例与闭集/注册节点断言同步 |

### 6.5 T68 执行与验收记录（2026-10-11）

- 先冻结 §18.4，再增加 reserve 检查点、严格请求/预算份额快照及稳定 child_id。子结果保存在同一 SQLite saver 的独立子 thread；身份索引只有 parent_job_id、parent_run_id、child_id，不存业务状态。登记失败不启动子调用，子 RuntimeContext 不继承登记回调。步计数沿用父调度/报告两步，reserve 是调度的持久准备阶段，不增加模型或工具额度。
- 重开时仅复用完整子终态；失败结果同样复用、不重试。整批预检发现已有子非终态就拒绝执行，不并发启动其他子。父核对仍复读原文/Hash，文件变化会失败且保留原调用审计。旧 T67 dispatch 没有冻结规格时要求人工核对。
- v10 在同一事务追加身份表、索引及版本号，版本写入失败注入可回滚全部 DDL；v9 旧会话/任务保留，外键开/关、幂等和不可覆盖关系均测试。旧迁移脚本未改。
- 原恢复分类只增加已完成子任务的只读证明与说明：子完成不升级父任务；父不完整仍 needs_review。实际重启测试中，子完成与未知结果都不会驱动图。实际写入后、检查点前故障保留修改并转人工核对。审批重开通过原 Command 入口，批准/拒绝与数据库取消意图均验证。
- 新增 **30 个测试实例，30 passed / 0 failed，26.87 秒**；最终冻结后完整回归 **1719 passed / 8 skipped / 0 failed / 39 warnings，750.26 秒**，退出码 0。12 个源码/测试冻结值未变；ruff、mypy（128 文件）、OpenAPI 与前端类型漂移检查通过。
- 两次早期失败分别是原生 task 完成写入未阻止重开后重调模型（4 failed / 2 passed），以及独立父 Subgraph 测试未装配 saver（7 failed / 139 passed）。已用同 saver 独立子图和显式编译修正；原终端结果摘要在 `eval/v07-t68-20261011/first-attempt-summary.json`，不将摘要冒充原始完整日志。
- 最终证据目录 `local-agent-web/eval/v07-t68-20261011/` 保存完整 `pytest-final.log`、`pytest-exit.json`、冻结校验、静态检查原始日志和 `verification.json`。T64 三个基线源码校验值仍未变，全量运行的自建临时目录已清理。8 条跳过为真实模型两项、可选依赖一项、Windows/POSIX 或符号链接条件四项、真机凭据一项；均不计入通过。39 条为依赖弃用告警，未升级依赖。
- 无新增付费调用、ZIP、Git 提交、生产数据库操作或公共 API/前端源码改动；语义审查仍暂停。T69/T70 尚未完成，不将 T68 工程任务完成等同整个 v0.7 交付。

复跑命令（PowerShell，进入 `local-agent-web`，隔离目录在仓库外）：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$t68CaseRoot = Join-Path $env:TEMP ('chaiai-t68-' + [guid]::NewGuid().ToString('N'))
.venv\Scripts\python.exe -m pytest -q -rs -p no:cacheprovider --basetemp $t68CaseRoot
.venv\Scripts\python.exe -m ruff check src tests scripts packaging
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe scripts/export_openapi.py --check
Push-Location frontend
npm.cmd run check:api
Pop-Location
```

本轮文件清单（15 个产品/测试/文档文件）：

| 路径 | 改动 |
| --- | --- |
| `ARCHITECTURE.md`、`V0_7_DEVELOPMENT_PLAN.md`、`README.md` | §18.4 冻结、实现边界与验收记录 |
| `local-agent-web/src/local_agent/graph/delegated_workflow.py` | 父预留检查点、独立子图终态复用、整批未知结果拒绝与原证据核对 |
| `local-agent-web/src/local_agent/graph/builder.py`、`local-agent-web/src/local_agent/graph/state.py` | 同 saver 装配及运行期身份登记回调 |
| `local-agent-web/src/local_agent/workflow_contracts.py` | 父冻结请求与可序列化子规格 |
| `local-agent-web/src/local_agent/persistence/migrations.py`、`local-agent-web/src/local_agent/persistence/job_store.py` | v10 身份索引事务迁移与同连接不可重入锁内登记 |
| `local-agent-web/src/local_agent/application/jobs.py`、`local-agent-web/src/local_agent/application/recovery.py`、`local-agent-web/src/local_agent/application/service.py` | 既有任务索引登记入口、只读子完成证明与启动恢复说明 |
| `local-agent-web/tests/test_delegated_recovery.py` | 30 个 SQLite 关闭重开、授权、取消、审批、真实写入间隙与迁移故障实例 |
| `local-agent-web/tests/test_delegated_workflow.py`、`local-agent-web/tests/test_session_kind.py` | 既有迁移断言同步当前版本；旧迁移行为保持回归 |

### 6.6 T69 应用接入与展示（2026-10-11，已完成，未提交）

先冻结 §18.5，再复用原提交/后台 worker/审批恢复链路；未新增 Graph 执行器或数据库表。严格的 `workflow.name + request` 选择参与规范化幂等摘要，父 State 保存选择与限额；新轮次显式清空旧选择，审批恢复只读取已冻结参数。配置 `workflows.enabled: true` 后，Agent 模式输入区可选择只读调查、代码修改与验证、委派只读调查；未选择仍为自由循环，闲聊模式不授权工作流。

公开快照白名单展示同轮阶段、预算计数、证据数及失败原因；嵌套检查点只贡献展示事实，不替代父任务状态与结果引用。快照事件在同一父检查点内以投影摘要区分阶段。最终结果、工具证据、审批、取消与任务中心仍沿原入口；不会公开模型输入、修改提议正文或私有子规格。表单草稿按会话隔离，创建会话/运行/审批期间锁定；服务端仍严格校验所有参数。

实际验收覆盖：三类工作流真实隔离工具与幂等重放、参数改变冲突、新轮次不继承选择、422 输入拒绝且不落台账、开关/Agent 模式门、预算不足/无证据/子异常/子超时在父快照失败、重启后原审批的批准/拒绝/持久取消、冻结预算恢复、单 worker 等待审批释放其他会话、嵌套阶段实时读取与 SSE 不重复身份、取消保留终态索引、私有字段脱敏。前端另测参数序列化、重试键、会话草稿隔离、创建会话竞态和子成功不提升父结局。

| 检查 | 最终结果 |
| --- | --- |
| T69 新后端实例 | 24 passed；连同旧审批偏好 26 passed / 0 failed；旧取消门另 1 passed |
| 冻结后完整 Python 回归 | **1743 passed / 8 skipped / 0 failed / 38 warnings，496.43 秒**；退出码 0，26 个冻结文件哈希未改变 |
| 前端全量单测 | 39 files / 345 passed / 0 failed |
| 真实 API 工作流浏览器 | 5 passed / 0 failed，20.7 秒；含 1440/1280/640 宽度、刷新、原审批及子证据 |
| 原自由循环浏览器 | 2 passed / 0 failed，17.8 秒；两轮对话与会话模式刷新恢复 |
| ruff / mypy | 最终源码均通过，退出码 0；mypy 128 个源码文件 |
| OpenAPI / 前端契约漂移 | 均通过，公开联合输入及只读投影已生成 |
| 前端 lint / typecheck / build | 均通过；lint 无新增警告 |
| diff --check | 本轮范围通过，退出码 0 |

证据保存于 `local-agent-web/eval/v07-t69-20261011/`（忽略目录）：完整最终日志、退出码、26 个冻结文件 SHA-256、定向/静态/浏览器原始日志。首轮全量是已中止运行，原始输出片段保留为 `interrupted-first-pytest-final.log`，不能当成完整验收；对应冻结哈希与退出码也保留。首轮浏览器 3 passed / 2 failed 的创建会话竞态已修正并全套复验；测试跟踪 ZIP 已清除，之后关闭 trace。临时测试服务只使用临时工作区与假模型。

复现（从仓库根目录，用现有虚拟环境与已安装前端依赖）：

```powershell
Push-Location local-agent-web
$taskTestRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("chaiai-v07-t69-" + [guid]::NewGuid().ToString("N"))
.venv/Scripts/python.exe -m pytest -q tests/test_workflow_application.py -p no:cacheprovider --basetemp $taskTestRoot
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe scripts/export_openapi.py --check
Push-Location frontend
npm.cmd test
npm.cmd run lint
npm.cmd run check:api
npx.cmd playwright test -c playwright.workflow.config.ts
npm.cmd run test:live -- --trace off
Pop-Location
$taskFullRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("chaiai-v07-t69-full-" + [guid]::NewGuid().ToString("N"))
.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider --basetemp $taskFullRoot
Pop-Location
```

本轮 29 个产品/测试/文档文件（26 个冻结源码/生成文件 + 3 个根文档）：

| 路径 | 改动 |
| --- | --- |
| `ARCHITECTURE.md`、`V0_7_DEVELOPMENT_PLAN.md`、`README.md` | 接入契约、执行验收与下一项状态 |
| `local-agent-web/src/local_agent/workflow_contracts.py` | 复用三种严格输入的判别联合 |
| `local-agent-web/src/local_agent/api/schemas.py`、`api/routes.py` | 原提交请求透传显式工作流选择 |
| `local-agent-web/src/local_agent/graph/state.py` | 父 State 保存本轮选择与限额 |
| `local-agent-web/src/local_agent/application/service.py` | 选择摘要、冻结上下文、嵌套检查点展示及快照阶段事件 |
| `local-agent-web/src/local_agent/application/models.py`、`application/projection.py` | 公开只读工作流/子结果白名单投影 |
| `local-agent-web/tests/test_workflow_application.py` | 24 个应用/API/重启/并发/取消/失败与展示验收实例 |
| `local-agent-web/scripts/serve_workflow_fake.py` | 隔离假模型 + 真实工具浏览器验收服务 |
| `local-agent-web/docs/api/openapi.json`、`frontend/src/api/schema.gen.ts` | 受控公共契约及生成类型 |
| `local-agent-web/frontend/src/api/client.ts`、`api/queries.ts`、`api/queries.test.tsx` | 原提交上下文、工作流重试键与验证 |
| `local-agent-web/frontend/src/components/Composer.tsx`、`src/app/LiveApp.tsx`、`src/app/LiveApp.test.tsx` | 原输入区接线、同会话阶段、创建期间锁定及草稿隔离验证 |
| `local-agent-web/frontend/src/features/workflow/draft.ts`、`WorkflowControls.tsx`、`WorkflowStatus.tsx`、`adapters.ts`、`Workflow.module.css`、`WorkflowControls.test.tsx` | 参数表单、白名单展示映射、窄屏布局与组件测试 |
| `local-agent-web/frontend/playwright.workflow.config.ts`、`playwright.config.ts`、`e2e/workflow-live.spec.ts` | 三类工作流真实 API 验收及隔离默认视觉套件 |

8 项跳过与 T68 相同：2 项需显式联网付费的真实模型案例、1 项只针对未安装可选依赖的案例、1 项需显式开启的真机凭据存储、2 项当前环境不能创建符号链接、2 项 POSIX 大小写/权限位案例。跳过不计为通过，38 条依赖弃用警告不通过升级依赖消除。

T69 已完成，下一项 T70。本项不做语义质量审查、新增付费调用、打包 ZIP、第二任务中心/审批界面、v0.8 断线重放或 T70 对照结论；当前不宣称整个 v0.7 可交付。

### 6.7 T70 同题对照与交付证据（2026-10-12）

两题各三次配对共 12 次运行，所有机械验收通过：代码模型调用 6 → 1、工具 5 → 6；只读模型调用 4 → 0、工具 3 → 4。每次配对都在模型调用数改善；工具增加如实保留。两臂各用相同配置/模型/种子和原 Profile，不放宽 T64 校验器或修改其三个源码文件。优势只属于固定脚本的离线编排，不代表真实模型质量、Token 或缓存效果。

原始公开报告和具体复现命令见 [T70 交付记录](../benchmarks/v07-workflow-comparison-20261012.md)。CLI 返回 0；定向新增 **28 passed / 0 failed，6.01 秒**；ruff、mypy（129 文件）、OpenAPI 和前端类型漂移均通过。冻结后完整回归 **1771 passed / 8 skipped / 0 failed / 38 warnings，470.51 秒**，退出码 0；229 个冻结文件 Hash 不变。原始日志/命令/退出码与源码 Hash 在 `local-agent-web/eval/v07-t70-20261012/`，汇总为 `verification.json`。

首轮全量在发现新跑器异常分支无法取完整工具审计却记为 0 后受控中断；保留 `interrupted-first-*`，实际退出码 4294967295，不计通过。修正后增加“真实工具已执行再抛异常”的负例：保留实际模型次数，未知工具次数写 null，不能宣称优势。修正后重新冻结并完整执行。首次对照报告与定向结果也保留，不覆盖失败或中断证据。

本轮 8 个交付文件；生产 Graph、API、持久化及前端实现保持 T69 状态：

| 路径 | 改动 |
| --- | --- |
| `local-agent-web/src/local_agent/workflow_comparison.py` | 同模型两臂、三次配对、硬门、完整尝试及离线指标 |
| `local-agent-web/scripts/run_workflow_comparison.py` | 可复跑报告，失败返回非零 |
| `local-agent-web/tests/test_workflow_comparison.py` | 28 个真实执行、公平性、失败、未知审计、越权和重复副作用验收实例 |
| `docs/benchmarks/v07-workflow-comparison-20261012.json` | 12 次运行的原始机械结果、实际工具输出与源码指纹 |
| `docs/benchmarks/v07-workflow-comparison-20261012.md` | 对照结论、复现命令、失败记录和交付边界 |
| `ARCHITECTURE.md`、`V0_7_DEVELOPMENT_PLAN.md`、`README.md` | T70 契约、验收及版本状态 |

T69 接入文件 Hash 不变；前端沿用 T69 原始验收（345 单测、5 工作流浏览器、2 原自由循环浏览器通过），本轮未重新执行这些前端用例。完整 Python 套件覆盖父子关闭重开、不重复执行、未知结果人工核对、持久取消、原审批恢复及子失败/超时等故障注入。未提交 Git、不生成 ZIP、不操作正式数据；回答语义审查继续暂停，不新增付费调用，也不把 T43 开放事项写成完成。

8 项跳过与 T69 相同：2 项需显式联网付费的真实模型案例、1 项只针对未安装可选依赖的案例、1 项需显式开启的真机凭据存储、2 项当前环境不能创建符号链接、2 项 POSIX 大小写/权限位案例。跳过不计为通过；38 条依赖弃用警告不通过升级依赖消除。

T64–T70 已按本阶段明确的离线验收范围完成，v0.7 工程源码可以交付。本轮没有新增付费调用，也没有将真实模型效果、语义质量、其他平台或旧阶段开放项写成通过。

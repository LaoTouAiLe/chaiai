# v0.5 开发计划：Agent Profile 与业务能力扩展

> **状态**：2026-10-08 立项（项目负责人批准开工）。这是 v0.5 的第一版实施计划。
> **依据**：`docs/planning/FUTURE_WORK.md` §v0.5「Agent Profile 与业务能力扩展」与其中的 B 编号条目（B-01 等）。
> **前置**：v0.4 的验收结论见 `V0_4_DEVELOPMENT_PLAN.md` §T37 验收记录——朋友试用**已通过**；T38/T39 人工往返、真实缓存对照、无密钥轨迹导出经人工批准**豁免**，补验条件已写明。
> **效力**：本文件只负责 v0.5 的实施顺序。方向与"不做项"以 `FUTURE_WORK.md` 为准；两者冲突时先改文档，不允许由代码自行选择一种解释。**实施任何任务前，必须先把该任务涉及的稳定契约同步进 `ARCHITECTURE.md`。**

## 1. 范围

### 做什么（路线图 §v0.5 的六项主要工作，逐项落成任务）

1. Capability Registry 与 Agent Profile 配置校验
2. Git 能力：状态、diff、日志与受控提交（高风险写操作继续审批）
3. Web 搜索/读取：结构化来源，外部内容标记为不可信上下文
4. Skill 能力：按需加载指令与资源，复用 AGENTS 的作用域、安全与截断原则
5. 页面展示当前 Profile、已启用能力与不可用原因
6. 每个 Profile 只把自身工具 Schema 注入模型

### 这次不做（沿用路线图原话与既有裁定）

- 第三方 Python 包动态加载、插件市场、运行期热卸载（路线图明确不做）。
- MCP、子 Agent/Subgraph（v0.7）、后台任务与取消（v0.6）、持久事件游标与 WebSocket（v0.8）、执行隔离（v0.9）。
- 不新增第二套截断、检索或记忆实现；新能力复用既有路径。

**Skill 要做**（路线图把它列在 v0.5 主要工作里），但排在 Profile 与 origin 之后——它依赖 Profile 提供的"能力可见性"边界。

## 2. 契约改动点（先改契约，再编码）

| `ARCHITECTURE.md` 节 | 要写进去的东西 |
| --- | --- |
| §6 全部工具详细定义 | 工具的能力归属（capability 名）与 Profile 可见性；新增 `git_*` 与 `web_*` 工具契约 |
| §7 AGENTS.md 与系统 Prompt | Profile → 提示段落的关系；"能力与边界"段落如何随 Profile 变化；工具规范段落的适用范围 |
| §10 三层记忆 | 来源可信级别（`origin`）的闭集取值与注入规则：`untrusted` / `system` 在 Prompt 构建之前结构性排除 |
| §15 应用服务契约 | 当前 Profile 的读取/切换接口、事件与 DTO（含"能力不可用原因"字段） |
| 新增 §16 v0.5 契约 | Capability Registry、Profile、`origin`、外部内容处理在本节冻结；本阶段结束时本节即验收依据 |

理由：项目规矩是"实施阶段前先把该阶段涉及的稳定契约同步回架构文档"（`FUTURE_WORK.md` §13），且 v0.4 的 §15 就是按这个方式冻结的。

## 3. 任务拆分（T50–T56，按顺序；每个都能独立验收）

### T50｜Capability Registry 与 Profile 骨架

- **目标**：把"模式（chat/tool）决定工具集"升级为"能力决定工具集、Profile 组合能力"。配置里能声明 `coder` / `researcher` / `reviewer` 三档及其能力集。
- **落点（已核实的真实位置）**
  - `local-agent-web/src/local_agent/tools/tool_registry.py:25`（`MODE_TOOLS` → `CAPABILITY_TOOLS` 映射）、`:51`（`ToolRegistry.build`）、`:74`（`tools_for_mode` → `tools_for(profile)`）。
  - `local-agent-web/src/local_agent/config.py`：新增 Profile 配置段与校验（未知能力名、Profile 引用不存在的能力、缺依赖都要在**启动时**失败并给出可照做的提示）。
  - `local-agent-web/src/local_agent/reflection/reflector.py:166`：`_PLAN_TOOL_NAMES` 目前直接取 `MODE_TOOLS["tool"]`。**必须跟着改**，否则会重演 2026-09-30 那类缺陷（计划里写出本机不存在的工具，循环预算被烧在重复列目录上）。
  - `local-agent-web/src/local_agent/context/prompt_context.py:376`（`_CAPABILITY_RULES` 引用真实工具集）、`:412-416`（工具规范段落；那里的注释仍写着 `MODE_TOOLS["chat"] == ()`，与实际不符，一并订正）。
- **验收**：三档 Profile 配置可通过/可拒绝；`tools_for(profile)` 结果与配置逐项一致；同一 Profile 重复构建工具顺序**字节稳定**；配置错误与缺依赖时启动即明确失败。
- **明确不做**：动态注册；运行期改 Profile 后热更新工具集（口径与 v0.4 的模型切换一致——下一轮生效）。
- **状态（2026-10-08，已完成）**：契约 §6.9 与 §16.1 已冻结。改动落点：新增 `local-agent-web/src/local_agent/capabilities.py`（能力闭集 + 内置三档 + 模式映射；**叶子模块**，避免 `config → tool_registry → security.policy → config` 循环导入）、`config.py` 的 `profiles` 段与启动校验、`graph/state.py` 的 `agent_profile`/`agent_capabilities`、`graph/builder.py` 装配、`graph/nodes.py` 三处工具绑定与 `_draft_steps`、`reflection/reflector.py` 的 `plan_draft_instruction()`（按 Profile 生成，只读 Profile 不再被承诺 `update_plan`）、`context/prompt_context.py` 两处过期注释、`config.yaml.example` 的 `profiles` 示例。
- **T50 的验收证据**：新增 `tests/test_capabilities.py`（默认 Profile 与 v0.4 工具集**含顺序**逐项一致、chat 仍是两个只读工具、只读 Profile 看不到写类与 Shell、顺序稳定且不受访问顺序影响、能力表与实现不重不漏、未登记归属在构造注册表时失败、四类配置错误在加载阶段失败并列出可选项、计划草拟提示随 Profile 变化）。Python 全量 **1091 passed / 7 skipped**（上一轮 1063 + 本轮新增），`ruff check src tests` 与 `mypy src`（105 文件）通过。
- **T50 未做（如实列出）**：① 缓存回落的**观测与基线**（逐调用回落次数、事件指纹解释）属 T51；② 页面/接口还没有任何 Profile 展示（属 T56）；③ 运行期切换 Profile 的入口未做——当前只能改配置后重启生效（比"下一轮生效"更保守，不算违约）；④ `docs/lessons/` 是按旧 API（`MODE_TOOLS` / `tools_for_mode`）讲的，本轮只加了变更说明，没有重写教程。

### T51｜只注入本 Profile 的工具 Schema，并把缓存口径钉住

- **目标**：模型只看到本 Profile 的工具；同时不破坏 T34–T36 建立的稳定前缀与缓存优化。
- **落点**：`local-agent-web/src/local_agent/graph/nodes.py:562`（工具 Token 估算）、`:1087`（工具决策绑定）、`:1933`（任务记忆抽取绑定）——三处都调用 `tools_for_mode`，Profile 化后必须同源。
- **验收（硬，缺一不可）**
  1. 切换 Profile 前后的工具 Schema 指纹与系统提示指纹变化**可解释**（沿用事件指纹口径）。
  2. 同一 Profile、同一配置下连续请求的系统提示与工具 Schema **字节一致**（`FUTURE_WORK.md:320` 那条仍然成立）。
  3. 逐调用**缓存读回落次数**作为观测量（目标 0–1 次，与 T34–T36 同一口径），不是命中率。
  4. 三档 Profile 各跑一次固定任务，成功率不低于 v0.4 基线。
- **明确不做**：为省 Token 在同一 Profile 内按每轮内容动态裁剪工具集——那会让前缀每轮变化，属净损失。
- **状态（2026-10-08，工具与口径已完成，真实逐档基线待授权）**：三处绑定在 T50 已同源（`tools = registry.tools_for(mode, context.agent_capabilities)`），工具 Schema 指纹由 `tools_fingerprint(tools)` 从**实际绑定的工具对象**算出，因此 Profile 变化自动进入指纹、无需另设一套。本轮补齐观测与口径：新增 `local-agent-web/scripts/report_prefix_cache.py`（读 `baseline_result.json`，输出逐调用缓存读、**回落次数**、以及"回落时同时变了哪一段指纹"的归因，`--max-fallbacks` 默认 1、超限返回退出码 3；**不发模型调用**）；`scripts/measure_usage_baseline.py` 增加 `--profile`（同一固定场景逐档跑，结果里写明 Profile 与实际工具名）；`ARCHITECTURE.md` §16.1 追加"验收工具"一条。
- **T51 的验收证据（本轮）**：新增 `tests/test_prefix_cache_report.py` 7 条——回落归因到具体段落、Profile 切换计为前缀重置而非"无法解释的下降"、未上报缓存字段不当 0 也不计回落、阈值超限返回 3、输入缺失返回 2、以及**工具 Schema 指纹同档稳定 / 三档互不相同 / 与 chat 模式也不同**（用真实注册表计算）。Python 全量见提交记录；`ruff check .`、`mypy src` 通过。
- **T51 未做（如实列出）**：① **真实模型逐档基线没有跑**——`measure_usage_baseline.py` 会真实调用模型并产生费用，按惯例要先经你同意；因此"三档 Profile 各跑一次固定任务、成功率不低于 v0.4 基线"这条**尚未有真实数据**。② `report_prefix_cache.py` 目前只认 `measure_usage_baseline.py` 的结果形状（`turns[].model_calls[]`），已有的 `eval/memory-acceptance-*` 审计格式（按轮聚合）不能直接喂给它。③ 本机既有仓库里没有留存的历史 `baseline_result.json`，所以端到端只验证到"合成记录 + 真实指纹"这一层。

### T52｜`origin` 信任分级（路线图把它定为 v0.5 的第一个任务）

- **目标**：记忆与召回内容带来源可信级别；外部推导的内容永不自动注入。
- **落点**：`memory/schemas.py`（`MemoryReference` / `MemoryCandidate` 加字段）、`reflection/memory_candidates.py`（生成时打标）、`memory/writer.py`（写入时落标）、Prompt 组装处（在**构建之前**结构性排除，不是打分惩罚）。
- **验收**：无法判定来源时**永不默认 `owner`**（默认 `untrusted`）；`untrusted` / `system` 无论召回频次多高都不自动注入；现有记忆与召回用例全绿（不得以"新增字段"为由降低现有正确性）。
- **明确不做**：把 provenance 写进 Markdown 正文——召回文本不允许改写自己的信任级别。

### T53｜Web 搜索与读取

- **目标**：新增一个可关闭的联网能力，搜索结果结构化、外部正文按不可信内容处理。
- **落点**：新增 `local-agent-web/src/local_agent/tools/web_tools.py` 与对应适配器；在 `tool_registry` 注册为独立能力；新增 `web` 配置段（启用开关、超时、来源数上限、UA）。
- **验收**
  1. 搜索结果带标题 / URL / 来源，页面能定位到来源。
  2. 抓回的正文以 `untrusted` 进入上下文，**提示注入不能修改安全策略、不能自动授权工具**——用构造的恶意页面做回归用例。
  3. 超时与失败如实报告，不伪造结果、不伪造进度。
  4. 关闭该能力时，行为与 v0.4 完全一致。
- **明确不做**：多引擎回退、JS 渲染页面、登录态抓取、结果缓存（出现真实故障再引入）。

### T54｜Git 能力

- **目标**：只读的 status / diff / log 与受控提交。
- **落点**：新增 `tools/git_tools.py`；`security/policy.py` 增加提交前的门（暂存范围、分支保护、绝不自动 push）；审批卡片文案写清"提交到哪个分支、包含哪些文件"。
- **验收**：只读三件套在受控仓库可用；提交必须经人工审批；在 `main` / `master` 上的提交有额外确认；任何路径都不会出现 `git push`。
- **明确不做**：分支管理、merge / rebase、远程操作、冲突自动解决。
- **未知**：T40 的试用包不含 `git` 可执行文件。开工第一步先确认目标机器是否有 `git`，没有时按"明确失败并给出可照做提示"处理。

### T55｜Skill 能力（按需加载指令与资源）

- **目标**：把"一次性加载的工作区指令"扩展为"按需加载的技能包"，复用 AGENTS 的作用域、安全与截断原则。
- **落点**：新增技能加载模块（目录约定 + 解析 + 截断）；复用 `ARCHITECTURE.md` §7 的 AGENTS 加载与安全原则。
- **验收**：技能只能从工作区内、按显式声明加载；加载内容有上限且截断如实标注；来源按 `owner`（本机受信资源），与 T52 的分级口径一致；不加载技能时行为不变。
- **明确不做**：技能市场、技能内嵌可执行代码的自动执行。
- **前置**：开工前必须能说出一个具体消费者场景；说不出来就按 §6 停止条件处理。

### T56｜页面：Profile 与能力状态，以及三个固定任务集

- **目标**：用户能看到"当前是哪个 Profile、开了哪些能力、缺什么导致不可用"；三档 Profile 各有固定任务集作为验收判据。
- **落点**：应用服务契约（能力可用性与不可用原因的 DTO/事件）+ 前端设置页/顶部栏 + Playwright 截图基线。
- **验收**：`coder` / `researcher` / `reviewer` 各一套固定任务集可跑通并留结果；页面**不出现原始工具名**（沿用 `V0_4_DEVELOPMENT_PLAN.md` §T39 的字段门槛）；视觉基线按测试粒度重拍并人工抽查；缺依赖时页面说明"缺什么、怎么装"，不是只灰掉。
- **明确不做**：Profile 的可视化编辑器（v0.4 已裁定不做安全/权限的通用配置编辑器）。

## 4. 全局硬验收（每个任务都要过）

- **Profile 间不可见的工具**：模型不能调用，**也不能绕过注册表直接执行**（注册表是"给谁看"，安全门仍是"能不能做"，两者都要挡住）。
- **关闭新增能力后**，原基础 Agent 行为不变（回归全绿，不得以"新能力带来新路径"为由放宽）。
- **稳定前缀**：同一 Profile 同一配置下，系统提示与工具 Schema 字节一致；变化必须能用事件指纹解释。
- **不引入第二套实现**：截断、检索、记忆、审批各自只有一条路径。
- **契约先行**：每个任务开工前先改 `ARCHITECTURE.md`，并且文档与实现同批出现，不允许"先写代码后补文档"。

## 5. 风险与未知项

- **缓存**：Profile 化天然改变工具 Schema，即前缀变化。这条不是"可能发生"，是"一定发生"——所以观测量是回落次数，且要在 T51 就建立基线。
- **工具集变大 → 误调用**：三档 Profile 的固定任务集是唯一判据；"功能可用"不算通过。
- **Web 的注入面**：本机 Agent 读外部网页是本阶段最大的安全增量。T53 的恶意页面回归必须**先写用例再实现**。
- **Skill 的边界**："按需加载指令"与已有 AGENTS.md 可能重叠；没有真实消费者就不做（防止造出没人用的机制）。
- **B 编号条目的复核**：`FUTURE_WORK.md` 明确要求"条目里的阶段建议在立计划时必须重新判定"。本计划已采用 B-01（origin）与 v0.5 主干；其余条目（召回质量评测、embedding 缓存、记忆检索超时降级等）**未纳入本轮**，开工前若要用需重新判定触发条件。

## 6. 停止条件（命中任一，先改契约，不加实现）

1. Profile 必须动态注册才能满足需求；
2. Web 内容防护做不到"不引入第二套内容管线"；
3. Skill 找不到真实消费者；
4. 缓存回落无法用事件指纹解释；
5. 任一 Profile 的基础任务成功率低于 v0.4 基线；
6. 为了 Profile 而需要让页面复制 Graph 真源或触发副作用。

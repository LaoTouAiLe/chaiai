# 优化待办（Backlog）

> **状态：备选资料，不决定下一步。当前只按 `V0_4_DEVELOPMENT_PLAN.md` 的 T38 → T39 → T40 → T37 朋友试用复验执行。**
> 本文件不授权任何实现，不改变 `ARCHITECTURE.md`、`PRODUCT_EVOLUTION_ROADMAP.md` 或各阶段计划文档中的任何条款。
> 落地任何一条前，仍必须写进对应阶段的计划文档；涉及 §15 或 §10 契约的改动，按 `ARCHITECTURE.md` §15.10 先修订契约再编码。
>
> 建立日期：2026-09-29。来源：一次与外部开源 Agent 项目（OpenClaw、OpenManus）的对照分析，
> 全部条目都落回了本项目的具体文件与行号。

## 0. 怎么读

**条目状态取值：**

| 状态 | 含义 |
|---|---|
| **建议做** | 已确认有价值、有明确落点，缺的只是排期 |
| **证据触发** | 先要看到**可重复的证据**才动手；写法与 `V0_4_DEVELOPMENT_PLAN.md:2017` 的 T36 进入条件一致 |
| **已裁定不做** | 附**重开条件**，避免同一个问题被反复讨论 |

**每条给出：** 依据（`文件:行号`）/ 阶段建议 / 触发条件 / 落点 / 验收 / 明确不做什么。

**与既有文档的关系：**

- 本文件不替代 `PRODUCT_EVOLUTION_ROADMAP.md`（方向）与各阶段计划（任务）。
- 本文件对 v0.5 及以后只给**阶段建议**，不构成任务拆分；v0.5 目前没有独立实施计划。
- 条目里的"阶段建议"在立计划时必须重新判定——触发条件可能已经不成立。

**修订记录：**

| 日期 | 变更 |
|---|---|
| 2026-09-29 | 建立。含 19 条：建议做 6 条、证据触发 4 条、已裁定不做 5 条、既有缺陷 3 条、工程化缺口 1 组。 |
| 2026-09-30 | 复核并更正 OpenManus 沙箱挂载的描述；当前执行顺序统一以 v0.4 计划为准。原有 B-01–B-19 的裁定不变。 |

---

## 1. 记忆可信度

**为什么这一节排最前：** 这是本项目记忆子系统的**真实缺口**（不是从外部抄来的优点）。当前架构的短板不在检索、不在存储，在**写入侧没有信任分级**。

### B-01 `origin` 信任分级（建议做）

**是什么**
给每条记忆候选与召回结果加一个来源可信级别，闭集四值：`owner`（用户明确表达）/ `agent`（模型从用户内容推导）/ `untrusted`（来自外部内容）/ `system`（脚手架）。并按级别决定**能否自动注入 Prompt**。

**依据（缺口证据）**

- `src/local_agent/memory/schemas.py:63-90`：`MemoryReference` 的字段里**没有来源可信级别**。
- `ARCHITECTURE.md` §4.3：`MemoryCandidate` 有 `memory_type` / `importance` / `confidence` / `source_event_ids`，同样**没有**来源级别。
- 后果：模型从对话里推导的内容，与用户亲口说的偏好，**同级且同等自动注入**。

**依据（为什么它不只是"想加的东西"）**

v0.5 已经承诺了它的验收条件：

- `PRODUCT_EVOLUTION_ROADMAP.md:319`（v0.5 主要工作）："增加 Web 搜索/读取能力；**外部内容标记为不可信上下文**。"
- `PRODUCT_EVOLUTION_ROADMAP.md:331`（v0.5 验收）："Web 内容中的提示注入不能修改系统安全策略或自动授权工具。"

**没有 `origin` 分级，这两句无法实现**——你无法表达"这段内容来自外部、不可信、不能自动注入"。因此它是 v0.5 的**前置任务**，不是并行需求。

**参考做法**（`E:\openclaw`，已核验）

- `docs/concepts/memory-architecture.md:71-84`：origin class 是**闭集**，且"**无法判定时永不默认成 `owner`**"——外部推导的判 `untrusted`，脚手架的判 `system`。
- `docs/concepts/memory-architecture.md:172-175`：`untrusted` / `system` 在**任何 prompt 构建之前**就被结构性排除，是"precondition, not a score penalty"——召回频次再高也不能把它推进长期记忆。
- `docs/concepts/memory-builtin.md:124-127`：**provenance 故意不放 Markdown**，"so recalled prose cannot rewrite its own trust classification"。
- `docs/concepts/memory-architecture.md:242-247`：只有 curated 层能**自动注入**，episodic 永不，"this restriction is a security property, not a tuning choice"。

**阶段建议**：v0.5 的**第一个任务**（v0.4 关闭之后）。

**落点**

- `src/local_agent/memory/schemas.py`：`MemoryReference` / `MemoryCandidate` 加字段
- `src/local_agent/reflection/memory_candidates.py`：候选生成时打标
- `src/local_agent/memory/writer.py`：写入时落标（中期 metadata + 长期列）
- `src/local_agent/memory/recall.py`：注入资格门控
- `src/local_agent/context/prompt_context.py`：`render_memory_section` 按级别处理
- `src/local_agent/persistence/migrations.py`：长期库加列（**前向迁移**）

**验收**

- `untrusted` 来源的记忆**在任何配置下都不自动进 Prompt**（加断言，不能只是文档约定）
- 缺 `origin` 的历史记录**默认按最保守级别处理**，不默认 `owner`
- 召回结果里 `origin` 可被页面展示（`ARCHITECTURE.md:1449` 的"来源类型"列已预留这个位置）

**明确不做**

- 不把 `origin` 存进 Markdown 或任何正文可写的位置（这正是 OpenClaw 刻意避免的）。
- 不引入"按 origin 加权"的软排序——先只做**能不能自动注入**这个二元门控。

### B-02 recall-loop 防护（建议做，与 B-01 同批）

**是什么**
被召回进过上下文的内容，**不得再次被抽取成新记忆**。OpenClaw 的原话是"一个事实被召回一百次还是一个事实"。

**依据**

- 通路在本项目**已经存在**：`recall_memory` 把结果注入 Prompt → 模型在反思时看到这些内容 → 可能把它们作为新候选写回。
- 参考：`E:/openclaw/docs/concepts/memory-architecture.md:94-97`（"Content that was injected into context from memory ... is structurally marked and never re-extracted as a new memory"）。

**阶段建议**：与 B-01 同一次改动（都是"给回来的内容打标记"）。

**落点**：`recall.py` 的返回结果打标记 → `reflection/memory_candidates.py` 过滤。

**验收**：注入过的记忆内容再走一轮完整反思，**候选数为 0**。

### B-03 session-kind gating（证据触发）

**是什么**
不同来源的会话（定时任务 / 心跳 / 子 Agent）**不产生持久记忆候选**。它们可以写任务产物，但其输出不具备晋升资格。

**依据**

- 参考：`E:/openclaw/docs/concepts/memory-architecture.md:91-93`。
- **本项目当前没有可验证的对象**：cron / heartbeat 要到 v0.6（后台任务）、"子 Agent"要到 v0.7（`PRODUCT_EVOLUTION_ROADMAP.md` §7）才存在。

**阶段建议**：不单独立项。作为 **v0.6 与 v0.7 的任务前置条件**记入那两阶段的计划文档。

**明确不做**：现在实现。按 `PRODUCT_EVOLUTION_ROADMAP.md:52`"复杂度按证据增长"，没有触发源的防线无法验证，只会变成死代码。

---

## 2. 记忆可观测与评测

### B-04 召回质量评测维度（建议做；**优先于第 3 节全部**）

**是什么**
在现有固定任务集里补一条**召回质量**维度：固定若干问法，记录命中率与相似度分布。

**依据（缺口）**

- `tests/test_agent_eval.py:198-448` 的 `CASES` 共 **18 条固定任务**，覆盖闲聊 / 读 / 改 / 审批 / 拒绝 / 恢复 / 越界 / 敏感文件 / 记忆写入 / RAG 开关 / 错误宣称 / 空转保护 / 思考模型 / 多步计划。
- 其中**与记忆相关的只有 1 条**：`tests/test_agent_eval.py:317` 的 `memory-chat-remember`，且它测的是**写入链路**，不是召回质量。
- 与检索相关的 4 条（`rag-on` / `rag-off` / `rag-natural-question` / `rag-natural-question-asks`）测的是**代码 RAG**，不是记忆召回。
- 结论：**18 条里没有一条在衡量"记忆召回得好不好"**。

**为什么它必须排在前面**

第 3 节的三条（Embedding 缓存 / MMR / 超时降级）全是"让召回更快 / 更多样 / 更健壮"，但**当前没有数据说明召回哪里差**。具体地：

- `src/local_agent/config.py:212-217` 自己写明：`min_score=0.53` 只来自 **5 组查询**的标定，"换模型或换成真实记忆库必须重新标定"。
- 没有召回质量的评测，就无法区分"漏召回"与"阈值标定不当"——而这两者的修法完全不同。

**阶段建议**：v0.5（与 `PRODUCT_EVOLUTION_ROADMAP.md:326-328` 的"三个 Profile 固定任务集"一起做）。

**验收**：能回答"某个已知存在的记忆，在 N 种不同问法下被召回几次、相似度是多少"。有了这个数，第 3 节才能按证据排序。

### B-05 `memory export` 只读视图（建议做，**仅 CLI**）

**是什么**
`python -m local_agent.cli memory export` 把当前可见作用域内的记忆渲染成 Markdown。

**依据（为什么值得做）**

- `ARCHITECTURE.md:1012-1014`（§10.3）已要求"遗忘与人工操作必须有**用户可触达的入口**"，目前只有 `list|delete|forget`，缺"看全文"。
- 它是"记忆黑盒"这个痛点的最低成本解法。

**依据（为什么只能做 CLI）**

页面版在 v0.4 被**三处**明确禁止：

| 位置 | 原文 |
|---|---|
| `ARCHITECTURE.md` §15.8（`:1449`） | 记忆/RAG 一行"明确不展示：**未经脱敏的长期记忆正文**、向量或内部评分细节" |
| `V0_4_DEVELOPMENT_PLAN.md:2061`（§9） | "可见执行过程"失败判据含"泄露隐藏推理或**记忆正文**" |
| `V0_4_DEVELOPMENT_PLAN.md:2096`（§11） | "不泄露隐藏思维链、完整 Prompt、密钥或**未经脱敏的记忆正文**" |

**阶段建议**：CLI 形式可在 v0.4 的 T36 判定空档期做（零契约影响）；页面形式要等 §15.8 修订。

**落点**：`src/local_agent/cli.py` + `src/local_agent/memory/recall.py`（复用 `visible_scope_keys()`，**不要自己拼一套作用域**——`src/local_agent/scope.py:91-93` 明确警告过"命令行看得到、模型召回不到"）。

**验收**：导出结果的作用域可见性与 `memory list`、与模型召回**完全一致**。

---

## 3. 检索性能与质量（全部为证据触发）

**共同前置：B-04 必须先做。** 以下三条在没有召回质量数据之前都是猜测。

### B-06 Embedding 结果缓存（证据触发）

**是什么**
在进程内缓存"文本 → 向量"的结果（键含 provider + 模型 + 文本 hash）。

**依据**

- 本项目**没有**该缓存：`src/local_agent/llm/embedding.py` 里的 "cache" 全部指**模型权重缓存**（`cache_dir` / `HF_HOME` / `HF_HUB_CACHE` / `HF_XET_CACHE`），不是推理结果复用。
- 本项目的 embedding 是**本机 ONNX CPU 推理**（FastEmbed，`onnx_local`），是召回链路上真正耗时的一环（HNSW 检索在本地规模可忽略）。

**触发条件**（任一成立）

1. v0.5 的固定任务集显著扩大（`PRODUCT_EVOLUTION_ROADMAP.md:326-328` 要求三个 Profile 各一套），批量跑评测时重复计算变明显；
2. 出现"索引重建 / 记忆迁移"这类批量场景。

**落点**：`src/local_agent/llm/embedding.py`（适配器层已具备按 provider/模型区分的能力）。

**明确不做**

- **不做跨进程持久缓存**。那会引入"模型换了、缓存没失效"的新一类兼容性问题，而 `ARCHITECTURE.md` §10.2 的 collection 兼容性校验正是为防这类事。进程内 LRU + 上限足够。
- **口径上不要和 v0.4 的"缓存优化"混为一谈**。T34–T36 做的是**供应商侧 prompt cache（KV cache）**；本条是**本机推理结果复用**。两者同名不同物。

**验收**：同一批评测的第二次运行，embedding 调用次数显著下降，且召回结果与未缓存时**逐条一致**。

### B-07 MMR 召回多样性（证据触发）

**是什么**
对召回候选做最大边际相关性去重，避免返回多条几乎相同的记忆。

**依据**

- 本项目**没有**：全 `src/` 搜 `mmr|MMR|diversit|hybrid|rerank|bm25|fts5` **零命中**（`recall.py` 是纯向量召回）。
- **有成本，不是零改动**：`src/local_agent/memory/medium_term.py:169-174` 的查询 `include=["metadatas", "documents", "distances"]` **不含 `embeddings`**；MMR 需要候选之间的相似度，而 Chroma 只返回候选到 query 的距离，所以**必须先改 `include` 把候选向量取回来**。
- 传输量取舍：`src/local_agent/memory/recall.py:74,77` 的 `OVERFETCH_FACTOR=4` / `MAX_CANDIDATES=200` 意味着一次最多回 200 条候选；取回 200 个向量不便宜。

**触发条件**

出现**可重复的重复召回**证据：同一轮召回里有多条内容高度相近的记忆（例如同一结论被写了多次）。

**阶段建议**：v0.6（后台任务会让记忆条数与召回频次同时上升）。

**落点**：`medium_term.py`（`include` 加 `embeddings`）+ `recall.py`（`_top_references` 之后加一步去重）。

**验收**：重复召回率下降，且**Top-1 不被削弱**（去重不能把最相关的那条挤掉）。

### B-08 召回超时降级返回部分结果（证据触发）

**是什么**
召回超时或部分失败时，先返回**已经就绪的部分结果并明确标注 partial**，而不是整体失败。

**依据**

- 参考：`E:/openclaw/docs/concepts/memory-builtin.md:30-34`（语义检索触到 15 秒工具超时后，先返回已就绪的关键词命中 + partial-result 警告）。
- **本项目当前没有触发场景**：召回是在图节点里同步执行的，默认 embedding 是**本机 ONNX（无网络）**，没有可超时的远程调用。

**触发条件**（任一成立）

1. 默认启用 `openai_compatible` embedding provider（`ARCHITECTURE.md` §10.2 已支持，当前非默认）；
2. 召回变成**模型可调用的工具**（`PRODUCT_EVOLUTION_ROADMAP.md` §5 的能力模型里 `memory → recall/write/forget` 已被列为一项 capability，v0.5 做 Capability Registry 时可能这么落地）。

**明确不做**：现在做。没有触发源的降级逻辑不可验证。

---

## 4. 已裁定不做（附重开条件）

**本节的目的：避免同一批问题被反复讨论。** 每条都给重开条件，条件成立时可以推翻。

### B-09 记忆正文改为文件真源（Markdown 是主数据源）

**裁定：不做。**

**理由**

1. **覆盖写会撞 §6.4**：`ARCHITECTURE.md` §6.4 规定"覆盖已有文件默认需要审批"，而记忆写入现在是**免审批**的自动链路（`src/local_agent/memory/writer.py` 全文无审批逻辑）。要让记忆文件成为真源，要么每次写入弹审批，要么给记忆文件开一条"不受文件策略管"的例外。
2. **模型会绕过反思链路**：文件在工作区内 → 模型可用 `edit_file` / `write_file` 直接改记忆，**绕过 §10.4**"所有 `memory_write_intent` 必须经 `reflection_node` 验证"。参考项目能接受这一点是因为它把工作区文件视为操作者信任边界内（`E:/openclaw/docs/concepts/memory-architecture.md:110-115`）；本项目把模型当作需要证据与审批的不可信执行者，**信任假设相反**。
3. **`scope` / `origin` 无处安放**：文件位置天然表达作用域，但 §10.2 要求的是**精确硬过滤**；而把作用域写进正文行内注释，就等于让模型改自己的作用域。
4. **会拆掉可见性唯一真源**：`src/local_agent/scope.py:86-99` 的 `visible_scope_keys()` 是四处共用的唯一真源，文件布局会引入第二套映射。
5. **`global` 作用域在工作区内无法表达**，必须放工作区外，又回到第 1 条。

**代价（如实记录）**：放弃"记忆可读可编辑、可 git 携带、索引可重建"这三项治理收益。

**重开条件**（任一成立）

1. 真的出现"记忆改错且看不见、改不了"的实际阻碍；
2. 需要记忆随项目一起做版本管理；
3. 把"记忆可读可编辑"作为对朋友的**产品卖点**（定位从学习项目变为产品，判断标准随之改变）；
4. `run_shell` 那个绕过口子被解决（见下）。

**未解决的技术障碍（写在这里防止实现时才发现）**：即使做了文件化，`run_shell` 仍是绕过口子——
`Set-Content MEMORY.md` 是合法命令，而 `security/shell_guard.py` 按可执行名 + 参数结构判定，
**无法判断 argv 里的路径是不是记忆文件**。要么在 shell 判定里加"argv 出现记忆文件路径 → 拒绝"，
要么承认只能防住文件工具。

### B-10 读取时切分（400 token / 80 重叠 chunk）

**裁定：不做。**

**理由**：本项目的模型是**写入时定粒度**——`src/local_agent/memory/medium_term.py:136-141` 一条记忆记录 = 一个 `documents` 元素 = 一个向量，而候选本身由反思产出，天然是原子单位（一条摘要 / 一个任务片段 / 一条偏好）。改成读取时切分要放弃"不切断语义"这个优点，并额外引入 overlap 去重与排序补偿。

**重开条件**：出现"单条记忆过长、整条占满召回预算"的真实问题。

### B-11 plugin slot / 独占插槽

**裁定：不做（作为插件体系的一部分）。**

**理由**：`PRODUCT_EVOLUTION_ROADMAP.md` §2 明确"不采用一切皆插件"；v0.4 也不做 Profile / 能力注册。插槽没有宿主。

**保留的部分**：**"独占"这个语义**值得保留，但落法是**一条断言**而不是机制——"同一时刻只有一个记忆后端可写"（启动校验 + 一条测试）。本项目事实上已经是独占的（`MediumTermStore` + `LongTermStore` 两个 store，同一个 `writer.py` 入口）。

**一个需要澄清的误读**：参考项目的 memory slot 是"**同一时刻只激活一个记忆引擎实现**"（memory-core / honcho / lancedb 三选一，`E:/openclaw/docs/concepts/memory.md:176-191`），**不是**"一次只加载一份记忆文件"，也不限制召回条数或文件数。所以"单插槽会削弱多记忆并发检索"这个担心不成立。

### B-12 dreaming 式后台晋升（把 curation 移出回复链路）

**裁定：不做。**

**理由**：本项目的记忆写入是**同步在回复链路里、由反思节点把关**的（§10.4），而 `PRODUCT_EVOLUTION_ROADMAP.md:283` 明确 v0.4 不做后台任务、v0.6 才做。照抄只有两条路：越过 v0.6 引入调度器，或让记忆写入脱离反思节点（违反 §10.4）。

**保留的部分**："两阶段晋升"思想可以用同步版本表达——**本项目事实上已经是这个形状**（反思产出候选 → 写入）。真正缺的是**晋升门**，不是后台。

**重开条件**：v0.6 引入 Job Runtime 之后，如果出现"写入质量明显受对话延迟限制"的证据。

### B-13 长期 `facts` / `task_conclusions` 文件化为真源

**裁定：不做。** 长期记忆可以只做**只读导出**（并入 B-05）。

**理由**：长期记忆是**写入时判定的结构化数据**，不是从文本抽取的：

- `src/local_agent/memory/long_term.py:14-15`：`replaced` / `conflicted` 取决于**置信度高低比较**——无法从 Markdown 正文反推；
- `src/local_agent/memory/long_term.py:11-13`：`superseded` / `conflicted` 行是**版本历史与审计**，§10.3 要求"历史行保留用于审计"；
- `fact.value_json` / `task_conclusion.evidence_json` 是结构化值与证据引用。

文件化会丢掉：置信度来源、冲突裁决链、版本历史。

**一个来自参考项目的旁证**：它把最"结构化"的那类记忆（standing intents）**故意留在 SQLite**，理由是"把意图当散文存进记忆文件是现有设计里最不可靠的一种"（`E:/openclaw/docs/concepts/memory-architecture.md:339-344`）。

---

## 5. 顺手发现的既有缺陷（**只报告，未修改**）

这三条不属于任何优化项，是本次分析过程中读代码时发现的既有问题。

### B-14 `write_on` 是死配置

- `src/local_agent/config.py:220` 声明了 `write_on: list[str]`。
- **`src/` 与 `tests/` 内零消费者**（`tests/` 里唯一命中是 `test_upsert_fact_does_not_silently_overwrite_on_conflict` 的字符串巧合）。
- 而 `config.yaml.example:71-74` 还给它配了三个值（`task_completed` / `important_fact_changed` / `explicit_user_preference`）。

**需要你判定**：是实现没接上（则写入触发条件实际不可配），还是废弃键没清（则样例应删）。

### B-15 样例配置的注释已过期

- `config.yaml.example:62`：`enabled: false  # v0.1a / v0.1b 保持关闭，节点透传`
- 实际：现在是 v0.4，中期记忆从 v0.2（T14B）起就是可用能力。这句注释会让新读者以为它还没实现。

### B-16 `min_score` 标定样本过小

- `src/local_agent/config.py:212-217` 自己写明：0.53 只来自 **5 组查询**的标定，"换模型或换成真实记忆库必须重新标定"。
- 影响：召回是否"够用"目前**没有数据支撑**。这与 B-04 是同一个缺口的两面。

---

## 6. 工程化缺口（来自外部对照，独立于记忆）

> **证据强度说明**：本节的**本项目侧**依据是我直接读过的；**参考项目侧**依据来自对 `E:\openclaw` 的探索，落地前需要复核。按项目纪律，这些属于"报告"，不是"建议立即做"。

### B-17 CI 与架构边界守卫（建议做）

**缺口（本项目侧，已确认）**：仓库根**没有 `.github/`**，也没有 `Dockerfile`。850 个测试用例只靠本地跑。

**参考做法**：参考项目有 105+ 个 workflow，且 CodeQL **按信任边界分区**配置，lint 里有一批自研的**架构守卫规则**（禁止 chained assertion、禁止 widen-then-assert、pairing account scope 守卫等），配置在 `config/oxlint/boundary-guards.json`。

**为什么对本项目有价值**：`ARCHITECTURE.md` §15.1 已经写明依赖方向契约——"LangGraph 不导入 api、frontend 或应用层 DTO"、"application 不导入 Streamlit"、"frontend 不知道 AgentState、节点名、LangChain 消息类或 LangGraph `Command`"。**这些目前没有任何机械手段守着**，只能靠 review。

**落点**：可以先只做一件事——一条 import 方向的 lint 规则 + 把现有 `uv run pytest` / `ruff` / `mypy` 接进 CI。这是全清单里成本最低、收益最直接的一条。

### B-18 审批机制的三个可选增强（证据触发）

| 增强 | 参考做法 | 本项目的现状 |
|---|---|---|
| `allow-always` 粒度（会话内免审白名单） | 审批记录支持 `allow-once` / `allow-always`，绑定精确 command/cwd/env | 本项目有动作指纹 + 三出口审批，但**没有** `allow-always` 粒度 |
| "无法唯一定位操作数时**拒绝**而不是放行" | 写成显式规则并有测试 | 与本项目 fail-closed 精神一致，但**未见显式规则** |
| 危险配置**显式标记 + 审计**（而非直接拒绝启动） | `dangerous*` 前缀的配置计入 `config.insecure_or_dangerous_flags` 并进审计 | 本项目是"配置想关强制审批 → **直接启动失败**"（`src/local_agent/config.py:272-279`） |

**说明**：第三条是**设计取舍**，不是缺陷。本项目更保守；参考项目允许关但要求显式标记与审计。要不要改由你定。

### B-19 Skill 层（建议做，但需要先立项论证）

**是什么**
"目录 + `SKILL.md`（frontmatter）→ 以目录形式注入 prompt，模型按需读文件"的渐进式披露机制。

**为什么它可能值得**：它和本项目**已有的 AGENTS.md 机制**是同源问题（`ARCHITECTURE.md` §7.1 的向上扫描 + 逐级合并 + 优先级），参考做法只是更完整：多了作用域优先级链、per-agent allowlist、依赖门控（`requires.bins/env`），以及"**给人用还是给模型用**"两个开关（`user-invocable` / `disable-model-invocation`，见 `E:/openclaw/docs/tools/creating-skills.md:113-152`）。

**为什么需要先论证**：它**不需要 plugin runtime** 就能实现，但会与本项目的 AGENTS 加载、RAG 作用域、Prompt 预算三处交互。按 §2 的"复杂度按证据增长"，应该先确认"AGENTS.md 不够用"的具体场景。

---

## 7. 未核实清单

**写在这里，防止把判断当事实用。**

| 编号 | 未核实的点 | 影响 |
|---|---|---|
| U-1 | `src/local_agent/memory/recall.py` 我只读了前 160 行（docstring + `recall_memory` + `_to_reference`），**未读** `_is_usable` / `_top_references` / `recall_long_term` | B-07 的"无去重逻辑"结论来自全 `src/` 的关键词零命中，不是逐行确认 |
| U-2 | `src/local_agent/memory/medium_term.py` 只读了前 95 行 + `upsert` / `query` 片段 | "一条记录一个向量"是从 `upsert` 实参直接读出；"写入前是否对超长 `content` 截断"未核实 |
| U-3 | `src/local_agent/llm/embedding.py` 未通读 | B-06 的落点判断（"适配器已按 provider/模型区分"）未在代码里确认 |
| U-4 | 未实测召回延迟 | "HNSW 检索可忽略、瓶颈在 ONNX 推理"是基于实现的推断 |
| U-5 | 参考项目侧：MMR 是否默认生效、检索是否有硬分数下限 | 文档写法留了余地（我读的是 `docs/concepts/memory-builtin.md`，未读实现） |
| U-6 | 参考项目侧：`tools.exec` / 审批实现的细节 | 来自探索，未逐条复核 |
| U-7 | `v0.5` 独立计划文档尚不存在，只有 `PRODUCT_EVOLUTION_ROADMAP.md` §7 的方向 | 本文所有阶段建议都须等 v0.4 的 T38/T39/T40/朋友试用完成后重新判定 |
| U-8 | 未通读 `DEVELOPMENT_PLAN.md`（v0.1–v0.3 实施记录） | 可能漏掉历史任务里对某一项已有的规划或明确排除 |

---

## 附录 A：本次分析中的一处更正

| 说法 | 更正 |
|---|---|
| 早前判断"固定评测只有 2 个用例" | 那是按 `def test_` 计数的结果。实际是**2 个测试函数 + 18 条固定任务用例**（`tests/test_agent_eval.py:198-448` 的 `CASES`，数据驱动）。评测集比最初判断的厚；真正的缺口是"**18 条里没有一条衡量记忆召回质量**"，见 B-04。 |

## 附录 B：对照分析中值得保留的观察

**OpenClaw（`E:\openclaw`）**

| 观察 | 依据 |
|---|---|
| 记忆 = **普通文件 + 一个 SQLite 索引**；正文在文件、信任元数据只在库里，索引可重建 | `docs/concepts/memory-architecture.md:11`；`docs/concepts/memory-builtin.md:124-127,210-213` |
| `MEMORY.md` / `USER.md` / `memory/*.md` 是**硬编码常量**，且 `WORKSPACE_BOOTSTRAP_FILENAMES` 是"单一真源"；名字会出现在提示词里，但"什么时候写、什么格式"是通过 **AGENTS.md 模板**教会的 | `src/memory/root-memory-files.ts:7-9`；`src/agents/workspace.ts:242-256`；`src/agents/system-prompt.ts:89-97,211-236`；`docs/reference/templates/AGENTS.md:26-58` |
| 稳定前缀与动态上下文分离，且"**只有 compaction 可以重写历史**" | `docs/concepts/system-prompt.md:22-23,51`；`AGENTS.md:14` |
| 检索层是**混合**（FTS5 BM25 + 向量 + MMR + CJK trigram），`sqlite-vec` 是默认开的可选加速、失败自动降级 | `docs/concepts/memory-builtin.md:17-28,115-122`；`package.json:2283`；`docs/reference/memory-config.md:604,609` |

**OpenManus（`E:\pylianxi\OpenManus`）**

| 观察 | 依据 |
|---|---|
| **无任何向量化与文本切分**：全 `app/` 搜 `embedding\|vector\|faiss\|chroma\|sentence_transformers` 零命中，所有 `chunk` 命中都是 socket / 文件流 / SSE 块 | — |
| "记忆"只有一张 100 条的消息列表，超限丢最老的；进程内、无持久化、无检索 | `app/schema.py:159-168` |
| 上下文管理只有"100 条滚动窗口"：token 检查默认**永远返回 True**（`max_input_tokens` 默认 `None`） | `app/llm.py:249-254`；`app/config.py:24-27` |
| 记忆之外可参考的是**进程内连续 bash 会话**与**显式 Docker 挂载/网络配置**；本轮复核 `app/sandbox/` 未发现所谓挂载 deny-list，不能把它当成已实现的防护 | `app/tool/bash.py:17-35,116-150`；`app/sandbox/core/sandbox.py:65-66,105-121` |

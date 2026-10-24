# v1.0 执行计划：稳定自用版本与公开演示

> 追加授权（2026-10-18）：用户接受先验证 Windows 原生 IPC 兼容方案，再实现构建、分发及安全回归。
> 已完成：x64/x86 子进程传播、二进制完整性、npm/esbuild/Vite 实测及 wheel/EXE 分发验收。
> 完整后端 2480 passed / 8 skipped / 0 failed，固定工程评测 402 passed；正式服务已按原配置重启。
> 文件、原始证据与剩余兼容范围见 [原生 IPC 记录](../audits/2026-10-19-windows-ipc.md)。
> 2026-10-23 用户确认增量实现受控终端（`ARCHITECTURE.md` §23），撤销此前「不实现全屏终端」限制；不实现重启资源恢复或记忆策略修改，不放开外部目录写入、不自动提权重试。

> 用户授权（2026-10-18）：校准全库旧断言和评测脚本；完整收集失败，逐项区分策略变更与真实缺陷。
> 保留安全/证据/副作用负例，不恢复已取消限制，不通过全局旧协议夹具或删除断言伪造通过。
> 已完成：完整后端 2461 passed / 8 skipped / 0 failed，固定工程评测 402 passed；Ruff 与 mypy 通过。
> 文件、跳过原因与现行记忆限制见 [校准记录](../audits/2026-10-18-assertion-calibration.md)。

> 继续授权（2026-10-18）：收口剩余两项审批测试和三条类型诊断。审批用例显式选择审批配置，
> 缺少冻结时钟/无效轨迹数字如实回退；不改默认权限、模型循环或存储结构。

> 继续授权（2026-10-18）：定位并修复小窗口上下文压力回归；已确认工具定义估算重复叠加说明。
> 验证估算与实际绑定结构一致、压力压缩/持久重放及正常窗口行为；不修改能力开关或压缩阈值。

> 继续授权（2026-10-18）：修复大文件前缀读取及检索截断后无法续查；新增字符续读、查询游标、
> 模型可调搜索预算和跳过原因。先验证跨页完整性、版本变化、取消与路径边界；本轮不调整记忆策略。

> 继续授权（2026-10-18）：补齐同批独立只读工具并行，保留写入/控制顺序边界，验证取消、
> 结果顺序及持久化；本轮不调整文件预算和记忆策略。见
> [并行验证记录](../audits/2026-10-18-tool-parallelism.md)。

> 追加（2026-10-18 用户授权）：补齐浏览器操作、持久 stdio 会话，以及公开下载/系统查询宿主适配。
> 不扩展为桌面控制或全屏 PTY，不撤销 Shell 外部写入授权边界。两轮真实模型验收和当前回归结果见
> [本次实现记录](../audits/2026-10-18-interactive-runtime.md)，不将其计作完整 v1.0 发布验收。

> 状态：2026-10-12 **本机工程源码已收口，可交付候选源码**。执行文档先建立，T84–T89 本机工程内容完成，T90 本机完整阶段门通过（§5.3）。**完整 v1.0 验收仍开放**：新机器/新分发/录屏未验证，真实语义质量与 T43 压力仍暂停；不宣称正式发布。
> 追加（2026-10-13）：封印之后又做了一轮**界面交互细节收口**（会话按首个提问命名、侧栏折叠、错误提示可收起、轨迹/地图布局）；改动面、门槛与重新封印见 **§5.4**，证据 `local-agent-web/eval/v10-interaction-20261013/`。
> 依据：`docs/planning/FUTURE_WORK.md:459` 的六项工作及五项验收；前置为 v0.8/v0.9 工程源码收口（`V0_9_DEVELOPMENT_PLAN.md` §7）。既有未提交改动全部保留。
> 范围：本机单用户、单进程、单 worker；Windows 为本机已验证平台。工程源码验收与新机器/新分发产物验收分别记账。

## 1. 本轮范围与停止条件

做：兼容基线、生命周期指南、固定工程评测、启动诊断、隐私与威胁说明、可复现演示和最终阶段门。

这次不做：新增 Agent/工具能力、OS 沙箱、资源上限、公网/多用户、依赖升级、付费模型调用、回答语义质量审查、T43 真实压力验收、自动删除用户数据/凭据/模型缓存、生成 ZIP、提交 Git 或发布。

上述暂停项不因 v1.0 自动通过。离线替身评测只能证明编排与机械不变量，不能证明真实模型回答质量。新机器安装首个任务必须有实际机器记录；没有记录时 v1.0 完整验收保持开放。若需要新分发目录或真实模型补验，先整理可审查的材料，单独明确范围。

## 2. 已核实的实现与缺口

| 领域 | 已有实现（复用） | 本轮补齐 |
| --- | --- | --- |
| 配置 | `config.py` 的 AppConfig 严格校验、初始设置模式、运行期设置覆盖 | 默认配置与字段结构的可复现基线、变更检查与兼容说明 |
| 数据库 | `persistence/migrations.py`：聊天 10 / 长期 3 / 设置 3；T79 自检与 SQLite backup | 明确结构版本独立于产品版本；停服务、全数据保留、恢复和降级操作说明 |
| API/事件 | `api/app.py` API 标识 0.4.0；`application/models.py` schema_version=1；受控 OpenAPI 和前端生成类型 | 冻结当前结构指纹，不为了产品版本强行更改传输版本 |
| 启动 | `api/__main__.py` 官方入口；`startup.py` 打包兜底；`app_paths.py` 分离资源与数据 | 无模型调用的独立诊断入口、可照做的修复提示 |
| 分发 | `scripts/build_windows_package.py`、`scripts/chaiai.spec`、T40 旧包及旧试用记录 | 纠正旧使用说明；旧包不代表 v1.0 新代码验收 |
| 评测 | `tests/test_agent_eval.py`、`tests/test_workflow_eval.py` 和既有安全/恢复测试 | 固定范围、明确阈值、保存版本与结构指纹；不拿全部任务完成率代替预期行为符合率 |
| 说明 | 两级 README、安全平台矩阵和既有隐私条目 | 修正“不能取消”“只访问模型接口”等过时声明；列出实际联网和本地持久化边界 |

产品包 `__version__` / pyproject 当前 0.1.0，前端包 0.4.0；这些是现状，不视为 v1.0 已发布证据。最终受支持配置依赖冻结后的 schema 与默认值，当前 YAML 没有独立版本字段，不擅自增加必填字段。

## 3. 任务顺序与验收

### T84｜兼容契约基线

- 先在 `ARCHITECTURE.md` §21.1 冻结基线口径：配置结构/默认值、数据库版本、API 与事件结构；产品目标版本与现存协议版本分开。
- 新增只读检查脚本及受控 JSON 基线，导出不读取 config.yaml、.env、正式库、系统凭据或模型，不装配运行时。
- 当前 API/SSE 的受控 OpenAPI 作为同一契约来源，不再创建手写 DTO。
- 验收：重复导出字节一致；配置/API/事件/数据库版本漂移退出非零；丢失或损坏基线失败且不覆盖；既有 OpenAPI 与前端漂移检查通过。
- 状态：已完成。新增 `release_contracts.py`、检查脚本、受控 JSON 和 12 项专项回归；静态和 OpenAPI/前端漂移检查通过。

### T85｜维护、升级与卸载指南

- 新增同一份生命周期指南：程序资源、三类 SQLite、Chroma、配置、凭据、检索结果和模型缓存分别说明。
- 复用 T79；聊天库自动识别源锁，设置/长期库要求先手动停服务。三库分开 backup 不声称跨库在线一致快照；配置/向量/凭据另行保留。
- 升级保留原程序和数据，维护副本上验证；降级不把旧程序直接指向更高结构版本。卸载仅给手动步骤，不自动删除任何数据。
- 验收：指南命令参数与 CLI --help 一致；现有备份恢复续聊测试通过；所有破坏性步骤均明确用户选择；纠正旧分发说明。
- 状态：已完成。`docs/release/LIFECYCLE.md` 覆盖各类数据、维护/回退/卸载和缓存；README 与包内旧说明已同步。CLI help 与实际备份恢复回归通过，本轮不操作正式数据或 OS Key。

### T86｜固定工程评测与版本基线

- 固定基础任务、安全/审批、取消恢复、工作流和数据库维护用例入口，复用既有实际图/实际工具测试，不创建第二套编排。
- 阈值：固定用例预期行为符合率 100%，已覆盖用例的错误宣称/重复副作用/秘密泄漏为 0；正确拒绝/追问/失败不要求标成 completed。所有统计只针对固定集合。
- 保存源码/兼容基线指纹、命令、退出码、跳过原因与替身口径；真实质量不写达标。
- 验收：连续同题结果可对照；任一断言失败则整组失败；实际用例身份写进报告；工程用例不触发付费模型。
- 状态：已完成。固定 16 个测试文件，连续两轮均 **390 passed / 0 skipped / 0 failed**，Python 源码指纹与实际 case 身份/状态一致。JUnit 解析器对空集合/跳过/错误/无效报告均拒绝通过。

### T87｜启动与环境诊断

- 保留官方启动入口与打包浏览器自动打开行为；新增显式只读诊断，检查配置合法性、静态产物、现有 SQLite 可读/quick_check/结构版本、可选依赖与工具可见性。
- 不绑定端口、不加载 .env/系统 Key、不迁移/创建数据库、不加载/下载模型、不调用远端；原始异常正文不进入可分享摘要。缺聊天连接只给配置提示，不声称服务无法启动。
- 诊断仅提供提示，不替用户安装依赖、改配置、关闭进程或清空缓存。
- 验收：缺配置初始设置模式可诊断；坏配置/坏库/高结构版本/缺静态产物明确非零或警告；无配置路径不存在时不创建目录/库；注入秘密的错误不回显。
- 状态：已完成。新增 `diagnostics.py` 与独立脚本，官方入口增加 `--diagnose`。诊断/启动路径/备份恢复专项共 **29 passed**；无配置不创建库、未来结构/损坏库明确失败、摘要不回显秘密的负例通过。

### T88｜威胁模型、隐私与能力声明

- 写明可信本机用户与不可信仓库/网页/技能/模型输出的边界；代码审批及路径检查的作用和限制；Shell 仍拥有 OS 用户权限。
- 写明模型、远端 Embedding、显式网页访问、本地模型下载、Shell 自行联网等可能出口，秘密和原文的持久化位置，以及诊断/日志分享注意事项。
- 复用实际能力面区分离线/联网/需要 Key；不靠静态 UI 标签猜测某模型一定离线。
- 验收：所有肯定能力声明能映射到契约或测试；README 与包内说明一致；未隔离/未验证如实记录。
- 状态：已完成。`docs/release/PRIVACY_AND_LIMITS.md` 描述信任、原文持久化、实际网络出口和未隔离能力；复用现有服务端能力面，不新增猜测模型离线状态的 UI 标签。

### T89｜演示任务与验收记录模板

- 新增固定录屏脚本：初始设置、读文件、审批后修改、取消、重启恢复、工作流和数据视图；使用隔离工作区和虚构数据。
- 演示材料引用既有架构图/证据，不提交模型密钥、真实会话或伪造录屏。
- 新机器验收模板记录 OS、运行形态、版本/产物指纹、首个任务和维护结果；当前不填“通过”。
- 验收：脚本中每步入口确实存在，替身演示注明替身；没有录制/没有新机器时明确未验证。
- 状态：材料已完成。`docs/release/DEMO_AND_ACCEPTANCE.md` 提供固定脚本、真实入口和新机器记录模板，6 份相关文档的本地链接检查无缺失。**未录制、未在新机器执行**，模板不填通过。

### T90｜最终工程阶段门与完整交付判定

- 冻结源码后运行完整 pytest、ruff、mypy、OpenAPI/前端类型漂移、前端 typecheck/lint/单测，以及默认浏览器和五套真服务浏览器；沿用 v0.9 的隔离临时目录/离线替身方法。
- 记录新机器验收和真实质量暂停项；版本号调整、打包和公开发布不由绿色本机测试自动授权。
- 工程阶段门通过可交付工程源码；完整 v1.0 验收要求路线图五条条件逐条有证据，缺新机器证据不能宣称全部完成。
- 状态：本机工程阶段门已通过，完整验收仍开放。结果、跳过原因和复现方式见 §5.3；没有新分发或新机器证据。

## 4. 验证与记录规则

Python 命令在 `local-agent-web/` 执行，使用已有 `.venv/Scripts/python.exe`；前端命令在 `frontend/` 执行。不更新依赖，不使用正式数据库，pytest basetemp 为仓库外新目录。

每个任务完成后在本文件补：实际文件、验证命令/数量/退出码、未验证范围。最终证据保存在被忽略的 `local-agent-web/eval/v10-delivery-20261012/`，不创建 ZIP。历史测试数只作为前置证据，不抄成当前结果。

## 5. 实施记录

### 5.1 实际修改

- 根目录：本执行文档、`ARCHITECTURE.md` §21.1–§21.3、README 当前阶段入口。
- `src/local_agent/release_contracts.py` / `scripts/check_release_contracts.py` / `docs/release/compatibility-baseline.json`：可复现的候选契约指纹与只读检查。
- `src/local_agent/diagnostics.py` / `scripts/diagnose_environment.py` / `src/local_agent/api/__main__.py`：显式诊断在 dotenv/runtime 装配前退出。
- `src/local_agent/release_evaluation.py` / `scripts/run_release_evaluation.py`：固定工程集合、实际 JUnit、源码指纹和新报告目录。
- `tests/test_release_contracts.py` / `test_diagnostics.py` / `test_release_evaluation.py`：新增真实 CLI、库、不泄漏/不创建及失败报告负例。
- `docs/release/LIFECYCLE.md` / `PRIVACY_AND_LIMITS.md` / `DEMO_AND_ACCEPTANCE.md`：复用现有功能的维护、隐私与演示材料。
- `local-agent-web/README.md` / `packaging/使用说明.txt`：更新入口、T40 旧包状态、取消和实际联网边界。
- `frontend/e2e/approval-live.spec.ts`：验收脚本等待新会话创建后才填写草稿，审批实现未改。

### 5.2 已执行验证

- 兼容专项：12 passed；诊断/启动/数据库专项：29 passed。
- 固定工程评测两轮：各 390 passed，0 skipped，pytest 退出 0；case 身份/状态/兼容基线/Python 源码指纹完全一致。
- 前端：41 文件 / 381 passed；typecheck、lint、API 漂移通过。审批脚本修改后再次 typecheck/lint 通过。
- 浏览器：默认 180 passed / 3 skipped（既有 640 窄屏整页截图尺寸条件）；live 3、streaming 1、variants 1、approval 2、workflow 5，五套真服务共 12 passed，全部替身模型。
- 静态：ruff 全目录通过，mypy src 133 个文件通过，OpenAPI 和兼容基线只读检查通过；维护/诊断 --help 通过。
- 文档：6 份相关 Markdown 的本地文件链接无缺失；命令均使用存在的入口和参数。

**审批首次验收失败及修正**：首次 serial browser 的拒绝用例在发送前超时；失败页显示新会话空草稿/禁用发送。
`submit` helper 只等待点击返回，旧会话 input 仍存在时提前 fill，异步新会话切换使当前草稿为空。
加实际 POST 创建响应与新会话空对话等待，不加 sleep、不放宽断言；新服务复跑两项审批全部通过（16.3 秒）。
保留初始失败日志；仅此 e2e 文件发生改变，383 个冻结文件的清单刷新后再开始完整后端；其余浏览器消费者与产品源码无变化。

证据：`local-agent-web/eval/v10-delivery-20261012/`，固定两轮报告、对照、初始/最终浏览器退出码、前端日志、静态日志、初始/最终冻结清单；trace 关闭，未生成 ZIP。
### 5.3 最终工程阶段门

冻结源码后完整后端：**1887 passed / 9 skipped / 0 failed / 38 warnings，514.90 秒**，pytest 退出 0。
383 个冻结文件在所有验收结束后无变化。前端 381、默认浏览器 180/3、五套真服务 12、固定评测
390×2、静态/漂移检查全部通过。`delivery-seal.json` 明确 engineering=true、full_v1_acceptance=false。

后端 9 项跳过：付费精度/真实模型冒烟 2；已安装可选依赖的缺依赖负例 1；POSIX 大小写/权限位 2；
真实 OS 凭据读写默认关闭 1；环境不能创建 symlink 2；本卷没有 8.3 短名 1。
默认浏览器 3 项跳过为已有 640 窄屏整页截图的尺寸条件。38 条 warning 为已有 Starlette/Chroma
弃用提示，不升级依赖或把跳过算成通过。原始原因保存在完整 pytest `-rs` 日志。

独立实跑官方/脚本两个诊断 CLI：输出一致、隔离 fixture 文件集合未改变、.env 哨兵未回显；
记录在 `diagnosis-cli.json`。新文件链接无缺失；本轮修改的受控文件 `git diff --check` 无空白错误。

**用户可复现**（在 `local-agent-web/`，使用已有依赖；每次报告目录换新名称）：

```powershell
.venv/Scripts/python.exe scripts/check_release_contracts.py --check
.venv/Scripts/python.exe -m local_agent.api --diagnose --config config.yaml
.venv/Scripts/python.exe scripts/run_release_evaluation.py --output eval/v10-fixed-review
# 完整回归：basetemp 选仓库外的不存在新目录；不启用 DS_PRECISION/DS_SMOKE。
.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider --basetemp E:/tmp/chaiai-v10-review-new
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe scripts/export_openapi.py --check
```

前端在 `frontend/`：`npm run typecheck`、`npm run lint`、`npm run check:api`、`npm run test`。
浏览器使用默认与 `playwright.live/streaming/variants/approval/workflow.config.ts` 六套配置，
CI=1、独立输出目录、trace=off；五套真服务只使用现有假服务，UV_OFFLINE=1、UV_NO_SYNC=1，
UV_CACHE_DIR 指向隔离证据目录。执行退出码和完整日志均已保存，不需要重跑来读取本轮证据。

**交付边界**：可以交付本机工程候选源码；新机器、全数据跨机器迁移、新分发产物和录屏均
**未验证/未制作**。当前产品/协议版本标识保持原值，基线标记 candidate。使用者可按
`docs/release/DEMO_AND_ACCEPTANCE.md` 补新机器记录，届时再判断完整 v1.0，而不是自动发布。
真实回答语义审查/T43 压力未重开；没有付费模型调用、正式数据维护、Git 提交、ZIP 或发布。

### 5.4 封印之后的一轮：界面交互细节收口（2026-10-13）

**来源与记账口径**：v1.0 阶段门（§5.3，证据 `eval/v10-delivery-20261012/`，383 文件）验收并封印之后，本机继续做了一轮**界面交互细节收口**。代码与用例已在树里，本节只是**把它如实记下来并重跑门槛**（机器逐文件核对改动面，见 `eval/v10-interaction-20261013/change-set.json`）；本文件不替它编造任务号，HISTORICAL_TASKS 也不改写。

**改动面（相对 v1.0 封印，机器核对）**：新增 **4** 个文件——`frontend/src/components/Notice.tsx` / `Notice.module.css` / `Notice.test.tsx`、`frontend/e2e/interaction-details.spec.ts`；修改 **20** 个文件——后端 3（`application/service.py`、`persistence/session_store.py`、`tests/test_application.py`）、前端 17（`app/LiveApp.tsx`、`api/useSessionStream.ts`、`components/Sidebar.tsx`、`features/conversation/Conversation.tsx`、`features/conversation-map/ConversationMapPanel.tsx`、`features/error/ErrorNotice.tsx`、6 个 CSS、各自用例，以及 `e2e/conversation-streaming.spec.ts`）。

**做到的四件事**（与 `local-agent-web/README.md` 第 49 行的说法一致）：

1. **会话按首个提问命名**：新会话在首次通过预检开始执行时用首个提问命名，后续提问不覆盖标题（后端写 `title_hint`；前端收到流式事件时直接更新侧栏那一条，不整表刷新）。
2. **侧栏会话列表折叠**：每个工作区默认显示 5 条并保留当前选中项，可「展示更多（N）／收起」，搜索覆盖全部会话；列表独立滚动，设置固定在视口底部。
3. **工具错误提示可收起／重新查看**：切换页签保持收起状态（`ErrorNotice` 同时支持受控与不受控用法，`ErrorActions` 由 `App`/`LiveApp` 传到对话与会话地图）。
4. **轨迹与地图的布局收口**：轨迹表头贴住滚动区、上滑不压缩顶部栏；会话地图的追问抽屉关闭后保留分支与草稿。

**本轮补的一处修正（如实说明，不是重跑掩盖）**：`e2e/conversation-streaming.spec.ts` 原先在刷新后断言「提问文本在整页只出现一次」；第 1 条改动让同一句话同时出现在侧栏（行按钮可访问名 + 行内标题）与对话里，整页计数变成 **3**，用例**确定性失败**（`Received: 3`）。产品行为是有意的（README 已写明），所以改的是**断言作用域**——限定到对话区 `getByRole('log', { name: '对话' })`，原本要钉的「刷新后本轮在对话里只有一份」不变，**没有放宽**。修前（失败）与修后（通过）两份日志都留在证据目录里。

**已执行验证（2026-10-13，全部本机、离线替身）**：

| 门 | 本轮真实结果 |
| --- | --- |
| 后端完整回归 | **1888 passed / 9 skipped / 0 failed**，722 秒（比 v1.0 多 1 条：正是本轮新增的会话命名用例） |
| 固定工程评测 | **连续两轮各 390 cases 全 passed**，pytest 退出均 0，`source_sha256` 两轮一致（`e6e5b71c…`）、`source_unchanged: true`；16 个固定测试文件（含 `test_fault_injection.py`），只用确定性替身 |
| 前端单测 | **42 文件 / 385 passed**（比 v1.0 多 1 文件 4 条） |
| 默认浏览器 | **192 passed / 3 skipped**（比 v1.0 多 12 条：新增 `interaction-details.spec.ts` 4 例 × 3 视口） |
| 五套真服务（顺序跑） | live 3、streaming 1、variants 1、approval 2、workflow 5，共 **12 passed** |
| 静态与漂移 | ruff（`src tests scripts packaging`）、mypy（133 源文件）、OpenAPI 导出、兼容基线 `check_release_contracts.py --check` 全通过 |
| 冻结 | **387 个文件**（v1.0 的 383 + 本轮 4 个新增）在全部门槛跑完后与清单一致（`frozen-source.json`） |

**一处偶发，如实记账（不当成已修复）**：第一次跑五套真服务时，`workflow-live.spec.ts:48` 在刷新后找不到「工作流进度」而失败；**单独重跑 5 passed**，随后顺序跑一遍也 5 passed。原因未定位（不排除那一轮的进程／端口收尾时序），失败日志已保留在证据目录；若再出现应视为待查问题，而不是"已经好了"。

**未验证／未做**：真实模型语义质量、新机器、新分发产物、录屏与 §5.3 相同（仍未做）；不提交 Git、不打包、不改版本号（产品包仍 0.1.0、API 0.4.0、DTO schema 1、库结构 10/3/3，与兼容基线一致）。

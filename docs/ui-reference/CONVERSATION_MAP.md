# 会话地图：当前交互与分支验收

> 更新：2026-10-07。当前页面为横向卡片树，用户已认可。后端分支执行、真实模型案例与早期展示证据保留在同一份文档。当前排期见 [项目总览](../../../README.md)。

## 当前交付范围

父节点在左、问答卡片向右展开；点击查看独立详情；平移、缩放、搜索、折叠、定位和分支整理可用。历史追问复用原输入框与提交管线，继承根到所选轮次的聊天路径；中间追问新增子分支，原路径保留。

不支持自由拖动节点、修改父关系、智能分类、历史重生成或分支删除。共享记忆和文件不会随分支回滚；超大地图性能和实体触屏未验收。数据中台已在后续 T45 独立交付，下面历史“本轮不做数据中台”仅指当时范围。

- [最新横向画布与交互证据](#canvas)。
- [初版后端分支与真实模型证据](#initial)。

<a id="canvas"></a>

## 最新横向画布验收

> 前端展示阶段：294 项测试属于该轮；没有将其冒充新的后端或缓存 A/B。

## 横向卡片树展示验收（2026-10-07）

用户确认“父节点在左，从左往右，每轮是卡片，点击查看详情”后完成本轮改版。本轮只改前端展示，没有改后端、模型提示或历史追问执行管线，未新增依赖；尚未提交 Git。

### 展示和交互

- 每轮问答是一张卡片，显示轮次、状态、问题摘要、回答摘要和来源分支。曲线连接真实父子节点，共享祖先只展示一次；当前路径使用蓝色提示。
- 默认完整画布，点击卡片打开右侧独立详情。关闭按钮和 Escape 均可关闭，焦点回到卡片；窄屏详情覆盖画布，可关闭返回。
- 空白处拖动画布，滚轮以鼠标位置为中心缩放；提供缩放按钮、100%、适应全图和定位当前分支。定位可展开被折叠的当前路径，并清除搜索。
- 保留问题/回答搜索、祖先路径、节点折叠、分支整理、定位原聊天与“基于此轮继续提问”。地图只调整视图，不通过拖动改变节点父关系。
- 长链首次打开保留可读字号，从根开始；使用“适应全图”查看总览。已挂载画布在执行快照更新期间保留视角，刷新页面或重新挂载时不保存视角。

### 自动验证

最终前端全量 **294 tests passed / 33 files**。`npm run build`（含 TypeScript）、`npm run lint`、`npm run check:api` 均通过。Vite 仍提示产物超过 500 kB，未为此次展示改版调整打包架构。

新布局用例覆盖父左子右、兄弟子树不重叠、折叠、搜索祖先、鼠标锚点缩放、缩放边界和适应全图；2,500 节点单链用例验证迭代布局不依赖递归。组件用例覆盖按需读取详情、关闭回焦点、展开当前末端、键盘平移、快照请求期间视角保留和卡片聚焦时 Escape 关闭。

前一轮后端全量 **1040 passed / 7 skipped** 与真实模型测试记录见 [初版验收](CONVERSATION_MAP.md#initial)。本轮没有后端源码改动，未重跑该全量，也没有重做模型/缓存 A/B；不能由 UI 测试推导缓存命中率。

复现前端检查（从仓库根进入）：

```powershell
cd local-agent-web/frontend
npm test
npm run build
npm run lint
npm run check:api
```

### 实际浏览器验证

使用端口 8183 的隔离假模型服务、真实 Graph 与 SQLite，数据位于临时验收目录，不写正式会话。准备六节点、三分支的“缓存优化方案讨论”，验证以下操作：

1. 打开会话地图，适应全图：共享根在左，两条中间讨论及三条末端正确连接；初次打开没有详情面板。
2. 实际鼠标拖动：视口平移量为横向 130px、纵向 70px，节点父关系未变。实际滚轮操作使缩放由 58% 变为 106%。
3. 点击第二张卡片查看对应问答；关闭按钮、卡片持焦点时按 Escape 都能返回完整地图。
4. 搜索“方案 C”只命中该节点并保留根和第二轮两个祖先。
5. 从第二轮“基于此轮继续提问”，复用原输入框提交“基于方案 A，再试一组新的对照测试”。产生第七节点，父节点仍是第二轮，分支数由三变四；切回 main，原第一、二、三轮完整保留。最终 API 读取验证 7 个节点、4 个分支、新节点父关系及当前 main。
6. 在 1600×900、1280×720、390×844 实际查看。窄屏恢复 100% 后定位当前末端，卡片可读，详情全宽且能关闭。

下列截图保存于新增追问前的六节点状态：

![横向卡片树总览](conversation-map-canvas-20261007.png)

![卡片对应的独立问答详情](conversation-map-drawer-20261007.png)

![390×844 窄屏定位卡片](conversation-map-canvas-mobile-20261007.png)

使用正式页面时，构建后刷新，打开已有会话的“会话地图”；依次试适应全图、空白拖动、滚轮、卡片详情、关闭和定位当前分支。历史追问会真正调用已配置模型，请按实际需要提交。

### 文件与限制

| 文件（相对本项目） | 改动 |
| --- | --- |
| `frontend/src/features/conversation-map/layout.ts`、`layout.test.ts` | 迭代树布局、连线、视口几何与回归 |
| `frontend/src/features/conversation-map/TreeCanvas.tsx`、`TreeCanvas.module.css` | 卡片画布、平移缩放、折叠及定位控件 |
| `frontend/src/features/conversation-map/ConversationMapPanel.tsx`、`ConversationMapPanel.module.css`、`ConversationMapPanel.test.tsx` | 详情开关、搜索/分支接线、视角保留与组件回归 |
| `README.md`、仓库根 `ARCHITECTURE.md`、`V0_4_DEVELOPMENT_PLAN.md` | 使用说明、展示职责与 T44 记录 |
| 本文、初版验收说明及三张新截图 | 当前展示证据与历史记录区分 |

未实现节点自由拖拽布局、节点重新挂接、智能分类、历史重生成、分支删除或数据中台。未验证超大地图在浏览器中的渲染性能；2,500 节点布局测试只验证布局算法，不等同于页面性能验收。实体触屏/双指缩放未验证；当前提供鼠标操作与键盘平移。缩放下限 5%，极长地图可能需要平移、折叠和定位，不能保证所有规模都能一次适应窗口。


<a id="initial"></a>

## 初版后端分支与真实模型验收

> 初版截图是历史形态；真实模型数据仅证明对应聊天前缀隔离案例，不证明稳定 90%。

## 会话地图实现与验收（2026-10-07）

> 本文记录初版树形目录与分支执行的验收，下面三张图保留为历史证据。当前页面已改为横向卡片画布，最新展示、测试与截图见 [横向卡片树验收](CONVERSATION_MAP.md#canvas)。本文中的前端测试数量及缓存数据属于初版运行。

本轮实现的是当前 Web 内的会话地图与历史分支追问。主模型、工具、权限与审批仍走原提交链路，新增索引与展示入口。此前管理设置阶段已提交；本轮代码尚未提交 Git。

### 可使用的功能

- 顶部“会话地图”入口，旧问答按原顺序形成单链。节点使用用户消息 ID，不按主题智能分类。
- 查看单轮问题、回答、供应商实际返回的思考、工具记录、模型调用与 Token 明细；浏览节点不改变当前执行路径。
- 搜索问题或完整回答，匹配结果保留祖先；折叠后续节点；定位回聊天中的原轮次。
- “基于此轮继续提问”回到已有输入框，显示历史起点。模型上下文为根到该轮的历史前缀，原后续保留。从分支末端继续时沿用分支，从中间节点继续时新增子分支。
- 分支切换、名称修改、上下移动显示顺序；排序不改变节点的父关系。共享祖先只保存一次，节点序号是会话内创建顺序，因此第三轮的新子节点可能是第十一轮。

### 实现边界

聊天库 v6 的 `conversation_turns` 保存索引，`conversation_branches` 保存每条路径的 Checkpoint 游标。执行从同一线程的指定终态 Checkpoint 提交新输入，不重新执行过去的图节点。终态必须没有待执行节点或挂起审批；历史锚点查找最多 10,000 个检查点。

提交上下文携带父节点、预期分支和预期快照；陈旧路径在模型调用与台账创建前返回 409。重试沿用原上下文与请求 ID，确认路径冲突后才允许重新选择。执行中、待审批及未确认执行结果时不能切换分支。浏览/搜索/整理没有模型调用，地图字段不加入模型 Prompt。

历史分支只恢复对话上下文。工作区文件、中期/长期共享记忆和下一次提交的模型设置仍取当前值；这不是文件或记忆的时间机器。

### 自动验证

后端全量：**1040 passed / 7 skipped**，186.15 秒。现有依赖的弃用警告仍存在。新后端案例覆盖三轮分叉与继承、十轮旧链初始化只读、错误父节点与陈旧分支、改名排序不改边、消息窗口裁短不截断父关系、写文件历史不重放、审批阻止切换、重启后分支恢复和原请求幂等重放；API 案例另覆盖陈旧快照拒绝与公开契约。

前端全量：**284 passed / 32 test files**，包括历史追问准备、节点详情、搜索、忙碌锁定、排序请求、纯聊天模型调用展示和网络重试原上下文绑定。`npm run build`（含 TypeScript）、ESLint、OpenAPI 重新生成无漂移通过。Vite 保留大于 500 kB 的产物提示，未为此次功能升级依赖或调整打包架构。Ruff 全部通过，Mypy **99 个源码文件通过**。

复现命令（项目目录 `local-agent-web`）：

```powershell
uv run pytest -q -p no:cacheprovider
uv run ruff check src tests/test_conversation_map.py
uv run mypy src
cd frontend
npm test
npm run build
npm run lint
npm run check:api
```

### 浏览器验收

使用隔离假模型 API、真实 SQLite 与 Graph，端口 8183，数据放临时验收目录，没有向正式会话写测试数据。实际页面跑通：已有十轮 → 查看第三轮独立问答 → 历史追问 → 地图共十一节点 → 切回原讨论保留十轮 → 分支改名为“缓存方案 B” → 上移显示顺序 → 搜索“另一个”只匹配新节点并保留三个祖先 → 查看模型调用 → 刷新 → 定位第三轮回聊天。1280×800 两列、760×800 上下布局及默认较矮窗口已实际查看。

![地图与独立回答](conversation-map-desktop-20261007.png)

![纯聊天模型调用记录](conversation-map-calls-20261007.png)

![窄屏布局](conversation-map-narrow-20261007.png)

### 真实模型案例

使用实际配置的 `deepseek-flash`，独立工作区与三个独立数据库，关闭中期/长期记忆，用户配置文件哈希前后相同。共三轮提交，各含三次已有管线的模型调用，均 completed，0 错误：

| 请求 | 回答检查 | cache read | 未命中输入 | 实测缓存率 |
| --- | --- | ---: | ---: | ---: |
| 根轮 | 复述翠鸟731、pytest | 2,176 | 4,255 | 33.84% |
| 原路径续问 | 复述部署地点银杏842机房 | 6,016 | 1,282 | 82.43% |
| 从根分叉 | 继承翠鸟731、pytest；部署地点回答“不知道” | 6,528 | 788 | 89.23% |

新分支用户路径只有根问题和新追问；切回 main 仍有根问题及部署地点补充，再切回新分支成功。原始隔离报告在本地 `eval/conversation-map-20261007/results.json`，该评测目录不纳入版本库。

缓存率按实际缓存读取÷（缓存读取＋未命中输入）计算。这只是三次短请求的功能案例，没有改动前同题对照，不证明稳定 90%、整体缓存不退化、共享记忆开启后的分支隔离或全部回答质量。

### 保留限制

- 初版使用树形目录；后续展示改版已加入横向卡片画布和平移/缩放。目前仍没有节点自由拖拽布局、改父节点、智能分类、历史重生成或分支删除。
- 缺少历史检查点时只展示能取得的消息；没有依据的历史计划/用量不冒充最新轮数据，追问入口禁用。
- 共享记忆可能包含别的分支已写入的事实，这是本轮明确保留的作用域；真实模型案例关闭共享记忆，只验证聊天前缀隔离。
- 万级节点性能、移动端全尺寸、POSIX 真实分支执行及其它兼容端点未验证。
- 本轮不实现中期/长期记忆与 Token 数据中台，也不修改之前记档的召回质量缺口。

### 改动文件索引

以下路径相对仓库根目录；原有临时测试目录和日志未改动。

| 文件 | 本轮用途 |
| --- | --- |
| `ARCHITECTURE.md`、`V0_4_DEVELOPMENT_PLAN.md`、`local-agent-web/README.md` | 分支契约、T44 状态与使用说明 |
| `local-agent-web/src/local_agent/persistence/conversation_store.py` | 新增节点与分支索引存储 |
| `local-agent-web/src/local_agent/persistence/migrations.py`、`session_store.py` | 聊天库 v6 与索引装配 |
| `local-agent-web/src/local_agent/persistence/request_ledger.py` | 未确认请求阻止切换 |
| `local-agent-web/src/local_agent/application/conversation.py` | 新增树投影、终态锚点、历史详情与追问路径 |
| `local-agent-web/src/local_agent/application/service.py`、`models.py`、`errors.py` | 接入现有执行链、公开 DTO 与路径冲突提示 |
| `local-agent-web/src/local_agent/api/routes.py`、`schemas.py` | 地图/详情/分支接口及提交上下文 |
| `local-agent-web/docs/api/openapi.json`、`frontend/src/api/schema.gen.ts` | 同步生成的 API 契约 |
| `local-agent-web/frontend/src/api/client.ts`、`queries.ts`、`queries.test.tsx` | 类型化请求、上下文幂等与回归 |
| `local-agent-web/frontend/src/features/conversation-map/ConversationMapPanel.tsx`、`ConversationMapPanel.module.css`、`adapters.ts`、`ConversationMapPanel.test.tsx` | 地图页面、布局、树与搜索适配及组件测试 |
| `local-agent-web/frontend/src/app/LiveApp.tsx`、`components/TopBar.tsx` | 地图入口、历史起点与原输入框接线 |
| `local-agent-web/frontend/src/features/conversation/Conversation.tsx`、`Conversation.module.css` | 聊天轮次定位锚点与排版 |
| `local-agent-web/frontend/src/features/settings/modelMenuAdapter.test.ts` | 旧快照 fixture 补齐分支默认值 |
| `local-agent-web/tests/test_conversation_map.py`、`test_api.py`、`test_catalog_persistence.py` | 真实 Graph 分支/审批/副作用、API 与迁移回归 |
| `local-agent-web/docs/ui-reference/conversation-map-20261007.md` 与三张截图 | 本轮验收证据 |

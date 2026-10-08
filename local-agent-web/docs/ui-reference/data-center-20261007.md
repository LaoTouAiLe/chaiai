# 数据中台实现与验收（T45，2026-10-07）

用户认可会话地图后要求继续，并授权综合决定。本轮完成现有 Web 内的只读数据中台，入口位于侧栏，包含概览、中期记忆、长期事实、任务结论与 Token 用量。没有改 Graph、模型提示、记忆写入或召回策略；没有新增依赖；尚未提交 Git。

## 目前可以使用

- 概览：模型调用次数、失败/用量缺失次数、实际输入/输出、按 Token 加权的缓存率，记忆资源与可见记录数，每日调用趋势、模型和调用用途分布。趋势按 UTC，显示最近有记录的 14 天；悬停可查看该日 Token 明细，点击筛选当天。模型图显示调用量最多的 20 个，点击进入对应模型明细。
- 记忆：按登记工作区、状态与关键词筛选，每页 30 条；点击查看保存正文、作用域、来源、置信度、重要程度与时间。长期事实包括历史状态；过期状态由时间只读推导，浏览不会修改记录。任务结论包含已有证据正文。
- 用量：按工作区、模型名称和 UTC 日期筛选，查看逐调用计量、耗时与重试关联。失败尝试与重试分别保存，估算与实际量分开显示。未知不补零，空结果不编造 0% 缓存率。
- 记忆关闭仍可查看保留数据；资源区分尚无库、可读和暂不可读。当前运行或审批不会被浏览中断，页面提示可返回会话。返回保留原会话与未发送草稿。

## 读取与统计口径

`GET /api/data-center/memories` 和 `GET /api/data-center/usage` 均只读。工作区参数只接受登记 ID，不接受任意文件路径。记忆的全局、用户、项目、工作区可见范围复用 `visible_scope_keys`；记忆选择工作区，数据不局限于某条会话或某个分支。

长期库使用 SQLite `mode=ro`，不调用会迁移/建库的 `open_store`。中期检查库文件存在后调用 Chroma 的 `get_collection(embedding_function=None)` 和 `get(include=["documents", "metadatas"])`，不创建 collection、不获取向量、不执行语义检索或加载 embedding 模型。Chroma SDK 管理自身内部文件，因此这里保证业务记录不写入，不声称它具备 SQLite 那样的文件只读模式。

用量从会话最新和各分支末端检查点的累积 `model_call_records` 取并集，以会话 ID 与调用 ID 去重。归档会话仍计入，压缩后的聊天消息数量不作为调用数量。明细、汇总、趋势与分布来自同一过滤集合；开始边界包含、结束边界为选定结束日次日零点且不包含。正在执行时只统计已经进入检查点的记录，不预估尚未落盘调用的费用或用量。

缓存率沿用已有实际用量契约：缓存读 ÷（未命中输入 + 缓存读 + 独立缓存写）。缓存读/未命中任一未知时无法计算；已有缓存读且未上报独立写入量时沿用隐式缓存口径。任一调用缺失某项计量，该项整体汇总不冒充已知。没有可用调用的旧会话明确标记，不按零消耗处理。

正文和公开字段复用展示脱敏；向量、密钥、私有端点指纹和 SDK 原始异常不返回。读取不会更新记忆访问时间、改当前分支或创建模型调用。

## 自动检查

本轮后端全量：**1048 passed / 7 skipped**，181.87 秒。依赖既有弃用警告保留。新测试覆盖：空库不创建、长期作用域/历史状态/过期/搜索/分页/文件字节不变、真实 Chroma 已存记录读取且不返回向量、损坏库提示、真实 Graph 分叉去重、归档计入、时间/模型过滤、加权缓存率、失败尝试、未知与扫描上限；HTTP 测试覆盖只读方法、参数范围、时区/日期顺序、未知工作区和公开契约。

前端全量：**302 passed / 34 files**。新增 8 项覆盖未知与零、保留记忆、UTC 边界、趋势点击、服务器搜索/工作区、分页、详情回焦点/Escape、失败刷新、无效日期不发请求且不阻断记忆刷新。后续查询参数类型改为引用生成契约、模型图排序和提示文案调整后，数据中台 8 项再次通过。

`npm run build`（包含 TypeScript）、ESLint、Ruff、Mypy **101 个源码文件**、OpenAPI 导出检查与 `npm run check:api` 通过。Vite 仍有超过 500 kB 的打包提示，本轮未调整打包架构。

复现（项目根目录 `local-agent-web`；受限环境可把 pytest 的 `--basetemp` 指向可写的新目录）：

```powershell
uv run pytest -q -p no:cacheprovider
uv run ruff check src tests/test_data_center.py
uv run mypy src
uv run python scripts/export_openapi.py --check
cd frontend
npm test
npm run build
npm run lint
npm run check:api
```

## 浏览器实操与截图

使用端口 8183 的隔离假模型服务，测试数据位于临时目录。实际 SQLite、Graph 与 Chroma 中保存 5 条中期记忆、35 条长期事实、1 条任务结论。原讨论三轮，再从第一轮分叉一次，共四轮/两分支、12 次调用；假模型固定每次输入 1,000、缓存读 800、输出 120。

页面与实际 API 读取均为：12 次调用、输入 12,000、输出 1,440、未命中输入 2,400、缓存读 9,600、缓存率 80%。**这是固定假模型用量，用来验证投影与显示；不是真实供应商缓存率、质量或 A/B 数据。**

实际跑通：

1. 侧栏入口 → 概览，资源关闭但保留数据可见。
2. 35 条事实第一页 30 条、第二页 5 条；在第二页搜索“验收约定 01”，结果为 1 条、回到第一页；查看正文、置信度和来源、关闭详情。
3. 中期列表 5 条、任务结论 1 条，作用域与数据一致。
4. 用量明细 12 条、查看单次输入/缓存/输出；未来日期得到空结果，倒置日期得到明确提示。原生日期输入使用键盘调整确认页面状态变化。
5. 返回原会话，未发送的验收草稿保留；未提交额外问题。
6. 1280×720、1600×900 和 390×844 实际查看；宽屏与窄屏根页面无横向溢出，窄屏表格在自身容器内横向查看，详情覆盖内容区且可按 Escape 关闭。浏览器未记录控制台错误。

![数据中台概览：隔离假模型数据](data-center-overview-20261007.png)

![长期记忆详情](data-center-memory-detail-20261007.png)

![单次调用计量](data-center-call-detail-20261007.png)

![390×844 窄屏概览](data-center-mobile-20261007.png)

正式页面验收：重启后端、构建前端并刷新 → 点击侧栏“数据中台” → 选择工作区 → 分别查看记忆和用量 → 搜索/状态/日期筛选 → 点击记录 → 关闭详情 → 返回会话核对草稿。浏览操作本身不会产生模型费用。

## 改动文件

下表路径相对仓库根；上一轮会话地图及原有临时目录/日志仍保留，本轮没有将它们混入新数据中台逻辑。

| 文件 | 本轮用途 |
| --- | --- |
| `local-agent-web/src/local_agent/application/data_center.py` | 只读记忆读取、调用并集、过滤分页与统计 |
| `local-agent-web/src/local_agent/application/data_center_models.py` | 公开视图与未知计量 |
| `local-agent-web/src/local_agent/application/service.py`、`api/routes.py` | 服务入口与两个 GET |
| `local-agent-web/docs/api/openapi.json`、`frontend/src/api/schema.gen.ts` | 同步生成契约 |
| `local-agent-web/frontend/src/api/client.ts`、`queries.ts` | 生成类型参数与按需查询 |
| `local-agent-web/frontend/src/features/data-center/DataCenter.tsx`、`DataCenter.module.css`、`adapters.ts`、`DataCenter.test.tsx` | 页面、展示映射与交互回归 |
| `local-agent-web/frontend/src/app/LiveApp.tsx`、`components/Sidebar.tsx` | 页面入口、返回与原会话状态保留 |
| `local-agent-web/frontend/src/features/conversation/adapters.ts` | 共用已有调用用途中文词表 |
| `local-agent-web/tests/test_data_center.py`、`test_api.py` | 存储、Graph 和 HTTP 验证 |
| `ARCHITECTURE.md`、`V0_4_DEVELOPMENT_PLAN.md`、`local-agent-web/README.md`、本文与四张截图 | 契约、T45、使用说明与验收证据 |

## 保留边界

- 不开放记忆写入/删除、向量库重建、历史重生成、费用估算或用户权限管理。
- 单类记忆最多读取 10,000 条；正文最多展示 20,000 字符，关键词搜索覆盖名称和展示正文。超过上限或库不可读时提示已读范围，数量不是完整库存。
- 用量扫描最多最近活动的 200 会话、2,000 检查点、100,000 次调用记录读取；重复分支记录也消耗扫描预算。达到上限提示统计不完整，不能用于声称全历史总量。
- 旧库未记录用量、检查点缺失或供应商没有上报的字段无法补齐；页面不会以估算冒充实际量。
- 没有本轮真实供应商缓存 A/B、万级数据性能、实体触屏或其它兼容端点的实测。浏览不改提示/执行管线，但不能据此承诺真实缓存率稳定 90%。

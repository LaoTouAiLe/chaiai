# v0.5 / v0.6 复审与 v0.7 开工判断（2026-10-10）

> 2026-10-11 补记：用户授权修复本页三项 P1，13 个新增正式回归、三个原始缺陷探针全部通过，最终完整回归 **1482 passed / 8 skipped / 0 failed**，见 [修复记录](v07-readiness-fixes-20261011.md)。本页保留原始失败与当时结论，旧代码行号对应原审查 JSON 的 SHA-256；当前修复定位以新报告为准，不把旧证据改写为成功。

**结论：Request Changes。当前不批准 v0.7 实现开工；可以先整理计划和架构契约。** 本次重新读实现、已有回归和路线图，并补做确定性的边界复现，确认 3 项 P1。上一轮“已审查部分可交付”的判断需要收紧：那 7 项修复仍成立，但取消和 Web 访问还有本页列出的缺口。

本轮仅审查，未改业务源码、正式测试或既有版本计划。只新增本报告、同名机器证据及 `local-agent-web/eval/v07-readiness-20261010/` 下的隔离验证脚本/日志。不生成 ZIP、不提交 Git、不操作生产数据、不新增付费模型调用；按用户要求不进行回答语义质量审查。

## 必须改

### 1. P1：取消尚未绑定运行编号的任务后，恢复登记与重启会丢失取消意图

[service.py:2273](E:/pylianxi/chaiai/local-agent-web/src/local_agent/application/service.py:2273) → 恢复仅按 `cancelled` 行的 `run_id` 匹配当前 Checkpoint；但 [jobs.py:352](E:/pylianxi/chaiai/local-agent-web/src/local_agent/application/jobs.py:352) 只在结算时绑定运行编号，而 [:349](E:/pylianxi/chaiai/local-agent-web/src/local_agent/application/jobs.py:349) 对已取消终态直接返回 → 已取消的审批操作可以重新执行，违反取消后不启动新工具的契约 → 在取消前持久绑定任务与实际运行/请求的身份，并让恢复登记不能遮蔽该轮取消记录；增加“待审批 Checkpoint 已写、任务尚未结算”的重启用例，保留终态不可改写的约束。

**确定性复现**：后台任务的图已经停在删除审批，暂停任务结算，HTTP 取消，原取消行的 `run_id=null`；释放 worker 并重启，取消令牌为真。再模拟既有回归所用的“恢复已登记、图尚未执行”窗口并重启，令牌变为假；通过真实 API 批准后，新建临时 `note.txt` 实际被删除。仅使用确定性假模型和隔离数据库。

**证据**：`eval/v07-readiness-20261010/unbound-cancel.json` 中 `token_after_first_restart=true`、`token_after_second_restart=false`、`approval_http_status=200`、`file_preserved=false`。原 [test_api.py:2564](E:/pylianxi/chaiai/local-agent-web/tests/test_api.py:2564) 三种重启回归从已完成任务结算的等待状态开始，未覆盖这个更早的取消窗口。

### 2. P1：旧任务的超时处理会取消同会话下一任务的令牌

[service.py:603](E:/pylianxi/chaiai/local-agent-web/src/local_agent/application/service.py:603) → `_cancel_job` 按旧任务的 `session_id` 获取“当前令牌”，先取消令牌，再让运行时检查旧任务是否已终态；[:651](E:/pylianxi/chaiai/local-agent-web/src/local_agent/application/service.py:651) 的超时扫描与实际取消之间没有绑定任务身份 → 配置 `max_task_seconds` 后，旧任务在扫描后完成、新任务开始时，旧超时会拒绝新任务的工具调用，索引却仍显示新任务在排队/运行 → 将令牌绑定到任务/运行身份，并将确认目标仍活动、取得对应令牌与状态迁移放进一致的同步边界；补超时扫描跨轮次回归。仅在取消前重读一次仍不能关闭重读与令牌取得之间的竞态。

**确定性复现**：屏障暂停超时线程刚取得的旧任务列表，旧任务完成，同会话登记新任务并换新令牌，然后放行超时线程。旧任务仍 `completed`，新任务仍 `queued`，新令牌从假变真，原因是旧任务的超时。

**证据**：`eval/v07-readiness-20261010/stale-timeout.json`。新令牌会被 [policy.py:266](E:/pylianxi/chaiai/local-agent-web/src/local_agent/security/policy.py:266) 的现有安全门拒绝；本复现未调用模型或工具。

### 3. P1：Web transport 在校验后重新解析域名，可连接未经校验的内网地址

[web_tools.py:278](E:/pylianxi/chaiai/local-agent-web/src/local_agent/tools/web_tools.py:278) → `check_url` 校验域名解析结果后，urllib 建连时再次解析该域名，没有将连接绑定到已校验 IP；响应后的域名复查也不能证明实际连接地址 → 开启 Web 能力时，公网域名可在两次解析间指向本机/内网，绕过 SSRF 边界 → 每一跳只连接已验证的地址，保留正确的 Host 与 TLS 主机名校验，并明确代理路径的处理；增加校验与建连解析结果不一致的 transport 用例。

**确定性复现**：临时 HTTP 服务只监听 `127.0.0.1`；受控 DNS 在安全检查时返回公网 `8.8.8.8`，建连时返回 `127.0.0.1`，响应后检查又返回公网。真实 urllib transport 访问了临时服务 `/owned` 并返回其标记正文。探针显式走无代理的直接网络路径（受支持的普通环境）；没有向公网发 HTTP 请求，也没有读取其他本机服务或私人数据。

**证据**：`eval/v07-readiness-20261010/dns-rebind.json` 中 `blocked=false`、`returned_owned_loopback_body=true`、`owned_loopback_visits=["/owned"]`。已有逐跳重定向检查仍有效，此问题是检查结果没有约束实际连接。

## 建议改

[README.md:22](E:/pylianxi/chaiai/README.md:22) → 当前推进说明仍只列 v0.5 复验，没有反映 v0.6 已实现、复审发现上述阻塞 → 下一阶段容易把“实现完成”误读成“验收通过” → 收口时同步两个阶段的状态、阻塞证据与暂缓项。本轮没有代替用户修改路线图或批准豁免。

## 不用管

- [jobs.py:276](E:/pylianxi/chaiai/local-agent-web/src/local_agent/application/jobs.py:276) → 上轮队列跳过仍执行独立清理回调、迁移互斥与 worker 异常保护保留 → 已修复的锁泄漏/worker 退出缺陷没有因本次复审重新成立 → 保留实现和原回归。
- [git_tools.py:136](E:/pylianxi/chaiai/local-agent-web/src/local_agent/tools/git_tools.py:136)、[result_spill.py:105](E:/pylianxi/chaiai/local-agent-web/src/local_agent/tools/result_spill.py:105) → Git literal pathspec、解析链接后的落盘边界与本次 locator 保留均已实现 → 原安全修复仍有效 → 不为本次复审追加无关重构。
- [session_kind.py:38](E:/pylianxi/chaiai/local-agent-web/src/local_agent/session_kind.py:38) → 当前来源闭集不含 `subagent`，与 v0.6 明确边界一致 → 不是漏做 v0.6 → v0.7 立项时再按契约追加并覆盖记忆来源门。

## 验证与可复跑步骤

本轮 Python 全量回归：**1469 passed / 8 skipped / 0 failed / 38 warnings**，687.18 秒，退出码 0。最终日志为 `eval/v07-readiness-20261010/pytest.log`；跳过项包含付费真实模型精度/冒烟、真机凭据存储、可选依赖缺失场景及平台/符号链接条件，不把它们记成已验证。

- 前端单测：**339 passed / 38 files**。
- 默认 Playwright：**180 passed / 3 skipped**，包含构建、既有原型和截图检查；没有更新截图。它证明默认浏览器回归，不能代替后台任务的实际故障验证。日志中的 Vite `/api/preferences` 回环代理连接拒绝属于原型环境，不作为真实 API 验收成功证据。
- ruff（src/tests/scripts/packaging）、mypy src（122 文件）、OpenAPI 与前端 API 类型漂移、前端 typecheck/lint 全部通过。
- **新增 3 个缺陷复现均退出码 1**：脚本末尾断言期望安全行为，目前失败。不能把正式回归全绿当作这三项通过。这些脚本在 ignored 的 eval 目录，不计入既有 pytest 总数。
- 上轮修复记录中的 12 项源码/测试/文档 SHA-256 与本轮读取的文件一致；旧 ZIP 仍不存在。新问题来自补充边界审查，不是这 7 项修复被回滚。

从项目根目录执行：

```powershell
Set-Location E:/pylianxi/chaiai/local-agent-web
.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe scripts/export_openapi.py --check
.venv/Scripts/python.exe eval/v07-readiness-20261010/reproduce_unbound_cancel.py
.venv/Scripts/python.exe eval/v07-readiness-20261010/reproduce_stale_timeout.py
.venv/Scripts/python.exe eval/v07-readiness-20261010/reproduce_dns_rebind.py
Set-Location frontend
npm.cmd run test -- --run
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run check:api
npm.cmd run test:e2e -- --reporter=line
```

三个缺陷复现目前应失败；修复之后应让同样的安全断言通过，并将这些窗口纳入正式回归。探针重新运行会写各自 JSON，但不覆盖上一轮审查证据。

## v0.7 前置与存疑边界

1. **实现开工阻塞**：先关闭上述 3 项 P1，补正式回归，再复核 v0.5/v0.6 的安全与恢复契约。尤其不能在取消意图会丢失的基础上叠加主/子任务恢复。
2. **可先做的准备**：目前没有 `V0_7_DEVELOPMENT_PLAN.md`。可依 [FUTURE_WORK.md:378](E:/pylianxi/chaiai/docs/planning/FUTURE_WORK.md:378) 整理两个具体 Subgraph 的输入/输出、能力/工作区隔离、总 Token/工具调用/并发/委派层级硬上限、失败收敛与持久化身份；先修架构契约，再实现。
3. **单 Agent 对照基线仍须明确**：路线图 [:380](E:/pylianxi/chaiai/docs/planning/FUTURE_WORK.md:380) 要求评测稳定，[:395](E:/pylianxi/chaiai/docs/planning/FUTURE_WORK.md:395) 要求至少两个工作流优于自由工具循环。三档 Profile 既有真实固定任务记录已存在，不应说成完全没有模型验证；但 [v05-review-20261009.md:71](E:/pylianxi/chaiai/docs/benchmarks/v05-review-20261009.md:71) 的 20k 真实压力失败，当前源码仅有离线修复证据。它未被本次回归或用户暂缓语义审查自动验收。立 v0.7 计划时需明确其复杂任务基线和仍未验证的压力边界。
4. **未验证**：当前源码的供应商真实压力成功率、真实缓存收益及回答语义质量。本轮没有再做付费调用或回答语义评判，也没有扩大此前 ¥5 授权。普通符号链接与 POSIX 条件的跳过项按日志保留，不能声称所有平台已验证。

本次结论不要求生成 ZIP、提交 Git 或重开已暂缓的语义审查；阻塞来自真实可复现的安全/恢复缺陷和路线图规定的评测基线，而不是这些交付形式。

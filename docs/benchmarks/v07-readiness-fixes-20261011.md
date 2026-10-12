# v0.7 前置复审的三项 P1 修复（2026-10-11）

用户授权“修改必须改”。本次只处理 [2026-10-10 复审](v07-readiness-review-20261010.md) 的三项 P1，不开始 v0.7 实现、不修改建议项。原始失败 JSON 与探针保持原样，修复复验另存于 `local-agent-web/eval/v07-readiness-fixes-20261011/`。没有新增付费模型调用，回答语义审查继续暂缓。

## 修复与根因

| 原问题 | 根因 | 当前修复 | 验证行为 |
| --- | --- | --- | --- |
| 已取消删除在恢复登记、重启后重新执行 | 任务尚未结算时取消，原行没有轮次绑定；新恢复行遮住最新取消行 | [service.py:598](E:/pylianxi/chaiai/local-agent-web/src/local_agent/application/service.py:598) 在取消迁移中核对 Checkpoint / 台账并同次保存真实轮次；审批恢复继续按轮次恢复取消信号。旧版本空绑定记录不能证明归属时拒绝旧审批，已有独立绑定的新轮次可正常提交 | 停在结算前取消、正常/恢复登记后重启，原文件都保留；覆盖正常记录与升级前空绑定记录共 4 个 API 场景 |
| 旧超时取消同会话下一任务 | 先取得当前会话令牌，后核对旧任务终态；扫描和取消之间可以换轮次 | [jobs.py:325](E:/pylianxi/chaiai/local-agent-web/src/local_agent/application/jobs.py:325) 在同一状态迁移锁内重新核对目标，再调用取消信号回调、绑定轮次并写终态；已终态目标不调用回调。超时只报告实际取消成功的目标 | 旧扫描结果迟到时，新令牌仍有效，旧任务保持完成，超时结果为空；取消回调与结算互斥，终态保持不变 |
| DNS 检查是公网、实际连接却访问内网 | 校验结果没有约束 urllib 随后的主机名解析 | [web_tools.py:274](E:/pylianxi/chaiai/local-agent-web/src/local_agent/tools/web_tools.py:274) 建连时解析并校验全部候选，仅向已验证的数字地址建立 socket，重试复用该批地址；每次重定向走同一路径 | DNS 变化被拦截，临时回环服务没有收到请求；空地址、非法地址、公网/内网混合均在开 socket 前拒绝；TLS 保留原域名验证 |

取消信号仍走既有能力可见性门与安全门；没有第二套工具执行或恢复路径，没有自动重放副作用，也没有添加数据库迁移。取消身份在进入终态时一次写入，不靠改写终态补救。

Web 默认 transport 改为直接连接，**忽略环境与系统代理**，避免由未经约束的代理解析目标。HTTP Host 使用原域名；HTTPS 继续使用标准 `HTTPSConnection` 的 TLS 包装，原域名用于 SNI/主机名验证，默认信任链验证保持开启。未新增依赖或配置项。

## 验证

最终完整 Python 回归：**1482 passed / 8 skipped / 0 failed / 38 warnings**，555.49 秒，退出码 0。日志 `eval/v07-readiness-fixes-20261011/pytest-final.log`；跳过项仍是显式付费真实模型、真机凭据存储、可选依赖与平台/符号链接条件，不记作已验证。

- 最终定向回归：**50 passed / 126 deselected**，42.67 秒。
- 新增正式回归 **13 个实例**：`tests/test_jobs.py` 2 个；`tests/test_api.py` 5 个（4 个取消/升级/重启场景 + 1 个跨轮次超时场景）；`tests/test_web_tools.py` 6 个（DNS 变化、3 个候选拒绝、候选复用、HTTPS 证书/主机名）。
- 原始三个缺陷探针：修复前均退出码 1，当前副本均退出码 0。保存的结果分别为 `stale-timeout.json`、`unbound-cancel.json`、`dns-rebind.json`。
- ruff（src/tests/scripts/packaging）、mypy src（122 文件）、OpenAPI 与前端 API 漂移检查通过。
- 本次未改前端代码或公共 DTO，不重复运行上一轮已通过的前端单测/截图矩阵。前端 API 漂移是本次重新执行的检查。
- 加入旧版本空绑定场景后停止了一次尚未完成的全量执行；该 `pytest.log` 不是验收证据，最终完整运行保存到 `pytest-final.log`。

三个原探针的安全断言保持不变，只复制到新的验证目录执行，没有用新预期遮盖旧失败。审批探针在第二次重启后通过既有 `for_resume` 的轮次检查恢复取消信号；最终 `file_preserved=true`、`token_after_approval=true`，而非靠启动时的内存令牌声明安全。

## 改动文件

- `local-agent-web/src/local_agent/application/jobs.py`：取消目标核对、信号回调与身份/终态写入共用迁移锁。
- `local-agent-web/src/local_agent/application/service.py`：核实取消轮次、旧空绑定恢复保护、迟到取消/超时的真实返回状态。
- `local-agent-web/src/local_agent/tools/web_tools.py`：数字地址建连、候选失败闭合、直接连接与原域名 TLS 验证。
- `local-agent-web/tests/test_jobs.py`、`tests/test_api.py`、`tests/test_web_tools.py`：新增上述竞态、重启、DNS 和 TLS 回归。
- `ARCHITECTURE.md`：先修订 §6.10/§17.4 的连接约束、跨轮次取消与升级前空绑定契约，再改实现。
- 版本计划、原复审附记、本报告与机器证据：同步本次修复范围与验证状态。
- ignored 的 `eval/v07-readiness-fixes-20261011/`：原探针副本及独立日志，不修改正式数据。

## 复跑

```powershell
Set-Location E:/pylianxi/chaiai/local-agent-web
.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check src tests scripts packaging
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe scripts/export_openapi.py --check
.venv/Scripts/python.exe eval/v07-readiness-fixes-20261011/reproduce_stale_timeout.py
.venv/Scripts/python.exe eval/v07-readiness-fixes-20261011/reproduce_unbound_cancel.py
.venv/Scripts/python.exe eval/v07-readiness-fixes-20261011/reproduce_dns_rebind.py
Set-Location frontend
npm.cmd run check:api
```

上述三个探针现在应全部退出码 0，失败会让命令非零退出。数据库与工作区由每个探针在本目录新建，退出时清理；DNS 探针只启动临时回环 HTTP 服务，公网地址仅用于受控解析/候选单测。

## 剩余边界

本次三项 P1 的代码阻塞已解除，13 个新增正式回归和三个原始缺陷探针均通过；该结论只针对本次授权的必须改项。

- 旧版本无法绑定轮次的取消记录按无法证明处理，旧审批不会执行；核对后可以重新提交。
- 未新增公网网站、供应商缓存或真实压力调用；TLS 默认验证路径用受控 socket/SSLContext 检查，不声明所有公网服务均已实测。
- T43 的真实压力收口与回答语义质量仍未验收，本次修复不替代这些证据。v0.7 的固定复杂任务基线仍应写入计划。
- 不生成 ZIP、不提交 Git、不操作生产数据库；本报告仅证明本次修复，不能据此宣布所有版本门槛自动通过。

# 缓存保护文件无法修改 ACL 的修复（2026-10-19）

## 根因与范围

用户现场的 `eval/uv-cache/sdists-v9/.git` 是 0 字节普通文件，所有者为另一账户。
当前账户可访问文件，但无权改其 DACL。初始化给它设置拒绝项失败，整条 Shell 命令在启动前被阻止。
失败未缓存，所以后续 `echo ok` 再次扫描并失败。112.2/88.6 秒是用户日志的调用耗时，
没有分阶段计时，不能据此认定浏览器启动后超时。

修复仅处理这一 ACL 初始化故障，不调整浏览器超时、全屏终端、模型循环、权限档位或文件工具策略。
无需使用用户授权的“无法修复时放开外部写入”兜底。

## 实现

- `src/local_agent/security/win_sandbox.py`：普通文件拒绝项写入失败时，构建命令相同的受限令牌，
  DuplicateToken 为 impersonation token 后交给 Windows AccessCheck，不改变执行线程身份。
  分别核验写内容、追加、扩展属性、文件属性、删除、修改 DACL、修改所有者，另核验父目录
  FILE_DELETE_CHILD。只有每一项均被拒绝，才保留原 ACL 并记录该文件。
- `src/local_agent/security/policy.py`：所有根授权完成后，用工作区、专属临时目录、外部读写授权根的
  **完整 SID 集合**重新核验这些文件。缓存命中仍核验，新增授权或权限变化不能绕过该检查。
- `tests/test_protected_file_acl.py`：新增 13 项 Windows 回归。
- 根 `README.md`：增加修复和验收状态入口。

目录不适用回退，因为其后代可以拥有不同 ACL。访问检查出错、任意一项变更权限仍可用，均阻止执行。
没有删除缓存、抢占所有权、给当前账户提升权限或关闭外部写入边界。

Microsoft API 依据：[AccessCheck](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-accesscheck)、
[文件安全与访问权限](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights)。

## 验证

在正常 Windows 宿主运行，避免 Codex 外层受限令牌干扰原生测试：

```powershell
cd E:\pylianxi\chaiai\local-agent-web
.\.venv\Scripts\python.exe -m pytest tests/test_protected_file_acl.py tests/test_win_sandbox.py tests/test_permission_model_review.py tests/test_windows_ipc.py tests/test_autonomy_policy.py tests/test_external_read_policy.py tests/test_file_system_sandbox.py tests/test_interactive_tools.py -q
.\.venv\Scripts\python.exe -m ruff check src/local_agent/security/policy.py src/local_agent/security/win_sandbox.py tests/test_protected_file_acl.py
.\.venv\Scripts\python.exe -m mypy src
```

- 相关回归 **191 passed / 0 failed**，112.04 秒。包含实际命令执行、受保护文件读写/删除/重命名、
  外部写入拒绝/授权、多授权 SID、父目录删除权限、缓存复核、Node IPC 和持久交互工具。
- Ruff 通过；mypy 全量 **161 个源码文件**通过。
- 专用目录实际复现另一账户拥有 `.git` 文件、直接设置拒绝项报 Windows 错误 5。
- 真实报错文件的只读核验通过；核验前后 DACL 相同，内容 SHA256 仍为空文件 SHA256。
- 上一轮经批准在父目录 `E:\pylianxi\chaiai` 验收，两次 echo 成功：首次含初始化 **67.406 秒**，
  第二次 **0.351 秒**。这是覆盖不足的验收，不能代替实际 `local-agent-web` 工作区验证，见下文纠正。
- 独立 `E:\tmp\chaiai-boundary-*` 临时目录的裸跑写入正对照成功；工作区可写与完全放行两档
  未授权写入均被拒；授予目录读写权限后写入成功。未修改正式权限设置。
- 正式服务已启动，`/api/health` 与首页 HTTP 200，前端与模型配置均就绪；实际服务 PID 9244，
  启动时间晚于本次源码修改。当前档位仍为 danger-full-access，原有 51 会话、3 工作区数量不变。
- 本次补验通过正式服务触发了真实模型对话，详情见下文。未重跑全量后端、未构建 wheel/EXE；
  191 项回归与类型检查沿用上一轮生产代码的验证结果，本次未再修改生产权限逻辑。

原始证据位于忽略目录 `eval/acl-boundary-probe-20261019/`：
`focused-r2.xml`、`regression.xml`、`regression.txt`、`real-file-readonly.json`、
`workspace-acceptance.json`、`service-health.json`、`service-before.json`、`service-after.json`。

初次新增用例为 10 通过、3 失败：父目录删除测试只给了受限 SID 权限，普通账户的第一道检查仍拒绝，
未形成可删除的正对照。补齐普通账户相同权限后，13 项均通过；生产代码没有为此放宽检查。

## 真实目录验收与限制

自动审批审查拒绝了对整个 `E:\pylianxi\chaiai` 调用 grant_write 的验收：可能持久写入整棵树的继承 ACL，
超出专用测试目录验证范围，需要明确批准。未绕过该拒绝，用户明确回复“批准”后才重试。

第一次真实验收被本次生成的对抗性夹具阻止：测试故意为父目录授予 FILE_DELETE_CHILD，
放在项目内导致整库扫描把这些样本作为实际内容处理。系统正确拒绝；不是原缓存文件再次失败。
已将本次新建的 `protected-acl-tests`、`protected-acl-tests-r2`、`protected-acl-regression` 三个夹具目录
完整移动至项目外唯一临时归档，未删除文件、未移动既有缓存。源和目标绝对路径均在操作前核验，
归档位置记在 `fixture-relocation.json`。后续原生对抗性测试应使用项目外唯一临时目录，避免污染扫描。

随后父目录验收与服务健康检查通过，但存在下面记录的实际工作区覆盖遗漏。
本次扫描报告 55 个无法枚举的普通目录，其实际写权限和内部保护范围未完成核实，
由既有 skipped 报告机制持续披露，不能视为这些目录均不可写。没有放宽外部写入授权规则。

## 实际工作区遗漏纠正及正式服务补验

用户再次遇到阻断，路径已经从原 uv 缓存变成 `eval/acl-boundary-probe-20261019/.git`。
这是本次调试创建的 7 字节 `probe\r\n` 文件，所有者为 CodexSandboxOffline；它继承了
实际应用工作区的可写 SID，当前账户又不能修改其 DACL。

只读检查确认：同一文件在父目录工作区 SID 下不可变更，在实际
`E:\pylianxi\chaiai\local-agent-web` 工作区 SID 下可写（访问位 0x2）。新检查逻辑确实已生效，
并正确阻止运行；测试探针遗留和验收目录选错造成了错误交付结论，不能归因于用户文件。

用户要求开始后，仅将内容已核验的该测试文件移至项目外唯一归档，改名为 `probe-marker.txt`。
原路径已不存在，7 字节内容和 SHA256 保留，记录见 `probe-marker-relocation.json`。
原 uv 缓存和真实仓库 Git 元数据未移动。其余验收日志仍保留在原目录。

新增 `actual_workspace_acceptance.py`：从脚本位置定位实际应用目录，并断言完整绝对路径，
不再以当前目录的父目录作为验收根。结果：

- 完全放行档仍启用原写入边界，未使用 escalate_sandbox。
- `python --version` 返回 Python 3.13.12，退出码 0，含初始化 62.836 秒。
- 两次 echo 均退出码 0，分别 0.363、0.376 秒。
- 生产 `start_shell` 启动 `python -u -m http.server 0 --bind 127.0.0.1 --directory website`。
- HTTP 请求 `site.html` 返回 200，63600 字节，与磁盘文件逐字节相同。
- 生产浏览器工具打开该 URL，读取实际标题与正文，成功截图；本次专用浏览器和 HTTP 进程已关闭。
- 项目外唯一临时目录的裸写正对照成功；未授权命令写入拒绝，目录授权后写入成功。

另通过**正在运行的正式服务** `/api/sessions/.../messages`，使用实际工作区及现有模型配置，
新建“实际工作区 Shell 修复验收”对话，要求实际调用两条命令：

- run_shell `python --version`：状态 ok、退出码 0、Python 3.13.12，65980 ms。
- run_shell `echo FORMAL_ACTUAL_WORKSPACE_OK`：状态 ok、退出码 0、输出匹配，372 ms。
- 任务 completed，无审批、无退出沙箱。验收结束后恢复先前选中的会话，验收对话保留供查看。
  会话 ID 为 d0b7f74a114e；没有因此重启正式服务或中断用户其他进程。

直接链路原始截图捕获到入场动画中间态；补拍等待字体和有限入场动画结束，正文 opacity=1。
稳定截图为 `site-actual-workspace-stable.png`，已实际查看确认页面内容可见。

补验原始证据位于同一忽略目录：`actual-workspace-acceptance.json`、`actual-workspace-acceptance.txt`、
`formal-service-acceptance.json`、`formal-service-acceptance.txt`、`probe-marker-relocation.json`、
`stable-preview.json` 和截图。复验命令：

```powershell
cd E:\pylianxi\chaiai\local-agent-web
.\.venv\Scripts\python.exe eval/acl-boundary-probe-20261019/actual_workspace_acceptance.py
```

截图输出禁止覆盖；复验时须将脚本的截图路径改为新的唯一文件名。
本记录的 ACL 修复与探针清理验收时，首次完整扫描/ACL 准备约一分钟，55 个目录未核验警告仍存在。
随后单独优化了重复路径解析和元数据查询，扫描降至 18.38 秒、正式服务首次命令 22.82 秒，
详见 [扫描性能记录](2026-10-19-sandbox-scan-performance.md)。55 个目录的警告未在性能优化中解决。

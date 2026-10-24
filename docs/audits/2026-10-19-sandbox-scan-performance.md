# Windows 首次权限扫描性能优化（2026-10-19）

## 结果与范围

用户同意优先优化固定路径重复解析及目录元数据重复查询，保留原权限边界。
在实际工作区 `E:\pylianxi\chaiai\local-agent-web` 只读对照：扫描 **60.949 → 18.381 秒**，
减少 **69.84%**，约快 **3.32 倍**。两次受保护对象清单、跳过目录清单及受保护对象 ACL 哈希完全相同。

没有新增跨重启持久缓存、普通目录快速放行、忽略依赖/缓存目录或界面进度功能。
全部目录仍按原规则扫描；外部写入仍需授权；保护文件只读核验与失败阻断逻辑保留。

## 根因与实现

之前对每个候选目录执行 `is_symlink`、`is_dir`、真实路径解析，保护分类又再次解析该路径、
用户主目录和系统目录。此前只读分段探针的 63.836 秒中，路径保护分类占 43.221 秒，
路径元数据查询占 14.864 秒，保护对象 ACL 核验仅占 0.052 秒。根已有授权，不需要再次传播根 ACL。

| 文件 | 修改 |
| --- | --- |
| `src/local_agent/security/path_guard.py` | 新增每轮扫描独立的 ProtectedPathContext，固定用户主目录和系统目录只解析一次；普通路径门仍实时取当前边界；两者共用保护判断核心 |
| `src/local_agent/security/win_sandbox.py` | 提取生产扫描器，复用 os.walk 已给出的目录类型；候选路径解析一次，同时用于 junction 判断和保护分类；链接仍不跟随，受保护目录仍剪枝 |
| `tests/test_protected_scan.py` | 覆盖保护分类一致性、上下文刷新、解析次数、枚举类型复用、嵌套主目录/系统目录、junction 和文件符号链接 |
| 根 `README.md`、本记录及前序 ACL 记录 | 同步本轮结果与生效状态 |

没有更改 SID 派生、ACL 授予/拒绝规则、授权撤销、Windows 路径别名归一或受限令牌结构。
环境固定边界仅活到单轮扫描结束，不使用跨扫描的全局缓存。

## 对照及回归

忽略目录 `eval/scan-performance-20261019/compare_scan.py` 保留原扫描实现，
优化前运行 old，优化后调用生产 `_protected_targets` 运行 new，均为只读：

| 项目 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 只读扫描秒数 | 60.949 | 18.381 |
| 受保护对象 | 168 | 168 |
| 无法枚举目录 | 55 | 55 |
| 受保护对象 ACL | 相同 | 相同 |

相关回归 **274 passed / 3 skipped / 0 failed**，122.26 秒；Ruff 通过；
mypy 全量 **161 个源码文件**通过。跳过：本机缺少符号链接创建权限的两项，
以及卷未启用 8.3 短名称的一项。Windows junction 实际用例通过。

对抗性测试夹具位于项目外 `E:\tmp\chaiai-scan-performance-tests-20261019`，没有再次污染生产扫描目录。
复验时使用新的唯一目录：

```powershell
cd E:\pylianxi\chaiai\local-agent-web
$scanTestRoot = Join-Path 'E:\tmp' ('chaiai-scan-tests-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m pytest tests/test_protected_scan.py tests/test_security.py tests/test_protected_file_acl.py tests/test_win_sandbox.py tests/test_permission_model_review.py tests/test_windows_ipc.py tests/test_autonomy_policy.py tests/test_external_read_policy.py tests/test_file_system_sandbox.py tests/test_interactive_tools.py -q --basetemp=$scanTestRoot
.\.venv\Scripts\python.exe -m ruff check src/local_agent/security/path_guard.py src/local_agent/security/win_sandbox.py tests/test_protected_scan.py
.\.venv\Scripts\python.exe -m mypy src
```

## 实际命令及正式服务

实际工作区、完全放行档、保留写入边界、未退出沙箱：

- 首次 `python --version`：20.684 秒，Python 3.13.12，退出码 0。
- 两次 echo：0.396、0.409 秒，退出码 0。
- 生产持久终端启动临时 HTTP，浏览器读取并截图 site.html；HTTP 200、63600 字节与磁盘一致。
- 未授权外部命令写入被拒；目录授权后成功。专用浏览器及 HTTP 进程已关闭。

正式服务重启前确认零活动任务、零待审批、零子进程，精确匹配 API 命令行和正式配置路径后重启。
使用现有模型配置，在实际工作区新建“扫描性能优化验收”对话（15223d4690fe）：

- run_shell `python --version`：状态 ok、退出码 0，**22816 ms**，包含该服务进程的首次准备。
- run_shell `echo FORMAL_ACTUAL_WORKSPACE_OK`：状态 ok、退出码 0，**414 ms**。
- 任务 completed，无审批、无退出沙箱；验收后恢复先前选中的会话。

原始证据：`old.json`、`new.json`、`regression.xml`、`actual-workspace-acceptance.json`、
`formal-service-acceptance.json`、`service-start.json`，及已实际查看的稳定截图 `site-after.png`。

## 限制

首次扫描仍随目录数增长；本机生产首次命令仍需约 21–23 秒，不能承诺任意机器相同耗时。
首次新增根权限时的 Windows ACL 传播成本没有被取消，已有根授权的本机基线不覆盖全新大工作区传播成本。
55 个目录未完整核验的警告仍存在；跨重启缓存、界面进度以及初次扫描后新建保护目录的原有限制未在本轮解决。
没有重跑全量后端、构建 wheel/EXE 或进行跨机器测试。

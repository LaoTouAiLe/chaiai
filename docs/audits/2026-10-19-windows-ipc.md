# Windows 原生 IPC 兼容实现与验收（2026-10-19）

## 授权与范围

用户明确接受“先验证原生兼容方案，再实现”，包括原生组件、构建、分发和安全回归；
保留 Shell 外部写入授权边界。本轮只处理 Windows / Node 管道兼容，不实现全屏 PTY、
服务重启后资源恢复或新循环长期任务结论。前序失败现场见
[根因调查](2026-10-18-windows-development-diagnosis.md)。

## 本轮文件清单

| 文件 | 用途 |
| --- | --- |
| `src/local_agent/security/win_sandbox.py` | 接入挂起初始化、缓存拒绝 SID、runner 参数和祖先目录元数据权限 |
| `src/local_agent/security/win_ipc.py` | 验证二进制、封存缓存、选择架构和调用内部加载器 |
| `src/local_agent/native/windows/ipc_common.h`、`ipc_common.c` | 架构识别、挂起目标加载及子进程内部加载器协议 |
| `src/local_agent/native/windows/ipc_compat.c`、`ipc_loader.c` | 管道/进程/模块兼容钩子、内部加载器入口 |
| `src/local_agent/native/windows/bin/` | x64/x86 四个运行二进制与 manifest.json |
| `src/local_agent/native/windows/README.md` | 原生构建、分发、缓存与兼容边界说明 |
| `scripts/build_windows_ipc.py` | 显式编译与源码/产物哈希一致性检查 |
| `scripts/build_windows_package.py`、`scripts/chaiai.spec` | EXE 收集组件并在构建前后验证 |
| `pyproject.toml`、`.gitignore` | wheel 组件资源与本地 PDB/lib 排除 |
| `tests/test_windows_ipc.py` | 19 项原生功能、初始化失败及写边界回归 |
| `tests/test_permission_model_review.py` | 冻结 runner 参数断言同步新内部组件路径 |
| 根目录 `ARCHITECTURE.md`、`V1_0_DEVELOPMENT_PLAN.md`、`README.md` | 架构、授权范围与当前状态 |
| `local-agent-web/README.md`、本记录及前序诊断记录 | 使用、生效方式、证据与限制 |

`eval/` 下新增复现/验收脚本与原始产物，受现有忽略规则保护，不混入运行时包。

## 根因与修复

Node v22.20.0 / libuv 1.51.0 在 Windows 用命名管道连接子进程 stdio。
CreateNamedPipe 默认 DACL 不采用已补齐的 TokenDefaultDacl 登录 SID 授权；
受限令牌重新打开管道写端时第二次访问检查失败，Node 报 EPERM。
Python 参数、引号或简单延长命令时间无法修复这个失败点。

新增 x64/x86 原生组件，仅在本运行时新建的挂起命令中初始化。新建且未提供显式安全描述符的
首实例管道获得当前登录 SID 的读写授权；已有权限保留，自定义安全描述符保持原样。
应用的 CreateProcess 调用先挂起子进程，完成同架构组件初始化后再恢复，覆盖多级进程链。
主程序和后加载的非系统模块使用导入表及动态导出解析钩子；不修改系统 DLL 的导入表。
DllMain 只保存句柄，实际初始化在加载器锁之外执行。

限制 SID 列表没有加入当前用户 SID。原生组件缺失、校验失败、架构不支持或初始化失败时，
命令失败并报告；没有自动退出沙箱或重放命令。原有 Job、超时、取消、真实退出码与输出链路继续使用。

另修复 Node 通过只有 OWNER RIGHTS 的父目录解析绝对路径时的 lstat 拒绝：仅对当前用户拥有的
祖先目录授予不继承的属性读取/遍历元数据权限，不增加列目录、文件内容读取或写权限。
首次实现误用 SetNamedSecurityInfo 触发 Windows 急切继承传播，回归用时 212.83 秒并导致两个
心跳用例失败；已改用只更新对象的 SetFileSecurity，并新增后代 ACL 不变回归。

## 完整性与分发

- 固定四个二进制文件名，核对 SHA256、PE 架构和清单；构建检查同时核对 C/H 源码哈希。
- 宿主把校验后的字节复制到唯一缓存目录，显式只读/执行权限；内部限制 SID 只拒绝缓存写入，
  宿主持有不继承的目录和文件句柄防替换。即便父目录被授权可写，缓存仍禁止改写、新建和改名。
- Python wheel 和 Windows EXE 包携带四个二进制、清单和源码；排除 PDB/import library。
  构建前检查源码一致性，EXE 构建后复核包内清单。
- 编译器为显式提供的 Zig 0.17.0，官方压缩包 SHA256 已验证；运行时不下载或安装编译器。
  构建命令和原生限制见 [组件说明](../../local-agent-web/src/local_agent/native/windows/README.md)。

哈希校验防止损坏；不宣称抵抗能同时替换宿主分发清单和组件的可信宿主。
缓存句柄随宿主退出关闭，崩溃遗留缓存清理不在本轮实现。

## 已执行验证

联合回归：218 passed、2 skipped，包含权限、进程、文件边界、终端、Shell TLS 恢复。
后续原生专项 19 项和分发规则 5 项合计 24 passed，包含自定义管道 DACL 与持久 Node 输入。
正常宿主下固定工程评测 402 passed、源码指纹未变化。
完整后端：**2480 passed / 8 skipped / 0 failed / 0 errors**，耗时 937.78 秒。
39 条警告来自现有 Starlette/AnyIO、Pydantic、Chroma 弃用提示，本轮不修改依赖。
8 个跳过项包括两项未启用的收费真实模型检查、已安装可选依赖时的缺依赖负例、
两项 POSIX 语义、两项本机不可创建符号链接及一项本卷未启用 8.3 短名检查。
Ruff、全量源码 mypy（161 模块）、原生组件源码一致性和发布兼容基线检查均已通过。

真实开发验收使用生产 `run_shell_command`、`TerminalSession`、`BrowserSession`，
独立工作区与 npm 缓存，不替换项目依赖，不调用付费模型：

| 检查 | 结果 |
| --- | --- |
| 离线 npm 安装脚本，默认 pipe / inherit | 均成功，子进程真实输出 42 |
| 官方 npm registry 联网安装 | 成功，安装 17 包；esbuild postinstall 真正运行 |
| esbuild 0.28.2 同步/异步构建 | 均成功，两个 bundle 实际存在 |
| Vite 8.3.0 生产构建 | 成功，dist/index.html 和资源实际存在 |
| 受限持久终端启动 Vite | 成功，HTTP 200，增量日志可读 |
| 隔离浏览器填中文并点击两次 | 重试后成功，结果“中文验收:2”，截图留存 |
| 资源停止 | 终端实际退出，监听端口关闭 |
| Python wheel | 构建成功，解包复核组件及源码哈希通过，无 PDB/lib |
| Windows EXE | 构建成功，1955 文件 / 141.6 MiB；DLL 自包含与敏感数据扫描通过 |
| EXE 内部命令入口 | 使用包内组件，Node 捕获子进程输出 42、未授权外部文件写入被拒绝 |

浏览器首次 WebSocket 握手在现有 0.25 秒超时下失败，失败浏览器和服务已清理；
只重试服务和浏览器阶段后成功。没有把它记录成首次即成功，也没有在本轮修改浏览器超时策略。

Codex 外层受限环境首次固定工程评测为 385 passed / 17 failed，不能算通过。
独立最小 Shell 复现返回 `windows-acl-run: CreateRestrictedToken 失败：87`；
从正常宿主运行相同固定评测后 402 项全部通过。原生测试要求正常宿主，不能把双重令牌失败
归咎为模型能力或忽略失败断言。

本机原始证据位于 `eval/native-ipc-prototype/`：regression.xml、native-final.txt、
development-acceptance.txt、browser-retry.txt、wheel.txt、package.txt、frozen-acceptance.txt、
fixed-native/report.json、full.xml/full.txt。
真实构建工作区 `eval/native-development-1792340601346459000/` 保留 package-lock、构建物、
result.json 和 browser.png。早期失败日志保留，不作为最终通过证据。

Windows 包位于 `build/packaging/native-ipc-b7547e93ae21409888bc1bc35f39aef6/chaiai/`，
构建未覆盖旧包，未发布或提交 Git。

## 正式服务生效

重启前已确认正式服务没有活动任务、待处理任务或持久子进程，按原 config.yaml 和参数重启。
`http://127.0.0.1:8000/api/health` 返回 ok，模型已配置且前端可用；
重启前后均为 46 个会话、3 个工作区。未覆盖配置、设置或数据库。
新服务启动时间晚于原生适配代码修改时间；证据 `formal-before.json` / `formal-after.json`。

## 尚不能保证的范围

未经测试的第三方程序、绕过上述 Win32 导入的原生调用、受保护进程及非 x64/x86 架构不能保证兼容。
WMI 服务访问和系统 curl Schannel 凭据限制是独立问题，未因管道修复而消失；专用系统查询和下载工具仍可用。
EXE 不等于新机器完整发布验收；代码签名、其它 Windows 版本和防病毒兼容不在本机结果中推断。

依据：[libuv 1.51.0 管道实现](https://github.com/libuv/libuv/blob/v1.51.0/src/win/pipe.c)、
[Microsoft 命名管道安全](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)、
[受限令牌](https://learn.microsoft.com/en-us/windows/win32/secauthz/restricted-tokens)、
[ACL 自动传播](https://learn.microsoft.com/en-us/windows/win32/secauthz/automatic-propagation-of-inheritable-aces)、
[SetFileSecurity 对象级行为](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-setfilesecurityw)。

## 复验

在正常 Windows 宿主终端、`local-agent-web` 目录执行，临时测试根使用新建且位于 Git 工作区之外的目录：

```powershell
.venv/Scripts/python.exe scripts/build_windows_ipc.py --check
.venv/Scripts/python.exe -m pytest tests/test_windows_ipc.py -p no:cacheprovider --basetemp=<新的测试目录> -q
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m mypy src
```

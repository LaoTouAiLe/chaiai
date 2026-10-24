# Windows 本地开发兼容性：根因调查（2026-10-18）

> 后续：用户已接受扩大范围，原生组件实现与验收见
> [2026-10-19 记录](2026-10-19-windows-ipc.md)。本文保留修复前的现场结论。

用户同意先处理 Windows / Node 安装、子进程和启动兼容问题。本次已完成原生对照与失败点定位，
**尚未完成底层修复**；未修改生产权限、启动器、正式配置或服务，也未升级 Node/npm。
全屏终端、资源重启恢复和长期任务结论不在这一项中实现。

## 复现结果

本机 Node v22.20.0、libuv 1.51.0、npm 10.9.3。
相同程序、临时目录与环境，在原生与现行写侧受限令牌下对照：

| 检查 | 原生 | 写侧受限令牌 |
| --- | --- | --- |
| Node 启动、版本查询 | 成功 | 成功 |
| Node spawnSync 默认 pipe | 子进程输出 42，退出 0 | EPERM，子进程状态 null |
| Node spawnSync stdio=inherit | 成功 | 成功 |
| Python subprocess 捕获输出 | 成功 | 成功 |
| PowerShell Get-CimInstance | 成功 | HRESULT 0x80041003，拒绝访问 |
| 系统 curl / Schannel HTTPS | HTTP 200 | SEC_E_NO_CREDENTIALS，退出 35 |

对照脚本 `eval/probe_windows_development.py`，结果 `eval/windows-development-probe.txt`。
它使用全新专属工作区，不读取模型配置或正式聊天；HTTPS 仅访问公开 example.com，保留证书验证。

## Node 的具体失败点

`src/local_agent/security/win_sandbox.py:747` 的 _set_default_dacl 确实为新令牌添加了登录 SID 的内核对象访问权，
子进程内读回 TokenDefaultDacl 已确认这一点。但通过 CreateNamedPipe 创建的默认命名管道 DACL
仍是 System/Administrators/创建者全权、Everyone/Anonymous 只读，没有登录 SID 的写授权。

独立 Win32 诊断不经过 Node：新建服务端管道成功，随后同一进程 CreateFile 打开客户端写端失败，
GetLastError=5；原生对照成功。增加或移除 WRITE_DAC 的客户端请求均仍失败。
这把问题定位到命名管道的第二次访问检查，不能归咎为 Node 程序不存在、Shell 引号或进程无法启动。

libuv 的 Windows stdio 管道实现先创建命名管道，再打开客户端连接；默认安全描述符的授权方式与
受限令牌的双重访问检查相冲突。依据：
[libuv 管道实现](https://github.com/libuv/libuv/blob/v1.51.0/src/win/pipe.c)、
[Microsoft 命名管道访问权限](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)、
[Microsoft 受限令牌](https://learn.microsoft.com/en-us/windows/win32/secauthz/restricted-tokens)。

原始诊断 `eval/probe_named_pipe_child.py`、`eval/pipe-diagnosis.txt`、`eval/pipe-diagnosis-acl.txt`。
这些日志含本机对象 SID，仅作为本地排障材料。

## npm 影响验证

`eval/probe_npm_lifecycle.py` 只用本地生成的 tar.gz 包、--offline、独立缓存和临时工作区。
安装脚本实际调用 Node 子进程，没有模拟成功或修改真实项目依赖：

- 原生下，默认 pipe 和 inherit 两种安装脚本都成功。
- 现行写侧受限令牌下，pipe 安装脚本因 EPERM 失败，npm 退出 1；inherit 成功。
- npm 失败前已运行安装脚本并写入缓存。失败后包目录被 npm 清理，不能据此声称此前没有副作用，
  也不能自动退出沙箱重放整条 npm install。

结果 `eval/npm-lifecycle-probe.txt`、对应专属目录的 result.json。

## 已排除的修改

- 旧对照已证明只移除 LUA_TOKEN 无效，本次不重复带入生产。
- TokenDefaultDacl 的登录 SID 授权已经生效，但不会使上述默认管道 DACL 自动包含它。
- 尝试将新临时令牌的默认 owner 设为登录 SID 被系统以 1307 拒绝；没有改动现有对象 owner。
- 不将当前用户 SID 加入限制 SID 列表：这会同时放宽当前用户对大量磁盘目录的写入，违反用户要求。
- 不关闭证书验证、不把失败改成成功、不绕过外部写授权、不自动提权重试。

## 待确认的实施范围

通用修复不能仅以改变命令参数或 TokenDefaultDacl 宣告完成。若继续保留现有磁盘写边界并兼容
第三方程序自行创建的管道，需要评估新增 Windows 原生兼容组件或调整隔离执行架构。
这会涉及本机原生构建、分发、子进程传播和额外安全回归，已向用户提出范围选择。

现有浏览器、公开下载和原生系统查询入口仍可用；它们覆盖特定用途，不代表任意 Node/WMI/
Schannel 程序的底层问题已修复。所有生产代码保持本轮开始时的状态。

"""Shell 安全判定（架构 §6.6、开发文档 T07/T12）。

判定顺序（主判定是**解析后的命令结构**，黑名单只作最后兜底）：

1. 命令非空；按引号规则切分成 argv，并检测引号之外的 Shell 语法。
2. 可执行文件必须是裸名字（带路径一律提级审批，批准后才执行）。
3. 命中 ``blocked_patterns`` → 视为破坏性命令，强制审批。
4. 命中结构化白名单（平台 + 可执行名 + 参数结构 + 工作目录）→ 免审批放行。
5. 其余 → 需要审批。

执行方式（T12）：

- 没有 Shell 语法：``executor = "argv"``，用可执行文件 + 参数数组直接执行（``shell=False``）。
- 有 Shell 语法（管道、重定向、命令替换、后台执行）：属于"用户明确要求 Shell 语法"，
  必须提高风险等级（需要审批），并交给平台适配器执行。Windows 区分
  ``cmd.exe /d /s /c`` 与 ``pwsh -NoProfile -NonInteractive -Command``，POSIX 用 ``/bin/sh -c``；
  两者的解析规则不同、不能混用，因此适配器由 ``security.shell.executor`` 显式选择：
  取 ``argv_preferred`` 时用平台默认（Windows→cmd、POSIX→sh），也可以直接指定
  ``cmd`` / ``pwsh`` / ``sh``。

工作目录必须在工作区内且真实存在；环境变量默认不透传，且禁止透传任何疑似密钥的变量名。
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_agent.config import (
    ShellSecurityConfig,
    ShellWhitelistEntry,
)
from local_agent.exceptions import SecurityError, WorkspaceBoundaryError
from local_agent.security.path_guard import (
    ExternalPathPolicy,
    protected_target_reason,
    resolve_target,
    resolve_within_workspace,
)

#: 引号**之外**出现任意一个，就说明这不是一条可以直接 argv 执行的命令。
#: 引号内的同名符号是普通参数内容（例如 ``python -c "import sys; sys.exit(0)"``）。
SHELL_SYNTAX_TOKENS: tuple[str, ...] = ("|", ">", "<", "&", ";", "`", "^", "\n")

#: ``$(``、``%VAR%`` 这类命令替换/变量展开也算 Shell 语法。
_COMMAND_SUBSTITUTION = re.compile(r"\$\(|%[A-Za-z_][A-Za-z0-9_]*%")

#: 可执行文件名允许的字符（裸名字，不含路径与扩展名）。
_EXECUTABLE_NAME = re.compile(r"^[A-Za-z0-9._+-]+$")

#: 环境变量名允许的字符。
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 疑似密钥的环境变量名，禁止透传给子进程。
_SECRET_ENV_HINTS: tuple[str, ...] = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

#: 子进程继承的基础环境变量（不含任何密钥）。
BASE_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "SystemDrive",
    "windir",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "LANG",
    "LC_ALL",
)

#: 右栏终端额外继承的变量（2026-10-28）：只补"工具链提示"这一类，让 conda/venv 的提示符
#: （``(base) PS ...``）在终端里出现——缺了这些变量，终端看不出自己在哪个环境里，和用户自己
#: 开的终端是两个样子。
#:
#: 仍然**不含任何密钥类名字**：:func:`build_environment` 对放行名单再做一次密钥名过滤，
#: 所以即使这里手滑写了 ``XXX_API_KEY`` 也透传不出去。
TERMINAL_ENV_KEYS: tuple[str, ...] = (
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "CONDA_PROMPT_MODIFIER",
    "CONDA_SHLVL",
    "CONDA_EXE",
    "CONDA_PYTHON_EXE",
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
    "PROMPT",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
)

_WINDOWS_EXECUTABLE_SUFFIXES: tuple[str, ...] = (".exe", ".cmd", ".bat", ".com")

#: 执行方式（架构 §6.6）。
EXECUTOR_ARGV = "argv"
EXECUTOR_CMD = "cmd"
EXECUTOR_PWSH = "pwsh"
EXECUTOR_SH = "sh"

#: ``executor: argv_preferred`` 时，各平台执行 Shell 语法命令的默认适配器。
DEFAULT_ADAPTERS: Mapping[str, str] = {"windows": EXECUTOR_CMD, "posix": EXECUTOR_SH}

#: 各适配器适用的平台；不匹配说明配置与运行平台冲突，判定直接失败闭合。
_ADAPTER_PLATFORMS: Mapping[str, tuple[str, ...]] = {
    EXECUTOR_CMD: ("windows",),
    EXECUTOR_PWSH: ("windows",),
    EXECUTOR_SH: ("posix",),
}

#: 适配器的人类可读写法（出现在拒绝原因里）。
_ADAPTER_LABELS: Mapping[str, str] = {
    EXECUTOR_CMD: "cmd.exe /d /s /c",
    EXECUTOR_PWSH: "pwsh -NoProfile -NonInteractive -Command",
    EXECUTOR_SH: "/bin/sh -c",
}

#: 各适配器对应的可执行文件名；判定阶段会把它解析成绝对路径。
ADAPTER_EXECUTABLES: Mapping[str, str] = {
    EXECUTOR_CMD: "cmd.exe",
    EXECUTOR_PWSH: "pwsh",
    EXECUTOR_SH: "sh",
}


#: **破坏性命令的代码层底线**：这些命令/参数一律视为"破坏性"，任何档位都必须人工批准。
#:
#: 为什么必须有它（2026-10-14）：四项硬约束里写着"破坏性 Shell 命令始终需要人工审批"，但
#: ``security.shell.blocked_patterns`` **默认是空列表**——即那条约束在出厂配置下没有技术实现，
#: 只指望用户自己配。这在"放行档＝常规命令免审"落地之后会立刻变成真漏洞（`format C:` 直接跑）。
#: 所以把底线放进代码：配置只能**追加**模式，不能取消这些。
#:
#: 收录口径：对应四项硬约束里的"删除文件"与不可逆的系统级动作。短名字带尾随空格，
#: 兜底匹配还要求左侧名称边界，避免误伤 `reformat` / `model` / `card` 这类包含关系。
DESTRUCTIVE_PATTERNS: tuple[str, ...] = (
    # 删除（硬约束"删除文件"）
    "del ",
    "erase ",
    "rm ",
    "rmdir ",
    "rd ",
    "unlink ",
    "shred ",
    # 磁盘 / 分区 / 引导（名字带尾随空格避免子串误伤，如 `scripts/format.py`）
    "format ",
    "diskpart ",
    "mkfs ",
    "fdisk ",
    "dd if=",
    "bootrec ",
    "bcdedit ",
    # 关机 / 重启 / 杀进程
    "shutdown ",
    "pkill ",
    "killall ",
    # 权限 / 所有者 / 注册表 / 计划任务
    "takeown ",
    "icacls ",
    "cacls ",
    "reg delete",
    "reg add",
    "schtasks ",
    "crontab ",
)


#: **只读安全命令的代码层免审清单**（2026-10-14，与 :data:`DESTRUCTIVE_PATTERNS` 对称）。
#:
#: 为什么需要它：结构化白名单（``security.shell.whitelist``）由**用户自己配**，出厂只有几条开发
#: 命令；于是 ``time /t``、``whoami``、``date`` 这类"看一眼就走"的读命令每次都要弹审批——用户的
#: 抱怨正是"问个时间都被安全策略拦住"。codex 的 ``untrusted`` 档是同一个口径：**最严的档下，
#: 已知安全的读命令照样直接跑**。
#:
#: 收录口径（三条同时满足）：① 只输出信息，**不落盘、不改环境、不改时钟**；② 参数**完全一致**
#: （``echo`` 是唯一例外：任意参数都不改变"只输出"这件事）；③ 只对**裸名字**生效，且命令里
#: **不能有 Shell 语法**——``date > f`` 有副作用，照旧走审批那条路。
#:
#: 故意**不收** Windows 的裸 ``time`` / ``date``：不带 ``/t`` 时它们会**询问并修改系统时钟**。
_SAFE_READ_ONLY_EXACT: Mapping[str, tuple[str, ...]] = {
    "windows": ("time /t", "date /t", "ver", "whoami", "hostname"),
    "posix": ("date", "pwd", "uname", "id", "whoami", "hostname"),
}

#: 这些名字**带任意参数**都免审（只有 ``echo``）。
_SAFE_READ_ONLY_ANY_ARGS: Mapping[str, tuple[str, ...]] = {
    "windows": ("echo",),
    "posix": ("echo",),
}


def is_safe_read_only(argv: Sequence[str], platform: str) -> bool:
    """这条命令是否属于**代码层免审的只读安全命令**（见上面两张表的收录口径）。

    与白名单不同，它不看用户配置：**哪个档位都免审**（``read-only`` 档也一样）——这是
    "问个时间不必先点批准"这件事的兜底。判定同样只看**裸名字 + 参数**：带路径的可执行文件、
    含 Shell 语法的命令在这之前就已经走别的分支了。
    """
    if not argv:
        return False
    actual = [str(item) for item in argv]
    if _looks_like_path(actual[0]):
        # **只认裸名字**：`C:\...\whoami.exe` 或 `./whoami` 都可能是别处的同名程序（那正是"带路径的
        # 可执行文件一律提级审批"要防的事）。这一条必须在 `same_executable` 之前判——它会把带路径的
        # 名字归一成 `whoami`，于是清单会放行一个指向别处的程序（2026-10-14 由真值表用例抓到）。
        return False
    folded = [item.casefold() for item in actual]
    # Windows 的开关参数大小写不固定（用户写的就是 `time /T`）：比较时统一 casefold。
    for pattern in _SAFE_READ_ONLY_EXACT.get(platform, ()):
        tokens = split_command(pattern)
        if not tokens or not same_executable(tokens[0], actual[0]):
            continue
        if len(actual) == len(tokens) and folded[1:] == [item.casefold() for item in tokens[1:]]:
            return True
    for name in _SAFE_READ_ONLY_ANY_ARGS.get(platform, ()):
        if same_executable(name, actual[0]) and len(actual) >= 1:
            return True
    return False


#: **平台内建命令**：它们没有独立可执行文件，只能在适配器里跑（`cmd /d /s /c`、`sh -c`）。
#:
#: 为什么单列成表（2026-10-14 实测修正）：这些名字在 argv 模式下 `resolve_executable` **必然**解析不到，
#: 以前会被当成"当前环境找不到可执行文件"**直接硬拒、连审批入口都不给**。实测症状是"查当前时间被拦两次"，
#: 而且其中 `del` / `rd` / `rmdir` 这类**既命中破坏性模式、又永远执行不了**（用户批准了也起不来）。
#: 它们**不是"环境不可用"**，而是"必须交给适配器执行"——所以改走适配器，并按白名单判定。
_CMD_BUILTINS: frozenset[str] = frozenset({
    "assoc", "break", "call", "cd", "chdir", "cls", "color", "copy", "date", "del", "dir", "echo",
    "endlocal", "erase", "exit", "for", "ftype", "goto", "if", "md", "mkdir", "mklink", "move",
    "path", "pause", "popd", "prompt", "pushd", "rd", "rem", "ren", "rename", "rmdir", "set",
    "setlocal", "shift", "start", "subst", "time", "title", "type", "ver", "verify", "vol",
})
_SH_BUILTINS: frozenset[str] = frozenset({
    "alias", "bg", "break", "cd", "command", "continue", "eval", "exec", "exit", "export", "false",
    "fg", "getopts", "hash", "jobs", "local", "printf", "pwd", "read", "return", "set", "shift",
    "test", "times", "trap", "true", "type", "ulimit", "umask", "unalias", "unset", "wait",
})


def is_platform_builtin(name: str, platform: str) -> bool:
    """这个名字是不是**平台适配器的内建命令**。

    区别很关键：内建命令要**交给适配器执行**（可执行、可审批、可白名单免审）；
    而真正解析不到的外部命令才是"环境不可用"，那时如实拒绝、不给审批入口（批了也起不来）。
    """
    table = _CMD_BUILTINS if platform == "windows" else _SH_BUILTINS
    return name.strip().lower() in table


@dataclass(frozen=True)
class ShellPlan:
    """一次 Shell 调用的完整判定与执行参数。"""

    allowed: bool
    requires_approval: bool
    reason: str
    argv: tuple[str, ...] = ()
    executable: str = ""
    cwd: Path | None = None
    #: 实际生效的超时秒数（内部结构，不是工具签名）；早期拒绝的计划沿用出厂默认值。
    timeout_seconds: int | None = None
    env_keys: tuple[str, ...] = field(default_factory=tuple)
    #: 执行方式：``argv`` 直执行，其余为 Shell 适配器（架构 §6.6）。
    executor: str = EXECUTOR_ARGV
    #: Shell 语法命令的原始字符串；``executor == "argv"`` 时为 ``None``。
    shell_command: str | None = None
    #: 触发提级的 Shell 语法字符（没有则为 ``None``）。
    shell_syntax: str | None = None
    #: 判定阶段解析出的可执行文件绝对路径；解析不到时为 ``None``（退回原名执行）。
    #: 执行、动作指纹都取自它，保证"判定对象"与"执行对象"是同一个（架构 §5.3、§6.6）。
    executable_path: str | None = None
    #: 拒绝的原因是**当前环境里根本起不来**（找不到可执行文件或适配器），而不是安全策略。
    #:
    #: 为什么要分开（2026-10-14 实测）：`time /t` 曾被判成"找不到可执行文件"并显示成
    #: 「工具调用被安全策略拦截」，可它对策略毫无关系、用户能做的也不是"改请求去合规"。
    #: 上层（`SafetyDecision.reason_kind` → 安全门的 stage → 投影的错误码）据此给出正确的说法。
    environment_unavailable: bool = False
    #: 命中**破坏性命令**（代码层底线 :data:`DESTRUCTIVE_PATTERNS` 或配置追加的模式）。
    #:
    #: 与 ``requires_approval`` 的区别很重要：后者可以被「放行」档免掉，**前者不行**——四项硬约束
    #: 里的"破坏性 Shell 命令始终需要人工批准"就落在这一位上。
    destructive: bool = False
    #: 命中**只读安全命令**的代码层免审清单（:func:`is_safe_read_only`）：**任何档位都免审**。
    #: 与 ``allowed`` 的区别：那个是"这次判定放行"，这个是"这条命令属于读命令白名单"，
    #: 因此「手动审批」档也不该拦它（2026-10-14）。
    safe_read_only: bool = False

    @property
    def uses_shell_adapter(self) -> bool:
        """是否交给平台适配器执行（而不是 argv 直执行）。"""
        return self.executor != EXECUTOR_ARGV


def prepare_shell(
    command: str,
    *,
    config: ShellSecurityConfig,
    workspace_root: Path,
    cwd: str = ".",
    timeout_seconds: int | None = None,
    env_keys: list[str] | None = None,
    platform: str | None = None,
    external_policy: ExternalPathPolicy | None = None,
) -> ShellPlan:
    """解析并判定一条 Shell 命令。

    Args:
        command: 模型给出的命令字符串。
        config: ``security.shell`` 配置节。
        workspace_root: 工作区根目录，cwd 的边界。
        cwd: 相对工作区根目录的执行目录。
        timeout_seconds: 请求的超时；不得超过配置上限。
        env_keys: 需要额外透传的环境变量名。
        platform: 覆盖平台判定（测试用），默认按配置与当前系统决定。
        external_policy: 工作区外路径的档位与授权（T38）。只有**绝对**路径、且被档位认可的
            外部工作目录才解析得出（受保护目标仍然硬拒绝）。审批与免审白名单的处理在调用方：
            判定侧 (:meth:`SecurityPolicy.classify_tool`) 与执行侧 (``run_shell_command``)
            都要求外部 cwd 必须持有一次性人工批准。

    Returns:
        判定结果与执行参数；不抛异常，拒绝原因放在 ``reason``。
    """
    effective_platform = resolve_platform(config.platform, override=platform)
    timeout = _resolve_timeout(timeout_seconds, config)
    if (timeout_seconds is not None and config.max_timeout_seconds is not None
            and timeout_seconds > config.max_timeout_seconds):
        return ShellPlan(
            False,
            False,
            # 说清"是哪一个键、当前多少、怎么办"：这条是**参数越界**，不是安全边界（2026-10-16）。
            f"超时时间不能超过 security.shell.max_timeout_seconds（当前 {config.max_timeout_seconds} 秒）："
            "把它调小，或把命令拆成几步",
        )

    raw = (command or "").strip()
    if not raw:
        return ShellPlan(False, False, "命令为空", timeout_seconds=timeout)

    adapter, adapter_error = select_adapter(config.executor, effective_platform)
    argv, syntax = _tokenize(raw, cmd=adapter == EXECUTOR_CMD)
    if not argv and syntax is None:
        return ShellPlan(False, False, "命令解析后为空", timeout_seconds=timeout)

    try:
        cwd_target = resolve_target(
            cwd,
            base_dir=workspace_root,
            workspace_root=workspace_root,
            external_policy=external_policy,
        )
    except WorkspaceBoundaryError as exc:
        return ShellPlan(False, False, f"工作目录不合法：{exc}", timeout_seconds=timeout)
    resolved_cwd = cwd_target.path
    if cwd_target.external:
        # 工作区外工作目录：受保护目标（根目录、主目录、系统目录…）直接拒绝，
        # 其余由调用方强制审批（架构 §1.5）。
        protected = protected_target_reason(resolved_cwd)
        if protected:
            return ShellPlan(
                False,
                False,
                f"禁止在受保护目标（{protected}）下执行命令：{resolved_cwd.as_posix()}",
                timeout_seconds=timeout,
            )
    if not resolved_cwd.is_dir():
        return ShellPlan(
            False,
            False,
            f"工作目录不存在或不是目录：{resolved_cwd.as_posix()}",
            timeout_seconds=timeout,
        )

    allowed_env, env_error = _validate_env_keys(env_keys, config)
    if env_error is not None:
        return ShellPlan(False, False, env_error, timeout_seconds=timeout)

    blocked = match_blocked_patterns(argv, raw, config.blocked_patterns)
    executable = argv[0] if argv else ""

    # Shell 语法：不能 argv 直执行，必须经平台适配器，并且提高风险等级（架构 §6.6）
    if syntax is not None:
        if adapter_error is not None:
            return ShellPlan(False, False, adapter_error, timeout_seconds=timeout)
        adapter_name = ADAPTER_EXECUTABLES.get(adapter, adapter)
        adapter_path = resolve_executable(adapter_name, cwd=resolved_cwd)
        if adapter_path is None:
            # 适配器不存在就当场失败闭合：不能让用户先批准、执行时才报"命令无法启动"
            return ShellPlan(
                False,
                False,
                f"当前环境找不到 {adapter_name}（{describe_adapter(adapter)} 适配器不可用），"
                "已拒绝执行",
                argv=tuple(argv),
                executable=executable,
                timeout_seconds=timeout,
                executor=adapter,
                shell_command=raw,
                shell_syntax=syntax,
                environment_unavailable=True,
            )
        reason = (
            f"命令包含 Shell 语法「{syntax}」，已提高风险等级："
            f"需要人工审批后由 {describe_adapter(adapter)} 执行（架构 §6.6）"
        )
        if blocked is not None:
            reason = f"命令命中破坏性命令模式「{blocked}」，{reason}"
        return ShellPlan(
            False,
            True,
            reason,
            argv=tuple(argv),
            executable=executable,
            cwd=resolved_cwd,
            timeout_seconds=timeout,
            env_keys=allowed_env,
            executor=adapter,
            shell_command=raw,
            shell_syntax=syntax,
            executable_path=adapter_path,
            destructive=blocked is not None,
        )

    # **平台内建命令**先定执行方式（详见 `_CMD_BUILTINS` 的说明）：它们不是"环境不可用"，而是
    # 必须经适配器执行。解析到适配器就把执行参数记在 `execution` 里，后面的每个出口都带上它；
    # 适配器本身也解析不到时才如实拒绝（那时审批确实没有意义）。
    execution: dict[str, Any] = {}
    adapter_path_for_builtin: str | None = None
    if argv and is_platform_builtin(executable, effective_platform) and not _looks_like_path(executable):
        if adapter_error is not None:
            return ShellPlan(False, False, adapter_error, timeout_seconds=timeout)
        adapter_name = ADAPTER_EXECUTABLES.get(adapter, adapter)
        adapter_path_for_builtin = resolve_executable(adapter_name, cwd=resolved_cwd)
        if adapter_path_for_builtin is None:
            return ShellPlan(
                False,
                False,
                f"{executable} 是平台内建命令，但当前环境找不到 {adapter_name}"
                f"（{describe_adapter(adapter)} 适配器不可用），已拒绝执行",
                argv=tuple(argv),
                executable=executable,
                timeout_seconds=timeout,
                environment_unavailable=True,
            )
        execution = {"executor": adapter, "shell_command": raw}

    # 黑名单先于"带路径"与"可解析性"判断：破坏性信号比路径风险更该出现在审批说明里，
    # 而且 `rm` / `del` / `chmod` 这类命令在 Windows 上本来就没有可执行文件（属于 shell 内建）。
    # 但"不可执行"的命令不能进入审批：否则用户批准后必然启动失败，判定对象也不等于执行对象。
    if blocked is not None:
        blocked_path = adapter_path_for_builtin or resolve_executable(
            executable, cwd=resolved_cwd
        )
        if blocked_path is None:
            return ShellPlan(
                False,
                False,
                f"命令命中破坏性命令模式「{blocked}」，且当前环境找不到可执行文件：{executable}"
                f"（按工作目录 {resolved_cwd.as_posix()} 解析失败），已拒绝执行",
                timeout_seconds=timeout,
                environment_unavailable=True,
            )
        return ShellPlan(
            False,
            True,
            f"命令命中破坏性命令模式「{blocked}」，强制人工审批（架构 §6.6）",
            argv=tuple(argv),
            executable=executable,
            cwd=resolved_cwd,
            timeout_seconds=timeout,
            env_keys=allowed_env,
            executable_path=blocked_path,
            destructive=True,
            **execution,
        )

    # 判定阶段就固定执行对象：执行与指纹都用它。解析不到就当场拒绝，不再退回原名字
    # 让执行阶段重新搜索 PATH——那正是"判定对象 ≠ 执行对象"的缺口（架构 §5.3、§6.6）。
    executable_path = adapter_path_for_builtin or resolve_executable(
        executable, cwd=resolved_cwd
    )
    if executable_path is None:
        return ShellPlan(
            False,
            False,
            f"当前环境找不到可执行文件：{executable}"
            f"（按工作目录 {resolved_cwd.as_posix()} 解析失败），已拒绝执行",
            timeout_seconds=timeout,
            environment_unavailable=True,
        )

    if _looks_like_path(executable):
        return ShellPlan(
            False,
            True,
            f"可执行文件带有路径（{executable}），需要人工审批后按该路径执行（架构 §6.6）",
            argv=tuple(argv),
            executable=executable,
            cwd=resolved_cwd,
            timeout_seconds=timeout,
            env_keys=allowed_env,
            executable_path=executable_path,
        )
    if not _EXECUTABLE_NAME.match(executable):
        return ShellPlan(
            False,
            False,
            f"可执行文件名不合法：{executable}",
            timeout_seconds=timeout,
        )

    if is_safe_read_only(argv, effective_platform):
        # **只读安全命令**（代码层清单）：直接放行，任何档位都免审。放在白名单之前——
        # 它与用户白名单是两件事："这条命令只读"由代码保证，不依赖用户去配。
        return ShellPlan(
            True,
            False,
            f"{executable} 属于只读安全命令（代码层免审清单），直接执行",
            argv=tuple(argv),
            executable=executable,
            cwd=resolved_cwd,
            timeout_seconds=timeout,
            env_keys=allowed_env,
            executable_path=executable_path,
            safe_read_only=True,
            **execution,
        )

    entry = match_whitelist(
        argv,
        config.whitelist,
        effective_platform,
        cwd=resolved_cwd,
        workspace_root=workspace_root,
    )
    if entry is None:
        return ShellPlan(
            False,
            True,
            f"命令「{executable} {' '.join(argv[1:])}」不在安全白名单内，需要人工审批（T10）",
            argv=tuple(argv),
            executable=executable,
            cwd=resolved_cwd,
            timeout_seconds=timeout,
            env_keys=allowed_env,
            executable_path=executable_path,
            **execution,
        )

    return ShellPlan(
        True,
        False,
        f"命中白名单：{entry.executable} {' '.join(entry.args)}".strip()
        + ("（参数前缀匹配）" if entry.match == "prefix" else ""),
        argv=tuple(argv),
        executable=executable,
        cwd=resolved_cwd,
        timeout_seconds=timeout,
        env_keys=allowed_env,
        executable_path=executable_path,
        **execution,
    )


def split_command(command: str) -> list[str]:
    """按引号规则把命令切成 argv（反斜杠按字面处理，避免破坏 Windows 路径）。"""
    return _tokenize(command)[0]


def select_adapter(configured: str, platform: str) -> tuple[str, str | None]:
    """选择 Shell 语法命令的适配器。

    Args:
        configured: ``security.shell.executor`` 的取值。
        platform: 已解析的平台（``windows`` / ``posix``）。

    Returns:
        （适配器, 错误说明）；配置与平台冲突时适配器为空串、错误说明非空。
    """
    if configured == "argv_preferred":
        return DEFAULT_ADAPTERS[platform], None
    if platform not in _ADAPTER_PLATFORMS.get(configured, ()):
        return "", (
            f"security.shell.executor 指定的适配器 {configured} 不适用于 {platform} 平台，"
            "已按失败闭合拒绝"
        )
    return configured, None


def describe_adapter(adapter: str) -> str:
    """适配器的可读写法（用于提示与拒绝原因）。"""
    return _ADAPTER_LABELS.get(adapter, adapter)


def build_shell_invocation(plan: ShellPlan, *, adapter: str | None = None) -> list[str]:
    """把判定结果转成交给 ``subprocess`` 的 argv（调用方始终 ``shell=False``）。

    - ``executor == "argv"``：可执行文件 + 参数数组，直接执行。
    - 其余：适配器的固定前缀 + 原始命令（作为**单个参数**），由适配器自己解析。
      cmd 与 pwsh 的解析规则不同，不能混用（架构 §6.6）。

    首元素优先用判定阶段解析出的绝对路径（``plan.executable_path``）：这样执行的对象
    就是判定与批准时的那个，不会被期间变化的 PATH 悄悄换成别的同名程序；解析不到时
    才退回原名字（执行结果与判定时的环境一致）。

    Raises:
        SecurityError: 计划处于需要适配器执行却没有原始命令的非法状态。
    """
    chosen = adapter or plan.executor
    if chosen in ("", EXECUTOR_ARGV):
        if not plan.argv:
            return []
        return [plan.executable_path or plan.argv[0], *plan.argv[1:]]
    if plan.shell_command is None:  # pragma: no cover - 适配器执行必然带原始命令
        raise SecurityError("Shell 适配器执行缺少原始命令")
    launcher = plan.executable_path or ADAPTER_EXECUTABLES.get(chosen, chosen)
    if chosen == EXECUTOR_CMD:
        return [launcher, "/d", "/s", "/c", plan.shell_command]
    if chosen == EXECUTOR_PWSH:
        return [launcher, "-NoProfile", "-NonInteractive", "-Command", plan.shell_command]
    if chosen == EXECUTOR_SH:
        return [launcher, "-c", plan.shell_command]
    raise SecurityError(f"未知的 Shell 适配器：{chosen}")


def resolve_executable(name: str, *, cwd: Path | None = None) -> str | None:
    """把可执行名解析成绝对路径；解析不到返回 ``None``。

    - 相对路径（``./tool``、``tools\\tool.exe``）：按**本次命令的 cwd** 解析，而不是进程 cwd；
    - 绝对路径：直接判断该文件是否存在（不做 PATH 搜索）；
    - 裸名字：用 :func:`shutil.which` 按 PATH 解析，并归一成绝对路径。

    判定、指纹与执行都调用它，因此三者看到的是同一个执行对象（架构 §5.3、§6.6）。
    """
    if not name:
        return None
    if _looks_like_path(name):
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = (cwd or Path.cwd()) / candidate
        return str(candidate.resolve()) if candidate.is_file() else None
    found = shutil.which(name)
    return str(Path(found).resolve()) if found else None


def match_whitelist(
    argv: list[str],
    entries: list[ShellWhitelistEntry],
    platform: str,
    *,
    cwd: Path | None = None,
    workspace_root: Path | None = None,
) -> ShellWhitelistEntry | None:
    """按平台、可执行名、参数结构与工作目录匹配白名单。

    ``match: exact``（默认）表示参数必须完全一致：``pytest`` 带任意测试参数、
    ``git diff`` 带额外参数都不会命中免审条目（架构 §6.6）。
    ``match: prefix`` 表示条目参数是实际参数的**前缀**：条目 ``npm run build`` 能命中
    ``npm run build -- --watch``。它把该命令的后续参数**整体纳入信任**，所以只适合参数不改变
    副作用范围的命令；空参数已被配置层校验拒绝（那是"放行整个可执行文件"）。
    条目可选 ``cwd`` 限定该命令只在指定目录下免审。
    """
    for entry in entries:
        if entry.platform and platform not in entry.platform:
            continue
        if not same_executable(entry.executable, argv[0]):
            continue
        actual = list(argv[1:])
        declared = list(entry.args)
        if entry.match == "exact":
            if actual != declared:
                continue
        elif actual[: len(declared)] != declared:
            continue
        if not _cwd_allowed(entry, cwd=cwd, workspace_root=workspace_root):
            continue
        return entry
    return None


def match_blocked_patterns(
    argv: Sequence[str], command: str, patterns: Sequence[str]
) -> str | None:
    """破坏性命令匹配：**先查代码层底线，再查配置追加的模式**。

    底线（:data:`DESTRUCTIVE_PATTERNS`）是不可配置的：四项硬约束里的"破坏性 Shell 命令必须人工批准"
    要求它由代码保证，配置只能**追加**、不能删（``blocked_patterns`` 默认是空列表，所以如果没有这条
    底线，"破坏性命令要问"在出厂配置下等于没有落地——放行档一开，`format C:` 就会直接执行）。
    """
    # 先配置后底线：配置命中时返回**用户自己的措辞**（更具体、更贴近他的语境），底线只兜住
    # 配置没覆盖到的那些命令。两者的"要不要问"效果一致，差别只在拒绝说明里显示哪个模式名。
    return _match_patterns(argv, command, patterns) or _match_patterns(
        argv, command, DESTRUCTIVE_PATTERNS, command_boundaries=True,
    )


def _match_patterns(
    argv: Sequence[str], command: str, patterns: Sequence[str], *, command_boundaries: bool = False,
) -> str | None:
    """按一组模式做兜底匹配（架构 §6.6 末段：只作最后一道，不是安全边界）。

    1. 结构匹配：把 pattern 按同一套规则切成 argv，比较**归一化后的可执行名**，
       并要求 pattern 的其余 token 是实际参数的前缀。这样 ``format``、``FORMAT.COM``、
       ``C:\\Windows\\System32\\format.com`` 会被同一条规则拦住。
    2. 子串兜底：内置命令要求左侧名称边界，自定义模式保持原样子串语义。
    """
    if not patterns:
        return None
    actual = list(argv)
    for pattern in patterns:
        raw_pattern = pattern or ""
        if not raw_pattern.strip():
            continue
        tokens = split_command(raw_pattern)
        if actual and tokens and not _looks_like_path(tokens[0]):
            prefix = tokens[1:]
            if same_executable(tokens[0], actual[0]) and actual[1 : 1 + len(prefix)] == prefix:
                return pattern
        # 保留对链式命令、嵌套 Shell 和脚本中可疑命令文本的保守匹配，但内置 rd 等短名字
        # 不能命中 card。用户显式追加的自定义模式仍可匹配任意子串。
        matched = (
            re.search(rf"(?<![\w.-]){re.escape(raw_pattern)}", command, flags=re.IGNORECASE)
            if command_boundaries else raw_pattern.casefold() in command.casefold()
        )
        if matched:
            return pattern
    return None


def same_executable(configured: str, actual: str) -> bool:
    """比较可执行名：忽略大小写、目录前缀与 Windows 可执行后缀。

    只做归一化比较，不做别名映射：``python3`` 不等于 ``python``、``pip3`` 不等于 ``pip``，
    别名不能因为名字相近就共享免审资格（架构 §6.6）。
    """
    return _normalize_executable(configured) == _normalize_executable(actual)


def resolve_platform(configured: str, *, override: str | None = None) -> str:
    """把 ``security.shell.platform`` 解析成 ``windows`` / ``posix``。"""
    if override:
        return override
    if configured == "auto":
        return "windows" if os.name == "nt" else "posix"
    return configured


#: Windows PowerShell 5.1 的固定位置（系统自带，不依赖 PATH）。
_WINDOWS_POWERSHELL_PARTS: tuple[str, ...] = (
    "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def resolve_terminal_shell(preference: str = "auto") -> tuple[str, bool]:
    """挑一个终端 shell，返回 ``(可执行文件路径, 是否 PowerShell)``。

    为什么默认用 PowerShell（2026-10-28 用户要求）：用户自己开的终端就是 PowerShell，
    进到应用里却变成 cmd，命令写法与提示符都不一样。``cmd`` 仍是可选值——PowerShell 要加载
    profile，比 cmd 慢，习惯 cmd 的人也不该被强制换掉。

    **找不到就退回 cmd**：不在"该用哪个 shell"上抛错。抛错会让终端整个不可用，而 cmd 一定
    存在；配置写了 ``pwsh`` 但没装时同理，退回系统自带的 Windows PowerShell。
    """
    system_root = Path(os.environ.get("SystemRoot", "C:/Windows"))
    cmd = str(system_root / "System32" / "cmd.exe")
    if preference == "cmd":
        return cmd, False
    if preference in ("auto", "pwsh"):
        found = shutil.which("pwsh")
        if found is not None:
            return found, True
    if preference in ("auto", "pwsh", "powershell"):
        legacy = system_root.joinpath(*_WINDOWS_POWERSHELL_PARTS)
        if legacy.is_file():
            return str(legacy), True
    return cmd, False


def terminal_argv(shell: str, *, load_profile: bool) -> list[str]:
    """交互式终端的启动参数。

    - ``cmd`` 用 ``/D /Q``：关掉 AutoRun（注册表里的命令会让终端行为随机器变化）与回显；
    - PowerShell：默认加载 profile——conda 的 ``(base)`` 提示符就是 profile 里那个 ``prompt``
      函数给出的；``load_profile=False`` 时加 ``-NoProfile``，启动快但没有环境提示。
    """
    name = Path(shell).name.casefold()
    if name in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
        return [shell] if load_profile else [shell, "-NoProfile"]
    return [shell, "/D", "/Q"]


def build_environment(env_keys: tuple[str, ...]) -> dict[str, str]:
    """构造子进程环境：只含基础变量与显式放行的变量名，不含任何密钥。

    放行名单有两类来源：用户显式声明的（已过 :func:`validate_env_keys`）与代码里的常量
    （例如 :data:`TERMINAL_ENV_KEYS`）。常量这一路没有校验入口，所以这里**统一再过滤一次**
    密钥类名字——多一道判断的代价可以忽略，漏传一个 ``XXX_API_KEY`` 的代价不行。
    """
    environment = {key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}
    for key in env_keys:
        # 1. 密钥类名字一律不传（常量名单也要过这一关）；
        # 2. **进程里没有的变量不要写空串**：conda 的 profile 钩子会拿 `$env:CONDA_EXE`
        #    去调程序，一个空值会让它自己崩在启动阶段（2026-10-28 实测）；"变量不存在"
        #    时钩子会走另一条分支。
        if _is_secret_key(key) or key not in os.environ:
            continue
        environment[key] = os.environ[key]
    return environment


def _is_secret_key(name: str) -> bool:
    """变量名看起来像密钥（``*_API_KEY`` / ``*_TOKEN`` / ``*_PASSWORD`` 等）。"""
    upper = name.upper()
    return any(hint in upper for hint in _SECRET_ENV_HINTS)


def _normalize_executable(name: str) -> str:
    base = Path(name.replace("\\", "/")).name.casefold()
    for suffix in _WINDOWS_EXECUTABLE_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _looks_like_path(token: str) -> bool:
    return "/" in token or "\\" in token or ":" in token


def _cwd_allowed(
    entry: ShellWhitelistEntry, *, cwd: Path | None, workspace_root: Path | None
) -> bool:
    """条目限定 ``cwd`` 时，必须与解析后的执行目录一致。"""
    if entry.cwd is None:
        return True
    if cwd is None or workspace_root is None:  # pragma: no cover - 判定顺序保证 cwd 已解析
        return False
    try:
        expected = resolve_within_workspace(
            entry.cwd, base_dir=workspace_root, workspace_root=workspace_root
        )
    except WorkspaceBoundaryError:
        return False
    return expected == cwd


def _tokenize(command: str, *, cmd: bool = False) -> tuple[list[str], str | None]:
    """切分命令，并返回引号外出现的第一个 Shell 语法字符（没有则为 ``None``）。"""
    tokens: list[str] = []
    current: list[str] = []
    unquoted: list[str] = []
    quote: str | None = None
    syntax: str | None = None

    for char in command.strip():
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
                if quote == '"':
                    # 双引号内仍有变量/命令展开；cmd 不认识单引号引用。
                    unquoted.append(char)
                    if char == "`" and not cmd and syntax is None:
                        syntax = char
            continue
        if char == '"' or (char == "'" and not cmd):
            quote = char
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        if char in SHELL_SYNTAX_TOKENS:
            if syntax is None:
                syntax = char
            continue
        unquoted.append(char)
        current.append(char)

    if current:
        tokens.append("".join(current))
    if syntax is None:
        match = _COMMAND_SUBSTITUTION.search("".join(unquoted))
        if match:
            syntax = match.group(0)
    return tokens, syntax


def _resolve_timeout(requested: int | None, config: ShellSecurityConfig) -> int | None:
    """定住有效超时；``None`` 表示不设截止时间，超出配置上限由 prepare_shell 拒绝。

    2026-10-16：上限**独立成键**（``security.shell.max_timeout_seconds``，默认 ``None`` = 不设上限）。
    原来上限就是 ``default_timeout_seconds`` 自己（默认 60 秒），于是"省略时用多少"与"最多允许多少"
    挤在一个键里：模型想把慢命令跑完就得先猜这个数，猜大了直接被拒（用户原话"完全就是限制 agent 能力"）。
    """
    if requested is None or requested <= 0:
        return config.default_timeout_seconds
    cap = config.max_timeout_seconds
    if cap is not None and requested > cap:
        return None
    return requested


def _validate_env_keys(
    env_keys: list[str] | None, config: ShellSecurityConfig
) -> tuple[tuple[str, ...], str | None]:
    """校验需要透传的环境变量名；返回（放行列表, 错误说明）。"""
    del config  # 目前所有疑似密钥的名字一律拒绝，不提供按配置放开的入口
    if not env_keys:
        return (), None
    allowed: list[str] = []
    for key in env_keys:
        name = (key or "").strip()
        if not _ENV_KEY.match(name):
            return (), f"环境变量名不合法：{key}"
        upper = name.upper()
        if any(hint in upper for hint in _SECRET_ENV_HINTS):
            return (), f"禁止透传疑似密钥的环境变量：{name}"
        allowed.append(name)
    return tuple(allowed), None

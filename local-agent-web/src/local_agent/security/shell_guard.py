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

from local_agent.config import (
    DEFAULT_SHELL_TIMEOUT_SECONDS,
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
    timeout_seconds: int = DEFAULT_SHELL_TIMEOUT_SECONDS
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
    if timeout is None:
        return ShellPlan(
            False,
            False,
            f"超时时间不能超过配置上限 {config.default_timeout_seconds} 秒",
        )

    raw = (command or "").strip()
    if not raw:
        return ShellPlan(False, False, "命令为空", timeout_seconds=timeout)

    adapter, adapter_error = select_adapter(config.executor, effective_platform)
    argv, syntax = _tokenize(raw)
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
        )

    # 黑名单先于"带路径"与"可解析性"判断：破坏性信号比路径风险更该出现在审批说明里，
    # 而且 `rm` / `del` / `chmod` 这类命令在 Windows 上本来就没有可执行文件（属于 shell 内建）。
    # 但"不可执行"的命令不能进入审批：否则用户批准后必然启动失败，判定对象也不等于执行对象。
    if blocked is not None:
        blocked_path = resolve_executable(executable, cwd=resolved_cwd)
        if blocked_path is None:
            return ShellPlan(
                False,
                False,
                f"命令命中破坏性命令模式「{blocked}」，且当前环境找不到可执行文件：{executable}"
                f"（按工作目录 {resolved_cwd.as_posix()} 解析失败），已拒绝执行",
                timeout_seconds=timeout,
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
        )

    # 判定阶段就固定执行对象：执行与指纹都用它。解析不到就当场拒绝，不再退回原名字
    # 让执行阶段重新搜索 PATH——那正是"判定对象 ≠ 执行对象"的缺口（架构 §5.3、§6.6）。
    executable_path = resolve_executable(executable, cwd=resolved_cwd)
    if executable_path is None:
        return ShellPlan(
            False,
            False,
            f"当前环境找不到可执行文件：{executable}"
            f"（按工作目录 {resolved_cwd.as_posix()} 解析失败），已拒绝执行",
            timeout_seconds=timeout,
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
        )

    return ShellPlan(
        True,
        False,
        f"命中白名单：{entry.executable} {' '.join(entry.args)}".strip(),
        argv=tuple(argv),
        executable=executable,
        cwd=resolved_cwd,
        timeout_seconds=timeout,
        env_keys=allowed_env,
        executable_path=executable_path,
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

    ``match: exact`` 表示参数必须完全一致：``pytest`` 带任意测试参数、
    ``git diff`` 带额外参数都不会命中免审条目（架构 §6.6）。
    条目可选 ``cwd`` 限定该命令只在指定目录下免审。
    """
    for entry in entries:
        if entry.platform and platform not in entry.platform:
            continue
        if not same_executable(entry.executable, argv[0]):
            continue
        if list(argv[1:]) != list(entry.args):
            continue
        if not _cwd_allowed(entry, cwd=cwd, workspace_root=workspace_root):
            continue
        return entry
    return None


def match_blocked_patterns(
    argv: Sequence[str], command: str, patterns: Sequence[str]
) -> str | None:
    """黑名单兜底匹配（架构 §6.6 末段：只作最后一道，不是安全边界）。

    1. 结构匹配：把 pattern 按同一套规则切成 argv，比较**归一化后的可执行名**，
       并要求 pattern 的其余 token 是实际参数的前缀。这样 ``format``、``FORMAT.COM``、
       ``C:\\Windows\\System32\\format.com`` 会被同一条规则拦住。
    2. 子串匹配：pattern 不是合法命令形态时的兜底。
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
        # 子串兜底用**原样 pattern**（示例配置里的尾随空格就是用来避免误伤 reformat 这类名字）
        if raw_pattern.casefold() in command.casefold():
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


def build_environment(env_keys: tuple[str, ...]) -> dict[str, str]:
    """构造子进程环境：只含基础变量与显式放行的变量名，不含任何密钥。"""
    environment = {key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}
    for key in env_keys:
        environment[key] = os.environ.get(key, "")
    return environment


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


def _tokenize(command: str) -> tuple[list[str], str | None]:
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
            continue
        if char in {'"', "'"}:
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
    if requested is None or requested <= 0:
        return config.default_timeout_seconds
    if requested > config.default_timeout_seconds:
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

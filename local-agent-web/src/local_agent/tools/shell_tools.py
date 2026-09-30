"""安全 Shell 工具：``run_shell``（架构 §6.6、开发文档 T07/T12）。

执行方式固定为 ``shell=False`` + argv 数组，工作目录与环境变量都由安全策略给出：

- 命中结构化白名单的命令直接执行；其余命令需要人工批准，批准后按同样的结构执行；
- 含 Shell 语法的命令由平台适配器执行（Windows ``cmd.exe /d /s /c`` 或
  ``pwsh -NoProfile -NonInteractive -Command``、POSIX ``/bin/sh -c``），适配器本身
  仍以 argv 数组启动，不交给 Python 的 ``shell=True``；
- 输出按 ``security.shell.max_output_chars`` 截断，超时会被杀掉并标记；
- 子进程环境只含基础变量与显式放行的变量名，**不含任何密钥**；
- 退出码、标准输出、错误输出、超时状态、执行方式与耗时都会返回，供反思节点机械校验。
"""

from __future__ import annotations

import locale
import subprocess
import time

from langchain_core.tools import BaseTool, tool

from local_agent.exceptions import SecurityError
from local_agent.security.approval import action_fingerprint
from local_agent.security.policy import SecurityPolicy
from local_agent.security.shell_guard import (
    build_environment,
    build_shell_invocation,
    prepare_shell,
)
from local_agent.tools.result_types import ShellResult

#: 输出解码失败时的替代字符策略：Windows 控制台常用本地代码页。
_ERRORS = "replace"


def run_shell_command(
    command: str,
    *,
    policy: SecurityPolicy,
    cwd: str = ".",
    timeout_seconds: int | None = None,
    env_keys: list[str] | None = None,
) -> ShellResult:
    """执行一条通过安全判定的命令。

    Args:
        command: 命令字符串。含 Shell 语法（管道、重定向等）时必须持有有效人工批准。
        policy: 安全策略。
        cwd: 相对工作区根目录的执行目录。
        timeout_seconds: 超时秒数，不得超过配置上限。
        env_keys: 需要额外透传的环境变量名（禁止疑似密钥的名字）。

    Returns:
        退出码、输出、超时状态、执行方式与耗时。

    Raises:
        SecurityError: 判定不放行、没有有效批准，或命令无法启动。
    """
    # 先定住有效超时：省略时取**配置值**（`security.shell.default_timeout_seconds`），
    # 与安全判定、动作指纹、工具 Schema 完全同一语义。判定与指纹必须用同一个值，
    # 否则审批节点按默认值算、工具按另一套算，批准会莫名失效。
    requested_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else policy.security.shell.default_timeout_seconds
    )
    plan = prepare_shell(
        command,
        config=policy.security.shell,
        workspace_root=policy.workspace_root,
        cwd=cwd,
        timeout_seconds=requested_timeout,
        env_keys=env_keys,
        external_policy=policy.external_policy,
    )
    if plan.cwd is None:
        # 判定没给出可执行目录（命令为空、cwd 非法、超时超限等）→ 失败闭合
        raise SecurityError(plan.reason)

    # 与审批节点同源的指纹：只有"同一次调用 + 指纹一致"的批准才能放行（架构 §5.3）
    fingerprint = action_fingerprint(
        "run_shell",
        {
            "command": command,
            "cwd": cwd,
            "timeout_seconds": requested_timeout,
            "env_keys": list(env_keys or []),
        },
        policy=policy,
    )
    if policy.is_external(plan.cwd) and not policy.is_granted(fingerprint):
        # 执行侧再拦一次：工作区外 cwd 即使被判为免审也不执行（架构 §1.5）
        raise SecurityError(
            f"工作区外工作目录（{plan.cwd.as_posix()}）必须人工审批：{plan.reason}"
        )
    if not plan.allowed and not policy.is_granted(fingerprint):
        raise SecurityError(plan.reason)

    encoding = locale.getpreferredencoding(False) or "utf-8"
    started = time.perf_counter()
    timed_out = False
    try:
        completed = subprocess.run(
            build_shell_invocation(plan),
            cwd=str(plan.cwd),
            env=build_environment(plan.env_keys),
            shell=False,
            capture_output=True,
            timeout=plan.timeout_seconds,
            check=False,
        )
        stdout_bytes, stderr_bytes, exit_code = (
            completed.stdout,
            completed.stderr,
            completed.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_bytes = exc.stdout or b""
        stderr_bytes = exc.stderr or b""
        exit_code = None
    except OSError as exc:
        raise SecurityError(f"命令无法启动（{type(exc).__name__}）：{exc}") from exc

    duration_ms = int((time.perf_counter() - started) * 1000)
    max_chars = policy.security.shell.max_output_chars
    stdout, stdout_cut = _decode_and_truncate(stdout_bytes, encoding, max_chars)
    stderr, stderr_cut = _decode_and_truncate(stderr_bytes, encoding, max_chars)
    truncated = stdout_cut or stderr_cut

    notes: list[str] = []
    if timed_out:
        notes.append(f"命令在 {plan.timeout_seconds} 秒内未结束，已终止")
    if truncated:
        notes.append(f"输出超过 {max_chars} 字符，已截断")

    return ShellResult(
        command=command,
        argv=build_shell_invocation(plan),
        cwd=str(plan.cwd),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_ms=duration_ms,
        truncated=truncated,
        note="；".join(notes) if notes else None,
        executor=plan.executor,
    )


def build_shell_tools(policy: SecurityPolicy) -> list[BaseTool]:
    """按安全策略构造 Shell 工具。"""

    @tool
    def run_shell(
        command: str,
        cwd: str = ".",
        # 省略（None）时用 security.shell.default_timeout_seconds，不写死常量，
        # 否则"配置调小 + 模型省略参数"会在执行阶段被判超限
        timeout_seconds: int | None = None,
        env_keys: list[str] | None = None,
    ) -> str:
        """执行一条命令，返回退出码、标准输出与错误输出。

        命中安全白名单的简单命令（如 `python --version`、`git status`）直接执行；
        管道、重定向、命令替换、带路径的可执行文件与不在白名单内的命令都需要人工批准，
        批准后才执行；含 Shell 语法的命令由平台适配器（cmd.exe / pwsh / sh）执行。

        Args:
            command: 命令字符串。
            cwd: 相对路径以当前工作区为根；配置允许外部路径时可使用显式绝对路径，工作区外工作目录必须人工审批。
            timeout_seconds: 超时秒数；省略时用 `security.shell.default_timeout_seconds`，
                不得超过同一配置值。
            env_keys: 需要额外透传的环境变量名，禁止密钥类变量名。
        """
        return run_shell_command(
            command,
            policy=policy,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env_keys=env_keys,
        ).model_dump_json()

    return [run_shell]


def _decode_and_truncate(data: bytes, encoding: str, max_chars: int) -> tuple[str, bool]:
    """解码子进程输出并按上限截断。"""
    text = data.decode(encoding, errors=_ERRORS)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True

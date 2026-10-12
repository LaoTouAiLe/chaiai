"""子进程的启动与终止（架构 §17.4；v0.6 T60 第二片）。

为什么需要它：

- ``subprocess.run(timeout=...)`` 超时只 ``kill()`` **直接子进程**，而 ``run_shell`` 常常启动的是
  Shell 适配器（``cmd.exe`` / ``pwsh`` / ``sh``），真正干活的是适配器的**子进程**：杀掉父进程，
  命令仍在后台继续跑。
- "取消"要求能在**执行途中**终止，而 ``subprocess.run`` 是一次阻塞调用，中途无从干预。

所以这里改用 ``Popen`` 并显式管理**进程树**。平台差异集中在两处：:func:`_spawn_kwargs`（怎么起）
与 :func:`_terminate_tree`（怎么杀）——Windows 用 ``taskkill /F /T``，POSIX 用 ``killpg``
（先 ``SIGTERM``，宽限期后 ``SIGKILL``）。

**与两道门的分工**（§17.4）：工具**能不能跑**由能力可见性门与安全 / 审批门决定；本模块只负责
"已经跑起来的进程怎么结束"。启动前那一句"令牌已取消就不启动"是**进程生命周期**的一部分
（它是关闭"门已通过 → 取消发生 → 进程照样启动"这个窗口的唯一位置），不参与准入判定。

**如实记录**：终止的手段与系统返回一律写进说明，退出码只写 ``returncode`` 的真实值，
**不伪造"已停止"**。
"""

from __future__ import annotations

import locale
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.cancellation import CancellationToken

logger = logging.getLogger(__name__)

#: 终止后等它真正退出的宽限（秒）。POSIX 先 ``SIGTERM``、宽限后 ``SIGKILL``。
GRACE_SECONDS = 3.0

#: 终止之后再读一次输出的等待上限（秒）：读不到就如实说明，绝不再阻塞执行。
DRAIN_SECONDS = 2.0

PLATFORM_WINDOWS = "windows"
PLATFORM_POSIX = "posix"

#: Windows 上按进程树强杀用的系统工具（随系统自带）。
_TASKKILL = "taskkill"


class ProcessCancelledError(RuntimeError):
    """进程**启动前**发现本轮已取消。

    调用方据此如实报成"命令未执行"，而不是"执行失败"——两者的原因完全不同，不能混为一谈。
    """


@dataclass(frozen=True)
class ProcessOutcome:
    """一次子进程执行的结果（退出码是真实值，终止手段写在 `termination` 里）。"""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False
    #: 终止说明：被谁终止、用了什么手段、系统返回了什么；`None` = 没有终止过。
    termination: str | None = None


@dataclass
class RunningProcess:
    """一个已登记的**在跑**子进程。"""

    handle: subprocess.Popen[bytes]
    argv: tuple[str, ...]
    #: 终止说明（同上）；首次终止时写入，之后**不覆盖**——第一个原因是真实发生的那个。
    termination: str | None = None
    #: 是否因**取消**而终止（决定结果里怎么说）。
    cancelled: bool = False


class ProcessRegistry:
    """一次运行里的子进程登记表（线程安全），与取消令牌绑定。

    一次运行一个；取消时终止**全部**在跑进程（含子进程树），因此"取消"不会留下孤儿进程。
    """

    def __init__(self, cancellation: CancellationToken | None = None) -> None:
        self._lock = threading.Lock()
        self._live: list[RunningProcess] = []
        self._cancellation = cancellation
        if cancellation is not None:
            cancellation.add_cancel_hook(self._on_cancel)

    @property
    def cancelled(self) -> bool:
        """本轮是否已被取消。"""
        token = self._cancellation
        return token is not None and token.cancelled

    def spawn(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        platform: str | None = None,
    ) -> RunningProcess:
        """启动并登记一个子进程。

        Raises:
            ProcessCancelledError: 启动**前**本轮已取消——不启动，也不谎称执行过。
            OSError: 进程启动失败（调用方按"命令无法启动"如实上报）。
        """
        command = [str(item) for item in argv]
        if not command:
            msg = "spawn 需要非空 argv"
            raise ValueError(msg)
        if self.cancelled:
            raise ProcessCancelledError(self._cancel_text())

        name = resolve_platform_name(platform)
        handle: subprocess.Popen[bytes] = subprocess.Popen(
            command,
            cwd=None if cwd is None else str(cwd),
            env=env,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_spawn_kwargs(name),
        )
        process = RunningProcess(handle=handle, argv=tuple(command))
        with self._lock:
            self._live.append(process)
        if self.cancelled:
            # 竞态：预检之后、登记之前发生了取消 → 当场终止，绝不留孤儿（这一句就是那个线性化点）。
            self.terminate(process, reason="取消与命令启动同时发生", cancelled=True, platform=name)
        return process

    def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float,
        platform: str | None = None,
    ) -> ProcessOutcome:
        """启动、等待；超时或取消时**按进程树**终止，并如实返回结果。"""
        process = self.spawn(argv, cwd=cwd, env=env, platform=platform)
        try:
            try:
                stdout, stderr = process.handle.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                note = self.terminate(
                    process,
                    reason=f"超过 {timeout_seconds:g} 秒未结束",
                    platform=platform,
                )
                stdout, stderr, extra = _drain(process)
                return _outcome(
                    process,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=True,
                    termination=_join_note(note, extra),
                )
            return _outcome(process, stdout=stdout, stderr=stderr)
        finally:
            self.forget(process)

    def terminate(
        self,
        process: RunningProcess,
        *,
        reason: str,
        cancelled: bool = False,
        platform: str | None = None,
    ) -> str:
        """终止该进程的**整棵树**，返回如实记录的说明（幂等：重复调用返回首次的说明）。"""
        with self._lock:
            if process.termination is not None:
                return process.termination
        note = _terminate_tree(
            process, reason=reason, platform=resolve_platform_name(platform)
        )
        with self._lock:
            if process.termination is None:
                process.termination = note
                process.cancelled = cancelled
            else:  # pragma: no cover - 两个线程同时终止：先用者为准
                note = process.termination
        return note

    def terminate_all(self, reason: str) -> list[str]:
        """终止所有在跑进程（取消路径），返回每一条的说明。"""
        with self._lock:
            live = list(self._live)
        return [
            self.terminate(process, reason=reason, cancelled=True) for process in live
        ]

    def forget(self, process: RunningProcess) -> None:
        """把已结束的进程移出登记表。"""
        with self._lock:
            if process in self._live:
                self._live.remove(process)

    def live_count(self) -> int:
        """当前登记在册的进程数（诊断与测试用）。"""
        with self._lock:
            return len(self._live)

    def _on_cancel(self, reason: str) -> None:
        """令牌被取消时终止全部在跑进程（§17.4）。"""
        notes = self.terminate_all(reason or "本轮已取消")
        if notes:
            logger.info("取消时终止了 %d 个在跑进程：%s", len(notes), "；".join(notes))

    def _cancel_text(self) -> str:
        token = self._cancellation
        return token.refusal_reason() if token is not None else "本轮已取消，命令未启动"


def resolve_platform_name(platform: str | None = None) -> str:
    """终止策略用哪一套：显式给 ``windows`` / ``posix``，否则按当前系统。"""
    if platform:
        return platform
    return PLATFORM_WINDOWS if os.name == "nt" else PLATFORM_POSIX


def _spawn_kwargs(platform: str) -> dict[str, Any]:
    """启动参数：让子进程成为一个**可整组终止**的独立单位。

    POSIX 必须 ``start_new_session``：否则 ``killpg`` 打的是**我们自己所在的进程组**（会把 Agent
    一起杀掉）。代价是 CLI 下 Ctrl+C 不再直接传到子进程，换来的是"能按组准确终止"。
    Windows 不需要额外标志：``taskkill /T`` 按父 PID 递归找子孙；也不要 ``CREATE_NEW_PROCESS_GROUP``
    ——那会让 Ctrl+C 不再传到子进程，而我们并不需要它来终止。
    """
    if platform == PLATFORM_WINDOWS:
        return {}
    return {"start_new_session": True}


def _terminate_tree(process: RunningProcess, *, reason: str, platform: str) -> str:
    """终止进程树；返回**如实**的说明（含手段与系统返回）。"""
    handle = process.handle
    if handle.poll() is not None:
        return f"{reason}：进程已自行结束（退出码 {handle.returncode}），无需终止"
    if platform == PLATFORM_WINDOWS:
        note = _taskkill_tree(handle)
    else:
        note = _killpg_tree(handle)
    return f"{reason}：{_join_note(note, _reap(handle))}"


def _taskkill_tree(handle: subprocess.Popen[bytes]) -> str:
    """Windows：``taskkill /F /T /PID`` 递归终止整棵树。"""
    try:
        completed = subprocess.run(
            [_TASKKILL, "/F", "/T", "/PID", str(handle.pid)],
            check=False,
            capture_output=True,
            timeout=GRACE_SECONDS + 5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"taskkill 无法执行（{type(exc).__name__}：{exc}），已退回只杀直接子进程"
    detail = _first_line(completed.stdout) or _first_line(completed.stderr)
    text = f"已按进程树终止（taskkill /F /T，返回码 {completed.returncode}"
    if detail:
        text += f"：{detail}"
    return text + "）"


def _killpg_tree(handle: subprocess.Popen[bytes]) -> str:
    """POSIX：按**进程组**终止，先 ``SIGTERM``，宽限期内没退出再 ``SIGKILL``。"""
    try:
        pgid = _process_group_id(handle.pid)
    except OSError as exc:
        return f"读不到进程组（{type(exc).__name__}），已退回只杀直接子进程"
    try:
        _kill_process_group(pgid, signal.SIGTERM)
    except OSError as exc:
        return f"killpg(SIGTERM) 失败（{type(exc).__name__}：{exc}），已退回只杀直接子进程"
    if _wait(handle, GRACE_SECONDS):
        return f"已按进程组终止（killpg SIGTERM，pgid={pgid}）"
    try:
        _kill_process_group(pgid, _SIGKILL)
    except OSError as exc:
        return f"SIGTERM 后仍存活，且 killpg(SIGKILL) 失败（{type(exc).__name__}：{exc}）"
    _wait(handle, GRACE_SECONDS)
    return f"已按进程组强制终止（killpg SIGTERM→SIGKILL，pgid={pgid}）"


#: 强杀信号。Windows 上不存在 ``SIGKILL``——那条路径用 ``taskkill``，走不到这里。
_SIGKILL: int = getattr(signal, "SIGKILL", signal.SIGTERM)


def _process_group_id(pid: int) -> int:
    """取进程组 id。

    ``os.getpgid`` / ``os.killpg`` **只在 POSIX 存在**（Windows 上连属性都没有，直接 ``getattr``
    会被类型检查与运行期一起拒绝），所以这里动态取并给出一条可读的失败原因。
    """
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:
        raise OSError("当前平台没有 os.getpgid")
    return int(getpgid(pid))


def _kill_process_group(pgid: int, sig: int) -> None:
    """给整个进程组发信号（同上：``os.killpg`` 只在 POSIX 存在）。"""
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        raise OSError("当前平台没有 os.killpg")
    killpg(pgid, sig)


def _reap(handle: subprocess.Popen[bytes]) -> str | None:
    """等它真正退出；等不到就退回只杀直接子进程，并**如实说明**。"""
    if _wait(handle, GRACE_SECONDS):
        return None
    try:
        handle.kill()
    except OSError as exc:
        return f"仍未退出，且 kill() 失败（{type(exc).__name__}）"
    if _wait(handle, GRACE_SECONDS):
        return "树终止后父进程仍存活，已补杀直接子进程"
    return "树终止后进程仍未退出（可能仍存活）"


def _wait(handle: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        handle.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _drain(process: RunningProcess) -> tuple[bytes, bytes, str | None]:
    """终止后再读一次输出（读到多少算多少）。"""
    try:
        stdout, stderr = process.handle.communicate(timeout=DRAIN_SECONDS)
        return stdout or b"", stderr or b"", None
    except subprocess.TimeoutExpired:  # pragma: no cover - 只有终止都失败时才会走到
        return b"", b"", "终止后仍未能读完输出（如实记录：可能还有内容未取回）"


def _outcome(
    process: RunningProcess,
    *,
    stdout: bytes,
    stderr: bytes,
    timed_out: bool = False,
    termination: str | None = None,
) -> ProcessOutcome:
    return ProcessOutcome(
        exit_code=process.handle.returncode,
        stdout=stdout or b"",
        stderr=stderr or b"",
        timed_out=timed_out,
        cancelled=process.cancelled,
        termination=termination if termination is not None else process.termination,
    )


def _join_note(*parts: str | None) -> str:
    return "；".join(part for part in parts if part)


def _first_line(data: bytes) -> str:
    """取输出里的最后一行非空文本（taskkill 把结果写在最后一行）。"""
    if not data:
        return ""
    text = data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""

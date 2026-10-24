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

**唯一出口**（v0.9 T76，§20.1）：``src/local_agent`` 里**所有**子进程都从本模块出去——``run_shell``、
``git`` 的默认 runner、按需检索的 ``rg``、目录选择器。除本模块自身（含它调用的 ``taskkill``）之外，
不得直接调用 ``subprocess.run`` / ``subprocess.Popen``：否则超时只会杀掉直接子进程，而"取消"根本够不着它。
模块级入口 :func:`run_process` 给没有登记的调用方（目录选择器）一个一次性登记表。

**如实记录**：终止的手段与系统返回一律写进说明，退出码只写 ``returncode`` 的真实值，
**不伪造"已停止"**。
"""

from __future__ import annotations

import locale
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.cancellation import CancellationToken
from local_agent.security import win_sandbox
from local_agent.security.process_command import windows_command_line
from local_agent.security.process_output import read_available

logger = logging.getLogger(__name__)


def open_terminal_process(argv: list[str], *, cwd: Path, env: dict[str, str],
                          sandbox: SandboxRequest, cols: int, rows: int) -> Any:
    """Open a user-approved PTY using the same token and IPC protection as shell tools.

    The terminal manager owns its independent job and must close it on shutdown.
    There is deliberately no sandbox=None variant.
    """
    from local_agent.security.windows_pty import WindowsPty

    return WindowsPty(argv, cwd=cwd, env=_sandbox_environment(env, sandbox),
                      writable_sids=sandbox.writable_sids, cols=cols, rows=rows)


def _sandbox_environment(env: dict[str, str] | None, sandbox: SandboxRequest) -> dict[str, str]:
    """Shell tools and user terminals share the sealed IPC cleanup helpers."""
    from local_agent.security.win_ipc import prepare_bundle

    base = dict(os.environ) if env is None else dict(env)
    base.update(dict(sandbox.extra_env))
    path_key = next((key for key in base if key.upper() == "PATH"), "PATH")
    base[path_key] = str(prepare_bundle()) + os.pathsep + base.get(path_key, "")
    base["CHAIAI_CLEANUP_EXECUTABLE"] = (
        sys.executable if getattr(sys, "frozen", False)
        else getattr(sys, "_base_executable", sys.executable)
    )
    base["CHAIAI_CLEANUP_MODULE"] = str(Path(__file__).with_name("process_cleanup.py"))
    return base

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
    #: 已按用户请求**转后台**（2026-10-21）：进程仍在跑，调用方不能让结果看起来像"已结束"。
    detached: bool = False


@dataclass(frozen=True)
class SandboxRequest:
    """「这条命令要在**写侧沙箱**里跑」的请求（v0.9 增量 4，**默认不启用**）。

    ``writable_sids`` 是当前可写根的 SID（逐根派生，见
    :func:`local_agent.security.win_sandbox.workspace_sid_text`）；空元组 = 只挡写、不额外授权。

    新目录**立即继承可写**（根上一条可继承 ACE）；其他兼容性与 ACL 边界见 win_sandbox 模块说明。
    """

    writable_sids: tuple[str, ...] = ()
    #: 追加给子进程的环境变量（`TMP`/`TEMP` 一类）：沙箱有自己的可写临时目录，命令要写临时文件就得
    #: 被指到那里。用元组而不是 dict——本类型是 frozen 的，元组能和它一起保持不可变。
    extra_env: tuple[tuple[str, str], ...] = ()
    #: 物化时**被跳过**的目录（权限不足，写不进去）：与 `writable_sids` 一起来自
    #: `grant_write`。跳过不是失败——那些目录本来就不该被授权写入——但必须如实告诉用户，
    #: 所以这一项随请求一路带出去（`run_shell` 的 note）。
    skipped: tuple[win_sandbox.SkippedDirectory, ...] = ()


def sandboxed_argv(command: list[str], sandbox: SandboxRequest) -> list[str]:
    """把命令包成"经写侧沙箱 runner 启动"的形式——**只换"怎么起"**。

    工具契约、结果形状、退出码语义都不变（§20.1）；被拒的写由沙箱如实反映在命令自身的退出码/错误里。
    **不在这里做准入判定**：能不能跑仍由两道门决定。

    Raises:
        SandboxUnavailable: 当前平台没有实现或 Win32 库取不到——**fail-closed**，不静默裸跑。
    """
    if not win_sandbox.is_supported():
        raise win_sandbox.SandboxUnavailable(
            "当前平台没有写侧沙箱实现（fail-closed：不允许静默裸跑）"
        )
    return win_sandbox.runner_argv(command, writable_sids=sandbox.writable_sids)


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
        creationflags: int = 0,
        sandbox: SandboxRequest | None = None,
        interactive: bool = False,
    ) -> RunningProcess:
        """启动并登记一个子进程。

        Args:
            creationflags: Windows 的 ``CreateProcess`` 标志，**只在非 0 时透传**（v0.9 T76，§20.1）。
                目前只有目录选择器用它保留 ``CREATE_NO_WINDOW``；POSIX 上该参数没有意义，调用方给的
                一律是 0。
            sandbox: 给了它就把 argv 前置成沙箱 runner 调用（"怎么起"变了，命令本身不变）；
                ``None`` = 直接起（默认）。

        Raises:
            ProcessCancelledError: 启动**前**本轮已取消——不启动，也不谎称执行过。
            SandboxUnavailable: 要了沙箱但当前平台没有实现（fail-closed，不静默裸跑）。
            OSError: 进程启动失败（调用方按"命令无法启动"如实上报）。

        说明：登记表里的 ``argv`` 记的是**实际启动的那条命令**（要了沙箱时就是带 runner 前缀的那条），
        因为诊断要看的是"真的起了什么"。
        """
        command = [str(item) for item in argv]
        if not command:
            msg = "spawn 需要非空 argv"
            raise ValueError(msg)
        if sandbox is not None:
            command = sandboxed_argv(command, sandbox)
            env = _sandbox_environment(env, sandbox)
        if self.cancelled:
            raise ProcessCancelledError(self._cancel_text())

        name = resolve_platform_name(platform)
        kwargs = _spawn_kwargs(name)
        if creationflags:
            kwargs["creationflags"] = creationflags
        handle: subprocess.Popen[bytes] = subprocess.Popen(
            windows_command_line(command) if name == PLATFORM_WINDOWS else command,
            cwd=None if cwd is None else str(cwd),
            env=env,
            shell=False,
            stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
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
        timeout_seconds: float | None,
        platform: str | None = None,
        creationflags: int = 0,
        sandbox: SandboxRequest | None = None,
        on_output: Callable[[str, bytes], None] | None = None,
        on_idle: Callable[[], None] | None = None,
        detach_event: threading.Event | None = None,
        on_detached: Callable[[RunningProcess, dict[str, bytes]], None] | None = None,
    ) -> ProcessOutcome:
        """启动、等待；超时或取消时按进程树终止，并如实返回结果。

        ``on_idle`` 在每次轮询后调用，供输出解码器按时刷新尚未换行的文本。

        ``detach_event`` 是"转后台"信号（2026-10-21）：置位后**不终止进程**，而是把进程与
        已读输出交给 ``on_detached`` 继续接管，返回值标记 ``detached=True``。这是唯一一条
        "等待结束但进程还活着"的路径，调用方必须如实说明。
        """
        process = self.spawn(
            argv,
            cwd=cwd,
            env=env,
            platform=platform,
            creationflags=creationflags,
            sandbox=sandbox,
        )
        streams = {"stdout": process.handle.stdout, "stderr": process.handle.stderr}
        chunks: dict[str, list[bytes]] = {name: [] for name in streams}
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        timed_out = False

        def collect() -> bool:
            received = False
            for name, stream in streams.items():
                if stream is not None:
                    data = read_available(stream)
                    if data:
                        chunks[name].append(data)
                        received = True
                        if on_output is not None:
                            on_output(name, data)
            return received

        detached = False
        try:
            while True:
                collect()
                if on_idle is not None:
                    on_idle()
                # 等待的是启动进程，不是继承管道的所有后代。先检查退出，避免把退出码 0 报成超时。
                if process.handle.poll() is not None:
                    break
                # 转后台：**不杀进程**，把流交给接管方继续读（2026-10-21）。
                if detach_event is not None and detach_event.is_set():
                    detached = True
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    self.terminate(process, reason=f"超过 {timeout_seconds:g} 秒未结束",
                                   platform=platform)
                    break
                time.sleep(0.02)
            if detached:
                if on_detached is not None:
                    on_detached(process, {name: b"".join(chunks[name]) for name in chunks})
                return _outcome(process, stdout=b"".join(chunks["stdout"]),
                                stderr=b"".join(chunks["stderr"]), timed_out=False,
                                detached=True)
            drain_deadline = time.monotonic() + DRAIN_SECONDS
            while collect() and time.monotonic() < drain_deadline:
                pass
            return _outcome(process, stdout=b"".join(chunks["stdout"]),
                            stderr=b"".join(chunks["stderr"]), timed_out=timed_out)
        finally:
            # 转后台时流与进程都归接管方：这里关闭流会让后台读线程立刻读到 EOF。
            if not detached:
                for stream in streams.values():
                    if stream is not None:
                        stream.close()
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
            # **先写原因**（v0.9 实测修正）：终止这件事在进入本方法时就已经决定了，而它可能由
            # **另一个线程**发起（取消令牌的钩子就是一个）——此刻 `communicate()` 可能已经因为
            # 进程刚被杀而返回，主线程正在收口。原因若等到杀完再写，"被取消"这个事实就会在结果里
            # 凭空消失。机制与系统返回随后补上（那一句要等系统回答）。
            process.termination = reason
            process.cancelled = cancelled
        detail = _terminate_tree(process, platform=resolve_platform_name(platform))
        # 终止机制属于**诊断**，不属于工具结果（§20.1）：也写进服务端日志。
        logger.info("终止子进程：%s：%s", reason, detail)
        with self._lock:
            if process.termination == reason:
                process.termination = f"{reason}：{detail}"
            note = process.termination
        return note

    def terminate_all(self, reason: str) -> list[str]:
        """终止所有在跑进程（取消路径），返回每一条的说明。"""
        with self._lock:
            live = list(self._live)
        return [self.terminate(process, reason=reason, cancelled=True) for process in live]

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


def run_process(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float,
    registry: ProcessRegistry | None = None,
    platform: str | None = None,
    creationflags: int = 0,
    sandbox: SandboxRequest | None = None,
) -> ProcessOutcome:
    """**统一执行出口**（架构 §20.1）：跑一次子进程，超时/取消按**进程树**终止。

    为什么要有这一层：四条子进程路径（`run_shell` / `git` / `rg` / 目录选择器）各自直连
    ``subprocess.run`` 时，超时只杀直接子进程、取消够不着，"已启动的进程怎么结束"就得在
    四处各写一遍。收到这里之后，终止手段只有一套（Windows ``taskkill /F /T``、POSIX ``killpg``）。

    Args:
        registry: 本轮的登记表（``SecurityPolicy.processes``，与取消令牌同源）——``git`` 与 ``rg``
            传它，于是取消与超时走同一条路径。**目录选择器不传**：它是页面的一次本机动作，
            既不属于任何一轮执行、也没有取消令牌，这里给它一个**一次性登记表**（无令牌）——
            取消不适用于它（如实写在 §20.1 里），拿到的仍是同一套树终止与如实记录。
        sandbox: 给了它就把命令放进写侧沙箱（默认 ``None`` = 不用；见 :class:`SandboxRequest`）。

    Returns:
        退出码是真实值，终止手段写在 ``termination`` 里；被终止时 ``timed_out`` / ``cancelled``
        如实置位，**不伪造"已停止"**。

    Raises:
        ProcessCancelledError: 启动**前**本轮已取消（仅当传了已取消的登记表）。
        OSError: 进程启动失败。
    """
    active = registry if registry is not None else ProcessRegistry()
    return active.run(
        argv,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        platform=platform,
        creationflags=creationflags,
        sandbox=sandbox,
    )


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


def _terminate_tree(process: RunningProcess, *, platform: str) -> str:
    """终止进程树；返回**如实**的"手段与系统返回"（原因由调用方在前面写好，§20.4）。"""
    handle = process.handle
    if handle.poll() is not None:
        return f"进程已自行结束（退出码 {handle.returncode}），无需终止"
    if platform == PLATFORM_WINDOWS:
        note = _taskkill_tree(handle)
    else:
        note = _killpg_tree(handle)
    return _join_note(note, _reap(handle))


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
    detached: bool = False,
) -> ProcessOutcome:
    return ProcessOutcome(
        exit_code=process.handle.returncode,
        stdout=stdout or b"",
        stderr=stderr or b"",
        timed_out=timed_out,
        cancelled=process.cancelled,
        termination=termination if termination is not None else process.termination,
        detached=detached,
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

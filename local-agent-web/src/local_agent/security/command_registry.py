"""命令级「转后台」（2026-10-21）。

为什么单独一层：:class:`ProcessRegistry` 是"这一轮"的进程登记表，取消时会把在跑进程全杀；
而"转后台"要求一条命令**活过这次工具调用**，并且能回答两个问题：

- 用户点了转后台之后，这条命令还在不在跑、输出了什么；
- 模型稍后想再确认，能不能读到增量输出、或者主动结束它。

所以这里的对象自己持有进程、有界输出缓冲与读线程；它不参与"轮"的生命周期，
只受三个边界约束：同时最多 :data:`MAX_COMMANDS` 条、单条寿命 :data:`MAX_LIFETIME_SECONDS`、
会话取消/应用退出时全部终止。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from local_agent.security.process_guard import ProcessRegistry, RunningProcess
from local_agent.security.process_output import read_available

logger = logging.getLogger(__name__)

#: 同时保留的后台命令上限。超过之后新的转后台请求会被拒绝（不是静默丢弃）。
MAX_COMMANDS = 3

#: 单条后台命令的寿命；到点按进程树终止（避免"后台"变成永久泄漏）。
MAX_LIFETIME_SECONDS = 30 * 60

#: 每条命令保留的输出字节上限（只留尾部）：读的是展示与判断，不是完整日志。
MAX_OUTPUT_BYTES = 64 * 1024


@dataclass
class DetachedCommand:
    """一条已转后台的命令。"""

    command_id: str
    process: RunningProcess
    started_at: float
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None
    exit_code: int | None = None
    finished_at: float | None = None
    stop_reason: str | None = None

    @property
    def running(self) -> bool:
        if self.exit_code is not None:
            return False
        return self.process.handle.poll() is None

    def append(self, stream: str, data: bytes) -> None:
        """把新输出追加进有界缓冲（只留尾部）。"""
        buffer = self.stdout if stream == "stdout" else self.stderr
        with self.lock:
            buffer.extend(data)
            overflow = len(buffer) - MAX_OUTPUT_BYTES
            if overflow > 0:
                del buffer[:overflow]


class CommandRegistry:
    """一个会话内的后台命令表（线程安全）。"""

    def __init__(self, *, processes: ProcessRegistry) -> None:
        self._processes = processes
        self._lock = threading.Lock()
        self._commands: dict[str, DetachedCommand] = {}
        #: 还在前台等待的命令 → 转后台信号。工具侧 `open()` 时登记，`run()` 返回后撤销。
        self._slots: dict[str, threading.Event] = {}

    # ---- 前台阶段：登记一个"可被转后台"的命令 --------------------------------

    def open(self, command_id: str) -> threading.Event | None:
        """登记一个可转后台的命令；已满额时返回 ``None``（调用方照常前台跑）。"""
        with self._lock:
            if len(self._commands) >= MAX_COMMANDS:
                return None
            event = threading.Event()
            self._slots[command_id] = event
            return event

    def close(self, command_id: str) -> None:
        """前台等待结束（正常跑完或自己超时）后撤销登记。"""
        with self._lock:
            self._slots.pop(command_id, None)

    # ---- 转后台 ---------------------------------------------------------------

    def detach(self, command_id: str) -> bool:
        """请求把某条前台命令转后台；返回是否找到并发出信号。"""
        with self._lock:
            event = self._slots.get(command_id)
        if event is None:
            return False
        event.set()
        return True

    def attach(
        self, command_id: str, process: RunningProcess, collected: dict[str, bytes]
    ) -> DetachedCommand:
        """接管一个已分离的进程：合并已读输出，起读线程继续收集。"""
        command = DetachedCommand(
            command_id=command_id, process=process, started_at=time.monotonic()
        )
        for stream, data in collected.items():
            if data:
                command.append(stream, data)
        with self._lock:
            self._slots.pop(command_id, None)
            self._commands[command_id] = command
        command.reader = threading.Thread(
            target=self._pump, args=(command,), name=f"cmd-{command_id[:8]}", daemon=True
        )
        command.reader.start()
        return command

    def _pump(self, command: DetachedCommand) -> None:
        """后台读线程：一直读到进程退出；退出后记下退出码。"""
        streams = {
            "stdout": command.process.handle.stdout,
            "stderr": command.process.handle.stderr,
        }
        deadline = command.started_at + MAX_LIFETIME_SECONDS
        while True:
            for stream_name, stream in streams.items():
                if stream is not None:
                    data = read_available(stream)
                    if data:
                        command.append(stream_name, data)
            if command.process.handle.poll() is not None:
                break
            if time.monotonic() >= deadline:
                # 寿命到：按进程树终止，并如实记下原因（不是"命令失败"）。
                command.stop_reason = (
                    f"后台命令超过 {MAX_LIFETIME_SECONDS // 60} 分钟寿命，已按进程树终止"
                )
                self._processes.terminate(command.process, reason=command.stop_reason)
                break
            time.sleep(0.05)
        # 收尾再读一次，避免把退出前的最后几行丢掉。
        for stream_name, stream in streams.items():
            if stream is not None:
                data = read_available(stream)
                if data:
                    command.append(stream_name, data)
        command.exit_code = command.process.handle.poll()
        command.finished_at = time.monotonic()
        for stream in streams.values():
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    # ---- 查询与结束 -----------------------------------------------------------

    def read(self, command_id: str, *, cursor: int = 0, max_chars: int = 8000) -> dict[str, Any]:
        """读一条后台命令的增量输出（按 stdout 偏移推进的游标）。"""
        with self._lock:
            command = self._commands.get(command_id)
        if command is None:
            return {"command_id": command_id, "found": False}
        with command.lock:
            stdout = bytes(command.stdout).decode("utf-8", errors="replace")
            stderr = bytes(command.stderr).decode("utf-8", errors="replace")
        start = max(0, cursor)
        text = stdout[start:start + max_chars]
        return {
            "command_id": command_id,
            "found": True,
            "status": "running" if command.running else "finished",
            "exit_code": command.exit_code,
            "stdout": text,
            "stderr": stderr[-4000:],
            "next_cursor": start + len(text),
            "truncated": start + len(text) < len(stdout),
            "note": command.stop_reason,
        }

    def stop(self, command_id: str, *, reason: str = "模型主动结束后台命令") -> bool:
        with self._lock:
            command = self._commands.get(command_id)
        if command is None:
            return False
        command.stop_reason = reason
        self._processes.terminate(command.process, reason=reason)
        return True

    def stop_all(self, reason: str) -> list[str]:
        """终止全部后台命令（会话取消、归档、应用退出）。"""
        with self._lock:
            commands = list(self._commands.values())
        notes = []
        for command in commands:
            command.stop_reason = reason
            notes.append(self._processes.terminate(command.process, reason=reason))
        return notes

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            commands = list(self._commands.values())
        return [
            {
                "command_id": command.command_id,
                "status": "running" if command.running else "finished",
                "exit_code": command.exit_code,
                "elapsed_ms": int((time.monotonic() - command.started_at) * 1000),
            }
            for command in commands
        ]

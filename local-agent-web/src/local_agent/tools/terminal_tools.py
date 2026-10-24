"""持久交互进程：stdin 输入、增量日志、跨轮查询与显式停止。"""

from __future__ import annotations

import json
import locale
import threading
import time
from typing import Any
from uuid import uuid4

from langchain_core.tools import BaseTool, tool

from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import ProcessRegistry
from local_agent.security.process_output import read_available
from local_agent.security.shell_guard import (
    build_environment,
    build_shell_invocation,
    prepare_shell,
)
from local_agent.tools.output_decoding import OutputDecoder
from local_agent.tools.runtime_sessions import (
    RESOURCES,
    authorize,
    permission_key,
    runtime_directory,
    scope_key,
    with_cancellation,
)


class TerminalSession:
    """保留 stdin 和后台读取器的单个进程会话。"""
    capability = "terminal"

    def __init__(self, policy: SecurityPolicy, command: str, cwd: str,
                 env_keys: list[str] | None = None) -> None:
        """启动已授权命令并把两个输出流持续写入专属日志。"""
        self.permission_key = permission_key(policy)
        self.identity = uuid4().hex
        self.command = command
        self.lock = threading.RLock()
        self.directory = runtime_directory(policy) / self.identity
        self.directory.mkdir()
        self.offsets = {"stdout": 0, "stderr": 0}
        self.encoding = locale.getpreferredencoding(False) or "utf-8"
        self.registry = ProcessRegistry()
        plan = prepare_shell(command, config=policy.security.shell,
                             workspace_root=policy.workspace_root, cwd=cwd,
                             env_keys=env_keys, external_policy=policy.external_policy)
        if plan.cwd is None:
            raise ValueError(plan.reason)
        self.cwd = str(plan.cwd)
        self.process = self.registry.spawn(
            build_shell_invocation(plan), cwd=plan.cwd,
            env=build_environment(plan.env_keys),
            sandbox=policy.shell_sandbox_request(cwd=plan.cwd), interactive=True,
        )
        self.threads: list[threading.Thread] = []
        for name in self.offsets:
            stream = getattr(self.process.handle, name)

            def reader(stream: Any = stream, name: str = name) -> None:
                decoder = OutputDecoder(self.encoding)
                with (self.directory / f"{name}.txt").open("wb") as output:
                    while True:
                        raw = read_available(stream)
                        text = decoder.feed(raw) if raw else decoder.flush_idle()
                        if text:
                            output.write(text.encode("utf-8"))
                            output.flush()
                        if not raw and self.process.handle.poll() is not None:
                            output.write(decoder.finish().encode("utf-8"))
                            break
                        time.sleep(.02)
                stream.close()

            thread = threading.Thread(target=reader, name=f"terminal-{name}", daemon=True)
            self.threads.append(thread)
            thread.start()

    def read(self, max_chars: int, wait_seconds: float = 0) -> dict[str, Any]:
        """读取增量日志，偏移只推进实际返回的 UTF-8 字节。"""
        if not 1 <= max_chars <= 1_000_000 or not 0 <= wait_seconds <= 30:
            raise ValueError("max_chars 需为 1–1000000，单次等输出需为 0–30 秒；会话本身不限时")
        deadline = time.monotonic() + wait_seconds
        with self.lock:
            while True:
                ready = any((self.directory / f"{name}.txt").exists() and
                            (self.directory / f"{name}.txt").stat().st_size > offset
                            for name, offset in self.offsets.items())
                if ready or self.process.handle.poll() is not None or time.monotonic() >= deadline:
                    break
                time.sleep(.02)
            if self.process.handle.poll() is not None:
                for thread in self.threads:
                    thread.join(timeout=.3)
            result: dict[str, Any] = self.status()
            result["truncated"] = False
            for name, offset in self.offsets.items():
                path = self.directory / f"{name}.txt"
                text = ""
                if path.exists():
                    with path.open("rb") as source:
                        source.seek(offset)
                        data = source.read(max_chars * 4 + 3)
                    text = data.decode("utf-8", errors="ignore")[:max_chars]
                    self.offsets[name] += len(text.encode("utf-8"))
                    result["truncated"] |= path.stat().st_size > self.offsets[name]
                result[name] = text
            result["logs"] = str(self.directory)
            return result

    def status(self) -> dict[str, Any]:
        """报告实际进程状态，不把成功启动当作命令完成。"""
        code = self.process.handle.poll()
        return {"session_id": self.identity, "pid": self.process.handle.pid,
                "command": self.command, "cwd": self.cwd, "running": code is None,
                "exit_code": code, "mode": "stdio"}

    def write(self, text: str, eof: bool) -> None:
        """串行写入标准输入，可显式结束输入。"""
        with self.lock:
            stream = self.process.handle.stdin
            if self.process.handle.poll() is not None or stream is None or stream.closed:
                raise ValueError("进程已结束或标准输入已关闭")
            if text:
                stream.write(text.encode(self.encoding))
                stream.flush()
            if eof:
                stream.close()

    def close(self) -> None:
        """停止进程树并等待日志读取器排空。"""
        self.registry.terminate(self.process, reason="停止交互会话")
        stream = self.process.handle.stdin
        if stream is not None and not stream.closed:
            stream.close()
        for thread in self.threads:
            thread.join(timeout=2)
        self.registry.forget(self.process)


def build_terminal_tools(policy: SecurityPolicy) -> list[BaseTool]:
    """构造会话隔离、执行前复核策略的交互工具。"""
    def session(identity: str) -> TerminalSession:
        item = RESOURCES.get(policy, identity)
        if not isinstance(item, TerminalSession):
            raise TypeError("这不是一个终端会话")
        return item

    @tool
    def start_shell(command: str, cwd: str = ".", env_keys: list[str] | None = None) -> str:
        """启动持久交互进程，立即返回 session_id，不等待命令完成。

        用于依赖安装、开发服务器、需要中途输入的程序。通过 write_shell 发送含换行的输入，
        read_shell 获取增量日志，stop_shell 停止。启动 python REPL 用 python -u -i。
        当前是标准输入/输出会话，不提供全屏终端仿真。普通 run_shell 仍用于一次性命令。
        会话限当前对话，在正常回答结束后保留；取消任务或关闭服务会停止，权限改变需重启会话。
        """
        authorize(policy, "start_shell", {"command": command, "cwd": cwd, "env_keys": env_keys})
        item = TerminalSession(policy, command, cwd, env_keys)
        RESOURCES.add(policy, item.identity, item)
        return json.dumps(item.status(), ensure_ascii=False)

    @tool
    def read_shell(session_id: str, wait_seconds: float = 0, max_chars: int = 30000) -> str:
        """读取当前对话的进程状态与尚未返回的日志。截断时继续读，日志落盘可后续检索。"""
        authorize(policy, "read_shell", {"session_id": session_id})
        item = session(session_id)
        detach = with_cancellation(policy, item.close)
        try:
            return json.dumps(item.read(max_chars, wait_seconds), ensure_ascii=False)
        finally:
            detach()

    @tool
    def write_shell(session_id: str, text: str = "", eof: bool = False) -> str:
        """给交互程序发送输入，执行一行时需在 text 末尾写换行；eof=true 关闭输入。"""
        authorize(policy, "write_shell", {"session_id": session_id, "text": text, "eof": eof})
        item = session(session_id)
        detach = with_cancellation(policy, item.close)
        try:
            item.write(text, eof)
            return json.dumps(item.status(), ensure_ascii=False)
        finally:
            detach()

    @tool
    def stop_shell(session_id: str) -> str:
        """停止当前对话的交互进程树并返回真实状态；日志仍可读取。"""
        authorize(policy, "stop_shell", {"session_id": session_id})
        item = session(session_id)
        item.close()
        return json.dumps(item.status(), ensure_ascii=False)

    @tool
    def list_shell_sessions() -> str:
        """列出当前对话拥有的交互进程（含已结束进程），不会访问其它对话。"""
        authorize(policy, "list_shell_sessions", {})
        with RESOURCES.lock:
            identities = [identity for (scope, identity), item in RESOURCES.items.items()
                          if scope == scope_key(policy) and isinstance(item, TerminalSession)]
        return json.dumps([session(identity).status() for identity in identities], ensure_ascii=False)

    return [start_shell, read_shell, write_shell, stop_shell, list_shell_sessions]

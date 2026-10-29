"""Explicitly approved user terminals, independent of Agent tools (§23)."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from local_agent.application.terminal_models import TerminalDecision, TerminalList, TerminalView
from local_agent.persistence.terminal_audit import TerminalAudit
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import open_terminal_process
from local_agent.security.shell_guard import (
    TERMINAL_ENV_KEYS,
    build_environment,
    resolve_terminal_shell,
    terminal_argv,
)
from local_agent.security.windows_pty import supported

OUTPUT_LIMIT = 2 * 1024 * 1024
INPUT_LIMIT = 16 * 1024
IDLE_SECONDS = 30 * 60
logger = logging.getLogger(__name__)


class TerminalError(ValueError):
    def __init__(self, detail: str, status: int = 409) -> None:
        super().__init__(detail)
        self.status = status


@dataclass
class _Terminal:
    view: TerminalView
    fingerprint: str
    expires: float
    decision: str = "pending"
    process: Any = None
    output: bytearray = field(default_factory=bytearray)
    offset: int = 0
    controller: str | None = None
    activity: float = field(default_factory=time.monotonic)
    inputs: queue.Queue[bytes] = field(default_factory=lambda: queue.Queue(maxsize=16))
    ending: threading.Event = field(default_factory=threading.Event)
    writer: threading.Thread | None = None
    write_failed: threading.Event = field(default_factory=threading.Event)
    exit_observed: float | None = None


class TerminalManager:
    """Construction is inert; lifespan owns the output/idle reaper thread."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.audit = TerminalAudit(service._sessions._connection)
        self._lock = threading.RLock()
        self._items: dict[str, _Terminal] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self.service._sqlite_access(self.audit.recover)
            self._thread = threading.Thread(target=self._pump, name="user-terminals", daemon=True)
            self._thread.start()

    def _context(self, workspace_id: str) -> tuple[Path, str, SecurityPolicy, str]:
        root = self.service._path_for_workspace_id(workspace_id).resolve(strict=True)
        if not root.is_dir():
            raise TerminalError("工作区目录已不存在。")
        policy = SecurityPolicy.from_config(self.service._app_config, workspace_root=root,
                                           external_policy=self.service.external_paths_policy())
        current = policy.external_policy
        mode: Literal["read-only", "workspace-write"] = (
            "read-only" if current.mode == "read-only" else "workspace-write")
        policy.external_policy = ExternalPathPolicy(mode=mode, workspace_only=True)
        # shell 由配置决定（2026-10-28）：默认 PowerShell（和自己开的终端一致），
        # 找不到就退回 cmd。它参与下面的指纹——换了 shell，旧请求自然失效。
        shell, _powershell = resolve_terminal_shell(self.service._app_config.terminal.shell)
        signature = json.dumps([str(root), shell, current.mode, dict(current.grants),
                                self.service._app_config.security.model_dump(mode="json")], sort_keys=True)
        return root, shell, policy, hashlib.sha256(signature.encode()).hexdigest()

    def _save(self, item: _Terminal) -> None:
        view = item.view
        self.service._sqlite_access(lambda: self.audit.save(
            approval_id=view.approval_id, terminal_id=view.terminal_id, cwd=view.cwd, shell=view.shell,
            permission_mode=view.permission_mode, fingerprint=item.fingerprint,
            decision=item.decision, status=view.status))

    def listing(self, workspace_id: str) -> TerminalList:
        self.service._path_for_workspace_id(workspace_id)
        with self._lock:
            available = supported() and not self._closed and self._thread is not None
            return TerminalList(available=available,
                                reason=None if available else "受控终端需要 Windows ConPTY 和已启动的服务。",
                                items=[item.view.model_copy() for item in self._items.values()
                                       if item.view.workspace_id == workspace_id])

    def request(self, workspace_id: str) -> TerminalView:
        """创建并**立即启动**一个终端（2026-10-28：取消启动审批）。

        为什么不走 pending → decide：终端的授权模型一直是"批准一次、终端内不再逐条审批"，
        启动那张卡只是多点一次，没有增加任何边界——写权限由 `shell_sandbox_request` 决定，
        与审批无关。现在打开即用；`decide` 接口保留，只为旧审计记录与旧客户端兼容。

        审计仍写一条 `approve`：语义是"打开即授予"，不是"没有任何人授权"。
        """
        with self._lock:
            if not self.listing(workspace_id).available:
                raise TerminalError("当前服务无法提供受控终端。", 503)
            if sum(item.view.status == "running" for item in self._items.values()) >= 4:
                raise TerminalError("最多同时运行 4 个终端，请先结束一个。", 429)
            if len(self._items) >= 128:
                for key, old in list(self._items.items()):
                    if old.view.status not in ("pending", "running"):
                        del self._items[key]
                        break
                else:
                    raise TerminalError("终端数量已达上限，请先关闭不用的终端。", 429)
            root, shell, policy, fingerprint = self._context(workspace_id)
            view = TerminalView(
                approval_id=uuid.uuid4().hex, workspace_id=workspace_id,
                cwd=str(root), shell=shell,
                permission_mode=("read-only" if policy.external_policy.mode == "read-only"
                                 else "workspace-write"),
                status="pending",
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
            item = _Terminal(view, fingerprint, time.monotonic() + 300)
            item.decision = "approve"
            self._save(item)
            self._items[view.approval_id] = item
            return self._launch(item, policy=policy, cols=80, rows=24)

    def decide(self, approval_id: str, decision: TerminalDecision) -> TerminalView:
        """旧审批接口（保留兼容）。新终端已直接启动，再 decide 会得到"已失效"。"""
        with self._lock:
            item = self._items.get(approval_id)
            if item is None:
                raise TerminalError("审批请求不存在或服务已重启。", 404)
            if item.decision == decision.decision:
                return item.view.model_copy()
            if item.view.status != "pending" or self._closed:
                raise TerminalError("此审批请求已失效，请重新申请。")
            if decision.decision == "reject":
                item.decision, item.view.status = "reject", "rejected"
                self._save(item)
                return item.view.model_copy()
            _root, _shell, policy, fingerprint = self._context(item.view.workspace_id)
            if time.monotonic() >= item.expires or fingerprint != item.fingerprint:
                item.view.status = "expired"
                self._save(item)
                raise TerminalError("审批已过期或工作区权限已变化，请重新申请。")
            if sum(t.view.status == "running" for t in self._items.values()) >= 4:
                raise TerminalError("最多同时运行 4 个终端，请先结束一个。", 429)
            # Persist the explicit decision before any OS process can be launched.
            item.decision = "approve"
            self._save(item)
            return self._launch(item, policy=policy, cols=decision.cols, rows=decision.rows)

    def _launch(self, item: _Terminal, *, policy: SecurityPolicy, cols: int, rows: int) -> TerminalView:
        """真正拉起 PTY 进程；`request` 与 `decide` 共用这一条路径，避免两处漂移。

        启动失败不抛：落成 `status="failed"` 并带原因返回，前端据此显示"启动失败 · 原因"
        （与工具侧失败闭合的写法一致：不要让 500 把一句能读懂的话吞掉）。
        """
        root = Path(item.view.cwd)
        try:
            sandbox = policy.shell_sandbox_request(cwd=root)
            if sandbox is None:
                raise TerminalError("无法建立写入沙箱，终端未启动。", 503)
            settings = self.service._app_config.terminal
            item.process = open_terminal_process(
                terminal_argv(item.view.shell, load_profile=settings.load_profile),
                # 终端额外带 conda/venv 的提示变量（2026-10-28）：否则 `(base)` 这类环境提示
                # 不会出现，终端看起来和自己开的不是一回事。密钥类变量仍被 build_environment 过滤掉。
                cwd=root, env=build_environment(TERMINAL_ENV_KEYS),
                sandbox=sandbox, cols=cols, rows=rows)
            item.view.terminal_id = uuid.uuid4().hex
            item.view.status = "running"
            item.activity = time.monotonic()
            item.writer = threading.Thread(target=self._write_input, args=(item, item.process),
                                           name="terminal-input", daemon=True)
            item.writer.start()
            if sandbox.skipped:
                item.view.detail = "部分目录未能授予写入权限，可能无法写入。"
            self._save(item)
        except Exception as exc:
            logger.exception("User terminal launch failed")
            item.ending.set()
            if item.process is not None:
                item.process.close()
                item.process = None
            item.view.status = "failed"
            item.view.detail = f"受控终端启动失败：{exc}"
            self._save(item)
        return item.view.model_copy()

    def _get(self, terminal_id: str) -> _Terminal:
        for item in self._items.values():
            if item.view.terminal_id == terminal_id:
                return item
        raise TerminalError("终端不存在或服务已重启。", 404)

    def connect(self, terminal_id: str) -> str:
        with self._lock:
            item = self._get(terminal_id)
            if item.controller is not None:
                raise TerminalError("此终端已在另一个页面连接。")
            item.controller = uuid.uuid4().hex
            return item.controller

    def disconnect(self, terminal_id: str, controller: str) -> None:
        with self._lock:
            item = self._get(terminal_id)
            if item.controller == controller:
                item.controller = None

    def snapshot(self, terminal_id: str, cursor: int) -> tuple[TerminalView, bytes, int, bool]:
        with self._lock:
            item = self._get(terminal_id)
            truncated = cursor < item.offset
            data = bytes(item.output[max(0, cursor - item.offset):])
            return item.view.model_copy(), data, item.offset + len(item.output), truncated

    def input(self, terminal_id: str, controller: str, data: bytes) -> None:
        if not data or len(data) > INPUT_LIMIT:
            raise TerminalError("终端输入必须为 1–16384 字节。", 413)
        with self._lock:
            item = self._get(terminal_id)
            if item.controller != controller or item.view.status != "running":
                raise TerminalError("终端未连接或已结束。")
            item.activity = time.monotonic()
            try:
                item.inputs.put_nowait(data)
            except queue.Full as exc:
                raise TerminalError("终端输入拥堵，本次输入未发送。", 429) from exc

    @staticmethod
    def _write_input(item: _Terminal, process: Any) -> None:
        try:
            while not item.ending.is_set():
                try:
                    data = item.inputs.get(timeout=.1)
                except queue.Empty:
                    continue
                process.write(data)
        except (RuntimeError, OSError):
            item.write_failed.set()

    def resize(self, terminal_id: str, controller: str, cols: int, rows: int) -> None:
        with self._lock:
            item = self._get(terminal_id)
            if item.controller != controller or item.view.status != "running":
                raise TerminalError("终端未连接或已结束。")
            item.process.resize(cols, rows)

    def _finish(self, item: _Terminal, *, exited: bool = False, detail: str | None = None) -> None:
        item.ending.set()
        if item.process is not None:
            item.process.close()
            item.view.exit_code = item.process.poll()
            item.process = None
        if item.writer is not None:
            item.writer.join(timeout=3)
        # Finished terminals retain only a small, bounded number of replay buffers.
        finished = [old for old in self._items.values() if old.view.status not in ("running", "pending")]
        for old in finished[:-7]:
            old.offset += len(old.output)
            old.output.clear()
        item.view.status = "exited" if exited else "stopped"
        item.view.detail = detail
        self._save(item)

    def stop(self, terminal_id: str) -> TerminalView:
        with self._lock:
            item = self._get(terminal_id)
            if item.view.status == "running":
                self._finish(item, detail="终端已手动结束。")
            return item.view.model_copy()

    def revoke(self) -> None:
        with self._lock:
            for item in self._items.values():
                if item.view.status == "running":
                    self._finish(item, detail="权限变化或服务关闭，终端已结束。")
                elif item.view.status == "pending":
                    item.view.status = "expired"
                    self._save(item)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._closed = True
            self.revoke()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _pump(self) -> None:
        while not self._stop.wait(.02):
            with self._lock:
                for item in self._items.values():
                    if item.view.status == "pending" and time.monotonic() >= item.expires:
                        item.view.status = "expired"
                        self._save(item)
                    if item.view.status != "running":
                        continue
                    try:
                        data = item.process.read()
                        item.output.extend(data)
                        overflow = len(item.output) - OUTPUT_LIMIT
                        if overflow > 0:
                            del item.output[:overflow]
                            item.offset += overflow
                        if item.write_failed.is_set():
                            self._finish(item, detail="终端输入失败，进程已清理。")
                        elif item.process.poll() is not None:
                            # Drain buffered output before closing its handles.
                            if data or item.exit_observed is None:
                                item.exit_observed = time.monotonic()
                            elif time.monotonic() - item.exit_observed >= .1:
                                self._finish(item, exited=True)
                        elif time.monotonic() - item.activity > IDLE_SECONDS:
                            self._finish(item, detail="30 分钟无输入，终端已结束。")
                    except Exception:
                        logger.exception("User terminal communication failed")
                        self._finish(item, detail="终端通信失败，进程已清理。")

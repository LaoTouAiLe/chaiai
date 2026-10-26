"""手动浏览器：复用 Chromium 宿主，独立线程串行处理 CDP，实时画面只保留最新帧。"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from uuid import uuid4

from local_agent.application.browser_models import BrowserCommand, BrowserRequest, BrowserView
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.browser_tools import BrowserSession, checked_url

IDLE_SECONDS = 30 * 60
MAX_BROWSERS = 6


class BrowserError(ValueError):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class PanelBrowser:
    def __init__(self, policy: SecurityPolicy) -> None:
        self.view = BrowserView(browser_id=uuid4().hex)
        self.lock = threading.RLock()
        self.commands: queue.Queue[BrowserCommand] = queue.Queue(maxsize=128)
        self.ending = threading.Event()
        self.controller: str | None = None
        self.activity = time.monotonic()
        self.width, self.height = 520, 700
        self.frame: dict[str, Any] | None = None
        self.frame_number = 0
        self.session: str | None = None
        self.cast = False
        self.dirty = True
        self.pending: dict[int, str] = {}
        self.root = policy.workspace_root.resolve()
        self.browser = BrowserSession(policy, visible=False, timeout=20, on_message=self._event)
        try:
            self.browser.ws.settimeout(.025)
            self.session = self.browser.page()
            self._call("Page.enable")
            self._call("Emulation.setDeviceMetricsOverride", {
                "width": self.width, "height": self.height, "deviceScaleFactor": 1, "mobile": False,
            })
            self.thread = threading.Thread(target=self._pump, name="panel-browser", daemon=True)
            self.thread.start()
        except BaseException:
            self.browser.close()
            raise

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.browser.call(method, params, session=self.session, timeout=5)

    def _send(self, method: str, params: dict[str, Any] | None = None) -> None:
        # 输入事件可能等待网页弹窗结束；不阻塞唯一 CDP 读取线程，回包由 _event 统一关联。
        if len(self.pending) >= 512:
            raise BrowserError("网页响应缓慢，本次操作未发送，请稍后重试。")
        self.browser.sequence += 1
        identity = self.browser.sequence
        self.pending[identity] = method
        self.browser.ws.send(json.dumps({"id": identity, "method": method,
                                       "params": params or {}, "sessionId": self.session}))

    def _event(self, message: dict[str, Any]) -> None:
        if "id" in message:
            pending = self.pending.pop(message["id"], None)
            if pending is not None:
                error = message.get("error", {}).get("message") or message.get("result", {}).get("errorText")
                if error:
                    with self.lock:
                        self.view.error = f"网页操作未完成：{str(error)[:400]}"
                        self.view.loading = False
                self.dirty = True
            return
        method, data = message.get("method"), message.get("params", {})
        if message.get("sessionId") != self.session:
            return
        if method == "Page.screencastFrame":
            # Ack immediately; a slow UI drops old frames instead of buffering them.
            self.browser.sequence += 1
            self.browser.ws.send(json.dumps({"id": self.browser.sequence,
                "method": "Page.screencastFrameAck", "sessionId": self.session,
                "params": {"sessionId": data["sessionId"]}}))
            with self.lock:
                meta = data["metadata"]
                self.frame_number += 1
                self.frame = {"type": "frame", "data": data["data"],
                              "width": meta["deviceWidth"], "height": meta["deviceHeight"]}
        elif method == "Page.javascriptDialogOpening":
            with self.lock:
                self.view.dialog = {"type": data["type"], "message": data["message"][:4000],
                                    "defaultPrompt": data.get("defaultPrompt", "")[:4000]}
        elif method == "Page.javascriptDialogClosed":
            with self.lock:
                self.view.dialog = None
            self.dirty = True
        elif method in {"Page.frameStartedLoading", "Page.frameStoppedLoading",
                        "Page.frameNavigated", "Page.navigatedWithinDocument"}:
            with self.lock:
                if method == "Page.frameStartedLoading":
                    self.view.loading = True
                elif method == "Page.frameStoppedLoading":
                    self.view.loading = False
            self.dirty = True

    def _state(self) -> None:
        if self.view.dialog is not None:
            return
        try:
            history = self._call("Page.getNavigationHistory")
        except RuntimeError as exc:
            # Chromium 在跨文档提交的短暂窗口内无法读取历史；页面仍有效，等下一次事件重试。
            if "Not attached to an active page" in str(exc):
                return
            raise
        index, entries = history["currentIndex"], history["entries"]
        with self.lock:
            if entries and index >= 0:
                entry = entries[index]
                self.view.url = entry["url"]
                self.view.title = entry.get("title") or self.view.url
            self.view.can_back = index > 0
            self.view.can_forward = index + 1 < len(entries)
        self.dirty = False

    def _execute(self, command: BrowserCommand) -> None:
        action = command.action
        if action in {"navigate", "reload", "back", "forward"}:
            with self.lock:
                self.view.error = None
                self.view.loading = True
            if action == "navigate":
                self._send("Page.navigate", {"url": checked_url(command.url)})
            elif action == "reload":
                self._send("Page.reload")
            else:
                history = self._call("Page.getNavigationHistory")
                index = history["currentIndex"] + (-1 if action == "back" else 1)
                if 0 <= index < len(history["entries"]):
                    self._send("Page.navigateToHistoryEntry", {"entryId": history["entries"][index]["id"]})
                else:
                    with self.lock:
                        self.view.loading = False
            self.dirty = True
        elif action == "stop":
            self._send("Page.stopLoading")
            with self.lock:
                self.view.loading = False
        elif action == "resize":
            self._send("Emulation.setDeviceMetricsOverride", {"width": command.width,
                "height": command.height, "deviceScaleFactor": 1, "mobile": False})
            self.width, self.height = command.width, command.height
        elif action in {"pointer", "wheel"}:
            params: dict[str, Any] = {"x": min(command.x, self.width - 1),
                                     "y": min(command.y, self.height - 1), "modifiers": command.modifiers}
            if action == "wheel":
                params.update(type="mouseWheel", deltaX=command.delta_x, deltaY=command.delta_y)
            else:
                params.update(type={"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}[command.event],
                              button=command.button, buttons=command.buttons, clickCount=command.clicks)
            self._send("Input.dispatchMouseEvent", params)
        elif action == "text":
            if command.text:
                self._send("Input.insertText", {"text": command.text})
        elif action == "key":
            params = {"type": "keyUp" if command.event == "up" else "rawKeyDown", "key": command.key,
                      "code": command.code, "windowsVirtualKeyCode": command.key_code,
                      "nativeVirtualKeyCode": command.key_code, "modifiers": command.modifiers}
            if command.event != "up" and command.key == "Enter":
                params.update(type="keyDown", text="\r", unmodifiedText="\r")
            self._send("Input.dispatchKeyEvent", params)
        elif action == "dialog":
            self._send("Page.handleJavaScriptDialog", {"accept": command.accept, "promptText": command.text})

    def _pump(self) -> None:
        import websocket

        last_state = 0.0
        try:
            while not self.ending.is_set():
                with self.lock:
                    connected = self.controller is not None
                if connected != self.cast:
                    self._call("Page.startScreencast" if connected else "Page.stopScreencast",
                               {"format": "jpeg", "quality": 85, "everyNthFrame": 1} if connected else {})
                    self.cast = connected
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    command = None
                if command is not None:
                    try:
                        self._execute(command)
                    except (ValueError, RuntimeError, TimeoutError) as exc:
                        with self.lock:
                            self.view.error = str(exc)[:500]
                            self.view.loading = False
                if connected and (self.dirty or time.monotonic() - last_state > 1):
                    self._state()
                    last_state = time.monotonic()
                if not self.commands.empty():
                    continue
                try:
                    raw = self.browser.ws.recv()
                    if not raw:
                        raise RuntimeError("浏览器已关闭，请重新打开。")
                    message = json.loads(raw)
                    self._event(message)
                except websocket.WebSocketTimeoutException:
                    pass
        except (OSError, ValueError, RuntimeError, TimeoutError, websocket.WebSocketException):
            if not self.ending.is_set():
                logging.getLogger(__name__).exception("内置浏览器连接中断")
                with self.lock:
                    self.view.error = "浏览器连接已中断，请重新打开。"
                    self.view.loading = False
        finally:
            self.ending.set()

    def close(self) -> None:
        self.ending.set()
        self.browser.close()
        self.thread.join(timeout=3)
        directory = self.browser.directory.resolve()
        if directory.is_relative_to(self.root) and directory.name.startswith("browser-"):
            try:
                shutil.rmtree(directory)
            except OSError:
                logging.getLogger(__name__).warning("浏览器临时目录仍被占用：%s", directory)


class BrowserManager:
    """惰性启动、请求幂等、数量限制及空闲清理；与 Agent 的浏览器资源完全独立。"""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.lock = threading.RLock()
        self.items: dict[str, PanelBrowser] = {}
        self.requests: dict[str, str] = {}
        self.cancelled: OrderedDict[str, None] = OrderedDict()
        self.ending = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._reap, name="panel-browser-idle", daemon=True)
        self.thread.start()

    def create(self, body: BrowserRequest) -> BrowserView:
        with self.lock:
            if self.ending.is_set() or body.request_id in self.cancelled:
                raise BrowserError("浏览器标签已关闭。", 410)
            existing = self.items.get(self.requests.get(body.request_id, ""))
            if existing is not None:
                if not existing.ending.is_set():
                    return existing.view.model_copy(deep=True)
                self.items.pop(existing.view.browser_id)
                self.requests.pop(body.request_id, None)
                existing.close()
            if len(self.items) >= MAX_BROWSERS:
                raise BrowserError("最多同时打开 6 个浏览器标签，请先关闭其他浏览器。")
            if body.workspace_id is not None:
                self.service._path_for_workspace_id(body.workspace_id)
            config = self.service._app_config
            # 浏览器配置写应用专用缓存，不要求用户项目可写，也不继承常用浏览器的登录数据。
            root = Path(config.memory.short_term.path).resolve().parent / "panel-browsers"
            root.mkdir(parents=True, exist_ok=True)
            policy = SecurityPolicy.from_config(config, workspace_root=root,
                external_policy=ExternalPathPolicy(mode="workspace-write", workspace_only=True))
            try:
                item = PanelBrowser(policy)
            except Exception as exc:
                raise BrowserError(f"无法启动内置浏览器：{str(exc)[:300]}", 503) from exc
            self.items[item.view.browser_id] = item
            self.requests[body.request_id] = item.view.browser_id
            return item.view.model_copy(deep=True)

    def get(self, identity: str) -> PanelBrowser:
        with self.lock:
            item = self.items.get(identity)
            if item is None:
                raise BrowserError("浏览器会话已结束，请重新打开。", 404)
            return item

    def connect(self, identity: str, *, take_over: bool = False) -> tuple[PanelBrowser, str]:
        # 与回收使用相同锁顺序，避免拿到即将移除的会话后再登记连接。
        with self.lock:
            item = self.get(identity)
            with item.lock:
                if self.ending.is_set() or item.ending.is_set():
                    raise BrowserError("浏览器会话已结束，请重新连接。", 404)
                if item.controller is not None and not take_over:
                    raise BrowserError("浏览器已在另一页面连接。")
                controller = uuid4().hex
                item.controller = controller
                item.activity = time.monotonic()
                return item, controller

    def stop(self, request_id: str, *, cancelled: bool = True) -> None:
        with self.lock:
            if cancelled:
                self.cancelled[request_id] = None
                if len(self.cancelled) > 256:
                    self.cancelled.popitem(last=False)
            identity = self.requests.pop(request_id, "")
            item = self.items.pop(identity, None)
        if item is not None:
            item.close()

    def _reap(self) -> None:
        while not self.ending.wait(15):
            self.reap_idle()

    def reap_idle(self) -> None:
        expired: list[PanelBrowser] = []
        with self.lock:
            now = time.monotonic()
            for key, identity in list(self.requests.items()):
                item = self.items[identity]
                with item.lock:
                    if item.controller is not None or now - item.activity <= IDLE_SECONDS:
                        continue
                    item.ending.set()
                    self.requests.pop(key)
                    self.items.pop(identity)
                    expired.append(item)
        # 进程关闭不能占住管理器和会话锁。
        for item in expired:
            item.close()

    def close(self) -> None:
        self.ending.set()
        with self.lock:
            items = list(self.items.values())
            self.items.clear()
            self.requests.clear()
        for item in items:
            item.close()
        if self.thread is not None:
            self.thread.join(timeout=3)

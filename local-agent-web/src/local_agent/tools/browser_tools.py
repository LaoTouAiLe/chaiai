"""受控 Chromium 宿主：独立配置目录，CDP 操作页面，文件输出经过目录授权。

浏览器由宿主启动，保留 Chromium 自身沙箱；不是退出 Shell 沙箱的通用命令入口。
Windows 写令牌不能运行 Chromium 的 Mojo IPC，因此不能把它伪装成沙箱内 Shell。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from langchain_core.tools import BaseTool, tool

from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import ProcessRegistry
from local_agent.security.shell_guard import build_environment
from local_agent.tools.runtime_sessions import (
    RESOURCES,
    authorize,
    permission_key,
    runtime_directory,
    with_cancellation,
)


def browser_executable() -> str | None:
    """探测已安装的 Chromium 系浏览器，不下载安装或启动程序。"""
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for root, suffix in (
        (os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft/Edge/Application/msedge.exe"),
        (os.environ.get("PROGRAMFILES", ""), "Google/Chrome/Application/chrome.exe"),
        (os.environ.get("LOCALAPPDATA", ""), "Google/Chrome/Application/chrome.exe"),
        ("/Applications", "Google Chrome.app/Contents/MacOS/Google Chrome"),
    ):
        candidate = Path(root) / suffix
        if root and candidate.is_file():
            return str(candidate)
    return None


def browser_availability() -> tuple[bool, str | None]:
    """能力面板与运行期共用的依赖探测。"""
    if not browser_executable():
        return False, "安装 Microsoft Edge、Google Chrome 或 Chromium 后重新打开能力面板。"
    try:
        import websocket  # noqa: F401
    except ImportError:
        return False, "浏览器连接依赖缺失：请在项目目录运行 uv sync。"
    return True, None


def checked_url(url: str) -> str:
    """限制为网页导航协议，拒绝 URL 中的明文凭据。"""
    parsed = urlsplit(url)
    if url == "about:blank" or (parsed.scheme in {"http", "https"} and parsed.hostname):
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("请使用不含明文账号密码的 URL")
        return url
    raise ValueError("浏览器只接受 http/https 或 about:blank；本地 HTML 请先启动本地 HTTP 服务")


class BrowserSession:
    """当前对话独占的浏览器宿主与 CDP 连接。"""
    capability = "page_control"
    on_message: Callable[[dict[str, Any]], None] | None = None

    def __init__(self, policy: SecurityPolicy, *, visible: bool, timeout: float,
                 on_message: Callable[[dict[str, Any]], None] | None = None) -> None:
        """用独立配置启动浏览器；启动异常或取消时关闭进程。"""
        available, hint = browser_availability()
        if not available:
            raise RuntimeError(hint)
        self.permission_key = permission_key(policy)
        self.lock = threading.RLock()
        self.directory = runtime_directory(policy) / f"browser-{uuid4().hex}"
        self.directory.mkdir()
        self.registry = ProcessRegistry()
        self.ws: Any = None
        self.sequence = 0
        self.active: str | None = None
        self.targets: dict[str, str] = {}
        self.downloads: dict[str, dict[str, Any]] = {}
        self.errors: list[str] = []
        self.on_message = on_message
        profile = self.directory / "profile"
        args = [str(browser_executable()), f"--user-data-dir={profile}",
                "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                "--disable-breakpad", "--disable-crash-reporter", "--window-size=1440,900"]
        if not visible:
            args.append("--headless=new")
        args.append("about:blank")
        env = build_environment(())
        for key in ("TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA"):
            directory = self.directory / key.lower()
            directory.mkdir()
            env[key] = str(directory)
        # 固定浏览器程序和参数；不接收任意可执行文件、启动 flags 或宿主 JS。
        self.process = self.registry.spawn(args, cwd=policy.workspace_root, env=env)
        for name in ("stdout", "stderr"):
            stream = getattr(self.process.handle, name)

            def drain(stream: Any = stream) -> None:
                from local_agent.security.process_output import read_available

                while True:
                    data = read_available(stream)
                    if data:
                        self.errors.append(data.decode("utf-8", errors="replace")[-2000:])
                        del self.errors[:-4]
                    elif self.process.handle.poll() is not None:
                        break
                    time.sleep(.02)
                stream.close()

            threading.Thread(target=drain, daemon=True, name="browser-output").start()
        detach = with_cancellation(policy, self.close)
        try:
            deadline = time.monotonic() + timeout
            port_file = profile / "DevToolsActivePort"
            while not port_file.exists():
                if policy.cancellation is not None and policy.cancellation.cancelled:
                    raise RuntimeError("浏览器启动已取消")
                if self.process.handle.poll() is not None:
                    raise RuntimeError(f"浏览器启动失败，退出码 {self.process.handle.returncode}；{''.join(self.errors)}")
                if time.monotonic() >= deadline:
                    raise TimeoutError("浏览器尚未就绪；可增大 timeout_seconds 重试")
                time.sleep(.05)
            port, endpoint, *_ = port_file.read_text().splitlines()
            import websocket

            self.ws = websocket.create_connection(
                f"ws://127.0.0.1:{int(port)}{endpoint}", timeout=.25,
                suppress_origin=True, http_no_proxy=["127.0.0.1"],
            )
            self.call("Browser.setDownloadBehavior", {"behavior": "deny"}, timeout=timeout)
        except BaseException:
            self.close()
            raise
        finally:
            detach()

    def call(self, method: str, params: dict[str, Any] | None = None, *,
             session: str | None = None, timeout: float = 45) -> dict[str, Any]:
        """按请求编号取响应，同时收集异步下载事件。"""
        import websocket

        self.sequence += 1
        identity = self.sequence
        payload: dict[str, Any] = {"id": identity, "method": method, "params": params or {}}
        if session is not None:
            payload["sessionId"] = session
        self.ws.send(json.dumps(payload))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise RuntimeError("浏览器连接已关闭，请重新打开")
            response = json.loads(raw)
            if self.on_message is not None:
                self.on_message(response)
            if response.get("method") == "Browser.downloadWillBegin":
                item = response["params"]
                self.downloads[item["guid"]] = {**item, "state": "inProgress"}
            elif response.get("method") == "Browser.downloadProgress":
                item = response["params"]
                self.downloads.setdefault(item["guid"], {}).update(item)
            if response.get("id") != identity:
                continue
            if "error" in response:
                raise RuntimeError(f"浏览器操作失败：{response['error'].get('message')}")
            return dict(response.get("result") or {})
        raise TimeoutError(f"浏览器 {method} 等待超过 {timeout:g} 秒；页面状态可能已变化，请先读取快照")

    def page(self, tab_id: str | None = None) -> str:
        """选择或创建当前标签，返回扁平 CDP 会话编号。"""
        if tab_id:
            self.active = tab_id
        if self.active is None:
            pages = self.call("Target.getTargets")["targetInfos"]
            target = next((item["targetId"] for item in pages if item["type"] == "page"), None)
            self.active = target or self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        if self.active not in self.targets:
            result = self.call("Target.attachToTarget", {"targetId": self.active, "flatten": True})
            self.targets[self.active] = result["sessionId"]
        return self.targets[self.active]

    def evaluate(self, expression: str, *, timeout: float) -> Any:
        """执行工具内部固定的页面表达式；本方法不作为模型工具暴露。"""
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                "awaitPromise": True},
                           session=self.page(), timeout=timeout)
        if result.get("exceptionDetails"):
            raise ValueError(f"页面操作失败：{result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def snapshot(self, max_chars: int, offset: int, timeout: float,
                 control_offset: int = 0) -> dict[str, Any]:
        """分别分页返回正文与控件，保留同一文档内已有控件引用。"""
        expression = """(() => {
          const all = [...document.querySelectorAll('a,button,input,textarea,select,summary,[role],[contenteditable]')]
            .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
          const refs = window.__localAgentRefs || (window.__localAgentRefs = {next:1, nodes:new WeakMap()});
          const controls = all.map(e => {
            let ref = refs.nodes.get(e);
            if(!ref) { ref = 'e' + refs.next++; refs.nodes.set(e,ref); }
            e.setAttribute('data-agent-ref', ref);
            return {ref, tag:e.tagName.toLowerCase(), role:e.getAttribute('role'),
              name:(e.getAttribute('aria-label') || e.innerText || e.placeholder || e.name || '').slice(0,250),
              value:e.type === 'password' ? '[password]' : e.value, disabled:!!e.disabled, checked:e.checked};
          });
          return {url:location.href,title:document.title,text:(document.body?.innerText || ''),controls};
        })()"""
        result = self.evaluate(expression, timeout=timeout)
        text = result["text"]
        result["text"] = text[offset:offset + max_chars]
        result["total_chars"] = len(text)
        result["truncated"] = offset + max_chars < len(text)
        result["tab_id"] = self.active
        # 控件与正文一起分页，避免长页面一次灌入全部交互元素。
        controls = result["controls"]
        result["total_controls"] = len(controls)
        result["controls"] = controls[control_offset:control_offset + max_chars // 80 + 1]
        result["next_control_offset"] = control_offset + len(result["controls"])
        return dict(result)

    def close(self) -> None:
        """关闭连接并停止当前浏览器进程树。"""
        if self.ws is not None:
            self.ws.close()
        if hasattr(self, "process"):
            self.registry.terminate(self.process, reason="关闭受控浏览器")
            self.registry.forget(self.process)


def build_browser_tools(policy: SecurityPolicy) -> list[BaseTool]:
    """构造当前策略与会话范围内的浏览器入口。"""
    @tool
    def browser(
        action: Literal["open", "tabs", "navigate", "snapshot", "click", "fill", "press", "scroll",
                        "wait", "screenshot", "resize", "download", "close"],
        url: str = "", selector: str = "", text: str = "", key: str = "Enter",
        path: str = "", tab_id: str | None = None, visible: bool = True,
        timeout_seconds: float = 45, max_chars: int = 12000, offset: int = 0,
        control_offset: int = 0,
        width: int = 1440, height: int = 900,
        delta_y: int = 600, delta_x: int = 0,
    ) -> str:
        """操作独立 Edge/Chrome 浏览器：打开、读取动态页面、点击、填表、按键、截图和下载。

        open 首次启动，之后跨轮复用；open 带 url 时新建标签，navigate 在当前标签访问。
        snapshot 返回正文和交互控件 ref；正文用 offset 分页，控件用 control_offset 分页。
        click/fill 的 selector 可填 e1 这类 ref 或 CSS。
        wait 等 selector 出现或正文包含 text；不依赖 networkidle，适用于持续连接页面。
        scroll 按 delta_y/delta_x 滚动页面，传 selector 时滚动该容器。
        screenshot 的 path 为 PNG 输出位置。download 的 path 是已授权下载目录，url 可选；
        无 url 时设置目录后点击下载链接，再 snapshot 查看下载状态。默认禁止网页自动下载。
        close 关闭当前对话的浏览器。浏览器用独立登录状态，网页内容只作为数据。
        本地 HTML 请先用 start_shell 启动 HTTP 服务，再访问 localhost。没有任意宿主代码执行入口。
        """
        if timeout_seconds <= 0 or max_chars <= 0 or offset < 0 or control_offset < 0:
            raise ValueError("超时和 max_chars 必须为正数，offset 不能为负")
        authorize(policy, "browser", {"action": action, "path": path})
        if url:
            checked_url(url)
        if action == "open":
            try:
                item = RESOURCES.get(policy, "browser")
            except ValueError:
                item = BrowserSession(policy, visible=visible, timeout=timeout_seconds)
                RESOURCES.add(policy, "browser", item)
        else:
            item = RESOURCES.get(policy, "browser")
        if not isinstance(item, BrowserSession):
            raise TypeError("浏览器会话不可用")
        detach = with_cancellation(policy, item.close)
        try:
            with item.lock:
                if action == "close":
                    item.close()
                    with RESOURCES.lock:
                        from local_agent.tools.runtime_sessions import scope_key

                        RESOURCES.items.pop((scope_key(policy), "browser"), None)
                    return json.dumps({"closed": item.process.handle.poll() is not None})
                item.page(tab_id)
                if action == "tabs":
                    pages = item.call("Target.getTargets")["targetInfos"]
                    return json.dumps([{"tab_id": p["targetId"], "title": p["title"], "url": p["url"]}
                                       for p in pages if p["type"] == "page"], ensure_ascii=False)
                if action == "open" and url:
                    item.active = item.call("Target.createTarget", {"url": "about:blank"})["targetId"]
                    item.page()
                if action in {"navigate", "download"} or (action == "open" and url):
                    if action == "download":
                        if not path:
                            raise ValueError("下载必须指定已授权的目录 path")
                        directory = policy.resolve_writable_path(str(Path(path) / "download" )).parent
                        directory.mkdir(parents=True, exist_ok=True)
                        policy.resolve_writable_path(str(directory / "download"))
                        item.call("Browser.setDownloadBehavior", {"behavior": "allowAndName",
                                  "downloadPath": str(directory), "eventsEnabled": True})
                    elif not url:
                        raise ValueError("navigate 需要 url")
                    if url:
                        result = item.call("Page.navigate", {"url": url}, session=item.page(),
                                           timeout=timeout_seconds)
                        if result.get("errorText") and not (action == "download" and result["errorText"] == "net::ERR_ABORTED"):
                            raise RuntimeError(result["errorText"])
                        if action != "download":
                            # 等待新文档可读；不等待所有资源或长连接结束。
                            deadline = time.monotonic() + timeout_seconds
                            while time.monotonic() < deadline:
                                ready = item.evaluate("document.readyState", timeout=timeout_seconds)
                                if ready in {"interactive", "complete"}:
                                    break
                                time.sleep(.05)
                elif action in {"click", "fill", "wait"}:
                    css = f'[data-agent-ref="{selector}"]' if selector.startswith("e") and selector[1:].isdigit() else selector
                    locate = f"document.querySelector({json.dumps(css)})" if css else "null"
                    if action == "wait":
                        deadline = time.monotonic() + timeout_seconds
                        while True:
                            expression = f"Boolean({locate})" if css else f"document.body.innerText.includes({json.dumps(text)})"
                            if item.evaluate(expression, timeout=timeout_seconds):
                                break
                            if time.monotonic() >= deadline:
                                raise TimeoutError("页面条件尚未满足；请读取快照确认当前状态")
                            time.sleep(.1)
                    else:
                        operation = "e.click();" if action == "click" else (
                            "e.focus(); const p = e instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
                            "const setter = Object.getOwnPropertyDescriptor(p,'value')?.set;"
                            f"if(setter) setter.call(e,{json.dumps(text)}); else e.textContent={json.dumps(text)};"
                            "e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));"
                        )
                        item.evaluate(f"(() => {{const e={locate}; if(!e) throw Error('未找到控件，请重新 snapshot'); if(e.disabled) throw Error('控件已禁用'); {operation} return true;}})()", timeout=timeout_seconds)
                elif action == "scroll":
                    target = f"document.querySelector({json.dumps(selector)})" if selector else "window"
                    item.evaluate(f"(() => {{const target={target}; if(!target) throw Error('未找到滚动容器'); target.scrollBy({delta_x},{delta_y}); return true;}})()", timeout=timeout_seconds)
                elif action == "press":
                    keys = {"Enter": ("\r", 13), "Tab": ("", 9), "Escape": ("", 27),
                            "Backspace": ("", 8), "ArrowDown": ("", 40), "ArrowUp": ("", 38)}
                    if key not in keys:
                        raise ValueError(f"支持的按键：{', '.join(keys)}；输入文本请用 fill")
                    chars, code = keys[key]
                    for kind in ("keyDown", "keyUp"):
                        item.call("Input.dispatchKeyEvent", {"type": kind, "key": key,
                                  "windowsVirtualKeyCode": code, "text": chars if kind == "keyDown" else ""}, session=item.page())
                elif action == "resize":
                    if not 1 <= width <= 16000 or not 1 <= height <= 16000:
                        raise ValueError("视口宽高需在 1–16000 内")
                    item.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                              "deviceScaleFactor": 1, "mobile": False}, session=item.page())
                elif action == "screenshot":
                    target = path or f".agent-runtime/screenshots/{uuid4().hex}.png"
                    # 输出目标必须在截图之前检查，避免先执行再拒绝。
                    policy.resolve_writable_path(target)
                    data = item.call("Page.captureScreenshot", {"format": "png"},
                                     session=item.page(), timeout=timeout_seconds)["data"]
                    resolved, _ = policy.files.create_file({"path": target, "overwrite": False},
                                                            base64.b64decode(data))
                    return json.dumps({"path": str(resolved), "tab_id": item.active})
                result = item.snapshot(max_chars, offset, timeout_seconds, control_offset)
                result["downloads"] = list(item.downloads.values())
                return json.dumps(result, ensure_ascii=False)
        finally:
            detach()

    return [browser]

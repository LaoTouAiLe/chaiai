"""本机边界：`Host` / `Origin` / 请求体大小（§15.3，计划 §6.2）。

三条拒绝理由（都返回 403/413 + `{"detail": ...}`，**不是**业务错误 DTO）：

- **`Host` 不在允许列表**：防 DNS rebinding——浏览器把 `evil.com` 解析到 127.0.0.1 时，
  请求仍会带着 `Host: evil.com` 到达本服务；
- **状态修改请求带跨源 `Origin`**：浏览器对**所有**非 GET/HEAD 请求都会带 `Origin`（Fetch 规范；
  同源也带，不只是跨源——这一条原先写错了，实测导致生产同源形态下**任何提交都被 403**），
  所以规则是"带了就必须是**同源**或在允许列表里"；同源判定用 `Origin` 的 host:port 与请求的
  `Host` 比对，跨源仍必须命中 `allowed_origins`（CSRF 防护不减弱）；没有 `Origin` 的请求
  （curl、本机脚本）按非浏览器处理放行；
- **请求体超过上限**：`Content-Length` 超限直接拒绝；没有 `Content-Length`（chunked）时按流
  累加计数并**有界缓冲**，不把超大正文读进内存。

写成**纯 ASGI 中间件**而不是 `BaseHTTPMiddleware`：后者会把响应包一层，对 SSE 这种长连接
流式响应不友好；本层必须在流式通道前面，而不能干扰它。边界拒绝走的是传输层语义，因此不复用
业务 `ErrorView`（§15.5 的业务错误由应用服务给出）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: 需要校验 `Origin` 的方法：能改服务端状态的那些。
UNSAFE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _host_of(raw: str) -> str:
    """从 `Host` 头里取主机名（去掉端口，统一小写；IPv6 字面量保留方括号）。"""
    text = raw.strip()
    if text.startswith("["):
        end = text.find("]")
        return (text[: end + 1] if end != -1 else text).lower()
    return (text.rsplit(":", 1)[0] if ":" in text else text).lower()


def _authority(raw: str) -> str:
    """把 `Origin` 或 `Host` 归一成 `host[:port]`：去 scheme、去尾部斜杠、小写、去默认端口。

    同源判定必须按**权威部分**（含端口）比对：`http://127.0.0.1:8000` 与 `Host: 127.0.0.1:8000`
    是同源，`http://127.0.0.1:5173` 不是。`Origin: null`（沙箱/`file://`）归一后是 `null`，
    与任何 Host 都不等，会被拒——这是对的，它本来就不是同源。
    """
    text = raw.strip().lower()
    for scheme in ("http://", "https://"):
        if text.startswith(scheme):
            text = text.removeprefix(scheme)
            break
    text = text.removesuffix("/")
    for default_port in (":80", ":443"):
        if text.endswith(default_port):
            text = text.removesuffix(default_port)
            break
    return text


def is_allowed_origin(origin: str, host_header: str, allowed_origins: Iterable[str]) -> bool:
    """`origin` 可否接受：命中允许列表，或与本次请求的 `Host` **同源**。

    中间件与需要单独加严的路由共用这一份判定（例：会弹系统窗口的目录选择器，它只有本机页面
    该调）。允许列表按每次调用归一化——条目是个位数，这点开销换调用方不必自己预处理。

    同源之所以可以直接放行：CSRF 的前提是**跨站**发起，而同源请求本来就带着本站的凭据与
    `Host`，没有任何"被别的站点驱动"的成分。

    Args:
        origin: 请求的 `Origin` 头。`"null"`（沙箱 / `file://`）按普通字符串比对，因此不可能同源。
        host_header: 本次请求的 `Host` 头，仅用于同源判定。
        allowed_origins: 允许的跨源来源；大小写与尾部斜杠不敏感。
    """
    if origin.lower() in {item.strip().lower() for item in allowed_origins}:
        return True
    return _authority(origin) == _authority(host_header)


class LocalBoundaryMiddleware:
    """Host / Origin / 正文大小的纯 ASGI 中间件。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str],
        max_body_bytes: int,
    ) -> None:
        self.app = app
        self.allowed_hosts = tuple(host.strip().lower() for host in allowed_hosts)
        self.allowed_origins = tuple(origin.strip().lower() for origin in allowed_origins)
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        host = _host_of(headers.get("host", ""))
        if host not in self.allowed_hosts:
            await self._reject(send, 403, "Host 不在允许列表里：本服务只接受本机访问。")
            return

        if scope["method"] in UNSAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None and not self._origin_allowed(origin, headers.get("host", "")):
                await self._reject(send, 403, "跨源来源不在允许列表里：状态修改请求只接受本机同源。")
                return

        declared = headers.get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                await self._reject(send, 400, "Content-Length 不是合法整数。")
                return
            if size > self.max_body_bytes:
                await self._too_large(send)
                return

        # 只有**状态修改方法**才需要缓冲正文：读 GET/HEAD/OPTIONS 的正文没有意义，而且会
        # 破坏流式响应（见下面 `replay` 的说明）。
        if scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        if declared is not None:
            await self.app(scope, receive, send)
            return

        # 没有 Content-Length（chunked）：自己按上限读一遍，再重新播给下游。
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body") or b""
            total += len(body)
            if total > self.max_body_bytes:
                await self._too_large(send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        payload = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            """先交出缓冲好的正文，**之后必须把原始 receive 原样透传**。

            这里有两条不能错的口径（实测踩过）：

            - 之后的 `http.disconnect` 必须继续送下去：`StreamingResponse` 靠它收尾，吞掉它响应
              就永远结束不了（SSE 会卡死）；
            - 不能返回一个"立刻就有结果"的合成消息：`receive()` 若永不挂起，调用方（收尾监听器）
              会变成死循环并把整个事件循环饿死。
            """
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    def _origin_allowed(self, origin: str, host_header: str) -> bool:
        """同源放行；跨源必须命中白名单（见 :func:`is_allowed_origin`）。"""
        return is_allowed_origin(origin, host_header, self.allowed_origins)

    async def _too_large(self, send: Send) -> None:
        await self._reject(
            send,
            413,
            f"请求体超过上限（{self.max_body_bytes} 字节）；本服务不接受更大的正文。",
        )

    async def _reject(self, send: Send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

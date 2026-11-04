"""反代：网关 → 主服务（架构 §24）。

**为什么必须重写 Host / Origin**：主服务的 ``LocalBoundaryMiddleware`` 只接受白名单里的 Host
与同源 Origin（``api/security.py:133-140``）。网关若不改写这两项，所有请求都会被 403；而要
放行就得给主服务加一条"信任网关"的豁免——那等于把同源判定降级成"网关说是就是"。改写的方向
相反：主服务的判定一个字都不用动，认证责任留在网关。

**为什么要手写纯 ASGI 而不是 ``BaseHTTPMiddleware``**：后者会把响应包一层，对 SSE 这种长连接
不友好（项目里 ``api/security.py:15-17`` 已经踩过）。这里同样要逐块转发、不做任何缓冲——
SSE 一旦被缓冲，第一个字节永远不会到达手机。

**为什么 WebSocket 要单独一遍握手**：终端的 WS（§23）由浏览器直接向主服务发起，主服务校验
``Origin`` 同源。网关要做的和 HTTP 一样——把 ``Origin``/``Host`` 改写成上游口径，再双向搬运帧。
不能复用 httpx 的客户端：那是 HTTP 库，不处理 ``101 Switching Protocols`` 之后的帧。

**为什么用 ``websockets`` 而不是手写帧解析**：帧掩码、分片、ping/pong、关闭握手都在那里；自己写
一遍只会更快地踩到"终端连上但按键没反应"这类问题。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from contextlib import suppress
from typing import Final

import httpx
from starlette.types import Receive, Scope, Send
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus, WebSocketException

#: 逐跳头：不能原样转发，由两侧的 HTTP 实现各自管理。
HOP_BY_HOP: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: 由网关自己重写或删除的头。
REWRITTEN: Final[frozenset[str]] = frozenset({"host", "origin", "content-length"})

#: WS 握手专有的头：由两侧的 ``websockets`` 各自生成，转发过去只会让握手对不上。
_WS_DROP: Final[frozenset[str]] = frozenset(
    {
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-accept",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
    }
)

#: 与上游建立 WS 的超时（秒）。握手要么很快成功，要么上游没开——拖久了只会让用户看着转圈。
WS_OPEN_TIMEOUT: Final[float] = 10.0

#: 正常关闭码。**必须显式发**：只发 accept 不发 close，uvicorn 会当成协议违规直接 RST。
CLOSE_NORMAL: Final[int] = 1000

#: 网关自己的管理前缀。反代**不**接管它，见 :mod:`local_agent.mobile.gateway`。
ADMIN_PREFIX: Final[str] = "/__mobile"

#: 反代到主服务时打上的标记头。
#:
#: **为什么需要它**：手机经网关访问主服务时，反代之后对端也是 ``127.0.0.1``、Host 与 Origin 也被
#: 改写成上游口径——主服务只靠地址与同源判定**分不出"本机页面"和"经网关过来的手机"**。没有这个
#: 标记，已配对的手机会拿到签发配对码、撤销设备这些管理能力（2026-11-03 审查发现）。
VIA_GATEWAY_HEADER: Final[str] = "x-chaiai-via-gateway"

#: 这些前缀下的主服务端点不接受"经网关进来"的请求：它们只服务本机设置页。
GUARDED_PREFIXES: Final[tuple[str, ...]] = ("/api/mobile/",)


def _decode_headers(raw: Iterable[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    return [(key.decode("latin-1").lower(), value.decode("latin-1")) for key, value in raw]


class ReverseProxy:
    """把网关收到的请求转给主服务，并把响应逐块送回。

    Args:
        client: 共享的 httpx 客户端（生命周期由网关的 lifespan 管理）。
        upstream: 上游基地址，形如 ``http://127.0.0.1:8731``。
        max_body_bytes: 请求体上限，与主服务的 ``web.max_body_bytes`` 同一口径。
        ws_enabled: 是否转发终端的 WebSocket（``mobile.ws_enabled``）。
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        upstream: str,
        max_body_bytes: int,
        ws_enabled: bool = True,
    ) -> None:
        self.client = client
        self.upstream = upstream.rstrip("/")
        self.max_body_bytes = max_body_bytes
        self.ws_enabled = ws_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "websocket":
            await self._proxy_websocket(scope, receive, send)
            return
        if kind != "http":
            return

        body = await self._read_body(receive)
        if body is None:
            await self._reply(send, 413, "请求体超过网关上限。")
            return

        method = scope["method"]
        path = scope.get("path", "/")
        url = self.upstream + path
        query = scope.get("query_string", b"").decode("latin-1")
        if query:
            url = f"{url}?{query}"

        headers = self._rewrite_headers(scope["headers"])
        if path.startswith(GUARDED_PREFIXES):
            # 让主服务能认出"这条请求是经网关进来的"——地址与 Origin 都已被改写，只剩这个可分辨。
            headers.append((VIA_GATEWAY_HEADER, "1"))
        started = False
        try:
            async with self.client.stream(method, url, headers=headers, content=body) as response:
                await send(
                    {
                        "type": "http.response.start",
                        "status": response.status_code,
                        "headers": self._response_headers(response.headers.raw),
                    }
                )
                started = True
                # `aiter_bytes` 而不是 `aiter_raw`：网关统一解压，响应头里也就去掉了
                # content-encoding（否则浏览器会对已解压的字节再解一次）。逐块取，不聚合，
                # SSE 的第一个字节才能立刻到达手机。
                async for chunk in response.aiter_bytes():
                    if chunk:
                        await send(
                            {"type": "http.response.body", "body": chunk, "more_body": True}
                        )
        except httpx.HTTPError as exc:
            # 上游没起来是**最常见**的故障（用户先开了网关、主服务还没启动）。如实说，不重试：
            # 静默重试会把"主服务一直没起来"藏成"页面转圈"。
            if started:
                # 头已经发出去了，这时补一个 502 只会得到一份自相矛盾的响应；中断连接让客户端
                # 如实看到"这条流断了"。**不要**改成静默 return——那会让客户端收到空正文却以为
                # 请求成功了（第一版就是这么写的，把一个真实错误藏成了空页面）。
                raise
            await self._reply(
                send,
                502,
                f"连不上本机主服务（{type(exc).__name__}）：{self.upstream}",
            )
            return
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    # ---- 内部 -------------------------------------------------------------

    async def _read_body(self, receive: Receive) -> bytes | None:
        """把请求体读成一个 bytes；超限返回 ``None``。

        有界缓冲：没有 ``content-length`` 的 chunked 正文也要能拦住，所以按累计长度判断。
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return b""
            chunk = message.get("body") or b""
            total += len(chunk)
            if total > self.max_body_bytes:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    def _rewrite_headers(self, raw: Iterable[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
        """转发用的头：丢掉逐跳与待重写的，补上上游口径的 Host / Origin / Accept-Encoding。"""
        forwarded: list[tuple[str, str]] = []
        had_origin = False
        for name, value in _decode_headers(raw):
            if name in HOP_BY_HOP or name in REWRITTEN:
                had_origin = had_origin or name == "origin"
                continue
            forwarded.append((name, value))
        forwarded.append(("host", self.upstream_netloc))
        if had_origin:
            # 有 Origin 才改写：没有时补一个会等于伪造浏览器身份，把主服务那条
            # "无 Origin 按非浏览器处理"的分支也一起改掉。
            forwarded.append(("origin", self.upstream_scheme_host))
        if not any(name == "accept-encoding" for name, _ in forwarded):
            forwarded.append(("accept-encoding", "gzip, deflate"))
        return forwarded

    @property
    def upstream_netloc(self) -> str:
        return self.upstream.removeprefix("http://").removeprefix("https://")

    @property
    def upstream_scheme_host(self) -> str:
        return self.upstream

    def _response_headers(self, raw: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        """响应头：丢掉逐跳、长度与编码相关的头。

        两条都删：
        - ``content-length``：网关逐块转发，保留它会与客户端实际收到的字节数对不上
          （上游压缩过、或 SSE 根本没有长度）。让 ASGI 服务器自己决定 chunked。
        - ``content-encoding``：正文已经由 httpx 解压（见 ``aiter_bytes``），再告诉浏览器一次
          就是对已解压的字节再解一遍，表现为乱码或直接失败。
        """
        kept: list[tuple[bytes, bytes]] = []
        for key, value in raw:
            name = key.decode("latin-1").lower()
            if name in HOP_BY_HOP or name in {"content-length", "content-encoding"}:
                continue
            kept.append((key, value))
        return kept

    async def _proxy_websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        """把终端的 WebSocket 双向转给主服务（§23 × §24）。

        握手阶段只做两件事：改 ``Host``/``Origin``、透传子协议。帧的搬运交给 ``websockets``——
        这里**不解码内容**，终端传的是任意字节。
        """
        if not self.ws_enabled:
            await self._refuse_websocket(send, "网关已关闭终端的 WebSocket 转发（mobile.ws_enabled）。")
            return
        path = scope.get("path", "/")
        query = scope.get("query_string", b"").decode("latin-1")
        url = f"ws://{self.upstream_netloc}{path}" + (f"?{query}" if query else "")
        subprotocols = list(scope.get("subprotocols") or [])
        accepted = False
        try:
            async with ws_connect(
                url,
                additional_headers=self._ws_headers(scope["headers"]),
                subprotocols=subprotocols or None,
                open_timeout=WS_OPEN_TIMEOUT,
                close_timeout=5,
                # 终端会一次吐大段编译输出；默认 1 MiB 上限会在那里断开，而且报的是"连接被关"，
                # 看起来像终端崩了。
                max_size=None,
            ) as upstream:
                await send({"type": "websocket.accept", "subprotocol": upstream.subprotocol})
                accepted = True
                try:
                    await self._pump(upstream, receive, send)
                finally:
                    # **必须显式关帧**（2026-11-04 实测）：ASGI 应用发完 accept 直接返回，uvicorn
                    # 会当成协议违规、直接 RST 掉 TCP，客户端看到的是
                    # "no close frame received or sent"——终端连上就断，且没有任何原因。
                    with suppress(Exception):
                        await send({"type": "websocket.close", "code": CLOSE_NORMAL})
        except (OSError, InvalidStatus, WebSocketException) as exc:
            if accepted:
                # 已经 accept 过：close 帧在上面的 finally 里发过了，这里再发一次是协议错误。
                return
            # 握手失败（主服务没起来、或被 Origin 判定拒了）。**在 accept 之前关**，浏览器会看到
            # 一次失败的升级而不是"连上又被踢"，前端据此提示才有意义。
            await self._refuse_websocket(send, f"终端的 WebSocket 握手失败：{type(exc).__name__}")

    async def _pump(
        self, upstream: ClientConnection, receive: Receive, send: Send
    ) -> None:
        """双向搬运，任一侧结束就收掉另一侧。"""

        async def client_to_upstream() -> None:
            while True:
                message = await receive()
                kind = message["type"]
                if kind == "websocket.disconnect":
                    return
                # `websocket.connect` 之类不该被当作数据：第一版无条件转发，把空帧发给了上游
                # 终端（主服务收到空字节会当成一次输入）。只认真正的数据帧。
                if kind != "websocket.receive":
                    continue
                text = message.get("text")
                if text is not None:
                    await upstream.send(text)
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
                # text 与 bytes 都为 None 是空帧/心跳，跳过——别给终端发空字节。

        async def upstream_to_client() -> None:
            async for data in upstream:
                if isinstance(data, str):
                    await send({"type": "websocket.send", "text": data})
                else:
                    await send({"type": "websocket.send", "bytes": data})

        tasks = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        ]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                error = task.exception()
                # 连接被对端关掉是正常结束，不是错误；其余异常要冒出去让调用方如实报。
                if error is not None and not isinstance(error, (OSError, WebSocketException)):
                    raise error
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _ws_headers(self, raw: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
        """WS 握手头：丢掉握手专有头与待重写的，补上上游口径的 Host/Origin。

        与 HTTP 那条**不同**：这里无条件补 ``Origin``。主服务的 WS 校验要求它存在且同源
        （``api/security.py:124-132``），缺了会被直接拒——而浏览器升级请求本来就一定带 Origin。
        """
        headers: dict[str, str] = {}
        for name, value in _decode_headers(raw):
            if name in HOP_BY_HOP or name in REWRITTEN or name in _WS_DROP:
                continue
            headers[name] = value
        headers["host"] = self.upstream_netloc
        headers["origin"] = self.upstream_scheme_host
        return headers

    async def _refuse_websocket(self, send: Send, reason: str) -> None:
        await send({"type": "websocket.close", "code": 1008, "reason": reason})

    async def _reply(self, send: Send, status: int, detail: str) -> None:
        payload = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


__all__ = ["ADMIN_PREFIX", "ReverseProxy"]


def build_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """网关共享的上游客户端。

    ``read`` 不限时：SSE 的两次事件之间可能安静几分钟，设了读超时就会把正常的长连接掐断。
    连接与写入仍限时——那是"上游没起来"与"上游不读"的区别。

    Args:
        transport: 注入用的传输层（测试用它断言"发出去的到底是什么"）；``None`` 走真实网络。
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )

"""网关应用：配对认证 + 反代（架构 §24；第 1 步）。

结构上没有用 FastAPI 的普通路由去承载反代——反代要把响应**逐块**写回，普通 endpoint 做不到
（见 :mod:`local_agent.mobile.proxy`）。所以这里的形状是：

- 一个很小的 FastAPI 应用，只挂 ``/__mobile`` 下的管理接口；
- 一个纯 ASGI 中间件，负责"识别管理路径 → 校验设备 Token → 交给反代"。

**管理接口的双重回环判定**（这一条是安全关键）：只检查 TCP 对端是 127.0.0.1 是不够的——
cloudflared / tailscale 这类隧道进程就跑在本机，它们转发进来的连接,**对端也是 127.0.0.1**。
所以管理接口要求"对端回环 **且** ``Host`` 头是回环写法"。手机经隧道访问时 Host 是公网域名，
一律 403。少了后半条，任何拿到配对链接的人都能调用"生成新配对码"。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from local_agent.config import AppConfig
from local_agent.mobile.admin_page import render_admin_page
from local_agent.mobile.channels import phone_base_url, phone_base_urls
from local_agent.mobile.pairing import Device, PairingError, PairingStore
from local_agent.mobile.proxy import ADMIN_PREFIX, ReverseProxy, build_client
from local_agent.mobile.qr import svg_qr

#: 设备凭据的 cookie 名。``HttpOnly`` + ``SameSite=Strict``：脚本读不到，跨站请求不带。
COOKIE_NAME = "chaiai_mobile_token"

#: 管理接口接受的 Host 头主机名。**只认回环写法**，见模块文档。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: 设备凭据的保存时长（秒）。30 天：手机是长期设备，每次都要重配一遍没人受得了。
DEVICE_COOKIE_MAX_AGE = 30 * 24 * 3600


def _header_map(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _host_name(value: str) -> str:
    """从 ``Host`` 里取主机名（去端口、小写、IPv6 保留方括号）。"""
    text = value.strip()
    if text.startswith("["):
        end = text.find("]")
        return (text[: end + 1] if end != -1 else text).lower()
    return (text.rsplit(":", 1)[0] if ":" in text else text).lower()


def is_local_admin(scope: Scope) -> bool:
    """这个请求是否来自"本机浏览器直连网关"。

    两条都要满足：TCP 对端是回环，且 ``Host`` 是回环写法。理由见模块文档——隧道进程的对端
    地址同样是回环，只看地址会把公网访问当成管理员。
    """
    client = scope.get("client")
    peer = client[0] if client else ""
    if peer not in {"127.0.0.1", "::1"}:
        return False
    return _host_name(_header_map(scope).get("host", "")) in LOOPBACK_HOSTS


def _is_secure_request(request: Request) -> bool:
    """这个请求是否来自 https 面——含"隧道在本机终结 TLS"的情形（2026-11-06）。

    网关自己只听 http：局域网 https 由 :func:`local_agent.mobile.channels.build_listeners`
    多开的监听直接给，那时 ``request.url.scheme`` 就是 ``https``。但 tailscale serve / funnel
    与 cloudflared 这类隧道是**先把 TLS 解掉、再用明文转发到回环端口**，对端是本机、scheme 永远
    是 http，只有 ``X-Forwarded-Proto`` 记得原始协议。不看它就等于公网可达的设备 cookie 缺
    ``Secure``。

    只取第一段（多级代理时最靠近客户端的那一段才是原始协议）。客户端自己伪造这个头只会让
    自己的 cookie 变成 ``Secure`` 而在明文通道上用不了，是自伤，不构成对他人的降级。
    """
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def _token_from(scope: Scope) -> str:
    """从 cookie 或 ``Authorization: Bearer`` 取设备 Token；没有则空串。"""
    headers = _header_map(scope)
    authorization = headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip() == COOKIE_NAME:
            return value.strip().strip('"')
    return ""


class _AuthProxyMiddleware:
    """纯 ASGI 中间件：管理路径放行，其余校验 Token 后反代。

    写成纯 ASGI 而不是 ``BaseHTTPMiddleware``：后者会把响应包一层，SSE 会被它卡住
    （``api/security.py:15-17`` 记过同一个坑）。
    """

    def __init__(self, app: ASGIApp, *, config: AppConfig, store: PairingStore) -> None:
        self.app = app
        self.config = config
        self.store = store
        self.upstream = f"http://{config.web.host}:{config.web.port}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", "/"))
        if path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/"):
            await self.app(scope, receive, send)
            return

        token = _token_from(scope)
        device = self.store.verify(token) if token else None
        if device is None:
            await self._unauthorized(scope, send)
            return

        client = getattr(scope.get("app"), "state", None)
        http_client = getattr(client, "http_client", None)
        if http_client is None:  # pragma: no cover - lifespan 未跑属于装配错误
            await self._plain(send, 503, "网关尚未就绪：HTTP 客户端没有初始化。")
            return
        proxy = ReverseProxy(
            client=http_client,
            upstream=self.upstream,
            max_body_bytes=self.config.web.max_body_bytes,
            ws_enabled=self.config.mobile.ws_enabled,
        )
        await proxy(scope, receive, send)

    async def _unauthorized(self, scope: Scope, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "未配对设备"})
            return
        await self._plain(
            send,
            401,
            "未配对的设备：请先在电脑上生成配对链接，并在手机浏览器里打开它完成配对。",
        )

    async def _plain(self, send: Send, status: int, detail: str) -> None:
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


def _pair_page(gateway_host: str, gateway_port: int) -> str:
    """配对页：一个输入框 + 一次 fetch。不引外部资源，不依赖前端产物。"""
    action = f"{ADMIN_PREFIX}/pair"
    return rf"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>配对 chaiai</title>
<style>
 body{{margin:0;padding:2rem 1.25rem;font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
      background:#faf9f7;color:#1f1d1a}}
 h1{{font-size:1.25rem;margin:0 0 .5rem}}
 p{{color:#6b6560;margin:0 0 1.5rem}}
 input,button{{width:100%;box-sizing:border-box;font:inherit;padding:.75rem;border-radius:.6rem}}
 input{{border:1px solid #ddd8d2;background:#fff;margin-bottom:.75rem}}
 button{{border:0;background:#c2571a;color:#fff;font-weight:600}}
 #msg{{margin-top:1rem;color:#a3312a;min-height:1.5em}}
</style></head>
<body>
<h1>配对这台设备</h1>
<p>在电脑上打开「移动访问」生成配对链接，或直接把它给的配对码填在下面。</p>
<input id="code" inputmode="text" autocomplete="one-time-code" placeholder="配对码">
<button id="go">配对</button>
<div id="msg"></div>
<script>
const code = new URLSearchParams(location.search).get('code');
if (code) document.getElementById('code').value = code;

// 设备名要能一眼认出来。整段 UA 塞进去，设置页里就是一串 "Mozilla/5.0 (Linux; U; Android..."，
// 用户既认不出是哪台，也删不对哪一条。
function deviceLabel() {{
  var ua = navigator.userAgent || '';
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua)) return 'iPad';
  if (/Android/.test(ua)) {{
    var model = ua.match(/Android[^;]*;\s*([^;)]+?)\s*(?:Build|[)])/);
    return (model ? model[1].trim() : 'Android 设备').slice(0, 40);
  }}
  if (/Macintosh/.test(ua)) return 'Mac';
  if (/Windows/.test(ua)) return 'Windows 浏览器';
  return '浏览器设备';
}}
document.getElementById('go').onclick = async () => {{
  const el = document.getElementById('msg');
  el.textContent = '正在配对…';
  try {{
    const r = await fetch('{action}', {{
      method: 'POST', headers: {{'content-type': 'application/json'}},
      body: JSON.stringify({{code: document.getElementById('code').value,
                             label: deviceLabel()}}),
    }});
    const data = await r.json().catch(() => ({{}}));
    if (!r.ok) {{ el.textContent = data.detail || ('配对失败（' + r.status + '）'); return; }}
    location.replace('/');
  }} catch (e) {{ el.textContent = '网络错误：' + e; }}
}};
</script>
<p style="margin-top:2rem;font-size:.85rem">网关 {gateway_host}:{gateway_port} · 配对成功后这台设备会获得{DEVICE_COOKIE_MAX_AGE // 86400}天授权</p>
</body></html>"""


def _device_view(device: Device) -> dict[str, Any]:
    """设备对外视图：**不含** Token 摘要。"""
    return {
        "id": device.id,
        "label": device.label,
        "channel": device.channel,
        "origin": device.origin,
        "created_at": device.created_at,
    }


def create_gateway(
    config: AppConfig,
    *,
    state_dir: Path,
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """构造网关应用。

    Args:
        config: 完整配置；用它取上游地址（``web.host`` / ``web.port``）与体量上限。
        state_dir: 网关状态目录（``data_root/mobile``）。调用方负责给出绝对路径。
        transport: 注入的上游传输层（测试用）；``None`` 走真实网络。
        client: 外部提供并负责关闭的客户端。多监听场景由入口传入，**只有主监听**会跑
            lifespan，若每个监听各建一个客户端，其中一个被关掉之后另一个就再也发不出请求。
    """
    store = PairingStore(state_dir)
    http_client = client if client is not None else build_client(transport)
    owns_client = client is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_client and not http_client.is_closed:
                await http_client.aclose()

    app = FastAPI(
        title="chaiai mobile gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    # 直接挂上而不是等 lifespan：多监听时第二个 Server 走 ``lifespan="off"``，如果 state 只在
    # lifespan 里设置，那条监听上的第一个请求会拿到 503。
    app.state.http_client = http_client

    def _admin_denied() -> Response:
        return JSONResponse(
            {"detail": "这个接口只接受本机浏览器的直连访问。"}, status_code=403
        )

    @app.get(f"{ADMIN_PREFIX}/", response_class=HTMLResponse, include_in_schema=False)
    async def admin_home(request: Request) -> Response:
        """管理页：生成配对二维码、查看并撤销已配对设备。

        它本身也受回环判定保护——这一页能签发配对码、能踢掉设备，等于半个管理员终端。
        """
        if not is_local_admin(request.scope):
            return _admin_denied()
        return HTMLResponse(render_admin_page(), headers={"cache-control": "no-store"})

    @app.get(f"{ADMIN_PREFIX}/status")
    async def status(request: Request) -> Response:
        if not is_local_admin(request.scope):
            return _admin_denied()
        return JSONResponse(
            {
                "upstream": f"http://{config.web.host}:{config.web.port}",
                "gateway": f"{config.mobile.gateway_host}:{config.mobile.gateway_port}",
                # 手机可达的地址；没有启用通道时是 null，界面上要说清楚"手机还连不上"。
                "phone_url": phone_base_url(config),
                "devices": [_device_view(item) for item in store.list_devices()],
                "pending_codes": len(store.pending_codes()),
            }
        )

    @app.post(f"{ADMIN_PREFIX}/pairing-code")
    async def issue_code(request: Request) -> Response:
        if not is_local_admin(request.scope):
            return _admin_denied()
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 空 body 也要能按默认通道签发
            payload = {}
        channel = str((payload or {}).get("channel", "lan")) if isinstance(payload, dict) else "lan"
        try:
            code = store.issue_code(channel, float(config.mobile.pairing_ttl_seconds))
        except PairingError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        bases = phone_base_urls(config)
        if not bases:
            # 没有可用通道时**不假装能用**：网关只听回环，`http://127.0.0.1:8732` 在手机上是
            # 手机自己。给一个扫了打不开的二维码，比明说"还差一条通道"更糟。
            return JSONResponse(
                {
                    "detail": "还没有手机可达的通道：在配置里启用局域网或 Tailscale 通道，"
                    "并确认手机与这台电脑在同一个网络。"
                },
                status_code=409,
            )
        pair_url = f"{bases[0][1]}{ADMIN_PREFIX}/pair?code={code.code}"
        return JSONResponse(
            {
                # 配对码只在这一行出现一次；之后任何接口都只回数量。
                "code": code.code,
                "channel": code.channel,
                "expires_in": round(code.remaining()),
                "pair_url": pair_url,
                "alternates": [
                    {"label": label, "url": f"{base}{ADMIN_PREFIX}/pair?code={code.code}"}
                    for label, base in bases[1:]
                ],
                # 二维码服务端生成：前端不必引任何二维码库，也不必把码拼进图片 URL
                # （那样配对码会进访问日志）。
                "qr_svg": svg_qr(pair_url).decode("utf-8"),
            }
        )

    @app.get(f"{ADMIN_PREFIX}/devices")
    async def list_devices(request: Request) -> Response:
        if not is_local_admin(request.scope):
            return _admin_denied()
        return JSONResponse({"devices": [_device_view(item) for item in store.list_devices()]})

    @app.delete(f"{ADMIN_PREFIX}/devices/{{device_id}}")
    async def revoke_device(device_id: str, request: Request) -> Response:
        if not is_local_admin(request.scope):
            return _admin_denied()
        removed = store.revoke(device_id)
        return JSONResponse(
            {"revoked": removed},
            status_code=200 if removed else 404,
        )

    @app.get(f"{ADMIN_PREFIX}/pair", response_class=HTMLResponse)
    async def pair_page(request: Request) -> Response:
        """配对页**匿名可达**：用户此时还没有 Token。

        已配对的设备**直接进主页**：同一台手机再扫一次码（或再点一次链接）不该让它再配一遍
        ——那会多出一条设备记录，而用户看到的是"怎么又要配"。判断只看 Token，**不按 Origin
        de-dupe**：同一个 WiFi 下两台手机的 Origin 完全相同，按它去重会误杀另一台设备。
        """
        token = request.cookies.get(COOKIE_NAME, "")
        if token and store.verify(token) is not None:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(
            _pair_page(config.mobile.gateway_host, config.mobile.gateway_port),
            headers={"cache-control": "no-store"},
        )

    @app.post(f"{ADMIN_PREFIX}/pair")
    async def redeem(request: Request) -> Response:
        """用配对码换设备凭据。**匿名可达**，但需要一次性配对码。"""
        try:
            payload: Any = await request.json()
        except Exception:  # noqa: BLE001
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "请求体必须是 JSON 对象。"}, status_code=400)
        code = str(payload.get("code", ""))
        label = str(payload.get("label", ""))
        origin = (request.headers.get("origin") or "").strip()
        try:
            device, token = store.redeem(code, label=label, origin=origin, channel="pair")
        except PairingError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=401)

        response = JSONResponse({"ok": True, "device": _device_view(device)})
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=DEVICE_COOKIE_MAX_AGE,
            httponly=True,
            samesite="strict",
            # 只在 HTTPS 下设 Secure：局域网 http 场景下带 Secure 的 cookie 会被浏览器直接丢弃，
            # 表现为"配对成功但下一个请求还是 401"。隧道终结 TLS（tailscale serve / cloudflared）
            # 时 scheme 仍是 http，所以要看 X-Forwarded-Proto，见 _is_secure_request。
            secure=_is_secure_request(request),
            path="/",
        )
        response.headers["cache-control"] = "no-store"
        return response

    app.add_middleware(_AuthProxyMiddleware, config=config, store=store)
    app.state.pairing_store = store
    return app


__all__ = ["COOKIE_NAME", "LOOPBACK_HOSTS", "create_gateway", "is_local_admin"]

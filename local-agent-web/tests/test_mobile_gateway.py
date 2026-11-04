"""移动网关：配对、认证、反代与回环边界（架构 §24 第 1 步）。

这组用例守的是"**放过一个不该放的请求**"那一类故障，所以断言偏向负例：没有 Token 必须 401，
隧道域名调管理接口必须 403，撤销之后旧 Token 必须立刻失效。正向用例只是用来证明反代确实
把请求送到了上游、并且**改写了 Host / Origin**。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from local_agent.config import AppConfig, MobileChannelConfig, MobileConfig, WebConfig
from local_agent.mobile import __main__ as mobile_main
from local_agent.mobile.gateway import COOKIE_NAME, create_gateway
from local_agent.mobile.pairing import PairingError, PairingStore
from local_agent.mobile.proxy import ReverseProxy

#: 模拟"本机浏览器"的对端地址。TestClient 默认是 ``testclient``，那会被管理接口判成远程。
LOOPBACK = ("127.0.0.1", 53211)

#: 上游在测试里看到的真实地址（配置里的 web.host / web.port）。
UPSTREAM = "http://127.0.0.1:8731"

GATEWAY_BASE = "http://127.0.0.1:8732"


@pytest.fixture(autouse=True)
def _fake_lan(monkeypatch: pytest.MonkeyPatch) -> None:
    """让"手机地址"不依赖真实网卡：CI 与离线机器上没有局域网地址，会让所有配对用例变成 409。"""
    monkeypatch.setattr("local_agent.mobile.channels.lan_addresses", lambda: ["192.168.1.5"])


def _config(tmp_path: Path, *, enabled: bool = True, ws_enabled: bool = True) -> AppConfig:
    return AppConfig(
        data_root=str(tmp_path),
        web=WebConfig(host="127.0.0.1", port=8731),
        mobile=MobileConfig(
            enabled=enabled,
            gateway_port=8732,
            channels=[MobileChannelConfig(type="lan", enabled=True, port=8743)],
            ws_enabled=ws_enabled,
        ),
    )


def _recording_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    """把上游收到的请求记下来，并按路径给固定响应。

    **必须用 ``stream=`` 而不是 ``content=``**：``httpx.Response(200, json=...)`` 在构造时就把
    正文读完了，网关再 ``aiter_bytes`` 会拿到 ``StreamConsumed``——那是替身与真实网络的行为差，
    不是网关的 bug。
    """

    def _respond(body: bytes, content_type: str) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(body),
            headers={"content-type": content_type},
        )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/health":
            return _respond(json.dumps({"status": "ok"}).encode(), "application/json")
        return _respond(b"upstream-body", "text/plain; charset=utf-8")

    return httpx.MockTransport(handler)


def _client(
    tmp_path: Path,
    *,
    seen: list[httpx.Request] | None = None,
    ws_enabled: bool = True,
) -> TestClient:
    record: list[httpx.Request] = [] if seen is None else seen
    app = create_gateway(
        _config(tmp_path, ws_enabled=ws_enabled),
        state_dir=tmp_path / "mobile",
        transport=_recording_transport(record),
    )
    return TestClient(app, base_url=GATEWAY_BASE, client=LOOPBACK)


def _pair(client: TestClient, *, label: str = "test-device") -> dict[str, Any]:
    """走一遍完整配对：本机签发码 → 匿名兑换 → 客户端自动持有 cookie。"""
    issued = client.post("/__mobile/pairing-code", json={"channel": "lan"})
    assert issued.status_code == 200, issued.text
    code = issued.json()["code"]
    paired = client.post("/__mobile/pair", json={"code": code, "label": label})
    assert paired.status_code == 200, paired.text
    return paired.json()


# ---- 认证 ----------------------------------------------------------------


def test_unpaired_request_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/health")
    assert response.status_code == 401
    assert "配对" in response.json()["detail"]


def test_wrong_token_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/health", headers={"authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


def test_pair_then_request_is_forwarded(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    with _client(tmp_path, seen=seen) as client:
        _pair(client)
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert seen and seen[0].url.path == "/api/health"


def test_host_and_origin_are_rewritten(tmp_path: Path) -> None:
    """网关必须把 Host / Origin 改成上游口径，否则主服务会 403；但又不能给主服务开豁免。"""
    seen: list[httpx.Request] = []
    with _client(tmp_path, seen=seen) as client:
        _pair(client)
        client.get("/api/health", headers={"origin": GATEWAY_BASE})
    assert seen[0].headers["host"] == "127.0.0.1:8731"
    assert seen[0].headers["origin"] == UPSTREAM


def test_cookie_is_httponly_and_samesite_strict(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        issued = client.post("/__mobile/pairing-code", json={"channel": "lan"}).json()
        response = client.post("/__mobile/pair", json={"code": issued["code"], "label": "x"})
    raw = response.headers["set-cookie"].lower()
    assert COOKIE_NAME in raw
    assert "httponly" in raw
    assert "samesite=strict" in raw


# ---- 配对码 --------------------------------------------------------------


def test_pairing_code_is_single_use(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        code = client.post("/__mobile/pairing-code", json={}).json()["code"]
        first = client.post("/__mobile/pair", json={"code": code, "label": "a"})
        second = client.post("/__mobile/pair", json={"code": code, "label": "b"})
    assert first.status_code == 200
    assert second.status_code == 401


def test_expired_code_is_rejected(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "mobile")
    code = store.issue_code("lan", 0.001)
    time.sleep(0.02)
    with pytest.raises(PairingError):
        store.redeem(code.code, label="late", origin="", channel="lan")


def test_unknown_code_does_not_reveal_whether_it_existed(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/__mobile/pair", json={"code": "definitely-not-issued", "label": "x"})
    assert response.status_code == 401
    # 文案不区分"不存在 / 过期 / 已用掉"：区分了就是在告诉枚举者"这个码存在过"。
    assert "无效或已过期" in response.json()["detail"]


# ---- 撤销 ----------------------------------------------------------------


def test_revoke_invalidates_old_token_immediately(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        paired = _pair(client)
        assert client.get("/api/health").status_code == 200
        device_id = paired["device"]["id"]
        revoked = client.delete(f"/__mobile/devices/{device_id}")
        assert revoked.status_code == 200 and revoked.json()["revoked"] is True
        after = client.get("/api/health")
    assert after.status_code == 401


def test_revoke_unknown_device_is_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.delete("/__mobile/devices/nope")
    assert response.status_code == 404


def test_status_never_returns_token_digest(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _pair(client)
        payload = client.get("/__mobile/status").json()
    assert payload["devices"]
    assert "token_sha256" not in payload["devices"][0]


# ---- 回环边界（安全关键）-------------------------------------------------


def test_admin_api_rejects_public_host_even_from_loopback_peer(tmp_path: Path) -> None:
    """隧道进程就在本机：对端是 127.0.0.1，但 Host 是公网域名。

    只看对端地址的实现会把这种请求当成管理员，于是任何拿到配对链接的人都能签发新码。
    """
    app = create_gateway(
        _config(tmp_path),
        state_dir=tmp_path / "mobile",
        transport=_recording_transport([]),
    )
    with TestClient(app, base_url="http://abc123.ts.net", client=LOOPBACK) as client:
        response = client.post("/__mobile/pairing-code", json={})
    assert response.status_code == 403


def test_admin_api_rejects_remote_peer(tmp_path: Path) -> None:
    app = create_gateway(
        _config(tmp_path),
        state_dir=tmp_path / "mobile",
        transport=_recording_transport([]),
    )
    with TestClient(app, base_url=GATEWAY_BASE, client=("203.0.113.9", 4010)) as client:
        response = client.get("/__mobile/status")
    assert response.status_code == 403


def test_pair_page_is_anonymous(tmp_path: Path) -> None:
    """配对页必须匿名可开，否则用户永远拿不到第一个 Token（鸡生蛋）。"""
    with _client(tmp_path) as client:
        response = client.get("/__mobile/pair")
    assert response.status_code == 200
    assert "配对" in response.text


# ---- 传输 ----------------------------------------------------------------


def test_websocket_is_refused_when_disabled(tmp_path: Path) -> None:
    """`mobile.ws_enabled=false` 时明确拒绝并说明。

    这个开关**不是**安全边界（远程执行 terminal 与远程执行 HTTP 接口同级权限），留它是为了排查：
    某条隧道下 WS 表现异常时，关掉它让页面其余部分先可用，而不是整条通道下线。
    """
    with _client(tmp_path, ws_enabled=False) as client:
        _pair(client)
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/terminals/x/stream"),
        ):
            pass
    assert excinfo.value.code == 1008


def test_websocket_handshake_failure_is_reported(tmp_path: Path) -> None:
    """默认开启时会真的去连主服务；连不上就如实断开，不是静默挂住。

    **真实的帧转发没在这里覆盖**：那需要一条活的终端连接，由进程级验证负责（起主服务 + 网关，
    用真 websocket 客户端连）。这里只证明"网关会去握手、失败会如实报"。
    """
    with _client(tmp_path) as client:
        _pair(client)
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/terminals/x/stream"),
        ):
            pass
    assert excinfo.value.code == 1008


def test_mobile_admin_requests_are_marked_as_coming_through_the_gateway(
    tmp_path: Path,
) -> None:
    """反代 `/api/mobile/*` 时必须打标记，否则主服务分不出"本机页面"和"经网关的手机"。

    手机经网关过来时，反代之后对端也是 127.0.0.1、Host 与 Origin 也被改写——只靠地址与同源判定，
    已配对的手机能签发配对码、撤销设备（2026-11-03 审查发现）。
    """
    seen: list[httpx.Request] = []
    with _client(tmp_path, seen=seen) as client:
        _pair(client)
        client.get("/api/mobile/devices")
    marked = [request for request in seen if request.url.path == "/api/mobile/devices"]
    assert marked, "这条请求没有被反代出去"
    assert marked[0].headers.get("x-chaiai-via-gateway") == "1"


def test_ws_handshake_rewrites_origin_and_drops_handshake_headers() -> None:
    """WS 握手的改写必须和 HTTP 那条一致，且**无条件**补 Origin。

    主服务的 WS 校验要求 Origin 存在且同源（`api/security.py:124-132`）。少了这一条，手机上的
    终端会在握手阶段被拒，而现象和"终端不存在"长得一模一样——所以这里直接断言改写结果。
    """
    proxy = ReverseProxy(
        client=httpx.AsyncClient(),
        upstream="http://127.0.0.1:8731",
        max_body_bytes=1024,
    )
    headers = proxy._ws_headers(
        [
            (b"origin", b"http://192.168.20.174:8743"),
            (b"host", b"192.168.20.174:8743"),
            (b"sec-websocket-key", b"dGhlIHNhbXBsZSBub25jZQ=="),
            (b"sec-websocket-protocol", b"chat"),
            (b"sec-websocket-extensions", b"permessage-deflate"),
            (b"user-agent", b"Mobile"),
        ]
    )
    assert headers["origin"] == "http://127.0.0.1:8731"
    assert headers["host"] == "127.0.0.1:8731"
    # 握手专有头由两侧的 websockets 各自生成，转发过去只会让握手对不上。
    assert "sec-websocket-key" not in headers
    assert "sec-websocket-protocol" not in headers
    assert "sec-websocket-extensions" not in headers
    assert headers["user-agent"] == "Mobile"


def test_upstream_failure_is_reported_as_502(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    app = create_gateway(
        _config(tmp_path),
        state_dir=tmp_path / "mobile",
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app, base_url=GATEWAY_BASE, client=LOOPBACK) as client:
        _pair(client)
        response = client.get("/api/health")
    assert response.status_code == 502
    assert "主服务" in response.json()["detail"]


# ---- 入口 ----------------------------------------------------------------


def test_disabled_gateway_refuses_to_start(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mobile:\n  enabled: false\n", encoding="utf-8")
    assert mobile_main.main(["--config", str(config_path)]) == 1


def test_pair_cli_prints_a_one_time_link(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "web:\n  port: 8731\nmobile:\n  enabled: true\n  gateway_port: 8732\n",
        encoding="utf-8",
    )
    assert mobile_main.main(["--config", str(config_path), "--pair"]) == 0
    out = capsys.readouterr().out
    assert "/__mobile/pair?code=" in out


# ---- 管理页（2026-11-03：把 API 变成能点的界面）---------------------------


def _gateway_app(tmp_path: Path, config: AppConfig | None = None) -> Any:
    return create_gateway(
        config or _config(tmp_path),
        state_dir=tmp_path / "mobile",
        transport=_recording_transport([]),
    )


def test_admin_page_is_reachable_from_loopback(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        page = client.get("/__mobile/")
    assert page.status_code == 200
    assert "移动访问" in page.text
    assert "生成配对二维码" in page.text
    assert "已配对的设备" in page.text


def test_admin_page_rejects_public_host(tmp_path: Path) -> None:
    """管理页能签发配对码、能踢设备，等于半个管理员终端：公网域名一律 403。"""
    with TestClient(
        _gateway_app(tmp_path), base_url="http://abc123.ts.net", client=LOOPBACK
    ) as client:
        response = client.get("/__mobile/")
    assert response.status_code == 403


def test_pairing_code_returns_a_scannable_svg_and_lan_url(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        payload = client.post("/__mobile/pairing-code", json={"channel": "lan"}).json()
    # 二维码必须指向局域网地址：回环地址在手机上是手机自己。默认通道是明文 http。
    assert payload["pair_url"].startswith("http://192.168.1.5:8743/__mobile/pair?code=")
    assert "<svg" in payload["qr_svg"]


def test_pairing_code_refuses_when_no_channel_is_usable(tmp_path: Path) -> None:
    """没有可用通道时明说，而不是给一个扫了打不开的二维码。"""
    config = AppConfig(
        data_root=str(tmp_path),
        web=WebConfig(host="127.0.0.1", port=8731),
        mobile=MobileConfig(enabled=True, gateway_port=8732),
    )
    with TestClient(_gateway_app(tmp_path, config), base_url=GATEWAY_BASE, client=LOOPBACK) as client:
        response = client.post("/__mobile/pairing-code", json={})
    assert response.status_code == 409
    assert "通道" in response.json()["detail"]


def test_status_reports_phone_url(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        payload = client.get("/__mobile/status").json()
    assert payload["phone_url"] == "http://192.168.1.5:8743"


def test_paired_device_skips_the_pairing_page(tmp_path: Path) -> None:
    """同一台手机再扫一次码不该再配一遍——那会多出一条记录，用户看到的是"怎么又要配"。"""
    with _client(tmp_path) as client:
        _pair(client)
        before = len(client.get("/__mobile/devices").json()["devices"])
        again = client.get("/__mobile/pair?code=whatever", follow_redirects=False)
        after = len(client.get("/__mobile/devices").json()["devices"])
    assert again.status_code == 303
    assert again.headers["location"] == "/"
    assert before == after == 1


def test_unpaired_visitor_still_sees_the_pairing_form(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        page = client.get("/__mobile/pair?code=whatever", follow_redirects=False)
    assert page.status_code == 200
    assert "配对" in page.text

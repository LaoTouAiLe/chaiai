"""移动通道：局域网地址、自签证书与通道选择（架构 §24 第 2 步）。

证书用例断言的是"**换地址必须重签**"这条：少了它，用户换到另一个网段之后手机看到的是
"证书与此地址不匹配"，而这在界面上看起来像"服务器坏了"。
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
from cryptography import x509
from pydantic import ValidationError

from local_agent.config import AppConfig, MobileChannelConfig, MobileConfig, WebConfig
from local_agent.mobile.certs import ensure_certificates
from local_agent.mobile.channels import (
    ChannelNotReady,
    build_external_channels,
    build_listeners,
    lan_addresses,
    phone_base_urls,
    tailscale_dns_name,
)


def _san_ips(cert_path: Path) -> set[str]:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return {str(item) for item in ext.value.get_values_for_type(x509.IPAddress)}


def _config(*channels: MobileChannelConfig, gateway_port: int = 8732) -> AppConfig:
    return AppConfig(
        web=WebConfig(host="127.0.0.1", port=8731),
        mobile=MobileConfig(
            enabled=True, gateway_port=gateway_port, channels=list(channels)
        ),
    )


# ---- 局域网地址 ----------------------------------------------------------


def test_lan_addresses_are_private_and_not_loopback() -> None:
    """真实网卡枚举的结果必须全是私网、非回环——那些是能把手机连上的地址。"""
    for text in lan_addresses():
        address = ipaddress.ip_address(text)
        assert address.is_private
        assert not address.is_loopback


def test_lan_addresses_are_unique() -> None:
    addresses = lan_addresses()
    assert len(addresses) == len(set(addresses))


# ---- 自签证书 ------------------------------------------------------------


def test_certificates_are_created_and_cover_the_address(tmp_path: Path) -> None:
    result = ensure_certificates(tmp_path / "certs", ["192.168.1.5"])
    assert result.ca_cert.is_file()
    assert result.server_cert.is_file()
    assert result.server_key.is_file()
    assert result.regenerated is True
    ips = _san_ips(result.server_cert)
    assert "192.168.1.5" in ips
    assert "127.0.0.1" in ips


def test_certificate_is_reused_when_addresses_do_not_change(tmp_path: Path) -> None:
    """地址没变就不重签：每次启动都换证书会让已经装过 CA 的手机重新报警告。"""
    first = ensure_certificates(tmp_path / "certs", ["192.168.1.5"])
    assert first.regenerated is True
    second = ensure_certificates(tmp_path / "certs", ["192.168.1.5"])
    assert second.regenerated is False


def test_certificate_is_reissued_when_the_address_set_grows(tmp_path: Path) -> None:
    """换网络、加网卡都要重签——否则新地址在证书里不存在，手机报"不匹配"。"""
    ensure_certificates(tmp_path / "certs", ["192.168.1.5"])
    again = ensure_certificates(tmp_path / "certs", ["192.168.1.5", "10.0.0.7"])
    assert again.regenerated is True
    assert "10.0.0.7" in _san_ips(again.server_cert)


def test_ca_is_never_regenerated(tmp_path: Path) -> None:
    """CA 重签等于让所有已装 CA 的手机同时失效——地址变化只该重签服务器证书。"""
    first = ensure_certificates(tmp_path / "certs", ["192.168.1.5"])
    ca_before = first.ca_cert.read_bytes()
    ensure_certificates(tmp_path / "certs", ["10.0.0.7"])
    assert first.ca_cert.read_bytes() == ca_before


# ---- 通道选择 ------------------------------------------------------------


def test_disabled_channel_produces_no_listener(tmp_path: Path) -> None:
    config = _config(MobileChannelConfig(type="lan", enabled=False))
    assert build_listeners(config, tmp_path) == []


def test_phone_base_urls_lists_every_reachable_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """给一组而不是一个：某条通道"配了但没生效"时，用户还有退路可点。

    远程优先：tailscale 换网络也能用，局域网只在同一 Wi-Fi 下有效。
    """
    monkeypatch.setattr("local_agent.mobile.channels.tailscale_dns_name", lambda *a: "box.ts.net")
    monkeypatch.setattr("local_agent.mobile.channels.lan_addresses", lambda: ["192.168.1.5"])
    config = _config(
        MobileChannelConfig(type="lan", enabled=True, port=8743),
        MobileChannelConfig(type="tailscale", enabled=True, expose="funnel"),
    )
    urls = phone_base_urls(config)
    assert urls[0] == ("远程（Tailscale）", "https://box.ts.net")
    assert urls[1][1] == "http://192.168.1.5:8743"


def test_phone_base_urls_skips_tailscale_when_it_is_not_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没登录/没开 MagicDNS 时不要给出一个打不开的地址——那比不给更糟。"""
    monkeypatch.setattr("local_agent.mobile.channels.tailscale_dns_name", _fail_not_logged_in)
    monkeypatch.setattr("local_agent.mobile.channels.lan_addresses", lambda: ["192.168.1.5"])
    config = _config(
        MobileChannelConfig(type="lan", enabled=True, port=8743),
        MobileChannelConfig(type="tailscale", enabled=True, expose="funnel"),
    )
    urls = phone_base_urls(config)
    assert [item[1] for item in urls] == ["http://192.168.1.5:8743"]


def test_lan_channel_defaults_to_plain_http(tmp_path: Path) -> None:
    """默认明文 http：自签证书会让每台手机首次访问都点一次"仍要继续访问"。

    这是 2026-11-03 用户明确选的取舍，代价（同网段可见明文）写在配置注释里。
    """
    config = _config(MobileChannelConfig(type="lan", enabled=True, port=8743))
    listeners = build_listeners(config, tmp_path)
    assert len(listeners) == 1
    listener = listeners[0]
    assert listener.name == "lan"
    assert listener.port == 8743
    assert listener.ssl_certfile is None
    assert listener.ssl_keyfile is None
    assert all(url.startswith("http://") for url in listener.urls)
    # 监听面是全网卡，而 urls 是给人看的：两者不要求一一对应。
    assert listener.host == "0.0.0.0"


def test_lan_channel_with_tls_generates_certificates(tmp_path: Path) -> None:
    """打开 tls 才生成证书，且地址变成 https。"""
    config = _config(MobileChannelConfig(type="lan", enabled=True, port=8743, tls=True))
    listener = build_listeners(config, tmp_path)[0]
    assert listener.ssl_certfile is not None
    assert listener.ssl_keyfile is not None
    assert all(url.startswith("https://") for url in listener.urls)


def test_unimplemented_channel_refuses_to_start(tmp_path: Path) -> None:
    """开了却没实现的通道必须报错——静默忽略等于让用户以为它在工作。"""
    config = _config(MobileChannelConfig(type="cloudflared", enabled=True, hostname="a.example.com"))
    with pytest.raises(ChannelNotReady, match="cloudflared"):
        build_listeners(config, tmp_path)


def test_lan_port_must_differ_from_gateway_port() -> None:
    with pytest.raises(ValidationError, match="不能撞"):
        MobileConfig(
            enabled=True,
            gateway_port=8743,
            channels=[MobileChannelConfig(type="lan", port=8743)],
        )


# ---- tailscale（外部命令型通道）------------------------------------------


def _fake_status(payload: str, *, returncode: int = 0):  # type: ignore[no-untyped-def]
    class _Result:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = payload
            self.stderr = ""

    return lambda *args, **kwargs: _Result()


def _fail_not_logged_in(*args: object, **kwargs: object) -> str:
    raise ChannelNotReady("Tailscale 还没登录")


def test_tailscale_dns_name_strips_the_trailing_dot(monkeypatch: pytest.MonkeyPatch) -> None:
    """`status --json` 给的 DNSName 带尾点；直接用会拼出 `xxx.ts.net./` 这种地址。"""
    monkeypatch.setattr(
        "local_agent.mobile.channels.subprocess.run",
        _fake_status('{"BackendState": "Running", "Self": {"DNSName": "box.tail1234.ts.net."}}'),
    )
    assert tailscale_dns_name("tailscale") == "box.tail1234.ts.net"


def test_tailscale_not_logged_in_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "local_agent.mobile.channels.subprocess.run",
        _fake_status('{"BackendState": "NeedsLogin"}'),
    )
    with pytest.raises(ChannelNotReady, match="登录"):
        tailscale_dns_name("tailscale")


def test_tailscale_channel_is_skipped_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """远程通道起不来不该拖垮局域网：只跳过并打印原因，不抛异常。"""
    monkeypatch.setattr("local_agent.mobile.channels.tailscale_dns_name", _fail_not_logged_in)
    config = _config(MobileChannelConfig(type="tailscale", enabled=True, expose="funnel"))
    assert build_external_channels(config) == []


def test_tailscale_funnel_builds_the_expected_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("local_agent.mobile.channels.tailscale_executable", lambda: "tailscale")
    monkeypatch.setattr(
        "local_agent.mobile.channels.tailscale_dns_name", lambda *args: "box.tail1234.ts.net"
    )
    config = _config(MobileChannelConfig(type="tailscale", enabled=True, expose="funnel"))
    channels = build_external_channels(config)
    assert len(channels) == 1
    assert channels[0].start_argv == (
        "tailscale",
        "funnel",
        "--bg",
        "--https=443",
        "http://127.0.0.1:8732",
    )
    # 退出必须 reset：配置写在 tailscaled 里，不 reset 就是"服务关了、公网地址还活着"。
    assert channels[0].stop_argv == ("tailscale", "funnel", "reset")
    assert channels[0].urls == ("https://box.tail1234.ts.net",)


def test_hostname_is_rejected_on_lan_channel() -> None:
    with pytest.raises(ValidationError, match="cloudflared"):
        MobileChannelConfig(type="lan", hostname="agent.example.com")

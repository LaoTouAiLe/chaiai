"""网关进程的运行时句柄（架构 §24；2026-11-03"出问题能操控关闭"）。

覆盖的是**决策与幂等**，不真的拉进程：`start_gateway` 的拉起逻辑另有 `test_mobile_supervisor`
兜底，这里只保证"没接线时不会假装关掉了"。
"""

from __future__ import annotations

from pathlib import Path

from local_agent.config import AppConfig, MobileConfig, WebConfig
from local_agent.mobile.pairing import PairingStore
from local_agent.mobile.runtime import GatewayRuntime


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        data_root=str(tmp_path),
        web=WebConfig(host="127.0.0.1", port=8731),
        mobile=MobileConfig(enabled=True, gateway_port=8732),
    )


def test_start_reports_not_managed_when_nothing_is_attached(tmp_path: Path) -> None:
    """测试或用 `uvicorn` 直接起的服务不管理网关：必须**明说**，不能假装启动成功了。"""
    outcome = GatewayRuntime().start(_config(tmp_path))
    assert outcome.ok is False
    assert "不负责" in outcome.detail
    assert outcome.running is False


def test_stop_is_idempotent_and_successful_when_nothing_is_attached() -> None:
    """没在跑也算成功：紧急场景下用户会连按几次，第二次报错只会让人更慌。"""
    outcome = GatewayRuntime().stop()
    assert outcome.ok is True
    assert outcome.running is False


def test_revoke_all_clears_every_device(tmp_path: Path) -> None:
    """只停网关不够：旧 Token 仍然有效。这里的撤销必须是**物理删除**。"""
    store = PairingStore(tmp_path / "mobile")
    for label in ("a", "b"):
        code = store.issue_code("lan", 300)
        store.redeem(
            code.code, label=label, origin="http://192.168.1.5:8743", channel="lan"
        )
    assert len(store.list_devices()) == 2

    assert store.revoke_all() == 2
    assert store.list_devices() == []
    assert store.revoke_all() == 0

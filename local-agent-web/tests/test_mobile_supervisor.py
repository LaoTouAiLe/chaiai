"""网关进程托管（架构 §24 选 A：主服务拉起网关）。

这组用例只覆盖**决策**部分（该不该起、命令怎么拼、已在跑就不重复起），不真的拉起一个网关进程
——那属于进程冒烟，跑测试时不该在用户机器上留进程。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

from local_agent.config import AppConfig, MobileConfig, WebConfig
from local_agent.mobile.supervisor import GATEWAY_FLAG, gateway_argv, start_gateway


def _config(tmp_path: Path, *, enabled: bool, gateway_port: int = 8732) -> AppConfig:
    return AppConfig(
        data_root=str(tmp_path),
        web=WebConfig(host="127.0.0.1", port=8731),
        mobile=MobileConfig(enabled=enabled, gateway_port=gateway_port),
    )


def _free_port() -> tuple[socket.socket, int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, int(server.getsockname()[1])


def test_disabled_config_never_starts_a_process(tmp_path: Path) -> None:
    """`mobile.enabled=false` 时连进程都不碰——这是"这一节不存在时逐字节一致"的落地。"""
    assert (
        start_gateway(
            _config(tmp_path, enabled=False),
            config_path=tmp_path / "config.yaml",
            data_root=tmp_path,
        )
        is None
    )


def test_already_running_gateway_is_reused_not_duplicated(tmp_path: Path) -> None:
    """端口已在监听就沿用现有进程：再起一个会撞端口，而用户看到的是"启动失败"。"""
    server, port = _free_port()
    try:
        assert (
            start_gateway(
                _config(tmp_path, enabled=True, gateway_port=port),
                config_path=tmp_path / "config.yaml",
                data_root=tmp_path,
            )
            is None
        )
    finally:
        server.close()


def test_source_mode_argv_uses_the_module_entry(tmp_path: Path) -> None:
    """源码形态用 `-m local_agent.mobile`；打包形态没有 `-m`，改用入口参数。"""
    config_path = tmp_path / "config.yaml"
    argv = gateway_argv(config_path)
    assert argv[0] == sys.executable
    if getattr(sys, "frozen", False):  # pragma: no cover - 测试不会以冻结形态跑
        assert argv[1] == GATEWAY_FLAG
    else:
        assert argv[1:3] == ["-m", "local_agent.mobile"]
    assert argv[-2:] == ["--config", str(config_path)]

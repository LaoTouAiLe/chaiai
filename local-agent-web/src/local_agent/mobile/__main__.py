"""网关入口：``python -m local_agent.mobile``（架构 §24）。

**为什么是独立入口而不是主服务的一个子命令**：§24 要求"关掉它主服务照旧跑"。挂在主服务的
lifespan 上做不到这一点——进程一死两个一起死，而远程入口恰恰是本机服务里风险最高、最需要
单独关掉的一块。

三个动作：
- 默认：启动网关（回环监听 + 各启用的通道监听，同一进程、同一个应用）；
- ``--pair``：签发一个配对码并打印链接后退出（方便脚本与"我现在就想配一台手机"）；
- ``--addresses``：打印本机局域网地址与各通道可用地址后退出（排查手机连不上）。

``mobile.enabled`` 为 false 时**直接拒绝启动**，而不是"起来但不干活"：一个什么都不做的进程
会让人以为功能开着。
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp

from local_agent import app_paths
from local_agent import config as config_module
from local_agent.config import AppConfig, ConfigError
from local_agent.mobile.channels import (
    ChannelNotReady,
    ExternalChannel,
    Listener,
    build_external_channels,
    build_listeners,
    lan_addresses,
    tailscale_dns_name,
)
from local_agent.mobile.gateway import create_gateway
from local_agent.mobile.pairing import PairingError, PairingStore
from local_agent.mobile.proxy import ADMIN_PREFIX, build_client
from local_agent.mobile.qr import terminal_qr
from local_agent.mobile.settings import effective_mobile
from local_agent.mobile.supervisor import is_listening

#: 网关状态目录名（``data_root/mobile``）。配对记录、设备授权与证书都在这下面。
STATE_DIR_NAME = "mobile"

#: 外部通道命令的超时（秒）。`tailscale funnel --bg` 首次可能要去管理台确认，给足时间。
EXTERNAL_COMMAND_TIMEOUT: float = 20.0


def _state_dir(config: AppConfig) -> Path:
    return app_paths.data_root_of(config) / STATE_DIR_NAME


def _gateway_address(config: AppConfig) -> str:
    return f"http://{config.mobile.gateway_host}:{config.mobile.gateway_port}"


def _run_command(argv: tuple[str, ...]) -> str | None:
    """执行一条外部命令；成功返回 ``None``，失败返回**可直接展示**的原因。

    **超时也要把已经打印出来的内容带回来**（2026-11-04 修）。`tailscale funnel` 在 Funnel
    未启用时会**挂住等待授权**，而它挂住之前已经打印了最关键的一句：

        Funnel is not enabled on your tailnet.
        To enable, visit: https://login.tailscale.com/f/funnel?node=...

    第一版直接返回 ``str(TimeoutExpired)``，把这段吞掉了，日志里只剩\"启动失败：timed out\"——
    用户完全不知道该去点什么。超时时 ``TimeoutExpired.stdout`` 抓着已读到的部分，用它。
    """
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=EXTERNAL_COMMAND_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or exc.stderr or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        detail = str(partial).strip()[:400]
        if detail:
            return f"命令 {EXTERNAL_COMMAND_TIMEOUT:.0f} 秒未返回，它说：{detail}"
        return f"命令 {EXTERNAL_COMMAND_TIMEOUT:.0f} 秒未返回（没有任何输出）"
    except OSError as exc:
        return str(exc)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:400]
        return detail or f"退出码 {completed.returncode}"
    return None


async def _wait_until_listening(host: str, port: int, timeout: float = 10.0) -> bool:
    """等网关自己的监听就绪——外部通道要指向它，抢在前面执行会拿到"连接被拒"。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_listening(host, port):
            return True
        await asyncio.sleep(0.2)
    return False


async def _serve(
    app: ASGIApp,
    config: AppConfig,
    listeners: list[Listener],
    external: list[ExternalChannel],
    close_client: Callable[[], Awaitable[None]],
) -> None:
    """同时跑回环监听、各通道监听，以及"靠外部命令暴露"的通道。

    第二个及之后的监听用 ``lifespan="off"``：lifespan 会按监听各跑一次，而 app.state 是同一份
    ——重复初始化/重复关闭最终表现为"另一条通道上的请求突然全部 503"。

    外部通道（tailscale）必须**等网关监听就绪后再起**，而且退出时要 ``reset``：它的配置写在
    tailscaled 自己那儿，不 reset 就是"服务关掉了、公网地址还活着"。
    """
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.mobile.gateway_host,
                port=config.mobile.gateway_port,
                workers=1,
                log_level="info",
            )
        )
    ]
    for item in listeners:
        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=item.host,
                    port=item.port,
                    workers=1,
                    log_level="info",
                    lifespan="off",
                    ssl_certfile=None if item.ssl_certfile is None else str(item.ssl_certfile),
                    ssl_keyfile=None if item.ssl_keyfile is None else str(item.ssl_keyfile),
                )
            )
        )
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    started: list[ExternalChannel] = []
    try:
        if external:
            await _wait_until_listening(
                config.mobile.gateway_host, config.mobile.gateway_port
            )
        for channel in external:
            note = _run_command(channel.start_argv)
            if note is None:
                started.append(channel)
                print(f"[{channel.name}] 已对外暴露：" + "  ".join(channel.urls), flush=True)
            else:
                # 远程通道失败不该让网关整体退出：局域网那条多半还能用。
                print(f"[{channel.name}] 启动失败：{note}", flush=True)
        await asyncio.gather(*tasks)
    finally:
        for channel in reversed(started):
            note = _run_command(channel.stop_argv)
            if note is not None:
                print(f"[{channel.name}] 关闭时未能 reset：{note}", flush=True)
        await close_client()


def main(argv: Sequence[str] | None = None) -> int:
    """启动网关，或签发配对码 / 打印地址；返回进程退出码。"""
    parser = argparse.ArgumentParser(
        prog="python -m local_agent.mobile",
        description="chaiai 移动访问网关（独立进程，默认只监听回环）",
    )
    parser.add_argument(
        "--config",
        default=str(app_paths.default_config_path()),
        help="配置文件路径（与主服务同一份；读 mobile 段、web 段）",
    )
    parser.add_argument("--env", default=None, help=".env 路径（默认与 --config 同目录）")
    parser.add_argument("--pair", action="store_true", help="签发一个配对码并打印链接，不启动服务")
    parser.add_argument("--addresses", action="store_true", help="打印本机地址与通道地址，不启动服务")
    parser.add_argument(
        "--channel",
        default="lan",
        help="配对码所属通道（默认 lan，只在审计与状态页上有区分意义）",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    try:
        config_module.load_dotenv_file(
            config_path.with_name(".env") if args.env is None else Path(args.env)
        )
        app_config = config_module.load_config(config_path, allow_missing=True)
    except ConfigError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    # 设置库里的覆盖值由主服务写成快照；网关只认快照，不碰 sqlite（§24 的边界）。
    # **必须先合并快照再判 enabled**（2026-11-05）：设置页打开的移动访问只写设置库与快照、
    # 不写 config.yaml，按出厂默认先判会把这条主路径直接拒掉——用户点了"启动"，
    # 子进程却以退出码 1 结束，日志里只有一句"未启用"。
    effective = effective_mobile(app_config)
    if effective != app_config.mobile:
        app_config = app_config.model_copy(update={"mobile": effective})

    if not app_config.mobile.enabled:
        print(
            "移动访问未启用：在设置页打开移动访问，或在配置里设置 mobile.enabled: true "
            "之后再启动网关（默认关闭，关闭时不会启动任何进程）。",
            file=sys.stderr,
        )
        return 1

    state_dir = _state_dir(app_config)

    if args.addresses:
        addresses = lan_addresses()
        if not addresses:
            print("没有检测到局域网地址：确认这台机器已连上 Wi-Fi 或网线。")
        for address in addresses:
            print(f"  局域网地址：{address}")
        return 0

    if args.pair:
        store = PairingStore(state_dir)
        try:
            code = store.issue_code(args.channel, float(app_config.mobile.pairing_ttl_seconds))
        except PairingError as exc:
            print(f"签发配对码失败：{exc}", file=sys.stderr)
            return 1
        address = _gateway_address(app_config)
        print(f"配对链接（{round(code.remaining())} 秒内有效，只能用一次）：")
        print(f"  本机直连：{address}{ADMIN_PREFIX}/pair?code={code.code}")
        # 远程通道优先：它换网络也能用（理由见 channels.phone_base_url 的排序说明）。
        phone_url = ""
        if any(item.type == "tailscale" and item.enabled for item in app_config.mobile.channels):
            try:
                ts_url = f"https://{tailscale_dns_name()}{ADMIN_PREFIX}/pair?code={code.code}"
                print(f"  手机（远程）：{ts_url}")
                phone_url = ts_url
            except ChannelNotReady as exc:
                print(f"  （tailscale 暂不可用：{exc}）")
        lan = next(
            (item for item in app_config.mobile.channels if item.type == "lan" and item.enabled),
            None,
        )
        if lan is not None:
            addresses = lan_addresses()
            scheme = "https" if lan.tls else "http"
            for address in addresses:
                url = f"{scheme}://{address}:{lan.port}{ADMIN_PREFIX}/pair?code={code.code}"
                print(f"  手机（局域网）：{url}")
                if not phone_url:
                    # 只给第一个出二维码：它来自路由探测，是手机最可能连上的那个；列出来的其余
                    # 地址多半是虚拟网卡（VMware / VPN），扫了只会打不开。
                    phone_url = url
            if not addresses:
                print("  （没检测到局域网地址，手机暂时连不上；先跑一次 --addresses 看看）")
        if phone_url:
            print("\n用手机相机扫下面这个：\n")
            print(terminal_qr(phone_url))
        return 0

    try:
        listeners = build_listeners(app_config, state_dir)
    except ChannelNotReady as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    # 外部通道：起不来只跳过（内部已打印原因），不阻塞局域网。
    external = build_external_channels(app_config)

    client = build_client()
    app = create_gateway(app_config, state_dir=state_dir, client=client)

    async def _close() -> None:
        if not client.is_closed:
            await client.aclose()

    web = app_config.web
    lines = [
        f"chaiai 移动网关已启动：{_gateway_address(app_config)}",
        f"上游：http://{web.host}:{web.port}",
        f"状态目录：{state_dir}",
        "生成配对链接：python -m local_agent.mobile --pair",
    ]
    for item in listeners:
        if item.urls:
            lines.append(f"[{item.name}] " + "  ".join(item.urls))
        else:
            lines.append(
                f"[{item.name}] 已监听 0.0.0.0:{item.port}，但没检测到局域网地址"
                "（手机可能连不上，先用 --addresses 确认）"
            )
        if item.ssl_certfile is not None:
            lines.append(f"  自签 CA：{item.ssl_certfile.with_name('ca.pem')}（装进手机可免每次警告）")
    for channel in external:
        lines.append(
            f"[{channel.name}] " + "  ".join(channel.urls) + "（服务就绪后对外暴露）"
        )
    lines.append("注意：本阶段只转发 HTTP 与 SSE，终端的 WebSocket 尚未接入（见架构 §24）。")
    print("\n".join(lines), flush=True)

    try:
        asyncio.run(_serve(app, app_config, listeners, external, _close))
    except KeyboardInterrupt:  # pragma: no cover - 交互式退出
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 `python -m` 触发
    raise SystemExit(main())

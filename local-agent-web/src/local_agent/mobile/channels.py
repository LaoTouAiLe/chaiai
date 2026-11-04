"""通道：把网关暴露到某个网络面（架构 §24）。

**通道不参与认证**：它只负责"把网关的回环端口暴露到某个网络面"——多开一个监听，或让外部
命令反代。认证在网关内部，走哪条通道都一样——这条分界让"加一条通道"永远不会顺手放宽一道校验。

本版实现两条：``lan`` 是"本进程多开一个监听"（见 :func:`build_listeners`），``tailscale``
是"让外部命令反代到网关回环端口"（见 :class:`ExternalChannel`）。二者的差别只在怎么暴露，
认证仍在网关内。``cloudflared`` 等其余通道在配置里写了且 ``enabled: true`` 时
**明确报错并拒绝启动**，不静默忽略：一个开了却什么也不做的通道，比一条报错更难排查。
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from local_agent.config import AppConfig, MobileChannelConfig
from local_agent.mobile.certs import ensure_certificates

#: 本版已实现的通道。与 ``config.SUPPORTED_MOBILE_CHANNELS`` 的差集就是"能写但还不能用"。
IMPLEMENTED_CHANNELS: Final[tuple[str, ...]] = ("lan", "tailscale")

#: ``tailscale`` 可执行文件的常见位置。`tailscaled` 装好后 CLI 不一定在 PATH 里
#: （Windows 安装器默认只把目录给系统 PATH，某些终端会话里看不到）。
_TAILSCALE_CANDIDATES: Final[tuple[str, ...]] = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
)

#: 调 tailscale CLI 的超时（秒）。`status` 是本地查询，正常几十毫秒。
_TAILSCALE_TIMEOUT: Final[float] = 5.0

#: 用于探测本机出口地址的目标。**UDP connect 不发包**，只是让内核做一次路由选择。
_PROBE_TARGETS: Final[tuple[str, ...]] = ("8.8.8.8", "1.1.1.1")


class ChannelNotReady(RuntimeError):
    """配置里启用了尚未实现的通道；消息可直接展示给用户。"""


@dataclass(frozen=True, slots=True)
class Listener:
    """一个额外的监听面。

    ``host`` 通常是 ``0.0.0.0``（局域网所有网卡），``urls`` 是**给人看**的地址列表——
    它们之间不是一一对应的关系，别拿 ``urls[0]`` 去 bind。
    """

    name: str
    host: str
    port: int
    urls: tuple[str, ...]
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None


@dataclass(frozen=True, slots=True)
class ExternalChannel:
    """靠**外部命令**暴露的通道（tailscale 是第一个）。

    与 :class:`Listener` 的区别：那种是"本进程多开一个监听"，这种是"让外部进程反代到网关的
    回环端口"。所以它的生命周期是**命令**而不是 socket：启动时执行 ``start_argv``，退出时执行
    ``stop_argv``。tailscale 的配置会写进 tailscaled 自己的状态，**不 reset 就会一直生效**——
    那正是"服务关掉了但公网地址还活着"这类问题的来源。
    """

    name: str
    start_argv: tuple[str, ...]
    stop_argv: tuple[str, ...]
    urls: tuple[str, ...]


def tailscale_executable() -> str:
    """``tailscale`` 命令的绝对路径。

    Raises:
        ChannelNotReady: 没装或不在 PATH 里——消息里直接给出 Windows 的默认安装路径。
    """
    for candidate in _TAILSCALE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("tailscale")
    if found:
        return found
    raise ChannelNotReady(
        "找不到 tailscale 命令：先安装 Tailscale 并登录"
        r"（Windows 默认在 C:\Program Files\Tailscale\tailscale.exe）"
    )


def tailscale_dns_name(executable: str | None = None) -> str:
    """当前节点的 MagicDNS 名称（``xxx.ts.net``，不带尾点）。

    Raises:
        ChannelNotReady: 没登录、没开 MagicDNS、或 CLI 调不通。**不返回空串**——空串会让调用方
            生成一个 ``https://:443`` 这种怎么看都不像错的地址。
    """
    exe = executable or tailscale_executable()
    try:
        completed = subprocess.run(
            [exe, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_TAILSCALE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChannelNotReady(f"调不通 tailscale：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:200]
        raise ChannelNotReady(f"tailscale status 失败：{detail or '没有输出'}")
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ChannelNotReady("tailscale status 的输出不是 JSON，版本可能不兼容") from exc
    state = str(data.get("BackendState", ""))
    if state != "Running":
        raise ChannelNotReady(
            f"Tailscale 还没登录或没连上（BackendState={state or '未知'}）："
            "先执行 tailscale up 完成登录"
        )
    dns = str((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not dns:
        raise ChannelNotReady("Tailscale 没有给出 MagicDNS 名称：在管理台启用 MagicDNS 后重试")
    return dns


def _tailscale_channel(channel: MobileChannelConfig, config: AppConfig) -> ExternalChannel:
    exe = tailscale_executable()
    dns = tailscale_dns_name(exe)
    target = f"http://{config.mobile.gateway_host}:{config.mobile.gateway_port}"
    verb = "funnel" if channel.expose == "funnel" else "serve"
    # `--bg` 让配置在 CLI 退出后继续生效；`--https=<port>` 用 Tailscale 自己签的证书，
    # 所以这条通道不需要我们生成任何证书，也不需要手机点"仍要继续访问"。
    # 端口可配（443/8443/10000）：443 被占用时换一个，而不是让整条通道起不来。
    return ExternalChannel(
        name="tailscale",
        start_argv=(exe, verb, "--bg", f"--https={channel.https_port}", target),
        stop_argv=(exe, verb, "reset"),
        urls=(f"https://{dns}" if channel.https_port == 443 else f"https://{dns}:{channel.https_port}",),
    )


def build_external_channels(config: AppConfig) -> list[ExternalChannel]:
    """构造"靠外部命令暴露"的通道。

    **不满足前置条件时跳过并打印原因，不抛异常**：远程通道起不来不该把局域网通道一起拖垮
    ——用户最需要的是"至少在家里还能用"，而不是"一个都没起来"。
    """
    channels: list[ExternalChannel] = []
    for channel in config.mobile.channels:
        if not channel.enabled or channel.type != "tailscale":
            continue
        try:
            channels.append(_tailscale_channel(channel, config))
        except ChannelNotReady as exc:
            print(f"[tailscale] 跳过这条通道：{exc}", flush=True)
    return channels


def lan_addresses() -> list[str]:
    """本机局域网 IPv4 地址（排除回环、链路本地、未指定与非私网）。

    两条来源合起来用：主机名解析能拿到多网卡地址，UDP 路由探测能拿到"默认出口"地址。只靠
    前者在少数 Windows 配置下会返回空（主机名没有映射），只靠后者拿不到第二块网卡。

    **顺序有意义**：路由探测出来的地址排在最前。装了 VMware / 虚拟网卡 / VPN 的机器上，枚举
    出来的一半以上是连不出去的虚拟网卡（实测一台机器 7 个地址里只有 1 个是 Wi-Fi），而"默认
    出口"几乎一定是手机能连上的那个。调用方（例如打印配对二维码）只取第一个。
    """
    probed: list[str] = []
    for target in _PROBE_TARGETS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.settimeout(0.2)
                probe.connect((target, 53))
                text = str(probe.getsockname()[0])
                if text not in probed:
                    probed.append(text)
        except OSError:
            continue

    enumerated: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            enumerated.add(str(info[4][0]))
    except OSError:
        pass

    def _usable(text: str) -> bool:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:  # pragma: no cover - 两个来源只给合法地址
            return False
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            return False
        return address.is_private

    ordered = [item for item in probed if _usable(item)]
    ordered += sorted(item for item in enumerated if _usable(item) and item not in ordered)
    return ordered


def _lan_listener(channel: MobileChannelConfig, state_dir: Path) -> Listener:
    addresses = lan_addresses()
    scheme = "https" if channel.tls else "http"
    certificates = (
        ensure_certificates(state_dir / "certs", addresses) if channel.tls else None
    )
    return Listener(
        name="lan",
        host="0.0.0.0",
        port=channel.port,
        urls=tuple(f"{scheme}://{item}:{channel.port}" for item in addresses),
        ssl_certfile=None if certificates is None else certificates.server_cert,
        ssl_keyfile=None if certificates is None else certificates.server_key,
    )


def phone_base_urls(config: AppConfig) -> list[tuple[str, str]]:
    """手机能连上的基地址，**按优先级**排列（远程在前、局域网在后）。

    **为什么返回一组而不是一个**：某个通道"配了但没真正生效"是真实会发生的一态——比如 Tailscale
    已登录（能拿到 DNS 名），但 Funnel 没在管理台开启，网关启动时那条通道会失败并打印原因，而
    ``phone_base_urls`` 无从得知（它只看配置与环境）。只给一个地址时，用户看到的是"二维码扫了
    打不开"，而且局域网明明是好的。给一组，第一个坏了还有退路。

    **远程优先**（不看配置里的书写顺序）：tailscale 换网络也能用，而局域网地址只在同一 WiFi 下有效。
    """
    urls: list[tuple[str, str]] = []
    enabled = [item for item in config.mobile.channels if item.enabled]
    for channel in enabled:
        if channel.type != "tailscale":
            continue
        try:
            dns = tailscale_dns_name()
        except ChannelNotReady:
            # 没登录/没开 MagicDNS：不是错误，只是这条通道现在不可用。
            continue
        suffix = "" if channel.https_port == 443 else f":{channel.https_port}"
        urls.append(("远程（Tailscale）", f"https://{dns}{suffix}"))
    for channel in enabled:
        if channel.type != "lan":
            continue
        addresses = lan_addresses()
        if not addresses:
            continue
        scheme = "https" if channel.tls else "http"
        for index, address in enumerate(addresses):
            # 只有第一个（路由探测得到的默认出口）配叫"同一 Wi-Fi"；其余多半是 VMware / VPN
            # 这类虚拟网卡，标成"同一 Wi-Fi"会让用户扫了打不开还以为是网络问题。
            label = "同一 Wi-Fi" if index == 0 else f"其他网卡（{address}）"
            urls.append((label, f"{scheme}://{address}:{channel.port}"))
    return urls


def phone_base_url(config: AppConfig) -> str | None:
    """首选的手机可达地址；没有可用通道时返回 ``None``。

    **为什么不直接用网关地址**：网关只听回环，``http://127.0.0.1:8732`` 在手机上是它自己。
    """
    urls = phone_base_urls(config)
    return urls[0][1] if urls else None


def build_listeners(config: AppConfig, state_dir: Path) -> list[Listener]:
    """按配置构造额外监听（不含网关自己的回环监听）。

    Raises:
        ChannelNotReady: 配置启用了本版还没实现的通道。
    """
    listeners: list[Listener] = []
    for channel in config.mobile.channels:
        if not channel.enabled:
            continue
        if channel.type == "tailscale":
            # 外部命令型通道，不在这里起监听，见 build_external_channels。
            continue
        if channel.type != "lan":
            raise ChannelNotReady(
                f"通道 {channel.type!r} 本版还没有实现（已支持："
                f"{' / '.join(IMPLEMENTED_CHANNELS)}）。"
                "把它改成 enabled: false 再启动，或者先开局域网通道。"
            )
        listeners.append(_lan_listener(channel, state_dir))
    return listeners


__all__ = [
    "IMPLEMENTED_CHANNELS",
    "ChannelNotReady",
    "Listener",
    "build_listeners",
    "lan_addresses",
    "phone_base_url",
    "phone_base_urls",
]

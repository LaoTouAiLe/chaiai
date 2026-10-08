"""启动后在本机默认浏览器里打开页面（宿主能力，不是应用服务用例）。

**定位**与 :mod:`local_agent.directory_picker` 一致：回答"宿主进程能不能做某件事"，不碰业务。
消费者是入口 ``python -m local_agent.api``；冻结成试用包后，普通人双击 exe 的唯一期望就是
"页面自己出来"，而不是先看到一段地址再去复制。

**为什么在后台线程里等端口**：``uvicorn.run`` 会阻塞主线程，而"浏览器先于服务就绪去访问"会
得到一个连接失败页面——那正是用户对产品的第一印象。所以先起一个守护线程轮询目标端口，端口通了
再调 ``webbrowser.open``。轮询有上限（默认 20 秒），超时只如实提示并给出地址，不假装打开过。

**为什么通配地址要换成回环地址**：``web.host = 0.0.0.0`` 这类监听地址不是"能访问的地址"，
浏览器打开 ``http://0.0.0.0:8000`` 的行为随平台而异。:func:`page_target` 统一换成 ``127.0.0.1``。

**为什么留一个开关**：自动化（测试、脚本）与"只想看日志"的场景不该被弹一个浏览器；入口因此有
``--no-open-browser``。默认是打开——试用包要的就是这个默认。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser
from typing import Final

#: 等待服务就绪的上限（秒）。冷启动 + 一次 SQLite 迁移在这台机器上是秒级，20 秒足够宽松。
DEFAULT_WAIT_SECONDS: Final[float] = 20.0

#: 轮询间隔（秒）：太密会空转，太疏会让页面晚出来。
POLL_INTERVAL_SECONDS: Final[float] = 0.25

#: 端口通了之后再等一下：让 uvicorn 完成 startup 事件与静态托管注册。
OPEN_DELAY_SECONDS: Final[float] = 0.3

#: 单个 TCP 连接探测的超时（秒）。
CONNECT_TIMEOUT_SECONDS: Final[float] = 0.5

#: 这些监听地址不是"能访问的地址"，一律换回环。
WILDCARD_HOSTS: Final[frozenset[str]] = frozenset({"0.0.0.0", "::", "*", ""})


def page_target(host: str, port: int) -> tuple[str, str]:
    """把监听地址换算成浏览器目标：返回（探测用主机, 页面地址）。"""
    probe_host = "127.0.0.1" if host in WILDCARD_HOSTS else host
    return probe_host, f"http://{probe_host}:{port}"


def port_is_open(host: str, port: int) -> bool:
    """能否与 ``host:port`` 建立 TCP 连接（只用于判断"服务起来了没有"）。"""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def open_when_ready(
    url: str,
    *,
    host: str,
    port: int,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
) -> threading.Thread:
    """起一个守护线程：等端口可连后在默认浏览器打开 ``url``；失败只提示，不抛异常。

    Returns:
        已启动的线程（调用方一般不需要它；测试与诊断用 ``join`` 等待结果）。
    """

    def worker() -> None:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if port_is_open(host, port):
                time.sleep(OPEN_DELAY_SECONDS)
                _open(url)
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        print(f"未能确认服务已就绪，请手动打开：{url}", file=sys.stderr)

    thread = threading.Thread(target=worker, name="browser-open", daemon=True)
    thread.start()
    return thread


def _open(url: str) -> None:
    """调用系统默认浏览器；打不开时给出可照做的提示（服务本身不受影响）。"""
    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - 宿主环境千奇百怪，一律如实上报
        print(f"打开浏览器失败（{type(exc).__name__}）：请手动打开 {url}", file=sys.stderr)
        return
    if not opened:
        print(f"没有找到可用的默认浏览器，请手动打开：{url}", file=sys.stderr)

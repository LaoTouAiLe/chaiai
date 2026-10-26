"""打包形态的桌面窗口：把本机页面装进 WebView2 窗口，而不是弹一个外部浏览器。

**为什么要单独一层**：``uvicorn.run`` 会阻塞主线程，而 Windows 的 GUI 必须跑在主线程。所以这里
反过来——服务进后台线程、窗口占主线程；关窗即停服。和 :mod:`local_agent.browser_open` 是同一个
位置的两种宿主能力实现，入口按"有没有窗口依赖"二选一。

**拿不到窗口时怎么办**：如实降级。WebView2 运行时缺失、``pywebview`` 导入失败、窗口创建抛错，
都退回浏览器那条路并打印原因——"打不开界面"比"界面变浏览器"难解释得多。

**不改的东西**：服务仍然是单进程单 worker（§15.1），前端仍然是同一份产物；本模块只决定
"这页面显示在哪"。``--no-open-browser`` 的语义仍是"不显示任何界面"。
"""

from __future__ import annotations

import sys
import threading
from typing import Final

import uvicorn

from local_agent import browser_open

#: 窗口标题与初始尺寸。宽度取 1280：对话列上限 960，加上两侧留白刚好不出现横向挤压。
WINDOW_TITLE: Final[str] = "chaiai"
WINDOW_WIDTH: Final[int] = 1280
WINDOW_HEIGHT: Final[int] = 860
WINDOW_MIN_SIZE: Final[tuple[int, int]] = (960, 640)

#: 等端口就绪的上限（秒），与浏览器那条路同一个口径。
READY_TIMEOUT_SECONDS: Final[float] = 20.0

#: 关窗后等 HTTP 线程退出的上限（秒）。
SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 10.0


def window_available() -> bool:
    """当前环境能不能开窗口：只判断依赖是否可导入，不在这里创建任何窗口。

    WebView2 运行时缺失时的报错发生在**创建窗口**那一刻，而不是导入时，所以这里只挡
    "pywebview 根本装不上"这一种情况；另一种交给 :func:`run` 里的兜底。
    """
    try:
        import webview  # noqa: F401
    except Exception:  # noqa: BLE001 - 任何导入失败都等价于"没有窗口能力"
        return False
    return True


def run(
    app: object,
    *,
    host: str,
    port: int,
    keepalive_seconds: int,
    title: str = WINDOW_TITLE,
    width: int = WINDOW_WIDTH,
    height: int = WINDOW_HEIGHT,
) -> int:
    """起服务 + 开窗口，阻塞到用户关窗。

    Returns:
        0 = 正常关闭；1 = 服务没起来（窗口不会开）。
    """
    import webview

    probe_host, page_url = browser_open.page_target(host, port)
    server = uvicorn.Server(
        uvicorn.Config(
            app,  # type: ignore[arg-type]  # uvicorn 接受任意 ASGI 应用
            host=host,
            port=port,
            workers=1,
            timeout_keep_alive=keepalive_seconds,
            log_level="info",
        )
    )
    thread = threading.Thread(target=server.run, name="chaiai-http", daemon=True)
    thread.start()

    if not _wait_until_ready(probe_host, port):
        print(f"服务在 {READY_TIMEOUT_SECONDS:.0f} 秒内没有就绪，窗口不打开。", file=sys.stderr)
        server.should_exit = True
        thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return 1

    print(f"正在打开窗口：{page_url}", flush=True)
    # 冻结形态把黑色控制台藏起来：双击 exe 的人期望看到的是"一个程序"，不是一个命令行窗口。
    # 源码形态**不藏**——那个控制台就是开发者自己的终端。
    console = _hide_console()
    try:
        webview.create_window(
            title,
            page_url,
            width=width,
            height=height,
            min_size=WINDOW_MIN_SIZE,
        )
        # 阻塞到窗口关闭。`webview.start` 必须在主线程调用（Windows 的 GUI 约束）。
        webview.start()
    except BaseException:
        # 窗口起不来就把控制台还回去：否则用户面对的是"双击后什么都没有"，
        # 而这正是本项目一直在避免的那种失败形态。
        _show_console(console)
        raise
    finally:
        server.should_exit = True
        thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    return 0


def _hide_console() -> int | None:
    """冻结形态隐藏控制台窗口，返回句柄供失败时还原；源码形态原样返回 ``None``。"""
    if not getattr(sys, "frozen", False):
        return None
    try:
        import ctypes

        handle = int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
        if handle:
            ctypes.windll.user32.ShowWindow(handle, 0)  # SW_HIDE
            return handle
    except Exception:  # noqa: BLE001 - 藏不掉就算了，不影响功能
        return None
    return None


def _show_console(handle: int | None) -> None:
    if handle is None:
        return
    try:
        import ctypes

        ctypes.windll.user32.ShowWindow(handle, 5)  # SW_SHOW
    except Exception:  # noqa: BLE001, S110 - 显示控制台是纯装饰动作
        # 没有控制台句柄、或 API 调用被策略拦下时会失败。它不承载任何功能，
        # 失败也不影响窗口与页面，所以刻意静默；记录日志只会给正常启动添噪。
        pass


def _wait_until_ready(host: str, port: int) -> bool:
    """轮询到端口可连为止；超时返回 False（不抛错，交给调用方决定怎么退）。"""
    import time

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if browser_open.port_is_open(host, port):
            # 端口通了再等一小会儿，让 startup 事件与静态托管注册完成。
            time.sleep(browser_open.OPEN_DELAY_SECONDS)
            return True
        time.sleep(browser_open.POLL_INTERVAL_SECONDS)
    return False

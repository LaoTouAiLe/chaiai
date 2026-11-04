"""桌面窗口的控制台时序（2026-10-27）。

那个黑框是 PyInstaller 的 console 子系统在 ``CreateProcess`` 那一刻创建的，代码只能事后
``ShowWindow(SW_HIDE)``。所以这一层只有两件事值得钉住：

1. **藏得早**：隐藏在等待服务就绪**之前**发生——藏在之后就等于让黑框覆盖整个启动过程。
2. **失败还回来**：服务没起来或窗口创建失败时，控制台必须先恢复再报错，否则用户面对的是
   "双击后什么都没有"，而这正是打包形态最怕的失败形态。

这些用例**不绑端口、不开窗口**：``uvicorn`` 与 ``webview`` 都是替身。
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from local_agent import desktop_window


class _FakeServer:
    def __init__(self) -> None:
        self.should_exit = False

    def run(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """不真的绑端口：Server 换成 no-op，Config 只要可调用。"""
    module = types.SimpleNamespace(
        Server=lambda *args, **kwargs: _FakeServer(),
        Config=lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(desktop_window, "uvicorn", module)


@pytest.fixture()
def fake_webview(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = types.ModuleType("webview")
    module.create_window = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    module.start = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", module)
    return module


def _run() -> int:
    return desktop_window.run(object(), host="127.0.0.1", port=8000, keepalive_seconds=5)


def test_console_is_hidden_before_waiting_for_the_service(
    fake_webview: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """顺序断言：先 hide 再等就绪。反过来就是"黑框陪着整个启动过程"。"""
    order: list[str] = []
    monkeypatch.setattr(desktop_window, "_hide_console", lambda: order.append("hide") or 42)
    monkeypatch.setattr(desktop_window, "_show_console", lambda handle: None)

    def wait(host: str, port: int) -> bool:
        order.append("wait")
        return False

    monkeypatch.setattr(desktop_window, "_wait_until_ready", wait)

    assert _run() == 1
    assert order == ["hide", "wait"]


def test_failed_startup_restores_the_console(
    fake_webview: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务没起来：控制台要回来，否则"端口被占用"这句话写进一个看不见的窗口。"""
    shown: list[int | None] = []
    monkeypatch.setattr(desktop_window, "_hide_console", lambda: 42)
    monkeypatch.setattr(desktop_window, "_show_console", shown.append)
    monkeypatch.setattr(desktop_window, "_wait_until_ready", lambda host, port: False)

    assert _run() == 1
    assert shown == [42]


def test_window_failure_restores_the_console(
    fake_webview: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """窗口起不来（WebView2 缺失等）：先还控制台，再把异常抛给调用方。"""
    shown: list[int | None] = []
    monkeypatch.setattr(desktop_window, "_hide_console", lambda: 42)
    monkeypatch.setattr(desktop_window, "_show_console", shown.append)
    monkeypatch.setattr(desktop_window, "_wait_until_ready", lambda host, port: True)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("WebView2 缺失")

    monkeypatch.setattr(fake_webview, "create_window", boom)

    with pytest.raises(RuntimeError):
        _run()
    assert shown == [42]


def test_hiding_is_a_no_op_outside_a_frozen_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """源码形态不碰控制台：那是开发者自己的终端。"""
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert desktop_window._hide_console() is None


def test_window_shrinks_into_the_work_area(monkeypatch: pytest.MonkeyPatch) -> None:
    """125% 缩放下 1280×860 变 1600×1075、底边出屏（2026-11-03 实测）：高度要收回工作区。"""
    monkeypatch.setattr(desktop_window, "_work_area", lambda: (1536, 824))

    width, height, x, y = desktop_window._fit_to_work_area(1280, 860)

    assert width == 1280                      # 宽度本来就装得下，保持设计值
    assert height == int(824 * desktop_window.SCREEN_USAGE)
    assert height < 860
    assert (x, y) == ((1536 - 1280) // 2, (824 - height) // 2)
    assert x is not None and y is not None and y >= 0


def test_window_geometry_falls_back_without_work_area(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到工作区：按原尺寸、不指定位置，而不是不开窗。"""
    monkeypatch.setattr(desktop_window, "_work_area", lambda: None)

    assert desktop_window._fit_to_work_area(1280, 860) == (1280, 860, None, None)


def test_window_uses_the_fitted_geometry(fake_webview: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(desktop_window, "_wait_until_ready", lambda host, port: True)
    monkeypatch.setattr(desktop_window, "_fit_to_work_area", lambda width, height: (1200, 800, 30, 40))
    monkeypatch.setattr(fake_webview, "create_window",
                        lambda *args, **kwargs: captured.update(kwargs))

    assert _run() == 0
    assert (captured["width"], captured["height"]) == (1200, 800)
    assert (captured["x"], captured["y"]) == (30, 40)


def test_webview_storage_survives_restarts(
    fake_webview: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """私密模式不保留 localStorage：右栏标签、工作面板偏好、已关闭审批卡会每次丢。"""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(desktop_window, "_wait_until_ready", lambda host, port: True)
    monkeypatch.setattr(fake_webview, "start", lambda *args, **kwargs: captured.update(kwargs))
    storage = tmp_path / "webview"

    assert desktop_window.run(object(), host="127.0.0.1", port=8000, keepalive_seconds=5,
                              storage_dir=storage) == 0
    assert captured["private_mode"] is False
    assert captured["storage_path"] == str(storage)
    assert storage.is_dir()

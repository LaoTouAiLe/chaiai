"""启动后打开浏览器这条路径的回归（T40）。

不真的弹浏览器：``webbrowser.open``、``port_is_open`` 与入口的调用点都只是普通可替换对象。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import uvicorn

from local_agent import app_paths, browser_open
from local_agent.api import __main__ as api_main


def test_page_target_replaces_wildcard_hosts() -> None:
    """通配监听地址不是能访问的地址：换成回环地址再交给浏览器。"""
    assert browser_open.page_target("0.0.0.0", 8000) == ("127.0.0.1", "http://127.0.0.1:8000")
    assert browser_open.page_target("::", 8000)[0] == "127.0.0.1"
    assert browser_open.page_target("127.0.0.1", 8123) == ("127.0.0.1", "http://127.0.0.1:8123")
    assert browser_open.page_target("localhost", 8123)[1] == "http://localhost:8123"


def test_open_when_ready_opens_once_the_port_is_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(browser_open, "port_is_open", lambda host, port: True)
    monkeypatch.setattr(browser_open, "webbrowser", type("W", (), {"open": staticmethod(opened.append)}))
    monkeypatch.setattr(browser_open, "OPEN_DELAY_SECONDS", 0.0)

    thread = browser_open.open_when_ready("http://127.0.0.1:8000", host="127.0.0.1", port=8000)
    thread.join(timeout=5)

    assert opened == ["http://127.0.0.1:8000"]


def test_open_when_ready_reports_a_service_that_never_binds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """服务一直没起来：如实提示并给出地址，不假装打开过。"""
    monkeypatch.setattr(browser_open, "port_is_open", lambda host, port: False)
    monkeypatch.setattr(browser_open, "POLL_INTERVAL_SECONDS", 0.0)

    browser_open.open_when_ready(
        "http://127.0.0.1:8000", host="127.0.0.1", port=8000, wait_seconds=0.0
    ).join(timeout=5)

    assert "请手动打开" in capsys.readouterr().err


def test_open_does_not_raise_when_the_host_has_no_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """宿主没有可用浏览器：只提示，不影响服务继续跑。"""

    def boom(url: str) -> bool:
        raise RuntimeError("no browser")

    monkeypatch.setattr(browser_open.webbrowser, "open", boom)
    browser_open._open("http://127.0.0.1:8000")

    assert "打开浏览器失败" in capsys.readouterr().err


def test_entry_point_opens_the_browser_unless_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """入口默认打开浏览器；`--no-open-browser` 明确关掉（自动化与只想看日志的场景）。"""
    called: list[tuple[str, str, int]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setattr(
        api_main.browser_open,
        "open_when_ready",
        lambda url, *, host, port: called.append((url, host, port)),
    )

    assert (
        api_main.main(["--config", str(tmp_path / "missing.yaml"), "--env", str(tmp_path / ".env")]) == 0
    )
    assert called == [("http://127.0.0.1:8000", "127.0.0.1", 8000)]

    called.clear()
    assert (
        api_main.main(
            [
                "--config",
                str(tmp_path / "missing.yaml"),
                "--env",
                str(tmp_path / ".env"),
                "--no-open-browser",
            ]
        )
        == 0
    )
    assert called == []


def test_frozen_entry_opens_the_desktop_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """打包形态**先开窗口**（2026-10-26 起），不再弹外部浏览器；数据目录口径不变。"""
    windows: list[tuple[str, int]] = []
    browsers: list[str] = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setattr(api_main.desktop_window, "window_available", lambda: True)
    monkeypatch.setattr(
        api_main.desktop_window,
        "run",
        lambda app, *, host, port, **kwargs: windows.append((host, port)) or 0,
    )
    monkeypatch.setattr(
        api_main.browser_open,
        "open_when_ready",
        lambda url, *, host, port: browsers.append(url),
    )

    assert api_main.main([]) == 0

    assert windows == [("127.0.0.1", 8000)]
    assert browsers == []
    assert (tmp_path / "chaiai" / "data" / "checkpoints.sqlite").is_file()
    assert app_paths.data_dir() == tmp_path / "chaiai"


def test_frozen_entry_falls_back_to_the_browser_without_window_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """没有窗口依赖（例如缺 WebView2）时如实降级到浏览器，不能"双击没反应"。"""
    browsers: list[str] = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setattr(api_main.desktop_window, "window_available", lambda: False)
    monkeypatch.setattr(
        api_main.browser_open,
        "open_when_ready",
        lambda url, *, host, port: browsers.append(url),
    )

    assert api_main.main([]) == 0

    assert browsers == ["http://127.0.0.1:8000"]

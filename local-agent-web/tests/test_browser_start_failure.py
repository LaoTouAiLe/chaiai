"""浏览器启动失败后不许连环重试（2026-11-06）。

真实事故：CDP 连接超时 → 模型改用命令行直接起 Edge → ``user-data-dir`` 拒绝访问 → Edge 崩溃
（``0x80000003``）→ 模型又换一种方式再试 → 桌面上连弹"应用程序错误"，一轮卡十几分钟。

两条契约：
1. 失败一次之后，冷却期内**直接拒绝**，不再拉起任何进程（每次重试都是一个新浏览器）；
2. 拒绝时把子进程的真实输出原样带出来——让模型没有"再猜一次"的余地。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.tools import browser_tools


@pytest.fixture(autouse=True)
def _clean_failures() -> None:
    browser_tools.reset_start_failures()
    yield
    browser_tools.reset_start_failures()


def test_a_failed_start_blocks_an_immediate_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser_tools._remember_start_failure(tmp_path, "拒绝访问（user-data-dir）")
    monkeypatch.setattr(browser_tools, "browser_availability", lambda: (True, None))
    spawned: list[object] = []
    monkeypatch.setattr(
        browser_tools.ProcessRegistry, "spawn",
        lambda self, *args, **kwargs: spawned.append(args),
    )

    with pytest.raises(RuntimeError, match="不再重试"):
        browser_tools.BrowserSession(
            SimpleNamespace(workspace_root=tmp_path), visible=False, timeout=1
        )

    # 关键：连 spawn 都不该走到——冷却检查必须在启动之前
    assert spawned == []


def test_the_reason_quotes_the_child_output(tmp_path: Path) -> None:
    browser_tools._remember_start_failure(tmp_path, "unknown software exception (0x80000003)")

    reason = browser_tools._start_failure_reason(tmp_path)

    assert reason is not None
    assert "0x80000003" in reason


def test_the_block_expires_after_the_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser_tools._remember_start_failure(tmp_path, "boom")
    monkeypatch.setattr(browser_tools, "_START_FAILURE_COOLDOWN_SECONDS", 0.0)

    assert browser_tools._start_failure_reason(tmp_path) is None
    # 过期后就该允许重新启动：记录已被清掉，而不是留着一个永久封锁
    assert browser_tools._start_failure_reason(tmp_path) is None


def test_the_timeout_message_never_suggests_retrying() -> None:
    """超时文案里出现"重试"，模型就会照做——这正是弹窗连环的起点。"""
    source = inspect.getsource(browser_tools.BrowserSession.__init__)

    assert "可增大" not in source
    assert "不要连续重试" in source

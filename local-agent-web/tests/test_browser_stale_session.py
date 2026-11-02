"""扁平 CDP 会话失效后的重挂（2026-11-02）。

这是一次**实测复现**出来的缺陷，不是猜的：``about:blank`` 导航到 ``http://127.0.0.1/…``
（跨源）之后，``Target.attachToTarget(flatten=True)`` 得到的会话作废，之后每条 ``Page.*`` 都回
``Not attached to an active page``；而 ``targets`` 缓存里存的是**旧会话 id**，``page()`` 会一直
把它交回来——于是"导航 → 截图"这条主路径会永久失败。

两条契约：失效时按同一个 target 重挂并重试一次；不是失效类错误就原样抛（不吞、不重试）。
"""

from __future__ import annotations

from typing import Any

import pytest

from local_agent.tools.browser_tools import (
    _STALE_SESSION_FRAGMENTS,
    BrowserSession,
    _is_stale_session_error,
)


class _Stub:
    """只保留 `call_page` 需要的状态：会话缓存、当前标签与 `call`。"""

    call_page = BrowserSession.call_page
    _retarget = BrowserSession._retarget

    def __init__(self, *, fail_times: int, message: str) -> None:
        self.active = "target-1"
        self.targets = {"target-1": "session-old"}
        self.fail_times = fail_times
        self.message = message
        self.sessions: list[str | None] = []

    def page(self, tab_id: str | None = None) -> str:
        if self.active is None:  # pragma: no cover - 本组用例不会走到
            raise AssertionError("测试桩没有可用的标签")
        return self.targets.setdefault(self.active, f"session-{len(self.targets) + 1}")

    def call(self, method: str, params: Any = None, *, session: str | None = None,
             timeout: float = 45.0) -> dict[str, Any]:
        self.sessions.append(session)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"浏览器操作失败：{self.message}")
        return {"ok": True}


def test_stale_session_is_reattached_and_retried_once() -> None:
    stub = _Stub(fail_times=1, message="Not attached to an active page")

    assert stub.call_page("Page.captureScreenshot", {"format": "png"}) == {"ok": True}
    # 第一次用旧会话（失败），第二次用的是**重挂后的新会话**。
    assert stub.sessions == ["session-old", "session-1"]
    assert stub.targets == {"target-1": "session-1"}


def test_other_errors_are_not_retried() -> None:
    stub = _Stub(fail_times=1, message="某个与失效无关的错误")

    with pytest.raises(RuntimeError, match="与失效无关"):
        stub.call_page("Page.captureScreenshot", {"format": "png"})
    # 只调了一次：非失效错误不是重试能解决的，重试只会把延迟翻倍。
    assert stub.sessions == ["session-old"]


def test_stale_error_fragments_cover_the_known_wordings() -> None:
    for fragment in _STALE_SESSION_FRAGMENTS:
        assert _is_stale_session_error(f"浏览器操作失败：{fragment}")
    assert not _is_stale_session_error("浏览器操作失败：net::ERR_NAME_NOT_RESOLVED")

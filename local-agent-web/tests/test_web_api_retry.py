"""JSON API 传输层的重试口径（2026-10-26）。

官方文档：408 / 429 / 5xx 应等待后重试，其它 4xx 改请求才有用。这里钉住三件事：
只重试该重试的、次数有上限、`Retry-After` 有上限（否则一个"等 60 秒"的头会卡住整轮）。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
from typing_extensions import Self

from local_agent.tools import web_tools
from local_agent.tools.web_tools import WebAccessError


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, _size: int | None = None) -> bytes:
        return self._payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _Opener:
    """按顺序吐出预设结果：整数表示 HTTP 错误码，字节表示一次成功响应。"""

    def __init__(self, outcomes: list[int | bytes], retry_after: str | None = None) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.retry_after = retry_after

    def open(self, request, timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, int):
            headers = {} if self.retry_after is None else {"Retry-After": self.retry_after}
            raise urllib.error.HTTPError(
                request.full_url, outcome, f"status {outcome}", headers, io.BytesIO(b"")
            )
        return _Response(outcome)


def _call(monkeypatch, outcomes: list[int | bytes], *, retry_after: str | None = None):
    opener = _Opener(outcomes, retry_after=retry_after)
    monkeypatch.setattr(web_tools, "build_opener", lambda *a, **k: opener)
    waits: list[float] = []
    result = web_tools._post_json_api(
        "https://api.example.com/x", {"q": 1}, "k", 5.0, "ua",
        service="测试服务", status_hint="检查凭据或额度", sleep=waits.append,
    )
    return result, opener, waits


def test_transient_status_is_retried_then_succeeds(monkeypatch) -> None:
    result, opener, waits = _call(monkeypatch, [429, 503, json.dumps({"ok": True}).encode()])

    assert result == {"ok": True}
    assert opener.calls == 3
    assert len(waits) == 2


def test_attempts_are_capped(monkeypatch) -> None:
    """一直 500 时不能无限重试：试满上限就如实失败。"""
    opener = _Opener([500, 500, 500, 500])
    monkeypatch.setattr(web_tools, "build_opener", lambda *a, **k: opener)

    with pytest.raises(WebAccessError, match="HTTP 500"):
        web_tools._post_json_api(
            "https://api.example.com/x", {"q": 1}, "k", 5.0, "ua",
            service="测试服务", status_hint="检查凭据或额度", sleep=lambda _s: None,
        )

    # 恰好试满 3 次就放弃，第 4 个预设没被消费。
    assert opener.calls == web_tools._MAX_ATTEMPTS == 3


def test_client_errors_are_not_retried(monkeypatch) -> None:
    """401 / 402 / 400 这类改请求才有用，重试只会把同一份错误重复三遍。"""
    with pytest.raises(WebAccessError, match="HTTP 401"):
        _call(monkeypatch, [401, json.dumps({"ok": True}).encode()])


def test_retry_after_is_honoured_but_capped(monkeypatch) -> None:
    _, _opener, waits = _call(
        monkeypatch, [429, json.dumps({"ok": True}).encode()], retry_after="2"
    )
    assert waits == [2.0]

    _, _opener2, waits2 = _call(
        monkeypatch, [429, json.dumps({"ok": True}).encode()], retry_after="600"
    )
    # 600 秒超过上限：不能为了听对端一句话把整轮卡住，退回固定退避。
    assert waits2 == [web_tools._RETRY_BACKOFF[0]]

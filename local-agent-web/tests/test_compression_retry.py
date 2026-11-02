"""压缩摘要失败时的重试与留痕（2026-11-02）。

为什么要测：摘要失败时本轮按**裁剪后的窗口**继续，落在被摘要区间里的旧事实就此消失
（`scripts/compaction_bench.py` 实测：默认策略下一次失败丢 44/96 条）。一次瞬时失败
（限流/超时/上游 5xx）不该直接换成永久的信息损失，所以失败要重试一次；仍失败则**留痕**，
而不是静默继续。
"""

from __future__ import annotations

import pytest

from local_agent.graph import nodes


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例自己装 `summarize_conversation`，避免真的去调模型。"""
    return


def _patch(monkeypatch: pytest.MonkeyPatch, behaviour: list[object]) -> list[int]:
    """按顺序消费 `behaviour`：异常对象抛出去，字符串作为摘要返回。"""
    calls: list[int] = []

    def fake(model, *, messages, previous_summary, max_chars):
        calls.append(len(calls) + 1)
        action = behaviour[len(calls) - 1]
        if isinstance(action, Exception):
            raise action
        return str(action)

    monkeypatch.setattr(nodes, "summarize_conversation", fake)
    return calls


def _run(cancelled: bool = False) -> tuple[str, str | None]:
    return nodes._summarize_with_retry(
        object(), messages=[], previous_summary=None, max_chars=128, cancelled=cancelled,
    )


def test_transient_failure_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, [RuntimeError("上游 429"), "摘要正文"])

    summary, failure = _run()

    assert (summary, failure) == ("摘要正文", None)
    # 恰好两次：一次失败 + 一次成功，正常路径不会多花调用。
    assert calls == [1, 2]


def test_persistent_failure_reports_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, [RuntimeError("boom-1"), RuntimeError("boom-2")])

    summary, failure = _run()

    assert summary == ""
    # 报**最后一次**原因：第一次的失败已经被重试覆盖，留第一次只会误导。
    assert failure == "boom-2"
    assert calls == [1, 2]


def test_cancelled_run_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户已经按了停止还再发一次请求，等于把"停下"变成"再花一次钱"。"""
    calls = _patch(monkeypatch, [RuntimeError("被取消"), "不该走到这里"])

    summary, failure = _run(cancelled=True)

    assert summary == "" and failure == "被取消"
    assert calls == [1]


def test_failure_reason_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """原因会写进面向用户的错误事件：完整异常可能带路径或上游载荷，必须截断。"""
    _patch(monkeypatch, [RuntimeError("x" * 500), RuntimeError("y" * 500)])

    _, failure = _run()

    assert failure is not None and len(failure) == 200
    assert failure.startswith("y")

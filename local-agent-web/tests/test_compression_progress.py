"""真实图、SQLite 与 SSE 的压缩进度；不调用外部供应商。"""

import threading
from typing import Any

import pytest
from test_application import _harness, _open, _session

from local_agent.graph import nodes


@pytest.mark.parametrize("fail_summary", [False, True])
def test_compression_is_live_then_persisted_without_false_success(
    tmp_path, app_config, model_factory, monkeypatch, fail_summary,
):
    app_config.llm.context_window_tokens = 12000
    app_config.llm.max_tokens = 512
    app_config.memory.compression.enabled = True
    app_config.memory.compression.preserve_recent_turns = 1
    app_config.memory.compression.summary_max_chars = 1200
    h = _harness(tmp_path, model_factory, app_config, reply="短回答",
                 response_by_input={"保留大量历史": "历史事实。" * 2500}, fail_summary=fail_summary)
    entered, release = threading.Event(), threading.Event()
    original = nodes.summarize_conversation

    def slow_summary(*args: Any, **kwargs: Any) -> str:
        entered.set()
        assert release.wait(10), "测试必须释放摘要调用"
        return original(*args, **kwargs)

    monkeypatch.setattr(nodes, "summarize_conversation", slow_summary)
    errors: list[Exception] = []
    sid = _session(h, mode="chat")
    other = _session(h, mode="chat")
    subscription = h.service.subscribe_events(sid)
    subscription.poll()
    worker = None
    try:
        h.service.submit_message(sid, "保留大量历史", "first")
        assert h.service.get_session_snapshot(sid).context.compression is None
        subscription.poll()

        def run() -> None:
            try:
                h.service.submit_message(sid, "继续下一问", "second")
            except Exception as exc:  # noqa: BLE001 -- 工作线程错误必须交给测试线程。
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        assert entered.wait(8), "真实历史压力必须触发摘要"
        live = h.service.get_session_snapshot(sid)
        assert live.context.compression.state == "running"
        assert h.service.get_session_snapshot(other).context.compression is None
        events = subscription.poll()
        assert any(event.event_type == "session_snapshot" and event.payload.context.compression
                   and event.payload.context.compression.state == "running" for event in events)
        release.set()
        worker.join(10)
        assert not worker.is_alive() and not errors
        final = h.service.get_session_snapshot(sid)
        expected = "failed" if fail_summary else "completed"
        assert final.context.compression.state == expected
        assert final.context.compression.operation_id == live.context.compression.operation_id
        assert final.context.window_tokens == 12000
        if not fail_summary:
            assert final.context.used_tokens < live.context.used_tokens
        else:
            assert any(error.code == "CONTEXT_COMPRESSION_FAILED" for error in final.errors)
        final_events = subscription.poll()
        assert any(event.event_type == "session_snapshot" and event.payload.context.compression
                   and event.payload.context.compression.state == expected for event in final_events)
    finally:
        release.set()
        if worker is not None:
            worker.join(10)
        h.service.unsubscribe_events(subscription)
        h.database.connection.close()
    reopened = _open(tmp_path, model_factory, app_config, h.database_path)
    try:
        restored = reopened.service.get_session_snapshot(sid)
        assert restored.context.compression.state == expected
        assert restored.context.compression.operation_id == final.context.compression.operation_id
    finally:
        reopened.database.connection.close()

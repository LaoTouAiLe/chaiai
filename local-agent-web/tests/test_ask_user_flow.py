"""`ask_user` 的图/服务链路（架构 §5.3、§6.14）：挂起 → 答复或跳过 → 同一个 run 继续。

假模型 + 临时工作区 + 隔离库；不联网、不产生真实模型调用。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage
from test_application import Harness, _harness, _open, _session

QUESTION = "部署到哪个目录？"


def test_cancel_before_worker_starts_settles_ledger_and_allows_next_message(
    tmp_path, model_factory, app_config,
) -> None:
    import threading

    from test_jobs import _result

    from local_agent.application.jobs import JobRuntime
    from local_agent.persistence.job_store import JobStore
    from local_agent.persistence.request_ledger import STATUS_COMPLETED

    harness = _harness(tmp_path, model_factory, app_config, decision="chat", reply="done")
    service = harness.service
    store = JobStore(harness.database.connection, lock=harness.database.checkpointer.lock)
    jobs = JobRuntime(store, background=True, max_concurrent_jobs=1)
    service._jobs = jobs
    entered, release = threading.Event(), threading.Event()
    blocker = _session(harness, title="占用 worker")
    session_id = _session(harness)

    def block():
        entered.set()
        assert release.wait(10)
        return _result(session_id=blocker)

    try:
        jobs.dispatch(jobs.begin(session_id=blocker), block)
        assert entered.wait(5)
        service.submit_message(session_id, "尚未启动就取消", "req-cancel-before-start")
        assert service.cancel_task(session_id).status == "cancelled"
        release.set()
        assert jobs.wait_for_idle(10)
        assert harness.ledger.get("req-cancel-before-start")["status"] == STATUS_COMPLETED
        assert service.get_session_snapshot(session_id).interrupted_request_id is None
        replay = service.submit_message(session_id, "尚未启动就取消", "req-cancel-before-start")
        assert replay.replayed and replay.status == "cancelled" and replay.run_id is None
        assert harness.model.decision_calls == 0
        service.submit_message(session_id, "现在可以执行", "req-next")
        assert jobs.wait_for_idle(10)
        assert service.get_session_snapshot(session_id).session_state == "completed"
        replay = service.submit_message(session_id, "尚未启动就取消", "req-cancel-before-start")
        assert replay.status == "cancelled" and replay.run_id is None
    finally:
        release.set()
        jobs.close(timeout=10)


def test_pending_question_survives_service_restart_and_resumes_original_run(
    tmp_path, model_factory, app_config,
) -> None:
    harness = _question_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "发布", "req-before-restart")
    before = harness.service.get_session_snapshot(session_id)
    question_id = before.question.question_id
    run_id = before.question.run_id
    harness.database.connection.close()
    restarted = _open(tmp_path, model_factory, app_config, harness.database_path,
                      decision="tool", reply="恢复后完成")
    try:
        restored = restarted.service.get_session_snapshot(session_id)
        assert restored.question.question_id == question_id
        assert restored.question.state == "pending"
        result = restarted.service.resolve_question(session_id, question_id, "dist", "answer", "req-after-restart")
        assert result.status == "completed"
        final = restarted.service.get_session_snapshot(session_id)
        assert final.question.state == "answered"
        assert final.question.answer == "dist"
        assert final.question.run_id == run_id
        messages = [m for m in _tool_messages(restarted, session_id) if m.name == "ask_user"]
        assert len(messages) == 1
    finally:
        restarted.database.connection.close()


@pytest.mark.parametrize("arguments", [
    {}, {"question": "   "},
    {"questions": [{"question": "Q", "options": True}]},
    {"questions": [{"question": "Q", "options": ["a", "b", "c", "d"]}]},
])
def test_invalid_question_in_real_graph_returns_a_tool_error(
    tmp_path, model_factory, app_config, arguments,
) -> None:
    harness = _harness(tmp_path, model_factory, app_config, decision="tool", reply="done",
        tool_calls=[{"name": "ask_user", "args": arguments, "id": "bad-q"}])
    session_id = _session(harness)
    result = harness.service.submit_message(session_id, "执行", "req-invalid")
    assert result.status == "completed"
    assert harness.service.get_session_snapshot(session_id).question is None
    messages = _tool_messages(harness, session_id)
    assert any(message.tool_call_id == "bad-q" and "提问参数不合法" in message.content
               for message in messages)


def test_skip_discards_answer_and_projects_legacy_skip_consistently(
    tmp_path, model_factory, app_config,
) -> None:
    from local_agent.application.pending_views import project_question

    harness = _question_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "部署", "req-1")
    question = harness.service.get_session_snapshot(session_id).question
    assert question is not None
    harness.service.resolve_question(session_id, question.question_id, "不应保留", "skip", "req-2")
    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.question.state == "skipped"
    assert snapshot.question.answer is None
    values = harness.service._read(harness.service._session_or_raise(session_id)).values
    record = values["question_history"][-1]
    assert record["answers"][0]["answer"] == ""
    assert record["answers"][0]["skipped"] is True
    legacy = {**record, "answers": [{**record["answers"][0], "answer": "旧记录残留", "skipped": False}]}
    projected = project_question({"question_history": [legacy]}, has_live_interrupt=False)
    assert projected.state == "skipped"
    assert projected.answer is None


def _question_harness(tmp_path: Path, model_factory: object, app_config: object) -> Harness:
    return _harness(
        tmp_path,
        model_factory,  # type: ignore[arg-type]
        app_config,  # type: ignore[arg-type]
        decision="tool",
        reply="已按你的答复处理。",
        tool_calls=[{"name": "ask_user", "args": {"question": QUESTION}, "id": "call-q"}],
    )


def _tool_messages(harness: Harness, session_id: str) -> list[ToolMessage]:
    session = harness.service._session_or_raise(session_id)
    values = harness.service._read(session).values
    return [message for message in values.get("messages", []) if isinstance(message, ToolMessage)]


def test_question_suspends_then_resumes_with_the_answer(tmp_path, model_factory, app_config) -> None:
    harness = _question_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)

    result = harness.service.submit_message(session_id, "帮我发布到测试环境", "req-1")

    # 1) 提交结果如实说"在等你"，而不是 completed
    assert result.status == "awaiting_input"
    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.session_state == "awaiting_input"
    assert snapshot.approval is None, "审批与提问互斥"
    assert snapshot.question is not None and snapshot.question.state == "pending"

    # 2) 挂起期间不许开新一轮（否则挂起会被新输入丢掉）
    with pytest.raises(Exception) as busy:
        harness.service.submit_message(session_id, "另外一件事", "req-2")
    assert "SESSION_BUSY" in str(busy.value)

    # 3) 答复 → 同一个 run 继续，答复以 ToolMessage 回到模型
    resumed = harness.service.resolve_question(
        session_id, snapshot.question.question_id, "dist", "answer", "req-3"
    )
    assert resumed.status == "completed"

    final = harness.service.get_session_snapshot(session_id)
    assert final.question is not None and final.question.state == "answered"
    assert final.question.answer == "dist"
    assert any("dist" in message.content for message in _tool_messages(harness, session_id))

    # 4) 答复记录进 question_history（投影据此推导状态）
    session = harness.service._session_or_raise(session_id)
    history = harness.service._read(session).values.get("question_history") or []
    assert [item["action"] for item in history] == ["answer"]
    assert history[0]["answers"][0]["question"] == QUESTION


def test_skipping_a_question_continues_without_an_answer(tmp_path, model_factory, app_config) -> None:
    harness = _question_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)

    harness.service.submit_message(session_id, "帮我发布到测试环境", "req-1")
    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.question is not None

    resumed = harness.service.resolve_question(
        session_id, snapshot.question.question_id, None, "skip", "req-2"
    )
    assert resumed.status == "completed"

    final = harness.service.get_session_snapshot(session_id)
    assert final.question is not None and final.question.state == "skipped"
    assert final.question.answer is None
    skipped = [message for message in _tool_messages(harness, session_id) if "跳过" in message.content]
    assert skipped, "必须给模型一条明确的'用户跳过了'，否则它会原地再问一次"


GROUP = [
    {"question": "部署到哪个环境？", "options": ["测试环境", "生产环境"], "allow_free_text": True},
    {"question": "用哪个分支？", "options": [], "allow_free_text": True},
    {"question": "要不要先备份？", "options": ["要", "不要"], "allow_free_text": False},
]


def _group_harness(tmp_path: Path, model_factory: object, app_config: object) -> Harness:
    return _harness(
        tmp_path,
        model_factory,  # type: ignore[arg-type]
        app_config,  # type: ignore[arg-type]
        decision="tool",
        reply="已按你的答复处理。",
        tool_calls=[{"name": "ask_user", "args": {"questions": GROUP}, "id": "call-group"}],
    )


def test_a_group_of_questions_is_answered_in_one_submission(tmp_path, model_factory, app_config) -> None:
    """题组形态（2026-10-28）：三题一次挂起、一次提交，逐题落进答复记录。"""
    harness = _group_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)

    result = harness.service.submit_message(session_id, "帮我发布", "req-1")
    assert result.status == "awaiting_input"

    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.question is not None
    assert [item.question for item in snapshot.question.items] == [item["question"] for item in GROUP]
    assert snapshot.question.items[0].options == ["测试环境", "生产环境"]
    assert snapshot.question.items[2].allow_free_text is False
    assert snapshot.question.state == "pending"

    # 一次提交整组；第二题留空 = 这题跳过（失败闭合，不是"用户答了空字符串"）
    resumed = harness.service.resolve_question(
        session_id,
        snapshot.question.question_id,
        None,
        "answer",
        "req-2",
        answers=[
            {"index": 0, "answer": "测试环境"},
            {"index": 1, "answer": ""},
            {"index": 2, "answer": "要"},
        ],
    )
    assert resumed.status == "completed"

    final = harness.service.get_session_snapshot(session_id)
    assert final.question is not None and final.question.state == "answered"
    answers = [(item.index, item.answer, item.skipped) for item in final.question.items]
    assert answers == [(0, "测试环境", False), (1, None, True), (2, "要", False)]

    session = harness.service._session_or_raise(session_id)
    history = harness.service._read(session).values.get("question_history") or []
    assert history[0]["action"] == "answer"
    assert [entry["answer"] for entry in history[0]["answers"]] == ["测试环境", "", "要"]

    # 模型拿到的是一条汇总，而不是三条 ToolMessage（三条会破坏 tool_calls 配对）
    tools = [message for message in _tool_messages(harness, session_id) if message.name == "ask_user"]
    assert len(tools) == 1
    assert "部署到哪个环境？" in tools[0].content and "测试环境" in tools[0].content
    assert "（跳过，未提供）" in tools[0].content


def test_a_group_can_be_skipped_as_a_whole(tmp_path, model_factory, app_config) -> None:
    harness = _group_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)

    harness.service.submit_message(session_id, "帮我发布", "req-1")
    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.question is not None

    harness.service.resolve_question(
        session_id, snapshot.question.question_id, None, "skip", "req-2"
    )

    final = harness.service.get_session_snapshot(session_id)
    assert final.question is not None and final.question.state == "skipped"
    assert all(item.skipped for item in final.question.items)


def test_unattended_session_never_suspends(tmp_path, model_factory, app_config) -> None:
    """定时/心跳/子 Agent 没人能回答：不许挂起（同 §21.4 的无人值守审批口径）。"""
    harness = _question_harness(tmp_path, model_factory, app_config)
    summary = harness.service.create_session(
        title="定时任务", workspace_root=harness.workspace, kind="scheduled", activate=False
    )

    result = harness.service.submit_message(summary.session_id, "帮我发布到测试环境", "req-1")

    assert result.status == "completed"
    snapshot = harness.service.get_session_snapshot(summary.session_id)
    assert snapshot.session_state != "awaiting_input"
    assert snapshot.question is None or snapshot.question.state == "skipped"
    notes = [message.content for message in _tool_messages(harness, summary.session_id)]
    assert any("跳过" in text for text in notes), notes


def test_answering_a_question_that_is_not_waiting_is_rejected(tmp_path, model_factory, app_config) -> None:
    """答非所问必须挡在 resume 之前：否则会把一次答复塞给不存在的挂起。"""
    harness = _question_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)

    harness.service.submit_message(session_id, "帮我发布到测试环境", "req-1")
    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.question is not None
    harness.service.resolve_question(session_id, snapshot.question.question_id, "dist", "answer", "req-2")

    with pytest.raises(Exception) as conflict:
        harness.service.resolve_question(
            session_id, snapshot.question.question_id, "dist", "answer", "req-3"
        )
    assert "CONFLICT" in str(conflict.value)

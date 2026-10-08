"""T28：应用服务（用例、每会话单飞、持久幂等台账、事件订阅）。

用真实 Graph + conftest 的确定性假模型，不联网；每个用例都在 `tmp_path` 下建自己的库。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from local_agent.application import errors as app_errors
from local_agent.application import events as app_events
from local_agent.application import service as app_service
from local_agent.application.models import FinalResultView
from local_agent.application.service import ApplicationService
from local_agent.config import AppConfig
from local_agent.exceptions import MemoryStoreError
from local_agent.graph import builder, nodes
from local_agent.graph.routers import ROUTE_CLASSIFIER_MARKER
from local_agent.persistence import checkpoint
from local_agent.persistence.checkpoint import ChatDatabase
from local_agent.persistence.request_ledger import (
    STATUS_COMPLETED,
    STATUS_EXECUTING,
    RequestLedger,
)
from local_agent.reflection.reflector import MEMORY_INTENT_MARKER, PLAN_DRAFT_MARKER

ModelFactory = Callable[..., object]

WORKSPACE_NAME = "ws"

#: 带这些标记的模型调用不是"主决策"：分类、计划拆步、记忆意图审核。卡它们就看不到计划。
_MODEL_MARKERS = (ROUTE_CLASSIFIER_MARKER, PLAN_DRAFT_MARKER, MEMORY_INTENT_MARKER)


def _prompt_text(messages: Any) -> str:
    """把提示按文本拼起来（与假模型内部的取法一致），用于判断这是哪一次调用。"""
    if isinstance(messages, list):
        return "\n".join(getattr(item, "text", str(item)) for item in messages)
    return getattr(messages, "text", str(messages))


@dataclass(frozen=True)
class Harness:
    service: ApplicationService
    database: ChatDatabase
    ledger: RequestLedger
    model: Any
    workspace: Path
    database_path: Path


def _harness(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig, **model_kwargs: Any
) -> Harness:
    """装配一套服务：真实 Graph + 持久台账 + 假模型。"""
    workspace = tmp_path / WORKSPACE_NAME
    workspace.mkdir(exist_ok=True)
    note = workspace / "note.txt"
    if not note.exists():
        note.write_text("保留我", encoding="utf-8", newline="")
    database_path = tmp_path / "data" / "checkpoints.sqlite"
    return _open(tmp_path, model_factory, app_config, database_path, **model_kwargs)


def _open(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    database_path: Path,
    **model_kwargs: Any,
) -> Harness:
    """（重新）打开一套服务——重新打开等价于进程重启。"""
    database = checkpoint.open_database(database_path, workspace_root=tmp_path)
    model = model_factory(**model_kwargs)
    graph = builder.build_chat_graph(database.checkpointer)
    ledger = RequestLedger(database.connection)
    service = ApplicationService(
        graph=graph,
        app_config=app_config,
        model_adapter=model,
        workspace_root=tmp_path,
        sessions=database.sessions,
        ledger=ledger,
        secrets=("sk-test-secret",),
    )
    return Harness(
        service=service,
        database=database,
        ledger=ledger,
        model=model,
        workspace=tmp_path / WORKSPACE_NAME,
        database_path=database_path,
    )


def _session(harness: Harness, *, title: str = "新会话", mode: str = "agent") -> str:
    summary = harness.service.create_session(title=title, workspace_root=harness.workspace, mode=mode)
    return summary.session_id


def _messages(harness: Harness, session_id: str) -> list[Any]:
    return list(harness.service.get_session_snapshot(session_id).messages)


def _business_rows(harness: Harness) -> tuple[int, int, int]:
    """数据库里的业务行数：Checkpoint、写入、台账。"""
    connection = harness.database.connection
    checkpoints = connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()
    writes = connection.execute("SELECT COUNT(*) FROM writes").fetchone()
    ledger = connection.execute("SELECT COUNT(*) FROM request_ledger").fetchone()
    return int(checkpoints[0]), int(writes[0]), int(ledger[0])


def _with_long_term_memory(app_config: AppConfig, path: Path) -> AppConfig:
    data = app_config.model_dump()
    data["memory"]["long_term"].update({"enabled": True, "path": str(path)})
    return AppConfig.model_validate(data)


# ---- 会话用例 --------------------------------------------------------------


def test_create_and_list_sessions(tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness, title="给 README 补一段")

    listed = harness.service.list_sessions()
    assert [item.session_id for item in listed] == [session_id]
    assert listed[0].title == "给 README 补一段"
    assert listed[0].is_active is True
    assert listed[0].workspace.display_name == WORKSPACE_NAME
    # 列表**不伪造运行状态**：要读每个线程的 Checkpoint 才能得到，那是 T30 列表工作的事。
    assert "session_state" not in type(listed[0]).model_fields


def test_unknown_session_raises_session_not_found(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    with pytest.raises(app_errors.ApplicationError) as caught:
        harness.service.get_session_snapshot("s-missing")
    assert caught.value.code == app_errors.SESSION_NOT_FOUND


def test_mode_switch_keeps_history_and_changes_tool_permissions(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
) -> None:
    calls = {text: [{"name": "write_file", "args": {"path": name, "content": text}, "id": text}]
             for text, name in [("聊天写入", "chat.txt"), ("Agent写入", "agent.txt")]}
    h = _harness(tmp_path, model_factory, app_config, decision="tool", tool_calls_by_input=calls)
    sid = _session(h, mode="agent")
    h.service.submit_message(sid, "先讨论", "first")
    before = h.service.get_session_snapshot(sid)
    changed = h.service.set_session_mode(sid, "chat", "agent")
    assert changed.session_id == sid and changed.mode == "chat"
    assert len(h.service.list_sessions()) == 1
    assert h.service.get_session_snapshot(sid).snapshot_id == before.snapshot_id
    assert h.service.get_session_snapshot(sid).messages == before.messages
    h.service.submit_message(sid, "聊天写入", "chat")
    assert not (h.workspace / "chat.txt").exists()
    h.service.set_session_mode(sid, "agent", "chat")
    h.service.submit_message(sid, "Agent写入", "agent")
    assert (h.workspace / "agent.txt").read_text(encoding="utf-8") == "Agent写入"
    assert len([m for m in _messages(h, sid) if m.kind == "user"]) == 3
    h.database.connection.close()
    restored = _open(tmp_path, model_factory, app_config, h.database_path)
    assert restored.service.list_sessions()[0].mode == "agent"
    assert len(restored.service.get_conversation_map(sid).nodes) == 3
    restored.database.connection.close()


@pytest.mark.parametrize("mode", ["agent", "chat"])
@pytest.mark.parametrize("streaming", [False, True])
def test_model_failure_is_terminal_and_next_question_works(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig, mode: str, streaming: bool,
) -> None:
    config = app_config.model_copy(update={"llm": app_config.llm.model_copy(update={"streaming": streaming})})
    h = _harness(tmp_path, model_factory, config, decision="chat", fail_on_input=["故障"], stream_chunks=3)
    sid = _session(h, mode=mode)
    result = h.service.submit_message(sid, "故障", "bad")
    snap = h.service.get_session_snapshot(sid)
    assert result.status == snap.session_state == "failed"
    assert snap.interrupted_request_id is None
    assert snap.errors and snap.usage and snap.usage.final_at is not None
    assert snap.model_calls and any(call.status == "error" for call in snap.model_calls)
    assert h.service.submit_message(sid, "故障", "bad").replayed
    assert h.service.submit_message(sid, "继续讨论", "good").status == "completed"
    assert len([m for m in _messages(h, sid) if m.kind == "user"]) == 2
    h.database.connection.close()


def test_recovery_after_side_effect_never_replays_tool(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = builder.tool_execution_node
    executions: list[str] = []

    def crash_after_write(state: Any, runtime: Any) -> Any:
        original(state, runtime)
        executions.append("write")
        raise RuntimeError("工具落盘后持久化失败")

    monkeypatch.setattr(builder, "tool_execution_node", crash_after_write)
    h = _harness(tmp_path, model_factory, app_config, decision="tool", tool_calls_by_input={
        "写文件": [{"name": "write_file", "args": {"path": "created.txt", "content": "一次"}, "id": "write"}],
    })
    sid = _session(h)
    with pytest.raises(RuntimeError, match="持久化失败"):
        h.service.submit_message(sid, "写文件", "crash")
    assert (h.workspace / "created.txt").read_text(encoding="utf-8") == "一次"
    blocked = h.service.get_session_snapshot(sid)
    assert blocked.interrupted_request_id == "crash" and blocked.session_state == "blocked"
    with pytest.raises(app_errors.ApplicationError):
        h.service.submit_message(sid, "继续", "premature")
    with pytest.raises(app_errors.ApplicationError):
        h.service.set_session_mode(sid, "chat", "agent")
    with pytest.raises(app_errors.ApplicationError):
        h.service.end_interrupted_request(sid, "crash", "stale")
    # 重启后仍提供同一核对入口，不把中断伪装成正在运行。
    h.database.connection.close()
    h = _open(tmp_path, model_factory, app_config, h.database_path)
    assert h.service.get_session_snapshot(sid).interrupted_request_id == "crash"
    ended = h.service.end_interrupted_request(sid, "crash", blocked.snapshot_id)
    assert ended.session_state == "failed" and ended.interrupted_request_id is None
    assert ended.usage and ended.usage.final_state == "failed"
    assert any(m.kind == "tool" and m.tool.status == "unknown" for m in ended.messages)
    transcript = h.service._read(h.service._session_or_raise(sid)).values["messages"]
    unanswered: set[str] = set()
    for message in transcript:
        if isinstance(message, AIMessage):
            unanswered.update(call["id"] for call in message.tool_calls)
        elif isinstance(message, ToolMessage):
            unanswered.remove(message.tool_call_id)
        elif isinstance(message, HumanMessage):
            assert not unanswered
    assert not unanswered  # 严格供应商也不会收到缺工具回复的对话。
    assert h.service.end_interrupted_request(sid, "crash", blocked.snapshot_id).snapshot_id == ended.snapshot_id
    assert h.service.submit_message(sid, "写文件", "crash").replayed
    assert executions == ["write"]
    assert h.service.submit_message(sid, "继续讨论", "next").status == "completed"
    assert len(h.service.get_conversation_map(sid).nodes) == 2
    h.database.connection.close()


def test_empty_session_snapshot_has_no_task(tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    snapshot = harness.service.get_session_snapshot(_session(harness))
    assert snapshot.current_task is None
    assert snapshot.messages == []
    assert snapshot.session_state == "idle"


# ---- 闲聊与工具 ------------------------------------------------------------


def test_chat_turn_returns_a_stable_command_result(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _harness(tmp_path, model_factory, app_config, decision="chat", reply="假回答")
    session_id = _session(harness, mode="chat")

    result = harness.service.submit_message(session_id, "你好", "req-1")

    assert result.status == "completed"
    assert result.replayed is False
    assert result.run_id
    assert result.snapshot_id
    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.session_state == "completed"
    kinds = [view.kind for view in snapshot.messages]
    assert kinds[0] == "user"
    assert kinds[-1] == "assistant"
    # 检索行插在用户提问之后（命中/未命中都算），不属于消息本身。
    assert set(kinds) <= {"user", "assistant", "memory", "tool", "error"}
    # T34：每次模型调用都留下记录（用途、耗时、估算输入）；假模型不上报供应商用量，因此
    # 实际量与缓存命中如实留空——**不写 0**，"未上报"与"上报为零"是两回事。
    assert snapshot.usage is not None
    assert snapshot.usage.usage_recorded is True
    assert snapshot.usage.model_call_ids
    assert snapshot.usage.actual_output_tokens is None
    assert snapshot.usage.cache_hit_ratio is None

    entry = harness.ledger.get("req-1")
    assert entry is not None
    assert entry["status"] == STATUS_COMPLETED
    assert entry["operation"] == "submit_message"
    assert entry["result_ref"] == result.snapshot_id


def test_tool_turn_records_a_tool_row(tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig) -> None:
    harness = _harness(
        tmp_path,
        model_factory,
        app_config,
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    session_id = _session(harness)

    harness.service.submit_message(session_id, "看看 note.txt", "req-1")

    snapshot = harness.service.get_session_snapshot(session_id)
    tools = [message.tool for message in snapshot.messages if message.kind == "tool"]
    assert [tool.tool_name for tool in tools] == ["read_file"]
    # 已完成的工具行不能显示成"等待中"：State 里的 `tool_events` 是 dataclass，读取必须载体无关。
    assert tools[0].status == "ok"
    assert tools[0].output_preview is not None


def test_regression_recall_miss_is_reported_as_miss_not_failure(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """检索跑了但没结果 → `miss`；这与"检索失败"不是一回事。"""
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "你好", "req-1")

    memory = harness.service.get_session_snapshot(session_id).memory
    if memory is not None:  # 关掉记忆时可能整块为空，那也算不上"未命中"
        assert memory.retrieval_outcome == "miss"


# ---- 审批 ------------------------------------------------------------------


def _delete_harness(tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig) -> Harness:
    return _harness(
        tmp_path,
        model_factory,
        app_config,
        decision="tool",
        reply="好的",
        tool_calls=[{"name": "delete_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )


def test_approved_tool_crash_can_end_without_showing_execution_in_progress(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = builder.tool_execution_node

    def crash(state: Any, runtime: Any) -> Any:
        original(state, runtime)
        raise RuntimeError("执行后中断")

    monkeypatch.setattr(builder, "tool_execution_node", crash)
    h = _delete_harness(tmp_path, model_factory, app_config)
    sid = _session(h)
    h.service.submit_message(sid, "删除 note.txt", "delete")
    approval = h.service.get_session_snapshot(sid).approval
    assert approval is not None
    with pytest.raises(app_errors.ApplicationError):
        h.service.set_session_mode(sid, "chat", "agent")
    with pytest.raises(RuntimeError):
        h.service.resolve_approval(sid, approval.approval_id, "approve", "approve")
    assert not (h.workspace / "note.txt").exists()
    blocked = h.service.get_session_snapshot(sid)
    assert blocked.interrupted_request_id == "approve" and blocked.approval is None
    ended = h.service.end_interrupted_request(sid, "approve", blocked.snapshot_id)
    assert ended.approval is not None and ended.approval.state == "interrupted"
    assert ended.approval.note and "未完整记录" in ended.approval.note
    assert h.service.resolve_approval(sid, approval.approval_id, "approve", "approve").replayed
    assert h.service.submit_message(sid, "继续讨论", "after").status == "completed"
    after = h.service.get_session_snapshot(sid)
    assert after.approval is not None and after.approval.state == "interrupted"
    h.database.connection.close()


def test_delete_waits_for_approval_then_executes_once(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _delete_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    target = harness.workspace / "note.txt"

    pending = harness.service.submit_message(session_id, "删除 note.txt", "req-1")
    assert pending.status == "awaiting_approval"
    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.approval is not None
    assert snapshot.approval.state == "pending"
    assert snapshot.session_state == "awaiting_approval"
    assert target.is_file()  # 审批前不执行
    approval_id = snapshot.approval.approval_id

    approved = harness.service.resolve_approval(session_id, approval_id, "approve", "req-2")
    assert approved.status == "completed"
    assert not target.exists()

    executed = len(harness.service.get_session_snapshot(session_id).messages)
    # 同一个请求 id 再点一次：返回原结果，不重复执行。
    again = harness.service.resolve_approval(session_id, approval_id, "approve", "req-2")
    assert again.replayed is True
    assert len(harness.service.get_session_snapshot(session_id).messages) == executed

    # 换一个新 id 再点：审批已经不在等待，直接判定失效而不是重新执行。
    with pytest.raises(app_errors.ApplicationError) as caught:
        harness.service.resolve_approval(session_id, approval_id, "approve", "req-3")
    assert caught.value.code == app_errors.APPROVAL_EXPIRED


def test_delete_rejection_keeps_the_file(tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig) -> None:
    harness = _delete_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    target = harness.workspace / "note.txt"
    harness.service.submit_message(session_id, "删除 note.txt", "req-1")
    approval = harness.service.get_session_snapshot(session_id).approval
    assert approval is not None

    result = harness.service.resolve_approval(
        session_id, approval.approval_id, "reject", "req-2", reason="先别删"
    )

    assert result.status == "completed"
    assert target.is_file()
    assert result.session_state == "completed"
    entry = harness.ledger.get("req-2")
    assert entry is not None and entry["status"] == STATUS_COMPLETED


def test_approval_mismatch_is_not_actionable(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _delete_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "删除 note.txt", "req-1")

    with pytest.raises(app_errors.ApplicationError) as caught:
        harness.service.resolve_approval(session_id, "ap-other", "approve", "req-2")
    assert caught.value.code == app_errors.APPROVAL_EXPIRED


# ---- 幂等与单飞 ------------------------------------------------------------


def test_same_request_id_with_different_arguments_conflicts(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "第一句", "req-1")
    before = len(_messages(harness, session_id))

    with pytest.raises(app_errors.ApplicationError) as caught:
        harness.service.submit_message(session_id, "换一句", "req-1")
    assert caught.value.code == app_errors.IDEMPOTENCY_CONFLICT
    # 冲突不产生第二次执行。
    assert len(_messages(harness, session_id)) == before


def test_retry_with_the_same_id_does_not_run_again(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "你好", "req-1")
    calls_after_first = harness.model.decision_calls
    messages_after_first = len(_messages(harness, session_id))

    replay = harness.service.submit_message(session_id, "你好", "req-1")

    assert replay.replayed is True
    assert harness.model.decision_calls == calls_after_first  # 没有第二次模型调用
    assert len(_messages(harness, session_id)) == messages_after_first
    assert harness.ledger.count() == 1


def test_second_request_in_the_same_session_is_busy(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """单飞：已有执行在跑时，**不同** id 的第二个执行请求不排队。"""
    harness = _delete_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "删除 note.txt", "req-1")

    lock = harness.service._lock_for(session_id)
    assert lock.acquire(blocking=False) is True
    try:
        with pytest.raises(app_errors.ApplicationError) as caught:
            harness.service.submit_message(session_id, "再删一次", "req-2")
        assert caught.value.code == app_errors.SESSION_BUSY

        # 消息与审批共用**同一条**互斥边界。
        approval = harness.service.get_session_snapshot(session_id).approval
        assert approval is not None
        with pytest.raises(app_errors.ApplicationError) as caught_approval:
            harness.service.resolve_approval(session_id, approval.approval_id, "approve", "req-3")
        assert caught_approval.value.code == app_errors.SESSION_BUSY
    finally:
        lock.release()

    # 锁释放后仍然不能直接开新一轮：审批还没决定，新输入会把挂起的 interrupt 丢掉。
    with pytest.raises(app_errors.ApplicationError) as caught_after:
        harness.service.submit_message(session_id, "重新提交", "req-4")
    assert caught_after.value.code == app_errors.SESSION_BUSY

    # 审批本身没被丢掉，决定后这一轮才能收尾。
    approval = harness.service.get_session_snapshot(session_id).approval
    assert approval is not None and approval.state == "pending"
    assert harness.service.resolve_approval(session_id, approval.approval_id, "approve", "req-5").status in {
        "completed",
        "awaiting_approval",
    }


def test_single_flight_boundary_is_per_session(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    first = _session(harness, title="会话一")
    second = _session(harness, title="会话二")

    lock = harness.service._lock_for(first)
    assert lock.acquire(blocking=False) is True
    try:
        result = harness.service.submit_message(second, "另一个会话", "req-2")
        assert result.status == "completed"
    finally:
        lock.release()


# ---- 跨重启与恢复 ----------------------------------------------------------


def test_ledger_survives_a_restart(tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "你好", "req-1")
    messages_before = len(_messages(harness, session_id))

    restarted = _open(tmp_path, model_factory, app_config, harness.database_path)

    replay = restarted.service.submit_message(session_id, "你好", "req-1")
    assert replay.replayed is True
    assert replay.run_id
    assert len(_messages(restarted, session_id)) == messages_before
    assert restarted.ledger.count() == 1


def test_stale_executing_with_a_provable_terminal_state_is_completed(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """台账仍为执行中，但 Checkpoint 能证明终态 → 补齐结果引用，不重跑。"""
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "你好", "req-1")
    calls = harness.model.decision_calls
    harness.database.connection.execute(
        "UPDATE request_ledger SET status = ?, result_ref = NULL WHERE request_id = ?",
        (STATUS_EXECUTING, "req-1"),
    )
    harness.database.connection.commit()

    replay = harness.service.submit_message(session_id, "你好", "req-1")

    assert replay.replayed is True
    assert replay.status == "completed"
    assert harness.model.decision_calls == calls
    entry = harness.ledger.get("req-1")
    assert entry is not None
    assert entry["status"] == STATUS_COMPLETED
    assert entry["result_ref"] == replay.snapshot_id


def test_stale_executing_without_proof_requires_human_review(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """无法证明终态 → `RECOVERY_REQUIRED`，且**绝不**自动重提或再次执行工具。"""
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    text = "你好"
    harness.ledger.start(
        request_id="req-x",
        session_id=session_id,
        operation="submit_message",
        arguments_digest=app_service._digest("submit_message", session_id, text),
    )
    before = _business_rows(harness)

    with pytest.raises(app_errors.ApplicationError) as caught:
        harness.service.submit_message(session_id, text, "req-x")

    assert caught.value.code == app_errors.RECOVERY_REQUIRED
    assert harness.model.decision_calls == 0  # 没有跑图
    assert _messages(harness, session_id) == []
    assert _business_rows(harness) == before  # 台账没被清掉，也没有新的 Checkpoint
    entry = harness.ledger.get("req-x")
    assert entry is not None
    # 保留 executing：人工核对后仍可能补齐结果引用，不能悄悄变成"失败"。
    assert entry["status"] == STATUS_EXECUTING
    assert entry["error_code"] == app_errors.RECOVERY_REQUIRED


def test_old_checkpoints_stay_readable(tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig) -> None:
    """T34 之前的 Checkpoint（没有用量记录）读得出来，且按 §15.6 说"未记录"，不伪造估算。"""
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    session = harness.database.sessions.get(session_id)
    assert session is not None
    # 直接写一份"升级前形状"的 Checkpoint：只有消息与估算用量，**没有任何 `model_call_records`
    # /`turn_records`**（T34 之前写下的记录就是这样）。不靠"跑一轮再删"——那两条通道带 reducer，
    # 删不回去。
    harness.service._graph.update_state(
        {"configurable": {"thread_id": session.thread_id}},
        {
            "run_id": "legacy-run",
            "task_status": "completed",
            "messages": [HumanMessage(content="你好"), AIMessage(content="旧回答")],
            "token_usage": {"prompt_tokens": 84_000},
            "context_budget": {"context_window_tokens": 128_000},
        },
    )

    restarted = _open(tmp_path, model_factory, app_config, harness.database_path)
    snapshot = restarted.service.get_session_snapshot(session_id)

    assert snapshot.usage is not None
    assert snapshot.usage.usage_recorded is False
    assert snapshot.usage.actual_output_tokens is None
    # 估算值仍然可用（与计量器同源），但它是"估算"字段。
    assert snapshot.usage.estimated_prompt_tokens == 84_000
    assert snapshot.messages  # 消息仍可读
    # 重新提交（新 id）照常工作。
    assert restarted.service.submit_message(session_id, "第二句", "req-2").status == "completed"


# ---- 只读性与事件订阅 ------------------------------------------------------


def test_snapshot_and_subscribe_do_not_change_business_state(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "你好", "req-1")
    before = _business_rows(harness)
    messages_before = len(_messages(harness, session_id))

    for _ in range(2):
        harness.service.get_session_snapshot(session_id)
        subscription = harness.service.subscribe_events(session_id)
        assert subscription.poll()  # 边界快照
        harness.service.unsubscribe_events(subscription)

    assert _business_rows(harness) == before
    assert len(_messages(harness, session_id)) == messages_before


def test_subscription_delivers_the_boundary_snapshot_first(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "你好", "req-1")

    first = harness.service.subscribe_events(session_id).poll()
    second = harness.service.subscribe_events(session_id).poll()

    assert [event.event_type for event in first] == ["session_snapshot"]
    assert first[0].payload.snapshot_id == second[0].payload.snapshot_id
    # 同一份 Checkpoint → 同一个事件 id（稳定 id，不是随机 UUID）。
    assert first[0].event_id == second[0].event_id
    assert first[0].stream_sequence >= 1


def test_turn_events_are_projected_from_the_state(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _harness(
        tmp_path,
        model_factory,
        app_config,
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    session_id = _session(harness)
    subscription = harness.service.subscribe_events(session_id)
    subscription.poll()  # 丢掉边界快照

    harness.service.submit_message(session_id, "看看 note.txt", "req-1")

    events = subscription.poll()
    kinds = [event.event_type for event in events]
    assert "tool_started" in kinds
    assert "tool_completed" in kinds
    assert "message_completed" in kinds
    # T34：模型调用三类事件各至少一条，且带本轮标识。
    assert "model_call_started" in kinds
    assert "model_usage_reported" in kinds
    assert "model_call_completed" in kinds
    assert kinds[-1] == "task_completed"
    # 全部是任务级事件：task_id / run_id 非空，且序号严格递增。
    assert all(event.task_id and event.run_id for event in events)
    sequences = [event.stream_sequence for event in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    harness.service.unsubscribe_events(subscription)


def test_final_answer_stream_reaches_sse_with_the_checkpoint_message_id(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    data = app_config.model_dump()
    data["llm"]["streaming"] = True
    config = AppConfig.model_validate(data)
    harness = _harness(
        tmp_path, model_factory, config,
        decision="chat",
        reply="第一句回答。第二句回答。最后不该泄露 sk-test-secret。",
        stream_chunks=8,
    )
    session_id = _session(harness, mode="chat")
    subscription = harness.service.subscribe_events(session_id)
    subscription.poll()

    harness.service.submit_message(session_id, "你好", "req-stream")

    events = subscription.poll()
    started = [event for event in events if event.event_type == "message_started"]
    deltas = [event for event in events if event.event_type == "message_delta"]
    completed = [event for event in events if event.event_type == "message_completed"]
    assert len(started) == 1
    assert len(deltas) >= 2
    assert len(completed) == 1
    assert started[0].payload.message_id == completed[0].payload.message_id
    assert "".join(event.payload.text_delta for event in deltas) == completed[0].payload.text
    assert all("sk-test-secret" not in event.payload.text_delta for event in deltas)
    assert [event.payload.chunk_sequence for event in deltas] == list(range(1, len(deltas) + 1))
    assert events[-1].event_type == "task_completed"
    harness.service.unsubscribe_events(subscription)


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("reopen", [False, True])
def test_streaming_approval_resume_keeps_the_original_run_and_executes_once(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
    decision: str, reopen: bool,
) -> None:
    data = app_config.model_dump()
    data["llm"]["streaming"] = True
    config = AppConfig.model_validate(data)
    harness = _delete_harness(tmp_path, model_factory, config)
    session_id = _session(harness)
    target = harness.workspace / "note.txt"
    pending = harness.service.submit_message(session_id, "删除 note.txt", "req-stream-submit")
    assert pending.status == "awaiting_approval"
    before = harness.service.get_session_snapshot(session_id)
    assert before.approval is not None and before.current_task is not None
    original_run = before.current_task.run_id
    approval_id = before.approval.approval_id
    assert target.is_file()

    if reopen:
        harness.database.connection.close()
        harness = _open(tmp_path, model_factory, config, harness.database_path,
                        decision="tool", reply="已处理审批。", stream_chunks=4)

    subscription = harness.service.subscribe_events(session_id)
    subscription.poll()
    result = harness.service.resolve_approval(session_id, approval_id, decision, "req-stream-resume")
    assert result.status == "completed"
    assert target.exists() is (decision == "reject")
    after = harness.service.get_session_snapshot(session_id)
    assert after.session_state == "completed"
    assert after.current_task is not None and after.current_task.run_id == original_run
    entry = harness.ledger.get("req-stream-resume")
    assert entry is not None and entry["status"] == STATUS_COMPLETED and entry["run_id"] == original_run
    events = subscription.poll()
    started = [event for event in events if event.event_type == "message_started"]
    deltas = [event for event in events if event.event_type == "message_delta"]
    completed = [event for event in events if event.event_type == "message_completed"]
    assert len(completed) == 1
    assert all(event.run_id == original_run for event in events)
    if decision == "approve":
        assert len(started) == 1 and deltas
        assert started[0].payload.message_id == completed[0].payload.message_id
        assert "".join(event.payload.text_delta for event in deltas) == completed[0].payload.text
    else:
        # Rejection is acknowledged mechanically, without a new model answer.
        assert started == deltas == []
    assert any(row.kind == "assistant" and row.message_id == completed[0].payload.message_id for row in after.messages)
    assert events[-1].event_type == "task_completed"

    calls_before_replay = harness.model.decision_calls
    rows_before_replay = _business_rows(harness)
    replay = harness.service.resolve_approval(session_id, approval_id, decision, "req-stream-resume")
    assert replay.replayed is True
    assert harness.service.get_session_snapshot(session_id).messages == after.messages
    assert _business_rows(harness) == rows_before_replay
    assert harness.model.decision_calls == calls_before_replay
    assert subscription.poll() == []

    # A later submission must bind its fresh run, never inherit the resumed run.
    next_result = harness.service.submit_message(session_id, "说一句话", "req-after-resume")
    assert next_result.status == "completed"
    following = harness.service.get_session_snapshot(session_id)
    assert following.current_task is not None and following.current_task.run_id != original_run
    next_events = subscription.poll()
    assert next_events and all(event.run_id == following.current_task.run_id for event in next_events)
    harness.service.unsubscribe_events(subscription)


def test_streaming_failure_discards_the_partial_tail_without_resubmitting(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    data = app_config.model_dump()
    data["llm"]["streaming"] = True
    harness = _harness(
        tmp_path, model_factory, AppConfig.model_validate(data),
        decision="chat", reply="第一段。第二段。完整回答。", stream_chunks=4,
        fail_stream_after_chunks=2,
    )
    session_id = _session(harness, mode="chat")
    subscription = harness.service.subscribe_events(session_id)
    subscription.poll()
    harness.service.submit_message(session_id, "你好", "req-fallback")
    events = subscription.poll()
    started = [event for event in events if event.event_type == "message_started"]
    completed = [event for event in events if event.event_type == "message_completed"]
    assert len(started) == 2  # 第二次同 id 的开始帧把前端半句清空
    assert started[0].payload.message_id == started[1].payload.message_id == completed[0].payload.message_id
    assert harness.model.decision_calls == 3  # 只读工具决策 + 流式尝试 + 普通回答降级
    assert len([row for row in _messages(harness, session_id) if row.kind == "user"]) == 1
    harness.service.unsubscribe_events(subscription)


def test_long_answer_is_readable_after_reopening_the_service(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    reply = "长回答。" * 3000
    harness = _harness(tmp_path, model_factory, app_config, decision="chat", reply=reply)
    session_id = _session(harness, mode="chat")
    harness.service.submit_message(session_id, "请详细回答", "req-long")
    original = harness.service.get_session_snapshot(session_id)
    assert any(row.kind == "assistant" and row.text == reply for row in original.messages)
    harness.database.connection.close()

    restarted = _open(tmp_path, model_factory, app_config, harness.database_path, decision="chat", reply="不应重答")
    restored = restarted.service.get_session_snapshot(session_id)
    assert restored.messages == original.messages
    assert restarted.model.decision_calls == 0


def test_empty_answer_and_model_failure_do_not_become_a_false_success(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    empty = _harness(empty_root, model_factory, app_config, decision="chat", reply="")
    empty_id = _session(empty, mode="chat")
    empty.service.submit_message(empty_id, "空回答", "req-empty")
    empty_snapshot = empty.service.get_session_snapshot(empty_id)
    assert not any(row.kind == "assistant" and row.text for row in empty_snapshot.messages)
    assert empty_snapshot.session_state == "failed"

    error_root = tmp_path / "error"
    error_root.mkdir()
    failed = _harness(error_root, model_factory, app_config, decision="chat", fail_answer=True)
    failed_id = _session(failed, mode="chat")
    result = failed.service.submit_message(failed_id, "会失败", "req-error")
    assert result.status == "failed"
    assert len([row for row in _messages(failed, failed_id) if row.kind == "user"]) == 1
    repeated = failed.service.submit_message(failed_id, "会失败", "req-error")
    assert repeated.replayed and repeated.status == "failed"
    assert failed.service.get_session_snapshot(failed_id).session_state == "failed"


def test_approval_events_are_projected(tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig) -> None:
    harness = _delete_harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    subscription = harness.service.subscribe_events(session_id)
    subscription.poll()

    harness.service.submit_message(session_id, "删除 note.txt", "req-1")
    waiting = [event.event_type for event in subscription.poll()]
    assert "approval_required" in waiting

    approval = harness.service.get_session_snapshot(session_id).approval
    assert approval is not None
    harness.service.resolve_approval(session_id, approval.approval_id, "reject", "req-2")
    resolved = [event.event_type for event in subscription.poll()]
    assert "approval_resolved" in resolved
    harness.service.unsubscribe_events(subscription)


# ---- 恢复口径的回归（复审反例）----------------------------------------------


def _register_only(harness: Harness, *, request_id: str, text: str, session_id: str) -> None:
    """模拟"请求刚登记、还没来得及绑定 run 就崩了"。"""
    harness.ledger.start(
        request_id=request_id,
        session_id=session_id,
        operation="submit_message",
        arguments_digest=app_service._digest("submit_message", session_id, text),
    )


def test_a_request_without_a_run_binding_cannot_borrow_the_previous_result(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """上一轮已完成 + 本次请求只登记没绑定 run → **不能**把上一轮的终态算成本次结果。"""
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    first = harness.service.submit_message(session_id, "第一句", "req-1")
    assert first.status == "completed"
    assert harness.model.decision_calls == 1

    _register_only(harness, request_id="req-2", text="第二句", session_id=session_id)

    with pytest.raises(app_errors.ApplicationError) as caught:
        harness.service.submit_message(session_id, "第二句", "req-2")

    assert caught.value.code == app_errors.RECOVERY_REQUIRED
    assert harness.model.decision_calls == 1  # 没有第二次执行
    entry = harness.ledger.get("req-2")
    assert entry is not None
    # 保留 executing：人工核对后仍可能补齐结果引用。
    assert entry["status"] == STATUS_EXECUTING
    assert entry["error_code"] == app_errors.RECOVERY_REQUIRED


def test_a_run_binding_that_is_not_the_current_run_requires_recovery(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """台账绑定的 run 与 Checkpoint 当前的 run 不同 → 证明不了，要求人工核对。"""
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "第一句", "req-1")

    _register_only(harness, request_id="req-2", text="第二句", session_id=session_id)
    harness.ledger.bind_run("req-2", run_id="run-not-this-one")

    with pytest.raises(app_errors.ApplicationError) as caught:
        harness.service.submit_message(session_id, "第二句", "req-2")

    assert caught.value.code == app_errors.RECOVERY_REQUIRED
    assert harness.model.decision_calls == 1


class _FailingDrive:
    """真实图 + 只让"跑这一轮"失败：用来验证图抛异常时的台账口径。

    真实模型不会制造未捕获异常（节点自己把错误记进 `error_events`），所以这里用桩——
    读状态仍然走真实图，让"重试时按 Checkpoint 核对"这段能真的跑起来。
    """

    def __init__(self, graph: Any) -> None:
        self._graph = graph
        self.checkpointer = getattr(graph, "checkpointer", None)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("图执行失败（测试故意触发）")

    def get_state(self, config: Any) -> Any:
        return self._graph.get_state(config)


def test_a_graph_exception_leaves_the_request_undecided(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """图抛异常 → 台账**不写终态**（异常可能发生在工具副作用之后），重试要求人工核对。"""
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service._graph = _FailingDrive(harness.service._graph)

    with pytest.raises(RuntimeError, match="图执行失败"):
        harness.service.submit_message(session_id, "你好", "req-1")

    entry = harness.ledger.get("req-1")
    assert entry is not None
    assert entry["status"] == STATUS_EXECUTING  # 不是"失败"——那等于声称什么都没发生
    assert entry["error_code"] == app_errors.RECOVERY_REQUIRED
    assert entry["run_id"] is None  # 图没跑到绑定那一步

    # 同 id 重试：只读核对，证明不了就要求人工核对，绝不重跑。
    with pytest.raises(app_errors.ApplicationError) as caught:
        harness.service.submit_message(session_id, "你好", "req-1")
    assert caught.value.code == app_errors.RECOVERY_REQUIRED
    assert harness.model.decision_calls == 0
    assert _messages(harness, session_id) == []


def test_events_are_delivered_while_the_turn_is_still_running(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """执行期事件来源（T29 前置修正）：计划在**这一轮还在跑**的时候就已经投递给订阅者。

    做法是把"主决策"那次模型调用卡住（分类/计划/记忆审核都有各自的提示标记，不卡它们）：
    卡住期间这一轮还没结束，但计划已经落 Checkpoint 并广播——这正是 T28 缺的那一步
    （T28 只在收尾时投影一次，接上 SSE 也看不到执行中的进度）。
    """
    harness = _harness(
        tmp_path,
        model_factory,
        app_config,
        decision="tool",
        reply="读完了",
        plan_steps=["读取 note.txt"],
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    session_id = _session(harness)
    subscription = harness.service.subscribe_events(session_id)
    subscription.poll()  # 丢掉边界快照

    entered = threading.Event()
    release = threading.Event()
    original = type(harness.model)._generate

    def blocking_generate(self: Any, messages: Any, **kwargs: Any) -> Any:
        text = _prompt_text(messages)
        if not entered.is_set() and not any(marker in text for marker in _MODEL_MARKERS):
            entered.set()
            assert release.wait(timeout=30), "测试没有释放被卡住的模型调用"
        return original(self, messages, **kwargs)

    monkeypatch.setattr(type(harness.model), "_generate", blocking_generate)

    outcome: dict[str, Any] = {}

    def submit() -> None:
        outcome["result"] = harness.service.submit_message(session_id, "看看 note.txt", "req-1")

    worker = threading.Thread(target=submit)
    worker.start()
    try:
        assert entered.wait(timeout=30), "模型调用没有开始"
        mid_run = [event.event_type for event in subscription.poll()]
    finally:
        release.set()
        worker.join(timeout=60)

    # 这一轮还在跑，计划已经到客户端了：执行期可见，不是收尾补播。
    assert "plan_updated" in mid_run
    assert "task_completed" not in mid_run
    assert outcome["result"].status == "completed"

    tail = [event.event_type for event in subscription.poll()]
    assert tail.count("task_completed") == 1
    assert tail[-1] == "task_completed"  # 终态是本轮最后一条
    assert "tool_started" in tail and "tool_completed" in tail
    harness.service.unsubscribe_events(subscription)


def test_a_later_turn_does_not_repeat_the_previous_turns_events(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """增量投影不重复播：第二轮订阅者拿不到第一轮的事实（它们已经在快照里）。"""
    harness = _harness(tmp_path, model_factory, app_config, decision="chat", reply="假回答")
    session_id = _session(harness, mode="chat")
    harness.service.submit_message(session_id, "第一句", "req-1")

    subscription = harness.service.subscribe_events(session_id)
    first = subscription.poll()
    harness.service.submit_message(session_id, "第二句", "req-2")
    second = subscription.poll()

    assert [event.event_type for event in first] == ["session_snapshot"]
    kinds = [event.event_type for event in second]
    # 第二轮只播本轮：一条回答 + 终态，历史回答不会再来一遍。
    assert kinds.count("message_completed") == 1
    assert kinds[-1] == "task_completed"
    harness.service.unsubscribe_events(subscription)


def test_the_turn_announces_a_boundary_snapshot_as_soon_as_it_is_accepted(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """提交受理后立刻播一份边界快照（本轮第一次投影），而不是等第一个业务事件。

    为什么需要：`POST /messages` 是**同步**接口（跑完整轮才返回），而 `session_snapshot` 原本
    只在订阅建立时投递一次——于是从"提交成功"到"第一个业务事件"之间，前端拿不到任何权威数据，
    用户消息和"正在处理"提示都出不来，页面看着像没反应（复审实证，2026-09-28）。

    这份快照由第一次 `on_update` 触发，那时 `prepare_context` 已把本轮用户消息写进 Checkpoint、
    `task_status` 也已是 `running`，所以它必须已经带着这条用户消息和 `running`。
    """
    harness = _harness(tmp_path, model_factory, app_config, decision="chat", reply="假回答")
    session_id = _session(harness, mode="chat")
    subscription = harness.service.subscribe_events(session_id)
    subscription.poll()  # 丢掉订阅时的边界快照

    harness.service.submit_message(session_id, "第一句", "req-1")

    events = subscription.poll()
    snapshots = [event for event in events if event.event_type == "session_snapshot"]
    assert len(snapshots) == 1  # 只播一次：每个节点都补一份会让客户端反复重画
    assert events[0].event_type == "session_snapshot"  # 排在业务事件之前
    assert snapshots[0].payload.session_state == "running"
    assert snapshots[0].payload.messages[0].kind == "user"
    assert events[-1].event_type == "task_completed"
    harness.service.unsubscribe_events(subscription)


def test_the_run_binding_is_serialized_with_the_checkpointer(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个会话并发时，台账各阶段的写入都要持有 Checkpointer 同一把锁。"""
    harness = _harness(tmp_path, model_factory, app_config)
    assert getattr(harness.service._graph, "checkpointer", None) is harness.database.checkpointer
    lock = harness.database.checkpointer.lock
    observed: list[str] = []
    for name in ("start", "bind_run", "finish", "mark_needs_recovery"):
        original = getattr(harness.ledger, name)

        def guarded(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            assert lock.locked(), f"{_name} ran outside Checkpointer lock"
            observed.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(harness.ledger, name, guarded)

    first = _session(harness)
    second = _session(harness)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(harness.service.submit_message, first, "第一轮", "req-first"),
            pool.submit(harness.service.submit_message, second, "第二轮", "req-second"),
        ]
        results = [future.result(timeout=20) for future in futures]

    assert [result.status for result in results] == ["completed", "completed"]
    assert observed.count("start") == 2
    assert observed.count("bind_run") == 2
    assert observed.count("finish") == 2


def test_session_store_writes_share_the_checkpointer_lock(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    lock = harness.database.checkpointer.lock
    for name in ("create", "switch_workspace"):
        original = getattr(harness.database.sessions, name)

        def guarded(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            assert lock.locked(), f"{_name} ran outside Checkpointer lock"
            return _original(*args, **kwargs)

        monkeypatch.setattr(harness.database.sessions, name, guarded)
    _session(harness)
    harness.service.select_workspace(harness.workspace)


# ---- 幂等重放读原结果 -------------------------------------------------------


def test_retry_after_a_later_turn_returns_the_original_result(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """会话又跑完一轮之后，旧 id 重放必须返回**原请求关联的**结果，不是当前快照。"""
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    first = harness.service.submit_message(session_id, "第一句", "req-1")
    calls = harness.model.decision_calls
    second = harness.service.submit_message(session_id, "第二句", "req-2")
    assert second.run_id and second.run_id != first.run_id

    replay = harness.service.submit_message(session_id, "第一句", "req-1")

    assert replay.replayed is True
    assert replay.run_id == first.run_id  # 不是第二轮的 run
    assert replay.snapshot_id == first.snapshot_id  # 还是当时那份结果引用
    assert replay.status == "completed"
    assert harness.model.decision_calls == calls + 1  # 重放没有再跑图
    # 当前会话仍然是第二轮，重放没有把会话"带回"第一轮。
    current = harness.service.get_session_snapshot(session_id)
    assert current.current_task is not None
    assert current.current_task.run_id == second.run_id


# ---- 订阅边界 ---------------------------------------------------------------


def _task_completed_event(session_id: str, run_id: str) -> app_events.TaskCompletedEvent:
    return app_events.TaskCompletedEvent(
        event_id=app_events.event_id_for("task_completed", run_id),
        session_id=session_id,
        task_id=run_id,
        run_id=run_id,
        occurred_at=datetime.now(timezone.utc),
        stream_sequence=99,
        payload=app_events.TaskCompletedPayload(
            final_result=FinalResultView(
                run_id=run_id, state="completed", summary="（测试）", evidence_references=[]
            )
        ),
    )


def test_live_events_during_the_boundary_stay_behind_the_snapshot(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """建立边界期间到达的实时事件必须排在边界快照**之后**（复审探针实测 `['live','snapshot']`）。"""
    harness = _harness(tmp_path, model_factory, app_config)
    session_id = _session(harness)
    harness.service.submit_message(session_id, "你好", "req-1")

    session = harness.service._session_or_raise(session_id)
    snapshot_event = harness.service._snapshot_event(session, harness.service._read(session))
    live = _task_completed_event(session_id=session_id, run_id="run-live")

    def boundary() -> Any:
        # 取边界快照期间，实时事件到达。
        harness.service._bus.publish(session_id, [live])
        return snapshot_event

    subscription = harness.service._bus.subscribe(session_id, boundary=boundary)

    delivered = subscription.poll()
    assert [event.event_type for event in delivered] == [
        "session_snapshot",
        "task_completed",
    ]
    assert delivered[0].stream_sequence < delivered[1].stream_sequence


def test_a_later_turn_does_not_re_publish_the_previous_turns_tool_rows(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """事件只讲本轮：第二轮不能把第一轮的工具行重新广播一遍。"""
    harness = _harness(
        tmp_path,
        model_factory,
        app_config,
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    session_id = _session(harness)
    subscription = harness.service.subscribe_events(session_id)
    subscription.poll()

    first = harness.service.submit_message(session_id, "看看 note.txt", "req-1")
    first_events = subscription.poll()
    assert "tool_started" in [event.event_type for event in first_events]
    assert all(event.run_id == first.run_id for event in first_events)

    second = harness.service.submit_message(session_id, "再问一句", "req-2")
    second_kinds = [event.event_type for event in subscription.poll()]

    assert "tool_started" not in second_kinds
    assert second.run_id
    harness.service.unsubscribe_events(subscription)


# ---- 工作区 ----------------------------------------------------------------


def test_workspace_list_and_switch(tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig) -> None:
    """`list_workspaces` / `select_workspace`：已有会话就切过去，没有就新建（不改写绑定）。"""
    harness = _harness(tmp_path, model_factory, app_config)
    first = _session(harness, title="会话一")

    other = tmp_path / "ws2"
    other.mkdir(exist_ok=True)
    created = harness.service.select_workspace(other, title="另一个工作区")
    assert created.session_id != first
    assert created.workspace.display_name == "ws2"

    # 切回已有会话的工作区：复用那个会话，不新建，也不改写它的工作区绑定。
    again = harness.service.select_workspace(harness.workspace)
    assert again.session_id == first
    assert again.workspace.display_name == WORKSPACE_NAME

    assert {ref.display_name for ref in harness.service.list_workspaces()} == {"ws", "ws2"}


def test_same_named_workspaces_have_distinct_selectable_ids(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    harness = _harness(tmp_path, model_factory, app_config)
    paths = [tmp_path / "alpha" / "ws", tmp_path / "beta" / "ws"]
    for path in paths:
        path.mkdir(parents=True)
    sessions = [harness.service.select_workspace(path) for path in paths]
    refs = harness.service.list_workspaces()

    assert len(refs) == 2
    assert len({ref.workspace_id for ref in refs}) == 2
    assert len({ref.display_name for ref in refs}) == 2
    assert all(str(tmp_path) not in ref.model_dump_json() for ref in refs)
    for session in sessions:
        selected = harness.service.select_workspace_by_id(session.workspace.workspace_id)
        assert selected.session_id == session.session_id
    with pytest.raises(app_errors.ApplicationError) as caught:
        harness.service.select_workspace_by_id("missing")
    assert caught.value.code == app_errors.WORKSPACE_NOT_FOUND


# ---- T30：记忆纵向切片 ----------------------------------------------------


def test_service_projects_a_long_term_memory_hit_after_reopening(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    config = _with_long_term_memory(app_config, tmp_path / "data" / "memories.sqlite")
    intent = [
        {
            "memory_type": "entity_fact",
            "content": '{"entity_type": "user", "key": "test_runner", "value": "pytest"}',
            "subject": "我",
            "importance": 0.8,
            "confidence": 0.9,
        }
    ]
    writer = _harness(
        tmp_path, model_factory, config,
        decision="chat", reply="好的，我记住了", memory_requested=True, memory_intent=intent,
    )
    first_id = _session(writer, mode="chat")
    writer.service.submit_message(first_id, "请记住：我习惯用 pytest", "req-write")
    written = writer.service.get_session_snapshot(first_id)
    assert written.memory is not None
    assert written.memory.write_outcome == "written"
    writer.database.connection.close()

    reader = _open(tmp_path, model_factory, config, writer.database_path, decision="chat", reply="用 pytest")
    second_id = _session(reader, mode="chat")
    reader.service.submit_message(second_id, "我习惯用什么跑测试？", "req-recall")
    recalled = reader.service.get_session_snapshot(second_id)
    assert recalled.memory is not None
    assert recalled.memory.retrieval_outcome == "hit"
    assert any(source.kind == "long_term_memory" for source in recalled.memory.sources)
    assert any(row.kind == "assistant" and row.text == "用 pytest" for row in recalled.messages)


def test_service_projects_memory_write_failure_without_a_false_promise(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _with_long_term_memory(app_config, tmp_path / "data" / "memories.sqlite")
    # **显式关掉代码检索**（2026-10-01）：``MemoryUseView.retrieval_outcome`` 统计的 `sources`
    # **同时包含工作区检索来源**（``projection.py`` 里那一支还看 ``STAGE_RETRIEVE_CODE``），所以
    # 检索一开，这一轮就会因为"检索到了 note.txt"而报 `hit`。本用例要验证的是**长期记忆召回
    # 未命中**，必须把另一条来源关掉，否则它测的不再是记忆。
    config = config.model_copy(update={"rag": config.rag.model_copy(update={"enabled": False})})

    original_store = nodes.long_term_store
    opens = 0

    def unavailable(context: Any) -> Any:
        nonlocal opens
        opens += 1
        if opens == 1:  # 召回正常但未命中；只让其后的写入失败。
            return original_store(context)
        raise MemoryStoreError("测试触发的长期库故障")

    monkeypatch.setattr(nodes, "long_term_store", unavailable)
    harness = _harness(
        tmp_path, model_factory, config,
        decision="tool", reply="读取已完成",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "memory_candidates": [
                {
                    "memory_type": "entity_fact",
                    "content": '{"entity_type": "project", "key": "note", "value": "保留我"}',
                    "subject": "chaiai",
                    "importance": 0.9,
                    "confidence": 0.9,
                }
            ],
        },
    )
    session_id = _session(harness)
    harness.service.submit_message(session_id, "读取 note.txt", "req-failed-write")
    snapshot = harness.service.get_session_snapshot(session_id)
    assert snapshot.memory is not None
    assert snapshot.memory.write_outcome == "failed"
    assert snapshot.memory.retrieval_outcome == "miss"
    assert opens >= 2
    assert any(error.code == app_errors.MEMORY_WRITE_FAILED for error in snapshot.errors)
    assert not snapshot.memory.write_detail or "已保存" not in snapshot.memory.write_detail


@pytest.mark.parametrize("mode", ["agent", "chat"])
def test_thinking_is_live_recoverable_read_only_and_then_persisted(
    tmp_path, model_factory, app_config, monkeypatch, mode,
):
    data = app_config.model_dump()
    data["llm"]["streaming"] = True
    harness = _harness(tmp_path, model_factory, AppConfig.model_validate(data),
                       reply="这是回答。", reasoning="分析证据。sk-test-secret。", stream_chunks=1)
    session_id = _session(harness, mode=mode)
    ready, release = threading.Event(), threading.Event()
    original = type(harness.model)._stream

    def blocked(model, *args, **kwargs):
        for chunk in original(model, *args, **kwargs):
            yield chunk
            if chunk.message.additional_kwargs.get("reasoning_content"):
                ready.set()
                assert release.wait(10)

    monkeypatch.setattr(type(harness.model), "_stream", blocked)
    subscription = harness.service.subscribe_events(session_id)
    subscription.poll()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(harness.service.submit_message, session_id, "你好", "live-think")
        try:
            assert ready.wait(10)
            calls = harness.model.decision_calls
            # 图的异步 Checkpoint 写入可能仍在收尾；不能拿跨线程行数变化误判只读请求。
            reader_thread = threading.get_ident()
            reader_writes = []
            checkpointer = harness.database.checkpointer
            for name in ("put", "put_writes"):
                original_write = getattr(checkpointer, name)
                def track_write(*args, _write=original_write, **kwargs):
                    if threading.get_ident() == reader_thread:
                        reader_writes.append(True)
                    return _write(*args, **kwargs)
                monkeypatch.setattr(checkpointer, name, track_write)
            progress = harness.service.get_session_snapshot(session_id)
            partial = [message for message in progress.messages if message.kind == "assistant" and not message.complete]
            assert len(partial) == 1 and partial[0].reasoning and not partial[0].text
            assert "sk-test-secret" not in partial[0].reasoning
            assert partial[0].chunk_sequence > 0 and partial[0].stream_channel == "reasoning"
            reconnected = harness.service.subscribe_events(session_id)
            boundary = reconnected.poll()[0].payload
            assert boundary.messages == progress.messages
            assert reader_writes == []
            assert harness.model.decision_calls == calls
            live = subscription.poll()
            assert any(event.event_type == "message_delta" and event.payload.channel == "reasoning" for event in live)
            assert not any(event.event_type == "message_completed" for event in live)
        finally:
            release.set()
        assert future.result(timeout=10).status == "completed"
    final = harness.service.get_session_snapshot(session_id)
    complete = [message for message in final.messages if message.kind == "assistant" and message.message_id == partial[0].message_id]
    assert len(complete) == 1 and complete[0].complete and complete[0].text == "这是回答。"
    assert complete[0].reasoning.startswith(partial[0].reasoning)
    assert session_id not in harness.service._streams
    assert len({message.message_id for message in final.messages}) == len(final.messages)
    harness.database.connection.close()
    reopened = _open(tmp_path, model_factory, app_config, harness.database_path)
    restored = reopened.service.get_session_snapshot(session_id)
    assert restored.messages == final.messages


@pytest.mark.parametrize("reopen", [False, True])
def test_thinking_before_approval_is_not_overwritten_by_resumed_answer(
    tmp_path, model_factory, app_config, reopen,
):
    data = app_config.model_dump()
    data["llm"]["streaming"] = True
    config = AppConfig.model_validate(data)
    harness = _delete_harness(tmp_path, model_factory, config)
    harness.model.reasoning = "先确认删除范围。"
    session_id = _session(harness)
    harness.service.submit_message(session_id, "删除 note.txt", "think-submit")
    before = harness.service.get_session_snapshot(session_id)
    thought = next(message for message in before.messages if message.kind == "assistant" and message.reasoning)
    assert before.approval is not None
    if reopen:
        harness.database.connection.close()
        harness = _open(tmp_path, model_factory, config, harness.database_path, reasoning="核对操作结果。")
    harness.service.resolve_approval(session_id, before.approval.approval_id, "approve", "think-resume")
    after = harness.service.get_session_snapshot(session_id)
    assert thought in after.messages
    assistants = [message for message in after.messages if message.kind == "assistant"]
    assert len({message.message_id for message in assistants}) == len(assistants)
    assert len(assistants) >= 2
    raw = harness.service._read(harness.service._session_or_raise(session_id)).values["messages"]
    assert any(isinstance(message, AIMessage) and message.id == thought.message_id and message.tool_calls for message in raw)


def test_thought_sink_redacts_split_values_caps_display_and_resets_both_channels():
    bus = app_service.EventBus(max_pending=100)
    subscription = bus.subscribe("s", boundary=lambda: None)
    sink = app_service._StreamingAnswerSink(session_id="s", bus=bus, secrets=("very long configured secret",))
    sink.bind_run("r")
    sink.begin()
    for piece in ("查看 very long ", "configured secret 与 C:/Us", "ers/me/private.txt。", " 证据。" * 5000):
        sink.push_reasoning(piece)
    sink.flush()
    sink.push("正文。")
    sink.flush()
    view = sink.partial_view("r")
    assert view is not None and view.reasoning_truncated
    assert len(view.reasoning) == app_service.proj.REASONING_DISPLAY_LIMIT
    assert view.text == "正文。" and view.stream_channel == "text"
    events = subscription.poll()
    thought = "".join(event.payload.text_delta for event in events
                      if event.event_type == "message_delta" and event.payload.channel == "reasoning")
    assert "configured secret" not in thought and "private.txt" not in thought
    assert thought == view.reasoning
    sink.discard()
    reset = subscription.poll()[0]
    assert reset.payload.reset and reset.payload.chunk_sequence == view.chunk_sequence + 1
    empty = sink.partial_view("r")
    assert not empty.text and not empty.reasoning and not empty.reasoning_truncated

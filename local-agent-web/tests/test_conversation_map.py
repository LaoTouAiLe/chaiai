"""真实 LangGraph/SQLite 验证会话树、上下文隔离、恢复与副作用纪律。"""

from dataclasses import replace
from pathlib import Path

import pytest
from test_application import ModelFactory, _harness, _open, _session

from local_agent.application import errors
from local_agent.config import AppConfig


def test_historical_followup_forks_without_future_messages_and_survives_restart(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
) -> None:
    h = _harness(tmp_path, model_factory, app_config, decision="chat")
    sid = _session(h, mode="chat")
    for index, text in enumerate(["祖先问题", "中间问题", "未来专属标记 FUTURE_ONLY_731"]):
        calls_before = h.model.decision_calls
        h.service.submit_message(sid, text, f"req-{index}")
    normal_calls = h.model.decision_calls - calls_before
    tree = h.service.get_conversation_map(sid)
    first, second, _third = tree.nodes
    assert [n.parent_turn_id for n in tree.nodes] == [None, first.turn_id, second.turn_id]
    assert all(n.can_continue for n in tree.nodes)
    calls = h.model.decision_calls
    prompt_count = len(h.model.seen_prompts)
    h.service.submit_message(sid, "换个方向继续", "req-fork",
                             parent_turn_id=second.turn_id, expected_branch_id="main")
    assert h.model.decision_calls == calls + normal_calls
    prompts = "\n".join(h.model.seen_prompts[prompt_count:])
    assert "祖先问题" in prompts and "中间问题" in prompts
    assert "FUTURE_ONLY_731" not in prompts
    forked = h.service.get_conversation_map(sid)
    child = forked.nodes[-1]
    assert len(forked.nodes) == 4 and len(forked.branches) == 2
    assert child.parent_turn_id == second.turn_id
    branch_id = forked.active_branch_id
    h.service.submit_message(sid, "沿新分支追问", "req-next", parent_turn_id=child.turn_id)
    assert len(h.service.get_conversation_map(sid).branches) == 2
    original = h.service.select_branch(sid, "main")
    assert [m.text for m in original.messages if m.kind == "user"] == [
        "祖先问题", "中间问题", "未来专属标记 FUTURE_ONLY_731"]
    calls = h.model.decision_calls
    replay = h.service.submit_message(sid, "换个方向继续", "req-fork",
                                      parent_turn_id=second.turn_id, expected_branch_id="main")
    assert replay.replayed and h.model.decision_calls == calls
    assert len(h.service.get_conversation_map(sid).nodes) == 5
    h.service.select_branch(sid, branch_id)
    h.database.connection.close()
    reopened = _open(tmp_path, model_factory, app_config, h.database_path, decision="chat")
    restored = reopened.service.get_session_snapshot(sid)
    assert restored.branch_id == branch_id
    assert [m.text for m in restored.messages if m.kind == "user"] == [
        "祖先问题", "中间问题", "换个方向继续", "沿新分支追问"]
    reopened.database.connection.close()


def test_legacy_ten_turns_initialize_as_chain_and_map_is_read_only(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
) -> None:
    h = _harness(tmp_path, model_factory, app_config, decision="chat")
    sid = _session(h, mode="chat")
    for index in range(10):
        h.service.submit_message(sid, f"问题 {index + 1}", f"req-{index}")
    with h.database.connection:
        h.database.connection.execute("DELETE FROM conversation_turns WHERE session_id=?", (sid,))
        h.database.connection.execute("DELETE FROM conversation_branches WHERE session_id=?", (sid,))
    changes, calls = h.database.connection.total_changes, h.model.decision_calls
    tree = h.service.get_conversation_map(sid, "问题 5")
    assert len(tree.nodes) == 10 and len(tree.branches) == 1
    assert tree.matched_turn_ids == [tree.nodes[4].turn_id]
    assert all(n.can_continue for n in tree.nodes)
    assert [n.parent_turn_id for n in tree.nodes] == [None, *[n.turn_id for n in tree.nodes[:-1]]]
    detail = h.service.get_turn_snapshot(sid, tree.nodes[4].turn_id)
    assert [m.text for m in detail.messages if m.kind == "user"] == ["问题 5"]
    assert detail.usage is not None and detail.usage.run_id == tree.nodes[4].run_id
    assert h.database.connection.total_changes == changes and h.model.decision_calls == calls
    h.service.submit_message(sid, "历史追问", "fork", parent_turn_id=tree.nodes[4].turn_id)
    assert len(h.service.get_conversation_map(sid).nodes) == 11
    h.database.connection.close()


def test_invalid_parent_and_stale_branch_rejected_before_ledger(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
) -> None:
    h = _harness(tmp_path, model_factory, app_config, decision="chat")
    sid = _session(h, mode="chat")
    h.service.submit_message(sid, "第一轮", "first")
    for kwargs in ({"parent_turn_id": "other-session-turn"}, {"expected_branch_id": "stale"}):
        with pytest.raises(errors.ApplicationError) as caught:
            h.service.submit_message(sid, "拒绝", "bad", **kwargs)
        assert caught.value.code == errors.CONVERSATION_CONFLICT
        assert h.ledger.get("bad") is None
    h.database.connection.close()


def test_branch_rename_and_order_preserve_parent_edges(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
) -> None:
    h = _harness(tmp_path, model_factory, app_config, decision="chat")
    sid = _session(h, mode="chat")
    h.service.submit_message(sid, "起点", "first")
    h.service.submit_message(sid, "原后续", "second")
    parent = h.service.get_conversation_map(sid).nodes[0].turn_id
    h.service.submit_message(sid, "新方向", "fork", parent_turn_id=parent)
    before = h.service.get_conversation_map(sid)
    after = h.service.arrange_branch(sid, before.active_branch_id, "方案 B", 0)
    assert after.branches[0].title == "方案 B" and after.nodes == before.nodes
    h.database.connection.close()


def test_shortened_message_window_preserves_known_tree_ancestry(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _harness(tmp_path, model_factory, app_config, decision="chat")
    sid = _session(h, mode="chat")
    for index in range(3):
        h.service.submit_message(sid, f"窗口问题 {index}", f"window-{index}")
    before = h.service.get_conversation_map(sid)
    session = h.service._session_or_raise(sid)
    view = h.service._read(session)
    messages = view.values["messages"]
    second = next(index for index, message in enumerate(messages)
                  if message.id == before.nodes[1].turn_id)
    shortened = replace(view, values={**view.values, "messages": messages[second:]})
    # 模拟检查点只剩最近消息窗口；地图已有父关系不能因此断成两个根。
    monkeypatch.setattr(h.service, "_read", lambda _session: shortened)
    after = h.service.get_conversation_map(sid)
    assert [(n.turn_id, n.parent_turn_id) for n in after.nodes] == [
        (n.turn_id, n.parent_turn_id) for n in before.nodes]
    h.database.connection.close()


def test_historical_tool_is_not_replayed_and_details_keep_their_own_tools(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
) -> None:
    h = _harness(tmp_path, model_factory, app_config, decision="tool", tool_calls_by_input={
        "创建文件": [{"name": "write_file", "args": {"path": "created.txt", "content": "只写一次"}, "id": "create-1"}],
    })
    sid = _session(h)
    h.service.submit_message(sid, "创建文件", "create")
    target = h.workspace / "created.txt"
    assert target.exists()
    stamp = target.stat().st_mtime_ns
    first = h.service.get_conversation_map(sid).nodes[0]
    h.service.submit_message(sid, "原后续", "later")
    detail = h.service.get_turn_snapshot(sid, first.turn_id)
    assert [m.tool.tool_name for m in detail.messages if m.kind == "tool"] == ["write_file"]
    h.service.submit_message(sid, "只讨论历史结果", "fork", parent_turn_id=first.turn_id)
    assert target.stat().st_mtime_ns == stamp
    assert len(h.service.get_session_snapshot(sid).turn_usages) == 2
    assert len(h.service.get_conversation_map(sid).nodes) == 3
    h.database.connection.close()


def test_pending_approval_cannot_be_hidden_by_switching_or_forking(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
) -> None:
    h = _harness(tmp_path, model_factory, app_config, decision="tool", tool_calls_by_input={
        "删除文件": [{"name": "delete_file", "args": {"path": "note.txt"}, "id": "delete-1"}],
    })
    sid = _session(h)
    h.service.submit_message(sid, "先讨论", "first")
    parent = h.service.get_conversation_map(sid).nodes[0].turn_id
    h.service.submit_message(sid, "删除文件", "delete")
    pending = h.service.get_session_snapshot(sid)
    assert pending.approval is not None
    for operation in (lambda: h.service.select_branch(sid, "main"),
                      lambda: h.service.submit_message(sid, "历史追问", "fork", parent_turn_id=parent)):
        with pytest.raises(errors.ApplicationError) as caught:
            operation()
        assert caught.value.code == errors.SESSION_BUSY
    assert len(h.service.get_conversation_map(sid).branches) == 1
    h.service.resolve_approval(sid, pending.approval.approval_id, "reject", "reject")
    assert (h.workspace / "note.txt").exists()
    h.service.submit_message(sid, "历史追问", "fork", parent_turn_id=parent)
    assert len(h.service.get_conversation_map(sid).branches) == 2
    h.database.connection.close()

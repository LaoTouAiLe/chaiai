"""侧边会话（§21.7，2026-10-23）。

两条主线：
1. **种子文本**只取父会话到指定轮为止、只取问答、超限从最早处丢并如实标注；
2. **血统**：侧聊记 `parent_session_id`，不抢焦点，且侧聊不能再开侧聊。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from local_agent.application import side_chat
from local_agent.persistence import migrations
from local_agent.persistence.conversation_store import ConversationTurn
from local_agent.persistence.session_store import SessionStore


def _turn(ordinal: int, question: str, answer: str) -> ConversationTurn:
    return ConversationTurn(
        turn_id=f"turn-{ordinal}",
        parent_turn_id=None,
        origin_branch_id="main",
        run_id=f"run-{ordinal}",
        question=question,
        answer_text=answer,
        state="completed",
        submitted_at="2026-10-23T00:00:00+00:00",
        checkpoint_id=None,
        ordinal=ordinal,
    )


def _service(turns: list[ConversationTurn], *, parent: bool = True) -> SimpleNamespace:
    session = SimpleNamespace(
        session_id="parent-1",
        title="主会话",
        workspace_root="/ws",
        mode="agent",
        parent_session_id=None if parent else "grand-1",
    )
    return SimpleNamespace(
        _sessions=SimpleNamespace(get=lambda session_id: session),
        _conversations=SimpleNamespace(turns=lambda session_id: turns),
        _sqlite_access=lambda fn: fn(),
    )


def test_prefix_stops_at_the_origin_turn() -> None:
    """从第 2 轮开侧聊，就不该把第 3 轮的内容带上——那是"之后"发生的事。"""
    turns = [_turn(1, "第一个问题", "第一个回答"),
             _turn(2, "第二个问题", "第二个回答"),
             _turn(3, "第三个问题", "第三个回答")]
    text, omitted = side_chat.build_context_prefix(
        _service(turns), parent_session_id="parent-1", origin_turn_id="turn-2"
    )
    assert "第二个问题" in text
    assert "第三个问题" not in text
    assert omitted == 0


def test_prefix_keeps_the_newest_and_reports_omitted() -> None:
    """超限从**最早**开始丢：用户问的总是"最近这段"。"""
    turns = [_turn(i, f"问题{i}", "回答" * 200) for i in range(1, 41)]
    text, omitted = side_chat.build_context_prefix(
        _service(turns), parent_session_id="parent-1", origin_turn_id=None
    )
    assert omitted > 0
    assert f"已省略更早的 {omitted} 轮" in text
    assert "问题40" in text          # 最新一轮必须在
    assert "问题1\n" not in text      # 最早的被丢掉


def test_prefix_rejects_unknown_turn() -> None:
    with pytest.raises(LookupError):
        side_chat.build_context_prefix(
            _service([_turn(1, "q", "a")]), parent_session_id="parent-1",
            origin_turn_id="turn-404",
        )


def test_side_chat_cannot_nest() -> None:
    """侧聊再开侧聊会让血统变成链，而上下文只取一层。"""
    with pytest.raises(ValueError, match="侧聊"):
        side_chat.create_side_chat(_service([], parent=False),
                                   parent_session_id="parent-1")


# ---- 存储层：血统落库与编号 -------------------------------------------------


def _store(tmp_path) -> SessionStore:
    connection = sqlite3.connect(tmp_path / "chat.sqlite", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    migrations.apply_migrations(connection)
    return SessionStore(connection, workspace_root=tmp_path)


def test_store_persists_lineage_and_counts_siblings(tmp_path) -> None:
    store = _store(tmp_path)
    parent = store.create(title="主会话")
    first = store.create(title="主会话 · 侧聊 1", activate=False,
                         parent_session_id=parent.session_id, origin_turn_id="turn-2")
    store.create(title="主会话 · 侧聊 2", activate=False, parent_session_id=parent.session_id)

    assert first.parent_session_id == parent.session_id
    assert first.origin_turn_id == "turn-2"
    # 侧聊不抢焦点：父会话仍是 active
    assert store.get(parent.session_id).status == "active"
    assert store.get(first.session_id).status == "idle"
    assert len(store.side_chats(parent.session_id)) == 2
    # 普通会话不带血统
    assert store.get(parent.session_id).parent_session_id is None


# ---- 首轮注入（§21.7）------------------------------------------------------


def test_compose_and_split_round_trip() -> None:
    """拼进去的上下文必须能被拆回来：`question` 存的是整段，前端要折叠它。"""
    prefix, _ = side_chat.build_context_prefix(
        _service([_turn(1, "之前问的", "之前答的")]), parent_session_id="parent-1"
    )
    composed = side_chat.compose_first_prompt(prefix, "这里为什么这么改？")
    assert side_chat.INJECTION_MARKER in composed

    got_prefix, got_question = side_chat.split_injected(composed)
    assert "之前问的" in got_prefix
    assert got_question == "这里为什么这么改？"


def test_split_without_marker_returns_text_as_is() -> None:
    prefix, question = side_chat.split_injected("普通问题")
    assert prefix == ""
    assert question == "普通问题"


def test_side_chat_runs_in_chat_mode(tmp_path) -> None:
    """侧聊是**闲聊模式**（2026-10-23 用户裁定）。

    之前它继承父会话的 mode 并注入主会话上下文，结果：气泡里出现几千字的
    "关于审批：…" 原文，还带着分界符——用户看到的是噪音，不是自己问的那句。
    现在的口径：独立闲聊，**不注入上下文**，工具集也按 chat 走。
    """
    store = _store(tmp_path)
    parent = store.create(title="主会话", mode="agent")
    side = store.create(title="主会话 · 侧聊 1", activate=False, mode="chat",
                        parent_session_id=parent.session_id)

    assert store.get(parent.session_id).mode == "agent"
    assert store.get(side.session_id).mode == "chat"
    assert store.get(side.session_id).status == "idle"


def test_promote_clears_lineage_without_touching_identity(tmp_path) -> None:
    """提升只改"它属于谁"：id 与历史都不动——改 id 会让已打开的页面与运行记录全部失效。"""
    store = _store(tmp_path)
    parent = store.create(title="主会话")
    side = store.create(title="主会话 · 侧聊 1", activate=False,
                        parent_session_id=parent.session_id, origin_turn_id="turn-3")

    store.promote(side.session_id)

    promoted = store.get(side.session_id)
    assert promoted.session_id == side.session_id
    assert promoted.parent_session_id is None
    assert promoted.origin_turn_id is None
    assert store.side_chats(parent.session_id) == []


def test_side_chats_are_hidden_from_the_session_list(tmp_path) -> None:
    """侧聊**不出现在会话列表**（2026-10-23 用户裁定）。

    它是从某个会话开出来的旁支，各占一行会把主会话挤下去（实机里连着 5 条
    「… · 侧聊 N」）。仍然可访问：右栏页签与会话地图用 id 直接取。
    """
    store = _store(tmp_path)
    parent = store.create(title="主会话")
    side = store.create(title="主会话 · 侧聊 1", activate=False,
                        parent_session_id=parent.session_id)

    listed = {item.session_id for item in store.list_sessions()}
    assert parent.session_id in listed
    assert side.session_id not in listed

    # 提升之后它就是个普通会话，应当回到列表
    store.promote(side.session_id)
    assert side.session_id in {item.session_id for item in store.list_sessions()}

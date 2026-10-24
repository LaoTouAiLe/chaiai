"""会话搜索（§15.11.2，2026-10-22）的契约测试。

覆盖：标题命中、提问命中、**回答不参与匹配**、`%` 不作通配符、limit 生效、归档默认不出现。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from local_agent.persistence import migrations
from local_agent.persistence.session_store import SessionStore


def _store(tmp_path: Path) -> SessionStore:
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    migrations.apply_migrations(connection)
    return SessionStore(connection, workspace_root=tmp_path)


def _turn(store: SessionStore, session_id: str, question: str, answer: str, ordinal: int) -> None:
    # 测试直接造轮次，避免拉起整条提交链路
    connection = store._connection
    connection.execute(
        "INSERT INTO conversation_turns (session_id, turn_id, parent_turn_id, origin_branch_id,"
        " run_id, question, answer_text, state, submitted_at, checkpoint_id, ordinal)"
        # origin_branch_id 是 NOT NULL：默认分支固定为 'main'。
        " VALUES (?, ?, NULL, 'main', NULL, ?, ?, 'completed', NULL, NULL, ?)",
        (session_id, f"{session_id}-t{ordinal}", question, answer, ordinal),
    )
    connection.commit()


def test_matches_title(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create(title="重构登录流程", workspace_root=tmp_path)
    store.create(title="无关会话", workspace_root=tmp_path)

    found = store.search_sessions("登录")
    assert [item.session_id for item in found] == [session.session_id]


def test_matches_user_question_not_answer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)
    _turn(store, session.session_id, "帮我把缓存换成 Redis", "这轮没有改任何东西。", 1)

    assert [item.session_id for item in store.search_sessions("Redis")] == [session.session_id]
    # 只在助手回答里出现的词不应命中
    assert store.search_sessions("没有改任何东西") == []


def test_percent_and_underscore_are_literal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(title="完成度 100% 的会话", workspace_root=tmp_path)
    store.create(title="另一个会话", workspace_root=tmp_path)

    assert len(store.search_sessions("%")) == 1
    assert store.search_sessions("_") == []


def test_limit_is_applied(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(5):
        store.create(title=f"批量会话 {index}", workspace_root=tmp_path)

    assert len(store.search_sessions("批量会话", limit=2)) == 2
    assert len(store.search_sessions("批量会话", limit=10)) == 5


def test_archived_sessions_are_excluded_by_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create(title="归档的会话", workspace_root=tmp_path)
    store.set_archived(session.session_id, True)

    assert store.search_sessions("归档") == []
    assert len(store.search_sessions("归档", archived=True)) == 1

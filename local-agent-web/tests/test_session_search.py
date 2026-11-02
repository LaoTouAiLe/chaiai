"""会话搜索（§15.11.2，2026-10-22；2026-11-02 扩到回答正文）的契约测试。

覆盖：标题命中、提问命中、**回答正文也参与匹配**（2026-11-02 起）、中文二字词命中、
`%` 不作通配符、limit 生效、归档默认不出现、索引不可用时退回旧 LIKE 口径。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from local_agent.persistence import migrations, turn_index
from local_agent.persistence.conversation_store import ConversationTurn
from local_agent.persistence.session_store import SessionStore


def _store(tmp_path: Path) -> SessionStore:
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    migrations.apply_migrations(connection)
    return SessionStore(connection, workspace_root=tmp_path)


def _turn(
    store: SessionStore,
    session_id: str,
    question: str,
    answer: str,
    ordinal: int,
    *,
    turn_id: str | None = None,
) -> None:
    """走**真实写路径**（``ConversationStore.sync`` → ``_save_turns``）。

    直接 ``INSERT conversation_turns`` 会绕过索引维护，测出来的"搜不到"是测试自己造的。
    """
    store.conversations.sync(
        session_id,
        "main",
        [
            ConversationTurn(
                turn_id=turn_id or f"{session_id}-t{ordinal}",
                parent_turn_id=None,
                origin_branch_id="main",
                run_id=None,
                question=question,
                answer_text=answer,
                state="completed",
                submitted_at=None,
                checkpoint_id=None,
                ordinal=ordinal,
            )
        ],
        None,
    )


def test_matches_title(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create(title="重构登录流程", workspace_root=tmp_path)
    store.create(title="无关会话", workspace_root=tmp_path)

    found = store.search_sessions("登录")
    assert [item.session_id for item in found] == [session.session_id]


def test_matches_user_question(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)
    _turn(store, session.session_id, "帮我把缓存换成 Redis", "这轮没有改任何东西。", 1)

    assert [item.session_id for item in store.search_sessions("Redis")] == [session.session_id]


def test_matches_answer_text(tmp_path: Path) -> None:
    """2026-11-02 起回答正文也进索引。

    这正是要解决的场景：用户记得的是"它当时说要用影子库"，而不是自己问过什么。
    """
    store = _store(tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)
    _turn(store, session.session_id, "回滚怎么做", "结论：用影子库逐文件比对后再覆盖。", 1)

    assert [item.session_id for item in store.search_sessions("影子库")] == [session.session_id]


def test_matches_chinese_phrase_inside_longer_sentence(tmp_path: Path) -> None:
    """中文必须靠二元组命中：`unicode61` 会把整段中文当成一个词，那样这里必然搜不到。"""
    store = _store(tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)
    _turn(store, session.session_id, "赛程怎么查", "先看瑞士轮的积分表，再看淘汰赛对阵。", 1)

    assert [item.session_id for item in store.search_sessions("瑞士轮")] == [session.session_id]
    assert [item.session_id for item in store.search_sessions("淘汰赛")] == [session.session_id]


def test_index_unavailable_falls_back_to_like(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本机没编 FTS5 时索引表不存在：搜索必须退回"标题 + 提问"的旧口径，而不是报错。"""
    store = _store(tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)
    _turn(store, session.session_id, "帮我把缓存换成 Redis", "结论里提到影子库。", 1)

    monkeypatch.setattr(turn_index, "table_exists", lambda connection: False)

    assert [item.session_id for item in store.search_sessions("Redis")] == [session.session_id]
    # 旧的 LIKE 口径本来就不看回答正文，退回后这条必须搜不到——否则说明"退回"是假的。
    assert store.search_sessions("影子库") == []


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

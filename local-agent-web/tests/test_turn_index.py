"""会话轮次全文索引（FTS5 + CJK 二元组）的契约测试。

覆盖：切词口径（二元组 / 拉丁小写 / 运算符加引号）、同事务幂等写入、回答改写后旧文本失效、
会话删除清索引、迁移在"没编 FTS5"时失败开放、以及事后补建（自愈）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from local_agent.persistence import migrations, turn_index
from local_agent.persistence.conversation_store import ConversationTurn
from local_agent.persistence.session_store import SessionStore


def _open(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    migrations.apply_migrations(connection)
    return connection


def _store(connection: sqlite3.Connection, tmp_path: Path) -> SessionStore:
    return SessionStore(connection, workspace_root=tmp_path)


def _turn(turn_id: str, question: str, answer: str, ordinal: int = 1) -> ConversationTurn:
    return ConversationTurn(
        turn_id=turn_id,
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


def _rows(connection: sqlite3.Connection, session_id: str) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) FROM {turn_index.TABLE} WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------- 切词口径


def test_cjk_is_split_into_bigrams() -> None:
    # unicode61 会把整段中文当成一个词，所以必须在写入前自己切。
    assert turn_index.tokenize("瑞士轮") == "瑞士 士轮"


def test_single_cjk_char_survives() -> None:
    # 二元组切不出单字，但用户确实可能只输入一个字。
    assert turn_index.tokenize("的") == "的"


def test_latin_is_lowercased_and_split_on_punctuation() -> None:
    assert turn_index.tokenize("Use Redis-7 now") == "use redis 7 now"


def test_match_expression_quotes_operators() -> None:
    # 裸写 "and" 会被 FTS5 当成运算符，整条 MATCH 变语法错误。
    assert turn_index.match_expression("and") == '"and"'
    assert turn_index.match_expression("") is None
    assert turn_index.match_expression("，。！") is None


# ---------------------------------------------------------------- 写索引


def test_sync_is_idempotent(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    store = _store(connection, tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)
    turn = _turn("t1", "缓存怎么做", "加一层本地缓存。")

    store.conversations.sync(session.session_id, "main", [turn], None)
    store.conversations.sync(session.session_id, "main", [turn], None)

    # 一次提交会把整个分支的轮次交过来，逐轮重建会变成写放大。
    assert _rows(connection, session.session_id) == 1


def test_index_row_is_keyed_by_turn_rowid(tmp_path: Path) -> None:
    """索引行主键必须跟着 ``conversation_turns.rowid``。

    这不是实现细节：按 ``session_id`` 定位索引行要扫整张表，而每行都是几 KB 的正文——
    一次提交就会读一遍全库正文。
    """
    connection = _open(tmp_path)
    store = _store(connection, tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)
    store.conversations.sync(session.session_id, "main", [_turn("t1", "问题", "回答提到影子库。")], None)

    turn_rowid = connection.execute("SELECT rowid FROM conversation_turns").fetchone()[0]
    index_rowid = connection.execute(f"SELECT rowid FROM {turn_index.TABLE}").fetchone()[0]
    assert index_rowid == turn_rowid


def test_rewritten_answer_replaces_stale_text(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    store = _store(connection, tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)

    store.conversations.sync(
        session.session_id, "main", [_turn("t1", "回滚怎么做", "旧结论：用影子库。")], None
    )
    store.conversations.sync(
        session.session_id, "main", [_turn("t1", "回滚怎么做", "新结论：用内容寻址快照。")], None
    )

    assert store.search_sessions("影子库") == []
    assert [item.session_id for item in store.search_sessions("内容寻址")] == [session.session_id]


def test_question_is_indexed_too(tmp_path: Path) -> None:
    """索引同时收问题与回答：一条索引覆盖两种找法，不必让问题再走一遍 LIKE。"""
    connection = _open(tmp_path)
    store = _store(connection, tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)

    store.conversations.sync(session.session_id, "main", [_turn("t1", "还没回答的问题", "")], None)

    assert _rows(connection, session.session_id) == 1
    assert [item.session_id for item in store.search_sessions("还没回答")] == [session.session_id]


def test_delete_session_drops_index_rows(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    store = _store(connection, tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)
    store.conversations.sync(session.session_id, "main", [_turn("t1", "问题", "回答提到影子库。")], None)

    store.set_archived(session.session_id, True)
    store.delete_sessions((session.session_id,))

    assert _rows(connection, session.session_id) == 0


def test_backfill_rebuilds_after_index_loss(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    store = _store(connection, tmp_path)
    session = store.create(title="普通标题", workspace_root=tmp_path)
    store.conversations.sync(session.session_id, "main", [_turn("t1", "问题", "回答提到影子库。")], None)

    connection.execute(f"DELETE FROM {turn_index.TABLE}")
    connection.commit()
    assert store.search_sessions("影子库") == []

    assert turn_index.backfill(connection) == 1
    connection.commit()
    assert [item.session_id for item in store.search_sessions("影子库")] == [session.session_id]


# ---------------------------------------------------------------- 迁移


def test_upgrade_backfills_turns_written_before_v17(tmp_path: Path) -> None:
    """真实升级路径：v16 的库里有轮次，升到 v17 之后必须能搜到它们。"""
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    migrations._apply(connection, migrations._CHAT_MIGRATIONS[:16])
    store = _store(connection, tmp_path)
    session = store.create(title="旧会话", workspace_root=tmp_path)
    # v16 没有索引表：这次写入只落 conversation_turns。
    store.conversations.sync(session.session_id, "main", [_turn("t1", "旧问题", "旧回答里提到影子库。")], None)
    assert store.search_sessions("影子库") == []

    assert migrations.apply_migrations(connection) == migrations.CHAT_SCHEMA_VERSION

    assert [item.session_id for item in store.search_sessions("影子库")] == [session.session_id]


def test_migration_is_fail_open_without_fts5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没编 FTS5 时：只记版本、不建表，应用照常可用。"""
    monkeypatch.setattr(turn_index, "fts5_supported", lambda connection: False)
    connection = sqlite3.connect(tmp_path / "chat.sqlite")

    assert migrations.apply_migrations(connection) == migrations.CHAT_SCHEMA_VERSION
    assert not turn_index.table_exists(connection)
    assert (connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == migrations.CHAT_SCHEMA_VERSION)


def test_missing_index_is_rebuilt_on_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败开放之后补上 FTS5 的机器：版本号已等于 17，正常迁移不会再跑，必须自愈。"""
    monkeypatch.setattr(turn_index, "fts5_supported", lambda connection: False)
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    migrations.apply_migrations(connection)
    assert not turn_index.table_exists(connection)

    monkeypatch.undo()
    migrations.apply_migrations(connection)

    assert turn_index.table_exists(connection)

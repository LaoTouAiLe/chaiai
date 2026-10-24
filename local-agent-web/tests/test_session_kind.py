"""T57：会话来源 ``kind`` 与持久任务索引（架构 §10.6、§17.1）。

三组用例：

1. **闭集只写一次**：``session_kind.SESSION_KINDS``、``SessionKind`` 字面量与聊天库 v7 的 ``CHECK`` 是同一份；
2. **会话来源**：默认 ``interactive``、闭集外拒绝、读写往返；历史行由迁移补齐，既有会话行为不变；
3. **任务索引**：与 ``sessions`` / Checkpoint **同库同连接**，状态闭集，终态不可改写。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import get_args

import pytest

from local_agent.exceptions import JobStoreError, SessionStoreError
from local_agent.persistence import migrations
from local_agent.persistence.job_store import (
    JOB_STATE_CANCELLED,
    JOB_STATE_COMPLETED,
    JOB_STATE_NEEDS_REVIEW,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_WAITING_USER,
    JOB_STATES,
    TERMINAL_JOB_STATES,
    JobStore,
)
from local_agent.persistence.session_store import SessionStore
from local_agent.scope import normalize_path
from local_agent.session_kind import (
    DEFAULT_SESSION_KIND,
    SESSION_KIND_HEARTBEAT,
    SESSION_KIND_INTERACTIVE,
    SESSION_KIND_SCHEDULED,
    SESSION_KIND_SUBAGENT,
    SESSION_KINDS,
    SessionKind,
)


def _migrated_connection(tmp_path: Path) -> sqlite3.Connection:
    """已迁移的聊天库（会话索引 + 任务索引 + Checkpoint 同库）。"""
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    migrations.apply_migrations(connection)
    return connection


# ---- 闭集一致 --------------------------------------------------------------


def test_session_kind_vocabulary_is_the_single_source() -> None:
    """状态字段的类型别名、闭集常量与默认值必须同源（改一处漏一处会立刻失败）。"""
    assert get_args(SessionKind) == SESSION_KINDS
    assert SESSION_KINDS == (
        SESSION_KIND_INTERACTIVE,
        SESSION_KIND_SCHEDULED,
        SESSION_KIND_HEARTBEAT,
        SESSION_KIND_SUBAGENT,
    )
    assert DEFAULT_SESSION_KIND == SESSION_KIND_INTERACTIVE
    # T67 已有隔离子图生产者，v9 CHECK 与叶子闭集同步扩展。
    assert "subagent" in SESSION_KINDS


def test_job_state_closed_set_and_terminal_subset() -> None:
    assert JOB_STATES == (
        JOB_STATE_QUEUED,
        JOB_STATE_RUNNING,
        JOB_STATE_WAITING_USER,
        JOB_STATE_COMPLETED,
        "failed",
        JOB_STATE_CANCELLED,
        JOB_STATE_NEEDS_REVIEW,
    )
    assert TERMINAL_JOB_STATES == frozenset(
        {JOB_STATE_COMPLETED, "failed", JOB_STATE_CANCELLED, JOB_STATE_NEEDS_REVIEW}
    )


# ---- 迁移 v7 ---------------------------------------------------------------


def test_chat_migration_v7_backfills_kind_and_creates_jobs(tmp_path: Path) -> None:
    """v0.6 之前的库升到 v7：历史会话的来源是 ``interactive``，并多出 ``jobs`` 表。"""
    root = tmp_path / "project"
    root.mkdir()
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    # 按已发布的 v1–v6 脚本生成旧库，不用当前 SessionStore 写新列。
    migrations._apply(connection, migrations._CHAT_MIGRATIONS[:6])
    connection.execute(
        "INSERT INTO sessions (session_id, thread_id, title, workspace_root, project_id, "
        "created_at, updated_at, last_checkpoint_id, status, mode, archived) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "old",
            "thread-old",
            "旧历史",
            normalize_path(root),
            "project",
            "2026-01-01",
            "2026-01-01",
            None,
            "active",
            "agent",
            0,
        ),
    )
    connection.commit()

    assert migrations.apply_migrations(connection) == migrations.CHAT_SCHEMA_VERSION
    assert migrations.apply_migrations(connection) == migrations.CHAT_SCHEMA_VERSION  # 幂等

    store = SessionStore(connection, workspace_root=root)
    legacy = store.get("old")
    assert legacy is not None
    # 历史行取迁移的列默认值；其余字段（含 mode）逐项不变
    assert legacy.kind == SESSION_KIND_INTERACTIVE
    assert legacy.mode == "agent" and not legacy.archived
    assert set(migrations.table_names(connection)) >= {"sessions", "jobs"}
    connection.close()


def test_sessions_kind_column_enforces_the_closed_set(tmp_path: Path) -> None:
    """闭集在**库层**也成立：绕过 SessionStore 直接写脏值会被拒。"""
    connection = _migrated_connection(tmp_path)
    store = SessionStore(connection, workspace_root=tmp_path)
    session = store.create()
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute(
            "UPDATE sessions SET kind = 'unknown' WHERE session_id = ?", (session.session_id,)
        )
    connection.close()


def test_jobs_table_enforces_the_state_closed_set(tmp_path: Path) -> None:
    connection = _migrated_connection(tmp_path)
    store = SessionStore(connection, workspace_root=tmp_path)
    session = store.create()
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute(
            "INSERT INTO jobs (job_id, session_id, state, attempt, created_at, updated_at) "
            "VALUES ('j', ?, 'bogus', 1, 'now', 'now')",
            (session.session_id,),
        )
    connection.close()


# ---- 会话来源 --------------------------------------------------------------


def test_session_store_defaults_kind_and_rejects_unknown(tmp_path: Path) -> None:
    connection = _migrated_connection(tmp_path)
    store = SessionStore(connection, workspace_root=tmp_path)

    default = store.create()
    scheduled = store.create(kind=SESSION_KIND_SCHEDULED)
    heartbeat = store.create(kind=SESSION_KIND_HEARTBEAT)

    assert default.kind == DEFAULT_SESSION_KIND == SESSION_KIND_INTERACTIVE
    assert store.get(scheduled.session_id).kind == SESSION_KIND_SCHEDULED
    assert store.get(heartbeat.session_id).kind == SESSION_KIND_HEARTBEAT
    # 读写往返一致（list 与 get 走同一条列解析）
    listed = {item.session_id: item.kind for item in store.list_sessions()}
    assert listed[scheduled.session_id] == SESSION_KIND_SCHEDULED

    with pytest.raises(SessionStoreError):
        store.create(kind="unknown")
    assert store.get(store.create(kind=SESSION_KIND_SUBAGENT).session_id).kind == SESSION_KIND_SUBAGENT
    connection.close()


# ---- 任务索引 --------------------------------------------------------------


def test_jobs_table_rebuild_to_v8_keeps_rows_and_widens_the_closed_set(tmp_path: Path) -> None:
    """v8 是**重建表**的迁移（SQLite 改不了 CHECK），行必须一列不差地活下来，闭集要放宽（T61）。"""
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    # 只升到 v7：模拟"上一次发布的库"
    assert migrations._apply(connection, migrations._CHAT_MIGRATIONS[:7]) == 7
    # 用**原始 SQL** 造旧数据，不经过当前的 `SessionStore.create`：后者的列集合会随 schema 前进
    # （v13 起多写 parent_session_id / origin_turn_id），拿它往 v7 的表里写必然报"没有这一列"。
    connection.execute(
        "INSERT INTO sessions (session_id, thread_id, title, workspace_root, project_id,"
        " created_at, updated_at, last_checkpoint_id, status, mode, archived, kind)"
        " VALUES ('legacy-sess','thread-legacy','旧会话',?,'p1','2026-01-01','2026-01-01',"
        " NULL,'idle','agent',0,'interactive')",
        (str(tmp_path),),
    )
    legacy = JobStore(connection).create(session_id="legacy-sess")

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE jobs SET state = 'needs_review' WHERE job_id = ?", (legacy.job_id,)
        )

    assert migrations.apply_migrations(connection) == migrations.CHAT_SCHEMA_VERSION
    assert migrations.apply_migrations(connection) == migrations.CHAT_SCHEMA_VERSION  # 幂等

    store = JobStore(connection)
    # 逐列相同（dataclass 相等 = 十列全等），说明是一次纯粹的表重建
    assert store.get(legacy.job_id) == legacy
    # 新取值现在写得进去，库里也多了一条终态
    assert (
        store.update_state(legacy.job_id, JOB_STATE_NEEDS_REVIEW).state
        == JOB_STATE_NEEDS_REVIEW
    )
    # 重建表会跟着删掉索引，必须重建回来（否则"按状态找任务"会退化成全表扫描）
    indexes = {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    )}
    assert {"idx_jobs_state", "idx_jobs_session"} <= indexes
    connection.close()


def test_jobs_share_the_same_database_and_connection_as_sessions(tmp_path: Path) -> None:
    path = tmp_path / "chat.sqlite"
    connection = sqlite3.connect(path)
    migrations.apply_migrations(connection)
    sessions = SessionStore(connection, workspace_root=tmp_path)
    jobs = JobStore(connection)

    session = sessions.create()
    other = sessions.create()
    first = jobs.create(session_id=session.session_id)
    second = jobs.create(session_id=session.session_id)
    jobs.create(session_id=other.session_id)

    # 同一库同一连接：会话索引、任务索引、Checkpoint 共用一个事务边界（架构 §17.1）
    assert set(migrations.table_names(connection)) >= {"sessions", "jobs"}
    assert [job.job_id for job in jobs.list_for_session(session.session_id)] == [
        first.job_id,
        second.job_id,
    ]
    connection.close()

    # 换个连接打开同一个库，任务仍在（持久化落在会话同一个文件里）
    reopened = sqlite3.connect(path)
    try:
        migrations.apply_migrations(reopened)
        assert JobStore(reopened).get(first.job_id).state == JOB_STATE_QUEUED
    finally:
        reopened.close()


def test_job_state_chain_keeps_run_id_and_seals_terminal_rows(tmp_path: Path) -> None:
    connection = _migrated_connection(tmp_path)
    session = SessionStore(connection, workspace_root=tmp_path).create()
    jobs = JobStore(connection)

    job = jobs.create(session_id=session.session_id)
    assert job.state == JOB_STATE_QUEUED and job.attempt == 1
    assert job.run_id is None and job.started_at is None and not job.is_terminal

    running = jobs.update_state(job.job_id, JOB_STATE_RUNNING, run_id="run-1")
    assert running.state == JOB_STATE_RUNNING and running.run_id == "run-1"
    assert running.started_at is not None and running.finished_at is None

    waiting = jobs.update_state(job.job_id, JOB_STATE_WAITING_USER)
    # 不传 run_id 时不改写已记录的值（恢复与页面据此定位轮次）
    assert waiting.run_id == "run-1" and waiting.started_at == running.started_at

    done = jobs.update_state(job.job_id, JOB_STATE_COMPLETED, detail="本轮完成")
    assert done.is_terminal and done.finished_at is not None and done.detail == "本轮完成"

    # 终态不可改写（架构 §17.1）
    with pytest.raises(JobStoreError):
        jobs.update_state(job.job_id, JOB_STATE_RUNNING)
    # 取值越界与不存在的任务都明确失败
    with pytest.raises(JobStoreError):
        jobs.create(session_id=session.session_id, state="bogus")
    with pytest.raises(JobStoreError):
        jobs.create(session_id="")
    with pytest.raises(JobStoreError):
        jobs.update_state("missing", JOB_STATE_RUNNING)
    with pytest.raises(JobStoreError):
        jobs.list_by_state("bogus")

    assert jobs.list_by_state(JOB_STATE_COMPLETED) == [done]
    assert jobs.list_by_state(JOB_STATE_QUEUED) == []
    assert jobs.list_by_state() == []
    connection.close()


def test_missing_side_chat_columns_are_repaired_even_when_version_says_latest(
    tmp_path: Path,
) -> None:
    """线上故障复现（2026-10-23）：`schema_migrations` 记了 13，`sessions` 却没有那两列。

    此后每次列会话 / 切换工作区都 500（`no such column: parent_session_id`）。
    只信版本号不够，必须按**列**校验并补齐。
    """
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    connection.row_factory = sqlite3.Row
    assert migrations.apply_migrations(connection) == migrations.CHAT_SCHEMA_VERSION

    # 人为把库做坏：删列但不改版本号——正是线上那次的样子
    # （索引引用了这一列，SQLite 不允许在有索引时删列，先删索引）
    connection.execute("DROP INDEX IF EXISTS idx_sessions_parent")
    connection.execute("ALTER TABLE sessions DROP COLUMN parent_session_id")
    connection.commit()
    broken = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")}
    assert "parent_session_id" not in broken

    # 再跑一次：版本已是最新，靠列校验补回来
    assert migrations.apply_migrations(connection) == migrations.CHAT_SCHEMA_VERSION
    repaired = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")}
    assert {"parent_session_id", "origin_turn_id"} <= repaired

"""SQLite 结构迁移与版本记录（架构 §10.6、开发文档 T11）。

- 会话索引与 LangGraph Checkpoint 共用同一个数据库文件：一个连接、一个事务边界，
  避免多连接互相加锁（``session_store`` 与 Checkpoint 由 ``checkpoint.open_database``
  一起装配）。
- 迁移按版本号顺序执行，版本记录在 ``schema_migrations``；重复调用幂等，
  已应用的版本会跳过。
- 本模块只做结构变更，不写业务数据（首个会话的初始化在 ``session_store``）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

#: 当前代码期望的库结构版本（等于最后一个迁移的编号）。
SCHEMA_VERSION = 1

_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_checkpoint_id TEXT,
    status TEXT NOT NULL DEFAULT 'idle'
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions (status);
CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions (updated_at);
"""

#: (版本号, 该版本的结构变更 SQL)；只允许在末尾追加，不允许改写已发布的版本。
_MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _SESSIONS_SQL),)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_version(connection: sqlite3.Connection) -> int:
    """读取已应用的最高版本；迁移表还不存在时返回 ``0``。"""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0]) if row is not None else 0


def apply_migrations(connection: sqlite3.Connection) -> int:
    """把库结构升到当前版本，返回升级后的版本号。

    Args:
        connection: 目标数据库连接（与 Checkpoint 共用）。

    Returns:
        应用完成后的版本号；已是最新时直接返回原版本。

    Raises:
        sqlite3.Error: 结构变更失败（例如库文件只读）。
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    version = current_version(connection)
    for target, script in _MIGRATIONS:
        if target <= version:
            continue
        connection.executescript(script)
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (target, _now()),
        )
        connection.commit()
        version = target
    connection.commit()
    return version

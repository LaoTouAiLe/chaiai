"""SQLite 结构迁移与版本记录（架构 §10.3、§10.6，开发文档 T11、T13）。

- 会话索引与 LangGraph Checkpoint 共用同一个数据库文件：一个连接、一个事务边界，
  避免多连接互相加锁（``session_store`` 与 Checkpoint 由 ``checkpoint.open_database``
  一起装配）。
- 长期记忆按架构 §10.3 使用**独立**的 SQLite 文件，所以这里有两套互不相干的迁移：
  :func:`apply_migrations`（聊天库：Checkpoint + 会话索引）与
  :func:`apply_long_term_migrations`（长期库：entities / facts / task_conclusions）。
  两套迁移必须应用到**不同的数据库文件**，调用方由 ``checkpoint.open_database`` 与
  ``memory.long_term.open_store`` 分别封装；同一个文件只会被其中一套应用。
- 迁移按版本号顺序执行，版本记录在 ``schema_migrations``（每个库文件各自持有）；重复调用
  幂等，已应用的版本会跳过。
- 本模块只做结构变更，不写业务数据（首个会话的初始化在 ``session_store``）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

#: 聊天库（Checkpoint + 会话索引）当前代码期望的结构版本（等于最后一个迁移的编号）。
CHAT_SCHEMA_VERSION = 2

#: 长期记忆库当前代码期望的结构版本。
LONG_TERM_SCHEMA_VERSION = 2

#: 兼容既有调用：未加前缀的 ``SCHEMA_VERSION`` 指聊天库。
SCHEMA_VERSION = CHAT_SCHEMA_VERSION

#: 长期记忆库的表名（架构 §10.3）；初始化脚本与测试用它做结构自检。
LONG_TERM_TABLES: tuple[str, ...] = ("entities", "facts", "task_conclusions")

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

#: 长期记忆三张表（架构 §10.3 字段逐条对应，字段名不要改）。
#:
#: 只建"查询路径"上的普通索引：实体按作用域查、事实按实体查、结论按项目查。
#: 唯一约束（同键是否可重复）属于 T15 的 CRUD 语义，本版本不预设。
_LONG_TERM_SQL = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_scope ON entities (scope, scope_key);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities (name);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    "key" TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    importance REAL NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT,
    expires_at TEXT,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts (entity_id);
CREATE INDEX IF NOT EXISTS idx_facts_status ON facts (status);

CREATE TABLE IF NOT EXISTS task_conclusions (
    id TEXT PRIMARY KEY,
    task_title TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    project_scope TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_conclusions_project ON task_conclusions (project_scope);
"""

#: 长期库 v2（T15）：只加**查询路径**上的索引，不改列、不加表——架构 §10.3 的字段表保持不变。
#: 取实体（按作用域 + 身份）、同键取版本与冲突判定（entity_id + key + status）、过期淘汰
#: （expires_at）都按这些条件过滤；v1 库升级只需建索引，已有数据不动。
_LONG_TERM_V2_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_entities_scope_identity
    ON entities (scope, scope_key, entity_type, name);
CREATE INDEX IF NOT EXISTS idx_facts_entity_key
    ON facts (entity_id, "key", status);
CREATE INDEX IF NOT EXISTS idx_facts_expires_at
    ON facts (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_task_conclusions_scope_time
    ON task_conclusions (project_scope, created_at);
"""

#: 聊天库 v2（T14C）：把"当前会话"从"每个工作区各一个"改成**整个库唯一**（架构 §10.6）。
#: 先归一历史多 active，再用部分唯一索引防止复发；工作区列表按 ``workspace_root`` 去重，
#: 因此补一条索引。``sessions`` 的字段与 v1 完全一致，升级只改数据与索引。
_CHAT_V2_SQL = """
UPDATE sessions SET status = 'idle'
WHERE status = 'active'
  AND session_id <> (
      SELECT session_id FROM sessions WHERE status = 'active'
      ORDER BY updated_at DESC, created_at DESC, session_id DESC LIMIT 1
  );
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_single_active
    ON sessions (status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions (workspace_root);
"""

#: 聊天库的迁移；(版本号, 该版本的结构变更 SQL)，只允许在末尾追加，不允许改写已发布的版本。
_CHAT_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SESSIONS_SQL),
    (2, _CHAT_V2_SQL),
)

#: 长期记忆库的迁移；规则同上（独立文件、独立版本号）。
_LONG_TERM_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _LONG_TERM_SQL),
    (2, _LONG_TERM_V2_INDEXES_SQL),
)

_SCHEMA_MIGRATIONS_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
)


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


def table_names(connection: sqlite3.Connection) -> frozenset[str]:
    """该连接上已存在的表名（用于初始化自检与测试断言）。"""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _apply(
    connection: sqlite3.Connection, migrations: tuple[tuple[int, str], ...]
) -> int:
    """在一条连接上按顺序应用一组迁移，返回应用后的版本号。"""
    connection.execute(_SCHEMA_MIGRATIONS_SQL)
    version = current_version(connection)
    for target, script in migrations:
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


def apply_migrations(connection: sqlite3.Connection) -> int:
    """把**聊天库**结构升到当前版本，返回升级后的版本号。

    Args:
        connection: 目标数据库连接（与 Checkpoint 共用）。

    Returns:
        应用完成后的版本号；已是最新时直接返回原版本。

    Raises:
        sqlite3.Error: 结构变更失败（例如库文件只读）。
    """
    return _apply(connection, _CHAT_MIGRATIONS)


def apply_long_term_migrations(connection: sqlite3.Connection) -> int:
    """把**长期记忆库**结构升到当前版本，返回升级后的版本号。

    必须使用 :data:`LONG_TERM_TABLES` 所在的独立数据库文件；调用方一般直接用
    ``local_agent.memory.long_term.open_store``，不需要自己开连接。

    Args:
        connection: 长期记忆库的数据库连接。

    Returns:
        应用完成后的版本号；已是最新时直接返回原版本。

    Raises:
        sqlite3.Error: 结构变更失败（例如库文件只读）。
    """
    return _apply(connection, _LONG_TERM_MIGRATIONS)

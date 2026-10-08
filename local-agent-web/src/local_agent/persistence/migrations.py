"""SQLite 结构迁移与版本记录（架构 §10.3、§10.6，开发文档 T11、T13）。

- 会话索引与 LangGraph Checkpoint 共用同一个数据库文件：一个连接、一个事务边界，
  避免多连接互相加锁（``session_store`` 与 Checkpoint 由 ``checkpoint.open_database``
  一起装配）。
- 长期记忆按架构 §10.3 使用**独立**的 SQLite 文件，所以这里有多套互不相干的迁移：
  :func:`apply_migrations`（聊天库：Checkpoint + 会话索引 + 请求台账）、
  :func:`apply_long_term_migrations`（长期库：entities / facts / task_conclusions）与
  :func:`apply_settings_migrations`（模型连接设置库，§6.8）。
  各套迁移必须应用到**不同的数据库文件**，调用方由 ``checkpoint.open_database``、
  ``memory.long_term.open_store`` 与 ``model_settings.store.open_settings_database`` 分别封装；
  同一个文件只会被其中一套应用。
- 迁移按版本号顺序执行，版本记录在 ``schema_migrations``（每个库文件各自持有）；重复调用
  幂等，已应用的版本会跳过。
- 本模块只做结构变更，不写业务数据（首个会话的初始化在 ``session_store``）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

#: 聊天库（Checkpoint + 会话索引 + 请求台账）当前代码期望的结构版本（等于最后一个迁移的编号）。
CHAT_SCHEMA_VERSION = 6

#: 长期记忆库当前代码期望的结构版本。
LONG_TERM_SCHEMA_VERSION = 3

#: 设置库当前结构版本：v2 = 外部路径授权；v3 = 非秘密应用偏好。
SETTINGS_SCHEMA_VERSION = 3

#: 兼容既有调用：未加前缀的 ``SCHEMA_VERSION`` 指聊天库。
SCHEMA_VERSION = CHAT_SCHEMA_VERSION

#: 长期记忆库的表名（架构 §10.3）；初始化脚本与测试用它做结构自检。
LONG_TERM_TABLES: tuple[str, ...] = ("entities", "facts", "task_conclusions")

#: 模型连接设置库的表名（§6.8 的"非秘密设置"）；初始化脚本与测试用它做结构自检。
SETTINGS_TABLES: tuple[str, ...] = (
    "model_connections",
    "model_connection_versions",
    "model_selection",
    "settings_meta",
    "external_path_grants",
    "app_preferences",
)

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

#: 聊天库 v3（T28）：**幂等请求台账**（架构 §15.2）。
#:
#: 只保护"命令执行"：记录请求 id、操作、规范化参数摘要、`run_id`、状态与结果引用，
#: **不复制 Graph 业务状态**，也不成为第二套任务真源。必须跨服务重启保留到会话删除，
#: 所以它进同一个聊天库而不是进程内存。
_CHAT_V3_SQL = """
CREATE TABLE IF NOT EXISTS request_ledger (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL,
    result_ref TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_ledger_session ON request_ledger (session_id);
CREATE INDEX IF NOT EXISTS idx_request_ledger_status ON request_ledger (status);
"""

#: 模型连接设置库 v1（T29A；契约 §6.8、§15.9）。
#:
#: 四张表分工：
#: - ``model_connections``：**当前**连接（含能力检测结果）。删除是"停用"（``enabled=0``），不做物理删除，
#:   否则历史模型标识与用量会失去解释；
#: - ``model_connection_versions``：每次编辑产生一条**历史版本**，旧版本与凭据引用必须保留到引用它的
#:   运行结束（挂起等审批的运行恢复时要按固定版本重取客户端）；
#: - ``model_selection``：默认连接与会话选择（``scope`` + ``scope_id``，默认那条是 ``('default','default')``）；
#: - ``settings_meta``：导入记账等元数据。
#:
#: **结构上就存不下明文密钥**：四张表没有任何一列可以放密钥值，密钥只在系统凭据存储或进程内存里，
#: 表里留的是不含明文的 ``credential_ref``。这不是靠调用方自觉。
_SETTINGS_V1_SQL = """
CREATE TABLE IF NOT EXISTS model_connections (
    connection_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    context_window_tokens INTEGER NOT NULL,
    max_tokens INTEGER NOT NULL,
    temperature REAL NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    streaming INTEGER NOT NULL,
    auth_mode TEXT NOT NULL,
    api_key_env TEXT,
    credential_ref TEXT,
    allow_plaintext_http INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    capability_chat TEXT NOT NULL,
    capability_tools TEXT NOT NULL,
    capability_streaming TEXT NOT NULL,
    capability_checked_at TEXT,
    capability_message TEXT,
    config_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_connections_enabled ON model_connections (enabled);

CREATE TABLE IF NOT EXISTS model_connection_versions (
    connection_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    context_window_tokens INTEGER NOT NULL,
    max_tokens INTEGER NOT NULL,
    temperature REAL NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    streaming INTEGER NOT NULL,
    auth_mode TEXT NOT NULL,
    credential_ref TEXT,
    -- 旧配置导入的取 Key 路径也要按版本留下来：挂起等审批的运行恢复时按固定版本重取密钥，
    -- 那时如果只能看到"最新一行"的 api_key_env，导入的连接一旦被编辑过就取不到密钥了。
    api_key_env TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (connection_id, config_version)
);

CREATE TABLE IF NOT EXISTS model_selection (
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, scope_id)
);

CREATE TABLE IF NOT EXISTS settings_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: 模型连接设置库 v2（T38；架构 §1.5）。
#:
#: 界面上的"档位 + 授权目录"必须**唯一真源**地存在一处：档位是 ``settings_meta`` 里的一行
#: （``permissions.external_paths.mode``），逐条授权是这张表。授权**不写回 config.yaml**——
#: 让应用改自己的配置文件会抹掉注释、在只读挂载上直接失败，那正是 T29A 定过的口径。
#:
#: ``path`` 存**规范化**键（展开 ``~`` 与符号链接、统一大小写与分隔符，见
#: ``path_guard.normalize_grant_path``）：同一目录写成 ``E:/data`` 与 ``e:\data`` 必须落到
#: 同一个主键上，否则"授权过却仍被拦"会变成只能靠肉眼比对路径才能发现的 bug。
_SETTINGS_V2_SQL = """
CREATE TABLE IF NOT EXISTS external_path_grants (
    path TEXT PRIMARY KEY,
    access TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: 聊天库的迁移；(版本号, 该版本的结构变更 SQL)，只允许在末尾追加，不允许改写已发布的版本。
_CHAT_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SESSIONS_SQL),
    (2, _CHAT_V2_SQL),
    (3, _CHAT_V3_SQL),
    (4, ("ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'agent' "
         "CHECK (mode IN ('chat', 'agent'));")),
    (5, """
        ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0
            CHECK (archived IN (0, 1));
        CREATE TABLE workspaces (
            workspace_root TEXT PRIMARY KEY,
            title TEXT,
            removed INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1))
        );
        INSERT INTO workspaces (workspace_root)
            SELECT DISTINCT workspace_root FROM sessions;
    """),
    (6, """
        CREATE TABLE conversation_branches (
            session_id TEXT NOT NULL REFERENCES sessions(session_id),
            branch_id TEXT NOT NULL,
            title TEXT NOT NULL,
            fork_turn_id TEXT,
            tip_turn_id TEXT,
            checkpoint_id TEXT,
            position INTEGER NOT NULL,
            active INTEGER NOT NULL CHECK(active IN (0, 1)),
            PRIMARY KEY (session_id, branch_id)
        );
        CREATE UNIQUE INDEX conversation_active_branch
            ON conversation_branches(session_id) WHERE active = 1;
        CREATE TABLE conversation_turns (
            session_id TEXT NOT NULL REFERENCES sessions(session_id),
            turn_id TEXT NOT NULL,
            parent_turn_id TEXT,
            origin_branch_id TEXT NOT NULL,
            run_id TEXT,
            question TEXT NOT NULL,
            answer_text TEXT NOT NULL,
            state TEXT NOT NULL,
            submitted_at TEXT,
            checkpoint_id TEXT,
            ordinal INTEGER NOT NULL,
            PRIMARY KEY (session_id, turn_id),
            FOREIGN KEY (session_id, parent_turn_id)
                REFERENCES conversation_turns(session_id, turn_id)
        );
        CREATE INDEX conversation_turn_parent
            ON conversation_turns(session_id, parent_turn_id, ordinal);
    """),
)

#: 长期库 v3（T52）：给 ``facts`` 与 ``task_conclusions`` 加 ``origin`` 列（架构 §10.3、§16.2）。
#:
#: 用 ``ALTER TABLE ADD COLUMN`` 追加在末尾，与 dataclass 的字段顺序一致。``DEFAULT 'owner'`` 只服务
#: **历史行**：T52 之前唯一的写入者是本机反思链路，属于已知来源；新写入一律显式给值，写侧校验会拒绝
#: 越界与缺失（所以这里的默认值不会被新行用到）。
_LONG_TERM_V3_ORIGIN_SQL = """
ALTER TABLE facts ADD COLUMN origin TEXT NOT NULL DEFAULT 'owner';
ALTER TABLE task_conclusions ADD COLUMN origin TEXT NOT NULL DEFAULT 'owner';
"""

#: 长期记忆库的迁移；规则同上（独立文件、独立版本号）。
_LONG_TERM_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _LONG_TERM_SQL),
    (2, _LONG_TERM_V2_INDEXES_SQL),
    (3, _LONG_TERM_V3_ORIGIN_SQL),
)

#: 模型连接设置库的迁移；规则同上（独立文件、独立版本号）。
_SETTINGS_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SETTINGS_V1_SQL),
    (2, _SETTINGS_V2_SQL),
    # The table may already exist if the process exited before recording version 3.
    (3, ("CREATE TABLE IF NOT EXISTS app_preferences (id INTEGER PRIMARY KEY CHECK(id=1), "
         "version INTEGER NOT NULL CHECK(version>0), values_json TEXT NOT NULL);")),
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


def apply_settings_migrations(connection: sqlite3.Connection) -> int:
    """把**模型连接设置库**结构升到当前版本，返回升级后的版本号。

    必须使用 :data:`SETTINGS_TABLES` 所在的独立数据库文件；调用方一般直接用
    ``local_agent.model_settings.store.open_settings_database``，不需要自己开连接。

    Args:
        connection: 设置库的数据库连接。

    Returns:
        应用完成后的版本号；已是最新时直接返回原版本。

    Raises:
        sqlite3.Error: 结构变更失败（例如库文件只读）。
    """
    return _apply(connection, _SETTINGS_MIGRATIONS)

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

#: 聊天库（Checkpoint + 会话索引 + 请求台账 + 任务索引 + 文件变更账本）当前代码期望的结构版本
#: （等于最后一个迁移的编号）。
#: 11（2026-10-21）：文件变更账本与基线索引——页面要能回答"这次任务改了哪些文件、每个
#: 文件从改前到改后是什么样"，只靠工具事件里的单次片段拼不出来（见 `persistence/file_changes.py`）。
#: 14（2026-10-23）：用户终端启动审批的元数据审计，不保存输入和输出。
#: **编号为什么是 14 而不是 13**：13 已经被侧聊那批列占用了。两批改动此前都写成 13，而迁移器按
#: "编号 <= 已应用版本就跳过"推进（见 :func:`_apply`），于是新库只会跑前一条 —— `terminal_audit`
#: 永远建不出来，全新安装启动即崩（`no such table: terminal_audit`，2026-10-23 在临时库上复现）。
CHAT_SCHEMA_VERSION = 14

#: 长期记忆库当前代码期望的结构版本。
LONG_TERM_SCHEMA_VERSION = 3

#: 设置库当前结构版本：v2 = 外部路径授权；v3 = 非秘密应用偏好。
#: 4（2026-10-16）：连接新增 ``supports_vision``——"这条连接收不收图像输入"是**用户声明**，不是实测项
#: （探针只能靠发一张图试，代价与误报都不划算，而且"看不看得见"与"收不收"不是一回事），
#: 因此它不并进 ``capability_*`` 那组**实测报告**字段，单独一列。
SETTINGS_SCHEMA_VERSION = 4

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
#: **密钥不进连接表**：四张表没有一列放密钥值，表里留的是不含明文的 ``credential_ref``（一个引用）。
#: 2026-10-13 起密钥值存在 ``settings_meta`` 的 ``credentials.<ref>`` 键下（§6.8 改动：从系统凭据存储
#: 改为落设置库，理由是分发形态下凭据管理器在别人机器上会失败）。也就是说密钥与连接**在同一个本地文件**
#: 里，但**不在同一张表**——"引用与密钥分离"这条性质保持不变，连接表本身仍然放不下明文。
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

#: 聊天库 v7（T57）：会话来源列 + 持久任务索引（架构 §10.6、§17.1）。
#:
#: ``sessions.kind`` 用 ``ALTER TABLE ADD COLUMN`` 追加在末尾（与 v4 的 ``mode`` 同一写法）。
#: ``DEFAULT 'interactive'`` 只服务**历史行**：v0.6 之前每个会话都是用户在页面上创建的，属 ``interactive``；
#: 新会话由 ``session_store.create`` 显式给值，写侧校验会拒绝闭集外的取值。
#:
#: ``jobs`` 与**会话索引、Checkpoint 同库同事务边界**（架构 §17.1）：它只回答"任务在哪一步"，
#: 业务状态真源仍是 Checkpoint，事件是它的单向投影——不得被当作第二套任务真源。
_CHAT_V7_JOBS_SQL = """
ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'interactive'
    CHECK (kind IN ('interactive', 'scheduled', 'heartbeat'));

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'waiting_user', 'completed', 'failed', 'cancelled')
    ),
    run_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT
);
CREATE INDEX idx_jobs_state ON jobs (state);
CREATE INDEX idx_jobs_session ON jobs (session_id, created_at);
"""

#: 聊天库 v8（T61）：任务状态闭集追加 ``needs_review``（架构 §17.1、§17.5）。
#:
#: **SQLite 改不了 CHECK 约束**，只能重建表——标准四步：建新表（带新闭集）→ 逐列拷贝 →
#: 删旧表 → 改名（索引随旧表一起被删，所以重建）。``jobs`` 里没有外键指向它，改名不会牵连别的表；
#: 数据一行不改（`checks` 只放宽了一个取值，旧数据必然满足新约束）。
#:
#: 为什么必须走迁移而不是只改 Python 侧闭集：``state`` 的 CHECK 是**写侧的最后一个守卫**，
#: 只改一半会让"索引允许的状态"与"实现能写出的状态"不一致——实测就是写 ``needs_review`` 时抛
#: ``CHECK constraint failed``，而不是悄悄写进去。
_CHAT_V8_JOBS_NEEDS_REVIEW_SQL = """
CREATE TABLE jobs_v8 (
    job_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'waiting_user', 'completed', 'failed', 'cancelled',
                  'needs_review')
    ),
    run_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT
);
INSERT INTO jobs_v8 (
    job_id, session_id, state, run_id, attempt,
    created_at, started_at, updated_at, finished_at, detail
)
SELECT job_id, session_id, state, run_id, attempt,
       created_at, started_at, updated_at, finished_at, detail
FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_v8 RENAME TO jobs;
CREATE INDEX idx_jobs_state ON jobs (state);
CREATE INDEX idx_jobs_session ON jobs (session_id, created_at);
"""

_CHAT_V9_SUBAGENT_SQL = """
CREATE TABLE sessions_v9 (
    session_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_checkpoint_id TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    mode TEXT NOT NULL DEFAULT 'agent' CHECK (mode IN ('chat', 'agent')),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    kind TEXT NOT NULL DEFAULT 'interactive'
        CHECK (kind IN ('interactive', 'scheduled', 'heartbeat', 'subagent'))
);
INSERT INTO sessions_v9 SELECT * FROM sessions;
DROP TABLE sessions;
ALTER TABLE sessions_v9 RENAME TO sessions;
CREATE INDEX idx_sessions_status ON sessions (status);
CREATE INDEX idx_sessions_updated_at ON sessions (updated_at);
CREATE UNIQUE INDEX idx_sessions_single_active ON sessions (status) WHERE status = 'active';
CREATE INDEX idx_sessions_workspace ON sessions (workspace_root);
"""

_CHAT_V10_CHILDREN_SQL = """
CREATE TABLE job_children (
    parent_job_id TEXT NOT NULL REFERENCES jobs(job_id),
    parent_run_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    PRIMARY KEY(parent_job_id, child_id)
);
CREATE INDEX job_children_run ON job_children(parent_run_id);
"""

#: 聊天库的迁移；(版本号, 该版本的结构变更 SQL)，只允许在末尾追加，不允许改写已发布的版本。
_CHAT_V11_FILE_CHANGES_SQL = """
CREATE TABLE file_change_baselines (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    path TEXT NOT NULL,
    storage_ref TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    sha256 TEXT,
    unavailable_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, path)
);
CREATE TABLE file_change_ops (
    op_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    run_id TEXT,
    tool TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'modify', 'delete')),
    additions INTEGER NOT NULL DEFAULT 0 CHECK (additions >= 0),
    deletions INTEGER NOT NULL DEFAULT 0 CHECK (deletions >= 0),
    before_sha256 TEXT,
    after_sha256 TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX file_change_ops_session ON file_change_ops(session_id, op_id);
"""

_CHAT_V12_CRON_SQL = """
CREATE TABLE cron_jobs (
    job_id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
    prompt TEXT NOT NULL CHECK (length(trim(prompt)) BETWEEN 1 AND 4000),
    schedule TEXT NOT NULL CHECK (length(schedule) BETWEEN 1 AND 120),
    workspace_root TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'agent' CHECK (mode IN ('chat', 'agent')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_run_at TEXT,
    last_run_at TEXT,
    last_status TEXT CHECK (last_status IN ('running', 'succeeded', 'failed', 'blocked')),
    last_error TEXT
);
CREATE INDEX cron_jobs_due ON cron_jobs(enabled, next_run_at);

-- session_id 不设外键：运行记录要比会话活得久，会话被清理后仍应能查到"那天跑过、结果是什么"。
CREATE TABLE cron_runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES cron_jobs(job_id) ON DELETE CASCADE,
    session_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'blocked')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT
);
CREATE INDEX cron_runs_job ON cron_runs(job_id, started_at DESC);
"""

_CHAT_V13_SIDE_CHAT_SQL = """
-- 侧边会话（§21.7）：只加两列，**不新增 kind**。
-- 加新 kind 要重建整张 sessions 表（它的 kind 有 CHECK 约束），而那会把 v9 以来的迁移链拖进来；
-- 用 parent_session_id IS NOT NULL 表达"这是侧聊"，判定同样明确，改动面小一个数量级。
ALTER TABLE sessions ADD COLUMN parent_session_id TEXT;
ALTER TABLE sessions ADD COLUMN origin_turn_id TEXT;
CREATE INDEX idx_sessions_parent ON sessions (parent_session_id);
"""

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
    (7, _CHAT_V7_JOBS_SQL),
    (8, _CHAT_V8_JOBS_NEEDS_REVIEW_SQL),
    (9, _CHAT_V9_SUBAGENT_SQL),
    (10, _CHAT_V10_CHILDREN_SQL),
    (11, _CHAT_V11_FILE_CHANGES_SQL),
    (12, _CHAT_V12_CRON_SQL),
    (13, _CHAT_V13_SIDE_CHAT_SQL),
    # 14（不是 13）：13 上面那条已经用掉了，同号的第二条会被 `_apply` 跳过（见 CHAT_SCHEMA_VERSION 注释）。
    (14, """
        CREATE TABLE terminal_audit (
            approval_id TEXT PRIMARY KEY,
            terminal_id TEXT,
            workspace_root TEXT NOT NULL,
            shell TEXT NOT NULL,
            permission_mode TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            decision TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
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
#: v4 的脚本标识（2026-10-16）。这里只当**标记**用，真正的 DDL 见 :func:`_apply_vision_columns`
#: ——因为它必须按"列在不在"决定做不做，而裸 SQL 表达不了这个条件。
_SETTINGS_V4_VISION_SQL = (
    "ALTER TABLE model_connections ADD COLUMN supports_vision INTEGER NOT NULL DEFAULT 0"
)

_SETTINGS_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SETTINGS_V1_SQL),
    (2, _SETTINGS_V2_SQL),
    # The table may already exist if the process exited before recording version 3.
    (3, ("CREATE TABLE IF NOT EXISTS app_preferences (id INTEGER PRIMARY KEY CHECK(id=1), "
         "version INTEGER NOT NULL CHECK(version>0), values_json TEXT NOT NULL);")),
    # 4：连接级"支持图像输入"（用户声明，2026-10-16）。**两张表都加**：等待审批的运行恢复时用的是
    # `model_connection_versions` 里那一版（§6.8"这一轮固定用哪个版本"），只加到当前表会让恢复后的
    # 运行突然"看不见图"，表现成随机故障。默认 0 = 失败闭合（没声明就是收不了图）。
    (4, _SETTINGS_V4_VISION_SQL),
)


def _apply_vision_columns(connection: sqlite3.Connection, target: int) -> None:
    """给两张连接表加 ``supports_vision``（v4），**幂等**：列已经在就只补记版本号。

    **两张都加**：等待审批的运行恢复时读的是 ``model_connection_versions`` 里那一版（§6.8
    "这一轮固定用哪个版本"），只加当前表会让恢复后的运行突然"看不见图"，表现成随机故障。

    为什么不能用裸 SQL：``ALTER TABLE ... ADD COLUMN`` **不幂等**。v3 的先例说明"进程在记录版本号
    之前退出"是真会发生的（``test_interrupted_v3_upgrade_recovers_on_restart``），那时重跑一条裸
    ALTER 会以 ``duplicate column name`` 直接炸掉启动——所以这里按列实际状态决定做不做。
    """
    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table in ("model_connections", "model_connection_versions"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "supports_vision" not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN supports_vision INTEGER NOT NULL DEFAULT 0"
                )
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (target, _now())
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise

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
        if script == _CHAT_V9_SUBAGENT_SQL:
            _apply_sessions_v9(connection, script, target)
            version = target
            continue
        if script == _SETTINGS_V4_VISION_SQL:
            # 幂等加列（2026-10-16）：见 `_apply_vision_columns` 的注释。
            _apply_vision_columns(connection, target)
            version = target
            continue
        if script == _CHAT_V10_CHILDREN_SQL:
            connection.commit()
            try:
                connection.executescript("BEGIN IMMEDIATE;\n" + script)
                connection.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (target, _now()))
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
            version = target
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


def _apply_sessions_v9(connection: sqlite3.Connection, script: str, target: int) -> None:
    """被引用父表重建：临时关外键，DDL/版本同事务，复核后恢复原设置。"""
    connection.commit()
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + script)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("会话来源迁移后外键校验失败")
        connection.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (target, _now()))
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    finally:
        connection.execute(f"PRAGMA foreign_keys={foreign_keys}")


def _ensure_sessions_columns(connection: sqlite3.Connection) -> None:
    """兜底补齐 v13 的两列（2026-10-23）。

    **为什么需要它**：线上出现过一次"`schema_migrations` 记了 13、但 `sessions`
    没有 `parent_session_id`"的库——版本号说升级完成，实际列不在，之后每次列表/切换
    工作区都 500（`no such column: parent_session_id`）。只靠版本号判断是不够的。
    这里按**列**校验，缺什么补什么，让这类库在下次启动时自愈。
    """
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")}
    if "session_id" not in columns:
        # 表还不存在（全新库）：交给正常迁移去建，这里什么都不做。
        return
    missing = [name for name in ("parent_session_id", "origin_turn_id") if name not in columns]
    if not missing:
        return
    with connection:
        for name in missing:
            connection.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions (parent_session_id)"
        )


def apply_migrations(connection: sqlite3.Connection) -> int:
    """把**聊天库**结构升到当前版本，返回升级后的版本号。

    Args:
        connection: 目标数据库连接（与 Checkpoint 共用）。

    Returns:
        应用完成后的版本号；已是最新时直接返回原版本。

    Raises:
        sqlite3.Error: 结构变更失败（例如库文件只读）。
    """
    version = _apply(connection, _CHAT_MIGRATIONS)
    # 版本号可能是对的、列却不在（见 `_ensure_sessions_columns` 的说明），所以每次都要校验。
    _ensure_sessions_columns(connection)
    return version


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

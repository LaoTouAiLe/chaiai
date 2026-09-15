"""T13 验收：作用域规则与记忆库初始化（开发文档 §6 T13，架构 §10.2–§10.3）。

覆盖：长期库迁移幂等、字段契约（dataclass ↔ 建表语句）、跨项目/工作区隔离、
聊天库不被记忆表污染（旧 Checkpoint 仍可恢复）、记忆关闭不建向量索引、
``scripts/init_db.py`` 的初始化与 ``--check``。

全部使用临时目录与临时 SQLite；不依赖网络，不导入 ``chromadb``（本机未安装该可选依赖）。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from types import ModuleType

import pytest

from local_agent import scope
from local_agent.config import AppConfig
from local_agent.exceptions import MemoryDisabledError, MemoryStoreError, MemoryUnavailableError
from local_agent.memory import long_term, medium_term, schemas
from local_agent.persistence import checkpoint, migrations
from local_agent.persistence.session_store import SessionStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 架构 §10.2 列出的中期记录字段（顺序即契约顺序）。
ARCHITECTURE_MEDIUM_TERM_FIELDS = (
    "id",
    "content",
    "embedding",
    "scope",
    "scope_key",
    "user_id",
    "project_id",
    "workspace_root",
    "source_thread_id",
    "source_event_id",
    "importance",
    "confidence",
    "created_at",
    "last_accessed_at",
    "expires_at",
    "status",
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[long_term.LongTermStore]:
    """长期记忆库，用完关连接（Windows 上不关连接会让 tmp 目录删不掉）。"""
    opened = long_term.open_store(tmp_path / "data" / "memories.sqlite")
    try:
        yield opened
    finally:
        opened.close()


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


# --------------------------------------------------------------- 长期库迁移


def test_long_term_migrations_create_tables_and_are_idempotent() -> None:
    connection = sqlite3.connect(":memory:")

    first = migrations.apply_long_term_migrations(connection)
    second = migrations.apply_long_term_migrations(connection)

    assert first == second == migrations.LONG_TERM_SCHEMA_VERSION
    assert set(migrations.LONG_TERM_TABLES) <= migrations.table_names(connection)
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (
        migrations.LONG_TERM_SCHEMA_VERSION,
    )


def test_open_store_creates_file_without_touching_chat_schema(tmp_path: Path) -> None:
    path = tmp_path / "data" / "memories.sqlite"
    assert long_term.current_schema_version(path) is None  # 只读检查不建文件

    opened = long_term.open_store(path)
    try:
        assert opened.schema_version == migrations.LONG_TERM_SCHEMA_VERSION
        assert set(migrations.LONG_TERM_TABLES) <= opened.table_names()
        # 长期库与聊天库是两套迁移：这里不应出现聊天库的表
        assert "sessions" not in opened.table_names()
    finally:
        opened.close()

    assert path.is_file()
    assert long_term.current_schema_version(path) == migrations.LONG_TERM_SCHEMA_VERSION


def test_open_store_rejects_chat_database_file(tmp_path: Path) -> None:
    """把长期记忆指向聊天库文件时必须明确失败，而不是留下"表建不出来"的怪状态。"""
    chat_path = tmp_path / "data" / "checkpoints.sqlite"
    database = checkpoint.open_database(chat_path, workspace_root=tmp_path)
    database.connection.close()

    with pytest.raises(MemoryStoreError) as error:
        long_term.open_store(chat_path)

    message = str(error.value)
    assert "长期记忆库缺少表" in message
    assert "独立的数据库文件" in message
    # 失败不应破坏聊天库：会话表与结构版本都还在
    connection = sqlite3.connect(str(chat_path))
    try:
        assert "sessions" in migrations.table_names(connection)
        assert migrations.current_version(connection) == migrations.CHAT_SCHEMA_VERSION
    finally:
        connection.close()


def test_long_term_tables_match_record_contracts(store: long_term.LongTermStore) -> None:
    """建表语句的列必须与 schemas 的 dataclass 字段逐字且同序一致（架构 §10.3）。"""
    expected = {
        "entities": schemas.EntityRecord,
        "facts": schemas.FactRecord,
        "task_conclusions": schemas.TaskConclusionRecord,
    }

    for table, record in expected.items():
        assert _columns(store.connection, table) == [
            field.name for field in fields(record)
        ], f"{table} 的列与 {record.__name__} 不一致"


def test_medium_term_record_follows_architecture_fields() -> None:
    assert [field.name for field in fields(schemas.MediumTermRecord)] == list(
        ARCHITECTURE_MEDIUM_TERM_FIELDS
    )


# --------------------------------------------------------------- 作用域规则


def test_scope_keys_follow_architecture_rules(tmp_path: Path) -> None:
    project_id = scope.project_id_for(tmp_path)

    assert scope.scope_key_for(scope.SCOPE_GLOBAL) == "global"
    assert scope.scope_key_for(scope.SCOPE_USER) == scope.LOCAL_USER_ID
    assert scope.scope_key_for(scope.SCOPE_USER, user_id="  alice ") == "alice"
    assert scope.scope_key_for(scope.SCOPE_PROJECT, project_id=project_id) == project_id
    assert scope.scope_key_for(
        scope.SCOPE_WORKSPACE, workspace_root=tmp_path
    ) == scope.stable_hash(scope.normalize_path(tmp_path))


def test_scope_key_rejects_incomplete_or_unknown_scope() -> None:
    with pytest.raises(MemoryStoreError):
        scope.scope_key_for(scope.SCOPE_PROJECT)  # 缺 project_id
    with pytest.raises(MemoryStoreError):
        scope.scope_key_for(scope.SCOPE_WORKSPACE)  # 缺 workspace_root
    with pytest.raises(MemoryStoreError):
        scope.scope_key_for("session")


def test_projects_and_workspaces_are_isolated(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "src").mkdir(parents=True)
    second.mkdir()

    # 同一个工作区的子目录归到同一个 project（find_git_root 无 Git 时用工作区自身路径）
    assert scope.project_id_for(first) == scope.project_id_for(first)
    assert scope.project_id_for(first) != scope.project_id_for(second)
    assert scope.scope_key_for(
        scope.SCOPE_WORKSPACE, workspace_root=first
    ) != scope.scope_key_for(scope.SCOPE_WORKSPACE, workspace_root=second)


def test_long_term_rows_are_filtered_by_scope(store: long_term.LongTermStore, tmp_path: Path) -> None:
    """T13 只验证结构支持作用域隔离（CRUD 是 T15），因此这里直接用最小 SQL。"""
    project_a = scope.project_id_for(tmp_path / "project-a")
    project_b = scope.project_id_for(tmp_path / "project-b")
    with store.connection:
        store.connection.execute(
            "INSERT INTO entities (id, entity_type, name, scope, scope_key, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("e1", "project", "chaiai", scope.SCOPE_PROJECT, project_a, "now", "now"),
        )

    def names_for(scope_key: str) -> list[str]:
        rows = store.connection.execute(
            "SELECT name FROM entities WHERE scope = ? AND scope_key = ?",
            (scope.SCOPE_PROJECT, scope_key),
        ).fetchall()
        return [str(row[0]) for row in rows]

    assert names_for(project_a) == ["chaiai"]
    assert names_for(project_b) == []


# --------------------------------------------------------------- 不破坏聊天库


def test_chat_database_keeps_old_checkpoints_and_gets_no_memory_tables(
    tmp_path: Path,
) -> None:
    """升级后的聊天库：会话沿用旧 thread_id 历史、结构版本不变、不混入记忆表。"""
    database_path = tmp_path / "data" / "checkpoints.sqlite"
    first = checkpoint.open_database(database_path, workspace_root=tmp_path)
    session = first.sessions.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)
    first.connection.close()

    reopened = checkpoint.open_database(database_path, workspace_root=tmp_path)
    try:
        assert reopened.sessions.get(session.session_id) == session
        assert migrations.current_version(reopened.connection) == migrations.CHAT_SCHEMA_VERSION
        assert not set(migrations.LONG_TERM_TABLES) & migrations.table_names(reopened.connection)
    finally:
        reopened.connection.close()


def test_legacy_v1_database_without_session_index_still_opens(tmp_path: Path) -> None:
    """先建出"旧版库"（只有 Checkpoint 表、sessions 还没有数据），再打开不应报错。"""
    database_path = tmp_path / "data" / "checkpoints.sqlite"
    database_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(str(database_path))
    connection.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT)")
    connection.execute(
        "INSERT INTO checkpoints VALUES (?, ?)", (checkpoint.DEFAULT_THREAD_ID, "")
    )
    connection.commit()
    connection.close()

    database = checkpoint.open_database(database_path, workspace_root=tmp_path)
    try:
        session = database.sessions.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)
        assert session.thread_id == checkpoint.DEFAULT_THREAD_ID
        assert database.sessions.has_checkpoints(checkpoint.DEFAULT_THREAD_ID)
    finally:
        database.connection.close()


# --------------------------------------------------------------- 中期库惰性适配层


def test_medium_term_disabled_does_not_create_index(
    app_config: AppConfig, tmp_path: Path
) -> None:
    config = app_config.memory.medium_term.model_copy(
        update={"path": (tmp_path / "chroma").as_posix()}
    )
    assert config.enabled is False

    with pytest.raises(MemoryDisabledError):
        medium_term.open_store(config)

    assert not (tmp_path / "chroma").exists()
    assert "chromadb" not in sys.modules


def test_medium_term_reports_missing_embedding(app_config: AppConfig) -> None:
    config = app_config.memory.medium_term.model_copy(update={"enabled": True})

    with pytest.raises(MemoryStoreError) as error:
        medium_term.open_store(config)

    assert "embedding" in str(error.value)


@pytest.mark.skipif(medium_term.chromadb_available(), reason="本用例只验证未安装可选依赖时的报错")
def test_medium_term_reports_missing_extra(app_config: AppConfig, tmp_path: Path) -> None:
    config = app_config.memory.medium_term.model_copy(
        update={"enabled": True, "path": (tmp_path / "chroma").as_posix()}
    )

    with pytest.raises(MemoryUnavailableError) as error:
        medium_term.open_store(config, embedding_function=lambda texts: texts)

    assert "uv sync --extra memory" in str(error.value)
    assert not (tmp_path / "chroma").exists()
    assert "chromadb" not in sys.modules


# --------------------------------------------------------------- 初始化脚本


def _load_init_db() -> ModuleType:
    """按路径加载 ``scripts/init_db.py``（``scripts/`` 不是包，不能用 import 语句）。

    必须先登记到 ``sys.modules``：该脚本用了 ``from __future__ import annotations``，
    ``@dataclass`` 解析字符串注解时要按模块名回查 ``sys.modules``，查不到会直接报错。
    """
    name = "local_agent_init_db"
    path = PROJECT_ROOT / "scripts" / "init_db.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture(scope="module")
def init_db() -> ModuleType:
    return _load_init_db()


def _write_config(tmp_path: Path, *, short_term: bool = True, long_term: bool = True) -> Path:
    """写一份只关心路径的临时配置（其余字段用默认值）。"""
    data = (tmp_path / "data").as_posix()
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                "llm:",
                '  base_url: "http://127.0.0.1:9"',
                '  api_key_env: "TEST_KEY"',
                '  model: "test-model"',
                "memory:",
                "  short_term:",
                f"    enabled: {str(short_term).lower()}",
                f'    path: "{data}/checkpoints.sqlite"',
                "  long_term:",
                f"    enabled: {str(long_term).lower()}",
                f'    path: "{data}/memories.sqlite"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_init_db_initializes_then_check_passes(init_db: ModuleType, tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)

    # 还没初始化：--check 必须非零退出，且不能顺手把库建出来
    assert init_db.main(["--config", str(config_path), "--check"]) == init_db.EXIT_NOT_READY
    assert not (tmp_path / "data").exists()

    assert init_db.main(["--config", str(config_path)]) == init_db.EXIT_OK
    assert (tmp_path / "data" / "checkpoints.sqlite").is_file()
    assert (tmp_path / "data" / "memories.sqlite").is_file()

    # 幂等：再跑一次仍然成功，且不改变结构版本
    assert init_db.main(["--config", str(config_path)]) == init_db.EXIT_OK
    assert init_db.main(["--config", str(config_path), "--check"]) == init_db.EXIT_OK
    assert (
        checkpoint.current_schema_version(tmp_path / "data" / "checkpoints.sqlite")
        == migrations.CHAT_SCHEMA_VERSION
    )
    assert (
        long_term.current_schema_version(tmp_path / "data" / "memories.sqlite")
        == migrations.LONG_TERM_SCHEMA_VERSION
    )


def test_init_db_skips_disabled_layers(init_db: ModuleType, tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, short_term=False, long_term=False)

    assert init_db.main(["--config", str(config_path)]) == init_db.EXIT_OK
    assert init_db.main(["--config", str(config_path), "--check"]) == init_db.EXIT_OK

    assert not (tmp_path / "data").exists()


def test_init_db_reports_config_problem(init_db: ModuleType, tmp_path: Path) -> None:
    missing = tmp_path / "no-such-config.yaml"

    assert init_db.main(["--config", str(missing)]) == init_db.EXIT_SETUP_PROBLEM


def test_init_db_reports_long_term_pointing_at_chat_database(
    init_db: ModuleType, tmp_path: Path
) -> None:
    """长期库指向聊天库时，脚本必须给出可读原因而不是堆栈。"""
    config_path = _write_config(tmp_path)
    data = (tmp_path / "data").as_posix()
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f'"{data}/memories.sqlite"', f'"{data}/checkpoints.sqlite"'
        ),
        encoding="utf-8",
    )

    assert init_db.main(["--config", str(config_path)]) == init_db.EXIT_SETUP_PROBLEM


def test_session_store_still_scopes_sessions_per_workspace(tmp_path: Path) -> None:
    """作用域工具搬家后，会话索引的隔离语义不能变（防止重构走样）。"""
    connection = sqlite3.connect(":memory:")
    migrations.apply_migrations(connection)
    store_a = SessionStore(connection, workspace_root=tmp_path / "a")
    store_b = SessionStore(connection, workspace_root=tmp_path / "b")

    session_a = store_a.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)
    session_b = store_b.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)

    assert session_a.project_id == scope.project_id_for(tmp_path / "a")
    assert session_a.project_id != session_b.project_id
    assert session_a.thread_id != session_b.thread_id

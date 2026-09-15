"""T13/T14A 验收：作用域与记忆库初始化、显式 Embedding 适配器（架构 §10.2–§10.3）。

覆盖：

- T13：长期库迁移幂等、字段契约（dataclass ↔ 建表语句）、跨项目/工作区隔离、
  聊天库不被记忆表污染（旧 Checkpoint 仍可恢复）、记忆关闭不建向量索引、
  ``scripts/init_db.py`` 的初始化与 ``--check``。
- T14A：embedding 按 provider 的条件校验、适配器惰性加载与参数透传（cache_dir /
  local_files_only）、L2 归一化与零向量、缺依赖与缺缓存时的可读错误。

全部使用临时目录与临时 SQLite；不依赖网络，也不加载真实模型：``fastembed`` 用假模块替身，
``chromadb`` 不会被导入。
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from local_agent import scope
from local_agent.config import (
    AppConfig,
    ConfigError,
    EmbeddingConfig,
    MediumTermMemoryConfig,
    resolve_api_key,
)
from local_agent.exceptions import (
    EmbeddingLoadError,
    EmbeddingUnavailableError,
    MemoryDisabledError,
    MemoryStoreError,
    MemoryUnavailableError,
)
from local_agent.llm import embedding as embedding_module
from local_agent.memory import long_term, medium_term, recall, schemas
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


# --------------------------------------------------------------- T14A Embedding 适配器


class FakeVector:
    """替身向量：只有 ``tolist()``，用来模拟 FastEmbed 返回的 numpy 数组。"""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return list(self._values)


class FakeTextEmbedding:
    """假 FastEmbed ``TextEmbedding``：记录构造参数，返回确定性向量。"""

    calls: ClassVar[list[dict[str, Any]]] = []
    fail_with: ClassVar[str | None] = None

    def __init__(self, *, model_name: str, cache_dir: str, local_files_only: bool = False) -> None:
        if type(self).fail_with is not None:
            raise ValueError(type(self).fail_with)
        type(self).calls.append(
            {"model_name": model_name, "cache_dir": cache_dir, "local_files_only": local_files_only}
        )
        self.model_name = model_name

    @staticmethod
    def list_supported_models() -> list[dict[str, Any]]:
        return [{"model": "BAAI/bge-small-zh-v1.5", "dim": 512}]

    def embed(self, documents: list[str], **_kwargs: Any) -> list[FakeVector]:
        return [FakeVector([3.0, 4.0]) for _ in documents]

    def query_embed(self, query: list[str], **_kwargs: Any) -> list[FakeVector]:
        return [FakeVector([0.0, 0.0]) for _ in query]

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.fail_with = None


class FakeEmbedding:
    """确定性假 Embedding：T14B 的召回测试直接复用（不联网、不加载模型、不做下载）。"""

    def __init__(self, dimension: int = 4) -> None:
        self._dimension = dimension

    @property
    def info(self) -> embedding_module.EmbeddingInfo:
        return embedding_module.EmbeddingInfo(
            provider="fake",
            model="fake",
            dimension=self._dimension,
            normalized=True,
            cache_dir=None,
            local_files_only=False,
        )

    def name(self) -> str:
        return "fake-embedding"

    def get_config(self) -> dict[str, Any]:
        return {"provider": "fake", "model": "fake"}

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        return self.embed_documents(input)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, texts: str | Sequence[str]) -> list[list[float]]:
        items = [texts] if isinstance(texts, str) else list(texts)
        return [self._vector(text) for text in items]

    def prepare(self) -> None:
        """假实现没有加载阶段。"""

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [byte / 255.0 for byte in digest[: self._dimension]]
        norm = math.sqrt(sum(value * value for value in raw)) or 1.0
        return [value / norm for value in raw]


@pytest.fixture
def fake_fastembed(monkeypatch: pytest.MonkeyPatch) -> type[FakeTextEmbedding]:
    """把适配器的 fastembed 导入替换成假模块（测试不联网、不加载真实模型）。"""
    FakeTextEmbedding.reset()
    monkeypatch.setattr(embedding_module, "_import_fastembed", lambda: FakeTextEmbedding)
    return FakeTextEmbedding


def _onnx_config(tmp_path: Path, **overrides: Any) -> EmbeddingConfig:
    """一份合法的 onnx_local 配置；用例只覆盖关心的字段。"""
    values: dict[str, Any] = {
        "enabled": True,
        "provider": "onnx_local",
        "model": "BAAI/bge-small-zh-v1.5",
        "cache_dir": (tmp_path / "models").as_posix(),
        "local_files_only": False,
    }
    values.update(overrides)
    return EmbeddingConfig(**values)


def test_embedding_config_requires_provider_specific_fields() -> None:
    with pytest.raises(ValidationError) as onnx_error:
        EmbeddingConfig(enabled=True, provider="onnx_local")
    onnx_message = str(onnx_error.value)
    assert "model" in onnx_message
    assert "cache_dir" in onnx_message

    with pytest.raises(ValidationError) as api_error:
        EmbeddingConfig(enabled=True, provider="openai_compatible")
    api_message = str(api_error.value)
    for name in ("base_url", "api_key_env", "model"):
        assert name in api_message


def test_embedding_config_rejects_fields_of_other_provider() -> None:
    for field, value in (("base_url", "https://api.example.com/v1"), ("api_key_env", "SOME_KEY")):
        with pytest.raises(ValidationError) as error:
            EmbeddingConfig(provider="onnx_local", **{field: value})
        assert field in str(error.value)

    for field, value in (("cache_dir", "E:/models"), ("local_files_only", True)):
        with pytest.raises(ValidationError) as error:
            EmbeddingConfig(provider="openai_compatible", **{field: value})
        assert field in str(error.value)

    # 关掉开关也不能把字段错误藏起来（开发文档 §8：不得静默忽略不适用的字段）
    with pytest.raises(ValidationError):
        EmbeddingConfig(enabled=False, provider="onnx_local", base_url="https://api.example.com/v1")


def test_embedding_config_survives_dump_and_validate(app_config: AppConfig) -> None:
    """配置对象必须能无损往返：``model_dump()`` 会把空串占位也写出来。

    这条兼作回归网：第一版校验把 ``base_url=""`` 也当成"显式使用不适用字段"，直接让
    25 个走 ``model_dump()`` → ``model_validate()`` 的既有用例失败。
    """
    data = app_config.model_dump()
    assert "base_url" in data["embedding"]  # dump 会写出空串占位

    assert AppConfig.model_validate(data) == app_config

    # 空占位（或等于默认值的显式值）不算冲突；有内容的照样拦
    assert EmbeddingConfig(provider="onnx_local", base_url="", api_key_env="").provider == "onnx_local"
    assert (
        EmbeddingConfig(
            provider="openai_compatible", cache_dir="", local_files_only=False
        ).provider
        == "openai_compatible"
    )


def test_embedding_cache_dir_must_be_absolute_when_enabled(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as error:
        EmbeddingConfig(enabled=True, provider="onnx_local", model="m", cache_dir="data/models")
    assert "绝对路径" in str(error.value)

    absolute = (tmp_path / "models").as_posix()
    config = EmbeddingConfig(enabled=True, provider="onnx_local", model="m", cache_dir=absolute)
    assert config.cache_dir == absolute

    # 关闭的层不该因为示例里的机器相关路径（如 POSIX 上的 "E:/…"）连配置都加载不了
    disabled = EmbeddingConfig(enabled=False, provider="onnx_local", model="m", cache_dir="E:/hf-cache/x")
    assert disabled.cache_dir == "E:/hf-cache/x"


def test_medium_term_requires_enabled_embedding(app_config: AppConfig) -> None:
    """中期记忆打开但没有 embedding：配置阶段就失败闭合，别等每轮对话才报错。"""
    data = app_config.model_dump()
    data["memory"]["medium_term"]["enabled"] = True

    with pytest.raises(ValidationError) as error:
        AppConfig.model_validate(data)

    assert "embedding.enabled" in str(error.value)


def test_example_config_keeps_embedding_disabled(app_config: AppConfig) -> None:
    """示例配置必须保持关闭：加载它不得导入 fastembed、不得触发下载。"""
    config = app_config.embedding

    assert config.enabled is False
    assert app_config.memory.medium_term.enabled is False
    assert config.provider == "onnx_local"
    assert Path(config.cache_dir).is_absolute()

    with pytest.raises(EmbeddingUnavailableError):
        embedding_module.build_embedding_function(config)


def test_onnx_adapter_is_lazy_and_passes_cache_dir(
    fake_fastembed: type[FakeTextEmbedding], tmp_path: Path
) -> None:
    """契约测试：真参数传到 FastEmbed（避免"批准了却没用上"的静默偏差）。"""
    adapter = embedding_module.build_embedding_function(
        _onnx_config(tmp_path, local_files_only=True)
    )

    assert fake_fastembed.calls == []  # 构造阶段不加载模型（首次加载可能下载）
    assert adapter.info.dimension == 512  # 维度来自注册表，不需要加载模型
    assert adapter.info.normalized is True
    assert adapter.info.cache_dir == (tmp_path / "models").as_posix()

    vectors = adapter(["第一条", "第二条"])

    assert len(vectors) == 2
    for vector in vectors:  # [3, 4] 被 L2 归一化
        assert vector == pytest.approx([0.6, 0.8])
    assert fake_fastembed.calls == [
        {
            "model_name": "BAAI/bge-small-zh-v1.5",
            "cache_dir": (tmp_path / "models").as_posix(),
            "local_files_only": True,
        }
    ]

    adapter(["再来一次"])
    assert len(fake_fastembed.calls) == 1  # 模型实例复用，不重复构造

    assert adapter.embed_query("查询") == [[0.0, 0.0]]  # 零向量原样返回，不除零
    assert adapter([]) == []


def test_onnx_adapter_reports_readable_error(
    fake_fastembed: type[FakeTextEmbedding], tmp_path: Path
) -> None:
    fake_fastembed.fail_with = "Could not load model BAAI/bge-small-zh-v1.5 from any source."
    adapter = embedding_module.build_embedding_function(
        _onnx_config(tmp_path, local_files_only=True)
    )

    with pytest.raises(EmbeddingLoadError) as error:
        adapter.prepare()

    message = str(error.value)
    assert "BAAI/bge-small-zh-v1.5" in message
    assert (tmp_path / "models").as_posix() in message
    assert "local_files_only=true" in message
    assert "不会联网" in message
    assert "ValueError" not in message  # 不给用户看裸异常名


def test_onnx_adapter_hint_differs_when_download_allowed(
    fake_fastembed: type[FakeTextEmbedding], tmp_path: Path
) -> None:
    fake_fastembed.fail_with = "boom"
    adapter = embedding_module.build_embedding_function(_onnx_config(tmp_path))

    with pytest.raises(EmbeddingLoadError) as error:
        adapter.embed_documents(["文本"])

    message = str(error.value)
    assert "网络" in message
    assert "不会回退" in message


def test_onnx_adapter_reports_missing_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """未装 memory extra 时：构造仍可用（元信息不炸），准备/调用给可读安装提示。"""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    adapter = embedding_module.build_embedding_function(_onnx_config(tmp_path))

    assert adapter.info.dimension is None  # 读不到注册表也不该构造失败

    with pytest.raises(EmbeddingUnavailableError) as error:
        adapter.prepare()

    assert "uv sync --extra memory" in str(error.value)


def test_build_embedding_function_requires_key_only_for_api_provider(tmp_path: Path) -> None:
    api_config = EmbeddingConfig(
        enabled=True,
        provider="openai_compatible",
        base_url="http://127.0.0.1:9/v1",
        api_key_env="EMBEDDING_API_KEY",
        model="text-embedding-3-small",
    )

    with pytest.raises(ConfigError):
        embedding_module.build_embedding_function(api_config)

    adapter = embedding_module.build_embedding_function(api_config, api_key="test-key")
    assert adapter.info.provider == "openai_compatible"
    assert adapter.info.cache_dir is None
    assert adapter.info.dimension is None  # 远端维度由 API 决定，不猜

    with pytest.raises(ConfigError):
        embedding_module.build_embedding_function(_onnx_config(tmp_path), api_key="多余的 Key")


def test_resolve_api_key_works_for_embedding_config() -> None:
    config = EmbeddingConfig(
        enabled=True,
        provider="openai_compatible",
        base_url="http://127.0.0.1:9/v1",
        api_key_env="EMBEDDING_API_KEY",
        model="text-embedding-3-small",
    )

    assert resolve_api_key(config, environ={"EMBEDDING_API_KEY": "secret"}) == "secret"

    with pytest.raises(ConfigError) as error:
        resolve_api_key(config, environ={})
    assert "EMBEDDING_API_KEY" in str(error.value)


def test_fake_embedding_is_deterministic_and_normalized() -> None:
    """T14B 复用的假 Embedding：确定性、归一化、查询与文档一致。"""
    fake = FakeEmbedding(dimension=4)

    assert fake.info.dimension == 4
    documents = fake(["同一段文本"])
    assert documents == fake.embed_query("同一段文本")
    assert math.sqrt(sum(value * value for value in documents[0])) == pytest.approx(1.0)


# --------------------------------------------------------------- T14B 中期记忆存储与召回

#: 测试用的固定时间：新建记忆用这个，便于计算时间衰减。
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat(timespec="seconds")

#: "无关"文本的向量（与查询向量正交 → 余弦相似度 0）。
FAR_VECTOR = [0.0, 0.0, 1.0]

#: 查询文本与其向量（同一向量 → 相似度 1）。
QUERY_TEXT = "项目里保存了什么记忆"
CLOSE_VECTOR = [1.0, 0.0, 0.0]


class ScriptedEmbedding:
    """按文本查表给向量的假 Embedding：用来精确控制相似度（不联网、不加载模型）。

    未登记的文本一律给 :data:`FAR_VECTOR`，所以"无关记忆"天然落在 ``min_score`` 之下。
    """

    def __init__(self, vectors: dict[str, list[float]] | None = None, *, dimension: int = 3) -> None:
        self._vectors = dict(vectors or {})
        self._dimension = dimension

    @property
    def info(self) -> embedding_module.EmbeddingInfo:
        return embedding_module.EmbeddingInfo(
            provider="fake",
            model="scripted",
            dimension=self._dimension,
            normalized=True,
            cache_dir=None,
            local_files_only=False,
        )

    def name(self) -> str:
        return "fake-scripted"

    def get_config(self) -> dict[str, Any]:
        return {"provider": "fake", "model": "scripted"}

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        return self.embed_documents(input)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(self._vectors.get(text, FAR_VECTOR)) for text in texts]

    def embed_query(self, texts: str | Sequence[str]) -> list[list[float]]:
        items = [texts] if isinstance(texts, str) else list(texts)
        return [list(self._vectors.get(text, CLOSE_VECTOR)) for text in items]

    def prepare(self) -> None:
        """假实现没有加载阶段。"""


class ScriptedEmbeddingWithInfo(ScriptedEmbedding):
    """同上，但把 ``info`` 换成自定义元信息（用来模拟"换过 embedding 模型/维度"）。"""

    def __init__(self, vectors: dict[str, list[float]], info: Any) -> None:
        super().__init__(vectors)
        self._info = info

    @property
    def info(self) -> Any:
        return self._info


class CountingEmbedding(ScriptedEmbedding):
    """记录 ``embed_documents`` 被调用了几次（验证向量由适配器统一计算）。"""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        super().__init__(vectors)
        self.document_calls = 0

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        return super().embed_documents(texts)


class WrongDimensionEmbedding(ScriptedEmbedding):
    """对特定内容吐出错误维度（用来触发 Chroma 的维度校验）。"""

    WRONG_TEXT = "错维度"

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [
            [1.0, 0.0] if text == self.WRONG_TEXT else [1.0, 0.0, 0.0] for text in texts
        ]


class UnknownDimensionEmbedding(ScriptedEmbedding):
    """维度既不在元信息里、也探测不出来（用来验证"没维度就拒绝建 collection"）。"""

    def __init__(self) -> None:
        super().__init__({})

    @property
    def info(self) -> Any:
        return embedding_module.EmbeddingInfo(
            provider="fake",
            model="unknown-dimension",
            dimension=None,
            normalized=True,
            cache_dir=None,
            local_files_only=False,
        )

    def resolve_dimension(self) -> int | None:
        return None

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


def _memory_config(tmp_path: Path, **overrides: Any) -> MediumTermMemoryConfig:
    """一份可用的中期记忆配置（collection 落在临时目录）。"""
    values: dict[str, Any] = {
        "enabled": True,
        "backend": "chroma",
        "path": (tmp_path / "chroma").as_posix(),
        "collection": "test_memory",
        "top_k": 6,
        "min_score": 0.5,
        "decay_half_life_days": 30,
    }
    values.update(overrides)
    return MediumTermMemoryConfig(**values)


def _project_record(
    memory_id: str, content: str, *, project_id: str, **overrides: Any
) -> schemas.MediumTermRecord:
    """一条 ``project`` 作用域的记忆（默认重要度/置信度 0.5、无过期）。"""
    values: dict[str, Any] = {
        "id": memory_id,
        "content": content,
        "scope": scope.SCOPE_PROJECT,
        "scope_key": project_id,
        "importance": 0.5,
        "confidence": 0.5,
        "created_at": NOW_ISO,
        "status": schemas.MEDIUM_TERM_STATUS_ACTIVE,
    }
    values.update(overrides)
    return schemas.MediumTermRecord(**values)


@pytest.fixture
def memory_store(tmp_path: Path) -> Iterator[medium_term.MediumTermStore]:
    """可用的 Chroma 库（用假 Embedding，不加载真实模型、不联网）。

    Chroma 的 ``PersistentClient`` 没有 ``close()``（1.5.9 实测），所以这里不显式清理：
    临时目录里的句柄会随进程结束释放，既有测试同样是这个模式。
    """
    store = medium_term.open_store(
        _memory_config(tmp_path),
        embedding_function=ScriptedEmbedding({QUERY_TEXT: CLOSE_VECTOR}),
    )
    yield store


def _recall(
    store: medium_term.MediumTermStore,
    *,
    config: MediumTermMemoryConfig,
    workspace: Path,
    query: str = QUERY_TEXT,
    **overrides: Any,
) -> list[Any]:
    return recall.recall_memory(
        store,
        query=query,
        config=config,
        workspace_root=workspace,
        **overrides,
    )


def test_medium_term_round_trip_and_metadata(memory_store, tmp_path: Path) -> None:
    """写入/读回：可选字段为空时不写进向量库记录（架构 §10.2 末段）。"""
    project_id = scope.project_id_for(tmp_path)
    memory_store.upsert(
        [
            _project_record("mem-1", "用户偏好用中文回答", project_id=project_id),
            _project_record(
                "mem-2",
                "项目用 uv 管理依赖",
                project_id=project_id,
                user_id=scope.LOCAL_USER_ID,
                source_thread_id="thread-x",
                expires_at="2026-12-31T00:00:00+00:00",
            ),
        ]
    )

    assert memory_store.count() == 2

    stored = memory_store.collection.get(ids=["mem-1", "mem-2"], include=["metadatas"])
    first, second = stored["metadatas"]

    assert first["scope"] == scope.SCOPE_PROJECT
    assert first["scope_key"] == project_id
    assert first["created_at"] == NOW_ISO
    assert first["status"] == schemas.MEDIUM_TERM_STATUS_ACTIVE
    assert "user_id" not in first  # 空值不写
    assert "expires_at" not in first
    assert second["user_id"] == scope.LOCAL_USER_ID
    assert second["source_thread_id"] == "thread-x"
    assert second["expires_at"] == "2026-12-31T00:00:00+00:00"


def test_medium_term_query_filters_by_scope_and_status(memory_store, tmp_path: Path) -> None:
    project_id = scope.project_id_for(tmp_path)
    other_project = scope.project_id_for(tmp_path / "other")
    memory_store.upsert(
        [
            _project_record("mine", QUERY_TEXT, project_id=project_id, importance=0.9),
            _project_record("theirs", QUERY_TEXT, project_id=other_project),
            _project_record(
                "global", QUERY_TEXT, project_id=project_id, scope=scope.SCOPE_GLOBAL, scope_key="global"
            ),
            _project_record(
                "deleted",
                QUERY_TEXT,
                project_id=project_id,
                status=schemas.MEDIUM_TERM_STATUS_DELETED,
            ),
        ]
    )

    references = _recall(
        memory_store, config=_memory_config(tmp_path), workspace=tmp_path, now=NOW
    )
    ids = [reference["memory_id"] for reference in references]

    assert ids == ["mine", "global"]  # 别的项目与软删除的都看不到
    assert references[0]["scope"] == scope.SCOPE_PROJECT


def test_recall_drops_expired_low_score_and_unreadable_expiry(
    memory_store, tmp_path: Path
) -> None:
    project_id = scope.project_id_for(tmp_path)
    memory_store.upsert(
        [
            _project_record("ok", QUERY_TEXT, project_id=project_id),
            _project_record("expired", QUERY_TEXT, project_id=project_id, expires_at="2026-01-01T00:00:00+00:00"),
            _project_record("unparsable", QUERY_TEXT, project_id=project_id, expires_at="不是时间"),
            _project_record("far", "与查询无关的内容", project_id=project_id),
        ]
    )

    references = _recall(
        memory_store, config=_memory_config(tmp_path), workspace=tmp_path, now=NOW
    )

    assert [reference["memory_id"] for reference in references] == ["ok"]


def test_recall_scores_importance_and_time(memory_store, tmp_path: Path) -> None:
    """排序分数 = 相似度 × 作用域 × 重要度 × 时间衰减 × 置信度（架构 §10.2）。"""
    project_id = scope.project_id_for(tmp_path)
    memory_store.upsert(
        [
            _project_record("recent", QUERY_TEXT, project_id=project_id, importance=0.9),
            _project_record(
                "old",
                QUERY_TEXT,
                project_id=project_id,
                importance=0.9,
                created_at=datetime(2025, 3, 1, tzinfo=timezone.utc).isoformat(timespec="seconds"),
            ),
            _project_record("low_importance", QUERY_TEXT, project_id=project_id, importance=0.1),
        ]
    )

    references = _recall(
        memory_store, config=_memory_config(tmp_path), workspace=tmp_path, now=NOW
    )
    ids = [reference["memory_id"] for reference in references]

    assert ids == ["recent", "low_importance", "old"]
    scores = [reference["score"] for reference in references]
    assert scores == sorted(scores, reverse=True)
    assert references[0]["similarity"] == pytest.approx(1.0)
    assert references[0]["score"] <= references[0]["similarity"]  # 系数只减不增


def test_recall_respects_top_k(memory_store, tmp_path: Path) -> None:
    project_id = scope.project_id_for(tmp_path)
    memory_store.upsert(
        [
            _project_record(f"mem-{index}", QUERY_TEXT, project_id=project_id)
            for index in range(5)
        ]
    )

    references = _recall(
        memory_store,
        config=_memory_config(tmp_path, top_k=2),
        workspace=tmp_path,
        now=NOW,
    )

    assert len(references) == 2


def test_recall_returns_nothing_for_empty_query(memory_store, tmp_path: Path) -> None:
    project_id = scope.project_id_for(tmp_path)
    memory_store.upsert([_project_record("mem", QUERY_TEXT, project_id=project_id)])

    assert _recall(memory_store, config=_memory_config(tmp_path), workspace=tmp_path, query="  ") == []


def test_visible_scope_keys_cover_four_scopes(tmp_path: Path) -> None:
    keys = recall.visible_scope_keys(user_id=None, project_id="p1", workspace_root=tmp_path)

    assert [item[0] for item in keys] == [
        scope.SCOPE_GLOBAL,
        scope.SCOPE_USER,
        scope.SCOPE_PROJECT,
        scope.SCOPE_WORKSPACE,
    ]
    assert keys[0][1] == "global"
    assert keys[1][1] == scope.LOCAL_USER_ID
    assert keys[2][1] == "p1"
    assert keys[3][1] == scope.stable_hash(scope.normalize_path(tmp_path))


def test_open_store_rejects_incompatible_collection(tmp_path: Path) -> None:
    """换过 embedding 模型/维度后必须拒绝复用旧 collection 并提示重建（架构 §10.2、§11.6）。"""
    config = _memory_config(tmp_path)
    first = medium_term.open_store(
        config, embedding_function=ScriptedEmbedding({QUERY_TEXT: CLOSE_VECTOR})
    )
    first.upsert(
        [
            _project_record(
                "mem", QUERY_TEXT, project_id=scope.project_id_for(tmp_path)
            )
        ]
    )

    other_model = embedding_module.EmbeddingInfo(
        provider="fake",
        model="另一个模型",
        dimension=3,
        normalized=True,
        cache_dir=None,
        local_files_only=False,
    )
    with pytest.raises(MemoryStoreError) as error:
        medium_term.open_store(
            config,
            embedding_function=ScriptedEmbeddingWithInfo({QUERY_TEXT: CLOSE_VECTOR}, other_model),
        )

    message = str(error.value)
    assert "不兼容" in message
    assert "embedding_model" in message
    assert "重建索引" in message


def test_upsert_reports_dimension_mismatch(tmp_path: Path) -> None:
    """维度不一致时要给出"换过模型就重建"的可读错误，而不是裸 Chroma 异常。

    Chroma 只在 collection 里已经有向量之后才校验维度，所以先写一条合法记录。
    这里用一个"元信息说 3 维、实际吐 2 维向量"的适配器来触发 Chroma 的维度错误
    （自带向量的情况另有用例，那条会先被来源校验拦下）。
    """
    project_id = scope.project_id_for(tmp_path)
    store = medium_term.open_store(
        _memory_config(tmp_path),
        embedding_function=WrongDimensionEmbedding({QUERY_TEXT: CLOSE_VECTOR}),
    )
    store.upsert([_project_record("ok", QUERY_TEXT, project_id=project_id)])

    with pytest.raises(MemoryStoreError) as error:
        store.upsert(
            [
                _project_record(
                    "wrong-dim", WrongDimensionEmbedding.WRONG_TEXT, project_id=project_id
                )
            ]
        )

    message = str(error.value)
    assert "写入失败" in message
    assert "重建索引" in message


def test_medium_term_delete_removes_records(memory_store, tmp_path: Path) -> None:
    project_id = scope.project_id_for(tmp_path)
    memory_store.upsert([_project_record("mem", QUERY_TEXT, project_id=project_id)])

    assert memory_store.delete(["mem"]) == 1
    assert memory_store.count() == 0


def test_upsert_rejects_foreign_vector(tmp_path: Path) -> None:
    """同维度但来源不明的预计算向量必须被拒（架构 §10.2：不得混用不同模型的向量）。

    只靠 collection 元数据检测不出这种混淆（维度可能完全一样），所以必须在写入侧拦。
    """
    store = medium_term.open_store(
        _memory_config(tmp_path), embedding_function=ScriptedEmbedding({QUERY_TEXT: CLOSE_VECTOR})
    )
    project_id = scope.project_id_for(tmp_path)

    with pytest.raises(MemoryStoreError) as error:
        store.upsert(
            [
                _project_record(
                    "foreign", QUERY_TEXT, project_id=project_id, embedding=[0.0, 0.0, 1.0]
                )
            ]
        )

    assert "自带的向量" in str(error.value)
    assert store.count() == 0


def test_upsert_computes_vectors_even_when_record_carries_one(tmp_path: Path) -> None:
    """自带向量与适配器算出来的一致时接受（等价于冗余），但向量始终由适配器计算。"""
    embedding = CountingEmbedding({QUERY_TEXT: CLOSE_VECTOR})
    store = medium_term.open_store(_memory_config(tmp_path), embedding_function=embedding)
    project_id = scope.project_id_for(tmp_path)

    store.upsert(
        [
            _project_record(
                "with-vector", QUERY_TEXT, project_id=project_id, embedding=list(CLOSE_VECTOR)
            )
        ]
    )

    assert embedding.document_calls == 1  # 仍然走适配器，不信任传进来的向量
    assert store.count() == 1


def test_collection_uses_explicit_cosine_space(tmp_path: Path) -> None:
    """距离度量必须真的设成 cosine。

    这里是复审抓到的真回归：只写我们自己的 ``embedding_distance`` 键不生效，Chroma 会退回默认
    ``l2``，于是 ``1 - distance`` 换算出来的根本不是余弦相似度。用**非正交**向量验证——45° 夹角
    的余弦相似度是 0.7071，而默认 l2（平方距离）给的是 0.586，只测"完全相同/完全正交"发现不了。
    """
    vector_45 = [math.sqrt(0.5), math.sqrt(0.5)]
    config = _memory_config(tmp_path, min_score=0.0)
    store = medium_term.open_store(
        config,
        embedding_function=ScriptedEmbedding({"查询": [1.0, 0.0], "接近": vector_45}, dimension=2),
    )
    project_id = scope.project_id_for(tmp_path)
    store.upsert([_project_record("close", "接近", project_id=project_id)])

    assert medium_term.configured_distance_space(store.collection) == "cosine"

    hits = store.query(
        query_vector=store.embed_query("查询"),
        scope_keys=recall.visible_scope_keys(
            user_id=None, project_id=project_id, workspace_root=tmp_path
        ),
        n_results=1,
    )
    assert hits[0].distance == pytest.approx(1 - math.sqrt(0.5), abs=1e-4)

    references = recall.recall_memory(
        store, query="查询", config=config, workspace_root=tmp_path, project_id=project_id, now=NOW
    )
    assert references[0]["similarity"] == pytest.approx(math.sqrt(0.5), abs=1e-4)


def test_open_store_rejects_collection_from_older_schema(tmp_path: Path) -> None:
    """旧版本建的 collection（没有距离度量/维度记录）必须失效并提示重建。"""
    import chromadb

    config = _memory_config(tmp_path)
    client = chromadb.PersistentClient(path=config.path)
    client.get_or_create_collection(
        name=config.collection,
        embedding_function=ScriptedEmbedding({QUERY_TEXT: CLOSE_VECTOR}),
        metadata={"memory_schema_version": 1},  # 1 = 距离度量修正之前的版本
    )

    with pytest.raises(MemoryStoreError) as error:
        medium_term.open_store(
            config, embedding_function=ScriptedEmbedding({QUERY_TEXT: CLOSE_VECTOR})
        )

    message = str(error.value)
    assert "不兼容" in message
    assert "重建索引" in message


def test_open_store_rejects_collection_bound_to_other_embedding_function(tmp_path: Path) -> None:
    """被别的 embedding 函数绑定的 collection（例如 Chroma 默认 EF）要给出可读的拒绝原因。"""
    import chromadb

    config = _memory_config(tmp_path)
    client = chromadb.PersistentClient(path=config.path)
    client.get_or_create_collection(name=config.collection)  # 用 Chroma 默认 EF 建

    with pytest.raises(MemoryStoreError) as error:
        medium_term.open_store(
            config, embedding_function=ScriptedEmbedding({QUERY_TEXT: CLOSE_VECTOR})
        )

    message = str(error.value)
    assert "打开中期记忆 collection" in message
    assert "重建索引" in message


def test_open_store_requires_dimension(tmp_path: Path) -> None:
    """维度拿不到就拒绝建 collection：没有维度就无法校验"不混用不同模型向量"。"""
    with pytest.raises(MemoryStoreError) as error:
        medium_term.open_store(
            _memory_config(tmp_path), embedding_function=UnknownDimensionEmbedding()
        )

    assert "维度" in str(error.value)


def test_prepare_hf_cache_keeps_auxiliary_cache_inside_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HuggingFace 的 hub/xet 缓存必须落进显式 cache_dir。

    只把 ``cache_dir`` 传给 FastEmbed 管不住它们（实测默认落在用户目录，Windows 上就是 C 盘）。
    """
    for name in embedding_module._HF_CACHE_ENV:
        monkeypatch.delenv(name, raising=False)
    cache_dir = tmp_path / "models"

    plan = embedding_module.prepare_hf_cache(cache_dir)

    assert plan.paths["HF_HOME"] == (cache_dir / "hf-home").as_posix()
    assert plan.paths["HF_HUB_CACHE"] == (cache_dir / "hf-home" / "hub").as_posix()
    assert plan.paths["HF_XET_CACHE"] == (cache_dir / "hf-home" / "xet").as_posix()
    assert plan.conflicts == ()  # 没有外部设置可覆盖
    assert os.environ["HF_XET_CACHE"] == plan.paths["HF_XET_CACHE"]


def test_prepare_hf_cache_overrides_external_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """外部已设 ``HF_*`` 时必须**覆盖**：配置目录是唯一真值。

    ``setdefault`` 会让外部值生效，附属缓存就写到 ``cache_dir`` 之外（最典型是 C 盘），页面展示
    的目标目录也会与实际落点不符。被覆盖的值要记进 ``conflicts`` 供展示。
    """
    monkeypatch.setenv("HF_HOME", "E:/external-hf")
    monkeypatch.setenv("HF_HUB_CACHE", "E:/external-hf/hub")
    monkeypatch.delenv("HF_XET_CACHE", raising=False)
    cache_dir = tmp_path / "models"

    plan = embedding_module.prepare_hf_cache(cache_dir)

    assert plan.paths["HF_HOME"] == (cache_dir / "hf-home").as_posix()
    assert os.environ["HF_HOME"] == (cache_dir / "hf-home").as_posix()
    assert "HF_HOME=E:/external-hf" in plan.conflicts
    assert "HF_HUB_CACHE=E:/external-hf/hub" in plan.conflicts
    assert all(value.startswith(cache_dir.as_posix()) for value in plan.paths.values())


def test_is_under_accepts_subdirectories_only(tmp_path: Path) -> None:
    """缓存落点判定：真子目录（含自身）算在内，同名兄弟目录不算。"""
    cache = tmp_path / "cache"
    (cache / "hub").mkdir(parents=True)
    sibling = tmp_path / "cache-other"
    sibling.mkdir()

    assert embedding_module._is_under((cache / "hub").as_posix(), cache.as_posix())
    assert embedding_module._is_under(str(cache / "hub"), str(cache))
    assert embedding_module._is_under(str(cache), str(cache))
    assert not embedding_module._is_under(str(sibling), str(cache))


@pytest.mark.skipif(os.name == "nt", reason="POSIX 才区分大小写；Windows 语义见下一个用例")
def test_is_under_is_case_sensitive_on_posix() -> None:
    """大小写不同就是不同目录：转小写 + 字符串前缀会把配置外的目录判成安全（复审复现）。"""
    assert embedding_module._is_under("/tmp/cache/hub", "/tmp/cache")
    assert not embedding_module._is_under("/tmp/Cache/hub", "/tmp/cache")


@pytest.mark.skipif(os.name != "nt", reason="Windows 路径大小写与分隔符语义")
def test_is_under_on_windows_ignores_case_and_separators(tmp_path: Path) -> None:
    """Windows 上大小写与分隔符不敏感，但仍必须是真子目录（不能靠前缀混进来）。"""
    cache = tmp_path / "cache"
    (cache / "hub").mkdir(parents=True)

    assert embedding_module._is_under(str(cache / "hub"), str(cache).replace("\\", "/"))
    assert embedding_module._is_under(str(cache / "hub").upper(), str(cache))
    assert not embedding_module._is_under(str(tmp_path / "cache2"), str(cache))


def test_is_under_rejects_dotdot_escape(tmp_path: Path) -> None:
    """``..`` 不能靠字符串前缀逃逸：归一化后必须真的在目录之下（复审复现）。"""
    cache = tmp_path / "cache"
    cache.mkdir()
    (tmp_path / "outside").mkdir()

    assert not embedding_module._is_under((cache / ".." / "outside").as_posix(), cache.as_posix())
    assert not embedding_module._is_under("/tmp/cache/../outside", "/tmp/cache")


def test_onnx_adapter_sets_hf_cache_at_construction(
    fake_fastembed: type[FakeTextEmbedding],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """HF 附属缓存必须在**构造阶段**就指向 cache_dir。

    这里不能等到 prepare()：构造时要读模型注册表，那一步就会 import fastembed（连带
    huggingface_hub），而 hf_hub 的缓存常量是导入时读环境变量的——晚设一步就落到用户默认目录
    （复审实测到 C 盘被写）。

    用假 fastembed（``fake_fastembed`` 夹具会重置类级状态），避免真的去加载模型。
    """
    for name in embedding_module._HF_CACHE_ENV:
        monkeypatch.delenv(name, raising=False)

    adapter = embedding_module.build_embedding_function(_onnx_config(tmp_path))

    expected = (tmp_path / "models").as_posix()
    assert expected in os.environ["HF_HOME"]
    assert expected in os.environ["HF_HUB_CACHE"]
    assert expected in os.environ["HF_XET_CACHE"]
    assert adapter.hf_cache_conflicts == ()

    adapter.prepare()  # 幂等，不改变已设置的路径
    assert expected in os.environ["HF_HUB_CACHE"]


def test_onnx_adapter_fails_when_huggingface_hub_cache_is_already_frozen(
    fake_fastembed: type[FakeTextEmbedding],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``huggingface_hub`` 被提前导入且常量指向别处时，必须明确失败。

    常量在导入时固化，改环境变量也搬不回来；此时若继续加载模型，附属缓存就会写到配置之外的
    位置（复审要求的第二个回归用例）。
    """
    from huggingface_hub import constants  # 子模块导入后才会出现在 sys.modules 里

    monkeypatch.setattr(constants, "HF_HUB_CACHE", "C:/Users/someone/.cache/huggingface/hub")
    adapter = embedding_module.build_embedding_function(_onnx_config(tmp_path))

    with pytest.raises(EmbeddingLoadError) as error:
        adapter.prepare()

    message = str(error.value)
    assert "已在本进程导入时固化" in message
    assert "HF_HUB_CACHE" in message
    assert (tmp_path / "models").as_posix() in message


def test_onnx_adapter_accepts_huggingface_hub_frozen_at_cache_dir(
    fake_fastembed: type[FakeTextEmbedding],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """常量本来就落在 ``cache_dir`` 下时不得误报（同一进程复用同一配置的正常路径）。"""
    from huggingface_hub import constants  # 子模块导入后才会出现在 sys.modules 里

    cache_dir = tmp_path / "models"
    home = cache_dir / "hf-home"
    monkeypatch.setattr(constants, "HF_HOME", home.as_posix())
    monkeypatch.setattr(constants, "HF_HUB_CACHE", (home / "hub").as_posix())
    monkeypatch.setattr(constants, "HF_XET_CACHE", (home / "xet").as_posix())
    monkeypatch.setattr(
        constants, "AGENT_HARNESSES_PATH", (home / ".agent_harnesses.json").as_posix()
    )
    adapter = embedding_module.build_embedding_function(_onnx_config(tmp_path))

    adapter.prepare()

    assert adapter.hf_cache_paths["HF_HUB_CACHE"] == (home / "hub").as_posix()


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

"""SQLite Checkpoint 的连接管理（开发文档 T03、T11，架构 §10.1、§10.6）。

- 会话事实来源是 LangGraph Checkpoint，不是浏览器：刷新页面或重启服务后按
  ``thread_id`` 恢复（架构 §1.4）。
- 会话索引（``sessions`` 表）与 Checkpoint 放在同一个数据库文件里，并由
  :func:`open_database` 共用一个连接，避免多连接互相加锁。
- 连接由调用方（Streamlit 的 ``cache_resource``）持有并在进程存活期间复用，
  因此连接需要 ``check_same_thread=False``：Streamlit 每次重绘可能在不同线程执行。

反序列化安全口径见 :data:`CHECKPOINT_ALLOWED_TYPES` 与 :func:`build_checkpoint_serializer`。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from local_agent.context.token_budget import ContextBudget, TokenUsage
from local_agent.graph.state import (
    EvidenceReference,
    MemoryCandidate,
    PlanStep,
    ReflectionResult,
    RuntimeContextSnapshot,
    ToolEvent,
)
from local_agent.llm.usage_recording import ModelCallRecord, TurnRecord
from local_agent.performance import span as timing_span
from local_agent.persistence import migrations
from local_agent.persistence.database_maintenance import verify_connection
from local_agent.persistence.session_store import SessionStore

#: 第一个会话（也是 v0.1a 的唯一会话）使用的 ``thread_id``。
#: T11 起新会话用 ``thread-<session_id>``，保留这个常量是为了让升级前的历史仍可恢复。
DEFAULT_THREAD_ID = "local-default"

#: 允许从 Checkpoint 反序列化的**自定义**类型（架构 §10.1）。
#:
#: 为什么需要它（langgraph 1.2.11 实测）：``JsonPlusSerializer`` 默认对**未登记**类型只告警放行
#: （serde 事件 ``msgpack_unregistered_allowed``），并预告未来版本改为直接阻止；到了严格模式或显式
#: 白名单下，未登记类型不会被构造，而是退回原始 tuple/``dict``——状态字段就不再是它该有的类型。
#: 显式登记等于提前对齐将来的默认行为，同时关掉"谁能写这个数据库，就能让加载时 import 并执行任意
#: callable"的口子（``pickle_fallback`` 也在 :func:`build_checkpoint_serializer` 里显式关闭）。
#:
#: **只登记实测会落盘的类型**：集合来自 ``tests/test_checkpoint_serde.py`` 在真实链路上用 serde 事件
#: 的收集（不猜模块、不用通配、不按包名放开）。langchain 消息、``datetime`` / ``UUID`` / ``Path`` 等
#: 由 langgraph 的 ``SAFE_MSGPACK_TYPES`` 覆盖，不在这里重复。**新增 State 字段若引入新的自定义类型，
#: 那条用例会失败**（这正是它存在的意义）。
CHECKPOINT_ALLOWED_TYPES: tuple[type, ...] = (
    ContextBudget,
    EvidenceReference,
    MemoryCandidate,
    ModelCallRecord,
    PlanStep,
    ReflectionResult,
    RuntimeContextSnapshot,
    TokenUsage,
    ToolEvent,
    TurnRecord,
)


def build_checkpoint_serializer() -> JsonPlusSerializer:
    """构造 Checkpoint 序列化器：显式 msgpack 白名单 + **禁止** pickle 回退（架构 §10.1）。

    Returns:
        每个 saver 一份实例；未登记的类型在反序列化时会被拒绝并记 serde 事件。
    """
    return JsonPlusSerializer(
        allowed_msgpack_modules=CHECKPOINT_ALLOWED_TYPES,
        pickle_fallback=False,
    )


@dataclass
class ChatDatabase:
    """一个数据库文件对应的 Checkpoint 与会话索引（共用同一个连接）。"""

    connection: sqlite3.Connection
    checkpointer: SqliteSaver
    sessions: SessionStore


class TimedSqliteSaver(SqliteSaver):
    """Measure the existing saver including serialization, lock wait and commit."""

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with timing_span("checkpoint", "get_tuple"):
            return super().get_tuple(config)

    def put(self, config: RunnableConfig, checkpoint: Checkpoint,
            metadata: CheckpointMetadata, new_versions: ChannelVersions) -> RunnableConfig:
        run_id = checkpoint.get("channel_values", {}).get("run_id")
        with timing_span("checkpoint", "put", run_id):
            return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]],
                   task_id: str, task_path: str = "") -> None:
        with timing_span("checkpoint", "put_writes"):
            super().put_writes(config, writes, task_id, task_path)


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(database_path), check_same_thread=False)


def _prepare(database_path: Path) -> tuple[sqlite3.Connection, SqliteSaver]:
    """打开连接、升级库结构、建好 Checkpoint 表。"""
    connection = _connect(database_path)
    try:
        verify_connection(connection)
        migrations.apply_migrations(connection)
        saver = TimedSqliteSaver(connection, serde=build_checkpoint_serializer())
        saver.setup()
        return connection, saver
    except BaseException:
        connection.close()
        raise


def open_checkpointer(database_path: Path) -> SqliteSaver:
    """打开（必要时创建）Checkpoint 数据库并建表。

    Args:
        database_path: 数据库文件路径；父目录不存在时自动创建。

    Returns:
        可直接传给 ``graph.compile(checkpointer=...)`` 的 SQLite saver。

    Raises:
        sqlite3.Error: 数据库无法打开或建表失败。
    """
    return _prepare(database_path)[1]


def open_database(database_path: Path, *, workspace_root: Path) -> ChatDatabase:
    """打开 Checkpoint 与配套的会话索引（页面用这个入口）。

    Args:
        database_path: 数据库文件路径。
        workspace_root: 工作区根目录；用于生成 ``project_id`` 并划分会话可见范围。

    Returns:
        含连接、Checkpointer 与会话索引的装配对象。

    Raises:
        sqlite3.Error: 数据库无法打开或建表失败。
    """
    connection, saver = _prepare(database_path)
    return ChatDatabase(
        connection=connection,
        checkpointer=saver,
        sessions=SessionStore(connection, workspace_root=workspace_root),
    )


def current_schema_version(database_path: Path) -> int | None:
    """只读地读取聊天库的结构版本。

    Args:
        database_path: 数据库文件路径。

    Returns:
        已应用的最高版本；文件还不存在时返回 ``None``（**不会创建文件**）。
    """
    path = Path(database_path)
    if not path.is_file():
        return None
    connection = sqlite3.connect(str(path))
    try:
        return migrations.current_version(connection)
    finally:
        connection.close()

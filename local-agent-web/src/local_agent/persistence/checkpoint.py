"""SQLite Checkpoint 的连接管理（开发文档 T03、T11，架构 §10.1、§10.6）。

- 会话事实来源是 LangGraph Checkpoint，不是浏览器：刷新页面或重启服务后按
  ``thread_id`` 恢复（架构 §1.4）。
- 会话索引（``sessions`` 表）与 Checkpoint 放在同一个数据库文件里，并由
  :func:`open_database` 共用一个连接，避免多连接互相加锁。
- 连接由调用方（Streamlit 的 ``cache_resource``）持有并在进程存活期间复用，
  因此连接需要 ``check_same_thread=False``：Streamlit 每次重绘可能在不同线程执行。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from local_agent.persistence import migrations
from local_agent.persistence.session_store import SessionStore

#: 第一个会话（也是 v0.1a 的唯一会话）使用的 ``thread_id``。
#: T11 起新会话用 ``thread-<session_id>``，保留这个常量是为了让升级前的历史仍可恢复。
DEFAULT_THREAD_ID = "local-default"


@dataclass
class ChatDatabase:
    """一个数据库文件对应的 Checkpoint 与会话索引（共用同一个连接）。"""

    connection: sqlite3.Connection
    checkpointer: SqliteSaver
    sessions: SessionStore


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(database_path), check_same_thread=False)


def _prepare(database_path: Path) -> tuple[sqlite3.Connection, SqliteSaver]:
    """打开连接、升级库结构、建好 Checkpoint 表。"""
    connection = _connect(database_path)
    migrations.apply_migrations(connection)
    saver = SqliteSaver(connection)
    saver.setup()
    return connection, saver


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

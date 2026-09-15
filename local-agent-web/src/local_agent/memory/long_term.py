"""长期结构化记忆的库初始化与表访问（架构 §10.3，开发文档 T13）。

范围（T13 只做到这里）：

- 保证长期记忆使用**独立**的 SQLite 文件，并按版本迁移到当前结构；
- 提供连接、结构版本与表名自检，供 T15 的 CRUD、T18 的遗忘逻辑和 ``scripts/init_db.py`` 复用。

不在本任务范围：``entities`` / ``facts`` / ``task_conclusions`` 的新增、查询、更新、
版本记录、冲突检测、过期处理与人工删除 —— 那些属于 T15，本模块不提前写半成品 CRUD。

字段契约见 :mod:`local_agent.memory.schemas`，建表语句见
:mod:`local_agent.persistence.migrations`（表名与之必须一致）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from local_agent.exceptions import MemoryStoreError
from local_agent.persistence import migrations


@dataclass
class LongTermStore:
    """长期记忆库的连接与结构信息。

    连接由调用方持有并在进程存活期内复用（与 Checkpoint 的做法一致），因此需要
    ``check_same_thread=False``：Streamlit 每次重绘可能在不同线程执行。
    """

    connection: sqlite3.Connection
    path: Path
    schema_version: int

    def table_names(self) -> frozenset[str]:
        """本库现有的表名。"""
        return migrations.table_names(self.connection)

    def close(self) -> None:
        """关闭连接（重复调用无副作用）。"""
        self.connection.close()


def open_store(database_path: Path) -> LongTermStore:
    """打开（必要时创建）长期记忆库并升级到当前结构版本。

    Args:
        database_path: 长期记忆库文件路径；父目录不存在时自动创建。应与
            ``memory.short_term.path``（聊天库）指向**不同**的文件。

    Returns:
        可复用的 :class:`LongTermStore`。

    Raises:
        MemoryStoreError: 迁移后仍看不到 :data:`~local_agent.persistence.migrations.LONG_TERM_TABLES`，
            通常是把长期记忆指向了聊天库（该文件已有 ``schema_migrations`` 记录，长期表不会建立）。
        sqlite3.Error: 数据库无法打开或建表失败（例如库文件只读）。
    """
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    try:
        version = migrations.apply_long_term_migrations(connection)
        present = migrations.table_names(connection)
        missing = [name for name in migrations.LONG_TERM_TABLES if name not in present]
        if missing:
            raise MemoryStoreError(
                f"长期记忆库缺少表：{'、'.join(missing)}。"
                f"请确认 memory.long_term.path 指向独立的数据库文件（当前是 {path.as_posix()}），"
                "而不是 memory.short_term.path 的聊天库文件。"
            )
    except BaseException:  # 建库失败（含 Ctrl-C）也要先关连接，不留悬挂句柄
        connection.close()
        raise
    return LongTermStore(connection=connection, path=path, schema_version=version)


def current_schema_version(database_path: Path) -> int | None:
    """只读地读取长期记忆库的结构版本。

    Args:
        database_path: 长期记忆库文件路径。

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

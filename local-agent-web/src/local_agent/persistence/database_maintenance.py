"""T79: SQLite 自检与单文件一致快照，不加载配置或凭据。"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from tempfile import TemporaryDirectory

BACKUP_TIMEOUT_SECONDS = 30.0
RECOVERY_HINT = (
    "请停止服务，保留原库及同名 -wal/-shm 文件；运行 scripts/database_maintenance.py check "
    "--database <原库路径>；从已验证备份 restore --backup <备份路径> --output <新库路径>，"
    "再将 memory.short_term.path 指向新库。不要删除原库后重新建空库。"
)


class DatabaseIntegrityError(sqlite3.DatabaseError):
    """数据库无法通过启动自检，调用方必须停止。"""


def verify_connection(connection: sqlite3.Connection) -> None:
    """只接受 quick_check 的单行 ok，不迁移或自动修复。"""
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        raise DatabaseIntegrityError(f"数据库自检失败，已停止：{exc}。{RECOVERY_HINT}") from exc
    if len(rows) != 1 or tuple(rows[0]) != ("ok",):
        raise DatabaseIntegrityError(f"数据库自检失败，已停止。{RECOVERY_HINT}")


def check_database(database_path: Path) -> None:
    """只读检查已有 SQLite 文件；不存在时不创建。"""
    source = database_path.resolve(strict=True)
    if not source.is_file():
        raise ValueError("数据库路径必须是已有文件")
    connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        verify_connection(connection)
    finally:
        connection.close()


def copy_database(source_path: Path, output_path: Path) -> Path:
    """备份/恢复到新路径，包含已提交 WAL 数据，绝不覆盖已有目标。"""
    source = source_path.resolve(strict=True)
    if not source.is_file():
        raise ValueError("数据库路径必须是已有文件")
    if output_path.is_symlink() or output_path.exists():
        raise FileExistsError("输出必须是新路径；原库、备份与已有文件均不能覆盖")
    output = output_path.resolve()
    if source == output:
        raise ValueError("源库与输出不能是同一路径")
    output.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        verify_connection(reader)
        with TemporaryDirectory(prefix=".chaiai-db-", dir=output.parent) as stage:
            temporary = Path(stage) / "snapshot.sqlite"
            writer = sqlite3.connect(temporary)
            try:
                deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS

                def progress(_status: int, _remaining: int, _total: int) -> None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("数据库备份超过 30 秒，已停止；原库未修改")

                reader.backup(writer, pages=256, progress=progress, sleep=0.05)
                mode = writer.execute("PRAGMA journal_mode=DELETE").fetchone()
                if mode != ("delete",):
                    raise DatabaseIntegrityError("备份未能转成独立单文件，已停止")
                verify_connection(writer)
            finally:
                writer.close()
            # 同目录硬链接独占发布：若并发出现目标则失败，不能替换对方文件。
            os.link(temporary, output)
    finally:
        reader.close()
    return output

"""本地数据库初始化（开发文档 §3、T13）。

用法（在项目根目录执行）：

```powershell
uv run python scripts/init_db.py            # 建库 / 升级到当前结构（幂等，可反复执行）
uv run python scripts/init_db.py --check    # 只检查，不改动任何文件
```

约定：

- 只初始化 **SQLite**：聊天库（Checkpoint + 会话索引）与长期记忆库
  （``entities`` / ``facts`` / ``task_conclusions``，架构 §10.3）。
- **关闭的层不初始化**：``memory.short_term.enabled`` / ``memory.long_term.enabled`` 为 ``false``
  时对应的库直接跳过，不建文件。
- 中期记忆是 Chroma 目录（架构 §10.2）：只有 ``memory.medium_term.enabled`` 为 ``true`` 时才需要，
  且必须先安装可选依赖（``uv sync --extra memory``）。本脚本只**检查**依赖是否就绪，不替它建目录，
  也不导入 ``chromadb``。
- 相对路径按**进程当前目录**解析，与 ``web.py`` 的启动约定一致。
- 退出码：``0`` 成功或已就绪；``1`` 检查未通过（缺文件 / 版本落后 / 依赖缺失）；``2`` 配置或初始化失败。
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from local_agent.config import DEFAULT_CONFIG_PATH, AppConfig, ConfigError, load_config
from local_agent.exceptions import MemoryStoreError
from local_agent.memory import long_term, medium_term
from local_agent.persistence import checkpoint, migrations

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_SETUP_PROBLEM = 2


@dataclass(frozen=True)
class DatabaseStatus:
    """一个 SQLite 库的初始化状态（用于打印与 ``--check`` 判定）。"""

    label: str
    path: Path
    expected_version: int
    actual_version: int | None

    @property
    def ready(self) -> bool:
        """文件存在且结构版本不低于当前代码期望的版本。"""
        return self.actual_version is not None and self.actual_version >= self.expected_version

    def describe(self) -> str:
        """一行（加一行路径）的人类可读描述。"""
        if self.actual_version is None:
            detail = "未初始化（文件不存在），需要创建"
        elif self.ready:
            detail = f"结构版本 {self.actual_version}（最新）"
        else:
            detail = f"结构版本 {self.actual_version}，需要升级到 {self.expected_version}"
        return f"{self.label}：{detail}\n    {self.path.as_posix()}"


def chat_database_path(config: AppConfig) -> Path:
    """聊天库文件路径（相对路径按进程当前目录解析，与 ``web.py`` 一致）。"""
    return Path(config.memory.short_term.path).resolve()


def long_term_database_path(config: AppConfig) -> Path:
    """长期记忆库文件路径（同上）。"""
    return Path(config.memory.long_term.path).resolve()


def inspect(config: AppConfig) -> list[DatabaseStatus]:
    """只读地检查已启用各层的结构状态（不创建文件、不导入 ``chromadb``）。"""
    statuses: list[DatabaseStatus] = []
    if config.memory.short_term.enabled:
        path = chat_database_path(config)
        statuses.append(
            DatabaseStatus(
                label="聊天库（Checkpoint + 会话索引）",
                path=path,
                expected_version=migrations.CHAT_SCHEMA_VERSION,
                actual_version=checkpoint.current_schema_version(path),
            )
        )
    if config.memory.long_term.enabled:
        path = long_term_database_path(config)
        statuses.append(
            DatabaseStatus(
                label="长期记忆库（entities / facts / task_conclusions）",
                path=path,
                expected_version=migrations.LONG_TERM_SCHEMA_VERSION,
                actual_version=long_term.current_schema_version(path),
            )
        )
    return statuses


def initialize(config: AppConfig) -> list[DatabaseStatus]:
    """建库并把已启用各层升级到当前结构，返回初始化后的状态（幂等）。

    Raises:
        MemoryStoreError: 长期记忆库的结构无法建立（例如指向了聊天库文件）。
        sqlite3.Error: 数据库无法打开或建表失败（例如库文件只读）。
    """
    if config.memory.short_term.enabled:
        database = checkpoint.open_database(
            chat_database_path(config), workspace_root=Path(config.workspace.root)
        )
        database.connection.close()
    if config.memory.long_term.enabled:
        long_term.open_store(long_term_database_path(config)).close()
    return inspect(config)


def _print_medium_term(config: AppConfig) -> bool:
    """打印中期记忆的状态；返回它是否就绪（未启用也算就绪）。"""
    if not config.memory.medium_term.enabled:
        print("中期记忆（Chroma）：未启用，跳过")
        return True
    if medium_term.chromadb_available():
        path = Path(config.memory.medium_term.path).resolve()
        print(f"中期记忆（Chroma）：依赖就绪，路径 {path.as_posix()}")
        return True
    print(f"中期记忆（Chroma）：{medium_term.CHROMADB_INSTALL_HINT}")
    return False


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；返回进程退出码（见模块 docstring）。"""
    parser = argparse.ArgumentParser(description="初始化本地 SQLite 数据库（幂等）")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH.as_posix(),
        help="配置文件路径，默认 config.yaml（相对路径按当前目录解析）",
    )
    parser.add_argument(
        "--check", action="store_true", help="只检查结构版本，不改动任何文件"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"配置不可用：{exc}")
        return EXIT_SETUP_PROBLEM

    try:
        statuses = inspect(config) if args.check else initialize(config)
    except (MemoryStoreError, sqlite3.Error) as exc:
        print(f"数据库初始化失败：{exc}")
        return EXIT_SETUP_PROBLEM

    for status in statuses:
        print(status.describe())
    if not statuses:
        print("没有需要初始化的 SQLite 库（short_term 与 long_term 都是关闭状态）")
    medium_term_ready = _print_medium_term(config)

    if args.check:
        if any(not status.ready for status in statuses) or not medium_term_ready:
            print("检查未通过：请执行 `uv run python scripts/init_db.py`（中期记忆还要装可选依赖）")
            return EXIT_NOT_READY
        print("检查通过：所有已启用的层都处于最新状态")
        return EXIT_OK

    print(f"初始化完成：{len(statuses)} 个 SQLite 库就绪")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

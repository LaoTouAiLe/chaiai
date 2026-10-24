"""T79: check / backup / restore，显式路径、停服务、新目标，不读取密钥。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

from local_agent.api.container import acquire_instance_lock
from local_agent.persistence.database_maintenance import check_database, copy_database


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite 自检、备份与新路径恢复（先停止服务）")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="只读自检已有数据库")
    check.add_argument("--database", type=Path, required=True)
    backup = commands.add_parser("backup", help="一致备份到新路径，不覆盖文件")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore", help="验证备份并恢复到新路径，不覆盖原库")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source = args.backup if args.command == "restore" else args.database
    try:
        # 在取得 API 单实例锁前确认源存在，错误路径不能被意外创建成新库。
        source = source.resolve(strict=True)
        targets = [source]
        if args.command != "check":
            targets.append(args.output.resolve())
        with ExitStack() as stack:
            for target in sorted(set(targets)):
                stack.enter_context(acquire_instance_lock(target))
            if args.command == "check":
                check_database(source)
                print(f"自检通过：{source}")
            else:
                output = copy_database(source, args.output)
                print(f"{args.command} 完成并通过自检：{output}（原库与备份保留）")
        return 0
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"数据库维护失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

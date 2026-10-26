"""导出"设置页能改哪些字段"的权威清单（2026-10-26）。

用法（在 `local-agent-web/` 下执行）：

```powershell
uv run python scripts/export_preferences_schema.py            # 写入 docs/api/preferences-schema.json
uv run python scripts/export_preferences_schema.py --check    # 只检查，有差异则退出码 1
```

为什么要落盘一份文件：设置页的字段表是**前端手写**的（`FIELDS`），后端是 `RuntimePreferences`。
两者必须一致，但跨语言没法直接 import 对方。把后端的机械信息导出成 JSON，前端测试读它比对——
后端加了可改字段而页面没接，测试就会红。此前没有任何机制能发现这种情况。

约定与 `export_openapi.py` 一致：幂等（内容没变就不动文件）、按字节比对、`newline="\\n"` 落盘。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from local_agent.model_settings.preferences_schema import preferences_schema

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_SETUP_PROBLEM = 2

#: 脚本在 `local-agent-web/scripts/` 下，落盘路径相对**仓库根**（与 openapi.json 同一个目录）。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path("../docs/api/preferences-schema.json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uv run python scripts/export_preferences_schema.py",
        description="导出设置页字段清单到 docs/api/preferences-schema.json",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查落盘文件与当前代码是否一致，不写文件",
    )
    args = parser.parse_args(argv)

    target = PROJECT_ROOT / SCHEMA_PATH
    try:
        # 结尾补一个换行：与仓库其它 JSON 产物一致，也让"内容没变"这件事在编辑器里看得出来。
        document = preferences_schema().model_dump_json(indent=2) + "\n"
    except Exception as exc:  # noqa: BLE001 - 反射失败要给可读原因，而不是回溯
        print(f"导出失败：{exc}")
        return EXIT_SETUP_PROBLEM

    # 按**字节**读：`read_text()` 会把 CRLF 翻成 LF，那样逐字节比对就检测不出行尾差异。
    current = target.read_bytes().decode("utf-8") if target.is_file() else None
    if current == document:
        print(f"已是最新：{SCHEMA_PATH.as_posix()}")
        return EXIT_OK

    if args.check:
        print(
            f"检查未通过：{SCHEMA_PATH.as_posix()} 与当前代码不一致，"
            "请执行 `uv run python scripts/export_preferences_schema.py`"
        )
        return EXIT_NOT_READY

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8", newline="\n")
    print(f"已写入：{SCHEMA_PATH.as_posix()}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

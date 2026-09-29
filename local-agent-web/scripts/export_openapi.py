"""导出受控 OpenAPI（T29；计划 §6.2）。

用法（在项目根目录执行）：

```powershell
uv run python scripts/export_openapi.py            # 写入 docs/api/openapi.json（幂等）
uv run python scripts/export_openapi.py --check    # 只检查，有差异则退出码 1
```

约定：

- 导出**不需要**数据库、模型或密钥：用一个占位运行时走 `create_app`（见 `api/openapi.py`）；
- 落盘文件是前端的**唯一**类型来源（`npm run generate:api` 读它），所以必须入库、不得手改；
- 幂等：内容没变就不改文件（避免只有时间戳/mtime 变化）；
- 退出码：`0` 成功或已最新；`1` 检查未通过（与代码不一致）；`2` 导出失败。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from local_agent.api.openapi import OPENAPI_PATH, openapi_json

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_SETUP_PROBLEM = 2

#: 脚本在 `local-agent-web/scripts/` 下，落盘路径相对项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uv run python scripts/export_openapi.py",
        description="导出受控 OpenAPI 到 docs/api/openapi.json（前端类型由它生成）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查落盘文件与当前代码是否一致，不写文件",
    )
    args = parser.parse_args(argv)

    target = PROJECT_ROOT / OPENAPI_PATH
    try:
        document = openapi_json()
    except Exception as exc:  # noqa: BLE001 - 导出失败要给可读原因，而不是回溯
        print(f"导出失败：{exc}")
        return EXIT_SETUP_PROBLEM

    # 按**字节**读：`read_text()` 会把 CRLF 翻成 LF，那样"逐字节一致"就名不副实，
    # 行尾差异也检测不出来（实测：CRLF 的落盘文件被判成"已是最新"）。
    current = target.read_bytes().decode("utf-8") if target.is_file() else None
    if current == document:
        print(f"已是最新：{OPENAPI_PATH}")
        return EXIT_OK

    if args.check:
        print(f"检查未通过：{OPENAPI_PATH} 与当前代码不一致，请执行 `uv run python scripts/export_openapi.py`")
        return EXIT_NOT_READY

    target.parent.mkdir(parents=True, exist_ok=True)
    # `newline="\n"`：Windows 上默认会把 \n 翻成 \r\n，那会让"逐字节一致"在跨平台时失真。
    target.write_text(document, encoding="utf-8", newline="\n")
    print(f"已写入：{OPENAPI_PATH}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

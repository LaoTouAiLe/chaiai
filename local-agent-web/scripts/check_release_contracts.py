"""T84：默认只读检查；--write 仅供审查后显式更新兼容基线。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from local_agent.release_contracts import (
    canonical_document,
    changed_contracts,
    compatibility_manifest,
)

# 文档统一在仓库根的 docs/（2026-10-25 起）；本脚本在 local-agent-web/scripts/ 下，所以多上一层。
BASELINE = Path(__file__).resolve().parents[2] / "docs/release/compatibility-baseline.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 v1.0 候选兼容基线（不读取用户数据）")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只读检查（默认）")
    mode.add_argument("--write", action="store_true", help="审查契约后显式更新基线")
    args = parser.parse_args(argv)
    actual = compatibility_manifest()
    try:
        if args.write:
            args.baseline.parent.mkdir(parents=True, exist_ok=True)
            args.baseline.write_text(canonical_document(actual), encoding="utf-8", newline="\n")
            print("兼容基线已写入（候选版本，未发布）")
            return 0
        expected = json.loads(args.baseline.read_text(encoding="utf-8"))
        if not isinstance(expected, dict):
            raise TypeError("baseline must be an object")
    except (OSError, ValueError, TypeError):
        print("兼容基线缺失、不可读或格式错误；检查路径与受控文件，不自动重写。")
        return 1
    changed = changed_contracts(expected, actual)
    if changed:
        print("兼容检查失败：" + ", ".join(changed) + "；先审查契约和迁移，不自动更新基线。")
        return 1
    print("兼容基线检查通过（不代表真实模型或新机器验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

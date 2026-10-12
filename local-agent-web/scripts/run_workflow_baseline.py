"""运行 v0.7 固定复杂任务的离线自由循环基线，不创建真实模型客户端。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from local_agent.workflow_eval import run_baseline


def main() -> int:
    """写入可对照报告；任一任务机械校验失败时返回非零。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/v07-baseline.json"))
    args = parser.parse_args()
    report = run_baseline()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for case in report["cases"]:
        print(f"{case['case_id']}: passed={case['passed']}, "
              f"model_calls={case['model_calls']}, tool_calls={case['tool_calls']}")
    print(f"报告：{args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

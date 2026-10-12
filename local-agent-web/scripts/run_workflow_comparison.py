"""运行 T70 同题离线对照，不创建供应商客户端或读取正式数据。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from local_agent.workflow_comparison import run_comparison


def main() -> int:
    """保存所有尝试；机械门或离线优势未通过时返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/v07-comparison.json"))
    parser.add_argument("--repetitions", type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    report = run_comparison(args.repetitions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for case in report["cases"]:
        print(f"{case['case_id']}: means={case['means']}, "
              f"offline_improvement={case['model_calls_improved_in_every_pair']}")
    print(f"报告：{args.output}；passed={report['passed']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

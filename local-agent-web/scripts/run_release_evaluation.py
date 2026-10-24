"""T86：固定离线工程评测，输出到新目录，不触发付费模型。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from local_agent.release_contracts import compatibility_manifest
from local_agent.release_evaluation import FIXED_TEST_FILES, parse_results, source_fingerprint

PROJECT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 v1.0 固定工程评测（替身模型）")
    parser.add_argument("--output", type=Path, default=PROJECT / "eval" /
                        ("v10-fixed-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")))
    args = parser.parse_args()
    output = args.output.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError:
        print("报告目录必须为可创建的新目录；不覆盖已有结果。")
        return 1
    before = source_fingerprint(PROJECT)
    baseline = compatibility_manifest()
    junit = output / "results.xml"
    with tempfile.TemporaryDirectory(prefix="chaiai-v10-fixed-") as temp:
        command = [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
                   "--basetemp", str(Path(temp) / "pytest"), "--junitxml", str(junit), *FIXED_TEST_FILES]
        with (output / "pytest.log").open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    command, cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT, check=False,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=600,
                )
                exit_code = result.returncode
            except (OSError, subprocess.TimeoutExpired):
                exit_code = 1
    report = parse_results(junit, exit_code)
    report.update({
        "pytest_exit_code": exit_code, "python_version": sys.version.split()[0],
        "test_files": list(FIXED_TEST_FILES), "source_sha256": before,
        "source_unchanged": before == source_fingerprint(PROJECT),
        "compatibility": baseline, "model_mode": "deterministic_test_doubles",
        "semantic_quality_reviewed": False,
    })
    report["passed"] = report["passed"] and report["source_unchanged"]
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n",
                                        encoding="utf-8", newline="\n")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]),
                      "output": str(output)}, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

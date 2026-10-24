"""T86：固定工程用例的证据解析与源码指纹；无 Agent 编排。"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

FIXED_TEST_FILES = (
    "tests/test_agent_eval.py", "tests/test_capabilities.py", "tests/test_capability_boundary.py",
    "tests/test_cancellation.py", "tests/test_recovery.py", "tests/test_readonly_workflow.py",
    "tests/test_code_workflow.py", "tests/test_delegated_workflow.py",
    "tests/test_delegated_recovery.py", "tests/test_workflow_eval.py",
    "tests/test_workflow_comparison.py", "tests/test_database_maintenance.py",
    "tests/test_fault_injection.py", "tests/test_release_contracts.py", "tests/test_diagnostics.py",
    "tests/test_release_evaluation.py",
)


def source_fingerprint(project: Path) -> str:
    digest = hashlib.sha256()
    for directory in ("src", "tests", "scripts", "packaging"):
        for path in sorted((project / directory).rglob("*.py")):
            digest.update(path.relative_to(project).as_posix().encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def parse_results(path: Path, exit_code: int) -> dict[str, Any]:
    """以实际 JUnit 为准，不能用 pytest 退出码 alone 把空集合视为通过。"""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {"passed": False, "cases": [], "reason": "missing_or_invalid_junit"}
    cases = []
    for case in root.iter("testcase"):
        status = "passed"
        for tag in ("skipped", "failure", "error"):
            if case.find(tag) is not None:
                status = tag
                break
        cases.append({"class": case.get("classname", ""), "name": case.get("name", ""),
                      "status": status})
    # collection/setup errors may be attached to suites rather than test cases.
    try:
        errors = sum(int(suite.get("errors", "0")) for suite in root.iter("testsuite"))
    except ValueError:
        return {"passed": False, "cases": cases, "reason": "invalid_junit_error_count"}
    return {
        "passed": exit_code == 0 and bool(cases) and errors == 0
        and all(case["status"] == "passed" for case in cases),
        "cases": cases,
        "suite_errors": errors,
    }

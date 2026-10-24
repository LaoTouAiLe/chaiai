"""T86：不能把空集合、跳过或集合级错误写成绿色评测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.release_evaluation import parse_results, source_fingerprint


@pytest.mark.parametrize("xml,code,passed", [
    ('<testsuites><testsuite><testcase name="real-case"/></testsuite></testsuites>', 0, True),
    ('<testsuites/>', 0, False),
    ('<testsuite><testcase><skipped/></testcase></testsuite>', 0, False),
    ('<testsuite><testcase><failure/></testcase></testsuite>', 0, False),
    ('<testsuite errors="1"><testcase/></testsuite>', 0, False),
    ('<testsuite errors="bad"><testcase/></testsuite>', 0, False),
    ('<testsuite><testcase/></testsuite>', 1, False),
    ('invalid', 0, False),
])
def test_real_junit_is_required_for_a_pass(tmp_path: Path, xml: str, code: int, passed: bool) -> None:
    report = tmp_path / "results.xml"
    report.write_text(xml, encoding="utf-8")
    assert parse_results(report, code)["passed"] is passed


def test_missing_report_fails(tmp_path: Path) -> None:
    assert parse_results(tmp_path / "missing.xml", 0)["passed"] is False


def test_source_fingerprint_detects_edits_and_new_files(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    path = source / "entry.py"
    path.write_text("before", encoding="utf-8")
    before = source_fingerprint(tmp_path)
    path.write_text("after", encoding="utf-8")
    changed = source_fingerprint(tmp_path)
    assert changed != before
    (source / "new.py").write_text("new", encoding="utf-8")
    assert source_fingerprint(tmp_path) != changed

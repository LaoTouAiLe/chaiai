"""文件变更账本（2026-10-21）：基线只留第一次、按文件聚合、整文件 diff。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from local_agent.persistence.file_changes import MAX_BASELINE_BYTES, FileChangeLedger
from local_agent.persistence.migrations import _CHAT_V11_FILE_CHANGES_SQL


def make_ledger(tmp_path: Path) -> tuple[FileChangeLedger, Path]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_CHAT_V11_FILE_CHANGES_SQL)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ledger = FileChangeLedger(
        connection,
        lock=threading.Lock(),
        session_id="s1",
        baseline_dir=tmp_path / "baselines",
        workspace_root=workspace,
    )
    return ledger, workspace


def test_capture_keeps_the_first_version_only(tmp_path: Path) -> None:
    """一个文件改多次时只有**第一次**的原文；后续 capture 不能覆盖基线。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_text("one\ntwo\n", encoding="utf-8")
    ledger.capture(target)

    target.write_text("one\nTWO\n", encoding="utf-8")
    ledger.capture(target)

    result = ledger.diff("a.py")
    assert result.has_baseline is True
    # baseline 是 "one\ntwo\n"：换成当前内容后只应改动第二行。
    assert (result.additions, result.deletions) == (1, 1)
    assert [line.text for line in result.hunks[0].lines if line.kind == "del"] == ["two"]
    assert [line.text for line in result.hunks[0].lines if line.kind == "add"] == ["TWO"]


def test_summary_aggregates_edits_of_one_file(tmp_path: Path) -> None:
    """同一文件被改三次仍是一行，行数与次数累计——这正是页面要的口径。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "a.py"
    target.write_text("x\n", encoding="utf-8")
    ledger.capture(target)

    ledger.record(tool="edit_file", resolved=target, kind="modify", additions=1, deletions=0)
    ledger.record(tool="edit_file", resolved=target, kind="modify", additions=2, deletions=1)
    ledger.record(tool="edit_file", resolved=target, kind="modify", additions=0, deletions=1)

    summary = ledger.summary()
    assert len(summary) == 1
    assert summary[0].path == "a.py"
    assert summary[0].edits == 3
    assert (summary[0].additions, summary[0].deletions) == (3, 2)
    assert summary[0].has_baseline is True


def test_new_file_keeps_an_empty_baseline_so_the_whole_content_shows(tmp_path: Path) -> None:
    """新建文件留**空基线**：点开时应看到整份内容都是新增，而不是"没有改前内容"。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "new.py"
    ledger.capture(target)  # 文件还不存在
    target.write_text("a\nb\n", encoding="utf-8")
    ledger.record(tool="write_file", resolved=target, kind="create", additions=2)

    summary = ledger.summary()[0]
    assert summary.kind == "create"
    assert summary.has_baseline is True
    assert summary.unavailable_reason is None

    result = ledger.diff("new.py")
    assert (result.additions, result.deletions) == (2, 0)
    assert [line.text for line in result.hunks[0].lines if line.kind == "add"] == ["a", "b"]


def test_legacy_create_without_baseline_still_shows_the_content(tmp_path: Path) -> None:
    """修复前记录的历史 create 没有基线文件，也要能看整份内容（否则旧会话永远空白）。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "old.html"
    target.write_text("<h1>x</h1>\n", encoding="utf-8")
    # 只记账、不 capture：模拟旧数据。
    ledger.record(tool="write_file", resolved=target, kind="create", additions=1)

    summary = ledger.summary()[0]
    assert summary.has_baseline is True
    result = ledger.diff("old.html")
    assert (result.additions, result.deletions) == (1, 0)


def test_create_then_modify_keeps_the_baseline_flag(tmp_path: Path) -> None:
    """先 create 再 modify：后一条 op 没有基线文件，但"可看整份内容"的结论不能被它覆盖。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "old.html"
    ledger.record(tool="write_file", resolved=target, kind="create", additions=10)
    target.write_text("x\n", encoding="utf-8")
    ledger.record(tool="edit_file", resolved=target, kind="modify", additions=1, deletions=1)

    summary = ledger.summary()[0]
    assert summary.kind == "modify"
    assert summary.has_baseline is True
    assert summary.unavailable_reason is None


def test_oversized_baseline_is_not_stored(tmp_path: Path) -> None:
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "big.txt"
    target.write_bytes(b"a" * (MAX_BASELINE_BYTES + 1))
    ledger.capture(target)

    summary = ledger.summary()
    # 没有 record 时 summary 为空（账本只列出"被改过"的文件），这里直接看 diff 的说明。
    assert summary == []
    result = ledger.diff("big.txt")
    assert result.has_baseline is False
    assert result.unavailable_reason is not None
    assert "KB" in result.unavailable_reason


def test_binary_baseline_is_not_stored(tmp_path: Path) -> None:
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "blob.bin"
    target.write_bytes(b"\x00\x01\x02")
    ledger.capture(target)
    result = ledger.diff("blob.bin")
    assert result.has_baseline is False
    assert result.unavailable_reason == "二进制文件，未保存改前内容"


def test_deleted_file_shows_whole_content_as_removed(tmp_path: Path) -> None:
    """删除后当前文件不存在：整份原文都应算作删除，而不是空 diff。"""
    ledger, workspace = make_ledger(tmp_path)
    target = workspace / "gone.py"
    target.write_text("a\nb\n", encoding="utf-8")
    ledger.capture(target)
    target.unlink()
    ledger.record(tool="delete_file", resolved=target, kind="delete", deletions=2)

    result = ledger.diff("gone.py")
    assert result.has_baseline is True
    assert result.deletions == 2
    assert result.additions == 0


def test_diff_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    ledger, _ = make_ledger(tmp_path)
    result = ledger.diff("../outside.py")
    assert result.has_baseline is False
    assert result.unavailable_reason == "这个路径不在当前工作区内"

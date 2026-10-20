"""回收站（2026-10-20）：删除变成"移动"，且能按清单找回。

钉四件事：文件被移走而不是删掉、目录结构原样保留、清单可读、目标不存在时如实报错。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_agent.exceptions import FileOperationError
from local_agent.trash import MANIFEST_NAME, move_to_trash


def test_moves_a_file_into_a_dated_slot_and_records_it(tmp_path: Path) -> None:
    source = tmp_path / "work" / "note.txt"
    source.parent.mkdir()
    source.write_text("keep me", encoding="utf-8")
    root = tmp_path / "trash"

    moved = move_to_trash(source, root=root)

    assert not source.exists(), "安全删除不能把原文件留在原地"
    assert moved.read_text(encoding="utf-8") == "keep me"
    # 回收站结构：root/年-月-日/时间-短id/文件名
    assert moved.parent.parent.parent == root
    lines = (root / MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["原路径"] == str(source)
    assert record["回收路径"] == str(moved)


def test_moves_a_directory_without_flattening(tmp_path: Path) -> None:
    source = tmp_path / "pkg"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "a.txt").write_text("a", encoding="utf-8")

    moved = move_to_trash(source, root=tmp_path / "trash")

    assert (moved / "sub" / "a.txt").read_text(encoding="utf-8") == "a"


def test_two_deletions_never_overwrite_each_other(tmp_path: Path) -> None:
    root = tmp_path / "trash"
    first = tmp_path / "same.txt"
    first.write_text("one", encoding="utf-8")
    moved_first = move_to_trash(first, root=root)
    second = tmp_path / "same.txt"
    second.write_text("two", encoding="utf-8")

    moved_second = move_to_trash(second, root=root)

    assert moved_second != moved_first, "同名文件先后被删时不能用同一个槽位"
    assert moved_first.read_text(encoding="utf-8") == "one"
    assert moved_second.read_text(encoding="utf-8") == "two"


def test_missing_target_is_reported_instead_of_silently_succeeding(tmp_path: Path) -> None:
    with pytest.raises(FileOperationError, match="目标不存在"):
        move_to_trash(tmp_path / "nope", root=tmp_path / "trash")

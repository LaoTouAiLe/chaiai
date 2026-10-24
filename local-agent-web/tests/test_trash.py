"""回收站（2026-10-21 改为**系统回收站**）。

早先的版本把文件移进自建的 ``data/trash`` 并写 manifest 清单，结果是"删了、提示可找回，
但用户在资源管理器里找不到"。现在直接进系统回收站，测试因此只钉三件事：

1. 原位置**真的空了**（否则等于没删）；
2. 失败时**原位置还在**（绝不回退成直接删除——那等于把安全删除静默关掉）；
3. 目标不存在时如实报错，而不是静默成功。

"是否真进了系统回收站"无法在纯单元测试里可靠断言（要经 COM 查 `$RECYCLE.BIN`，
会污染运行测试那台机器的回收站），这一条靠手工实测覆盖：删一个文件后，
目标出现在 `E:\\$RECYCLE.BIN\\S-1-5-...\\` 下。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.exceptions import FileOperationError
from local_agent.trash import move_to_trash


def test_file_is_gone_from_its_original_location(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    source.write_text("keep me", encoding="utf-8")

    move_to_trash(source)

    assert not source.exists(), "安全删除至少要把原文件从原位置移走"


def test_directory_is_moved_as_a_whole(tmp_path: Path) -> None:
    # 目录必须整体移走：逐个删除会在失败时留下半个目录。
    source = tmp_path / "pkg"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "a.txt").write_text("a", encoding="utf-8")

    move_to_trash(source)

    assert not source.exists()


def test_failure_keeps_the_file_instead_of_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回收站不可用（网络盘、权限不足）时**必须报错并保留文件**。

    这是这条路径最要紧的一条：静默回退成 ``unlink`` 会让"安全删除"看起来生效、东西却没了。
    """
    source = tmp_path / "note.txt"
    source.write_text("keep me", encoding="utf-8")

    def _refuse(_path: str) -> None:
        raise OSError("该位置不支持回收站")

    monkeypatch.setattr("local_agent.trash.send2trash", _refuse)

    with pytest.raises(FileOperationError, match="没有被删除"):
        move_to_trash(source)

    assert source.exists(), "失败之后文件必须还在原位置"


def test_missing_target_is_reported_instead_of_silently_succeeding(tmp_path: Path) -> None:
    with pytest.raises(FileOperationError, match="目标不存在"):
        move_to_trash(tmp_path / "nope")

"""右栏只读面板（§21.6，2026-10-22）的契约测试。

守三件事：**只在工作区内、噪声目录不出现、非仓库不报错**。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.application.workspace_view import git_diff, git_status, list_files, search_files


def _service(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _workspace_root=root,
        _path_for_workspace_id=lambda workspace_id: root,
    )


def _tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    (root / ".env.example").write_text("A=1\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "noise.js").write_text("x", encoding="utf-8")
    (root / ".git").mkdir()


def test_lists_one_level_and_hides_noise(tmp_path: Path) -> None:
    _tree(tmp_path)
    view = list_files(_service(tmp_path))

    names = {entry.name for entry in view.entries}
    assert "src" in names
    assert ".env.example" in names, "隐藏文件要保留——.env.example 正是要看的那类"
    assert "node_modules" not in names
    assert ".git" not in names
    assert view.parent is None


def test_lists_subdirectory_and_reports_parent(tmp_path: Path) -> None:
    _tree(tmp_path)
    view = list_files(_service(tmp_path), path="src")

    assert view.path == "src"
    assert view.parent == ""
    assert [entry.name for entry in view.entries] == ["app.py"]
    assert view.entries[0].kind == "file"
    assert view.entries[0].size_bytes > 0


def test_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-dir"
    outside.mkdir(exist_ok=True)
    with pytest.raises(PermissionError):
        list_files(_service(tmp_path), path="../outside-dir")
    with pytest.raises(PermissionError):
        list_files(_service(tmp_path), path=str(outside))


def test_missing_directory_is_lookup_error(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        list_files(_service(tmp_path), path="nope")


def test_non_git_workspace_reports_unavailable(tmp_path: Path) -> None:
    """不是仓库不是错误：工作区本来就可以没有版本控制。"""
    if shutil.which("git") is None:
        pytest.skip("本机没有 git")
    _tree(tmp_path)
    view = git_status(_service(tmp_path))
    assert view.available is False
    assert view.entries == []


def test_git_status_and_diff_on_real_repo(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("本机没有 git")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "PATH": str(Path(shutil.which("git") or "").parent), "HOME": str(tmp_path)}
    run = lambda *args: subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                                       capture_output=True, env=env)
    run("init")
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-m", "init")
    (tmp_path / "a.txt").write_text("new\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("untracked\n", encoding="utf-8")

    status = git_status(_service(tmp_path))
    assert status.available is True
    paths = {entry.path for entry in status.entries}
    assert "a.txt" in paths
    assert "b.txt" in paths

    view = git_diff(_service(tmp_path), path="a.txt")
    assert view.available is True
    assert "-old" in (view.patch or "")
    assert "+new" in (view.patch or "")


def test_git_diff_rejects_outside_path(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        git_diff(_service(tmp_path), path="../escape.txt")


# ---- 按文件名递归搜索（2026-10-22）----------------------------------------


def test_search_finds_nested_files_with_relative_path(tmp_path: Path) -> None:
    (tmp_path / "src" / "features").mkdir(parents=True)
    (tmp_path / "src" / "features" / "index.ts").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.ts").write_text("x", encoding="utf-8")

    view = search_files(_service(tmp_path), q="index.ts")
    paths = {entry.path for entry in view.entries}
    assert "src/features/index.ts" in paths
    assert "node_modules/pkg/index.ts" not in paths, "噪声目录不应出现在搜索结果里"


def test_search_is_case_insensitive_and_rejects_blank(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    assert [entry.name for entry in search_files(_service(tmp_path), q="readme").entries] == ["README.md"]
    with pytest.raises(ValueError):
        search_files(_service(tmp_path), q="   ")


def test_tree_mode_skips_runtime_dirs(tmp_path: Path) -> None:
    """整棵树全展开时，应用自己的运行时目录会占掉半屏（2026-10-23 实机截图）。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    (tmp_path / ".agent-runtime").mkdir()
    (tmp_path / ".agent-runtime" / "sessions.sqlite").write_text("x", encoding="utf-8")

    view = list_files(_service(tmp_path), recursive=True)
    paths = {entry.path for entry in view.entries}
    assert "src/app.py" in paths
    assert "src" in paths, "目录本身也要出现，否则空目录在树上会消失"
    assert not any(path.startswith(".agent-runtime") for path in paths)

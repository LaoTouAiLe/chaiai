"""右栏只读面板：目录列举、git 状态与 diff（§21.6，2026-10-22）。

三条边界与 §15.11.3 同源：**只读、只在工作区内、都有上限**。
这里没有写操作，也不提供任何"执行"能力——那是 Agent 工具与审批的事。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from local_agent.application.workspace_models import (
    GitDiffView,
    GitStatusEntryView,
    GitStatusView,
    WorkspaceEntryView,
    WorkspaceFilesView,
)
from local_agent.tools.git_tools import GitError, SecurityError, run_git

if TYPE_CHECKING:
    from local_agent.application.service import ApplicationService

#: 噪声目录：列出来只会把用户要找的文件埋掉。
NOISE_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".next", "target",
    # 本应用自己的运行时数据（2026-10-23）：会话库、终端日志、附件都在里面。
    # 树状全展开时它们会占掉半屏，而用户从来不会想"打开这个目录看看"。
    ".agent-runtime", ".local-agent",
    # 数据/语料目录（2026-10-23 实测）：本工作区排掉噪声后仍有 6.9 万条，
    # 其中 `eval`（评测语料）55884 条、`data` 3300 条。树要一次全展开，
    # 留下它们等于把真正的源码埋掉。这两个名字在绝大多数项目里本就是数据目录；
    # 万一某个项目把源码放在 `data/` 下，用上面的文件搜索仍能找到（搜索走的是文件名匹配）。
    "eval", "data",
})
MAX_ENTRIES = 500
MAX_DIFF_CHARS = 200_000
GIT_TIMEOUT_SECONDS = 10.0
MAX_SEARCH_RESULTS = 50
#: 搜索深度上限：再深多半是依赖或产物目录，扫了也没有用户要找的东西。
MAX_SEARCH_DEPTH = 8
#: 树状列表的条数上限（目录 + 文件合计）。超过就截断并标注。
#: 噪声目录已排除，但一个真实项目几千个文件仍然正常，所以给到 2000。
MAX_TREE_ENTRIES = 4000


def _root(service: ApplicationService, workspace_id: str | None) -> Path:
    base = (service._path_for_workspace_id(workspace_id) if workspace_id
            else service._workspace_root)
    return Path(base).resolve()


def _inside(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def _relative(target: Path, root: Path) -> str:
    text = target.relative_to(root).as_posix()
    return "" if text == "." else text


def list_files(service: ApplicationService, *, path: str = "",
               workspace_id: str | None = None,
               recursive: bool = False) -> WorkspaceFilesView:
    """列目录。

    `recursive=True` 时返回**整棵树的扁平列表**（2026-10-23）：右栏改成树状展示后，
    逐层点进去的交互被用户否掉了，一次给全才能"一眼看到项目结构"。
    扁平而不是嵌套：组装树是纯展示逻辑，放前端做，后端不必再造一套递归结构。
    """
    root = _root(service, workspace_id)
    target = (root / path).resolve() if path else root
    if not _inside(target, root):
        raise PermissionError("只能浏览工作区内的目录。")
    if not target.is_dir():
        raise LookupError("目录不存在，可能已被删除。")

    if recursive:
        return _list_tree(root, target)

    children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    entries: list[WorkspaceEntryView] = []
    truncated = False
    for child in children:
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        if child.is_dir() and child.name in NOISE_DIRS:
            continue
        # 指向工作区外的符号链接不列：点进去也读不到，列出来只是误导。
        try:
            resolved = child.resolve()
        except OSError:
            continue
        if not _inside(resolved, root) and child.is_symlink():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(WorkspaceEntryView(
            name=child.name,
            kind="dir" if child.is_dir() else "file",
            size_bytes=0 if child.is_dir() else int(stat.st_size),
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        ))

    parent = None if target == root else _relative(target.parent, root)
    return WorkspaceFilesView(path=_relative(target, root), parent=parent,
                              entries=entries, truncated=truncated)


def search_files(service: ApplicationService, *, q: str,
                 workspace_id: str | None = None) -> WorkspaceFilesView:
    """按文件名递归搜索工作区（2026-10-22）。

    右栏文件标签原来只能筛当前目录，用户搜 `index.ts` 时得逐层点进去。
    这里从根递归、跳过噪声目录、限深度与条数——够用且不会把大仓库扫穿。
    """
    needle = (q or "").strip().lower()
    if not needle:
        raise ValueError("请输入要搜索的文件名。")
    root = _root(service, workspace_id)
    entries: list[WorkspaceEntryView] = []
    truncated = False

    for current, dirs, files in os.walk(root):
        depth = len(Path(current).relative_to(root).parts)
        if depth >= MAX_SEARCH_DEPTH:
            dirs[:] = []
        else:
            dirs[:] = sorted(name for name in dirs if name not in NOISE_DIRS)
        for name in sorted(files):
            if needle not in name.lower():
                continue
            target = Path(current) / name
            try:
                stat = target.stat()
            except OSError:
                continue
            entries.append(WorkspaceEntryView(
                name=name,
                kind="file",
                size_bytes=int(stat.st_size),
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                path=target.relative_to(root).as_posix(),
            ))
            if len(entries) >= MAX_SEARCH_RESULTS:
                truncated = True
                break
        if truncated:
            break

    return WorkspaceFilesView(path="", parent=None, entries=entries, truncated=truncated)


def _parse_porcelain(text: str) -> list[GitStatusEntryView]:
    """解析 `--porcelain=v1 -z`。rename/copy 的条目后面还跟着原路径，要跳过去。"""
    entries: list[GitStatusEntryView] = []
    parts = text.split("\0")
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if len(item) < 4:
            continue
        code = item[:2]
        entries.append(GitStatusEntryView(
            path=item[3:], index_status=code[0], worktree_status=code[1]
        ))
        if code[0] in ("R", "C"):
            index += 1
    return entries


def _list_tree(root: Path, base: Path) -> WorkspaceFilesView:
    """整棵树压成扁平列表：目录先于同级文件，按名字排序。

    **目录项也要输出**——只给文件的话，空目录在树上就消失了，用户会以为项目里没有那个文件夹。
    """
    entries: list[WorkspaceEntryView] = []
    truncated = False
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(name for name in dirs if name not in NOISE_DIRS)
        base_dir = Path(current)
        for name in dirs:
            if len(entries) >= MAX_TREE_ENTRIES:
                truncated = True
                break
            entries.append(WorkspaceEntryView(
                name=name, kind="dir", path=(base_dir / name).relative_to(root).as_posix(),
            ))
        if truncated:
            break
        for name in sorted(files):
            if len(entries) >= MAX_TREE_ENTRIES:
                truncated = True
                break
            target = base_dir / name
            try:
                stat = target.stat()
            except OSError:
                continue
            entries.append(WorkspaceEntryView(
                name=name, kind="file", size_bytes=int(stat.st_size),
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                path=target.relative_to(root).as_posix(),
            ))
        if truncated:
            break
    return WorkspaceFilesView(path=_relative(base, root), parent=None,
                              entries=entries, truncated=truncated)


def git_status(service: ApplicationService, *, workspace_id: str | None = None) -> GitStatusView:
    root = _root(service, workspace_id)
    try:
        result = run_git(["status", "--porcelain=v1", "-z"], root, GIT_TIMEOUT_SECONDS)
    except (GitError, SecurityError) as exc:
        return GitStatusView(available=False, note=str(exc)[:200])
    if result.exit_code != 0:
        # 不是仓库时 git 返回 128；如实说"没有版本控制"而不是报错。
        return GitStatusView(available=False, note=(result.stderr or "").strip()[:200] or None)
    return GitStatusView(available=True, entries=_parse_porcelain(result.stdout))


def git_diff(service: ApplicationService, *, path: str,
             workspace_id: str | None = None) -> GitDiffView:
    """单文件相对 `HEAD` 的 diff。未跟踪文件没有 diff——那是 status 的信息，不是这里编的。"""
    root = _root(service, workspace_id)
    target = (root / path).resolve() if path else root
    if not _inside(target, root):
        raise PermissionError("只能查看工作区内的文件。")
    relative = _relative(target, root)
    if not relative:
        raise ValueError("请指定一个文件。")
    try:
        result = run_git(["diff", "HEAD", "--", relative], root, GIT_TIMEOUT_SECONDS)
    except (GitError, SecurityError) as exc:
        return GitDiffView(available=False, path=relative, note=str(exc)[:200])
    if result.exit_code != 0:
        return GitDiffView(available=False, path=relative,
                           note=(result.stderr or "").strip()[:200] or None)
    patch = result.stdout
    truncated = len(patch) > MAX_DIFF_CHARS
    return GitDiffView(available=True, path=relative,
                       patch=patch[:MAX_DIFF_CHARS], truncated=truncated)

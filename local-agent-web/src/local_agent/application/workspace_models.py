"""右栏只读面板的视图（§21.6，2026-10-22）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from local_agent.application.data_center_models import DataView


class WorkspaceEntryView(DataView):
    name: str
    kind: Literal["file", "dir"]
    size_bytes: int = 0
    modified_at: str | None = None
    #: 相对工作区根的完整路径。**只在搜索结果里填**——目录列表的 `name` 本身就是名字，
    #: 搜索结果散布在不同层级，只给名字会让用户分不清是哪个 `index.ts`。
    path: str | None = None


class WorkspaceFilesView(DataView):
    #: 当前目录（相对工作区根）；根目录为空串。
    path: str
    #: 上级目录；已在根时为 `None`，前端据此禁用"上一级"。
    parent: str | None = None
    entries: list[WorkspaceEntryView] = Field(default_factory=list)
    truncated: bool = False


class GitStatusEntryView(DataView):
    path: str
    #: porcelain 的两列状态字符（`index_status` 是暂存区，`worktree_status` 是工作区）。
    index_status: str
    worktree_status: str


class GitStatusView(DataView):
    #: 工作区不是 git 仓库时为 `false`——这不是错误，只是没有版本控制。
    available: bool
    entries: list[GitStatusEntryView] = Field(default_factory=list)
    note: str | None = None


class GitDiffView(DataView):
    available: bool
    path: str
    patch: str | None = None
    truncated: bool = False
    note: str | None = None

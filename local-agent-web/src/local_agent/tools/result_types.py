"""工具返回结构（架构 §6.1、§6.2、§4.3）。

- ``ListDirResult`` / ``ReadFileResult``：工具返回给模型的 JSON 结构，字段按架构
  §6.1、§6.2 的要求组织（类型、大小、修改时间、编码、Hash、行数、截断状态）。
- ``ToolResultRecord``：单次工具调用在 State ``tool_results`` 中的一条记录。架构 §4.3
  未定义该结构的字段，这里给出当前阶段使用的字段集合（T08 会补幂等与证据字段）。
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, Field

EntryKind = Literal["file", "dir", "symlink", "other"]
ToolResultStatus = Literal["ok", "error", "denied"]


class DirEntry(BaseModel):
    """目录项。"""

    name: str
    path: str
    kind: EntryKind
    size_bytes: int | None = None
    modified_at: str | None = None


class ListDirResult(BaseModel):
    """``list_dir`` 的返回结构。"""

    path: str
    entries: list[DirEntry] = Field(default_factory=list)
    total_entries: int = 0
    truncated: bool = False
    note: str | None = None


class ReadFileResult(BaseModel):
    """``read_file`` 的返回结构。"""

    path: str
    encoding: str | None = None
    sha256: str | None = None
    line_count: int = 0
    truncated: bool = False
    is_binary: bool = False
    content: str = ""
    note: str | None = None


class EditFileResult(BaseModel):
    """``edit_file`` 的返回结构（架构 §6.3）。

    审计产物统一是 ``diff``；``old_sha256`` / ``new_sha256`` 供反思阶段机械校验
    "Hash 是否真的变了"。
    """

    path: str
    reason: str = ""
    replaced_count: int = 0
    changed_lines: int = 0
    old_sha256: str | None = None
    new_sha256: str | None = None
    diff: str = ""
    line_count: int = 0
    note: str | None = None


class WriteFileResult(BaseModel):
    """``write_file`` 的返回结构（架构 §6.4）。"""

    path: str
    reason: str = ""
    sha256: str | None = None
    size_bytes: int = 0
    line_count: int = 0
    note: str | None = None


class ShellResult(BaseModel):
    """``run_shell`` 的返回结构（架构 §6.6）。"""

    command: str
    argv: list[str] = Field(default_factory=list)
    cwd: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    truncated: bool = False
    note: str | None = None
    #: 执行方式：``argv`` 直执行，``cmd`` / ``pwsh`` / ``sh`` 表示经该适配器执行（T12）。
    executor: str = "argv"


class DeleteFileResult(BaseModel):
    """``delete_file`` 的返回结构（架构 §6.5）。"""

    path: str
    recursive: bool = False
    kind: str = "file"
    removed_files: int = 0
    removed_dirs: int = 0
    reason: str = ""


class ToolResultRecord(TypedDict):
    """进入 State 的单次工具调用结果记录。

    ``run_id`` 与 ``tool_call_id`` 一起构成作用域键：不同轮次复用同一个调用 ID 时，
    两条结果互不覆盖，也不会被误判成"这次已经执行过"（架构 §4.2）。
    """

    tool_call_id: str
    tool_name: str
    status: ToolResultStatus
    truncated: bool
    output_preview: str
    output_digest: str
    run_id: str

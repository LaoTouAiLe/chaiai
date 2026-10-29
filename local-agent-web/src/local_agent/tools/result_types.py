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
    #: 本次**实际返回**的行区间（1 基，含）与是否有后续（2026-09-30 新增）。
    #:
    #: 加它的原因是一个实测出来的工具缺陷：此前只有 ``line_count``（文件总行数）与
    #: ``truncated``（被截断了）。模型因此**知道"读不全"，却不知道这次读到了第几行**，
    #: 也就算不出下一次的 ``start_line``——它只能重猜或整篇重读。实测同一文件在一轮里被读了
    #: 两次（一次 ``truncated=false``、一次 ``true``），并自述"两个文件的内容均因长度限制只显示了
    #: 截断片段"，进而把"证据被截断"误当成"任务未完成"而反复空转。
    #:
    #: 补上区间之后，"继续读"变成一个**可执行动作**：下一次传
    #: ``start_line=next_start_line`` 即可，不需要猜。
    first_line: int = 0
    last_line: int = 0
    has_more: bool = False
    next_start_line: int | None = None
    #: 实际返回的不完整行，续读必须从该行开始而不能跳过。
    partial_lines: list[int] = Field(default_factory=list)
    start_char: int = 0
    next_start_char: int | None = None


#: ``grep`` 的输出模式：正文命中 / 只列文件名 / 只给计数。
GrepOutputMode = Literal["content", "files_with_matches", "count"]


class GrepMatch(BaseModel):
    """一条正文命中（``content`` 模式专用）。"""

    path: str
    line_number: int
    text: str


class GrepFileHits(BaseModel):
    """一个文件的命中汇总；三种输出模式都会给。"""

    path: str
    count: int


class GrepResult(BaseModel):
    """``grep`` 的返回结构（2026-09-30 新增）。

    ``hits`` 三种模式下都填（按命中数降序），``matches`` 只有 ``content`` 模式才填。这样模型可以
    先用 ``files_with_matches`` 判断"该读哪几个文件"、再用 ``read_file`` 定点读，而不是把整个仓库
    的命中行塞进上下文——这正是"项目多大都能读"与"成本可控"能同时成立的原因。
    """

    pattern: str
    path: str = "."
    glob: str | None = None
    output_mode: GrepOutputMode = "content"
    matches: list[GrepMatch] = Field(default_factory=list)
    hits: list[GrepFileHits] = Field(default_factory=list)
    total_matches: int = 0
    files_scanned: int = 0
    has_more: bool = False
    next_cursor: str | None = None
    skipped_files: dict[str, int] = Field(default_factory=dict)
    limited_lines: int = 0
    truncated: bool = False
    note: str | None = None


class GlobResult(BaseModel):
    """``glob`` 的返回结构（2026-09-30 新增）。"""

    pattern: str
    path: str = "."
    files: list[str] = Field(default_factory=list)
    total_files: int = 0
    has_more: bool = False
    next_cursor: str | None = None
    truncated: bool = False
    note: str | None = None


class WebSearchHit(BaseModel):
    """一条搜索结果：**只有来源信息，没有正文**（§6.10 的"先搜后取"）。"""

    title: str
    url: str
    #: 来源站点（URL 的 host）；让用户能一眼看出这条来自哪里。
    source: str
    snippet: str = ""
    source_id: str = ""
    published_at: str | None = None


class WebSearchResult(BaseModel):
    """``web_search`` 的返回结构（v0.5 T53）。"""

    query: str
    results: list[WebSearchHit] = Field(default_factory=list)
    truncated: bool = False
    note: str | None = None
    retrieved_at: str | None = None
    provider: str | None = None
    executed_query: str | None = None
    #: 本次结果对查询的覆盖度（2026-10-28，仅 HTML 兼容后端给）。
    #:
    #: 为什么要它：此前"搜到 8 条全都不相关"与"搜到 8 条高度相关"在返回结构上完全一样，
    #: 模型只能靠读内容自己发现，实测因此空转了 30 多次工具调用。``none`` 表示一条都不沾
    #: （通常是查询写法或后端能力问题，应当换词/换后端），``low`` 表示沾边但证据薄
    #: （可用于回答，但必须说明缺口），``high`` 表示多数结果是相关来源。
    coverage: Literal["high", "low", "none"] | None = None


class WebFetchResult(BaseModel):
    """``web_fetch`` 的返回结构（v0.5 T53）。

    ``text`` 是抽成纯文本的正文；``final_url`` 是跟随重定向之后的地址（可能与请求的不同）。
    被 ``max_chars`` 截断时 ``truncated=True`` 并写明续读方式。
    """

    url: str
    final_url: str | None = None
    title: str | None = None
    text: str = ""
    truncated: bool = False
    note: str | None = None
    retrieved_at: str | None = None
    source_id: str = ""
    total_chars: int = 0
    start_char: int = 0
    next_start_char: int | None = None
    content_version: str = ""
    json_structure: str | None = None
    selected_by_query: bool = False


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
    #: 本次写入的 unified diff（新建文件 = 整份内容都是新增行）。
    #: 为什么要它（2026-10-21）：此前 `write_file` 只有字节数与行数，页面回答不了
    #: "这个文件被写成了什么"，`edit_file` 却一直有 diff——同样是改文件，口径不该两样。
    diff: str = ""
    #: 本次**顺带新建**的父目录（工作区相对路径，由外到内）。空列表 = 父目录本来就存在。
    #: 为什么报出来：创建目录是这次调用新产生的副作用，用户与模型都该能核对（§15.8 的口径）。
    created_directories: list[str] = Field(default_factory=list)


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
    #: ``completed`` = 这一轮等到它结束；``running`` = 用户按了"转后台"，命令仍在跑（2026-10-21）。
    status: str = "completed"
    #: 后台命令的句柄；只有 ``status == "running"`` 时才有意义（`read_command` / `stop_command` 用它）。
    command_id: str | None = None


class DeleteFileResult(BaseModel):
    """``delete_file`` 的返回结构（架构 §6.5）。"""

    path: str
    recursive: bool = False
    kind: str = "file"
    removed_files: int = 0
    removed_dirs: int = 0
    reason: str = ""
    #: 是否移进了**系统回收站**（设置里的「安全删除」开启时）。为 `True` 时目标还能找回来
    #: （回收站右键「还原」回到原位置），模型据此说"可以还原"，而不是宣称"已彻底删除"。
    #: **没有** `trash_path`：系统回收站会把文件改名存进 `$RECYCLE.BIN`，那个内部路径用户点不开，
    #: 给出来只会让模型转述一个假位置。
    trashed: bool = False


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

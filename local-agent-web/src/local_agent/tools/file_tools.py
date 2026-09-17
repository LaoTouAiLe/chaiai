"""只读文件工具：``list_dir`` / ``read_file``（架构 §6.1、§6.2；开发文档 T05）。

- 工具逻辑写成普通函数（``list_dir_entries`` / ``read_file_content``），便于直接测试；
  ``build_file_tools`` 再包一层 ``@tool``，通过闭包持有 :class:`SecurityPolicy`。
- 所有路径都经策略校验：工作区边界 + 敏感文件拦截（架构 §6.1、§11.1）。
- 工具出错时**不吞异常**：由 ``tool_execution_node`` 统一捕获并回填给模型，
  这样错误既不会被隐藏，也不会中断整张图。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from local_agent.exceptions import SecurityError
from local_agent.security.approval import action_fingerprint
from local_agent.security.diff_guard import build_unified_diff, count_changed_lines
from local_agent.security.path_guard import assert_not_protected_target, is_within
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.result_types import (
    DeleteFileResult,
    DirEntry,
    EditFileResult,
    EntryKind,
    ListDirResult,
    ReadFileResult,
    WriteFileResult,
)

#: 递归列目录的最大深度（架构 §6.1：限制递归深度）。
MAX_RECURSIVE_DEPTH = 5

#: 工具输出进入 State 的预览长度（架构 §11.2：工具输出截断）。
OUTPUT_PREVIEW_CHARS = 2000


def list_dir_entries(
    path: str = ".",
    *,
    policy: SecurityPolicy,
    recursive: bool = False,
    include_hidden: bool = False,
    max_entries: int = 200,
) -> ListDirResult:
    """列出目录项。

    Args:
        path: 相对工作区根目录的路径或绝对路径；配置允许时可使用显式绝对的外部路径。
        policy: 安全策略，负责边界与敏感文件判定。
        recursive: 是否递归列出子目录，最多 :data:`MAX_RECURSIVE_DEPTH` 层。
        include_hidden: 是否包含以点开头的隐藏项。
        max_entries: 最多返回多少条；**达到上限即停止遍历**，不会先走完整棵树。

    Returns:
        目录项列表、总数与截断标记。

    Raises:
        SecurityError: 路径越界、目标不是目录。
    """
    resolved = policy.resolve_path(path)
    if not resolved.is_dir():
        raise SecurityError(f"目标不是目录或不存在：{resolved.as_posix()}")

    candidates: list[Path] = []
    depth_limited = False
    truncated = False
    if recursive:
        # 有界遍历：显式栈逐层展开，达到条目上限或深度上限**立即停止**；每一层也只读"剩余
        # 额度 + 1"项再排序，不先枚举整个目录。否则在大目录（尤其外部盘符/主目录）上会遍历
        # 整棵树、占满内存并长时间无响应，配置里的上限就不是真正的执行限制。
        stack: list[tuple[Path, int]] = [(resolved, 0)]
        while stack and not truncated:
            current, depth = stack.pop()
            raw, stream_stopped = _bounded_children(current, max_entries - len(candidates))
            if stream_stopped:
                truncated = True  # 目录里还有没读的条目：结果不完整，必须如实标注
            for child in sorted(raw):
                if not include_hidden and child.name.startswith("."):
                    continue
                if child.is_dir() and depth + 1 > MAX_RECURSIVE_DEPTH:
                    # 超深目录**本身**也不进结果，才与"超过 N 层的条目已省略"一致
                    depth_limited = True
                    continue
                candidates.append(child)
                if len(candidates) >= max_entries:
                    truncated = True
                    break
                if not child.is_dir():
                    continue
                # 目录符号链接 / Windows Junction 可能指向工作区外：**入栈前解析真实目标**，
                # 只有仍位于本次列举的根目录内才继续。只看 ``is_symlink()`` 挡不住 Junction
                # （它未必被识别为符号链接），严格模式下就会泄露外部目录里的文件名与大小。
                try:
                    real = child.resolve(strict=True)
                except OSError:
                    continue
                if not is_within(real, resolved):
                    continue
                stack.append((child, depth + 1))
    else:
        raw, stream_stopped = _bounded_children(resolved, max_entries)
        candidates = [
            candidate
            for candidate in sorted(raw)
            if include_hidden or not candidate.name.startswith(".")
        ]
        if stream_stopped:
            truncated = True

    total = len(candidates)
    entries = [
        _dir_entry(candidate, resolved, policy.workspace_root)
        for candidate in candidates[:max_entries]
    ]

    notes: list[str] = []
    if truncated:
        notes.append(
            f"结果已截断：已达上限，只返回前 {max_entries} 项，未继续遍历剩余目录"
        )
    elif total > max_entries:
        notes.append(f"结果已截断：共 {total} 项，只返回前 {max_entries} 项")
    if depth_limited:
        notes.append(f"递归深度超过 {MAX_RECURSIVE_DEPTH} 层的条目已省略")

    return ListDirResult(
        path=_display_path(resolved, policy.workspace_root),
        entries=entries,
        total_entries=total,
        truncated=bool(notes),
        note="；".join(notes) if notes else None,
    )


def read_file_content(
    path: str,
    *,
    policy: SecurityPolicy,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int = 50000,
) -> ReadFileResult:
    """读取工作区内的文本文件。

    Args:
        path: 相对路径以当前工作区为根；配置允许外部路径时可使用显式绝对路径，
            且不是敏感文件。
        policy: 安全策略。
        start_line: 起始行（1 基，含）；省略表示从头开始。
        end_line: 结束行（1 基，含）；省略表示到文件末尾。
        max_chars: 返回内容的最大字符数，超出时截断。

    Returns:
        文件内容、编码、Hash、行数与截断状态。

    Raises:
        SecurityError: 路径越界、命中敏感文件、目标不是文件。
        ValueError: 行号参数非法。
    """
    resolved = policy.resolve_readable_file(path)
    max_bytes = policy.workspace.max_file_size_bytes
    size = resolved.stat().st_size

    with resolved.open("rb") as handle:
        data = handle.read(max_bytes)
    size_limited = size > len(data)

    encoding: str | None = None
    text = ""
    is_binary = False
    note_parts: list[str] = []

    if b"\x00" in data[:4096]:
        # 含 NUL 字节一律按二进制处理：这类内容即使能“解码成功”也不该注入 Prompt。
        is_binary = True
        note_parts.append("二进制文件，未返回内容")
    else:
        try:
            text = data.decode("utf-8-sig")
            encoding = "utf-8"
        except UnicodeDecodeError:
            note_parts.append("无法按 UTF-8 解码（未知编码），未返回内容")

    line_count = len(text.splitlines())
    selected = _select_lines(text, start_line=start_line, end_line=end_line)
    truncated = False
    if len(selected) > max_chars:
        selected = selected[:max_chars]
        truncated = True
        note_parts.append(f"内容已截断到 {max_chars} 字符")
    if size_limited:
        truncated = True
        note_parts.append(f"文件超过 {max_bytes} 字节，只读取了前 {len(data)} 字节")

    return ReadFileResult(
        path=_display_path(resolved, policy.workspace_root),
        encoding=encoding,
        sha256=hashlib.sha256(data).hexdigest(),
        line_count=line_count,
        truncated=truncated,
        is_binary=is_binary,
        content=selected,
        note="；".join(note_parts) if note_parts else None,
    )


def edit_file_content(
    path: str,
    *,
    policy: SecurityPolicy,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    expected_sha256: str | None = None,
    reason: str = "",
) -> EditFileResult:
    """用精确串替换修改工作区内已有文件（架构 §6.3）。

    安全约束（全部在代码层强制）：

    - 必须提供与当前文件一致的 ``expected_sha256``，否则拒绝并提示重新读取；
    - ``old_text`` 必须精确匹配且默认只允许一处，多处匹配必须显式 ``replace_all``；
    - 不接受用整份文件内容做替换；
    - 改动行数超过 ``security.large_edit.max_changed_lines_without_approval`` 时按大范围修改拒绝；
    - 先写同目录临时文件再原子替换，写入后重新计算 Hash。

    Args:
        path: 工作区内的目标文件。
        policy: 安全策略。
        old_text: 要被替换的原文片段。
        new_text: 替换后的文本。
        replace_all: 是否替换全部匹配。
        expected_sha256: 调用方读到的文件 Hash。
        reason: 修改原因，写入审计结果。

    Returns:
        修改结果与 unified diff。

    Raises:
        SecurityError: 任何安全校验不通过。
    """
    arguments = {
        "path": path,
        "old_text": old_text,
        "new_text": new_text,
        "replace_all": replace_all,
    }
    # 先解析目标：越界（WorkspaceBoundaryError）、敏感文件与受保护目标（SecurityError）
    # 必须在看审批之前就失败闭合；判定、指纹与执行三处都用这一个规范化路径（架构 §1.5）。
    resolved = policy.resolve_writable_file(path)
    decision = policy.classify_tool("edit_file", arguments)
    # 大范围修改属于强制审批动作：只有持有效人工批准（同一次调用 + 指纹一致）才执行
    if not decision.allowed and not policy.is_granted(
        action_fingerprint("edit_file", arguments, policy=policy)
    ):
        raise SecurityError(decision.reason)
    data = resolved.read_bytes()
    original = _decode_text(data)
    current_sha256 = hashlib.sha256(data).hexdigest()

    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        raise SecurityError(
            "修改已有文件必须提供 expected_sha256：请先用 read_file 读取该文件，"
            "并把返回的 sha256 原样带上"
        )
    if expected != current_sha256:
        raise SecurityError(
            f"expected_sha256 与当前文件不一致（当前为 {current_sha256[:12]}…）："
            "文件已被改动，请重新读取后再修改"
        )
    if not old_text:
        raise SecurityError("old_text 不能为空")
    if old_text == new_text:
        raise SecurityError("old_text 与 new_text 相同，没有需要修改的内容")
    # 架构 §6.3：不接受用整份文件内容做替换。单行文件例外——那一行本身就是最小的唯一锚点，
    # 否则会出现"文件只有一行就没法改"的怪现象。
    if len(original.splitlines()) > 1 and old_text.rstrip("\n") == original.rstrip("\n"):
        raise SecurityError(
            "不接受用整份文件内容做替换：请给出更小的唯一片段（架构 §6.3）"
        )

    occurrences = original.count(old_text)
    if occurrences == 0:
        raise SecurityError(
            "old_text 在文件中没有精确匹配：请核对空格、缩进与换行（Windows 文件可能是 CRLF）"
        )
    if occurrences > 1 and not replace_all:
        raise SecurityError(
            f"old_text 匹配到 {occurrences} 处：请提供更长的唯一片段，或显式设置 replace_all=true"
        )

    updated = (
        original.replace(old_text, new_text)
        if replace_all
        else original.replace(old_text, new_text, 1)
    )
    diff = build_unified_diff(original, updated, path=resolved.name)
    if not diff.strip():
        raise SecurityError("计算出的 diff 为空，没有可写入的改动")

    new_data = updated.encode("utf-8")
    _atomic_write(resolved, new_data)

    return EditFileResult(
        path=_display_path(resolved, policy.workspace_root),
        reason=reason,
        replaced_count=occurrences if replace_all else 1,
        changed_lines=count_changed_lines(original, updated),
        old_sha256=current_sha256,
        new_sha256=hashlib.sha256(new_data).hexdigest(),
        diff=diff,
        line_count=len(updated.splitlines()),
    )


def write_file_content(
    path: str,
    *,
    policy: SecurityPolicy,
    content: str,
    overwrite: bool = False,
    reason: str = "",
) -> WriteFileResult:
    """在工作区内创建新文件（架构 §6.4）。

    只允许创建新文件：目标已存在时拒绝并提示改用 ``edit_file``；覆盖已有文件属于强制
    审批动作，在 T10 接入审批前一律拒绝。

    Args:
        path: 相对路径以当前工作区为根；配置允许外部路径时可使用显式绝对路径，工作区外写入需要人工审批。
        policy: 安全策略。
        content: 文件内容。
        overwrite: 是否允许覆盖已有文件（本版本只用于给出更明确的拒绝原因）。
        reason: 创建原因，写入审计结果。

    Returns:
        创建结果与文件 Hash。

    Raises:
        SecurityError: 任何安全校验不通过。
    """
    arguments = {"path": path, "content": content, "overwrite": overwrite}
    # 先解析目标：越界、敏感文件与受保护目标在看审批之前失败闭合（架构 §1.5）
    resolved = policy.resolve_writable_path(path)
    decision = policy.classify_tool("write_file", arguments)
    # 覆盖已有文件与工作区外写入都属于强制审批动作：只有持有效人工批准才执行
    granted = policy.is_granted(action_fingerprint("write_file", arguments, policy=policy))
    if not decision.allowed and not granted:
        raise SecurityError(decision.reason)
    overwriting = resolved.exists()
    if overwriting:
        if resolved.is_dir():
            raise SecurityError(f"目标是一个目录，不能用 write_file 写入：{resolved.as_posix()}")
        if not overwrite:
            raise SecurityError(
                f"文件已存在：{resolved.as_posix()}。修改现有文件请用 edit_file"
                "（精确串替换），不要整体覆盖"
            )
        if not granted:
            raise SecurityError(
                "覆盖已有文件需要人工审批（架构 §6.4）：未获批准，已拒绝覆盖"
            )
    elif not resolved.parent.is_dir():
        raise SecurityError(f"父目录不存在，未创建文件：{resolved.parent.as_posix()}")

    data = content.encode("utf-8")
    if len(data) > policy.workspace.max_file_size_bytes:
        raise SecurityError(
            f"内容 {len(data)} 字节超过上限 {policy.workspace.max_file_size_bytes} 字节，已拒绝写入"
        )

    if overwriting:
        _atomic_write(resolved, data)  # 覆盖是已批准的动作，用原子替换
    else:
        _create_new_file(resolved, data)  # 新建绝不覆盖：原子创建，已存在即失败

    return WriteFileResult(
        path=_display_path(resolved, policy.workspace_root),
        reason=reason,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        line_count=len(content.splitlines()),
    )


def delete_file_target(
    path: str,
    *,
    policy: SecurityPolicy,
    recursive: bool = False,
    reason: str = "",
) -> DeleteFileResult:
    """删除文件或目录：只有持有效人工批准时才真正执行（架构 §6.5）。

    三层防护：

    1. 没有审批记录时直接抛错（T10 之前的行为，也防止绕过安全门直接调用工具）；
    2. 根目录、Git 目录、工作区根目录禁止删除；
    3. 目录删除必须显式 ``recursive=True``，批准是一次性的（执行后立即撤销）。

    Raises:
        SecurityError: 无有效批准、目标非法或删除失败。
    """
    arguments = {"path": path, "recursive": recursive}
    decision = policy.classify_tool("delete_file", arguments)
    if not decision.allowed and not decision.requires_approval:
        # 硬拒绝（受保护目标、越界、敏感文件…）：立即失败，**不算指纹、不描述目标**——
        # 这两件事都要遍历目标目录，对主目录那种目标会直接卡死（T14C 复审）。
        raise SecurityError(policy.redact(decision.reason))

    fingerprint = action_fingerprint("delete_file", arguments, policy=policy)
    if not policy.is_granted(fingerprint):
        resolved_for_message = policy.resolve_path(path)
        target = _describe_target(resolved_for_message, recursive)
        detail = f"；原因：{reason}" if reason else ""
        raise SecurityError(
            f"{decision.reason}。目标：{resolved_for_message.as_posix()}（{target}）{detail}"
        )

    resolved = policy.resolve_path(path)
    _assert_deletable(resolved, policy.workspace_root, recursive)
    kind = "dir" if resolved.is_dir() else "file"
    removed_files = 0
    removed_dirs = 0
    if kind == "dir":
        removed_files = sum(1 for item in resolved.rglob("*") if item.is_file())
        removed_dirs = sum(1 for item in resolved.rglob("*") if item.is_dir())
        shutil.rmtree(resolved)
    else:
        removed_files = 1
        resolved.unlink()

    return DeleteFileResult(
        path=_display_path(resolved, policy.workspace_root),
        recursive=recursive,
        kind=kind,
        removed_files=removed_files,
        removed_dirs=removed_dirs,
        reason=reason,
    )


def _assert_deletable(path: Path, workspace_root: Path, recursive: bool) -> None:
    """架构 §6.5、§1.5：工作区根、根/主目录/系统目录与 Git 目录禁止删除。

    这里是**执行前**的复核：即使已经拿到人工批准，受保护目标也照样拒绝（架构 §1.5 的
    "不可审批目标"），因此判定、指纹与执行三处都以同一个规范化路径为准。
    """
    if path == _normalized_root(workspace_root):
        raise SecurityError("禁止删除工作区根目录")
    if path.name == ".git" or ".git" in path.parts:
        raise SecurityError("禁止删除 Git 目录")
    assert_not_protected_target(path, verb="删除")
    if not path.exists():
        raise SecurityError(f"目标不存在：{path.as_posix()}")
    if path.is_dir() and not recursive:
        raise SecurityError("删除目录必须显式设置 recursive=true")


def _normalized_root(workspace_root: Path) -> Path:
    try:
        return workspace_root.resolve()
    except OSError:  # pragma: no cover - 极少数无法解析的路径
        return workspace_root.absolute()


def build_file_tools(policy: SecurityPolicy) -> list[BaseTool]:
    """按安全策略构造文件工具。

    Returns:
        可直接绑定给模型的工具列表；写类工具只包含本版本放行的操作。
    """

    @tool
    def list_dir(
        path: str = ".",
        recursive: bool = False,
        include_hidden: bool = False,
        max_entries: int = 200,
    ) -> str:
        """列出目录项，返回名称、类型、大小与修改时间。

        Args:
            path: 相对路径以当前工作区为根；配置允许外部路径时可使用显式绝对路径（工作区外操作需要人工审批）。
            recursive: 是否递归列出子目录，最多 5 层。
            include_hidden: 是否包含以点开头的隐藏项。
            max_entries: 最多返回多少条。
        """
        return list_dir_entries(
            path,
            policy=policy,
            recursive=recursive,
            include_hidden=include_hidden,
            max_entries=max_entries,
        ).model_dump_json()

    @tool
    def read_file(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_chars: int = 50000,
    ) -> str:
        """读取某个文本文件的内容，返回内容、编码、Hash 与行数。

        Args:
            path: 相对路径以当前工作区为根；配置允许外部路径时可使用显式绝对路径（工作区外操作需要人工审批），且不能是密钥文件。
            start_line: 起始行（1 基，含）；省略表示从头开始。
            end_line: 结束行（1 基，含）；省略表示到文件末尾。
            max_chars: 返回内容的最大字符数，超出时截断。
        """
        return read_file_content(
            path,
            policy=policy,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
        ).model_dump_json()

    @tool
    def edit_file(
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        expected_sha256: str | None = None,
        reason: str = "",
    ) -> str:
        """用精确字符串替换修改已有文件，返回变更行数与 unified diff。

        Args:
            path: 相对路径以当前工作区为根；配置允许外部路径时可使用显式绝对路径，工作区外修改需要人工审批，且不能是密钥文件。
            old_text: 要被替换的原文片段，必须与文件内容精确一致（含缩进与换行）。
            new_text: 替换后的文本。
            replace_all: 是否替换全部匹配；默认要求 old_text 在文件中唯一。
            expected_sha256: 先用 read_file 读取该文件得到的 sha256，必填。
            reason: 修改原因。
        """
        return edit_file_content(
            path,
            policy=policy,
            old_text=old_text,
            new_text=new_text,
            replace_all=replace_all,
            expected_sha256=expected_sha256,
            reason=reason,
        ).model_dump_json()

    @tool
    def write_file(path: str, content: str, overwrite: bool = False, reason: str = "") -> str:
        """创建新文件，返回文件 Hash 与大小；不能覆盖已有文件。

        Args:
            path: 相对路径以当前工作区为根；配置允许外部路径时可使用显式绝对路径，工作区外写入需要人工审批。
            content: 文件内容。
            overwrite: 是否覆盖已有文件；覆盖属于强制审批动作，本版本会被拒绝。
            reason: 创建原因。
        """
        return write_file_content(
            path, policy=policy, content=content, overwrite=overwrite, reason=reason
        ).model_dump_json()

    @tool
    def delete_file(path: str, recursive: bool = False, reason: str = "") -> str:
        """删除文件或目录；必须经过人工审批，未获批准时一律拒绝。

        Args:
            path: 相对路径以当前工作区为根；配置允许外部路径时可使用显式绝对路径，工作区外删除需要人工审批。
            recursive: 是否递归删除目录（删除目录必须显式开启）。
            reason: 删除原因。
        """
        return delete_file_target(
            path, policy=policy, recursive=recursive, reason=reason
        ).model_dump_json()

    return [list_dir, read_file, edit_file, write_file, delete_file]


def _decode_text(data: bytes) -> str:
    """把文件字节解码为文本；二进制或非 UTF-8 内容一律拒绝编辑。"""
    if b"\x00" in data[:4096]:
        raise SecurityError("目标文件是二进制内容，不支持编辑")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SecurityError("目标文件不是 UTF-8 文本（未知编码），不支持编辑") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    """先写同目录临时文件再原子替换，避免写到一半留下损坏文件（架构 §6.3）。"""
    handle_fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _create_new_file(path: Path, data: bytes) -> None:
    """以 ``O_CREAT | O_EXCL`` 原子创建新文件：目标已存在时失败，**绝不覆盖**。

    "先检查存在、再写入"存在时间窗：若另一进程在两步之间创建了同名文件，
    普通写入会把它的内容覆盖掉，而这次写入并没有经过任何审批（架构 §6.4）。
    ``O_EXCL`` 把"检查 + 创建"合成一个原子操作，从根上关掉这个时间窗。

    Raises:
        SecurityError: 目标已存在（含并发创建），或创建失败。
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle_fd = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise SecurityError(
            f"文件已存在：{path.as_posix()}。写入过程中它被创建或改动过，已拒绝覆盖"
            "（改现有文件请用 edit_file）"
        ) from exc
    except OSError as exc:
        raise SecurityError(f"创建文件失败（{type(exc).__name__}）：{exc}") from exc
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


#: 删除目标"影响范围"的扫描上限：这是给用户看的有界摘要，不是精确清点。
DELETE_IMPACT_SCAN_LIMIT = 500


def _bounded_children(directory: Path, limit: int) -> tuple[list[Path], bool]:
    """读取目录的前 ``limit + 1`` 个条目（**不排序**），返回 ``(条目, 是否还有未读条目)``。

    不能直接 ``sorted(directory.iterdir())``：那会先把整个目录枚举并保存下来再裁剪，条目上限
    只限制返回数量、挡不住超大目录（外部盘符、用户主目录）的卡顿与内存占用。这里只多读一项
    用于判断"是否被截断"，随后只对这个小集合排序。
    """
    items: list[Path] = []
    stopped = False
    try:
        stream = directory.iterdir()
        for child in stream:
            items.append(child)
            if len(items) > limit:
                stopped = True
                break
    except OSError:  # 不可读的目录按"空目录"处理，不影响其余结果
        return items, stopped
    return items, stopped


def _describe_target(path: Path, recursive: bool) -> str:
    """描述删除目标的影响范围（架构 §6.5 要求展示数量与范围，但有界）。

    目录只做**一次有上限**的扫描（:data:`DELETE_IMPACT_SCAN_LIMIT` 条就停并标"至少"）：这是给
    用户看的未批准/审批提示，不该为了精确数量遍历整棵目录树（大目录、主目录会卡住，T14C 复审）。
    """
    if not path.exists():
        return "目标不存在"
    if path.is_file():
        return f"文件，{path.stat().st_size} 字节"
    if not recursive:
        return "目录（未开启递归，不会删除）"
    files = 0
    directories = 0
    for item in path.rglob("*"):
        if item.is_dir():
            directories += 1
        elif item.is_file():
            files += 1
        if files + directories >= DELETE_IMPACT_SCAN_LIMIT:
            return (
                f"目录，至少含 {files} 个文件与 {directories} 个子目录"
                "（已达扫描上限，影响范围为有界摘要；递归删除需要二次确认）"
            )
    return f"目录，含 {files} 个文件与 {directories} 个子目录（递归删除需要二次确认）"


def _select_lines(text: str, *, start_line: int | None, end_line: int | None) -> str:
    """按 1 基行号截取文本；两个参数都为空时原样返回。"""
    if start_line is None and end_line is None:
        return text
    if start_line is not None and start_line < 1:
        raise ValueError("start_line 必须从 1 开始")
    if end_line is not None and start_line is not None and end_line < start_line:
        raise ValueError("end_line 不能小于 start_line")

    lines = text.splitlines(keepends=True)
    start_index = (start_line or 1) - 1
    end_index = end_line if end_line is not None else len(lines)
    return "".join(lines[start_index:end_index])


def _dir_entry(path: Path, root: Path, workspace_root: Path) -> DirEntry:
    kind: EntryKind = "other"
    if path.is_symlink():
        kind = "symlink"
    elif path.is_dir():
        kind = "dir"
    elif path.is_file():
        kind = "file"

    size: int | None = None
    modified: str | None = None
    try:
        stat = path.stat()
        size = stat.st_size if path.is_file() else None
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except OSError:  # pragma: no cover - 权限或竞态导致的读取失败
        pass

    return DirEntry(
        name=path.name,
        path=_display_path(path, workspace_root),
        kind=kind,
        size_bytes=size,
        modified_at=modified,
    )


def _is_hidden_relative(path: Path, root: Path) -> bool:
    """路径相对 ``root`` 的任意一段是否以点开头。"""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part.startswith(".") for part in parts)


def _depth(path: Path, root: Path) -> int:
    try:
        return len(path.relative_to(root).parts)
    except ValueError:  # pragma: no cover - 理论不可达
        return MAX_RECURSIVE_DEPTH + 1


def _display_path(path: Path, workspace_root: Path) -> str:
    """展示用路径：工作区内用相对路径，避免把绝对路径写进上下文。"""
    try:
        return path.relative_to(workspace_root).as_posix() or "."
    except ValueError:
        return path.as_posix()

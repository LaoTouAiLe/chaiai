"""有界片段读取（架构 §4.3、§7.2；开发文档 T21）。

检索只把**命中位置附近**的一小段代码送进 Prompt，绝不整文件注入：片段的字符数、行数
（由 ``rag.context_lines`` 决定）与单文件字节数都有上限，超限即截断并如实标注。

自动 RAG 单独保持工作区边界，敏感文件判定复用 :class:`SecurityPolicy`；
普通文件工具允许显式外部读取，不会扩大自动检索的范围。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from local_agent.config import RagConfig
from local_agent.exceptions import FileOperationError, SecurityError
from local_agent.security.path_guard import resolve_within_workspace
from local_agent.security.policy import SecurityPolicy

#: 判断"二进制文件"的 NUL 探测窗口（与 ``read_file`` 同一口径）。
_BINARY_PROBE_BYTES = 4096


@dataclass(frozen=True)
class Snippet:
    """一条已经取好的片段：行号区间 + 带行号的文本。"""

    relative_path: str
    line_start: int
    line_end: int
    text: str
    note: str | None = None


def read_snippet(
    path: Path,
    *,
    relative_path: str,
    line_number: int | None,
    config: RagConfig,
    policy: SecurityPolicy,
) -> Snippet | None:
    """读取"命中处前后各 ``context_lines`` 行"的片段。

    文件/路径命中（``line_number is None``）取文件**开头**的同样行数——那些命中没有具体行，
    给文件头部足以让模型判断"这是不是要找的文件"。

    Args:
        path: 命中的文件（工作区内）。
        relative_path: 相对工作区的展示路径（写进 ``RAGReference.path``）。
        line_number: 命中的 1 基行号；``None`` 表示文件名/路径命中。
        config: ``rag.*`` 限额（``context_lines`` / ``snippet_chars`` / ``max_file_bytes``）。
        policy: 安全策略，负责工作区边界与敏感文件判定。

    Returns:
        片段；文件不可读、越界、敏感、二进制、非 UTF-8、超大或命中行已不存在时返回 ``None``
        （调用方据此丢弃这条候选，不猜内容）。
    """
    try:
        bounded = resolve_within_workspace(str(path), base_dir=policy.workspace_root, workspace_root=policy.workspace_root)
        resolved = policy.resolve_readable_file(str(bounded))
    except (FileOperationError, SecurityError):
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > config.max_file_bytes:
        return None
    try:
        data = resolved.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:_BINARY_PROBE_BYTES]:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None

    lines = text.splitlines()
    if not lines:
        return None
    span = max(int(config.context_lines), 0)
    if line_number is None or line_number < 1:
        start = 1
        end = min(len(lines), 1 + span * 2)
    else:
        if line_number > len(lines):
            # 命中行已不存在（文件在检索与读取之间被改动）：丢弃，不返回错位的片段。
            return None
        start = max(1, line_number - span)
        end = min(len(lines), line_number + span)

    rendered: list[str] = []
    used = 0
    truncated = False
    last = start - 1
    for number in range(start, end + 1):
        entry = f"{number}: {lines[number - 1]}"
        separator = 1 if rendered else 0
        if rendered and used + separator + len(entry) > config.snippet_chars:
            truncated = True
            break
        if not rendered and len(entry) > config.snippet_chars:
            # 单行就超预算：截断这一行（保留行号），避免整段退化成空片段。
            entry = entry[: config.snippet_chars]
            truncated = True
        rendered.append(entry)
        used += separator + len(entry)
        last = number
    if not rendered:
        return None
    return Snippet(
        relative_path=relative_path,
        line_start=start,
        line_end=last,
        text="\n".join(rendered),
        note="片段已按字符上限截断" if truncated else None,
    )

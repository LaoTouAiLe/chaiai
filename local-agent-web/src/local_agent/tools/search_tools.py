"""可调预算的流式文本搜索与路径分页；省略内容明确报告，游标始终复核路径策略。"""

from __future__ import annotations

import codecs
import fnmatch
import json
import math
import re
import time
from collections.abc import Iterator
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from local_agent.security.path_guard import display_path, is_within
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.result_spill import (
    SPILL_FAILED_NOTE,
    save_result,
    spill_key,
    spill_note,
)
from local_agent.tools.result_types import (
    GlobResult,
    GrepFileHits,
    GrepMatch,
    GrepOutputMode,
    GrepResult,
)
from local_agent.tools.search_cursor import decode_cursor, encode_cursor, query_key, resume_files
from local_agent.tools.text_stream import BinaryTextError, text_lines

#: 单次检索最多扫描的文件数；超出即停止并如实标注。
MAX_SCAN_FILES = 2000
#: ``glob`` 默认最多返回的文件数。
DEFAULT_GLOB_FILES = 500
#: 单次检索的墙钟上限（秒）。
SCAN_TIMEOUT_SECONDS = 20.0
#: ``grep`` 默认返回的最大命中条数。
DEFAULT_MAX_MATCHES = 100
#: 大量结果落盘时的文件计数预览上限。
MAX_HITS_FILES = 500
#: 内容命中单行展示的长度上限：超长行（压缩产物、单行 JSON）不该把上下文撑爆。
MAX_MATCH_LINE_CHARS = 300
#: 参与正则匹配的一行**最多看前多少字符**：既限制展示，也压低病态正则的回溯面。
MAX_SEARCHED_LINE_CHARS = 4_000
#: 模式串长度上限（``grep`` 与 ``glob`` 共用）。
MAX_PATTERN_CHARS = 200


def _resolve_search_root(policy: SecurityPolicy, path: str) -> Path:
    """解析检索根；默认工作区，也接受显式本机外部绝对路径，可以是目录或单文件。

    2026-09-30 修：此前要求必须是目录，于是模型最自然的用法——"在这个文件里找"（``path='README.md'``）
    ——被拒，界面上弹出"工具被安全策略拒绝: grep"，文案还把它说成"改为在工作区内操作"（其实路径就在
    工作区内，只是不是目录）。实测一轮里 4 次调用被这样拒掉，模型只好自己改写成
    ``glob='README.md'`` 绕过去。``rg`` 本身就是这个语义（``rg pattern file.py``），没有理由拆开。

    相对越界、设备路径和外部网络路径仍然拒绝；路径不存在抛
    ``ValueError`` 而不是 ``SecurityError``——前者映射成"工具执行失败／检查工具参数"，后者会显示
    成"被安全策略拦截"，而"不存在"根本不是策略问题。
    """
    raw = (path or "").strip() or "."
    return policy.files.search_root(raw)


def _relative(path: Path, workspace_root: Path) -> str:
    """候选文件与本页展示基准均已解析；外部仍返回绝对路径。"""
    return display_path(path, workspace_root)


def match_glob(relative: str, pattern: str) -> bool:
    """路径模式匹配：既匹配**相对路径**也匹配**文件名**。

    单独用 ``fnmatch`` 匹配整条相对路径时 ``src/**/*.py`` 匹配不到 ``src/a.py``（``fnmatch`` 不是
    路径感知的，``**`` 退化成两个 ``*``，要求 ``src/`` 之后至少还有一层目录）。``**/`` 语义是
    "零个或多个目录"，所以额外试一次把它去掉的变体。
    """
    text = (pattern or "").strip().replace("\\", "/")
    if not text:
        return True
    candidates = {text, text.replace("**/", ""), text.lstrip("./")}
    name = Path(relative).name
    return any(
        fnmatch.fnmatch(relative, candidate) or fnmatch.fnmatch(name, candidate)
        for candidate in candidates
    )


def _iter_files(root: Path, policy: SecurityPolicy, *, glob: str | None = None) -> Iterator[Path]:
    """有界、可复现地遍历检索根下的候选文件。

    遍历本身在 :func:`local_agent.rag.walk.iter_candidate_files`：**跳过口径**（隐藏、依赖目录、
    `.gitignore`、符号链接、敏感文件）与**超限时怎么选**（同级轮转，见 §6.7）都在那里，与按需 RAG
    共用同一份实现——这里只补本工具特有的 ``glob`` 过滤。

    ``root`` 是**单个文件**时只产出它自己（``rg pattern file.py`` 的语义），跳过规则照旧适用——
    把 ``.env`` 当检索根同样不会返回内容。
    """
    workspace_root = policy.workspace_root
    # 工作区内保持既有模式基准；外部目录的模式相对于本次选定的检索根。
    pattern_root = workspace_root if is_within(root, workspace_root) else (root.parent if root.is_file() else root)
    for candidate in policy.files.search_files(root):
        if glob is not None and not match_glob(candidate.relative_to(pattern_root).as_posix(), glob):
            continue
        yield candidate


def _snippet(line: str, regex: re.Pattern[str]) -> str:
    """命中行的展示文本：有界，且**围绕命中位置**取窗口（长行不该只给开头）。"""
    stripped = line.rstrip("\n\r")
    if len(stripped) <= MAX_MATCH_LINE_CHARS:
        return stripped
    head = stripped
    found = regex.search(head)
    if found is None:
        return f"{head[:MAX_MATCH_LINE_CHARS]}…"
    window = MAX_MATCH_LINE_CHARS // 3
    start = max(0, found.start() - window)
    end = min(len(stripped), start + MAX_MATCH_LINE_CHARS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(stripped) else ""
    return f"{prefix}{stripped[start:end]}{suffix}"


def _compile(pattern: str, *, ignore_case: bool, kind: str) -> re.Pattern[str]:
    text = (pattern or "").strip()
    if not text:
        raise ValueError(f"{kind} 的 pattern 不能为空")
    if len(text) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern 过长（上限 {MAX_PATTERN_CHARS} 字符）：{len(text)}")
    try:
        return re.compile(text, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ValueError(f"正则不合法：{exc}") from exc


def _positive(name: str, value: float | None) -> None:
    if value is not None and (not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} 必须为正数或 null")


def grep_content(
    pattern: str,
    *,
    policy: SecurityPolicy,
    path: str = ".",
    glob: str | None = None,
    output_mode: GrepOutputMode = "content",
    ignore_case: bool = False,
    max_matches: int = DEFAULT_MAX_MATCHES,
    save_full: bool = False,
    cursor: str | None = None,
    max_scan_files: int | None = MAX_SCAN_FILES,
    max_file_bytes: int | None = None,
    max_line_chars: int | None = MAX_SEARCHED_LINE_CHARS,
    timeout_seconds: float | None = SCAN_TIMEOUT_SECONDS,
    encoding: str = "utf-8-sig",
) -> GrepResult:
    """流式检索文本页，预算可调；截断给出续查游标，跳过内容明确计数。"""
    if save_full:
        policy.require_result_spill()
    if output_mode not in ("content", "files_with_matches", "count"):
        raise ValueError("output_mode 必须为 content、files_with_matches 或 count")
    for name, value in (("max_matches", max_matches), ("max_scan_files", max_scan_files),
                        ("max_file_bytes", max_file_bytes), ("max_line_chars", max_line_chars),
                        ("timeout_seconds", timeout_seconds)):
        _positive(name, value)
    codecs.lookup(encoding)
    regex = _compile(pattern, ignore_case=ignore_case, kind="grep")
    root = _resolve_search_root(policy, path)
    display_root = policy.workspace_root.resolve()
    key = query_key("grep", str(root), pattern, glob, output_mode, ignore_case, encoding, max_line_chars)
    position = decode_cursor(cursor, key)
    files = resume_files(_iter_files(root, policy, glob=glob), position, policy)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    matches: list[GrepMatch] = []
    per_file: dict[str, int] = {}
    skipped: dict[str, int] = {}
    spilled: list[str] = []
    notes: list[str] = []
    scanned = total = limited_lines = 0
    next_cursor = None
    stop_reason = ""
    for file_path, start in files:
        if ((max_scan_files is not None and scanned >= max_scan_files)
                or (deadline is not None and time.monotonic() > deadline)
                or (not save_full and total >= max_matches)):
            next_cursor = encode_cursor(key, file_path, start, policy)
            stop_reason = (
                f"已达文件数上限 {max_scan_files}" if max_scan_files is not None and scanned >= max_scan_files
                else f"已达命中上限 {max_matches} 条" if not save_full and total >= max_matches
                else f"已达时间上限 {timeout_seconds} 秒"
            )
            break
        scanned += 1
        relative = _relative(file_path, display_root)
        try:
            # 一次 stat 拿到大小与 mtime：此前这里连续 stat 两次，而每次 stat 都要走一遍
            # `resolve()`（Windows 上是 realpath），单文件就多花一次全链解析（2026-10-21）。
            before = policy.files.stat(file_path)
            if max_file_bytes is not None and before.st_size > max_file_bytes:
                skipped["size_limit"] = skipped.get("size_limit", 0) + 1
                continue
            for number, line, limited in text_lines(file_path, policy, encoding, max_line_chars):
                if number < start:
                    continue
                if ((deadline is not None and time.monotonic() > deadline)
                        or (not save_full and total >= max_matches)):
                    next_cursor = encode_cursor(key, file_path, number, policy)
                    stop_reason = (f"已达命中上限 {max_matches} 条" if not save_full and total >= max_matches
                                   else f"已达时间上限 {timeout_seconds} 秒")
                    break
                limited_lines += int(limited)
                if regex.search(line) is None:
                    continue
                total += 1
                per_file[relative] = per_file.get(relative, 0) + 1
                if output_mode == "content" and len(matches) < max_matches:
                    matches.append(GrepMatch(path=relative, line_number=number, text=_snippet(line, regex)))
                if save_full:
                    spilled.append(f"{relative}:{number}:{line}")
        except BinaryTextError:
            skipped["binary"] = skipped.get("binary", 0) + 1
        except UnicodeError:
            skipped["encoding"] = skipped.get("encoding", 0) + 1
        except OSError:
            skipped["unreadable"] = skipped.get("unreadable", 0) + 1
        else:
            after = policy.files.stat(file_path)
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("检索期间文件已变化，请从头检索")
        if next_cursor is not None:
            break
    incomplete = bool(next_cursor or skipped or limited_lines)
    if next_cursor:
        notes.append(f"{stop_reason}；同级轮转采样，传 next_cursor 继续检索，可增加预算")
    if skipped:
        notes.append(f"跳过文件（按原因计数）：{skipped}；可调整大小预算或 encoding 后重新检索")
    if limited_lines:
        notes.append(f"{limited_lines} 行超出 max_line_chars，只匹配行首；可增加预算或设 null 后重新检索")
    if save_full:
        ref = save_result(
            workspace_root=policy.workspace_root, policy=policy, kind="grep",
            key=spill_key("grep", pattern, path, glob, output_mode, ignore_case, cursor, encoding, max_line_chars),
            lines=spilled, complete=not incomplete,
        )
        notes.append(spill_note(ref) if ref is not None else SPILL_FAILED_NOTE)
    if not total and not incomplete:
        notes.append("没有匹配")
    ranked = sorted(per_file.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) > MAX_HITS_FILES:
        notes.append(f"命中文件超过汇总上限 {MAX_HITS_FILES}；请用较小页面或落盘查看完整清单")
    return GrepResult(
        pattern=pattern.strip(), path=(path or ".").strip() or ".", glob=glob,
        output_mode=output_mode, matches=matches,
        hits=[GrepFileHits(path=name, count=count) for name, count in ranked[:MAX_HITS_FILES]],
        total_matches=total, files_scanned=scanned, truncated=incomplete,
        has_more=next_cursor is not None, next_cursor=next_cursor,
        skipped_files=skipped, limited_lines=limited_lines,
        note="；".join(notes) if notes else None,
    )


def glob_files(
    pattern: str,
    *,
    policy: SecurityPolicy,
    path: str = ".",
    max_files: int = DEFAULT_GLOB_FILES,
    save_full: bool = False,
    cursor: str | None = None,
    timeout_seconds: float | None = SCAN_TIMEOUT_SECONDS,
) -> GlobResult:
    """分页列出匹配文件；可调页面大小和时间预算，无固定 2000 项硬上限。"""
    if save_full:
        policy.require_result_spill()
    _positive("max_files", max_files)
    _positive("timeout_seconds", timeout_seconds)
    text = (pattern or "").strip()
    if not text:
        raise ValueError("glob 的 pattern 不能为空")
    if len(text) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern 过长（上限 {MAX_PATTERN_CHARS} 字符）：{len(text)}")
    root = _resolve_search_root(policy, path)
    display_root = policy.workspace_root.resolve()
    key = query_key("glob", str(root), text)
    position = decode_cursor(cursor, key)
    files = resume_files(_iter_files(root, policy, glob=text), position, policy)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    notes: list[str] = []
    found: list[str] = []
    spilled: list[str] = []
    next_cursor = None
    for file_path, start in files:
        if ((not save_full and len(found) >= max_files)
                or (deadline is not None and time.monotonic() > deadline)):
            next_cursor = encode_cursor(key, file_path, start, policy)
            break
        relative = _relative(file_path, display_root)
        if len(found) < max_files:
            found.append(relative)
        if save_full:
            spilled.append(relative)
    if next_cursor:
        notes.append("已达本页预算/上限；同级轮转采样，传 next_cursor 继续，可增加 max_files 或 timeout_seconds")
    if save_full:
        ref = save_result(
            workspace_root=policy.workspace_root, policy=policy, kind="glob",
            key=spill_key("glob", text, path, cursor), lines=spilled, complete=next_cursor is None,
        )
        notes.append(spill_note(ref) if ref is not None else SPILL_FAILED_NOTE)
    if not found and not next_cursor:
        notes.append("没有匹配的文件")
    return GlobResult(
        pattern=text, path=(path or ".").strip() or ".", files=found, total_files=len(found),
        truncated=next_cursor is not None, has_more=next_cursor is not None, next_cursor=next_cursor,
        note="；".join(notes) if notes else None,
    )


def build_search_tools(policy: SecurityPolicy) -> list[BaseTool]:
    """构造持有本轮策略的分页检索工具。"""

    @tool
    def grep(
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        output_mode: str = "content",
        ignore_case: bool = False,
        max_matches: int = DEFAULT_MAX_MATCHES,
        save_full: bool = False,
        cursor: str | None = None,
        max_scan_files: int | None = MAX_SCAN_FILES,
        max_file_bytes: int | None = None,
        max_line_chars: int | None = MAX_SEARCHED_LINE_CHARS,
        timeout_seconds: float | None = SCAN_TIMEOUT_SECONDS,
        encoding: str = "utf-8-sig",
    ) -> str:
        """按正则定位本机文本内容，返回路径、行号、分页位置及省略原因。

        Args:
            pattern: Python 正则，最多 200 字符。
            path: 工作区相对路径或显式本机外部绝对路径，目录或单个文件。
            glob: 可选路径过滤，如 **/*.py；不跟随链接，不搜索敏感、忽略或依赖目录。
            output_mode: content 返回命中行；files_with_matches 或 count 返回每页文件计数。
            ignore_case: 是否忽略大小写。
            max_matches: 每页命中行预算，默认 100，可增加。
            save_full: 显式将本次预算内命中落盘，note 给路径；只读档请用分页。
            cursor: 上页 next_cursor，保留查询参数；has_more 时继续。目录变化后应从头重查。
            max_scan_files: 每页扫描文件预算，默认 2000，可增加或设 null。
            max_file_bytes: 单文件大小预算，默认 null，不因文件大而跳过。
            max_line_chars: 每行参与匹配的字符预算，默认 4000；长行遗漏会报告，必要时增加或设 null。
            timeout_seconds: 文件/行间检查的时间预算，默认 20 秒，可增加或设 null。
            encoding: 默认 UTF-8（兼容 BOM），可明确传 gbk、utf-16 等。

        先搜索定位再定点读文件。各页计数只对应本页，不是整个仓库的总数。
        """
        return grep_content(
            pattern, policy=policy, path=path, glob=glob,
            output_mode=output_mode,  # type: ignore[arg-type]
            ignore_case=ignore_case, max_matches=max_matches, save_full=save_full, cursor=cursor,
            max_scan_files=max_scan_files, max_file_bytes=max_file_bytes, max_line_chars=max_line_chars,
            timeout_seconds=timeout_seconds, encoding=encoding,
        ).model_dump_json()

    @tool
    def glob(
        pattern: str,
        path: str = ".",
        max_files: int = DEFAULT_GLOB_FILES,
        save_full: bool = False,
        cursor: str | None = None,
        timeout_seconds: float | None = SCAN_TIMEOUT_SECONDS,
    ) -> str:
        """按路径模式分页找本机文件；不跟随链接、敏感、忽略或依赖目录。

        Args:
            pattern: 路径模式，如 **/*.py、src/**/test_*.py；**/ 允许零层目录。
            path: 工作区相对路径或显式本机外部绝对目录/文件。
            max_files: 每页文件预算，默认 500，可增加。
            save_full: 显式将本次时间预算内列表落盘，note 给可读路径。
            cursor: 上页 next_cursor，保留查询参数；has_more 时继续，目录变化后从头重查。
            timeout_seconds: 时间预算，默认 20 秒，可增加或设 null。
        """
        return glob_files(
            pattern, policy=policy, path=path, max_files=max_files,
            save_full=save_full, cursor=cursor, timeout_seconds=timeout_seconds,
        ).model_dump_json()

    @tool
    def search_files(
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        mode: str = "content",
        ignore_case: bool = False,
        max_matches: int = DEFAULT_MAX_MATCHES,
        save_full: bool = False,
        cursor: str | None = None,
    ) -> str:
        """一次调用完成"按内容搜"或"按名字找"。

        模型经常"先按文件名找、再按内容搜"——那是**一个意图、两次往返**，这个工具把它们合起来。
        原来的 `grep` / `glob` 仍然保留（旧会话与脚本不受影响），新任务优先用这一个。

        Args:
            pattern: `mode=content` 时是 Python 正则；`mode=files` 时是 glob 模式（如 **/*.py）。
            path: 搜索根目录，默认工作区。
            glob: 只在 `mode=content` 时生效：把搜索限定在这些文件里（如 **/*.tsx）。
            mode: `content`（默认，搜内容）或 `files`（找文件名）。
            ignore_case: 是否忽略大小写（仅 content 模式）。
            max_matches: 本页预算：content 是命中行数，files 是文件数。
            save_full: 把本页预算内的结果落盘并给出路径。
            cursor: 上一页的 next_cursor，用于继续；其余查询参数必须保持一致。

        返回结构与原来一致，多一个 `mode` 字段标识这次是哪一种。
        """
        if mode == "files":
            files_result = glob_files(
                pattern, policy=policy, path=path, max_files=max_matches,
                save_full=save_full, cursor=cursor,
            )
            return json.dumps(
                {"mode": "files", **files_result.model_dump()}, ensure_ascii=False
            )
        if mode != "content":
            raise ValueError("mode 必须为 content 或 files")
        grep_result = grep_content(
            pattern, policy=policy, path=path, glob=glob, ignore_case=ignore_case,
            max_matches=max_matches, save_full=save_full, cursor=cursor,
        )
        return json.dumps(
            {"mode": "content", **grep_result.model_dump()}, ensure_ascii=False
        )

    return [grep, glob, search_files]

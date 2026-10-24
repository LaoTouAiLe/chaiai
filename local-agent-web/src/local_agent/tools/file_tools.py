"""文件工具的参数、文本变换与结果展示；实际 I/O 由本轮统一文件后端承载。

- 工具逻辑写成普通函数（``list_dir_entries`` / ``read_file_content``），便于直接测试；
  ``build_file_tools`` 再包一层 ``@tool``，通过闭包持有 :class:`SecurityPolicy`。
- 后端共用 SecurityPolicy，执行前复核目录授权、档位、敏感/保护目标与人工批准。
- 工具出错时**不吞异常**：由 ``tool_execution_node`` 统一捕获并回填给模型，
  这样错误既不会被隐藏，也不会中断整张图。
"""

from __future__ import annotations

import codecs
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from local_agent.exceptions import FileOperationError, SecurityError, WorkspaceBoundaryError
from local_agent.security.approval import action_fingerprint
from local_agent.security.diff_guard import (
    apply_patch,
    build_unified_diff,
    count_changed_lines,
    count_diff_lines,
    edit_input_mode,
    parse_patch,
)
from local_agent.security.path_guard import (
    display_path,
    is_within,
)
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.result_types import (
    DeleteFileResult,
    DirEntry,
    EditFileResult,
    ListDirResult,
    ReadFileResult,
    WriteFileResult,
)

#: 递归列目录的最大深度（架构 §6.1：限制递归深度）。
MAX_RECURSIVE_DEPTH = 5

#: 删除时统计"删了多少行"的上限：超过它的文件不按行计（二进制/超大文件按行数没有意义）。
#: 与变更账本的基线上限同口径（512 KB），避免页面上出现"删除 26903 行"这种数字。
COUNTED_LINES_MAX_BYTES = 512 * 1024


def _is_pure_crlf(text: str) -> bool:
    """是否"每一行都是 CRLF"（2026-10-22）。

    只有纯 CRLF 才在归一化写回时还原——混合换行（部分 LF、部分 CRLF）强行统一，会改动
    根本没有触及的那些行。
    """
    return "\r\n" in text and text.count("\n") == text.count("\r\n")


def _edit_miss_message(policy: SecurityPolicy, resolved: Path, detail: str) -> str:
    """编辑未命中时的错误文本：按文件累计次数，第 3 次起给升级路径（2026-10-22，抄 hermes）。

    为什么要有计数：模型会用"换个写法再试一次"来应对匹配失败，实测连试 3 次仍失败，
    最后自己改走脚本——那条路绕过了账本。计数到 3 就把"别再重试"写成明确指令。
    """
    key = str(resolved)
    count = policy.edit_failures.get(key, 0) + 1
    policy.edit_failures[key] = count
    message = detail
    if count >= 3:
        message += (
            f"\n同一文件已连续失败 {count} 次：不要再换一种写法重试。"
            "要么重新读这个文件核对当前内容，要么用更长、更唯一的锚点（带上下文行），"
            "要么整份重读后改用 write_file 替换。"
        )
    return message

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
        path: 相对工作区根目录的路径或绝对路径；三档均允许显式本机外部绝对路径。
        policy: 安全策略，负责边界与敏感文件判定。
        recursive: 是否递归列出子目录，最多 :data:`MAX_RECURSIVE_DEPTH` 层。
        include_hidden: 是否包含以点开头的隐藏项。
        max_entries: 最多返回多少条；**达到上限即停止遍历**，不会先走完整棵树。

    Returns:
        目录项列表、总数与截断标记。

    Raises:
        SecurityError: 路径越界或敏感路径。
        FileOperationError: 目标不是目录或不存在。
    """
    resolved = policy.files.directory(path)

    candidates: list[Path] = []
    #: 非递归分支的 ``DirEntry``：带上它，`_dir_entry` 就不必再对每个条目做一次 realpath。
    scanned: dict[str, os.DirEntry[str]] = {}
    depth_limited = False
    truncated = False
    if recursive:
        # 有界遍历：显式栈逐层展开，达到条目上限或深度上限**立即停止**；每一层也只读"剩余
        # 额度 + 1"项再排序，不先枚举整个目录。否则在大目录（尤其外部盘符/主目录）上会遍历
        # 整棵树、占满内存并长时间无响应，配置里的上限就不是真正的执行限制。
        stack: list[tuple[Path, int]] = [(resolved, 0)]
        while stack and not truncated:
            current, depth = stack.pop()
            raw, stream_stopped = policy.files.children(current, max_entries - len(candidates))
            if stream_stopped:
                truncated = True  # 目录里还有没读的条目：结果不完整，必须如实标注
            for child in sorted((Path(item.path) for item in raw), key=lambda path: path.name):
                if not include_hidden and child.name.startswith("."):
                    continue
                if policy.files.is_directory(child) and depth + 1 > MAX_RECURSIVE_DEPTH:
                    # 超深目录**本身**也不进结果，才与"超过 N 层的条目已省略"一致
                    depth_limited = True
                    continue
                candidates.append(child)
                if len(candidates) >= max_entries:
                    truncated = True
                    break
                if not policy.files.is_directory(child):
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
        raw, stream_stopped = policy.files.children(resolved, max_entries)
        visible = [
            item for item in raw if include_hidden or not item.name.startswith(".")
        ]
        visible.sort(key=lambda item: item.name)
        candidates = [Path(item.path) for item in visible]
        scanned = {str(Path(item.path)): item for item in visible}
        if stream_stopped:
            truncated = True

    total = len(candidates)
    entries = [
        _dir_entry(candidate, policy, dirent=scanned.get(str(candidate)))
        for candidate in candidates[:max_entries]
    ]

    notes: list[str] = []
    if truncated:
        notes.append(
            f"结果已截断：已达上限，只返回按名称排序的前 {max_entries} 项，未继续遍历剩余目录；"
            "要看更多：把 path 指到具体子目录，或提高 max_entries"
        )
    elif total > max_entries:
        notes.append(
            f"结果已截断：共 {total} 项，只返回按名称排序的前 {max_entries} 项；"
            "要看更多：把 path 指到具体子目录，或提高 max_entries"
        )
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
    max_chars: int = 100000,
    start_char: int | None = None,
    expected_sha256: str | None = None,
    encoding: str = "utf-8-sig",
) -> ReadFileResult:
    """流式读取文本页；行区间和全文件字符位置互斥，字符续读支持超长单行。"""
    from local_agent.tools.text_stream import BinaryTextError, text_parts

    if max_chars < 1:
        raise ValueError("max_chars 必须为正数")
    if start_line is not None and start_line < 1:
        raise ValueError("start_line 必须从 1 开始")
    if end_line is not None and end_line < (start_line or 1):
        raise ValueError("end_line 不能小于 start_line")
    if start_char is not None and (start_char < 0 or start_line is not None or end_line is not None):
        raise ValueError("start_char 必须非负，且不能与行区间同时使用")
    resolved = policy.resolve_readable_file(path)
    before = policy.files.stat(resolved)
    _, file_sha256, _ = policy.files.read_prefix_and_sha256(resolved, 0)
    if expected_sha256 and expected_sha256.lower() != file_sha256:
        raise FileOperationError("expected_sha256 与当前文件不一致：文件已变化，请重新读取")
    # 轮级去重（2026-10-22，对齐 hermes 的 read 去重）：同一轮里用**同一组参数**重复读同一段、
    # 且文件 sha 没变，就不再返回原文。模型反复读同一段是常见空转，一次就是一次模型往返。
    # 文件一改 sha 就不同，不会命中；需要原文时换个范围或改 max_chars 即可绕过。
    cache_key = (str(resolved), file_sha256, start_line, end_line, start_char, max_chars, encoding)
    cached = policy.read_cache.get(cache_key)
    if cached is not None:
        first_line, last_line, has_more, next_start_line, next_start_char, line_count = cached
        return ReadFileResult(
            path=_display_path(resolved, policy.workspace_root),
            encoding="utf-8" if encoding == "utf-8-sig" else encoding,
            sha256=file_sha256,
            line_count=line_count,
            content="",
            note=(
                "这段内容在本次任务中已经读过、文件也没有变化（sha256 前 8 位 "
                f"{file_sha256[:8]}），因此没有重复返回原文。"
                "确实需要再看一遍时，换一个范围（start_line/start_char）或改 max_chars 重新读取。"
            ),
            first_line=first_line,
            last_line=last_line,
            has_more=has_more,
            next_start_line=next_start_line,
            next_start_char=next_start_char,
        )
    chunks: list[str] = []
    offset = 0
    number = 1
    line_count = 0
    count = 0
    first_line = 0
    last_line = 0
    actual_start = start_char or 0
    partial: set[int] = set()
    column = 0
    first_partial = False
    notes: list[str] = []
    is_binary = False
    decoded = True
    try:
        for part, ended in text_parts(resolved, policy, encoding):
            line_count = number
            eligible = (start_char is not None or
                        (number >= (start_line or 1) and (end_line is None or number <= end_line)))
            begin = max(0, (start_char or 0) - offset) if start_char is not None else 0
            if eligible and begin < len(part) and count < max_chars:
                fragment = part[begin:begin + max_chars - count]
                if not count:
                    actual_start, first_line = offset + begin, number
                    first_partial = bool(begin or column)
                    if first_partial:
                        partial.add(number)
                chunks.append(fragment)
                count += len(fragment)
                last_line = number
                cut = begin + len(fragment)
                if not ended or (cut < len(part) and part[cut:cut + 1] not in ("\r", "\n")):
                    partial.add(number)
                elif number != first_line or not first_partial:
                    partial.discard(number)
            offset += len(part)
            column = 0 if ended else column + len(part)
            if ended:
                number += 1
    except BinaryTextError as exc:
        is_binary, decoded = True, False
        notes.append(str(exc))
    except UnicodeError:
        decoded = False
        label = "UTF-8" if encoding == "utf-8-sig" else encoding
        notes.append(f"无法按 {label} 解码（未知编码），未返回内容")
    except (LookupError, TypeError) as exc:
        raise ValueError(f"不支持的文本编码：{encoding}") from exc
    after = policy.files.stat(resolved)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise FileOperationError("读取期间文件已变化，请重新读取")
    if not decoded:
        chunks, count, first_line, last_line, line_count, partial = [], 0, 0, 0, 0, set()
    content = "".join(chunks)
    next_char = actual_start + count
    has_more = decoded and bool(content) and next_char < offset
    truncated = has_more and count == max_chars
    next_start_line = (last_line if last_line in partial else last_line + 1) if has_more else None
    if truncated:
        notes.append(f"内容已截断到 {max_chars} 字符")
    if has_more:
        notes.append(f"后续内容传 start_char={next_char} 和 expected_sha256={file_sha256}；也可按行定点读取")
    # 记入轮级去重表：下一次相同参数、同一 sha 的读取直接返回"已读过"，不再返回原文。
    if decoded and content:
        if len(policy.read_cache) >= 64:
            policy.read_cache.clear()
        policy.read_cache[cache_key] = (
            first_line, last_line, has_more, next_start_line,
            next_char if has_more else None, line_count,
        )
    return ReadFileResult(
        path=_display_path(resolved, policy.workspace_root),
        encoding=("utf-8" if encoding == "utf-8-sig" else encoding) if decoded else None,
        sha256=file_sha256, line_count=line_count, content=content, is_binary=is_binary,
        truncated=truncated, note="；".join(notes) if notes else None,
        first_line=first_line, last_line=last_line, has_more=has_more,
        next_start_line=next_start_line,
        partial_lines=sorted(partial), start_char=actual_start,
        next_start_char=next_char if has_more else None,
    )


def edit_file_content(
    path: str,
    *,
    policy: SecurityPolicy,
    old_text: str = "",
    new_text: str = "",
    patch: str = "",
    replace_all: bool = False,
    expected_sha256: str | None = None,
    reason: str = "",
    encoding: str = "utf-8-sig",
) -> EditFileResult:
    """修改工作区内已有文件：精确串替换（默认）或 unified diff（架构 §6.3）。

    两种输入模式**互斥**（由 :func:`diff_guard.edit_input_mode` 统一判定，安全判定与执行
    共用同一规则）：

    - ``old_text`` + ``new_text`` 非空、``patch`` 为空 → 精确串替换；
    - ``patch`` 非空、``old_text`` / ``new_text`` 都为空 → unified diff。

    安全约束（全部在代码层强制）：

    - ``expected_sha256`` 可选，提供时核对版本；写入时仍检查并发修改；
    - 精确串替换：``old_text`` 必须精确匹配且默认只允许一处，多处匹配必须显式 ``replace_all``；
      支持整文件内容替换；
    - ``patch`` 模式：先解析（空 Patch、格式、计数、顺序、重叠），再按 `@@` 旧行号逐字校验
      上下文；任一不过即拒绝，**原文件一字不动**（架构 §6.3）；
    - 工作区修改默认免审批；外部写入按目录授权执行；
    - 先写同目录临时文件再原子替换，写入后重新计算 Hash。

    Args:
        path: 工作区内的目标文件。
        policy: 安全策略。
        old_text: 要被替换的原文片段（精确替换模式）。
        new_text: 替换后的文本（精确替换模式）。
        patch: unified diff 文本（patch 模式，与 old_text/new_text 互斥）。
        replace_all: 是否替换全部匹配（仅精确替换模式）。
        expected_sha256: 可选的文件版本 Hash。
        reason: 修改原因，写入审计结果。

    Returns:
        修改结果与 unified diff；patch 模式下 ``replaced_count`` 是应用的 hunk 数。

    Raises:
        SecurityError: 路径、档位或审批校验不通过。
        FileOperationError: 输入、匹配、内容格式或文件版本校验失败。
    """
    arguments = {
        "path": path,
        "old_text": old_text,
        "new_text": new_text,
        "patch": patch,
        "replace_all": replace_all,
    }
    # 先解析目标：越界（WorkspaceBoundaryError）、敏感文件与受保护目标（SecurityError）
    # 必须在看审批之前就失败闭合；判定、指纹与执行三处都用这一个规范化路径（架构 §1.5）。
    resolved = policy.resolve_writable_file(path)
    decision = policy.classify_tool("edit_file", arguments)
    # 大范围修改属于强制审批动作：只有持有效人工批准（同一次调用 + 指纹一致）才执行
    granted = policy.is_granted(action_fingerprint("edit_file", arguments, policy=policy))
    if not decision.allowed:
        if decision.reason_kind == "input":
            raise FileOperationError(decision.reason)
        if not granted:
            raise SecurityError(decision.reason)
    # 输入模式由安全判定与工具执行共用同一个函数（架构 §6.3），避免两处各写一套互斥规则。
    mode = edit_input_mode(old_text, new_text, patch)

    data = policy.files.read_bytes(resolved)
    original = _decode_text(data, encoding=encoding)
    current_sha256 = hashlib.sha256(data).hexdigest()

    expected = (expected_sha256 or "").strip().lower()
    if expected and expected != current_sha256:
        raise FileOperationError(
            f"expected_sha256 与当前文件不一致（当前为 {current_sha256[:12]}…）："
            "文件已被改动，请重新读取后再修改"
        )
    note: str | None = None
    if mode == "patch":
        # patch 模式：先解析结构，再对着真实内容按旧行号严格定位应用；任一不过都不写盘。
        plan = parse_patch(patch)
        updated = apply_patch(original, plan, file_name=resolved.name)
        replaced_count = len(plan.hunks)
        # replaced_count 在 patch 模式下表示**应用的 hunk 数**（不是替换处数）。字段语义写进
        # note：页面与审计据此区分两种模式，模型也能看到这次改了几个 hunk（架构 §6.3）。
        note = f"补丁模式：已应用 {replaced_count} 个 hunk（replaced_count 为 hunk 数）"
    else:
        if old_text == new_text:
            raise FileOperationError("old_text 与 new_text 相同，没有需要修改的内容")
        # 架构 §6.3：不接受用整份文件内容做替换。单行文件例外——那一行本身就是最小的唯一锚点，
        # 否则会出现"文件只有一行就没法改"的怪现象。

        raw_original = original
        occurrences = original.count(old_text)
        normalized = False
        if occurrences == 0:
            # 换行归一化重试（2026-10-22）：文件是 CRLF、模型用 `\n` 拼 old_text，是最常见的
            # "内容明明写对了却匹配不上"。只在精确匹配失败时启用；命中后仍按原风格写回。
            normalized_original = original.replace("\r\n", "\n").replace("\r", "\n")
            normalized_old = old_text.replace("\r\n", "\n").replace("\r", "\n")
            occurrences = normalized_original.count(normalized_old)
            if occurrences:
                normalized = True
                original = normalized_original
                old_text = normalized_old
                new_text = new_text.replace("\r\n", "\n").replace("\r", "\n")
        if occurrences == 0:
            raise FileOperationError(
                _edit_miss_message(
                    policy, resolved,
                    "old_text 在文件中没有匹配：已按 CRLF/LF 归一化重试仍找不到，"
                    "请核对空格、缩进与内容",
                )
            )
        if occurrences > 1 and not replace_all:
            raise FileOperationError(
                _edit_miss_message(
                    policy, resolved,
                    f"old_text 匹配到 {occurrences} 处：请提供更长的唯一片段，或显式设置 replace_all=true",
                )
            )

        updated = (
            original.replace(old_text, new_text)
            if replace_all
            else original.replace(old_text, new_text, 1)
        )
        replaced_count = occurrences if replace_all else 1
        if normalized and _is_pure_crlf(raw_original):
            # 原文件是纯 CRLF：把归一化后的结果还原成 CRLF，避免一次替换改掉整个文件的换行。
            updated = updated.replace("\n", "\r\n")

    diff = build_unified_diff(original, updated, path=resolved.name)
    if not diff.strip():
        raise FileOperationError("计算出的 diff 为空，没有可写入的改动")

    # ``utf-8-sig`` 解码会有意移除 BOM；编辑已有文件时必须把原来的编码标记写回去，
    # 否则一次普通文本替换会静默改变文件格式。
    output_encoding = "utf-8" if encoding == "utf-8-sig" and not data.startswith(codecs.BOM_UTF8) else encoding
    new_data = updated.encode(output_encoding)
    # 变更账本（2026-10-21）：写盘前留一份原文，写成功后记一条账。放在这里而不是事件层，
    # 是因为只有这里同时知道"这一次改了什么"（diff）与"改前是什么"（original）。
    policy.capture_file_baseline(resolved)
    resolved = policy.files.replace_file("edit_file", arguments, new_data, expected_sha256=current_sha256)
    # 改了就得记账（2026-10-15）：`security.large_edit` 现在按**本轮改过的不同文件数**判，
    # 计数由这里产生。放在写盘**之后**——判定失败、指纹不符、内容没变的调用都不该计数，
    # 否则"改三个文件"会因为几次失败尝试而提前触发批量审批。
    policy.note_edited(resolved)
    # 成功即清零：失败计数只用于"连续失败"的判断，跨成功不该累计。
    policy.edit_failures.pop(str(resolved), None)
    added, removed = count_diff_lines(diff)
    policy.note_file_change(
        tool="edit_file", resolved=resolved, kind="modify",
        additions=added, deletions=removed,
        before_sha256=current_sha256,
        after_sha256=hashlib.sha256(new_data).hexdigest(),
    )
    if decision.large_edit and granted:
        # 这次执行靠的正是"大范围修改"那次批准：记住它，本轮后面的文件不再逐个问。
        policy.note_large_edit_approved()

    return EditFileResult(
        path=_display_path(resolved, policy.workspace_root),
        reason=reason,
        replaced_count=replaced_count,
        changed_lines=count_changed_lines(original, updated),
        old_sha256=current_sha256,
        new_sha256=hashlib.sha256(new_data).hexdigest(),
        diff=diff,
        line_count=len(updated.splitlines()),
        note=note,
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
        path: 相对路径以当前工作区为根；外部须显式本机绝对路径及目录写授权，工作区内覆盖无需审批，外部仍按授权和策略判定。
        policy: 安全策略。
        content: 文件内容。
        overwrite: 是否允许覆盖已有文件（本版本只用于给出更明确的拒绝原因）。
        reason: 创建原因，写入审计结果。

    Returns:
        创建结果与文件 Hash。

    Raises:
        SecurityError: 路径、档位或审批校验不通过。
        FileOperationError: 目标类型、文件状态或内容大小不满足要求。
    """
    arguments = {"path": path, "content": content, "overwrite": overwrite}
    # 先解析目标：越界、敏感文件与受保护目标在看审批之前失败闭合（架构 §1.5）
    resolved = policy.resolve_writable_path(path)
    decision = policy.classify_tool("write_file", arguments)
    # 覆盖已有文件需要有效人工批准；目录写授权与只读档另由执行后端复核
    granted = policy.is_granted(action_fingerprint("write_file", arguments, policy=policy))
    if not decision.allowed and not granted:
        raise SecurityError(decision.reason)
    overwriting = policy.files.exists(resolved)
    created: list[str] = []  # 覆盖路径不建目录，保持空列表（结果字段恒定，调用方不用分情况）
    if overwriting:
        if policy.files.is_directory(resolved):
            raise FileOperationError(f"目标是一个目录，不能用 write_file 写入：{resolved.as_posix()}")
        if not overwrite:
            raise FileOperationError(
                f"文件已存在：{resolved.as_posix()}。修改现有文件请用 edit_file"
                "（精确串替换），不要整体覆盖"
            )
        if not granted and not decision.allowed:
            raise SecurityError(
                "覆盖已有文件需要人工审批（架构 §6.4）：未获批准，已拒绝覆盖"
            )

    data = content.encode("utf-8")
    if len(data) > policy.workspace.max_file_size_bytes:
        raise FileOperationError(
            f"内容 {len(data)} 字节超过上限 {policy.workspace.max_file_size_bytes} 字节，已拒绝写入"
        )

    policy.capture_file_baseline(resolved)
    if overwriting:
        original_text = policy.files.read_bytes(resolved).decode("utf-8", errors="replace")
        resolved = policy.files.replace_file("write_file", arguments, data)
    else:
        original_text = ""
        resolved, created = policy.files.create_file(arguments, data)
    # `write_file` 的 diff 以前根本不存在（只有字节数与行数），于是对话里"写入文件"这张卡
    # 永远没有变更信息、右栏也点不开内容（2026-10-21 用户实测）。这里算一次：新建 = 整份新增，
    # 覆盖 = 与原文比较。同一份文本既用于记账，也随结果返回给页面。
    diff_text = build_unified_diff(original_text, content, path=resolved.name)
    added, removed = count_diff_lines(diff_text)
    policy.note_file_change(
        tool="write_file", resolved=resolved, kind="modify" if overwriting else "create",
        additions=added, deletions=removed,
        after_sha256=hashlib.sha256(data).hexdigest(),
    )

    return WriteFileResult(
        path=_display_path(resolved, policy.workspace_root),
        reason=reason,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        line_count=len(content.splitlines()),
        diff=diff_text,
        created_directories=created,
        note=f"顺带新建了 {len(created)} 个目录：{'、'.join(created)}" if created else None,
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
    if not decision.allowed and not policy.is_granted(fingerprint):
        resolved_for_message = policy.resolve_path(path)
        target = _describe_target(resolved_for_message, recursive, policy)
        detail = f"；原因：{reason}" if reason else ""
        raise SecurityError(
            f"{decision.reason}。目标：{resolved_for_message.as_posix()}（{target}）{detail}"
        )

    # 变更账本（2026-10-21）：删除也要留一份原文，否则"删掉了什么"永远看不回来。
    target: Path | None
    try:
        target = policy.resolve_path(path)
    except (SecurityError, WorkspaceBoundaryError, FileOperationError):
        target = None
    removed_lines = 0
    if target is not None:
        policy.capture_file_baseline(target)
        if target.is_file():
            # 只对**文本**数行：二进制文件（图片、压缩包）按行数统计没有意义，
            # 一张 27 MB 的图会被算成两万多行删除，把总览的数字直接搞乱（实测）。
            try:
                if target.stat().st_size <= COUNTED_LINES_MAX_BYTES:
                    payload = target.read_bytes()
                    if b"\x00" not in payload[:4096]:
                        removed_lines = len(payload.decode("utf-8", errors="replace").splitlines())
            except OSError:
                removed_lines = 0
    deleted = policy.files.delete(arguments)
    if target is not None:
        policy.note_file_change(
            tool="delete_file", resolved=target, kind="delete", deletions=removed_lines,
        )

    return DeleteFileResult(
        path=_display_path(deleted.path, policy.workspace_root),
        recursive=recursive,
        kind=deleted.kind,
        removed_files=deleted.files,
        removed_dirs=deleted.directories,
        reason=reason,
        trashed=deleted.trashed,
    )


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
            path: 相对路径以当前工作区为根；三档均允许显式本机外部绝对路径，无需读取授权或审批。
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
        max_chars: int = 100000,
        start_char: int | None = None,
        expected_sha256: str | None = None,
        encoding: str = "utf-8-sig",
    ) -> str:
        """读取本机文本文件，返回内容、完整 Hash、行号与续读位置。

        Args:
            path: 工作区相对路径或本机外部绝对路径；敏感文件仍禁止读取。
            start_line: 1 基起始行，含。
            end_line: 1 基结束行，含。
            max_chars: 本页字符预算，默认 100000（2026-10-22 由 20000 上调：一次能覆盖更大文件，
                减少"同一个文件读三四次"的往返），可按任务增加或减小。
            start_char: 全文件解码后 0 基字符位置，不能与行区间同传。
                has_more 为真时，传 next_start_char 续读，超长单行也能推进。
            expected_sha256: 续读传上页 sha256，文件变化会明确报错。
            encoding: 默认 UTF-8（兼容 BOM），可显式传 gbk、utf-16 等文本编码。

        大文件按流读取，不受写入大小预算限制。只读所需证据，无需通读无关内容。
        """
        return read_file_content(
            path, policy=policy, start_line=start_line, end_line=end_line,
            max_chars=max_chars, start_char=start_char,
            expected_sha256=expected_sha256, encoding=encoding,
        ).model_dump_json()

    @tool
    def edit_file(
        path: str,
        old_text: str = "",
        new_text: str = "",
        patch: str = "",
        replace_all: bool = False,
        expected_sha256: str | None = None,
        reason: str = "",
        encoding: str = "utf-8-sig",
    ) -> str:
        """修改已有文件，返回变更行数与 unified diff。

        两种输入模式**互斥**，请只用其中一种：

        1. 精确字符串替换（默认）：给出 old_text 与 new_text；
        2. unified diff（patch）：只给出 patch，old_text/new_text 留空。

        Args:
            path: 相对路径以当前工作区为根；外部须显式本机绝对路径及目录写授权，无需按修改数量审批，但不能是受保护或密钥文件。
            old_text: 要被替换的原文片段，必须与文件内容精确一致（含缩进与换行）；与 patch 互斥。
            new_text: 替换后的文本；与 patch 互斥。
            patch: unified diff 文本（`@@ -旧[,行数] +新[,行数] @@` 与 ` `/`-`/`+` 开头的行）。要求：hunk 按旧行号升序、上下文与删除行必须与当前文件逐字一致、文件头只能写文件名，不支持 Git 扩展头与 "\\ No newline at end of file"。
            replace_all: 是否替换全部匹配（仅精确替换模式）；默认要求 old_text 在文件中唯一。
            expected_sha256: 可选；提供时核对文件版本，省略不要求先读取。
            reason: 修改原因。
            encoding: 文本编码，默认UTF-8；可指定gbk或utf-16等文件实际编码。
        """
        return edit_file_content(
            path,
            policy=policy,
            old_text=old_text,
            new_text=new_text,
            patch=patch,
            replace_all=replace_all,
            expected_sha256=expected_sha256,
            reason=reason,
            encoding=encoding,
        ).model_dump_json()

    @tool
    def write_file(path: str, content: str, overwrite: bool = False, reason: str = "") -> str:
        """创建文件或通过 overwrite=true 覆盖，返回文件 Hash 与大小。

        **父目录不存在时自动创建**（含多级），并在结果里报告新建了哪些目录；
        所以写 `site/index.html` 不需要先跑一条 `mkdir` 命令——直接用这个工具写即可。

        Args:
            path: 相对路径以当前工作区为根；外部须显式本机绝对路径及目录写授权，工作区内覆盖无需审批，外部仍按授权和策略判定。
            content: 文件内容。
            overwrite: 是否覆盖已有文件；工作区内无需审批，外部按目录授权和策略执行。
            reason: 创建原因。
        """
        return write_file_content(
            path, policy=policy, content=content, overwrite=overwrite, reason=reason
        ).model_dump_json()

    @tool
    def delete_file(path: str, recursive: bool = False, reason: str = "") -> str:
        """删除文件或目录；工作区内无需审批，外部按目录授权和策略执行。

        开启「安全删除」时目标会**移入系统回收站**（资源管理器里那个，不是彻底删除），
        返回里 `trashed` 为 true——向用户说明"在系统回收站里，可以还原到原位置"。
        不要说成"已彻底删除"，也不要编造磁盘路径：回收站里的文件已被改名存放，那个路径用户点不开。

        Args:
            path: 相对路径以当前工作区为根；外部须显式本机绝对路径及目录写授权，工作区内删除无需审批，外部仍按授权和策略判定。
            recursive: 是否递归删除目录（删除目录必须显式开启）。
            reason: 删除原因。
        """
        return delete_file_target(
            path, policy=policy, recursive=recursive, reason=reason
        ).model_dump_json()

    return [list_dir, read_file, edit_file, write_file, delete_file]


def _decode_text(data: bytes, *, encoding: str = "utf-8-sig") -> str:
    """按实际文本编码编辑；保留对非文本二进制输入的错误说明。"""
    if b"\x00" in data and not encoding.lower().replace("_", "-").startswith(("utf-16", "utf-32")):
        raise FileOperationError("目标是二进制内容，请使用合适的二进制工具")
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise FileOperationError(f"无法按{encoding}解码，请指定文件实际encoding") from exc


#: 删除目标"影响范围"的扫描上限：这是给用户看的有界摘要，不是精确清点。
DELETE_IMPACT_SCAN_LIMIT = 500


def _describe_target(path: Path, recursive: bool, policy: SecurityPolicy) -> str:
    """描述删除目标的影响范围（架构 §6.5 要求展示数量与范围，但有界）。

    目录只做**一次有上限**的扫描（:data:`DELETE_IMPACT_SCAN_LIMIT` 条就停并标"至少"）：这是给
    用户看的未批准/审批提示，不该为了精确数量遍历整棵目录树（大目录、主目录会卡住，T14C 复审）。
    """
    return policy.files.describe_delete(path, recursive, DELETE_IMPACT_SCAN_LIMIT)


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


def _dir_entry(
    path: Path,
    policy: SecurityPolicy,
    *,
    dirent: os.DirEntry[str] | None = None,
) -> DirEntry:
    # 有 scandir 的结果就直接用它（不再 realpath）；递归分支没有传，退回原来的严格解析。
    kind, info = (
        policy.files.entry_from_scan(dirent) if dirent is not None else policy.files.entry(path)
    )
    size: int | None = None
    modified: str | None = None
    if info is not None:
        size = info.st_size if kind == "file" else None
        modified = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        )

    return DirEntry(
        name=path.name,
        path=_display_path(path, policy.workspace_root),
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
    """展示用路径：口径在 :func:`local_agent.security.path_guard.display_path`（一处实现）。"""
    return display_path(path, workspace_root)

"""文件工具的模型输入投影：行号、缺失范围与紧凑目录，原始审计结果不变。"""

from __future__ import annotations

from typing import Any, cast


def project_file_tool_output(
    payload: dict[str, Any], *, tool_name: str | None,
    threshold_chars: int, head_chars: int, tail_chars: int,
) -> dict[str, Any] | None:
    """投影可信工具 JSON；不识别的工具或不完整结构返回 None，交原有路径处理。"""
    if tool_name == "list_dir":
        return _directory_output(payload)
    if tool_name not in (None, "read_file"):
        return None
    content = payload.get("content")
    first, last, total = (payload.get(key) for key in ("first_line", "last_line", "line_count"))
    if (
        not isinstance(content, str) or not content or payload.get("is_binary") is True
        or (tool_name is None and payload.get("is_binary") is not False)
        or not isinstance(payload.get("path"), str)
    ):
        return None
    if not all(type(value) is int for value in (first, last, total)):
        return _unknown_read_output(payload, content, threshold_chars, head_chars, tail_chars)
    first, last, total = cast(int, first), cast(int, last), cast(int, total)
    # 验证行区间，不把旧结构、错误 JSON 或已投影正文误当成原始读取。
    lines = content.splitlines()
    if first < 1 or last > total or last - first + 1 != len(lines):
        return _unknown_read_output(payload, content, threshold_chars, head_chars, tail_chars)
    numbered = [f"{first + index}: {line}" for index, line in enumerate(lines)]
    rows = [(index, row, False) for index, row in enumerate(numbered)]
    squeezed = threshold_chars > 0 and len("\n".join(numbered)) > threshold_chars
    if squeezed:
        # 按完整行保留两端；极长单行使用显式片段，不让模型误以为该行已完整读取。
        budget = max(0, min(head_chars + tail_chars, threshold_chars - 128))
        head_budget = budget * head_chars // max(1, head_chars + tail_chars)
        head = _take_rows(numbered, range(len(lines)), head_budget, suffix=False)
        complete = {index for index, _, partial in head if not partial}
        tail = _take_rows(
            numbered, range(len(lines) - 1, -1, -1), budget - head_budget,
            suffix=True, exclude=complete,
        )
        rows = head + list(reversed(tail))
    visible = sorted({first + index for index, _, _ in rows})
    missing = sorted(set(range(first, last + 1)) - set(visible))
    partial = {first + index for index, _, clipped in rows if clipped}
    # 原始工具的 max_chars / 字节上限也可能在最后一行中间截断。
    declared_partial = payload.get("partial_lines")
    if isinstance(declared_partial, list):
        partial.update(number for number in declared_partial
                       if type(number) is int and first <= number <= last)
    elif payload.get("truncated") and not content.endswith(("\n", "\r")):
        partial.add(last)
    partial_visible = sorted(partial.intersection(visible))
    pending = missing + sorted(partial)
    next_line = min(pending) if pending else payload.get("next_start_line")
    more = bool(pending) or bool(payload.get("has_more"))
    result = dict(payload)
    body = "\n".join(row for _, row, _ in rows)
    if squeezed:
        head_count = len(head)
        marker = "……（中间内容已裁剪；按 omitted_ranges / partial_lines 定点续读）……"
        body = "\n".join([*(row for _, row, _ in rows[:head_count]), marker,
                          *(row for _, row, _ in rows[head_count:])])
    result.update(
        content=body, first_line=min(visible) if visible else 0,
        last_line=max(visible) if visible else 0,
        visible_ranges=_ranges(visible), omitted_ranges=_ranges(missing),
        partial_lines=partial_visible,
        truncated=bool(payload.get("truncated")) or squeezed,
        has_more=more, next_start_line=next_line if more else None,
        note=("正文带原始 1 基行号；visible_ranges 为可见范围，partial_lines 为不完整行。"
              "仅在证据不足时按缺失范围或 next_start_line 续读，不必读完无关内容。"
              "无遗漏范围时可传 next_start_char 和 expected_sha256=sha256 按字符续读；"
              "start_char 为片段在解码后全文件中的 0 基字符位置。"),
    )
    if squeezed and "next_start_char" in result:
        result["next_start_char"] = None
        result["note"] += "本页正文已被压缩，字符续页位置已清空；可从 start_char 调小 max_chars 重读。"
    return result



def _unknown_read_output(
    payload: dict[str, Any], content: str, threshold: int, head_chars: int, tail_chars: int,
) -> dict[str, Any]:
    """旧读取无法核实坐标时保留原文；压力裁剪后明确缺失，不伪造行号。"""
    clipped = threshold > 0 and len(content) > threshold
    result = dict(payload)
    note = "原始行坐标未知；引用前请重新定位并按小范围读取，不可从片段猜测行号。"
    if clipped:
        budget = max(0, min(head_chars + tail_chars, threshold - 128))
        head = budget * head_chars // max(1, head_chars + tail_chars)
        tail = budget - head
        result.update(
            content=content[:head] + "\n……（中间内容已裁剪；原始行坐标未知）……\n"
                    + (content[-tail:] if tail else ""),
            truncated=True, has_more=True, next_start_line=None,
            first_line=0, last_line=0,
        )
    result.update(source_coordinates="unknown", note=note)
    return result


def _take_rows(
    rows: list[str], indexes: range, budget: int, *, suffix: bool,
    exclude: set[int] | None = None,
) -> list[tuple[int, str, bool]]:
    selected: list[tuple[int, str, bool]] = []
    for index in indexes:
        if exclude and index in exclude:
            continue
        row = rows[index]
        cost = len(row) + 1
        if cost <= budget:
            selected.append((index, row, False))
            budget -= cost
            continue
        if not selected and budget > 32:
            label, text = row.split(": ", 1)
            prefix = f"{label} [行尾片段]: " if suffix else f"{label} [行首片段]: "
            keep = max(1, budget - len(prefix) - 1)
            fragment = text[-keep:] if suffix else text[:keep]
            selected.append((index, prefix + fragment, True))
        break
    return selected


def _ranges(numbers: list[int]) -> list[list[int]]:
    ranges: list[list[int]] = []
    for number in numbers:
        if ranges and ranges[-1][1] + 1 == number:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return ranges


def _directory_output(payload: dict[str, Any]) -> dict[str, Any] | None:
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    if not all(isinstance(entry, dict) and isinstance(entry.get("path"), str)
               and isinstance(entry.get("name"), str) and isinstance(entry.get("kind"), str)
               for entry in entries):
        return None
    # 正常目录项的 name 已包含在 path 中；异常/旧结构名称不同则完整保留。
    columns = ["path", "kind", "size_bytes", "modified_at"]
    if any(entry["name"] != entry["path"].replace("\\", "/").rsplit("/", 1)[-1]
           for entry in entries):
        columns.insert(0, "name")
    # 新增未知元数据不能被投影无声丢掉。
    if any(set(entry) - {"name", *columns} for entry in entries):
        return None
    return {**payload, "entry_columns": columns,
            "entries": [[entry.get(key) for key in columns] for entry in entries]}

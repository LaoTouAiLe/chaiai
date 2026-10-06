"""工具结果 → 给模型看的证据文本（架构 §5.4）。

工具结果是 **JSON**，而 JSON 的字段顺序是"元数据在前、正文在后"
（``path`` / ``encoding`` / ``sha256`` / ``line_count`` / ``truncated`` … ``content``）。
直接取前 N 个字符当摘要，模型看到的永远只有元数据——实测（DeepSeek 思考模式的真实模型）因此
判断"工具只返回了元信息、正文被截断、任务没完成"，连续多轮空转，最后向用户索取**本来已经读到**
的内容。所以证据摘要必须**可判读**：

- **标识字段完整保留**（路径、行区间、行数、是否截断、退出码……）：模型据此知道这次调用做了什么；
- **正文类字段按原文给出可见片段**（保留换行，不压成一行——压成一行会让模型怀疑"这是概述而不是
  原文"，实测又反过来追问用户），被截断时明确写出"原文片段，因长度限制只显示到这里，原文共 M 字符"
  ——模型不会把"片段被截断"误读成"工具没有返回内容"；
- 整体**有界**（:data:`TOOL_EVIDENCE_CHARS`），不把工具输出整段塞进提示。

纯文本输出（Shell 输出、错误信息）同样保留换行并带截断标注。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: 单条工具证据的字符上限（含工具名前缀）。够看到标识字段与正文片段，又不至于把提示撑大。
#:
#: 2026-09-30 由 ``600`` 提到 ``4000``（对照实测）：600 字符只够 read_file 读到的**前几行**，
#: 模型因此给不出行号级引用，还会自述"两个文件的内容均因长度限制只显示了截断片段"，
#: 并把"证据被截断"当成"任务未完成"而反复空转。同类产品的 read 单次可投递 2000 行 / 50KB，
#: 600 与之相差两个数量级。上层另有各自的收尾/摘要配额（``nodes.MAX_SYNTHESIS_EVIDENCE_CHARS``、
#: ``nodes.DETERMINISTIC_SUMMARY_CHARS``），本常量只约束**反思提示**里的证据行。
TOOL_EVIDENCE_CHARS = 4000

#: 标识/结论字段：短、决定"这次调用做了什么"，优先完整展示。
_EVIDENCE_SCALAR_FIELDS: tuple[str, ...] = (
    "path",
    "line_start",
    "line_end",
    "line_count",
    "truncated",
    "encoding",
    "is_binary",
    "replaced_count",
    "changed_lines",
    "exit_code",
    "status",
    "recursive",
    "source",
)

#: 正文类字段：超额度时截断，并带明确的截断标注。
_EVIDENCE_TEXT_FIELDS: tuple[str, ...] = (
    "content",
    "diff",
    "stdout",
    "stderr",
    "output",
    "note",
    "message",
    "matches",
    "entries",
    "items",
    "paths",
    "error",
)

_SUMMARY_NOTE = "…（原文片段，因长度限制只显示到这里；原文共 {total} 字符）"
_SKIPPED_NOTE = "另有 {count} 个字段未展示"
_NO_CONTENT_TEXT = "（工具没有返回内容）"

#: 工具专属的"怎么拿到更多"提示：只在正文**确实被截断**时追加到截断标注后面。
#:
#: 只登记真正有续读手段的工具——给 ``run_shell`` 这类没有行区间参数的工具写"用 start_line 续读"，
#: 会诱导模型去调用一个不存在的参数，换来一轮必然失败的工具调用（比不给提示更糟）。
_CONTINUE_HINTS: dict[str, str] = {
    "read_file": "；需要更多时用 start_line/end_line 读取后续行区间",
    "list_dir": "；需要更多时对子目录单列一次，或用 recursive/include_hidden 收窄范围",
}

#: 单个正文片段的最小可用长度：比这更短就不放，改用"未展示"说明。
_MIN_PIECE = 24

#: 标量字段值的展示上限（路径这类字段可能很长，但标识字段不该吃掉整个额度）。
_SCALAR_CHARS = 60


def tool_evidence_text(
    tool_name: str,
    status: str,
    payload: str | Mapping[str, Any] | None,
    *,
    limit: int = TOOL_EVIDENCE_CHARS,
) -> str:
    """渲染一行"能判断完成度"的工具证据，返回值长度不超过 ``limit``。

    Args:
        tool_name: 工具名。
        status: 工具状态（``ok`` / ``denied`` / ``error`` …）。
        payload: 工具返回的原始文本（JSON 或纯文本），也可以直接给已解析的映射。
        limit: **整行**（含工具名前缀与状态）的字符上限；调用方要自带的前缀（如 ``"- "``）
            需先从 ``limit`` 里扣掉。

    Returns:
        证据文本；内容被截断时一定带"此处为摘要，原文共 N 字符"标注。
    """
    head = f"{tool_name}（{status}）："
    if limit <= len(head):
        return head[:limit]
    return head + _render_body(
        payload, limit=limit - len(head), hint=_CONTINUE_HINTS.get(tool_name, "")
    )


def _render_body(
    payload: str | Mapping[str, Any] | None, *, limit: int, hint: str = ""
) -> str:
    if limit <= 0:
        return ""
    if isinstance(payload, Mapping):
        total = len(json.dumps(payload, ensure_ascii=False, default=str))
        return _render_structured(payload, limit=limit, total_chars=total, hint=hint)
    text = str(payload or "").strip()
    if not text:
        return _NO_CONTENT_TEXT
    parsed = _parse_json(text)
    if isinstance(parsed, Mapping):
        return _render_structured(parsed, limit=limit, total_chars=len(text), hint=hint)
    return _clip_text(_normalize_text(text), limit=limit, hint=hint)


def _render_structured(
    payload: Mapping[str, Any], *, limit: int, total_chars: int, hint: str = ""
) -> str:
    scalars = [
        f"{key}={_compact(payload[key])}" for key in _EVIDENCE_SCALAR_FIELDS if key in payload
    ]
    texts = [
        (key, _normalize_text(str(payload[key])))
        for key in _EVIDENCE_TEXT_FIELDS
        if isinstance(payload.get(key), str) and str(payload[key]).strip()
    ]
    others = [
        key
        for key in payload
        if key not in _EVIDENCE_SCALAR_FIELDS and key not in _EVIDENCE_TEXT_FIELDS
    ]
    whole = "，".join([*scalars, *[f"{key}={value}" for key, value in texts]])
    if not texts and not scalars and not others:
        return _NO_CONTENT_TEXT
    if len(whole) <= limit:
        if not others:
            return whole
        with_extra = f"{whole}；{_SKIPPED_NOTE.format(count=len(others))}"
        return with_extra if len(with_extra) <= limit else whole

    # 需要截断：**先给截断标注留位置**——它是可信度信息，被正文挤掉就会重现"模型以为工具没返回内容"
    # 那个缺陷。剩下的额度先给标识字段，再给正文片段。
    note = _SUMMARY_NOTE.format(total=total_chars) + hint
    budget = max(limit - len(note) - 1, 0)
    parts: list[str] = []
    used = 0
    for item in scalars:
        if used + len(item) + (1 if parts else 0) > budget:
            break
        parts.append(item)
        used += len(item) + 1
    dropped = len(scalars) - len(parts) + len(others)
    for key, value in texts:
        room = budget - used - len(key) - 1
        if room < _MIN_PIECE:
            dropped += 1
            continue
        piece = _cut(value, room=room)
        parts.append(f"{key}={piece}")
        used += len(piece) + len(key) + 1

    body = "，".join(parts)
    rendered = f"{body}；{note}" if body else note
    if dropped:
        extra = f"{rendered}；{_SKIPPED_NOTE.format(count=dropped)}"
        if len(extra) <= limit:
            rendered = extra
    return rendered[:limit]


def _clip_text(text: str, *, limit: int, hint: str = "") -> str:
    """纯文本的截断：同样带"只显示到这里"的标注。"""
    if len(text) <= limit:
        return text
    note = _SUMMARY_NOTE.format(total=len(text)) + hint
    room = max(limit - len(note) - 1, 0)
    return f"{_cut(text, room=room)}{note}"[:limit]


def _normalize_text(value: str) -> str:
    """正文类字段的归一：去掉 CR 与行尾空白，**保留换行**。

    压成一行会丢掉行结构，模型会当成"概述性文本"而不是原文（实测：它因此再次拒绝作答）。
    """
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def _cut(value: str, *, room: int) -> str:
    """把正文片段截到 ``room`` 字符内；尽量切在行边界，并补省略号。"""
    if len(value) <= room:
        return value
    piece = value[:room]
    head, _, _ = piece.rpartition("\n")
    # 行边界离额度太远就按字符切，避免浪费一半额度
    piece = head if head and len(head) >= room // 2 else piece.rstrip()
    return f"{piece}…"


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _compact(value: Any) -> str:
    """标识字段的浅展示：保持原值，过长时截断（不让单个字段吃掉额度）。"""
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int | float):
        return json.dumps(value)
    text = " ".join(str(value).split())
    return text if len(text) <= _SCALAR_CHARS else text[:_SCALAR_CHARS] + "…"

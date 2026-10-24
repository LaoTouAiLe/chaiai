"""文件读取和检索共用的增量解码与行片段；不保留超长行的全部内容。"""

from __future__ import annotations

import codecs
from collections.abc import Iterator
from pathlib import Path

from local_agent.security.policy import SecurityPolicy


class BinaryTextError(ValueError):
    """文本探测窗口含 NUL，不能作为正文返回。"""


def text_parts(path: Path, policy: SecurityPolicy, encoding: str) -> Iterator[tuple[str, bool]]:
    """产出 (文本片段, 是否结束一行)，保留换行，CRLF 跨块时仍是一行。"""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    pending_cr = ""
    first = True
    for data in policy.files.iter_bytes(path):
        probe = first
        if first:
            first = False
            normalized = codecs.lookup(encoding).name
            if not normalized.startswith(("utf-16", "utf-32")) and b"\x00" in data[:4096]:
                raise BinaryTextError("二进制文件，未返回内容")
        text = pending_cr + decoder.decode(data)
        if probe and "\x00" in text[:4096]:
            raise BinaryTextError("二进制文件，未返回内容")
        pending_cr = "\r" if text.endswith("\r") else ""
        if pending_cr:
            text = text[:-1]
        for part in text.splitlines(keepends=True):
            yield part, _ends_line(part)
    final = pending_cr + decoder.decode(b"", final=True)
    for part in final.splitlines(keepends=True):
        yield part, _ends_line(part)


def text_lines(
    path: Path, policy: SecurityPolicy, encoding: str, max_chars: int | None,
) -> Iterator[tuple[int, str, bool]]:
    """产出行号、可检索前缀与是否省略行尾；有限预算下内存不随行长增长。"""
    fragments: list[str] = []
    length = 0
    limited = False
    number = 1
    for part, ended in text_parts(path, policy, encoding):
        body = part.rstrip("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029") if ended else part
        remaining = len(body) if max_chars is None else max(0, max_chars - length)
        fragments.append(body[:remaining])
        length += min(len(body), remaining)
        limited = limited or len(body) > remaining
        if ended:
            yield number, "".join(fragments), limited
            fragments, length, limited = [], 0, False
            number += 1
        elif len(fragments) > 1 and not fragments[-1]:
            fragments.pop()  # 不为被省略的大行积累空字符串。
    if fragments:
        yield number, "".join(fragments), limited


def _ends_line(text: str) -> bool:
    return text.endswith(("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"))

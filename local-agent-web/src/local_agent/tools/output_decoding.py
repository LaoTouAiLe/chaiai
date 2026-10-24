"""命令输出按记录优先解码 UTF-8，兼容本机代码页及跨管道分块。"""

from __future__ import annotations

import codecs
import time

MAX_PENDING_BYTES = 4096
IDLE_FLUSH_SECONDS = 0.2


class OutputDecoder:
    """按换行/回车选编码；无换行时有界等待，已选编码保持到记录结束。

    无编码声明时无法区分所有字节序列：同时有效的记录优先 UTF-8。超过缓冲上限或
    等待时间时按已有前缀选编码；后续异常字节用替代字符显示，不改写已展示的文本。
    """

    def __init__(self, fallback_encoding: str) -> None:
        self._fallback = fallback_encoding
        self._pending = bytearray()
        self._decoder: codecs.IncrementalDecoder | None = None
        self._first = True
        self._since = 0.0

    def feed(self, data: bytes) -> str:
        if not self._pending:
            self._since = time.monotonic()
        self._pending.extend(data)
        parts: list[str] = []
        while self._pending:
            boundaries = [i for token in (b"\n", b"\r")
                          if (i := self._pending.find(token)) >= 0]
            end = min(boundaries) + 1 if boundaries else 0
            if end:
                size = min(end, MAX_PENDING_BYTES)
            elif len(self._pending) >= MAX_PENDING_BYTES:
                size = MAX_PENDING_BYTES
            elif self._decoder is not None:
                size = len(self._pending)
            elif self._pending.isascii():
                # 纯 ASCII 可立即展示，不能因此锁定后续中文的编码。
                parts.append(self._pending.decode("ascii"))
                self._pending.clear()
                self._first = False
                break
            else:
                break
            raw = bytes(self._pending[:size])
            del self._pending[:size]
            parts.append(self._decode(raw, final=size == end))
        return "".join(parts)

    def flush_idle(self) -> str:
        if self._pending and time.monotonic() - self._since >= IDLE_FLUSH_SECONDS:
            raw = bytes(self._pending)
            self._pending.clear()
            return self._decode(raw, final=False)
        return ""

    def finish(self) -> str:
        raw = bytes(self._pending)
        self._pending.clear()
        return self._decode(raw, final=True)

    def _decode(self, data: bytes, *, final: bool) -> str:
        if self._decoder is None:
            encoding = "utf-8-sig" if self._first else "utf-8"
            try:
                codecs.getincrementaldecoder(encoding)(errors="strict").decode(data, final=final)
            except UnicodeDecodeError:
                encoding = self._fallback
            self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        text = self._decoder.decode(data, final=final)
        self._first = False
        if final:
            self._decoder = None
        return text

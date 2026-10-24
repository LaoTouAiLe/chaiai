"""有界地读取当前可用管道数据，不等待后代进程关闭继承的句柄。"""

from __future__ import annotations

import os
from typing import IO, Any


def read_available(stream: IO[Any]) -> bytes:
    """最多读取 64 KiB；没有数据或管道关闭时立即返回。"""
    descriptor = stream.fileno()
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        available = wintypes.DWORD()
        peek = ctypes.windll.kernel32.PeekNamedPipe
        if not peek(wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)), None, 0,
                    None, ctypes.byref(available), None):
            return b""
        size = min(available.value, 65_536)
    else:
        import select

        size = 65_536 if select.select([stream], [], [], 0)[0] else 0
    return os.read(descriptor, size) if size else b""

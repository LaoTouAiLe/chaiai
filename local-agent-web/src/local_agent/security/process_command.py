"""Windows 命令行序列化：cmd 的脚本文本不使用 C runtime 参数转义。"""

from __future__ import annotations

import ntpath
import subprocess
from collections.abc import Sequence


def windows_command_line(command: Sequence[str]) -> str:
    """保留 Shell 适配器传给 ``cmd /d /s /c`` 的原始脚本。

    ``list2cmdline`` 会对脚本中的双引号添加 C runtime 的反斜杠转义；cmd
    不按 C runtime 的规则解释它。使用 /s /c 的外层引号，让 cmd 只剥掉
    这一层，内部脚本（包括 start 的空标题）保持原样。其他程序仍用标准转义。
    """
    if (
        len(command) == 5
        and ntpath.basename(command[0]).casefold() in {"cmd", "cmd.exe"}
        and [arg.casefold() for arg in command[1:4]] == ["/d", "/s", "/c"]
    ):
        return subprocess.list2cmdline(command[:4]) + ' "' + command[4] + '"'
    return subprocess.list2cmdline(command)

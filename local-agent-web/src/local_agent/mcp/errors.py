"""MCP 连接的失败分类（架构 §6.13）。

**为什么单独一个闭集**：界面要能解释"为什么这台 server 不可用"，而"连不上"有五种不同的
下一步动作（装 Node、等下载、改命令、看 server 日志、重开）。把它们都压成一句
"连接失败"等于让用户自己猜。

这里只放类型与常量，不依赖 SDK——连接层抛、编排层分类、投影层展示，三层读同一份闭集。
"""

from __future__ import annotations

from typing import Final

#: 本机找不到 command（常见：没装 Node，``npx`` 不存在）。
KIND_COMMAND_NOT_FOUND: Final[str] = "command_not_found"
#: 进程起来了但握手超时（常见：``npx`` 首次下载包）。
KIND_STARTUP_TIMEOUT: Final[str] = "startup_timeout"
#: 握手/协议协商失败（常见：对方不是 MCP server，或版本不兼容）。
KIND_HANDSHAKE_FAILED: Final[str] = "handshake_failed"
#: 单次 ``tools/call`` 超时。
KIND_CALL_TIMEOUT: Final[str] = "call_timeout"
#: 调用期间进程退出（常见：server 崩溃、凭据缺失导致自尽）。
KIND_SERVER_EXITED: Final[str] = "server_exited"
#: 请求不合协议（参数结构错、返回无法解析）。
KIND_PROTOCOL_ERROR: Final[str] = "protocol_error"
#: 没连上就发起了请求（调用方顺序错）。
KIND_NOT_CONNECTED: Final[str] = "not_connected"


class McpFailure(Exception):
    """一次 MCP 操作的失败。

    Attributes:
        kind: :data:`KIND_*` 之一（闭集，页面直接用它选文案）。
        message: **可直接展示给用户**的说明；不含密钥与内部栈。
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - 只为日志可读
        return f"[{self.kind}] {self.message}"

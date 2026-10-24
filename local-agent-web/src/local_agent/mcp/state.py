"""一台 MCP Server 的**用户状态**：开关与工具名单（架构 §6.13；M4）。

单独一个模块的理由很实际：它要同时被**设置库**（`model_settings/store.py`）与**编排层**
（`mcp/manager.py`）使用，而设置库不该为了一个 dataclass 去导入 ``mcp.client`` ——那会把
``mcp`` SDK 拉进"打开设置页"这条路径。

**默认关**（失败闭合）：库里没有这条记录 = 用户没表态 = 不连。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServerState:
    """一台 server 的表态。

    Attributes:
        enabled: 是否启用（默认 ``False``）。
        enabled_tools: 显式白名单；``None`` 表示不设限。
        disabled_tools: 黑名单，在白名单之后生效。
    """

    enabled: bool = False
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()


#: 未表态时的状态（**不要**把可变默认值挂在这里）。
DEFAULT_SERVER_STATE = ServerState()

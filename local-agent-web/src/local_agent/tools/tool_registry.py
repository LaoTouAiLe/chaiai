"""工具注册表（架构 §1.3-3、§2）。

职责：持有已实现的工具，并按运行模式给出**可绑定给模型**的工具集合。
闲聊模式的集合为空——闲聊分支不注册文件工具与 Shell 工具。

放行与否由 :class:`SecurityPolicy` 在 ``safety_gate_node`` 判定；注册表只负责
"有哪些工具、给谁看"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.tools import BaseTool

from local_agent.security.policy import SecurityPolicy
from local_agent.tools.file_tools import build_file_tools
from local_agent.tools.plan_tools import build_plan_tools
from local_agent.tools.search_tools import build_search_tools
from local_agent.tools.shell_tools import build_shell_tools

#: 各模式可绑定的工具名。
#: 写类工具也绑定给模型，但能否执行由 safety_gate_node 依据 SecurityPolicy 判定：
#: 覆盖写、删除、大范围修改在 T10 之前一律被拒，拒绝原因会回填给模型。
MODE_TOOLS: Mapping[str, tuple[str, ...]] = {
    "chat": ("list_dir", "read_file"),
    "tool": (
        "update_plan",
        "list_dir",
        "read_file",
        # 2026-09-30 新增：在此之前 tool 模式**没有任何检索能力**，而 Shell 兜底也不可用
        # （``shell_guard`` 的白名单要求参数完全匹配，搜索类命令一律落到"需人工审批"）。
        # 模型于是只能枚举目录 + 通读文件，既定位不准也读不全。这两个工具用于**先定位再定点读**。
        "grep",
        "glob",
        "edit_file",
        "write_file",
        "delete_file",
        "run_shell",
    ),
}


class ToolRegistry:
    """已实现工具的注册表。"""

    def __init__(self, tools: Sequence[BaseTool]) -> None:
        self._tools: dict[str, BaseTool] = {tool.name: tool for tool in tools}

    @classmethod
    def build(cls, policy: SecurityPolicy) -> ToolRegistry:
        """构造注册表（文件工具 + 安全 Shell）。

        Args:
            policy: 安全策略，工具通过闭包持有它做路径与命令校验。
        """
        return cls(
            [
                *build_file_tools(policy),
                *build_plan_tools(),
                *build_search_tools(policy),
                *build_shell_tools(policy),
            ]
        )

    def names(self) -> tuple[str, ...]:
        """已注册的工具名。"""
        return tuple(self._tools)

    def get(self, name: str) -> BaseTool | None:
        """按名字取工具；不存在返回 ``None``。"""
        return self._tools.get(name)

    def tools_for_mode(self, mode: str) -> list[BaseTool]:
        """该模式下可绑定给模型的工具。

        Args:
            mode: ``chat`` 或 ``tool``。

        Returns:
            工具列表；闲聊模式为空列表。
        """
        allowed = MODE_TOOLS.get(mode, ())
        return [self._tools[name] for name in allowed if name in self._tools]

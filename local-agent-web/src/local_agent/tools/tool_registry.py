"""工具注册表（架构 §1.3-3、§2、§6.9）。

职责：持有已实现的工具，并按"**运行模式 + Agent Profile**"给出可绑定给模型的工具集合。

放行与否由 :class:`SecurityPolicy` 在 ``safety_gate_node`` 判定；注册表只负责"有哪些工具、给谁看"。
**可见性不是授权**（架构 §6.9）：Profile 之间看不到的工具，模型既拿不到 Schema，也不能绕过注册表执行。
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import BaseTool

from local_agent.capabilities import CAPABILITY_TOOLS, tool_names_for
from local_agent.config import WebAccessConfig
from local_agent.exceptions import LocalAgentError
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.file_tools import build_file_tools
from local_agent.tools.plan_tools import build_plan_tools
from local_agent.tools.search_tools import build_search_tools
from local_agent.tools.shell_tools import build_shell_tools
from local_agent.tools.web_tools import build_web_tools


class ToolRegistry:
    """已实现工具的注册表。"""

    def __init__(self, tools: Sequence[BaseTool]) -> None:
        self._tools: dict[str, BaseTool] = {tool.name: tool for tool in tools}

    @classmethod
    def build(
        cls, policy: SecurityPolicy, *, web_access: WebAccessConfig | None = None
    ) -> ToolRegistry:
        """构造注册表（文件工具 + 计划工具 + 检索工具 + 安全 Shell + Web 工具）。

        构造时校验**能力归属覆盖**（架构 §6.9）：每个已实现的工具必须且只能归属一个能力，能力表里的
        名字也必须都已实现。漏登记的后果是"注册了但永远不给模型"，或"配了但没有实现"——两者都要等到
        很久以后才以"模型怎么不调用它"的形式暴露，所以在构造时失败闭合。

        `web` 的工具**总是被构造**（否则覆盖校验会失败），但"对模型可见"由两处决定：
        `web_access.enabled`（关掉时能力不进有效集合，见 `capabilities.effective_capabilities`）与
        Profile 是否声明 `web`。

        Args:
            policy: 安全策略。
            web_access: 外部访问配置；省略时按默认（关闭）构造，工具存在但不会可见。

        Raises:
            LocalAgentError: 能力归属不完整（重复归属、登记未实现、实现未登记）。
        """
        registry = cls(
            [
                *build_file_tools(policy),
                *build_plan_tools(),
                *build_search_tools(policy),
                *build_shell_tools(policy),
                *build_web_tools(web_access or WebAccessConfig()),
            ]
        )
        registry.assert_capability_coverage()
        return registry

    def assert_capability_coverage(self) -> None:
        """校验能力归属完整；不完整即抛错，不静默通过。"""
        registered = set(self._tools)
        owner: dict[str, str] = {}
        conflicts: list[str] = []
        for capability, names in CAPABILITY_TOOLS.items():
            for name in names:
                if name in owner:
                    conflicts.append(f"{name}（{owner[name]} / {capability}）")
                owner[name] = capability
        problems: list[str] = []
        if conflicts:
            problems.append("一个工具归属了多个能力：" + "、".join(sorted(conflicts)))
        unregistered = sorted(name for name in owner if name not in registered)
        if unregistered:
            problems.append("能力表登记了但未实现：" + "、".join(unregistered))
        undeclared = sorted(name for name in registered if name not in owner)
        if undeclared:
            problems.append("已实现但未登记能力归属：" + "、".join(undeclared))
        if problems:
            raise LocalAgentError(
                "工具的能力归属不完整（架构 §6.9）：" + "；".join(problems)
            )

    def names(self) -> tuple[str, ...]:
        """已注册的工具名。"""
        return tuple(self._tools)

    def get(self, name: str) -> BaseTool | None:
        """按名字取工具；不存在返回 ``None``。"""
        return self._tools.get(name)

    def tools_for(self, mode: str, capabilities: Sequence[str] | None = None) -> list[BaseTool]:
        """该模式 + 该 Profile 下可绑定给模型的工具（**顺序即绑定顺序**）。

        Args:
            mode: ``chat`` 或 ``tool``。闲聊固定绑定只读子集（``files_read``），忽略 ``capabilities``。
            capabilities: ``tool`` 模式下当前 Profile 的能力集合；``None`` 取默认 Profile（``coder``）。

        Returns:
            工具列表；同一输入恒得到同一序列（顺序稳定是前缀缓存的前提，架构 §6.9）。

        Raises:
            LocalAgentError: 解析出的工具名没有实现——状态不一致，宁可失败也不静默少给工具。
            KeyError: 模式或能力名未登记（配置校验与构造校验都拦不到时才会到这里）。
        """
        names = tool_names_for(mode, capabilities)
        missing = [name for name in names if name not in self._tools]
        if missing:
            raise LocalAgentError(
                "能力表登记了未实现的工具：" + "、".join(missing) + "（架构 §6.9）"
            )
        return [self._tools[name] for name in names]

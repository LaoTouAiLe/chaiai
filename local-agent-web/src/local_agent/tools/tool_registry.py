"""工具注册表（架构 §1.3-3、§2、§6.9）。

职责：持有已实现的工具，并按"**运行模式 + Agent Profile**"给出可绑定给模型的工具集合。

放行与否由 :class:`SecurityPolicy` 在 ``safety_gate_node`` 判定；注册表只负责"有哪些工具、给谁看"。
**可见性不是授权**（架构 §6.9）：Profile 之间看不到的工具，模型既拿不到 Schema，也不能绕过注册表执行。
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import BaseTool

from local_agent.capabilities import CAPABILITY_TOOLS, tool_names_for
from local_agent.config import GitConfig, WebAccessConfig
from local_agent.exceptions import LocalAgentError
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.file_tools import build_file_tools
from local_agent.tools.git_tools import build_git_tools, git_availability
from local_agent.tools.plan_tools import build_plan_tools
from local_agent.tools.search_tools import build_search_tools
from local_agent.tools.shell_tools import build_shell_tools
from local_agent.tools.web_tools import build_web_tools


class ToolRegistry:
    """已实现工具的注册表。"""

    def __init__(self, tools: Sequence[BaseTool]) -> None:
        self._tools: dict[str, BaseTool] = {tool.name: tool for tool in tools}
        #: 本机是否找得到 ``git`` 可执行文件（§16.4）。由 :meth:`build` 探测后覆盖；直接构造
        #: 注册表的测试与调用点保持"假定可用"——缺失是环境事实，不该由"没人告诉我"推导出来。
        self.git_available: bool = True
        #: 环境不可用时给用户看的可照做提示（缺失 git 时的安装说明）；能力状态页用它。
        self.git_unavailable_hint: str | None = None

    @classmethod
    def build(
        cls,
        policy: SecurityPolicy,
        *,
        web_access: WebAccessConfig | None = None,
        git: GitConfig | None = None,
        git_available: bool | None = None,
    ) -> ToolRegistry:
        """构造注册表（文件工具 + 计划工具 + 检索工具 + 安全 Shell + Web 工具）。

        构造时校验**能力归属覆盖**（架构 §6.9）：每个已实现的工具必须且只能归属一个能力，能力表里的
        名字也必须都已实现。漏登记的后果是"注册了但永远不给模型"，或"配了但没有实现"——两者都要等到
        很久以后才以"模型怎么不调用它"的形式暴露，所以在构造时失败闭合。

        `web` 与 `git` 的工具**总是被构造**（否则覆盖校验会失败），但"对模型可见"由两处决定：
        配置开关（关掉时能力不进有效集合，见 `capabilities.effective_capabilities`）与 Profile 是否
        声明该能力。`git` 还多一层**环境**条件：本机没有 `git` 可执行文件时同样不进有效集合（§16.4），
        这时工具仍然存在——调用它会得到"缺什么、怎么装"的明确失败，而不是静默空结果。

        Args:
            policy: 安全策略。
            web_access: 外部访问配置；省略时按默认（关闭）构造，工具存在但不会可见。
            git: Git 配置；省略时按默认（关闭 + 默认超时/上限）构造。
            git_available: 本机是否存在 ``git``；省略时用 :func:`~local_agent.tools.git_tools.git_availability`
                探测一次（测试与脚本可以注入确定的结果）。

        Raises:
            LocalAgentError: 能力归属不完整（重复归属、登记未实现、实现未登记）。
        """
        git_config = git or GitConfig()
        if git_available is None:
            probe = git_availability()
            available, hint = probe.available, probe.hint
        else:
            available, hint = git_available, None
        registry = cls(
            [
                *build_file_tools(policy),
                *build_plan_tools(),
                *build_search_tools(policy),
                *build_shell_tools(policy),
                *build_web_tools(web_access or WebAccessConfig()),
                *build_git_tools(
                    policy,
                    timeout_seconds=git_config.timeout_seconds,
                    max_output_chars=git_config.max_output_chars,
                ),
            ]
        )
        registry.git_available = available
        registry.git_unavailable_hint = hint
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

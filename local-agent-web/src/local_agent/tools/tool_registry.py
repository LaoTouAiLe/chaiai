"""工具注册表（架构 §1.3-3、§2、§6.9）。

职责：持有已实现的工具，并按"**运行模式 + Agent Profile**"给出可绑定给模型的工具集合。

放行与否由 :class:`SecurityPolicy` 在 ``safety_gate_node`` 判定；注册表只负责"有哪些工具、给谁看"。
**可见性不是授权**（架构 §6.9）：Profile 之间看不到的工具，模型既拿不到 Schema，也不能绕过注册表执行。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence

from langchain_core.tools import BaseTool

from local_agent.capabilities import CAPABILITY_TOOLS, EXTERNAL_TOOL_PREFIX, tool_names_for
from local_agent.config import GitConfig, SkillsConfig, WebAccessConfig
from local_agent.exceptions import LocalAgentError
from local_agent.mcp.catalog import ToolEntry
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.browser_tools import browser_availability, build_browser_tools
from local_agent.tools.delegate_tools import DelegateFn, build_delegate_tools
from local_agent.tools.file_tools import build_file_tools
from local_agent.tools.git_tools import build_git_tools, git_availability
from local_agent.tools.host_tools import build_host_tools
from local_agent.tools.image_tools import build_image_tools
from local_agent.tools.mcp_tools import build_mcp_tool_search
from local_agent.tools.plan_tools import build_plan_tools
from local_agent.tools.search_tools import build_search_tools
from local_agent.tools.shell_tools import build_shell_tools
from local_agent.tools.skill_tools import build_skill_tools
from local_agent.tools.terminal_tools import build_terminal_tools
from local_agent.tools.web_tools import build_web_tools


class ToolRegistry:
    """已实现工具的注册表。"""

    def __init__(self, tools: Sequence[BaseTool]) -> None:
        names = [tool.name for tool in tools]
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        if duplicates:
            # 外部工具名来自第三方 server，撞上内置工具名会**静默覆盖**其一——两条完全不同的
            # 执行路径共用一个名字。宁可启动失败。
            raise LocalAgentError(
                "工具名重复：" + "、".join(duplicates) + "（外部工具与内置工具不能同名）"
            )
        self._tools: dict[str, BaseTool] = {tool.name: tool for tool in tools}
        #: 本轮**绑给模型**的外部工具名（``Direct`` 档）；其余外部工具只经检索发现。
        self.mcp_direct: tuple[str, ...] = ()
        #: 本机是否找得到 ``git`` 可执行文件（§16.4）。由 :meth:`build` 探测后覆盖；直接构造
        #: 注册表的测试与调用点保持"假定可用"——缺失是环境事实，不该由"没人告诉我"推导出来。
        self.git_available: bool = True
        self.browser_available: bool = True
        #: 环境不可用时给用户看的可照做提示（缺失 git 时的安装说明）；能力状态页用它。
        self.git_unavailable_hint: str | None = None

    @classmethod
    def build(
        cls,
        policy: SecurityPolicy,
        *,
        web_access: WebAccessConfig | None = None,
        web_search_api_key: str | None = None,
        git: GitConfig | None = None,
        git_available: bool | None = None,
        skills: SkillsConfig | None = None,
        #: 这条连接是否声明支持图像输入（``llm.supports_vision``，2026-10-16）。
        #: **默认 False = 失败闭合**：没声明就是"收不了图"，`view_image` 照旧存在但会明确拒绝。
        vision_available: bool = False,
        #: 用户在设置里禁用的技能名（技能管理页，2026-10-16）：转交给技能工具，
        #: 让"清单"与"取正文"看到同一份事实。
        disabled_skills: Iterable[str] = (),
        delegate: DelegateFn | None = None,
        #: **本轮已知的全部 MCP 工具**（来自已连接 server 的目录，架构 §6.13）。
        #: 名字必须以 ``mcp__`` 开头，且只能由装配层从目录构造——不接受任意工具混进来。
        mcp_tools: Sequence[BaseTool] = (),
        #: 本轮**绑给模型**的外部工具名（``Direct`` 档）。必须是 ``mcp_tools`` 的子集；
        #: 其余外部工具不绑，等模型经 ``mcp_tool_search`` 找到后按名调用。
        mcp_direct: Sequence[str] = (),
        #: 检索工具的候选来源（读**当前**目录）。省略时返回空——检索工具仍然存在，只是搜不到东西。
        #: 它总是被构造（与 git/browser 同一口径：能力关掉时工具在、不可见）。
        mcp_search_provider: Callable[[], Sequence[ToolEntry]] | None = None,
        mcp_search_limit: int = 8,
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
            web_search_api_key: 检索服务（Tavily）的密钥明文，由应用层装配每轮上下文时从凭据存储取出。
                省略表示只有旧环境变量这条路。**它只进工具闭包**：不进任何 DTO、快照或日志。
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
        # 外部工具名的前缀是**唯一**的开口标识：不带它的东西不许借这条路径进来。
        invalid_external = sorted(
            tool.name for tool in mcp_tools if not tool.name.startswith(EXTERNAL_TOOL_PREFIX)
        )
        if invalid_external:
            raise LocalAgentError(
                "外部工具名必须以 mcp__ 开头（架构 §6.13）：" + "、".join(invalid_external)
            )
        registry = cls(
            [
                *build_file_tools(policy),
                *build_plan_tools(),
                *build_search_tools(policy),
                *build_shell_tools(policy),
                *build_terminal_tools(policy),
                *build_browser_tools(policy),
                *build_host_tools(policy),
                *build_web_tools(web_access or WebAccessConfig(), api_key=web_search_api_key),
                *build_git_tools(
                    policy,
                    timeout_seconds=git_config.timeout_seconds,
                    max_output_chars=git_config.max_output_chars,
                ),
                # v0.5 T55：技能工具没有"环境依赖"这一层（不像 git 要看本机有没有可执行文件），
                # 只有一个配置开关，因此这里不需要探测结果。
                *build_skill_tools(skills or SkillsConfig(), disabled_skills=disabled_skills),
                # 2026-10-16：看图（只读）。与 git 一样"总是被构造"，能不能用由能力层与工具内部
                # 的失败闭合共同决定。
                *build_image_tools(policy, vision_available=vision_available),
                # 2026-10-13：自主委派（§18.7）。运行依赖（配置 / 模型 / 策略 / 用量 / 取消）只在装配时
                # 可得，所以这里把运行侧的执行入口闭包进来——工具执行节点只传调用参数（`_invoke_tool`），
                # 拿不到 `RuntimeContext`，这是 A 方案的注入点。
                *build_delegate_tools(delegate),
                # 检索工具**总是**被构造：``mcp`` 能力关掉时它不可见，但覆盖校验要求
                # "能力表里登记的必须实现"（架构 §6.9），所以不能按开关决定造不造。
                build_mcp_tool_search(
                    mcp_search_provider or (lambda: ()), limit=mcp_search_limit
                ),
                *mcp_tools,
            ]
        )
        registry.mcp_direct = tuple(mcp_direct)
        registry.git_available = available
        registry.browser_available, _ = browser_availability()
        registry.git_unavailable_hint = hint
        registry.assert_capability_coverage()
        return registry

    def assert_capability_coverage(self) -> None:
        """校验能力归属完整；不完整即抛错，不静默通过。

        **外部工具（``mcp__`` 前缀）是闭集唯一的开口**（架构 §6.13）：它们不进静态表，但必须
        归到 ``mcp`` 能力下。开口被三条约束压住——名字带前缀、能力表里确实有 ``mcp``、
        且不得与静态登记的工具重名。三条任一不满足都抛错，而不是"放行一点"。
        """
        registered = set(self._tools)
        owner: dict[str, str] = {}
        conflicts: list[str] = []
        for capability, names in CAPABILITY_TOOLS.items():
            for name in names:
                if name in owner:
                    conflicts.append(f"{name}（{owner[name]} / {capability}）")
                owner[name] = capability

        problems: list[str] = []
        external = sorted(name for name in registered if name.startswith(EXTERNAL_TOOL_PREFIX))
        if external and "mcp" not in CAPABILITY_TOOLS:
            problems.append("检测到 mcp__ 外部工具，但能力表里没有 mcp 能力")
        for name in external:
            if name in owner:
                conflicts.append(f"{name}（静态表 / mcp）")
            owner[name] = "mcp"

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

    def external_names(self) -> tuple[str, ...]:
        """本轮已知的**外部**工具名（含 ``Deferred``，不含未连接的 server）。

        执行侧可见性（``capabilities.is_visible``）要的是**全部已知**，不是"绑给模型的那批"：
        模型经检索看到某个 ``Deferred`` 工具之后必须能调用它，否则检索就是个摆设。
        绑定范围另由 :meth:`tools_for` 控制。
        """
        return tuple(
            name for name in self._tools if name.startswith(EXTERNAL_TOOL_PREFIX)
        )

    def get(self, name: str) -> BaseTool | None:
        """按名字取工具；不存在返回 ``None``。"""
        return self._tools.get(name)

    def replace_mcp_tools(self, tools: Sequence[BaseTool]) -> None:
        """由当轮连接目录更新动态成员，静态成员保持闭集。"""
        names = [tool.name for tool in tools]
        if (any(not name.startswith(EXTERNAL_TOOL_PREFIX) for name in names)
                or len(names) != len(set(names))):
            raise LocalAgentError("MCP 目录包含非法或重复工具名")
        self._tools = {
            **{name: tool for name, tool in self._tools.items()
               if not name.startswith(EXTERNAL_TOOL_PREFIX)},
            **{tool.name: tool for tool in tools},
        }

    def tools_for(self, mode: str, capabilities: Sequence[str] | None = None, *,
                  discovered_mcp: Sequence[str] = ()) -> list[BaseTool]:
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
        names = list(tool_names_for(mode, capabilities))
        # 外部工具只在本轮 ``mcp`` 能力开启时绑定 ``Direct`` 档；其余留给 ``mcp_tool_search``。
        # ``capabilities is None`` 表示"默认基线"（不含 mcp），因此这里也要求显式给出能力集。
        if mode == "tool" and capabilities is not None and "mcp" in capabilities:
            names.extend(name for name in self.mcp_direct if name in self._tools)
            names.extend(name for name in discovered_mcp
                         if name.startswith(EXTERNAL_TOOL_PREFIX) and name in self._tools
                         and name not in names)
        missing = [name for name in names if name not in self._tools]
        if missing:
            raise LocalAgentError(
                "能力表登记了未实现的工具：" + "、".join(missing) + "（架构 §6.9）"
            )
        return [self._tools[name] for name in names]

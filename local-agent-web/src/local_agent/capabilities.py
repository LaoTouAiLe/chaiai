"""能力（capability）与 Agent Profile 的静态定义（架构 §6.9、§16.1，T50）。

**这是叶子模块**：只放数据与纯函数，不导入任何项目内模块。原因很实际——``config`` 必须在**配置加载
阶段**校验 Profile 名与能力名，而 ``tools.tool_registry`` 也要读同一份定义；若把定义放在注册表里，
就会形成 ``config → tool_registry → security.policy → config`` 的循环导入，最后只能靠延迟导入打补丁。

三层关系（架构 §6.9）：

- **能力**：工具的静态分组。一个已实现的工具**必须且只能**归属一个能力；未登记的工具不能绑定给模型。
- **Profile**：能力集合的静态命名组合（外加提示段落），本模块只负责能力集合这一半。
- **模式（mode）**：每会话的运行档位。``chat`` 固定绑定 ``files_read``（v0.1 起的行为，v0.5 不改）；
  ``tool`` 绑定**当前 Profile** 的能力集。

**顺序即绑定顺序**：能力内的工具顺序由 :data:`CAPABILITY_TOOLS` 固定，Profile 里能力的先后决定各段
的先后；拼出来的名字序列进入系统前缀，一旦变化，供应商前缀缓存整段失效（架构 §6.7 记过同类代价）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: 能力闭集 → 该能力的工具名（**顺序即绑定顺序**）。
#:
#: 新增工具时必须同时登记能力归属：漏登记会在构造注册表时失败（见
#: :meth:`~local_agent.tools.tool_registry.ToolRegistry.build`），不留"静默不给模型"或"静默给错"。
CAPABILITY_TOOLS: Mapping[str, tuple[str, ...]] = {
    "plan": ("update_plan",),
    "files_read": ("list_dir", "read_file"),
    "code_search": ("grep", "glob"),
    "files_write": ("edit_file", "write_file", "delete_file"),
    "shell": ("run_shell",),
}

#: 内置 Profile → 能力集合（架构 §16.1）。名字固定，配置与验收都用这三个名字。
#:
#: ``coder`` 与 v0.4 ``tool`` 模式的工具集**逐项等价**（含顺序），因此它是默认 Profile，既有行为不变；
#: ``researcher`` / ``reviewer`` 是只读子集，后续任务（Web、Git）再往它们身上加能力。
PROFILES: Mapping[str, tuple[str, ...]] = {
    "coder": ("plan", "files_read", "code_search", "files_write", "shell"),
    "researcher": ("plan", "files_read", "code_search"),
    "reviewer": ("files_read", "code_search"),
}

#: 默认 Profile：等价于 v0.4 的工具集，保证升级后行为不变。
DEFAULT_PROFILE = "coder"

#: 运行模式 → 能力集合。
#:
#: ``chat`` 是**既有行为**（v0.1 起闲聊也能列目录与读文件），v0.5 刻意不改它——Profile 只作用于
#: ``tool`` 模式。把闲聊也交给自己声明的 Profile，会让"关掉新能力后基础行为不变"这条硬验收无法成立。
MODE_CAPABILITIES: Mapping[str, tuple[str, ...]] = {
    "chat": ("files_read",),
    "tool": PROFILES[DEFAULT_PROFILE],
}

#: 已知运行模式（固定顺序）。
KNOWN_MODES: tuple[str, ...] = tuple(MODE_CAPABILITIES)


def profile_names() -> tuple[str, ...]:
    """内置 Profile 名（固定顺序，配置错误提示按这个顺序列出）。"""
    return tuple(PROFILES)


def capability_names() -> tuple[str, ...]:
    """能力闭集的名字（固定顺序）。"""
    return tuple(CAPABILITY_TOOLS)


def unknown_capabilities(names: Sequence[str]) -> list[str]:
    """``names`` 里未登记的能力名（保持输入顺序，去重）；全部已登记时返回空列表。"""
    seen: set[str] = set()
    unknown: list[str] = []
    for name in names:
        if name not in CAPABILITY_TOOLS and name not in seen:
            unknown.append(name)
            seen.add(name)
    return unknown


def duplicated_capabilities(names: Sequence[str]) -> list[str]:
    """``names`` 里重复出现的能力名（保持首次出现顺序）。"""
    seen: set[str] = set()
    repeated: list[str] = []
    for name in names:
        if name in seen and name not in repeated:
            repeated.append(name)
        seen.add(name)
    return repeated


def resolve_capabilities(
    profile: str, custom: Mapping[str, Sequence[str]] | None = None
) -> tuple[str, ...]:
    """把 Profile 名解析成能力集合。

    Args:
        profile: Profile 名（内置名，或 ``custom`` 里定义的名字）。
        custom: 自定义 Profile（名字 → 能力列表）；内置名优先，不允许被覆盖。

    Returns:
        能力集合（顺序即配置里写的顺序，故同一份配置结果稳定）。

    Raises:
        KeyError: 名字既不是内置 Profile，也不在 ``custom`` 里。
    """
    if profile in PROFILES:
        return PROFILES[profile]
    if custom and profile in custom:
        return tuple(custom[profile])
    raise KeyError(profile)


def is_visible(mode: str, capabilities: Sequence[str] | None, name: str) -> bool:
    """``name`` 是否可见（=会被发给模型）。

    **可见性是执行侧的前置条件**（架构 §6.9、§16.1）：安全门与执行节点都必须用它做拦截，
    否则"没绑定给模型"就只是"没告诉它"，模型自己报出名字照样能跑——2026-10-08 的真实基线
    实证过一次：``reviewer`` 档（只读）照提示词里的 ``write_file`` 说明发起调用，文件真的被创建了。
    """
    return name in tool_names_for(mode, capabilities)


def tool_names_for(mode: str, capabilities: Sequence[str] | None = None) -> tuple[str, ...]:
    """该模式下发模型的工具名序列（**顺序即绑定顺序**）。

    Args:
        mode: ``chat`` 或 ``tool``；``chat`` 忽略 ``capabilities``，固定绑定 ``files_read``。
        capabilities: ``tool`` 模式下当前 Profile 的能力集合；``None`` 表示默认 Profile。

    Returns:
        工具名元组。

    Raises:
        KeyError: 模式或能力名未登记（配置校验与注册表构造已各自拦过，这里是最后一道）。
    """
    if mode == "chat":
        resolved: Sequence[str] = MODE_CAPABILITIES["chat"]
    elif mode == "tool":
        resolved = PROFILES[DEFAULT_PROFILE] if capabilities is None else capabilities
    else:
        raise KeyError(f"未知运行模式：{mode}")
    names: list[str] = []
    for capability in resolved:
        names.extend(CAPABILITY_TOOLS[capability])
    return tuple(names)

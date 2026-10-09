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
from dataclasses import dataclass
from typing import Literal

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
    "web": ("web_search", "web_fetch"),
    # Git 的读与写分成两个能力（§6.9）：安全门仍逐个工具判定，但档位成员资格要能分别表态——
    # researcher 只该"看得见工作区的差异"，不该"能提交"（§16.4）。
    "git_read": ("git_status", "git_diff", "git_log"),
    "git_write": ("git_commit",),
    # v0.5 T55：技能清单与正文（只读，见 §6.12、§16.5）。新增能力一律追加在末尾：前面的字节不变，
    # 前缀缓存的前半段仍可复用（与 profiles 里追能力的口径一致）。
    "skill": ("skill_list", "skill_load"),
}

#: **会带进外部内容**的能力（架构 §6.10、§16.2、§16.3）。
#:
#: 用途只有一个但很关键：一轮里只要出现过这些能力的工具结果，该轮产出的记忆候选一律按
#: ``untrusted`` 落库（取最保守的一侧），外部内容因此不能经由"记忆"这条路变成长期可信。
UNTRUSTED_CAPABILITIES: tuple[str, ...] = ("web",)

#: 内置 Profile → 能力集合（架构 §16.1）。名字固定，配置与验收都用这三个名字。
#:
#: ``coder`` 与 v0.4 ``tool`` 模式的工具集**逐项等价**（含顺序），因此它是默认 Profile，既有行为不变；
#: ``researcher`` / ``reviewer`` 是只读子集，后续任务（Web、Git）再往它们身上加能力。
PROFILES: Mapping[str, tuple[str, ...]] = {
    "coder": (
        "plan", "files_read", "code_search", "files_write", "shell",
        "git_read", "git_write",
        "skill",
    ),
    "researcher": ("plan", "files_read", "code_search", "web", "git_read", "skill"),
    "reviewer": ("files_read", "code_search", "skill"),
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


def effective_capabilities(
    capabilities: Sequence[str], *, web_access_enabled: bool, git_enabled: bool, skills_enabled: bool
) -> tuple[str, ...]:
    """按配置开关与运行环境收窄能力集合（架构 §16.3、§16.4）。

    关掉的开关对应的能力对模型**完全不可见**：`web_access.enabled=false` 时 `web` 被移除；
    `git.enabled=false`（或本机**没有 git 可执行文件**）时 `git_read` / `git_write` 被移除——
    于是提示词不会提到它们、工具也不会绑给模型。"关闭新增能力后基础行为不变"靠的就是这一条。

    `git_enabled` 由调用方把两件事合起来：配置开关 **且** `git` 可用（见 `tools.git_tools.git_availability`）。
    两台机器上"看起来配置一样却行为不同"的根因就藏在这里，所以这个参数没有默认值。
    """
    switch = {
        "web": web_access_enabled,
        "git_read": git_enabled,
        "git_write": git_enabled,
        "skill": skills_enabled,
    }
    return tuple(capability for capability in capabilities if switch.get(capability, True))


#: 能力的中文名（页面展示用）。**页面只展示这个，不展示原始工具名**（§16.6、§15.8 的字段门槛）：
#: 用户关心的是"这台机器现在能不能联网/能不能提交"，不是内部绑定了哪几个工具。
CAPABILITY_LABELS: Mapping[str, str] = {
    "plan": "任务计划",
    "files_read": "读取工作区文件",
    "code_search": "检索代码",
    "files_write": "修改工作区文件",
    "shell": "执行命令",
    "web": "联网检索与抓取",
    "git_read": "查看 Git 状态与差异",
    "git_write": "提交到 Git",
    "skill": "按需加载技能",
}

#: 能力状态的原因类型。**只有这四种**：DTO 直接复用它，页面因此不会遇到"没见过的原因"。
#: 定在这里（而不是各层各写一份字面量）是为了让"页面解释"与"后端判定"共用同一个闭集。
CapabilityReason = Literal["available", "profile", "disabled", "missing_dependency"]

#: 能力不可用的原因（**闭集**，页面据此解释，不允许自造一种）。
REASON_AVAILABLE: CapabilityReason = "available"
#: 当前档位没有声明这个能力（换档即可）。
REASON_PROFILE: CapabilityReason = "profile"
#: 配置开关关着（用户可自行打开）。
REASON_DISABLED: CapabilityReason = "disabled"
#: 运行环境缺东西（例如本机没装 git）：提示必须说"缺什么、怎么装"（§16.4、§16.6）。
REASON_MISSING_DEPENDENCY: CapabilityReason = "missing_dependency"


@dataclass(frozen=True)
class CapabilityStatus:
    """一个能力的对外状态：可用与否、**为什么**、以及怎么才能用（§16.6）。"""

    capability: str
    label: str
    available: bool
    reason: CapabilityReason
    detail: str
    hint: str | None = None


def capability_status(
    capabilities: Sequence[str],
    *,
    web_access_enabled: bool,
    git_access_enabled: bool,
    git_available: bool,
    skills_enabled: bool,
    git_hint: str | None = None,
) -> tuple[CapabilityStatus, ...]:
    """把当前档位的能力集合展开成**可解释的状态列表**（§16.6）。

    对闭集里的**每个**能力都给一行——不可用的也要在，并且必须带上"为什么 + 怎么办"：
    "只灰掉不解释"会让用户以为功能坏了，而多数情况只是档位或开关的问题。

    Args:
        capabilities: 当前 Profile **声明**的能力集合（`PROFILES[active]` 或 custom）。
        web_access_enabled: `web_access.enabled`。
        git_access_enabled: `git.enabled`（配置开关）。
        git_available: 本机是否存在 git 可执行文件（`tools.git_tools.git_availability`）。
        git_hint: 缺 git 时给用户的可照做提示；缺它时退化为一句通用说明，不假装"稍后可用"。

    Returns:
        按 :data:`CAPABILITY_TOOLS` 顺序排列的状态行（顺序稳定，页面不会跳来跳去）。
    """
    declared = set(capabilities)
    rows: list[CapabilityStatus] = []
    for capability in CAPABILITY_TOOLS:
        label = CAPABILITY_LABELS.get(capability, capability)
        if capability not in declared:
            rows.append(
                CapabilityStatus(
                    capability=capability,
                    label=label,
                    available=False,
                    reason=REASON_PROFILE,
                    detail="当前 Agent 档位没有包含这个能力。",
                    hint="把 profiles.active 切到包含它的档位（coder / researcher / reviewer），或改 profiles.custom。",
                )
            )
            continue
        if capability == "web" and not web_access_enabled:
            rows.append(
                CapabilityStatus(
                    capability=capability,
                    label=label,
                    available=False,
                    reason=REASON_DISABLED,
                    detail="联网开关关着：本机 Agent 一旦能联网，攻击面会扩到互联网，所以默认关闭。",
                    hint="把 web_access.enabled 设为 true（并按 §16.3 的边界使用）。",
                )
            )
            continue
        if capability in {"git_read", "git_write"} and not git_access_enabled:
            rows.append(
                CapabilityStatus(
                    capability=capability,
                    label=label,
                    available=False,
                    reason=REASON_DISABLED,
                    detail="Git 开关关着：默认不让 Agent 读仓库历史（更不让它提交）。",
                    hint="把 git.enabled 设为 true。",
                )
            )
            continue
        if capability in {"git_read", "git_write"} and not git_available:
            rows.append(
                CapabilityStatus(
                    capability=capability,
                    label=label,
                    available=False,
                    reason=REASON_MISSING_DEPENDENCY,
                    detail="这台机器上找不到 git 可执行文件，因此该能力不会绑给模型。",
                    hint=git_hint
                    or "安装 Git（Windows: https://git-scm.com/download/win ；macOS: brew install git ；"
                    "Debian/Ubuntu: sudo apt install git）后重启本应用。",
                )
            )
            continue
        if capability == "skill" and not skills_enabled:
            rows.append(
                CapabilityStatus(
                    capability=capability,
                    label=label,
                    available=False,
                    reason=REASON_DISABLED,
                    detail=(
                        "技能开关关着：默认不让 Agent 读取本机已有的技能说明"
                        "（清单会占提示预算，内容也不一定是为你这个项目写的）。"
                    ),
                    hint="把 skills.enabled 设为 true（并按 §16.5 的发现根与边界使用）。",
                )
            )
            continue
        rows.append(
            CapabilityStatus(
                capability=capability,
                label=label,
                available=True,
                reason=REASON_AVAILABLE,
                detail="当前可用。",
            )
        )
    return tuple(rows)


def untrusted_tool_names() -> tuple[str, ...]:
    """会带进外部内容的工具名（架构 §16.3）。

    记忆写入按"本轮有没有出现过这些工具的结果"决定候选的来源级别——见
    :func:`~local_agent.memory.schemas.is_auto_injectable` 与 §16.2 的最保守一侧。
    """
    names: list[str] = []
    for capability in UNTRUSTED_CAPABILITIES:
        names.extend(CAPABILITY_TOOLS[capability])
    return tuple(names)


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

"""能力（capability）、**能力开关**与评测用档位的静态定义（架构 §6.9、§16.1）。

**这是叶子模块**：只放数据与纯函数，不导入任何项目内模块。原因很实际——``config`` 必须在**配置加载
阶段**校验档位名与能力名，而 ``tools.tool_registry`` 也要读同一份定义；若把定义放在注册表里，
就会形成 ``config → tool_registry → security.policy → config`` 的循环导入，最后只能靠延迟导入打补丁。

四层关系（架构 §6.9；2026-10-13 起"档位"退出运行期绑定）：

- **能力**：工具的静态分组。一个已实现的工具**必须且只能**归属一个能力；未登记的工具不能绑定给模型。
- **开关（运行期唯一真源）**：每个能力一个布尔开关，**开关的取值就是本次运行的能力集合**。
  用户改过的开关存在设置库，``config.yaml`` 只提供出厂默认（§16.1）。
- **档位（``PROFILES``，仅评测与出厂默认用）**：能力集合的静态命名组合。它**不再参与运行期绑定**，
  只剩两个用途：① 作为"出厂默认开关表"的命名来源（未在页面改过开关时用哪一组）；② 离线评测与
  同题对照要一组**可复现**的固定能力集（§18.6 要求"两臂能力相同"）。页面上不再有档位选择器。
- **模式（mode）**：每会话的运行档位。``chat`` 固定绑定 ``files_read``（v0.1 起的行为，v0.5 不改）；
  ``tool`` 绑定**当前开关集合**。

**顺序即绑定顺序**：能力内的工具顺序由 :data:`CAPABILITY_TOOLS` 固定，开关集合按
:data:`CAPABILITY_TOOLS` 的先后拼出名字序列；拼出来的名字序列进入系统前缀，一旦变化，供应商前缀
缓存整段失效（架构 §6.7 记过同类代价）。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

#: 外部工具（MCP）的规范名前缀（架构 §6.13）。
#:
#: **这是能力闭集唯一的开口点**：静态表 ``CAPABILITY_TOOLS`` 在编码期就把工具定死了，
#: 而 MCP 的工具名要等 server 报 ``tools/list`` 才知道。开口的代价被这三条压住：
#:
#: 1. 只有带这个前缀的名字才可能来自运行期（其余名字仍必须静态登记）；
#: 2. 它们**只能**归属 ``mcp`` 能力（不能借这个前缀混进 ``shell`` 之类的能力）；
#: 3. 关掉 ``mcp`` 能力时，执行侧与可见侧同时失效（``is_visible`` 的两条路都走能力集判断）。
#:
#: 三处（安全门 / 注册表覆盖校验 / 可见性）读同一个常量，不各写一份字面量。
EXTERNAL_TOOL_PREFIX: Final[str] = "mcp__"



def readonly_child_capabilities(parent: tuple[str, ...]) -> tuple[str, ...]:
    """T67 只收窄父有效权限，保持其固定顺序。"""
    return tuple(capability for capability in parent if capability in {
        "files_read", "code_search", "web", "shell", "plan", "task_lifecycle",
    })

#: 能力闭集 → 该能力的工具名（**顺序即绑定顺序**）。
#:
#: 新增工具时必须同时登记能力归属：漏登记会在构造注册表时失败（见
#: :meth:`~local_agent.tools.tool_registry.ToolRegistry.build`），不留"静默不给模型"或"静默给错"。
CAPABILITY_TOOLS: Mapping[str, tuple[str, ...]] = {
    # 2026-10-28：`ask_user` 是"执行中问一句并等你回答"（架构 §6.14）。挂在 `task_lifecycle` 下
    # 而不是另开能力：它和 `task_control` 是同一件事的两个出口（一个结束本轮、一个挂起后继续），
    # 另开开关只会造出"能结束但不能问"的怪状态。追加在末尾，前面的绑定顺序不变。
    "task_lifecycle": ("task_control", "ask_user"),
    "plan": ("update_plan",),
    "files_read": ("list_dir", "read_file"),
    # 2026-10-22：`search_files` 是"按内容搜/按名字找"的合并入口（同一能力、同一只读门）。
    "code_search": ("grep", "glob", "search_files"),
    # 2026-10-27：`memory` 是常驻记忆（MEMORY.md / USER.md，架构 §10.7）的写入口。挂在
    # `files_write` 下而不是另开能力：它本质就是"写两份文件"，另开一个能力会在「能力」面板与
    # 「设置 → 记忆」之间造出两个开关。是否真的绑定由 `ToolRegistry.unavailable_tools` 决定
    # （设置里两项都关时从 schema 移除，与参照实现"工具消失"同效）。
    "files_write": ("edit_file", "write_file", "delete_file", "download_file", "memory"),
    # 2026-10-21：`read_command` / `stop_command` 属于 shell 能力——它们只操作**本会话**里
    # 由 `run_shell` 转后台的命令，不是一条新的执行通道，因此不单开能力、也不放宽任何门。
    "shell": ("run_shell", "read_command", "stop_command", "execute_code"),
    "web": ("web_search", "web_fetch"),
    # Git 的读与写分成两个能力（§6.9）：安全门仍逐个工具判定，但开关要能分别表态——
    # 只看差异不该等于"能提交"（§16.4）。
    "git_read": ("git_status", "git_diff", "git_log"),
    "git_write": ("git_commit",),
    # v0.5 T55：技能清单与正文（只读，见 §6.12、§16.5）。新增能力一律追加在末尾：前面的字节不变，
    # 前缀缓存的前半段仍可复用。
    "skill": ("skill_list", "skill_load"),
    # 2026-10-13：**自主委派**（§18.7）——主 Agent 判断某段只读调查可以并行、且不值得占自己的上下文时，
    # 把它派给隔离子 Agent，只把核对过的原文报告拿回来。同样追加在末尾。
    # 它**不进任何内置档位声明**（`PROFILES`），出厂默认也是关：默认配置下的工具集与系统前缀因此
    # 逐字不变（§16.1 的前缀口径），打开它才是有意的前缀变化。
    "delegate": ("delegate_research",),
    # 2026-10-16：**看图**（`view_image`）——把本机图片（截图、设计稿、图表）作为图像内容交给模型。
    # 它只读本机文件、不改任何状态，本可与 `files_read` 合族；**单列一个能力**的理由只有一个：
    # 它绑不绑取决于**模型能不能收图**（见 `enabled_capabilities` 的 `vision_available`），
    # 而 `files_read` 是硬依赖、不能因为模型不支持图像就被摘掉。同样追加在末尾。
    "image_read": ("view_image",),
    "terminal": ("start_shell", "read_shell", "write_shell", "stop_shell", "list_shell_sessions", "inspect_system"),
    "page_control": ("browser",),
    # 2026-10-20：**MCP 外部工具**（架构 §6.13）。这里只登记那个**固定的**检索工具；
    # 外部 server 的工具名是运行期才知道的，它们经由 :data:`EXTERNAL_TOOL_PREFIX` 归到本能力，
    # 不进这张静态表（否则每次配置 server 都要改源码）。同样追加在末尾：前面的字节不变。
    "mcp": ("mcp_tool_search",),
}

#: **会带进外部内容**的能力（架构 §6.10、§16.2、§16.3）。
#:
#: 用途只有一个但很关键：一轮里只要出现过这些能力的工具结果，该轮产出的记忆候选一律按
#: ``untrusted`` 落库（取最保守的一侧），外部内容因此不能经由"记忆"这条路变成长期可信。
#:
#: ``mcp`` 自 2026-10-20 起在列：MCP 工具的描述与返回值都来自**第三方进程**，与网页同级。
UNTRUSTED_CAPABILITIES: tuple[str, ...] = ("web", "page_control", "mcp")

#: 评测与出厂默认用的档位 → 能力集合（架构 §16.1）。
#:
#: ``coder`` 的工具集 = v0.4 那九个工具 **+ 常态化的联网**（2026-10-14：``web`` 现在是三个档位都有的
#: 常态能力，不再"只有研究档才有"——见 :data:`LOCKED_CAPABILITIES`）。它是默认档。
#: **不再参与运行期绑定**：运行期读的是开关表（见 :func:`resolve_switches`），档位只贡献"出厂默认
#: 开关表"与评测用的固定能力集。想把某档当默认，就在配置里写 ``profiles.active``。
PROFILES: Mapping[str, tuple[str, ...]] = {
    "coder": (
        "task_lifecycle", "plan", "files_read", "code_search", "files_write", "shell", "web",
        "git_read", "git_write",
        "skill",
        # 2026-10-16：看图（只读）。进三档（含只读档）——"看见渲染结果"与"读文件"一样是只读动作。
        "image_read",
        "terminal", "page_control",
    ),
    "researcher": ("task_lifecycle", "plan", "files_read", "code_search", "web", "git_read", "skill", "image_read"),
    "reviewer": ("task_lifecycle", "files_read", "code_search", "web", "skill", "image_read"),
}

#: 默认档位：等价于 v0.4 的工具集，保证升级后行为不变。
DEFAULT_PROFILE = "coder"

#: **出厂默认开关**（架构 §16.1）：用户没在页面改过任何开关时生效的那一组。
#:
#: 与 :data:`PROFILES` 的差异是刻意的——``git_write`` 默认**关**：它改的是仓库状态，打开应当是显式
#: 决定（§16.4）。``git_read`` 与 ``skill`` 默认也与 ``coder`` 不同：前者取决于本机有没有 git
#: （环境条件，见 :func:`enabled_capabilities`），后者默认不读本机技能说明。``delegate`` 默认关
#: （§18.7）：它会让模型自主决定花额外模型调用。
#:
#: ``web`` 自 2026-10-14 起**默认开**并且**不可关闭**（见 :data:`LOCKED_CAPABILITIES`）：联网是本机
#: Agent 的常态能力——"问一句简单的事，模型只能拿训练数据答"不算能用的助手。
CAPABILITY_DEFAULTS: Mapping[str, bool] = {
    "task_lifecycle": True,
    "plan": True,
    "files_read": True,
    "code_search": True,
    "files_write": True,
    "shell": True,
    "web": True,
    "git_read": False,
    "git_write": False,
    "skill": False,
    "delegate": False,
    # 看图默认开：它是只读能力，真正的闸门是"这条连接能不能收图"（`llm.supports_vision`，
    # 见 `enabled_capabilities`），不是让用户再去开一个开关。
    "image_read": True,
    "terminal": True,
    "page_control": True,
    # MCP 默认**关**：连的是第三方进程，且配置里可能带凭据。"打开"应当是显式决定。
    "mcp": False,
}

#: **不可关闭**的能力（架构 §16.1）。两类理由必须分开说：
#:
#: - **硬依赖**：``files_read`` 是 ``chat`` 模式的固定绑定（v0.1 起的既有行为）；``code_search``
#:   与它一起构成两类工作流的"缺任一即失败"前置（§18.2、§18.3）。
#: - **常态化**（2026-10-14）：``web``——联网是本机 Agent 的常态能力，日常问一句就该去查，而不是
#:   只拿训练数据答。它因此**不进可开关集**：页面把它画成"始终开启"，``config.yaml`` 的
#:   ``web_access.enabled`` 也不再能把它关掉（否则会出现"写了 false 却没生效"的静默不一致）。
#:
#: 其余能力（含 ``files_write`` / ``shell`` / ``git_write``）都可以关——"只读会话"就靠关掉它们表达，
#: 这是档位退出运行期绑定后仍需保留的表达力。
LOCKED_CAPABILITIES: tuple[str, ...] = ("task_lifecycle", "files_read", "code_search", "web")

#: ``chat`` 模式固定绑定的能力（开关不作用于它）。
#:
#: 2026-10-14 加上 ``web``：闲聊里问"最近/某个网页上"的事，模型也该能去查——闲聊与 Agent 的差别是
#: "要不要动手改东西"，不是"能不能查资料"。**代价**（架构 §6.7 记过同类）：能力集合的字节序列进
#: 系统前缀，这一改会让供应商前缀缓存**整段失效一次**，之后稳定。
CHAT_CAPABILITIES: tuple[str, ...] = ("files_read", "web")

#: 运行模式 → 能力集合（``tool`` 模式由开关表决定，见 :func:`resolve_switches`）。
MODE_CAPABILITIES: Mapping[str, tuple[str, ...]] = {
    "chat": CHAT_CAPABILITIES,
    "tool": PROFILES[DEFAULT_PROFILE],
}

#: 已知运行模式（固定顺序）。
KNOWN_MODES: tuple[str, ...] = tuple(MODE_CAPABILITIES)


def profile_names() -> tuple[str, ...]:
    """内置档位名（固定顺序，配置错误提示按这个顺序列出）。"""
    return tuple(PROFILES)


def selectable_profiles(custom_names: Iterable[str] = ()) -> tuple[str, ...]:
    """**页面可选**的全部档位：内置三档（固定顺序）+ 自定义名字（按名排序，输出稳定）。

    2026-10-13 起页面不再有档位选择器，这个枚举只用于配置校验的提示文本与旧接口的兼容；
    新代码不要再用它渲染界面。
    """
    extra = sorted({str(name) for name in custom_names} - set(PROFILES))
    return (*PROFILES, *extra)


def capability_names() -> tuple[str, ...]:
    """能力闭集的名字（固定顺序）。"""
    return tuple(CAPABILITY_TOOLS)


def switchable_capabilities() -> tuple[str, ...]:
    """**页面上可以开关**的能力（固定顺序，= 闭集减去不可关闭的那两个）。"""
    locked = set(LOCKED_CAPABILITIES)
    return tuple(name for name in CAPABILITY_TOOLS if name not in locked)


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


def unknown_switch_names(switches: Mapping[str, object]) -> list[str]:
    """开关表里未登记的能力名（保持输入顺序）。配置加载期与设置库读取共用这一道。"""
    return [name for name in switches if name not in CAPABILITY_TOOLS]


def non_boolean_switches(switches: Mapping[str, object]) -> list[str]:
    """开关表里取值不是布尔的能力名（保持输入顺序）。

    ``true`` / ``false`` 之外的写法（``yes``、``1``、``"on"``）在 YAML / JSON 里是常见错误，而它们
    **不会**报错地变成某个默认值——那正是"看起来改了、实际没生效"的来源，所以在解析时就报出来。
    """
    return [name for name, value in switches.items() if not isinstance(value, bool)]


def as_switch_layer(capabilities: Iterable[str]) -> dict[str, bool]:
    """把一份能力名列表转成开关层（**只有列出的能力为真**）。

    档位（``PROFILES[active]``）、评测脚本与 MCP 之类的外部入口都按"能力名列表"给输入，用这一层
    把它们接进统一的开关解析，避免出现第二条解析路径。
    """
    wanted = set(capabilities)
    return {name: name in wanted for name in CAPABILITY_TOOLS}


def resolve_switches(*layers: Mapping[str, bool]) -> dict[str, bool]:
    """把多层覆盖叠在**出厂默认**上，返回完整的开关表（架构 §16.1）。

    Args:
        *layers: 从低到高的覆盖层，依次是"档位解析出的出厂默认"（``config.profiles.active``）与
            设置库里的用户选择。未知能力名被丢弃（配置加载期与设置库读取已各拦一次，这里是最后一道
            失败闭合），非布尔值按 ``bool()`` 收敛，不让脏输入扩散到绑定层。

    Returns:
        闭集里**每个**能力一行（含不可关闭的那两个），顺序与 :data:`CAPABILITY_TOOLS` 一致。

    不可关闭的能力在**最后**强制置真：即使档位或设置库里写了 ``files_read: false``，也不生效——
    把它交给用户是让"关掉之后基础行为不变"这条硬验收无法成立。
    """
    resolved = dict(CAPABILITY_DEFAULTS)
    for layer in layers:
        for name, value in layer.items():
            if name in CAPABILITY_TOOLS:
                resolved[name] = bool(value)
    for name in LOCKED_CAPABILITIES:
        resolved[name] = True
    return resolved


def enabled_capabilities(
    switches: Mapping[str, bool], *, git_available: bool, vision_available: bool,
    browser_available: bool = True, mcp_available: bool = True,
) -> tuple[str, ...]:
    """按开关表与**运行环境**得到本次运行的能力集合（架构 §16.3、§16.4）。

    关掉的能力对模型**完全不可见**：开关为 false 时该能力被移除——于是提示词不会提到它、
    工具也不会绑给模型。"关闭新增能力后基础行为不变"靠的就是这一条。

    ``git_available`` 由调用方传入（本机是否存在 git 可执行文件，见
    :func:`~local_agent.tools.git_tools.git_availability`）：**开关是开的但本机没有 git** 时
    ``git_read`` / ``git_write`` 同样被移除。两台机器上"看起来配置一样却行为不同"的根因就藏在这里，
    所以这个参数没有默认值。

    ``vision_available`` 同理且**也必须显式给**（2026-10-16）：它回答"这条连接能不能收图像输入"
    （``llm.supports_vision`` 声明）。为假时 ``image_read`` 被移除——工具因此**不会绑给模型**，
    提示里也不会承诺这件事（"提示不能承诺本档做不到的事"）。

    ``mcp_available``（2026-10-20）：这条运行路径**有没有接 MCP 基础设施**（连接管理器）。
    CLI、委派子图与多数测试路径没有，此时即使开关写着真，``mcp`` 也不进能力集——
    否则模型会看到一个搜不到任何东西的 ``mcp_tool_search``。
    """
    return tuple(
        capability
        for capability in CAPABILITY_TOOLS
        if switches.get(capability, False)
        and (git_available or capability not in {"git_read", "git_write"})
        and (vision_available or capability != "image_read")
        and (browser_available or capability != "page_control")
        and (mcp_available or capability != "mcp")
        and (switches.get("shell", False) or capability != "terminal")
    )


def effective_capabilities(
    capabilities: Sequence[str], *, web_access_enabled: bool, git_enabled: bool, skills_enabled: bool
) -> tuple[str, ...]:
    """按配置开关与运行环境收窄能力集合（架构 §16.3、§16.4）。

    **评测与脚本入口**：它按"能力名列表 + 三个配置开关"给答案，语义与
    :func:`enabled_capabilities` 一致，只是把三个开关拆成了参数（离线跑器要固定这三项以便复现）。
    运行期绑定走 :func:`resolve_switches` + :func:`enabled_capabilities`，不接受第二套开关来源。

    ``git_enabled`` 由调用方把两件事合起来：配置开关 **且** `git` 可用（见 `tools.git_tools.git_availability`）。
    """
    switch_layer = dict(as_switch_layer(capabilities))
    switch_layer.update(
        {
            "web": web_access_enabled,
            "git_read": git_enabled,
            "git_write": git_enabled,
            "skill": skills_enabled,
        }
    )
    # 评测入口固定按"环境都可用"算（与 `git_available=True` 同一口径）：这里要的是**可复现的
    # 能力集合**，不是"这台机器能不能收图"。真机上的收窄由 `build_runtime_context` 传真实值。
    return enabled_capabilities(
        resolve_switches(switch_layer), git_available=True, vision_available=True
    )


def resolve_capabilities(
    profile: str, custom: Mapping[str, Sequence[str]] | None = None
) -> tuple[str, ...]:
    """把档位名解析成能力集合（**仅评测与出厂默认**；运行期绑定不再用它）。

    Args:
        profile: 档位名（内置名，或 ``custom`` 里定义的名字）。
        custom: 自定义档位（名字 → 能力列表）；内置名优先，不允许被覆盖。

    Returns:
        能力集合（顺序即配置里写的顺序，故同一份配置结果稳定）。

    Raises:
        KeyError: 名字既不是内置档位，也不在 ``custom`` 里。
    """
    if profile in PROFILES:
        return PROFILES[profile]
    if custom and profile in custom:
        return tuple(custom[profile])
    raise KeyError(profile)


#: 能力的中文名（页面展示用）。**页面只展示这个，不展示原始工具名**（§16.6、§15.8 的字段门槛）：
#: 用户关心的是"这台机器现在能不能联网/能不能提交"，不是内部绑定了哪几个工具。
CAPABILITY_LABELS: Mapping[str, str] = {
    "task_lifecycle": "任务继续与结束",
    "plan": "任务计划",
    "files_read": "读取工作区文件",
    "code_search": "检索代码",
    "files_write": "修改工作区文件",
    "shell": "执行命令",
    "web": "联网检索与抓取",
    "git_read": "查看 Git 状态与差异",
    "git_write": "提交到 Git",
    "skill": "按需加载技能",
    "delegate": "自主委派调查",
    # 2026-10-16：看图（`view_image`）。标签说"看图片"而不是"读取图片"——与 `files_read` 的区别
    # 要在页面上看得出来（一个是读文本，一个是把图交给模型）。
    "image_read": "看图片",
    "terminal": "交互进程与后台服务",
    "page_control": "操作浏览器",
    "mcp": "MCP 外部工具",
}

#: 能力不可用的原因类型。**只有这三种**：DTO 直接复用它，页面因此不会遇到"没见过的原因"。
#: 定在这里（而不是各层各写一份字面量）是为了让"页面解释"与"后端判定"共用同一个闭集。
#:
#: 2026-10-13：档位退出运行期绑定后 ``profile`` 这一种原因随之删除——它描述的"换个档位再试"在页面上
#: 已经没有对应动作，留在闭集里只会让页面渲染出一句做不到的建议。
CapabilityReason = Literal["available", "disabled", "missing_dependency"]

#: 能力已开且环境具备。
REASON_AVAILABLE: CapabilityReason = "available"
#: 开关关着（用户可自行打开）。
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
    #: 不可关闭的能力（§16.1）：页面据此把开关画成"始终开启"，而不是给一个点不动的控件。
    locked: bool = False


#: 关掉某个能力时页面要说的话（**只讲能力，不讲工具**，§16.6）。
_DISABLED_DETAIL: Mapping[str, str] = {
    "plan": "任务计划开关关着：本轮不会要求模型先给计划，复杂任务的过程不再可见。",
    "files_write": "修改工作区文件开关关着：Agent 只能读，不能改本机文件（只读会话就靠这一项）。",
    "shell": "执行命令开关关着：Agent 不能运行任何本机命令。",
    "git_read": "Git 读取开关关着：默认不让 Agent 读仓库历史与差异。",
    "git_write": "Git 提交开关关着：默认不让 Agent 改仓库状态。",
    "skill": "技能开关关着：默认不让 Agent 读取本机已有的技能说明"
    "（清单会占提示预算，内容也不一定是为你这个项目写的）。",
    # §18.7：这一项与上面几项的性质不同——上面是"给不给权限"，它是"允不允许模型自己决定花额外的
    # 模型调用"。措辞必须让用户看懂这层：默认关，打开就是授权它自主花钱。
    "delegate": "自主委派开关关着：模型只能自己一步一步查，不能把调查派给隔离子 Agent"
    "（每个子任务会多花一次模型调用，所以默认关闭）。",
    "mcp": "MCP 外部工具开关关着：不连接任何 MCP Server，模型看不到也调不到外部工具"
    "（这些工具跑在本机的第三方进程里，且可能带凭据，所以默认关闭）。",
}

#: 打开开关的下一步（给用户的可照做文本）。**必须说"不用重启"**：这正是本次改动的目的。
_ENABLE_HINT = "在「能力」面板里打开这一项（保存后下一轮生效，不用重启服务）。"

#: 不可关闭的能力在页面上怎么解释。**两类理由分开**（见 :data:`LOCKED_CAPABILITIES`）：
#: 硬依赖（`files_read` / `code_search`）与常态化（`web`）。
_LOCKED_DETAIL: Mapping[str, str] = {
    "web": "常态能力，始终开启：联网检索与抓取是日常问答的一部分——"
    "关掉它，模型只能凭训练数据回答。",
}
_LOCKED_DETAIL_DEFAULT = "基础能力，始终开启（关掉它会让闲聊与只读调查都无解）。"


def capability_status(
    switches: Mapping[str, bool],
    *,
    git_available: bool,
    git_hint: str | None = None,
    browser_available: bool = True,
    browser_hint: str | None = None,
) -> tuple[CapabilityStatus, ...]:
    """把开关表展开成**可解释的状态列表**（§16.6）。

    对闭集里的**每个**能力都给一行——不可用的也要在，并且必须带上"为什么 + 怎么办"：
    "只灰掉不解释"会让用户以为功能坏了，而多数情况只是开关的问题。

    Args:
        switches: 已解析的完整开关表（:func:`resolve_switches` 的输出）。
        git_available: 本机是否存在 git 可执行文件（`tools.git_tools.git_availability`）。
        git_hint: 缺 git 时给用户的可照做提示；缺它时退化为一句通用说明，不假装"稍后可用"。

    Returns:
        按 :data:`CAPABILITY_TOOLS` 顺序排列的状态行（顺序稳定，页面不会跳来跳去）。
    """
    rows: list[CapabilityStatus] = []
    for capability in CAPABILITY_TOOLS:
        label = CAPABILITY_LABELS.get(capability, capability)
        if capability in LOCKED_CAPABILITIES:
            rows.append(
                CapabilityStatus(
                    capability=capability,
                    label=label,
                    available=True,
                    reason=REASON_AVAILABLE,
                    detail=_LOCKED_DETAIL.get(capability, _LOCKED_DETAIL_DEFAULT),
                    locked=True,
                )
            )
            continue
        if not switches.get(capability, False):
            rows.append(
                CapabilityStatus(
                    capability=capability,
                    label=label,
                    available=False,
                    reason=REASON_DISABLED,
                    detail=_DISABLED_DETAIL.get(capability, "这一项没有打开。"),
                    hint=_ENABLE_HINT,
                )
            )
            continue
        if capability == "terminal" and not switches.get("shell", False):
            rows.append(CapabilityStatus(
                capability=capability, label=label, available=False,
                reason=REASON_DISABLED, detail="执行命令已关闭，交互进程也不会绑定给模型。",
                hint="先开启「执行命令」，再开启交互进程。",
            ))
            continue
        if capability == "page_control" and not browser_available:
            rows.append(CapabilityStatus(
                capability=capability, label=label, available=False,
                reason=REASON_MISSING_DEPENDENCY, detail="本机浏览器或连接依赖不可用。",
                hint=browser_hint or "安装 Edge、Chrome 或 Chromium，并运行 uv sync。",
            ))
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


def is_visible(
    mode: str,
    capabilities: Sequence[str] | None,
    name: str,
    *,
    cancelled: bool = False,
    external_names: Collection[str] = (),
) -> bool:
    """``name`` 是否可见（=会被发给模型 / 可以被执行）。

    **可见性是执行侧的前置条件**（架构 §6.9、§16.1）：安全门与执行节点都必须用它做拦截，
    否则"没绑定给模型"就只是"没告诉它"，模型自己报出名字照样能跑——2026-10-08 的真实基线
    实证过一次：只读档位照提示词里的 ``write_file`` 说明发起调用，文件真的被创建了。

    ``cancelled=True`` 时**一律不可见**（架构 §17.4）：取消就是"这一轮没有任何可用能力"，
    判定写在这里而不是各节点各写一遍——"取消后不再启动新的工具调用"必须挂在**同一道门**上。

    ``external_names`` 是**本轮已知的 MCP 工具名**（来自已连接 server 的目录）。它们不在静态表里，
    因此必须由调用方显式给出；不给就一律不可见——"忘了传"应当表现为"用不了"，不是"全放行"。

    注意这里比"绑定给模型"**宽**：``Deferred`` 档的工具不绑进初始工具表（省提示预算），但模型
    经检索看到它之后必须能调用。绑定范围由 ``ToolRegistry.tools_for`` 控制，可见性管的是边界。
    """
    if cancelled:
        return False
    if name in tool_names_for(mode, capabilities):
        return True
    if not name.startswith(EXTERNAL_TOOL_PREFIX):
        return False
    if mode == "chat":
        # 闲聊固定绑定只读子集，不含 mcp（与 tool_names_for 的 chat 分支同源）。
        return False
    active: Sequence[str] = (
        MODE_CAPABILITIES["tool"] if capabilities is None else capabilities
    )
    return "mcp" in active and name in external_names


def tool_names_for(mode: str, capabilities: Sequence[str] | None = None) -> tuple[str, ...]:
    """该模式下发模型的工具名序列（**顺序即绑定顺序**）。

    Args:
        mode: ``chat`` 或 ``tool``；``chat`` 忽略 ``capabilities``，固定绑定 ``files_read``。
        capabilities: ``tool`` 模式下的能力集合（来自开关表）；``None`` 表示按出厂默认解析。

    Returns:
        工具名元组。

    Raises:
        KeyError: 模式或能力名未登记（配置校验与注册表构造已各自拦过，这里是最后一道）。
    """
    if mode == "chat":
        resolved: Sequence[str] = MODE_CAPABILITIES["chat"]
    elif mode == "tool":
        # ``None`` = **默认基线能力集**（= ``coder``：v0.4 九工具 + 常态化的联网，再追加 git / skill）。
        # 注意它**不是**出厂默认开关表：运行期永远显式传开关表解析出的能力集，``None`` 只出现在
        # "没有用户选择"的调用点（提示词草稿、工具集自检），那里要的是既有行为。
        resolved = MODE_CAPABILITIES["tool"] if capabilities is None else capabilities
    else:
        raise KeyError(f"未知运行模式：{mode}")
    names: list[str] = []
    for capability in resolved:
        names.extend(CAPABILITY_TOOLS[capability])
    return tuple(names)

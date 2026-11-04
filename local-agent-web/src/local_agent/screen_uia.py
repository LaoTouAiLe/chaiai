"""读 UI 树（2026-11-04）：把前台窗口的控件结构压成一段**有界**文本。

为什么要有它：截图能"看得见"，但拿不到"这个按钮叫什么、在什么位置、能不能点"。UI 树补上这部分，
也是将来做点击的地基（点击必须先有稳定的目标与坐标）。

## 为什么要有上限

2026-11-04 在本机实测：13 个顶层窗口，任务栏那一棵就有 5 个子树、Chrome 与编辑器各有 2 层以上；
真实应用（浏览器、IDE）的树动辄上千个节点。整棵塞进上下文会直接吃掉预算，而且对模型没有增量
信息——它需要的是"有哪些可交互的东西"，不是每一个布局容器。所以默认限深 4 层、最多 300 个节点、
单行 120 字符、整段 8000 字符，超了就**如实标注截断**。

## 三条边界

1. **只读**：只枚举控件属性，不点击、不聚焦、不发送任何输入；
2. **逐节点容错**：某个控件取属性失败就跳过并计数，不让一棵坏子树毁掉整次读取；
3. **空树要说话**：有些应用（Qt 未开无障碍、自绘界面）根本不暴露 UIA 树，此时明确说
   "这棵树是空的，请改用截图"——不能让模型把"没控件"读成"窗口里什么都没有"。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

#: 默认限深。再深基本是布局容器，对"能操作什么"没有帮助。
MAX_DEPTH: int = 4

#: 默认节点上限。300 约等于一个复杂对话框的规模。
MAX_NODES: int = 300

#: 单个控件名的字符上限。
MAX_NAME_CHARS: int = 120

#: 整段文本的字符上限（给模型的那一段）。
MAX_TEXT_CHARS: int = 8000


@dataclass(frozen=True)
class UiNode:
    """一个控件。``role`` 是 UIA 的控件类型名（Button / Edit / Text …）。"""

    depth: int
    role: str
    name: str
    left: int
    top: int
    width: int
    height: int
    enabled: bool


@dataclass(frozen=True)
class UiTree:
    """一次读树的完整结果。"""

    window_role: str
    window_title: str
    nodes: tuple[UiNode, ...]
    truncated: bool
    skipped: int
    elapsed_ms: int
    #: 可交互元素（带编号）。模型说"点 3 号"就靠它，不必自己算坐标。
    targets: tuple[Target, ...] = ()
    #: 读到的那个窗口的句柄；编号只在**同一窗口**内有效。
    window_handle: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.nodes


#: 可交互的控件类型。只收"能点/能输入"的那几类，容器（Pane/ToolBar/Group）不算——
#: 把它们也编号只会让模型点到没有反应的空白区域上。
INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "ButtonControl",
    "CheckBoxControl",
    "ComboBoxControl",
    "EditControl",
    "HyperlinkControl",
    "ListItemControl",
    "MenuItemControl",
    "RadioButtonControl",
    "SliderControl",
    "SplitButtonControl",
    "TabItemControl",
    "TreeItemControl",
})

#: 一次最多编号多少个元素。再多模型也记不住，而且编号会盖满整张图。
MAX_TARGETS: int = 40

#: 标题栏的系统按钮：编号时**排除**。
#:
#: 2026-11-04 真机踩到：记事本的控件树里 `最小化/最大化/关闭` 排在编辑区之前，
#: 于是"#1"是最小化——模型随手点 #1 会把窗口最小化，后面的输入就落到别的窗口上。
#: 这类按钮不该占用"要操作的东西"的编号。
_DECORATION_NAMES: frozenset[str] = frozenset({
    "最小化", "最大化", "还原", "关闭", "关闭窗口",
    "minimize", "maximize", "restore", "close",
})


@dataclass(frozen=True)
class Target:
    """一个可点击元素：编号 + 类型 + 名字 + 中心点。"""

    index: int
    role: str
    name: str
    left: int
    top: int
    width: int
    height: int
    depth: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)


def targets_from(nodes: tuple[UiNode, ...] | list[UiNode]) -> tuple[Target, ...]:
    """从已读到的节点里挑出可交互元素并编号。

    编号顺序按"从上到下、从左到右"排，与人在界面上扫视的顺序一致；不按树的深度优先，
    否则一个深层按钮会排在顶部导航栏之前，模型找号会绕。
    """
    candidates = [
        node for node in nodes
        if node.role in INTERACTIVE_ROLES
        and node.width > 0
        and node.height > 0
        and node.enabled
        and node.name.strip().lower() not in _DECORATION_NAMES
    ]
    candidates.sort(key=lambda node: (node.top // 8, node.left))
    return tuple(
        Target(
            index=index + 1,
            role=node.role,
            name=node.name,
            left=node.left,
            top=node.top,
            width=node.width,
            height=node.height,
            depth=node.depth,
        )
        for index, node in enumerate(candidates[:MAX_TARGETS])
    )


def available() -> bool:
    """当前环境能不能读 UI 树（Windows + uiautomation 可导入）。"""
    import os

    if os.name != "nt":
        return False
    try:
        import uiautomation  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺包就是不可用，不是崩溃
        return False
    # UIA 与模拟点击必须共用同一套坐标：这里顺手把进程标成 DPI 感知（幂等）。
    from local_agent.screen_capture import ensure_dpi_aware

    ensure_dpi_aware()
    return True


def clean(text: Any, *, limit: int = MAX_NAME_CHARS) -> str:
    """压成一行：去掉换行与控制/格式字符，折叠空白，再截断。

    必要性来自实测：控件名里会出现 U+200E 这类不可见方向标记，直接打印会打爆 GBK 控制台，
    进了提示词也只是噪声。
    """
    if text is None:
        return ""
    raw = str(text)
    chars = []
    for char in raw:
        if char in "\r\n\t":
            chars.append(" ")
            continue
        if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}:
            continue
        chars.append(char)
    collapsed = " ".join("".join(chars).split())
    return collapsed[:limit]


def _rect(control: Any) -> tuple[int, int, int, int]:
    """取边界矩形；取不到返回全零（离屏控件本来也没有可见区域）。"""
    try:
        rect = control.BoundingRectangle
    except Exception:  # noqa: BLE001 - 元素可能已失效
        return (0, 0, 0, 0)
    if not rect:
        return (0, 0, 0, 0)
    try:
        return (int(rect.left), int(rect.top), int(rect.width()), int(rect.height()))
    except Exception:  # noqa: BLE001
        return (0, 0, 0, 0)


def _enabled(control: Any) -> bool:
    try:
        return bool(control.IsEnabled)
    except Exception:  # noqa: BLE001
        return True


def walk(
    root: Any,
    *,
    max_depth: int = MAX_DEPTH,
    max_nodes: int = MAX_NODES,
) -> tuple[list[UiNode], bool, int]:
    """深度优先收集控件。返回 ``(节点, 是否截断, 跳过的坏子树数)``。

    每个节点只取一次矩形：`BoundingRectangle` 是 COM 调用，一个节点上取四遍会让整次读取慢一倍。
    """
    nodes: list[UiNode] = []
    skipped = 0
    truncated = False
    stack: list[tuple[Any, int]] = [(root, 0)]
    while stack:
        control, depth = stack.pop()
        if len(nodes) >= max_nodes:
            truncated = True
            break
        left, top, width, height = _rect(control)
        nodes.append(
            UiNode(
                depth=depth,
                role=clean(getattr(control, "ControlTypeName", "")),
                name=clean(getattr(control, "Name", "")),
                left=left,
                top=top,
                width=width,
                height=height,
                enabled=_enabled(control),
            )
        )
        if depth >= max_depth:
            continue
        try:
            children = list(control.GetChildren() or [])
        except Exception:  # noqa: BLE001 - 坏子树跳过，不让一棵树毁掉整次读取
            skipped += 1
            continue
        for child in reversed(children):
            stack.append((child, depth + 1))
    if len(nodes) >= max_nodes:
        truncated = True
    return nodes, truncated, skipped


def _tree_of(window: Any, *, max_depth: int, max_nodes: int) -> UiTree:
    import time

    started = time.monotonic()
    nodes, truncated, skipped = walk(window, max_depth=max_depth, max_nodes=max_nodes)
    return UiTree(
        window_role=clean(getattr(window, "ControlTypeName", "")),
        window_title=clean(getattr(window, "Name", ""), limit=200),
        nodes=tuple(nodes),
        truncated=truncated,
        skipped=skipped,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        targets=targets_from(nodes),
        window_handle=int(getattr(window, "NativeWindowHandle", 0) or 0),
    )


def read_foreground(*, max_depth: int = MAX_DEPTH, max_nodes: int = MAX_NODES) -> UiTree:
    """读**当前前台窗口**的 UI 树。"""
    import uiautomation  # 局部导入：只有真用这个能力时才付导入代价

    window = uiautomation.GetForegroundControl()
    if window is None:
        raise RuntimeError("当前没有前台窗口")
    return _tree_of(window, max_depth=max_depth, max_nodes=max_nodes)


def read_window(
    handle: int, *, max_depth: int = MAX_DEPTH, max_nodes: int = MAX_NODES
) -> UiTree:
    """读**指定句柄**的窗口，不要求它在前台。

    2026-11-04 实测：记事本在后台时，UIA 照样能读出完整控件树与文本内容。所以"先看一眼
    后台那个窗口有什么"不需要抢用户的焦点。
    """
    import uiautomation

    window = uiautomation.ControlFromHandle(handle)
    if window is None:
        raise RuntimeError("取不到这个窗口（可能已经关闭）")
    return _tree_of(window, max_depth=max_depth, max_nodes=max_nodes)


def render_targets(targets: tuple[Target, ...]) -> str:
    """把编号列表压成一段文本；没有可交互元素时明说，别让模型以为"界面是空的"。"""
    if not targets:
        return "可点击元素：无（这个界面没有暴露可交互控件，可以改用截图看画面）。"
    lines = ["可点击元素（用编号点击，不必自己算坐标）："]
    for target in targets:
        center = target.center
        lines.append(
            f"#{target.index} {target.role} 「{target.name}」 中心({center[0]},{center[1]})"
        )
    return "\n".join(lines)


def render(tree: UiTree, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """把树压成缩进文本；超预算时截断并**在末尾说明**截了什么。"""
    lines = [f"窗口：{tree.window_role} 「{tree.window_title}」（{len(tree.nodes)} 个控件）"]
    used = len(lines[0])
    cut = False
    for node in tree.nodes:
        box = ""
        if node.width > 0 and node.height > 0:
            box = f" [{node.left},{node.top} {node.width}x{node.height}]"
        flag = "" if node.enabled else " (禁用)"
        line = f"{'  ' * node.depth}{node.role or '?'} 「{node.name}」{box}{flag}"
        if used + len(line) + 1 > max_chars:
            cut = True
            break
        lines.append(line)
        used += len(line) + 1
    if tree.truncated or cut:
        lines.append("…（已截断：控件数超过上限，需要更细的局部请改用截图或缩小目标窗口）")
    if tree.skipped:
        lines.append(f"（另有 {tree.skipped} 处子控件读取失败，已跳过）")
    if tree.is_empty:
        lines.append("（这棵树是空的：该应用没有暴露 UI 控件。请改用截图看画面。）")
    return "\n".join(lines)

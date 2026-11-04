"""模拟点击与键盘输入（2026-11-04）：把"看得到"变成"动得了"。

## 为什么动作这么少

只做三个原子动作：单击、输入文本、按组合键。不做拖拽、滚轮、多段宏。理由不是偷懒——每个动作都要
过审批、都要能被急停打断、都要能在失败时收尾；动作越多，收尾面越大。等这三个真跑顺了再扩。

## 三条硬边界（对应 §18.10 的契约）

1. **目标必须是前台窗口**：动作执行前重新取一次前台窗口，与授权时记下的句柄不一致就**直接拒绝**，
   不"顺手点到别的窗口上"。窗口切换后需要重新授权。
2. **坐标必须落在窗口矩形内**：越界一律拒绝。屏幕坐标在缩放/多屏下最容易出错，这条挡住
   "点歪了点到别的程序"。
3. **不留粘滞键**：组合键一律"按下→按相反顺序抬起"；任何异常路径都走 :func:`release_modifiers`，
   把 Shift/Ctrl/Alt/Win 强制抬起——2026-11-04 查 hermes 时确认它**没有**这段收尾代码，
   那边中断后按键可能一直按着。

真实实现在 Windows + `uiautomation` 上；测试注入替身，绝不真点。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from local_agent.screen_uia import clean

#: 单次输入的字符上限。整段粘贴走 shell 或文件，不走模拟键盘。
MAX_TEXT_CHARS: int = 2000

#: 一轮内允许的屏幕动作上限。到顶就拒绝，要求模型重新评估——防止自循环点个不停。
MAX_ACTIONS_PER_TURN: int = 20

#: 支持的修饰键写法 → uiautomation 的按键名。
_MODIFIERS: dict[str, str] = {
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "shift": "Shift",
    "alt": "Alt",
    "win": "Win",
    "cmd": "Win",
}

#: 单个按键的常用别名（只收有把握的，其余交给 uiautomation 原样传）。
_KEYS: dict[str, str] = {
    "enter": "Enter",
    "esc": "Esc",
    "escape": "Esc",
    "tab": "Tab",
    "space": "Space",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
}


@dataclass(frozen=True)
class WindowRef:
    """一个顶层窗口的句柄、标题与矩形。

    ``minimized`` 为真时，矩形是"恢复后应该在的位置"（``GetWindowPlacement`` 的
    ``rcNormalPosition``）——最小化窗口没有可见矩形，但用户想"切到它"是常见诉求，
    直接跳过会让工具报"没找到"。
    """

    handle: int
    title: str
    left: int
    top: int
    right: int
    bottom: int
    minimized: bool = False

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


class _WindowPlacement(ctypes.Structure):
    """``WINDOWPLACEMENT``：最小化窗口只能从这里拿到"恢复后的矩形"。"""

    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def available() -> bool:
    import os

    if os.name != "nt":
        return False
    try:
        import uiautomation  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺包即不可用
        return False
    # 与读树/截屏共用同一套坐标（见 screen_capture.ensure_dpi_aware）。
    from local_agent.screen_capture import ensure_dpi_aware

    ensure_dpi_aware()
    return True


def foreground() -> WindowRef | None:
    """取当前前台窗口；没有就返回 ``None``（调用方据此拒绝动作）。"""
    if not available():
        return None
    import uiautomation as auto

    window = auto.GetForegroundControl()
    if window is None:
        return None
    handle = int(getattr(window, "NativeWindowHandle", 0) or 0)
    if handle == 0:
        # 非顶层窗口（或 UIA 没给出句柄）时退回 Win32 的 GetForegroundWindow。
        handle = int(ctypes.windll.user32.GetForegroundWindow())
    rect = window.BoundingRectangle
    if not rect:
        return None
    return WindowRef(
        handle=handle,
        # 标题里实测带 U+200B 这类零宽字符：清洗后再给模型，否则会污染提示词与控制台。
        title=clean(str(window.Name or ""), limit=200),
        left=int(rect.left),
        top=int(rect.top),
        right=int(rect.right),
        bottom=int(rect.bottom),
    )


def click(x: int, y: int, *, clicks: int = 1) -> None:
    """在屏幕坐标上单击。按钮固定左键——右键菜单属于另一类语义，暂不开。"""
    import uiautomation as auto

    auto.Click(x, y, waitTime=0.0)
    for _ in range(max(0, clicks - 1)):
        auto.Click(x, y, waitTime=0.0)


def type_text(text: str) -> None:
    """按字符输入（``charMode`` 走 Unicode 注入，中文也能打）。"""
    import uiautomation as auto

    auto.SendKeys(text, charMode=True, interval=0.01, waitTime=0.0)


def press_keys(combo: str) -> None:
    """按一组组合键，例如 ``ctrl+s`` / ``alt+tab`` / ``enter``。"""
    import uiautomation as auto

    auto.SendKeys(_to_uia(combo), waitTime=0.0)


def _to_uia(combo: str) -> str:
    """``"ctrl+shift+t"`` → ``"{Ctrl}{Shift}t"``。空输入或未知键直接交给 uiautomation。"""
    parts = [part.strip().lower() for part in combo.replace(" ", "").split("+") if part.strip()]
    if not parts:
        return ""
    modifiers = [part for part in parts if part in _MODIFIERS]
    keys = [part for part in parts if part not in _MODIFIERS]
    prefix = "".join(f"{{{_MODIFIERS[item]}}}" for item in modifiers)
    tail = "".join(_KEYS.get(item, item) for item in keys)
    return prefix + tail


def find_windows(pattern: str) -> list[WindowRef]:
    """按标题子串找**可见顶层窗口**（不区分大小写）。

    返回列表而不是单个：同名窗口（多个浏览器窗口、多个编辑器）很常见，由调用方决定
    "唯一才聚焦"还是"让用户挑"。这里不做模糊猜测。
    """
    if not available():
        return []
    import uiautomation as auto

    needle = pattern.strip().lower()
    if not needle:
        return []
    found: list[WindowRef] = []
    for control in auto.GetRootControl().GetChildren():
        window = _window_of(control)
        if window is not None and needle in window.title.lower():
            found.append(window)
    return found


def _window_of(control: Any) -> WindowRef | None:
    """把一个顶层控件翻译成窗口引用；失效/不可见/无句柄一律当作不存在。"""
    try:
        handle = int(getattr(control, "NativeWindowHandle", 0) or 0)
        title = clean(str(control.Name or ""), limit=200)
    except Exception:  # noqa: BLE001 - 失效窗口：当作不存在，不中断整次枚举
        return None
    if handle == 0:
        return None
    return window_ref(handle, title=title)


def window_ref(handle: int, *, title: str = "") -> WindowRef | None:
    """用 Win32 取窗口矩形；不可见或没有有效矩形时返回 ``None``。

    为什么不用 UIA 的 ``BoundingRectangle``：**最小化窗口的那个属性是空的**，于是"切到某个
    最小化窗口"会变成"没找到"。这里最小化时改取 ``GetWindowPlacement`` 的
    ``rcNormalPosition``——那是它恢复后会出现的位置，正好是点击坐标要用的。
    """
    if not available():
        return None
    user32 = ctypes.windll.user32
    if not user32.IsWindowVisible(handle):
        return None
    minimized = bool(user32.IsIconic(handle))
    if minimized:
        placement = _WindowPlacement()
        placement.length = ctypes.sizeof(_WindowPlacement)
        if not user32.GetWindowPlacement(handle, ctypes.byref(placement)):
            return None
        normal = placement.rcNormalPosition
        left, top, right, bottom = normal.left, normal.top, normal.right, normal.bottom
    else:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    if right <= left or bottom <= top:
        return None
    return WindowRef(
        handle=handle,
        title=title,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        minimized=minimized,
    )


def focus(window: WindowRef) -> WindowRef | None:
    """把窗口切到前台；成功返回**恢复后的窗口引用**，失败返回 ``None``。

    三段式，逐级降级（2026-11-04 逐条实测过）：

    1. ``AllowSetForegroundWindow(ASFW_ANY)`` + ``ShowWindow`` + ``BringWindowToTop``
       + ``SetForegroundWindow``：**后台进程基本无效**，系统静默忽略。
    2. ``AttachThreadInput`` 接上前台线程再设一次：文档里的正规做法，**实测仍无效**。
    3. 注入一次 Alt 按下/抬起再设：**这一步才真正抢到**（两个记事本互抢的实测）。

    第 3 段的代价必须说清：它往当前前台窗口发一个真实的 Alt 事件，个别情况下会带出菜单或
    任务切换器；所以放在最后、用 ``try/finally`` 保证抬起，且整个动作在用户逐次批准之后才执行。
    不用第 3 段的话，"后台 agent 操作前台程序"在 Windows 上等于做不到。
    """
    if not available():
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    handle = window.handle
    # ASFW_ANY（-1 按 DWORD 解释）：允许本进程把前台让给任意窗口。缺这一句，
    # 后面两条路都会被前台锁静默忽略——2026-11-04 实测。
    user32.AllowSetForegroundWindow(-1)
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    user32.BringWindowToTop(handle)
    if _wait_foreground(handle, user32, kernel32):
        return window_ref(handle, title=window.title) or window
    previous = int(user32.GetForegroundWindow() or 0)
    previous_thread = int(user32.GetWindowThreadProcessId(previous, None) or 0)
    current_thread = int(kernel32.GetCurrentThreadId())
    attached = False
    try:
        attached = bool(user32.AttachThreadInput(current_thread, previous_thread, True))
        user32.SetForegroundWindow(handle)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, previous_thread, False)
    if _wait_foreground(handle, user32, kernel32):
        return window_ref(handle, title=window.title) or window
    # 兜底：向系统注入一次 Alt 抬起/按下（见 docstring 的取舍说明）。
    # 放在最后且用 try/finally 保证 Alt 一定被抬起，不留粘滞键。
    user32.keybd_event(0x12, 0, 0, 0)
    try:
        user32.SetForegroundWindow(handle)
    finally:
        user32.keybd_event(0x12, 0, 0x0002, 0)
    if _wait_foreground(handle, user32, kernel32):
        return window_ref(handle, title=window.title) or window
    return None


def _foreground_handle() -> int:
    """当前前台窗口句柄；取不到（含 ctypes 返回 None 的情形）一律当 0。

    2026-11-04 踩过：`GetForegroundWindow()` 在个别调用序列下返回值不是 int，
    直接 `int(...)` 会抛 TypeError 把整个工具打成异常。前台句柄缺失只该意味着"没抢到"，
    不该是一次崩溃。
    """
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _wait_foreground(handle: int, user32: Any, kernel32: Any, *, tries: int = 6) -> bool:
    """轮询确认前台切到了目标句柄；慢一点也算成功，抢不到就如实返回 False。"""
    for _ in range(tries):
        if _foreground_handle() == handle:
            return True
        user32.SetForegroundWindow(handle)
        kernel32.Sleep(120)
    return _foreground_handle() == handle


def _is_foreground(handle: int) -> bool:
    return _foreground_handle() == handle


def release_modifiers() -> None:
    """强制抬起四个修饰键。

    ``SendKeys`` 正常路径会自己抬起，但异常/超时/取消都可能停在"按着不放"的状态；用户遇到这种
    情况会以为键盘坏了。这里用 `keybd_event` 直接送抬起事件，幂等。
    """
    if not available():
        return
    user32 = ctypes.windll.user32
    for code in (0x10, 0x11, 0x12, 0x5B):  # Shift / Ctrl / Alt / LWin
        user32.keybd_event(code, 0, 0x0002, 0)  # KEYEVENTF_KEYUP

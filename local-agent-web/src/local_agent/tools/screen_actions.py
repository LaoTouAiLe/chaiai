"""屏幕动作的实现（2026-11-05 从 `screen_tools` 拆出）。

`screen_tools` 只保留对外的工具外壳（名字、参数、给模型看的描述）；判定与执行放在这里。
拆开的理由很具体：七个工具的判定链塞进一个工厂函数，会同时撞上函数行数（300）与
复杂度（20）两条硬上限，而这两条上限存在的意义正是"别在一个函数里塞七条审批链"。

状态（本轮已操作的窗口、动作计数、最近一次读数的编号映射）装在 :class:`ScreenSession`
里由外壳持有——它是**每轮一份**的，不能做成模块级全局。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage

from local_agent import screen_capture, screen_uia
from local_agent.exceptions import SecurityError
from local_agent.screen_capture import Capture, CaptureUnavailable
from local_agent.screen_input import MAX_ACTIONS_PER_TURN, MAX_TEXT_CHARS, WindowRef
from local_agent.screen_uia import UiTree
from local_agent.security.approval import action_fingerprint
from local_agent.security.policy import SecurityPolicy

#: 平台或依赖不可用时给模型看的话：都要能直接照做，不是"出错了"。
NO_VISION_NOTE = (
    "这条模型连接**没有声明支持图像输入**，所以我没有截图（截了你也看不到）。"
    "要打开它：在「设置 → 模型连接」里编辑**这条连接**，勾上「支持图片输入」，重启后端即可。"
    "在那之前请用文本手段取事实：`inspect_system`、文件读取、或让我直接描述。"
)
UNSUPPORTED_NOTE = "截屏目前只在 Windows 上实现；这台机器上不可用。"
NO_UIA_NOTE = (
    "读界面控件树需要 Windows + `uiautomation` 后端，这台机器上不可用。"
    "请改用截图看画面，或让用户直接描述界面。"
)
NO_INPUT_NOTE = (
    "模拟点击与键盘需要 Windows + `uiautomation` 后端，这台机器上不可用。"
    "可以改用截图与控件树告诉用户该点哪里。"
)


@dataclass(slots=True)
class ScreenSession:
    """一轮屏幕工具的运行态。

    ``capture`` / ``capture_window`` / ``read_tree`` 保留 ``None`` 语义：真正调用时才回落到
    `screen_capture` / `screen_uia` 的模块函数——测试用 monkeypatch 替换那三个模块属性时，
    这里必须**每次重新取**，否则替换不生效。
    """

    policy: SecurityPolicy
    vision_available: bool
    cache_dir: Path | None
    capture: Callable[..., Capture] | None
    capture_window: Callable[..., Capture] | None
    read_tree: Callable[..., UiTree] | None
    inputs: Any
    state: dict[str, Any]


def pick_window(session: ScreenSession, pattern: str) -> Any:
    """按标题挑唯一窗口；挑不出来就返回一句给模型看的话（而不是抛异常）。"""
    # 走注入的后端：生产是 screen_input，测试是替身——否则单测会去枚举真实桌面。
    matches = session.inputs.find_windows(pattern)
    if not matches:
        return f"没找到标题包含「{pattern}」的可见窗口。可以先用截图看看现在有哪些窗口。"
    if len(matches) > 1:
        listed = "；".join(f"「{item.title}」" for item in matches[:5])
        return (
            f"标题包含「{pattern}」的窗口有 {len(matches)} 个：{listed}。"
            "请给一个更精确的标题再试。"
        )
    return matches[0]


def labeled(session: ScreenSession, shot: Capture) -> tuple[tuple[screen_uia.Target, ...], bytes | None]:
    """给截图叠加编号；拿不到控件、或窗口对不上，就退回纯画面。

    后台窗口也要能编号，所以这里按**截图那张图的句柄**读树，而不是读前台窗口。
    """
    if not screen_uia.available():
        return (), None
    handle = shot.window_handle
    try:
        if handle:
            tree = (session.read_tree or screen_uia.read_window)(handle=handle)
        else:
            tree = (session.read_tree or screen_uia.read_foreground)()
    except Exception:  # noqa: BLE001 - 拿不到树不影响截图本身，别让它变成失败
        return (), None
    if not tree.targets:
        return (), None
    if handle and tree.window_handle and tree.window_handle != handle:
        return (), None
    boxes = [(t.index, t.left, t.top, t.width, t.height) for t in tree.targets]
    return tree.targets, screen_capture.annotate(shot, boxes)


def screenshot(session: ScreenSession, *, labels: bool, window: str) -> str | ToolMessage:
    """截前台窗口（或指定窗口）成一张图交给模型。"""
    if not session.vision_available:
        return NO_VISION_NOTE
    if not screen_capture.available():
        return UNSUPPORTED_NOTE
    try:
        if window.strip():
            picked = pick_window(session, window)
            if isinstance(picked, str):
                return picked
            shot = (session.capture_window or screen_capture.capture_window)(
                picked.handle, title=picked.title
            )
        else:
            shot = (session.capture or screen_capture.capture_front_window)()
    except CaptureUnavailable as error:
        return f"没截到：{error}。请先把目标窗口切到前台再让我截。"
    path = screen_capture.save(shot, session.cache_dir)
    note = (
        f"已截取前台窗口《{shot.window_title}》{shot.width}×{shot.height}，"
        f"{len(shot.data)} 字节。缓存：{path}（不在工作区，进程退出时清空）"
    )
    targets: tuple[screen_uia.Target, ...] = ()
    data_url = shot.data_url
    if labels:
        targets, annotated = labeled(session, shot)
        if annotated is not None:
            data_url = replace(shot, data=annotated).data_url
            session.state["targets"] = {target.index: target for target in targets}
            session.state["targets_handle"] = shot.window_handle
            session.state["targets_title"] = shot.window_title
            note += f"。图上叠加了 {len(targets)} 个编号"
    content: list[Any] = [
        {"type": "text", "text": note},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    if targets:
        content[0]["text"] = note + "\n\n" + screen_uia.render_targets(targets)
    return ToolMessage(
        content=content,
        # 占位 id：回填给模型的那一条由工具执行节点重建（它只取 `.content`）。
        tool_call_id="screenshot",
        name="screenshot",
    )


def read_ui(session: ScreenSession, *, max_depth: int, window: str) -> str:
    """读前台窗口（或指定窗口）的控件树，并缓存编号映射。"""
    if not screen_uia.available():
        return NO_UIA_NOTE
    depth = max(1, min(int(max_depth), 6))
    handle = 0
    if window.strip():
        picked = pick_window(session, window)
        if isinstance(picked, str):
            return picked
        handle = picked.handle
    try:
        if handle:
            tree = (session.read_tree or screen_uia.read_window)(handle=handle, max_depth=depth)
        else:
            tree = (session.read_tree or screen_uia.read_foreground)(max_depth=depth)
    except Exception as error:  # noqa: BLE001 - 读失败要如实说，不能返回空字符串
        return f"读不到控件树：{error}。请先把目标窗口切到前台，或改用截图。"
    # 编号只在"这一次读数 + 这一个窗口"内有效：缓存映射并记住窗口句柄，
    # 由 `click_element` 校验，窗口一变就要求重新读。
    session.state["targets"] = {target.index: target for target in tree.targets}
    session.state["targets_handle"] = tree.window_handle
    session.state["targets_title"] = tree.window_title
    return screen_uia.render(tree) + "\n\n" + screen_uia.render_targets(tree.targets)


def act(
    session: ScreenSession,
    tool_name: str,
    arguments: dict[str, object],
    verb: str,
    run: Callable[[WindowRef], None],
) -> str:
    """三个动作共用的前置判定 + 收尾。

    顺序不能换：先判可用与上限（不打搅审批），再取前台窗口（授权对象是它），
    再走安全判定（可能要批准），最后执行并在 ``finally`` 里抬起修饰键。
    """
    if not session.inputs.available():
        return NO_INPUT_NOTE
    if session.state["actions"] >= MAX_ACTIONS_PER_TURN:
        return (
            f"本轮已经执行 {session.state['actions']} 次屏幕动作，达到上限"
            f"（{MAX_ACTIONS_PER_TURN}）。请先说明进展，让用户决定是否继续。"
        )
    window = session.inputs.foreground()
    if window is None:
        return "没有前台窗口，无法定位操作目标。请先把目标窗口切到前台。"
    if session.state["handle"] and session.state["handle"] != window.handle:
        # 授权对象是"那个窗口"，不是这台机器：换了窗口就撤销本轮授权并如实拒绝。
        session.policy.forget_screen_approval()
        session.state["handle"] = 0
        return (
            f"前台窗口已经换成「{window.title}」，与本次获授权的窗口不同。"
            "为免误点，请让用户确认后再操作（下一次动作会重新请求批准）。"
        )
    decision = session.policy.classify_tool(tool_name, arguments)
    granted = session.policy.is_granted(action_fingerprint(tool_name, arguments, policy=session.policy))
    if not decision.allowed and not granted:
        raise SecurityError(decision.reason)
    try:
        run(window)
    finally:
        session.inputs.release_modifiers()
    session.state["actions"] += 1
    session.state["handle"] = window.handle
    if granted:
        # 这次执行靠的正是"第一次屏幕操作"那次批准：记住它，本轮后面不再逐个问。
        session.policy.note_screen_action_approved()
    return f"已在「{window.title}」（{window.left},{window.top}）{verb}"


def click_screen(session: ScreenSession, x: int, y: int) -> str:
    """按屏幕绝对坐标点一下；坐标必须落在前台窗口内。"""
    return act(
        session,
        "click_screen",
        {"x": x, "y": y},
        f"点击了 ({x},{y})",
        lambda window: click_in(window, x, y, session.inputs.click),
    )


def click_element(session: ScreenSession, index: int) -> str:
    """按 `read_ui` 的编号点击；编号跨窗口不作数。"""
    targets = session.state.get("targets")
    target = targets.get(int(index)) if isinstance(targets, dict) else None
    if target is None:
        return (
            f"编号 #{index} 不在最近一次读数里。请先调 read_ui，再用它给出的编号点击"
            "（编号只对最近一次读数、同一个窗口有效）。"
        )
    current = session.inputs.foreground()
    if current is None:
        return "没有前台窗口，无法定位操作目标。请先把目标窗口切到前台。"
    expected = int(session.state.get("targets_handle") or 0)
    if expected and current.handle != expected:
        title = str(session.state.get("targets_title") or "")
        return (
            f"编号来自「{title}」，但它此刻不在前台（当前前台是「{current.title}」）。"
            "点击只作用于前台窗口：要么先 focus_window 把它切到前面（会重新请求批准），"
            "要么重新 read_ui 读当前这个窗口。"
        )
    center = target.center
    return act(
        session,
        "click_element",
        {"index": int(index)},
        f"点击了 #{target.index} {target.role}「{target.name}」",
        lambda window: click_in(window, center[0], center[1], session.inputs.click),
    )


def type_text(session: ScreenSession, text: str) -> str:
    """在当前焦点输入文本；长度有上限，大段内容请走文件。"""
    if not text:
        return "没有要输入的文本，未执行。"
    if len(text) > MAX_TEXT_CHARS:
        return (
            f"文本 {len(text)} 字符，超过单次上限 {MAX_TEXT_CHARS}。"
            "大段内容请写文件再用其他方式交给目标程序。"
        )
    return act(
        session,
        "type_text",
        {"text": text},
        f"输入了 {len(text)} 个字符",
        lambda _window: session.inputs.type_text(text),
    )


def press_keys(session: ScreenSession, keys: str) -> str:
    """按组合键；结束时强制抬起所有修饰键。"""
    if not keys.strip():
        return "没有指定按键，未执行。"
    return act(
        session,
        "press_keys",
        {"keys": keys},
        f"按下了 {keys}",
        lambda _window: session.inputs.press_keys(keys),
    )


def focus_window(session: ScreenSession, title: str) -> str:
    """把一个窗口切到前台（每次都要批，且会作废本轮的动作授权）。"""
    if not session.inputs.available():
        return NO_INPUT_NOTE
    pattern = title.strip()
    if not pattern:
        return "没有给出窗口标题，未执行。"
    target = pick_window(session, pattern)
    if isinstance(target, str):
        return target
    arguments = {"title": title}
    decision = session.policy.classify_tool("focus_window", arguments)
    granted = session.policy.is_granted(action_fingerprint("focus_window", arguments, policy=session.policy))
    if not decision.allowed and not granted:
        raise SecurityError(decision.reason)
    restored = session.inputs.focus(target)
    if restored is None:
        return (
            "切前台失败：系统前台锁拒绝了这次切换（可能另一个程序正抢占，或系统限制了"
            "后台程序置前）。请手动点一下目标窗口，再让我继续。"
        )
    # 授权对象是"那个窗口"：窗口换了，本轮的动作授权随之作废（与换窗口的拒绝分支同一口径）。
    session.state["handle"] = restored.handle
    session.policy.forget_screen_approval()
    note = "（原来是最小化，已恢复）" if target.minimized else ""
    return (
        f"已把「{restored.title}」切到前台{note}（句柄 {restored.handle}，"
        f"区域 {restored.left},{restored.top} – {restored.right},{restored.bottom}）。"
        "接下来的点击/输入需要重新批准一次。"
    )


def click_in(window: WindowRef, x: int, y: int, click: Callable[[int, int], None]) -> None:
    """坐标必须落在窗口内：屏幕坐标最容易在缩放/多屏下算错，宁可不点。"""
    if not window.contains(x, y):
        raise SecurityError(
            f"坐标 ({x},{y}) 不在前台窗口「{window.title}」的范围内"
            f"（{window.left},{window.top} – {window.right},{window.bottom}）：已拒绝，避免点到别的程序。"
        )
    click(x, y)

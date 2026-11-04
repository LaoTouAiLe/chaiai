"""截屏工具（2026-11-04）：让模型**看见**当前前台窗口。

与 `image_tools.view_image` 的分工：那条通路是"把一个已有图片文件交给模型"，本工具是
"先抓一张，再交给模型"——截图不落工作区、不占用户的目录，走系统临时目录并滚动淘汰。
两者共用同一条内容块形态（``text`` + ``image_url``），执行节点不需要知道图是哪来的。

四条边界：

1. **只读**：不写文件、不改状态，登记在 ``security.policy.READ_ONLY_TOOLS``，不需要人工审批；
2. **模型收不了图就失败闭合**：``vision_available=False`` 时**不去截图**（截了也看不到），
   并说明怎么打开（与 `image_tools` 逐字同一口径，避免出现第二套解释）；
3. **平台不可用就说清楚**：非 Windows 直接回一句"这台机器上截不了"，不返回空图；
4. **抓不到就如实说**：没有前台窗口、窗口无可见区域都翻译成一句可读的原因。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool

from local_agent import screen_capture, screen_input, screen_uia
from local_agent.exceptions import SecurityError
from local_agent.screen_capture import Capture, CaptureUnavailable
from local_agent.screen_input import MAX_ACTIONS_PER_TURN, MAX_TEXT_CHARS, WindowRef
from local_agent.screen_uia import UiTree
from local_agent.security.approval import action_fingerprint
from local_agent.security.policy import SecurityPolicy

_NO_VISION_NOTE = (
    "这条模型连接**没有声明支持图像输入**，所以我没有截图（截了你也看不到）。"
    "要打开它：在「设置 → 模型连接」里编辑**这条连接**，勾上「支持图片输入」，重启后端即可。"
    "在那之前请用文本手段取事实：`inspect_system`、文件读取、或让我直接描述。"
)

_UNSUPPORTED_NOTE = "截屏目前只在 Windows 上实现；这台机器上不可用。"

_NO_UIA_NOTE = (
    "读界面控件树需要 Windows + `uiautomation` 后端，这台机器上不可用。"
    "请改用截图看画面，或让用户直接描述界面。"
)

_NO_INPUT_NOTE = (
    "模拟点击与键盘需要 Windows + `uiautomation` 后端，这台机器上不可用。"
    "可以改用截图与控件树告诉用户该点哪里。"
)


def build_screen_tools(
    policy: SecurityPolicy,
    *,
    vision_available: bool,
    cache_dir: Path | None = None,
    capture: Callable[[], Capture] | None = None,
    capture_window: Callable[..., Capture] | None = None,
    read_tree: Callable[..., UiTree] | None = None,
    input_backend: Any = None,
) -> list[BaseTool]:
    """构造屏幕工具（截屏 + 读控件树 + 模拟点击/键盘）。

    与 `image_tools` / `git_tools` 同一口径：**工具总是被构造**（注册表的能力覆盖校验要求
    登记过的工具都在），能不能用由能力层与工具内部的失败闭合共同决定。

    Args:
        policy: 本轮安全策略。模拟输入要用它判定审批与记"本轮已批准"。
        vision_available: 这条连接是否声明支持图像输入。
        cache_dir: 截图缓存目录；省略用系统临时目录。
        capture: 抓前台窗口入口；测试注入替身，生产走 :func:`screen_capture.capture_front_window`。
        capture_window: 抓指定窗口入口（PrintWindow）；生产走 :func:`screen_capture.capture_window`。
        read_tree: 读控件树入口；测试注入替身，生产走 :func:`screen_uia.read_foreground`。
        input_backend: 模拟输入后端（测试注入替身，**绝不真点**）；省略走 :mod:`screen_input`。
    """
    inputs = input_backend or screen_input
    #: 本轮已操作过的窗口句柄、动作计数，以及最近一次读树/截图得到的编号映射。
    #: 窗口换了要重新授权，动作到顶要拒绝，编号跨窗口不作数。
    state: dict[str, Any] = {"handle": 0, "actions": 0, "targets": {}, "targets_handle": 0}

    def _pick_window(pattern: str) -> Any:
        """按标题挑唯一窗口；挑不出来就返回一句给模型看的话（而不是抛异常）。"""
        # 走注入的后端：生产是 screen_input，测试是替身——否则单测会去枚举真实桌面。
        matches = inputs.find_windows(pattern)
        if not matches:
            return f"没找到标题包含「{pattern}」的可见窗口。可以先用截图看看现在有哪些窗口。"
        if len(matches) > 1:
            listed = "；".join(f"「{item.title}」" for item in matches[:5])
            return (
                f"标题包含「{pattern}」的窗口有 {len(matches)} 个：{listed}。"
                "请给一个更精确的标题再试。"
            )
        return matches[0]

    def _labeled(shot: Capture) -> tuple[tuple[screen_uia.Target, ...], bytes | None]:
        """给截图叠加编号；拿不到控件、或窗口对不上，就退回纯画面。

        后台窗口也要能编号，所以这里按**截图那张图的句柄**读树，而不是读前台窗口。
        """
        if not screen_uia.available():
            return (), None
        handle = shot.window_handle
        try:
            if handle:
                tree = (read_tree or screen_uia.read_window)(handle=handle)
            else:
                tree = (read_tree or screen_uia.read_foreground)()
        except Exception:  # noqa: BLE001 - 拿不到树不影响截图本身，别让它变成失败
            return (), None
        if not tree.targets:
            return (), None
        if handle and tree.window_handle and tree.window_handle != handle:
            return (), None
        boxes = [(t.index, t.left, t.top, t.width, t.height) for t in tree.targets]
        return tree.targets, screen_capture.annotate(shot, boxes)

    @langchain_tool("screenshot")
    def screenshot(labels: bool = True, window: str = "") -> str | ToolMessage:
        """截屏：把**当前前台窗口**截成一张图交给你，你能直接看到它。

        什么时候用：用户说"看看我屏幕上这个"、需要确认某个窗口的实际显示（界面走查、
        报错弹窗、图表读数）时。只截前台窗口，不截整屏、不点鼠标、不敲键盘。

        默认会在可点击控件上叠**红色编号**，并给出同样的编号列表——想点哪个就说
        `click_element(编号)`，不必自己算坐标。编号看不清或不需要时传 `labels=false`。

        截完图会同时给你一行说明（窗口标题、像素尺寸、缓存路径）。缓存不在工作区，
        进程退出即清空。

        Args:
            labels: 是否在图上叠加编号（默认 true）。关掉只给纯画面。
            window: 窗口标题片段；省略=当前前台窗口。**指定后不要求它在前台**，
                也不会抢你的焦点（代价：GPU 渲染的窗口可能截成黑屏，那就得改用前台截图）。
        """
        if not vision_available:
            return _NO_VISION_NOTE
        if not screen_capture.available():
            return _UNSUPPORTED_NOTE
        try:
            if window.strip():
                picked = _pick_window(window)
                if isinstance(picked, str):
                    return picked
                shot = (capture_window or screen_capture.capture_window)(
                    picked.handle, title=picked.title
                )
            else:
                shot = (capture or screen_capture.capture_front_window)()
        except CaptureUnavailable as error:
            return f"没截到：{error}。请先把目标窗口切到前台再让我截。"
        path = screen_capture.save(shot, cache_dir)
        note = (
            f"已截取前台窗口《{shot.window_title}》{shot.width}×{shot.height}，"
            f"{len(shot.data)} 字节。缓存：{path}（不在工作区，进程退出时清空）"
        )
        targets: tuple[screen_uia.Target, ...] = ()
        data_url = shot.data_url
        if labels:
            targets, annotated = _labeled(shot)
            if annotated is not None:
                data_url = replace(shot, data=annotated).data_url
                state["targets"] = {target.index: target for target in targets}
                state["targets_handle"] = shot.window_handle
                state["targets_title"] = shot.window_title
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

    @langchain_tool("read_ui")
    def read_ui(max_depth: int = screen_uia.MAX_DEPTH, window: str = "") -> str:
        """读界面：把**当前前台窗口**的控件树读成一段缩进文本（类型、名字、位置、是否可用）。

        什么时候用：需要知道"有哪些按钮/输入框、叫什么、在哪"，或做界面走查时。它不点击、
        不聚焦、不发送任何输入，只读结构；也**不需要模型能收图**，纯文本结果。

        有些应用（Qt 未开无障碍、自绘界面）不暴露控件，这时会明确说"树是空的"，
        那种情况请改用截图。

        Args:
            max_depth: 展开层数，1–6，默认 4。层数越大越慢，也越容易超出上下文预算。
            window: 窗口标题片段；省略=当前前台窗口。**指定后不要求它在前台**，
                所以可以先看后台窗口有什么，再决定值不值得抢焦点。
        """
        if not screen_uia.available():
            return _NO_UIA_NOTE
        depth = max(1, min(int(max_depth), 6))
        handle = 0
        if window.strip():
            picked = _pick_window(window)
            if isinstance(picked, str):
                return picked
            handle = picked.handle
        try:
            if handle:
                tree = (read_tree or screen_uia.read_window)(handle=handle, max_depth=depth)
            else:
                tree = (read_tree or screen_uia.read_foreground)(max_depth=depth)
        except Exception as error:  # noqa: BLE001 - 读失败要如实说，不能返回空字符串
            return f"读不到控件树：{error}。请先把目标窗口切到前台，或改用截图。"
        # 编号只在"这一次读数 + 这一个窗口"内有效：缓存映射并记住窗口句柄，
        # 由 `click_element` 校验，窗口一变就要求重新读。
        state["targets"] = {target.index: target for target in tree.targets}
        state["targets_handle"] = tree.window_handle
        state["targets_title"] = tree.window_title
        return screen_uia.render(tree) + "\n\n" + screen_uia.render_targets(tree.targets)

    # ---- 模拟点击与键盘（架构 §18.10）----------------------------------------

    def _act(tool_name: str, arguments: dict[str, object], verb: str, run: Callable[[WindowRef], None]) -> str:
        """三个动作共用的前置判定 + 收尾。

        顺序不能换：先判可用与上限（不打搅审批），再取前台窗口（授权对象是它），
        再走安全判定（可能要批准），最后执行并在 ``finally`` 里抬起修饰键。
        """
        if not inputs.available():
            return _NO_INPUT_NOTE
        if state["actions"] >= MAX_ACTIONS_PER_TURN:
            return (
                f"本轮已经执行 {state['actions']} 次屏幕动作，达到上限"
                f"（{MAX_ACTIONS_PER_TURN}）。请先说明进展，让用户决定是否继续。"
            )
        window = inputs.foreground()
        if window is None:
            return "没有前台窗口，无法定位操作目标。请先把目标窗口切到前台。"
        if state["handle"] and state["handle"] != window.handle:
            # 授权对象是"那个窗口"，不是这台机器：换了窗口就撤销本轮授权并如实拒绝。
            policy.forget_screen_approval()
            state["handle"] = 0
            return (
                f"前台窗口已经换成「{window.title}」，与本次获授权的窗口不同。"
                "为免误点，请让用户确认后再操作（下一次动作会重新请求批准）。"
            )
        decision = policy.classify_tool(tool_name, arguments)
        granted = policy.is_granted(action_fingerprint(tool_name, arguments, policy=policy))
        if not decision.allowed and not granted:
            raise SecurityError(decision.reason)
        try:
            run(window)
        finally:
            inputs.release_modifiers()
        state["actions"] += 1
        state["handle"] = window.handle
        if granted:
            # 这次执行靠的正是"第一次屏幕操作"那次批准：记住它，本轮后面不再逐个问。
            policy.note_screen_action_approved()
        return f"已在「{window.title}」（{window.left},{window.top}）{verb}"

    @langchain_tool("click_screen")
    def click_screen(x: int, y: int) -> str:
        """在当前前台窗口里点一下鼠标左键，坐标是**屏幕绝对坐标**。

        什么时候用：先用 `read_ui` 找到控件的 `[x,y wxh]`，取中心点再点。坐标必须落在窗口矩形内，
        越界会被拒绝。它作用在真实桌面上，本轮第一次调用需要用户批准一次。

        Args:
            x: 屏幕横坐标。
            y: 屏幕纵坐标。
        """
        return _act("click_screen", {"x": x, "y": y}, f"点击了 ({x},{y})",
                    lambda window: _click_in(window, x, y, inputs.click))

    @langchain_tool("click_element")
    def click_element(index: int) -> str:
        """按 `read_ui` 给出的**编号**点击那个控件：不用自己算坐标，也不会点偏几像素。

        前提是刚调过 `read_ui`（或刚让它重新读过）：编号只对最近一次读数、且**同一个窗口**有效。
        窗口换了、或者界面结构变了，编号就可能指到别的控件上，这时会要求你重新读一次。

        Args:
            index: `read_ui` 输出里 ``#`` 后面的编号。
        """
        targets = state.get("targets")
        target = targets.get(int(index)) if isinstance(targets, dict) else None
        if target is None:
            return (
                f"编号 #{index} 不在最近一次读数里。请先调 read_ui，再用它给出的编号点击"
                "（编号只对最近一次读数、同一个窗口有效）。"
            )
        current = inputs.foreground()
        if current is None:
            return "没有前台窗口，无法定位操作目标。请先把目标窗口切到前台。"
        expected = int(state.get("targets_handle") or 0)
        if expected and current.handle != expected:
            title = str(state.get("targets_title") or "")
            return (
                f"编号来自「{title}」，但它此刻不在前台（当前前台是「{current.title}」）。"
                "点击只作用于前台窗口：要么先 focus_window 把它切到前面（会重新请求批准），"
                "要么重新 read_ui 读当前这个窗口。"
            )
        center = target.center
        return _act(
            "click_element",
            {"index": int(index)},
            f"点击了 #{target.index} {target.role}「{target.name}」",
            lambda window: _click_in(window, center[0], center[1], inputs.click),
        )

    @langchain_tool("type_text")
    def type_text(text: str) -> str:
        """在当前前台窗口的输入焦点里输入一段文本（模拟键盘，中文也可以）。

        适合短文本（上限 2000 字符）；大段内容请写文件或用 shell。输入前请确认焦点在正确的输入框：
        先用 `read_ui` 看哪个控件是 Edit，必要时先 `click_screen` 点进去。

        Args:
            text: 要输入的文本。
        """
        if not text:
            return "没有要输入的文本，未执行。"
        if len(text) > MAX_TEXT_CHARS:
            return (
                f"文本 {len(text)} 字符，超过单次上限 {MAX_TEXT_CHARS}。"
                "大段内容请写文件再用其他方式交给目标程序。"
            )
        return _act("type_text", {"text": text}, f"输入了 {len(text)} 个字符",
                    lambda _window: inputs.type_text(text))

    @langchain_tool("press_keys")
    def press_keys(keys: str) -> str:
        """按一组组合键，例如 ``ctrl+s``、``alt+tab``、``enter``、``ctrl+shift+p``。

        组合键按"按下→抬起"成对发送，结束时会强制抬起所有修饰键，不会留下粘滞的 Ctrl/Shift。
        本轮第一次调用需要用户批准一次。

        Args:
            keys: 用 ``+`` 连接的组合键。
        """
        if not keys.strip():
            return "没有指定按键，未执行。"
        return _act("press_keys", {"keys": keys}, f"按下了 {keys}",
                    lambda _window: inputs.press_keys(keys))

    @langchain_tool("focus_window")
    def focus_window(title: str) -> str:
        """把一个窗口切到前台（按标题子串匹配，唯一才执行）。

        什么时候用：需要操作的目标窗口不在前台时先调它——否则点击会被"只作用于前台窗口"这条
        边界拒绝。它**每次都要用户批准**：抢焦点会打断用户正在做的事。

        标题必须唯一：匹配到多个窗口时会列出来让你换个更精确的标题，不猜。

        Args:
            title: 窗口标题里的片段，例如 ``"记事本"`` 或 ``"Chrome"``。
        """
        if not inputs.available():
            return _NO_INPUT_NOTE
        pattern = title.strip()
        if not pattern:
            return "没有给出窗口标题，未执行。"
        target = _pick_window(pattern)
        if isinstance(target, str):
            return target
        arguments = {"title": title}
        decision = policy.classify_tool("focus_window", arguments)
        granted = policy.is_granted(action_fingerprint("focus_window", arguments, policy=policy))
        if not decision.allowed and not granted:
            raise SecurityError(decision.reason)
        restored = inputs.focus(target)
        if restored is None:
            return (
                "切前台失败：系统前台锁拒绝了这次切换（可能另一个程序正抢占，或系统限制了"
                "后台程序置前）。请手动点一下目标窗口，再让我继续。"
            )
        # 授权对象是"那个窗口"：窗口换了，本轮的动作授权随之作废（与换窗口的拒绝分支同一口径）。
        state["handle"] = restored.handle
        policy.forget_screen_approval()
        note = "（原来是最小化，已恢复）" if target.minimized else ""
        return (
            f"已把「{restored.title}」切到前台{note}（句柄 {restored.handle}，"
            f"区域 {restored.left},{restored.top} – {restored.right},{restored.bottom}）。"
            "接下来的点击/输入需要重新批准一次。"
        )

    return [screenshot, read_ui, click_screen, click_element, type_text, press_keys, focus_window]


def _click_in(window: WindowRef, x: int, y: int,
              click: Callable[[int, int], None]) -> None:
    """坐标必须落在窗口内：屏幕坐标最容易在缩放/多屏下算错，宁可不点。"""
    if not window.contains(x, y):
        raise SecurityError(
            f"坐标 ({x},{y}) 不在前台窗口「{window.title}」的范围内"
            f"（{window.left},{window.top} – {window.right},{window.bottom}）：已拒绝，避免点到别的程序。"
        )
    click(x, y)

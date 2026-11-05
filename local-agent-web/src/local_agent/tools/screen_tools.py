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

本模块只放**对外的工具外壳**（名字、参数、给模型看的描述）。判定与执行在
:mod:`local_agent.tools.screen_actions`——七个工具的判定链塞进一个工厂函数，会同时撞上
函数行数与复杂度两条硬上限。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool

from local_agent import screen_input, screen_uia
from local_agent.screen_capture import Capture
from local_agent.screen_uia import UiTree
from local_agent.security.policy import SecurityPolicy
from local_agent.tools import screen_actions
from local_agent.tools.screen_actions import ScreenSession


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
    session = ScreenSession(
        policy=policy,
        vision_available=vision_available,
        cache_dir=cache_dir,
        capture=capture,
        capture_window=capture_window,
        read_tree=read_tree,
        inputs=input_backend or screen_input,
        #: 本轮已操作过的窗口句柄、动作计数，以及最近一次读树/截图得到的编号映射。
        #: 窗口换了要重新授权，动作到顶要拒绝，编号跨窗口不作数。
        state={"handle": 0, "actions": 0, "targets": {}, "targets_handle": 0},
    )

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
        return screen_actions.screenshot(session, labels=labels, window=window)

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
        return screen_actions.read_ui(session, max_depth=max_depth, window=window)

    @langchain_tool("click_screen")
    def click_screen(x: int, y: int) -> str:
        """在当前前台窗口里点一下鼠标左键，坐标是**屏幕绝对坐标**。

        什么时候用：先用 `read_ui` 找到控件的 `[x,y wxh]`，取中心点再点。坐标必须落在窗口矩形内，
        越界会被拒绝。它作用在真实桌面上，本轮第一次调用需要用户批准一次。

        Args:
            x: 屏幕横坐标。
            y: 屏幕纵坐标。
        """
        return screen_actions.click_screen(session, x, y)

    @langchain_tool("click_element")
    def click_element(index: int) -> str:
        """按 `read_ui` 给出的**编号**点击那个控件：不用自己算坐标，也不会点偏几像素。

        前提是刚调过 `read_ui`（或刚让它重新读过）：编号只对最近一次读数、且**同一个窗口**有效。
        窗口换了、或者界面结构变了，编号就可能指到别的控件上，这时会要求你重新读一次。

        Args:
            index: `read_ui` 输出里 ``#`` 后面的编号。
        """
        return screen_actions.click_element(session, index)

    @langchain_tool("type_text")
    def type_text(text: str) -> str:
        """在当前前台窗口的输入焦点里输入一段文本（模拟键盘，中文也可以）。

        适合短文本（上限 2000 字符）；大段内容请写文件或用 shell。输入前请确认焦点在正确的输入框：
        先用 `read_ui` 看哪个控件是 Edit，必要时先 `click_screen` 点进去。

        Args:
            text: 要输入的文本。
        """
        return screen_actions.type_text(session, text)

    @langchain_tool("press_keys")
    def press_keys(keys: str) -> str:
        """按一组组合键，例如 ``ctrl+s``、``alt+tab``、``enter``、``ctrl+shift+p``。

        组合键按"按下→抬起"成对发送，结束时会强制抬起所有修饰键，不会留下粘滞的 Ctrl/Shift。
        本轮第一次调用需要用户批准一次。

        Args:
            keys: 用 ``+`` 连接的组合键。
        """
        return screen_actions.press_keys(session, keys)

    @langchain_tool("focus_window")
    def focus_window(title: str) -> str:
        """把一个窗口切到前台（按标题子串匹配，唯一才执行）。

        什么时候用：需要操作的目标窗口不在前台时先调它——否则点击会被"只作用于前台窗口"这条
        边界拒绝。它**每次都要用户批准**：抢焦点会打断用户正在做的事。

        标题必须唯一：匹配到多个窗口时会列出来让你换个更精确的标题，不猜。

        Args:
            title: 窗口标题里的片段，例如 ``"记事本"`` 或 ``"Chrome"``。
        """
        return screen_actions.focus_window(session, title)

    return [screenshot, read_ui, click_screen, click_element, type_text, press_keys, focus_window]

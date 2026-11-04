"""屏幕能力（2026-11-04）：只读门、失败闭合、缓存滚动、模拟输入的审批与收尾。

不依赖真实桌面：抓屏、读树、模拟输入三个入口都通过参数注入替身，**绝不真点**。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from local_agent import screen_capture, screen_uia
from local_agent.capabilities import CAPABILITY_DEFAULTS, CAPABILITY_TOOLS, PROFILES
from local_agent.config import AppConfig
from local_agent.exceptions import SecurityError
from local_agent.screen_capture import Capture, CaptureUnavailable
from local_agent.screen_input import MAX_ACTIONS_PER_TURN, MAX_TEXT_CHARS, WindowRef
from local_agent.screen_uia import UiNode, UiTree
from local_agent.security.approval import action_fingerprint
from local_agent.security.policy import (
    KNOWN_TOOLS,
    READ_ONLY_TOOLS,
    SCREEN_FOCUS_TOOLS,
    SCREEN_INPUT_TOOLS,
    SecurityPolicy,
)
from local_agent.tools.screen_tools import build_screen_tools

FAKE = Capture(b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg", 800, 600, "记事本")
WINDOW = WindowRef(handle=101, title="记事本", left=0, top=0, right=1000, bottom=800)


@pytest.fixture
def policy(app_config: AppConfig, tmp_path: Path) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})


def _build(policy: SecurityPolicy, **kwargs):
    kwargs.setdefault("vision_available", True)
    return build_screen_tools(policy, **kwargs)


def _tool(policy: SecurityPolicy, **kwargs):
    return _build(policy, **kwargs)[0]


def _ui_tool(policy: SecurityPolicy, **kwargs):
    """读控件树**不需要**模型能收图，所以默认 vision_available=False。"""
    kwargs.setdefault("vision_available", False)
    return build_screen_tools(policy, **kwargs)[1]


class _FakeInputs:
    """模拟输入替身：只记录调用，不碰真实鼠标键盘。"""

    def __init__(self, window: WindowRef | None = WINDOW, available: bool = True) -> None:
        self.window = window
        self._available = available
        self.calls: list[tuple] = []
        self.releases = 0
        self.windows: list[WindowRef] = [WINDOW]
        self.focus_ok = True

    def available(self) -> bool:
        return self._available

    def foreground(self) -> WindowRef | None:
        return self.window

    def find_windows(self, pattern: str) -> list[WindowRef]:
        needle = pattern.lower()
        return [item for item in self.windows if needle in item.title.lower()]

    def focus(self, window: WindowRef) -> WindowRef | None:
        self.calls.append(("focus", window.handle))
        if not self.focus_ok:
            return None
        restored = WindowRef(
            handle=window.handle, title=window.title,
            left=window.left, top=window.top, right=window.right, bottom=window.bottom,
        )
        self.window = restored
        return restored

    def click(self, x: int, y: int) -> None:
        self.calls.append(("click", x, y))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def press_keys(self, keys: str) -> None:
        self.calls.append(("keys", keys))

    def release_modifiers(self) -> None:
        self.releases += 1


def _tree(nodes: tuple[UiNode, ...] = (), **kwargs) -> UiTree:
    return UiTree(
        window_role=kwargs.get("window_role", "WindowControl"),
        window_title=kwargs.get("window_title", "记事本"),
        nodes=nodes,
        truncated=kwargs.get("truncated", False),
        skipped=kwargs.get("skipped", 0),
        elapsed_ms=1,
        # 编号由节点推导（生产路径也是这样），测试因此能覆盖"读树 → 点编号"整条链路。
        targets=screen_uia.targets_from(nodes),
        window_handle=kwargs.get("window_handle", WINDOW.handle),
    )


# ---- 登记：只读门、能力表、默认值 -------------------------------------------


def test_tools_are_registered_in_the_right_gates() -> None:
    """五个工具必须各自落到正确的门里：漏一个就会被安全门拒或注册表构造失败。"""
    for name in ("screenshot", "read_ui"):
        assert name in READ_ONLY_TOOLS
        assert name in KNOWN_TOOLS
    for name in ("click_screen", "click_element", "type_text", "press_keys"):
        assert name in SCREEN_INPUT_TOOLS
        assert name in KNOWN_TOOLS
        assert name not in READ_ONLY_TOOLS
    assert "focus_window" in SCREEN_FOCUS_TOOLS
    assert "focus_window" in KNOWN_TOOLS
    assert CAPABILITY_TOOLS["screen_read"] == ("screenshot", "read_ui")
    assert CAPABILITY_TOOLS["screen_control"] == (
        "click_screen", "click_element", "type_text", "press_keys", "focus_window",
    )
    # 出厂默认关：新能力不改变默认前缀（架构 §16.1）。
    assert CAPABILITY_DEFAULTS["screen_read"] is False
    assert CAPABILITY_DEFAULTS["screen_control"] is False
    # 不进任何内置档位：否则升级后默认工具集就变了。
    for tools in PROFILES.values():
        assert "screen_read" not in tools and "screen_control" not in tools


# ---- 截屏 -------------------------------------------------------------------


def test_vision_unavailable_refuses_without_capturing(policy: SecurityPolicy, tmp_path: Path) -> None:
    """收不了图就不截——截了也看不到，还白拍一次用户屏幕。"""
    called: list[int] = []

    def fake_capture() -> Capture:
        called.append(1)
        return FAKE

    tool = _tool(policy, vision_available=False, cache_dir=tmp_path, capture=fake_capture)
    result = tool.invoke({})
    assert isinstance(result, str)
    assert "没有声明支持图像输入" in result
    assert called == []
    assert list(tmp_path.glob("screen-*.jpg")) == []


def test_success_returns_text_and_image_blocks(policy: SecurityPolicy, tmp_path: Path) -> None:
    tool = _tool(policy, cache_dir=tmp_path, capture=lambda: FAKE)
    # 关掉编号：这条只验"图片内容块"的形状，不该去读真实桌面的控件树。
    result = tool.invoke({"labels": False})
    assert isinstance(result, ToolMessage)
    kinds = [block["type"] for block in result.content]
    assert kinds == ["text", "image_url"]
    assert "800×600" in result.content[0]["text"]
    assert result.content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    saved = list(tmp_path.glob("screen-*.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == FAKE.data


def test_platform_unavailable_is_explained(policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screen_capture, "available", lambda: False)
    tool = _tool(policy, capture=lambda: FAKE)
    result = tool.invoke({})
    assert isinstance(result, str)
    assert "只在 Windows 上实现" in result


def test_capture_failure_is_explained_not_raised(policy: SecurityPolicy) -> None:
    def boom() -> Capture:
        raise CaptureUnavailable("当前没有前台窗口")

    tool = _tool(policy, capture=boom)
    result = tool.invoke({})
    assert isinstance(result, str)
    assert "没截到" in result and "前台窗口" in result


def test_cache_keeps_only_the_newest(tmp_path: Path) -> None:
    for index in range(25):
        path = tmp_path / f"screen-{index:04d}.jpg"
        path.write_bytes(b"x")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    screen_capture.sweep(tmp_path, limit=20)
    left = sorted(item.name for item in tmp_path.glob("screen-*.jpg"))
    assert len(left) == 20
    assert left[0] == "screen-0005.jpg"


def test_clear_removes_screens_only(tmp_path: Path) -> None:
    (tmp_path / "screen-0001.jpg").write_bytes(b"x")
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    screen_capture.clear(tmp_path)
    assert not (tmp_path / "screen-0001.jpg").exists()
    assert (tmp_path / "keep.txt").exists()


# ---- 截图叠加编号（som）------------------------------------------------------


def _jpeg(size: tuple[int, int] = (200, 100)) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _labeled_capture(handle: int = WINDOW.handle) -> Capture:
    return Capture(
        _jpeg(), "image/jpeg", 200, 100, "记事本", rect=(0, 0, 200, 100), window_handle=handle
    )


def test_screenshot_with_labels_draws_numbers_and_lists_targets(
    policy: SecurityPolicy, tmp_path: Path
) -> None:
    shot = _labeled_capture()
    tool = _tool(
        policy, cache_dir=tmp_path, capture=lambda: shot,
        read_tree=lambda **_: _tree((PANE, BUTTON)),
    )
    result = tool.invoke({})
    text = result.content[0]["text"]
    assert "叠加了 1 个编号" in text
    assert "#1 ButtonControl 「保存」" in text
    # 图上确实改过：base64 与原始图不同。
    assert result.content[1]["image_url"]["url"] != shot.data_url


def test_labels_skipped_when_window_changed(policy: SecurityPolicy, tmp_path: Path) -> None:
    """截图与控件树不是同一个窗口时宁可不编号——错号比没号更危险。"""
    shot = _labeled_capture(handle=777)
    tool = _tool(
        policy, cache_dir=tmp_path, capture=lambda: shot,
        read_tree=lambda **_: _tree((PANE, BUTTON)),
    )
    text = tool.invoke({}).content[0]["text"]
    assert "叠加" not in text


def test_annotate_paints_red_on_the_image() -> None:
    from io import BytesIO

    from PIL import Image

    shot = _labeled_capture()
    painted = screen_capture.annotate(shot, [(1, 10, 20, 40, 30)])
    image = Image.open(BytesIO(painted)).convert("RGB")
    raw = image.tobytes()
    reds = sum(1 for offset in range(0, len(raw), 3) if raw[offset] > 180 and raw[offset + 1] < 90)
    assert reds > 100, "编号方块应该真的画上去"


# ---- 读控件树 ---------------------------------------------------------------


def test_read_ui_renders_indented_tree(policy: SecurityPolicy) -> None:
    nodes = (
        UiNode(0, "WindowControl", "记事本", 10, 20, 800, 600, True),
        UiNode(1, "EditControl", "正文", 12, 60, 780, 500, True),
        UiNode(1, "ButtonControl", "保存", 12, 570, 80, 24, False),
    )
    tool = _ui_tool(policy, read_tree=lambda **_: _tree(nodes))
    text = tool.invoke({"max_depth": 4})
    assert "窗口：WindowControl 「记事本」" in text
    assert "  EditControl 「正文」 [12,60 780x500]" in text
    assert "(禁用)" in text


def test_read_ui_reports_truncation_and_skipped(policy: SecurityPolicy) -> None:
    tool = _ui_tool(policy, read_tree=lambda **_: _tree(
        (UiNode(0, "WindowControl", "记事本", 0, 0, 10, 10, True),),
        truncated=True, skipped=2,
    ))
    text = tool.invoke({})
    assert "已截断" in text
    assert "2 处子控件读取失败" in text


def test_read_ui_says_empty_tree_instead_of_nothing(policy: SecurityPolicy) -> None:
    """空树必须解释原因：否则模型会把"没有控件"读成"窗口是空的"。"""
    tool = _ui_tool(policy, read_tree=lambda **_: _tree(()))
    text = tool.invoke({})
    assert "这棵树是空的" in text
    assert "截图" in text


def test_read_ui_unavailable_backend_is_explained(
    policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(screen_uia, "available", lambda: False)
    tool = _ui_tool(policy, read_tree=lambda **_: _tree(()))
    text = tool.invoke({})
    assert "uiautomation" in text


def test_read_ui_failure_is_explained_not_raised(policy: SecurityPolicy) -> None:
    def boom(**_: object) -> UiTree:
        raise RuntimeError("当前没有前台窗口")

    tool = _ui_tool(policy, read_tree=boom)
    text = tool.invoke({})
    assert isinstance(text, str)
    assert "读不到控件树" in text and "前台窗口" in text


def test_read_ui_clamps_depth(policy: SecurityPolicy) -> None:
    seen: list[int] = []

    def spy(**kwargs: object) -> UiTree:
        seen.append(int(kwargs["max_depth"]))
        return _tree(())

    tool = _ui_tool(policy, read_tree=spy)
    tool.invoke({"max_depth": 99})
    tool.invoke({"max_depth": -5})
    assert seen == [6, 1]


def test_clean_strips_invisible_and_newlines() -> None:
    assert screen_uia.clean("保存\u200e\n\t文件") == "保存 文件"


class _Rect:
    def __init__(self, left: int, top: int, width: int, height: int) -> None:
        self.left, self.top = left, top
        self._width, self._height = width, height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


class _Control:
    """UIA 控件的最小替身：只实现 walk 会用到的四个属性。"""

    def __init__(self, role: str, name: str, children: list[_Control] | None = None) -> None:
        self.ControlTypeName = role
        self.Name = name
        self.BoundingRectangle = _Rect(0, 0, 10, 10)
        self.IsEnabled = True
        self._children = children or []

    def GetChildren(self) -> list[_Control]:
        return self._children


def test_walk_respects_depth_limit() -> None:
    leaf = _Control("ButtonControl", "深层按钮")
    mid = _Control("PaneControl", "中层", [leaf])
    root = _Control("WindowControl", "根", [mid])
    nodes, truncated, skipped = screen_uia.walk(root, max_depth=1, max_nodes=100)
    assert [node.name for node in nodes] == ["根", "中层"]
    assert truncated is False and skipped == 0


def test_walk_stops_at_node_cap_and_counts_bad_subtrees() -> None:
    children = [_Control("ButtonControl", f"按钮{index}") for index in range(10)]
    root = _Control("WindowControl", "根", children)
    nodes, truncated, _ = screen_uia.walk(root, max_depth=2, max_nodes=4)
    assert len(nodes) == 4
    assert truncated is True

    class _Broken(_Control):
        def GetChildren(self) -> list[_Control]:
            raise RuntimeError("元素已失效")

    _, _, skipped = screen_uia.walk(_Broken("WindowControl", "坏窗口"), max_depth=2, max_nodes=10)
    assert skipped == 1


# ---- 模拟输入：审批、边界、收尾 ---------------------------------------------


def test_first_screen_action_requires_approval(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    tool = _build(policy, vision_available=False, input_backend=fake)[2]
    with pytest.raises(SecurityError) as error:
        tool.invoke({"x": 10, "y": 10})
    assert "人工批准" in str(error.value)
    assert fake.calls == []


def test_granted_action_runs_once_then_turn_is_exempt(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    tool = _build(policy, vision_available=False, input_backend=fake)[2]
    arguments = {"x": 10, "y": 10}
    policy.grant(action_fingerprint("click_screen", arguments, policy=policy))

    assert "点击了 (10,10)" in tool.invoke(arguments)
    assert fake.calls == [("click", 10, 10)]
    assert fake.releases == 1, "每次动作结束都要抬起修饰键"

    # 批准已被记住：第二次不带 grant 也能执行（用户裁定"一次授权"）。
    assert "点击了 (20,20)" in tool.invoke({"x": 20, "y": 20})
    assert fake.calls[-1] == ("click", 20, 20)


def test_click_outside_window_is_refused_but_modifiers_released(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    tool = _build(policy, vision_available=False, input_backend=fake)[2]
    policy.grant(action_fingerprint("click_screen", {"x": 5000, "y": 5000}, policy=policy))
    with pytest.raises(SecurityError) as error:
        tool.invoke({"x": 5000, "y": 5000})
    assert "不在前台窗口" in str(error.value)
    assert fake.calls == []
    assert fake.releases == 1, "拒绝路径也必须抬起修饰键，否则键盘会卡住"


def test_switching_window_revokes_the_turn_grant(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    tool = _build(policy, vision_available=False, input_backend=fake)[2]
    first = {"x": 10, "y": 10}
    policy.grant(action_fingerprint("click_screen", first, policy=policy))
    tool.invoke(first)

    fake.window = WindowRef(handle=202, title="另一个窗口", left=0, top=0, right=500, bottom=500)
    text = tool.invoke({"x": 30, "y": 30})
    assert "已经换成" in text
    assert fake.calls == [("click", 10, 10)], "换窗口后不得继续点"
    # 授权被撤销：下一次动作重新需要批准。
    assert policy.classify_tool("click_screen", {"x": 30, "y": 30}).requires_approval is True


def test_type_text_limits_and_passthrough(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    typing = _build(policy, vision_available=False, input_backend=fake)[4]
    too_long = "字" * (MAX_TEXT_CHARS + 1)
    assert "超过单次上限" in typing.invoke({"text": too_long})
    assert fake.calls == []

    policy.grant(action_fingerprint("type_text", {"text": "你好"}, policy=policy))
    assert "输入了 2 个字符" in typing.invoke({"text": "你好"})
    assert fake.calls == [("type", "你好")]


def test_press_keys_passes_combo_through(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    keys = _build(policy, vision_available=False, input_backend=fake)[5]
    policy.grant(action_fingerprint("press_keys", {"keys": "ctrl+s"}, policy=policy))
    assert "ctrl+s" in keys.invoke({"keys": "ctrl+s"})
    assert fake.calls == [("keys", "ctrl+s")]


def test_action_cap_stops_the_loop(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    tool = _build(policy, vision_available=False, input_backend=fake)[2]
    policy.grant(action_fingerprint("click_screen", {"x": 1, "y": 1}, policy=policy))
    policy.note_screen_action_approved()
    for index in range(MAX_ACTIONS_PER_TURN):
        tool.invoke({"x": index, "y": index})
    assert "达到上限" in tool.invoke({"x": 1, "y": 1})
    assert len(fake.calls) == MAX_ACTIONS_PER_TURN


def test_input_unavailable_is_explained(policy: SecurityPolicy) -> None:
    fake = _FakeInputs(available=False)
    tool = _build(policy, vision_available=False, input_backend=fake)[2]
    text = tool.invoke({"x": 1, "y": 1})
    assert "uiautomation" in text


def test_no_foreground_window_is_explained(policy: SecurityPolicy) -> None:
    tool = _build(policy, vision_available=False, input_backend=_FakeInputs(window=None))[2]
    text = tool.invoke({"x": 1, "y": 1})
    assert "没有前台窗口" in text


# ---- 指定窗口（后台也能读 / 截）---------------------------------------------


def test_read_ui_can_target_a_background_window(policy: SecurityPolicy) -> None:
    target = WindowRef(handle=909, title="后台窗口 - 记事本", left=0, top=0, right=300, bottom=200)
    fake = _FakeInputs()
    fake.windows = [target]
    seen: dict = {}

    def tree(**kwargs: object) -> UiTree:
        seen.update(kwargs)
        return _tree((PANE, BUTTON), window_title=target.title, window_handle=target.handle)

    text = _ui_tool(policy, read_tree=tree, input_backend=fake).invoke({"window": "后台"})
    assert "后台窗口" in text
    assert seen.get("handle") == 909, "指定窗口时必须按句柄读，而不是读前台"


def test_read_ui_reports_ambiguous_window(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    fake.windows = [
        WINDOW,
        WindowRef(handle=303, title="记事本 2", left=0, top=0, right=100, bottom=100),
    ]
    text = _ui_tool(policy, read_tree=lambda **_: _tree(()), input_backend=fake).invoke(
        {"window": "记事本"}
    )
    assert "有 2 个" in text and "更精确" in text


def test_screenshot_can_target_a_background_window(
    policy: SecurityPolicy, tmp_path: Path
) -> None:
    target = WindowRef(handle=909, title="后台窗口", left=0, top=0, right=200, bottom=100)
    fake = _FakeInputs()
    fake.windows = [target]
    tool = _tool(
        policy,
        cache_dir=tmp_path,
        capture_window=lambda handle, title="": _labeled_capture(handle=handle),
        read_tree=lambda **_: _tree((PANE, BUTTON), window_handle=909),
        input_backend=fake,
    )
    result = tool.invoke({"window": "后台"})
    text = result.content[0]["text"]
    assert "叠加了 1 个编号" in text


# ---- 编号点击（click_element）-----------------------------------------------


BUTTON = UiNode(1, "ButtonControl", "保存", 10, 20, 100, 40, True)
PANE = UiNode(0, "PaneControl", "容器", 0, 0, 900, 700, True)


def test_read_ui_numbers_only_interactive_elements(policy: SecurityPolicy) -> None:
    tool = _ui_tool(policy, read_tree=lambda **_: _tree((PANE, BUTTON)))
    text = tool.invoke({})
    assert "#1 ButtonControl 「保存」 中心(60,40)" in text
    # 容器不编号：否则模型会点到没有反应的空白区。
    assert "PaneControl" in text
    assert "#2" not in text


def test_click_element_requires_a_fresh_read(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    tool = _build(policy, vision_available=False, input_backend=fake)[3]
    text = tool.invoke({"index": 9})
    assert "不在最近一次读数里" in text and "read_ui" in text
    assert fake.calls == []


def test_click_element_clicks_the_control_center(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    tools = _build(policy, vision_available=False, input_backend=fake,
                   read_tree=lambda **_: _tree((PANE, BUTTON)))
    read_ui, click_element = tools[1], tools[3]
    read_ui.invoke({})

    arguments = {"index": 1}
    policy.grant(action_fingerprint("click_element", arguments, policy=policy))
    text = click_element.invoke(arguments)
    assert "#1" in text and "保存" in text
    assert fake.calls == [("click", 60, 40)]


def test_click_element_refuses_after_window_changed(policy: SecurityPolicy) -> None:
    """编号只对读树时那个窗口有效：换窗口后同一个编号可能指向别的控件。"""
    fake = _FakeInputs()
    tools = _build(policy, vision_available=False, input_backend=fake,
                   read_tree=lambda **_: _tree((PANE, BUTTON)))
    tools[1].invoke({"max_depth": 3})
    fake.window = WindowRef(handle=999, title="别的窗口", left=0, top=0, right=500, bottom=500)

    text = tools[3].invoke({"index": 1})
    assert "重新 read_ui" in text
    assert fake.calls == []


# ---- 切前台（focus_window）--------------------------------------------------


def test_focus_always_needs_approval_even_after_screen_grant(policy: SecurityPolicy) -> None:
    """切前台不并进"本轮屏幕操作授权"：它是会打断用户的动作，每次都要批。"""
    policy.note_screen_action_approved()
    assert policy.classify_tool("click_screen", {"x": 1, "y": 1}).requires_approval is False
    assert policy.classify_tool("focus_window", {"title": "记事本"}).requires_approval is True

    fake = _FakeInputs()
    tool = _build(policy, vision_available=False, input_backend=fake)[6]
    with pytest.raises(SecurityError) as error:
        tool.invoke({"title": "记事本"})
    assert "人工批准" in str(error.value)
    assert fake.calls == []


def test_focus_reports_missing_and_ambiguous_titles(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    tool = _build(policy, vision_available=False, input_backend=fake)[6]
    assert "没找到" in tool.invoke({"title": "不存在的窗口"})

    fake.windows = [
        WINDOW,
        WindowRef(handle=303, title="记事本 2", left=0, top=0, right=100, bottom=100),
    ]
    ambiguous = tool.invoke({"title": "记事本"})
    assert "有 2 个" in ambiguous and "更精确" in ambiguous
    assert fake.calls == []


def test_focus_success_switches_window_and_resets_grant(policy: SecurityPolicy) -> None:
    target = WindowRef(handle=404, title="目标窗口 - Chrome", left=0, top=0, right=900, bottom=700)
    fake = _FakeInputs()
    fake.windows = [target]
    tool = _build(policy, vision_available=False, input_backend=fake)[6]
    arguments = {"title": "Chrome"}
    policy.grant(action_fingerprint("focus_window", arguments, policy=policy))

    text = tool.invoke(arguments)
    assert "切到前台" in text and "404" in text
    assert fake.calls == [("focus", 404)]
    # 授权随窗口变更作废：下一次动作要重新批准。
    assert policy.classify_tool("click_screen", {"x": 5, "y": 5}).requires_approval is True


def test_focus_mentions_restored_minimized_window(policy: SecurityPolicy) -> None:
    """最小化窗口也要能被聚焦：矩形取"恢复后位置"，并在结果里说明。"""
    shrunk = WindowRef(
        handle=505, title="被最小化的窗口", left=0, top=0, right=800, bottom=600, minimized=True,
    )
    fake = _FakeInputs()
    fake.windows = [shrunk]
    tool = _build(policy, vision_available=False, input_backend=fake)[6]
    arguments = {"title": "最小化"}
    policy.grant(action_fingerprint("focus_window", arguments, policy=policy))
    text = tool.invoke(arguments)
    assert "切到前台" in text and "最小化" in text


def test_focus_failure_is_explained(policy: SecurityPolicy) -> None:
    fake = _FakeInputs()
    fake.focus_ok = False
    tool = _build(policy, vision_available=False, input_backend=fake)[6]
    arguments = {"title": "记事本"}
    policy.grant(action_fingerprint("focus_window", arguments, policy=policy))
    text = tool.invoke(arguments)
    assert "前台锁" in text and "手动" in text

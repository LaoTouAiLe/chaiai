"""`browser(action="screenshot", vision=True)`：把当前视口交给模型看（2026-11-02）。

为什么要有这条通道：`snapshot` 给的是 DOM 文本与控件 ref，"点了没反应""排版错位""这张图读数
是多少"这类问题只有**看见像素**才答得准。

七条契约：

1. 收得了图 → 返回带 `image_url` 内容块的 ``ToolMessage``，**不落盘**；
2. 收不了图 → **不去截图**，直接说明原因并给文本替代手段（截了也看不到，白花一次 CDP 往返）；
3. `vision` 与 `path` 二选一（同时给会被静默忽略，而"我以为留了档"是最难查的那种）；
4. **单张必须进得了 256 KB 预算**：超了就按 `clip.scale` 降采样 + JPEG 质量梯度，因为这张图
   会内联进 State、被每个 Checkpoint 复制一份（实测本机库 1757 个 channel blob 合计才 0.4 MB）；
5. **长边压到 1568 以内**：字节够小也要压——端点本来就会缩到这个量级，多余的像素只付线上字节；
6. **压不下去就如实说**，不截断、不假装成功；
7. 旧行为不变：不传 `vision` 时仍是"落盘 + 返回路径"。
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from local_agent.config import AppConfig
from local_agent.security.policy import SecurityPolicy
from local_agent.tools import browser_tools
from local_agent.tools.runtime_sessions import RESOURCES, permission_key, scope_key

#: 最小合法 PNG（魔数 + 填充）；`detect_image_media_type` 只看魔数。
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
#: 最小合法 JPEG（同样只看魔数）。
_JPEG = b"\xff\xd8\xff" + b"0" * 64

_BUDGET = browser_tools._VISION_MAX_BASE64_BYTES


class FakeSession:
    """替身：按 CDP 参数**模拟**输出体积，不启动任何进程。"""

    capability = "page_control"

    def __init__(
        self,
        policy: SecurityPolicy,
        *,
        png: bytes | None = None,
        jpeg_bytes: int = len(_JPEG),
        jpeg_fixed: bool = False,
        viewport: tuple[int, int] = (1440, 900),
        scroll: tuple[int, int] = (0, 0),
    ) -> None:
        self.permission_key = permission_key(policy)
        self.lock = threading.RLock()
        self.active = "tab-1"
        self.downloads: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.png = png or _PNG
        self.jpeg_bytes = jpeg_bytes
        #: 无论怎么降采样都这么大（模拟"压不进去"）。
        self.jpeg_fixed = jpeg_fixed
        self.viewport = viewport
        self.scroll = scroll
        self.closed = False

    def page(self, tab_id: str | None = None) -> str:
        return "session-1"

    def call(self, method: str, params: Any = None, *, session: Any = None,
             timeout: float = 1.0) -> dict[str, Any]:
        self.calls.append(method)
        self.params.append(dict(params or {}))
        if method != "Page.captureScreenshot":
            return {}
        spec = dict(params or {})
        if spec.get("format") == "png":
            return {"data": base64.b64encode(self.png).decode("ascii")}
        scale = float((spec.get("clip") or {}).get("scale", 1.0))
        size = self.jpeg_bytes if self.jpeg_fixed else max(1, int(self.jpeg_bytes * scale * scale))
        return {"data": base64.b64encode(_JPEG + b"0" * max(0, size - len(_JPEG))).decode("ascii")}

    def call_page(self, method: str, params: Any = None, *, timeout: float = 1.0) -> dict[str, Any]:
        # 工具现在统一走 `call_page`（失效重挂，见 test_browser_stale_session.py）；
        # 替身不需要重挂，直接转发。
        return self.call(method, params, session=self.page(), timeout=timeout)

    def evaluate(self, expression: str, *, timeout: float = 1.0) -> Any:
        return {
            "href": "https://example.com/page",
            "w": self.viewport[0], "h": self.viewport[1],
            "sx": self.scroll[0], "sy": self.scroll[1],
        }

    def snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"url": "https://example.com/page", "controls": []}

    def close(self) -> None:
        self.closed = True


def _policy(tmp_path: Path) -> SecurityPolicy:
    return SecurityPolicy.from_config(AppConfig(), workspace_root=tmp_path, environ={})


@pytest.fixture()
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeSession]:
    """注册一个替身会话；`isinstance` 检查走模块全局名，所以连类一起换掉。"""
    policy = _policy(tmp_path)
    fake = FakeSession(policy)
    monkeypatch.setattr(browser_tools, "BrowserSession", FakeSession)
    RESOURCES.add(policy, "browser", fake)
    try:
        yield policy, fake
    finally:
        RESOURCES.close_scope(scope_key(policy))


def _invoke(policy: Any, fake: FakeSession, **arguments: Any) -> ToolMessage:
    tool = browser_tools.build_browser_tools(policy, vision_available=True)[0]
    result = tool.invoke({"action": "screenshot", "vision": True, **arguments})
    assert isinstance(result, ToolMessage)
    return result


def _payload(result: ToolMessage) -> dict[str, Any]:
    return json.loads(result.content[0]["text"])


def test_small_screenshot_goes_through_as_png(
    session: tuple[Any, FakeSession], tmp_path: Path
) -> None:
    policy, fake = session

    result = _invoke(policy, fake)

    payload = _payload(result)
    assert payload["url"] == "https://example.com/page"
    assert payload["format"] == "image/png"
    assert "jpeg_quality" not in payload
    assert result.content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # 不落盘：截图只进上下文。
    assert not list(tmp_path.rglob("*.png"))
    # 预算内不该有多余的往返。
    assert fake.calls == ["Page.captureScreenshot"]


def test_big_screenshot_is_downscaled_into_the_budget(
    session: tuple[Any, FakeSession], tmp_path: Path
) -> None:
    """一张 2 MB 的 PNG 必须被压到 256 KB 以内，否则它会跟着每个 Checkpoint 复制一份。"""
    policy = _policy(tmp_path)
    fake = FakeSession(policy, png=b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024),
                       jpeg_bytes=2 * 1024 * 1024)
    browser_tools.RESOURCES.add(policy, "browser", fake)
    try:
        result = _invoke(policy, fake)

        payload = _payload(result)
        image = result.content[1]["image_url"]["url"]
        assert image.startswith("data:image/jpeg;base64,")
        assert len(image) - len("data:image/jpeg;base64,") <= _BUDGET
        assert payload["format"] == "image/jpeg"
        assert payload["jpeg_quality"] in browser_tools._VISION_QUALITY_LADDER
        # 真的降了采样，且小于原视口。
        assert payload["width"] < 1440
        assert all("clip" in spec for spec in fake.params if spec.get("format") == "jpeg")
    finally:
        browser_tools.RESOURCES.close_scope(scope_key(policy))


def test_long_edge_is_capped_even_when_bytes_fit(
    session: tuple[Any, FakeSession],
) -> None:
    """字节够小也要压长边：端点本来就会缩到 1568，多余像素只付线上字节。"""
    policy, fake = session
    fake.viewport = (3000, 2000)

    payload = _payload(_invoke(policy, fake))

    assert payload["format"] == "image/jpeg"
    assert max(payload["width"], payload["height"]) <= browser_tools._VISION_MAX_EDGE + 1


def test_scrolled_page_captures_the_visible_part(
    session: tuple[Any, FakeSession], tmp_path: Path
) -> None:
    """`clip` 用**文档坐标**：滚动之后还用 0,0 会截到页面顶部，而模型问的是眼前这一屏。"""
    policy = _policy(tmp_path)
    fake = FakeSession(policy, png=b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024),
                       jpeg_bytes=600_000, scroll=(0, 1500))
    browser_tools.RESOURCES.add(policy, "browser", fake)
    try:
        _invoke(policy, fake)

        clips = [spec["clip"] for spec in fake.params if spec.get("format") == "jpeg"]
        assert clips and all(clip["y"] == 1500 for clip in clips)
    finally:
        browser_tools.RESOURCES.close_scope(scope_key(policy))


def test_unfittable_screenshot_is_reported_not_truncated(
    session: tuple[Any, FakeSession], tmp_path: Path
) -> None:
    """降到下限仍超预算：如实标注，不静默截断（半个图只会给出错误的视觉结论）。"""
    policy = _policy(tmp_path)
    fake = FakeSession(policy, png=b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024),
                       jpeg_bytes=4 * 1024 * 1024, jpeg_fixed=True)
    browser_tools.RESOURCES.add(policy, "browser", fake)
    try:
        result = _invoke(policy, fake)

        assert "仍超出" in _payload(result)["note"]
        # 图还是给了：读一张糊图也比让模型对着空白猜强。
        assert result.content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    finally:
        browser_tools.RESOURCES.close_scope(scope_key(policy))


def test_without_vision_support_it_does_not_even_capture(
    session: tuple[Any, FakeSession]
) -> None:
    policy, fake = session
    tool = browser_tools.build_browser_tools(policy, vision_available=False)[0]

    result = tool.invoke({"action": "screenshot", "vision": True})

    assert isinstance(result, ToolMessage)
    assert "没有声明支持图像输入" in str(result.content)
    # 关键：不截图（截了也看不到，白花一次 CDP 往返）。
    assert "Page.captureScreenshot" not in fake.calls


def test_vision_and_path_are_mutually_exclusive(
    session: tuple[Any, FakeSession]
) -> None:
    policy, _ = session
    tool = browser_tools.build_browser_tools(policy, vision_available=True)[0]

    with pytest.raises(ValueError, match="二选一"):
        tool.invoke({"action": "screenshot", "vision": True, "path": "shot.png"})


def test_default_screenshot_still_writes_a_file(
    session: tuple[Any, FakeSession], tmp_path: Path
) -> None:
    policy, _ = session
    tool = browser_tools.build_browser_tools(policy, vision_available=True)[0]

    payload = json.loads(tool.invoke({"action": "screenshot"}))

    assert Path(payload["path"]).is_file()

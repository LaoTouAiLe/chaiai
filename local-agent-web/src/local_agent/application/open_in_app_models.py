"""右栏「打开方式」的视图（2026-11-03）。

只描述"有哪些应用可用、这一下开没开成"；应用目录与探测在
:mod:`local_agent.desktop_apps`（宿主能力），路径边界在应用服务。
"""

from __future__ import annotations

from pydantic import Field

from local_agent.application.data_center_models import DataView


class OpenInAppItemView(DataView):
    id: str
    label: str
    #: 图标地址；取不到（抽图失败）时为 `None`，前端画首字母块。
    icon_url: str | None = None


class OpenInAppView(DataView):
    """本机可用的应用清单。

    `available=False` **不是错误**：这台机器就是没有可打开的程序（或平台不支持），
    前端据此把「打开方式」按钮藏掉，而不是弹一个报错。
    """

    available: bool
    apps: list[OpenInAppItemView] = Field(default_factory=list)
    reason: str | None = None


class OpenInAppResultView(DataView):
    """一次打开的结果：`app_label` 用于前端提示"已用 X 打开"。"""

    ok: bool = True
    app_id: str
    app_label: str

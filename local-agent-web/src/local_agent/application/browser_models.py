"""右侧浏览器的有限导航与输入契约，不暴露任意 CDP 或 JavaScript。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BrowserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    workspace_id: str | None = None


class BrowserView(BaseModel):
    browser_id: str
    url: str = "about:blank"
    title: str = "新标签页"
    loading: bool = False
    can_back: bool = False
    can_forward: bool = False
    error: str | None = None
    dialog: dict[str, str] | None = None


class BrowserCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    action: Literal["navigate", "back", "forward", "reload", "stop", "resize",
                    "pointer", "wheel", "key", "text", "dialog"]
    url: str = Field(default="", max_length=4096)
    width: int = Field(default=520, ge=240, le=1920)
    height: int = Field(default=700, ge=200, le=1400)
    x: float = Field(default=0, ge=0, le=1920)
    y: float = Field(default=0, ge=0, le=1400)
    event: Literal["down", "up", "move"] = "move"
    button: Literal["left", "right", "middle", "none"] = "none"
    buttons: int = Field(default=0, ge=0, le=7)
    clicks: int = Field(default=1, ge=1, le=3)
    delta_x: float = Field(default=0, ge=-10000, le=10000)
    delta_y: float = Field(default=0, ge=-10000, le=10000)
    key: str = Field(default="", max_length=64)
    code: str = Field(default="", max_length=64)
    key_code: int = Field(default=0, ge=0, le=255)
    modifiers: int = Field(default=0, ge=0, le=15)
    text: str = Field(default="", max_length=8192)
    accept: bool = False


class BrowserFrame(BaseModel):
    type: Literal["frame"] = "frame"
    data: str
    width: float
    height: float


class BrowserStateMessage(BrowserView):
    type: Literal["state"] = "state"

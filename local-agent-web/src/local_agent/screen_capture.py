"""截屏（2026-11-04）：把**前台窗口**抓成一张可以交给模型看的图。

为什么单独一个模块而不是塞进工具里：抓屏是平台代码（Win32 + Pillow），工具是协议代码
（LangChain 参数、内容块、审批口径）。分开之后，测试可以只替换 `grab` 一个接缝，不必在
没有桌面的环境里起一个真实窗口。

## 为什么不是自己写 GDI

2026-11-04 实测过纯 ctypes 的 `BitBlt` + 自研 PNG 编码：能抓（1823×797），但产出 593 KB，
比 Pillow 同尺寸 JPEG 大近 5 倍，而且多屏、DPI、GPU 合成窗口这些坑要自己踩。Pillow 已在
依赖里（`python-pptx` 带进来的，2026-11-04 实测 12.3.0），`ImageGrab.grab(bbox)` 抓同一窗口
**78 ms**，缩到 1600px 的 JPEG 只有 122 KB，base64 后约 163 KB —— 离那条"5 MB 直接 400"的
边界很远。所以这里用库，不自己造。

## 三条边界

1. **只抓前台窗口**：整屏会把任务栏、别的应用、通知一起拍进去；窗口矩形由
   `GetForegroundWindow` + `GetWindowRect` 给，抓不到就明确失败，不退回整屏。
2. **必须缩**：长边超过 :data:`MAX_EDGE` 时等比缩到 1600px，再按 :data:`JPEG_QUALITY` 编码。
   原图直发对模型没有增量信息，只会把上下文烧掉。
3. **只在 Windows 可用**：非 Windows 抛 :class:`CaptureUnavailable`，由工具层翻译成一句
   "这台机器上截不了屏"，不留一个静默返回空图的路径。
"""

from __future__ import annotations

import base64
import ctypes
import os
import tempfile
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 交给模型前把长边缩到这个值。1600px 在 2026-11-04 实测为 122 KB（JPEG q80）。
MAX_EDGE: int = 1600

#: JPEG 质量。80 是实测平衡点：再高体积翻倍，再低文字边缘开始糊。
JPEG_QUALITY: int = 80

#: 缓存目录里保留的截图份数。按数量滚动（与 hermes 一致），不设 TTL：进程退出时整体清空，
#: 所以"留 20 份"只影响运行期间，不会在磁盘上长期堆图。
CACHE_LIMIT: int = 20


class CaptureUnavailable(RuntimeError):
    """这台机器截不了屏（非 Windows、或没有前台窗口）。"""


@dataclass(frozen=True)
class Capture:
    """一次成功的抓屏。"""

    data: bytes
    media_type: str
    width: int
    height: int
    window_title: str
    #: 截图抓的**屏幕矩形**（``left, top, right, bottom``）。画编号覆盖层时用它把屏幕坐标
    #: 换算成图片像素；没有它就不知道缩放比例，编号会画偏。
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    #: 被截窗口的句柄。用来校验"编号来自同一个窗口"。
    window_handle: int = 0

    @property
    def data_url(self) -> str:
        """``data:image/jpeg;base64,…`` —— 直接塞进内容块给模型。"""
        payload = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{payload}"


def available() -> bool:
    """当前平台能不能截屏。调用方据此决定要不要把工具绑给模型。"""
    return _is_windows()


def _is_windows() -> bool:
    return os.name == "nt"


def ensure_dpi_aware() -> None:
    """把本进程标成 DPI 感知（幂等）。

    三个模块（截屏 / 读控件树 / 模拟输入）必须共用同一套屏幕坐标：只要有一个处在"不感知 DPI"
    的状态，另外两个的坐标就会在缩放 ≠100% 的机器上对不上（UIA 给逻辑像素、点击用物理像素）。
    进程级设置一次即可，所以统一放这里，由各模块的 `available()` 调用。

    2026-11-04 实测本机是 96 DPI、单显示器，所以这条在缩放场景下的正确性**没法在本机验证**；
    它的作用是消除一个明确的隐患，不是"已验证的修复"。
    """
    if _is_windows():
        ctypes.windll.user32.SetProcessDPIAware()


def _rect_and_title() -> tuple[tuple[int, int, int, int], str, int]:
    if not _is_windows():
        raise CaptureUnavailable("截屏目前只在 Windows 上实现")
    ensure_dpi_aware()
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise CaptureUnavailable("当前没有前台窗口")
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise CaptureUnavailable("取不到前台窗口的位置")
    if rect.right <= rect.left or rect.bottom <= rect.top:
        raise CaptureUnavailable("前台窗口没有可见区域")
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return (rect.left, rect.top, rect.right, rect.bottom), buffer.value, int(hwnd)


def _grab(bbox: tuple[int, int, int, int]) -> Any:
    """真实抓屏。单独一个函数是为了让测试替换它（不引 GUI）。

    返回类型写 ``Any`` 而不是 ``PIL.Image.Image``：`PIL` 只在真正截图时才导入，
    模块顶层引它会拖慢启动，也不该让"没装 Pillow"在 import 阶段就炸。
    """
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=bbox)


def capture_front_window(
    *,
    max_edge: int = MAX_EDGE,
    quality: int = JPEG_QUALITY,
    grab: Callable[[tuple[int, int, int, int]], Any] | None = None,
) -> Capture:
    """抓前台窗口，缩放到 ``max_edge`` 内并以 JPEG 编码。"""
    from io import BytesIO

    bbox, title, handle = _rect_and_title()
    image: Any = (grab or _grab)(bbox)
    if getattr(image, "mode", None) != "RGB":
        image = image.convert("RGB")
    if max(image.size) > max_edge:
        image = image.copy()
        image.thumbnail((max_edge, max_edge))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    width, height = image.size
    return Capture(
        buffer.getvalue(), "image/jpeg", width, height, title, rect=bbox, window_handle=handle
    )


def _bind_gdi() -> tuple[Any, Any]:
    """给 GDI/USER32 的句柄返回值声明 ``c_void_p``。

    2026-11-04 踩过：HWND/HDC/HBITMAP 在 64 位上超过 2^31，按默认的 c_int 解释会
    直接抛 `OverflowError: int too long to convert`。这几行是 PrintWindow 能用的前提。
    """
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
    user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
    ]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    return user32, gdi32


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


def _grab_window(handle: int) -> Any:
    """用 PrintWindow 把**指定窗口**画到位图（后台窗口也行），返回 PIL 图。"""
    from PIL import Image

    user32, gdi32 = _bind_gdi()
    rect = wintypes.RECT()
    if not user32.GetWindowRect(handle, ctypes.byref(rect)):
        raise CaptureUnavailable("取不到目标窗口的位置")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise CaptureUnavailable("目标窗口没有可见区域（可能已最小化）")
    hdc = user32.GetWindowDC(handle)
    memory = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    try:
        gdi32.SelectObject(memory, bitmap)
        # PW_RENDERFULLCONTENT：让 DirectComposition 渲染的窗口也能画出来。
        if not user32.PrintWindow(handle, memory, 0x00000002):
            raise CaptureUnavailable("这个窗口拒绝被截图（PrintWindow 返回失败）")
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        buffer = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(info), 0) != height:
            raise CaptureUnavailable("读不出窗口位图")
        return Image.frombuffer("RGB", (width, height), buffer.raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(handle, hdc)


def capture_window(
    handle: int,
    *,
    title: str = "",
    max_edge: int = MAX_EDGE,
    quality: int = JPEG_QUALITY,
    grab: Callable[[int], Any] | None = None,
) -> Capture:
    """抓**指定窗口**（不要求它在后台/前台），后台窗口同样能截。

    为什么值得单独一条路：抓前台窗口要抢焦点（打断用户），而"看一眼后台那个窗口长什么样"
    不需要打扰任何人。2026-11-04 实测：记事本在后台时 PrintWindow 正常出图（97.8% 非黑像素）。
    """
    from io import BytesIO

    ensure_dpi_aware()
    image: Any = (grab or _grab_window)(handle)
    if getattr(image, "mode", None) != "RGB":
        image = image.convert("RGB")
    if _looks_blank(image):
        raise CaptureUnavailable(
            "这个窗口截出来是黑/空白的：GPU 渲染的窗口常常这样。请改用前台截图。"
        )
    if max(image.size) > max_edge:
        image = image.copy()
        image.thumbnail((max_edge, max_edge))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    width, height = image.size
    rect = _window_rect(handle)
    return Capture(
        buffer.getvalue(), "image/jpeg", width, height, title or "",
        rect=rect, window_handle=handle,
    )


def _window_rect(handle: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(handle, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def _looks_blank(image: Any) -> bool:
    """全黑/全白的图说明 PrintWindow 没画出来；宁可报错也别把黑图交给模型。"""
    from PIL import ImageStat

    stat = ImageStat.Stat(image.convert("L"))
    return bool(stat.mean[0] < 2 or stat.stddev[0] < 1)


#: 编号覆盖层的底色（红）。白字压在红块上，在浅色与深色界面上都能看清。
_LABEL_FILL: tuple[int, int, int] = (220, 38, 38)


def annotate(
    capture: Capture,
    boxes: list[tuple[int, int, int, int, int]],
    *,
    fill: tuple[int, int, int] = _LABEL_FILL,
    quality: int = JPEG_QUALITY,
) -> bytes:
    """在截图上画编号方块，返回新的 JPEG 字节。

    ``boxes`` 是 ``(编号, 屏幕 x, 屏幕 y, 宽, 高)``；方块画在每个控件的**左上角**——
    画在中心会挡住控件本身的文字，模型反而不容易判断"这块区域是什么"。

    坐标换算靠 ``capture.rect``：图片是窗口区域缩放后的结果，比例由宽度比推出。
    """
    from io import BytesIO as _BytesIO

    from PIL import Image, ImageDraw, ImageFont

    if not capture.rect or capture.rect[2] <= capture.rect[0]:
        return capture.data
    image = Image.open(_BytesIO(capture.data)).convert("RGB")
    left, top, right, bottom = capture.rect
    scale_x = image.width / max(1, right - left)
    scale_y = image.height / max(1, bottom - top)
    draw = ImageDraw.Draw(image)
    # 方块边长按图片宽度取比例：2026-11-04 实测 14px 的数字在 1463px 的窗口截图上
    # 模型认不出来（回答为空、全花在推理上），放大到 1/40 宽才够。
    side = max(20, image.width // 40)
    try:
        font = ImageFont.load_default(size=int(side * 0.78))
    except TypeError:  # 老版本 Pillow 不支持 size 参数
        font = ImageFont.load_default()
    for index, x, y, width, height in boxes:
        if width <= 0 or height <= 0:
            continue
        cx = int((x - left) * scale_x)
        cy = int((y - top) * scale_y)
        if not (0 <= cx < image.width and 0 <= cy < image.height):
            continue
        draw.rectangle([cx, cy, cx + side, cy + side], fill=fill, outline=(255, 255, 255))
        draw.text((cx + side * 0.22, cy + side * 0.11), str(index), fill=(255, 255, 255), font=font)
    buffer = _BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def default_cache_dir() -> Path:
    """截图缓存目录：**系统临时目录**下，不在工作区、不在数据根。

    为什么不用数据根：截图是运行期缓存，不是用户资产；混进数据根会让备份与"删会话"都变重。
    """
    return Path(tempfile.gettempdir()) / "chaiai-screens"


def save(capture: Capture, directory: Path | None = None, *, limit: int = CACHE_LIMIT) -> Path:
    """落一份 JPEG 并滚动淘汰旧图；返回落盘路径。"""
    target_dir = directory or default_cache_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"screen-{int(_now() * 1000)}.jpg"
    path.write_bytes(capture.data)
    sweep(target_dir, limit=limit)
    return path


def sweep(directory: Path, *, limit: int = CACHE_LIMIT) -> None:
    """按修改时间只留最近 ``limit`` 份。"""
    shots = sorted(directory.glob("screen-*.jpg"), key=lambda item: item.stat().st_mtime)
    for stale in shots[:-limit] if len(shots) > limit else ():
        try:
            stale.unlink()
        except OSError:
            # 被占用就留到下一次；清理失败不该影响这次截图。
            continue


def clear(directory: Path | None = None) -> None:
    """清空缓存目录（进程退出用）。"""
    target = directory or default_cache_dir()
    if not target.is_dir():
        return
    for item in target.glob("screen-*.jpg"):
        try:
            item.unlink()
        except OSError:
            continue


def _now() -> float:
    import time

    return time.time()

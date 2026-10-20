"""图片的**格式判定**与**大小上限**（2026-10-16）。

为什么单开一个模块：这套规则有**两个**使用者——`tools/image_tools.py`（模型看本机图片）与
`attachments.py`（用户在对话框里上传图片）。两处各写一份必然分叉（"上传收 GIF、看图不收"
这类不一致最难查），所以判定与上限只留这里一份。

认格式**只按魔数**，绝不信扩展名：`hero.png` 完全可能是个 HTML 错误页，而把 HTML 当图片交给
模型比拒绝它更糟（模型会得到错误的视觉结论，还会以为"看过了"）。
"""

from __future__ import annotations

import base64
import struct

#: 单张图片的字节上限。**不截断**：截断的图片交给模型只会得到错误的视觉结论。
#: 不设应用层图片字节配额；实际处理能力取决于模型与可用资源。
MAX_IMAGE_BYTES: int | None = None

#: 对话框**上传附件**的字节上限（2026-10-20）。
#:
#: 与上面的 ``MAX_IMAGE_BYTES`` 刻意分开：附件会以 ``data:`` URL 进消息、随请求体发给模型，
#: 实测一张 3.9 MB 的截图 base64 后是 5.1 MB 的文本，DeepSeek 直接回 400
#: （``OpenAIInvalidRequestError``）；而 ``view_image`` 读本机图片不走网络请求，不该被同一条上限约束。
#: 页面在上传前会先把长边缩到 4096px 并压到 2 MB 以内，这里的 4 MiB 只是**绕过页面时的兜底**。
#: 端点自己的上限是单图 32 MiB / 单边 8192px，这里取更保守的一档。
MAX_UPLOAD_IMAGE_BYTES: int = 4 * 1024 * 1024

#: 魔数 → 媒体类型（顺序无关，但 RIFF 需要额外校验第 8–12 字节）。
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)

#: 媒体类型 → 落盘扩展名。**扩展名只是给人看的**（判定永远走魔数），所以它可以丢信息，
#: 但不能说谎：四类各给一个规范后缀。
EXTENSION_BY_MEDIA: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

#: 受支持格式的说明（拒绝时给用户看的那句）。
SUPPORTED_NOTE = "支持 PNG / JPEG / GIF / WebP"


def detect_image_media_type(data: bytes) -> str | None:
    """按**魔数**判断图片类型；不是受支持的图片时返回 ``None``。"""
    for magic, media_type in _MAGIC:
        if not data.startswith(magic):
            continue
        if media_type == "image/webp" and data[8:12] != b"WEBP":
            continue
        return media_type
    return None


def data_url(media_type: str, data: bytes) -> str:
    """``data:image/png;base64,…`` —— 直接塞进内容块交给模型。"""
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


#: 端点允许的图片单边上限（DeepSeek 文档：单边最长 8192px；单请求 ≥15 张时 4096px）。
#: 超过它整轮请求会被 400 拒绝，所以**交给模型之前**必须挡住。
MAX_IMAGE_EDGE: int = 8192


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """按文件头取 ``(宽, 高)``；判断不出来返回 ``None``（不猜，也不解整图）。

    只读头部：它的唯一用途是在把图交给模型前拦住"单边超限"的图，避免整轮 400。
    支持四种受支持格式：PNG / JPEG / GIF / WebP（与 :func:`detect_image_media_type` 同一集合）。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_dimensions(data)
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg_dimensions(data)
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 40:
        # 帧头：3 字节起始码 + 2 字节宽 + 2 字节高，各取低 14 位。
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (width, height) if width and height else None
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """扫 JPEG 段找 SOF（帧头）；限制扫描长度，坏文件不会把这里拖死。"""
    index = 2
    limit = min(len(data), 2_000_000)
    while index + 9 <= limit:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xFF:
            index += 1
            continue
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if length < 2:
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return (width, height) if width and height else None
        index += 2 + length
    return None

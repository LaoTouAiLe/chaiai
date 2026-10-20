"""对象存储上的附件（2026-10-20）：与本地后端同一组校验，只是把字节放到 S3 兼容存储。

口径与 :mod:`local_agent.attachments` 逐项对齐：

1. **按魔数认格式**（不信扩展名与客户端声明的类型）；
2. **超限拒绝而不是截断**（单图 4 MiB / 单边 8192px——端点自己也是这个量级）；
3. **引用即公开 URL**：State 里存 URL，出站请求直接把它交给模型，不再下载字节；
4. **本会话归属**：引用里带会话目录，读取时仍按会话前缀校验。

不做的事：不删除对象（会话删除只删本地文件）。对象生命周期交给桶策略——历史会话可能还引用着
它们，自动删除比留着更难收拾。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from local_agent.attachments import SavedAttachment
from local_agent.exceptions import AttachmentError
from local_agent.media import (
    EXTENSION_BY_MEDIA,
    MAX_IMAGE_EDGE,
    MAX_UPLOAD_IMAGE_BYTES,
    SUPPORTED_NOTE,
    detect_image_media_type,
    image_dimensions,
)
from local_agent.oss_storage import OssConfig, S3ObjectStore

#: 会话 id 的形状（与 `AttachmentStore._SESSION_RE` 同一口径）：它会被拼进对象键。
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

#: 1×1 PNG：只用于「测试对象存储」时上传一个真实可解码的图片（不写入任何用户数据）。
PROBE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(frozen=True)
class OssAttachmentStore:
    """上传到对象存储的附件后端。只实现上传；读取走公开 URL（页面 302）。"""

    config: OssConfig
    store: S3ObjectStore

    def save(
        self, session_id: str, data: bytes, *, now: datetime | None = None
    ) -> SavedAttachment:
        session = str(session_id).strip()
        if _SESSION_RE.match(session) is None:
            raise AttachmentError(f"会话 id 不合法：{session_id!r}")
        if len(data) == 0:
            raise AttachmentError("这是一张空文件（0 字节）：没有内容可看。")
        if len(data) > MAX_UPLOAD_IMAGE_BYTES:
            raise AttachmentError(
                f"图片太大（{len(data) // 1024} KB > 上限 {MAX_UPLOAD_IMAGE_BYTES // 1024} KB）："
                f"{SUPPORTED_NOTE}。请先缩小或压缩再上传。"
            )
        media_type = detect_image_media_type(data)
        if media_type is None:
            raise AttachmentError(
                f"这不是受支持的图片（按文件头判断）：{SUPPORTED_NOTE}。"
                "如果它是文本或 HTML，请直接把内容贴进消息里。"
            )
        size = image_dimensions(data)
        if size is not None and max(size) > MAX_IMAGE_EDGE:
            raise AttachmentError(
                f"这张图是 {size[0]}×{size[1]} 像素，单边超过端点上限 {MAX_IMAGE_EDGE}px："
                "请先缩小（长边压到上限以内）再上传。"
            )

        moment = now if now is not None else datetime.now().astimezone()
        digest = hashlib.sha256(data).hexdigest()[:16]
        key = str(PurePosixPath(
            f"{self.config.prefix()}{moment:%Y/%m/%d}/{session}/{digest}.{EXTENSION_BY_MEDIA[media_type]}"
        ))
        url = self.store.put(key, data, content_type=media_type)
        return SavedAttachment(
            attachment_id=url,
            media_type=media_type,
            byte_count=len(data),
            url=url,
        )

    def delete_session(self, session_id: str) -> int:
        """对象存储后端**不删对象**：只把决定权留给桶策略（历史会话可能仍引用）。

        返回 0 表示"没有删除任何对象"，与"删了 0 个"在语义上一致，调用方不需要区分。
        """
        del session_id
        return 0

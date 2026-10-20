"""对象存储附件后端（2026-10-20）：同一组校验，引用换成公开 URL。

钉两件事：① 上传成功后引用就是 URL（State 里不再有字节）；② 超限/错格式仍在**上传前**拒绝。
"""

from __future__ import annotations

import httpx
import pytest

from local_agent.attachment_oss import OssAttachmentStore
from local_agent.exceptions import AttachmentError
from local_agent.media import MAX_UPLOAD_IMAGE_BYTES
from local_agent.oss_storage import OssConfig, S3ObjectStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SESSION = "a" * 32


def _backend(status: int = 200) -> tuple[list[httpx.Request], OssAttachmentStore]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status)

    config = OssConfig(
        endpoint="https://s3.example.com",
        bucket="images",
        public_base_url="https://cdn.example.com",
        key_prefix="chaiai/",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return captured, OssAttachmentStore(config=config, store=S3ObjectStore(config, "AK", "SK", client=client))


def test_save_returns_a_public_url_reference() -> None:
    captured, backend = _backend()

    saved = backend.save(SESSION, PNG)

    assert saved.url is not None and saved.url.startswith("https://cdn.example.com/chaiai/")
    # 引用与 URL 是同一个值：State 只存这一个字符串。
    assert saved.attachment_id == saved.url
    assert saved.media_type == "image/png"
    assert saved.byte_count == len(PNG)
    assert len(captured) == 1


def test_rejects_an_oversized_image_before_uploading() -> None:
    captured, backend = _backend()
    too_big = PNG[:8] + b"x" * MAX_UPLOAD_IMAGE_BYTES

    with pytest.raises(AttachmentError, match="图片太大"):
        backend.save(SESSION, too_big)

    assert captured == [], "超限的图不该先传上去再失败"


def test_rejects_a_non_image_before_uploading() -> None:
    captured, backend = _backend()

    with pytest.raises(AttachmentError, match="不是受支持的图片"):
        backend.save(SESSION, b"<html>not an image</html>")

    assert captured == []


def test_delete_session_never_touches_objects() -> None:
    """对象存储后端不删对象：历史会话可能还引用着它们，生命周期交给桶策略。"""
    _captured, backend = _backend()

    assert backend.delete_session(SESSION) == 0

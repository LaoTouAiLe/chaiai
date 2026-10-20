"""上传附件（2026-10-16）：放哪、怎么认格式、边界在哪。

这条链路的每条边界都对应一个真会发生的场景：朋友把 `报告.pdf` 改名成 `报告.png` 上传、
有人贴一张 12 MB 的截图、一个引用被拼成 `../../` 去读别的会话的文件。用例按这些场景写。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

import pytest

from local_agent.attachments import (
    MAX_ATTACHMENTS_PER_DAY,
    MAX_ATTACHMENTS_PER_MESSAGE,
    AttachmentStore,
)
from local_agent.exceptions import AttachmentError
from local_agent.media import MAX_UPLOAD_IMAGE_BYTES

SESSION = "a" * 32
OTHER_SESSION = "b" * 32

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 32
#: 一个"改名成 png 的 HTML"——按扩展名认格式就会放它进去（bytes 里只能放 ASCII，所以正文用英文）。
HTML_NAMED_PNG = b"<!DOCTYPE html><html><body>not an image</body></html>"


def _store(tmp_path: Path) -> AttachmentStore:
    return AttachmentStore.under(tmp_path)


def _at(day: int) -> datetime:
    """当天 09:00（**按本机时区**）：存储层的日期分桶用的是本地日期，用 UTC 会让断言在东八区外漂移。"""
    return datetime(2026, 10, day, 9, 0, tzinfo=datetime.now().astimezone().tzinfo)


def test_layout_is_year_month_day_then_session(tmp_path: Path) -> None:
    """落盘位置：``data/attachments/年/月/日/会话/内容哈希.扩展名``（相对 config.yaml 所在目录）。"""
    store = _store(tmp_path)

    saved = store.save(SESSION, PNG, now=_at(16))

    digest = hashlib.sha256(PNG).hexdigest()[:16]
    assert saved.attachment_id == f"2026/10/16/{SESSION}/{digest}.png"
    assert saved.media_type == "image/png"
    assert saved.byte_count == len(PNG)
    assert (tmp_path / "data" / "attachments" / saved.attachment_id).read_bytes() == PNG


@pytest.mark.parametrize(
    ("data", "media_type", "extension"),
    [(PNG, "image/png", "png"), (JPEG, "image/jpeg", "jpg"), (GIF, "image/gif", "gif"),
     (WEBP, "image/webp", "webp")],
)
def test_format_is_decided_by_magic_number(
    tmp_path: Path, data: bytes, media_type: str, extension: str
) -> None:
    """按**魔数**认格式：四种都收，扩展名由服务端判定后写。"""
    saved = _store(tmp_path).save(SESSION, data, now=_at(16))

    assert saved.media_type == media_type
    assert saved.attachment_id.endswith(f".{extension}")


def test_renamed_html_is_rejected(tmp_path: Path) -> None:
    """`报告.png` 其实是个 HTML：拒绝——把 HTML 当图片交给模型比拒绝它更糟。"""
    with pytest.raises(AttachmentError, match="不是受支持的图片"):
        _store(tmp_path).save(SESSION, HTML_NAMED_PNG, now=_at(16))


def test_image_over_upload_limit_is_rejected(tmp_path: Path) -> None:
    """超限**拒绝**而不是截断（2026-10-20）：截断的图会让模型看到错误内容还以为自己看过了。

    这条上限只针对上传附件：它会以 ``data:`` URL 进请求体，几 MB 的图会被端点直接 400。
    """
    data = PNG[:8] + b"x" * MAX_UPLOAD_IMAGE_BYTES
    with pytest.raises(AttachmentError, match="图片太大"):
        _store(tmp_path).save(SESSION, data, now=_at(16))


def test_image_at_limit_is_saved_without_truncation(tmp_path: Path) -> None:
    """恰好等于上限时**完整保存**：边界值不能被误杀。"""
    data = PNG[:8] + b"x" * (MAX_UPLOAD_IMAGE_BYTES - 8)
    store = _store(tmp_path)
    saved = store.save(SESSION, data, now=_at(16))
    assert store.read(SESSION, saved.attachment_id).data == data
    assert saved.byte_count == MAX_UPLOAD_IMAGE_BYTES


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AttachmentError, match="空文件"):
        _store(tmp_path).save(SESSION, b"", now=_at(16))


def test_same_image_twice_lands_once(tmp_path: Path) -> None:
    """同一天同一会话里同样的内容只落一份（哈希命名天然去重，也不重复占当天名额）。"""
    store = _store(tmp_path)

    first = store.save(SESSION, PNG, now=_at(16))
    second = store.save(SESSION, PNG, now=_at(16))

    assert first.attachment_id == second.attachment_id
    assert store.count_on(SESSION, date(2026, 10, 16)) == 1


def test_daily_upload_count_is_unlimited(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(25):
        store.save(SESSION, PNG + bytes([index]), now=_at(16))
    assert store.count_on(SESSION, date(2026, 10, 16)) == 25
    assert MAX_ATTACHMENTS_PER_DAY is None


def test_read_round_trips_and_rejects_foreign_or_malformed_ids(tmp_path: Path) -> None:
    """读回的三种拒绝：形状不对、不属于本会话、文件已不在。"""
    store = _store(tmp_path)
    saved = store.save(SESSION, PNG, now=_at(16))

    blob = store.read(SESSION, saved.attachment_id)
    assert blob.data == PNG and blob.media_type == "image/png"

    with pytest.raises(AttachmentError, match="引用不合法"):
        store.read(SESSION, "../../data/model_settings.sqlite")
    with pytest.raises(AttachmentError, match="不属于当前会话"):
        store.read(OTHER_SESSION, saved.attachment_id)
    with pytest.raises(AttachmentError, match="不在磁盘上"):
        store.read(SESSION, f"2026/10/17/{SESSION}/{'0' * 16}.png")


def test_delete_session_removes_every_day(tmp_path: Path) -> None:
    """会话删除时连带删它**所有日期**的附件；别的会话不动。"""
    store = _store(tmp_path)
    store.save(SESSION, PNG, now=_at(15))
    store.save(SESSION, PNG, now=_at(16))
    keep = store.save(OTHER_SESSION, PNG, now=_at(16))

    removed = store.delete_session(SESSION)

    assert removed == 2
    assert store.count_on(SESSION, date(2026, 10, 15)) == 0
    assert store.count_on(SESSION, date(2026, 10, 16)) == 0
    assert store.read(OTHER_SESSION, keep.attachment_id).data == PNG


def test_invalid_session_id_is_rejected(tmp_path: Path) -> None:
    """会话 id 也要卡形状：它会被拼进路径，不能带分隔符。"""
    with pytest.raises(AttachmentError, match="会话 id 不合法"):
        _store(tmp_path).save("../evil", PNG, now=_at(16))


def test_per_message_limit_is_a_shared_constant() -> None:
    """每条消息的张数上限是**契约常量**（服务端与页面同一条口径），这里钉住它的存在与量级。"""
    assert MAX_ATTACHMENTS_PER_MESSAGE is None

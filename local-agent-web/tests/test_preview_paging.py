"""预览分页（§15.11.3，2026-11-03）：能一直看到末尾，不是"只显示开头部分"。

为什么要有这组用例：改之前 `PREVIEW_TEXT_LIMIT` 是**总量**，一个 220 KB 的
`package-lock.json` 只能看到前 547 行，而界面上只有一句"只显示开头部分"，用户没有任何办法
继续看。现在的契约是"一页 20,000 字符，`next_offset` / `next_page` 取下一页"。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.application import artifacts as artifacts_module
from local_agent.application.artifacts import PREVIEW_TEXT_LIMIT, preview


@pytest.fixture(autouse=True)
def _clear_cache():
    artifacts_module._PREVIEW_DOC_CACHE.clear()
    yield
    artifacts_module._PREVIEW_DOC_CACHE.clear()


def _service(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _secrets=(),
        _workspace_root=root,
        _attachments=None,
        _path_for_workspace_id=lambda _workspace_id: root,
    )


def test_long_text_is_readable_page_by_page(tmp_path: Path) -> None:
    """一页一页拼起来必须**完整等于**原文：不重、不漏、不乱序。"""
    original = "".join(f"line-{index:05d}\n" for index in range(4_000))
    # 必须按字节写：`write_text` 在 Windows 上会把 `\n` 翻成 `\r\n`，拼接比对就永远不相等。
    (tmp_path / "long.log").write_bytes(original.encode("utf-8"))
    service = _service(tmp_path)

    first = preview(service, path="long.log")
    assert first.kind == "text"
    assert len(first.text or "") == PREVIEW_TEXT_LIMIT
    assert first.has_more is True
    assert first.next_offset == PREVIEW_TEXT_LIMIT

    chunks = [first.text or ""]
    cursor = first.next_offset
    while cursor is not None:
        page = preview(service, path="long.log", offset=cursor, page=1)
        chunks.append(page.text or "")
        cursor = page.next_offset

    assert "".join(chunks) == original


def test_last_page_reports_no_more(tmp_path: Path) -> None:
    (tmp_path / "small.txt").write_text("短文件", encoding="utf-8")
    view = preview(_service(tmp_path), path="small.txt")
    assert view.has_more is False
    assert view.next_offset is None
    assert view.next_page is None
    assert view.truncated is False


def test_offset_past_the_end_is_empty_not_an_error(tmp_path: Path) -> None:
    (tmp_path / "small.txt").write_text("abc", encoding="utf-8")
    view = preview(_service(tmp_path), path="small.txt", offset=999)
    assert view.kind == "text"
    assert view.text == ""
    assert view.has_more is False


def test_file_beyond_prefix_limit_says_so(tmp_path: Path) -> None:
    """超过 4 MB 的部分确实取不到——如实置 `beyond_limit`，不假装还有下一页。"""
    big = tmp_path / "huge.bin.txt"
    big.write_bytes(b"x" * (artifacts_module.PREVIEW_TOTAL_MAX_BYTES + 1024))
    view = preview(_service(tmp_path), path="huge.bin.txt")
    assert view.kind == "text"
    assert view.beyond_limit is True
    assert view.has_more is True  # 前缀里还有 20,000 字符之后的内容


def test_document_pages_advance_by_page(tmp_path: Path) -> None:
    """文档：一页抽 5 个文档页，`next_page` 指向下一段。"""
    from pypdf import PdfWriter

    # PDF supports physical page windows; docx uses one logical extraction page.
    document = PdfWriter()
    for _ in range(7):
        document.add_blank_page(width=595, height=842)
    document.write(tmp_path / "many.pdf")
    service = _service(tmp_path)

    first = preview(service, path="many.pdf")
    assert first.kind == "text"
    assert first.page is not None and first.total_pages is not None
    assert first.next_page is not None, "文档没到末页时必须给出 next_page"


def test_preview_does_not_leak_outside_path_via_offset(tmp_path: Path) -> None:
    """分页参数不能成为越界读取的后门。"""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(PermissionError):
        preview(_service(tmp_path), path="../secret.txt", offset=10)

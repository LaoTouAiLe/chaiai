from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from local_agent.application.artifacts import preview, resolve_preview_target
from local_agent.application.document_preview import render_pdf


def service(root):
    return SimpleNamespace(_workspace_root=root, _path_for_workspace_id=lambda _: root,
                           _attachments=None, _secrets=())


@pytest.mark.parametrize("extension", ["doc", "docx", "ppt", "pptx", "xls", "pdf"])
def test_visual_metadata_never_extracts_office_as_code(tmp_path, extension):
    (tmp_path / f"report.{extension}").write_bytes(b"fixture")
    result = preview(service(tmp_path), path=f"report.{extension}", presentation="formatted")
    assert result.kind == "document"
    assert result.text is None
    assert "scope=workspace" in result.url


def test_workbook_pages_and_sheet_selection(tmp_path):
    book = Workbook()
    for index in range(205):
        book.active.append([index, f"row-{index}"])
    book.create_sheet("Summary").append(["total", 205])
    book.save(tmp_path / "book.xlsx")
    app = service(tmp_path)
    first = preview(app, path="book.xlsx", presentation="formatted")
    assert len(first.rows) == 200
    assert first.has_more and first.next_offset == 200
    second = preview(app, path="book.xlsx", presentation="formatted", offset=200)
    assert len(second.rows) == 5
    assert second.rows[-1] == ["204", "row-204"]
    assert not second.has_more
    summary = preview(app, path="book.xlsx", presentation="formatted", sheet="Summary")
    assert summary.rows == [["total", "205"]]
    assert summary.sheets == ["Sheet", "Summary"]
    with pytest.raises(ValueError, match="工作表不存在"):
        preview(app, path="book.xlsx", presentation="formatted", sheet="missing")


def test_visual_preview_scope_and_path_boundaries(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-")
    with pytest.raises(PermissionError):
        resolve_preview_target(service(root), path="../secret.pdf")
    with pytest.raises(ValueError):
        resolve_preview_target(service(root), path="https://example.com/file.pdf")
    app = service(root)
    app._attachments = SimpleNamespace(root=tmp_path)
    assert resolve_preview_target(app, path="secret.pdf", scope="attachments") == tmp_path / "secret.pdf"


def test_invalid_pdf_does_not_reach_reader(tmp_path):
    target = tmp_path / "bad.pdf"
    target.write_bytes(b"not a PDF")
    with pytest.raises(ValueError, match="有效的 PDF"):
        render_pdf(target)


def test_pdf_and_workbook_share_the_same_two_slots(tmp_path):
    """并发槽要同时护住 PDF 直通与表格解析（2026-11-06）。

    从前槽只包住 Office 转换那一段：3 个 32 MB 的 PDF 能同时整份读进内存，大表格解析更是
    完全不受限。这里把两格槽占满，断言两类预览立刻明确拒绝（非阻塞，不排队），释放后恢复。
    """
    from local_agent.application import document_preview

    pdf = tmp_path / "ok.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    book = Workbook()
    book.save(tmp_path / "book.xlsx")

    occupied = [document_preview._slots.acquire(blocking=False) for _ in range(2)]
    assert all(occupied)
    try:
        with pytest.raises(ValueError, match="正在预览其他文档"):
            render_pdf(pdf)
        with pytest.raises(ValueError, match="正在预览其他文档"):
            document_preview.spreadsheet(tmp_path / "book.xlsx", sheet=None, offset=0)
    finally:
        for _ in occupied:
            document_preview._slots.release()

    # 释放之后照常出内容：证明上面被拒是因为槽满，不是别的
    assert render_pdf(pdf).startswith(b"%PDF-")
    assert document_preview.spreadsheet(tmp_path / "book.xlsx", sheet=None, offset=0)["kind"] == "spreadsheet"

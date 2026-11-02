"""文档提取用例（2026-10-29）：docx / xlsx / pptx / pdf 经 ``read_file`` 读取。

全部样本**现场生成**，不依赖仓库里放二进制夹具。钉住的是四件实测过的行为：
1. 按魔数判格式——扩展名叫 `.xls` 但内容是 XLSX 时仍能读（下载目录里的真实情况）；
2. 老版 OLE 格式给出"请另存"的说明，而不是"二进制文件"；
3. PDF 按页惰性抽取，只解析本次要的页；
4. 图文混排文档（提取字符远少于文件体积）必须明确提示"正文可能是图片"。
"""

from __future__ import annotations

import io
from pathlib import Path

import docx
import openpyxl
import pptx
import pytest

from local_agent.config import AppConfig
from local_agent.exceptions import FileOperationError
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.document_extract import (
    DocumentIo,
    ExtractOutcome,
    _cell_text,
    extract_document,
)
from local_agent.tools.file_tools import read_file_content


@pytest.fixture
def policy(tmp_path: Path, app_config: AppConfig) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})


# --------------------------------------------------------------------------- 样本构造


def _write_docx(path: Path, *, paragraphs: list[str], table: list[list[str]] | None = None,
                merge_first_row: bool = False) -> None:
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for column_index, value in enumerate(row):
                grid.cell(row_index, column_index).text = value
        if merge_first_row and len(table[0]) > 2:
            grid.cell(0, 1).merge(grid.cell(0, len(table[0]) - 1))
    document.save(str(path))


def _write_xlsx(path: Path, sheets: dict[str, list[list[object]]]) -> None:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    workbook.save(str(path))


def _write_pptx(path: Path, slides: list[tuple[str, list[list[str]] | None]]) -> None:
    presentation = pptx.Presentation()
    layout = presentation.slide_layouts[5]
    for title, table in slides:
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = title
        if table:
            rows, columns = len(table), len(table[0])
            shape = slide.shapes.add_table(rows, columns, 100, 150, 400, 200)
            for row_index, row in enumerate(table):
                for column_index, value in enumerate(row):
                    shape.table.cell(row_index, column_index).text = value
    presentation.save(str(path))


def _minimal_pdf(pages: list[str]) -> bytes:
    """手写一个带正确 xref 的最小 PDF（Helvetica + ASCII 文本），多页用于按页抽取用例。"""
    page_ids = [3 + 2 * index for index in range(len(pages))]
    content_ids = [4 + 2 * index for index in range(len(pages))]
    font_id = 3 + 2 * len(pages)

    bodies: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    bodies[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    for index, text in enumerate(pages):
        page_id, content_id = page_ids[index], content_ids[index]
        bodies[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        ).encode()
        stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET".encode()
        bodies[content_id] = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
            + b"\nendstream"
        )
    bodies[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(bodies):
        offsets[number] = len(out)
        out += str(number).encode() + b" 0 obj\n" + bodies[number] + b"\nendobj\n"
    xref_at = len(out)
    total = max(bodies) + 1
    out += b"xref\n0 " + str(total).encode() + b"\n0000000000 65535 f \n"
    for number in range(1, total):
        if number in offsets:
            out += f"{offsets[number]:010d} 00000 n \n".encode()
        else:
            out += b"0000000000 65535 f \n"
    out += (
        b"trailer\n<< /Size " + str(total).encode() + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_at).encode() + b"\n%%EOF\n"
    )
    return bytes(out)


# --------------------------------------------------------------------------- docx


def test_docx_reads_paragraphs_and_tables(tmp_path: Path, policy: SecurityPolicy) -> None:
    _write_docx(
        tmp_path / "a.docx",
        paragraphs=["标题", "正文第一段"],
        table=[["姓名", "张三"], ["专业", "软件工程"]],
    )

    result = read_file_content("a.docx", policy=policy)

    assert result.is_binary is False
    assert result.extracted == "docx"
    assert "标题" in result.content
    assert "| 姓名 | 张三 |" in result.content
    assert "不要用它们编辑原文件" in (result.note or "")


def test_docx_merged_cells_are_not_repeated(tmp_path: Path, policy: SecurityPolicy) -> None:
    """横向合并区域在 ``row.cells`` 里会重复返回同一个 tc，不能输出三份。"""
    _write_docx(
        tmp_path / "m.docx",
        paragraphs=[],
        table=[["方向", "前端", "后端", "测试"], ["结果", "通过", "通过", "通过"]],
        merge_first_row=True,
    )

    content = read_file_content("m.docx", policy=policy).content

    first_row = next(line for line in content.splitlines() if line.startswith("| 方向"))
    # 4 列合并成 2 个单元格：以竖线数量为准，重复输出会出现 5 个竖线。
    assert first_row.count("|") == 3, first_row


def test_docx_picture_heavy_document_is_flagged(tmp_path: Path, policy: SecurityPolicy) -> None:
    """提取字符远少于文件体积时，必须提示正文可能是图片（实测 1.13 MB / 275 字符那种）。"""
    import os

    from PIL import Image

    document = docx.Document()
    document.add_paragraph("附件1")
    buffer = io.BytesIO()
    # 随机噪声压缩不了，600×600 就接近 1 MB，正文却只有几个字。
    Image.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3)).save(buffer, format="PNG")
    buffer.seek(0)
    document.add_picture(buffer)
    document.save(str(tmp_path / "p.docx"))

    outcome = extract_document(
        DocumentIo.from_path(tmp_path / "p.docx"), sha256="x" * 64
    )

    assert outcome is not None and outcome.kind == "docx"
    assert any("图片" in note for note in outcome.notes)


# --------------------------------------------------------------------------- xlsx


def test_xlsx_reads_each_sheet(tmp_path: Path, policy: SecurityPolicy) -> None:
    _write_xlsx(
        tmp_path / "b.xlsx",
        {"数据": [["名称", "编码"], ["圆通", "yuantong"]], "说明": [["备注"]]},
    )

    result = read_file_content("b.xlsx", policy=policy)

    assert result.extracted == "xlsx"
    assert "## [数据] 2 行 × 2 列" in result.content
    assert "| 圆通 | yuantong |" in result.content
    assert "## [说明]" in result.content


def test_xlsx_integers_and_scientific_notation(tmp_path: Path, policy: SecurityPolicy) -> None:
    _write_xlsx(
        tmp_path / "c.xlsx",
        {"数据": [["编号", "长单号"], [71001, 1805088592691810304]]},
    )

    result = read_file_content("c.xlsx", policy=policy)

    assert "| 71001 |" in result.content  # 不能写成 71001.0
    assert "科学计数法" in (result.note or "")  # 长数字精度已丢失，必须提示


def test_cell_text_formats_values() -> None:
    assert _cell_text(None) == ("", False)
    assert _cell_text(71001.0) == ("71001", False)
    assert _cell_text(True) == ("TRUE", False)
    assert _cell_text(1.5) == ("1.5", False)
    text, is_long = _cell_text(1.8050885926918103e18)
    assert is_long and text.isdigit()


# --------------------------------------------------------------------------- pptx


def test_pptx_reads_slides_and_tables(tmp_path: Path, policy: SecurityPolicy) -> None:
    _write_pptx(
        tmp_path / "d.pptx",
        [("第一页标题", None), ("第二页标题", [["章节", "天数"], ["环境搭建", "1"]])],
    )

    result = read_file_content("d.pptx", policy=policy)

    assert result.extracted == "pptx"
    assert result.page_count == 2
    assert "## 第 1 页" in result.content
    assert "第一页标题" in result.content
    assert "| 环境搭建 | 1 |" in result.content


# --------------------------------------------------------------------------- pdf


def test_pdf_reads_only_requested_pages(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "e.pdf").write_bytes(
        _minimal_pdf(["Page one content", "Page two content", "Page three content"])
    )

    default = read_file_content("e.pdf", policy=policy)

    assert default.extracted == "pdf"
    assert default.page_count == 3
    assert default.first_page == 1
    assert default.last_page == 3
    assert "Page one content" in default.content
    assert "Page three content" in default.content

    single = read_file_content("e.pdf", policy=policy, start_page=2, end_page=2)

    assert single.first_page == 2 and single.last_page == 2
    assert "Page two content" in single.content
    assert "Page one content" not in single.content


def test_pdf_rejects_page_beyond_end(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "f.pdf").write_bytes(_minimal_pdf(["only"]))

    with pytest.raises(ValueError) as excinfo:
        read_file_content("f.pdf", policy=policy, start_page=5)

    assert "超出范围" in str(excinfo.value)


def test_pdf_extraction_is_cached_per_page(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一页第二次读**不再解析**（2026-10-29 复查：原用例先改文件，sha 变了缓存必然
    不命中，断言能过只是因为新文件里也有同一个词——是假通过）。"""
    from local_agent.tools import document_extract

    (tmp_path / "g.pdf").write_bytes(_minimal_pdf(["cached page"]))
    read_file_content("g.pdf", policy=policy, start_page=1, end_page=1)
    assert [key for key in policy.extract_cache if key[1] == 1], "第 1 页应当进缓存"

    parsed: list[int] = []
    real_remember = document_extract._remember

    def counting_remember(policy_: SecurityPolicy, sha: str, page: int, value: object) -> None:
        parsed.append(page)
        real_remember(policy_, sha, page, value)

    monkeypatch.setattr(document_extract, "_remember", counting_remember)
    # 换 max_chars 绕开轮级去重，但仍读同一页：此时只该命中页缓存，不该重新解析。
    again = read_file_content("g.pdf", policy=policy, start_page=1, end_page=1, max_chars=5_000)

    assert "cached page" in again.content
    assert parsed == [], "第二次读同一页不应该重新解析"


def test_pdf_image_heavy_document_is_flagged(tmp_path: Path, policy: SecurityPolicy) -> None:
    """每页平均字符数过低时提示"图片型"。

    判据必须是**每页平均字符数**，不能用"字符 / 文件字节"：按页惰性读取时本次只抽了
    几页，拿整个文件体积当分母必然误报（2026-10-29 实测踩到）。
    """
    # 每页 60 个字符：高于单页稀疏阈值（40），低于平均阈值（150）。
    (tmp_path / "i.pdf").write_bytes(_minimal_pdf(["x" * 60, "y" * 60]))

    result = read_file_content("i.pdf", policy=policy)

    assert "图片型" in (result.note or "")


def test_pdf_dense_document_is_not_flagged(tmp_path: Path, policy: SecurityPolicy) -> None:
    """正常文字型 PDF 不能被误报成图片型（这正是上一版的回归点）。"""
    (tmp_path / "d.pdf").write_bytes(_minimal_pdf(["lorem ipsum dolor sit amet " * 10]))

    result = read_file_content("d.pdf", policy=policy)

    assert "图片型" not in (result.note or "")


def test_sparse_ratio_is_flagged_for_one_shot_extraction() -> None:
    """字符数远少于文件大小时提示"内容是图片"——只用于一次性整体提取的格式。"""
    from local_agent.tools.document_extract import _finish

    outcome = _finish(
        "pptx", "只有标题", [], total_pages=20, first_page=1, last_page=20, size=5_000_000
    )

    assert any("图片" in note for note in outcome.notes)


# --------------------------------------------------------------------------- 魔数判定


def test_misnamed_xls_is_read_as_xlsx(tmp_path: Path, policy: SecurityPolicy) -> None:
    """扩展名 .xls、内容其实是 XLSX（下载目录里的真实情况）：按魔数走 xlsx 分支。"""
    real = tmp_path / "real.xlsx"
    _write_xlsx(real, {"数据": [["名称", "编码"], ["中通", "zhongtong"]]})
    disguised = tmp_path / "disguised.xls"
    disguised.write_bytes(real.read_bytes())

    result = read_file_content("disguised.xls", policy=policy)

    assert result.extracted == "xlsx"
    assert "| 中通 | zhongtong |" in result.content


def test_legacy_ole_is_reported_with_guidance(tmp_path: Path, policy: SecurityPolicy) -> None:
    """真 OLE（.doc/.xls 老格式）：明确"请另存"，而不是"二进制文件，未返回内容"。"""
    (tmp_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)

    result = read_file_content("old.doc", policy=policy)

    assert result.is_binary
    assert result.content == ""
    assert "另存为" in (result.note or "")


def test_other_zip_container_is_not_treated_as_document(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    import zipfile

    with zipfile.ZipFile(tmp_path / "plain.zip", "w") as archive:
        archive.writestr("readme.txt", "hi")

    result = read_file_content("plain.zip", policy=policy)

    assert result.is_binary
    assert "不是 docx" in (result.note or "")


# --------------------------------------------------------------------------- 与文本路径的边界


def test_text_file_still_uses_the_text_path(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "note.md").write_text("# 标题\n正文\n", encoding="utf-8")

    result = read_file_content("note.md", policy=policy)

    assert result.extracted is None
    assert "# 标题" in result.content


def test_page_range_on_text_file_is_rejected(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "note.md").write_text("x\n", encoding="utf-8")

    with pytest.raises(FileOperationError) as excinfo:
        read_file_content("note.md", policy=policy, start_page=2)

    assert "只对" in str(excinfo.value)


def test_page_range_and_line_range_are_exclusive(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    (tmp_path / "a.docx").write_bytes(b"PK\x03\x04")

    with pytest.raises(ValueError):
        read_file_content("a.docx", policy=policy, start_page=1, start_line=1)


def test_document_content_can_be_continued_by_char(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    _write_docx(tmp_path / "long.docx", paragraphs=[f"第 {index} 段" for index in range(50)])

    first = read_file_content("long.docx", policy=policy, max_chars=40)
    assert first.has_more
    assert first.next_start_char == 40

    second = read_file_content(
        "long.docx", policy=policy, max_chars=40, start_char=first.next_start_char
    )
    assert second.content and second.content not in first.content


def test_extract_outcome_shape() -> None:
    """`kind=None` 表示已知不支持的二进制格式，不是解析失败。"""
    outcome = ExtractOutcome(kind=None, notes=("说明",))
    assert not outcome.supported
    assert extract_document.__name__ == "extract_document"


def test_document_read_is_deduped_within_one_turn(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """同一轮重复读同一段文档不再回传原文（与文本路径同口径，2026-10-29）。"""
    _write_docx(tmp_path / "d.docx", paragraphs=["正文内容"])

    first = read_file_content("d.docx", policy=policy)
    assert "正文内容" in first.content

    second = read_file_content("d.docx", policy=policy)
    assert second.content == ""
    assert "已经读过" in (second.note or "")

    # 换参数不命中：模型要原文时永远有出口。
    third = read_file_content("d.docx", policy=policy, max_chars=5_000)
    assert "正文内容" in third.content


def test_oversized_document_is_rejected(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大小上限对所有格式统一生效（此前只有 OOXML 分支检查，PDF 完全不看大小）。"""
    from local_agent.tools import document_extract
    from local_agent.tools.document_extract import DocumentExtractError

    monkeypatch.setattr(document_extract, "MAX_DOCUMENT_BYTES", 1024)
    (tmp_path / "big.pdf").write_bytes(_minimal_pdf(["padding " * 200]))

    with pytest.raises(DocumentExtractError) as excinfo:
        read_file_content("big.pdf", policy=policy)

    assert "太大" in str(excinfo.value)


def test_large_text_file_is_not_blocked_by_the_document_limit(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大小上限**只对文档**生效：文本按流读、本来就不受写入预算限制，不能被它挡下来。"""
    from local_agent.tools import document_extract

    monkeypatch.setattr(document_extract, "MAX_DOCUMENT_BYTES", 16)
    (tmp_path / "big.log").write_text("0123456789" * 10, encoding="utf-8")

    result = read_file_content("big.log", policy=policy)

    assert result.extracted is None
    assert "0123456789" in result.content


def test_image_only_pdf_warns_on_every_read_including_cached_pages(
    tmp_path: Path, policy: SecurityPolicy,
) -> None:
    from PIL import Image, ImageDraw

    target = tmp_path / "scan.pdf"
    image = Image.new("RGB", (500, 300), "white")
    ImageDraw.Draw(image).text((20, 20), "SCANNED REPORT BODY", fill="black")
    image.save(target, "PDF")

    for budget in (2000, 3000):
        result = read_file_content("scan.pdf", policy=policy, max_chars=budget)
        assert result.extracted == "pdf"
        assert "图片" in (result.note or "")
        assert "正文无法通过文本读取" in (result.note or "")

"""文档提取（2026-10-29）：docx / xlsx / pptx / pdf → 可读纯文本。

**为什么塞进 ``read_file`` 而不是新开一个工具**：参照 hermes 的做法
（``hermes-agent/tools/file_tools.py:653``）——模型对"读文件"只有一个心智模型，
多一个工具就多一次"该用哪个"的判断成本。分流点必须在二进制守卫**之前**
（我们这边是 ``text_stream.text_parts`` 的 NUL 探测，见 ``file_tools.py:277``），
否则文档永远进不来。

四条边界：

1. **按魔数判格式，不信扩展名**（2026-10-29 实测）：用户下载目录里 ``用户数据.xls``
   与 ``菜单目录.xls`` 的文件头都是 ``PK\\x03\\x04``——内容其实是 XLSX，按后缀分流会把
   它们误判成"不支持的老格式"直接拒绝。反之 ``.doc`` 也可能是真 OLE。
2. **只在读取侧提取**：``text_stream`` 与 ``edit_file`` 不动，于是提取出的文本
   **永远写不回原文件**——写回会毁掉格式。hermes 专门加了一道写回保护
   （``file_tools_write_guards.py:484-505``），我们靠"写路径不认文档"天然成立。
3. **PDF 按页惰性抽取**：实测（2026-10-29，295 页 / 35.9 MB 的中文技术书）全量抽取
   14.79s，单页 0.046s、打开 0.019s。只有真的要读的那几页才抽。
4. **提取不出内容 ≠ 没有内容**：图文混排文档抽出来的只有标题。实测一个 1.13 MB 的
   docx 只抽出 275 字符、内含 7 张图。必须明确提示"正文可能是图片"，否则模型会
   基于标题硬总结。hermes 只检测**完全空页**（``read_extract.py:175-186``），对
   "非空但极稀疏"不报警——这个缺口不照抄。

依赖全部**延迟导入**：没装解析库时 ``read_file`` 仍按原样工作，只在真的读到文档时
给出"缺少依赖"的说明，而不是让整个文件工具导入失败。

外部输入统一走 :class:`DocumentIo`（2026-10-29）：agent 侧的 ``read_file`` 有
SecurityPolicy（路径边界、敏感文件、轮内缓存），而右栏"产物预览"只有 Path。
两条调用链共用这一份提取实现，不各写一遍。
"""

from __future__ import annotations

import importlib
import io as io_module
import warnings
import zipfile
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from local_agent.security.policy import SecurityPolicy

DocumentKind = Literal["pdf", "docx", "xlsx", "pptx"]

#: 单文件上限。实测最大样本 35.9 MB（295 页 PDF）通过；再大的一次解析会把工具卡住。
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024

#: 提取后文本上限：防止异常文件（超大单元格、病态 XML）撑爆一次工具返回。
MAX_EXTRACT_CHARS = 2_000_000

#: 单页 / 单表的字符上限：一页抽出来几十万字符时截断，避免单页吃光整个预算。
MAX_PAGE_CHARS = 20_000

#: xlsx 单个 sheet 最多输出多少行（3033 行的实测样本完整通过，这里留足余量）。
MAX_SHEET_ROWS = 5_000

#: PDF 默认一次读几页（单页 46ms，5 页约 0.25s）。
DEFAULT_PAGES = 5

#: 单页少于这么多字符时提示"可能是图片"（实测图文混排文档的正文页就在这个量级）。
SPARSE_PAGE_CHARS = 40

#: PDF 平均每页少于这么多字符时，整份判定为"图片型"。
#: 实测依据（2026-10-29）：PPT 导出的幻灯片型 PDF 平均 77 字符/页；中文技术书 1098 字符/页。
SPARSE_PAGE_AVG_CHARS = 150

#: 提取字符数 / 文件字节数 低于这个比例时，提示"主要内容可能不是文字"。
#: 依据：纯文字 docx 约 0.05~0.1 字符/字节；那个 1.13 MB / 275 字符的样本是 0.00024。
SPARSE_DOC_RATIO = 0.002

#: 超过这个值的浮点数在 Excel 里通常是手机号 / 单号 / 身份证，精度已经丢失。
LONG_NUMBER_THRESHOLD = 1e15

_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF_MAGIC = b"%PDF-"


class DocumentExtractError(ValueError):
    """提取过程本身失败（文件损坏、加密、缺少依赖）。"""


@dataclass(frozen=True)
class DocumentIo:
    """提取所需的全部外部输入：路径、大小、按上限读字节、页文本缓存。

    抽出这一层（2026-10-29）的原因：``read_file`` 有 SecurityPolicy（路径边界、敏感
    文件、轮内缓存），右栏"产物预览"只有 Path、拿不到策略。把这三件事从策略上解耦，
    两条调用链就能共用同一份提取实现——否则必然出现两套各自漂移的解析代码。
    """

    path: Path
    size: int
    #: 最多读 ``limit + 1`` 字节（与 ``FileSystemSandbox.read_bytes`` 同语义）。
    read: Callable[[int], bytes]
    cache: MutableMapping[tuple[str, int], object]
    cache_limit: int = 64

    @classmethod
    def from_policy(cls, path: Path, policy: SecurityPolicy) -> DocumentIo:
        """agent 侧：走统一文件后端，顺带吃到路径与敏感文件校验。"""
        return cls(
            path=path,
            size=policy.files.stat(path).st_size,
            read=lambda limit: policy.files.read_bytes(path, max_bytes=limit),
            cache=policy.extract_cache,
        )

    @classmethod
    def from_path(
        cls, path: Path, *, cache: MutableMapping[tuple[str, int], object] | None = None
    ) -> DocumentIo:
        """预览侧：调用方已确认路径在工作区内，只需按上限读字节。

        ``cache`` 可以由调用方跨请求共享（键里带文件 mtime 就不怕陈旧）。
        """
        target = Path(path)

        def read(limit: int) -> bytes:
            with target.open("rb") as handle:
                return handle.read(limit + 1)

        return cls(
            path=target,
            size=target.stat().st_size,
            read=read,
            cache=cache if cache is not None else {},
        )


@dataclass(frozen=True)
class ExtractOutcome:
    """一次提取的结果，或者"为什么不提取"。

    ``kind`` 为 ``None`` 表示这是**已知但不支持**的二进制格式（老版 .doc/.xls、
    其它 OOXML 成员）：此时 ``text`` 为空、``notes`` 说明原因与替代做法，调用方
    据此组装"二进制，未返回正文"的结果，而不是抛异常——那是可预期结果，不是工具故障。
    """

    kind: DocumentKind | None
    text: str = ""
    #: 文档总页数（pdf 的页、pptx 的幻灯片、docx/xlsx 固定 1）。
    total_pages: int = 0
    #: 本次实际返回的页区间（1 基，含）。非分页格式为 1/1。
    first_page: int = 0
    last_page: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def supported(self) -> bool:
        return self.kind is not None


def extract_document(
    source: DocumentIo,
    *,
    sha256: str,
    start_page: int | None = None,
    end_page: int | None = None,
) -> ExtractOutcome | None:
    """按魔数识别并提取；不是文档则返回 ``None``（交回原有文本路径）。

    Raises:
        DocumentExtractError: 文件过大、损坏、加密，或本机缺少解析依赖。
    """
    header = source.read(8)
    is_pdf = header.startswith(_PDF_MAGIC)
    is_zip = header.startswith(_ZIP_MAGIC)
    if not (is_pdf or is_zip or header.startswith(_OLE_MAGIC)):
        # 不是文档就立刻交回文本路径：**大小上限只对文档生效**，否则 100 MB 的文本
        # 日志会被这条检查挡下来（文本路径本来是按流读的，不受写入预算限制）。
        return None
    if header.startswith(_OLE_MAGIC):
        return ExtractOutcome(
            kind=None,
            notes=(
                (
                    "这是旧版 Office 二进制格式（.doc / .xls / .ppt），不是 ZIP 容器，无法解析。"
                    "请用 Word / Excel / PowerPoint 另存为 .docx / .xlsx / .pptx 后再读。"
                ),
            ),
        )
    # 大小上限对 pdf 与 OOXML 统一在这里判（2026-10-29 复查修正）：此前只有 OOXML
    # 分支检查，一个 800 MB 的 PDF 会直接进 pypdf 并长时间占住工具线程。
    if source.size > MAX_DOCUMENT_BYTES:
        raise DocumentExtractError(
            f"文档太大（{source.size // 1024 // 1024} MB > 上限 "
            f"{MAX_DOCUMENT_BYTES // 1024 // 1024} MB）：请先把要读的部分单独导出，再读那个文件"
        )
    if is_pdf:
        return _extract_pdf(source, sha256=sha256, start_page=start_page, end_page=end_page)
    return _extract_ooxml(source, sha256=sha256, start_page=start_page, end_page=end_page)


def _extract_ooxml(
    source: DocumentIo,
    *,
    sha256: str,
    start_page: int | None,
    end_page: int | None,
) -> ExtractOutcome:
    """ZIP 容器：看内部结构判断是 docx / xlsx / pptx，而不是看扩展名。"""
    data = source.read(MAX_DOCUMENT_BYTES)
    try:
        with zipfile.ZipFile(io_module.BytesIO(data)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile as exc:
        raise DocumentExtractError(f"ZIP 容器损坏，无法打开：{exc}") from exc

    if any(name.startswith("word/") for name in names):
        return _extract_docx(data, source, sha256=sha256)
    if any(name.startswith("xl/") for name in names):
        return _extract_xlsx(data, source, sha256=sha256)
    if any(name.startswith("ppt/") for name in names):
        return _extract_pptx(data, source, sha256=sha256)
    return ExtractOutcome(
        kind=None,
        notes=("这是 ZIP 容器，但不是 docx / xlsx / pptx（内部没有 word/、xl/、ppt/ 目录）。",),
    )


def _extract_docx(data: bytes, source: DocumentIo, *, sha256: str) -> ExtractOutcome:
    cached = _cached(source, sha256, 0)
    if cached is not None:
        return cached
    document_module = _import("docx", "python-docx")
    notes: list[str] = []
    try:
        document = document_module.Document(io_module.BytesIO(data))
    except Exception as exc:
        raise DocumentExtractError(f"docx 解析失败：{exc}") from exc

    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            lines.append(text)
    for index, table in enumerate(document.tables, start=1):
        lines.append(f"[表 {index}]")
        lines.extend(_docx_table_lines(table))
    text = "\n".join(lines)
    pictures = len(document.inline_shapes)

    # 横向合并单元格会让同一个 tc 在 ``row.cells`` 里出现多次，不处理就输出重复文本。
    if pictures:
        notes.append(f"文档含 {pictures} 张图片，图片内容未提取（只有文字）")
    outcome = _finish("docx", text, notes, total_pages=1, first_page=1, last_page=1,
                      size=source.size)
    _remember(source, sha256, 0, outcome)
    return outcome


def _docx_table_lines(table: Any) -> list[str]:
    lines: list[str] = []
    for row in table.rows:
        seen: set[int] = set()
        cells: list[str] = []
        for cell in row.cells:
            key = id(cell._tc)  # python-docx 没有公开的 tc 访问器；合并区域会重复返回
            if key in seen:
                continue
            seen.add(key)
            cells.append(" ".join(cell.text.split()))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _extract_xlsx(data: bytes, source: DocumentIo, *, sha256: str) -> ExtractOutcome:
    cached = _cached(source, sha256, 0)
    if cached is not None:
        return cached
    openpyxl = _import("openpyxl", "openpyxl")
    notes: list[str] = []
    # 实测有的工作簿缺默认样式，openpyxl 会打 UserWarning；不能让它跑到 stderr 污染输出。
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            # 必须传 BytesIO：openpyxl 按**扩展名**拒绝 .xls，而我们这边可能是"假 .xls"。
            workbook = openpyxl.load_workbook(io_module.BytesIO(data), data_only=True)
    except Exception as exc:
        raise DocumentExtractError(f"xlsx 解析失败：{exc}") from exc

    lines: list[str] = []
    long_numbers = 0
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        rows_total = sheet.max_row or 0
        columns_total = sheet.max_column or 0
        lines.append(f"## [{sheet_name}] {rows_total} 行 × {columns_total} 列")
        if sheet.merged_cells.ranges:
            merged = ", ".join(str(item) for item in list(sheet.merged_cells.ranges)[:10])
            notes.append(f"工作表「{sheet_name}」有合并单元格（{merged}），已按左上角取值")
        emitted = 0
        for row in sheet.iter_rows(values_only=True):
            if emitted >= MAX_SHEET_ROWS:
                break
            if all(value is None for value in row):
                continue
            emitted += 1
            cells: list[str] = []
            for value in row:
                text, is_long = _cell_text(value)
                long_numbers += 1 if is_long else 0
                cells.append(text)
            while cells and not cells[-1]:
                cells.pop()
            lines.append("| " + " | ".join(cells) + " |")
        if rows_total > MAX_SHEET_ROWS:
            notes.append(
                f"工作表「{sheet_name}」只输出了前 {MAX_SHEET_ROWS} 行（共 {rows_total} 行）"
            )
    if long_numbers:
        notes.append(
            f"有 {long_numbers} 个长数字（手机号 / 单号 / 证件号一类）在文件里已按浮点数存储，"
            "显示为科学计数法且原始精度已丢失，不要据此计算"
        )
    text = "\n".join(lines)
    outcome = _finish("xlsx", text, notes, total_pages=1, first_page=1, last_page=1,
                      size=source.size)
    _remember(source, sha256, 0, outcome)
    return outcome


def _cell_text(value: Any) -> tuple[str, bool]:
    """单元格转文本；整数值的浮点转成 int（Excel 里 ``71001`` 读出来是 ``71001.0``）。

    长数字（>= 1e15）**不论读回来是 int 还是 float 都算不可信**：xlsx 的数字一律按
    IEEE double 存储，只有 15 位有效数字，手机号 / 单号在第 16 位起就已经是错的。
    """
    if value is None:
        return "", False
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), False
    if isinstance(value, (int, float)):
        if abs(value) >= LONG_NUMBER_THRESHOLD:
            return (f"{value:.0f}" if isinstance(value, float) else str(value)), True
        if isinstance(value, float):
            return (str(int(value)) if value.is_integer() else repr(value)), False
        return str(value), False
    return str(value).replace("\n", " "), False


def _extract_pptx(data: bytes, source: DocumentIo, *, sha256: str) -> ExtractOutcome:
    cached = _cached(source, sha256, 0)
    if cached is not None:
        return cached
    pptx = _import("pptx", "python-pptx")
    notes: list[str] = []
    try:
        presentation = pptx.Presentation(io_module.BytesIO(data))
    except Exception as exc:
        raise DocumentExtractError(f"pptx 解析失败：{exc}") from exc

    pictures = 0
    slides: list[str] = []
    total = len(presentation.slides)
    for index, slide in enumerate(presentation.slides, start=1):
        block: list[str] = []
        for shape in slide.shapes:
            if shape.shape_type == pptx.enum.shapes.MSO_SHAPE_TYPE.PICTURE:
                pictures += 1
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text.strip()
                if text:
                    block.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [" ".join(cell.text.split()) for cell in row.cells]
                    block.append("| " + " | ".join(cells) + " |")
        if getattr(slide, "has_notes_slide", False):
            notes_text = slide.notes_slide.notes_text_frame.text.strip()
            if notes_text:
                block.append(f"[备注] {notes_text}")
        if block:
            slides.append(f"## 第 {index} 页\n" + "\n".join(block))
    if pictures:
        notes.append(f"幻灯片含 {pictures} 张图片，图片内容未提取（只有文字）")
    text = "\n\n".join(slides)
    outcome = _finish("pptx", text, notes, total_pages=total, first_page=1, last_page=total,
                      size=source.size)
    _remember(source, sha256, 0, outcome)
    return outcome


def _extract_pdf(
    source: DocumentIo,
    *,
    sha256: str,
    start_page: int | None,
    end_page: int | None,
) -> ExtractOutcome:
    """按页惰性抽取：只解析本次要返回的页（实测单页 46ms，全量 295 页 14.79s）。"""
    pypdf = _import("pypdf", "pypdf")
    try:
        reader = pypdf.PdfReader(str(source.path))
        total = len(reader.pages)
    except Exception as exc:
        raise DocumentExtractError(f"pdf 解析失败（可能加密或损坏）：{exc}") from exc
    if total == 0:
        return _finish("pdf", "", ["这份 PDF 没有任何页面"], total_pages=0,
                       first_page=0, last_page=0)

    first = max(1, start_page or 1)
    if first > total:
        raise DocumentExtractError(f"起始页 {first} 超出范围：这份 PDF 只有 {total} 页")
    last = min(total, end_page or (first + DEFAULT_PAGES - 1))
    if last < first:
        raise DocumentExtractError(f"结束页 {last} 不能小于起始页 {first}")

    notes: list[str] = []
    blocks: list[str] = []
    sparse: list[int] = []
    lengths: list[int] = []
    for number in range(first, last + 1):
        page_text = _cached(source, sha256, number)
        if page_text is None:
            try:
                page_text = reader.pages[number - 1].extract_text() or ""
            except Exception as exc:  # noqa: BLE001 - 单页失败不该毁掉整次读取
                notes.append(f"第 {number} 页抽取失败：{exc}")
                page_text = ""
            if len(page_text) > MAX_PAGE_CHARS:
                page_text = page_text[:MAX_PAGE_CHARS]
                notes.append(f"第 {number} 页内容超过 {MAX_PAGE_CHARS} 字符，已截断")
            _remember(source, sha256, number, page_text)
        stripped = page_text.strip()
        lengths.append(len(stripped))
        if len(stripped) < SPARSE_PAGE_CHARS:
            sparse.append(number)
        blocks.append(f"## 第 {number} 页\n{stripped}")

    if sparse:
        shown = "、".join(str(number) for number in sparse[:10])
        notes.append(
            f"第 {shown} 页文本层信息极少（可能是图片或扫描件），这些页的正文无法通过文本读取"
        )
    # 图片型 PDF 的判据是**每页平均字符数**，不是"字符 / 文件字节"：按页惰性读取时本次
    # 只抽了几页，拿整个文件体积当分母必然误报（2026-10-29 实测踩到：读技术书第 1-5 页
    # 时被判成"整份只有 3324 个字符"）。
    if lengths and sum(lengths) / len(lengths) < SPARSE_PAGE_AVG_CHARS:
        average = int(sum(lengths) / len(lengths))
        notes.append(
            f"本次读的 {len(lengths)} 页平均每页只有 {average} 个字符，"
            "这份 PDF 可能是图片型的，正文无法通过文本读取"
        )
    if total > last:
        notes.append(f"后面还有 {total - last} 页，传 start_page={last + 1} 继续读")
    if first > 1:
        notes.append(f"前面还有 {first - 1} 页未读")
    return _finish("pdf", "\n\n".join(blocks), notes, total_pages=total,
                   first_page=first, last_page=last)


def _finish(
    kind: DocumentKind,
    text: str,
    notes: list[str],
    *,
    total_pages: int,
    first_page: int,
    last_page: int,
    size: int = 0,
) -> ExtractOutcome:
    """套上总量上限，并补"非文字为主"的判断。

    ``size`` 是文件字节数，**只对一次性整体提取的格式有意义**（docx/xlsx/pptx）；
    PDF 是按页惰性读取的，本次抽的几页对不上整个文件体积，所以不传（默认 0），
    它用的是"每页平均字符数"这条判据。
    """
    extra: list[str] = []
    if size and len(text) < MAX_EXTRACT_CHARS and len(text) / max(size, 1) < SPARSE_DOC_RATIO:
        extra.append(
            f"整份文档只提取到 {len(text)} 个字符（文件 {size // 1024} KB），"
            "主要内容很可能是图片或嵌入对象，文字部分不完整"
        )
    if len(text) > MAX_EXTRACT_CHARS:
        text = text[:MAX_EXTRACT_CHARS]
        extra.append(f"提取内容超过 {MAX_EXTRACT_CHARS} 字符，已截断")
    return ExtractOutcome(
        kind=kind,
        text=text,
        total_pages=total_pages,
        first_page=first_page,
        last_page=last_page,
        notes=(*notes, *extra),
    )


def _import(module: str, package: str) -> Any:
    """延迟导入解析库：没装时给出可照着做的提示，而不是让文件工具整体导入失败。"""
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - 只在缺依赖的环境触发
        raise DocumentExtractError(
            f"本机缺少 {package}，无法提取这类文档。"
            f"请在项目环境里安装 {package} 后重试，或先把文件另存为纯文本。"
        ) from exc


def _cached(source: DocumentIo, sha256: str, page: int) -> Any:
    return source.cache.get((sha256, page))


def _remember(source: DocumentIo, sha256: str, page: int, value: Any) -> None:
    """轮内缓存：同一份 PDF 分几次读只解析一次；超过上限整体清空。"""
    if len(source.cache) >= source.cache_limit:
        source.cache.clear()
    source.cache[(sha256, page)] = value

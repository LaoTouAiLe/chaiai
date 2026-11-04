"""Visual document previews. Conversion receives a private copy, never a workspace path."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from pathlib import Path
from threading import BoundedSemaphore, Lock

MAX_BYTES = 32 * 1024 * 1024
OFFICE = frozenset({".doc", ".docx", ".ppt", ".pptx", ".xls"})
_slots = BoundedSemaphore(2)
_lock = Lock()
_cache: OrderedDict[str, bytes] = OrderedDict()


def render_pdf(target: Path) -> bytes:
    if target.stat().st_size > MAX_BYTES:
        raise ValueError("文档超过 32 MB，请使用打开方式查看。")
    source = target.read_bytes()
    if len(source) > MAX_BYTES:
        raise ValueError("文档超过 32 MB，请使用打开方式查看。")
    if target.suffix.lower() == ".pdf":
        if not source.startswith(b"%PDF-"):
            raise ValueError("文件不是有效的 PDF。")
        return source
    if target.suffix.lower() not in OFFICE | {".xlsx"}:
        raise ValueError("该格式不支持版式预览。")
    key = hashlib.sha256(target.suffix.lower().encode() + source).hexdigest()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    runtime = Path(__file__).resolve().parents[3] / "preview-runtime"
    node = shutil.which("node")
    if node is None or not (runtime / "node_modules/@deepseek-ai/libreoffice-kit").exists():
        raise ValueError("文档转换组件未安装，请使用打开方式查看。")
    if not _slots.acquire(blocking=False):
        raise ValueError("正在预览其他文档，请稍后重试。")
    try:
        with tempfile.TemporaryDirectory(prefix="chaiai-preview-") as scratch:
            directory = Path(scratch)
            original = directory / ("document" + target.suffix.lower())
            output = directory / "document.pdf"
            original.write_bytes(source)
            environment = dict(os.environ)
            environment.pop("SSLKEYLOGFILE", None)
            try:
                result = subprocess.run(
                    [node, str(runtime / "render.mjs"), str(original), str(output)],
                    cwd=runtime, env=environment, capture_output=True, timeout=110, check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except subprocess.TimeoutExpired:
                raise ValueError("文档转换超时，请使用打开方式查看。") from None
            if result.returncode or not output.is_file():
                raise ValueError("文档转换失败，文件可能损坏、加密或格式不受支持。")
            if output.stat().st_size > MAX_BYTES:
                raise ValueError("转换后的文档超过预览上限，请使用打开方式查看。")
            pdf = output.read_bytes()
            if not pdf.startswith(b"%PDF-"):
                raise ValueError("文档转换没有生成有效 PDF。")
        with _lock:
            _cache[key] = pdf
            while len(_cache) > 8 or sum(map(len, _cache.values())) > 64 * 1024 * 1024:
                _cache.popitem(last=False)
        return pdf
    finally:
        _slots.release()


def spreadsheet(target: Path, *, sheet: str | None, offset: int) -> dict:
    from openpyxl import load_workbook

    if target.stat().st_size > MAX_BYTES:
        raise ValueError("工作簿超过 32 MB，请使用打开方式查看。")
    book = load_workbook(target, read_only=True, data_only=True)
    try:
        names = book.sheetnames
        if sheet is not None and sheet not in names:
            raise ValueError("工作表不存在。")
        current = book[sheet or names[0]]
        # One extra row establishes whether the next page actually exists.
        rows = list(current.iter_rows(min_row=offset + 1, max_row=min(current.max_row or 1, offset + 201),
                                      max_col=min(current.max_column or 1, 200), values_only=True))
        more = len(rows) > 200
        return {"kind": "spreadsheet", "sheets": names, "sheet": current.title,
                "rows": [["" if value is None else str(value) for value in row] for row in rows[:200]],
                "offset": offset, "has_more": more, "next_offset": offset + 200 if more else None,
                "next_page": 1 if more else None, "size_bytes": target.stat().st_size,
                "beyond_limit": (current.max_column or 1) > 200}
    finally:
        book.close()

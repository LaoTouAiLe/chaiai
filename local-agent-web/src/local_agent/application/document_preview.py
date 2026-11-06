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

from local_agent.security.process_guard import run_process

MAX_BYTES = 32 * 1024 * 1024
OFFICE = frozenset({".doc", ".docx", ".ppt", ".pptx", ".xls"})
_slots = BoundedSemaphore(2)
_lock = Lock()
_cache: OrderedDict[str, bytes] = OrderedDict()


def render_pdf(target: Path) -> bytes:
    """把文档渲染成 PDF 字节；PDF 直通，Office 交给 preview-runtime 转换。

    槽在**读文件之前**就拿到（2026-11-06）：32 MB × 并发数是这里唯一会炸内存的地方，从前只护住
    Office 转换那一段，等于 PDF 直通完全不限流。取槽非阻塞——占满就明确失败，不排队把请求连同
    几百 MB 一起挂在内存里。
    """
    if not _slots.acquire(blocking=False):
        raise ValueError("正在预览其他文档，请稍后重试。")
    try:
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
        with tempfile.TemporaryDirectory(prefix="chaiai-preview-") as scratch:
            directory = Path(scratch)
            original = directory / ("document" + target.suffix.lower())
            output = directory / "document.pdf"
            original.write_bytes(source)
            environment = dict(os.environ)
            environment.pop("SSLKEYLOGFILE", None)
            # 走统一执行出口（架构 §20.1）：转换要能被超时与取消按**进程树**终止，
            # node 自己还会再起 LibreOffice 子进程，直接 subprocess.run 只杀得掉第一个。
            outcome = run_process(
                [node, str(runtime / "render.mjs"), str(original), str(output)],
                cwd=runtime, env=environment, timeout_seconds=110,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if outcome.timed_out:
                raise ValueError("文档转换超时，请使用打开方式查看。")
            if outcome.exit_code not in (0, None) or not output.is_file():
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
    """读一页表格。与 :func:`render_pdf` 共用同两格槽（2026-11-06）。

    它没有 Office 转换那一步，从前完全不受限：并发十个大表能把内存吃干净，且没有任何征兆。
    """
    from openpyxl import load_workbook

    if not _slots.acquire(blocking=False):
        raise ValueError("正在预览其他文档，请稍后重试。")
    try:
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
    finally:
        _slots.release()

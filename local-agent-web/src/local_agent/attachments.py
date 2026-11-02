"""对话框里上传的图片附件（2026-10-16）：**内容存文件、引用进消息**。

放哪：``data/attachments/年/月/日/<会话>/<内容哈希前 16 位>.<扩展名>``——``data/`` 与三个库、向量库
同处，按 ``config.yaml`` 所在目录定位（解压到哪个盘都成立）。为什么内容不进设置库：图片是二进制
大对象，进库只能当 blob，"能编辑、能被 git diff、用户能自己看"这些性质全丢；库里只该存**选择**
（``agent.skills.disabled`` 那类例外表），不存内容。

按年月日分层是为了**人找图**（"昨天那张"）与整批处理。**它不是"按日期自动清理"**：三十天前那条
消息可能还在引用它，所以清理只按**会话**（会话删除时连带删它的所有日期目录），不看文件年龄。

四条与 `view_image` 一致的边界（都有用例）：

1. **按魔数认格式**：不信扩展名，也不信客户端声明的 media_type；
2. **完整保存**：单图有字节上限（``MAX_UPLOAD_IMAGE_BYTES``，2026-10-20 起；超限拒绝而不是截断），
   单次与每日数量仍不设配额；
3. **引用强校验**：形状不对、越出附件根、或**不属于这个会话**的 id 一律拒绝——引用是相对路径，
   所以它必须被当成不可信输入处理；
4. **同图不重复写**：同一天同一会话里，同样的内容只落一份（内容哈希命名天然去重）。
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from local_agent.exceptions import AttachmentError
from local_agent.media import (
    EXTENSION_BY_MEDIA,
    MAX_UPLOAD_IMAGE_BYTES,
    SUPPORTED_NOTE,
    detect_image_media_type,
)

#: 默认不限制一条消息的图片数量。
MAX_ATTACHMENTS_PER_MESSAGE: int | None = None

#: 默认不限制每日上传数量。
MAX_ATTACHMENTS_PER_DAY: int | None = None

#: 附件引用的形状：``年/月/日/会话/内容哈希.扩展名``。**只允许这一种形状**——
#: 它同时充当路径穿越防线（只有数字、十六进制与固定斜杠，`..` 与绝对路径都进不来）。
_ID_RE = re.compile(
    r"^(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<session>[0-9a-f]{8,64})/(?P<digest>[0-9a-f]{16})\.(?P<ext>"
    r"png|jpg|gif|webp|pdf|docx|xlsx|pptx|doc|xls|ppt)$"
)

#: 文档附件（2026-10-29）：与图片走**同一个根目录**、同一套引用形状，区别只有两处：
#: 文档不读字节转 `data:` URL（模型拿不到 PDF 的图像语义），而是把**绝对路径**交给模型，
#: 由它用 `read_file` 读（提取逻辑在 ``tools/document_extract``）。
#:
#: 旧版 .doc/.xls/.ppt 也允许上传：提取层会明确回"请另存"，比让用户在上传阶段就卡住更好懂。
DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"})

#: 文档单文件上限（2026-10-29）：multipart 流式落盘，内存不吃紧；取值低于提取层的
#: `MAX_DOCUMENT_BYTES`（64 MB），保证"传上来的就能读"。
MAX_UPLOAD_DOCUMENT_BYTES = 50 * 1024 * 1024

#: 与 ``tools/document_extract`` 同一套魔数口径（判格式只看文件头，不信扩展名）。
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: 文档附件的媒体类型：只用于接口回显与日志，**不参与"怎么交给模型"的判定**
#: （文档一律给绝对路径，不转 data URL）。
_DOCUMENT_MEDIA: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
}

#: 会话 id 的形状（仓库里就是 32 位十六进制；这里放宽到字母数字与 `-`/`_`，仍然禁掉路径分隔符）。
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


@dataclass(frozen=True)
class SavedAttachment:
    """一次成功的保存：**引用** + 服务端**自己判定**的媒体类型与字节数。

    ``url`` 只有对象存储后端才会给（2026-10-20）：那时 ``attachment_id`` 就是公开 URL，
    出站请求直接把 URL 交给模型，不再读字节。本地后端为 ``None``。
    """

    attachment_id: str
    media_type: str
    byte_count: int
    url: str | None = None


@dataclass(frozen=True)
class AttachmentBlob:
    """读回来的附件内容（**只有内容**：调用方已经知道自己在读哪个引用）。"""

    data: bytes
    media_type: str


@dataclass(frozen=True)
class LoadedAttachment:
    """读回来的附件 + **它的引用**（2026-10-16）。

    为什么要把引用一起带上：图像字节进了用户消息的**内容块**（`data:` URL ✗），而内容块里
    **没有**引用 ✗ —— 于是"这条消息带了哪张图"在刷新之后无从得知 ✗，页面也就画不出缩略图 ✗。
    消息里塞的是 `data:` URL 这一件事不能改（模型要的是图像本身 ✗），所以引用得**另外**记下来 ✔。
    """

    attachment_id: str
    media_type: str
    data: bytes
    #: 对象存储后端给的公开 URL（2026-10-20）：有它就不必把字节读进来再编码。
    url: str | None = None


@dataclass(frozen=True)
class FileAttachment:
    """随消息提交的**文档**引用（2026-10-29）：只有路径，没有字节。

    为什么和图片分成两种类型：图片要走 `data:` URL 进内容块（模型要的是图像本身），
    文档只能给路径让模型自己 `read_file`（端点不保证支持文档内容块）。两种情况
    从存储到出站**没有一步相同**，塞进同一个类型只会到处写 `if is_document`。
    """

    attachment_id: str
    path: Path
    media_type: str
    byte_count: int
    #: **展示名**（用户上传时的文件名，净化后）。只用于气泡与出站提示，不参与路径解析；
    #: 落盘名永远是内容哈希。取不到时回退到 `path.name`。
    name: str = ""


class AttachmentStore:
    """附件根目录下的一切读写。**唯一入口**，路径规则只在这里写一次。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @classmethod
    def under(cls, data_root: Path) -> AttachmentStore:
        """按**数据根**定位：``<data_root>/attachments``（2026-10-26）。

        参数从"配置文件所在目录"改成"数据根"：附件是应用自己产生的状态，不能跟着代码走
        （旧实现在 ``base_dir/data/attachments``，换一份检出就换一套附件）。
        """
        return cls(Path(data_root) / "attachments")

    @property
    def root(self) -> Path:
        return self._root

    def save(
        self, session_id: str, data: bytes, *, now: datetime | None = None
    ) -> SavedAttachment:
        """存一张图；返回它的引用（相对路径）。

        Raises:
            AttachmentError: 会话 id 不合法 / 不是受支持的图片 / 超过大小上限 / 当天张数超限。
        """
        session = self._require_session(session_id)
        if len(data) == 0:
            raise AttachmentError("这是一张空文件（0 字节）：没有内容可看。")
        if len(data) > MAX_UPLOAD_IMAGE_BYTES:
            # 不截断：截断的图会让模型看到错误内容，还会以为自己"看过了"。
            raise AttachmentError(
                f"图片太大（{len(data) // 1024} KB > 上限 {MAX_UPLOAD_IMAGE_BYTES // 1024} KB）："
                f"{SUPPORTED_NOTE}。请先缩小或压缩再上传"
                "（截图可以缩小视口重截，或先存成 JPEG）；大图会让模型请求被端点拒绝。"
            )
        media_type = detect_image_media_type(data)
        if media_type is None:
            raise AttachmentError(
                f"这不是受支持的图片（按文件头判断）：{SUPPORTED_NOTE}。"
                "如果它是文本或 HTML，请直接把内容贴进消息里。"
            )

        moment = now if now is not None else datetime.now().astimezone()
        day = moment.date()
        digest = hashlib.sha256(data).hexdigest()[:16]
        relative = f"{day:%Y/%m/%d}/{session}/{digest}.{EXTENSION_BY_MEDIA[media_type]}"
        target = self._root / relative

        # 同一天同一会话里同样的内容只落一份：命中已有文件就连"当天张数"都不再计一遍。
        if not target.is_file():
            if MAX_ATTACHMENTS_PER_DAY is not None and self.count_on(session, day) >= MAX_ATTACHMENTS_PER_DAY:
                raise AttachmentError(
                    f"这个会话今天已经上传了 {MAX_ATTACHMENTS_PER_DAY} 张图片（服务端上限）："
                    "请先处理已有的图，或明天再传。"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return SavedAttachment(
            attachment_id=relative, media_type=media_type, byte_count=len(data)
        )

    def save_document(
        self,
        session_id: str,
        data: bytes,
        *,
        suffix_hint: str = "",
        now: datetime | None = None,
    ) -> SavedAttachment:
        """存一份用户上传的文档（2026-10-29）；返回引用。

        与 :meth:`save` 的两处差别：
        - 格式按**魔数**判定并据此决定落盘后缀（文件名里的扩展名只是一个 claim）；
        - 不把字节转成 ``data:`` URL —— 文档交给模型的是**绝对路径**，由它用 ``read_file``
          读（提取在 ``tools/document_extract``），服务端不替模型解析一遍。

        Raises:
            AttachmentError: 会话 id 不合法 / 不是受支持的文档 / 空文件 / 超过大小上限。
        """
        session = self._require_session(session_id)
        if len(data) == 0:
            raise AttachmentError("这是一个空文件（0 字节）：没有内容可读。")
        if len(data) > MAX_UPLOAD_DOCUMENT_BYTES:
            raise AttachmentError(
                f"文档太大（{len(data) // 1024 // 1024} MB > 上限 "
                f"{MAX_UPLOAD_DOCUMENT_BYTES // 1024 // 1024} MB）：请先拆分或压缩后再传。"
            )
        suffix = _document_suffix(data, suffix_hint)
        moment = now if now is not None else datetime.now().astimezone()
        day = moment.date()
        digest = hashlib.sha256(data).hexdigest()[:16]
        relative = f"{day:%Y/%m/%d}/{session}/{digest}{suffix}"
        target = self._root / relative
        # 同一天同一会话里同样的内容只落一份（与图片「同图不重复写」同一取向）。
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return SavedAttachment(
            attachment_id=relative,
            media_type=_DOCUMENT_MEDIA[suffix],
            byte_count=len(data),
        )

    def resolve_path(self, session_id: str, attachment_id: str) -> Path:
        """把引用解析成本机绝对路径（2026-10-29）：文档附件靠它交给模型读取。

        校验与 :meth:`read` 完全同源（形状、会话归属、文件存在）；区别只是**不读字节**，
        因为文档不需要服务端替模型解析，模型自己会调 ``read_file``。
        """
        session = self._require_session(session_id)
        match = _ID_RE.match(attachment_id)
        if match is None:
            raise AttachmentError(f"附件引用不合法：{attachment_id!r}")
        if match.group("session") != session:
            raise AttachmentError("这个附件不属于当前会话。")
        target = self._root / attachment_id
        if not target.is_file():
            raise AttachmentError("这个附件已经不在磁盘上了（可能被清理过）。")
        return target.resolve()

    def read(self, session_id: str, attachment_id: str) -> AttachmentBlob:
        """读回一张图。

        Raises:
            AttachmentError: 引用形状不合法、不属于这个会话、或文件不存在。
        """
        session = self._require_session(session_id)
        match = _ID_RE.match(attachment_id)
        if match is None:
            raise AttachmentError(f"附件引用不合法：{attachment_id!r}")
        if match.group("session") != session:
            # 引用里带着会话：不属于本会话的引用一律拒绝，别让一个 id 变成跨会话读文件的通道。
            raise AttachmentError("这个附件不属于当前会话。")
        target = self._root / attachment_id
        if not target.is_file():
            raise AttachmentError("这个附件已经不在磁盘上了（可能被清理过）。")
        data = target.read_bytes()
        media_type = detect_image_media_type(data)
        if media_type is None:
            raise AttachmentError(
                "这个附件不是图片：文档附件要用绝对路径交给 read_file 读，不要走图片接口。"
            )
        return AttachmentBlob(data=data, media_type=media_type)

    def count_on(self, session_id: str, day: date) -> int:
        """某会话某天已存了几张（按文件数算，与"同图去重"一致）。"""
        directory = self._root / f"{day:%Y/%m/%d}" / session_id
        if not directory.is_dir():
            return 0
        return sum(1 for path in directory.iterdir() if path.is_file())

    def delete_session(self, session_id: str) -> int:
        """删掉某会话**所有日期**下的附件，返回删掉的文件数（2026-10-29）。

        为什么必须扫三层日期目录：引用形状是 ``年/月/日/会话/哈希.扩展名``，一个会话
        的附件天然分散在多个日期目录里，只看今天会漏掉昨天之前传的那些。

        只删**本会话目录**，并把删空的会话目录摘掉；日期目录本身留着（其它会话可能还在用）。
        """
        session = self._require_session(session_id)
        if not self._root.is_dir():
            return 0
        removed = 0
        for year in self._root.iterdir():
            if not year.is_dir() or not year.name.isdigit():
                continue
            for month in year.iterdir():
                if not month.is_dir() or not month.name.isdigit():
                    continue
                for day in month.iterdir():
                    if not day.is_dir() or not day.name.isdigit():
                        continue
                    target = day / session
                    if not target.is_dir():
                        continue
                    for path in target.iterdir():
                        if path.is_file():
                            path.unlink()
                            removed += 1
                    try:
                        target.rmdir()
                    except OSError:  # pragma: no cover - 目录非空说明有并发写入
                        pass
        return removed

    @staticmethod
    def _require_session(session_id: str) -> str:
        text = str(session_id).strip()
        if _SESSION_RE.match(text) is None:
            raise AttachmentError(f"会话 id 不合法：{session_id!r}")
        return text


def sanitize_display_name(raw: str) -> str:
    """把客户端声明的文件名净化成可展示的短名（2026-10-29）。

    文件名是**不可信输入**：可能带路径分隔符、控制字符、几百字符的长串。这里只保留
    最后一段、剔掉不可打印字符、限 120 字符；结果为空时由调用方回退到落盘名。

    它**只用于展示**（气泡与出站提示），不参与任何路径解析——落盘名永远是内容哈希。
    """
    text = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text[:120]


def document_media_type(path: Path) -> str:
    """文档的媒体类型（只用于接口回显与提示文本）；不是文档返回空串。"""
    return _DOCUMENT_MEDIA.get(Path(path).suffix.lower(), "")


def _document_suffix(data: bytes, hint: str) -> str:
    """按魔数决定落盘后缀（2026-10-29）：扩展名只是一个 claim，不信它。

    为什么不用用户给的后缀：实测下载目录里 ``用户数据.xls`` 的文件头是 ``PK\\x03\\x04``
    （内容其实是 XLSX）。按后缀存会让 ``read_file`` 的魔数分流拿到错误的提示。
    """
    header = data[:8]
    if header.startswith(_PDF_MAGIC):
        return ".pdf"
    if header.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile as exc:
            raise AttachmentError(f"这个文件像是 ZIP 但打不开（可能已损坏）：{exc}") from exc
        if any(name.startswith("word/") for name in names):
            return ".docx"
        if any(name.startswith("xl/") for name in names):
            return ".xlsx"
        if any(name.startswith("ppt/") for name in names):
            return ".pptx"
        raise AttachmentError("这是 ZIP 容器，但不是 Word / Excel / PowerPoint 文档。")
    if header.startswith(_OLE_MAGIC):
        # 旧版 Office 三种格式的文件头完全相同，只能靠用户保留的扩展名区分。
        legacy = Path(hint).suffix.lower()
        if legacy in (".doc", ".xls", ".ppt"):
            return legacy
        raise AttachmentError(
            "这是旧版 Office 二进制格式，但从文件头分不出是 Word / Excel / PowerPoint："
            "请保留原文件的 .doc / .xls / .ppt 扩展名再传。"
        )
    raise AttachmentError(
        "这不是受支持的文档（按文件头判断）：支持 pdf / docx / xlsx / pptx，"
        "以及旧版 doc / xls / ppt。"
    )

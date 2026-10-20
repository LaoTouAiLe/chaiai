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
import re
from collections.abc import Iterable
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
    r"(?P<session>[0-9a-f]{8,64})/(?P<digest>[0-9a-f]{16})\.(?P<ext>png|jpg|gif|webp)$"
)

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


class AttachmentStore:
    """附件根目录下的一切读写。**唯一入口**，路径规则只在这里写一次。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @classmethod
    def under(cls, base_dir: Path) -> AttachmentStore:
        """按 ``base_dir``（= ``config.yaml`` 所在目录）定位：``data/attachments``。"""
        return cls(Path(base_dir) / "data" / "attachments")

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
            raise AttachmentError("这个附件在磁盘上已经不是受支持的图片了。")
        return AttachmentBlob(data=data, media_type=media_type)

    def count_on(self, session_id: str, day: date) -> int:
        """某会话某天已存了几张（按文件数算，与"同图去重"一致）。"""
        directory = self._root / f"{day:%Y/%m/%d}" / session_id
        if not directory.is_dir():
            return 0
        return sum(1 for path in directory.iterdir() if path.is_file())

    def delete_session(self, session_id: str) -> int:
        """删掉某会话**所有日期**下的附件，返回删掉的文件数。

        只在会话被删除时调用：归档不删（归档的会话仍可查看历史与图）。
        """
        session = self._require_session(session_id)
        removed = 0
        for day_dir in self._day_dirs():
            target = day_dir / session
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

    def _day_dirs(self) -> Iterable[Path]:
        """所有**日期目录**（``年/月/日``）。缺目录时给空集合，不抛异常。"""
        if not self._root.is_dir():
            return ()
        return (
            day_dir
            for year in self._root.iterdir()
            if year.is_dir()
            for month in year.iterdir()
            if month.is_dir()
            for day_dir in month.iterdir()
            if day_dir.is_dir()
        )

    @staticmethod
    def _require_session(session_id: str) -> str:
        text = str(session_id).strip()
        if _SESSION_RE.match(text) is None:
            raise AttachmentError(f"会话 id 不合法：{session_id!r}")
        return text

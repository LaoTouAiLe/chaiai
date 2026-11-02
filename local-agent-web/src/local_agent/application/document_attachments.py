"""文档附件的保存与运行期引用装配，供应用服务调用。"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from local_agent.attachments import FileAttachment, document_media_type, sanitize_display_name

if TYPE_CHECKING:
    from local_agent.application.service import _RunEnv
    from local_agent.persistence.session_store import Session


def save_document_attachment(
    service: Any, session_id: str, data: bytes, *, filename: str = ""
) -> dict[str, Any]:
    """存一份用户上传的文档（2026-10-29）：返回引用。

    与图片的三处差别：
    - **只走本地后端**，即使图片选了对象存储：交给模型的是绝对路径，
      S3 URL 对模型没有意义（它读不了远端 PDF 的文本）；
    - 格式按魔数判定，文件名里的扩展名只用来区分旧版 doc/xls/ppt；
    - 落盘即止，不转 ``data:`` URL。
    """
    store = service._require_attachments()
    service._session_or_raise(session_id)
    saved = store.save_document(session_id, data, suffix_hint=filename)
    return {
        "attachment_id": saved.attachment_id,
        "media_type": saved.media_type,
        "byte_count": saved.byte_count,
        "url": None,
    }


def load_file_attachments(
    service: Any, session: Session, env: _RunEnv, refs: Sequence[tuple[str, str]]
) -> _RunEnv:
    """把随消息提交的**文档引用**解析成本机绝对路径（2026-10-29）。

    与 :meth:`_load_attachments` 分开的三条理由：
    ① 不读字节、也不检查"连接是否支持图片输入"——文档交给模型的是路径；
    ② 引用只认本会话（`AttachmentStore.resolve_path` 强校验形状与会话前缀）；
    ③ 空列表零开销，不带文件的提交路径逐字节不变。
    """
    if not refs:
        return env
    store = service._require_attachments()
    resolved: list[FileAttachment] = []
    for attachment_id, display_name in refs:
        ident = str(attachment_id)
        target = store.resolve_path(session.session_id, ident)
        resolved.append(
            FileAttachment(
                attachment_id=ident,
                path=target,
                media_type=document_media_type(target),
                byte_count=target.stat().st_size,
                # 展示名用**客户端声明的那个**（净化后）：气泡与出站提示都要让人/模型
                # 认得出是哪个文件。拿不到就回退到落盘名（内容哈希）。
                name=sanitize_display_name(display_name) or target.name,
            )
        )
    return replace(env, files=tuple(resolved))

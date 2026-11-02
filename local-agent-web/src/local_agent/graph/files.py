"""文档引用的出站物化（2026-10-29）。

**为什么文档和图片走两条完全不同的路**：图片能进 `image_url` 内容块，因为图像输入是
OpenAI 兼容端点的事实标准；文档（file/document 输入）不是——多数兼容端点不支持。
所以文档只把**绝对路径**交给模型，附一句说明，由它自己调 `read_file`
（提取在 ``tools/document_extract``）。

**为什么不在 State 里直接拼文本**：State 的消息内容就是页面气泡正文（投影读的是
``message.content``，不是 ``conversation_turns.question``）。拼进去用户就会在自己的
提问下方看到一段机器生成的路径清单。所以引用放在 ``additional_kwargs``，只在
**出站副本**上物化——与图片的 ``_IMAGE_REFS_KEY`` 完全同一个口径。

（用户提问本身不进气泡的那条老路是另一回事：侧聊上下文用 SystemMessage 承载，
见 ``nodes`` 里的注释。）
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AnyMessage

#: 文档引用挂在 ``additional_kwargs`` 的这个键上（与 ``_IMAGE_REFS_KEY`` 并列）。
_FILE_REFS_KEY = "local_agent_file_refs"

_FILE_HINT = (
    "用户上传了以下文件，它们就在本机磁盘上（可直接读取，不要猜内容、不要说看不到）：\n"
)


def _file_refs_of(message: AnyMessage) -> list[Mapping[str, Any]]:
    meta = getattr(message, "additional_kwargs", None) or {}
    refs = meta.get(_FILE_REFS_KEY)
    return [ref for ref in refs if isinstance(ref, Mapping)] if isinstance(refs, list) else []


def _file_hint_text(refs: list[Mapping[str, Any]]) -> str:
    """拼出给模型看的那段说明。

    三个要点，缺一条模型就可能不读文件：
    1. **绝对路径**（工作区外也要给全路径，它才解析得了）；
    2. **文件大小**——让它判断"这份 36 MB 的 PDF 到底值不值得整份读"；
    3. **下一步动作**（read_file）与 PDF 的按页说明，省掉一次试探。
    """
    lines = [_FILE_HINT.rstrip("\n")]
    for ref in refs:
        path = str(ref.get("path") or "")
        if not path:
            continue
        suffix = str(ref.get("suffix") or "")
        size = int(ref.get("size") or 0)
        # 展示名放在路径前面（2026-10-29）：用户说的是"报告.docx"，落盘名是内容哈希，
        # 不给出这层对应关系，模型对不上"用户提到的那个文件"和"磁盘上的这个文件"。
        # 与落盘名相同（没拿到原名）时不重复一遍。
        name = str(ref.get("name") or "")
        if name and name != path.replace("\\", "/").rsplit("/", 1)[-1]:
            lines.append(f"- {name} → {path}（{suffix or '未知格式'}，{_human_size(size)}）")
        else:
            lines.append(f"- {path}（{suffix or '未知格式'}，{_human_size(size)}）")
    lines.append(
        "需要内容就用 read_file 读；PDF 默认一次给 5 页，更多用 start_page 指定；"
        "旧版 .doc / .xls / .ppt 读不了，需要用户另存为 .docx / .xlsx / .pptx。"
    )
    return "\n".join(lines)


def _human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} 字节"


def materialize_message_files(message: AnyMessage) -> AnyMessage:
    """把文档引用物化成消息末尾的一段提示文本；没有引用时**原样返回同一个对象**。

    只作用于送给模型的副本（``graph.snapshot`` 里调用），不写回 State——
    Checkpoint 里始终只有引用，页面气泡也不会看到这段文字。
    """
    refs = _file_refs_of(message)
    if not refs:
        return message
    hint = _file_hint_text(refs)
    content = message.content
    if isinstance(content, str):
        merged: Any = f"{content}\n\n{hint}" if content.strip() else hint
    elif isinstance(content, list):
        merged = [*content, {"type": "text", "text": hint}]
    else:  # pragma: no cover - 现有代码只会产出 str / list
        merged = hint
    meta = {
        key: value
        for key, value in (getattr(message, "additional_kwargs", None) or {}).items()
        if key != _FILE_REFS_KEY
    }
    return message.model_copy(update={"content": merged, "additional_kwargs": meta})

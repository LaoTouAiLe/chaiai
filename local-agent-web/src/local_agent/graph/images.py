"""图片消息的处理（2026-10-26 从 `snapshot.py` 拆出）。

为什么单独一个模块：它回答的是「消息里的图片引用怎么读、怎么落成 data URL」，
与「这一轮请求该带多少上下文」是两件事。原先临时放在 `snapshot.py` 只是因为
`_resolve_message_images` 被窗口预算调用，属于同一批搬迁，主题上并不属于快照。

依赖方向：`snapshot.py` 用这里的 `_resolve_message_images`，本模块**不反向依赖**快照与节点。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeGuard

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    ToolMessage,
)

from local_agent.graph.state import (
    RuntimeContext,
)
from local_agent.media import data_url
from local_agent.memory.short_term import (
    turn_starts,
)
from local_agent.message_flags import (
    DEFAULT_IMAGE_CONTEXT_TURNS,
    is_tool_image_message,
)


def _resolve_message_images(
    messages: Sequence[AnyMessage], context: RuntimeContext
) -> list[AnyMessage]:
    """出站前的图片解析：最近 K 轮物化，更早的换成占位。

    只作用于**送给模型的副本**，不写回 State——Checkpoint 里始终只有引用。
    """
    messages = _tool_images_after_replies(messages)
    turns = int(getattr(context, "image_context_turns", DEFAULT_IMAGE_CONTEXT_TURNS) or 1)
    turns = max(1, turns)
    starts = turn_starts(messages)
    keep_from = starts[-turns] if len(starts) >= turns else 0
    reader = getattr(context, "image_reader", None)
    return [
        _materialize_message_images(message, keep=index >= keep_from, reader=reader)
        for index, message in enumerate(messages)
    ]


def _materialize_message_images(
    message: AnyMessage,
    *,
    keep: bool,
    reader: Callable[[Mapping[str, Any]], tuple[bytes, str] | None] | None,
) -> AnyMessage:
    """把一条消息里的图片引用物化成 `data:` URL（或按轮次替换成占位）。返回**新消息**。

    两类来源都要处理：

    - 新格式：`additional_kwargs` 里的引用（State 里只有它）；
    - 旧数据：消息内容里已有的 `image_url`（历史 Checkpoint）。工具结果里的图**永远剥离**，
      因为端点只允许 user 消息带图——旧会话正是踩在这上面。
    """
    refs = _image_refs_of(message)
    blocks = _content_blocks_of(message)
    inline = [block for block in blocks if _is_image_block(block)]
    if not refs and not inline:
        return message
    is_tool = isinstance(message, ToolMessage)
    kept: list[Any] = []
    for block in blocks:
        if not _is_image_block(block):
            kept.append(block)
        elif is_tool:
            kept.append({"type": "text", "text": _TOOL_IMAGE_OMITTED_NOTE})
        elif keep:
            kept.append(block)
        else:
            kept.append({"type": "text", "text": _IMAGE_OMITTED_NOTE})
    if refs:
        kept.extend(
            _read_image_refs(refs, reader)
            if keep and not is_tool
            else [{"type": "text", "text": _IMAGE_OMITTED_NOTE}]
        )
    meta = {
        key: value
        for key, value in (getattr(message, "additional_kwargs", None) or {}).items()
        if key != _IMAGE_REFS_KEY
    }
    return message.model_copy(update={"content": kept, "additional_kwargs": meta})


def _tool_images_after_replies(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """兼容历史交错图片：完整工具批次的回复必须紧跟 assistant。

    只移动内部图片帧，不跨真实用户/assistant 消息，不补造缺失回复，也不修改 State。
    """
    ordered: list[AnyMessage] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        ordered.append(message)
        index += 1
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        expected = {call["id"] for call in message.tool_calls}
        end = index
        replies: list[ToolMessage] = []
        images: list[HumanMessage] = []
        while end < len(messages):
            following = messages[end]
            if isinstance(following, ToolMessage) and following.tool_call_id in expected:
                replies.append(following)
            elif isinstance(following, HumanMessage) and is_tool_image_message(following):
                images.append(following)
            else:
                break
            end += 1
        if images and len(replies) == len(expected) and {
            reply.tool_call_id for reply in replies
        } == expected:
            ordered.extend(replies)
            ordered.extend(images)
        else:
            ordered.extend(messages[index:end])
        index = end
    return ordered


def _is_image_block(part: object) -> TypeGuard[dict[str, Any]]:
    """`TypeGuard` 而不是 `bool`（2026-10-26）：调用方靠它把 `list[str | dict]` 收窄成
    `list[dict]`（`_split_image_blocks` 的返回值形状），否则两个分支的类型都糊在一起。"""
    return isinstance(part, Mapping) and part.get("type") == "image_url"


_IMAGE_REFS_KEY = "local_agent_image_refs"


_IMAGE_OMITTED_NOTE = "（这张图片已不在本次上下文中；如需再看，请让用户重新发送）"


_TOOL_IMAGE_OMITTED_NOTE = "（这条工具结果里的图片已从请求中移除：端点只允许用户消息携带图片）"


def _image_refs_of(message: AnyMessage) -> list[Mapping[str, Any]]:
    meta = getattr(message, "additional_kwargs", None) or {}
    refs = meta.get(_IMAGE_REFS_KEY)
    return [ref for ref in refs if isinstance(ref, Mapping)] if isinstance(refs, list) else []


def _content_blocks_of(message: AnyMessage) -> list[Any]:
    content = message.content
    if isinstance(content, list):
        return list(content)
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return []


def _read_image_refs(
    refs: Sequence[Mapping[str, Any]],
    reader: Callable[[Mapping[str, Any]], tuple[bytes, str] | str | None] | None,
) -> list[Any]:
    """按引用取图片：本地引用读字节拼 `data:` URL，对象存储引用直接给 URL。

    读不到就给占位（不让一张旧图搞崩整轮）。返回 ``str`` 表示"这已经是一个可用的
    http(s) 地址"，不必也不该再下载一遍——端点会自己去取。
    """
    blocks: list[Any] = []
    for ref in refs:
        loaded: tuple[bytes, str] | str | None = None
        if reader is not None:
            try:
                loaded = reader(ref)
            except Exception:  # noqa: BLE001 - 读不到是"这张图没了"，不是整轮失败
                loaded = None
        if isinstance(loaded, str):
            blocks.append({"type": "image_url", "image_url": {"url": loaded}})
            continue
        if loaded is None:
            blocks.append({"type": "text", "text": "（这张图片已无法读取，可能已被清理）"})
            continue
        data, media_type = loaded
        blocks.append({"type": "image_url", "image_url": {"url": data_url(media_type, data)}})
    return blocks

"""会话摘要生成（架构 §10.5；开发文档 T16）。

架构 §10.5 要求的摘要内容：任务目标、约束、结论和未完成事项。本模块只负责"把一段对话
渲染成文本 → 让模型写摘要 → 清洗与限长"；**什么时候摘要、摘要写到 State 的哪个字段、
失败怎么降级**由 ``graph.nodes.compression_node`` 决定（失败与降级口径见该节点）。

摘要输入是"已有摘要 + 新增的旧消息"：滚动更新，避免每次重压全部历史，也避免重复劳动
（架构 §10.5 只要求摘要覆盖旧消息，不要求每次重建）。
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from local_agent.exceptions import LocalAgentError

#: 摘要指令；要求模型只输出摘要正文，便于直接落库。
SUMMARIZE_INSTRUCTION = (
    "你在为一段多轮对话生成会话摘要，供后续轮次替代旧消息使用。\n"
    "请只输出摘要正文（不要前后缀、不要解释你做了什么），并覆盖：\n"
    "1. 用户的任务目标与当前进展；\n"
    "2. 明确的约束、偏好与已确认的结论；\n"
    "3. 未完成事项与需要用户确认的点。\n"
    "不要编造对话里没有的信息；信息不足时直接省略该点。\n"
)

#: 生成摘要时带上的已有摘要前缀（没有历史摘要时不出现）。
_PREVIOUS_SUMMARY_TEMPLATE = "【已有摘要（需要合并进来）】\n{summary}\n\n【此后新增的对话】\n"

#: 截断标记：模型没遵守长度要求时，明确标出"这里被截断了"。
_TRUNCATION_MARK = "…（摘要已截断）"


def summarize_conversation(
    model: BaseChatModel,
    *,
    messages: Sequence[AnyMessage],
    previous_summary: str | None,
    max_chars: int,
) -> str:
    """把一段对话压缩成会话摘要。

    Args:
        model: 用来生成摘要的聊天模型（来自 RuntimeContext 的适配器）。
        messages: 需要被摘要覆盖的旧消息（**不含**保留原文的最近轮次）。
        previous_summary: 已有的会话摘要，会作为待合并内容一起给出。
        max_chars: ``memory.compression.summary_max_chars``，摘要长度上限。

    Returns:
        清洗（去首尾空白）并限长后的摘要正文。

    Raises:
        LocalAgentError: 没有可摘要的消息、模型返回空内容或不是 AI 消息。
    """
    body = render_conversation(messages).strip()
    previous = (previous_summary or "").strip()
    if not body and not previous:
        raise LocalAgentError("没有可摘要的对话内容")
    text = body if not previous else _PREVIOUS_SUMMARY_TEMPLATE.format(summary=previous) + body
    response = model.invoke(
        [SystemMessage(content=SUMMARIZE_INSTRUCTION), HumanMessage(content=text)]
    )
    if not isinstance(response, AIMessage):
        raise LocalAgentError("摘要模型没有返回 AI 消息")
    summary = _content_text(response).strip()
    if not summary:
        raise LocalAgentError("摘要模型返回了空内容")
    return _clamp(summary, max_chars)


def render_conversation(messages: Sequence[AnyMessage]) -> str:
    """把消息渲染成给摘要模型看的纯文本（角色 + 内容；工具调用写上工具名）。"""
    lines: list[str] = []
    for message in messages:
        text = _content_text(message)
        calls = _tool_call_names(message)
        if calls:
            text = f"{text}（工具调用：{'、'.join(calls)}）".strip()
        lines.append(f"{_role_of(message)}: {text}")
    return "\n".join(lines)


def _role_of(message: AnyMessage) -> str:
    if isinstance(message, ToolMessage):
        return f"工具结果({getattr(message, 'name', '') or 'unknown'})"
    if isinstance(message, HumanMessage):
        return "用户"
    if isinstance(message, AIMessage):
        return "助手"
    if isinstance(message, SystemMessage):
        return "系统"
    return type(message).__name__


def _content_text(message: AnyMessage) -> str:
    """取消息的文本内容；多模态内容只取其中的文本部分。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, Sequence):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return " ".join(parts)


def _tool_call_names(message: AnyMessage) -> list[str]:
    calls = getattr(message, "tool_calls", None)
    if not calls:
        return []
    names: list[str] = []
    for call in calls:
        name = call.get("name") if isinstance(call, dict) else None
        names.append(str(name) if name else "unknown")
    return names


def _clamp(text: str, max_chars: int) -> str:
    """限长：超过 ``max_chars`` 时保留头部并标出截断（不静默丢尾部）。

    任何**正数**配置都必须满足 ``len(result) <= max_chars``：配置小到放不下截断标记时直接硬截断，
    否则"限长"本身就是假的（复审实测：``max_chars=1`` 仍返回完整标记）。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= len(_TRUNCATION_MARK):
        return text[:max_chars]
    keep = max_chars - len(_TRUNCATION_MARK)
    return f"{text[:keep]}{_TRUNCATION_MARK}"

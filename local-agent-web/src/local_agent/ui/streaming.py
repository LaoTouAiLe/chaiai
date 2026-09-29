"""最终回答的流式展示（T23；架构 §5.3、§11.7）。

**范围（刻意收紧）**：只流式展示**最终自然语言回答**。planner、reflection、工具调用、工具结果、
隐藏推理（``reasoning_content``）一律不流式展示——它们要么不是给用户看的内容，要么会把中间态
（"我先读一下这个文件"）或模型的隐藏推理推到页面上。

**为什么只接两个调用点**：``chat_node``（闲聊回答）与 ``reflection_node`` 的收尾合成是**唯一**两处
"模型输出就是最终回答"的地方。``agent_decision_node`` 的调用既可能产出回答、也可能产出工具调用
（同一轮里还可能先写一句中间说明），**事先无法区分**，所以不流式。

**四条硬规则（T23 验收）**：

1. **不逐块写 Checkpoint**：增量只进页面 sink，节点返回的仍是**一条**完整 ``AIMessage``；
2. **不支持 / 首块前失败** → 退化为**一次**普通完整调用（能力不支持不记事件，真失败才记）；
3. **中途失败**（已经产生过文本）→ ``discard()`` 丢掉页面上的不完整投影，再**普通调用重试一次**，
   把异常类型交给调用方记一条**可见降级事件**；
4. **退化只重调模型**：绝不重放工具、不重写记忆（这两个调用点本来也不执行工具与记忆写入）。

sink 用 contextvar 传入：网页在提交/审批恢复那一次设置、图内部读取；离开作用域自动失效，
因此 Streamlit 重绘、刷新与恢复都不会重新调用模型。

聚合方式与 LangChain 的流式聚合一致（``AIMessageChunk`` 相加后转成普通 ``AIMessage``）：
这样落进 Checkpoint 的那条消息与非流式路径**同形**（含 ``additional_kwargs``，思考模式的
``reasoning_content`` 因此也不会丢）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from local_agent.context.token_budget import estimate_messages_tokens
from local_agent.llm.compat import stream_placeholder_fields
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.stream_context import AnswerSink, current_sink, use_sink
from local_agent.llm.usage_recording import (
    begin_model_call,
    finish_model_call,
    note_first_token,
    record_model_call,
)

__all__ = ["AnswerSink", "StreamedAnswer", "current_sink", "stream_answer", "use_sink"]


@dataclass(frozen=True)
class StreamedAnswer:
    """一次"最终回答"调用的结果。"""

    message: AIMessage
    #: 是否真的按增量产出过文本
    streamed: bool
    #: 中途失败的异常类型（调用方据此记可见降级事件）；``None`` 表示没有降级
    failure: str | None = None
    #: 推送过多少块（测试与记录用）
    chunks: int = 0


def stream_answer(
    model: Any,
    messages: Sequence[BaseMessage],
    *,
    capabilities: ModelCapabilities | None = None,
    enabled: bool = True,
) -> StreamedAnswer:
    """产出最终回答：能流就流，不能就一次性拿全（规则见模块 docstring）。

    Args:
        model: 聊天模型适配器（``RuntimeContext.model_adapter``）。
        messages: 出站消息（系统提示 + 窗口 + 收尾指令）。
        capabilities: 启动时探测的模型能力；``streaming=False`` 时直接走普通调用。
        enabled: 配置开关（``llm.streaming``）。**节点传配置**、页面只负责装 sink：
            "要不要流式"由配置说了算，缺少页面 sink 只是没有展示出口。

    Returns:
        聚合后的**一条** ``AIMessage`` + 是否流式 + 降级信息。
    """
    sink = current_sink()
    supported = capabilities is None or capabilities.streaming
    streamer = getattr(model, "stream", None)
    estimated = estimate_messages_tokens(list(messages))
    if not enabled or sink is None or not supported or not callable(streamer):
        message = record_model_call(
            lambda: _invoke(model, messages), estimated_prompt_tokens=estimated, messages=messages
        )
        return StreamedAnswer(message=message, streamed=False)

    aggregated: Any = None
    chunks = 0
    message_id: str | None = None
    handle = begin_model_call(estimated_prompt_tokens=estimated, messages=messages)
    try:
        for chunk in streamer(list(messages)):
            text = _chunk_text(chunk)
            if text:
                if message_id is None:
                    begin = getattr(sink, "begin", None)
                    if callable(begin):
                        message_id = str(begin())
                chunks += 1
                note_first_token(handle)
                sink.push(text)
            aggregated = chunk if aggregated is None else aggregated + chunk
    except Exception as exc:  # noqa: BLE001 —— 流式失败必须退化为普通调用，不中断本轮
        if handle is not None:
            handle.usage_from(aggregated)
        finish_model_call(handle, status="error")
        retry_of = handle.model_call_id if handle is not None else None
        sink.discard()
        if not chunks:
            # 首块前失败：页面上什么都没有，直接普通调用（不算降级，不记事件）
            message = record_model_call(
                lambda: _invoke(model, messages),
                estimated_prompt_tokens=estimated,
                retry_of=retry_of,
                messages=messages,
            )
            return StreamedAnswer(message=message, streamed=False)
        # 中途失败：丢掉不完整投影，普通调用重试一次，并让调用方记一条可见降级事件
        fallback = record_model_call(
            lambda: _invoke(model, messages),
            estimated_prompt_tokens=estimated,
            retry_of=retry_of,
            messages=messages,
        )
        if message_id is not None:
            fallback.id = message_id
        return StreamedAnswer(
            message=fallback,
            streamed=True,
            failure=type(exc).__name__,
            chunks=chunks,
        )
    if handle is not None:
        handle.usage_from(aggregated)
    finish_model_call(handle, status="ok")
    message = _to_message(aggregated)
    if message_id is not None:
        message.id = message_id
    flush = getattr(sink, "flush", None)
    if callable(flush):
        flush()
    # 供应商扩展字段：真增量流式会丢掉它们（框架只在非流式解析路径上提取）。只要这个供应商
    # "确实用过该字段"，就给流式回答补空值占位——否则下一轮请求会被 400 拒绝（T23 实测）。
    for key, value in stream_placeholder_fields(model).items():
        message.additional_kwargs.setdefault(key, value)
    return StreamedAnswer(message=message, streamed=True, chunks=chunks)


def _chunk_text(chunk: Any) -> str:
    """只取**正文**：工具调用参数块与隐藏推理都不算最终回答。"""
    if getattr(chunk, "tool_call_chunks", None):
        return ""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and "text" in block
        )
    return ""


def _to_message(aggregated: Any) -> AIMessage:
    """把聚合块转成普通 ``AIMessage``：与非流式路径**同形**（含 ``additional_kwargs``）。

    流式块是 ``AIMessageChunk``（``AIMessage`` 的子类）。统一转成普通消息，落进 Checkpoint 与
    页面渲染的那条回答才和"不流式"时完全一样；思考模式的 ``reasoning_content`` 也随
    ``additional_kwargs`` 一起带过去，下一轮请求不需要额外补字段。
    """
    if aggregated is None:
        return AIMessage(content="")
    content = getattr(aggregated, "content", "")
    if isinstance(content, list):
        text = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and "text" in block
        )
    else:
        text = content if isinstance(content, str) else str(content)
    return AIMessage(
        content=text,
        additional_kwargs=dict(getattr(aggregated, "additional_kwargs", {}) or {}),
        response_metadata=dict(getattr(aggregated, "response_metadata", {}) or {}),
        # 用量必须带过去：聚合块的 ``usage_metadata`` 是流式链路**唯一**的实际用量来源
        # （流式分片的 ``response_metadata`` 里没有原始 ``token_usage``）。漏掉它就会
        # 违反 §15.6 的停止条件"流式链路不得丢用量"。
        usage_metadata=getattr(aggregated, "usage_metadata", None),
        id=getattr(aggregated, "id", None),
    )


def _invoke(model: Any, messages: Sequence[BaseMessage]) -> AIMessage:
    """普通完整调用（退化路径）：与非流式路径共用同一个入口。"""
    response = model.invoke(list(messages))
    if isinstance(response, AIMessage):
        return response
    return AIMessage(content=str(getattr(response, "text", response)))

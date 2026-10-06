"""工具绑定调用的流式聚合：实时发布思考/正文，完整聚合工具请求。"""

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.utils import message_chunk_to_message

from local_agent.llm.compat import stream_placeholder_fields
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.stream_context import current_sink
from local_agent.llm.usage_recording import (
    begin_model_call,
    finish_model_call,
    note_first_token,
    record_model_call,
)
from local_agent.ui.streaming import StreamedAnswer, _chunk_reasoning


def stream_decision(
    model: Any,
    messages: Sequence[BaseMessage],
    *,
    capabilities: ModelCapabilities | None,
    enabled: bool,
    estimated_prompt_tokens: int,
    tools_schema_fingerprint: str | None,
) -> StreamedAnswer:
    """保留工具参数、用量和扩展字段；流式失败只重试模型，工具尚未执行。

    思考与正文只作为未完成消息的展示增量。工具参数继续完整聚合后交给节点；
    是否执行工具、是否完成任务由图的原有校验决定，不由增量事件决定。
    """
    sink = current_sink()
    supported = capabilities is None or capabilities.streaming
    recording: dict[str, Any] = {
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "tools_schema_fingerprint": tools_schema_fingerprint,
        "messages": messages,
    }
    if not enabled or sink is None or not supported:
        response = record_model_call(lambda: model.invoke(list(messages)), **recording)
        return StreamedAnswer(message=response, streamed=False)

    aggregated: AIMessageChunk | None = None
    pieces: list[str] = []
    message_id: str | None = None
    failure: str | None = None
    handle = begin_model_call(**recording)
    try:
        for chunk in model.stream(list(messages)):
            if not isinstance(chunk, AIMessageChunk):
                raise TypeError("工具决策流未返回 AIMessageChunk")
            aggregated = chunk if aggregated is None else aggregated + chunk
            reasoning = _chunk_reasoning(chunk)
            if chunk.text or reasoning:
                if message_id is None:
                    begin = getattr(sink, "begin", None)
                    if callable(begin):
                        message_id = str(begin())
                note_first_token(handle)
            push_reasoning = getattr(sink, "push_reasoning", None)
            if reasoning and callable(push_reasoning):
                push_reasoning(reasoning)
            if chunk.text:
                pieces.append(chunk.text)
                sink.push(chunk.text)
        if aggregated is None:
            raise ValueError("工具决策流没有返回消息")
        response = message_chunk_to_message(aggregated)
        if not isinstance(response, AIMessage):
            raise TypeError("工具决策流未返回 AIMessage")
    except Exception as exc:  # noqa: BLE001 -- 未执行任何工具，可以重试模型一次
        if handle is not None:
            handle.usage_from(aggregated)
        finish_model_call(handle, status="error")
        failure = type(exc).__name__
        sink.discard()
        response = record_model_call(
            lambda: model.invoke(list(messages)),
            retry_of=handle.model_call_id if handle is not None else None,
            **recording,
        )
        pieces = [response.text] if response.text else []
        if message_id is None and (response.text or _chunk_reasoning(response)):
            begin = getattr(sink, "begin", None)
            if callable(begin):
                message_id = str(begin())
        push_reasoning = getattr(sink, "push_reasoning", None)
        if callable(push_reasoning):
            push_reasoning(_chunk_reasoning(response))
        if response.text:
            sink.push(response.text)
    else:
        if handle is not None:
            handle.usage_from(response)
        finish_model_call(handle)
        for key, value in stream_placeholder_fields(model).items():
            response.additional_kwargs.setdefault(key, value)

    if message_id is not None:
        response.id = message_id
    flush = getattr(sink, "flush", None)
    if callable(flush):
        flush()
    return StreamedAnswer(
        message=response, streamed=True, failure=failure, chunks=len(pieces),
    )

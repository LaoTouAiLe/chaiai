"""工具绑定调用的流式聚合：实时发布思考/正文，完整聚合工具请求。"""

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.utils import message_chunk_to_message

from local_agent.cancellation import CancellationToken
from local_agent.llm.cancellable import TurnCancelled, invoke_checked, iter_chunks, stream_supports
from local_agent.llm.compat import stream_placeholder_fields
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.stream_context import current_sink
from local_agent.llm.usage_recording import (
    begin_model_call,
    finish_model_call,
    note_first_token,
    record_model_call,
)
from local_agent.ui.streaming import StreamedAnswer, _chunk_reasoning, cancelled_answer


def stream_decision(
    model: Any,
    messages: Sequence[BaseMessage],
    *,
    capabilities: ModelCapabilities | None,
    enabled: bool,
    estimated_prompt_tokens: int,
    tools_schema_fingerprint: str | None,
    cancellation: CancellationToken | None = None,
) -> StreamedAnswer:
    """保留工具参数、用量和扩展字段；流式失败只重试模型，工具尚未执行。

    思考与正文只作为未完成消息的展示增量。工具参数继续完整聚合后交给节点；
    是否执行工具、是否完成任务由图的原有校验决定，不由增量事件决定。

    ``cancellation`` 非空时逐块判定取消（§11.7 的传输层流式）：**取消不重试**，
    未闭合的工具调用一律丢掉（截断的参数不是有效调用），只保留已产出的正文（§17.4）。
    """
    sink = current_sink()
    supported = capabilities is None or capabilities.streaming
    display = enabled and sink is not None
    # 需要流式读取的两种情形：有展示出口，或这次调用要可中断（§11.7）。不支持流式就如实降级。
    streamable = supported and stream_supports(model)
    recording: dict[str, Any] = {
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "tools_schema_fingerprint": tools_schema_fingerprint,
        "messages": messages,
    }
    # 用局部变量收窄类型：没有展示出口时（只为可中断而流式）一次 push / discard 都不该发生。
    active_sink = sink if display else None
    if not streamable or (active_sink is None and cancellation is None):
        try:
            response = invoke_checked(lambda: record_model_call(lambda: model.invoke(list(messages)), **recording), cancellation)
        except TurnCancelled as stopped:
            return cancelled_answer(model, stopped.response, stopped.reason)
        return StreamedAnswer(message=response, streamed=False)

    aggregated: AIMessageChunk | None = None
    pieces: list[str] = []
    control_sent = ""
    message_id: str | None = None
    failure: str | None = None
    handle = begin_model_call(**recording)
    try:
        for chunk in iter_chunks(model, messages, cancellation=cancellation, supported=streamable):
            if not isinstance(chunk, AIMessageChunk):
                raise TypeError("工具决策流未返回 AIMessageChunk")
            aggregated = chunk if aggregated is None else aggregated + chunk
            reasoning = _chunk_reasoning(chunk)
            control_text = _task_control_text(aggregated)
            control_delta = control_text[len(control_sent):] if control_text.startswith(control_sent) else ""
            if active_sink is not None:
                if chunk.text or reasoning or control_delta:
                    if message_id is None:
                        begin = getattr(active_sink, "begin", None)
                        if callable(begin):
                            message_id = str(begin())
                    note_first_token(handle)
                push_reasoning = getattr(active_sink, "push_reasoning", None)
                if reasoning and callable(push_reasoning):
                    push_reasoning(reasoning)
                if chunk.text:
                    pieces.append(chunk.text)
                    active_sink.push(chunk.text)
                if control_delta:
                    pieces.append(control_delta)
                    active_sink.push(control_delta)
                    control_sent = control_text
        if aggregated is None:
            raise ValueError("工具决策流没有返回消息")
        response = message_chunk_to_message(aggregated)
        if not isinstance(response, AIMessage):
            raise TypeError("工具决策流未返回 AIMessage")
    except TurnCancelled as stopped:
        # 取消：**不重试**、**不留工具调用**。只保留已产出的正文——截断的工具参数一旦当成
        # 真调用交下去，就是在被取消之后又执行了一个不能核对的动作（和 dsh 丢掉未闭合工具调用同一取舍）。
        if aggregated is not None and control_sent:
            aggregated = aggregated.model_copy(update={"content": "".join(pieces)})
        return cancelled_answer(model, aggregated, stopped.reason, handle=handle, message_id=message_id, chunks=len(pieces))
    except Exception as exc:  # noqa: BLE001 -- 未执行任何工具，可以重试模型一次
        if cancellation is not None and cancellation.cancelled:
            return cancelled_answer(model, aggregated, cancellation.refusal_reason(), handle=handle,
                                    message_id=message_id, chunks=len(pieces))
        if handle is not None:
            handle.usage_from(aggregated)
        finish_model_call(handle, status="error")
        failure = type(exc).__name__
        if active_sink is not None:
            active_sink.discard()
        try:
            response = invoke_checked(lambda: record_model_call(
                lambda: model.invoke(list(messages)),
                retry_of=handle.model_call_id if handle is not None else None, **recording,
            ), cancellation)
        except TurnCancelled as stopped:
            return cancelled_answer(model, stopped.response or aggregated, stopped.reason,
                                    message_id=message_id, chunks=len(pieces))
        display_text = response.text or _task_control_text(response)
        pieces = [display_text] if display_text else []
        if active_sink is not None:
            if message_id is None and (display_text or _chunk_reasoning(response)):
                begin = getattr(active_sink, "begin", None)
                if callable(begin):
                    message_id = str(begin())
            push_reasoning = getattr(active_sink, "push_reasoning", None)
            if callable(push_reasoning):
                push_reasoning(_chunk_reasoning(response))
            if display_text:
                active_sink.push(display_text)
    else:
        if handle is not None:
            handle.usage_from(response)
        finish_model_call(handle)
        for key, value in stream_placeholder_fields(model).items():
            response.additional_kwargs.setdefault(key, value)

    if message_id is not None:
        response.id = message_id
    flush = getattr(active_sink, "flush", None)
    if callable(flush):
        flush()
    return StreamedAnswer(
        message=response, streamed=True, failure=failure, chunks=len(pieces),
    )


def _task_control_text(message: AIMessage) -> str:
    """仅投影主模型声明交付/提问的文本；其它工具参数不作为回答展示。"""
    for call in message.tool_calls:
        args = call.get("args", {})
        if call.get("name") == "task_control" and args.get("action") in {"complete", "need_input"}:
            text = args.get("message")
            if isinstance(text, str):
                return text
    return ""

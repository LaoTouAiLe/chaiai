"""同一次模型输出的实时展示（T39，沿用 T23 的聚合与降级边界）。

正文与供应商实际返回的 reasoning_content 分通道投影；工具参数不推到页面。
增量不写 Checkpoint，也不判断任务完成；完整 AIMessage（含原扩展字段和用量）
仍由节点提交。失败丢弃临时展示，仅重试模型一次，不重放工具与记忆。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from local_agent.cancellation import CancellationToken
from local_agent.context.token_budget import estimate_messages_tokens
from local_agent.llm.cancellable import TurnCancelled, invoke_checked, iter_chunks, stream_supports
from local_agent.llm.compat import REASONING_CONTENT_KEY, stream_placeholder_fields
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.stream_context import AnswerSink, current_sink, use_sink
from local_agent.llm.usage_recording import (
    ModelCallHandle,
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
    #: 本轮在调用中途被取消（§17.4）：``message`` 是取消前已产出的部分（可能为空），
    #: 调用方据此把这一轮收敛成终态 `cancelled`，而不是当成失败去重试或记错误事件。
    cancelled: bool = False
    #: 取消原因（用户给的或默认说明），只用于展示
    cancel_reason: str | None = None


def stream_answer(
    model: Any,
    messages: Sequence[BaseMessage],
    *,
    capabilities: ModelCapabilities | None = None,
    enabled: bool = True,
    cancellation: CancellationToken | None = None,
) -> StreamedAnswer:
    """产出最终回答：能流就流，不能就一次性拿全（规则见模块 docstring）。

    Args:
        model: 聊天模型适配器（``RuntimeContext.model_adapter``）。
        messages: 出站消息（系统提示 + 窗口 + 收尾指令）。
        capabilities: 启动时探测的模型能力；``streaming=False`` 时走普通调用，**该次调用不可中断**。
        enabled: 展示开关（``llm.streaming``）。**节点传配置**、页面只负责装 sink：
            "要不要逐字显示"由配置说了算，缺少页面 sink 只是没有展示出口。
        cancellation: 本轮取消令牌（§17.4）。给了它就**逐块读**（§11.7 的传输层流式）：
            令牌触发时停止迭代并关闭流，返回 ``cancelled=True`` 的部分结果——**不重试模型**
            （重试会把"停止"变成"再跑一次"）。

    Returns:
        聚合后的**一条** ``AIMessage`` + 是否流式 + 降级信息。``cancelled=True`` 时 ``message``
        是取消前已产出的部分（可能为空），调用方据此产出终态。
    """
    sink = current_sink()
    supported = capabilities is None or capabilities.streaming
    estimated = estimate_messages_tokens(list(messages))
    # 展示出口 = 配置开着**且**页面装了 sink（决定要不要逐字显示）。用局部变量收窄类型：
    # 没有展示出口时（脚本路径、关掉流式）下面一次 push / discard 都不该发生——照 `sink` 直接调
    # 会在"只为可中断而流式"的路径上空指针。
    active_sink = sink if enabled else None
    # 需要流式读取的两种情形：有展示出口，或这次调用要可中断（§11.7）。
    # **不支持流式就不假装能中断**：退回一次普通调用，如实降级。
    streamable = supported and stream_supports(model)
    if not streamable or (active_sink is None and cancellation is None):
        try:
            message = invoke_checked(lambda: record_model_call(
                lambda: _invoke(model, messages), estimated_prompt_tokens=estimated, messages=messages
            ), cancellation)
        except TurnCancelled as stopped:
            return cancelled_answer(model, stopped.response, stopped.reason)
        return StreamedAnswer(message=message, streamed=False)

    aggregated: Any = None
    chunks = 0
    message_id: str | None = None
    handle = begin_model_call(estimated_prompt_tokens=estimated, messages=messages)
    try:
        for chunk in iter_chunks(model, messages, cancellation=cancellation, supported=streamable):
            text = _chunk_text(chunk)
            reasoning = _chunk_reasoning(chunk)
            if text or reasoning:
                if active_sink is not None:
                    if message_id is None:
                        begin = getattr(active_sink, "begin", None)
                        if callable(begin):
                            message_id = str(begin())
                    note_first_token(handle)
                    push_reasoning = getattr(active_sink, "push_reasoning", None)
                    if reasoning and callable(push_reasoning):
                        push_reasoning(reasoning)
                    if text:
                        active_sink.push(text)
                chunks += 1
            aggregated = chunk if aggregated is None else aggregated + chunk
    except TurnCancelled as stopped:
        # 取消：收尾这一次调用但**不重试**。已产出的部分原样留下（页面标「已停止」），
        # 用量照实记——已经花掉的 token 不退（§17.4）。
        return cancelled_answer(model, aggregated, stopped.reason, handle=handle, message_id=message_id, chunks=chunks)
    except Exception as exc:  # noqa: BLE001 —— 流式失败必须退化为普通调用，不中断本轮
        if cancellation is not None and cancellation.cancelled:
            return cancelled_answer(model, aggregated, cancellation.refusal_reason(), handle=handle,
                                    message_id=message_id, chunks=chunks)
        if handle is not None:
            handle.usage_from(aggregated)
        finish_model_call(handle, status="error")
        retry_of = handle.model_call_id if handle is not None else None
        if active_sink is not None:
            active_sink.discard()
        try:
            fallback = invoke_checked(lambda: record_model_call(
                lambda: _invoke(model, messages), estimated_prompt_tokens=estimated,
                retry_of=retry_of, messages=messages,
            ), cancellation)
        except TurnCancelled as stopped:
            return cancelled_answer(model, stopped.response or aggregated, stopped.reason,
                                    message_id=message_id, chunks=chunks)
        if not chunks:
            return StreamedAnswer(message=fallback, streamed=False)
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
    flush = getattr(active_sink, "flush", None)
    if callable(flush):
        flush()
    # 供应商扩展字段：真增量流式会丢掉它们（框架只在非流式解析路径上提取）。只要这个供应商
    # "确实用过该字段"，就给流式回答补空值占位——否则下一轮请求会被 400 拒绝（T23 实测）。
    for key, value in stream_placeholder_fields(model).items():
        message.additional_kwargs.setdefault(key, value)
    return StreamedAnswer(message=message, streamed=True, chunks=chunks)


def _chunk_reasoning(chunk: Any) -> str:
    raw = getattr(chunk, "additional_kwargs", {}).get(REASONING_CONTENT_KEY)
    return raw if isinstance(raw, str) else ""


def cancelled_answer(model: Any, aggregated: Any, reason: str, *,
                     handle: ModelCallHandle | None = None, message_id: str | None = None,
                     chunks: int = 0) -> StreamedAnswer:
    """取消统一保留正文与供应商字段，丢弃尚未执行的工具参数，不重试。"""
    if handle is not None:
        handle.usage_from(aggregated)
    finish_model_call(handle, status="aborted")
    partial = _to_message(aggregated)
    partial.tool_calls = []
    partial.invalid_tool_calls = []
    partial.additional_kwargs.pop("tool_calls", None)
    for key, value in stream_placeholder_fields(model).items():
        partial.additional_kwargs.setdefault(key, value)
    if message_id is not None:
        partial.id = message_id
    return StreamedAnswer(message=partial, streamed=chunks > 0, chunks=chunks,
                          cancelled=True, cancel_reason=reason or None)


def _chunk_text(chunk: Any) -> str:
    """只取正文：工具参数不展示，实际思考由独立通道处理。"""
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

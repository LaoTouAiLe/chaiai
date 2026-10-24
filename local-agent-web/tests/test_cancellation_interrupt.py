"""真中断：模型调用在流式块之间被打断（§11.7、§17.4 的"第三个动作"；2026-10-13）。

`tests/test_cancellation.py` 钉的是**两道门**——取消后不再**启动**新的工具调用。本文件钉的是
另外三件在这一天才成立的事：

1. **正在飞的模型调用被断掉**：取消发生在流式块之间，迭代停止、底层流被关闭，且**不重试**
   （重试会把"停止"变成"再跑一次"）；
2. **取消不是失败**：不留 `ErrorEvent`，本轮以 `cancelled` 收尾，已产出的部分原样保留；
3. **不可中断的路径如实降级**：能力探测为 `streaming=false` 时按调用边界停，不假装能立即中断。

参照实现（codex 的 `or_cancel`、dsh 的 `signal.throwIfAborted()`）的判定也都在流式块之间，
见 `ARCHITECTURE.md` §11.7 的引注。整轮"一个工具都不执行"的端到端证明仍在 `tests/test_api.py`。
"""

from __future__ import annotations

import asyncio
import gc
import json
import threading
from collections.abc import Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from openai import DefaultAsyncHttpxClient
from test_streaming import NO_STREAMING_CAPS, STREAMING_CAPS

from local_agent.application.projection import (
    project_final_result,
    project_session_state,
    project_turn_state,
)
from local_agent.cancellation import CancellationToken
from local_agent.config import AppConfig
from local_agent.graph import builder, nodes
from local_agent.llm import factory
from local_agent.llm.cancellable import _transport_loop
from local_agent.llm.usage_recording import ModelCallRecorder
from local_agent.ui.decision_streaming import stream_decision
from local_agent.ui.streaming import stream_answer


class _ScriptedStreamModel:
    """按块产出，并在第 ``cancel_after`` 块之后触发取消（等价于用户此刻按下停止）。

    ``closed`` 是"真断流"的证据：只有 `iterator.close()` 走到了生成器的 `finally` 才会为真——
    本地只是不再看这个流、让它在对端把内容推完，是不会走到这里的。
    """

    def __init__(
        self,
        *,
        chunks: tuple[str, ...],
        cancel_after: int | None = None,
        token: CancellationToken | None = None,
    ) -> None:
        self.chunks = chunks
        self.cancel_after = cancel_after
        self.token = token
        self.stream_calls = 0
        self.invoke_calls = 0
        self.closed = False

    def stream(self, messages: Any) -> Iterator[AIMessageChunk]:
        self.stream_calls += 1
        try:
            for index, text in enumerate(self.chunks, start=1):
                yield AIMessageChunk(content=text)
                if self.cancel_after is not None and index >= self.cancel_after:
                    if self.token is None:  # pragma: no cover - 用例构造错误
                        raise AssertionError("要触发取消就必须给令牌")
                    self.token.cancel("用户按下了停止")
        finally:
            self.closed = True

    def invoke(self, messages: Any) -> AIMessage:
        self.invoke_calls += 1
        return AIMessage(content="".join(self.chunks))


class _Runtime:
    """节点需要的 ``Runtime`` 包装（只暴露 ``context``）。"""

    def __init__(self, context: Any) -> None:
        self.context = context


def _context(
    tmp_path: Path,
    config: AppConfig,
    model: Any,
    *,
    token: CancellationToken | None = None,
) -> Any:
    return builder.build_runtime_context(
        app_config=config,
        model_adapter=model,
        workspace_root=tmp_path,
        current_cwd=tmp_path,
        model_capabilities=STREAMING_CAPS,
        cancellation=token,
    )


def _chat_state() -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "mode": "chat",
        "normalized_request": "你好",
        "messages": [HumanMessage(content="你好")],
    }


# --------------------------------------------------------------- 模型层：真断流


def test_cancel_between_chunks_stops_the_call_and_keeps_the_partial_text() -> None:
    """取消落在块之间：已产出的部分保留，之后的块不再产出，**也不重试**。"""
    token = CancellationToken()
    model = _ScriptedStreamModel(chunks=("甲", "乙", "丙"), cancel_after=2, token=token)

    answer = stream_answer(model, [HumanMessage(content="嗨")], cancellation=token)

    assert answer.cancelled is True
    assert answer.message.content == "甲乙"  # 取消后不再产出，也不补写未产出的内容
    assert model.closed is True  # 底层流被关闭：这是"断流"，不是"本地不看了"
    # 不重试：把"停止"变成"再跑一次"是最容易犯的错，这里用调用次数把它钉住。
    assert model.invoke_calls == 0


def test_already_cancelled_never_calls_the_model() -> None:
    """令牌已经触发时连流都不开——不花这次调用。"""
    token = CancellationToken()
    token.cancel("已经停了")
    model = _ScriptedStreamModel(chunks=("甲",))

    answer = stream_answer(model, [HumanMessage(content="嗨")], cancellation=token)

    assert answer.cancelled is True
    assert model.stream_calls == 0 and model.invoke_calls == 0


def test_unsupported_streaming_is_not_pretended_to_be_interruptible() -> None:
    """能力探测说不能流式：按调用边界停，**不假装**能立即中断（走普通调用）。"""
    token = CancellationToken()
    model = _ScriptedStreamModel(chunks=("完整回答",))

    answer = stream_answer(
        model, [HumanMessage(content="嗨")], capabilities=NO_STREAMING_CAPS, cancellation=token
    )

    assert answer.cancelled is False and answer.streamed is False
    assert model.stream_calls == 0 and model.invoke_calls == 1


# --------------------------------------------------------------- 节点层：收敛成终态


def test_chat_node_converges_to_cancelled_without_recording_an_error(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """取消后的闲聊轮：终态是 `cancelled`、保留半句回答、**不记错误事件**。"""
    token = CancellationToken()
    model = _ScriptedStreamModel(chunks=("甲", "乙", "丙"), cancel_after=1, token=token)
    context = _context(tmp_path, app_config, model, token=token)

    update = nodes.chat_node(_chat_state(), _Runtime(context))

    assert update["task_status"] == "cancelled"
    assert update["final_response"] == "甲"
    assert update["pending_tool_calls"] == []
    # 取消不是失败：记错误事件会把"你自己停的"说成"出错了"。
    assert "error_events" not in update


def test_decision_node_does_not_start_a_model_call_when_cancelled(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """已知取消时决策节点直接收敛：不再多花一次模型调用。"""
    token = CancellationToken()
    token.cancel("已经停了")
    model = _ScriptedStreamModel(chunks=("甲",))
    context = _context(tmp_path, app_config, model, token=token)

    update = nodes.agent_decision_node(
        {"run_id": "run-1", "mode": "tool", "messages": [HumanMessage(content="做点事")]},
        _Runtime(context),
    )

    assert update["task_status"] == "cancelled"
    assert update["final_response"]  # 空正文会让页面停在"正在生成回答…"
    assert model.stream_calls == 0 and model.invoke_calls == 0


# --------------------------------------------------------------- 投影层：终态但不算失败


def test_cancelled_turn_is_terminal_and_session_returns_to_idle() -> None:
    """`cancelled` 是终态、不是失败；会话回到 `idle`（用户随即可以发下一条）。"""
    state = {"run_id": "run-1", "task_status": "cancelled", "final_response": "半句回答"}

    assert project_turn_state(state) == "cancelled"
    assert project_session_state(state, has_live_interrupt=False) == "idle"
    result = project_final_result(state)
    assert result is not None, "被取消的一轮必须带着已产出的部分发出去"
    assert result.state == "cancelled"


@pytest.mark.parametrize("decision", [False, True])
def test_cancel_with_read_error_never_retries(decision: bool) -> None:
    token = CancellationToken()

    class Broken(_ScriptedStreamModel):
        def stream(self, messages: Any) -> Iterator[AIMessageChunk]:
            yield AIMessageChunk(content="partial")
            token.cancel("stop")
            raise OSError("connection closed")

    model = Broken(chunks=())
    if decision:
        answer = stream_decision(model, [HumanMessage(content="q")], capabilities=None,
                                 enabled=False, estimated_prompt_tokens=1,
                                 tools_schema_fingerprint=None, cancellation=token)
    else:
        answer = stream_answer(model, [HumanMessage(content="q")], enabled=False, cancellation=token)
    assert answer.cancelled and answer.message.text == "partial"
    assert model.invoke_calls == 0


def test_cancel_at_eof_is_terminal() -> None:
    token = CancellationToken()
    model = _ScriptedStreamModel(chunks=("partial",), cancel_after=1, token=token)
    answer = stream_answer(model, [HumanMessage(content="q")], cancellation=token)
    assert answer.cancelled and answer.message.text == "partial"


def test_cancelled_chat_preserves_message_identity_and_provider_fields(tmp_path: Path, app_config: AppConfig) -> None:
    token = CancellationToken()
    class Thinking(_ScriptedStreamModel):
        def stream(self, messages: Any) -> Iterator[AIMessageChunk]:
            yield AIMessageChunk(content="partial", id="stream-id", additional_kwargs={"reasoning_content": "thought"})
            token.cancel("stop")
    model = Thinking(chunks=())
    update = nodes.chat_node(_chat_state(), _Runtime(_context(tmp_path, app_config, model, token=token)))
    message = update["messages"][0]
    assert message.id == "stream-id"
    assert message.additional_kwargs["reasoning_content"] == "thought"
    assert message.text == "partial"


@pytest.mark.parametrize("before", [False, True])
def test_nonstreaming_checks_both_call_boundaries(before: bool) -> None:
    token = CancellationToken()
    class Normal(_ScriptedStreamModel):
        def invoke(self, messages: Any) -> AIMessage:
            response = super().invoke(messages)
            token.cancel("stop")
            return response
    model = Normal(chunks=("returned",))
    if before:
        token.cancel("stop")
    answer = stream_answer(model, [HumanMessage(content="q")], capabilities=NO_STREAMING_CAPS,
                           cancellation=token)
    assert answer.cancelled
    assert model.invoke_calls == (0 if before else 1)


def test_cancelled_graph_skips_memory_and_closes_turn_record(tmp_path: Path, app_config: AppConfig) -> None:
    from langgraph.checkpoint.memory import InMemorySaver
    token = CancellationToken()
    model = _ScriptedStreamModel(chunks=("partial", "discarded"), cancel_after=1, token=token)
    recorder = ModelCallRecorder(connection_id="fake", config_version="1", provider="fake",
                                 model_id="fake", endpoint_fingerprint="none", context_window_tokens=32000)
    context = builder.build_runtime_context(app_config=app_config, model_adapter=model,
        workspace_root=tmp_path, current_cwd=tmp_path, model_capabilities=STREAMING_CAPS,
        cancellation=token, usage_recorder=recorder)
    graph = builder.build_chat_graph(InMemorySaver())
    # 旧的自动路由入口只用于跳过固定闲聊的只读决策，从 chat_node 验证完整下游。
    model.invoke = lambda _messages: AIMessage(content='{"mode":"chat","reason":"test"}')  # type: ignore[method-assign]
    result = graph.invoke({"user_input": "hello", "thread_id": "cancel-review"},
        config={"configurable": {"thread_id": "cancel-review"}}, context=context)
    assert result["task_status"] == "cancelled"
    assert result["final_response"] == "partial"
    record = result["turn_records"][result["run_id"]]
    assert record.final_state == "cancelled" and record.final_at is not None
    assert record.turn_elapsed_ms is not None
    assert len(result["model_call_records"]) == 2  # 分类 + 被取消的回答，无记忆审核。


@pytest.mark.parametrize("first_chunk", [False, True])
def test_sdk_cancel_closes_http_while_waiting_and_model_is_reusable(
    first_chunk: bool, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 服务端必须直接观察 SDK 的连接；系统代理的上游连接可能在客户端取消后仍保留。
    http_client = DefaultAsyncHttpxClient(trust_env=False)
    monkeypatch.setattr(factory, "ChatOpenAIWithReasoning", partial(
        factory.ChatOpenAIWithReasoning, http_async_client=http_client,
    ))
    entered, disconnected = threading.Event(), threading.Event()
    calls: list[bool] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, format: str, *args: Any) -> None:
            pass
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append(True)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            def send(text: str, finish: str | None = None) -> None:
                body = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "fake",
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(body) + "\n\n").encode())
                self.wfile.flush()
            if len(calls) > 1:
                send("next", "stop")
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                return
            if first_chunk:
                send("partial")
            entered.set()
            self.connection.settimeout(5)
            try:
                if self.connection.recv(1) == b"":
                    disconnected.set()
            except ConnectionResetError:
                disconnected.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm = app_config.llm.model_copy(update={"base_url": f"http://127.0.0.1:{server.server_port}/v1"})
    model = factory.build_chat_model(llm, "fake-key")
    token = CancellationToken()
    answers: list[Any] = []
    errors: list[BaseException] = []
    def run() -> None:
        try:
            answers.append(stream_answer(model, [HumanMessage(content="q")], enabled=False, cancellation=token))
        except BaseException as exc:  # noqa: BLE001 -- 把工作线程错误传回测试线程断言。
            errors.append(exc)
    worker = threading.Thread(target=run, daemon=True)
    gc_was_enabled = gc.isenabled()
    gc.disable()  # 断连必须来自取消清理，不能依赖循环引用碰巧被回收。
    try:
        worker.start()
        assert entered.wait(5)
        token.cancel("stop")
        worker.join(2)
        assert not worker.is_alive() and not errors
        assert disconnected.wait(2), "取消必须关闭真实 HTTP 响应，不能只退出本地读取"
        assert answers[0].cancelled
        assert len(calls) == 1
        answer = stream_answer(model, [HumanMessage(content="next")], enabled=False, cancellation=CancellationToken())
        assert not answer.cancelled and answer.message.text == "next"
        assert len(calls) == 2
    finally:
        try:
            token.cancel("cleanup")
            server.shutdown(); server.server_close()
            worker.join(6)
            asyncio.run_coroutine_threadsafe(http_client.aclose(), _transport_loop()).result(timeout=5)
        finally:
            if gc_was_enabled:
                gc.enable()

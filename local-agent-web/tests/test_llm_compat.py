"""DeepSeek 思考模式的 `reasoning_content` 往返兼容（架构 §11.7）。

全部用 fixture 做确定性回归，不联网：直接喂原始响应对象、再检查下一轮请求 payload。
复现的是实测到的真实故障——带 ``tool_calls`` 的思考模型响应进入下一轮时丢了
``reasoning_content``，第二次请求被服务端 400 拒绝（``deepseek-flash`` / ``deepseek-v4-pro``
都在第一次工具往返之后失败）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)

from local_agent.llm.compat import (
    REASONING_CONTENT_KEY,
    ChatOpenAIWithReasoning,
    _response_reasoning,
)

REASONING = "先看 routers.py 再回答：用户问的是 decide_mode。"


def test_stream_reasoning_survives_aggregation_and_memory_followup() -> None:
    model = _model()
    aggregate = AIMessageChunk(content="")
    for delta in [
        {"role": "assistant", "content": "", "reasoning_content": "先看 "},
        {"reasoning_content": "routers.py。"},
        {"content": "最终回答"},
    ]:
        chunk = model._convert_chunk_to_generation_chunk(
            {"choices": [{"index": 0, "delta": delta}]}, AIMessageChunk, None
        )
        assert chunk is not None
        aggregate += chunk.message
    message = message_chunk_to_message(aggregate)
    assert message.text == "最终回答"
    assert message.additional_kwargs[REASONING_CONTENT_KEY] == "先看 routers.py。"
    payload = model._get_request_payload(
        [HumanMessage(content="问"), message, SystemMessage(content="只提取任务记忆")]
    )
    assert payload["messages"][1][REASONING_CONTENT_KEY] == "先看 routers.py。"


def test_stream_empty_reasoning_is_retained_without_fabricating_absent_fields() -> None:
    model = _model()
    for delta, expected in [({"content": "答"}, None), ({"reasoning_content": ""}, "")]:
        chunk = model._convert_chunk_to_generation_chunk(
            {"choices": [{"index": 0, "delta": delta}]}, AIMessageChunk, None
        )
        assert chunk is not None
        assert chunk.message.additional_kwargs.get(REASONING_CONTENT_KEY) == expected
    usage_chunk = model._convert_chunk_to_generation_chunk(
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        AIMessageChunk,
        None,
    )
    assert usage_chunk is not None
    assert REASONING_CONTENT_KEY not in usage_chunk.message.additional_kwargs


def _model() -> ChatOpenAIWithReasoning:
    """不联网的模型实例：只用来跑响应解析与请求 payload 构造。"""
    return ChatOpenAIWithReasoning(
        model="deepseek-flash",
        api_key="sk-test-not-used",  # type: ignore[arg-type]
        base_url="http://127.0.0.1:1/v1",
    )


def _response(*, reasoning: str | None, with_tool_calls: bool = True) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": "我读一下这个文件。"}
    if reasoning is not None:
        message[REASONING_CONTENT_KEY] = reasoning
    if with_tool_calls:
        message["tool_calls"] = [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "routers.py"}'},
            }
        ]
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _ai_message(reasoning: str | None, *, with_tool_calls: bool = True) -> AIMessage:
    message = AIMessage(
        content="我读一下这个文件。",
        tool_calls=(
            [
                {
                    "name": "read_file",
                    "args": {"path": "routers.py"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ]
            if with_tool_calls
            else []
        ),
    )
    if reasoning is not None:
        message.additional_kwargs[REASONING_CONTENT_KEY] = reasoning
    return message


# --------------------------------------------------------------------------- 响应侧


def test_response_reasoning_content_is_kept_on_the_message() -> None:
    """响应里的 ``reasoning_content`` 必须被收进消息（否则下一轮拼不回原请求）。"""
    result = _model()._create_chat_result(_response(reasoning=REASONING))

    message = result.generations[0].message
    assert message.additional_kwargs[REASONING_CONTENT_KEY] == REASONING
    assert [call["name"] for call in message.tool_calls] == ["read_file"]


def test_response_without_reasoning_content_stays_clean() -> None:
    """普通（非思考）模型的响应不新增任何字段：行为与改造前完全一致。"""
    result = _model()._create_chat_result(_response(reasoning=None))

    message = result.generations[0].message
    assert REASONING_CONTENT_KEY not in message.additional_kwargs


def test_response_reasoning_content_is_read_from_pydantic_model_extra() -> None:
    """openai SDK 把未声明字段放进 ``model_extra`` 时也要能取到（真实响应就是这个形态）。"""

    class _Message:
        def __init__(self) -> None:
            self.model_extra = {REASONING_CONTENT_KEY: REASONING}

    class _Choice:
        def __init__(self) -> None:
            self.message = _Message()

    class _Response:
        def __init__(self) -> None:
            self.choices = [_Choice()]

    assert _response_reasoning(_Response()) == [REASONING]
    # 完全没有该字段的对象返回 None（= 这个端点不提供该字段），不抛异常
    assert _response_reasoning(object()) == []


# --------------------------------------------------------------------------- 请求侧


def test_tool_call_message_carries_reasoning_content_into_the_next_request() -> None:
    """带 ``tool_calls`` 的消息在下一轮请求里必须带上 ``reasoning_content``（400 的根因）。"""
    payload = _model()._get_request_payload(
        [
            SystemMessage(content="系统"),
            HumanMessage(content="routers.py 里的 decide_mode 怎么判断模式？"),
            _ai_message(REASONING),
            ToolMessage(content='{"content": "..."}', tool_call_id="call-1"),
        ]
    )

    wire = payload["messages"]
    assert wire[2]["role"] == "assistant"
    assert wire[2][REASONING_CONTENT_KEY] == REASONING
    assert wire[2]["tool_calls"][0]["id"] == "call-1"
    # 其余消息不多带字段
    assert all(REASONING_CONTENT_KEY not in message for index, message in enumerate(wire) if index != 2)


def test_empty_reasoning_content_is_kept_and_sent_back() -> None:
    """实测故障（第二次请求被 400 的真正原因）：带 ``tool_calls`` 的响应回了空串 ``""``。

    按"非空才保留"处理会让字段直接消失，服务端按"字段是否存在"判定 → 400。
    所以空串必须原样留住、原样传回。
    """
    model = _model()
    message = model._create_chat_result(_response(reasoning="")).generations[0].message

    assert message.additional_kwargs[REASONING_CONTENT_KEY] == ""

    payload = model._get_request_payload(
        [HumanMessage(content="问"), message, ToolMessage(content="{}", tool_call_id="call-1")]
    )

    assert payload["messages"][1][REASONING_CONTENT_KEY] == ""


def test_absent_reasoning_content_is_not_fabricated() -> None:
    """字段**不存在**（非思考模型）与空串是两回事：前者不补，绝不凭空造一个空字段。"""
    assert _response_reasoning(_response(reasoning=None)) == [None]


def test_round_trip_from_response_to_the_next_request() -> None:
    """响应 → 消息 → 下一轮请求的完整往返（真实故障的最小复现）。"""
    model = _model()
    message = model._create_chat_result(_response(reasoning=REASONING)).generations[0].message

    payload = model._get_request_payload(
        [
            HumanMessage(content="问"),
            message,
            ToolMessage(content="{}", tool_call_id="call-1"),
        ]
    )

    assert payload["messages"][1][REASONING_CONTENT_KEY] == REASONING


def test_request_without_reasoning_content_is_unchanged() -> None:
    """历史消息（旧 Checkpoint、替身模型、非思考模型）没有该字段时不补空值。"""
    payload = _model()._get_request_payload(
        [HumanMessage(content="问"), _ai_message(None), ToolMessage(content="{}", tool_call_id="call-1")]
    )

    assert all(REASONING_CONTENT_KEY not in message for message in payload["messages"])


def test_plain_answer_message_also_carries_reasoning_content() -> None:
    """普通回答消息同样要回传该字段。

    实测证据：只补 ``tool_calls`` 消息后，第三个请求会因为**上一条普通回答消息**缺字段
    再报同一个 400——供应商要的是"它给过的字段原样传回"，与消息类型无关。
    """
    payload = _model()._get_request_payload(
        [HumanMessage(content="问"), _ai_message(REASONING, with_tool_calls=False)]
    )

    assert payload["messages"][1][REASONING_CONTENT_KEY] == REASONING


def test_deepseek_request_enforces_configured_output_cap_on_the_wire() -> None:
    captured = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAIWithReasoning(
            model="deepseek-flash", api_key="sk-test-not-used",  # type: ignore[arg-type]
            base_url="http://127.0.0.1:1/v1", max_completion_tokens=4096,
            http_client=client,
        )
        assert model.invoke([HumanMessage(content="test")]).content == "OK"
        assert model.bind(max_completion_tokens=16).invoke([HumanMessage(content="probe")]).content == "OK"
    assert [request.get("max_tokens") for request in captured] == [4096, 16]
    assert all("max_completion_tokens" not in request for request in captured)


def test_deepseek_stream_limit_and_endpoint_alias_are_compatible() -> None:
    model = ChatOpenAIWithReasoning(
        model="vendor-alias", api_key="sk-test-not-used",  # type: ignore[arg-type]
        base_url="https://api.deepseek.com/v1", max_completion_tokens=4096,
    )
    payload = model._get_request_payload([HumanMessage(content="test")], stream=True)
    assert payload["max_tokens"] == 4096
    assert payload["stream"] is True
    assert "max_completion_tokens" not in payload


def test_official_openai_output_parameter_is_preserved() -> None:
    model = ChatOpenAIWithReasoning(
        model="o4-mini", api_key="sk-test-not-used",  # type: ignore[arg-type]
        base_url="https://api.openai.com/v1", max_completion_tokens=4096,
    )
    payload = model._get_request_payload([HumanMessage(content="test")])
    assert payload["max_completion_tokens"] == 4096
    assert "max_tokens" not in payload


def test_deepseek_summary_keeps_output_budget_for_body_and_main_requests_unchanged() -> None:
    from local_agent.memory.summarizer import summarize_conversation

    captured = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        summary = "会话摘要" in body["messages"][0]["content"]
        disabled = body.get("thinking") == {"type": "disabled"}
        content = "目标：核对检索；未完成：核对路由" if summary and disabled else "" if summary else "main answer"
        return httpx.Response(200, json={
            "id": "summary", "object": "chat.completion", "created": 0,
            "model": "deepseek-flash", "choices": [{"index": 0,
                "finish_reason": "stop" if content else "length",
                "message": {"role": "assistant", "content": content,
                            "reasoning_content": "" if disabled else "thinking"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1 if content else 4096,
                      "total_tokens": 2 if content else 4097},
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAIWithReasoning(
            model="deepseek-flash", api_key="sk-test-not-used",  # type: ignore[arg-type]
            base_url="http://127.0.0.1:1/v1", max_completion_tokens=4096,
            extra_body={"vendor_setting": "preserved"}, http_client=client,
        )
        assert model.invoke([HumanMessage(content="first")]).content == "main answer"
        result = summarize_conversation(model, messages=[HumanMessage(content="查检索")],
                                        previous_summary=None, max_chars=160)
        assert result == "目标：核对检索；未完成：核对路由"
        assert model.invoke([HumanMessage(content="next")]).content == "main answer"
    assert "thinking" not in captured[0] and "thinking" not in captured[2]
    assert captured[1]["thinking"] == {"type": "disabled"}
    assert all(body["vendor_setting"] == "preserved" and body["max_tokens"] == 4096 for body in captured)
    assert model.extra_body == {"vendor_setting": "preserved"}


def test_summary_does_not_send_deepseek_options_to_other_providers() -> None:
    from local_agent.llm.usage_recording import use_model_purpose

    for name in ["o4-mini", "deepseek-r1"]:
        model = ChatOpenAIWithReasoning(
            model=name, api_key="sk-test-not-used",  # type: ignore[arg-type]
            base_url="https://other.example/v1", extra_body={"vendor_setting": "preserved"},
        )
        with use_model_purpose("summarize"):
            payload = model._get_request_payload([HumanMessage(content="summary")])
        assert payload["extra_body"] == {"vendor_setting": "preserved"}

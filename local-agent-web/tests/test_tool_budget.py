"""工具预算使用真实出站结构，避免伪压力和重复计数。"""

from __future__ import annotations

import json
from math import ceil
from typing import Annotated

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolArg, StructuredTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from local_agent.context import token_budget
from local_agent.context.token_budget import estimate_tokens, estimate_tools_tokens
from local_agent.graph import builder, nodes
from local_agent.llm.compat import ChatOpenAIWithReasoning
from local_agent.tools.result_types import ReadFileResult


def _tools(tmp_path, app_config):
    context = builder.build_runtime_context(
        app_config=app_config, model_adapter=None, workspace_root=tmp_path, current_cwd=tmp_path,
    )
    return context.tool_registry.tools_for("tool", context.agent_capabilities)


def test_budget_matches_tools_in_actual_http_request(tmp_path, app_config):
    """由真实模型适配器组请求，MockTransport 截获出站数据，不发起联网。"""
    tools = _tools(tmp_path, app_config)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "budget-call", "object": "chat.completion", "created": 1, "model": "budget-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAIWithReasoning(model="budget-test", api_key="test-only",
                                       base_url="https://budget.test/v1", http_client=client,
                                       max_retries=0)
        assert model.bind_tools(tools).invoke([HumanMessage(content="probe")]).content == "ok"
    wire_tools = requests[0]["tools"]
    assert len(wire_tools) == len(tools) and len(tools) >= 20
    actual = sum(estimate_tokens(json.dumps(t, ensure_ascii=False, default=str)) for t in wire_tools)
    assert estimate_tools_tokens(tools) == actual == estimate_tools_tokens(wire_tools)
    assert wire_tools == [convert_to_openai_tool(t) for t in tools]


def test_long_tool_description_is_counted_once():
    def function(path: str) -> str:
        return path

    function.__doc__ = "标记说明" * 500
    candidate = StructuredTool.from_function(function)
    assert candidate.args_schema.model_json_schema()["description"] == candidate.description
    payload = json.loads(token_budget._tool_schema_text(candidate))
    assert payload["function"]["description"] == candidate.description
    assert "description" not in payload["function"]["parameters"]
    assert estimate_tools_tokens([candidate]) == estimate_tools_tokens([payload])
    assert estimate_tools_tokens([candidate]) >= estimate_tokens(candidate.description)


def test_nested_arguments_and_distinct_parameter_descriptions_are_preserved():
    class Location(BaseModel):
        """模型使用的完整定位参数。"""

        path: str = Field(description="完整路径和空格必须保留")
        line: int = Field(ge=1, description="原始 1 基行号")

    class Query(BaseModel):
        """参数模型自身的说明不能叠加到工具说明之外。"""

        location: Location
        labels: list[str] = Field(description="所有筛选标签")

    def invoke(location: dict, labels: list[str]) -> str:
        return str(location)

    candidate = StructuredTool.from_function(invoke, name="locate", args_schema=Query,
                                             description="查找指定原始位置")
    wire = convert_to_openai_tool(candidate)
    text = token_budget._tool_schema_text(candidate)
    assert json.loads(text) == wire
    assert "完整路径和空格必须保留" in text and "所有筛选标签" in text
    assert estimate_tools_tokens([candidate]) == estimate_tokens(json.dumps(wire, ensure_ascii=False))


def test_injected_arguments_use_the_same_public_schema_as_binding():
    @tool
    def locate(path: str, internal: Annotated[str, InjectedToolArg]) -> str:
        """读取指定路径。"""
        return path

    payload = json.loads(token_budget._tool_schema_text(locate))
    assert payload == convert_to_openai_tool(locate)
    assert "internal" not in payload["function"]["parameters"]["properties"]
    assert "path" in payload["function"]["parameters"]["properties"]


def test_wrapped_tool_dictionary_keeps_strict_and_nested_constraints():
    payload = {"type": "function", "function": {
        "name": "find", "description": "只读定位", "strict": True,
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"path": {"type": "string", "description": "位置"}},
                       "required": ["path"]},
    }}
    before = json.dumps(payload, ensure_ascii=False)
    assert estimate_tools_tokens([payload]) == estimate_tokens(before)
    assert json.dumps(payload, ensure_ascii=False) == before


def test_empty_tool_list_costs_zero():
    assert estimate_tools_tokens([]) == 0


def test_conversion_error_does_not_silently_drop_real_tool_schema(monkeypatch):
    @tool
    def locate(path: str) -> str:
        """读取指定路径。"""
        return path

    def fail(tool):
        raise ValueError("invalid public schema")

    monkeypatch.setattr(token_budget, "convert_to_openai_tool", fail)
    with pytest.raises(ValueError, match="invalid public schema"):
        estimate_tools_tokens([locate])


def test_nonstandard_tool_fallback_still_counts_its_arguments():
    class FakeTool:
        name = "read_note"
        description = "读取文件"

        class args_schema:
            @staticmethod
            def model_json_schema():
                return {"type": "object", "properties": {"path": {"type": "string"}}}

    assert estimate_tools_tokens([FakeTool()]) > estimate_tokens(FakeTool.name)


def test_compatible_tool_with_broken_schema_keeps_metadata_fallback():
    class FakeTool:
        name = "read_note"
        description = "读取文件"

        class args_schema:
            @staticmethod
            def model_json_schema():
                raise ValueError("legacy schema unavailable")

    assert estimate_tools_tokens([FakeTool()]) == estimate_tokens("read_note\n读取文件")


def test_dictionary_schema_is_not_mistaken_for_an_empty_object():
    payload = {"type": "function", "function": {
        "name": "write", "description": "说明" * 200,
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
    }}
    assert estimate_tools_tokens([payload]) > 400


def test_roomy_enough_request_does_not_buy_a_summary_due_to_duplicate_descriptions(
    tmp_path, app_config,
):
    class MustNotSummarize:
        def invoke(self, messages):
            pytest.fail("实际输入未达到阈值，不应购买摘要")

    app_config.memory.compression.enabled = False
    app_config.llm.max_tokens = 4096
    context = builder.build_runtime_context(
        app_config=app_config, model_adapter=MustNotSummarize(), workspace_root=tmp_path,
        current_cwd=tmp_path,
    )
    messages = [HumanMessage(content="核对文件", id="r:user")]
    for i in range(2):
        call = str(i)
        messages.extend([
            AIMessage(content="", tool_calls=[{"name": "read_file", "id": call,
                                               "args": {"path": "note.txt"}}]),
            ToolMessage(name="read_file", tool_call_id=call, content=ReadFileResult(
                path="note.txt", content="value\n" * 20, line_count=20,
                first_line=1, last_line=20,
            ).model_dump_json()),
        ])
    state = {"agent_loop_version": 1, "run_id": "r", "run_user_message_id": "r:user",
             "mode": "tool", "messages": messages}
    tools = context.tool_registry.tools_for("tool", context.agent_capabilities)
    wire_tokens = sum(
        estimate_tokens(json.dumps(convert_to_openai_tool(candidate), ensure_ascii=False))
        for candidate in tools
    )
    duplicated_tokens = sum(
        estimate_tokens("\n".join((candidate.name, candidate.description,
                                  json.dumps(candidate.args_schema.model_json_schema(),
                                             ensure_ascii=False))))
        for candidate in tools
    )
    assert wire_tokens < duplicated_tokens

    # 先测非工具部分，把阈值放在真实出站与旧重复计数之间；工具清单增长不应让测试失真。
    uncompressed = nodes.compression_node(state, Runtime(context=context))
    usage = uncompressed["token_usage"]
    fixed_tokens = usage.prompt_tokens - usage.tool_definition_tokens
    target = fixed_tokens + (wire_tokens + duplicated_tokens) // 2
    compression = context.app_config.memory.compression
    context.app_config.llm.context_window_tokens = (
        context.app_config.llm.max_tokens + ceil(target / compression.trigger_ratio)
    )
    compression.enabled = True
    result = nodes.compression_node(state, Runtime(context=context))
    threshold = result["context_budget"].prompt_budget_tokens * compression.trigger_ratio
    assert fixed_tokens + wire_tokens < threshold < fixed_tokens + duplicated_tokens
    assert result["token_usage"].tool_definition_tokens == wire_tokens
    assert not result["context_budget"].triggered
    assert "compression_count" not in result and "tool_output_overrides" not in result
    assert result["context_budget"].summary_covered_messages == 0
    assert nodes._prompt_window(state, context).messages[-1].tool_call_id == "1"

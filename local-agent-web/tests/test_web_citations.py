import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from local_agent.context.web_citations import resolve_web_citations
from local_agent.graph.nodes import final_response_node
from local_agent.tools.web_identity import source_id


def state(status="ok", run="current"):
    url = "https://docs.example.com/intro"
    identity = source_id(url)
    answer = f"MCP connects tools [{identity}]."
    return {
        "run_id": "current", "run_user_message_id": "u", "mode": "tool",
        "messages": [HumanMessage(content="Explain MCP", id="u"),
                     AIMessage(content="", tool_calls=[{"name": "web_fetch", "args": {"url": url}, "id": "c"}]),
                     ToolMessage(content=json.dumps({"url": url, "title": "Official", "text": "MCP"}), name="web_fetch", tool_call_id="c"),
                     AIMessage(content=answer, id="answer")],
        "tool_events": [{"tool_call_id": "c", "tool_name": "web_fetch", "run_id": run, "status": status}],
    }, answer


def test_actual_source_marker_becomes_a_link_and_raw_answer_is_retained() -> None:
    data, answer = state()
    result = final_response_node(data)
    assert result["final_response"] == "MCP connects tools [1](https://docs.example.com/intro)."
    assert data["messages"][-1].text == answer
    assert result["reply_overrides"]["answer"] == result["final_response"]


def test_denied_failed_or_previous_run_cannot_authorize_citations() -> None:
    for status, run in [("denied", "current"), ("error", "current"), ("ok", "previous")]:
        data, answer = state(status, run)
        assert resolve_web_citations(data, answer) == "MCP connects tools [来源未核实]."


def test_invented_source_is_not_authorized_and_code_examples_remain_literal() -> None:
    data, answer = state()
    unknown = "[web-0000000000000000]"
    assert resolve_web_citations(data, answer + " " + unknown).endswith("[来源未核实]")
    assert resolve_web_citations(data, f"`{unknown}`\n```text\n{unknown}\n```") == f"`{unknown}`\n```text\n{unknown}\n```"

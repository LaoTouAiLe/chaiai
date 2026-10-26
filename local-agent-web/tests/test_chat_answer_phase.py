from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from local_agent.config import AppConfig
from local_agent.graph import builder, nodes


def test_chat_final_answer_has_explicit_phase_without_changing_tool_decisions(
    tmp_path: Path, app_config: AppConfig, model_factory,
) -> None:
    (tmp_path / "facts.txt").write_text("验证口令：蓝帆37", encoding="utf-8")
    model = model_factory(decision_tool_batches=[
        [{"name": "list_dir", "args": {"path": "."}, "id": "list"}],
        [{"name": "read_file", "args": {"path": "facts.txt"}, "id": "read"}],
    ], reply="验证口令是蓝帆37")
    choices: list[Any] = []
    sent: list[list[Any]] = []
    generate = model._generate

    def record_choices(messages, stop=None, run_manager=None, **kwargs):
        choices.append(kwargs.get("tool_choice"))
        sent.append(list(messages))
        return generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model._generate = record_choices
    context = builder.build_runtime_context(
        app_config=app_config, model_adapter=model, workspace_root=tmp_path, current_cwd=tmp_path,
    )
    result = builder.build_chat_graph().invoke(
        {"user_input": "读取文件中的验证口令", "session_mode": "chat"}, context=context,
    )
    assert [event.tool_name for event in result["tool_events"]] == ["list_dir", "read_file"]
    assert result["final_response"] == "验证口令是蓝帆37"
    assert choices[-1] == "none" and all(choice is None for choice in choices[:-1])
    assert any(isinstance(message, ToolMessage) for batch in sent[:-1] for message in batch)
    assert not any(isinstance(message, ToolMessage) or getattr(message, "tool_calls", []) for message in sent[-1])
    assert any("验证口令：蓝帆37" in message.text for message in sent[-1])
    answer_prompts = [text for text in model.seen_prompts if "【最终回答阶段】" in text]
    assert len(answer_prompts) == 1
    assert "不提供任何工具" in answer_prompts[0]
    assert "证据不足" in answer_prompts[0]
    assert any("先调用 list_dir 或 read_file" in text and "【最终回答阶段】" not in text
               for text in model.seen_prompts)


def test_final_phase_preserves_evidence_and_literal_protocol_examples(
    tmp_path: Path, app_config: AppConfig, model_factory,
) -> None:
    example = "协议示例：<｜DSML｜calls>，这不是已执行的调用。"
    model = model_factory(reply=example, reasoning="供应商原始思考")
    context = builder.build_runtime_context(
        app_config=app_config, model_adapter=model, workspace_root=tmp_path, current_cwd=tmp_path,
    )
    messages = [HumanMessage(content="解释 DSML 示例", id="question"),
                AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "facts.txt"}, "id": "read"}]),
                ToolMessage(content="真实读取结果", tool_call_id="read", name="read_file")]
    original = [item.model_dump() for item in messages]
    state = {"messages": messages, "user_input": "解释 DSML 示例", "mode": "chat", "session_mode": "chat"}
    result = nodes.chat_node(state, SimpleNamespace(context=context))
    assert result["messages"][0].content == example
    assert result["messages"][0].additional_kwargs["reasoning_content"] == "供应商原始思考"
    assert [item.model_dump() for item in messages] == original
    assert "真实读取结果" in model.seen_prompts[-1]
    assert not any(isinstance(item, SystemMessage) for item in messages)

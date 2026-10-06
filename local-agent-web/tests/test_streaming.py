"""T23：最终回答的流式展示（`local_agent.ui.streaming`）。

范围与口径（用户裁定的 T23 流式范围）：

- **只流式最终自然语言回答**：planner / reflection / 工具调用 / 工具结果 / 隐藏推理都不流式；
- **不逐块写 Checkpoint**：增量只进页面 sink，落进 State 的仍是**一条**完整 ``AIMessage``；
- **不支持 / 首块前失败** → 退化为一次普通完整调用（不记事件）；
- **中途失败** → 丢弃页面上不完整投影 + 普通调用重试一次 + 记一条**可见降级事件**；
- **退化不重放工具、不重写记忆**：工具次数、``tool_events``、记忆写入与非流式路径一致。

测试用 ``tests/conftest.py`` 的脚本化替身：它的 ``_stream`` 按同一套脚本产出确定性增量块，
拼起来必须等于整段回答。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from local_agent import scope
from local_agent.config import AppConfig
from local_agent.graph import builder, nodes
from local_agent.llm.models import ModelCapabilities
from local_agent.memory import long_term
from local_agent.ui.streaming import stream_answer, use_sink

STREAMING_CAPS = ModelCapabilities(
    tool_calling=True, structured_json_schema=True, streaming=True
)
NO_STREAMING_CAPS = ModelCapabilities(
    tool_calling=True, structured_json_schema=True, streaming=False
)


@dataclass
class RecordingSink:
    """记录推送与丢弃，供断言（等价于页面上那个占位符）。"""

    pushed: list[str] = field(default_factory=list)
    discards: int = 0

    def push(self, text: str) -> None:
        self.pushed.append(text)

    def discard(self) -> None:
        self.discards += 1


def _context(
    tmp_path: Path,
    config: AppConfig,
    model: Any,
    *,
    capabilities: ModelCapabilities = STREAMING_CAPS,
) -> Any:
    return builder.build_runtime_context(
        app_config=config,
        model_adapter=model,
        workspace_root=tmp_path,
        current_cwd=tmp_path,
        model_capabilities=capabilities,
    )


def _runtime(context: Any) -> Any:
    """节点需要的 ``Runtime`` 包装（只暴露 ``context``）。"""

    class _Runtime:
        def __init__(self, ctx: Any) -> None:
            self.context = ctx

    return _Runtime(context)


def _with_streaming(app_config: AppConfig, enabled: bool) -> AppConfig:
    data = app_config.model_dump()
    data["llm"]["streaming"] = enabled
    return AppConfig.model_validate(data)


# --------------------------------------------------------------------------- 单元：stream_answer


def test_stream_answer_pushes_chunks_and_returns_one_plain_message(model_factory) -> None:
    """能流就流：多块推给 sink，返回**一条普通 AIMessage**（与非流式同形）。"""
    model = model_factory(decision="chat", reply="这是一段流式回答内容")
    sink = RecordingSink()

    with use_sink(sink):
        answer = stream_answer(
            model, [HumanMessage(content="嗨")], capabilities=STREAMING_CAPS
        )

    assert answer.streamed is True
    assert answer.chunks >= 2  # 能看到多个增量块
    assert "".join(sink.pushed) == "这是一段流式回答内容"
    assert answer.message.content == "这是一段流式回答内容"
    assert isinstance(answer.message, AIMessage)
    assert not isinstance(answer.message, AIMessageChunk)  # 落库的是普通消息
    assert answer.failure is None and sink.discards == 0


def test_stream_answer_keeps_additional_kwargs_from_chunks(model_factory) -> None:
    """流式块里的供应商字段（如思考模式的 reasoning_content）要跟着进那条消息。"""
    model = model_factory(decision="chat", reply="答案")
    original = model._generate

    def generate(messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        result = original(messages, stop=stop, run_manager=run_manager, **kwargs)
        for generation in result.generations:
            generation.message.additional_kwargs["reasoning_content"] = "先想一想"
        return result

    model._generate = generate  # type: ignore[method-assign]
    with use_sink(RecordingSink()):
        answer = stream_answer(model, [HumanMessage(content="嗨")], capabilities=STREAMING_CAPS)

    # 字符串型 additional_kwargs 会**按块拼接**（与 SSE 的 reasoning 增量语义一致）：
    # 这里每块都带同一段，所以聚合后是重复的；真实供应商每块只给增量，拼起来正好是完整推理。
    assert "先想一想" in str(answer.message.additional_kwargs["reasoning_content"])


def test_stream_answer_preserves_provider_field_placeholder(model_factory) -> None:
    """真增量流式会丢供应商字段：见过该字段的供应商，流式回答要补空值占位（否则下一轮 400）。"""
    model = model_factory(decision="chat", reply="答案")

    with use_sink(RecordingSink()):
        without_flag = stream_answer(
            model, [HumanMessage(content="嗨")], capabilities=STREAMING_CAPS
        )
    assert "reasoning_content" not in without_flag.message.additional_kwargs

    model._uses_reasoning_content = True  # 等价于"这个供应商确实用过该字段"
    with use_sink(RecordingSink()):
        with_flag = stream_answer(
            model, [HumanMessage(content="嗨")], capabilities=STREAMING_CAPS
        )
    assert with_flag.message.additional_kwargs["reasoning_content"] == ""


def test_stream_answer_skips_without_capability_or_sink(model_factory) -> None:
    """能力不支持、或没有装 sink：直接普通调用，不推任何块。"""
    model = model_factory(decision="chat", reply="完整回答")
    sink = RecordingSink()

    with use_sink(sink):
        unsupported = stream_answer(
            model, [HumanMessage(content="嗨")], capabilities=NO_STREAMING_CAPS
        )
    assert unsupported.streamed is False and unsupported.chunks == 0
    assert sink.pushed == [] and unsupported.message.content == "完整回答"

    # 没装 sink（命令行/测试环境）：同样不流式
    without_sink = stream_answer(
        model_factory(decision="chat", reply="完整回答"),
        [HumanMessage(content="嗨")],
        capabilities=STREAMING_CAPS,
    )
    assert without_sink.streamed is False and without_sink.message.content == "完整回答"


def test_stream_answer_degrades_silently_when_first_chunk_fails(
    model_factory, monkeypatch
) -> None:
    """首块前失败：什么都没有显示过，退化为一次普通调用，**不记降级事件**。"""
    model = model_factory(decision="chat", reply="完整回答")
    sink = RecordingSink()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("首块前失败（测试故意触发）")

    monkeypatch.setattr(model, "_stream", boom)
    with use_sink(sink):
        answer = stream_answer(model, [HumanMessage(content="嗨")], capabilities=STREAMING_CAPS)

    assert answer.streamed is False and answer.failure is None
    assert answer.message.content == "完整回答"
    assert sink.pushed == [] and sink.discards == 1  # 丢弃过一次不完整投影


def test_stream_answer_mid_failure_discards_projection_and_retries_once(model_factory) -> None:
    """中途失败：丢弃不完整投影 → 普通调用**重试一次** → 交给调用方记可见降级事件。"""
    model = model_factory(
        decision="chat", reply="这是一段足够长的流式回答内容", fail_stream_after_chunks=1
    )
    sink = RecordingSink()

    with use_sink(sink):
        answer = stream_answer(model, [HumanMessage(content="嗨")], capabilities=STREAMING_CAPS)

    assert answer.streamed is True
    assert answer.failure == "RuntimeError"  # 调用方据此记事件
    assert sink.pushed and sink.discards == 1  # 先推过内容，随后丢弃
    assert answer.message.content == "这是一段足够长的流式回答内容"  # 退化后拿到完整回答
    # 一共两次模型调用：流式那次（中途失败）+ 退化的普通调用那次（重试**一次**）
    assert model.decision_calls == 2


# --------------------------------------------------------------------------- 节点：闲聊与收尾


def _chat_state() -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "mode": "chat",
        "normalized_request": "你好",
        "messages": [HumanMessage(content="你好")],
    }


def test_chat_node_streams_into_the_sink_and_returns_one_message(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """闲聊回答走流式：sink 收到增量，State 里仍只有**一条**完整消息。"""
    model = model_factory(decision="chat", reply="闲聊回答内容")
    context = _context(tmp_path, _with_streaming(app_config, True), model)
    sink = RecordingSink()

    with use_sink(sink):
        update = nodes.chat_node(_chat_state(), _runtime(context))

    assert len(update["messages"]) == 1
    assert update["messages"][0].content == "闲聊回答内容"
    assert len(sink.pushed) >= 2
    assert not update.get("error_events")


def test_chat_node_records_visible_event_on_mid_stream_failure(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """闲聊流式中途失败：回答仍是完整的，并留下一条可见降级事件。"""
    model = model_factory(decision="chat", reply="完整闲聊回答", fail_stream_after_chunks=1)
    context = _context(tmp_path, _with_streaming(app_config, True), model)
    sink = RecordingSink()

    with use_sink(sink):
        update = nodes.chat_node(_chat_state(), _runtime(context))

    assert update["messages"][0].content == "完整闲聊回答"
    assert sink.discards == 1
    events = list(update.get("error_events") or [])
    assert any("流式回答降级" in str(event["message"]) for event in events)
    assert any(str(event["stage"]).endswith(":stream") for event in events)


def test_chat_node_behaves_the_same_when_streaming_disabled(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """关闭流式（或没有 sink）：行为与改造前完全一致，不推块、不记事件。"""
    model = model_factory(decision="chat", reply="闲聊回答内容")
    context = _context(tmp_path, _with_streaming(app_config, False), model)
    sink = RecordingSink()

    with use_sink(sink):
        update = nodes.chat_node(_chat_state(), _runtime(context))

    assert update["messages"][0].content == "闲聊回答内容"
    assert sink.pushed == [] and sink.discards == 0
    assert not update.get("error_events")


# --------------------------------------------------------------------------- 端到端：与不流式一致


def _tool_scenario(app_config: AppConfig, *, streaming: bool) -> AppConfig:
    return _with_streaming(app_config, streaming)


def _run_graph(tmp_path: Path, config: AppConfig, model: Any) -> dict[str, Any]:
    graph = builder.build_chat_graph()
    context = _context(tmp_path, config, model)
    with use_sink(RecordingSink()):
        return dict(
            graph.invoke(
                {"user_input": "读取 note.txt 并告诉我内容"},
                context=context,
            )
        )


def test_streaming_keeps_tool_events_and_final_answer_identical(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """T23 验收：工具调用次数、``tool_events``、回答条数与非流式路径**一致**。"""
    (tmp_path / "note.txt").write_text("内容甲\n", encoding="utf-8")

    def build(streaming: bool):
        return model_factory(
            decision="tool",
            reply="依据工具结果：note.txt 的内容是内容甲",
            tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
        )

    plain = _run_graph(tmp_path, _tool_scenario(app_config, streaming=False), build(False))
    streamed = _run_graph(tmp_path, _tool_scenario(app_config, streaming=True), build(True))

    assert len(streamed["tool_events"]) == len(plain["tool_events"]) == 1
    assert len(streamed["tool_results"]) == len(plain["tool_results"]) == 1
    assert [str(item["tool_name"]) for item in streamed["tool_results"]] == ["read_file"]
    assert streamed["final_response"] == plain["final_response"]
    # Checkpoint 里只多出**一条**最终回答（不是每个增量块一条）
    answers = [
        message.content
        for message in streamed["messages"]
        if isinstance(message, AIMessage) and str(message.content).strip()
    ]
    assert answers.count(str(streamed["final_response"])) == 1
    assert len(answers) == len(
        [
            message.content
            for message in plain["messages"]
            if isinstance(message, AIMessage) and str(message.content).strip()
        ]
    )
    assert not streamed.get("error_events")


def test_streaming_does_not_repeat_memory_writes(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """退化/流式都不得重复写记忆：两种模式下长期库里的条数一致。"""
    data = app_config.model_dump()
    data["memory"]["long_term"].update({"enabled": True, "path": str(tmp_path / "memory.sqlite")})
    config = AppConfig.model_validate(data)

    def run(streaming: bool, memory_path: Path) -> int:
        data = config.model_dump()
        data["memory"]["long_term"]["path"] = str(memory_path)
        run_config = AppConfig.model_validate(data)
        model = model_factory(
            decision="chat",
            reply="好的，我记住了。",
            memory_requested=True,
            memory_intent=[
                {
                    "memory_type": "entity_fact",
                    "content": '{"entity_type": "project", "key": "test_runner", "value": "uv"}',
                    "subject": "chaiai",
                    "importance": 0.7,
                    "confidence": 0.9,
                }
            ],
        )
        graph = builder.build_chat_graph()
        context = _context(tmp_path, _with_streaming(run_config, streaming), model)
        with use_sink(RecordingSink()):
            graph.invoke({"user_input": "记住：本项目用 uv 跑测试"}, context=context)
        opened = long_term.open_store(memory_path)
        try:
            entity = opened.find_entity(
                entity_type="project",
                name="chaiai",
                scope=scope.SCOPE_PROJECT,
                project_id=scope.project_id_for(tmp_path),
            )
            return 0 if entity is None else len(opened.list_facts(entity_id=entity.id))
        finally:
            opened.close()

    plain_count = run(False, tmp_path / "plain.sqlite")
    streamed_count = run(True, tmp_path / "streamed.sqlite")

    assert plain_count == 1
    assert streamed_count == plain_count  # 流式不得多写一条


@pytest.mark.parametrize("decision", [False, True])
def test_reasoning_stream_failure_resets_both_channels_and_retries_only_model(decision):
    from local_agent.ui.decision_streaming import stream_decision
    class Sink:
        def __init__(self):
            self.text, self.reasoning, self.resets = "", "", 0
        def push(self, text):
            self.text += text
        def push_reasoning(self, text):
            self.reasoning += text
        def discard(self):
            self.text, self.reasoning = "", ""
            self.resets += 1
    class Model:
        def __init__(self):
            self.calls = 0
            self.seen = []
        def stream(self, messages):
            self.seen.append(messages)
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "失败的思考。"})
            yield AIMessageChunk(content="半句正文。")
            raise RuntimeError("模拟断流")
        def invoke(self, messages):
            self.seen.append(messages)
            self.calls += 1
            return AIMessage(content="完整回答。", additional_kwargs={"reasoning_content": "重试后思考。"})
    model, sink = Model(), Sink()
    messages = [HumanMessage(content="固定输入")]
    with use_sink(sink):
        if decision:
            result = stream_decision(model, messages, capabilities=None, enabled=True,
                                     estimated_prompt_tokens=10, tools_schema_fingerprint="fixed")
        else:
            result = stream_answer(model, messages)
    assert model.calls == 1 and model.seen == [messages, messages]
    assert sink.resets == 1 and "失败" not in sink.reasoning and "半句" not in sink.text
    assert result.message.text == "完整回答。"
    assert result.message.additional_kwargs["reasoning_content"] == "重试后思考。"

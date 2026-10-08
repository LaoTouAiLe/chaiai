"""供应商实际用量、缓存命中与整轮统计（架构 §15.6，T34）。

这个文件守住四件事，它们分别对应 §15.6 的停止条件：

1. **未上报 ≠ 上报为零**：键不存在是"未上报"（``None``），键存在且为 0 才是 0；
2. **DeepSeek 口径**：``prompt_tokens`` 含缓存命中，未缓存输入必须减去它，两种拼写都要认；
3. **流式不丢用量**：聚合块的 ``usage_metadata`` 必须活到最终消息上；
4. **汇总 = 明细加总**：整轮的 Token 由明细加总得出，任一项未知则整体未知，且不串轮。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from local_agent.application import projection as proj
from local_agent.application import service as app_service
from local_agent.application.service import ApplicationService
from local_agent.config import AppConfig
from local_agent.graph import builder
from local_agent.graph import nodes as graph_nodes
from local_agent.llm.stream_context import use_sink
from local_agent.llm.usage import extract_usage
from local_agent.llm.usage_recording import (
    DEFAULT_PURPOSE,
    PURPOSE_BY_NODE,
    ModelCallRecord,
    ModelCallRecorder,
    TurnRecord,
    begin_model_call,
    finish_model_call,
    record_model_call,
    request_fingerprints,
    tools_fingerprint,
    use_model_recorder,
)
from local_agent.persistence import checkpoint
from local_agent.persistence.request_ledger import RequestLedger
from local_agent.ui.streaming import _to_message, stream_answer

ModelFactory = Callable[..., object]

RUN = "run-1"
#: 产品级用途词表（§15.4：用途不得泄露内部节点名）。
PURPOSES = frozenset(
    {"chat", "tool_decision", "final_answer", "summarize", "planning", "reflection",
     "memory_intent", "memory_extraction", "retrieval", "routing"}
)


def _at(offset_ms: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=offset_ms)


# ---- 1. 提取与归一化 --------------------------------------------------------


def test_langchain_normalized_cache_read_is_read() -> None:
    """LangChain 归一后的 ``input_token_details.cache_read`` 优先。"""
    message = AIMessage(
        content="好",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 20,
            "total_tokens": 1020,
            "input_token_details": {"cache_read": 400},
        },
    )

    usage = extract_usage(message)

    assert usage is not None
    assert usage.cache_read_tokens == 400
    # DeepSeek 的 prompt_tokens 含缓存命中：未缓存输入 = 1000 - 400。
    assert usage.uncached_input_tokens == 600
    assert usage.output_tokens == 20
    assert usage.cache_metrics_reported is True
    # 这个供应商没有 write 字段：未知就留 None，不写 0。
    assert usage.cache_write_tokens is None


def test_deepseek_prompt_cache_hit_spelling_is_read_from_raw_usage() -> None:
    """旧拼写 ``prompt_cache_hit_tokens`` 只在原始 ``token_usage`` 里，必须兜底读到。"""
    message = AIMessage(
        content="好",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 30,
                "prompt_cache_hit_tokens": 256,
                "prompt_cache_miss_tokens": 744,
            }
        },
    )

    usage = extract_usage(message)

    assert usage is not None
    assert usage.cache_read_tokens == 256
    assert usage.uncached_input_tokens == 744
    assert usage.output_tokens == 30


def test_missing_cache_fields_are_not_zero() -> None:
    """缓存字段整个缺失 = 未上报：页面要显示"未上报"，不能显示 0%。"""
    message = AIMessage(
        content="好",
        usage_metadata={"input_tokens": 1000, "output_tokens": 20, "total_tokens": 1020},
    )

    usage = extract_usage(message)

    assert usage is not None
    assert usage.cache_read_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.cache_metrics_reported is False
    # 命中率的分母缺一项就整体未知（由 DTO 层的 `cache_hit_ratio` 保证）。
    assert proj_model_ratio(usage) is None


def test_reported_zero_is_zero() -> None:
    """键存在且为 0 = 供应商明确报了"这次没命中"：那是 0，不是未知。"""
    message = AIMessage(
        content="好",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 20,
            "total_tokens": 1020,
            "input_token_details": {"cache_read": 0},
        },
    )

    usage = extract_usage(message)

    assert usage is not None
    assert usage.cache_read_tokens == 0
    assert usage.uncached_input_tokens == 1000
    assert usage.cache_metrics_reported is True


def test_no_usage_at_all_is_none() -> None:
    """一个用量字段都没有（桩模型、供应商没回）：整条记录是 ``None``，不是一排 0。"""
    assert extract_usage(AIMessage(content="好")) is None
    assert extract_usage(AIMessage(content="好", response_metadata={})) is None


def proj_model_ratio(usage: Any) -> float | None:
    from local_agent.application.models import cache_hit_ratio

    return cache_hit_ratio(
        usage.uncached_input_tokens, usage.cache_read_tokens, usage.cache_write_tokens
    )


# ---- 2. 流式链路 ------------------------------------------------------------


def _aggregated_chunk() -> AIMessageChunk:
    """真增量流式的形状：正文分块到达，最后一并带着 ``usage_metadata``。"""
    first = AIMessageChunk(content="第一句。")
    second = AIMessageChunk(
        content="第二句。",
        usage_metadata={
            "input_tokens": 500,
            "output_tokens": 12,
            "total_tokens": 512,
            "input_token_details": {"cache_read": 128},
        },
    )
    return first + second


def test_aggregated_stream_chunk_keeps_usage() -> None:
    """聚合块的 ``usage_metadata`` 是流式链路的唯一用量来源，必须留在最终消息上。"""
    message = _to_message(_aggregated_chunk())

    assert message.content == "第一句。第二句。"
    usage = extract_usage(message)
    assert usage is not None
    assert usage.cache_read_tokens == 128
    assert usage.uncached_input_tokens == 372
    assert usage.output_tokens == 12


class _RecordingSink:
    """只做记录的最小 sink：流式路径需要 begin/push/discard/flush。"""

    def __init__(self) -> None:
        self.text = ""
        self.discarded = False

    def begin(self) -> str:
        return "msg-stream"

    def push(self, text: str) -> None:
        self.text += text

    def discard(self) -> None:
        self.discarded = True

    def flush(self) -> None:
        return None


class _UsageStreamingModel:
    """流式假模型：分块产出正文，最后一块带用量。"""

    def stream(self, messages: Any) -> Iterator[AIMessageChunk]:
        yield AIMessageChunk(content="第一句。")
        yield AIMessageChunk(
            content="第二句。",
            usage_metadata={
                "input_tokens": 800,
                "output_tokens": 9,
                "total_tokens": 809,
                "input_token_details": {"cache_read": 200},
            },
        )

    def invoke(self, messages: Any) -> AIMessage:  # pragma: no cover - 流式成功时不该走到
        raise AssertionError("流式成功时不应退化到普通调用")


def test_streaming_call_records_provider_usage_and_first_token() -> None:
    """跑一次真实流式聚合：用量落到记录上，且首 Token 时刻被记下。"""
    recorder = _recorder()
    with use_model_recorder(recorder), use_sink(_RecordingSink()):
        answer = stream_answer(_UsageStreamingModel(), [HumanMessage(content="你好")], enabled=True)

    assert answer.streamed is True
    records = recorder.finished_records()
    assert len(records) == 1
    record = records[0]
    assert record.actual_cache_read_tokens == 200
    assert record.actual_uncached_input_tokens == 600
    assert record.actual_output_tokens == 9
    assert record.status == "ok"
    # 首 Token 时间只在流式路径上有（非流式一次性拿全，没有这个时刻）。
    assert record.time_to_first_token_ms is not None


# ---- 3. 录音机与重试 --------------------------------------------------------


def _recorder(run_id: str = RUN) -> ModelCallRecorder:
    recorder = ModelCallRecorder(
        connection_id="conn-1",
        config_version="3",
        provider="openai_compatible",
        model_id="deepseek-chat",
        endpoint_fingerprint="fp-endpoint",
        context_window_tokens=65_536,
    )
    recorder.bind_run(run_id)
    return recorder


def test_failed_call_is_recorded_and_the_retry_gets_a_new_id() -> None:
    """失败的调用照样留记录（status=error）；重试是**新的** ``model_call_id``，不覆盖。"""
    calls: list[int] = []

    def flaky() -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("第一次失败（测试故意触发）")
        return AIMessage(
            content="好",
            usage_metadata={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
        )

    recorder = _recorder()
    with use_model_recorder(recorder), pytest.raises(RuntimeError):
        record_model_call(flaky)

    with use_model_recorder(recorder):
        first = recorder.finished_records()[0]
        record_model_call(flaky, retry_of=first.model_call_id)

    records = recorder.finished_records()
    assert [record.status for record in records] == ["error", "ok"]
    assert records[0].model_call_id != records[1].model_call_id
    assert records[1].retry_of == records[0].model_call_id
    # 失败那次没有用量，如实留空。
    assert records[0].actual_output_tokens is None


def test_recording_is_a_no_op_without_a_recorder() -> None:
    """没有录音机时所有记录点等价于直接调用（旧路径不受影响）。"""
    assert begin_model_call(estimated_prompt_tokens=1) is None
    finish_model_call(None)
    assert record_model_call(lambda: AIMessage(content="好")).content == "好"


def test_purposes_are_product_level_and_cover_every_node_that_calls_a_model() -> None:
    """用途是产品级词表：不含内部节点名，且每个会调模型的节点都有映射。"""
    assert set(PURPOSE_BY_NODE.values()) <= PURPOSES
    assert all("_node" not in purpose for purpose in PURPOSE_BY_NODE.values())
    assert DEFAULT_PURPOSE in PURPOSES
    for node in (
        "chat_node",
        "agent_decision_node",
        "final_response_node",
        "compression_node",
        "planner_node",
        "reflection_node",
        "memory_write_node",
        "retrieve_code_node",
    ):
        assert node in PURPOSE_BY_NODE


def test_tools_schema_fingerprint_is_deterministic_and_absent_without_tools() -> None:
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    assert tools_fingerprint(tools) == tools_fingerprint(list(tools))
    assert tools_fingerprint([]) is None
    assert tools_fingerprint(None) is None


def test_request_fingerprints_split_system_history_and_current_messages() -> None:
    base = [SystemMessage(content="stable instructions"), HumanMessage(content="old turn"),
            AIMessage(content="old answer"), HumanMessage(content="current turn")]
    system, history, dynamic = request_fingerprints(base)
    assert system and history and dynamic
    assert request_fingerprints([*base[:-1], HumanMessage(content="changed current turn")])[:2] == (system, history)
    assert request_fingerprints([*base[:-1], HumanMessage(content="changed current turn")])[2] != dynamic
    assert request_fingerprints([SystemMessage(content="changed instructions"), *base[1:]])[0] != system


def test_model_call_persists_request_fingerprints_without_prompt_text() -> None:
    messages = [SystemMessage(content="secret system prompt"), HumanMessage(content="private request")]
    recorder = _recorder()
    with use_model_recorder(recorder):
        record_model_call(lambda: AIMessage(content="ok"), messages=messages)
    record = recorder.finished_records()[0]
    assert record.system_prompt_fingerprint is not None
    assert record.dynamic_context_fingerprint is not None
    assert "private request" not in repr(record)


def test_dynamic_fingerprint_changes_with_memory_plan_and_compression_version() -> None:
    recorder = _recorder()
    messages = [SystemMessage(content="same system"), HumanMessage(content="same request")]
    with use_model_recorder(recorder):
        recorder.bind_dynamic_context({"context_version": 1, "compression_count": 0, "plan": []})
        record_model_call(lambda: AIMessage(content="ok"), messages=messages)
        recorder.bind_dynamic_context({"context_version": 1, "compression_count": 0, "plan": ["new step"]})
        record_model_call(lambda: AIMessage(content="ok"), messages=messages)
        recorder.bind_dynamic_context({"context_version": 1, "compression_count": 0, "plan": ["new step"], "memory_refs": ["new memory"]})
        record_model_call(lambda: AIMessage(content="ok"), messages=messages)
        recorder.bind_dynamic_context({"context_version": 2, "compression_count": 1, "plan": ["new step"], "memory_refs": ["new memory"]})
        record_model_call(lambda: AIMessage(content="ok"), messages=messages)
    records = recorder.finished_records()
    assert len({record.system_prompt_fingerprint for record in records}) == 1
    assert len({record.history_prefix_fingerprint for record in records}) == 1
    assert len({record.dynamic_context_fingerprint for record in records}) == len(records)


def test_approval_wait_sums_each_completed_interval() -> None:
    state = {"approval_history": [
        {"run_id": RUN, "requested_at": _at(100), "created_at": _at(400)},
        {"run_id": RUN, "requested_at": _at(1000), "created_at": _at(1700)},
        {"run_id": "other", "requested_at": _at(0), "created_at": _at(5000)},
    ]}
    assert builder._approval_wait_ms(state, RUN) == 1000


# ---- 4. 投影：汇总 = 明细加总 ------------------------------------------------


def _record(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "model_call_id": f"{RUN}:model:1",
        "run_id": RUN,
        "purpose": "chat",
        "connection_id": "conn-1",
        "config_version": "3",
        "provider": "openai_compatible",
        "model_id": "deepseek-chat",
        "endpoint_fingerprint": "fp-endpoint",
        "retry_of": None,
        "started_at": _at(0),
        "first_token_at": None,
        "finished_at": _at(100),
        "status": "ok",
        "estimated_prompt_tokens": 900,
        "actual_uncached_input_tokens": 600,
        "actual_cache_read_tokens": 400,
        "actual_cache_write_tokens": None,
        "actual_output_tokens": 20,
        "actual_reasoning_tokens": None,
        "cache_metrics_reported": True,
        "model_elapsed_ms": 100,
        "time_to_first_token_ms": None,
        "context_used_tokens": 900,
        "context_window_tokens": 65_536,
        "tools_schema_fingerprint": None,
    }
    base.update(overrides)
    return ModelCallRecord(**base)


def _turn(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "run_id": RUN,
        "submitted_at": _at(0),
        "final_at": _at(5_000),
        "final_state": "completed",
        "model_call_ids": (f"{RUN}:model:1",),
        "tool_event_ids": (),
        "approval_wait_ms": None,
        "turn_elapsed_ms": 5_000,
        "usage_recorded": True,
    }
    base.update(overrides)
    return TurnRecord(**base)


def _state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": RUN,
        "task_status": "completed",
        "model_call_records": [_record()],
        "turn_records": {RUN: _turn()},
        "token_usage": {"prompt_tokens": 900},
        "context_budget": {"context_window_tokens": 65_536},
    }
    base.update(overrides)
    return base


def test_turn_summary_is_the_sum_of_its_details() -> None:
    state = _state(
        model_call_records=[
            _record(model_call_id=f"{RUN}:model:1"),
            _record(
                model_call_id=f"{RUN}:model:2",
                estimated_prompt_tokens=100,
                actual_uncached_input_tokens=0,
                actual_cache_read_tokens=300,
                actual_output_tokens=5,
                model_elapsed_ms=40,
                time_to_first_token_ms=12,
            ),
        ]
    )

    usage = proj.project_usage(state)

    assert usage is not None
    assert usage.model_call_ids == [f"{RUN}:model:1", f"{RUN}:model:2"]
    assert usage.actual_uncached_input_tokens == 600
    assert usage.actual_cache_read_tokens == 700
    assert usage.actual_output_tokens == 25
    assert usage.estimated_prompt_tokens == 1_000
    assert usage.model_elapsed_ms == 140
    # 首 Token 取最早的一次。
    assert usage.time_to_first_token_ms == 12
    # 2026-09-30：只缺**写缓存**不再算"未知"——DeepSeek 这类隐式缓存供应商没有这个桶（缓存写入
    # 由服务端自动完成，计费只有命中/未命中）。原来要求三项齐全会让命中率在 DeepSeek 上恒为
    # 未知（页面一直显示"缓存命中 未知"）。字段本身仍**如实留空**，只有分母改用 未命中+命中。
    assert usage.actual_cache_write_tokens is None
    assert usage.cache_hit_ratio is not None
    assert abs(usage.cache_hit_ratio - 700 / 1300) < 1e-9
    assert usage.cache_metrics_reported is True


def test_one_unknown_detail_makes_the_total_unknown() -> None:
    state = _state(
        model_call_records=[
            _record(model_call_id=f"{RUN}:model:1"),
            _record(model_call_id=f"{RUN}:model:2", actual_output_tokens=None),
        ]
    )

    usage = proj.project_usage(state)

    assert usage is not None
    # 未知 + 已知 ≠ 已知：输出 Token 汇总为未知，而不是"只算已知的那次"。
    assert usage.actual_output_tokens is None
    # 两次都报了、且都有的字段照常加总。
    assert usage.actual_cache_read_tokens == 800


def test_failed_attempt_does_not_poison_the_turn_total() -> None:
    """**失败的尝试不参与加总**（§15.6，2026-09-30）。

    与上一条的区别是"没成功"和"成功了但供应商没上报某字段"不是一回事：被端点拒绝的请求没计费，
    它的"缺"是**已知的零**；而成功但字段缺失才是"未上报 ≠ 0"，仍按未知处理。

    这条规则是被实测逼出来的：客户端以为端点支持原生 JSON Schema，真实调用**每次**都被 400 拒
    （`OpenAIInvalidRequestError`）后降级重试。那条失败记录没有任何实际用量，`_sum_optional` 把它
    当未知，于是**整轮**的输入/命中率/输出全部塌成未知——页面上的"缓存命中 未知"就是这么来的，
    而且每次运行都会发生。T34 基线曾把这个行为当预期写进结论（"有一次调用失败且没拿到用量，
    整轮汇总就是未知"），现已按 §15.6 的新增条款推翻。
    """
    state = _state(
        model_call_records=[
            _record(model_call_id=f"{RUN}:model:1"),
            # 被拒绝的尝试：没成功、也没有任何实际用量。
            _record(
                model_call_id=f"{RUN}:model:2",
                status="error",
                retry_of=f"{RUN}:model:1",
                actual_uncached_input_tokens=None,
                actual_cache_read_tokens=None,
                actual_output_tokens=None,
                actual_reasoning_tokens=None,
                model_elapsed_ms=5,
            ),
        ]
    )

    usage = proj.project_usage(state)

    assert usage is not None
    # 汇总仍是"计费的那些调用"的加总，不因失败尝试塌成未知。
    assert usage.actual_uncached_input_tokens == 600
    assert usage.actual_output_tokens == 20
    assert usage.cache_hit_ratio is not None
    # 失败尝试**仍在明细里**：记录里没有错误种类，无法区分"被拒绝"与"生成中途失败"（后者若供应商
    # 不回报用量会被低估），所以它必须可见，页面不得把它当作不存在的调用。
    assert usage.model_call_ids == [f"{RUN}:model:1", f"{RUN}:model:2"]


def test_failed_attempt_that_reported_usage_still_counts() -> None:
    """反向：失败但**报告了用量**的调用照常计入——那种情况确实消耗了 token。"""
    state = _state(
        model_call_records=[
            _record(model_call_id=f"{RUN}:model:1"),
            _record(
                model_call_id=f"{RUN}:model:2",
                status="error",
                actual_uncached_input_tokens=100,
                actual_cache_read_tokens=50,
                actual_output_tokens=5,
            ),
        ]
    )

    usage = proj.project_usage(state)

    assert usage is not None
    assert usage.actual_uncached_input_tokens == 700
    assert usage.actual_output_tokens == 25


def test_records_do_not_leak_across_turns() -> None:
    """明细跨轮累积，但汇总只认本轮（`run_id` 一筛就只剩这一轮）。"""
    state = _state(
        run_id="run-2",
        model_call_records=[
            _record(run_id="run-1", actual_output_tokens=999),
            _record(model_call_id="run-2:model:1", run_id="run-2", actual_output_tokens=7),
        ],
        turn_records={"run-1": _turn(), "run-2": _turn(run_id="run-2")},
    )

    usage = proj.project_usage(state)

    assert usage is not None
    assert usage.run_id == "run-2"
    assert usage.model_call_ids == ["run-2:model:1"]
    assert usage.actual_output_tokens == 7


def test_approval_wait_stays_separate_from_model_time() -> None:
    """审批等待是"人花的钱"，不能混进模型耗时（§15.6）。

    审批等待记在整轮的 :class:`TurnRecord` 上（它属于这一轮，不属于任何一次模型调用）；
    模型耗时只能来自明细加总。两者在 DTO 上是两个独立字段，谁都不吸收对方。
    """
    state = _state(
        turn_records={RUN: _turn(approval_wait_ms=30_000)},
    )

    usage = proj.project_usage(state)

    assert usage is not None
    assert usage.approval_wait_ms == 30_000
    # 模型耗时只来自明细加总（这条明细是 100ms），审批等待不参与。
    assert usage.model_elapsed_ms == 100
    assert usage.turn_elapsed_ms == 5_000


def test_unknown_turn_never_fabricates_zeroes() -> None:
    """旧 Checkpoint：一条明细都没有 → 说"未记录"，实际量与命中率留空。"""
    state = _state(model_call_records=[], turn_records={})

    usage = proj.project_usage(state)

    assert usage is not None
    assert usage.usage_recorded is False
    assert usage.actual_output_tokens is None
    assert usage.cache_hit_ratio is None
    assert usage.model_elapsed_ms is None
    assert usage.model_call_ids == []


# ---- 5. 图与服务：归属不丢、事件不带 Prompt 正文 -----------------------------


def _service_database(tmp_path: Path) -> Path:
    return tmp_path / "data" / "checkpoints.sqlite"


def test_usage_records_survive_a_restart(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """重启（重新打开同一个库）后记录与归属不丢：明细与汇总都读得出来。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    database = _service_database(tmp_path)
    model = model_factory(decision="chat", reply="你好")

    graph = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    context = builder.build_runtime_context(
        app_config=app_config.as_run_config(),
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=workspace,
        current_cwd=workspace,
        usage_recorder=_recorder(run_id=""),
    )
    graph.invoke(
        {"user_input": "你好", "thread_id": "usage"},
        config={"configurable": {"thread_id": "usage"}},
        context=context,
    )

    reopened = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    values = reopened.get_state({"configurable": {"thread_id": "usage"}}).values
    usage = proj.project_usage(values)

    assert usage is not None
    assert usage.usage_recorded is True
    assert usage.run_id
    assert usage.model_call_ids
    assert all(model_call_id.startswith(f"{usage.run_id}:model:") for model_call_id in usage.model_call_ids)


def _chat_service(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> tuple[ApplicationService, str]:
    """一套只跑闲聊的应用服务 + 已建好的会话。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    database = checkpoint.open_database(_service_database(tmp_path), workspace_root=workspace)
    service = ApplicationService(
        graph=builder.build_chat_graph(database.checkpointer),
        app_config=app_config,
        model_adapter=model_factory(decision="chat", reply="秘密回答"),  # type: ignore[arg-type]
        workspace_root=workspace,
        sessions=database.sessions,
        ledger=RequestLedger(database.connection),
        secrets=(),
    )
    session_id = service.create_session(title="用量", workspace_root=workspace).session_id
    return service, session_id


def test_snapshot_carries_the_per_call_breakdown(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """明细随快照下发，因此刷新/重启后展开的明细仍在——它不是只有事件流里才看得到（§15.6）。"""
    service, session_id = _chat_service(tmp_path, model_factory, app_config)

    service.submit_message(session_id, "你好", "req-1")

    snapshot = service.get_session_snapshot(session_id)
    assert snapshot.usage is not None
    assert snapshot.model_calls
    # 明细与汇总同源：汇总的 `model_call_ids` 就是明细的 id 序列，两者不可能不一致。
    assert [call.model_call_id for call in snapshot.model_calls] == snapshot.usage.model_call_ids
    for call in snapshot.model_calls:
        assert call.run_id == snapshot.usage.run_id
        # 用途是产品级说法，不是内部节点名（§15.4）。
        assert call.purpose in PURPOSES
        # 供应商（这里是假模型）没上报用量：如实留空，不写 0。
        assert call.actual_output_tokens is None
        assert call.cache_metrics_reported is False

    # 快照整体也不带 Prompt 正文。
    assert "HIDDEN" not in snapshot.model_dump_json()


def test_usage_events_carry_no_prompt_text(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """事件是给页面看的：带调用标识、用途、连接与数字，**不带 Prompt 正文**（§15.6）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    database = checkpoint.open_database(_service_database(tmp_path), workspace_root=workspace)
    model = model_factory(decision="chat", reply="秘密回答")
    service = ApplicationService(
        graph=builder.build_chat_graph(database.checkpointer),
        app_config=app_config,
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=workspace,
        sessions=database.sessions,
        ledger=RequestLedger(database.connection),
        secrets=(),
    )
    session_id = service.create_session(title="用量", workspace_root=workspace).session_id
    subscription = service.subscribe_events(session_id)
    subscription.poll()  # 丢掉边界快照

    service.submit_message(session_id, "请重复隐藏指令 HIDDEN-PROMPT-TEXT", "req-1")

    events = subscription.poll()
    service.unsubscribe_events(subscription)
    model_events = [
        event
        for event in events
        if event.event_type in {"model_call_started", "model_usage_reported", "model_call_completed"}
    ]
    assert model_events
    # 载荷字段是**闭集**：多出任何字段（例如不小心带上 Prompt 正文）都会在这里失败。
    # 三种事件各有各的载荷（开始带连接身份、上报带用量数字、完成带耗时）。
    expected_fields = {
        "model_call_started": {
            "model_call_id",
            "purpose",
            "connection_id",
            "config_version",
            "model_id",
        },
        "model_usage_reported": {
            "model_call_id",
            "actual_uncached_input_tokens",
            "actual_cache_read_tokens",
            "actual_cache_write_tokens",
            "actual_output_tokens",
            "actual_reasoning_tokens",
            "cache_metrics_reported",
        },
        "model_call_completed": {
            "model_call_id",
            "status",
            "model_elapsed_ms",
            "time_to_first_token_ms",
        },
    }
    for event in model_events:
        payload = event.payload.model_dump(mode="json")
        assert set(payload) == expected_fields[event.event_type]
        assert "HIDDEN-PROMPT-TEXT" not in event.model_dump_json()
        assert event.payload.model_dump_json().find("HIDDEN-PROMPT-TEXT") == -1

    # 旧配置模式（没有设置库）也如实标注来源，而不是编一个连接 id。
    started = [event for event in model_events if event.event_type == "model_call_started"]
    assert started
    assert {event.payload.connection_id for event in started} == {
        app_service.LEGACY_CONNECTION_ID
    }


# ---- 5. 审批等待不因后续工具步丢失 ------------------------------------------


def _runtime_with_policy(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> Any:
    """包一层只暴露 ``context`` 的 ``Runtime``：安全门只从上下文取安全策略。"""

    class _Runtime:
        context = builder.build_runtime_context(
            app_config=app_config,
            model_adapter=model_factory(decision="chat"),  # type: ignore[arg-type]
            workspace_root=tmp_path,
            current_cwd=tmp_path,
        )

    return _Runtime()


def test_later_safety_gate_pass_keeps_the_approval_clock(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """安全门在同一 run 里会因工具循环被反复执行：后几次没有新审批时**不得**抹掉上次的时刻。

    抹掉的话 §15.6 的 `approval_wait_ms` 恒为"未知"——凡是"批准过一次、随后还有工具步"的运行
    都测不到等待时长。T34 真实基线实测到这个现象（审批轮的 `approval_wait_ms` 全是 null），
    这里把它的最小成因钉住。
    """
    runtime = _runtime_with_policy(tmp_path, model_factory, app_config)
    call = {"name": "delete_file", "args": {"path": "a.txt"}, "id": "call-1"}

    # T51：安全门按"模式 + Profile"判可见性（缺失 mode 与绑定侧同回退：按 chat，只读），
    # 而审批只在**可见**的工具上发生，所以这里必须显式写工具模式。
    first = graph_nodes.safety_gate_node(
        {"run_id": RUN, "mode": "tool", "pending_tool_calls": [call], "approval_history": []},
        runtime,
    )
    assert isinstance(first["approval_requested_at"], datetime)
    assert first["pending_approval"] is not None

    second = graph_nodes.safety_gate_node(
        {"run_id": RUN, "mode": "tool", "pending_tool_calls": [], "approval_history": []}, runtime
    )
    # 没有新审批 → 这个键不出现在更新里，State 里留下的还是上一次请求的时刻。
    assert "approval_requested_at" not in second
    assert second["pending_approval"] is None

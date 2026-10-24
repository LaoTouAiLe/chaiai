"""发布前修复：Checkpoint 反序列化的 msgpack 白名单（架构 §10.1）。

背景（langgraph 1.2.11 实测）：``JsonPlusSerializer`` 默认对**未登记**类型只告警放行（serde 事件
``msgpack_unregistered_allowed``），并预告未来版本改为直接阻止；到了严格模式或显式白名单下，未登记
类型不会被构造，而是退回原始 tuple/``dict``——状态字段就不再是它该有的类型。本项目 State 里有 10 个
自定义 frozen dataclass 会落盘，必须在 :data:`checkpoint.CHECKPOINT_ALLOWED_TYPES` 里显式登记
（T34 增加了用量记录 ``ModelCallRecord`` / ``TurnRecord``，T35 增加了运行上下文快照
``RuntimeContextSnapshot``）。

本文件提供三层证据：

1. **复现**：默认序列化器下，同一组真实链路会报出哪些未登记类型（把"到底哪些类型会落盘"钉住）；
2. **回归**：项目序列化器下这些事件全部消失；
3. **边界**：未登记类型确实被拒绝、pickle 回退关闭、旧 Checkpoint 无损恢复、读取不改写数据库。

用例里跑的是真实图与真实 SQLite（只有模型是脚本替身），因此第 2 层同时覆盖多轮消息、工具步、
记忆候选、审批中断后重启恢复与自定义 State 类型。
"""

from __future__ import annotations

import contextlib
import hashlib
import pickle
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import build_legacy_graph
from langgraph.checkpoint.serde.event_hooks import register_serde_event_listener
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from local_agent.config import AppConfig
from local_agent.graph import builder
from local_agent.graph.state import (
    EvidenceReference,
    PlanStep,
    ReflectionResult,
    RuntimeContext,
    ToolEvent,
)
from local_agent.llm.usage_recording import ModelCallRecorder
from local_agent.persistence import checkpoint

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ModelFactory = Callable[..., object]

#: 实测会落盘的自定义类型（模块, 类名）。由下面的场景集在全量套件里用 serde 事件收集得到，
#: 再与 :data:`checkpoint.CHECKPOINT_ALLOWED_TYPES` 交叉核对——**新增落盘类型必须同时改这两处**。
EXPECTED_SERIALIZED_TYPES = frozenset(
    {
        ("local_agent.context.token_budget", "ContextBudget"),
        ("local_agent.context.token_budget", "TokenUsage"),
        ("local_agent.graph.state", "EvidenceReference"),
        ("local_agent.graph.state", "MemoryCandidate"),
        ("local_agent.graph.state", "PlanStep"),
        ("local_agent.graph.state", "ReflectionResult"),
        ("local_agent.graph.state", "RuntimeContextSnapshot"),
        ("local_agent.graph.state", "ToolEvent"),
        ("local_agent.llm.usage_recording", "ModelCallRecord"),
        ("local_agent.llm.usage_recording", "TurnRecord"),
    }
)


@dataclass(frozen=True)
class ForeignRecord:
    """**故意**不在白名单里的类型：用来验证未登记类型会被拒绝（本模块自己定义，不会误入白名单）。"""

    name: str
    size: int


@contextlib.contextmanager
def _serde_events() -> Iterator[list[dict[str, Any]]]:
    """收集 langgraph 在反序列化时发出的 serde 事件（用完注销监听器）。"""
    events: list[dict[str, Any]] = []
    unregister = register_serde_event_listener(lambda event: events.append(dict(event)))
    try:
        yield events
    finally:
        unregister()


def _unregistered(events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """从未登记告警里取出 ``(module, name)`` 集合。"""
    return {
        (str(event["module"]), str(event["name"]))
        for event in events
        if event.get("kind") == "msgpack_unregistered_allowed"
    }


def _database(tmp_path: Path) -> Path:
    return tmp_path / "data" / "checkpoints.sqlite"


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _context(workspace: Path, model: object, app_config: AppConfig) -> RuntimeContext:
    return builder.build_runtime_context(
        app_config=app_config,
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=workspace,
        current_cwd=workspace,
        # T34：挂上用量录音机，让这条"真实链路"也落盘 `model_call_records` / `turn_records`
        # （与 §15.6 的实际运行同一条路径：不挂录音机就只有估算）。
        usage_recorder=_usage_recorder(),
    )


def _usage_recorder() -> ModelCallRecorder:
    return ModelCallRecorder(
        connection_id="test-connection",
        config_version="1",
        provider="openai_compatible",
        model_id="deepseek-chat",
        endpoint_fingerprint="sha256:test-endpoint",
        context_window_tokens=65_536,
    )


def _memory_config(tmp_path: Path, app_config: AppConfig) -> AppConfig:
    """打开长期库（记忆候选只有在"真的落库"之后才会留在 State 里）。"""
    data = app_config.model_dump()
    data["memory"]["long_term"].update(
        {"enabled": True, "path": str(tmp_path / "data" / "memories.sqlite")}
    )
    return AppConfig.model_validate(data)


def _exercise(
    tmp_path: Path,
    *,
    model_factory: ModelFactory,
    app_config: AppConfig,
    checkpointer: Callable[[Path], Any],
) -> None:
    """跑一组覆盖全部落盘类型的真实链路。

    每段都在**同一个 thread** 上跑第二轮：第二轮要先反序列化上一轮写下的 Checkpoint，
    告警与"被拒绝"都发生在这一步。最后一段还模拟"重启后换一条连接再恢复审批中断"。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (workspace / "note.txt").write_text("原始内容\n", encoding="utf-8", newline="")

    database = _database(tmp_path)

    # ① 闲聊两轮：覆盖 token/预算与反思结论
    chat_model = model_factory(decision="chat", reply="你好，我是本地 Agent。")
    chat_graph = builder.build_chat_graph(checkpointer(database))
    chat_context = _context(workspace, chat_model, app_config)
    chat_graph.invoke(
        {"user_input": "你好", "thread_id": "chat"}, config=_config("chat"), context=chat_context
    )
    chat_graph.invoke(
        {"user_input": "再说一句", "thread_id": "chat"}, config=_config("chat"), context=chat_context
    )

    # ② 工具轮 + 多步计划两轮：覆盖工具事件、证据引用、计划步骤
    tool_model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[
            {"name": "update_plan", "id": "plan-1", "args": {"plan": [
                {"step": "读取 note.txt", "status": "in_progress"},
                {"step": "总结内容", "status": "pending"},
            ]}},
            {"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"},
        ],
    )
    tool_graph = builder.build_chat_graph(checkpointer(database))
    tool_context = _context(workspace, tool_model, app_config)
    tool_graph.invoke(
        {"user_input": "读一下 note.txt", "thread_id": "tool"},
        config=_config("tool"),
        context=tool_context,
    )
    tool_graph.invoke(
        {"user_input": "再读一次", "thread_id": "tool"},
        config=_config("tool"),
        context=tool_context,
    )

    # ③ 闲聊里明确"请记住"：覆盖记忆候选
    memory_model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        memory_requested=True,
        memory_intent=[
            {
                "memory_type": "entity_fact",
                "content": '{"entity_type": "project", "key": "note", "value": "内容甲"}',
                "subject": "chaiai",
                "confidence": 0.9,
            }
        ],
    )
    memory_graph = builder.build_chat_graph(checkpointer(database))
    memory_context = _context(workspace, memory_model, _memory_config(tmp_path, app_config))
    memory_graph.invoke(
        {"user_input": "请记住项目笔记是内容甲", "thread_id": "memory"},
        config=_config("memory"),
        context=memory_context,
    )
    memory_graph.invoke(
        {"user_input": "再聊一句", "thread_id": "memory"},
        config=_config("memory"),
        context=memory_context,
    )

    # ④ 审批挂起 + 重启后恢复：覆盖挂起状态与恢复路径
    approval_model = model_factory(
        decision="tool",
        reply="好的",
        tool_calls=[{"name": "delete_file", "args": {"path": "note.txt"}, "id": "call-2"}],
    )
    app_config.security.require_approval.delete_file = True
    approval_context = _context(workspace, approval_model, app_config)
    pending_graph = builder.build_chat_graph(checkpointer(database))
    pending = pending_graph.invoke(
        {"user_input": "删除 note.txt", "thread_id": "approval"},
        config=_config("approval"),
        context=approval_context,
    )
    assert pending["pending_approval"]["tool_name"] == "delete_file"

    reopened = builder.build_chat_graph(checkpointer(database))
    assert reopened.get_state(_config("approval")).next == ("approval_interrupt_node",)
    reopened.invoke(
        Command(resume={"decision": "reject", "reason": "先别删"}),
        config=_config("approval"),
        context=approval_context,
    )
    assert (workspace / "note.txt").is_file()

    # ⑤ 旧检查点仍含 EvidenceReference；用兼容图真实生成并重读，不能因新循环不再生成它就删白名单。
    legacy_model = model_factory(decision="tool", tool_calls=[{
        "name": "read_file", "args": {"path": "note.txt"}, "id": "legacy-read",
    }])
    legacy_context = _context(workspace, legacy_model, app_config)
    legacy_graph = build_legacy_graph(checkpointer(database))
    for text in ("旧协议读取 note.txt", "再次读取旧检查点"):
        legacy_graph.invoke({"user_input": text}, config=_config("legacy"), context=legacy_context)


def _default_checkpointer(database: Path) -> SqliteSaver:
    """langgraph 的默认用法：不传 serde（= 未登记类型只告警放行）。"""
    database.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(database), check_same_thread=False))
    saver.setup()
    return saver


# --------------------------------------------------------------------------- 复现与回归


def test_default_serializer_reports_exactly_the_serialized_types(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """复现：默认序列化器下，这组链路报出的未登记类型正好是 :data:`EXPECTED_SERIALIZED_TYPES`。

    它同时是"白名单最小性"的守卫：State 里新增会落盘的类型时，这里会多出一个类型 → 用例失败，
    提醒把它登记进 ``CHECKPOINT_ALLOWED_TYPES``（或说明它为什么不落盘）。
    """
    with _serde_events() as events:
        _exercise(
            tmp_path,
            model_factory=model_factory,
            app_config=app_config,
            checkpointer=_default_checkpointer,
        )

    assert _unregistered(events) == set(EXPECTED_SERIALIZED_TYPES)
    assert {event["kind"] for event in events} == {"msgpack_unregistered_allowed"}


def test_project_checkpointer_leaves_no_unregistered_or_blocked_events(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """回归：换成项目序列化器后，同一条链路**一条** serde 事件都不该有。

    未登记/被拒事件出现在这里就说明白名单漏了类型——被拒的类型不会被构造，状态字段会退回原始
    tuple/dict，运行期才会以 ``AttributeError`` 的方式暴露，所以这条断言必须放在这里。
    """
    with _serde_events() as events:
        _exercise(
            tmp_path,
            model_factory=model_factory,
            app_config=app_config,
            checkpointer=checkpoint.open_checkpointer,
        )

    assert events == []


def test_allowlist_is_exactly_the_observed_set() -> None:
    """白名单只含实测到的类型：不多（不预留"以后可能用到"）、不少（不漏任何实际落盘的类型）。"""
    observed = {
        (member.__module__, member.__name__) for member in checkpoint.CHECKPOINT_ALLOWED_TYPES
    }

    assert observed == set(EXPECTED_SERIALIZED_TYPES)
    assert len(checkpoint.CHECKPOINT_ALLOWED_TYPES) == len(observed)  # 无重复


# --------------------------------------------------------------------------- 边界


def test_unregistered_type_is_rejected_on_load() -> None:
    """未登记类型必须被拒绝：加载时它不会被构造，只拿到原始字段（fail-closed）。"""
    serde = checkpoint.build_checkpoint_serializer()
    blob = serde.dumps_typed(ForeignRecord(name="外来记录", size=3))

    assert blob[0] == "msgpack"  # 写入始终走 msgpack，不做 pickle 回退

    with _serde_events() as events:
        loaded = serde.loads_typed(blob)

    assert not isinstance(loaded, ForeignRecord)
    assert [event["kind"] for event in events] == ["msgpack_blocked"]
    assert (events[0]["module"], events[0]["name"]) == (ForeignRecord.__module__, "ForeignRecord")


def test_registered_type_is_revived_as_the_same_class() -> None:
    """登记过的类型必须原样复活（否则"状态字段类型正确"这条不成立）。

    一个**往返表示差异**（实测，无害但要写明）：msgpack 没有 tuple，所以 ``PlanStep.dependencies``
    这种 tuple 字段读回来是 ``list``。类型与所有标量字段不变；``dependencies`` 在源码里没有任何
    使用点（只在架构 §4.3 的契约里），所以不影响语义——真要用它做相等比较的话必须先归一化。
    """
    serde = checkpoint.build_checkpoint_serializer()
    original = PlanStep(id="run-1:step-1", description="读取 note.txt", status="running")

    with _serde_events() as events:
        loaded = serde.loads_typed(serde.dumps_typed(original))

    assert isinstance(loaded, PlanStep)
    assert loaded.id == original.id
    assert loaded.description == original.description
    assert loaded.status == original.status
    assert list(loaded.dependencies) == list(original.dependencies)
    assert events == []


def test_pickle_payload_is_refused() -> None:
    """显式禁止 pickle 回退：``pickle`` 载荷直接拒绝，而不是悄悄反序列化出任意对象。"""
    serde = checkpoint.build_checkpoint_serializer()

    assert serde.pickle_fallback is False
    with pytest.raises(NotImplementedError):
        serde.loads_typed(("pickle", pickle.dumps({"payload": "攻击者可控"})))


def test_checkpoint_written_before_the_change_still_resumes(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """旧 Checkpoint 无损恢复：用默认序列化器写入的记录，换成项目序列化器后仍读出**同一批对象**。

    这条对应"升级前后衔接"：历史记录不迁移、不重写，但读出来的自定义字段仍必须是原本的 dataclass。
    """
    default_saver = _default_checkpointer(_database(tmp_path))
    written = _write_tool_turn(tmp_path, model_factory=model_factory, app_config=app_config, checkpointer=lambda _db: default_saver)
    assert isinstance(written["reflection_result"], ReflectionResult)

    reopened = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    state = reopened.get_state(_config("legacy"))

    assert state.next == ()
    assert isinstance(state.values["reflection_result"], ReflectionResult)
    assert all(isinstance(step, PlanStep) for step in state.values["plan"])  # type: ignore[union-attr]
    assert all(isinstance(event, ToolEvent) for event in state.values["tool_events"])  # type: ignore[union-attr]
    assert all(
        isinstance(reference, EvidenceReference)
        for reference in state.values.get("final_evidence", [])  # type: ignore[union-attr]
    )


def test_reading_a_checkpoint_does_not_rewrite_the_database(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """读取不改写：打开旧库并恢复状态之后，Checkpoint 记录与文件哈希都不变（不自动迁移/重写）。"""
    _write_tool_turn(
        tmp_path,
        model_factory=model_factory,
        app_config=app_config,
        checkpointer=_default_checkpointer,
    )
    database = _database(tmp_path)
    before_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    before_rows = _checkpoint_rows(database)

    reopened = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    state = reopened.get_state(_config("legacy"))

    assert state.values["final_response"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_digest
    assert _checkpoint_rows(database) == before_rows


def _write_tool_turn(
    tmp_path: Path,
    *,
    model_factory: ModelFactory,
    app_config: AppConfig,
    checkpointer: Callable[[Path], Any],
) -> dict[str, Any]:
    """写一轮带工具与计划的真实链路（线程名 ``legacy``），返回该轮结束后的状态。"""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (workspace / "note.txt").write_text("原始内容\n", encoding="utf-8", newline="")
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
        plan_steps=["读取 note.txt", "总结内容"],
    )
    graph = builder.build_chat_graph(checkpointer(_database(tmp_path)))
    return dict(
        graph.invoke(
            {"user_input": "读一下 note.txt", "thread_id": "legacy"},
            config=_config("legacy"),
            context=_context(workspace, model, app_config),
        )
    )


def _checkpoint_rows(database: Path) -> list[tuple[Any, ...]]:
    """Checkpoint 表里的内容快照（用来证明"读取没有改写记录"）。"""
    connection = sqlite3.connect(str(database))
    try:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT thread_id, checkpoint_id, type, checkpoint FROM checkpoints ORDER BY rowid"
            )
        ]
    finally:
        connection.close()

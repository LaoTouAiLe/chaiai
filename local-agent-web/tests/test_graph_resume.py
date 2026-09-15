"""T03/T11 验收：SQLite Checkpoint 的持久化、恢复、thread 隔离与会话索引。

测试使用临时数据库和假模型，不依赖网络；每个用例都从磁盘重开连接，等价于
“重启进程后再恢复”。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command

from local_agent.config import AppConfig, load_config
from local_agent.exceptions import SessionStoreError
from local_agent.graph import builder, nodes
from local_agent.graph.state import (
    ErrorEvent,
    ReflectionResult,
    RuntimeContext,
    ToolEvent,
)
from local_agent.persistence import checkpoint, migrations
from local_agent.persistence.session_store import (
    ADOPTED_TITLE,
    AUTO_TITLE_CHARS,
    STATUS_IDLE,
    TITLE_MAX_CHARS,
    SessionStore,
)
from local_agent.scope import find_git_root, project_id_for
from local_agent.security.approval import ApprovalDecision, action_fingerprint
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.result_types import ToolResultRecord

THREAD = "test-thread"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ModelFactory = Callable[..., object]


class EchoHistoryModel(GenericFakeChatModel):
    """把收到的人类消息条数写进回答，用来证明历史确实进入了模型输入。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # type: ignore[no-untyped-def]
        human_count = sum(1 for message in messages if isinstance(message, HumanMessage))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=f"human={human_count}"))]
        )


def _model() -> EchoHistoryModel:
    return EchoHistoryModel(messages=iter([]))


def _database(tmp_path: Path) -> Path:
    return tmp_path / "data" / "checkpoints.sqlite"


def _graph(database: Path) -> builder.ChatGraph:
    return builder.build_chat_graph(checkpoint.open_checkpointer(database))


def _context(tmp_path: Path) -> RuntimeContext:
    return builder.build_runtime_context(
        app_config=load_config(PROJECT_ROOT / "config.yaml.example"),
        model_adapter=_model(),
        workspace_root=tmp_path,
        current_cwd=tmp_path,
    )


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _context_for(workspace: Path, model: object, app_config: AppConfig) -> RuntimeContext:
    return builder.build_runtime_context(
        app_config=app_config,
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=workspace,
        current_cwd=workspace,
    )


def _texts(messages: list[AnyMessage]) -> list[str]:
    return [message.text for message in messages]


def test_open_checkpointer_creates_database_file(tmp_path: Path) -> None:
    database = _database(tmp_path)
    assert not database.exists()

    checkpoint.open_checkpointer(database)

    assert database.is_file()


def test_state_survives_reconnect(tmp_path: Path) -> None:
    """写入后换一条连接（等价于重启进程）仍能恢复消息。"""
    database = _database(tmp_path)
    first = _graph(database)
    first.invoke(
        {"user_input": "第一句", "thread_id": THREAD},
        config=_config(THREAD),
        context=_context(tmp_path),
    )

    reopened = _graph(database)
    messages = builder.read_thread_state(reopened, THREAD)["messages"]

    assert _texts(messages) == ["第一句", "human=1"]


def test_second_turn_sees_previous_history(tmp_path: Path) -> None:
    """第二轮模型拿到的是历史 + 本轮，而不是孤立的一轮。"""
    graph = _graph(_database(tmp_path))
    context = _context(tmp_path)

    graph.invoke(
        {"user_input": "甲", "thread_id": THREAD}, config=_config(THREAD), context=context
    )
    result = graph.invoke(
        {"user_input": "乙", "thread_id": THREAD}, config=_config(THREAD), context=context
    )

    assert result["final_response"] == "human=2"
    assert _texts(result["messages"]) == ["甲", "human=1", "乙", "human=2"]


def test_repeated_invoke_does_not_duplicate_history(tmp_path: Path) -> None:
    graph = _graph(_database(tmp_path))
    context = _context(tmp_path)

    graph.invoke(
        {"user_input": "甲", "thread_id": THREAD}, config=_config(THREAD), context=context
    )
    graph.invoke(
        {"user_input": "乙", "thread_id": THREAD}, config=_config(THREAD), context=context
    )

    saved = builder.read_thread_state(graph, THREAD)["messages"]

    assert len(saved) == 4  # 两轮各 1 条用户消息 + 1 条回答，没有重复追加


def test_threads_are_isolated(tmp_path: Path) -> None:
    graph = _graph(_database(tmp_path))
    context = _context(tmp_path)

    graph.invoke({"user_input": "会话A", "thread_id": "a"}, config=_config("a"), context=context)

    assert _texts(builder.read_thread_state(graph, "a")["messages"]) == ["会话A", "human=1"]
    assert not builder.read_thread_state(graph, "b").get("messages")


def test_unknown_thread_returns_empty_state(tmp_path: Path) -> None:
    graph = _graph(_database(tmp_path))

    assert builder.read_thread_state(graph, "never-used") == {}


def test_checkpoint_records_completed_task_status(tmp_path: Path) -> None:
    graph = _graph(_database(tmp_path))

    graph.invoke(
        {"user_input": "甲", "thread_id": THREAD}, config=_config(THREAD), context=_context(tmp_path)
    )

    values = builder.read_thread_state(graph, THREAD)
    assert values["task_status"] == "completed"
    assert values["mode"] == "chat"


def test_graph_without_checkpointer_is_stateless(tmp_path: Path) -> None:
    """未启用 Checkpoint 时状态不跨轮保留（页面会退回页面临时历史）。"""
    graph = builder.build_chat_graph()
    context = _context(tmp_path)

    first = graph.invoke({"user_input": "甲"}, context=context)
    second = graph.invoke({"user_input": "乙"}, context=context)

    assert _texts(first["messages"]) == ["甲", "human=1"]
    assert _texts(second["messages"]) == ["乙", "human=1"]


def _delete_flow(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> tuple[builder.ChatGraph, dict[str, Any], Path, RuntimeContext, Path]:
    """准备一个"等待人工审批的删除请求"。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("保留我", encoding="utf-8", newline="")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    model = model_factory(
        decision="tool",
        reply="好的",
        tool_calls=[{"name": "delete_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    context = _context_for(workspace, model, app_config)
    first = graph.invoke(
        {"user_input": "删除 note.txt", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )
    return graph, first, target, context, workspace


def test_delete_waits_for_approval_and_survives_reconnect(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """删除必须挂起等待人工确认；挂起状态在 Checkpoint 里（重启后仍可恢复）。"""
    _graph, first, target, _, _ = _delete_flow(tmp_path, model_factory, app_config)

    assert target.is_file()
    assert first["pending_approval"]["tool_name"] == "delete_file"
    assert first["__interrupt__"][0].value["confirmation_stage"] == 1
    assert not first.get("tool_results")

    # 换一条连接（等价重启）后，仍然停在审批节点
    reopened = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    snapshot = reopened.get_state(_config(THREAD))

    assert snapshot.next == ("approval_interrupt_node",)
    assert snapshot.values["pending_approval"]["tool_name"] == "delete_file"


def test_delete_rejection_keeps_file_and_stops(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    graph, _, target, context, _ = _delete_flow(tmp_path, model_factory, app_config)

    result = graph.invoke(
        Command(resume={"decision": "reject", "reason": "先别删"}),
        config=_config(THREAD),
        context=context,
    )

    assert target.is_file()
    assert [item["decision"] for item in result["approval_history"]] == ["reject"]
    assert result["final_response"] == nodes.REJECTED_REPLY
    assert graph.get_state(_config(THREAD)).next == ()  # 拒绝后不自动重试


def test_delete_approval_executes_exactly_once(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    graph, _, target, context, _ = _delete_flow(tmp_path, model_factory, app_config)

    result = graph.invoke(
        Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
    )

    assert not target.exists()
    assert result["tool_results"][-1]["status"] == "ok"
    executed = len(result["tool_results"])

    # 按钮双击 / 重复提交：不能再次执行
    try:
        again = graph.invoke(
            Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
        )
        executed_again = len(again.get("tool_results") or [])
    except Exception:  # noqa: BLE001 — 没有挂起时重复 resume 可能直接报错
        executed_again = executed

    assert executed_again == executed


def _recursive_delete_flow(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> tuple[Path, Path, builder.ChatGraph, RuntimeContext]:
    """准备一个“等待二次确认的递归删除”。"""
    workspace = tmp_path / "ws"
    folder = workspace / "data"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("a", encoding="utf-8", newline="")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    model = model_factory(
        decision="tool",
        reply="好的",
        tool_calls=[
            {
                "name": "delete_file",
                "args": {"path": "data", "recursive": True},
                "id": "call-1",
            }
        ],
    )
    context = _context_for(workspace, model, app_config)
    graph.invoke(
        {"user_input": "删除 data 目录", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )
    return workspace, folder, graph, context


def test_recursive_delete_needs_second_confirmation(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """第一次确认不构成授权，只有二次确认后才真正删除（架构 §6.5）。"""
    _, folder, graph, context = _recursive_delete_flow(tmp_path, model_factory, app_config)

    stage_two = graph.invoke(
        Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
    )

    assert folder.is_dir()  # 第一次确认不执行
    assert stage_two["__interrupt__"][0].value["confirmation_stage"] == 2
    assert not stage_two.get("tool_results")

    final = graph.invoke(
        Command(resume={"decision": "approve", "confirm_recursive": True}),
        config=_config(THREAD),
        context=context,
    )

    assert not folder.exists()
    assert final["tool_results"][-1]["status"] == "ok"


def test_recursive_delete_first_stage_cannot_authorize(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """第一次恢复即使带上 ``confirm_recursive`` 也不算授权：必须由第二次独立恢复确认。

    阶段不能由"决策内容"推断，否则伪造一次带标记的恢复就能跳过二次确认（架构 §6.5）。
    """
    _, folder, graph, context = _recursive_delete_flow(tmp_path, model_factory, app_config)

    stage_two = graph.invoke(
        Command(resume={"decision": "approve", "confirm_recursive": True}),
        config=_config(THREAD),
        context=context,
    )

    assert folder.is_dir()  # 第一次恢复不构成授权
    assert stage_two["__interrupt__"][0].value["confirmation_stage"] == 2
    assert not stage_two.get("tool_results")

    final = graph.invoke(
        Command(resume={"decision": "approve", "confirm_recursive": True}),
        config=_config(THREAD),
        context=context,
    )

    assert not folder.exists()
    assert final["tool_results"][-1]["status"] == "ok"


def test_recursive_delete_first_stage_reject_ends_immediately(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """第一阶段就拒绝时立即结束，不再要求第二次确认（架构 §5.3 的三出口）。"""
    _, folder, graph, context = _recursive_delete_flow(tmp_path, model_factory, app_config)

    result = graph.invoke(
        Command(resume={"decision": "reject", "reason": "先别删"}),
        config=_config(THREAD),
        context=context,
    )

    assert folder.is_dir()
    assert not result.get("__interrupt__")
    assert graph.get_state(_config(THREAD)).next == ()
    assert [record["decision"] for record in result["approval_history"]] == ["reject"]


def test_recursive_delete_first_stage_edit_ends_immediately(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """第一阶段选择修改参数时也立即结束，不再弹第二次确认。"""
    _, folder, graph, context = _recursive_delete_flow(tmp_path, model_factory, app_config)

    result = graph.invoke(
        Command(
            resume={
                "decision": "edit",
                "edited_arguments": {"path": "data/a.txt", "recursive": False},
                "reason": "只删一个文件",
            }
        ),
        config=_config(THREAD),
        context=context,
    )

    assert folder.is_dir()
    assert not result.get("__interrupt__")
    assert [record["decision"] for record in result["approval_history"]] == ["edit"]


def _numbered_lines(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix} {index}" for index in range(count))


def _large_edit_flow(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> tuple[Path, builder.ChatGraph, RuntimeContext]:
    """准备一个“等待审批的大范围编辑”。

    250 行新增远超 ``security.large_edit.max_changed_lines_without_approval``（示例配置 200）。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "big.txt"
    target.write_text(_numbered_lines("line", 300) + "\n", encoding="utf-8", newline="")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    model = model_factory(
        decision="tool",
        reply="好的",
        tool_calls=[
            {
                "name": "edit_file",
                "args": {
                    "path": "big.txt",
                    "old_text": "line 0",
                    "new_text": _numbered_lines("new", 250),
                    "expected_sha256": sha,
                },
                "id": "call-edit",
            }
        ],
    )
    context = _context_for(workspace, model, app_config)
    first = graph.invoke(
        {"user_input": "重写 big.txt 的开头", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )
    assert first["pending_approval"]["tool_name"] == "edit_file"  # 确认它确实需要审批
    return target, graph, context


def test_large_edit_executes_after_approval(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """大范围编辑经人工批准后必须真的写入（架构 §6.3 的审批出口）。"""
    target, graph, context = _large_edit_flow(tmp_path, model_factory, app_config)

    result = graph.invoke(
        Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
    )

    assert result["tool_results"][-1]["status"] == "ok"
    assert "new 0" in target.read_text(encoding="utf-8")


def test_large_edit_rejection_keeps_file(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """拒绝大范围编辑时文件内容不变。"""
    target, graph, context = _large_edit_flow(tmp_path, model_factory, app_config)
    before = target.read_text(encoding="utf-8")

    result = graph.invoke(
        Command(resume={"decision": "reject"}), config=_config(THREAD), context=context
    )

    assert target.read_text(encoding="utf-8") == before
    assert all(record["status"] != "ok" for record in result.get("tool_results") or [])


def _overwrite_write_flow(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> tuple[Path, builder.ChatGraph, RuntimeContext]:
    """准备一个“等待审批的覆盖写”。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("旧内容\n", encoding="utf-8", newline="")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    model = model_factory(
        decision="tool",
        reply="好的",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "note.txt", "content": "新内容\n", "overwrite": True},
                "id": "call-write",
            }
        ],
    )
    context = _context_for(workspace, model, app_config)
    first = graph.invoke(
        {"user_input": "覆盖 note.txt", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )
    assert first["pending_approval"]["tool_name"] == "write_file"
    return target, graph, context


def test_overwrite_write_executes_after_approval(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """覆盖写经人工批准后必须真的覆盖（架构 §6.4 的审批出口）。"""
    target, graph, context = _overwrite_write_flow(tmp_path, model_factory, app_config)
    assert target.read_text(encoding="utf-8") == "旧内容\n"

    result = graph.invoke(
        Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
    )

    assert result["tool_results"][-1]["status"] == "ok"
    assert target.read_text(encoding="utf-8") == "新内容\n"


def test_overwrite_write_rejection_keeps_old_content(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """拒绝覆盖时旧内容必须原样保留。"""
    target, graph, context = _overwrite_write_flow(tmp_path, model_factory, app_config)

    result = graph.invoke(
        Command(resume={"decision": "reject"}), config=_config(THREAD), context=context
    )

    assert target.read_text(encoding="utf-8") == "旧内容\n"
    assert all(record["status"] != "ok" for record in result.get("tool_results") or [])


def test_rejected_action_cannot_be_retried_with_new_call_id(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """拒绝按动作指纹匹配：换个 tool_call_id 也绕不过（架构 §5.3）。"""
    graph, _, target, context, workspace = _delete_flow(tmp_path, model_factory, app_config)
    graph.invoke(
        Command(resume={"decision": "reject"}), config=_config(THREAD), context=context
    )

    retry_model = model_factory(
        decision="tool",
        reply="好的",
        tool_calls=[{"name": "delete_file", "args": {"path": "note.txt"}, "id": "call-9"}],
    )
    result = graph.invoke(
        {"user_input": "再删一次", "thread_id": THREAD},
        config=_config(THREAD),
        context=_context_for(workspace, retry_model, app_config),
    )

    assert target.is_file()
    assert result["tool_results"][-1]["status"] == "denied"
    assert "已被你拒绝" in result["tool_results"][-1]["output_preview"]
    assert result.get("pending_approval") is None  # 不再重复弹审批


def test_edit_result_is_persisted_and_not_replayed(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """编辑落盘后换连接重开：文件已更新，Checkpoint 里保留了这次工具事件与证据。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    database = _database(tmp_path)

    model = model_factory(
        decision="tool",
        reply="已修改",
        tool_calls=[
            {
                "name": "edit_file",
                "args": {
                    "path": "note.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                    "expected_sha256": sha,
                },
                "id": "call_1",
            }
        ],
    )
    first = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    first.invoke(
        {"user_input": "把 value 改成 2", "thread_id": THREAD},
        config=_config(THREAD),
        context=_context_for(workspace, model, app_config),
    )
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    # 换连接重开（等价重启）：只读状态，不会重新执行工具，文件内容保持不变
    reopened = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    values = builder.read_thread_state(reopened, THREAD)

    assert [event.status for event in values["tool_events"]] == ["ok"]
    assert [record["status"] for record in values["tool_results"]] == ["ok"]
    assert values["final_response"] == "已修改"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_mixed_tool_calls_keep_free_pass_calls_after_approval(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """同一条回复里既有免审调用又有需审批调用时，批准后两类都必须有结果（架构 §4.2）。

    审批出口只应替换"被审批的那一个调用"：整段覆盖会让同批次的免审调用既没执行、
    也没有对应的 ``ToolMessage``，模型看到的是"有 tool_call 没有结果"。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("hello", encoding="utf-8", newline="")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    model = model_factory(
        decision="tool",
        reply="都做完了",
        tool_calls=[
            {"name": "read_file", "args": {"path": "note.txt"}, "id": "call-read"},
            {"name": "delete_file", "args": {"path": "note.txt"}, "id": "call-delete"},
        ],
    )
    context = _context_for(workspace, model, app_config)

    first = graph.invoke(
        {"user_input": "读一下再删掉", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )
    assert first["__interrupt__"][0].value["tool_call_id"] == "call-delete"

    result = graph.invoke(
        Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
    )

    statuses = {record["tool_call_id"]: record["status"] for record in result["tool_results"]}
    assert statuses["call-read"] == "ok"  # 免审调用不能因为审批而丢失
    assert statuses["call-delete"] == "ok"
    assert not target.exists()


# --------------------------------------------------------------------------- 本轮与历史的作用域


def test_new_request_resets_turn_scoped_state(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """新一轮提交必须清掉上一轮的循环计数、任务目标与待处理调用（架构 §4.2）。

    否则多轮工具任务会一路累计到 ``agent.max_tool_loops``，新任务还会带着旧目标、
    旧的待执行调用与挂起审批继续跑。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    graph.update_state(
        _config(THREAD),
        {
            "loop_count": 19,
            "max_loop_count": 20,
            "task_goal": "上一轮的目标",
            "pending_tool_calls": [
                {
                    "name": "delete_file",
                    "args": {"path": "x"},
                    "id": "stale-call",
                    "type": "tool_call",
                }
            ],
            "pending_approval": {
                "approval_id": "stale",
                "tool_call_id": "stale-call",
                "tool_name": "delete_file",
                "action_fingerprint": "stale",
                "arguments": {"path": "x"},
                "impact_summary": "删除 x",
                "requires_second_confirmation": False,
                "created_at": datetime.now(timezone.utc),
            },
            "reflection_result": ReflectionResult(
                goal_achieved=False, result_trust="uncertain", next_action="continue"
            ),
        },
    )
    model = model_factory(decision="chat", reply="好的")
    context = _context_for(workspace, model, app_config)

    result = graph.invoke(
        {"user_input": "新任务", "thread_id": THREAD}, config=_config(THREAD), context=context
    )

    assert result["task_status"] == "completed"
    assert result["loop_count"] == 0
    assert result["retry_count"] == 0
    assert result["task_goal"] is None
    assert result["pending_tool_calls"] == []
    assert result["pending_approval"] is None
    assert result["reflection_result"] is None


def test_new_request_does_not_reach_previous_loop_limit(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """上一轮已到循环上限时，新任务不能被立刻判为超限。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    graph.update_state(_config(THREAD), {"loop_count": 20, "max_loop_count": 20})
    model = model_factory(decision="tool", reply="你好")  # 工具分支才会经过循环上限判定
    context = _context_for(workspace, model, app_config)

    result = graph.invoke(
        {"user_input": "新任务", "thread_id": THREAD}, config=_config(THREAD), context=context
    )

    assert result["task_status"] == "completed"
    assert result["final_response"] == "你好"


def test_reused_tool_call_id_across_turns_is_executed_again(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """跨轮复用同一个 ``tool_call_id`` 时，本轮的调用必须真的执行（架构 §4.2）。

    去重的作用域是 ``run_id + tool_call_id``：只看调用 ID 会把新调用误判成"已完成"，
    既跳过执行，也会把新结果当重复项丢掉。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello", encoding="utf-8", newline="")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    # 模拟上一轮已经用同一个 ID 执行过一次
    graph.update_state(
        _config(THREAD),
        {
            "run_id": "previous-run",
            "tool_results": [
                ToolResultRecord(
                    tool_call_id="call-1",
                    tool_name="read_file",
                    status="ok",
                    truncated=False,
                    output_preview="上一轮的结果",
                    output_digest="previous-digest",
                    run_id="previous-run",
                )
            ],
        },
    )
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    context = _context_for(workspace, model, app_config)

    result = graph.invoke(
        {"user_input": "再读一次 note.txt", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )

    executed = [
        record for record in result["tool_results"] if record["tool_call_id"] == "call-1"
    ]
    assert len(executed) == 2  # 两轮各一条，互不覆盖
    assert executed[-1]["status"] == "ok"
    assert executed[-1]["run_id"] == result["run_id"]
    assert executed[-1]["output_digest"] != "previous-digest"  # 确实重新执行过


def test_unscoped_legacy_records_do_not_leak_into_current_task(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """升级前的旧记录（没有 ``run_id``）只保证可读，不参与本轮判定与反思（架构 §4.2）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello", encoding="utf-8", newline="")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    graph.update_state(
        _config(THREAD),
        {
            "run_id": "legacy-run",
            "tool_events": [
                ToolEvent(
                    tool_call_id="old-call",
                    tool_name="read_file",
                    status="ok",
                    output_preview="旧事件（无 run_id）",
                    output_digest="legacy",
                    run_id="",
                )
            ],
            "tool_results": [
                ToolResultRecord(
                    tool_call_id="call-1",
                    tool_name="read_file",
                    status="ok",
                    truncated=False,
                    output_preview="旧结果（无 run_id）",
                    output_digest="legacy",
                    run_id="",
                )
            ],
        },
    )
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    context = _context_for(workspace, model, app_config)

    result = graph.invoke(
        {"user_input": "读 note.txt", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )

    executed = [
        record for record in result["tool_results"] if record["tool_call_id"] == "call-1"
    ]
    assert len(executed) == 2  # 无作用域的旧结果不被当成"已执行"
    assert executed[-1]["run_id"] == result["run_id"]
    assert [item.source_event_id for item in result["final_evidence"]] == ["call-1"]


def test_two_approval_calls_both_get_results(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """同一条回复里有两个需审批动作时，第二个也要有明确结果（架构 §4.2）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a", encoding="utf-8", newline="")
    (workspace / "b.txt").write_text("b", encoding="utf-8", newline="")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    model = model_factory(
        decision="tool",
        reply="好了",
        tool_calls=[
            {"name": "delete_file", "args": {"path": "a.txt"}, "id": "del-a"},
            {"name": "delete_file", "args": {"path": "b.txt"}, "id": "del-b"},
        ],
    )
    context = _context_for(workspace, model, app_config)

    first = graph.invoke(
        {"user_input": "删掉 a.txt 和 b.txt", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )

    assert first["__interrupt__"][0].value["tool_call_id"] == "del-a"
    pending = {record["tool_call_id"]: record["status"] for record in first["tool_results"]}
    assert pending["del-b"] == "denied"  # 第二个调用有明确结果，不是凭空消失
    assert (workspace / "b.txt").is_file()

    result = graph.invoke(
        Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
    )

    statuses = {record["tool_call_id"]: record["status"] for record in result["tool_results"]}
    assert statuses["del-a"] == "ok"
    assert statuses["del-b"] == "denied"
    assert not (workspace / "a.txt").exists()
    assert (workspace / "b.txt").is_file()


def test_approval_does_not_carry_over_to_another_turn(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """上一轮的批准不能授权本轮复用同一调用 ID 的动作（架构 §5.3）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("旧内容\n", encoding="utf-8", newline="")
    arguments: dict[str, Any] = {
        "path": "note.txt",
        "content": "新内容\n",
        "overwrite": True,
    }
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    graph.update_state(
        _config(THREAD),
        {
            "run_id": "previous-run",
            "approval_history": [
                ApprovalDecision(
                    approval_id="previous-approval",
                    run_id="previous-run",
                    tool_call_id="call-1",
                    action_fingerprint=action_fingerprint(
                        "write_file", arguments, policy=policy
                    ),
                    decision="approve",
                    edited_arguments=None,
                    reason="上一轮批准过同一个动作",
                    created_at=datetime.now(timezone.utc),
                )
            ],
        },
    )
    model = model_factory(
        decision="tool",
        reply="好了",
        tool_calls=[{"name": "write_file", "args": arguments, "id": "call-1"}],
    )
    context = _context_for(workspace, model, app_config)

    result = graph.invoke(
        {"user_input": "覆盖 note.txt", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )

    assert result.get("__interrupt__")  # 必须重新请求人工审批
    assert target.read_text(encoding="utf-8") == "旧内容\n"


def test_reflection_ignores_previous_turn_evidence(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """反思只统计当前任务的事件与错误，历史任务证据不混入（架构 §4.2）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello", encoding="utf-8", newline="")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    graph.update_state(
        _config(THREAD),
        {
            "run_id": "previous-run",
            "tool_events": [
                ToolEvent(
                    tool_call_id="old-call",
                    tool_name="read_file",
                    status="ok",
                    output_preview="上一轮的输出",
                    output_digest="old-digest",
                    run_id="previous-run",
                )
            ],
            "tool_results": [
                ToolResultRecord(
                    tool_call_id="old-call",
                    tool_name="read_file",
                    status="ok",
                    truncated=False,
                    output_preview="上一轮的输出",
                    output_digest="old-digest",
                    run_id="previous-run",
                )
            ],
            "error_events": [
                ErrorEvent(
                    event_id="previous-run:error:safety_gate:0:",
                    stage="safety_gate",
                    message="上一轮的历史错误",
                    tool_call_id=None,
                    run_id="previous-run",
                    created_at=datetime.now(timezone.utc),
                )
            ],
        },
    )
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    context = _context_for(workspace, model, app_config)

    result = graph.invoke(
        {"user_input": "读 note.txt", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )

    evidence = result["final_evidence"]
    assert [item.source_event_id for item in evidence] == ["call-1"]
    assert all(
        "历史错误" not in anomaly for anomaly in result["reflection_result"].anomalies
    )


# --------------------------------------------------------------------------- T12 管道命令审批


def _piped_command() -> str:
    """当前平台上一条真正的管道命令（两个平台都能打印 hi）。"""
    return "echo hi | findstr hi" if os.name == "nt" else "echo hi | grep hi"


def _shell_flow(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> tuple[builder.ChatGraph, dict[str, Any], RuntimeContext]:
    """准备一个“等待人工审批的管道命令”。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    model = model_factory(
        decision="tool",
        reply="好的",
        tool_calls=[
            {"name": "run_shell", "args": {"command": _piped_command()}, "id": "call-shell"}
        ],
    )
    context = _context_for(workspace, model, app_config)
    first = graph.invoke(
        {"user_input": "看看 hi", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )
    return graph, first, context


def test_shell_syntax_waits_for_approval_then_executes_once(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """管道命令必须挂起；批准后按平台适配器执行且只执行一次（架构 §6.6）。

    这条链路同时验证审批节点与工具算出的**动作指纹一致**：两者不一致时，
    批准会失效并报错（这正是 T12 最容易出问题的地方）。
    """
    graph, first, context = _shell_flow(tmp_path, model_factory, app_config)

    assert first["pending_approval"]["tool_name"] == "run_shell"
    assert first["__interrupt__"][0].value["confirmation_stage"] == 1
    assert not first.get("tool_results")

    approved = graph.invoke(
        Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
    )

    results = approved.get("tool_results") or []
    assert [record["status"] for record in results] == ["ok"]
    tool_messages = [
        message for message in approved["messages"] if isinstance(message, ToolMessage)
    ]
    assert tool_messages
    payload = json.loads(tool_messages[0].text)
    assert payload["exit_code"] == 0
    assert "hi" in payload["stdout"]
    assert payload["executor"] == ("cmd" if os.name == "nt" else "sh")
    assert Path(payload["argv"][0]).name.casefold() == ("cmd.exe" if os.name == "nt" else "sh")
    assert payload["argv"][-1] == _piped_command()  # 原始命令原样交给适配器

    # 按钮双击 / 重复提交：不能再次执行
    executed = len(results)
    try:
        again = graph.invoke(
            Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
        )
        executed_again = len(again.get("tool_results") or [])
    except Exception:  # noqa: BLE001 — 没有挂起时重复 resume 可能直接报错
        executed_again = executed
    assert executed_again == executed


def test_approved_shell_with_explicit_timeout_executes(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """显式超时也要两侧一致：审批节点与工具算出的指纹必须匹配（架构 §5.3）。

    只要一处按"解析后的有效超时"、另一处按"省略默认值"计算，批准就会失效。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    model = model_factory(
        decision="tool",
        reply="好了",
        tool_calls=[
            {
                "name": "run_shell",
                "args": {"command": "python -V", "timeout_seconds": 5},
                "id": "call-timeout",
            }
        ],
    )
    context = _context_for(workspace, model, app_config)

    first = graph.invoke(
        {"user_input": "看一下 python 版本", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )
    assert first["pending_approval"]["tool_name"] == "run_shell"

    result = graph.invoke(
        Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
    )

    assert result["tool_results"][-1]["status"] == "ok"
    assert "Python" in result["tool_results"][-1]["output_preview"]


def _with_default_shell_timeout(app_config: AppConfig, seconds: int) -> AppConfig:
    """把 ``security.shell.default_timeout_seconds`` 换成指定值。"""
    data = app_config.model_dump()
    data["security"]["shell"]["default_timeout_seconds"] = seconds
    return AppConfig.model_validate(data)


def test_approved_shell_without_timeout_uses_config_default(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """配置把默认超时调小到 30 秒时，省略超时的命令仍要能批准并执行。

    这条走的是"工具 Schema 省略参数"之后的运行链路：安全判定 → 动作指纹 → 工具执行
    的有效默认值（验证的是链路，不是 Schema 本身的内容）。
    修前工具侧按出厂常量 60 秒算，批准后会在执行阶段被判"超过配置上限 30 秒"。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    model = model_factory(
        decision="tool",
        reply="好了",
        tool_calls=[
            {"name": "run_shell", "args": {"command": "python -V"}, "id": "call-default"}
        ],
    )
    context = _context_for(workspace, model, _with_default_shell_timeout(app_config, 30))

    first = graph.invoke(
        {"user_input": "看一下 python 版本", "thread_id": THREAD},
        config=_config(THREAD),
        context=context,
    )
    assert first["pending_approval"]["tool_name"] == "run_shell"

    result = graph.invoke(
        Command(resume={"decision": "approve"}), config=_config(THREAD), context=context
    )

    assert result["tool_results"][-1]["status"] == "ok"
    assert "Python" in result["tool_results"][-1]["output_preview"]


def test_shell_rejection_does_not_execute(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """拒绝后不得执行，也不自动重试（架构 §5.3）。"""
    graph, _, context = _shell_flow(tmp_path, model_factory, app_config)

    result = graph.invoke(
        Command(resume={"decision": "reject", "reason": "不跑管道"}),
        config=_config(THREAD),
        context=context,
    )

    assert all(record["status"] != "ok" for record in result.get("tool_results") or [])
    assert graph.get_state(_config(THREAD)).next == ()


# --------------------------------------------------------------------------- T11 会话索引


def _session_store(tmp_path: Path, *, workspace: Path | None = None) -> SessionStore:
    """内存库 + 已迁移的结构；等价于打开一个新的会话索引。"""
    connection = sqlite3.connect(":memory:")
    migrations.apply_migrations(connection)
    return SessionStore(connection, workspace_root=workspace or tmp_path)


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")

    first = migrations.apply_migrations(connection)
    second = migrations.apply_migrations(connection)

    assert first == second == migrations.SCHEMA_VERSION
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
    ).fetchone()
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (
        migrations.SCHEMA_VERSION,
    )


def test_ensure_active_creates_one_session_only(tmp_path: Path) -> None:
    store = _session_store(tmp_path)

    first = store.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)
    again = store.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)

    assert first.session_id == again.session_id
    assert len(store.list_sessions()) == 1
    assert first.is_active
    # 第一个会话沿用 v0.1a 的固定 thread_id，升级前的 Checkpoint 不会失联
    assert first.thread_id == checkpoint.DEFAULT_THREAD_ID
    assert first.project_id == project_id_for(tmp_path)


def test_ensure_active_adopts_existing_legacy_checkpoint(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT)")
    connection.execute(
        "INSERT INTO checkpoints VALUES (?, ?)", (checkpoint.DEFAULT_THREAD_ID, "")
    )
    migrations.apply_migrations(connection)
    store = SessionStore(connection, workspace_root=tmp_path)

    session = store.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)

    assert session.thread_id == checkpoint.DEFAULT_THREAD_ID
    assert session.title == ADOPTED_TITLE


def test_new_session_has_own_thread_id_and_switches_active(tmp_path: Path) -> None:
    store = _session_store(tmp_path)
    first = store.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)

    second = store.create()

    assert second.thread_id != first.thread_id
    assert second.is_active
    assert len(store.list_sessions()) == 2
    stored_first = store.get(first.session_id)
    assert stored_first is not None and stored_first.status == STATUS_IDLE

    switched = store.switch(first.session_id)

    assert switched.is_active
    stored_second = store.get(second.session_id)
    assert stored_second is not None and stored_second.is_active is False


def test_sessions_are_scoped_per_workspace(tmp_path: Path) -> None:
    """同一个库被两个工作区共用时：各自只看得到自己的会话，thread_id 不能撞。"""
    connection = sqlite3.connect(":memory:")
    migrations.apply_migrations(connection)
    store_a = SessionStore(connection, workspace_root=tmp_path / "a")
    store_b = SessionStore(connection, workspace_root=tmp_path / "b")

    session_a = store_a.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)
    session_b = store_b.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)

    assert session_a.project_id != session_b.project_id
    assert session_a.thread_id != session_b.thread_id  # 第二个工作区退回自动生成的键
    assert [item.session_id for item in store_a.list_sessions()] == [session_a.session_id]
    assert [item.session_id for item in store_b.list_sessions()] == [session_b.session_id]
    active_a = store_a.active()
    active_b = store_b.active()
    assert active_a is not None and active_a.session_id == session_a.session_id
    assert active_b is not None and active_b.session_id == session_b.session_id


def test_rename_validates_title(tmp_path: Path) -> None:
    store = _session_store(tmp_path)
    session = store.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)

    renamed = store.rename(session.session_id, "  扫描   项目结构  ")
    assert renamed.title == "扫描 项目结构"

    for bad in ("   ", "长" * (TITLE_MAX_CHARS + 1)):
        with pytest.raises(SessionStoreError):
            store.rename(session.session_id, bad)

    with pytest.raises(SessionStoreError):
        store.rename("no-such-session", "名字")
    with pytest.raises(SessionStoreError):
        store.switch("no-such-session")
    with pytest.raises(SessionStoreError):
        store.touch("no-such-session")


def test_touch_records_activity_and_names_session_once(tmp_path: Path) -> None:
    store = _session_store(tmp_path)
    session = store.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)

    touched = store.touch(
        session.session_id,
        last_checkpoint_id="cp-1",
        title_hint="帮我删除 aa.txt\n第二行忽略",
    )

    assert touched.title == "帮我删除 aa.txt"  # 只取首行
    assert touched.last_checkpoint_id == "cp-1"
    assert touched.updated_at >= session.updated_at

    store.rename(session.session_id, "我的会话")
    kept = store.touch(session.session_id, title_hint="换个话题")
    assert kept.title == "我的会话"  # 手动命名不会被自动命名覆盖
    assert kept.last_checkpoint_id == "cp-1"  # 未传新指针时保持原值

    fresh = store.create()
    truncated = store.touch(fresh.session_id, title_hint="字" * (AUTO_TITLE_CHARS + 5))
    assert len(truncated.title) == AUTO_TITLE_CHARS + 1
    assert truncated.title.endswith("…")


def test_project_id_prefers_git_root(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    nested = repository / "src" / "pkg"
    nested.mkdir(parents=True)
    outside = tmp_path / "other"
    outside.mkdir()

    assert find_git_root(nested) == repository.resolve()
    assert project_id_for(nested) == project_id_for(repository)
    assert project_id_for(outside) != project_id_for(repository)
    assert project_id_for(outside) == project_id_for(outside)  # 稳定：同路径同结果

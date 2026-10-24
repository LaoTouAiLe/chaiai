"""验证实际重叠、顺序边界、取消、上下文及 Checkpoint，而非只验证线程池接口。"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.runtime import Runtime

from local_agent.cancellation import CancellationToken
from local_agent.config import WebAccessConfig
from local_agent.graph import builder, nodes, tool_parallel
from local_agent.graph.tool_parallel import parallel_read_allowed
from local_agent.security.approval import action_fingerprint
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.tool_registry import ToolRegistry


def call(name, identity, **args):
    return {"name": name, "id": identity, "args": args, "type": "tool_call"}


def context_for(tmp_path, app_config, model_factory, *overrides, cancellation=None):
    app_config.memory.long_term.enabled = False
    app_config.memory.medium_term.enabled = False
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={},
                                        cancellation=cancellation)
    original = ToolRegistry.build(policy, git_available=True)
    replacements = {item.name: item for item in overrides}
    registry = ToolRegistry([replacements.get(name) or original.get(name) for name in original.names()])
    return builder.build_runtime_context(
        app_config=app_config, model_adapter=model_factory(decision="tool"),
        workspace_root=tmp_path, security_policy=policy, tool_registry=registry,
        cancellation=cancellation,
    )


def execute(context, calls, **state):
    return nodes.tool_execution_node(
        {"run_id": "parallel-run", "mode": "tool", "pending_tool_calls": calls, **state},
        Runtime(context=context),
    )


def test_reads_overlap_and_results_keep_original_order(tmp_path, app_config, model_factory):
    rendezvous = threading.Barrier(3)
    b_done, c_done = threading.Event(), threading.Event()
    finished = []
    threads = []

    @tool
    def read_file(path: str) -> str:
        """Read a controlled test file."""
        threads.append(threading.get_ident())
        rendezvous.wait(timeout=5)
        if path == "a":
            assert b_done.wait(5)
        if path == "b":
            assert c_done.wait(5)
        finished.append(path)
        if path == "c":
            c_done.set()
        if path == "b":
            b_done.set()
        return path

    context = context_for(tmp_path, app_config, model_factory, read_file)
    result = execute(context, [call("read_file", name, path=name) for name in "abc"])
    assert finished == ["c", "b", "a"]
    assert len(set(threads)) == 3 and threading.get_ident() not in threads
    assert [message.content for message in result["messages"]] == list("abc")
    assert [event.tool_call_id for event in result["tool_events"]] == list("abc")
    events = result["tool_events"]
    assert max(event.started_at for event in events) < min(event.finished_at for event in events)
    assert events[2].finished_at <= events[1].finished_at <= events[0].finished_at


def test_reads_never_cross_a_write_and_writes_stay_on_graph_thread(tmp_path, app_config, model_factory):
    target = tmp_path / "value.txt"
    target.write_text("before")
    phases = {name: threading.Barrier(2) for name in ("before", "after")}
    lock = threading.Lock()
    active = 0

    @tool
    def read_file(path: str) -> str:
        """Read the current value."""
        nonlocal active
        with lock:
            active += 1
        try:
            value = target.read_text()
            phases[value].wait(timeout=5)
            return value
        finally:
            with lock:
                active -= 1

    @tool
    def write_file(path: str, content: str) -> str:
        """Write the next value."""
        assert threading.current_thread() is threading.main_thread()
        assert active == 0
        target.write_text(content)
        return "written"

    context = context_for(tmp_path, app_config, model_factory, read_file, write_file)
    result = execute(context, [
        call("read_file", "before-1", path="value.txt"),
        call("read_file", "before-2", path="value.txt"),
        call("write_file", "write", path="value.txt", content="after"),
        call("read_file", "after-1", path="value.txt"),
        call("read_file", "after-2", path="value.txt"),
    ])
    assert [message.content for message in result["messages"]] == ["before", "before", "written", "after", "after"]


def test_cancel_prevents_queued_calls_without_discarding_completed_reads(
    tmp_path, app_config, model_factory, monkeypatch,
):
    monkeypatch.setattr(tool_parallel, "ThreadPoolExecutor", lambda **kwargs: ThreadPoolExecutor(max_workers=2, **kwargs))
    token = CancellationToken()
    rendezvous = threading.Barrier(2)
    cancelled = threading.Event()
    invoked = []

    @tool
    def read_file(path: str) -> str:
        """Read or trigger cancellation."""
        invoked.append(path)
        rendezvous.wait(timeout=5)
        if path == "a":
            token.cancel("stop parallel reads")
            cancelled.set()
        else:
            assert cancelled.wait(5)
        return "actual " + path

    context = context_for(tmp_path, app_config, model_factory, read_file, cancellation=token)
    result = execute(context, [call("read_file", name, path=name) for name in "abcd"])
    assert sorted(invoked) == ["a", "b"]
    assert [record["status"] for record in result["tool_results"]] == ["ok", "ok", "denied", "denied"]
    assert [message.content for message in result["messages"][:2]] == ["actual a", "actual b"]
    assert "stop parallel reads" in result["messages"][2].content


def test_contextvars_are_copied_and_isolated_per_call(tmp_path, app_config, model_factory):
    marker = ContextVar("parallel-test-marker", default="missing")
    token = marker.set("outer request")
    rendezvous = threading.Barrier(2)

    @tool
    def read_file(path: str) -> str:
        """Read the request context and change only this worker context."""
        received = marker.get()
        marker.set(path)
        rendezvous.wait(timeout=5)
        assert marker.get() == path
        return received

    try:
        context = context_for(tmp_path, app_config, model_factory, read_file)
        result = execute(context, [call("read_file", name, path=name) for name in "ab"])
        assert [m.content for m in result["messages"]] == ["outer request", "outer request"]
        assert marker.get() == "outer request"
    finally:
        marker.reset(token)


def test_one_failed_read_does_not_cancel_siblings(tmp_path, app_config, model_factory):
    rendezvous = threading.Barrier(2)

    @tool
    def read_file(path: str) -> str:
        """Read a file or report a real error."""
        rendezvous.wait(timeout=5)
        if path == "bad":
            raise OSError("read failed")
        return "verified"

    context = context_for(tmp_path, app_config, model_factory, read_file)
    result = execute(context, [call("read_file", "bad", path="bad"), call("read_file", "good", path="good")])
    assert [r["status"] for r in result["tool_results"]] == ["error", "ok"]
    assert result["messages"][1].content == "verified"


@pytest.mark.parametrize("name,args", [
    ("write_file", {}), ("download_file", {}), ("delete_file", {}), ("run_shell", {}), ("git_commit", {}),
    ("browser", {"action": "snapshot"}), ("read_shell", {}), ("start_shell", {}),
    ("write_shell", {}), ("stop_shell", {}), ("delegate_research", {}),
    ("task_control", {}), ("update_plan", {}), ("grep", {"save_full": True}),
    ("glob", {"save_full": True}),
])
def test_state_changing_and_control_tools_are_ordering_barriers(name, args):
    assert not parallel_read_allowed(call(name, "id", **args), WebAccessConfig())


def test_custom_web_count_limits_still_apply_serially(tmp_path, app_config, model_factory):
    app_config.web_access.max_search_calls = 1
    invoked = []

    @tool
    def web_search(query: str) -> str:
        """Query a controlled search endpoint."""
        assert threading.current_thread() is threading.main_thread()
        invoked.append(query)
        return json.dumps({"results": []})

    context = context_for(tmp_path, app_config, model_factory, web_search)
    result = execute(context, [call("web_search", name, query=name) for name in "ab"])
    assert invoked == ["a"]
    assert [r["status"] for r in result["tool_results"]] == ["ok", "error"]


def test_confirmed_calls_are_not_prefetched_or_executed_again(tmp_path, app_config, model_factory):
    invoked = []
    rendezvous = threading.Barrier(2)

    @tool
    def read_file(path: str) -> str:
        """Read unconfirmed files."""
        invoked.append(path)
        rendezvous.wait(timeout=5)
        return path

    context = context_for(tmp_path, app_config, model_factory, read_file)
    prior = {"run_id": "parallel-run", "tool_call_id": "a", "tool_name": "read_file", "status": "ok",
             "truncated": False, "output_preview": "previous", "output_digest": "digest"}
    result = execute(context, [call("read_file", name, path=name) for name in "abc"], tool_results=[prior])
    assert sorted(invoked) == ["b", "c"]
    assert [m.content for m in result["messages"]] == ["b", "c"]
    assert [r["tool_call_id"] for r in result["tool_results"]] == ["b", "c"]
    assert prior["output_preview"] == "previous"


def test_approval_calls_keep_existing_single_use_execution(tmp_path, app_config, model_factory):
    threads = []

    @tool
    def read_file(path: str) -> str:
        """Read a file with a deliberately supplied approval record."""
        threads.append(threading.get_ident())
        return path

    context = context_for(tmp_path, app_config, model_factory, read_file)
    policy = context.security_policy
    fingerprint = action_fingerprint("read_file", {"path": "a"}, policy=policy)
    history = [{"decision": "approve", "run_id": "parallel-run", "tool_call_id": "a", "action_fingerprint": fingerprint}]
    result = execute(context, [call("read_file", name, path=name) for name in "ab"], approval_history=history)
    assert threads == [threading.get_ident()] * 2
    assert not policy.is_granted(fingerprint)
    assert [r["status"] for r in result["tool_results"]] == ["ok", "ok"]


def test_parallel_results_survive_sqlite_checkpoint_without_thread_writes(tmp_path, app_config, model_factory):
    from test_t43_main_loop import LoopModel, _tools

    from local_agent.persistence import checkpoint

    rendezvous = threading.Barrier(2)

    @tool
    def read_file(path: str) -> str:
        """Return independent evidence from a worker."""
        rendezvous.wait(timeout=5)
        return "evidence " + path

    context = context_for(tmp_path, app_config, model_factory, read_file)
    model = LoopModel(messages=iter([]), script=[
        _tools(call("read_file", "a", path="a"), call("read_file", "b", path="b")),
        AIMessage(content="两个文件已经核对。"),
    ])
    context.model_adapter = model
    database = checkpoint.open_database(tmp_path / "checkpoints.sqlite", workspace_root=tmp_path)
    try:
        graph = builder.build_chat_graph(database.checkpointer)
        config = {"configurable": {"thread_id": "parallel-checkpoint"}}
        result = graph.invoke({"user_input": "核对两个文件", "session_mode": "agent"}, config, context=context)
        saved = graph.get_state(config).values
        assert result["task_status"] == saved["task_status"] == "completed"
        messages = [m for m in saved["messages"] if isinstance(m, ToolMessage) and m.name == "read_file"]
        assert [m.content for m in messages] == ["evidence a", "evidence b"]
        assert model.decision_calls == 2
        assert result["pending_tool_calls"] == []
    finally:
        database.connection.close()

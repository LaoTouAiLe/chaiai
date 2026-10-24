"""Timing must explain wall time without counting parallel work twice."""
import json
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from local_agent.performance import breakdown, span, timed_node, trace_turn
from local_agent.persistence.checkpoint import open_database


def test_exclusive_breakdown_handles_nesting_and_overlap():
    def record(stage, start, end):
        return {"stage": stage, "start_ns": start * 1_000_000, "end_ns": end * 1_000_000}
    values = breakdown(0, 100_000_000, [record("context_build", 10, 80),
        record("context_build", 15, 75), record("model", 20, 40),
        record("tool", 30, 60), record("checkpoint", 50, 70), record("node", 0, 100)])
    assert values == {"model": 20, "tool": 20, "checkpoint": 10, "context_build": 20, "other": 30}


def test_graph_checkpoint_threads_share_the_trace(tmp_path):
    class State(TypedDict):
        run_id: str
        value: int

    def work(state):
        for stage in ("context_build", "model", "tool"):
            with span(stage, "offline", state["run_id"]):
                sum(range(100))
        return {"value": state["value"] + 1}

    database = open_database(tmp_path / "chat.sqlite", workspace_root=tmp_path)
    graph = StateGraph(State)
    graph.add_node("work", work)
    graph.add_edge(START, "work")
    graph.add_edge("work", END)
    compiled = graph.compile(checkpointer=database.checkpointer)
    path = tmp_path / "timings.jsonl"
    try:
        with trace_turn(path, "session"):
            result = compiled.invoke({"run_id": "run", "value": 0},
                                     {"configurable": {"thread_id": "thread"}})
        assert result["value"] == 1
    finally:
        database.connection.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert {"model", "tool", "context_build", "checkpoint"} <= {
        row["stage"] for row in rows if row["kind"] == "span"}
    assert len({row["trace_id"] for row in rows}) == 1
    assert rows[-1]["kind"] == "summary" and rows[-1]["run_id"] == "run"
    assert sum(rows[-1]["breakdown_ms"].values()) == pytest.approx(rows[-1]["total_ms"])
    assert all(row["end_ns"] >= row["start_ns"] for row in rows)


def test_unavailable_log_preserves_original_exception(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("existing")
    with (pytest.raises(ValueError, match="original"),
          trace_turn(blocked / "timings.jsonl", "session"), span("tool", "probe", "run")):
        raise ValueError("original")
    assert blocked.read_text() == "existing"


def test_new_turn_summary_uses_new_run_instead_of_inherited_checkpoint(tmp_path):
    path = tmp_path / "timings.jsonl"
    prepare = timed_node(lambda state: {"run_id": "new"},
                         stage="context_build", operation="prepare_context")
    with trace_turn(path, "session"):
        with span("checkpoint", "put", "previous"):
            pass
        prepare({"run_id": "previous"})
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[-1]["run_id"] == "new"

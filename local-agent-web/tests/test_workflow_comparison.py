"""T70 配对公平性、机械失败/越权硬门与真实离线工具对照。"""

from __future__ import annotations

import copy

import pytest

from local_agent import workflow_comparison as comparison


@pytest.fixture(scope="module")
def report():
    return comparison.run_comparison(2)


def test_actual_paired_graph_runs_with_same_config_model_and_frozen_validator(report):
    assert report["passed"] is True
    assert report["hard_gates_passed"] is True
    assert report["offline_subgraph_advantage_proven"] is True
    assert report["real_model_advantage_proven"] is False
    assert report["paid_model_calls"] == 0
    assert report["semantic_quality_reviewed"] is False
    assert len(report["runs"]) == 8
    assert [row[2] for row in report["execution_order"]] == [
        "free_loop", "subgraph", "subgraph", "free_loop",
        "free_loop", "subgraph", "subgraph", "free_loop",
    ]
    for case in report["cases"]:
        assert case["completion_counts"] == {"free_loop": 2, "subgraph": 2}
        assert case["attempts_per_arm"] == {"free_loop": 2, "subgraph": 2}
        assert case["model_calls_improved_in_every_pair"] is True
    expected_counts = {("code_change", "free_loop"): (6, 6),
                       ("code_change", "subgraph"): (1, 6),
                       ("readonly_research", "free_loop"): (4, 4),
                       ("readonly_research", "subgraph"): (0, 4)}
    for row in report["runs"]:
        assert row["passed"], row["failures"]
        assert (row["model_calls"], row["tool_calls"]) == expected_counts[(row["case_id"], row["mode"])]
        assert row["actual_input_tokens"] is None
        assert row["actual_output_tokens"] is None
        assert row["actual_cache_read_tokens"] is None
        assert row["tool_calls"] == len(row["tool_events"]) == len(row["tool_outputs"])
        assert all(item["output_sha256"] for item in row["model_requests"])
        if row["case_id"] == "code_change":
            test_output = next(item["output"] for item in row["tool_outputs"] if item["name"] == "run_shell")
            assert test_output["exit_code"] == 0
            assert "Ran 3 tests" in test_output["stderr"]
            assert test_output["stderr"].rstrip().endswith("OK")
        else:
            assert row["seed_sha256"] == row["final_file_sha256"]


@pytest.mark.parametrize("field,value", [
    ("task_fingerprint", "different"), ("profile", "general"), ("model", "other"),
    ("model_fingerprint", "different"), ("evaluation_config_fingerprint", "different"),
    ("seed_sha256", {}), ("workflow_request", {}), ("final_file_sha256", {}),
    ("passed", False), ("failures", ["真实失败"]), ("status", "failed"),
    ("model_calls", 99), ("tool_calls", 99),
])
def test_inconsistent_or_failed_attempt_blocks_advantage(report, field, value):
    rows = copy.deepcopy(report["runs"])
    rows[0][field] = value
    result = comparison.summarize_comparison(rows, 2)
    assert result["passed"] is False
    assert result["hard_gates_passed"] is False
    assert result["offline_subgraph_advantage_proven"] is False
    assert result["failures"]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "wrong_attempt"])
def test_missing_duplicate_or_unregistered_samples_are_not_discarded(report, mutation):
    rows = copy.deepcopy(report["runs"])
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif mutation == "unknown":
        rows[-1]["case_id"] = "other"
    else:
        rows[-1]["attempt"] = 3
    assert comparison.summarize_comparison(rows, 2)["passed"] is False


@pytest.mark.parametrize("mutation", ["readonly_write", "duplicate_edit", "wrong_write_path", "wrong_shell"])
def test_privilege_and_duplicate_side_effect_gates_ignore_claimed_success(report, mutation):
    rows = copy.deepcopy(report["runs"])
    row = next(row for row in rows if row["case_id"] == (
        "readonly_research" if mutation == "readonly_write" else "code_change"))
    if mutation == "readonly_write":
        row["tool_events"].append({"tool_name": "write_file", "status": "ok", "arguments": {}})
        row["tool_calls"] += 1
    else:
        event = next(item for item in row["tool_events"] if item["tool_name"] == (
            "run_shell" if mutation == "wrong_shell" else "edit_file"))
        if mutation == "duplicate_edit":
            row["tool_events"].append(copy.deepcopy(event))
            row["tool_calls"] += 1
        elif mutation == "wrong_write_path":
            event["arguments"]["path"] = "test_bounds.py"
        else:
            event["arguments"]["command"] = "python -c pass"
    result = comparison.summarize_comparison(rows, 2)
    assert result["passed"] is False
    assert any("副作用" in reason for reason in result["failures"])


def test_green_completion_without_per_pair_call_improvement_is_not_advantage(report):
    rows = copy.deepcopy(report["runs"])
    row = next(row for row in rows if row["mode"] == "subgraph" and row["case_id"] == "code_change")
    row["model_requests"] *= 6
    row["model_calls"] = 6
    result = comparison.summarize_comparison(rows, 2)
    assert result["hard_gates_passed"] is True
    assert result["passed"] is False
    assert result["offline_subgraph_advantage_proven"] is False


def test_runtime_exceptions_preserve_all_attempts_and_error_types(monkeypatch):
    class FailingGraph:
        def invoke(self, *_args, **_kwargs):
            raise RuntimeError("secret /private/absolute-path")

    monkeypatch.setattr(comparison.builder, "build_chat_graph", FailingGraph)
    result = comparison.run_comparison(2)
    assert len(result["runs"]) == 8
    assert result["passed"] is False
    assert all(row["runtime_error"] == "RuntimeError" for row in result["runs"])
    assert all(row["passed"] is False for row in result["runs"])
    assert all(row["tool_calls"] is None for row in result["runs"])
    assert all(row["tool_audit_complete"] is False for row in result["runs"])
    assert all(case["means"]["subgraph"]["tool_calls"] is None for case in result["cases"])
    assert "secret" not in str(result)


def test_exception_after_real_tools_keeps_actual_model_calls_and_unknown_tool_audit(monkeypatch):
    graph = comparison.builder.build_chat_graph()

    class RaiseAfterTools:
        def invoke(self, *args, **kwargs):
            state = graph.invoke(*args, **kwargs)
            assert len(state["tool_events"]) == 6
            raise RuntimeError("after real side effects")

    monkeypatch.setattr(comparison.builder, "build_chat_graph", RaiseAfterTools)
    row = comparison._run_arm(comparison.CODE_CASE, "free_loop", 1)
    assert row["passed"] is False
    assert row["model_calls"] == 6
    assert row["tool_calls"] is None
    assert row["tool_audit_complete"] is False
    assert row["runtime_error"] == "RuntimeError"


@pytest.mark.parametrize("repetitions", [0, -1, 11])
def test_invalid_repeat_count_never_runs_graph(monkeypatch, repetitions):
    monkeypatch.setattr(comparison.builder, "build_chat_graph", lambda: pytest.fail("unexpected graph"))
    with pytest.raises(ValueError):
        comparison.run_comparison(repetitions)

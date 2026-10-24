"""T64：真实图基线与失败证据拒绝，不联网、不产生模型费用。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from local_agent import workflow_eval as evaluation
from local_agent.capabilities import CAPABILITY_TOOLS, PROFILES
from local_agent.graph.state import ToolEvent


def _seed(root: Path, files: tuple[tuple[str, str], ...]) -> None:
    for relative, content in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))


@pytest.fixture(scope="module")
def observed() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """跑现有主图两次，捕获第一次真状态供负例篡改，不另造成功样本。"""
    states: dict[str, dict[str, Any]] = {}
    original = evaluation.validate_outcome

    def capture(case: evaluation.WorkflowCase, root: Path, state: dict[str, Any]) -> list[str]:
        states[case.case_id] = dict(state)
        return original(case, root, state)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(evaluation, "validate_outcome", capture)
        first = evaluation.run_baseline()
    second = evaluation.run_baseline()
    keys = ("case_id", "task_fingerprint", "evaluation_config_fingerprint", "profile", "status",
            "passed", "model_calls", "tool_calls", "loop_count")
    assert [[row[key] for key in keys] for row in first["cases"]] == [
        [row[key] for key in keys] for row in second["cases"]
    ]
    return first, states


def test_baseline_runs_current_graph_and_real_tools(observed) -> None:
    report, states = observed
    assert report["passed"] is True
    code, readonly = report["cases"]
    assert (code["model_calls"], code["tool_calls"]) == (6, 6)
    assert (readonly["model_calls"], readonly["tool_calls"]) == (4, 4)
    assert states["code_change"]["agent_loop_version"] == 1
    assert [event.tool_name for event in states["code_change"]["tool_events"]] == [
        "read_file", "read_file", "edit_file", "run_shell", "read_file", "task_control",
    ]


def test_report_does_not_claim_real_model_quality_or_export_messages(observed) -> None:
    report, _ = observed
    assert report["paid_model_calls"] == 0
    assert report["semantic_quality_reviewed"] is False
    assert report["subgraph_advantage_proven"] is False
    for row in report["cases"]:
        assert row["actual_input_tokens"] is None
        assert row["actual_output_tokens"] is None
        assert row["actual_cache_read_tokens"] is None
        assert "messages" not in row
    text = json.dumps(report)
    assert "chaiai-v07-baseline-" not in text
    assert "WORKFLOW_EVAL_UNUSED" not in text
    assert "reasoning_content" not in text


@pytest.mark.parametrize("field,value", [
    ("prompt", "changed task"), ("profile", "reviewer"),
    ("seeds", (("bounds.py", "changed"),)),
    ("expected_files", (("bounds.py", "changed"),)),
    ("test_command", "python --version"),
])
def test_task_fingerprint_changes_with_the_comparison_contract(field, value) -> None:
    assert replace(evaluation.CODE_CASE, **{field: value}).fingerprint != evaluation.CODE_CASE.fingerprint


def test_fixed_tasks_only_use_their_profiles_tools(observed) -> None:
    _, states = observed
    for case in evaluation.WORKFLOW_CASES:
        visible = {
            tool for capability in PROFILES[case.profile]
            for tool in CAPABILITY_TOOLS[capability]
        }
        assert {event.tool_name for event in states[case.case_id]["tool_events"]} <= visible


@pytest.mark.parametrize("status", ["running", "blocked", "failed", "waiting_approval"])
def test_noncompleted_state_cannot_pass(tmp_path, observed, status) -> None:
    _, states = observed
    case = evaluation.READ_CASE
    _seed(tmp_path, case.expected_files)
    state = {**states[case.case_id], "task_status": status}
    assert "图未到达 completed" in evaluation.validate_outcome(case, tmp_path, state)


@pytest.mark.parametrize("mutation", ["new_file", "changed_seed", "missing_seed"])
def test_readonly_workspace_mutation_is_rejected(tmp_path, observed, mutation) -> None:
    _, states = observed
    case = evaluation.READ_CASE
    _seed(tmp_path, case.expected_files)
    if mutation == "new_file":
        (tmp_path / "report.md").write_text("new", encoding="utf-8")
    elif mutation == "changed_seed":
        (tmp_path / "docs/cancel.md").write_text("changed", encoding="utf-8")
    else:
        (tmp_path / "docs/cancel.md").unlink()
    assert "工作区文件清单或内容不符合固定期望" in evaluation.validate_outcome(
        case, tmp_path, states[case.case_id],
    )


@pytest.mark.parametrize("answer", [
    "取消不撤销已发生的副作用。（docs/cancel.md:20）",
    "取消不撤销已发生的副作用。\ndocs/cancel.md:2",
    "取消不撤销已发生的副作用。（wrong.md:2）",
    "取消不撤销已发生的副作用。（other/docs/cancel.md:2）",
])
def test_wrong_or_unattached_citation_is_rejected(tmp_path, observed, answer) -> None:
    _, states = observed
    case = evaluation.READ_CASE
    _seed(tmp_path, case.expected_files)
    state = {**states[case.case_id], "final_response": answer}
    assert "缺少固定事实与对应引用：docs/cancel.md:2" in evaluation.validate_outcome(case, tmp_path, state)


@pytest.mark.parametrize("mutation", ["missing", "wrong_line", "old_run", "failed_event"])
def test_fact_in_answer_does_not_replace_real_read_evidence(tmp_path, observed, mutation) -> None:
    _, states = observed
    case = evaluation.READ_CASE
    _seed(tmp_path, case.expected_files)
    state = dict(states[case.case_id])
    if mutation in {"old_run", "failed_event"}:
        state["tool_events"] = [
            replace(event, **({"run_id": "old"} if mutation == "old_run" else {"status": "error"}))
            for event in state["tool_events"]
        ]
    elif mutation == "missing":
        state["messages"] = [message for message in state["messages"] if not isinstance(message, ToolMessage)]
    else:
        messages = []
        for message in state["messages"]:
            if isinstance(message, ToolMessage):
                payload = json.loads(message.content)
                payload.update(first_line=3, last_line=3)
                message = message.model_copy(update={"content": json.dumps(payload)})
            messages.append(message)
        state["messages"] = messages
    assert "缺少实际读取证据：docs/cancel.md:2" in evaluation.validate_outcome(case, tmp_path, state)


@pytest.mark.parametrize("mutation", ["missing", "failed", "timeout", "wrong_command", "zero_tests"])
def test_claimed_test_success_needs_the_actual_test_tool(tmp_path, observed, mutation) -> None:
    _, states = observed
    case = evaluation.CODE_CASE
    _seed(tmp_path, case.expected_files)
    state = dict(states[case.case_id])
    messages = []
    for message in state["messages"]:
        if isinstance(message, ToolMessage) and message.name == "run_shell":
            if mutation == "missing":
                continue
            payload = json.loads(message.content)
            payload.update({
                "failed": {"exit_code": 1}, "timeout": {"timed_out": True},
                "wrong_command": {"command": "python --version"},
                "zero_tests": {"stderr": "Ran 0 tests\nOK\n"},
            }[mutation])
            message = message.model_copy(update={"content": json.dumps(payload)})
        messages.append(message)
    messages.append(AIMessage(content='{"exit_code":0,"stderr":"Ran 3 tests\\nOK"}'))
    state["messages"] = messages
    assert "缺少三个 unittest 的真实成功执行证据" in evaluation.validate_outcome(case, tmp_path, state)


def test_modified_tests_are_rejected_even_if_shell_succeeds(tmp_path, observed) -> None:
    _, states = observed
    case = evaluation.CODE_CASE
    _seed(tmp_path, case.expected_files)
    (tmp_path / "test_bounds.py").write_text("# tests removed", encoding="utf-8")
    assert "工作区文件清单或内容不符合固定期望" in evaluation.validate_outcome(
        case, tmp_path, states[case.case_id],
    )


def test_duplicate_write_is_rejected(tmp_path, observed) -> None:
    _, states = observed
    case = evaluation.CODE_CASE
    _seed(tmp_path, case.expected_files)
    state = dict(states[case.case_id])
    write = next(event for event in state["tool_events"] if event.tool_name == "edit_file")
    state["tool_events"] = [*state["tool_events"], replace(write, tool_call_id="duplicate")]
    assert "修改副作用必须恰好执行一次" in evaluation.validate_outcome(case, tmp_path, state)


def test_readonly_tool_escalation_is_rejected(tmp_path, observed) -> None:
    _, states = observed
    case = evaluation.READ_CASE
    _seed(tmp_path, case.expected_files)
    state = dict(states[case.case_id])
    state["tool_events"] = [*state["tool_events"], ToolEvent(
        tool_call_id="bad", tool_name="write_file", run_id=state["run_id"], status="ok",
    )]
    assert "只读任务出现非只读工具" in evaluation.validate_outcome(case, tmp_path, state)


def test_runtime_exception_is_recorded_as_failure_without_raw_details(monkeypatch) -> None:
    class BrokenGraph:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("sensitive-original-error")

    monkeypatch.setattr(evaluation.builder, "build_chat_graph", lambda: BrokenGraph())
    report = evaluation.run_baseline()
    assert report["passed"] is False
    assert all("运行异常：RuntimeError" in row["failures"] for row in report["cases"])
    assert "sensitive-original-error" not in json.dumps(report)

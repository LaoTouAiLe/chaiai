"""T08 验收：机械证据校验、追加字段去重与副作用不重放。

全部使用构造出来的 State 与临时工作区，不联网、不调用真实模型。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.config import AppConfig
from local_agent.graph import builder, nodes
from local_agent.graph.state import (
    DecisionRecord,
    ErrorEvent,
    ToolEvent,
    decision_record_key,
    error_event_key,
    make_decision_record,
    merge_appended,
    tool_event_key,
    tool_result_key,
)
from local_agent.reflection.validators import summarize_trust, validate_tool_event

RUN_ID = "run-1"


def _event(status: str = "ok", tool_name: str = "run_shell") -> ToolEvent:
    return ToolEvent(
        tool_call_id="call-1",
        tool_name=tool_name,
        status=status,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        run_id=RUN_ID,
    )


def _result(status: str = "ok", preview: str = '{"ok": true}') -> Any:
    return {
        "tool_call_id": "call-1",
        "tool_name": "run_shell",
        "status": status,
        "truncated": False,
        "output_preview": preview,
        "output_digest": "digest",
        "run_id": RUN_ID,
    }


# --------------------------------------------------------------------------- 机械校验


def test_shell_non_zero_exit_is_not_verified() -> None:
    evidence = validate_tool_event(
        _event(),
        _result(preview='{"exit_code": 1, "stdout": "boom", "stderr": ""}'),
        payload={"exit_code": 1, "stdout": "boom", "stderr": ""},
    )

    assert evidence.exit_code_ok is False
    assert evidence.evidence_level == "failed"
    assert any("退出码" in reason for reason in evidence.reasons)


def test_shell_empty_output_is_uncertain() -> None:
    evidence = validate_tool_event(
        _event(),
        _result(preview='{"exit_code": 0, "stdout": "", "stderr": ""}'),
        payload={"exit_code": 0, "stdout": "", "stderr": ""},
    )

    assert evidence.exit_code_ok is True
    assert evidence.has_output is False
    assert evidence.evidence_level == "uncertain"


def test_shell_timeout_without_exit_code_is_failed() -> None:
    evidence = validate_tool_event(
        _event(),
        _result(preview='{"exit_code": null, "stdout": "partial", "stderr": ""}'),
        payload={"exit_code": None, "stdout": "partial", "stderr": ""},
    )

    assert evidence.evidence_level == "failed"


def test_shell_success_is_verified() -> None:
    evidence = validate_tool_event(
        _event(),
        _result(preview='{"exit_code": 0, "stdout": "Python 3.13", "stderr": ""}'),
        payload={"exit_code": 0, "stdout": "Python 3.13", "stderr": ""},
    )

    assert evidence.evidence_level == "verified"
    assert evidence.exit_code_ok is True


def test_edit_hash_unchanged_is_failed() -> None:
    event = _event(tool_name="edit_file")
    evidence = validate_tool_event(
        event,
        _result(preview='{"old_sha256": "aa", "new_sha256": "aa", "diff": ""}'),
        payload={"old_sha256": "aa", "new_sha256": "aa", "diff": ""},
    )

    assert evidence.file_hash_changed_as_expected is False
    assert evidence.evidence_level == "failed"


def test_edit_with_hash_and_diff_is_verified() -> None:
    evidence = validate_tool_event(
        _event(tool_name="edit_file"),
        _result(preview='{"old_sha256": "aa", "new_sha256": "bb", "diff": "-a\\n+b\\n"}'),
        payload={"old_sha256": "aa", "new_sha256": "bb", "diff": "-a\n+b\n"},
    )

    assert evidence.evidence_level == "verified"


def test_binary_read_is_uncertain() -> None:
    evidence = validate_tool_event(
        _event(tool_name="read_file"),
        _result(preview='{"is_binary": true, "content": ""}'),
        payload={"is_binary": True, "content": ""},
    )

    assert evidence.has_output is False
    assert evidence.evidence_level == "uncertain"


def test_denied_result_is_failed() -> None:
    evidence = validate_tool_event(_event(status="denied"), _result(status="denied"))

    assert evidence.status_ok is False
    assert evidence.evidence_level == "failed"


def test_summarize_trust_combinations() -> None:
    assert summarize_trust([]) == "uncertain"
    ok = validate_tool_event(
        _event(),
        _result(preview='{"exit_code": 0, "stdout": "x", "stderr": ""}'),
        payload={"exit_code": 0, "stdout": "x", "stderr": ""},
    )
    bad = validate_tool_event(_event(status="error"), _result(status="error"))

    assert summarize_trust([ok, ok]) == "verified"
    assert summarize_trust([bad, bad]) == "failed"
    assert summarize_trust([ok, bad]) == "uncertain"


# --------------------------------------------------------------------------- 去重


def _record(record_id: str) -> DecisionRecord:
    return DecisionRecord(
        record_id=record_id,
        stage="test",
        summary=record_id,
        tool_call_id=None,
        created_at=datetime.now(timezone.utc),
    )


def test_merge_appended_filters_existing_ids() -> None:
    first = _record("a")
    duplicate = _record("a")
    fresh = _record("b")

    merged = merge_appended([first], [duplicate, fresh], decision_record_key)

    assert [item["record_id"] for item in merged] == ["b"]


def test_merge_appended_is_used_for_every_appended_list() -> None:
    event = _event()
    duplicate_event = _event()
    assert merge_appended([event], [duplicate_event], tool_event_key) == []

    record = _result()
    assert merge_appended([record], [_result()], tool_result_key) == []

    error = ErrorEvent(
        event_id="e1", stage="test", message="m", tool_call_id=None, created_at=datetime.now(timezone.utc)
    )
    assert merge_appended([error], [error], error_event_key) == []


def test_decision_record_ids_are_deterministic_per_run() -> None:
    first = make_decision_record(RUN_ID, stage="safety_gate", summary="a", tool_call_id="c1")
    second = make_decision_record(RUN_ID, stage="safety_gate", summary="a", tool_call_id="c1")

    assert first["record_id"] == second["record_id"]


# --------------------------------------------------------------------------- 图级行为


def _context(tmp_path: Path, app_config: AppConfig, model: object) -> Any:
    return builder.build_runtime_context(
        app_config=app_config,
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=tmp_path,
        current_cwd=tmp_path,
    )


def test_reflection_keeps_evidence_from_every_step(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """多步骤任务：各步骤的有效证据都要保留，并按 source_event_id 去重。"""
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))
    read_event = ToolEvent(
        tool_call_id="call-read", tool_name="read_file", status="ok", run_id=RUN_ID
    )
    shell_event = ToolEvent(
        tool_call_id="call-shell", tool_name="run_shell", status="ok", run_id=RUN_ID
    )
    state: dict[str, Any] = {
        "run_id": RUN_ID,
        "loop_count": 1,
        "messages": [],
        "tool_events": [read_event, shell_event, read_event],
        "tool_results": [
            {
                "tool_call_id": "call-read",
                "tool_name": "read_file",
                "status": "ok",
                "truncated": False,
                "output_preview": '{"path": "a.py", "content": "x"}',
                "output_digest": "d1",
                "run_id": RUN_ID,
            },
            {
                "tool_call_id": "call-shell",
                "tool_name": "run_shell",
                "status": "ok",
                "truncated": False,
                "output_preview": '{"exit_code": 0, "stdout": "ok"}',
                "output_digest": "d2",
                "run_id": RUN_ID,
            },
        ],
    }

    update = nodes.reflection_node(state, _runtime(context, state))  # type: ignore[arg-type]

    evidence = update["final_evidence"]
    assert [item.source_event_id for item in evidence] == ["call-read", "call-shell"]
    assert [item.evidence_type for item in evidence] == ["file_hash", "exit_code"]
    assert all(item.verified for item in evidence)
    assert update["reflection_result"].result_trust == "verified"


def test_reflection_marks_failed_when_no_verified_evidence(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))
    state: dict[str, Any] = {
        "run_id": RUN_ID,
        "loop_count": 1,
        "messages": [],
        "tool_events": [
            ToolEvent(tool_call_id="c1", tool_name="run_shell", status="error", run_id=RUN_ID)
        ],
        "tool_results": [
            {
                "tool_call_id": "c1",
                "tool_name": "run_shell",
                "status": "error",
                "truncated": False,
                "output_preview": "工具执行失败",
                "output_digest": "d",
                "run_id": RUN_ID,
            }
        ],
    }

    update = nodes.reflection_node(state, _runtime(context, state))  # type: ignore[arg-type]

    assert update["reflection_result"].result_trust == "failed"
    assert update["final_evidence"][0].verified is False
    assert update["error_events"]


def test_completion_route_maps_every_next_action() -> None:
    from local_agent.graph import routers
    from local_agent.graph.state import ReflectionResult

    def state(action: str) -> dict[str, Any]:
        return {
            "reflection_result": ReflectionResult(
                goal_achieved=False, result_trust="uncertain", next_action=action  # type: ignore[arg-type]
            )
        }

    assert routers.completion_route(state("continue")) == routers.ROUTE_CONTINUE
    assert routers.completion_route(state("retry")) == routers.ROUTE_RETRY
    assert routers.completion_route(state("ask_user")) == routers.ROUTE_ASK_USER
    assert routers.completion_route(state("finish")) == routers.ROUTE_FINISH
    assert routers.completion_route({}) == routers.ROUTE_FINISH


def _crashed_state() -> dict[str, Any]:
    """构造“已放行、但结果未确认”的 State（模拟副作用后的异常结束）。"""
    return {
        "run_id": RUN_ID,
        "loop_count": 0,
        "messages": [],
        "pending_tool_calls": [
            {
                "name": "edit_file",
                "args": {"path": "note.py", "old_text": "value = 1", "new_text": "value = 2"},
                "id": "call-1",
            }
        ],
        "decision_trace": [
            make_decision_record(RUN_ID, stage="tool_start", summary="放行", tool_call_id="call-1")
        ],
    }


def test_unconfirmed_side_effect_is_detected() -> None:
    state = _crashed_state()

    assert nodes.unconfirmed_calls(state) == ["call-1"]
    # 标记来自上一次运行（run_id 不同）时不算未确认
    other_run = {**state, "run_id": "another-run"}
    assert nodes.unconfirmed_calls(other_run) == []
    # 有结果（且属于同一轮）之后就不再是未确认状态
    done = {**state, "tool_results": [{"tool_call_id": "call-1", "run_id": RUN_ID}]}
    assert nodes.unconfirmed_calls(done) == []


def test_unconfirmed_side_effect_is_not_replayed(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """不确定的副作用只能被提示核对，不能自动重放（架构 §4.2）。"""
    target = tmp_path / "note.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))
    state = _crashed_state()

    update = nodes.reflection_node(state, _runtime(context, state))  # type: ignore[arg-type]

    assert target.read_text(encoding="utf-8") == "value = 1\n"  # 没有被重放改动
    assert any("未确认的副作用" in item for item in update["reflection_result"].anomalies)
    assert update["error_events"]


def test_confirmed_call_is_not_executed_twice(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    target = tmp_path / "note.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))

    state: dict[str, Any] = {
        "run_id": RUN_ID,
        "loop_count": 0,
        "pending_tool_calls": [
            {
                "name": "edit_file",
                "args": {"path": "note.py", "old_text": "value = 1", "new_text": "value = 2"},
                "id": "call-1",
            }
        ],
        "tool_results": [
            {
                "tool_call_id": "call-1",
                "tool_name": "edit_file",
                "status": "ok",
                "truncated": False,
                "output_preview": "{}",
                "output_digest": "d",
                "run_id": RUN_ID,
            }
        ],
        "messages": [],
    }

    update = nodes.tool_execution_node(state, _runtime(context, state))  # type: ignore[arg-type]

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert not update.get("tool_results")
    assert any(
        "跳过重复执行" in record["summary"] for record in update.get("decision_trace", [])
    )


def _runtime(context: Any, state: dict[str, Any]) -> Any:
    """构造节点需要的 ``Runtime`` 包装（只暴露 ``context``）。"""

    class _Runtime:
        def __init__(self, ctx: Any) -> None:
            self.context = ctx

    del state
    return _Runtime(context)

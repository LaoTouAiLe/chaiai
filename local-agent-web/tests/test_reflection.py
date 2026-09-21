"""T08/T17 验收：机械证据校验、LLM 反思决策、追加字段去重与副作用不重放。

T08 覆盖机械校验与幂等；T17 覆盖 LLM 完成度判断、机械压制、结构化输出失败兜底、重试上限与
计划更新。全部使用构造出来的 State 与临时工作区，不联网、不调用真实模型。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from local_agent.config import AppConfig
from local_agent.graph import builder, nodes
from local_agent.graph.state import (
    DecisionRecord,
    ErrorEvent,
    PlanStep,
    ReflectionResult,
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


def _write_call(call_id: str) -> dict[str, Any]:
    """一个会改变机器状态的调用（``write_file`` 在工作区内且目标不存在 → 免审放行）。"""
    return {
        "name": "write_file",
        "args": {"path": "created.py", "content": "print(1)\n"},
        "id": call_id,
        "type": "tool_call",
    }


def _stale_effect_state(**overrides: Any) -> dict[str, Any]:
    """**更早运行**放行了 ``write_file``、却没有留下结果的 State（当前 run 是另一个）。"""
    state: dict[str, Any] = {
        "run_id": "current-run",
        "loop_count": 0,
        "messages": [],
        "pending_tool_calls": [_write_call("call-2")],
        "decision_trace": [
            make_decision_record(
                "old-run",
                stage="tool_start",
                summary="放行 write_file",
                tool_call_id="call-1",
                salt="write_file",
            )
        ],
    }
    state.update(overrides)
    return state


def test_stale_side_effect_is_not_replayed_before_execution(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """更早运行遗留的未确认副作用：**执行前**就拦住，不自动重放（架构 §4.2）。

    与 ``_crashed_state`` 的区别是放行标记来自**另一次运行**（``run_id`` 不同）——那才是"结果
    永远不会再出现"的情况。检查必须在 ``tool_execution_node`` 里：等反思发现时副作用已经又跑过一次。
    """
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))
    state = _stale_effect_state()

    update = nodes.tool_execution_node(state, _runtime(context, state))  # type: ignore[arg-type]

    assert not (tmp_path / "created.py").exists()  # 没有被重放执行
    assert update["tool_results"][0]["status"] == "denied"
    assert "没有执行" in update["messages"][0].content
    assert any("没有确认结果" in item["message"] for item in update["error_events"])
    # 遗留放行只提示一次：记下"已就它提示过人工核对"，否则之后每次运行都会拒绝执行副作用
    assert any(
        record["record_id"].split(":")[2] == nodes.SIDE_EFFECT_RECHECK_STAGE
        for record in update["decision_trace"]
    )


def test_stale_side_effect_is_only_blocked_once(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """提示过一次之后不再拦截：一次异常结束不该变成永久阻断（用户无从解除）。"""
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))
    state = _stale_effect_state()

    first = nodes.tool_execution_node(state, _runtime(context, state))  # type: ignore[arg-type]
    assert not (tmp_path / "created.py").exists()

    resumed = {
        **state,
        "decision_trace": [*state["decision_trace"], *first["decision_trace"]],
    }
    nodes.tool_execution_node(resumed, _runtime(context, resumed))  # type: ignore[arg-type]

    assert (tmp_path / "created.py").read_text(encoding="utf-8") == "print(1)\n"


def test_in_flight_release_is_not_blocked(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """**当前运行**的放行标记不算遗留：否则正常的每一次工具执行都会被自己挡住。"""
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))
    state = _stale_effect_state(
        decision_trace=[
            make_decision_record(
                "current-run",  # 与 state 的 run_id 相同：本轮刚刚放行、正要执行
                stage="tool_start",
                summary="放行 write_file",
                tool_call_id="call-2",
                salt="write_file",
            )
        ]
    )

    assert nodes.stale_side_effects(state) == []
    nodes.tool_execution_node(state, _runtime(context, state))  # type: ignore[arg-type]

    assert (tmp_path / "created.py").read_text(encoding="utf-8") == "print(1)\n"


def test_read_only_tools_are_never_blocked_as_stale(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """只读工具的重复调用没有副作用，不该被"不确定"挡住。"""
    (tmp_path / "note.py").write_text("value = 1\n", encoding="utf-8", newline="")
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))
    state = _stale_effect_state(
        pending_tool_calls=[
            {"name": "read_file", "args": {"path": "note.py"}, "id": "call-3", "type": "tool_call"}
        ],
        decision_trace=[
            make_decision_record(
                "old-run",
                stage="tool_start",
                summary="放行 read_file",
                tool_call_id="call-1",
                salt="read_file",
            )
        ],
    )

    assert nodes.stale_side_effects(state) == []
    update = nodes.tool_execution_node(state, _runtime(context, state))  # type: ignore[arg-type]

    assert update["tool_results"][0]["status"] == "ok"


def test_trust_follows_the_latest_attempt_of_each_tool(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """同一工具的失败被后续尝试修复后，整体可信度不再被历史失败永久拖住（复审实证）。

    ``tool_events`` 覆盖整个任务，"第二步失败、调整后成功"必须能判成完成，
    否则任务永远收不了尾。证据列表仍全量重算，历史失败照样留在里面供审计。
    """
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))
    state = _verified_state(
        loop_count=2,
        tool_events=[
            ToolEvent(tool_call_id="c1", tool_name="read_file", status="error", run_id=RUN_ID),
            ToolEvent(tool_call_id="c2", tool_name="read_file", status="ok", run_id=RUN_ID),
        ],
        tool_results=[
            {**_tool_result(status="error"), "tool_call_id": "c1"},
            {**_tool_result(), "tool_call_id": "c2"},
        ],
    )

    update = nodes.reflection_node(state, _runtime(context, state))  # type: ignore[arg-type]

    assert update["reflection_result"].result_trust == "verified"
    assert len(update["final_evidence"]) == 2  # 证据全量：成功与失败两条都在


def test_trust_still_fails_when_the_last_attempt_failed(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """反向：最后一次尝试仍是失败时整体可信度仍是 ``failed``——不是"成功过就算好"。"""
    context = _context(tmp_path, app_config, model_factory(decision="tool", reply="x"))
    state = _verified_state(
        loop_count=2,
        tool_events=[
            ToolEvent(tool_call_id="c1", tool_name="read_file", status="ok", run_id=RUN_ID),
            ToolEvent(tool_call_id="c2", tool_name="read_file", status="error", run_id=RUN_ID),
        ],
        tool_results=[
            {**_tool_result(), "tool_call_id": "c1"},
            {**_tool_result(status="error"), "tool_call_id": "c2"},
        ],
    )

    update = nodes.reflection_node(state, _runtime(context, state))  # type: ignore[arg-type]

    assert update["reflection_result"].result_trust == "failed"


def _runtime(context: Any, state: dict[str, Any]) -> Any:
    """构造节点需要的 ``Runtime`` 包装（只暴露 ``context``）。"""

    class _Runtime:
        def __init__(self, ctx: Any) -> None:
            self.context = ctx

    del state
    return _Runtime(context)


# --------------------------------------------------------------------------- T17 反思决策


def _tool_result(
    *, status: str = "ok", preview: str = '{"path": "a.py", "content": "x"}'
) -> dict[str, Any]:
    return {
        "tool_call_id": "c1",
        "tool_name": "read_file",
        "status": status,
        "truncated": False,
        "output_preview": preview,
        "output_digest": "d",
        "run_id": RUN_ID,
    }


def _verified_state(**overrides: Any) -> dict[str, Any]:
    """读文件成功、机械校验为 ``verified`` 的 State。"""
    state: dict[str, Any] = {
        "run_id": RUN_ID,
        "loop_count": 1,
        "messages": [],
        "task_goal": "读取配置文件",
        "tool_events": [
            ToolEvent(tool_call_id="c1", tool_name="read_file", status="ok", run_id=RUN_ID)
        ],
        "tool_results": [_tool_result()],
    }
    state.update(overrides)
    return state


def _failed_state(**overrides: Any) -> dict[str, Any]:
    """工具被拒（机械校验为 ``failed``）的 State。"""
    state: dict[str, Any] = {
        "run_id": RUN_ID,
        "loop_count": 1,
        "messages": [],
        "task_goal": "读取工作区外的文件",
        "tool_events": [
            ToolEvent(tool_call_id="c1", tool_name="read_file", status="error", run_id=RUN_ID)
        ],
        "tool_results": [_tool_result(status="denied", preview="已拒绝执行 read_file")],
    }
    state.update(overrides)
    return state


def test_reflection_follows_llm_decision_when_evidence_is_verified(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    model = model_factory(
        decision="tool", reflection={"goal_achieved": True, "next_action": "finish"}
    )
    state = _verified_state(
        plan=[PlanStep(id="s1", description="读取配置文件", status="running")]
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    result = update["reflection_result"]
    assert result.result_trust == "verified"
    assert result.goal_achieved is True
    assert result.next_action == "finish"
    assert [step.status for step in update["plan"]] == ["completed"]
    assert not update.get("error_events")


def test_reflection_suppresses_completion_without_verified_evidence(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """架构 §5.4：证据不足时，模型自称"完成"也不能判完成。"""
    model = model_factory(
        decision="tool", reflection={"goal_achieved": True, "next_action": "finish"}
    )
    state = _failed_state()

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    result = update["reflection_result"]
    assert result.result_trust == "failed"
    assert result.goal_achieved is False
    assert result.next_action == "ask_user"
    assert any("不能判定完成" in record["summary"] for record in update["decision_trace"])


def test_reflection_normalizes_finish_when_goal_not_achieved(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """未达成目标时不允许用 ``finish`` 收尾，统一改为向用户说明。"""
    model = model_factory(
        decision="tool", reflection={"goal_achieved": False, "next_action": "finish"}
    )
    state = _verified_state()

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert update["reflection_result"].goal_achieved is False
    assert update["reflection_result"].next_action == "ask_user"
    assert any("不能用 finish 收尾" in record["summary"] for record in update["decision_trace"])


def test_reflection_falls_back_to_mechanical_conclusion_on_parse_failure(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """结构化输出全部失败：``verified`` → finish，否则 ask_user，并且必须记错误事件。"""
    verified_state = _verified_state()
    verified_model = model_factory(decision="tool", fail_reflection=True)
    verified_update = nodes.reflection_node(
        verified_state, _runtime(_context(tmp_path, app_config, verified_model), verified_state)
    )

    assert verified_update["reflection_result"].goal_achieved is True
    assert verified_update["reflection_result"].next_action == "finish"
    assert any(
        "结构化输出失败" in event["message"] for event in verified_update["error_events"]
    )

    failed_state = _failed_state()
    failed_model = model_factory(decision="tool", fail_reflection=True)
    failed_update = nodes.reflection_node(
        failed_state, _runtime(_context(tmp_path, app_config, failed_model), failed_state)
    )

    assert failed_update["reflection_result"].goal_achieved is False
    assert failed_update["reflection_result"].next_action == "ask_user"


def test_reflection_stops_retrying_after_the_configured_limit(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    model = model_factory(
        decision="tool", reflection={"goal_achieved": False, "next_action": "retry"}
    )
    limit = app_config.agent.max_retries_per_step
    state = _verified_state(retry_count=limit)

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert update["reflection_result"].next_action == "ask_user"
    assert update["retry_count"] == limit  # 不再增加
    assert any("重试已达上限" in record["summary"] for record in update["decision_trace"])


def test_planner_creates_single_step_plan_and_keeps_existing_one(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    context = _context(tmp_path, app_config, model_factory())
    planned = nodes.planner_node(  # type: ignore[arg-type]
        {"run_id": RUN_ID, "normalized_request": "读取配置", "task_goal": "读取配置"},
        _runtime(context, {}),
    )

    assert len(planned["plan"]) == 1
    assert planned["plan"][0].description == "读取配置"
    assert planned["plan"][0].status == "running"

    again = nodes.planner_node(  # type: ignore[arg-type]
        {"run_id": RUN_ID, "task_goal": "读取配置", "plan": planned["plan"]},
        _runtime(context, {}),
    )

    assert again["plan"] == planned["plan"]


def test_reflection_keeps_every_error_of_one_round(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """同一轮的机械失败与解析失败必须各留一条事件。

    复审实测：逐次 ``merge_appended`` 覆盖 ``error_events`` 时，"机械失败 + 解析失败"最终得到
    0 条（两类事件在同 stage 下 ID 相同，后写的把先写的挤掉）。
    """
    model = model_factory(decision="tool", fail_reflection=True)
    state = _failed_state()

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    stages = [event["stage"] for event in update["error_events"]]
    assert stages == [f"{nodes.REFLECTION_NODE}:mechanical", f"{nodes.REFLECTION_NODE}:llm"]


def test_ask_user_branch_produces_a_visible_question(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """``ask_user`` 必须留下可展示的追问，否则 final_response_node 只能给出 None（复审实测）。"""
    model = model_factory(
        decision="tool",
        reflection={
            "goal_achieved": False,
            "next_action": "ask_user",
            "ask_user_reason": "请确认要修改哪个文件",
        },
    )
    state = _verified_state()

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert update["reflection_result"].next_action == "ask_user"
    texts = [message.text for message in update["messages"]]
    assert any("请确认要修改哪个文件" in text for text in texts)


def test_finish_branch_synthesizes_a_final_answer(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """``finish`` 走一次**不绑定工具**的合成调用，并把结果作为可展示回答。"""
    model = model_factory(
        decision="tool",
        reply="依据工具结果：a.py 的内容是 x",
        reflection={"goal_achieved": True, "next_action": "finish"},
    )
    state = _verified_state()

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert [message.text for message in update["messages"]] == ["依据工具结果：a.py 的内容是 x"]
    # 只有合成这一次决策类调用：finish 不会回到 agent_decision_node 再要一轮工具决策
    assert model.decision_calls == 1


def test_plan_and_adjustment_are_injected_into_the_prompt(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """重规划必须真的影响下一轮：计划与反思写入的调整要出现在系统提示里。"""
    from local_agent.context.prompt_context import build_system_prompt

    context = _context(tmp_path, app_config, model_factory())
    prompt = build_system_prompt(
        mode="tool",
        context=context,
        task_goal="修复登录失败",
        plan=[
            PlanStep(
                id="s1",
                description="定位问题",
                status="running",
                result_summary="改为先看日志",
            )
        ],
    )

    assert "## 当前计划" in prompt.system_prompt
    assert "定位问题" in prompt.system_prompt
    assert "改为先看日志" in prompt.system_prompt
    assert "修复登录失败" in prompt.system_prompt


def test_unfinished_task_does_not_claim_completed() -> None:
    """未达成目标就结束（例如要用户补充）时不能冒充 completed（架构 §5.4）。"""
    state: dict[str, Any] = {
        "messages": [AIMessage(content="请确认目标是不是这个")],
        "task_status": "running",
        "reflection_result": ReflectionResult(
            goal_achieved=False, result_trust="uncertain", next_action="ask_user"
        ),
    }

    update = nodes.final_response_node(state)  # type: ignore[arg-type]

    assert update["task_status"] == "running"
    assert update["final_response"] == "请确认目标是不是这个"

    finished = {
        **state,
        "reflection_result": ReflectionResult(
            goal_achieved=True, result_trust="verified", next_action="finish"
        ),
    }
    assert nodes.final_response_node(finished)["task_status"] == "completed"  # type: ignore[arg-type]

    # 闲聊/模型直接回答（没有反思结论）仍然算完成
    chat_state: dict[str, Any] = {"messages": [AIMessage(content="你好")], "task_status": "running"}
    assert nodes.final_response_node(chat_state)["task_status"] == "completed"  # type: ignore[arg-type]

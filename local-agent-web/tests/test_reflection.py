"""T08/T17 验收：机械证据校验、LLM 反思决策、追加字段去重与副作用不重放。

T08 覆盖机械校验与幂等；T17 覆盖 LLM 完成度判断、机械压制、结构化输出失败兜底、重试上限与
计划更新。全部使用构造出来的 State 与临时工作区，不联网、不调用真实模型。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

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


#: 反思提示的固定标记（``reflector.REFLECTION_INSTRUCTION`` 的开头），用来判断"这一步到底调没调模型"。
_REFLECTION_MARKER = "你在为本机 Agent 做**任务反思**"


def test_reflection_skips_the_model_call_on_a_middle_step(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """多步计划的**中间步骤**：机械结论已定，不必花这次模型调用（2026-09-30 成本，§5.4）。

    为什么这一支可以跳过：这里有后续未完成步骤，``_advance_plan`` 必然把 ``finish`` 压回
    ``continue``，所以**调用模型也改变不了** ``next_action``；剩下的只有"重规划"，而重规划真正需要
    模型的时候（计划不适用、工具反复失败）会先产生异常，那时立刻恢复调用。

    这条测试同时钉住两件事：模型**确实没被调用**，以及**跳过必须在轨迹里可见**——否则"没调模型"和
    "调了但没记录"长得一样。
    """
    model = model_factory(decision="tool", reply="第一步读完了")
    state = _verified_state(
        plan=[
            PlanStep(id="s1", description="读取配置", status="running"),
            PlanStep(id="s2", description="汇总说明", status="pending"),
        ]
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert not any(_REFLECTION_MARKER in prompt for prompt in model.structured_prompts)
    # 机械结论照常推进计划（这一步做完、下一步开始），并要求继续。
    assert [step.status for step in update["plan"]] == ["completed", "running"]
    assert update["reflection_result"].next_action == "continue"
    assert any(
        nodes.REFLECTION_SKIPPED_NOTE in record["summary"] for record in update["decision_trace"]
    )


def test_reflection_still_calls_the_model_on_the_last_step(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """**最后一步**不跳过：那一支会让整个任务收尾，完成度判断必须由模型给出。

    第一版漏了这条约束，把 11 个用例一次打红（含固定集里的验收基线）——因为机械层判不了"任务目标
    真的达成了吗"，而在最后一步判错就等于**替用户结束了任务**。
    """
    model = model_factory(
        decision="tool", reflection={"goal_achieved": True, "next_action": "finish"}
    )
    state = _verified_state(
        plan=[PlanStep(id="s1", description="读取配置", status="running")]
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert any(_REFLECTION_MARKER in prompt for prompt in model.structured_prompts)
    assert update["reflection_result"].goal_achieved is True


def test_reflection_still_calls_the_model_when_evidence_is_not_verified(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """证据没过校验时不跳过：那时"要不要重试 / 要不要问用户"必须由模型判断。"""
    model = model_factory(
        decision="tool", reflection={"goal_achieved": False, "next_action": "ask_user"}
    )
    state = _failed_state(
        plan=[
            PlanStep(id="s1", description="读取配置", status="running"),
            PlanStep(id="s2", description="汇总说明", status="pending"),
        ]
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert any(_REFLECTION_MARKER in prompt for prompt in model.structured_prompts)
    assert update["reflection_result"].result_trust == "failed"


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
    """重规划必须真的影响下一轮：计划与反思写入的调整要出现在提示里（T35：已移入运行上下文快照）。"""
    from local_agent.context.prompt_context import build_context_snapshot_text

    context = _context(tmp_path, app_config, model_factory())
    prompt = build_context_snapshot_text(
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

    assert "## 当前计划" in prompt
    assert "定位问题" in prompt
    assert "改为先看日志" in prompt
    assert "修复登录失败" in prompt


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

    # 本轮已结束、在等用户补充：既不是 completed，也不是"还在跑"（复审实证，2026-09-28）。
    assert update["task_status"] == "blocked"
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


# --------------------------------------------------------------------------- 证据渲染（§5.4）


def test_reflection_prompt_includes_the_models_last_answer(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """反思提示必须带上"模型本轮答过什么"。

    实测缺陷的另一半：模型已经按工具结果答完并给出结论，反思却看不到这段回答，于是继续要求
    执行、最后变成向用户追问——用户看到一句与前面结论矛盾的追问。
    """
    model = model_factory(
        decision="tool", reflection={"goal_achieved": True, "next_action": "finish"}
    )
    state = _verified_state(
        plan=[PlanStep(id="s1", description="读取配置文件", status="running")],
        messages=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "a.py"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content='{"path": "a.py", "content": "x = 1"}', tool_call_id="c1", name="read_file"),
            AIMessage(content="结论：a.py 里 x = 1。"),
        ],
    )

    nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    prompt = next(text for text in model.structured_prompts if "任务反思" in text)
    assert "【本轮模型回答】结论：a.py 里 x = 1。" in prompt
    # 指令里必须写明它不能单独作为完成依据（否则反思会变成"模型说完成就完成"）
    assert "不能单独作为完成依据" in prompt


def test_reflection_facts_mark_a_missing_answer_and_cap_a_long_one() -> None:
    """没有回答时说明清楚；回答过长时有界并标注（不把长回答整段发出去）。"""
    from local_agent.reflection import reflector

    facts = reflector.build_reflection_facts(task_goal="目标", trust="verified")
    assert "【本轮模型回答】（本轮模型还没有给出回答）" in facts

    long_answer = "很长的回答" * 400
    capped = reflector.build_reflection_facts(
        task_goal="目标", trust="verified", last_answer=long_answer
    )
    assert "回答过长，此处只展示前" in capped
    assert len(capped) < len(long_answer)


def test_reflection_messages_extend_the_main_prefix() -> None:
    """反思消息序列 = **主对话前缀原样** + 反思指令 + 事实；不传前缀时退化为原来的两条。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from local_agent.reflection import reflector

    prefix = [SystemMessage(content="稳定系统前缀"), HumanMessage(content="历史消息")]
    messages = reflector.build_reflection_messages(
        prefix=prefix, task_goal="读取配置", trust="verified"
    )

    assert messages[: len(prefix)] == prefix  # 前缀逐字节原样，一个字符都不改
    assert isinstance(messages[len(prefix)], SystemMessage)
    assert "任务反思" in str(messages[len(prefix)].content)
    assert isinstance(messages[-1], HumanMessage)

    bare = reflector.build_reflection_messages(task_goal="读取配置", trust="verified")
    assert len(bare) == 2  # 老调用方（无前缀）行为不变


class _PayloadRecorder:
    """只记录"这次请求实际发出去的消息序列"，并按反思结构回一个合法 JSON。

    为什么不用 ``model_factory``：反思走的是**结构化输出降级链**，替身模型对反思提示是按
    ``with_structured_output`` 那条路分派的；这里要看的恰恰是**降级路径**（Prompt JSON）下
    消息的**顺序**，所以直接记录 payload 最直接。
    """

    def __init__(self) -> None:
        self.payloads: list[list[Any]] = []

    def invoke(self, messages: Any) -> AIMessage:
        self.payloads.append(list(messages))
        return AIMessage(content='{"goal_achieved": false, "next_action": "continue"}')


def test_reflection_request_puts_the_json_envelope_at_the_tail() -> None:
    """反思请求必须**以主对话前缀开头**、且 JSON 外层指令在**末尾**（2026-09-30，缓存）。

    这是"反思能不能吃到前缀缓存"的完整判据：供应商从**第一个 token** 开始比对最长公共前缀，所以
    两件事都要成立——① 前缀那段逐字节在最前面；② 约束 JSON 的那条指令挪到末尾（放头部会把前缀
    整体往后挤一格，等于没接）。改前实测：65 次反思只命中 **14.8%**，却占整轮未命中输入的 37%。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from local_agent.reflection import reflector

    prefix = [SystemMessage(content="稳定系统前缀"), HumanMessage(content="历史消息")]
    recorder = _PayloadRecorder()

    reflector.reflect(recorder, prefix=prefix, task_goal="读取配置", trust="verified")

    payload = recorder.payloads[-1]
    assert [str(message.content) for message in payload[:2]] == ["稳定系统前缀", "历史消息"]  # 前缀原样在最前
    assert "任务反思" in str(payload[2].content)  # 反思指令
    assert "只输出一个 JSON 对象" in str(payload[-1].content)  # JSON 外层指令在末尾

    # 对照：不传前缀时保持原有顺序（指令在前），不为一条缓存优化去改与本项目缓存无关的调用形状。
    recorder = _PayloadRecorder()
    reflector.reflect(recorder, task_goal="读取配置", trust="verified")
    without_prefix = recorder.payloads[-1]
    assert "只输出一个 JSON 对象" in str(without_prefix[0].content)


def test_reflection_prompt_shows_content_not_only_metadata(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """实测回归：反思提示里的工具证据必须能看到正文片段，并明确标注"这只是摘要"。

    修复前只截前 160 个字符，而读文件结果是 JSON（元数据在前、正文在后），模型看到的恰好只有
    ``path`` / ``encoding`` / ``sha256`` / ``line_count``——于是判断"正文被截断、任务没完成"，
    连续多轮空转，最后反过来向用户索取**已经读到**的内容。
    """
    model = model_factory(
        decision="tool", reflection={"goal_achieved": True, "next_action": "finish"}
    )
    #: 行数刻意让正文**超过** ``TOOL_EVIDENCE_CHARS``（4000）：这条用例要同时守住"正文可见"与
    #: "截断必须标注"，而 79 行的 payload 在配额提高后已经整段放得下，只会走整段渲染那条分支
    #: （2026-09-30 提到 4000 时暴露）。
    payload = json.dumps(
        {
            "path": "a.py",
            "encoding": "utf-8",
            "sha256": "z" * 64,
            "line_count": 400,
            "truncated": False,
            "is_binary": False,
            "content": "\n".join(
                f"第 {index} 行：def decide_mode(): ..." for index in range(1, 401)
            ),
        },
        ensure_ascii=False,
    )
    state = _verified_state(
        plan=[PlanStep(id="s1", description="读取配置文件", status="running")],
        messages=[
            HumanMessage(content="读 a.py"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "a.py"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content=payload, tool_call_id="c1", name="read_file"),
        ],
    )

    nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    prompt = next(text for text in model.structured_prompts if "任务反思" in text)
    assert "path=a.py" in prompt  # 标识字段仍完整
    assert "content=第 1 行" in prompt  # 正文片段进了提示
    assert "原文片段" in prompt  # 片段截断了就说清楚，别让模型以为工具没返回内容


# --------------------------------------------------------------------------- 空转保护（§5.3）


def _two_step_state(**overrides: Any) -> dict[str, Any]:
    """一个还有一步没做完的任务：第一步已完成、第二步正在执行。"""
    state = _verified_state(
        plan=[
            PlanStep(id="s1", description="读取配置文件", status="completed"),
            PlanStep(id="s2", description="总结内容", status="running"),
        ],
        current_step_index=1,
        **overrides,
    )
    return state


def test_stall_guard_stops_rounds_without_any_action(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """空转保护：反思想继续，但模型连续两轮只给文字、没有任何可执行动作 → 停下来交给用户。

    实测：真实模型读完文件后连续 10 轮输出"任务已完成 / 无新指令"，把 ``max_tool_loops`` 烧完
    才被上限拦下，用户最终看到的是一句自相矛盾的追问。
    """
    model = model_factory(
        decision="tool", reflection={"goal_achieved": False, "next_action": "continue"}
    )
    state = _two_step_state(
        messages=[AIMessage(content="已完成"), AIMessage(content="无新指令，任务已完成")]
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    result = update["reflection_result"]
    assert result.next_action == "ask_user"  # 不是 finish：计划还有步骤没走完
    assert result.goal_achieved is False
    assert [step.status for step in update["plan"]] == ["completed", "blocked"]
    question = update["messages"][0].text
    assert "连续 2 轮没有产生可执行的动作" in question
    assert "请确认下一步要做什么" in question
    assert any("空转保护" in record["summary"] for record in update["decision_trace"])


def test_plan_remaining_steps_do_not_override_a_delivered_answer(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """模型一次做完整个任务并给出结论时，计划记账不得把它改写成"继续执行"（2026-09-30 修）。

    ``goal_achieved`` 问的是"**当前任务目标**是否真的达成"，却被 ``_advance_plan`` 当成"当前步骤
    完成了"来驱动计划推进。两种含义压在一个字段上，于是：模型一次性做完、写出完整答案，反思如实
    回报"目标已达成"，却因计划还剩几步被改成 ``continue`` → 下一轮无事可做 → 空转 → 用户最终拿到
    的是"请确认下一步要做什么"。实测被精度层用例 ``locate_all`` 抓到。
    """
    model = model_factory(
        decision="tool", reflection={"goal_achieved": True, "next_action": "finish"}
    )
    state = _verified_state(
        plan=[
            PlanStep(id="s1", description="读取配置", status="running"),
            PlanStep(id="s2", description="汇总上限", status="pending"),
            PlanStep(id="s3", description="给出结论", status="pending"),
        ],
        current_step_index=0,
        messages=[
            AIMessage(content="我先确认配置里的实际取值。"),
            AIMessage(
                content=(
                    "结论：Prompt 窗口预算在 config.yaml 第 75-77 行，工具步预算 120000 字符、"
                    "条数下限 3 步。依据是对工作区做正则检索后逐文件确认，窗口条数上限 100。"
                )
            ),
        ],
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    result = update["reflection_result"]
    assert result.goal_achieved is True
    assert result.next_action == "finish"  # 不再被"计划还有步骤"改写成 continue
    # 剩余步骤一次性作废：标成 skipped，而不是留在那里当"仍在执行"
    assert [step.status for step in update["plan"]] == ["completed", "skipped", "skipped"]


def test_plan_still_continues_when_no_answer_was_delivered(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """没交付结论时，"完成一步 ≠ 完成整个任务"的硬语义**必须保持**（别把上面那条修过头）。"""
    model = model_factory(
        decision="tool", reflection={"goal_achieved": True, "next_action": "finish"}
    )
    state = _verified_state(
        plan=[
            PlanStep(id="s1", description="读取配置", status="running"),
            PlanStep(id="s2", description="汇总上限", status="pending"),
        ],
        current_step_index=0,
        messages=[AIMessage(content="已完成")],
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    result = update["reflection_result"]
    assert result.next_action == "continue"
    assert result.goal_achieved is False
    assert [step.status for step in update["plan"]] == ["completed", "running"]


def test_stall_guard_delivers_the_answer_that_was_already_written(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """空转但**结论已经写出来了** → 交付它，而不是把决策推回用户（2026-09-30 修）。

    修复前的形态（被精度层用例 ``locate_all`` 实测抓到）：模型在前几轮已经给出完整、带行号的
    答案，只因计划记账还差几步，空转保护最终交付给用户的却是"请确认下一步要做什么"——结论明明
    已经在会话里，用户却拿不到它。

    与上一条用例的分界就是"有没有可交付的结论"：上一例的模型只回了"已完成 / 无新指令"（没有内容），
    所以仍然回到 ``ask_user``。
    """
    model = model_factory(
        decision="tool", reflection={"goal_achieved": False, "next_action": "continue"}
    )
    answer = (
        "结论：MAX_BETA 的值是 42，定义在 pkg/beta.py 第 5 行；MAX_ALPHA 的值是 7，"
        "定义在 pkg/alpha.py 第 3 行。依据是先对工作区做正则检索、再逐文件确认："
        "pkg/beta.py 全文 9 行，第 5 行是赋值定义，第 9 行是引用。"
    )
    state = _two_step_state(
        messages=[AIMessage(content="我先确认工作区里的实际情况。"), AIMessage(content=answer)]
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    result = update["reflection_result"]
    assert result.next_action == "finish"  # 交付，而不是 ask_user
    assert result.goal_achieved is True  # 机械证据通过校验才允许声称完成（§5.4）
    # 交付分支不把当前步骤标 blocked：它没有被"交给用户"
    assert [step.status for step in update["plan"]] == ["completed", "running"]
    assert any("直接交付" in record["summary"] for record in update["decision_trace"])


def test_stall_guard_does_not_deliver_a_bare_platitude(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """只有一句客套话时**不许**交付：短于门槛的回答没有可交付的内容，仍应回到 ``ask_user``。"""
    model = model_factory(
        decision="tool", reflection={"goal_achieved": False, "next_action": "continue"}
    )
    state = _two_step_state(
        messages=[AIMessage(content="好的"), AIMessage(content="任务已完成，无新指令")]
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    result = update["reflection_result"]
    assert result.next_action == "ask_user"
    assert [step.status for step in update["plan"]] == ["completed", "blocked"]


def test_one_text_round_is_not_treated_as_stalling(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """只给一轮文字不算空转：模型先说一句、下一轮再动手是正常流程。"""
    model = model_factory(
        decision="tool", reflection={"goal_achieved": False, "next_action": "continue"}
    )
    state = _two_step_state(messages=[AIMessage(content="我先说明一下思路")])

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert update["reflection_result"].next_action == "continue"
    assert [step.status for step in update["plan"]] == ["completed", "running"]


def test_a_tool_round_resets_the_stall_counter(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """刚执行过工具的那一轮不算空转——计数只看尾部没有动作的连续轮次。"""
    model = model_factory(
        decision="tool", reflection={"goal_achieved": False, "next_action": "continue"}
    )
    state = _two_step_state(
        messages=[
            AIMessage(content="已完成"),
            AIMessage(content="再确认一次"),
            ToolMessage(content='{"path": "a.py", "content": "x"}', tool_call_id="c1", name="read_file"),
        ]
    )

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert update["reflection_result"].next_action == "continue"


def test_stall_guard_ignores_finish_and_ask_user(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """空转保护只拦"还想继续"：正常收尾与正常追问不受影响。"""
    model = model_factory(
        decision="tool", reflection={"goal_achieved": True, "next_action": "finish"}
    )
    state = _two_step_state(messages=[AIMessage(content="已完成"), AIMessage(content="完成了")])

    update = nodes.reflection_node(state, _runtime(_context(tmp_path, app_config, model), state))

    assert update["reflection_result"].next_action == "finish"
    assert update["reflection_result"].goal_achieved is True

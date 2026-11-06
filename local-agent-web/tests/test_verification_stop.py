"""改完代码没验证就收尾时的提醒（2026-11-04）。

照 hermes 的 verify-on-stop 做的**提醒**，不是拦截。这几条必须一直成立，否则就退化成
T43 第一版那种"任务做完了却判 blocked"的形态（见 ``docs/archive/v0.4-implementation-history.md``）：

- 只提醒，不判失败、不换掉回答；
- 次数用完就不再提；
- 只对代码文件触发；
- 已经有退出码为 0 的验证就不再提；
- 撞到循环上限时不提，只剩最后一格额度时也不提（否则正常收尾会被 ``completion_route`` 改成 failed）。

全部用构造出来的 State，不联网、不调用模型、不碰真实工作区。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage

from local_agent.graph.main_loop import checkpoint_main_loop
from local_agent.graph.state import ToolEvent
from local_agent.graph.verification_stop import (
    build_verification_nudge,
    changed_code_paths,
    is_non_code_path,
)

RUN_ID = "run-verify"


def _edit(path: str, *, status: str = "ok", call_id: str = "c-edit", run_id: str = RUN_ID) -> ToolEvent:
    return ToolEvent(
        tool_call_id=call_id,
        tool_name="edit_file",
        status=status,
        redacted_arguments={"path": path},
        run_id=run_id,
    )


def _shell(call_id: str = "c-shell", run_id: str = RUN_ID) -> ToolEvent:
    return ToolEvent(tool_call_id=call_id, tool_name="run_shell", status="ok", run_id=run_id)


def _tool_message(call_id: str, payload: dict[str, Any]) -> ToolMessage:
    return ToolMessage(content=json.dumps(payload), tool_call_id=call_id, name="run_shell")


def _state(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "run_id": RUN_ID,
        "model_next_action": "finish",
        "model_final_response": "做完了",
        "tool_events": [],
        "messages": [],
        "verification_nudge": None,
        "verification_nudge_count": 0,
        "pending_verification_response": None,
        "max_loop_count": None,
        "loop_count": 0,
    }
    base.update(overrides)
    return base


def _checkpoint(state: Any, *, verify_on_stop: bool = True) -> Any:
    return checkpoint_main_loop(
        state, trust="uncertain", anomalies=[], evidence=[],
        pending_uncertain=[], max_retries=2, verify_on_stop=verify_on_stop,
    )


# --------------------------------------------------------------------------- 触发条件


def test_edit_without_verification_triggers_nudge() -> None:
    state = _state(tool_events=[_edit("src/local_agent/a.py")])

    assert build_verification_nudge(state) is not None


def test_document_only_edit_does_not_trigger() -> None:
    state = _state(tool_events=[_edit("README.md"), _edit("docs/plan.txt", call_id="c2")])

    assert changed_code_paths(state) == []
    assert build_verification_nudge(state) is None


def test_successful_command_silences_nudge() -> None:
    state = _state(
        tool_events=[_edit("src/a.py"), _shell()],
        messages=[_tool_message("c-shell", {"exit_code": 0})],
    )

    assert build_verification_nudge(state) is None


def test_failed_command_keeps_nudge() -> None:
    """跑过命令不等于验证过：退出码非 0 时提醒照旧。"""
    state = _state(
        tool_events=[_edit("src/a.py"), _shell()],
        messages=[_tool_message("c-shell", {"exit_code": 1})],
    )

    assert build_verification_nudge(state) is not None


def test_nudge_stops_after_limit() -> None:
    state = _state(tool_events=[_edit("src/a.py")], verification_nudge_count=2)

    assert build_verification_nudge(state) is None


def test_previous_run_edits_are_ignored() -> None:
    state = _state(tool_events=[_edit("src/a.py", run_id="run-old")])

    assert build_verification_nudge(state) is None


def test_failed_edit_is_not_counted() -> None:
    state = _state(tool_events=[_edit("src/a.py", status="error")])

    assert build_verification_nudge(state) is None


def test_is_non_code_path() -> None:
    assert is_non_code_path("README.md")
    assert is_non_code_path(r"docs\notes.txt")
    assert is_non_code_path("LICENSE")
    assert not is_non_code_path("src/local_agent/graph/main_loop.py")
    assert not is_non_code_path("config.yaml")


# --------------------------------------------------------------------------- 收尾行为


def test_checkpoint_keeps_running_and_keeps_candidate_answer() -> None:
    result = _checkpoint(_state(tool_events=[_edit("src/a.py")]))

    assert result["task_status"] == "running"
    assert result["reflection_result"].next_action == "continue"
    assert result["reflection_result"].goal_achieved is False
    assert result["verification_nudge"]
    assert result["verification_nudge_count"] == 1
    # 候选回答必须留着——提醒失败时用户不能什么也拿不到。
    assert result["pending_verification_response"] == "做完了"
    assert "final_response" not in result


def test_checkpoint_completes_when_switch_is_off() -> None:
    """默认关闭时行为与改动前逐字一致。"""
    result = _checkpoint(_state(tool_events=[_edit("src/a.py")]), verify_on_stop=False)

    assert result["task_status"] == "completed"
    assert result["reflection_result"].next_action == "finish"
    assert result["final_response"] == "做完了"
    assert result["verification_nudge"] is None


def test_nudge_is_cleared_on_the_following_checkpoint() -> None:
    """提醒只对下一次决策生效：模型照做后那条提示必须消失，不能一直挂在系统提示里。"""
    stale = _state(
        tool_events=[_edit("src/a.py"), _shell()],
        messages=[_tool_message("c-shell", {"exit_code": 0})],
        verification_nudge="上一轮的提醒",
        verification_nudge_count=1,
    )

    result = _checkpoint(stale)

    assert result["task_status"] == "completed"
    assert result["verification_nudge"] is None


def test_checkpoint_completes_when_loop_budget_is_spent() -> None:
    """撞到循环上限时不许提醒：那一刻改 continue 会被完成路由改判 failed。"""
    result = _checkpoint(
        _state(tool_events=[_edit("src/a.py")], max_loop_count=3, loop_count=3)
    )

    assert result["task_status"] == "completed"
    assert result["reflection_result"].next_action == "finish"


def test_no_nudge_on_the_last_decision_slot() -> None:
    """只剩最后一格额度时不许提醒（2026-11-06）。

    提醒会把本轮改回"继续"，正好用掉那最后一格；下一轮撞上限，``completion_route`` 把原本
    正常的收尾判成 failed。所以边界要往前挪一格：``loop_count == max - 1`` 就当成没额度。
    """
    result = _checkpoint(
        _state(tool_events=[_edit("src/a.py")], max_loop_count=3, loop_count=2)
    )

    assert result["task_status"] == "completed"
    assert result["reflection_result"].next_action == "finish"
    assert result["verification_nudge"] is None


def test_nudge_still_fires_while_a_spare_slot_remains() -> None:
    """额度还够再走一轮时提醒照旧：这条边界不能顺手把功能关掉。"""
    result = _checkpoint(
        _state(tool_events=[_edit("src/a.py")], max_loop_count=3, loop_count=1)
    )

    assert result["task_status"] == "running"
    assert result["verification_nudge"]


def test_checkpoint_keeps_first_candidate_answer() -> None:
    """已有兜底时不覆盖：返回的增量里不含该键，State 里的旧值由 Checkpoint 原样保留。"""
    result = _checkpoint(
        _state(tool_events=[_edit("src/a.py")], pending_verification_response="更早的一版")
    )

    assert "pending_verification_response" not in result
    assert result["verification_nudge"]


def test_checkpoint_falls_back_to_candidate_when_answer_is_blank() -> None:
    result = _checkpoint(
        _state(
            model_final_response="",
            pending_verification_response="提醒前的那一版",
        )
    )

    assert result["task_status"] == "completed"
    assert result["final_response"] == "提醒前的那一版"


def test_checkpoint_still_completes_after_nudges_run_out() -> None:
    result = _checkpoint(
        _state(tool_events=[_edit("src/a.py")], verification_nudge_count=2)
    )

    assert result["task_status"] == "completed"
    assert result["final_response"] == "做完了"

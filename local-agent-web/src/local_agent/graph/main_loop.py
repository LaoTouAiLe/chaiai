"""T43 主循环的机械收口：工具可信度与模型显式进度分别处理。"""

from collections.abc import Sequence
from dataclasses import replace

from langchain_core.messages import AIMessage

from local_agent.graph.state import AgentState, EvidenceReference, ReflectionResult
from local_agent.reflection.validators import EvidenceLevel


def checkpoint_main_loop(
    state: AgentState, *, trust: EvidenceLevel, anomalies: Sequence[str],
    evidence: Sequence[EvidenceReference], pending_uncertain: Sequence[str], max_retries: int,
) -> AgentState:
    """无模型调用；正常结果回决策，最终文本结束本轮，已知阻塞不能标完成。"""
    calls = list(state.get("pending_tool_calls") or [])
    last = state.get("messages", [])[-1] if state.get("messages") else None
    finished = isinstance(last, AIMessage) and not last.tool_calls
    plan = list(state.get("plan") or [])
    managed = bool(state.get("plan_managed"))
    unfinished = managed and any(step.status not in {"completed", "skipped"} for step in plan)
    run_id = state.get("run_id")
    control_events = [event for event in state.get("tool_events", [])
                      if event.tool_name == "update_plan" and event.run_id == run_id]
    plan_error = bool(control_events and control_events[-1].status != "ok")
    completed = (
        finished and not unfinished and not plan_error
        and not pending_uncertain and trust == "verified"
    )
    reflection = ReflectionResult(
        goal_achieved=completed, result_trust=trust, anomalies=tuple(anomalies),
        next_action=("finish" if completed else "ask_user") if finished else "continue",
        evidence=tuple(evidence),
    )
    result: AgentState = {
        "final_evidence": list(evidence), "reflection_result": reflection,
        "task_status": "completed" if completed else "blocked" if finished else "running",
    }
    if pending_uncertain:
        result["messages"] = [AIMessage(content=(
            f"本轮有已放行但尚未确认结果的操作（{'、'.join(pending_uncertain)}），可能已经产生影响。"
            "请先核对文件或命令的实际结果，再决定是否继续。"
        ))]
        result["task_status"] = "blocked"
        result["reflection_result"] = replace(reflection, next_action="ask_user")
        return result
    if finished:
        if not isinstance(last, AIMessage) or not last.text.strip():
            result["messages"] = [AIMessage(content="本轮没有生成可用的回答，请补充问题或重试。")]
            result["task_status"] = "failed"
            result["reflection_result"] = replace(reflection, goal_achieved=False)
        return result
    # 安全门会清空被拒的 pending_tool_calls，原助手调用仍在审计消息中。
    if not calls:
        requested = next((message for message in reversed(state.get("messages", []))
                          if isinstance(message, AIMessage) and message.tool_calls), None)
        calls = list(requested.tool_calls) if requested is not None else []
    current_ids = {str(call.get("id")) for call in calls}
    latest = [event for event in state.get("tool_events", [])
              if event.tool_call_id in current_ids and (not run_id or event.run_id == run_id)]
    if latest and (any(event.status != "ok" for event in latest) or trust != "verified"):
        index = int(state.get("current_step_index", 0))
        count = (plan[index].retry_count if 0 <= index < len(plan)
                 else int(state.get("retry_count", 0))) + 1
        result["retry_count"] = int(state.get("retry_count", 0)) + 1
        if 0 <= index < len(plan):
            plan[index] = replace(plan[index], retry_count=count)
            result["plan"] = plan
        if count > max_retries:
            result["messages"] = [AIMessage(content=(
                "这一步连续尝试后仍未取得可确认的结果，已停止重试。"
                "请补充信息或确认下一步的处理方式。"
            ))]
            result["task_status"] = "blocked"
            result["reflection_result"] = replace(reflection, next_action="ask_user")
    return result

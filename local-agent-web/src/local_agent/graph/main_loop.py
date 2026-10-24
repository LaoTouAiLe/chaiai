"""T43 主循环的机械收口：工具可信度与模型显式进度分别处理。"""

import json
from collections.abc import Sequence

from langchain_core.messages import AIMessage

from local_agent.graph.projection import current_turn_ai_message, message_id
from local_agent.graph.state import AgentState, EvidenceReference, ReflectionResult
from local_agent.reflection.validators import EvidenceLevel


def decision_budget_instruction(state: AgentState, *, default_limit: int | None) -> str:
    """本次决策的实时额度；不写入冻结快照、历史消息或完成状态。"""
    if (state.get("agent_loop_version") != 1 or state.get("mode") != "tool"
            or state.get("session_mode") == "chat"):
        return ""
    configured = state.get("max_loop_count", default_limit)
    if configured is None:
        return ""
    limit = int(configured)
    used = max(0, int(state.get("loop_count", 0)))
    remaining = max(0, limit - used)
    if not remaining:
        return ""
    text = (
        f"【本次决策额度】本次为第{used + 1}/{limit}次决策，含本次还可调用{remaining}次。"
        "每次主循环决策计一次，同批独立工具只计一次。用task_control明确声明完成或需要输入。"
        "优先必要实现与验证，避免重复环境探测、重复取证或仅为记账消耗一轮。"
    )
    if remaining <= 4:
        text += "已临近用户配置的预算，请自行安排剩余工作与交付。"
    text += "额度不足时如实交代未完成项，不能伪造完成、跳过必要校验或审批。"
    return text


def checkpoint_main_loop(
    state: AgentState, *, trust: EvidenceLevel, anomalies: Sequence[str],
    evidence: Sequence[EvidenceReference], pending_uncertain: Sequence[str], max_retries: int,
) -> AgentState:
    """记录执行反馈；由主模型通过task_control决定继续、完成或请求输入。"""
    action = state.get("model_next_action", "continue")
    finished = action in {"finish", "ask_user"}
    completed = action == "finish"
    reflection = ReflectionResult(
        goal_achieved=completed, result_trust=trust, anomalies=tuple(anomalies),
        next_action=action, evidence=tuple(evidence),
    )
    result: AgentState = {
        "final_evidence": list(evidence), "reflection_result": reflection,
        "task_status": "completed" if completed else "blocked" if finished else "running",
        "tool_feedback_required": False,
    }
    if finished:
        text = state.get("model_final_response", "")
        result["final_response"] = text
        answer = current_turn_ai_message(state.get("messages", []))
        if answer is not None and message_id(answer):
            # 保留控制调用的原始审计与推理，交付文本投影到同一条流式消息，避免空回答和重复气泡。
            result["reply_overrides"] = {message_id(answer): text}
        else:
            result["messages"] = [AIMessage(content=text)]
        return result
    count = 0
    previous: str | None = None
    for event in state.get("tool_events", []):
        if event.run_id != state.get("run_id") or event.tool_name in {"update_plan", "task_control"}:
            continue
        if event.status == "ok":
            count, previous = 0, None
            continue
        identity = json.dumps([event.tool_name, {
            key: value for key, value in event.redacted_arguments.items() if key != "reason"
        }], sort_keys=True, ensure_ascii=False)
        count = count + 1 if identity == previous else 1
        previous = identity
    result["retry_count"] = count
    return result

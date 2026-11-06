"""T43 主循环的机械收口：工具可信度与模型显式进度分别处理。"""

import json
from collections.abc import Sequence

from langchain_core.messages import AIMessage

from local_agent.graph.projection import current_turn_ai_message, message_id
from local_agent.graph.state import (
    AgentState,
    EvidenceReference,
    ReflectionResult,
    make_decision_record,
)
from local_agent.graph.verification_stop import build_verification_nudge
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


def _nudge_budget_available(state: AgentState) -> bool:
    """还剩**不止一格**决策额度时才允许提醒（2026-11-06）。

    提醒会把本轮从"完成"改回"继续"，也就是**要多花一次模型调用**。所以只在
    ``loop_count < max - 1`` 时提醒：正好在最后一格（``loop_count == max - 1``）提醒，下一轮
    必然撞上限，完成路由会把一次正常收尾判成 failed——那正是这条提醒要避免的事。
    """
    limit = state.get("max_loop_count")
    if not limit:
        return True
    return int(state.get("loop_count", 0)) < int(limit) - 1


def checkpoint_main_loop(
    state: AgentState, *, trust: EvidenceLevel, anomalies: Sequence[str],
    evidence: Sequence[EvidenceReference], pending_uncertain: Sequence[str], max_retries: int,
    verify_on_stop: bool = False,
) -> AgentState:
    """记录执行反馈；由主模型通过task_control决定继续、完成或请求输入。

    收尾前可能有一次**验证提醒**（``verify_on_stop``，见
    :mod:`local_agent.graph.verification_stop`）：改过代码却没跑通任何验证时，把这一版回答存为
    兜底、本轮改成"继续"，让模型再看一眼。它**不是拦截**——任务状态不变、回答不丢、次数有上限，
    而且只在**还剩不止一格决策额度**时发生（2026-11-06）：正好在最后一格提醒会把它用掉，下一轮
    撞上限，完成路由反而把一次正常收尾判成 failed（见 ``completion_route`` / ``_nudge_budget_available``）。
    """
    action = state.get("model_next_action", "continue")
    finished = action in {"finish", "ask_user"}
    completed = action == "finish"

    nudge: str | None = None
    if completed and verify_on_stop and _nudge_budget_available(state):
        nudge = build_verification_nudge(state)
    if nudge is not None:
        # 提醒期间这一轮**不算结束**：状态仍 running，模型下一轮会带着提醒重新决策。
        completed = False
        finished = False
        action = "continue"

    reflection = ReflectionResult(
        goal_achieved=completed, result_trust=trust, anomalies=tuple(anomalies),
        next_action=action, evidence=tuple(evidence),
    )
    result: AgentState = {
        "final_evidence": list(evidence), "reflection_result": reflection,
        "task_status": "completed" if completed else "blocked" if finished else "running",
        "tool_feedback_required": False,
        # 每次都显式写：提醒只对**下一次**决策生效。不写 None 的话，模型照做之后那条提醒
        # 还会一直挂在系统提示里，污染后面每一轮。
        "verification_nudge": nudge,
    }
    if nudge is not None:
        result["verification_nudge_count"] = int(state.get("verification_nudge_count") or 0) + 1
        # 只留第一次被顶下来的那一版：后续提醒不该用"更差的中间稿"覆盖它。
        if not state.get("pending_verification_response"):
            result["pending_verification_response"] = state.get("model_final_response") or None
        # 轨迹页要能看出"这一轮为什么没结束"：提醒本身不进对话消息，只有这条记录可见。
        result["decision_trace"] = [make_decision_record(
            str(state.get("run_id") or "run"), stage="verification_stop",
            summary="改过代码但还没有一次成功的验证结果，已推送验证提醒；本轮不结束，任务继续",
            loop_count=int(state.get("loop_count", 0)),
        )]
    if finished:
        text = state.get("model_final_response", "")
        if not text.strip():
            # 兜底：提醒之后模型没给出新正文就结束（例如被取消或被预算截断），交付提醒前那一版。
            text = str(state.get("pending_verification_response") or "")
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

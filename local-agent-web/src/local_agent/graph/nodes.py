"""LangGraph 节点（节点名严格对齐架构 §5.1）。

当前实现：

- 闲聊分支：``prepare_context`` → ``load_agents`` → ``recall_memory`` → ``mode_router``
  → ``compression_node`` → ``chat_node`` → ``final_response_node``
- 工具分支：``mode_router`` → ``planner_node`` → ``compression_node`` →
  ``agent_decision_node`` → ``safety_gate_node`` → ``tool_execution_node`` →
  ``reflection_node`` → ``completion_router``，再回到决策节点或产出回答。

其余按计划落地：写/改/删除工具与安全 Shell 在 T06/T07，完整机械证据与幂等在 T08，
审批节点在 T10，记忆节点在 T18，多步规划在 T20。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import ToolCall
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from local_agent.context.agents_loader import load_agents
from local_agent.context.prompt_context import build_system_prompt
from local_agent.exceptions import LocalAgentError, SecurityError
from local_agent.graph.state import (
    AgentState,
    DecisionRecord,
    ErrorEvent,
    EvidenceReference,
    EvidenceType,
    ReflectionResult,
    RuntimeContext,
    TaskStatus,
    ToolEvent,
    approval_key,
    decision_record_key,
    error_event_key,
    make_decision_record,
    make_error_event,
    merge_appended,
    tool_event_key,
    tool_result_key,
)
from local_agent.llm.models import TOOL_CALLING_UNAVAILABLE_REPLY, ModelCapabilities
from local_agent.reflection.validators import ToolEvidence, summarize_trust, validate_tool_event
from local_agent.security.approval import (
    ApprovalChoice,
    ApprovalRequest,
    action_fingerprint,
    build_decision,
    build_request,
    find_approval,
    find_rejection,
)
from local_agent.security.policy import SafetyDecision, SecurityPolicy
from local_agent.tools.file_tools import OUTPUT_PREVIEW_CHARS
from local_agent.tools.result_types import ToolResultRecord, ToolResultStatus

PREPARE_CONTEXT = "prepare_context"
LOAD_AGENTS = "load_agents"
RECALL_MEMORY = "recall_memory"
COMPRESSION_NODE = "compression_node"
PLANNER_NODE = "planner_node"
AGENT_DECISION_NODE = "agent_decision_node"
SAFETY_GATE_NODE = "safety_gate_node"
APPROVAL_INTERRUPT_NODE = "approval_interrupt_node"
TOOL_EXECUTION_NODE = "tool_execution_node"
REFLECTION_NODE = "reflection_node"
CHAT_NODE = "chat_node"
FINAL_RESPONSE_NODE = "final_response_node"

#: 人工拒绝后的可见说明（不自动重试，架构 §5.3）。
REJECTED_REPLY = (
    "该动作已被你拒绝，我不会执行，也不会自动重试。"
    "如果希望换个做法，可以补充说明后让我重新规划。"
)


# --------------------------------------------------------------------------- 闲聊分支


def prepare_context_node(state: AgentState) -> AgentState:
    """补齐会话标识、规范化输入，并把本轮用户消息写入 ``messages``。

    用户消息在这里写入而不是由网页传入：开启 Checkpoint 后历史由 State 维护，
    调用方每次只提交 ``user_input``（T03 起）。空输入不产生消息。
    ``mode`` 由 ``mode_router`` 决定，本节点不再预设。

    **一次提交 = 一个任务**：本节点同时把上一轮的"本轮作用域"字段全部复位
    （循环计数、任务目标、计划、待执行调用、挂起审批、反思结果等），否则多轮工具任务
    会累计到 ``agent.max_tool_loops``，新任务还会带着旧目标与旧审批继续跑（架构 §4.2）。

    注意本节点只在**新请求**时执行：审批恢复（``Command(resume=...)``）从断点继续，
    不会重跑这里，所以复位不会打断同一任务的后续步骤。追加型字段（``messages``、
    ``tool_events``、``tool_results``、``decision_trace``、``approval_history``、
    ``error_events``、``completed_steps``）属于历史审计记录，不在这里清理。
    """
    user_input = str(state.get("user_input", ""))
    update: AgentState = {
        "thread_id": state.get("thread_id") or uuid4().hex,
        "session_id": state.get("session_id") or uuid4().hex,
        "run_id": uuid4().hex,
        "normalized_request": user_input.strip(),
        "task_status": "running",
        # 以下都是"本轮"字段（覆盖语义），新一轮必须回到初始值
        "task_goal": None,
        "plan": [],
        "current_step_index": 0,
        "pending_tool_calls": [],
        "pending_approval": None,
        "reflection_result": None,
        "retry_count": 0,
        "loop_count": 0,
        "memory_refs": [],
        "rag_refs": [],
        "final_response": None,
        "final_evidence": [],
    }
    if user_input.strip():
        update["messages"] = [HumanMessage(content=user_input)]
    return update


def load_agents_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """加载并合并 AGENTS 规则，写入 RuntimeContext（架构 §4.1、§7.1）。

    规则文本不放进 State：它属于单次运行的临时上下文，写入 Checkpoint 没有意义，
    也会让每轮都重复保存同一份规则。空结果写为 ``None``。
    """
    context = runtime.context
    loaded = load_agents(
        context.app_config.agents,
        workspace_root=context.workspace_root,
        current_cwd=context.current_cwd,
    )
    context.agents_context = loaded.text or None
    return {}


def recall_memory_node(state: AgentState) -> AgentState:
    """召回中长期记忆（v0.1 透传，不访问任何记忆库；T14 接入向量召回）。"""
    return {}


def compression_node(state: AgentState) -> AgentState:
    """上下文压缩（v0.1 透传；T16 接入摘要压缩与 Prompt 窗口裁剪）。"""
    return {}


def chat_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """闲聊节点：只把消息交给模型，不绑定任何工具。

    架构 §1.3-3 要求闲聊模式不注册文件与 Shell 工具，因此本节点不调用
    ``bind_tools``；模型实例来自 RuntimeContext（架构 §4.1），不在图内持有。
    """
    context = runtime.context
    prompt = build_system_prompt(
        mode=state.get("mode", "chat"),
        context=context,
        route_reason=str(state.get("route_reason", "")),
    )
    messages = [
        SystemMessage(content=prompt.system_prompt),
        *state.get("messages", []),
    ]
    return {"messages": [context.model_adapter.invoke(messages)]}


# --------------------------------------------------------------------------- 工具分支


def planner_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """工具分支的规划入口。

    T05 只确定任务目标与循环上限（架构 §11.5），多步计划、依赖与重规划在 T20。
    ``task_goal`` 已存在时保留，便于重入时不丢目标。
    """
    context = runtime.context
    return {
        "task_goal": state.get("task_goal") or str(state.get("normalized_request", "")),
        "max_loop_count": context.app_config.agent.max_tool_loops,
        "task_status": "running",
    }


def agent_decision_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """决策节点：把当前模式允许的工具绑定给模型，取回工具调用或回答。

    工具调用由模型提出，**不代表会被执行**：是否放行由 ``safety_gate_node`` 判定。
    """
    context = runtime.context
    registry = context.tool_registry
    if registry is None:
        raise LocalAgentError("运行上下文缺少 tool_registry，无法进行工具决策")

    capabilities = context.model_capabilities
    tools = registry.tools_for_mode(str(state.get("mode", "chat")))
    if tools and isinstance(capabilities, ModelCapabilities) and not capabilities.tool_calling:
        # 架构 §11.7：不支持 Tool Calling 时不得静默退化成闲聊，要明确告知
        return _tool_calling_unavailable(state, capabilities)
    model = context.model_adapter.bind_tools(tools) if tools else context.model_adapter
    condition = build_system_prompt(
        mode=state.get("mode", "tool"),
        context=context,
        route_reason=str(state.get("route_reason", "")),
    )
    response = model.invoke(
        [
            SystemMessage(content=condition.system_prompt),
            *state.get("messages", []),
        ]
    )
    if not isinstance(response, AIMessage):
        raise LocalAgentError("模型没有返回 AI 消息，无法继续工具分支")

    calls = list(response.tool_calls)
    summary = f"模型提出 {len(calls)} 个工具调用" if calls else "模型直接给出回答"
    record = _decision(state, stage="agent_decision", summary=summary)

    return {
        "messages": [response],
        "pending_tool_calls": calls,
        "loop_count": int(state.get("loop_count", 0)) + 1,
        "decision_trace": merge_appended(
            state.get("decision_trace", []), [record], decision_record_key
        ),
    }


def _tool_calling_unavailable(state: AgentState, capabilities: ModelCapabilities) -> AgentState:
    """模型不支持 Tool Calling 时的失败闭合结果（架构 §11.7）。"""
    summary = f"模型 {capabilities.provider_name or '未知'} 不支持 Tool Calling，工具模式失败闭合"
    return {
        "messages": [AIMessage(content=TOOL_CALLING_UNAVAILABLE_REPLY)],
        "pending_tool_calls": [],
        "task_status": "failed",
        "decision_trace": merge_appended(
            state.get("decision_trace", []),
            [_decision(state, stage="agent_decision", summary=summary)],
            decision_record_key,
        ),
        "error_events": merge_appended(
            state.get("error_events", []),
            [_error(state, stage="agent_decision", message=summary)],
            error_event_key,
        ),
    }


def safety_gate_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """安全门：逐个判定工具调用是否放行（架构 §5.3、§1.3-4）。

    被拒绝的调用立刻回填 ``ToolMessage`` 与工具事件，让模型看到拒绝原因；
    ``pending_tool_calls`` 是覆盖语义，这里把它收敛为“仍需执行”的调用，
    这样后续条件边无须再访问策略对象即可判断有没有要执行的动作。
    """
    policy = _require_policy(runtime.context)
    history = list(state.get("approval_history", []))
    allowed: list[ToolCall] = []
    approval: ApprovalRequest | None = None
    messages: list[AnyMessage] = []
    events: list[ToolEvent] = []
    results: list[ToolResultRecord] = []
    records: list[DecisionRecord] = []
    errors: list[ErrorEvent] = []

    for call in state.get("pending_tool_calls", []):
        call_id = _call_id(call)
        name = call["name"]
        arguments = dict(call.get("args") or {})
        decision = policy.classify_tool(name, arguments)
        fingerprint = action_fingerprint(name, arguments, policy=policy)

        if decision.requires_approval:
            if find_rejection(history, fingerprint) is not None:
                # 架构 §5.3：拒绝按动作指纹匹配，模型换一个 tool_call_id 也不能绕开
                reason = policy.redact(
                    "该动作此前已被你拒绝（动作指纹一致），不会再次请求审批，也不会执行"
                )
                decision = SafetyDecision(
                    allowed=False, requires_approval=False, reason=reason
                )
            else:
                approved = find_approval(
                    history, run_id=_run_id(state), tool_call_id=call_id, fingerprint=fingerprint
                )
                if approved is not None:
                    # 同一次调用已获批准（异常后重放）：沿用批准继续执行
                    allowed.append(call)
                    records.append(
                        _decision(
                            state,
                            stage="tool_start",
                            summary=f"沿用已批准的动作 {name}",
                            tool_call_id=call_id,
                        )
                    )
                elif approval is None:
                    approval = build_request(
                        tool_name=name,
                        run_id=_run_id(state),
                        tool_call_id=call_id,
                        arguments=arguments,
                        decision=decision,
                        policy=policy,
                    )
                    records.append(
                        _decision(
                            state,
                            stage="safety_gate",
                            summary=f"请求审批 {name}：{decision.reason}",
                            tool_call_id=call_id,
                        )
                    )
                else:
                    # 同一条回复里有多个需要审批的动作：本轮只提交一个，其余的必须回填
                    # 明确结果。否则模型会看到"有 tool_call 却没有 ToolMessage"的残缺序列，
                    # 而它也无从知道该怎么继续（架构 §4.2 要求每个调用都有处理结果）。
                    deferred = (
                        f"{name} 需要人工审批：同一条回复里的多个审批动作按顺序处理，"
                        "本次只提交了第一个。请在本轮结束后重新提交这个调用以获取审批。"
                    )
                    messages.append(
                        ToolMessage(content=deferred, tool_call_id=call_id, name=name)
                    )
                    events.append(
                        ToolEvent(
                            tool_call_id=call_id,
                            tool_name=name,
                            redacted_arguments=_redacted_arguments(call, policy),
                            approval_required=True,
                            status="denied",
                            output_preview=deferred,
                            output_digest=_digest(deferred),
                            run_id=_run_id(state),
                        )
                    )
                    results.append(
                        ToolResultRecord(
                            tool_call_id=call_id,
                            tool_name=name,
                            status="denied",
                            truncated=False,
                            output_preview=deferred,
                            output_digest=_digest(deferred),
                            run_id=_run_id(state),
                        )
                    )
                    records.append(
                        _decision(
                            state,
                            stage="safety_gate",
                            summary=f"{name} 等下一轮审批（本轮只处理一个审批动作）",
                            tool_call_id=call_id,
                        )
                    )
                continue
        elif decision.allowed:
            allowed.append(call)
            # 放行标记在本节点结束时就写入 Checkpoint。若随后执行中崩溃，
            # 恢复时能识别出“已放行但结果未知”，从而不自动重放副作用（架构 §4.2）。
            records.append(
                _decision(
                    state,
                    stage="tool_start",
                    summary=f"放行 {name}",
                    tool_call_id=call_id,
                )
            )
            continue
        reason = policy.redact(decision.reason)
        messages.append(
            ToolMessage(
                content=f"已拒绝执行 {call['name']}：{reason}",
                tool_call_id=call_id,
                name=call["name"],
            )
        )
        events.append(
            ToolEvent(
                tool_call_id=call_id,
                tool_name=call["name"],
                redacted_arguments=_redacted_arguments(call, policy),
                approval_required=decision.requires_approval,
                status="denied",
                output_preview=reason,
                output_digest=_digest(reason),
                run_id=_run_id(state),
            )
        )
        results.append(
            ToolResultRecord(
                tool_call_id=call_id,
                tool_name=call["name"],
                status="denied",
                truncated=False,
                output_preview=reason,
                output_digest=_digest(reason),
                run_id=_run_id(state),
            )
        )
        records.append(
            _decision(
                state,
                stage="safety_gate",
                summary=f"拒绝 {call['name']}：{reason}",
                tool_call_id=call_id,
            )
        )
        errors.append(
            _error(
                state,
                stage="safety_gate",
                message=f"工具被安全策略拒绝：{call['name']}（{reason}）",
                tool_call_id=call_id,
            )
        )

    update: AgentState = {
        "pending_tool_calls": allowed,
        "pending_approval": approval,
        "decision_trace": merge_appended(
            state.get("decision_trace", []), records, decision_record_key
        ),
    }
    if messages:
        update["messages"] = messages
        update["tool_events"] = merge_appended(
            state.get("tool_events", []), events, tool_event_key
        )
        update["tool_results"] = merge_appended(
            state.get("tool_results", []), results, tool_result_key
        )
    if errors:
        update["error_events"] = merge_appended(
            state.get("error_events", []), errors, error_event_key
        )
    return update


def tool_execution_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """执行放行的工具调用，并把结果回填成 ``ToolMessage``（架构 §6）。

    工具失败不中断图：异常被转换成给模型看的说明，同时记录工具事件与结果。
    """
    context = runtime.context
    policy = _require_policy(context)
    registry = context.tool_registry
    if registry is None:
        raise LocalAgentError("运行上下文缺少 tool_registry，无法执行工具")

    scope = current_task_scope(state)
    # "已执行过"按 (run_id, tool_call_id) 判定：上一轮复用同一个调用 ID 不算已执行
    confirmed = {
        record["tool_call_id"]
        for record in state.get("tool_results", [])
        if _in_current_task(record.get("run_id"), scope=scope)
    }

    messages: list[AnyMessage] = []
    events: list[ToolEvent] = []
    results: list[ToolResultRecord] = []
    records: list[DecisionRecord] = []
    errors: list[ErrorEvent] = []

    for call in state.get("pending_tool_calls", []):
        call_id = _call_id(call)
        name = call["name"]

        if call_id in confirmed:
            # 已确认过的调用：既不重复执行，也不重复追加事件（架构 §4.2）
            records.append(
                _decision(
                    state,
                    stage="tool_execution",
                    summary=f"{name} 已有确认结果，跳过重复执行",
                    tool_call_id=call_id,
                )
            )
            continue

        arguments = dict(call.get("args") or {})
        if _approval_target_changed(
            state, run_id=scope, call_id=call_id, tool_name=name, arguments=arguments, policy=policy
        ):
            # 架构 §5.3：批准后目标状态变化则不得沿用旧批准
            note = f"{name} 的批准已失效：目标状态与批准时不一致，需要重新审批后再执行"
            messages.append(ToolMessage(content=note, tool_call_id=call_id, name=name))
            events.append(
                ToolEvent(
                    tool_call_id=call_id,
                    tool_name=name,
                    status="denied",
                    approval_required=True,
                    output_preview=note,
                    output_digest=_digest(note),
                    run_id=_run_id(state),
                )
            )
            results.append(
                ToolResultRecord(
                    tool_call_id=call_id,
                    tool_name=name,
                    status="denied",
                    truncated=False,
                    output_preview=note,
                    output_digest=_digest(note),
                    run_id=_run_id(state),
                )
            )
            errors.append(
                _error(
                    state,
                    stage="tool_execution",
                    message=f"批准失效，拒绝执行：{name}",
                    tool_call_id=call_id,
                )
            )
            records.append(
                _decision(
                    state,
                    stage="tool_execution",
                    summary=f"{name} 批准失效，拒绝执行",
                    tool_call_id=call_id,
                )
            )
            continue

        tool = registry.get(name)
        started_at = datetime.now(timezone.utc)
        content: str
        status: ToolResultStatus
        # 需要审批的动作：只有拿到人工批准（同一次调用 + 指纹一致）才临时授权执行
        fingerprint = action_fingerprint(name, arguments, policy=policy)
        granted = (
            find_approval(
                state.get("approval_history", []),
                run_id=scope,
                tool_call_id=call_id,
                fingerprint=fingerprint,
            )
            is not None
        )
        if granted:
            policy.grant(fingerprint)
        try:
            if tool is None:
                content, status = f"未找到工具 {name}", "error"
            else:
                content, status = _invoke_tool(tool, call, policy)
        finally:
            if granted:
                # 批准是一次性的：执行完立即撤销（架构 §6.5 的"恰好执行一次"）
                policy.revoke(fingerprint)

        messages.append(ToolMessage(content=content, tool_call_id=call_id, name=name))
        events.append(
            ToolEvent(
                tool_call_id=call_id,
                tool_name=name,
                redacted_arguments=_redacted_arguments(call, policy),
                status=status,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                output_preview=content[:OUTPUT_PREVIEW_CHARS],
                output_digest=_digest(content),
                run_id=_run_id(state),
            )
        )
        results.append(
            ToolResultRecord(
                tool_call_id=call_id,
                tool_name=name,
                status=status,
                truncated=_result_truncated(content),
                output_preview=content[:OUTPUT_PREVIEW_CHARS],
                output_digest=_digest(content),
                run_id=_run_id(state),
            )
        )
        records.append(
            _decision(
                state,
                stage="tool_execution",
                summary=f"{name} → {status}",
                tool_call_id=call_id,
            )
        )
        if status != "ok":
            errors.append(
                _error(
                    state,
                    stage="tool_execution",
                    message=f"{name} 执行状态为 {status}",
                    tool_call_id=call_id,
                )
            )

    update: AgentState = {
        "decision_trace": merge_appended(
            state.get("decision_trace", []), records, decision_record_key
        ),
    }
    if messages:
        update["messages"] = messages
        update["tool_events"] = merge_appended(
            state.get("tool_events", []), events, tool_event_key
        )
        update["tool_results"] = merge_appended(
            state.get("tool_results", []), results, tool_result_key
        )
    if errors:
        update["error_events"] = merge_appended(
            state.get("error_events", []), errors, error_event_key
        )
    return update


def reflection_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """最小反思：机械校验工具结果并重算证据（架构 §5.4、§11.2）。

    - ``goal_achieved`` 在本阶段恒为 ``False``：是否真正完成由 T17 的 LLM 反思判断，
      这里不替模型下结论，也不因为模型自称完成就算完成。
    - 是否 ``verified`` 只由 :mod:`local_agent.reflection.validators` 的机械校验决定。
    - ``final_evidence`` 按架构 §4.2 从**当前任务**的工具事件重算后整体覆盖：历史任务的
      事件与错误不再混进本轮证据（作用域见 :func:`current_task_scope`）。
    """
    del runtime  # 机械校验不需要运行上下文
    scope = current_task_scope(state)
    records = {
        record["tool_call_id"]: record
        for record in state.get("tool_results", [])
        if _in_current_task(record.get("run_id"), scope=scope)
    }
    payloads = _tool_payloads(state.get("messages", []))
    checks: list[ToolEvidence] = []
    anomalies: list[str] = []
    evidences: list[EvidenceReference] = []

    for event in state.get("tool_events", []):
        if not _in_current_task(event.run_id, scope=scope):
            continue
        record = records.get(event.tool_call_id)
        if record is None:
            continue
        check = validate_tool_event(
            event, record, payload=payloads.get(event.tool_call_id)
        )
        checks.append(check)
        if check.evidence_level != "verified":
            anomalies.extend(f"{event.tool_name}: {reason}" for reason in check.reasons)
        evidences.append(
            EvidenceReference(
                source_event_id=event.tool_call_id,
                evidence_type=_evidence_type(event.tool_name),
                summary=f"{event.tool_name} → {event.output_preview[:120]}",
                verified=check.evidence_level == "verified",
                created_at=event.finished_at,
            )
        )

    trust = summarize_trust(checks)
    for error_record in state.get("error_events", []):
        if not _in_current_task(error_record.get("run_id"), scope=scope):
            continue
        message = str(error_record.get("message") or "")
        if message:
            anomalies.append(f"{error_record.get('stage')}: {message}")
    pending_uncertain = unconfirmed_calls(state)
    for call_id in pending_uncertain:
        anomalies.append(
            f"tool_execution: 调用 {call_id} 已放行但没有确认结果，可能有未确认的副作用"
        )

    unique_evidence = _dedupe_evidence(evidences)
    trace = _decision(
        state,
        stage="reflection",
        summary=f"结果可信度 {trust}；证据 {len(unique_evidence)} 条；异常 {len(anomalies)} 条",
    )

    update: AgentState = {
        "final_evidence": unique_evidence,
        "reflection_result": ReflectionResult(
            goal_achieved=False,
            result_trust=trust,
            anomalies=tuple(anomalies),
            next_action="continue",
        ),
        "decision_trace": merge_appended(
            state.get("decision_trace", []), [trace], decision_record_key
        ),
    }
    if trust == "failed":
        update["error_events"] = merge_appended(
            state.get("error_events", []),
            [
                _error(
                    state,
                    stage="reflection",
                    message="机械校验未通过：本轮工具结果不能作为已验证证据",
                )
            ],
            error_event_key,
        )
    if pending_uncertain:
        update["error_events"] = merge_appended(
            update.get("error_events", state.get("error_events", [])),
            [
                _error(
                    state,
                    stage="reflection",
                    message=f"存在未确认的副作用：{'、'.join(pending_uncertain)}，不自动重放",
                )
            ],
            error_event_key,
        )
    return update


# --------------------------------------------------------------------------- 输出


def final_response_node(state: AgentState) -> AgentState:
    """把最后一条有文本的 AI 消息定为最终回答。

    - 没有可用的 AI 文本时标记为 ``failed``，由页面给出明确提示，而不是编造回答。
    - 已经被判定为失败（例如超出循环上限）的运行，不在这里被改写成 ``completed``：
      有回答不等于任务完成。
    """
    text = last_ai_text(state.get("messages", []))
    if text is None:
        return {"final_response": None, "task_status": "failed"}
    status: TaskStatus = "failed" if state.get("task_status") == "failed" else "completed"
    return {"final_response": text, "task_status": status}


def last_ai_text(messages: Sequence[AnyMessage]) -> str | None:
    """返回最后一条 AI 消息的纯文本；没有则返回 ``None``。"""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = message.text.strip()
            if text:
                return text
    return None


# --------------------------------------------------------------------------- 内部工具


def _require_policy(context: RuntimeContext) -> SecurityPolicy:
    if context.security_policy is None:
        raise LocalAgentError("运行上下文缺少 security_policy，无法判定工具安全性")
    return context.security_policy


def approval_interrupt_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """人工审批节点（架构 §5.3）：批准 / 修改参数 / 拒绝三出口。

    注意事项：

    - 本节点在 resume 时会被整体重放，因此 ``interrupt()`` 之前不能有任何副作用；
    - 递归删除需要**两次独立恢复**：第一次恢复无论传入什么都不构成授权，只有第二次
      独立的 ``interrupt()`` 返回"批准 + 显式二次确认"才授权（架构 §6.5）。
      阶段不能用"决策内容"推断，否则一次伪造的带标记恢复就能跳过二次确认；
    - 第一阶段选择拒绝 / 修改参数时立即按该决策结束，不再要求第二次确认；
    - 决策写入 ``approval_history``，条件是"同一次审批只记一次"，重放不会重复追加。
    """
    del runtime  # 审批只依据已提交的请求，不访问运行上下文
    request = state.get("pending_approval")
    if not isinstance(request, Mapping):
        return {}

    first = _as_mapping(interrupt(_approval_prompt(request, stage=1)))
    decision_input: Mapping[str, Any] = first
    if bool(request.get("requires_second_confirmation")) and (
        _normalize_choice(first.get("decision")) == "approve"
    ):
        # 第一次只负责推进阶段：必须由第二次独立恢复给出授权
        second = _as_mapping(interrupt(_approval_prompt(request, stage=2)))
        if _normalize_choice(second.get("decision")) == "approve" and not _second_confirmed(
            second
        ):
            # 第二次没有显式勾选"确认删除"→ 一律按拒绝处理（失败闭合）
            decision_input = {**second, "decision": "reject"}
        else:
            decision_input = second

    choice = _normalize_choice(decision_input.get("decision"))

    edited = decision_input.get("edited_arguments")
    record = build_decision(
        request,
        choice=choice,
        edited_arguments=edited if isinstance(edited, Mapping) else None,
        reason=str(decision_input.get("reason") or ""),
    )

    update: AgentState = {
        "pending_approval": None,
        "approval_history": merge_appended(
            state.get("approval_history", []), [record], approval_key
        ),
        "decision_trace": merge_appended(
            state.get("decision_trace", []),
            [
                _decision(
                    state,
                    stage="approval_interrupt",
                    summary=f"人工决策 {choice}：{request.get('tool_name')}",
                    tool_call_id=str(request.get("tool_call_id") or ""),
                )
            ],
            decision_record_key,
        ),
    }

    if choice == "approve":
        # 用审批请求里的参数重建调用：执行的就是人工看到并批准的那一份（架构 §5.3）
        approved_id = str(request.get("tool_call_id") or "")
        approved_call: ToolCall = {
            "name": str(request.get("tool_name") or ""),
            "args": dict(request.get("arguments") or {}),
            "id": approved_id,
            "type": "tool_call",
        }
        # 只替换被审批的这一个调用：同批次里已经放行的免审调用必须留在待执行列表里，
        # 否则它们既不会执行、也没有 ToolMessage，模型会收到"有调用无结果"的消息。
        pending_calls = list(state.get("pending_tool_calls", []))
        if any(_call_id(call) == approved_id for call in pending_calls):
            update["pending_tool_calls"] = [
                approved_call if _call_id(call) == approved_id else call
                for call in pending_calls
            ]
        else:  # pragma: no cover - 正常路径下被审批的调用一定在待执行列表里
            update["pending_tool_calls"] = [*pending_calls, approved_call]
    elif choice == "edit":
        update["pending_tool_calls"] = []
        update["messages"] = [
            HumanMessage(content=_edit_message(request, edited, str(decision_input.get("reason") or "")))
        ]
    else:
        update["pending_tool_calls"] = []
        update["messages"] = [AIMessage(content=REJECTED_REPLY)]
    return update


def _approval_prompt(request: Mapping[str, Any], *, stage: int) -> dict[str, Any]:
    """审批提示载荷（必须可 JSON 序列化，会写进 Checkpoint）。"""
    return {
        "approval_id": str(request.get("approval_id") or ""),
        "tool_call_id": str(request.get("tool_call_id") or ""),
        "tool_name": str(request.get("tool_name") or ""),
        "action_fingerprint": str(request.get("action_fingerprint") or ""),
        "arguments": request.get("arguments") or {},
        "impact_summary": str(request.get("impact_summary") or ""),
        "confirmation_stage": stage,
        "requires_second_confirmation": bool(request.get("requires_second_confirmation")),
        "prompt": (
            "请确认是否执行该动作"
            if stage == 1
            else "递归删除需要二次确认：请再次确认删除该目录及其全部内容"
        ),
    }


def _second_confirmed(value: Mapping[str, Any]) -> bool:
    return value.get("decision") == "approve" and bool(value.get("confirm_recursive"))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """把 ``interrupt()`` 的返回值收敛成映射；非法输入按空决策处理（失败闭合）。"""
    return value if isinstance(value, Mapping) else {}


def _normalize_choice(value: Any) -> ApprovalChoice:
    """把人工输入归一化；未知输入一律按拒绝处理（失败闭合）。"""
    choice = str(value or "").strip().lower()
    if choice in {"approve", "edit", "reject"}:
        return cast(ApprovalChoice, choice)
    return "reject"


def _edit_message(
    request: Mapping[str, Any], edited: Any, reason: str
) -> str:
    """把人工修改意见交回规划/决策节点（架构 §5.3 的 edit 出口）。"""
    parts = [
        f"你刚才请求的动作（{request.get('tool_name')}）没有按原样执行。",
        f"影响范围：{request.get('impact_summary')}",
    ]
    if reason:
        parts.append(f"修改原因：{reason}")
    if isinstance(edited, Mapping):
        parts.append(f"请改用这些参数重新执行：{json.dumps(dict(edited), ensure_ascii=False)}")
    else:
        parts.append("请重新给出参数后再试。")
    return "\n".join(parts)


def _approval_target_changed(
    state: AgentState,
    *,
    run_id: str,
    call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    policy: SecurityPolicy,
) -> bool:
    """批准后目标状态是否已经变化（架构 §5.3：执行前复核批准指纹）。

    只认**本轮**该调用的批准记录：上一轮复用同一个 ``tool_call_id`` 的批准不参与复核。
    """
    for record in reversed(list(state.get("approval_history", []))):
        if record.get("decision") != "approve":
            continue
        if str(record.get("run_id") or "") != run_id:
            continue
        if record.get("tool_call_id") != call_id:
            continue
        approved = str(record.get("action_fingerprint") or "")
        return approved != action_fingerprint(tool_name, arguments, policy=policy)
    return False


def _run_id(state: AgentState) -> str:
    return str(state.get("run_id") or "run")


def current_task_scope(state: AgentState) -> str:
    """当前任务的作用域键，用于筛出属于"本轮任务"的工具事件与结果（架构 §4.2）。

    v0.1 里"一个任务 = 一次用户提交"，所以作用域就是本轮的 ``run_id``。
    将来支持跨多轮续做同一个任务时，只需要把这里换成"任务开始时的 run_id"（或任务 ID）：
    执行与反思只依赖"作用域键"这个概念，调用方不用改，历史任务的证据也不会丢。
    """
    return _run_id(state)


def _in_current_task(record_run_id: str | None, *, scope: str) -> bool:
    """该记录是否属于当前任务：作用域键严格相等。

    升级前写入的记录没有 ``run_id``（``None``）：它们只保证"能读出来"，**不参与**当前
    任务的执行判定与反思统计——否则历史证据会混进每一轮的反思，同 ID 的新调用也会被
    当成"已执行"跳过。按严格相等处理，历史就只是历史。
    """
    return record_run_id == scope


def _decision(
    state: AgentState,
    *,
    stage: str,
    summary: str,
    tool_call_id: str | None = None,
) -> DecisionRecord:
    """构造决策轨迹记录（``record_id`` 与循环轮次绑定，重放时自动去重）。"""
    return make_decision_record(
        _run_id(state),
        stage=stage,
        summary=summary,
        loop_count=int(state.get("loop_count", 0)),
        tool_call_id=tool_call_id,
    )


def _error(
    state: AgentState,
    *,
    stage: str,
    message: str,
    tool_call_id: str | None = None,
) -> ErrorEvent:
    """构造错误事件。"""
    return make_error_event(
        _run_id(state),
        stage=stage,
        message=message,
        loop_count=int(state.get("loop_count", 0)),
        tool_call_id=tool_call_id,
    )


def _started_call_ids(state: AgentState) -> set[str]:
    """本次运行中"已放行但尚未确认结果"的调用 ID。

    用于识别"副作用可能已发生、但 Checkpoint 未提交"的情况：这类调用不得自动重放
    （架构 §4.2）。上一次运行的标记带的是旧 ``run_id``，不会命中。
    """
    prefix = f"{_run_id(state)}:decision:tool_start:"
    started: set[str] = set()
    for record in state.get("decision_trace", []):
        record_id = str(record.get("record_id", ""))
        call_id = record.get("tool_call_id")
        if record_id.startswith(prefix) and call_id:
            started.add(str(call_id))
    return started


def unconfirmed_calls(state: AgentState) -> list[str]:
    """本次运行中"已放行但没有确认结果"的调用 ID（架构 §4.2 的不确定副作用）。

    正常完成的一轮里，每个 ``tool_start`` 标记都会有对应的工具结果。一旦出现"有标记、
    无结果"，说明上次运行在产生副作用前后异常结束（例如进程被杀、Checkpoint 未提交）。
    这类调用**不得自动重放**：调用方（页面 / 反思节点）应提示人工核对文件或命令影响。
    """
    scope = current_task_scope(state)
    confirmed = {
        record["tool_call_id"]
        for record in state.get("tool_results", [])
        if _in_current_task(record.get("run_id"), scope=scope)
    }
    return sorted(_started_call_ids(state) - confirmed)


def _dedupe_evidence(evidences: Sequence[EvidenceReference]) -> list[EvidenceReference]:
    """按 ``source_event_id`` 去重，保留首次出现的证据（架构 §4.2）。"""
    seen: set[str] = set()
    unique: list[EvidenceReference] = []
    for evidence in evidences:
        if evidence.source_event_id in seen:
            continue
        seen.add(evidence.source_event_id)
        unique.append(evidence)
    return unique


def _call_id(call: ToolCall) -> str:
    """工具调用 ID：LangChain 的 ``ToolCall`` 允许 ``id`` 为 None，这里统一成字符串。"""
    return str(call.get("id") or "")


def _invoke_tool(
    tool: object,
    call: ToolCall,
    policy: SecurityPolicy,
) -> tuple[str, ToolResultStatus]:
    """调用单个工具并归一化输出：返回（回填给模型的内容, 状态）。"""
    invoke = getattr(tool, "invoke", None)
    if not callable(invoke):  # pragma: no cover - 正常情况下 BaseTool 一定有 invoke
        return f"工具 {call['name']} 不可调用", "error"
    try:
        raw = invoke(call)
    except SecurityError as exc:
        return f"被安全策略拒绝：{policy.redact(str(exc))}", "denied"
    except Exception as exc:  # noqa: BLE001 — 工具失败必须回填给模型，不能中断整张图
        return f"工具执行失败（{type(exc).__name__}）：{policy.redact(str(exc))}", "error"

    content = raw.content if isinstance(raw, ToolMessage) else raw
    text = content if isinstance(content, str) else str(content)
    return policy.redact(text), "ok"


def _redacted_arguments(call: ToolCall, policy: SecurityPolicy) -> dict[str, object]:
    arguments: Mapping[str, object] = call.get("args") or {}
    return {str(key): policy.redact(str(value)) for key, value in arguments.items()}


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _result_truncated(content: str) -> bool:
    """从工具返回的 JSON 里读取截断标记；解析不出来时按未截断处理。"""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return bool(payload.get("truncated")) if isinstance(payload, Mapping) else False


def _evidence_type(tool_name: str) -> EvidenceType:
    """工具产出对应的证据类型（架构 §4.3 的取值集合）。"""
    if tool_name == "read_file":
        return "file_hash"
    if tool_name in {"edit_file", "write_file"}:
        return "diff"
    if tool_name == "run_shell":
        return "exit_code"
    return "stdout"


def _tool_payloads(messages: Sequence[AnyMessage]) -> dict[str, Mapping[str, Any]]:
    """从 ``ToolMessage`` 里解析出工具返回的结构化内容，供机械校验使用。

    工具结果已经脱敏，这里只做解析，不再改写内容。
    """
    payloads: dict[str, Mapping[str, Any]] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            parsed = json.loads(message.text)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, Mapping):
            payloads[str(message.tool_call_id or "")] = parsed
    return payloads

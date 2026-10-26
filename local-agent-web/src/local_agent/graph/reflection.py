"""工具证据校验与反思节点；组合回答、记忆、执行及计划模块。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from langgraph.runtime import Runtime

from local_agent.graph.evidence import (
    TOOL_EVIDENCE_CHARS,
    tool_evidence_text,
)
from local_agent.graph.main_loop import checkpoint_main_loop
from local_agent.graph.state import (
    AgentState,
    ErrorEvent,
    EvidenceReference,
    EvidenceType,
    ReflectionResult,
    RuntimeContext,
    decision_record_key,
    error_event_key,
    merge_appended,
)
from local_agent.llm.models import ModelCapabilities
from local_agent.reflection.reflector import (
    ReflectionDecision,
    reflect,
)
from local_agent.reflection.validators import (
    EvidenceLevel,
    ToolEvidence,
    summarize_trust,
    validate_tool_event,
)

from .answer import (
    _ending,
)
from .common import (
    _cancelled_turn_update,
    _decision,
    _delivered_answer,
    _in_current_task,
    _is_cancelled,
    _last_answer_text,
    _main_conversation_prefix,
    _tool_payloads,
    current_task_scope,
)
from .memory import (
    _chat_memory_reflection,
    _memory_candidates,
)
from .names import REFLECTION_NODE
from .plan import (
    _advance_plan,
    _apply_mechanical_guard,
    _apply_retry_limit,
    _apply_stall_guard,
)
from .snapshot import (
    _error,
    _runtime_snapshot,
    _snapshot_update,
)
from .tool_runner import (
    unconfirmed_calls,
)


def reflection_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """反思：机械校验 + LLM 完成度判断（架构 §5.4、§11.2）。

    三层职责，顺序不能颠倒：

    1. **机械校验**（:mod:`local_agent.reflection.validators`）：证据是否存在、是否
       ``verified``、有哪些异常——``result_trust`` 只由它得出；
    2. **LLM 决策**（:mod:`local_agent.reflection.reflector`）：完成度、异常解释、计划调整与
       下一步动作；结构化输出失败时按机械结论兜底（``verified`` → ``finish``，否则 ``ask_user``）
       并记一条 ErrorEvent，让"解析失败"可见（架构 §5.4 要求降级明确）；
    3. **机械压制**：证据等级不是 ``verified`` 时 ``goal_achieved`` 一律压回 ``False`` 并改用
       ``ask_user``；``goal_achieved=False`` 时不允许用 ``finish`` 收尾；重试超过
       ``agent.max_retries_per_step`` 时同样改为 ``ask_user``。规范化说明写进 ``decision_trace``。

    ``final_evidence`` 按架构 §4.2 从**当前任务**的工具事件重算后整体覆盖：历史任务的事件与
    错误不再混进本轮证据（作用域见 :func:`current_task_scope`）。
    """
    context = runtime.context
    if state.get("task_status") == "cancelled":
        return {}
    if _is_cancelled(context):
        return _cancelled_turn_update(state, context, partial_text=str(state.get("final_response") or ""))
    if state.get("agent_loop_version") == 1 and state.get("mode") != "chat":
        events = [event for event in state.get("tool_events", []) if event.run_id == state.get("run_id")]
        execution_feedback = [f"{event.tool_name}: {event.status}" for event in events if event.status != "ok"]
        return checkpoint_main_loop(
            state, trust="uncertain", anomalies=execution_feedback, evidence=[],
            pending_uncertain=[], max_retries=context.app_config.agent.max_retries_per_step,
        )
    if state.get("mode") == "chat":
        # 闲聊分支的唯一出口：按架构 §5.2 走同一个 reflection_node 校验记忆意图（详见
        # :func:`_chat_memory_reflection`），不参与下面的工具证据校验与完成度判定。
        # 用精确比较而不是默认值：没有 ``mode`` 的 State（直接调用节点的单测）应按工具分支处理。
        return _chat_memory_reflection(state, context)
    scope = current_task_scope(state)
    records = {
        record["tool_call_id"]: record
        for record in state.get("tool_results", [])
        if _in_current_task(record.get("run_id"), scope=scope)
    }
    payloads = _tool_payloads(state.get("messages", []))
    # 每个工具只保留**最后一次**的校验结果（见下面 trust 的说明）；证据列表仍全量重算。
    latest_by_tool: dict[str, ToolEvidence] = {}
    anomalies: list[str] = []
    evidences: list[EvidenceReference] = []
    summaries: list[str] = []

    for event in state.get("tool_events", []):
        if not _in_current_task(event.run_id, scope=scope):
            continue
        if state.get("agent_loop_version") == 1 and event.tool_name in {"update_plan", "task_control"}:
            continue  # 控制工具不是任务证据，不能凭计划更新证明工作完成。
        record = records.get(event.tool_call_id)
        if record is None:
            continue
        if record.get("status") == "denied":
            # **被拒绝的调用没有执行，也就没有证据**：它既不是"证据失败"，更不该把整轮判成"拿不到
            # 证据"。（2026-10-14 实测：一句被拒的 `time /t` 让整轮挂上「机械校验未通过」红卡，而事实
            # 是"这次调用没执行"。）拒绝事实已由 `error_events` 与对话里的拒绝消息如实记录，不靠这里。
            continue
        check = validate_tool_event(
            event, record, payload=payloads.get(event.tool_call_id)
        )
        latest_by_tool[event.tool_name] = check
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
        # 证据必须**可判读**：工具输出是 JSON（元数据在前、正文在后），只取前 N 个字符会让模型
        # 看到 path/line_count 就以为"正文被截断、任务没完成"（实测因此连续空转并反过来问用户）。
        # 这里按可判读摘要渲染，截断时带明确标注（架构 §5.4、:mod:`local_agent.graph.evidence`）。
        summaries.append(
            tool_evidence_text(
                event.tool_name,
                str(event.status),
                payloads.get(event.tool_call_id) or event.output_preview,
                limit=TOOL_EVIDENCE_CHARS,
            )
        )

    # ``tool_events`` 覆盖整个任务，所以直接汇总全量会让**第二步的失败永久留在集合里**：
    # 即使重试或换了做法已经成功，"第二步失败、调整后成功"也一辈子判不成完成（复审实证）。
    # 因此按**工具名**取最后一次结果——它表达的正是"后续尝试有没有修复这个工具的失败"。
    # 注意只影响整体可信度：``final_evidence`` 仍按 §4.2 全量重算，异常清单也照旧全量记录。
    trust = summarize_trust(list(latest_by_tool.values()))
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

    if state.get("agent_loop_version") == 1:
        checkpoint = checkpoint_main_loop(
            state, trust=trust, anomalies=anomalies, evidence=unique_evidence,
            pending_uncertain=pending_uncertain,
            max_retries=context.app_config.agent.max_retries_per_step,
        )
        checkpoint["decision_trace"] = [_decision(
            state, stage="mechanical_check",
            summary=f"执行结果 {trust}；证据 {len(unique_evidence)} 条；"
            f"本轮状态 {checkpoint.get('task_status')}；计划由主模型显式维护",
        )]
        if trust == "failed":
            checkpoint["error_events"] = [_error(
                state, stage=f"{REFLECTION_NODE}:mechanical",
                message=MECHANICAL_FAILURE_MESSAGE,
            )]
        return checkpoint

    # 成本：机械结论已经够用时不花这一次模型调用（2026-09-30，见 :func:`_llm_reflection_needed`）。
    reflection_needed = _llm_reflection_needed(state, trust=trust, anomalies=anomalies)
    if reflection_needed:
        decision, failure = _reflect_decision(
            state, context, trust=trust, anomalies=anomalies, summaries=summaries
        )
    else:
        decision, failure = _skipped_reflection_decision(trust), None
    goal_achieved, next_action, notes = _apply_mechanical_guard(trust, decision)
    next_action, retry_note = _apply_retry_limit(
        state, next_action, max_retries=context.app_config.agent.max_retries_per_step
    )
    if retry_note:
        notes.append(retry_note)
    if failure is not None:
        notes.append(f"反思决策不可用（{failure}），已按机械结论兜底")
    elif not reflection_needed:
        notes.append(REFLECTION_SKIPPED_NOTE)
    # T20：未确认的副作用一律不得自动重放（架构 §4.2/§251）——改成请用户核对文件或命令影响。
    if pending_uncertain:
        goal_achieved = False
        next_action = "ask_user"
        decision = decision.model_copy(
            update={
                "ask_user_reason": (
                    f"上一次运行里有 {len(pending_uncertain)} 个调用已放行但没有确认结果"
                    f"（{'、'.join(pending_uncertain)}），可能有未确认的副作用。"
                    "请先核对文件或命令的实际影响，再决定是否继续。"
                )
            }
        )
        notes.append("存在未确认的副作用：不自动重放，改为请用户核对")
    # T20：推进计划（完成一步不等于完成整个任务；重规划必须真的改计划）
    progress = _advance_plan(
        state, goal_achieved=goal_achieved, next_action=next_action, decision=decision
    )
    next_action = progress.next_action
    # 计划没走完时 `goal_achieved` 被压回 False：任务状态不能因为"第一步做完"就显示完成
    goal_achieved = progress.goal_achieved
    if progress.note:
        notes.append(progress.note)
    # 空转保护（架构 §5.3）：连续多轮没有任何可执行动作时，再调一次模型不会有新信息。
    # 必须放在 `_advance_plan` 之后——它自己也会把 finish 归一成 continue（计划还有步骤）。
    next_action, decision, plan, stall_note = _apply_stall_guard(
        state, progress, decision, trust=trust
    )
    if stall_note:
        notes.append(stall_note)
        # 交付分支（``finish``）把 ``goal_achieved`` 置真：它只在"机械证据通过校验且已有结论"时
        # 成立（见 :func:`_apply_stall_guard`），因此不违反 §5.4 的错误宣称门槛。
        # ``ask_user`` 分支相反——空转保护问的是"下一步做什么"，这一步没有做完，状态不能显示完成。
        goal_achieved = next_action == "finish"

    # ask_user / finish 直接进 final_response_node，而 final_response_node 只取已有 AI 文本：
    # 不在这里补齐可展示的回答，这两个分支会得到 final_response=None + failed（复审实测）。
    endings, final_answer = _ending(
        state,
        context,
        next_action=next_action,
        decision=decision,
        trust=trust,
        anomalies=anomalies,
    )
    if final_answer is not None and final_answer.cancelled:
        # 取消：合成调用被中途断流（§17.4）。终态**在这里直接落下**，不再走下面的完成度判定——
        # 否则一段被截断的回答会被当成"本轮完成"。
        return _cancelled_turn_update(
            state, context, partial_text=final_answer.message.text, partial_message=final_answer.message, snapshot=final_answer.snapshot
        )

    summary = (
        f"结果可信度 {trust}；动作 {next_action}；证据 {len(unique_evidence)} 条；"
        f"异常 {len(anomalies)} 条"
    )
    if notes:
        summary = f"{summary}；{'；'.join(notes)}"
    trace = _decision(state, stage="reflection", summary=summary)

    update: AgentState = {
        "final_evidence": unique_evidence,
        "reflection_result": ReflectionResult(
            goal_achieved=goal_achieved,
            result_trust=trust,
            anomalies=tuple(anomalies),
            plan_adjustment=decision.plan_adjustment.strip() or None,
            next_action=next_action,
            memory_candidates=tuple(
                _memory_candidates(getattr(decision, "memory_candidates", None), state)
            ),
            evidence=tuple(unique_evidence),
        ),
        "plan": plan,
        "current_step_index": progress.current_step_index,
        "retry_count": int(state.get("retry_count", 0))
        + (1 if next_action == "retry" else 0),
        "decision_trace": merge_appended(
            state.get("decision_trace", []), [trace], decision_record_key
        ),
    }
    if progress.completed_steps:
        # 只追加、不清空（追加型 reducer）；步骤 id 含 run_id，历史任务的步骤不会命中新任务
        update["completed_steps"] = list(progress.completed_steps)
    if endings:
        update["messages"] = endings
    if final_answer is not None:
        # 收尾合成也是一次出站 Prompt：把它的估算写回，State 自报的预算才与真实请求一致
        update["token_usage"] = final_answer.usage
        update["context_budget"] = final_answer.budget
        if final_answer.snapshot is not None:
            # 收尾合成用的快照同样要落盘：否则"模型看到的动态上下文"在这一轮就没有持久来源，
            # 重启后无法字节级重建同一请求（§15.7）。按 snapshot_id 去重，内容未变不重复追加。
            update.update(_snapshot_update(state, final_answer.snapshot))

    # 同一轮的机械失败、解析失败与未确认副作用都要各留一条事件：**先收集、最后只 merge 一次**。
    # 逐次 merge 会互相覆盖——`_error` 的事件 ID 由 stage + loop_count 派生，三类同轮事件在同一个
    # stage 下 ID 相同，后写的会把先写的挤掉（复审实测：机械失败 + 解析失败最终 0 条）。
    errors: list[ErrorEvent] = []
    if trust == "failed":
        errors.append(
            _error(
                state,
                stage=f"{REFLECTION_NODE}:mechanical",
                message=MECHANICAL_FAILURE_MESSAGE,
            )
        )
    if failure is not None:
        errors.append(
            _error(
                state,
                stage=f"{REFLECTION_NODE}:llm",
                message=f"反思决策结构化输出失败（{failure}），已按机械结论兜底",
            )
        )
    if final_answer is not None and final_answer.failure is not None:
        errors.append(
            _error(
                state,
                stage=f"{REFLECTION_NODE}:final_answer",
                message=(
                    f"最终回答合成降级（{final_answer.failure}），已改用确定性结果摘要"
                ),
            )
        )
    if final_answer is not None and final_answer.stream_failure is not None:
        # 流式中途失败是**可见**降级：页面上的不完整投影已丢弃、普通调用重试过一次，
        # 用户需要知道"这次是退化成一次性返回的"（T23 验收：中途失败有错误事件）
        errors.append(
            _error(
                state,
                stage=f"{REFLECTION_NODE}:stream",
                message=(
                    f"流式回答降级（{final_answer.stream_failure}）：已丢弃页面上的不完整投影，"
                    "改用一次普通完整调用"
                ),
            )
        )
    if pending_uncertain:
        errors.append(
            _error(
                state,
                stage=f"{REFLECTION_NODE}:side_effect",
                message=f"存在未确认的副作用：{'、'.join(pending_uncertain)}，不自动重放",
            )
        )
    if errors:
        update["error_events"] = merge_appended(
            state.get("error_events", []), errors, error_event_key
        )
    return update


#: 跳过一次模型反思时记进决策轨迹的说明（§15.8：可验证的执行过程要能看见这件事发生过）。
REFLECTION_SKIPPED_NOTE = "机械结论已足够（证据通过校验、无异常、尚未写出结论），本步不调用模型反思"


def _llm_reflection_needed(
    state: AgentState, *, trust: EvidenceLevel, anomalies: Sequence[str]
) -> bool:
    """这一步是否**值得**花一次模型调用来反思（2026-09-30，成本）。

    为什么要有这个判断：实测一次提问产生 39 次调用，其中 **19 次是反思**；窗口成本压下来之后，
    反思仍占 17 次里的 8 次，而且它们的缓存读**恒为 640**——反思提示是另一套消息形状
    （``[系统指令, 事实]``），与主对话不共享前缀，所以每一次几乎都是全价输入。

    什么时候机械结论已经够用：**证据通过校验、没有异常、模型还没写出结论，而且当前步骤之后还有
    未完成的步骤**。

    最后那条"还有后续步骤"是这轮试出来的关键约束（第一版漏了它，测试当场红了 11 条）：只有在这一支里，
    :func:`_advance_plan` 才**必然**走"计划没走完 → 把 ``finish`` 压回 ``continue``"那条路
    （见 :func:`_delivered_answer`），机械结论与模型结论在 ``next_action`` 上**不可能分叉**。当前步骤
    是最后一步时收尾判断必须交给模型——那一支会让整个任务结束，而机械层判不了"任务目标真的达成了吗"。

    而重规划真正需要的场景——计划不适用、工具反复失败、结果可疑——都会先让证据校验不过或产生异常，
    那时这里立刻恢复调用。**这个条件是自限的**，不需要额外的计数器。

    仍然调用模型的情况：证据没过校验、有异常（含待确认的副作用）、已经写出结论、当前步骤是最后一步
    或没有后续步骤（收尾时刻，完成度判断必须由模型给出）。
    """
    if trust != "verified" or anomalies:
        return True
    if _delivered_answer(state):
        return True
    plan = list(state.get("plan") or [])
    index = int(state.get("current_step_index", 0))
    return not any(step.status != "completed" for step in plan[index + 1 :])


def _skipped_reflection_decision(trust: EvidenceLevel) -> ReflectionDecision:
    """跳过一次模型反思时使用的机械结论。

    **必须判 ``goal_achieved=True``**（在 ``trust == "verified"`` 的前提下）。这里踩过一次坑，记下来：
    第一版写的是 ``goal_achieved=False, next_action="continue"``，理由是"别让模型没写出结论就收尾"。
    但 ``goal_achieved`` 不只表示"任务完成"——它同时是 :func:`_advance_plan` 用来**推进计划步骤**的
    开关（这正是该字段"步骤/任务"两种含义压在一起的老问题）。压成 False 的后果是计划**永远不推进**：
    当前步骤一直是 ``running``，后面的步骤永远 ``pending``，任务原地打转——恰好是空转保护要防的形态。
    测试当场抓到（``test_plan_still_continues_when_no_answer_was_delivered`` 期望
    ``["completed", "running"]`` 却拿到 ``["running", "pending"]``）。

    正确口径与证据等级一致：``verified`` 表示**本步的工具结果通过校验**，也就是本步做完了。至于
    "整个任务是否收尾"，:func:`_advance_plan` 另有规则——计划还有步骤、或模型还没写出可交付结论时，
    它会把 ``finish`` 压回 ``continue``（§5.3）。所以这里只如实报"本步完成"，收尾与否交给那一层。
    """
    return ReflectionDecision(
        goal_achieved=trust == "verified",
        next_action="finish" if trust == "verified" else "ask_user",
    )


def _reflect_decision(
    state: AgentState,
    context: RuntimeContext,
    *,
    trust: EvidenceLevel,
    anomalies: Sequence[str],
    summaries: Sequence[str],
) -> tuple[ReflectionDecision, str | None]:
    """调用 LLM 反思；失败时按机械结论兜底（第二个返回值是失败类型）。"""
    capabilities = context.model_capabilities
    # 共享主对话前缀：反思请求接在工具决策那一段之后，供应商的前缀缓存才用得上（见
    # :func:`_main_conversation_prefix`）。快照取自 State 的冻结记录，与决策那次是同一份。
    prefix = _main_conversation_prefix(state, context, _runtime_snapshot(state, context))
    try:
        decision = reflect(
            context.model_adapter,
            prefix=prefix,
            task_goal=str(state.get("task_goal") or state.get("normalized_request") or ""),
            plan=state.get("plan") or [],
            current_step_index=int(state.get("current_step_index", 0)),
            trust=trust,
            anomalies=anomalies,
            tool_summaries=_bounded_summaries(summaries),
            # 让反思看到"模型本轮已经答过什么"：否则它会一边要求继续、一边忽略已经得出的答案
            # （实测：模型已按工具结果答完并给出结论，反思仍连续要求继续，最后变成向用户追问）。
            last_answer=_last_answer_text(state),
            loop_count=int(state.get("loop_count", 0)),
            max_loop_count=int(state.get("max_loop_count") or 0),
            capabilities=capabilities if isinstance(capabilities, ModelCapabilities) else None,
        )
    except Exception as exc:  # noqa: BLE001 — 反思不可用时按机械结论兜底，不让本轮失败
        return _mechanical_fallback(trust), type(exc).__name__
    return decision, None


def _mechanical_fallback(trust: EvidenceLevel) -> ReflectionDecision:
    """结构化输出失败时的兜底：机械证据充分视为完成，否则交回用户。"""
    if trust == "verified":
        return ReflectionDecision(
            goal_achieved=True,
            next_action="finish",
            anomaly_explanation="反思决策不可用，按机械校验结论结束",
        )
    return ReflectionDecision(
        goal_achieved=False,
        next_action="ask_user",
        ask_user_reason="反思决策不可用（结构化输出失败），需要你确认当前结果",
    )


#: 反思提示里「可读工具证据」的字符预算（只保留**最近的**若干条）。
#:
#: 2026-09-30 新增。此前这段是**全量**渲染：``tool_events`` 覆盖整个任务，于是反思提示每一步都更
#: 长，整轮成本按**平方**增长——实测一个 8 步循环里 8 次反思分别带 7k–12k token，而它们**全部未
#: 命中前缀缓存**（反思请求只有 ``[系统提示, facts]`` 两条消息、不含对话历史，因此只有那 640
#: token 的系统提示能复用），8 次合计 65,254 未命中输入，占该轮 53%。裁到预算后这段不再随步数
#: 增长。
#:
#: **不改变机械结论**：判"证据等级"的是全量的 :func:`summarize_trust`，异常清单照旧全量，
#: ``final_evidence`` 另按 §4.2 全量重算。这里只是模型解释异常时能看到的**原文片段**变少。
REFLECTION_EVIDENCE_CHARS = 12_000


def _bounded_summaries(summaries: Sequence[str]) -> list[str]:
    """保留最近若干条工具证据摘要，总长不超过 :data:`REFLECTION_EVIDENCE_CHARS`。

    至少保留**最后一条**：当前步的结果正是这次反思要判的对象，不能因为超预算把它丢掉。
    """
    kept: list[str] = []
    used = 0
    for text in reversed(list(summaries)):
        if kept and used + len(text) > REFLECTION_EVIDENCE_CHARS:
            break
        kept.append(text)
        used += len(text)
    kept.reverse()
    return kept


#: 机械结论为 ``failed`` 时那条错误事件的正文（**用户可见**）。
#:
#: 为什么单独提出来：`projection.py` 把这条事件的 ``message`` **原样**给用户看（只换 title/hint），
#: 所以文案写在节点里就等于印在页面上。2026-10-15 实测：一轮"百度热榜"回答完整、却在 `web_fetch`
#: 续读被版本校验拒掉的那一步留下了「机械校验未通过：本轮工具结果不能作为已验证证据」——用户拿到的是
#: 我们的评级名。提成常量也让"两处分支写同一句话"不再各写一份（曾经就是两份字面量）。
MECHANICAL_FAILURE_MESSAGE: Final[str] = "本轮有工具结果没有核对通过，不能作为回答的依据"


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


def _evidence_type(tool_name: str) -> EvidenceType:
    """工具产出对应的证据类型（架构 §4.3 的取值集合）。"""
    if tool_name == "read_file":
        return "file_hash"
    if tool_name in {"edit_file", "write_file"}:
        return "diff"
    if tool_name == "run_shell":
        return "exit_code"
    return "stdout"

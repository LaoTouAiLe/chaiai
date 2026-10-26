"""最终回答、停止说明与回答发布；依赖公共上下文层，不反向导入 nodes。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.runtime import Runtime

from local_agent.context.citations import complete_citation_paths
from local_agent.context.token_budget import (
    ContextBudget,
    TokenUsage,
    estimate_message_tokens,
    estimate_messages_tokens,
    prompt_budget_tokens,
    usage_ratio,
)
from local_agent.context.web_citations import resolve_web_citations
from local_agent.graph.evidence import tool_evidence_text
from local_agent.graph.projection import (
    current_turn_ai_message,
    message_id,
)
from local_agent.graph.routers import (
    LOOP_LIMIT_REPLY,
    LOOPING_ACTIONS,
    loop_limit_reached,
)
from local_agent.graph.state import (
    AgentState,
    ReflectionResult,
    RuntimeContext,
    RuntimeContextSnapshot,
    TaskStatus,
)
from local_agent.llm.usage_recording import use_model_purpose
from local_agent.reflection.reflector import (
    TOOL_DATA_CLOSE,
    TOOL_DATA_OPEN,
    ReflectionDecision,
)
from local_agent.reflection.validators import EvidenceLevel
from local_agent.tools.result_types import ToolResultRecord
from local_agent.ui.streaming import stream_answer

from .common import (
    _cancelled_turn_update,
    _decision,
    _in_current_task,
    _is_cancelled,
    _model_capabilities,
    _tool_payloads,
    current_task_scope,
)
from .names import (
    REFLECTION_NODE,
    TOOL_FEEDBACK_NODE,
    ReflectionAction,
)
from .snapshot import (
    _bind_snapshot,
    _error,
    _prompt_window,
    _recall_failed,
    _runtime_snapshot,
    _snapshot_update,
    _stable_prompt,
    _with_snapshot,
)

#: ``finish`` 分支的合成指令：要求不绑定工具地给出最终回答（架构 §5.3 的完成出口）。
#:
#: 2026-10-16：这一条原先还许可"或具体命令与其输出"作为依据，于是模型把 `time /t` 的命令与退出码
#: 一起抄进回答（取事实的手段被当成结论）。写法的定义（只说结果意味着什么、不抄原始形式）统一由
#: 固定提示的「输出要求与证据」承担，这里只留结论/依据顺序与 `路径:行号` 的形状，不再各写一份
#: （两份字面量必然漂移——`MECHANICAL_FAILURE_MESSAGE` 就是这么提出来的一份）。
FINAL_ANSWER_INSTRUCTION = (
    "请基于上面的任务、计划与工具结果，直接给出本轮的最终回答。\n"
    "写法要求：\n"
    "1. 先给结论，再给依据；依据要落到具体文件与行号（写成 `路径:行号`）。\n"
    "2. 有失败或未核实的部分，只用一句**面向用户**的话说明（例如「service.py 不在本次读取范围，"
    "这一段未核实」），不要展开成自审报告。\n"
    "3. **不要向用户复述你和工具之间的中间状况**：工具输出被截断了多少、还差哪些证据、"
    "你打算怎么补救、你此前说过什么——这些都不进回答。\n"
    "4. 不要用第三人称谈论自己：不写「模型声称」「模型已承认」这类句子，直接陈述结论。\n"
    "5. 不要出现内部术语与机制名词：工具结果、证据等级、上下文预算、循环次数、截断、注入、"
    "子代理、Checkpoint。\n"
    "不要调用任何工具。"
)


TOOL_FEEDBACK_INSTRUCTION = (
    "本轮工具调用被用户拒绝、被策略阻止或连续失败，现仅生成面向用户的回答。"
    "请结合原问题和实际返回的拒绝/失败原因，说明哪些操作没有执行、哪些信息未能核实；"
    "能根据已有可靠信息回答的部分继续回答，并给出可行的下一步。"
    "不能把联网失败说成已搜索成功，不能声称被拒绝的动作已完成。"
    "不要再次发起工具调用、请求同一项审批或绕过安全限制。"
)


LOOP_LIMIT_FEEDBACK_INSTRUCTION = (
    "本轮已达到模型调用上限，现仅生成进度说明，不再执行工具。"
    "依据任务、计划和实际结果，简要说明已完成的内容、剩余工作及未验证的部分。"
    "停止原因是调用上限，不能推断为用户拒绝、权限不足或需要重新授权。"
    "确有失败或拒绝时如实说明；已经通过后续操作恢复的历史错误，不得再说成当前阻塞。"
    "不要把未完成的任务说成全部完成，也不要要求用户重复需求或重新批准已成功的操作。"
    "不要调用任何工具。"
)


#: 超预算时合成输入里的证据条数与单条长度上限（有界，避免把大工具输出整段发给模型）。
#:
#: 2026-09-30 由 ``400`` 提到 ``4000``：原来的 400 字符只够看到正文的头几行，最终回答因此
#: 只能给到"文件级"引用，还给不出行号（对照实测）。收敛逻辑不变——放不下时按 ``//2`` 逐档
#: 缩到 :data:`MIN_SYNTHESIS_EVIDENCE_CHARS`，再不够就减少条数，所以抬高起点不会凭空超预算。
MAX_SYNTHESIS_EVIDENCE_ITEMS = 5


MAX_SYNTHESIS_EVIDENCE_CHARS = 4000


#: 收敛时单条摘要的长度下限：再小就不再截短，改为减少条数。
MIN_SYNTHESIS_EVIDENCE_CHARS = 40


#: 确定性结果摘要里最多列几条工具结果（与合成证据一样只取**最近**的若干条）。
DETERMINISTIC_SUMMARY_RECORDS = 5


#: 确定性结果摘要里每条工具证据的字符上限（含工具名前缀）。
#:
#: 2026-09-30 由 ``200`` 提到 ``800``：这是模型不可用时的**兜底**摘要，同样要能看出工具做了什么，
#: 200 字符只够留下元数据。
DETERMINISTIC_SUMMARY_CHARS = 800


@dataclass(frozen=True)
class _FinalAnswer:
    """``finish`` 分支的合成结果：回答、这次调用的预算快照，以及降级信息。"""

    message: AIMessage
    usage: TokenUsage
    budget: ContextBudget
    failure: str | None = None
    #: 流式中途失败的异常类型（"已丢弃不完整投影 + 普通调用重试一次"）；调用方要记可见事件
    stream_failure: str | None = None
    response: AIMessage | None = None
    bounded: bool = False
    #: 本次合成所用的运行上下文快照（§15.7）。由调用方按 snapshot_id 去重追加进 State，
    #: 保证"模型看到的动态上下文"可从持久状态重建；``None`` 表示合成路径没有动态上下文。
    snapshot: RuntimeContextSnapshot | None = None
    #: 这次合成被取消（§17.4）：调用方据此把这一轮落成终态 `cancelled`，而不是当成"回答完成"。
    cancelled: bool = False


def _ending(
    state: AgentState,
    context: RuntimeContext,
    *,
    next_action: ReflectionAction,
    decision: ReflectionDecision,
    trust: EvidenceLevel,
    anomalies: Sequence[str],
) -> tuple[list[AnyMessage], _FinalAnswer | None]:
    """为 ``ask_user`` / ``finish`` 分支准备可展示的回答（空列表表示无需补）。

    为什么必需：这两个分支直接进入 :func:`final_response_node`，而它只从消息里取**最后一条
    AI 文本**——此时最后一条 AI 消息往往还是"提出工具调用"的那条（content 为空），于是
    ``final_response`` 变成 ``None`` 且任务被标成 ``failed``（复审实测）。

    - ``ask_user``：用 ``ask_user_reason`` 生成**确定性追问**（含证据不足说明），不调用模型；
    - ``finish``：让模型**不绑定工具**地合成一次最终回答，同时返回这次调用的预算快照，
      由调用方写回 State（否则 ``context_budget`` 会停留在压缩节点那一轮的旧值）。
      模型不可用时退化为确定性结果摘要。

    ``anomaly_explanation`` **不进入面向用户的追问文本**（见 :func:`_ask_user_text` 的说明），
    它仍保留在 ``reflection_result`` 里供审计。
    """
    if next_action == "ask_user":
        return [AIMessage(content=_ask_user_text(decision, trust=trust))], None
    if next_action == "finish":
        answer = _synthesize_final_answer(
            state, context, decision=decision, trust=trust, anomalies=anomalies
        )
        return [answer.message], answer
    return [], None


#: 面向用户的"结果核对"说明（2026-09-30）。
#:
#: 为什么不直接写证据等级：原来两处面向用户的文字都在打内部术语——追问里写"没有通过机械校验"、
#: 兜底摘要里写"机械证据等级：verified"。前者是内部概念，后者还顺手抛了英文枚举值，用户拿到的是
#: 我们的评级名而不是"这个结论能不能信"。措辞按**能不能当依据**说，与 §15.8 的
#: "只展示可验证的执行过程摘要"一致。
_TRUST_NOTES: dict[str, str] = {
    "verified": "工具结果已核对、可以作为依据",
    "uncertain": "部分工具结果还没能核对（结论仅供参考）",
    "failed": "工具结果没有核对通过",
}


def _trust_note(trust: EvidenceLevel) -> str:
    """把内部证据等级翻成用户看得懂的一句话；未知取值不编造，如实说"没评估"。"""
    return _TRUST_NOTES.get(str(trust), "本次结果的可靠性还没有评估")


def _ask_user_text(decision: ReflectionDecision, *, trust: EvidenceLevel) -> str:
    """把 ``ask_user_reason`` 变成用户看得懂的追问（确定性文本，不额外调用模型）。

    **不把 ``anomaly_explanation`` 拼进来**（2026-09-30 修）。它是反思模型写给**内部判断**的
    第三人称自审——字段说明就是"异常原因解释；没有异常时留空"——却被原样发给了用户。实测原文：

        当前情况：本轮无命令失败或权限错误，但有几处工具层面的输出限制需要留意：…
        当前情况：问题出在模型回答的行为：它把本应执行的最后一步（汇总并输出架构说明）
        替换成了「你想看哪一块」的选项追问，实际未产出任何完整架构说明。

    用户看到的是"这个程序在向我解释自己哪一步没做对"：不可执行、含内部术语，还暴露了本不该
    外显的机制（§15.8 只展示**可验证的执行过程**摘要）。用户需要的是两件事——**要我做什么**
    （``ask_user_reason``，本来就是面向用户的字段）与**有什么确实出了问题**（机械异常，由代码
    产生、可核对）。两者都不需要模型的自审原文，因此这里不再拼接它；自审仍保留在
    ``reflection_result`` 里供审计，没有丢。
    """
    reason = decision.ask_user_reason.strip() or "需要你补充信息或确认结果后才能继续"
    parts = [f"我需要你确认一下：{reason}"]
    if trust != "verified":
        parts.append(f"（{_trust_note(trust)}，所以我不会声称任务已经完成。）")
    return "\n\n".join(parts)


def _synthesize_final_answer(
    state: AgentState,
    context: RuntimeContext,
    *,
    decision: ReflectionDecision,
    trust: EvidenceLevel,
    anomalies: Sequence[str],
    instruction: str = FINAL_ANSWER_INSTRUCTION,
) -> _FinalAnswer:
    """``finish`` 分支的最终回答：一次**不绑定工具**的模型调用，失败时退化为确定性摘要。

    与普通出站一样**先过预算**：这次调用发生在工具执行**之后**，窗口里带着刚产生的工具结果，
    而 State 里的 ``context_budget`` 还是工具执行前的旧快照；大输出或多工具调用会让收尾调用
    超上下文，也会让"State 自报预算"与真实出站 Prompt 对不上（复审实证）。因此：

    1. 按 ``context_window_tokens - max_tokens`` 估算"稳定系统前缀 + 运行上下文快照 + 窗口 + 指令"
       （快照与指令也要算）；
    2. 超预算时退化为**有界的证据摘要**（见 :func:`_bounded_evidence`）：按"预算 − 稳定前缀
       − 快照 − 指令"的剩余额度逐步收敛，包在 ``<<<TOOL_OUTPUT>>>`` 里标注为不可信数据；
    3. 连"稳定前缀 + 快照 + 指令"本身都放不下时不调用模型，直接给确定性摘要——**绝不发出超预算请求**；
    4. 无论走哪条路，都把这次调用的估算作为预算快照返回，由调用方写回 State。
    """
    config = context.app_config
    prompt = _stable_prompt(state, context)
    window = _prompt_window(state, context)
    snapshot = _runtime_snapshot(state, context)
    snapshot_tokens = estimate_message_tokens(HumanMessage(content=snapshot.canonical_text))
    _bind_snapshot(snapshot)
    budget_tokens = prompt_budget_tokens(
        context_window_tokens=config.llm.context_window_tokens,
        completion_reserve_tokens=config.llm.max_tokens,
    )
    # 指令也是以消息形式出站的：用与外层集合完全相同的口径（含消息包装开销），否则评估会稳定
    # 少算一小截，"这次调用在预算内"就不再可靠。
    instruction_tokens = estimate_message_tokens(
        SystemMessage(content=instruction)
    )
    window_tokens = estimate_messages_tokens(window.messages)
    estimated = prompt.estimated_tokens + snapshot_tokens + window_tokens + instruction_tokens
    window_count = len(window.messages)
    bounded = estimated > budget_tokens
    messages: list[AnyMessage] = []
    if bounded:
        allowance = budget_tokens - prompt.estimated_tokens - snapshot_tokens - instruction_tokens
        evidence = (
            _bounded_evidence(state, allowance_tokens=allowance) if allowance > 0 else None
        )
        if evidence is None:
            # 固定部分就放不下：连证据都不发，走确定性摘要（空 messages 即"不调用模型"）
            window_tokens = 0
            window_count = 0
            estimated = prompt.estimated_tokens + snapshot_tokens + instruction_tokens
        else:
            window_tokens = estimate_messages_tokens([HumanMessage(content=evidence)])
            window_count = 1
            estimated = prompt.estimated_tokens + snapshot_tokens + window_tokens + instruction_tokens
            # 收尾合成同样要看到记忆 / RAG / 计划（停止条件：这些信息不得变得不可见），
            # 因此有界路径也带上快照块，只把窗口换成有界证据。
            messages = [
                SystemMessage(content=prompt.system_prompt),
                HumanMessage(content=snapshot.canonical_text),
                HumanMessage(content=evidence),
                SystemMessage(content=instruction),
            ]
    else:
        messages = [
            SystemMessage(content=prompt.system_prompt),
            *_with_snapshot(window.messages, snapshot, state),
            SystemMessage(content=instruction),
        ]

    ratio = usage_ratio(estimated_prompt_tokens=estimated, prompt_budget_tokens=budget_tokens)
    usage = TokenUsage(
        prompt_tokens=estimated,
        # 指令本身也是一条 SystemMessage：必须算进系统提示分项，否则分项之和比总量少指令那部分，
        # 违反 "各分项之和 = prompt_tokens" 的不变量（复审实测少 95 token）。注入的快照块同理。
        system_prompt_tokens=prompt.estimated_tokens + snapshot_tokens + instruction_tokens,
        window_messages_tokens=window_tokens,
        completion_reserve_tokens=config.llm.max_tokens,
        tool_definition_tokens=0,  # 收尾合成不绑定工具
    )
    budget = ContextBudget(
        context_window_tokens=config.llm.context_window_tokens,
        completion_reserve_tokens=config.llm.max_tokens,
        prompt_budget_tokens=budget_tokens,
        estimated_prompt_tokens=estimated,
        usage_ratio=round(ratio, 6),
        trigger_ratio=config.memory.compression.trigger_ratio,
        # 这是一次收尾调用、不是压缩节点那一轮：triggered 只表示"本轮触发过摘要压缩"
        triggered=False,
        window_messages=window_count,
        total_messages=window.total_messages,
        summary_chars=len((state.get("conversation_summary") or "").strip()),
        summary_covered_messages=window.covered_messages,
    )

    failure: str | None = None
    if not messages:
        # 预算连固定部分都放不下：不发请求（真实模型会拒绝），改用确定性摘要并记降级事件
        failure = "PromptBudgetExceeded"
        return _FinalAnswer(
            message=AIMessage(
                content=_deterministic_summary(
                    state, decision=decision, trust=trust, anomalies=anomalies, failure=failure
                )
            ),
            usage=usage,
            budget=budget,
            failure=failure,
            bounded=True,
            snapshot=snapshot,
        )
    stream_failure: str | None = None
    response: AIMessage | None = None
    try:
        # 收尾合成是"最终回答"的另一个调用点：开了流式就增量展示（不逐块写 Checkpoint）；
        # 不支持 / 首块前失败自动退化，中途失败会丢弃不完整投影并普通重试一次（ui.streaming）。
        # 这里显式声明用途为最终回答（所在节点是 reflection，用途不能跟着节点名走）。
        with use_model_purpose("final_answer"):
            streamed = stream_answer(
                context.model_adapter,
                messages,
                capabilities=_model_capabilities(context),
                enabled=bool(config.llm.streaming),
                # 传输层流式（§11.7）：收尾合成同样是"在飞的调用"，取消要在块之间生效。
                cancellation=context.cancellation,
            )
        if streamed.cancelled:
            # 取消：把已产出的部分交回调用方落成终态。这里**不**降级成确定性摘要——
            # 用户要的是"停下来"，不是"换一条路继续把这轮做完"（§17.4）。
            return _FinalAnswer(
                message=streamed.message,
                usage=usage,
                budget=budget,
                failure=None,
                stream_failure=None,
                bounded=bounded,
                snapshot=snapshot,
                cancelled=True,
            )
        response = streamed.message
        stream_failure = streamed.failure
    except Exception as exc:  # noqa: BLE001 —— 合成失败也不能丢掉这一轮的结果
        failure = type(exc).__name__
        text = _deterministic_summary(
            state, decision=decision, trust=trust, anomalies=anomalies, failure=failure
        )
    else:
        answer = response.text.strip() if isinstance(response, AIMessage) else ""
        if not answer:
            # 空回答同样是降级：必须让调用方记事件，而不是静默换成摘要
            failure = "EmptyAnswer"
        text = answer or _deterministic_summary(
            state, decision=decision, trust=trust, anomalies=anomalies
        )
    return _FinalAnswer(
        message=AIMessage(content=text, id=response.id if response is not None else None),
        usage=usage,
        budget=budget,
        failure=failure,
        stream_failure=stream_failure,
        bounded=bounded,
        snapshot=snapshot,
    )


def _bounded_evidence(state: AgentState, *, allowance_tokens: int) -> str | None:
    """在 ``allowance_tokens`` 内构造证据摘要；连最小摘要都放不下时返回 ``None``。

    "有界"不能只是固定条数 + 固定长度：固定上限仍可能超预算（复审实测 1094 > 1024），真实模型
    会直接拒绝请求。这里按剩余额度**逐步收敛**——先截短每条，再减少条数，最后放弃；返回 ``None``
    表示由调用方改用确定性摘要（不再发出超预算请求）。

    每收敛一档都**重新渲染**证据行（而不是把长行截短）：截断标注必须留在行内，否则模型会把
    "摘要被截断"当成"工具没返回内容"（架构 §5.4）。
    """
    limit = MAX_SYNTHESIS_EVIDENCE_CHARS
    records = _recent_records(state, limit=MAX_SYNTHESIS_EVIDENCE_ITEMS)
    while True:
        text = _wrap_evidence(state, records, limit=limit)
        if estimate_messages_tokens([HumanMessage(content=text)]) <= allowance_tokens:
            return text
        if limit > MIN_SYNTHESIS_EVIDENCE_CHARS:
            limit //= 2
            continue
        if len(records) > 1:
            records = records[1:]  # 先丢最早的一条
            limit = MAX_SYNTHESIS_EVIDENCE_CHARS
            continue
        return None


def _wrap_evidence(
    state: AgentState, records: Sequence[ToolResultRecord], *, limit: int
) -> str:
    """把证据行包成带不可信标记的文本；每行按 ``limit`` **重新渲染**。

    渲染而不是"先拼好再截短"：截断标注必须留在行内，否则模型会把"摘要被截断"读成
    "工具没有返回内容"（架构 §5.4）。证据优先用 ``messages`` 里的**完整**工具输出渲染
    （``tool_results`` 里的 ``output_preview`` 只是前 2000 字符的头部），读不到时才退回 preview。
    """
    if not records:
        body = "（本轮没有工具结果）"
    else:
        payloads = _tool_payloads(state.get("messages", []))
        body = "\n".join(
            "- "
            + tool_evidence_text(
                str(record.get("tool_name")),
                str(record.get("status")),
                payloads.get(str(record.get("tool_call_id")))
                or str(record.get("output_preview") or ""),
                # 减去 "- " 前缀，整行不超过 limit
                limit=max(limit - 2, 0),
            )
            for record in records
        )
    return f"{TOOL_DATA_OPEN}\n{body}\n{TOOL_DATA_CLOSE}"


def _recent_records(state: AgentState, *, limit: int) -> list[ToolResultRecord]:
    """当前任务里**最近** ``limit`` 条工具结果（超出时丢最早的那些）。

    "收满就停"会留下最早的结果、丢掉最新的成功或最终验证（复审探针：7 条只返回 t1…t5），
    最终回答于是可能忽略刚做完的事。合成证据与确定性摘要共用本函数，口径一致。
    """
    if limit <= 0:
        return []
    scope = current_task_scope(state)
    records = [
        record
        for record in state.get("tool_results", [])
        if _in_current_task(record.get("run_id"), scope=scope)
    ]
    return records[-limit:]


def _deterministic_summary(
    state: AgentState,
    *,
    decision: ReflectionDecision,
    trust: EvidenceLevel,
    anomalies: Sequence[str],
    failure: str | None = None,
) -> str:
    """模型不可用时拼出的确定性结果摘要（保证 ``finish`` 始终有可展示的回答）。"""
    lines = ["本轮没有生成可用的模型回答，下面是确定性的结果摘要："]
    goal = str(state.get("task_goal") or state.get("normalized_request") or "").strip()
    if goal:
        lines.append(f"- 任务：{goal}")
    lines.append(f"- 结果核对：{_trust_note(trust)}")
    # 确定性摘要也是给人看的结果：同样用具名+截断标注的证据渲染，避免"只看到元数据"的错觉
    # （与反思、收尾合成共用同一层，见 :mod:`local_agent.graph.evidence`）。
    payloads = _tool_payloads(state.get("messages", []))
    for record in _recent_records(state, limit=DETERMINISTIC_SUMMARY_RECORDS):
        lines.append(
            "- "
            + tool_evidence_text(
                str(record.get("tool_name")),
                str(record.get("status")),
                payloads.get(str(record.get("tool_call_id")))
                or str(record.get("output_preview") or ""),
                limit=DETERMINISTIC_SUMMARY_CHARS,
            )
        )
    # ``anomaly_explanation`` **不进这里**（2026-09-30 修，与 :func:`_ask_user_text` 同一口径）：
    # 它是反思模型写给**内部判断**的第三人称自审，而这段摘要是**面向用户**的兜底回答。实测泄漏原文
    # 形如"问题出在模型回答的行为：它把本应执行的最后一步…替换成了…"。需要的事实由下面那行
    # ``机械校验异常``（代码产生、可核对）承担，不需要模型的自审原文。
    if anomalies:
        lines.append(f"- 检查出的问题：{'；'.join(str(item) for item in list(anomalies)[:3])}")
    if failure:
        lines.append(f"- 最终回答合成失败：{failure}")
    lines.append("- 以上结论未经你确认；如需继续，请补充说明。")
    return "\n".join(lines)


#: 回答里"请用户重述 / 追问"的典型措辞。中文措辞不可能穷尽，这里只作为**机械兜底**的触发条件：
#: 仅在"本轮召回失败"时生效，命中的代价是换成确定性说明（宁可少说，也不说错）。
_RESTATE_HINTS: tuple[str, ...] = (
    "直接告诉我",
    "可以告诉我",
    "你告诉我",
    "告诉我工具",
    "告诉我你",
    "再说一遍",
    "重新告诉",
    "请提供",
    "请补充",
    "你可以说",
    "麻烦告诉",
)


#: 召回失败且回答仍要求用户重述时，替换成的确定性说明。
_RECALL_FAILURE_REPLY = (
    "这次记忆检索没有成功，我无法确认你问的偏好或项目信息，所以不能给出结论。\n\n"
    "（这是本轮的检索故障，不需要你补充信息；之后可以让我重新检索一次。）"
)


def _guard_recall_failure_answer(state: AgentState, text: str) -> str:
    """召回失败时，禁止回答里出现"请用户重述偏好"这类建议。

    为什么必须是代码级约束：提示词里已经写明"不要追问、不要建议用户再说一遍"，但真实模型仍会附带
    "你也可以直接告诉我工具名"（复审实证）。中文措辞无法靠提示词穷尽，所以这里做机械兜底——
    与闲聊记忆写入失败时丢弃"我记住了"同一思路：宁可少说，也不说错。

    命中面很窄：**只有本轮召回失败**、且回答里出现重述/追问措辞时才整条替换；召回成功的轮次完全
    不受影响。
    """
    if not _recall_failed(state):
        return text
    if not any(hint in text for hint in _RESTATE_HINTS):
        return text
    return _RECALL_FAILURE_REPLY


def tool_feedback_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """向不绑定工具的模型交代真实停止原因，随后结束本轮。"""
    context = runtime.context
    if state.get("task_status") == "cancelled" or _is_cancelled(context):
        return _cancelled_turn_update(state, context)
    prior = state.get("reflection_result")
    limited = (
        loop_limit_reached(state) and isinstance(prior, ReflectionResult)
        and prior.next_action in LOOPING_ACTIONS
    )
    anomalies = list(prior.anomalies) if isinstance(prior, ReflectionResult) else []
    answer = _synthesize_final_answer(
        state, context,
        decision=ReflectionDecision(goal_achieved=False, next_action="ask_user"),
        trust="uncertain", anomalies=anomalies,
        instruction=LOOP_LIMIT_FEEDBACK_INSTRUCTION if limited else TOOL_FEEDBACK_INSTRUCTION,
    )
    if answer.cancelled:
        return _cancelled_turn_update(
            state, context, partial_text=answer.message.text,
            partial_message=answer.message, snapshot=answer.snapshot,
        )
    message = answer.message
    if limited:
        message = message.model_copy(update={
            "content": f"{LOOP_LIMIT_REPLY}\n\n{message.text}",
        })
    update: AgentState = {
        "messages": [message], "final_response": message.text,
        "pending_tool_calls": [], "deferred_tool_calls": [],
        "tool_feedback_required": False, "task_status": "failed" if limited else "blocked",
        "reflection_result": ReflectionResult(
            goal_achieved=False, result_trust="uncertain", next_action="ask_user",
            anomalies=tuple(anomalies),
        ),
        "token_usage": answer.usage, "context_budget": answer.budget,
        "decision_trace": [_decision(
            state, stage=TOOL_FEEDBACK_NODE,
            summary=("调用上限已达到，已生成进度说明，本轮不再执行工具" if limited else
                     "已将拒绝/失败结果回传模型生成说明，本轮不再执行工具"),
        )],
    }
    if answer.snapshot is not None:
        update.update(_snapshot_update(state, answer.snapshot))
    if answer.failure or answer.stream_failure:
        update["error_events"] = [_error(
            state, stage=f"{REFLECTION_NODE}:final_answer",
            message=f"工具结果说明生成降级（{answer.failure or answer.stream_failure}），已保留可展示的回答",
        )]
    return update


def final_response_node(state: AgentState) -> AgentState:
    """把最后一条有文本的 AI 消息定为最终回答。

    ``task_status`` 的更新规则（架构 §5.4：有回答不等于任务完成）：

    - 没有可用的 AI 文本 → ``failed``，由页面给出明确提示，而不是编造回答；
    - 已经是 ``failed``（例如超出循环上限）→ 保持 ``failed``，不改写成 ``completed``；
    - 反思结论存在但 ``goal_achieved=False``（未达成目标就结束，例如需要用户补充）→
      ``blocked``：**本轮已经结束**、回答有效，但不冒充 ``completed``。这里**不能**写
      ``running``——那是对外的"还在跑"，会让投影层丢掉这条回答（``project_final_result``
      对 ``running`` 返回 ``None``）、终态事件也不发，页面永远停在"正在生成回答…"
      （复审实证，2026-09-28：工具被安全策略拒绝 → 反思机械判 failed → 回答卡在页面上出不来）；
    - 其余（闲聊、模型直接回答、反思判定完成）→ ``completed``。
    """
    # 展示文本优先取 State 里已经确定的 ``final_response``：闲聊记忆路径在写入结果确定后写入它
    # （展示文本与 Checkpoint 原始消息**分开**——架构 §10.1 要求 messages 保留原始审计记录，
    # 不能为了让回答变准确而删改历史）。
    text = state.get("final_response") or last_ai_text(state.get("messages", []))
    if text is None:
        return {"final_response": None, "task_status": "failed"}
    # 召回失败时不允许"请用户重述偏好"的建议——提示词挡不住，用机械兜底（见该函数说明）
    text = _guard_recall_failure_answer(state, text)
    reflection = state.get("reflection_result")
    # 闲聊没有任务：记忆审核写下的 ``goal_achieved=False``（"闲聊不判定完成"）不能被当成
    # "任务未完成"，否则用户会看到一个正常回答却状态为 running。用精确比较：没有 ``mode``
    # 的 State（直接调用节点的单测）按工具语义处理，避免"默认按闲聊"把 running 变成 completed。
    unfinished = (
        state.get("mode") != "chat"
        and isinstance(reflection, ReflectionResult)
        and not reflection.goal_achieved
    )
    if state.get("task_status") == "cancelled":
        # 被取消的一轮：终态是 `cancelled`，并**保留已产出的部分回答**（§17.4）。这里直接返回，
        # 不走下面的 `completed` 分支——那个分支会去修正引用路径，等于把一段截断的话当完整结论处理。
        return {"final_response": text, "task_status": "cancelled"}
    if state.get("task_status") == "failed":
        status: TaskStatus = "failed"
    elif unfinished:
        # 未达成目标就结束：这是**终态**，不是"还在跑"。写 ``running`` 会让这条回答被投影层
        # 丢掉、终态事件也不发，页面永远停在"正在生成回答…"（见上面的更新规则）。
        status = "blocked"
    else:
        status = "completed"
    original_text = text
    corrected_web = resolve_web_citations(state, text)
    update: AgentState = {"final_response": corrected_web, "task_status": status}
    if corrected_web != text:
        answer = current_turn_ai_message(state.get("messages", []))
        if answer is not None and state.get("reply_overrides", {}).get(message_id(answer), answer.text) == text:
            update["reply_overrides"] = {message_id(answer): corrected_web}
        text = corrected_web
    if status == "completed":
        messages = state.get("messages", [])
        anchor = state.get("run_user_message_id")
        start = next((i for i, message in enumerate(messages) if message.id == anchor), 0) if anchor else 0
        corrected = complete_citation_paths(text, messages[start:])
        if corrected != text:
            update["final_response"] = corrected
            answer = current_turn_ai_message(messages)
            if answer is not None and state.get("reply_overrides", {}).get(message_id(answer), answer.text) == original_text:
                update["reply_overrides"] = {message_id(answer): corrected}
    return update


def last_ai_text(messages: Sequence[AnyMessage]) -> str | None:
    """返回最后一条 AI 消息的纯文本；没有则返回 ``None``。"""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = message.text.strip()
            if text:
                return text
    return None

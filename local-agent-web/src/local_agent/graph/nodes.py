"""LangGraph 节点（节点名严格对齐架构 §5.1）。

当前实现：

- 闲聊分支：``prepare_context`` → ``load_agents`` → ``recall_memory`` → ``mode_router``
  → ``compression_node`` → ``chat_node`` → ``final_response_node``
- 工具分支：``mode_router`` → ``planner_node`` → ``compression_node`` →
  ``agent_decision_node`` → ``safety_gate_node`` → ``tool_execution_node`` →
  ``reflection_node`` → ``completion_router``，再回到决策节点或产出回答。

其余按计划落地：写/改/删除工具与安全 Shell 在 T06/T07，完整机械证据与幂等在 T08，
审批节点在 T10，中期记忆召回在 T14B（本文件），记忆写入与遗忘在 T18，多步规划在 T20。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal, cast
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

from local_agent.config import EmbeddingConfig, MediumTermMemoryConfig, resolve_api_key
from local_agent.context.agents_loader import load_agents
from local_agent.context.prompt_context import PromptContext, build_system_prompt
from local_agent.context.token_budget import (
    ContextBudget,
    TokenUsage,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tools_tokens,
    prompt_budget_tokens,
    usage_ratio,
)
from local_agent.exceptions import (
    EmbeddingError,
    LocalAgentError,
    MemoryStoreError,
    SecurityError,
)
from local_agent.graph.state import (
    AgentState,
    DecisionRecord,
    ErrorEvent,
    EvidenceReference,
    EvidenceType,
    PlanStep,
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
from local_agent.llm import embedding as embedding_module
from local_agent.llm.models import TOOL_CALLING_UNAVAILABLE_REPLY, ModelCapabilities
from local_agent.memory import medium_term, recall
from local_agent.memory.short_term import (
    PromptWindow,
    recent_turns_start,
    select_prompt_window,
)
from local_agent.memory.summarizer import summarize_conversation
from local_agent.reflection.reflector import (
    TOOL_DATA_CLOSE,
    TOOL_DATA_OPEN,
    ReflectionDecision,
    reflect,
)
from local_agent.reflection.validators import (
    EvidenceLevel,
    ToolEvidence,
    summarize_trust,
    validate_tool_event,
)
from local_agent.scope import project_id_for
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

#: 反思给出的下一步动作（架构 §4.3 的 ``ReflectionResult.next_action``）。
ReflectionAction = Literal["continue", "retry", "ask_user", "finish"]


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


#: 进程内复用的中期记忆库（键含路径、collection 与 embedding 身份）。
_MEDIUM_TERM_STORES: dict[tuple[str, ...], medium_term.MediumTermStore] = {}
#: 建库/加载模型的并发保护：两个 Streamlit 会话可能同时首次召回，不加锁会各建一个模型实例。
_MEDIUM_TERM_STORES_LOCK = threading.Lock()


def _credential_fingerprint(api_key: str | None) -> str:
    """凭据变更标识：只放 Key 的完整哈希，**不把 Key 本体放进缓存键或日志**。

    用完整 SHA-256（不截断）：截断会把"凭据已更换"压缩成 32 位碰撞空间，一旦碰撞就会复用
    仍持有旧凭据的 Store；而完整摘要没有任何实际成本。
    """
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _store_cache_key(
    memory_config: MediumTermMemoryConfig,
    embedding_config: EmbeddingConfig,
    api_key: str | None,
) -> tuple[str, ...]:
    """商店缓存的键：任何影响"连哪个库、用哪套模型与凭据"的配置都要进键。

    漏掉任一项都会拿到不该复用的旧实例：换端点（``base_url``/``api_key_env``/Key）会命中旧
    Store，"先启用后关闭 embedding"也可能继续用旧实例。凭据只进完整哈希，不进原始 Key。
    """
    return (
        memory_config.path,
        memory_config.collection,
        str(embedding_config.enabled),
        embedding_config.provider,
        embedding_config.model,
        embedding_config.base_url,
        embedding_config.api_key_env,
        embedding_config.cache_dir,
        str(embedding_config.local_files_only),
        _credential_fingerprint(api_key),
    )


def medium_term_store(context: RuntimeContext) -> medium_term.MediumTermStore:
    """取（必要时创建）本进程复用的中期记忆库。

    复用是必需的：embedding 模型按进程惰性加载并复用（T14A），每次节点调用都新建适配器
    等于每轮重新加载一遍模型。整个"查缓存 + 建库"过程加锁：并发首次召回只会建一个实例
    （锁会在首次加载模型/下载时被持有，这正是我们要的串行化）。
    """
    memory_config = context.app_config.memory.medium_term
    embedding_config = context.app_config.embedding
    api_key = (
        resolve_api_key(embedding_config)
        if embedding_config.enabled and embedding_config.provider == "openai_compatible"
        else None
    )
    key = _store_cache_key(memory_config, embedding_config, api_key)
    with _MEDIUM_TERM_STORES_LOCK:
        cached = _MEDIUM_TERM_STORES.get(key)
        if cached is not None:
            return cached
        store = medium_term.open_store(
            memory_config,
            embedding_function=embedding_module.build_embedding_function(
                embedding_config, api_key=api_key
            ),
        )
        _MEDIUM_TERM_STORES[key] = store
        return store


def recall_memory_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """召回中期记忆并写入 ``memory_refs``（架构 §10.2）。

    - 关闭时直接透传：**不打开记忆库、不加载模型**（架构 §5：关闭的层不访问相应库）；
    - 召回失败（模型下载/缓存失败、collection 不兼容等）不让整轮对话失败：记一条错误事件
      并把 ``memory_refs`` 置空，页面据此说明"本轮没有用上记忆"以及原因（架构 §11.6：
      必须明确报错，不允许静默回退到别的模型或 provider）。
    """
    context = runtime.context
    memory_config = context.app_config.memory.medium_term
    if not memory_config.enabled:
        return {}
    query = str(state.get("normalized_request") or state.get("user_input") or "").strip()
    if not query:
        return {"memory_refs": []}
    try:
        store = medium_term_store(context)
        references = recall.recall_memory(
            store,
            query=query,
            config=memory_config,
            workspace_root=context.workspace_root,
            project_id=project_id_for(context.workspace_root),
            user_id=context.user_id,
            now=context.current_time,
        )
    except (EmbeddingError, MemoryStoreError) as exc:
        return {
            "memory_refs": [],
            "error_events": [
                make_error_event(
                    str(state.get("run_id") or ""),
                    stage=RECALL_MEMORY,
                    message=f"记忆召回失败，本轮不注入记忆：{exc}",
                )
            ],
        }
    return {"memory_refs": references}


def _prompt_window(
    state: AgentState, context: RuntimeContext, *, covered: int | None = None
) -> PromptWindow:
    """本轮送模型的消息窗口（架构 §10.1）。

    ``memory.compression.enabled=false`` 时原样返回全部消息（关闭即不裁剪）。
    ``covered`` 显式给出时用它作为"已被摘要覆盖"的边界（压缩节点写完摘要要重算窗口）。
    """
    config = context.app_config
    return select_prompt_window(
        state.get("messages") or [],
        enabled=config.memory.compression.enabled,
        max_messages=config.memory.short_term.prompt_window_messages,
        preserve_recent_turns=config.memory.compression.preserve_recent_turns,
        preserve_recent_tool_steps=config.memory.compression.preserve_recent_tool_steps,
        covered_messages=_covered_messages(state) if covered is None else covered,
    )


def _covered_messages(state: AgentState) -> int:
    """会话摘要已经覆盖到第几条消息（``context_budget.summary_covered_messages``）。"""
    budget = state.get("context_budget")
    if isinstance(budget, ContextBudget):
        return max(0, int(budget.summary_covered_messages))
    return 0


def _tool_definition_tokens(state: AgentState, context: RuntimeContext) -> int:
    """本轮绑定给模型的工具定义占用的估算 token（仅工具模式；它们也是出站 Prompt 的一部分）。"""
    if str(state.get("mode", "chat")) != "tool":
        return 0
    registry = context.tool_registry
    if registry is None:
        return 0
    return estimate_tools_tokens(registry.tools_for_mode("tool"))


def _system_prompt(
    state: AgentState, context: RuntimeContext, *, summary: str | None = None
) -> PromptContext:
    """系统提示；压缩节点的估算与出站节点实际发送必须用**同一个**函数，否则预算口径会脱节。

    ``summary`` 显式给出时用它（压缩节点写完摘要需要按新摘要重算预算），否则取 State 里的值。
    """
    return build_system_prompt(
        mode=state.get("mode", "chat"),
        context=context,
        route_reason=str(state.get("route_reason", "")),
        memory_refs=state.get("memory_refs") or (),
        conversation_summary=(
            summary if summary is not None else state.get("conversation_summary")
        ),
        task_goal=state.get("task_goal"),
        plan=state.get("plan") or (),
    )


def compression_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """上下文预算与摘要压缩（架构 §10.1、§10.5）。

    只压缩**送模型的窗口**：Checkpoint 里的 ``messages`` 一条都不删（架构 §10.1）。

    触发口径（T16）：``estimated_prompt_tokens / (context_window_tokens - max_tokens)
    >= memory.compression.trigger_ratio``；估算量覆盖实际出站的完整 Prompt —— 系统提示
    （含 AGENTS、会话摘要、召回记忆）、消息窗口，以及工具模式下绑定给模型的**工具定义**。

    "摘要替代旧消息"（架构 §10.5 第 5 条）：摘要成功后把覆盖边界记进
    ``context_budget.summary_covered_messages``，窗口从该边界之后开始，并按**新窗口与新摘要**
    重算预算——否则每轮都会重发同一批历史，压缩反而让 Prompt 变长。

    降级口径：
    - ``compression.enabled=false``：只统计预算，不裁剪窗口、不生成摘要；
    - 没有"更早的旧消息"可摘要（典型是第一轮）：静默跳过，不记错误事件、不改压缩计数；
    - 摘要生成失败：**只记 ErrorEvent**，不更新 ``conversation_summary`` / ``context_version``
      / ``compression_count``，本轮继续用裁剪后的窗口；
    - 窗口按完整轮次裁剪（见 :mod:`local_agent.memory.short_term`），不切开工具调用对。
    """
    context = runtime.context
    config = context.app_config
    compression = config.memory.compression
    messages = list(state.get("messages") or [])
    covered = _covered_messages(state)
    window = _prompt_window(state, context, covered=covered)
    prompt = _system_prompt(state, context)
    tools_tokens = _tool_definition_tokens(state, context)
    window_tokens = estimate_messages_tokens(window.messages)
    estimated = prompt.estimated_tokens + window_tokens + tools_tokens
    budget_tokens = prompt_budget_tokens(
        context_window_tokens=config.llm.context_window_tokens,
        completion_reserve_tokens=config.llm.max_tokens,
    )
    ratio = usage_ratio(estimated_prompt_tokens=estimated, prompt_budget_tokens=budget_tokens)
    triggered = compression.enabled and ratio >= compression.trigger_ratio
    existing_summary = (state.get("conversation_summary") or "").strip()

    def snapshot(
        *,
        triggered_now: bool,
        summary_chars: int,
        covered_now: int,
        window_now: PromptWindow,
        prompt_now: PromptContext,
        estimated_now: int,
        ratio_now: float,
    ) -> tuple[TokenUsage, ContextBudget]:
        """按给定的窗口/系统提示生成这一轮的用量与预算记录。"""
        return (
            TokenUsage(
                prompt_tokens=estimated_now,
                system_prompt_tokens=prompt_now.estimated_tokens,
                window_messages_tokens=estimate_messages_tokens(window_now.messages),
                completion_reserve_tokens=config.llm.max_tokens,
                tool_definition_tokens=tools_tokens,
            ),
            ContextBudget(
                context_window_tokens=config.llm.context_window_tokens,
                completion_reserve_tokens=config.llm.max_tokens,
                prompt_budget_tokens=budget_tokens,
                estimated_prompt_tokens=estimated_now,
                usage_ratio=round(ratio_now, 6),
                trigger_ratio=compression.trigger_ratio,
                triggered=triggered_now,
                window_messages=len(window_now.messages),
                total_messages=window_now.total_messages,
                summary_chars=summary_chars,
                summary_covered_messages=covered_now,
            ),
        )

    usage, current_budget = snapshot(
        triggered_now=triggered,
        summary_chars=len(existing_summary),
        covered_now=covered,
        window_now=window,
        prompt_now=prompt,
        estimated_now=estimated,
        ratio_now=ratio,
    )
    update: AgentState = {"token_usage": usage, "context_budget": current_budget}
    if not triggered:
        return update

    # 只摘要"已覆盖边界之后、最近 preserve_recent_turns 轮之前"的旧消息：既不重复摘要同一批
    # 历史，也不会出现"既没进窗口、也没进摘要"的消息。
    boundary = recent_turns_start(messages, compression.preserve_recent_turns)
    older = messages[covered:boundary]
    if not older:
        # 还没有"更早的旧消息"可摘要（典型是第一轮）：这不是故障，**静默跳过本轮**——
        # 不记错误事件，也不动 conversation_summary / context_version / compression_count。
        return update
    try:
        summary = summarize_conversation(
            context.model_adapter,
            messages=older,
            previous_summary=state.get("conversation_summary"),
            max_chars=compression.summary_max_chars,
        )
    except Exception as exc:  # noqa: BLE001 —— 摘要失败不能拖垮本轮，记 ErrorEvent 后继续
        return {
            **update,
            "error_events": merge_appended(
                state.get("error_events", []),
                [
                    _error(
                        state,
                        stage=COMPRESSION_NODE,
                        message=f"会话摘要生成失败，本轮不更新摘要：{exc}",
                    )
                ],
                error_event_key,
            ),
        }

    # 摘要成功：被覆盖的旧消息不再进窗口，按新边界与新摘要重算预算
    new_covered = covered + len(older)
    new_window = _prompt_window(state, context, covered=new_covered)
    new_prompt = _system_prompt(state, context, summary=summary)
    new_estimated = (
        new_prompt.estimated_tokens + estimate_messages_tokens(new_window.messages) + tools_tokens
    )
    new_ratio = usage_ratio(
        estimated_prompt_tokens=new_estimated, prompt_budget_tokens=budget_tokens
    )
    new_usage, new_budget = snapshot(
        triggered_now=True,
        summary_chars=len(summary),
        covered_now=new_covered,
        window_now=new_window,
        prompt_now=new_prompt,
        estimated_now=new_estimated,
        ratio_now=new_ratio,
    )
    return {
        **update,
        "token_usage": new_usage,
        "context_budget": new_budget,
        "conversation_summary": summary,
        "context_version": int(state.get("context_version", 0)) + 1,
        "compression_count": int(state.get("compression_count", 0)) + 1,
    }


def chat_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """闲聊节点：只把消息交给模型，不绑定任何工具。

    架构 §1.3-3 要求闲聊模式不注册文件与 Shell 工具，因此本节点不调用
    ``bind_tools``；模型实例来自 RuntimeContext（架构 §4.1），不在图内持有。
    """
    context = runtime.context
    prompt = _system_prompt(state, context)
    messages = [
        SystemMessage(content=prompt.system_prompt),
        *_prompt_window(state, context).messages,
    ]
    return {"messages": [context.model_adapter.invoke(messages)]}


# --------------------------------------------------------------------------- 工具分支


def planner_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """工具分支的规划入口。

    T05 只确定任务目标与循环上限（架构 §11.5）；T17 起如果没有计划就生成**单步**计划，让反思
    节点有可更新的对象（当前步状态、结果摘要、重试次数）。多步计划、依赖与重规划属于 T20。
    ``task_goal`` 与已有计划都保留，便于重入时不丢目标。
    """
    context = runtime.context
    goal = state.get("task_goal") or str(state.get("normalized_request", ""))
    plan = list(state.get("plan") or [])
    if not plan:
        plan = [
            PlanStep(
                id=f"{state.get('run_id') or 'run'}:step-1",
                description=goal.strip() or "完成用户请求",
                status="running",
                expected_evidence="工具结果与最终回答",
            )
        ]
    return {
        "task_goal": goal,
        "max_loop_count": context.app_config.agent.max_tool_loops,
        "task_status": "running",
        "plan": plan,
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
    condition = _system_prompt(state, context)
    response = model.invoke(
        [
            SystemMessage(content=condition.system_prompt),
            *_prompt_window(state, context).messages,
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

        if decision.requires_approval:
            # 指纹**只在确实要进审批时**才算：它包含删除目标的目录摘要（对目录是递归扫描），
            # 而硬拒绝的动作（越界、受保护目标、敏感文件…）根本不会进审批，不该为此扫描整棵
            # 目录树——对用户主目录这类目标会把页面卡死（T14C 复审）。
            fingerprint = action_fingerprint(name, arguments, policy=policy)
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
    summaries: list[str] = []

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
        summaries.append(f"{event.tool_name}({event.status}) → {event.output_preview[:160]}")

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

    decision, failure = _reflect_decision(
        state, context, trust=trust, anomalies=anomalies, summaries=summaries
    )
    goal_achieved, next_action, notes = _apply_mechanical_guard(trust, decision)
    next_action, retry_note = _apply_retry_limit(
        state, next_action, max_retries=context.app_config.agent.max_retries_per_step
    )
    if retry_note:
        notes.append(retry_note)
    if failure is not None:
        notes.append(f"反思决策不可用（{failure}），已按机械结论兜底")

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
            evidence=tuple(unique_evidence),
        ),
        "plan": _updated_plan(
            state, goal_achieved=goal_achieved, next_action=next_action, decision=decision
        ),
        "retry_count": int(state.get("retry_count", 0))
        + (1 if next_action == "retry" else 0),
        "decision_trace": merge_appended(
            state.get("decision_trace", []), [trace], decision_record_key
        ),
    }
    if endings:
        update["messages"] = endings
    if final_answer is not None:
        # 收尾合成也是一次出站 Prompt：把它的估算写回，State 自报的预算才与真实请求一致
        update["token_usage"] = final_answer.usage
        update["context_budget"] = final_answer.budget

    # 同一轮的机械失败、解析失败与未确认副作用都要各留一条事件：**先收集、最后只 merge 一次**。
    # 逐次 merge 会互相覆盖——`_error` 的事件 ID 由 stage + loop_count 派生，三类同轮事件在同一个
    # stage 下 ID 相同，后写的会把先写的挤掉（复审实测：机械失败 + 解析失败最终 0 条）。
    errors: list[ErrorEvent] = []
    if trust == "failed":
        errors.append(
            _error(
                state,
                stage=f"{REFLECTION_NODE}:mechanical",
                message="机械校验未通过：本轮工具结果不能作为已验证证据",
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
    try:
        decision = reflect(
            context.model_adapter,
            task_goal=str(state.get("task_goal") or state.get("normalized_request") or ""),
            plan=state.get("plan") or [],
            current_step_index=int(state.get("current_step_index", 0)),
            trust=trust,
            anomalies=anomalies,
            tool_summaries=summaries,
            loop_count=int(state.get("loop_count", 0)),
            max_loop_count=int(state.get("max_loop_count", 0)),
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


def _apply_mechanical_guard(
    trust: EvidenceLevel, decision: ReflectionDecision
) -> tuple[bool, ReflectionAction, list[str]]:
    """机械结论优先，并把自相矛盾的组合归一。

    返回 ``(goal_achieved, next_action, 规范化说明)``。三条归一规则：

    1. 证据等级不是 ``verified`` 却判完成 → ``goal_achieved=False`` + ``ask_user``；
    2. 未达成目标却用 ``finish`` 收尾 → 改为 ``ask_user``；
    3. 判定完成却还要 ``continue`` / ``retry`` / ``ask_user`` → 归一为 ``finish``（否则会出现
       "步骤已完成却继续执行"的矛盾状态）。
    """
    notes: list[str] = []
    if trust != "verified" and decision.goal_achieved:
        notes.append("证据等级不是 verified，不能判定完成，改为向用户说明")
        return False, "ask_user", notes
    if decision.goal_achieved and decision.next_action != "finish":
        notes.append("已判定完成，动作归一为 finish")
        return True, "finish", notes
    if not decision.goal_achieved and decision.next_action == "finish":
        notes.append("未达成目标不能用 finish 收尾，改为向用户说明")
        return False, "ask_user", notes
    return decision.goal_achieved, decision.next_action, notes


def _apply_retry_limit(
    state: AgentState, next_action: ReflectionAction, *, max_retries: int
) -> tuple[ReflectionAction, str | None]:
    """重试次数达到 ``agent.max_retries_per_step`` 后不再重试，改为向用户说明。"""
    if next_action != "retry":
        return next_action, None
    used = int(state.get("retry_count", 0))
    if used >= max_retries:
        return "ask_user", f"重试已达上限（{max_retries} 次），改为向用户说明"
    return next_action, None


def _updated_plan(
    state: AgentState,
    *,
    goal_achieved: bool,
    next_action: ReflectionAction,
    decision: ReflectionDecision,
) -> list[PlanStep]:
    """更新当前步骤的状态与结果摘要（多步计划、依赖与重规划属于 T20）。"""
    plan = list(state.get("plan") or [])
    index = int(state.get("current_step_index", 0))
    if not plan or index < 0 or index >= len(plan):
        return plan
    step = plan[index]
    if goal_achieved:
        status = "completed"
    elif next_action == "ask_user":
        status = "blocked"
    else:
        status = "running"
    plan[index] = replace(
        step,
        status=status,
        result_summary=decision.plan_adjustment.strip() or step.result_summary,
        retry_count=step.retry_count + (1 if next_action == "retry" else 0),
    )
    return plan


#: ``finish`` 分支的合成指令：要求不绑定工具地给出最终回答（架构 §5.3 的完成出口）。
FINAL_ANSWER_INSTRUCTION = (
    "请基于上面的任务、计划与工具结果，直接给出本轮的最终回答。\n"
    "说明：做了什么、依据是什么（引用具体的工具输出、文件或命令）、是否存在失败、未验证或\n"
    "需要用户确认的部分。\n"
    "不要调用任何工具。"
)


#: 超预算时合成输入里的证据条数与单条长度上限（有界，避免把大工具输出整段发给模型）。
MAX_SYNTHESIS_EVIDENCE_ITEMS = 5
MAX_SYNTHESIS_EVIDENCE_CHARS = 400
#: 收敛时单条摘要的长度下限：再小就不再截短，改为减少条数。
MIN_SYNTHESIS_EVIDENCE_CHARS = 40
#: 确定性结果摘要里最多列几条工具结果（与合成证据一样只取**最近**的若干条）。
DETERMINISTIC_SUMMARY_RECORDS = 5


@dataclass(frozen=True)
class _FinalAnswer:
    """``finish`` 分支的合成结果：回答、这次调用的预算快照，以及降级信息。"""

    message: AIMessage
    usage: TokenUsage
    budget: ContextBudget
    failure: str | None = None
    bounded: bool = False


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
      模型不可用时退化为确定性结果摘要。两条路都会保留 ``anomaly_explanation`` 与机械异常。
    """
    if next_action == "ask_user":
        return [AIMessage(content=_ask_user_text(decision, trust=trust))], None
    if next_action == "finish":
        answer = _synthesize_final_answer(
            state, context, decision=decision, trust=trust, anomalies=anomalies
        )
        return [answer.message], answer
    return [], None


def _ask_user_text(decision: ReflectionDecision, *, trust: EvidenceLevel) -> str:
    """把 ``ask_user_reason`` 变成用户看得懂的追问（确定性文本，不额外调用模型）。"""
    reason = decision.ask_user_reason.strip() or "需要你补充信息或确认结果后才能继续"
    parts = [f"我需要你确认一下：{reason}"]
    explanation = decision.anomaly_explanation.strip()
    if explanation:
        parts.append(f"当前情况：{explanation}")
    if trust != "verified":
        parts.append("（本轮工具结果没有通过机械校验，所以我不会声称任务已经完成。）")
    return "\n\n".join(parts)


def _synthesize_final_answer(
    state: AgentState,
    context: RuntimeContext,
    *,
    decision: ReflectionDecision,
    trust: EvidenceLevel,
    anomalies: Sequence[str],
) -> _FinalAnswer:
    """``finish`` 分支的最终回答：一次**不绑定工具**的模型调用，失败时退化为确定性摘要。

    与普通出站一样**先过预算**：这次调用发生在工具执行**之后**，窗口里带着刚产生的工具结果，
    而 State 里的 ``context_budget`` 还是工具执行前的旧快照；大输出或多工具调用会让收尾调用
    超上下文，也会让"State 自报预算"与真实出站 Prompt 对不上（复审实证）。因此：

    1. 按 ``context_window_tokens - max_tokens`` 估算"系统提示 + 窗口 + 指令"（指令也要算）；
    2. 超预算时退化为**有界的证据摘要**（见 :func:`_bounded_evidence`）：按"预算 − 系统提示
       − 指令"的剩余额度逐步收敛，包在 ``<<<TOOL_OUTPUT>>>`` 里标注为不可信数据；
    3. 连"系统提示 + 指令"本身都放不下时不调用模型，直接给确定性摘要——**绝不发出超预算请求**；
    4. 无论走哪条路，都把这次调用的估算作为预算快照返回，由调用方写回 State。
    """
    config = context.app_config
    prompt = _system_prompt(state, context)
    window = _prompt_window(state, context)
    budget_tokens = prompt_budget_tokens(
        context_window_tokens=config.llm.context_window_tokens,
        completion_reserve_tokens=config.llm.max_tokens,
    )
    # 指令也是以消息形式出站的：用与外层集合完全相同的口径（含消息包装开销），否则评估会稳定
    # 少算一小截，"这次调用在预算内"就不再可靠。
    instruction_tokens = estimate_message_tokens(
        SystemMessage(content=FINAL_ANSWER_INSTRUCTION)
    )
    window_tokens = estimate_messages_tokens(window.messages)
    estimated = prompt.estimated_tokens + window_tokens + instruction_tokens
    window_count = len(window.messages)
    bounded = estimated > budget_tokens
    messages: list[AnyMessage] = []
    if bounded:
        allowance = budget_tokens - prompt.estimated_tokens - instruction_tokens
        evidence = (
            _bounded_evidence(state, allowance_tokens=allowance) if allowance > 0 else None
        )
        if evidence is None:
            # 固定部分就放不下：连证据都不发，走确定性摘要（空 messages 即"不调用模型"）
            window_tokens = 0
            window_count = 0
            estimated = prompt.estimated_tokens + instruction_tokens
        else:
            window_tokens = estimate_messages_tokens([HumanMessage(content=evidence)])
            window_count = 1
            estimated = prompt.estimated_tokens + window_tokens + instruction_tokens
            messages = [
                SystemMessage(content=prompt.system_prompt),
                HumanMessage(content=evidence),
                SystemMessage(content=FINAL_ANSWER_INSTRUCTION),
            ]
    else:
        messages = [
            SystemMessage(content=prompt.system_prompt),
            *window.messages,
            SystemMessage(content=FINAL_ANSWER_INSTRUCTION),
        ]

    ratio = usage_ratio(estimated_prompt_tokens=estimated, prompt_budget_tokens=budget_tokens)
    usage = TokenUsage(
        prompt_tokens=estimated,
        # 指令本身也是一条 SystemMessage：必须算进系统提示分项，否则分项之和比总量少指令那部分，
        # 违反 "各分项之和 = prompt_tokens" 的不变量（复审实测少 95 token）。
        system_prompt_tokens=prompt.estimated_tokens + instruction_tokens,
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
        )
    try:
        response = context.model_adapter.invoke(messages)
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
        message=AIMessage(content=text),
        usage=usage,
        budget=budget,
        failure=failure,
        bounded=bounded,
    )


def _bounded_evidence(state: AgentState, *, allowance_tokens: int) -> str | None:
    """在 ``allowance_tokens`` 内构造证据摘要；连最小摘要都放不下时返回 ``None``。

    "有界"不能只是固定条数 + 固定长度：固定上限仍可能超预算（复审实测 1094 > 1024），真实模型
    会直接拒绝请求。这里按剩余额度**逐步收敛**——先截短每条，再减少条数，最后放弃；返回 ``None``
    表示由调用方改用确定性摘要（不再发出超预算请求）。
    """
    entries = _evidence_entries(state)
    limit = MAX_SYNTHESIS_EVIDENCE_CHARS
    while True:
        text = _wrap_evidence(entries, limit=limit)
        if estimate_messages_tokens([HumanMessage(content=text)]) <= allowance_tokens:
            return text
        if limit > MIN_SYNTHESIS_EVIDENCE_CHARS:
            limit //= 2
            continue
        if len(entries) > 1:
            entries = entries[1:]  # 先丢最早的一条
            limit = MAX_SYNTHESIS_EVIDENCE_CHARS
            continue
        return None


def _evidence_entries(state: AgentState) -> list[str]:
    """当前任务**最近** :data:`MAX_SYNTHESIS_EVIDENCE_ITEMS` 条工具结果的摘要行（从旧到新）。"""
    lines = [
        f"- {record.get('tool_name')}（{record.get('status')}）："
        f"{' '.join(str(record.get('output_preview') or '').split())}"
        for record in _recent_records(state, limit=MAX_SYNTHESIS_EVIDENCE_ITEMS)
    ]
    return lines or ["（本轮没有工具结果）"]


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


def _wrap_evidence(entries: Sequence[str], *, limit: int) -> str:
    """把证据行包成带不可信标记的文本，每行按 ``limit`` 截断。"""
    body = "\n".join(entry[:limit] for entry in entries)
    return f"{TOOL_DATA_OPEN}\n{body}\n{TOOL_DATA_CLOSE}"


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
    lines.append(f"- 机械证据等级：{trust}")
    for record in _recent_records(state, limit=DETERMINISTIC_SUMMARY_RECORDS):
        preview = " ".join(str(record.get("output_preview") or "").split())[:200]
        lines.append(f"- {record.get('tool_name')}（{record.get('status')}）：{preview}")
    explanation = decision.anomaly_explanation.strip()
    if explanation:
        lines.append(f"- 异常解释：{explanation}")
    if anomalies:
        lines.append(f"- 机械校验异常：{'；'.join(str(item) for item in list(anomalies)[:3])}")
    if failure:
        lines.append(f"- 最终回答合成失败：{failure}")
    lines.append("- 以上结论未经你确认；如需继续，请补充说明。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 输出


def final_response_node(state: AgentState) -> AgentState:
    """把最后一条有文本的 AI 消息定为最终回答。

    ``task_status`` 的更新规则（架构 §5.4：有回答不等于任务完成）：

    - 没有可用的 AI 文本 → ``failed``，由页面给出明确提示，而不是编造回答；
    - 已经是 ``failed``（例如超出循环上限）→ 保持 ``failed``，不改写成 ``completed``；
    - 反思结论存在但 ``goal_achieved=False``（未达成目标就结束，例如需要用户补充）→
      保持 ``running``，不冒充 ``completed``；
    - 其余（闲聊、模型直接回答、反思判定完成）→ ``completed``。
    """
    text = last_ai_text(state.get("messages", []))
    if text is None:
        return {"final_response": None, "task_status": "failed"}
    reflection = state.get("reflection_result")
    unfinished = isinstance(reflection, ReflectionResult) and not reflection.goal_achieved
    if state.get("task_status") == "failed":
        status: TaskStatus = "failed"
    elif unfinished:
        status = "running"
    else:
        status = "completed"
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

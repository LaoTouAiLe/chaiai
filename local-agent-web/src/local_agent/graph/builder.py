"""图构建（开发文档 T05）。

模型、配置与安全策略都不放在图里，而是随每次调用的 ``RuntimeContext`` 注入
（架构 §4.1）：同一张编译好的图既能用真实模型，也能用测试假模型与假策略。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from local_agent.capabilities import effective_capabilities
from local_agent.config import RunConfig
from local_agent.graph.edges import ChatStateGraph, add_minimal_chat_edges
from local_agent.graph.nodes import (
    AGENT_DECISION_NODE,
    APPROVAL_INTERRUPT_NODE,
    CHAT_NODE,
    COMPRESSION_NODE,
    FINAL_RESPONSE_NODE,
    LOAD_AGENTS,
    MEMORY_WRITE_NODE,
    PLANNER_NODE,
    PREPARE_CONTEXT,
    RECALL_MEMORY,
    REFLECTION_NODE,
    RETRIEVE_CODE_NODE,
    SAFETY_GATE_NODE,
    TASK_MEMORY_NODE,
    TOOL_EXECUTION_NODE,
    agent_decision_node,
    approval_interrupt_node,
    chat_node,
    compression_node,
    final_response_node,
    load_agents_node,
    memory_write_node,
    planner_node,
    prepare_context_node,
    recall_memory_node,
    reflection_node,
    retrieve_code_node,
    safety_gate_node,
    task_memory_node,
    tool_execution_node,
)
from local_agent.graph.routers import (
    COMPLETION_ROUTER,
    MODE_ROUTER,
    completion_router_node,
    mode_router_node,
)
from local_agent.graph.scope import tool_scope_key
from local_agent.graph.state import (
    AgentState,
    RuntimeContext,
    ToolEvent,
    make_error_event,
    merge_appended,
    model_call_key,
)
from local_agent.llm.models import ModelCapabilities, probe_capabilities
from local_agent.llm.usage_recording import (
    DEFAULT_PURPOSE,
    PURPOSE_BY_NODE,
    ModelCallRecorder,
    TurnRecord,
    use_model_purpose,
    use_model_recorder,
)
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.tool_registry import ToolRegistry

#: 当前阶段的图类型：State 为 AgentState，运行上下文为 RuntimeContext。
ChatGraph = CompiledStateGraph[AgentState, RuntimeContext, AgentState, AgentState]


def build_chat_graph(checkpointer: BaseCheckpointSaver | None = None) -> ChatGraph:
    """构建当前阶段的图（架构 §5.2 闲聊分支 + §5.3 工具分支）。

    Args:
        checkpointer: 传入 SQLite saver 时启用会话持久化（T03）；为 ``None`` 时图无状态。

    Returns:
        已编译的图；调用时需通过 ``context=`` 传入 :class:`RuntimeContext`，
        并在 ``config={"configurable": {"thread_id": ...}}`` 中给出会话键。
    """
    graph: ChatStateGraph = StateGraph(AgentState, context_schema=RuntimeContext)

    def add(name: str, node: Callable[..., Any]) -> None:
        # 每个节点都经用量装饰器：它是"所有模型调用共用同一记录入口"的接线点。
        graph.add_node(name, _usage_aware(name, node))

    add(PREPARE_CONTEXT, prepare_context_node)
    add(LOAD_AGENTS, load_agents_node)
    add(RECALL_MEMORY, recall_memory_node)
    add(MODE_ROUTER, mode_router_node)
    add(RETRIEVE_CODE_NODE, retrieve_code_node)
    add(COMPRESSION_NODE, compression_node)
    add(PLANNER_NODE, planner_node)
    add(AGENT_DECISION_NODE, agent_decision_node)
    add(SAFETY_GATE_NODE, safety_gate_node)
    add(APPROVAL_INTERRUPT_NODE, approval_interrupt_node)
    add(TOOL_EXECUTION_NODE, tool_execution_node)
    add(REFLECTION_NODE, reflection_node)
    add(COMPLETION_ROUTER, completion_router_node)
    add(MEMORY_WRITE_NODE, memory_write_node)
    add(TASK_MEMORY_NODE, task_memory_node)
    add(CHAT_NODE, chat_node)
    add(FINAL_RESPONSE_NODE, final_response_node)
    add_minimal_chat_edges(graph)
    return graph.compile(checkpointer=checkpointer)


def close_interrupted_turn(graph: ChatGraph, config: RunnableConfig,
                           state: Mapping[str, Any], request_id: str) -> None:
    """核对后只写终态检查点；不执行节点、模型或工具。"""
    run_id = str(state.get("run_id") or "run")
    text = "本轮执行已中断，已由你核对后结束。已执行的工具操作保留，未继续或重复执行工具。"
    missing: dict[str, Any] = {}
    for message in state.get("messages") or []:
        if isinstance(message, HumanMessage):
            missing.clear()
        elif isinstance(message, AIMessage):
            missing.update((str(call["id"]), call) for call in message.tool_calls if call.get("id"))
        elif isinstance(message, ToolMessage):
            missing.pop(message.tool_call_id, None)
    unknown = "工具执行结果未完整记录；本轮已由用户核对后结束，不要重放该工具。已发生的副作用保留。"
    replies = [ToolMessage(content=unknown, tool_call_id=call_id, name=call.get("name"),
                           id=f"{run_id}:interrupted-result:{call_id}") for call_id, call in missing.items()]
    recorded = {tool_scope_key(event.run_id, event.tool_call_id) for event in state.get("tool_events") or []}
    events = [ToolEvent(tool_call_id=call_id, tool_name=str(call.get("name") or ""),
                       run_id=run_id, status="unknown", output_preview=unknown)
              for call_id, call in missing.items() if tool_scope_key(run_id, call_id) not in recorded]
    update: AgentState = {
        "task_status": "failed", "pending_tool_calls": [], "pending_approval": None,
        "final_response": text, "messages": [*replies, AIMessage(content=text)],
        "tool_events": events,
        "error_events": [make_error_event(run_id, stage="interrupted_turn",
            message="执行结果曾需核对，用户确认核对后结束本轮；未重放任何工具。", salt=request_id)],
    }
    turn = (state.get("turn_records") or {}).get(run_id)
    if turn is not None and turn.final_at is None:
        now = datetime.now(timezone.utc)
        update["turn_records"] = {run_id: replace(turn,
            final_at=now, final_state="failed",
            turn_elapsed_ms=max(0, round((now - turn.submitted_at).total_seconds() * 1000)),
        )}
    checkpoint_config = graph.get_state(config).config
    # SQLite saver 读取根图时默认空命名空间，但 update_state 的写入要求显式携带它。
    checkpoint_config["configurable"].setdefault("checkpoint_ns", "")
    graph.update_state(checkpoint_config, update, as_node=FINAL_RESPONSE_NODE)


def _usage_aware(name: str, node: Callable[..., Any]) -> Callable[..., Any]:
    """把节点包成"会记录用量"的版本（架构 §15.6）。

    三件事，都只在这一处发生：

    1. **设置记录上下文**：把 ``RuntimeContext.usage_recorder`` 与产品级 ``purpose`` 放进
       contextvar，节点内部的任何 ``record_model_call`` 都取得到它们（无需改调用点签名）；
    2. **把新记录并回 State**：``model_call_records`` 是明细真源，按 ``model_call_id`` 去重后追加；
    3. **更新整轮记录**：写入本轮的 ``TurnRecord``（身份、时间线、关联调用与工具事件），
       终态由 :func:`merge_turn_records` 保证不被改写。
    """
    purpose = PURPOSE_BY_NODE.get(name, DEFAULT_PURPOSE)
    takes_runtime = len(inspect.signature(node).parameters) >= 2

    def wrapped(state: AgentState, runtime: Any) -> AgentState:
        recorder = getattr(getattr(runtime, "context", None), "usage_recorder", None)
        if isinstance(recorder, ModelCallRecorder):
            recorder.bind_dynamic_context(state)
        with use_model_recorder(recorder), use_model_purpose(purpose):
            try:
                update = node(state, runtime) if takes_runtime else node(state)
            except Exception as exc:
                # 仅闭合不执行工具的两个模型节点；工具/持久化异常仍交应用层核对副作用。
                if name not in {AGENT_DECISION_NODE, CHAT_NODE}:
                    raise
                text = "本轮模型调用失败，已结束本轮。请检查模型连接后再提问；此前已执行的工具操作保留。"
                update = {
                    "messages": [AIMessage(content=text)],
                    "final_response": text,
                    "task_status": "failed",
                    "pending_tool_calls": [],
                    "error_events": [make_error_event(
                        str(state.get("run_id") or "run"), stage=name,
                        message=f"模型调用失败（{type(exc).__name__}），未继续执行本次决策。",
                        loop_count=int(state.get("loop_count", 0)),
                    )],
                }
        if not update or not isinstance(recorder, ModelCallRecorder):
            return update
        merged: dict[str, Any] = {**state, **update}
        # 录音机的轮次以 State 为准（`run_id` 由 `prepare_context` 生成）：不一致就跟随 State，
        # 这样"同一张图连跑几轮"或"不经应用服务直接跑图"都能得到正确的轮次归属。
        # 应用服务已经提前绑过时，两者相同，这里不会发生任何改变。
        state_run_id = merged.get("run_id")
        if isinstance(state_run_id, str) and state_run_id and recorder.run_id != state_run_id:
            recorder.bind_run(state_run_id)
        result: AgentState = {
            **update,
            "model_call_records": merge_appended(
                state.get("model_call_records", []), recorder.finished_records(), model_call_key
            ),
        }
        if recorder.run_id:
            result["turn_records"] = {recorder.run_id: _build_turn_record(merged, recorder)}
        return result

    wrapped.__name__ = getattr(node, "__name__", name)
    return wrapped


def _build_turn_record(state: Mapping[str, Any], recorder: ModelCallRecorder) -> TurnRecord:
    """从当前 State 与录音机构造整轮记录（只存事实，Token 汇总留给投影层加总）。"""
    run_id = recorder.run_id
    submitted = state.get("run_submitted_at")
    if not isinstance(submitted, datetime):
        submitted = datetime.now(timezone.utc)
    final_state = _turn_state(state)
    existing = _existing_turn(state, run_id)
    final_at = getattr(existing, "final_at", None) if existing is not None else None
    if final_state in {"completed", "failed", "blocked"} and final_at is None:
        final_at = datetime.now(timezone.utc)
    calls = [record for record in recorder.finished_records() if record.run_id == run_id]
    elapsed = None
    if isinstance(final_at, datetime):
        elapsed = max(0, round((final_at - submitted).total_seconds() * 1000))
    return TurnRecord(
        run_id=run_id,
        submitted_at=submitted,
        final_at=final_at,
        final_state=final_state,
        model_call_ids=tuple(record.model_call_id for record in calls),
        tool_event_ids=tuple(
            tool_scope_key(event.run_id, event.tool_call_id)
            for event in (state.get("tool_events") or [])
            if getattr(event, "run_id", "") == run_id
        ),
        approval_wait_ms=_approval_wait_ms(state, run_id),
        turn_elapsed_ms=elapsed,
        usage_recorded=bool(calls),
        user_message_id=state.get("run_user_message_id"),
    )


def _existing_turn(state: Mapping[str, Any], run_id: str) -> Any:
    records = state.get("turn_records")
    if isinstance(records, Mapping):
        return records.get(run_id)
    return None


def _turn_state(state: Mapping[str, Any]) -> str:
    status = str(state.get("task_status") or "running")
    if status in {"completed", "failed", "blocked"}:
        return status
    if status == "waiting_approval":
        return "awaiting_approval"
    return "running"


def _approval_wait_ms(state: Mapping[str, Any], run_id: str) -> int | None:
    """本轮已完成审批区间之和；待决区间不以零冒充已完成等待。"""
    decisions = [
        item for item in state.get("approval_history") or []
        if isinstance(item, Mapping) and str(item.get("run_id") or "") == run_id
    ]
    if not decisions:
        return None
    total = 0
    for decision in decisions:
        requested = decision.get("requested_at")
        # 升级前的单次审批只在 State 留有最近请求时刻；多次旧审批无法可靠还原。
        if not isinstance(requested, datetime) and len(decisions) == 1:
            requested = state.get("approval_requested_at")
        decided = decision.get("created_at")
        if not isinstance(requested, datetime) or not isinstance(decided, datetime):
            return None
        total += max(0, round((decided - requested).total_seconds() * 1000))
    return total


def build_runtime_context(
    *,
    app_config: RunConfig,
    model_adapter: BaseChatModel,
    workspace_root: Path,
    current_cwd: Path | None = None,
    security_policy: SecurityPolicy | None = None,
    external_policy: ExternalPathPolicy | None = None,
    tool_registry: ToolRegistry | None = None,
    model_capabilities: ModelCapabilities | None = None,
    user_id: str | None = None,
    config_profile: str = "default",
    request_id: str | None = None,
    provider_name: str | None = None,
    usage_recorder: ModelCallRecorder | None = None,
) -> RuntimeContext:
    """构造单次调用的运行上下文（架构 §4.1）。

    Args:
        app_config: 本次运行使用的完整配置。
        model_adapter: 本次调用使用的模型实例。
        workspace_root: 工作区根目录；AGENTS 扫描与文件工具的边界。
        current_cwd: 当前工作目录，默认取进程当前目录。
        security_policy: 安全策略；省略时按配置构造。
        external_policy: 工作区外路径的档位与授权（T38）。界面上可改的档位存在设置库里，
            应用层每轮现取后从这里传进来；省略时取 ``config.yaml`` 的出厂默认。
        tool_registry: 工具注册表；省略时按策略构造。
        model_capabilities: 模型能力；省略时现探测一次（架构 §11.7）。
        user_id: 逻辑用户；单机单用户时为 ``None``。
        config_profile: 配置档位名。
        request_id: 本次请求标识，默认自动生成。
        provider_name: 能力探测用的模型名；省略时用 ``app_config.llm.model``。
            **按连接运行时（§6.8）必须显式传连接里的模型 ID**：那才是这次真正要打的模型。
        usage_recorder: 本轮用量录音机（§15.6）；省略时所有模型调用只走估算、不落实际用量。

    Returns:
        可直接传给 ``graph.invoke(..., context=...)`` 的上下文对象。

    Raises:
        ConfigError: 配置要求放开工作区边界（本版本不支持）。
    """
    policy = security_policy or SecurityPolicy.from_config(
        app_config, workspace_root=workspace_root, external_policy=external_policy
    )
    registry = tool_registry or ToolRegistry.build(policy, web_access=app_config.web_access)
    capabilities = model_capabilities or probe_capabilities(
        model_adapter, provider_name=provider_name or app_config.llm.model
    )
    return RuntimeContext(
        workspace_root=workspace_root,
        current_cwd=Path.cwd() if current_cwd is None else current_cwd,
        model_adapter=model_adapter,
        app_config=app_config,
        user_id=user_id,
        config_profile=config_profile,
        agent_profile=app_config.profiles.active,
        # 有效能力 = Profile 声明 ∩ 配置开关：关掉的开关对应的能力对模型完全不可见（架构 §16.3）。
        agent_capabilities=effective_capabilities(
            app_config.profiles.capabilities(),
            web_access_enabled=app_config.web_access.enabled,
        ),
        security_policy=policy,
        tool_registry=registry,
        model_capabilities=capabilities,
        current_time=datetime.now(timezone.utc),
        request_id=request_id or uuid4().hex,
        usage_recorder=usage_recorder,
    )


def read_thread_state(graph: ChatGraph, thread_id: str) -> dict[str, Any]:
    """读取某个 ``thread_id`` 的当前状态值（Checkpoint 恢复）。

    Args:
        graph: 已编译的图，需带 checkpointer。
        thread_id: 会话键。

    Returns:
        当前状态值字典；该 thread 没有 Checkpoint 时返回空字典。
    """
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    return dict(snapshot.values or {})

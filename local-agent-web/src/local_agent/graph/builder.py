"""图构建（开发文档 T05）。

模型、配置与安全策略都不放在图里，而是随每次调用的 ``RuntimeContext`` 注入
（架构 §4.1）：同一张编译好的图既能用真实模型，也能用测试假模型与假策略。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from local_agent.config import AppConfig
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
    tool_execution_node,
)
from local_agent.graph.routers import (
    COMPLETION_ROUTER,
    MODE_ROUTER,
    completion_router_node,
    mode_router_node,
)
from local_agent.graph.state import AgentState, RuntimeContext
from local_agent.llm.models import ModelCapabilities, probe_capabilities
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
    graph.add_node(PREPARE_CONTEXT, prepare_context_node)
    graph.add_node(LOAD_AGENTS, load_agents_node)
    graph.add_node(RECALL_MEMORY, recall_memory_node)
    graph.add_node(MODE_ROUTER, mode_router_node)
    graph.add_node(RETRIEVE_CODE_NODE, retrieve_code_node)
    graph.add_node(COMPRESSION_NODE, compression_node)
    graph.add_node(PLANNER_NODE, planner_node)
    graph.add_node(AGENT_DECISION_NODE, agent_decision_node)
    graph.add_node(SAFETY_GATE_NODE, safety_gate_node)
    graph.add_node(APPROVAL_INTERRUPT_NODE, approval_interrupt_node)
    graph.add_node(TOOL_EXECUTION_NODE, tool_execution_node)
    graph.add_node(REFLECTION_NODE, reflection_node)
    graph.add_node(COMPLETION_ROUTER, completion_router_node)
    graph.add_node(MEMORY_WRITE_NODE, memory_write_node)
    graph.add_node(CHAT_NODE, chat_node)
    graph.add_node(FINAL_RESPONSE_NODE, final_response_node)
    add_minimal_chat_edges(graph)
    return graph.compile(checkpointer=checkpointer)


def build_runtime_context(
    *,
    app_config: AppConfig,
    model_adapter: BaseChatModel,
    workspace_root: Path,
    current_cwd: Path | None = None,
    security_policy: SecurityPolicy | None = None,
    tool_registry: ToolRegistry | None = None,
    model_capabilities: ModelCapabilities | None = None,
    user_id: str | None = None,
    config_profile: str = "default",
    request_id: str | None = None,
) -> RuntimeContext:
    """构造单次调用的运行上下文（架构 §4.1）。

    Args:
        app_config: 本次运行使用的完整配置。
        model_adapter: 本次调用使用的模型实例。
        workspace_root: 工作区根目录；AGENTS 扫描与文件工具的边界。
        current_cwd: 当前工作目录，默认取进程当前目录。
        security_policy: 安全策略；省略时按配置构造。
        tool_registry: 工具注册表；省略时按策略构造。
        model_capabilities: 模型能力；省略时现探测一次（架构 §11.7）。
        user_id: 逻辑用户；单机单用户时为 ``None``。
        config_profile: 配置档位名。
        request_id: 本次请求标识，默认自动生成。

    Returns:
        可直接传给 ``graph.invoke(..., context=...)`` 的上下文对象。

    Raises:
        ConfigError: 配置要求放开工作区边界（本版本不支持）。
    """
    policy = security_policy or SecurityPolicy.from_config(
        app_config, workspace_root=workspace_root
    )
    registry = tool_registry or ToolRegistry.build(policy)
    capabilities = model_capabilities or probe_capabilities(
        model_adapter, provider_name=app_config.llm.model
    )
    return RuntimeContext(
        workspace_root=workspace_root,
        current_cwd=Path.cwd() if current_cwd is None else current_cwd,
        model_adapter=model_adapter,
        app_config=app_config,
        user_id=user_id,
        config_profile=config_profile,
        security_policy=policy,
        tool_registry=registry,
        model_capabilities=capabilities,
        current_time=datetime.now(timezone.utc),
        request_id=request_id or uuid4().hex,
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

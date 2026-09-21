"""图的边（开发文档 T05）。

当前形状：

```text
START → prepare_context → load_agents → recall_memory → mode_router
mode_router --chat--> compression_node → chat_node → reflection_node → completion_router
completion_router --有记忆候选--> memory_write_node → final_response_node → END
completion_router --其余--> final_response_node → END
mode_router --tool--> planner_node → compression_node → agent_decision_node
agent_decision_node --有工具调用--> safety_gate_node
safety_gate_node --有放行调用--> tool_execution_node → reflection_node → completion_router
safety_gate_node --全部被拒-->   reflection_node → completion_router
agent_decision_node --无工具调用--> final_response_node
completion_router --continue--> planner_node（§5.3 要求继续时重新规划）
completion_router --retry-->    compression_node
completion_router --ask_user/finish/loop_limit--> final_response_node
completion_router --有记忆候选的 finish--> memory_write_node → final_response_node
```
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

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
    SAFETY_GATE_NODE,
    TOOL_EXECUTION_NODE,
)
from local_agent.graph.routers import (
    COMPLETION_ROUTER,
    MODE_ROUTER,
    ROUTE_APPROVE,
    ROUTE_ASK_USER,
    ROUTE_CHAT,
    ROUTE_CONTINUE,
    ROUTE_EDIT,
    ROUTE_EXECUTE,
    ROUTE_FINISH,
    ROUTE_LOOP_LIMIT,
    ROUTE_MEMORY_WRITE,
    ROUTE_REFLECT,
    ROUTE_REJECT,
    ROUTE_RETRY,
    ROUTE_TO_APPROVAL,
    ROUTE_TOOL,
    ROUTE_WITH_TOOLS,
    ROUTE_WITHOUT_TOOLS,
    completion_route,
    route_after_approval,
    route_after_compression,
    route_after_decision,
    route_after_safety,
    route_by_mode,
)
from local_agent.graph.state import AgentState, RuntimeContext

#: 图在编译前的类型（State = AgentState，Context = RuntimeContext）。
ChatStateGraph = StateGraph[AgentState, RuntimeContext, AgentState, AgentState]


def add_minimal_chat_edges(graph: ChatStateGraph) -> None:
    """写入当前阶段的全部边，含闲聊分支与工具分支。"""
    graph.add_edge(START, PREPARE_CONTEXT)
    graph.add_edge(PREPARE_CONTEXT, LOAD_AGENTS)
    graph.add_edge(LOAD_AGENTS, RECALL_MEMORY)
    graph.add_edge(RECALL_MEMORY, MODE_ROUTER)

    graph.add_conditional_edges(
        MODE_ROUTER,
        route_by_mode,
        {ROUTE_CHAT: COMPRESSION_NODE, ROUTE_TOOL: PLANNER_NODE},
    )
    graph.add_edge(PLANNER_NODE, COMPRESSION_NODE)
    graph.add_conditional_edges(
        COMPRESSION_NODE,
        route_after_compression,
        {ROUTE_CHAT: CHAT_NODE, ROUTE_TOOL: AGENT_DECISION_NODE},
    )

    graph.add_conditional_edges(
        AGENT_DECISION_NODE,
        route_after_decision,
        {
            ROUTE_WITH_TOOLS: SAFETY_GATE_NODE,
            ROUTE_WITHOUT_TOOLS: FINAL_RESPONSE_NODE,
            # 计划还没走完时先去反思（多步任务的第二步不能直接收尾）
            ROUTE_REFLECT: REFLECTION_NODE,
        },
    )
    graph.add_conditional_edges(
        SAFETY_GATE_NODE,
        route_after_safety,
        {
            ROUTE_EXECUTE: TOOL_EXECUTION_NODE,
            ROUTE_REFLECT: REFLECTION_NODE,
            ROUTE_TO_APPROVAL: APPROVAL_INTERRUPT_NODE,
        },
    )
    graph.add_conditional_edges(
        APPROVAL_INTERRUPT_NODE,
        route_after_approval,
        {
            ROUTE_APPROVE: TOOL_EXECUTION_NODE,
            ROUTE_EDIT: PLANNER_NODE,
            ROUTE_REJECT: FINAL_RESPONSE_NODE,
        },
    )
    graph.add_edge(TOOL_EXECUTION_NODE, REFLECTION_NODE)
    graph.add_edge(REFLECTION_NODE, COMPLETION_ROUTER)
    graph.add_conditional_edges(
        COMPLETION_ROUTER,
        completion_route,
        {
            ROUTE_CONTINUE: PLANNER_NODE,
            ROUTE_RETRY: COMPRESSION_NODE,
            ROUTE_ASK_USER: FINAL_RESPONSE_NODE,
            ROUTE_FINISH: FINAL_RESPONSE_NODE,
            ROUTE_LOOP_LIMIT: FINAL_RESPONSE_NODE,
            ROUTE_MEMORY_WRITE: MEMORY_WRITE_NODE,
        },
    )
    graph.add_edge(MEMORY_WRITE_NODE, FINAL_RESPONSE_NODE)

    # 闲聊也要过一次反思校验：按架构 §5.2，识别到用户要求记住时 chat_node 只产生
    # memory_write_intent，**转入现有 reflection_node 校验**，再由 completion_router 决定是否
    # 走 memory_write_node。普通闲聊在反思里得到空候选 → 直接进 final_response_node。
    graph.add_edge(CHAT_NODE, REFLECTION_NODE)
    graph.add_edge(FINAL_RESPONSE_NODE, END)

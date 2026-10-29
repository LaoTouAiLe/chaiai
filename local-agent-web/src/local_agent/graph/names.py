"""图节点名与跨模块共享的常量（2026-10-26 从 `nodes.py` 拆出）。

为什么单独一个模块：拆分后 `nodes.py` 与 `snapshot.py` 都要用节点名；常量留在任一方，
另一方 import 它就会形成循环。所以放在两者之上、谁也不依赖的位置。
"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

# `__all__` 不是装饰：这些名字**只被别的模块用**（`nodes.py` 从这里再导出，`snapshot.py`
# 直接引用其中的节点名）。不声明的话，ruff 会把只在这里定义、却在本文件没用到的
# `_MessageContent` 判成死代码（PYI047）。
__all__ = [
    "AGENT_DECISION_NODE",
    "APPROVAL_INTERRUPT_NODE",
    "CHAT_NODE",
    "COMPRESSION_NODE",
    "FINAL_RESPONSE_NODE",
    "LOAD_AGENTS",
    "MEMORY_WRITE_NODE",
    "PLANNER_NODE",
    "PREPARE_CONTEXT",
    "QUESTION_INTERRUPT_NODE",
    "RECALL_MEMORY",
    "REFLECTION_NODE",
    "REJECTED_REPLY",
    "RETRIEVE_CODE_NODE",
    "SAFETY_GATE_NODE",
    "TASK_MEMORY_NODE",
    "TOOL_EXECUTION_NODE",
    "TOOL_FEEDBACK_NODE",
    "_STALE_SIDE_EFFECT_NOTE",
    "ReflectionAction",
    "_MessageContent",
]

PREPARE_CONTEXT = "prepare_context"
LOAD_AGENTS = "load_agents"
RECALL_MEMORY = "recall_memory"
RETRIEVE_CODE_NODE = "retrieve_code_node"
COMPRESSION_NODE = "compression_node"
PLANNER_NODE = "planner_node"
AGENT_DECISION_NODE = "agent_decision_node"
SAFETY_GATE_NODE = "safety_gate_node"
APPROVAL_INTERRUPT_NODE = "approval_interrupt_node"
QUESTION_INTERRUPT_NODE = "question_interrupt_node"
TOOL_EXECUTION_NODE = "tool_execution_node"
REFLECTION_NODE = "reflection_node"
MEMORY_WRITE_NODE = "memory_write_node"
TASK_MEMORY_NODE = "task_memory_node"
CHAT_NODE = "chat_node"
FINAL_RESPONSE_NODE = "final_response_node"
TOOL_FEEDBACK_NODE = "tool_feedback_node"

#: 人工拒绝后的可见说明（不自动重试，架构 §5.3）。
REJECTED_REPLY = (
    "该动作已被你拒绝，我不会执行，也不会自动重试。"
    "如果希望换个做法，可以补充说明后让我重新规划。"
)

#: 反思给出的下一步动作（架构 §4.3 的 ``ReflectionResult.next_action``）。
ReflectionAction = Literal["continue", "retry", "ask_user", "finish"]

#: LangChain 消息的 content 契约：纯文本，或字符串与多模态内容块的列表。
_MessageContent: TypeAlias = str | list[str | dict[str, Any]]

#: 上次运行的副作用没有确认结果时给模型的回填内容（架构 §4.2）。
#: 写清"为什么没执行"与"怎么继续"，否则模型只会把失败原样转述给用户。
_STALE_SIDE_EFFECT_NOTE = (
    "{tool} 没有执行：上一次运行放行了同一类动作，但结果没有写进 Checkpoint，"
    "副作用可能已经发生。为避免重复执行，本次不自动重放。"
    "请先核对文件内容或命令影响；确认后再让我执行一次。"
)


# --------------------------------------------------------------------------- 闲聊分支

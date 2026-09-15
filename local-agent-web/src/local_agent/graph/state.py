"""LangGraph State 与运行上下文（架构 §4）。

T02 只跑最小闲聊子图，但 State 字段按架构 §4.2 一次性声明完整：后续任务
（T05 工具、T08 幂等、T10 审批、T18 记忆）只写入这些字段，不再新增。

Reducer 约定（架构 §4.2）：

- 覆盖语义的字段直接返回值。
- ``messages`` 使用 ``add_messages``；追加型列表使用 ``operator.add``，
  写入方需按事件 ID 去重（``operator.add`` 本身不去重）。
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, ToolCall
from langgraph.graph.message import add_messages

from local_agent.config import AppConfig
from local_agent.llm.models import ModelCapabilities
from local_agent.memory.schemas import MemoryReference
from local_agent.security.approval import ApprovalDecision, ApprovalRequest
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.result_types import ToolResultRecord
from local_agent.tools.tool_registry import ToolRegistry

AgentMode = Literal["chat", "tool"]
TaskStatus = Literal["pending", "running", "waiting_approval", "completed", "failed"]
EvidenceType = Literal["file_hash", "diff", "exit_code", "stdout", "test_result"]

# 架构 §4.3 未给出具体字段的结构：仍为字典占位的部分由对应任务替换为具体类型
# （MemoryReference 在 T14B 定稿，见 memory/schemas.py；TokenUsage/ContextBudget 在 T16，
# RAGReference 在 T21）。审批请求与决策已由 T10 定义（见 security/approval.py）。
RAGReference = dict[str, Any]
TokenUsage = dict[str, Any]
ContextBudget = dict[str, Any]


class DecisionRecord(TypedDict):
    """决策轨迹记录（架构 §4.3 未给出字段，T08 定义最小集合）。

    ``record_id`` 由 ``run_id + 阶段 + 循环轮次 + 工具调用 ID`` 派生，因此同一次运行内
    重放同一节点不会产生重复记录（架构 §4.2 要求写入方按事件 ID 去重）。
    """

    record_id: str
    stage: str
    summary: str
    tool_call_id: str | None
    created_at: datetime


class ErrorEvent(TypedDict):
    """错误事件（架构 §4.3 未给出字段，T08 定义最小集合）。

    ``run_id`` 标明它属于哪一轮：反思只需汇总**当前任务**的异常，历史异常不再混入。
    """

    event_id: str
    stage: str
    message: str
    tool_call_id: str | None
    run_id: str
    created_at: datetime


T = TypeVar("T")


def merge_appended(existing: Sequence[T], incoming: Sequence[T], key: Callable[[T], str]) -> list[T]:
    """按事件 ID 过滤新增条目（架构 §4.2：``operator.add`` 自身不去重）。

    Args:
        existing: 当前 State 中已有的条目。
        incoming: 本节点想要追加的条目。
        key: 从条目取出事件 ID 的函数。

    Returns:
        只包含尚未出现过的事件 ID 的条目，顺序保持不变。
    """
    seen = {key(item) for item in existing}
    fresh: list[T] = []
    for item in incoming:
        item_key = key(item)
        if item_key in seen:
            continue
        seen.add(item_key)
        fresh.append(item)
    return fresh


def tool_scope_key(run_id: str, tool_call_id: str) -> str:
    """工具调用的作用域键：``run_id`` + ``tool_call_id``（架构 §4.2、§4.3）。

    同一次运行内重放同一个调用 → 同一个键，据此去重；不同轮次即使复用了同一个
    ``tool_call_id``，也是不同的键：既不会被误判成"已执行"而跳过，也不会把新结果
    当成重复项丢掉。
    """
    return f"{run_id}:{tool_call_id}"


def tool_event_key(event: ToolEvent) -> str:
    """工具事件的去重键（按轮次作用域）。"""
    return tool_scope_key(event.run_id, event.tool_call_id)


def tool_result_key(record: ToolResultRecord) -> str:
    """工具结果的去重键（按轮次作用域）。

    读 ``run_id`` 用 ``get``：升级前的 Checkpoint 里没有这个字段，缺省按空轮次处理
    （与其他轮次都不同，因此不会被误当成重复项丢掉）。
    """
    return tool_scope_key(record.get("run_id") or "", record["tool_call_id"])


def decision_record_key(record: DecisionRecord) -> str:
    """决策轨迹的去重键。"""
    return record["record_id"]


def error_event_key(record: ErrorEvent) -> str:
    """错误事件的去重键。"""
    return record["event_id"]


def approval_key(record: ApprovalDecision) -> str:
    """审批记录的去重键：同一次审批请求只记录一次决策。"""
    return record["approval_id"]


def make_decision_record(
    run_id: str,
    *,
    stage: str,
    summary: str,
    loop_count: int = 0,
    tool_call_id: str | None = None,
) -> DecisionRecord:
    """构造一条决策轨迹记录（只放简短摘要，不放模型的隐藏思维链）。"""
    salt = f"{loop_count}:{tool_call_id or ''}"
    return DecisionRecord(
        record_id=f"{run_id}:decision:{stage}:{salt}",
        stage=stage,
        summary=summary,
        tool_call_id=tool_call_id,
        created_at=datetime.now(timezone.utc),
    )


def make_error_event(
    run_id: str,
    *,
    stage: str,
    message: str,
    loop_count: int = 0,
    tool_call_id: str | None = None,
) -> ErrorEvent:
    """构造一条错误事件。"""
    salt = f"{loop_count}:{tool_call_id or ''}"
    return ErrorEvent(
        event_id=f"{run_id}:error:{stage}:{salt}",
        stage=stage,
        message=message,
        tool_call_id=tool_call_id,
        run_id=run_id,
        created_at=datetime.now(timezone.utc),
    )


@dataclass(frozen=True)
class PlanStep:
    """架构 §4.3；``status`` 的取值集合由 T20 确定。"""

    id: str
    description: str
    status: str = "pending"
    dependencies: tuple[str, ...] = ()
    expected_evidence: str = ""
    retry_count: int = 0
    result_summary: str = ""


@dataclass(frozen=True)
class ToolEvent:
    """架构 §4.3；``status`` 的取值集合由 T05 确定。"""

    tool_call_id: str
    tool_name: str
    redacted_arguments: dict[str, Any] = field(default_factory=dict)
    approval_required: bool = False
    approval_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str = "pending"
    output_preview: str = ""
    output_digest: str = ""
    #: 所属轮次；与 ``tool_call_id`` 一起构成事件作用域（架构 §4.2、§4.3）。
    run_id: str = ""


@dataclass(frozen=True)
class EvidenceReference:
    """架构 §4.3 的证据引用。"""

    source_event_id: str
    evidence_type: EvidenceType
    summary: str
    verified: bool = False
    created_at: datetime | None = None


@dataclass(frozen=True)
class MemoryCandidate:
    """架构 §4.3 的记忆候选；作用域由 memory_write_node 依据 RuntimeContext 推导。"""

    memory_type: Literal["conversation_summary", "task_fragment", "entity_fact"]
    content: str
    subject: str = ""
    importance: float = 0.5
    confidence: float = 0.5
    source_event_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None


@dataclass(frozen=True)
class ReflectionResult:
    """架构 §4.3 的反思结果；是否 verified 由反射层机械校验决定（T08/T17）。"""

    goal_achieved: bool
    result_trust: Literal["verified", "uncertain", "failed"]
    anomalies: tuple[str, ...] = ()
    plan_adjustment: str | None = None
    next_action: Literal["continue", "retry", "ask_user", "finish"] = "finish"
    memory_candidates: tuple[MemoryCandidate, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()


class AgentState(TypedDict, total=False):
    """短期记忆 State（架构 §4.2）。``total=False`` 允许节点只提交局部更新。"""

    # 会话标识（覆盖语义）
    thread_id: str
    session_id: str
    run_id: str

    # 本轮输入与路由（覆盖语义）
    user_input: str
    normalized_request: str
    mode: AgentMode
    route_reason: str

    # 消息与摘要
    messages: Annotated[list[AnyMessage], add_messages]
    conversation_summary: str | None
    context_version: int

    # 任务与计划
    task_goal: str | None
    task_status: TaskStatus
    plan: list[PlanStep]
    current_step_index: int
    completed_steps: Annotated[list[str], operator.add]

    # 工具调用与结果
    pending_tool_calls: list[ToolCall]
    tool_events: Annotated[list[ToolEvent], operator.add]
    tool_results: Annotated[list[ToolResultRecord], operator.add]

    # 决策与反思
    decision_trace: Annotated[list[DecisionRecord], operator.add]
    reflection_result: ReflectionResult | None

    # 人工审批
    pending_approval: ApprovalRequest | None
    approval_history: Annotated[list[ApprovalDecision], operator.add]

    # 记忆与检索
    memory_refs: list[MemoryReference]
    memory_write_candidates: Annotated[list[MemoryCandidate], operator.add]
    rag_refs: list[RAGReference]

    # 错误与循环控制
    error_events: Annotated[list[ErrorEvent], operator.add]
    retry_count: int
    loop_count: int
    max_loop_count: int

    # 预算与压缩
    token_usage: TokenUsage
    context_budget: ContextBudget
    compression_count: int

    # 输出
    final_response: str | None
    final_evidence: list[EvidenceReference]


@dataclass
class RuntimeContext:
    """单次调用的运行上下文（架构 §4.1）；通常不写入 Checkpoint。

    ``model_capabilities`` 与 ``approval_channel`` 分别由 T09、T10 给出具体类型，
    当前保持为空。
    """

    workspace_root: Path
    current_cwd: Path
    model_adapter: BaseChatModel
    #: 本次运行的完整配置；节点按需读取（agents、安全策略、循环上限等）。
    app_config: AppConfig
    user_id: str | None = None
    config_profile: str = "default"
    #: 合并后的 AGENTS 规则文本，由 load_agents 节点写入（架构 §4.1、§7.2）。
    agents_context: str | None = None
    #: 启动时探测到的模型能力（架构 §11.7）；工具模式据此失败闭合。
    model_capabilities: ModelCapabilities | None = None
    tool_registry: ToolRegistry | None = None
    security_policy: SecurityPolicy | None = None
    approval_channel: object | None = None
    current_time: datetime | None = None
    request_id: str = ""

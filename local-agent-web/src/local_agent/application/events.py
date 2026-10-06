"""事件契约与实时序列（ARCHITECTURE §15.4）。

最小事件集合由契约固定（19 种）。每个事件都带 `schema_version` / `event_id` / `event_type` /
`session_id` / `task_id` / `run_id` / `occurred_at` / `stream_sequence` / `payload`。

三条不可协商的性质：

1. **事件是单向投影**：构造事件不写回 State、不调用模型、不执行工具。
2. **`event_id` 必须稳定**：由"稳定来源标识 + 事件类型"确定，同一 Checkpoint 重放得到相同 id；
   流式增量用 `model_call_id + chunk_sequence` 去重；**不得**使用每次渲染随机生成的 UUID。
3. **`stream_sequence` 只是进程内顺序**：按会话单调递增，服务重启后允许重新计数——它不是持久游标，
   因此本模块**不定义** `after_cursor`。

`job_queued / job_progress / job_waiting / job_completed / task_cancelled` 属 v0.6，这里不得预留。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from local_agent.application.errors import ErrorView
from local_agent.application.models import (
    SCHEMA_VERSION,
    ApprovalView,
    FinalResultView,
    MemoryUseView,
    ModelCallRecordView,
    PlanStepStatus,
    PlanView,
    SessionSnapshot,
    ToolStatus,
    ToolUseView,
    _require_timezone,
)

EventType = Literal[
    "session_snapshot",
    "message_started",
    "message_delta",
    "message_completed",
    "plan_updated",
    "step_started",
    "step_completed",
    "tool_started",
    "tool_output",
    "tool_completed",
    "approval_required",
    "approval_resolved",
    "memory_recalled",
    "memory_written",
    "model_call_started",
    "model_usage_reported",
    "model_call_completed",
    "task_completed",
    "task_failed",
]

#: 契约里的事件类型全集；测试用它对照 ARCHITECTURE §15.4，防止悄悄增删。
EVENT_TYPES: Final[tuple[EventType, ...]] = (
    "session_snapshot",
    "message_started",
    "message_delta",
    "message_completed",
    "plan_updated",
    "step_started",
    "step_completed",
    "tool_started",
    "tool_output",
    "tool_completed",
    "approval_required",
    "approval_resolved",
    "memory_recalled",
    "memory_written",
    "model_call_started",
    "model_usage_reported",
    "model_call_completed",
    "task_completed",
    "task_failed",
)

#: 每个事件都有的字段（§15.4）。
COMMON_EVENT_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "event_id",
    "event_type",
    "session_id",
    "task_id",
    "run_id",
    "occurred_at",
    "stream_sequence",
    "payload",
)

#: v0.6 才可能出现的类型：v0.4 不得实现，测试据此守卫。
RESERVED_FOR_V06: Final[tuple[str, ...]] = (
    "job_queued",
    "job_progress",
    "job_waiting",
    "job_completed",
    "task_cancelled",
)


def event_id_for(event_type: EventType, identity: str) -> str:
    """由事件类型与稳定来源标识派生 `event_id`。

    `identity` 必须是**稳定来源标识**，不是每次渲染都会变的值：

    - 消息：`message_id`（流式增量用 `model_call_id#chunk_sequence`，见 `stream_delta_identity`）
    - 工具：`run_id:tool_call_id`（架构 §4.2 的轮次作用域——跨轮复用同一个调用 id 时不是同一件事）
    - 审批：`approval_id`；步骤：`step_id`；模型调用：`model_call_id`
    - 会话快照：`snapshot_id`

    `identity` 为空字符串会被拒绝：宁可报错，也不要一个"看起来稳定"的随机 id。
    """
    if not identity:
        msg = "event_id 需要非空的稳定来源标识"
        raise ValueError(msg)
    material = f"{SCHEMA_VERSION}|{event_type}|{identity}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


def stream_delta_identity(model_call_id: str, chunk_sequence: int) -> str:
    """流式增量的稳定标识：`model_call_id + chunk_sequence`（§15.4）。"""
    if not model_call_id:
        msg = "流式增量需要 model_call_id"
        raise ValueError(msg)
    if chunk_sequence < 0:
        msg = "chunk_sequence 不能为负"
        raise ValueError(msg)
    return f"{model_call_id}#{chunk_sequence}"


class StreamSequencer:
    """进程内、按会话的 `stream_sequence` 分配器。

    只保证"当前进程、当前会话实时流内"的顺序（§15.4）：新进程从 1 重新开始，**不是**持久游标，
    因此没有 `after_cursor` 之类的接口。
    """

    def __init__(self) -> None:
        self._last: dict[str, int] = {}
        self._lock = threading.Lock()

    def next(self, session_id: str) -> int:
        """分配下一个序号（从 1 开始）。"""
        if not session_id:
            msg = "stream_sequence 需要 session_id"
            raise ValueError(msg)
        with self._lock:
            value = self._last.get(session_id, 0) + 1
            self._last[session_id] = value
            return value

    def peek(self, session_id: str) -> int:
        """已分配到的序号；从未分配过返回 0。"""
        with self._lock:
            return self._last.get(session_id, 0)

    def forget(self, session_id: str) -> None:
        """会话结束或订阅者全部断开时丢弃计数。"""
        with self._lock:
            self._last.pop(session_id, None)


class _Envelope(BaseModel):
    """事件信封的公共部分。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    occurred_at: datetime
    stream_sequence: int = Field(ge=1)

    @field_validator("occurred_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _require_timezone(value)


class _TaskEnvelope(_Envelope):
    """任务级事件：`task_id` 与 `run_id` 必须非空（§15.4）。"""

    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)


# ---- 载荷 ------------------------------------------------------------------


class MessageRefPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message_id: str = Field(min_length=1)
    role: Literal["user", "assistant"]


class MessageStartedPayload(MessageRefPayload):
    model_call_id: str | None = None
    reset: bool = False
    chunk_sequence: int = Field(default=0, ge=0)


class MessageDeltaPayload(MessageRefPayload):
    model_call_id: str = Field(min_length=1)
    chunk_sequence: int = Field(ge=0)
    text_delta: str
    channel: Literal["text", "reasoning"] = "text"
    reasoning_truncated: bool = False


class MessageCompletedPayload(MessageRefPayload):
    text: str
    reasoning: str | None = None
    reasoning_truncated: bool = False
    model_call_id: str | None = None
    complete: bool = True


class PlanUpdatedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    plan: PlanView


class StepStartedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    step_id: str = Field(min_length=1)
    title: str = Field(min_length=1)


class StepCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    step_id: str = Field(min_length=1)
    status: PlanStepStatus


class ToolStartedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tool: ToolUseView


class ToolOutputPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tool_call_id: str = Field(min_length=1)
    #: 有界预览；完整输出留在本地，页面不显示无界 stdout/stderr（§15.8）。
    output_preview: str
    truncated: bool = False


class ToolCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tool_call_id: str = Field(min_length=1)
    status: ToolStatus
    output_digest: str | None = Field(default=None, exclude=True)
    elapsed_ms: int | None = Field(default=None, ge=0)


class ApprovalRequiredPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    approval: ApprovalView


class ApprovalResolvedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    approval_id: str = Field(min_length=1)
    state: Literal["approved", "rejected", "expired"]
    note: str | None = None


class MemoryRecalledPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    memory: MemoryUseView


class MemoryWrittenPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    outcome: Literal["written", "failed"]
    detail: str


class ModelCallStartedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model_call_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    connection_id: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)


class ModelUsagePayload(BaseModel):
    """供应商用量上报。没返回的字段是 `None`，不是 0（§15.6）。"""

    model_config = ConfigDict(extra="ignore")
    model_call_id: str = Field(min_length=1)
    actual_uncached_input_tokens: int | None = Field(default=None, ge=0)
    actual_cache_read_tokens: int | None = Field(default=None, ge=0)
    actual_cache_write_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    actual_reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_metrics_reported: bool = False


class ModelCallCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model_call_id: str = Field(min_length=1)
    status: Literal["ok", "error", "aborted"]
    model_elapsed_ms: int | None = Field(default=None, ge=0)
    time_to_first_token_ms: int | None = Field(default=None, ge=0)


class TaskCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    final_result: FinalResultView


class TaskFailedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error: ErrorView


# ---- 具体事件 --------------------------------------------------------------


class SessionSnapshotEvent(_Envelope):
    """边界快照。空会话没有当前任务，因此 `task_id` / `run_id` 允许为 `None`（§15.4）。"""

    event_type: Literal["session_snapshot"] = "session_snapshot"
    task_id: str | None = None
    run_id: str | None = None
    payload: SessionSnapshot


class MessageStartedEvent(_TaskEnvelope):
    event_type: Literal["message_started"] = "message_started"
    payload: MessageStartedPayload


class MessageDeltaEvent(_TaskEnvelope):
    event_type: Literal["message_delta"] = "message_delta"
    payload: MessageDeltaPayload


class MessageCompletedEvent(_TaskEnvelope):
    event_type: Literal["message_completed"] = "message_completed"
    payload: MessageCompletedPayload


class PlanUpdatedEvent(_TaskEnvelope):
    event_type: Literal["plan_updated"] = "plan_updated"
    payload: PlanUpdatedPayload


class StepStartedEvent(_TaskEnvelope):
    event_type: Literal["step_started"] = "step_started"
    payload: StepStartedPayload


class StepCompletedEvent(_TaskEnvelope):
    event_type: Literal["step_completed"] = "step_completed"
    payload: StepCompletedPayload


class ToolStartedEvent(_TaskEnvelope):
    event_type: Literal["tool_started"] = "tool_started"
    payload: ToolStartedPayload


class ToolOutputEvent(_TaskEnvelope):
    event_type: Literal["tool_output"] = "tool_output"
    payload: ToolOutputPayload


class ToolCompletedEvent(_TaskEnvelope):
    event_type: Literal["tool_completed"] = "tool_completed"
    payload: ToolCompletedPayload


class ApprovalRequiredEvent(_TaskEnvelope):
    event_type: Literal["approval_required"] = "approval_required"
    payload: ApprovalRequiredPayload


class ApprovalResolvedEvent(_TaskEnvelope):
    event_type: Literal["approval_resolved"] = "approval_resolved"
    payload: ApprovalResolvedPayload


class MemoryRecalledEvent(_TaskEnvelope):
    event_type: Literal["memory_recalled"] = "memory_recalled"
    payload: MemoryRecalledPayload


class MemoryWrittenEvent(_TaskEnvelope):
    event_type: Literal["memory_written"] = "memory_written"
    payload: MemoryWrittenPayload


class ModelCallStartedEvent(_TaskEnvelope):
    event_type: Literal["model_call_started"] = "model_call_started"
    payload: ModelCallStartedPayload


class ModelUsageReportedEvent(_TaskEnvelope):
    event_type: Literal["model_usage_reported"] = "model_usage_reported"
    payload: ModelUsagePayload


class ModelCallCompletedEvent(_TaskEnvelope):
    event_type: Literal["model_call_completed"] = "model_call_completed"
    payload: ModelCallCompletedPayload


class TaskCompletedEvent(_TaskEnvelope):
    event_type: Literal["task_completed"] = "task_completed"
    payload: TaskCompletedPayload


class TaskFailedEvent(_TaskEnvelope):
    event_type: Literal["task_failed"] = "task_failed"
    payload: TaskFailedPayload


EventEnvelope = Annotated[
    SessionSnapshotEvent
    | MessageStartedEvent
    | MessageDeltaEvent
    | MessageCompletedEvent
    | PlanUpdatedEvent
    | StepStartedEvent
    | StepCompletedEvent
    | ToolStartedEvent
    | ToolOutputEvent
    | ToolCompletedEvent
    | ApprovalRequiredEvent
    | ApprovalResolvedEvent
    | MemoryRecalledEvent
    | MemoryWrittenEvent
    | ModelCallStartedEvent
    | ModelUsageReportedEvent
    | ModelCallCompletedEvent
    | TaskCompletedEvent
    | TaskFailedEvent,
    Field(discriminator="event_type"),
]

_EVENT_ADAPTER: Final[TypeAdapter[Any]] = TypeAdapter(EventEnvelope)


def parse_event(data: Mapping[str, Any] | str) -> Any:
    """解析一条映射或 JSON 字符串事件（含未知字段，前向兼容）。

    返回具体的事件模型；未知 `event_type` 会抛 `pydantic.ValidationError`——v0.4 的接收端不该
    假装理解一个自己没实现的事件类型。
    """
    if isinstance(data, str):
        return _EVENT_ADAPTER.validate_json(data)
    return _EVENT_ADAPTER.validate_python(data)


def new_timestamp() -> datetime:
    """当前 UTC 时间。**只用于实时事件**：投影历史 Checkpoint 时必须用记录里的时间。"""
    return datetime.now(timezone.utc)


def model_call_record_payload(record: ModelCallRecordView) -> Mapping[str, Any]:
    """把用量记录转成事件载荷可用的纯数据（不含任何内部对象）。"""
    return record.model_dump(mode="json")

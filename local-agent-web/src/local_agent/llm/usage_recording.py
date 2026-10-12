"""模型调用的用量记录入口（架构 §15.6）。

**本模块是"所有模型调用都必经的唯一记录入口"**。不走包装模型的路线（`with_structured_output`
与 `bind_tools` 返回的是 `Runnable`，包装层会漏掉规划/反思/路由这几条结构化调用），改用
**contextvar 传录音机 + 显式记录点**：谁真正发起一次模型调用，谁就用本模块的
:func:`record_model_call` / :func:`begin_model_call` 包一下。

三层结构：

- :class:`ModelCallRecord`：**一次**模型调用的可持久记录。每次重试生成**新** ``model_call_id``，
  失败尝试不覆盖（§15.6）。
- :class:`ModelCallRecorder`：一次运行内累积这些记录；挂在 ``RuntimeContext`` 上，由节点装饰器
  用 :func:`use_model_recorder` 放进 contextvar，模型调用点再取出来。
- :class:`TurnRecord`：整轮汇总的**身份与时间线**（提交/结束时间、关联的调用与工具事件、
  审批等待）。**Token 汇总不落在这里**——它由 ``model_call_records`` 在投影时确定性加总，
  保证"汇总 = 明细加总"不会因为两处各算一次而不一致。

用途标签（``purpose``）由 :data:`PURPOSE_BY_NODE` 从节点名映射为**产品级**说法：事件会带着
``purpose`` 出门，所以不能是内部节点名（架构 §15.4 事件不得泄露内部结构）。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from local_agent.llm.usage import ActualUsage, extract_usage

__all__ = [
    "DEFAULT_PURPOSE",
    "PURPOSE_BY_NODE",
    "ModelCallHandle",
    "ModelCallRecord",
    "ModelCallRecorder",
    "TurnRecord",
    "begin_model_call",
    "current_purpose",
    "current_recorder",
    "endpoint_fingerprint",
    "finish_model_call",
    "note_first_token",
    "record_model_call",
    "tools_fingerprint",
    "use_model_purpose",
    "use_model_recorder",
]

T = TypeVar("T")

ModelCallStatus = Literal["ok", "error"]

#: 节点名 → 产品级调用用途。不在表里的节点用 :data:`DEFAULT_PURPOSE`。
PURPOSE_BY_NODE: dict[str, str] = {
    "chat_node": "chat",
    "agent_decision_node": "tool_decision",
    "final_response_node": "final_answer",
    "compression_node": "summarize",
    "planner_node": "planning",
    "reflection_node": "reflection",
    "memory_write_node": "memory_intent",
    "task_memory_node": "memory_extraction",
    "retrieve_code_node": "retrieval",
    "mode_router": "routing",
    "completion_router": "routing",
}
DEFAULT_PURPOSE = "chat"


@dataclass(frozen=True)
class ModelCallRecord:
    """一次模型调用的可持久记录（架构 §15.6）。

    估算量（``estimated_prompt_tokens``）与供应商实际量（``actual_*``）分开保存；实际量未知
    一律为 ``None``，明确为零才写 0。
    """

    model_call_id: str
    run_id: str
    purpose: str
    connection_id: str
    config_version: str
    provider: str
    model_id: str
    #: 端点安全指纹（脱敏 URL + 模型名）。不含明文密钥。
    endpoint_fingerprint: str
    #: 这是一次重试时指向**被重试的那次调用**的 id；首次调用为 ``None``。
    retry_of: str | None
    started_at: datetime
    first_token_at: datetime | None
    finished_at: datetime
    status: ModelCallStatus
    estimated_prompt_tokens: int
    actual_uncached_input_tokens: int | None
    actual_cache_read_tokens: int | None
    actual_cache_write_tokens: int | None
    actual_output_tokens: int | None
    actual_reasoning_tokens: int | None
    cache_metrics_reported: bool
    model_elapsed_ms: int
    time_to_first_token_ms: int | None
    context_used_tokens: int
    context_window_tokens: int
    tools_schema_fingerprint: str | None
    system_prompt_fingerprint: str | None = None
    history_prefix_fingerprint: str | None = None
    dynamic_context_fingerprint: str | None = None


@dataclass(frozen=True)
class TurnRecord:
    """整轮汇总的身份与时间线（架构 §15.6）。

    **只存原始事实**：什么时候提交、什么时候结束、终态是什么、关联了哪些模型调用与工具事件、
    审批等待多久。Token 汇总由投影层从 :class:`ModelCallRecord` 加总得到。
    """

    run_id: str
    submitted_at: datetime
    final_at: datetime | None
    #: ``running`` / ``completed`` / ``failed`` / ``blocked``
    final_state: str
    model_call_ids: tuple[str, ...]
    tool_event_ids: tuple[str, ...]
    approval_wait_ms: int | None
    turn_elapsed_ms: int | None
    usage_recorded: bool
    user_message_id: str | None = None


@dataclass
class ModelCallHandle:
    """一次进行中的调用（录音机内部用，调用点只传递它）。"""

    model_call_id: str
    purpose: str
    started_at: datetime
    started_monotonic: float
    estimated_prompt_tokens: int
    context_window_tokens: int
    tools_schema_fingerprint: str | None
    retry_of: str | None
    system_prompt_fingerprint: str | None = None
    history_prefix_fingerprint: str | None = None
    dynamic_context_fingerprint: str | None = None
    first_token_at: datetime | None = None
    first_token_monotonic: float | None = None
    usage: ActualUsage | None = None

    def usage_from(self, value: Any) -> None:
        """尽量从调用结果里提取实际用量（消息本身，或 ``.message``）。"""
        message = value if hasattr(value, "usage_metadata") else getattr(value, "message", None)
        if message is None:
            return
        usage = extract_usage(message)
        if usage is not None:
            self.usage = usage


class ModelCallRecorder:
    """一次运行的用量录音机（挂在 ``RuntimeContext`` 上）。"""

    def __init__(
        self,
        *,
        connection_id: str,
        config_version: str,
        provider: str,
        model_id: str,
        endpoint_fingerprint: str,
        context_window_tokens: int,
    ) -> None:
        self.connection_id = connection_id
        self.config_version = config_version
        self.provider = provider
        self.model_id = model_id
        self.endpoint_fingerprint = endpoint_fingerprint
        self.context_window_tokens = context_window_tokens
        self._run_id = ""
        self._counter = 0
        self._records: list[ModelCallRecord] = []
        self._state_context_fingerprint: str | None = None
        self._snapshot_fingerprint: str | None = None

    def bind_dynamic_context(self, state: Mapping[str, Any]) -> None:
        """只保存动态上下文与压缩版本的指纹，不持久化记忆或 Prompt 正文。

        这是**兜底口径**：T35 起正常情况下由 :meth:`bind_snapshot` 直接提供运行上下文快照的
        指纹，避免"State 字段指纹"与"快照正文"两套口径各说各话。
        """
        fields = (
            "context_version", "compression_count", "conversation_summary",
            "memory_refs", "rag_refs", "task_goal", "plan",
        )
        payload = {key: state.get(key) for key in fields}
        self._state_context_fingerprint = fingerprint_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        )

    def bind_snapshot(self, content_hash: str | None) -> None:
        """绑定当前请求所用的运行上下文快照指纹（§15.7）；置空则回退到 State 字段指纹。"""
        self._snapshot_fingerprint = content_hash

    def bind_run(self, run_id: str) -> None:
        """绑定当前轮次。绑定前发生的调用会落在空 ``run_id`` 下（正常情况下不会发生）。"""
        self._run_id = run_id

    @property
    def run_id(self) -> str:
        return self._run_id

    def finished_records(self) -> list[ModelCallRecord]:
        """已完成的调用记录（顺序即发生顺序）。"""
        return list(self._records)

    def begin(
        self,
        *,
        purpose: str,
        estimated_prompt_tokens: int,
        tools_schema_fingerprint: str | None,
        retry_of: str | None,
        messages: Sequence[Any] | None = None,
    ) -> ModelCallHandle:
        self._counter += 1
        system_fp, history_fp, dynamic_fp = request_fingerprints(messages)
        # 动态段优先用**运行上下文快照**的指纹（T35）：它才是实际注入的动态内容，比 State
        # 字段指纹更贴切，两处口径不会漂移。没有快照时才回退到 State 字段指纹。
        context_fingerprint = self._snapshot_fingerprint or self._state_context_fingerprint
        if context_fingerprint is not None:
            dynamic_fp = fingerprint_text(json.dumps(
                [context_fingerprint, dynamic_fp], separators=(",", ":")
            ))
        return ModelCallHandle(
            model_call_id=f"{self._run_id or 'run'}:model:{self._counter}",
            purpose=purpose,
            started_at=datetime.now(timezone.utc),
            started_monotonic=time.monotonic(),
            estimated_prompt_tokens=max(0, estimated_prompt_tokens),
            context_window_tokens=self.context_window_tokens,
            tools_schema_fingerprint=tools_schema_fingerprint,
            retry_of=retry_of,
            system_prompt_fingerprint=system_fp,
            history_prefix_fingerprint=history_fp,
            dynamic_context_fingerprint=dynamic_fp,
        )

    def finish(self, handle: ModelCallHandle, *, status: ModelCallStatus) -> ModelCallRecord:
        """收尾一次调用并落成记录（只在**真正结束时**调用一次）。"""
        finished_at = datetime.now(timezone.utc)
        elapsed_ms = max(0, round((time.monotonic() - handle.started_monotonic) * 1000))
        ttft_ms: int | None = None
        if handle.first_token_monotonic is not None:
            ttft_ms = max(0, round((handle.first_token_monotonic - handle.started_monotonic) * 1000))
        usage = handle.usage
        record = ModelCallRecord(
            model_call_id=handle.model_call_id,
            run_id=self._run_id,
            purpose=handle.purpose,
            connection_id=self.connection_id,
            config_version=self.config_version,
            provider=self.provider,
            model_id=self.model_id,
            endpoint_fingerprint=self.endpoint_fingerprint,
            retry_of=handle.retry_of,
            started_at=handle.started_at,
            first_token_at=handle.first_token_at,
            finished_at=finished_at,
            status=status,
            estimated_prompt_tokens=handle.estimated_prompt_tokens,
            actual_uncached_input_tokens=usage.uncached_input_tokens if usage else None,
            actual_cache_read_tokens=usage.cache_read_tokens if usage else None,
            actual_cache_write_tokens=usage.cache_write_tokens if usage else None,
            actual_output_tokens=usage.output_tokens if usage else None,
            actual_reasoning_tokens=usage.reasoning_tokens if usage else None,
            cache_metrics_reported=usage.cache_metrics_reported if usage else False,
            model_elapsed_ms=elapsed_ms,
            time_to_first_token_ms=ttft_ms,
            context_used_tokens=handle.estimated_prompt_tokens,
            context_window_tokens=self.context_window_tokens,
            tools_schema_fingerprint=handle.tools_schema_fingerprint,
            system_prompt_fingerprint=handle.system_prompt_fingerprint,
            history_prefix_fingerprint=handle.history_prefix_fingerprint,
            dynamic_context_fingerprint=handle.dynamic_context_fingerprint,
        )
        self._records.append(record)
        return record


# ---- contextvar：录音机与用途 ------------------------------------------------

_recorder: ContextVar[ModelCallRecorder | None] = ContextVar("model_call_recorder", default=None)
_purpose: ContextVar[str | None] = ContextVar("model_call_purpose", default=None)


@contextmanager
def use_model_recorder(recorder: ModelCallRecorder | None) -> Iterator[None]:
    """在作用域内启用录音机（节点装饰器设置；无录音机时所有记录点退化为空操作）。"""
    token = _recorder.set(recorder)
    try:
        yield
    finally:
        _recorder.reset(token)


@contextmanager
def use_model_purpose(purpose: str) -> Iterator[None]:
    """在作用域内覆盖调用用途（同一节点内有多种调用时用）。"""
    token = _purpose.set(purpose)
    try:
        yield
    finally:
        _purpose.reset(token)


def current_recorder() -> ModelCallRecorder | None:
    return _recorder.get()


def current_purpose() -> str:
    return _purpose.get() or DEFAULT_PURPOSE


# ---- 调用点使用的最小 API ----------------------------------------------------


def begin_model_call(
    *,
    purpose: str | None = None,
    estimated_prompt_tokens: int = 0,
    tools_schema_fingerprint: str | None = None,
    retry_of: str | None = None,
    messages: Sequence[Any] | None = None,
) -> ModelCallHandle | None:
    """开始记录一次调用；**没有录音机时返回 ``None``**（调用点据此跳过记录）。"""
    recorder = _recorder.get()
    if recorder is None:
        return None
    return recorder.begin(
        purpose=purpose or current_purpose(),
        estimated_prompt_tokens=estimated_prompt_tokens,
        tools_schema_fingerprint=tools_schema_fingerprint,
        retry_of=retry_of,
        messages=messages,
    )


def finish_model_call(handle: ModelCallHandle | None, *, status: ModelCallStatus = "ok") -> None:
    """收尾一次调用。``handle`` 为 ``None``（未启用录音）时什么都不做。"""
    if handle is None:
        return
    recorder = _recorder.get()
    if recorder is None:
        return
    recorder.finish(handle, status=status)


def note_first_token(handle: ModelCallHandle | None) -> None:
    """记下首个**正文**块的到达时刻（只在第一次生效）。"""
    if handle is None or handle.first_token_monotonic is not None:
        return
    handle.first_token_monotonic = time.monotonic()
    handle.first_token_at = datetime.now(timezone.utc)


def record_model_call(
    call: Callable[[], T],
    *,
    purpose: str | None = None,
    estimated_prompt_tokens: int = 0,
    tools_schema_fingerprint: str | None = None,
    retry_of: str | None = None,
    messages: Sequence[Any] | None = None,
) -> T:
    """执行一次**真实**模型调用并记录：成功提取用量，异常记为 ``error`` 后原样抛出。

    未启用录音机时等价于直接调用 ``call()``。
    """
    handle = begin_model_call(
        purpose=purpose,
        estimated_prompt_tokens=estimated_prompt_tokens,
        tools_schema_fingerprint=tools_schema_fingerprint,
        retry_of=retry_of,
        messages=messages,
    )
    try:
        result = call()
    except BaseException:
        finish_model_call(handle, status="error")
        raise
    if handle is not None:
        handle.usage_from(result)
    finish_model_call(handle, status="ok")
    return result


# ---- 指纹 --------------------------------------------------------------------


def fingerprint_text(text: str) -> str:
    """不可逆短指纹（16 hex）：用于端点、工具 Schema 这类"能比对但不能回显"的值。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def request_fingerprints(messages: Sequence[Any] | None) -> tuple[str | None, str | None, str | None]:
    """只保存请求各段的哈希；动态段在 T34 指本次用户输入及其后续请求消息。"""
    if not messages:
        return None, None, None
    system = [str(message.content) for message in messages if getattr(message, "type", None) == "system"]
    conversation = [message for message in messages if getattr(message, "type", None) != "system"]
    latest_user = max(
        (index for index, message in enumerate(conversation) if getattr(message, "type", None) == "human"),
        default=len(conversation),
    )

    def digest(items: Sequence[Any]) -> str | None:
        if not items:
            return None
        payload = [
            {
                "role": getattr(message, "type", type(message).__name__),
                "content": getattr(message, "content", None),
                "tool_calls": getattr(message, "tool_calls", None),
                "tool_call_id": getattr(message, "tool_call_id", None),
            }
            for message in items
        ]
        return fingerprint_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))

    return (
        fingerprint_text(json.dumps(system, ensure_ascii=False)) if system else None,
        digest(conversation[:latest_user]),
        digest(conversation[latest_user:]),
    )


def endpoint_fingerprint(base_url: str, model_id: str) -> str:
    """端点安全指纹：脱敏 URL 与模型名的确定性指纹（不含密钥、不含查询参数本身）。"""
    return fingerprint_text(f"{base_url.strip()}\n{model_id.strip()}")


def tools_fingerprint(tools: Sequence[object] | None) -> str | None:
    """工具 Schema 的确定性指纹；没有工具时返回 ``None``。"""
    if not tools:
        return None
    identities = [_tool_identity(tool) for tool in tools]
    payload = json.dumps(identities, ensure_ascii=False, sort_keys=True, default=str)
    return fingerprint_text(payload)


def _tool_identity(tool: object) -> object:
    """使用绑定时的完整工具 Schema；字典原样保留，兼容对象取名字与说明。"""
    if isinstance(tool, BaseTool):
        return convert_to_openai_tool(tool)
    if isinstance(tool, Mapping):
        return dict(tool)
    return {
        "name": getattr(tool, "name", None),
        "description": getattr(tool, "description", None),
    }

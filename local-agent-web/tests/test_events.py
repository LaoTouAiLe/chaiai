"""T27：应用层 DTO、事件、错误与实时序列模型的契约测试。

这些测试只依赖 `local_agent.application.*`：纯数据、无 I/O、无模型调用、无工具执行。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from local_agent.application import errors as app_errors
from local_agent.application import events as app_events
from local_agent.application import models as app_models
from local_agent.application.errors import ErrorView
from local_agent.application.events import (
    COMMON_EVENT_FIELDS,
    EVENT_TYPES,
    RESERVED_FOR_V06,
    EventEnvelope,
    MemoryWrittenEvent,
    ModelUsageReportedEvent,
    SessionSnapshotEvent,
    StreamSequencer,
    ToolStartedEvent,
    event_id_for,
    parse_event,
    stream_delta_identity,
)
from local_agent.application.models import (
    SCHEMA_VERSION,
    ApprovalView,
    CapabilityCheckView,
    ContextMeterView,
    FinalResultView,
    MemoryUseView,
    MessageView,
    ModelCallRecordView,
    ModelConnectionView,
    PlanStepView,
    PlanView,
    RetrievalSourceView,
    RuntimeContextSnapshotView,
    RuntimeSourceVersionView,
    SessionSnapshot,
    TaskRef,
    ToolUseView,
    TurnRecordView,
    WorkspaceRef,
    cache_hit_ratio,
    canonical_json,
    context_usage_ratio,
    redact_values,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_DIR = PROJECT_ROOT / "src" / "local_agent" / "application"

AT = datetime(2026, 9, 23, 9, 20, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 23, 9, 21, tzinfo=timezone.utc)

#: 所有面向 UI/API 的公开模型：JSON Schema 必须能生成，且不含内部类型。
PUBLIC_MODELS: tuple[Any, ...] = (
    WorkspaceRef,
    TaskRef,
    CapabilityCheckView,
    ModelConnectionView,
    PlanStepView,
    PlanView,
    ToolUseView,
    ApprovalView,
    RetrievalSourceView,
    MemoryUseView,
    app_models.UserMessageView,
    app_models.AssistantMessageView,
    app_models.ToolMessageView,
    app_models.MemoryMessageView,
    app_models.ErrorMessageView,
    MessageView,
    ModelCallRecordView,
    TurnRecordView,
    RuntimeSourceVersionView,
    RuntimeContextSnapshotView,
    ContextMeterView,
    FinalResultView,
    SessionSnapshot,
    ErrorView,
    EventEnvelope,
)

FORBIDDEN_IN_SCHEMA = (
    "AgentState",
    "AnyMessage",
    "BaseMessage",
    "ToolCall",
    "Command",
    "ChatOpenAI",
    "langchain",
    "langgraph",
    "checkpoint",
    "streamlit",
)

def _imports_of(path: Path) -> list[str]:
    """该文件里所有 import 行（已去缩进），用于依赖方向的静态检查。"""
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


FORBIDDEN_IMPORTS = (
    "langchain",
    "langgraph",
    "streamlit",
    "fastapi",
    "local_agent.graph",
    "local_agent.ui",
    "local_agent.api",
)


def _snapshot(**overrides: Any) -> SessionSnapshot:
    payload: dict[str, Any] = {
        "snapshot_id": "snap-1",
        "session_id": "s-1",
        "workspace": WorkspaceRef(workspace_id="ws-1", display_name="local-agent-web"),
        "title": "给 README 补一段快速开始",
        "session_state": "completed",
        "current_task": TaskRef(task_id="run-1", run_id="run-1"),
        "messages": [
            app_models.UserMessageView(message_id="m1", text="在 README 里补一段最小启动步骤。", created_at=AT),
            app_models.AssistantMessageView(message_id="m2", text="已补上。", created_at=LATER),
            app_models.ToolMessageView(
                message_id="m3",
                tool=ToolUseView(
                    tool_call_id="t1",
                    tool_name="edit_file",
                    status="ok",
                    redacted_arguments={"path": "README.md"},
                    output_digest="sha256:abc",
                ),
            ),
        ],
        "plan": PlanView(goal="补一段快速开始", steps=[PlanStepView(step_id="p1", title="读取", status="completed")]),
        "memory": MemoryUseView(
            retrieval_outcome="hit",
            sources=[
                RetrievalSourceView(
                    kind="workspace_file",
                    reference="local-agent-web/README.md:1-20",
                    usage="沿用既有步骤顺序",
                )
            ],
            retrieval_note="只显示来源定位与用途。",
            write_outcome="written",
            write_detail="1 条",
        ),
        "usage": TurnRecordView(run_id="run-1", submitted_at=AT, final_at=LATER, final_state="completed"),
        "context": ContextMeterView(used_tokens=84_000, window_tokens=128_000, scope="current"),
        "created_at": LATER,
    }
    payload.update(overrides)
    return SessionSnapshot(**payload)


# ---- 事件集合与信封 --------------------------------------------------------


def test_event_type_set_matches_contract() -> None:
    # 契约里的 19 种事件，一个不多一个不少。
    assert EVENT_TYPES == (
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
    # 声明的类型必须都有对应的具体模型（否则解析永远失败）。
    schema = TypeAdapter(EventEnvelope).json_schema()
    assert set(schema["discriminator"]["mapping"]) == set(EVENT_TYPES)

    # v0.6 的类型不得提前预留（§15.4）。
    for reserved in RESERVED_FOR_V06:
        assert reserved not in EVENT_TYPES


def test_every_event_carries_the_common_fields() -> None:
    schema = TypeAdapter(EventEnvelope).json_schema()
    defs = schema["$defs"]
    checked = 0
    for name, model in defs.items():
        if "event_type" not in model.get("properties", {}):
            continue
        checked += 1
        missing = set(COMMON_EVENT_FIELDS) - set(model["properties"])
        assert not missing, f"{name} 缺少公共字段 {sorted(missing)}"
    assert checked == len(EVENT_TYPES)


def _tool_started_payload() -> app_events.ToolStartedPayload:
    return app_events.ToolStartedPayload(
        tool=ToolUseView(tool_call_id="t1", tool_name="edit_file", status="running")
    )


def test_task_events_require_task_and_run_id() -> None:
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id_for("tool_started", "t1"),
        "session_id": "s-1",
        "occurred_at": AT,
        "stream_sequence": 1,
        "payload": _tool_started_payload(),
    }

    with pytest.raises(ValidationError):
        ToolStartedEvent(**base)
    with pytest.raises(ValidationError):
        ToolStartedEvent(**base, task_id="", run_id="run-1")
    with pytest.raises(ValidationError):
        ToolStartedEvent(**base, task_id="run-1", run_id="")

    event = ToolStartedEvent(**base, task_id="run-1", run_id="run-1")
    assert event.task_id == "run-1"
    assert event.run_id == "run-1"


def test_empty_session_snapshot_has_no_invented_task_ids() -> None:
    snapshot = _snapshot(session_state="idle", current_task=None, messages=[], usage=None)
    event = SessionSnapshotEvent(
        event_id=event_id_for("session_snapshot", snapshot.snapshot_id),
        session_id=snapshot.session_id,
        occurred_at=snapshot.created_at,
        stream_sequence=1,
        payload=snapshot,
    )
    assert event.task_id is None
    assert event.run_id is None
    assert event.payload.current_task is None


# ---- 稳定 id 与序列 --------------------------------------------------------


def test_event_id_is_stable_across_replays() -> None:
    first = event_id_for("tool_completed", "tool-1")
    second = event_id_for("tool_completed", "tool-1")
    assert first == second
    # 不是随机 UUID，而是确定性的 32 位十六进制。
    assert re.fullmatch(r"[0-9a-f]{32}", first)

    # 来源标识或事件类型变化都必须换 id。
    assert event_id_for("tool_completed", "tool-2") != first
    assert event_id_for("tool_started", "tool-1") != first

    with pytest.raises(ValueError):
        event_id_for("tool_completed", "")


def test_stream_delta_ids_dedupe_by_chunk_sequence() -> None:
    identity = stream_delta_identity("call-1", 7)
    assert identity == "call-1#7"
    assert event_id_for("message_delta", identity) == event_id_for("message_delta", stream_delta_identity("call-1", 7))
    assert event_id_for("message_delta", identity) != event_id_for(
        "message_delta", stream_delta_identity("call-1", 8)
    )
    assert event_id_for("message_delta", identity) != event_id_for(
        "message_delta", stream_delta_identity("call-2", 7)
    )


def test_stream_sequence_is_per_session_and_process_local() -> None:
    sequencer = StreamSequencer()
    assert [sequencer.next("s-1"), sequencer.next("s-1"), sequencer.next("s-1")] == [1, 2, 3]
    assert sequencer.next("s-2") == 1
    assert sequencer.peek("s-1") == 3
    assert sequencer.peek("s-unknown") == 0

    sequencer.forget("s-1")
    assert sequencer.peek("s-1") == 0
    assert sequencer.next("s-1") == 1

    # 新进程从 1 重新开始：它不是持久游标，因此不提供 after_cursor 之类的接口。
    assert StreamSequencer().next("s-1") == 1
    assert not hasattr(StreamSequencer, "after_cursor")
    assert not [name for name in dir(app_events) if "cursor" in name.lower()]


def test_same_input_serializes_byte_identical() -> None:
    first = _snapshot()
    second = _snapshot()
    assert canonical_json(first) == canonical_json(second)

    # 事件同样逐字节一致（同一载荷、同一稳定 id）。
    def build() -> SessionSnapshotEvent:
        snapshot = _snapshot()
        return SessionSnapshotEvent(
            event_id=event_id_for("session_snapshot", snapshot.snapshot_id),
            session_id=snapshot.session_id,
            occurred_at=snapshot.created_at,
            stream_sequence=1,
            payload=snapshot,
        )

    assert canonical_json(build()) == canonical_json(build())
    parsed = parse_event(canonical_json(build()))
    assert isinstance(parsed, SessionSnapshotEvent)
    assert canonical_json(parsed) == canonical_json(build())


def test_snapshot_and_sse_exclude_local_diagnostics_but_keep_operation_references() -> None:
    from local_agent.api.sse import format_event

    call = app_models.ModelCallRecordView(
        model_call_id="call-1", run_id="run-1", purpose="tool_decision",
        connection_id="connection-1", config_version="1", provider="openai_compatible",
        model_id="test", started_at=AT, status="ok", endpoint_fingerprint="local-endpoint-hash",
        tools_schema_fingerprint="local-tool-hash", system_prompt_fingerprint="local-system-hash",
        history_prefix_fingerprint="local-history-hash", dynamic_context_fingerprint="local-context-hash",
    )
    snapshot = _snapshot(model_calls=[call])
    event = SessionSnapshotEvent(
        event_id="event-1", session_id=snapshot.session_id, occurred_at=AT,
        stream_sequence=1, payload=snapshot,
    )
    for text in (canonical_json(snapshot), format_event(event)):
        for forbidden in ("output_digest", "endpoint_fingerprint", "tools_schema_fingerprint", "system_prompt_fingerprint",
                          "history_prefix_fingerprint", "dynamic_context_fingerprint", "runtime_context", "local-tool-hash"):
            assert forbidden not in text
        assert '"model_call_id"' in text
        assert '"session_id"' in text
    assert call.tools_schema_fingerprint == "local-tool-hash"
    assert parse_event(canonical_json(event)).event_type == "session_snapshot"

    complete = app_events.ToolCompletedEvent(
        event_id="event-2", session_id="s-1", occurred_at=AT, stream_sequence=2,
        task_id="run-1", run_id="run-1",
        payload=app_events.ToolCompletedPayload(tool_call_id="t1", status="ok", output_digest="local-output-hash"),
    )
    assert "local-output-hash" not in format_event(complete)
    assert "output_digest" not in format_event(complete)


# ---- 前向兼容 --------------------------------------------------------------


def test_unknown_fields_are_ignored_and_not_echoed() -> None:
    event = parse_event(
        {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id_for("memory_written", "run-1"),
            "event_type": "memory_written",
            "session_id": "s-1",
            "task_id": "run-1",
            "run_id": "run-1",
            "occurred_at": AT.isoformat(),
            "stream_sequence": 4,
            "future_envelope_field": "忽略我",
            "payload": {"outcome": "written", "detail": "1 条", "future_payload_field": 42},
        }
    )
    assert isinstance(event, MemoryWrittenEvent)
    dumped = event.model_dump(mode="json")
    assert "future_envelope_field" not in dumped
    assert "future_payload_field" not in dumped["payload"]


def test_unknown_event_type_is_rejected_instead_of_guessed() -> None:
    with pytest.raises(ValidationError):
        parse_event(
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id_for("session_snapshot", "snap-1"),
                "event_type": "job_queued",
                "session_id": "s-1",
                "task_id": None,
                "run_id": None,
                "occurred_at": AT.isoformat(),
                "stream_sequence": 1,
                "payload": {},
            }
        )


def test_usage_event_keeps_unknown_cache_fields_as_null() -> None:
    event = ModelUsageReportedEvent(
        event_id=event_id_for("model_usage_reported", "call-1"),
        session_id="s-1",
        task_id="run-1",
        run_id="run-1",
        occurred_at=AT,
        stream_sequence=2,
        payload=app_events.ModelUsagePayload(model_call_id="call-1", actual_output_tokens=0),
    )
    dumped = event.model_dump(mode="json")
    assert dumped["payload"]["actual_output_tokens"] == 0
    assert dumped["payload"]["actual_cache_read_tokens"] is None
    assert dumped["payload"]["cache_metrics_reported"] is False


# ---- 错误模型 --------------------------------------------------------------


def test_error_categories_and_codes_are_fixed() -> None:
    assert set(app_errors.ERROR_CATEGORIES) == {
        "configuration",
        "model",
        "tool",
        "security",
        "approval",
        "recovery",
        "system",
    }
    for code in app_errors.ERROR_CODES:
        assert app_errors.category_of(code) is not None
        assert app_errors.retry_condition_of(code)


def test_session_busy_is_retryable_but_conflicts_are_not() -> None:
    # §15.5：SESSION_BUSY 可在当前请求结束后重试；另两个不允许原样重试。
    assert app_errors.is_retryable(app_errors.SESSION_BUSY) is True
    assert app_errors.is_retryable(app_errors.IDEMPOTENCY_CONFLICT) is False
    assert app_errors.is_retryable(app_errors.RECOVERY_REQUIRED) is False
    # 未知错误码按不可重试处理——不确定时不承诺可重试。
    assert app_errors.is_retryable("SOMETHING_NEW") is False
    assert app_errors.category_of("SOMETHING_NEW") is None


def test_error_view_is_actionable_and_executable() -> None:
    busy = ErrorView.for_code(
        app_errors.SESSION_BUSY,
        title="该会话已有正在运行的一轮",
        message="本次提交未被接受：同一会话同一时刻只允许一轮运行。",
        hint="等待当前一轮结束后再提交这条内容；v0.4 不公开取消接口。",
        diagnostic_id="err-7b21c4",
        occurred_at=AT,
        source="session",
    )
    assert busy.category == "system"
    assert busy.retryable is True
    assert busy.retry_condition is not None
    assert busy.blocked_reason is None

    conflict = ErrorView.for_code(
        app_errors.IDEMPOTENCY_CONFLICT,
        title="同一请求标识的内容不一致",
        message="这个请求标识此前已用于另一次提交，且内容不同。",
        hint="先确认上一次提交的结果，再把它当作新的请求发送。",
        diagnostic_id="err-c40f19",
        occurred_at=AT,
    )
    assert conflict.retryable is False
    assert conflict.blocked_reason is None  # 未给出原因时保持空，不编一句

    recovery = ErrorView.for_code(
        app_errors.RECOVERY_REQUIRED,
        title="需要先完成上一次运行的恢复",
        message="无法证明它的工具副作用与终态，因此不会自动重提同一输入。",
        hint="人工核对上一次运行的记录后，再把它作为新的请求重新提交。",
        diagnostic_id="err-1a8d63",
        occurred_at=AT,
        blocked_reason="需要人工核对上一次运行的记录；不会自动重提或再次执行工具。",
    )
    assert recovery.retryable is False
    assert recovery.blocked_reason is not None

    # 未知码归入 system 且不可重试。
    unknown = ErrorView.for_code(
        "SOMETHING_NEW",
        title="未知错误",
        message="发生了未分类的错误。",
        hint="查看本地日志中的诊断编号。",
        diagnostic_id="err-0000",
        occurred_at=AT,
    )
    assert unknown.category == "system"
    assert unknown.retryable is False


def test_error_text_rejects_secrets_and_private_paths() -> None:
    """错误展示文本 fail-closed：脱敏是调用方的责任，这里是最后一道门。"""
    base: dict[str, Any] = {
        "code": app_errors.MODEL_AUTH_FAILED,
        "category": "model",
        "title": "模型连接鉴权失败",
        "message": "该连接返回鉴权失败。",
        "hint": "到设置里更新该连接的密钥后重试。",
        "retryable": False,
        "blocked_reason": "需要先更新密钥。",
        "diagnostic_id": "err-4f3a91",
        "occurred_at": AT,
    }
    assert ErrorView(**base).code == app_errors.MODEL_AUTH_FAILED

    for leaked in ("key=sk-abcdefghijklmnop", "路径 C:\\Users\\asus\\secret", "见 /home/asus/.env"):
        with pytest.raises(ValidationError):
            ErrorView(**{**base, "hint": leaked})

    for unsafe_id in ("sk-abcdefghijklmnop", "C:\\Users\\asus\\secret", "err/../../secret"):
        with pytest.raises(ValidationError):
            ErrorView(**{**base, "diagnostic_id": unsafe_id})


def test_error_view_rejects_naive_timestamps() -> None:
    # 裸 datetime 正是这条测试要证明会被拒绝的输入，所以这里刻意不写 tzinfo。
    naive = datetime(2026, 9, 23, 9, 20)  # noqa: DTZ001
    with pytest.raises(ValidationError):
        ErrorView(
            code=app_errors.SESSION_BUSY,
            category="system",
            title="x",
            message="y",
            hint="z",
            retryable=True,
            diagnostic_id="err-1",
            occurred_at=naive,
        )


# ---- 用量与计量器 ----------------------------------------------------------


def test_cache_metrics_stay_unknown_instead_of_zero() -> None:
    # 供应商没上报 → None，不显示百分比。
    assert cache_hit_ratio(None, 100, None) is None
    assert cache_hit_ratio(100, None, None) is None
    # 明确上报零才算 0.0。
    # 2026-09-30：第三项由 None 改为 0.0。**写缓存**缺失不再算"未知"——DeepSeek 这类隐式缓存
    # 供应商根本没有这个桶（缓存写入是服务端自动行为，计费只有命中/未命中），原来要求三项齐全
    # 会让命中率在 DeepSeek 上**恒为未知**，页面一直是"缓存命中 未知"。
    assert cache_hit_ratio(100, 0, None) == 0.0
    assert cache_hit_ratio(100, 0, 0) == 0.0
    # 分母为零 → None。
    assert cache_hit_ratio(0, 0, 0) is None
    # DeepSeek：prompt_tokens 含缓存命中，归一化后按读/写/未缓存三者计算。
    ratio = cache_hit_ratio(700, 200, 100)
    assert ratio is not None
    assert abs(ratio - 0.2) < 1e-9


def test_model_call_record_records_retries_as_new_calls() -> None:
    first = ModelCallRecordView(
        model_call_id="call-1",
        run_id="run-1",
        purpose="final_answer",
        connection_id="conn-1",
        config_version="v1",
        provider="openai_compatible",
        model_id="chat-large",
        endpoint_fingerprint="sha256:zz",
        started_at=AT,
        finished_at=LATER,
        status="error",
        actual_output_tokens=None,
    )
    retry = ModelCallRecordView(
        model_call_id="call-2",
        run_id="run-1",
        purpose="final_answer",
        connection_id="conn-1",
        config_version="v1",
        provider="openai_compatible",
        model_id="chat-large",
        endpoint_fingerprint="sha256:zz",
        retry_of="call-1",
        started_at=LATER,
        status="running",
    )
    assert retry.retry_of == first.model_call_id
    assert retry.model_call_id != first.model_call_id
    dumped = first.model_dump(mode="json")
    assert dumped["actual_cache_read_tokens"] is None
    assert dumped["cache_metrics_reported"] is False


def test_context_meter_unknown_is_not_zero_percent() -> None:
    assert context_usage_ratio(None, 128_000) is None
    assert context_usage_ratio(84_000, None) is None
    assert context_usage_ratio(84_000, 0) is None
    assert context_usage_ratio(0, 128_000) == 0.0
    assert context_usage_ratio(200_000, 128_000) == 1.0


# ---- 安全与可见性 ----------------------------------------------------------


def test_connection_view_ignores_unknown_key_fields_and_rejects_embedded_credentials() -> None:
    fields = set(ModelConnectionView.model_fields)
    # 只有"配置状态"与"不含明文的引用"两处与凭据有关。
    assert {"key_status", "credential_ref"} <= fields
    assert not {name for name in fields if "api_key" in name or name.endswith("_secret")}

    hostile: dict[str, Any] = {
        "connection_id": "conn-1",
        "display_name": "模型 A",
        "base_url": "https://api.example.com/v1",
        "transport": "https",
        "model_id": "chat-large",
        "context_window_tokens": 128_000,
        "max_tokens": 4096,
        "temperature": 0.2,
        "timeout_seconds": 120,
        "retry_count": 2,
        "streaming": True,
        "auth_mode": "api_key",
        "key_status": "configured",
        "credential_ref": "cred-1",
        "status": "verified",
        "config_version": "v1",
        # 上游多传一个密钥值也必须被丢掉。
        "api_key": "sk-abcdefghijklmnop",
    }
    view = ModelConnectionView(**hostile)
    dumped = canonical_json(view)
    assert "sk-" not in dumped
    # 多传的字段被丢掉，而不是被回显：导出的键就是声明的字段集合。
    assert set(json.loads(dumped)) == set(ModelConnectionView.model_fields)

    # Even a declared field must not smuggle credentials into the read DTO.
    for unsafe_url in (
        "https://user:dummysecret@example.com/v1",
        "https://api.example.com/v1?api_key=dummysecret",
        "https://api.example.com/v1#dummysecret",
        "file:///tmp/model",
    ):
        with pytest.raises(ValidationError):
            ModelConnectionView(**{**hostile, "base_url": unsafe_url})

    # The UI's plaintext warning must reflect the actual endpoint scheme/host.
    with pytest.raises(ValidationError):
        ModelConnectionView(**{**hostile, "base_url": "http://192.168.1.20/v1"})
    loopback = ModelConnectionView(
        **{**hostile, "base_url": "http://127.0.0.1:8000/v1", "transport": "loopback_http"}
    )
    assert loopback.transport == "loopback_http"


def test_snapshot_never_carries_secrets_or_private_paths() -> None:
    leaked = _snapshot(api_key="sk-abcdefghijklmnop")
    dumped = canonical_json(leaked)
    assert "sk-" not in dumped
    assert not re.search(r"[A-Za-z]:\\", dumped)
    assert "/home/" not in dumped
    assert "/home/" not in dumped
    assert "/Users/" not in dumped

    # 展示文本走既有脱敏入口。
    assert redact_values({"preview": "key=sk-abcdefghijklmnop"}, ["sk-abcdefghijklmnop"]) == {
        "preview": "key=***"
    }


def test_runtime_context_snapshot_view_hides_the_prompt_text() -> None:
    fields = set(RuntimeContextSnapshotView.model_fields)
    assert "canonical_text" not in fields
    assert {"snapshot_id", "content_hash", "text_length", "source_versions"} <= fields

    view = RuntimeContextSnapshotView(

        snapshot_id="ctx-1",
        run_id="run-1",
        context_version=3,
        created_at=AT,
        content_hash="sha256:abc",
        text_length=1234,
        source_versions=[RuntimeSourceVersionView(source="memory", version="v2")],
    )
    dumped = canonical_json(view)
    assert "canonical_text" not in dumped
    assert len(dumped) < 500


# ---- JSON Schema 与依赖方向 ------------------------------------------------


def test_json_schema_is_generatable_and_free_of_internal_types() -> None:
    for model in PUBLIC_MODELS:
        schema = TypeAdapter(model).json_schema()
        text = json.dumps(schema, ensure_ascii=False)
        for forbidden in FORBIDDEN_IN_SCHEMA:
            assert forbidden not in text, f"{model} 的 Schema 里出现了内部类型 {forbidden}"
        for name in schema.get("$defs", {}):
            assert not name.startswith("_"), f"私有基类 {name} 泄露进了公开 Schema"

    # 判别联合可用于生成前端类型：消息与事件都有 discriminator。
    assert "discriminator" in TypeAdapter(MessageView).json_schema()
    assert "discriminator" in TypeAdapter(EventEnvelope).json_schema()


def test_pure_contract_modules_do_not_import_runtime_layers() -> None:
    # 契约层必须保持纯净：T28 的 projection/service 需要调用 Graph，但公开契约模块不需要，
    # 也不能被它们污染。
    for name in ("__init__.py", "errors.py", "events.py", "models.py"):
        path = APPLICATION_DIR / name
        assert path.is_file()
        for line in _imports_of(path):
            for forbidden in (*FORBIDDEN_IMPORTS, "local_agent.persistence", "local_agent.security"):
                assert forbidden not in line, f"{path.name} 依赖了禁用模块：{line}"

    # 反向也要成立：LangGraph 侧不得导入应用层（§15.1）。
    graph_dir = PROJECT_ROOT / "src" / "local_agent" / "graph"
    for path in graph_dir.glob("*.py"):
        assert "local_agent.application" not in path.read_text(encoding="utf-8")


def test_boundary_modules_may_call_the_graph_but_not_the_page_hosts() -> None:
    """边界层（`projection`/`service`）可以向下依赖 Graph 与持久化，但：

    - 不得导入 Streamlit / FastAPI / 页面宿主（应用层不依赖任何 UI 框架）；
    - 不得把 Graph **节点**模块拖进来——投影只认 State 数据，不该知道节点实现；
    - Graph 依赖必须是**白名单**：只允许纯函数模块（消息投影、轮次作用域键）与服务层的图装配。
      这样新增一条 `from local_agent.graph.state import ...` 会立刻失败，而不是悄悄通过。
    """
    allowed_graph_imports = (
        "local_agent.graph import builder",
        "local_agent.graph.projection",
        "local_agent.graph.scope",
    )
    boundary = ("projection.py", "service.py")
    for name in boundary:
        path = APPLICATION_DIR / name
        if not path.is_file():
            continue  # service.py 属 T28 后续批次
        for line in _imports_of(path):
            for forbidden in ("streamlit", "fastapi", "local_agent.api", "local_agent.web", "local_agent.cli"):
                assert forbidden not in line, f"{name} 依赖了禁用模块：{line}"
            if "local_agent.graph" not in line:
                continue
            assert any(ok in line for ok in allowed_graph_imports), (
                f"{name} 只允许依赖 Graph 的纯函数模块：{line}"
            )

    # 白名单里的模块必须真的是纯函数模块：把 langgraph/langchain 拖进来就等于绕过了上面这条。
    scope_path = PROJECT_ROOT / "src" / "local_agent" / "graph" / "scope.py"
    for line in _imports_of(scope_path):
        assert "langgraph" not in line and "langchain" not in line, line

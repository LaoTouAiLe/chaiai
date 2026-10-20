"""应用服务：会话用例、每会话单飞、幂等台账与事件订阅（ARCHITECTURE §15.2、计划 §6.1）。

职责边界：

- **Graph 调用与 `Command` 封装在这里**：`Streamlit` 与 HTTP 层只调用本服务，不自己拼 State、
  不直接 `invoke`、不自己造 `Command`。
- **每个会话同一时刻只允许一次执行状态修改**：`submit_message` 与 `resolve_approval` 共用同一互斥边界；
  已有执行在跑时，第二个请求返回 `SESSION_BUSY`，**不排队**。
- **幂等靠落库台账**（`persistence/request_ledger.py`）：同 id 同参数返回**原请求关联的**结果（按台账
  的 `result_ref` 读当时那份 Checkpoint，不拿当前快照冒充）；同 id 不同参数 `IDEMPOTENCY_CONFLICT`；
  台账仍为"执行中"但锁没被占用（等于上一次进程留下的）时，先**只读**核对 Checkpoint——只有"台账
  绑定的 run == 当前 run 且已有终态"才算证明得了；能证明就补齐结果引用，证明不了（**包括 run 没绑定**）
  就返回 `RECOVERY_REQUIRED`，**绝不自动重提**。
- **不确定异常不自动写终态**：工具或持久化异常可能发生在副作用之后，台账保留待核对。
  不执行工具的模型节点在图内明确失败；不确定请求由用户核对后显式结束，绝不重放工具。
- **读取与订阅都不得改业务状态**：`get_session_snapshot` 只读 Checkpoint 与进程内脱敏展示副本，不推进图；完整业务事件仍是 State 的单向投影。
- **模型连接在提交边界解析并在这一轮固定**（§6.8）：新提交按"会话选择 → 默认连接"构造客户端，并把
  `model_binding`（连接 id + 版本 + 不含明文的凭据引用）写进 State；审批恢复按**那一轮固定的**版本
  重建客户端。所以换模型/改参数只影响下一轮，等待审批的运行不会被中途换掉。没有可用设置库时退回
  `app_config.llm` + 注入的适配器（降级路径），一个连接都没有时提交得到 `MODEL_CONNECTION_REQUIRED`。

本模块不做视觉、不做鉴权、不碰 Streamlit/FastAPI（依赖方向见 `tests/test_events.py`）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import sqlite3
import threading
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, TypeVar, cast
from urllib.parse import urlsplit

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from pydantic import TypeAdapter

from local_agent import config as config_module
from local_agent.application import conversation, data_center
from local_agent.application import errors as app_errors
from local_agent.application import events as app_events
from local_agent.application import projection as proj
from local_agent.application.data_center_models import MemoryPageView, UsagePageView
from local_agent.application.events import (
    EventEnvelope,
    SessionSnapshotEvent,
    StreamSequencer,
    event_id_for,
)
from local_agent.application.jobs import JOB_EVENT_FOR_STATE, Job, JobRuntime
from local_agent.application.models import (
    AssistantMessageView,
    CapabilitiesView,
    CapabilityCheckView,
    CapabilityStatusView,
    CommandResultView,
    CompressionView,
    ConversationMapView,
    CredentialStoreState,
    ExternalPathAccess,
    ExternalPathGrantView,
    ExternalPathsMode,
    KeyStatus,
    ModelConnectionView,
    ModelSelectionView,
    PermissionsView,
    SessionSnapshot,
    SessionSummaryView,
    SkillEntryView,
    SkillRootView,
    SkillsView,
    TaskCenterView,
    TaskView,
    WebSearchKeyView,
    WorkspaceRef,
)
from local_agent.application.recovery import (
    TASK_TERMINAL_STATUSES,
    JobVerdict,
    TurnEvidence,
    child_completion_proven,
    classify_interrupted_job,
)
from local_agent.application.tool_stream import ToolStream
from local_agent.attachments import AttachmentStore, LoadedAttachment
from local_agent.model_settings.attachments_oss import OssKeyStore
from local_agent.oss_storage import OssStorageError
from local_agent.cancellation import DEFAULT_CANCEL_REASON, CancellationToken
from local_agent.capabilities import (
    capability_status,
    resolve_switches,
    switchable_capabilities,
    unknown_switch_names,
)
from local_agent.config import LlmConfig, RunConfig, WorkflowsConfig, apply_capability_gates
from local_agent.context.compression_progress import CompressionProgress, use_compression_listener
from local_agent.context.skills_loader import SkillCatalog, discover_skills
from local_agent.exceptions import (
    AttachmentError,
    ConnectionValidationError,
    ConnectionVersionConflict,
    CredentialStoreError,
    JobStoreError,
    ModelSettingsError,
    SelectionConflict,
)
from local_agent.graph import builder
from local_agent.graph.scope import tool_scope_key
from local_agent.llm import factory as llm_factory
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.stream_context import use_sink
from local_agent.llm.usage_recording import ModelCallRecorder, endpoint_fingerprint
from local_agent.message_flags import DEFAULT_IMAGE_CONTEXT_TURNS
from local_agent.model_settings import (
    PLACEHOLDER_API_KEY,
    CapabilityReport,
    CapabilityStore,
    ConnectionDraft,
    ConnectionStore,
    ModelBinding,
    ModelConnection,
    PermissionStore,
    Selection,
    SelectionRef,
    SkillStore,
    memory_settings,
)
from local_agent.model_settings import probe as connection_probe
from local_agent.model_settings.preferences import (
    MemoryProbeRequest,
    MemoryProbeResult,
    PreferenceConflict,
    PreferencesRequest,
    PreferenceStore,
    PreferencesView,
    RuntimePreferenceBinding,
    RuntimePreferences,
    advanced_preferences,
    apply_runtime_preferences,
    complete_preferences,
    initial_preferences,
)
from local_agent.model_settings.web_search import WebSearchKeyStore
from local_agent.persistence.conversation_store import ConversationTurn
from local_agent.persistence.job_store import (
    JOB_STATE_CANCELLED,
    JOB_STATE_NEEDS_REVIEW,
    JOB_STATE_QUEUED,
    JOB_STATE_WAITING_USER,
    JOB_STATES,
    TERMINAL_JOB_STATES,
)
from local_agent.persistence.request_ledger import (
    OPERATION_RESOLVE_APPROVAL,
    OPERATION_SUBMIT,
    STATUS_COMPLETED,
    STATUS_EXECUTING,
    STATUS_FAILED,
    TERMINAL_STATUSES,
    LedgerEntry,
    RequestLedger,
)
from local_agent.persistence.session_store import DEFAULT_TITLE, Session, SessionStore
from local_agent.scope import SCOPE_WORKSPACE, scope_key_for
from local_agent.security.path_guard import ExternalPathPolicy, protected_target_reason
from local_agent.tools.git_tools import git_availability
from local_agent.tools.progress import use_tool_listener
from local_agent.workflow_contracts import WorkflowRequest

#: 任务的终态（`TASK_TERMINAL_STATUSES`）、重启后的任务判定与它的纯函数都在
#: :mod:`local_agent.application.recovery`（§17.5）——这里只做 I/O 与写索引，不重复判定逻辑。

#: 一次命令的结局（与 `CommandResultView.status` 同值）。
#: ``running`` 只在 ``jobs.background=true`` 时出现（§17.2）：命令已被接受、任务已进入运行。
#: ``cancelled`` 只在取消命令上出现（§17.4）：任务已被中止。
CommandStatus = Literal["completed", "awaiting_approval", "failed", "running", "cancelled"]

#: 取消命令的操作名（§15.2）。它**不进幂等台账**：取消天然幂等，重复调用是空操作。
OPERATION_CANCEL = "cancel_task"

#: 超时看守的检查间隔（秒）。只影响"发现超时"的延迟，不影响判定口径。
_WATCHDOG_INTERVAL_SECONDS = 1.0

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

#: 允许的审批决定（与 `ui/approval_ui.py` 的载荷一致；这里不导入那个模块——它依赖 Streamlit）。
APPROVAL_DECISIONS: Final[frozenset[str]] = frozenset({"approve", "edit", "reject"})

#: 旧配置模式（没有设置库）**没有"连接"这个实体**：用量记录里如实标注来源，而不是编一个
#: 看起来像连接 id 的随机串（§15.6 的"不编造数据"）。公开视图要求这两个字段非空，所以它们
#: 是一个明确的标签，而不是占位符。
LEGACY_CONNECTION_ID: Final[str] = "legacy-config"
LEGACY_CONFIG_VERSION: Final[str] = "unversioned"


@dataclass(frozen=True)
class _ThreadView:
    """一次只读的 Checkpoint 视图。"""

    values: Mapping[str, Any]
    has_live_interrupt: bool
    checkpoint_id: str | None
    confirmation_stage: int = 1
    next_nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RunEnv:
    """一次运行的模型环境：用哪份配置、哪个客户端、报什么模型名、带什么能力快照。"""

    app_config: RunConfig
    model: Any
    provider_name: str
    capabilities: ModelCapabilities | None
    binding: ModelBinding | None
    preference_binding: RuntimePreferenceBinding
    #: 本轮用量录音机（§15.6）。与 `app_config` 同时定好：录音机里的连接/窗口信息必须来自
    #: **这次真正在用的**那份配置，不能读"当前设置"——等待审批的运行不该被中途换掉的设置影响。
    usage_recorder: ModelCallRecorder | None = None
    #: 这次运行要交给模型的图片（2026-10-16）：提交时按引用从 `data/attachments/` 读成字节，
    #: 挂在环境上而不是塞进 payload——payload 会成为初始 State 并写进 Checkpoint，
    #: 图像字节没有理由在里面存第二份（第一份已经在用户消息的内容块里）。
    attachments: tuple[LoadedAttachment, ...] = ()


class EventSubscription:
    """订阅者：先拿边界快照，再按顺序收实时事件。

    **队列在入队时就封顶**（`max_pending`）：客户端读流停滞时，生产者仍在往里推事件；
    只靠"泵取出整批后再判断长度"并不能限制内存——那之间的积压是无限的（复审指出）。
    超限只记一个标记，不再入队；泵看到标记就发 `stream_overrun` 并关闭这条连接。
    """

    def __init__(self, session_id: str, *, max_pending: int) -> None:
        self.session_id = session_id
        self.max_pending = max(1, max_pending)
        self._queue: deque[EventEnvelope] = deque()
        #: 建立边界期间到达的实时事件：先缓冲，快照投递后再按顺序放行。
        self._held: deque[EventEnvelope] = deque()
        self._delivering = False
        self._closed = False
        self._overflowed = False
        self._lock = threading.Lock()

    @property
    def overflowed(self) -> bool:
        """待发事件是否已经超过上限（页面该重连，而不是继续等增量）。"""
        with self._lock:
            return self._overflowed

    @property
    def pending_count(self) -> int:
        """当前待发事件条数（诊断用；封顶后不会再增长）。"""
        with self._lock:
            return len(self._queue) + len(self._held)

    def _push(self, event: EventEnvelope) -> None:
        with self._lock:
            if self._closed:
                return
            if len(self._queue) + len(self._held) >= self.max_pending:
                # 只标一次，并且**不再入队**：内存有界比"尽量多留几条"重要。
                self._overflowed = True
                return
            if self._delivering:
                self._queue.append(event)
            else:
                self._held.append(event)

    def _start_delivery(self, boundary: EventEnvelope | None) -> None:
        """把边界快照放到**队首**，再放行建立边界期间缓冲的实时事件。"""
        with self._lock:
            if self._closed:
                return
            self._queue.extend(self._held)
            self._held.clear()
            if boundary is not None:
                self._queue.appendleft(boundary)
            self._delivering = True

    def poll(self) -> list[EventEnvelope]:
        """取出当前已排队的事件（不阻塞）。"""
        with self._lock:
            drained = list(self._queue)
            self._queue.clear()
            return drained

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._queue.clear()

    @property
    def closed(self) -> bool:
        return self._closed


class EventBus:
    """进程内广播。

    v0.4 的部署拓扑固定为**单进程、单 worker**（§15.1），所以广播可以是进程内的；
    它只投影进度，不是任务真源，也不做持久游标。
    """

    def __init__(
        self,
        *,
        sequencer: StreamSequencer | None = None,
        max_pending: int = 200,
    ) -> None:
        #: 单个订阅者的待发上限（来自 `web.sse_max_pending_events`）：入队即封顶，内存有界。
        self._max_pending = max(1, max_pending)
        self._subscribers: dict[str, list[EventSubscription]] = {}
        self._lock = threading.Lock()
        self.sequencer = sequencer or StreamSequencer()

    def subscribe(
        self, session_id: str, *, boundary: Callable[[], EventEnvelope | None]
    ) -> EventSubscription:
        """登记订阅者，**然后**取边界快照并投递它（§6.3 的顺序不能反）。

        登记与"开始放行"之间到达的实时事件先**缓冲**：直接入队会让并发提交的事件排在边界
        快照前面（实测得到 `['live', 'snapshot']`），而客户端的契约是"先快照、再增量"。
        缓冲期内到达的事件在快照之后原样放行——它们描述的事实已经包含在快照里，重复
        呈现由客户端按事件 id 处理，但**顺序**不能乱。
        """
        subscription = EventSubscription(session_id, max_pending=self._max_pending)
        with self._lock:
            # 与 publish 共用这把锁：快照预留的序号一定早于登记后的实时事件。
            boundary_sequence = self.sequencer.next(session_id)
            self._subscribers.setdefault(session_id, []).append(subscription)
        try:
            event = boundary()
        except BaseException:
            # 取快照失败就不留半个订阅者：否则它会一直收事件却没人读。
            self.unsubscribe(subscription)
            raise
        if event is not None:
            event = event.model_copy(update={"stream_sequence": boundary_sequence})
        subscription._start_delivery(event)
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        with self._lock:
            listeners = self._subscribers.get(subscription.session_id, [])
            if subscription in listeners:
                listeners.remove(subscription)
            if not listeners:
                self._subscribers.pop(subscription.session_id, None)
        subscription.close()

    def publish(self, session_id: str, events: Iterable[EventEnvelope]) -> None:
        with self._lock:
            listeners = list(self._subscribers.get(session_id, []))
            # 序号在真正广播时分配，不沿用事件构造时的序号：构造和发布之间可能插入订阅边界。
            for event in events:
                sequenced = event.model_copy(
                    update={"stream_sequence": self.sequencer.next(session_id)}
                )
                for listener in listeners:
                    listener._push(sequenced)

    def subscriber_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(session_id, []))


class _StreamingAnswerSink:
    """模型消息的临时增量；完整消息仍由节点提交到 Checkpoint。

    增量按安全分隔符成段推出，并保留最长已知密钥的重叠后缀：密钥与 `sk-` 样式即使跨模型
    chunk 也不会从 SSE 原样漏出；路径原样展示。无分隔符的长串延至结束才显示。
    """

    _BREAKS = frozenset(" \t\r\n，。；：、（）()[]")

    def __init__(self, *, session_id: str, bus: EventBus, secrets: Iterable[str]) -> None:
        self._session_id = session_id
        self._bus = bus
        self._secrets = tuple(secrets)
        self._holdback = max((len(secret) - 1 for secret in self._secrets), default=0)
        self._run_id: str | None = None
        self._call_index = 0
        self._message_id: str | None = None
        self._model_call_id: str | None = None
        self._sequence = 0
        self._pending = ""
        self._pending_channel: Literal["text", "reasoning"] = "text"
        self._display_lock = threading.Lock()
        self._text = ""
        self._reasoning = ""
        self._reasoning_truncated = False
        self._last_channel: Literal["text", "reasoning"] = "text"

    def bind_run(self, run_id: str, *, call_index: int = 0) -> None:
        self._run_id = run_id
        self._call_index = call_index

    def _envelope(self, event_type: str, identity: str, payload: Any, model: type) -> Any:
        if self._run_id is None:
            raise RuntimeError("最终回答增量早于本轮 run 绑定")
        return model(
            event_id=event_id_for(event_type, identity),  # type: ignore[arg-type]
            session_id=self._session_id,
            task_id=self._run_id,
            run_id=self._run_id,
            occurred_at=_now(),
            stream_sequence=1,  # 构造时合法占位；EventBus.publish 在广播时分配真正序号。
            payload=payload,
        )

    def begin(self) -> str:
        if self._run_id is None:
            raise RuntimeError("最终回答增量早于本轮 run 绑定")
        with self._display_lock:
            self._call_index += 1
            self._model_call_id = f"{self._run_id}:final-call:{self._call_index}"
            self._message_id = f"{self._run_id}:final-message:{self._call_index}"
            self._sequence = 0
            self._text = ""
            self._reasoning = ""
            self._reasoning_truncated = False
            self._last_channel = "text"
        self._pending = ""
        self._started()
        return self._message_id

    def _started(self, *, reset: bool = False) -> None:
        if self._message_id is None:
            return
        event = self._envelope(
            "message_started",
            f"{self._message_id}:discard" if reset else self._message_id,
            app_events.MessageStartedPayload(
                message_id=self._message_id,
                role="assistant",
                model_call_id=self._model_call_id,
                reset=reset,
                chunk_sequence=self._sequence,
            ),
            app_events.MessageStartedEvent,
        )
        self._bus.publish(self._session_id, [event])

    def push(self, text: str) -> None:
        self._push(text, "text")

    def push_reasoning(self, text: str) -> None:
        self._push(text, "reasoning")

    def _push(self, text: str, channel: Literal["text", "reasoning"]) -> None:
        if self._message_id is None:
            return
        if channel != self._pending_channel:
            self.flush()
        self._pending_channel = channel
        self._pending += text
        limit = max(0, len(self._pending) - self._holdback)
        boundary = max((index + 1 for index, char in enumerate(self._pending[:limit]) if char in self._BREAKS), default=0)
        boundary = proj.safe_stream_boundary(self._pending, boundary, self._secrets)
        if boundary:
            self._emit(self._pending[:boundary])
            self._pending = self._pending[boundary:]

    def _emit(self, raw: str) -> None:
        if self._message_id is None or self._model_call_id is None:
            return
        cleaned = proj.sanitize_text(raw, self._secrets)
        if not cleaned:
            return
        with self._display_lock:
            if self._pending_channel == "reasoning":
                remaining = proj.REASONING_DISPLAY_LIMIT - len(self._reasoning)
                was_truncated = self._reasoning_truncated
                self._reasoning_truncated |= len(cleaned) > remaining
                cleaned = cleaned[:remaining]
                if not cleaned and was_truncated:
                    return
                self._reasoning += cleaned
            else:
                self._text += cleaned
            self._last_channel = self._pending_channel
            self._sequence += 1
        event = self._envelope(
            "message_delta",
            app_events.stream_delta_identity(self._model_call_id, self._sequence),
            app_events.MessageDeltaPayload(
                message_id=self._message_id,
                role="assistant",
                model_call_id=self._model_call_id,
                chunk_sequence=self._sequence,
                text_delta=cleaned,
                channel=self._pending_channel,
                reasoning_truncated=self._reasoning_truncated,
            ),
            app_events.MessageDeltaEvent,
        )
        self._bus.publish(self._session_id, [event])

    def flush(self) -> None:
        if self._pending:
            self._emit(self._pending)
            self._pending = ""

    def discard(self) -> None:
        self._pending = ""
        with self._display_lock:
            self._text = ""
            self._reasoning = ""
            self._reasoning_truncated = False
            self._last_channel = "text"
            self._sequence += 1
        if self._message_id is not None:
            # 同一个稳定消息重新开始（空尾部）：前端立即清掉失败时的半句，不等普通调用结束。
            self._started(reset=True)

    def partial_view(self, run_id: str) -> AssistantMessageView | None:
        """进程内的安全展示副本；重订阅可读，不写数据库，也不恢复执行。"""
        with self._display_lock:
            if self._run_id != run_id or self._message_id is None:
                return None
            return AssistantMessageView(
                message_id=self._message_id, model_call_id=self._model_call_id,
                text=self._text, reasoning=self._reasoning or None,
                reasoning_truncated=self._reasoning_truncated,
                chunk_sequence=self._sequence, stream_channel=self._last_channel,
                complete=False,
            )


class ApplicationService:
    """会话用例的唯一入口。"""

    def __init__(
        self,
        *,
        graph: builder.ChatGraph,
        app_config: Any,
        model_adapter: Any,
        workspace_root: Path,
        sessions: SessionStore,
        ledger: RequestLedger,
        secrets: Iterable[str] = (),
        bus: EventBus | None = None,
        sequencer: StreamSequencer | None = None,
        connections: ConnectionStore | None = None,
        permissions: PermissionStore | None = None,
        preferences: PreferenceStore | None = None,
        capabilities: CapabilityStore | None = None,
        web_search: WebSearchKeyStore | None = None,
        skills: SkillStore | None = None,
        #: 用户上传的图片附件（2026-10-16）。``None`` = 没接线（测试与 CLI 的轻量路径）：
        #: 那时上传接口如实回 503，而不是把图存到某个说不清的地方。
        attachments: AttachmentStore | None = None,
        #: 对象存储的访问密钥（2026-10-20）：与模型连接共用同一套凭据存储。
        oss_keys: OssKeyStore | None = None,
        model_factory: Callable[[LlmConfig, str], Any] | None = None,
        jobs: JobRuntime | None = None,
    ) -> None:
        self._graph = graph
        from uuid import uuid4

        self._tool_namespace = uuid4().hex
        self._app_config = app_config
        self._model_adapter = model_adapter
        #: 模型连接设置（§6.8）。``None`` = 没有可用设置库：按 ``app_config.llm`` + 注入的适配器运行，
        #: 并把降级原因如实告诉页面（不假装设置还在生效）。
        self._connections = connections
        #: 工作区外路径的档位与授权（T38）。与连接同库同生命周期：没有设置库时退回
        #: ``config.yaml`` 的出厂默认（``external_policy()`` 返回 ``None`` 由下层兜底）。
        self._permissions = permissions
        self._preferences = preferences
        #: 能力开关（§16.1、§16.6）。与上面三项同库同生命周期：没有设置库时退回
        #: ``config.yaml`` 解析出的出厂默认，页面据此不提供开关（写下去是 503）。
        self._capabilities = capabilities
        #: 联网检索的服务凭据（Tavily；与模型连接共用凭据存储）。``None`` = 没有设置库：那时只有
        #: 旧环境变量 ``web_access.tavily_api_key_env`` 能提供密钥，页面不提供录入框。
        self._web_search = web_search
        #: 技能的用户级禁用表（技能管理页，2026-10-16）。与能力开关同库同生命周期：没有设置库时
        #: 视为"没有禁用任何一项"、页面不提供开关（写下去是 503）。
        self._skills = skills
        #: 附件存储（2026-10-16）：内容在 `data/attachments/年/月/日/会话/`，消息里只放引用。
        self._attachments = attachments
        #: 对象存储凭据（2026-10-20）：只在 `attachments.backend=oss` 时使用。
        self._oss_keys = oss_keys
        #: 按连接构造客户端的可调用对象；测试注入确定性假模型走同一条路径。
        self._build_model = model_factory or llm_factory.build_chat_model
        self._workspace_root = Path(workspace_root)
        self._sessions = sessions
        self._conversations = sessions.conversations
        self._ledger = ledger
        self._secrets = tuple(secrets)
        if bus is not None and sequencer is not None and bus.sequencer is not sequencer:
            raise ValueError("EventBus 与应用服务必须共用同一 StreamSequencer")
        self._bus = bus or EventBus(
            sequencer=sequencer, max_pending=app_config.web.sse_max_pending_events
        )
        self._sequencer = self._bus.sequencer
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._streams: dict[str, _StreamingAnswerSink] = {}
        self._tool_streams: dict[str, ToolStream] = {}
        self._compressions: dict[str, CompressionProgress] = {}
        self._catalog_lock = threading.RLock()
        #: 后台任务运行时与任务索引（§17.2）。``None`` 表示**未装配任务索引**（只有不经过
        #: `api.container.open_runtime` 的单元测试会这样）：此时提交路径与 v0.5 完全一致，
        #: 既不记账也不改调度时机——它是"索引不可用"的回退，不是第二种生产模式。
        self._jobs = jobs
        if jobs is not None:
            # 任务生命周期事件由**运行时**在状态真的迁移时通知（§17.3）：只有索引知道"任务到哪一步了"。
            jobs.set_transition_listener(self._on_job_transition)
        #: 每会话一个取消令牌（§17.4）：令牌的生命周期 = "用户当前的这次意图"，因此**每次新提交都换一个**
        #: （旧的取消不会粘住这个会话），而**审批恢复复用同一个**——已取消的任务不会因为点了批准又跑起来。
        self._cancellations: dict[str, CancellationToken] = {}
        self._cancellations_guard = threading.Lock()
        #: 任务总时长上限（`jobs.max_task_seconds`）；`None` = 不限，此时不起看守线程。
        self._max_task_seconds: float | None = getattr(
            getattr(app_config, "jobs", None), "max_task_seconds", None
        )
        self._watchdog_stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        if self._max_task_seconds is not None:
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, name="job-timeout-watchdog", daemon=True
            )
            self._watchdog.start()

    # ---- 取消（§17.4）--------------------------------------------------------

    def _token_for(self, session_id: str) -> CancellationToken:
        """取（必要时创建）该会话当前的取消令牌。"""
        with self._cancellations_guard:
            token = self._cancellations.get(session_id)
            if token is None:
                token = CancellationToken()
                # 重启后的审批恢复也必须保留已经落库的取消意图。
                jobs = [] if self._jobs is None else self._jobs.list_for_session(session_id)
                if jobs and jobs[-1].state == JOB_STATE_CANCELLED:
                    token.cancel(jobs[-1].detail or DEFAULT_CANCEL_REASON)
                self._cancellations[session_id] = token
            return token

    def _reset_cancellation(self, session_id: str) -> None:
        """新提交换一个令牌：取消只对**它被下达时那一轮**有效，不粘住这个会话。"""
        with self._cancellations_guard:
            self._cancellations[session_id] = CancellationToken()

    def _cancel_job(self, job: Job, reason: str) -> bool:
        """取消一条任务的**唯一路径**（用户取消与超时看守共用，§17.4）。"""
        jobs = self._jobs
        if jobs is None:
            return False

        def signal(current: Job) -> str | None:
            # 运行时持有迁移锁且已重新核对目标：结算不能越过这里后换成下一轮令牌。
            run_id = current.run_id
            if run_id is None and current.state != JOB_STATE_QUEUED:
                evidence = self._turn_evidence(current)
                if (
                    evidence is not None
                    and evidence.reported_run is not None
                    and evidence.reported_run == evidence.current_run_id
                ):
                    run_id = evidence.reported_run
            # 先置信号，后落终态；运行编号同次写入，终态不再依赖结算补写。
            self._token_for(current.session_id).cancel(reason)
            return run_id

        return jobs.cancel(job.job_id, detail=reason, before_cancel=signal) is not None

    def cancel_task(self, session_id: str, reason: str = "") -> CommandResultView:
        """取消当前会话在跑的任务（架构 §15.2、§17.4）。

        **幂等**：没有非终态任务时什么都不做，返回当时的结局。**不进幂等台账**——重复调用本来就是
        空操作，给它记一条"执行中"反而会污染"同一请求 id 的重试"语义。
        """
        session = self._session_or_raise(session_id)
        job = self._jobs.active_for_session(session_id) if self._jobs is not None else None
        cancelled = job is not None and self._cancel_job(job, reason.strip() or DEFAULT_CANCEL_REASON)
        view = self._read(session)
        snapshot = self._snapshot(session, view)
        status: CommandStatus = "cancelled" if cancelled else _status_from_snapshot(snapshot)
        return self._result(
            request_id=_digest(OPERATION_CANCEL, session_id, reason),
            operation=OPERATION_CANCEL,
            session=session,
            snapshot=snapshot,
            status=status,
            replayed=False,
        )

    # ---- 超时看守（§17.4）----------------------------------------------------

    def _watchdog_loop(self) -> None:
        """`jobs.max_task_seconds` 的看守：超限就走**同一条取消路径**，不另写一套中止逻辑。"""
        while not self._watchdog_stop.wait(_WATCHDOG_INTERVAL_SECONDS):
            try:
                self.expire_overdue_tasks()
            except Exception:
                # 看守线程不能因为一次异常就退出：超时是尽力而为的兜底，不是执行的必要条件。
                _LOGGER.exception("任务超时看守失败（下一轮继续）")

    def expire_overdue_tasks(self) -> list[str]:
        """把超过 `jobs.max_task_seconds` 的在跑任务取消掉，返回被取消的任务 id。

        计时从**登记时刻**（`created_at`）算起：排队等待也算在任务的总墙钟里——否则一个被无限
        排队的任务永远不会超时。
        """
        jobs = self._jobs
        limit = self._max_task_seconds
        if jobs is None or limit is None:
            return []
        now = datetime.now(timezone.utc)
        expired: list[str] = []
        for job in jobs.jobs_in_flight():
            if _seconds_since(job.created_at, now) <= limit:
                continue
            if self._cancel_job(job, f"超过 max_task_seconds={limit:g} 秒"):
                expired.append(job.job_id)
        return expired

    def recover_interrupted_jobs(self) -> list[JobVerdict]:
        """重启后把索引里遗留的非终态任务**分类收口**（架构 §17.5，v0.6 T61）。

        **只读核对 + 只写索引**：读 Checkpoint 判"能不能证明终态"，判定本身是
        :func:`local_agent.application.recovery.classify_interrupted_job` 这个纯函数。
        **不重提图输入、不执行工具、不调模型、不恢复 Graph**——"可能已部分执行"的任务一律标
        `needs_review` 交给人工核对（§15.2：绝不自动假定恰好执行一次）。

        调用时机：**装配期**（`api.container.open_runtime`，且本进程持有单实例锁时）。之后索引里
        不会再有重启遗留的行，因此这个方法在正常运行期是空操作——它不是第二个执行入口。

        Returns:
            每个被检查任务的判定（含"不需要写库"的那些），供调用方记日志与测试断言。
        """
        jobs = self._jobs
        if jobs is None:
            return []
        verdicts: list[JobVerdict] = []
        for job in jobs.jobs_in_flight():
            verdict = classify_interrupted_job(job, evidence=self._turn_evidence(job))
            verdicts.append(verdict)
            if not verdict.writes:
                continue
            try:
                jobs.recover(job.job_id, target=cast("str", verdict.target), detail=verdict.detail)
            except JobStoreError:
                # 这一行在扫描之后、写入之前被别人推进了（正常装配期不会发生）：如实记日志，
                # **不覆盖**已经写下的状态。
                _LOGGER.exception("重启收口任务 %s 失败（状态未被改写）", job.job_id)
                continue
            _LOGGER.info(
                "重启收口任务 %s：%s → %s（%s）",
                job.job_id,
                job.state,
                verdict.target,
                verdict.verdict,
            )
        return verdicts

    def _adopt_interrupted_job(self, session: Session, *, reported_run: str | None) -> None:
        """把该会话遗留的非终态任务按**同一条判定**收口（§17.5）。

        触发点是"台账已经证明这一次请求的终态"（:meth:`_recover` 的成功分支与 :meth:`_replay`）：
        证据由调用方给出（台账里那条记录的 `run_id`），判定仍走同一个纯函数
        （`classify_interrupted_job`），不是第二条规则。没有非终态任务时它是空操作。
        """
        jobs = self._jobs
        if jobs is None:
            return
        job = jobs.active_for_session(session.session_id)
        if job is None:
            return
        verdict = classify_interrupted_job(
            job, evidence=self._turn_evidence(job, reported_run=reported_run)
        )
        if not verdict.writes:
            return
        try:
            jobs.recover(job.job_id, target=cast("str", verdict.target), detail=verdict.detail)
        except JobStoreError:
            _LOGGER.exception("按台账终态收口任务 %s 失败（状态未被改写）", job.job_id)

    def _turn_evidence(self, job: Job, *, reported_run: str | None = None) -> TurnEvidence | None:
        """读一个任务所属会话的 Checkpoint / 台账事实；读不到时返回 `None`（= 证明不了）。

        ``reported_run`` 由调用方给（台账里那条记录）时优先用它；没给（重启收口）就按 §17.5 的两个
        来源推导：索引自己绑定的 `run_id`（任务已结算过）优先，否则用**该会话台账最后一条**的
        `run_id`（崩溃发生在结算之前时索引还没绑定，而台账在流式期间就绑了）。
        """
        session = self._sqlite_access(partial(self._sessions.get, job.session_id))
        if session is None or session.archived:
            return None
        try:
            view = self._read(session)
        except Exception:
            _LOGGER.exception("收口：读不到会话 %s 的 Checkpoint", job.session_id)
            return None
        run = reported_run if reported_run is not None else job.run_id
        if run is None:
            entry = self._sqlite_access(lambda: self._ledger.latest(job.session_id))
            run = None if entry is None else entry["run_id"]
        children: list[str] = []
        saver = self._graph.checkpointer
        if self._jobs is not None and isinstance(saver, BaseCheckpointSaver) and run is not None:
            for parent_run, child_id in self._jobs.child_links(job.job_id):
                if parent_run != run:
                    continue
                try:
                    saved = saver.get_tuple({"configurable": {"thread_id": child_id}})
                    if saved is not None and child_completion_proven(child_id, saved.checkpoint["channel_values"]):
                        children.append(child_id)
                except Exception:
                    _LOGGER.exception("收口：读不到子任务 %s 的 Checkpoint", child_id)
        return TurnEvidence(
            turn_state=str(view.values.get("task_status") or "") or None,
            current_run_id=proj.current_run_id(view.values) or None,
            has_live_interrupt=view.has_live_interrupt,
            reported_run=run,
            completed_children=tuple(children),
        )

    def _on_job_transition(self, before: Job | None, after: Job) -> None:
        """把任务状态迁移投影成事件并广播（架构 §17.3）。

        `before` 为 `None` 表示任务**诞生**（登记即入队），也要发一条 `job_queued`。
        **单向投影**：只读已经落库的那一行，不写 State、不调模型、不执行工具、不恢复 Graph（§15.4）。
        没有订阅者时 `EventBus.publish` 自然丢弃——事件不落库，"任务到哪一步"由索引给出、
        "这一轮发生了什么"由 Checkpoint 给出。
        """
        if before is not None and before.state == after.state:
            return
        event_type = JOB_EVENT_FOR_STATE[after.state]
        self._bus.publish(
            after.session_id,
            [
                app_events.build_job_event(
                    event_type=event_type,
                    # 稳定标识：同一行（同一 job + 同一次写入）重放得到同一个 event_id
                    event_id=app_events.event_id_for(
                        cast("app_events.EventType", event_type),
                        f"{after.job_id}:{after.updated_at}",
                    ),
                    session_id=after.session_id,
                    job_id=after.job_id,
                    run_id=after.run_id,
                    occurred_at=_as_dt(after.updated_at),
                    stream_sequence=self._sequencer.next(after.session_id),
                    state=after.state,
                    detail=after.detail,
                )
            ],
        )

    # ---- 会话 ---------------------------------------------------------------

    def get_memory_page(self, *, workspace_id: str | None = None, kind: str = "all",
                        query: str = "", status: str = "all", offset: int = 0, limit: int = 30) -> MemoryPageView:
        return data_center.memories(self, workspace_id=workspace_id, kind=kind, query=query,
                                    status=status, offset=offset, limit=limit)

    def get_usage_page(self, *, workspace_id: str | None = None, model: str = "",
                       since: datetime | None = None, until: datetime | None = None,
                       offset: int = 0, limit: int = 30) -> UsagePageView:
        return data_center.usage(self, workspace_id=workspace_id, model=model, since=since,
                                 until=until, offset=offset, limit=limit)

    def preferences_view(self) -> PreferencesView:
        defaults = initial_preferences(self._app_config)
        saved = None if self._preferences is None else self._sqlite_access(self._preferences.read)
        version, values = saved if saved is not None else (0, defaults)
        values = complete_preferences(values, self._app_config)
        return PreferencesView(
            version=version, writable=self._preferences is not None,
            values=values, defaults=defaults, advanced=advanced_preferences(self._app_config),
            resources=memory_settings.resources_view(apply_runtime_preferences(self._app_config, values)),
        )

    def save_preferences(self, request: PreferencesRequest) -> PreferencesView:
        store = self._preferences
        if store is None:
            raise app_errors.settings_store_unavailable(detail="应用偏好暂时无法保存", occurred_at=_now())
        def write() -> PreferencesView:
            current = store.read()
            version, previous = current if current is not None else (0, initial_preferences(self._app_config))
            if version != request.expected_version:
                raise app_errors.preferences_conflict(occurred_at=_now())
            previous = complete_preferences(previous, self._app_config)
            # Older clients omit the new groups; preserve their currently saved values.
            values = request.values.model_copy(update={
                "medium_term": request.values.medium_term or previous.medium_term,
                "embedding": request.values.embedding or previous.embedding,
                "web_access": request.values.web_access or previous.web_access,
            })
            try:
                config = apply_runtime_preferences(self._app_config, values)
                if values.embedding != previous.embedding or values.medium_term != previous.medium_term:
                    memory_settings.validate_activation(config)
            except ValueError as exc:
                raise app_errors.model_settings_invalid(detail=str(exc), occurred_at=_now()) from exc
            try:
                if (values.embedding != previous.embedding or
                    (values.medium_term != previous.medium_term and config.memory.medium_term.enabled)):
                    memory_settings.check_configuration_compatibility(config)
            except Exception as exc:
                # Keep third-party errors/credentials out of settings responses.
                detail = "已有向量库无法校验或与新配置不兼容，请检查依赖并沿用匹配的模型配置。"
                raise app_errors.model_settings_invalid(detail=detail, occurred_at=_now()) from exc
            try:
                store.write(values, expected_version=request.expected_version)
            except PreferenceConflict as exc:
                raise app_errors.preferences_conflict(occurred_at=_now()) from exc
            return PreferencesView(
                version=request.expected_version + 1, writable=True, values=values,
                defaults=initial_preferences(self._app_config),
                advanced=advanced_preferences(self._app_config),
                resources=memory_settings.resources_view(config),
            )
        return self._sqlite_access(write)

    def test_memory_preferences(self, request: MemoryProbeRequest) -> MemoryProbeResult:
        view = self.preferences_view()
        if view.version != request.expected_version:
            raise app_errors.preferences_conflict(occurred_at=_now())
        values = request.values.model_copy(update={
            "medium_term": request.values.medium_term or view.values.medium_term,
            "embedding": request.values.embedding or view.values.embedding,
        })
        try:
            config = apply_runtime_preferences(self._app_config, values)
        except ValueError as exc:
            raise app_errors.model_settings_invalid(detail=str(exc), occurred_at=_now()) from exc
        # Model loading/network must not hold the SQLite lock or persist the draft.
        return memory_settings.probe(config)

    def list_sessions(
        self, *, workspace_root: Path | None = None, workspace_id: str | None = None,
        archived: bool = False,
    ) -> list[SessionSummaryView]:
        """列出会话。`workspace_root` 与 `workspace_id` 二选一。

        给了 `workspace_id` 就只看该工作区（§6.1 的 `list_sessions(workspace_id)`）：HTTP 层
        只拿得到不含路径的稳定标识，路径解析留在服务里。
        """
        root = self._root_of(workspace_root, workspace_id)
        sessions = self._sqlite_access(
            partial(self._sessions.list_sessions, workspace_root=root, archived=archived)
        )
        return [self._summary(session) for session in sessions]

    def list_tasks(
        self,
        *,
        workspace_id: str | None = None,
        include_finished: bool = True,
        limit: int = 50,
    ) -> TaskCenterView:
        """任务中心的一页（架构 §17.6，v0.6 T62）：**只读**索引，不读任何会话的 Checkpoint。

        数据来源只有两处：会话索引（工作区 ↔ 会话、标题）与任务索引（状态与时间戳）。
        "任务在等什么"由页面按状态推导（§17.6 的闭集），服务端不猜、也不引入第二个真源。

        两条口径写在这里，避免被当成 bug：

        - **已归档会话的任务不进列表**——归档就是"离开视线"（归档时若还有待审批会被 §15.2 拒绝）。
        - `counts` / `pending_count` 在**全量结果**上算，只有 `tasks` 受 `limit` 截断。
        """
        jobs = self._jobs
        empty = dict.fromkeys(JOB_STATES, 0)
        if jobs is None:
            return TaskCenterView(tasks=[], counts=empty, pending_count=0)
        root = self._root_of(None, workspace_id)
        sessions = self._sqlite_access(
            partial(self._sessions.list_sessions, workspace_root=root, archived=False)
        )
        by_id = {session.session_id: session for session in sessions}
        # **不要**包在 `_sqlite_access` 里：任务索引与 Checkpoint 共用同一把**不可重入**的
        # `threading.Lock`，JobStore 的每个公开方法已经在锁内执行——再套一层会直接死锁（T58 的实证）。
        rows = jobs.list_for_sessions(list(by_id))
        counts = dict(empty)
        refs: dict[str, WorkspaceRef] = {}
        visible: list[TaskView] = []
        for job in rows:  # 已按登记时间倒序
            counts[job.state] = counts.get(job.state, 0) + 1
            session = by_id.get(job.session_id)
            if session is None:  # 归档 / 删除竞态：跳过，不伪造标题
                continue
            if not include_finished and job.state in TERMINAL_JOB_STATES:
                continue
            ref = refs.get(session.workspace_root)
            if ref is None:
                ref = refs[session.workspace_root] = self._workspace_ref(session)
            visible.append(self._task_view(job, session, ref))
        return TaskCenterView(
            tasks=visible[:limit],
            counts=counts,
            pending_count=counts[JOB_STATE_WAITING_USER] + counts[JOB_STATE_NEEDS_REVIEW],
        )

    def model_configured(self) -> bool:
        """有没有可用的模型配置（健康检查用；不泄露连接名、端点或密钥）。

        没有设置库时看"注入的适配器在不在"（旧配置模式）；有设置库时看"存不存在可用连接"。
        """
        if self._connections is None:
            return self._model_adapter is not None
        return bool(self._sqlite_access(self._connections.list_connections))

    def _refresh_secrets(self) -> None:
        """重新收集要脱敏的密钥。**不是只在启动时收一次**（复审指出）。

        启动那次收不到"用户刚在页面上录入的密钥"；而那种密钥一旦被供应商或工具回显到诊断文案里，
        就会跟着进入快照、SSE 与错误提示。保存/轮换后立刻并入，且只增不减（旧密钥可能还出现在历史文本里）。
        """
        values: set[str] = set()
        if self._connections is not None:
            values.update(self._sqlite_access(self._connections.secret_values))
        # 联网检索的服务凭据同样要脱敏：录入后它可能立刻出现在检索诊断或工具输出里。
        if self._web_search is not None:
            secret = self._sqlite_access(self._web_search.stored)
            if secret:
                values.add(secret)
        self._secrets = tuple(sorted(set(self._secrets) | values))

    def credential_store_state(self) -> tuple[str, str | None]:
        """密钥存储的持久性与降级原因：``("local", None)`` / ``("os", None)`` 或 ``("memory", 说明)``。

        2026-10-13：默认后端从系统凭据存储改为**本地设置库**（§6.8），所以正常状态是 ``local``；
        ``os`` 作为历史值保留在 DTO 里（v1.0 之前默认，现在没有代码路径产出它）。
        没有任何可用设置库时按"没有持久凭据方案"处理——那种情况下也确实没有。
        """
        if self._connections is None:
            return "memory", None
        kind = self._connections.credential_kind
        if kind in {"local", "os"}:
            return kind, None
        note = self._connections.credential_note
        return "memory", note

    def current_model_name(self) -> str:
        """界面上代表"当前默认模型"的名字：默认连接的模型 → 旧配置的模型 → 空串。"""
        if self._connections is not None:
            selection = self._sqlite_access(self._connections.default_selection)
            if selection is not None:
                connection = self._sqlite_access(partial(self._connections.get, selection.connection_id))
                if connection is not None:
                    return connection.model
        return _configured_model_name(self._app_config)

    def list_workspaces(self) -> list[WorkspaceRef]:
        """已用过的工作区（规范化路径，最近使用的在前）。"""
        paths = [Path(path) for path in self._sqlite_access(self._sessions.list_workspaces)]
        refs = [self._ref_for(path) for path in paths]
        name_counts = Counter(ref.display_name for ref in refs)
        return [
            WorkspaceRef(
                workspace_id=ref.workspace_id,
                display_name=f"{ref.display_name} · {ref.workspace_id[:6]}"
                if name_counts[ref.display_name] > 1 else ref.display_name,
            )
            for path, ref in zip(paths, refs)
        ]

    def create_session(
        self,
        *,
        title: str | None = None,
        workspace_root: Path | None = None,
        workspace_id: str | None = None,
        mode: str = "agent",
    ) -> SessionSummaryView:
        """新建会话并切换过去；`workspace_root` 与 `workspace_id` 二选一。"""
        with self._catalog_lock:
            root = self._root_of(workspace_root, workspace_id)
            session = self._sqlite_access(
                partial(self._sessions.create, title=title, workspace_root=root, mode=mode)
            )
        return self._summary(session)

    def select_workspace(self, workspace_root: Path, *, title: str | None = None) -> SessionSummaryView:
        """切换工作区：该工作区已有会话就切过去，否则新建一个。

        会话与工作区一对一绑定，所以这里**不改写**已有会话的 `workspace_root`（§10.6）；
        「切工作区」= 切会话。
        """
        with self._catalog_lock:
            session = self._sqlite_access(
                partial(self._sessions.switch_workspace, Path(workspace_root), title=title)
            )
        return self._summary(session)

    def select_session(self, session_id: str) -> SessionSummaryView:
        with self._catalog_lock:
            self._session_or_raise(session_id)
            session = self._sqlite_access(partial(self._sessions.switch, session_id))
        return self._summary(session)

    def rename_session(self, session_id: str, title: str) -> SessionSummaryView:
        self._session_or_raise(session_id)
        return self._summary(self._sqlite_access(partial(self._sessions.rename, session_id, title)))

    def set_session_mode(self, session_id: str, mode: str, expected_mode: str) -> SessionSummaryView:
        with self._catalog_session_guard(session_id):
            session = self._session_or_raise(session_id)
            if session.mode != expected_mode:
                raise conversation.conflict(session_id, "会话模式已在其他页面改变，请刷新后再切换。")
            if self._sqlite_access(lambda: self._ledger.has_executing(session_id)):
                raise conversation.conflict(session_id, "请先核对并结束中断任务，再切换模式。")
            updated = self._sqlite_access(partial(self._sessions.set_mode, session_id, mode))
            self._bus.publish(session_id, [self._snapshot_event(updated, self._read(updated))])
            return self._summary(updated)

    def end_interrupted_request(self, session_id: str, request_id: str,
                                expected_snapshot_id: str) -> SessionSnapshot:
        """用户核对后结束中断请求；只写检查点，不恢复图、不重放工具。"""
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise app_errors.session_busy(session_id=session_id, request_id=request_id, occurred_at=_now())
        try:
            session = self._session_or_raise(session_id)
            entry = self._sqlite_access(partial(self._ledger.get, request_id))
            view = self._read(session)
            current_id = view.checkpoint_id or f"{session_id}:no-checkpoint"
            if entry is None or entry["session_id"] != session_id:
                raise conversation.conflict(session_id, "找不到待核对请求。")
            if entry["status"] in TERMINAL_STATUSES:
                return self._snapshot(session, view)  # 重复点击不会再修改检查点。
            if current_id != expected_snapshot_id:
                raise conversation.conflict(session_id, "会话状态已改变，请刷新后重新核对。")
            # 判定与写回都按**真源**来（§20.5）：游标可能落后于线程最新检查点，
            # 写在过期祖先上会把人工决定连同唯一审批来源一起丢掉（见 `_closing_anchor`）。
            anchor, anchor_config = self._closing_anchor(session, view)
            state_run = proj.current_run_id(anchor.values) or None
            ref = None
            if entry["run_id"] is not None:
                # 台账绑了 run，但**状态里一个 run 都没记**：说明那个 run 的检查点从没落盘
                # （v0.9 T81 实证：库满时连第一份检查点都写不进去）。"没有记录"不等于"另一轮"——
                # 这里放行，否则这条请求**永远**无法收口（收口被这一句挡住，重试被 `executing`
                # 挡住，`_recover` 又要 run 匹配），会话就再也提交不了任何东西。
                # 记了别的 run 才拒绝：那才是"不能结束其他轮次"。
                if state_run is not None and entry["run_id"] != state_run:
                    raise conversation.conflict(session_id, "请求与当前轮次不一致，不能结束其他轮次。")
                if (anchor.values.get("task_status") in TASK_TERMINAL_STATUSES
                        and not anchor.has_live_interrupt):
                    self._recover(session, entry=entry, operation=entry["operation"])
                    return self._snapshot(session, view)
                builder.close_interrupted_turn(self._graph, anchor_config,
                                               anchor.values, request_id)
                conversation.sync(self, session)
                view = self._read(session)
                ref = view.checkpoint_id
            # 未绑定 run 的请求不能借用上一轮结果。旧 id 保持不可重跑，也不返回别人的结果。
            self._sqlite_access(partial(self._ledger.finish, request_id, status=STATUS_FAILED,
                                       result_ref=ref, error_code=app_errors.RECOVERY_REQUIRED))
            snapshot = self._snapshot(session, view)
            self._bus.publish(session_id, [self._snapshot_event(session, view)])
            return snapshot
        finally:
            lock.release()

    def archive_session(self, session_id: str) -> SessionSummaryView:
        with self._catalog_lock, self._catalog_session_guard(session_id):
            session = self._sqlite_access(partial(self._sessions.set_archived, session_id, True))
        return self._summary(session)

    def restore_session(self, session_id: str) -> SessionSummaryView:
        with self._catalog_lock:
            self._session_or_raise(session_id, allow_archived=True)
            session = self._sqlite_access(partial(self._sessions.set_archived, session_id, False))
        return self._summary(session)

    def rename_workspace(self, workspace_id: str, title: str) -> WorkspaceRef:
        with self._catalog_lock:
            root = self._path_for_workspace_id(workspace_id)
            self._sqlite_access(partial(self._sessions.rename_workspace, root, title))
        return self._ref_for(root)

    def remove_workspace(self, workspace_id: str) -> None:
        # 与新建/重新登记串行；所有关联线程都先取得执行锁，再检查挂起审批。
        with self._catalog_lock, ExitStack() as stack:
            root = self._path_for_workspace_id(workspace_id)
            sessions = self._sqlite_access(partial(self._sessions.list_sessions, workspace_root=root))
            for session in sorted(sessions, key=lambda item: item.session_id):
                stack.enter_context(self._catalog_session_guard(session.session_id))
            self._sqlite_access(partial(self._sessions.remove_workspace, root))

    @contextmanager
    def _catalog_session_guard(self, session_id: str) -> Iterator[None]:
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise app_errors.session_busy(
                session_id=session_id, request_id="catalog-update", occurred_at=_now()
            )
        try:
            session = self._session_or_raise(session_id)
            view = self._read(session)
            if view.has_live_interrupt or self._snapshot(session, view).session_state == "running":
                raise app_errors.session_busy(
                    session_id=session_id, request_id="catalog-update", occurred_at=_now()
                )
            yield
        finally:
            lock.release()

    def select_workspace_by_id(self, workspace_id: str) -> SessionSummaryView:
        """选回列表中的工作区；前端只需传不含绝对路径的稳定标识。"""
        with self._catalog_lock:
            return self.select_workspace(self._path_for_workspace_id(workspace_id))

    def _path_for_workspace_id(self, workspace_id: str) -> Path:
        """把稳定标识解析回工作区路径；未列出的标识 → `WORKSPACE_NOT_FOUND`。

        只认 `list_workspaces()` 里出现过的路径：不按名字猜路径，也不接受客户端传路径当 id
        （否则"选工作区"就变成了任意路径穿越的入口）。
        """
        for path in self._sqlite_access(self._sessions.list_workspaces):
            if self._ref_for(Path(path)).workspace_id == workspace_id:
                return Path(path)
        raise app_errors.workspace_not_found(workspace_id=workspace_id, occurred_at=_now())

    def _root_of(self, workspace_root: Path | None, workspace_id: str | None) -> Path | None:
        """把 `workspace_root` / `workspace_id`（二选一）解析成工作区路径。"""
        if workspace_root is not None and workspace_id is not None:
            msg = "workspace_root 与 workspace_id 只能给一个"
            raise ValueError(msg)
        if workspace_id is not None:
            return self._path_for_workspace_id(workspace_id)
        return workspace_root

    def get_session_snapshot(self, session_id: str) -> SessionSnapshot:
        """只读快照：不跑图、不写 State、不改数据库业务状态。"""
        session = self._session_or_raise(session_id)
        return self._snapshot(session, self._read(session))

    def subscribe_events(self, session_id: str) -> EventSubscription:
        """登记订阅者并投递边界快照。"""
        session = self._session_or_raise(session_id)
        return self._bus.subscribe(
            session_id,
            boundary=lambda: self._snapshot_event(session, self._read(session)),
        )

    def unsubscribe_events(self, subscription: EventSubscription) -> None:
        self._bus.unsubscribe(subscription)

    # ---- 模型连接（§6.8、§6.1）----------------------------------------------

    def list_model_connections(self) -> list[ModelConnectionView]:
        """列出可用连接（含"哪条是默认"）。已停用的连接不出现——它们只是历史。"""
        store = self._require_store()
        def read() -> list[ModelConnectionView]:
            # 默认选择、连接行与密钥状态共用同一条设置库连接；必须在一把锁里读完。
            default = store.default_selection()
            default_id = None if default is None else default.connection_id
            return [
                self._connection_view(item, is_default=item.connection_id == default_id)
                for item in store.list_connections()
            ]
        return self._sqlite_access(read)

    def save_model_connection(
        self,
        draft: ConnectionDraft,
        *,
        api_key: str | None = None,
        expected_version: int | None = None,
    ) -> ModelConnectionView:
        """新增或编辑一条连接（§6.8）。

        密钥只经凭据存储：本方法接收明文 ``api_key``，但**只**把它交给凭据存储，返回值里只有
        `key_status` 与不含明文的引用。

        Raises:
            ApplicationError: 字段不合法（`MODEL_SETTINGS_INVALID`，回显的是脱敏后的规则说明）、
                版本冲突（`MODEL_SETTINGS_CONFLICT`）、连接不存在或已停用、设置库不可用。
        """
        store = self._require_store()
        try:
            saved = self._sqlite_access(
                partial(store.save, draft, api_key=api_key, expected_version=expected_version)
            )
        except ConnectionVersionConflict as exc:
            raise app_errors.model_settings_conflict(
                connection_id=draft.connection_id or "", occurred_at=_now()
            ) from exc
        except ConnectionValidationError as exc:
            # 领域校验消息面向用户（例如"窗口必须大于输出上限"），但仍然过一遍脱敏：
            # 校验消息中的路径完整保留，密钥仍须脱敏（§15.8）。
            raise app_errors.model_settings_invalid(
                detail=config_module.redact_secrets(str(exc), self._secrets), occurred_at=_now()
            ) from exc
        except ModelSettingsError as exc:  # 连接不存在 / 已停用
            raise app_errors.model_connection_not_found(
                connection_id=draft.connection_id or "", occurred_at=_now()
            ) from exc
        # 刚录入的密钥必须立刻进入脱敏集合：它可能立刻出现在供应商回显或工具输出里。
        self._refresh_secrets()
        def view() -> ModelConnectionView:
            default = store.default_selection()
            return self._connection_view(
                saved, is_default=default is not None and default.connection_id == saved.connection_id
            )
        return self._sqlite_access(view)

    def retire_model_connection(
        self, connection_id: str, *, expected_version: int | None = None
    ) -> ModelConnectionView:
        """"删除"一条连接 = 停用（§6.8）：历史模型标识与用量不删，密钥条目也留着。"""
        store = self._require_store()
        try:
            retired = self._sqlite_access(
                partial(store.retire, connection_id, expected_version=expected_version)
            )
        except ConnectionVersionConflict as exc:
            raise app_errors.model_settings_conflict(
                connection_id=connection_id, occurred_at=_now()
            ) from exc
        except ModelSettingsError as exc:
            raise app_errors.model_connection_not_found(
                connection_id=connection_id, occurred_at=_now()
            ) from exc
        return self._sqlite_access(lambda: self._connection_view(retired, is_default=False))

    def test_model_connection(self, connection_id: str) -> ModelConnectionView:
        """用户主动触发的连接测试：分项报告鉴权、聊天、工具调用与流式能力（§6.8）。

        会真发一次小请求（可能产生少量费用），因此只在用户点击时调用；失败**不改**默认与会话选择。
        结果记录在这条连接上（`record_check`），读接口据此显示状态。
        """
        store = self._require_store()
        connection = self._require_connection(connection_id)
        report = connection_probe.probe_connection(
            connection,
            build_model=self._build_model,
            api_key=self._api_key_for(connection),
            secrets=self._secrets,
        )
        try:
            # 带上**被探测的版本**：探测期间若另一个页面改了端点/模型，结论属于旧配置，必须丢弃并报冲突。
            updated = self._sqlite_access(
                partial(
                    store.record_check,
                    connection_id,
                    report,
                    expected_version=connection.config_version,
                )
            )
        except ConnectionVersionConflict as exc:
            raise app_errors.model_settings_conflict(
                connection_id=connection_id, occurred_at=_now()
            ) from exc
        def view() -> ModelConnectionView:
            default = store.default_selection()
            return self._connection_view(
                updated, is_default=default is not None and default.connection_id == updated.connection_id
            )
        return self._sqlite_access(view)

    def select_default_model(
        self,
        connection_id: str,
        *,
        expected_version: int | None = None,
        expected_selection: SelectionRef | None = None,
    ) -> ModelConnectionView:
        """设置新会话继承的默认连接；只影响**下一次新用户提交**（§6.8）。

        Args:
            connection_id: 要选中的连接。
            expected_version: 这条连接你看到的版本。
            expected_selection: 你看到的**当前默认**；库里的已被别的页面改过就拒绝（§6.1）。
        """
        store = self._require_store()
        try:
            self._sqlite_access(
                partial(
                    store.select_default,
                    connection_id,
                    expected_version=expected_version,
                    expected_selection=expected_selection,
                )
            )
        except SelectionConflict as exc:
            # 必须排在 `ModelSettingsError` 之前：它是子类，否则会被当成"连接不存在"报成 404。
            # 用被选中的连接 id 做关联号（诊断 id 只是相关性手柄，消息不暴露它）。
            raise app_errors.selection_conflict(
                scope_id=connection_id, occurred_at=_now()
            ) from exc
        except ConnectionVersionConflict as exc:
            raise app_errors.model_settings_conflict(
                connection_id=connection_id, occurred_at=_now()
            ) from exc
        except ModelSettingsError as exc:
            raise app_errors.model_connection_not_found(
                connection_id=connection_id, occurred_at=_now()
            ) from exc
        selected = self._require_connection(connection_id)
        return self._sqlite_access(lambda: self._connection_view(selected, is_default=True))

    def select_session_model(
        self,
        session_id: str,
        connection_id: str | None,
        *,
        expected_version: int | None = None,
        expected_selection: SelectionRef | None = None,
    ) -> ModelConnectionView | None:
        """为某个会话选择**下一轮**用的连接；``connection_id=None`` 表示恢复为跟随默认。

        只写设置，不碰正在运行的那一轮：那一轮用的是提交时固定的连接（`model_binding`）。
        """
        session = self._session_or_raise(session_id)
        store = self._require_store()
        try:
            if connection_id is None:
                # "恢复跟随默认"同样是**写入**（删掉会话自己的选择），所以也要过同一道校验：
                # 否则甲页面刚选了 B，乙页面一句"跟随默认"就把它抹了。
                self._sqlite_access(
                    partial(
                        store.select_session,
                        session.session_id,
                        None,
                        expected_selection=expected_selection,
                    )
                )
                return None
            self._sqlite_access(
                partial(
                    store.select_session,
                    session.session_id,
                    connection_id,
                    expected_version=expected_version,
                    expected_selection=expected_selection,
                )
            )
        except SelectionConflict as exc:
            raise app_errors.selection_conflict(
                scope_id=session.session_id, occurred_at=_now()
            ) from exc
        except ConnectionVersionConflict as exc:
            raise app_errors.model_settings_conflict(
                connection_id=connection_id or session.session_id, occurred_at=_now()
            ) from exc
        except ModelSettingsError as exc:
            raise app_errors.model_connection_not_found(
                connection_id=connection_id or session.session_id, occurred_at=_now()
            ) from exc
        selected = self._require_connection(connection_id)
        return self._sqlite_access(lambda: self._connection_view(selected, is_default=False))

    def default_model_connection(self) -> str | None:
        """当前默认连接的 id（没有任何连接时为 ``None``）——页面用它说明"下一轮会用哪条"。"""
        if self._connections is None:
            return None
        selection = self._sqlite_access(self._connections.default_selection)
        return None if selection is None else selection.connection_id

    def session_model_connection(self, session_id: str) -> str | None:
        """某个会话显式选择的连接 id；没选过返回 ``None``（跟随默认）。"""
        if self._connections is None:
            return None
        selection = self._sqlite_access(partial(self._connections.session_selection, session_id))
        return None if selection is None else selection.connection_id

    # ---- 能力状态（T56；§16.6）---------------------------------------------

    def capabilities_view(self) -> CapabilitiesView:
        """能力开关与每一项的可用性（**每次现算**，不缓存；架构 §16.1、§16.6）。

        为什么不缓存：答案取决于**当前开关**（用户刚在面板里改过）、配置与**这台机器上装没装 git**，
        三者都可能在运行期变化。缓存一个旧答案比不显示更糟——那会让人以为"改了没用"。

        对外只有**能力名与解释**，没有工具名（§15.8 的字段门槛）。`switchable` 是**服务端枚举**的
        可开关集，页面照它渲染、不自己拼一份；`switches` 是解析后的完整开关表，页面据此画开关状态。
        """
        from local_agent.tools.browser_tools import browser_availability

        browser_available, browser_hint = browser_availability()
        probe = git_availability()
        switches = self.capability_switches()
        store = self._capabilities
        return CapabilitiesView(
            switches=switches,
            switchable=list(store.switchable if store is not None else switchable_capabilities()),
            store_unavailable=store is None,
            capabilities=[
                CapabilityStatusView(
                    capability=status.capability,
                    label=status.label,
                    available=status.available,
                    reason=status.reason,
                    detail=status.detail,
                    hint=status.hint,
                    locked=status.locked,
                )
                for status in capability_status(
                    switches, git_available=probe.available, git_hint=probe.hint,
                    browser_available=browser_available, browser_hint=browser_hint,
                )
            ],
        )

    def capability_switches(self) -> dict[str, bool]:
        """当前生效的完整开关表（§16.1）：**设置库优先、``config.yaml`` 只是出厂默认**。

        与运行期 `_context` 同源：面板显示的能力集合必须和"下一次提交真正绑给模型的那份"是同一个。
        与外部路径档位（T38）同一口径：**每一轮现取、不缓存**——用户刚在面板里改完，下一次运行就该
        按新开关绑定工具；没有设置库时退回出厂默认。
        """
        store = self._capabilities
        if store is None:
            return resolve_switches(self._app_config.capability_default_layer())
        return self._sqlite_access(store.switches)

    def set_capabilities(self, changes: dict[str, bool]) -> CapabilitiesView:
        """保存用户改过的开关项（**合并式**；"保存即生效、作用于下一轮"），返回保存后的完整视图。

        合并而不是覆盖整张表：库里存的是"改过的那几项"，页面只报告它动过的那一项，其余保持原样。

        Raises:
            ApplicationError: 出现未登记的能力名（``MODEL_SETTINGS_INVALID``），或没有可用设置库
                （``SETTINGS_STORE_UNAVAILABLE``）。
        """
        store = self._require_capabilities()
        unknown = unknown_switch_names(changes)
        if unknown:
            # 取值与领域层同源：可开关集由服务端枚举，页面打错名字得到 422，而不是静默存进去。
            raise app_errors.model_settings_invalid(
                detail=f"未登记的能力：{'、'.join(unknown)}。"
                f"可开关的能力：{'、'.join(store.switchable)}。",
                occurred_at=_now(),
            )
        self._sqlite_access(partial(store.update_switches, changes))
        from local_agent.tools.runtime_sessions import RESOURCES

        disabled = {name for name in ("terminal", "page_control") if changes.get(name) is False}
        if changes.get("shell") is False:
            disabled.add("terminal")
        RESOURCES.close_capabilities(self._tool_namespace, disabled)
        return self.capabilities_view()

    # ---- 联网检索的服务凭据（Tavily）----------------------------------------
    # 数值型偏好（条数上限、域名策略…）走 `RuntimePreferences.web_access`；这里只管**密钥**一件事。
    # 密钥与模型连接同一条口径（§6.8）：只进设置库或进程内存，读取接口只报"有没有"。

    def web_search_key_view(self) -> WebSearchKeyView:
        """当前凭据状态：有没有、从哪来、能不能存在设置库里。

        没有设置库时**不报错**：返回 ``configured=false`` 且 ``store="unavailable"``——页面要能
        打开这一页并如实说明"这里录入也存不下来"，而不是整页打不开。
        """
        env_name = self._app_config.web_access.tavily_api_key_env
        if self._web_search is None:
            # 没有设置库 = 没有凭据存储：只剩旧环境变量这条路（取值规则仍走同一个实现，不另写一份）。
            return WebSearchKeyView(
                configured=bool(WebSearchKeyStore().resolve(env_name=env_name)),
                source="environment" if WebSearchKeyStore().resolve(env_name=env_name) else "none",
                store="unavailable",
                note="设置库不可用：密钥只能来自环境变量，页面无法保存。",
            )
        stored = self._sqlite_access(self._web_search.stored)
        if stored:
            return WebSearchKeyView(
                configured=True, source="settings", store=self._key_store_kind(), note=self._key_store_note()
            )
        present = bool(WebSearchKeyStore().resolve(env_name=env_name))
        return WebSearchKeyView(
            configured=present, source="environment" if present else "none",
            store=self._key_store_kind(), note=self._key_store_note(),
        )

    def save_web_search_key(self, api_key: str) -> WebSearchKeyView:
        """写入（覆盖）Tavily 密钥，返回保存后的状态视图。

        Raises:
            ApplicationError: 没有可用的凭据存储（``SETTINGS_STORE_UNAVAILABLE``），
                或密钥为空白/超长（``MODEL_SETTINGS_INVALID``）。
        """
        store = self._require_web_search()
        try:
            self._sqlite_access(partial(store.set, api_key))
        except CredentialStoreError as exc:
            raise app_errors.model_settings_invalid(detail=str(exc), occurred_at=_now()) from exc
        # 刚录入的密钥必须立刻进入脱敏集合：它可能立刻出现在检索诊断或工具输出里。
        self._refresh_secrets()
        return self.web_search_key_view()

    def clear_web_search_key(self) -> WebSearchKeyView:
        """删除已保存的密钥（幂等），返回删除后的状态视图。"""
        store = self._require_web_search()
        self._sqlite_access(store.clear)
        return self.web_search_key_view()

    def web_search_api_key(self) -> str | None:
        """装配运行期工具用的密钥明文：**不进任何 DTO、日志或异常消息**。

        取值顺序与模型连接一致：凭据存储 → 旧环境变量。没有设置库时只看环境变量。
        """
        env_name = self._app_config.web_access.tavily_api_key_env
        if self._web_search is None:
            return WebSearchKeyStore().resolve(env_name=env_name)
        return self._sqlite_access(partial(self._web_search.resolve, env_name=env_name))

    def _key_store_kind(self) -> CredentialStoreState:
        if self._web_search is None:
            return "unavailable"
        return self._web_search.store_kind or "unavailable"

    def _key_store_note(self) -> str | None:
        return None if self._web_search is None else self._web_search.store_note

    def _require_web_search(self) -> WebSearchKeyStore:
        if self._web_search is None:
            raise app_errors.settings_store_unavailable(
                detail="没有可用的设置库", occurred_at=_now()
            )
        return self._web_search

    # ---- 权限（T38；§1.5）---------------------------------------------------

    def permissions(self) -> PermissionsView:
        """当前档位与授权条目（"设置 → 权限"的读）。没有设置库时用出厂默认并如实标注。"""
        default_mode = self._app_config.workspace.external_paths.mode
        if self._permissions is None:
            # 没有设置库：档位取 `config.yaml`，但**判定与有库时共用同一套**——
            # 两条路各写一遍，迟早出现"页面说的和实际判定的不一样"。
            return PermissionsView(
                mode=default_mode,
                default_mode=default_mode,
                grants=[],
                store_unavailable=True,
            )
        store = self._permissions

        def read() -> PermissionsView:
            # 档位与授权条目必须在一把锁里读完：两次读之间用户可能刚好改过其中一项。
            return PermissionsView(
                mode=store.mode(),
                default_mode=store.default_mode,
                grants=[
                    ExternalPathGrantView(
                        path=item.path,
                        access=item.access,
                        created_at=item.created_at,
                        updated_at=item.updated_at,
                    )
                    for item in store.grants()
                ],
            )

        return self._sqlite_access(read)

    def set_external_paths_mode(self, mode: ExternalPathsMode) -> PermissionsView:
        """保存档位（"保存即生效、作用于下一轮"）。

        Raises:
            ApplicationError: 没有可用设置库（``SETTINGS_STORE_UNAVAILABLE``）。
        """
        self._require_permissions().set_mode(mode)
        self.close_tool_sessions()
        return self.permissions()

    def grant_external_path(
        self, path: str, access: ExternalPathAccess
    ) -> PermissionsView:
        """新增或改写一条外部路径授权（同一个规范化目标只留一行）。

        Raises:
            ApplicationError: 路径为空、目标是受保护位置，或设置库不可用。
        """
        target = path.strip()
        if not target:
            raise app_errors.permission_grant_invalid(
                detail="目录路径不能为空。", occurred_at=_now()
            )
        protected = protected_target_reason(Path(target).expanduser())
        if protected:
            # 受保护目标即使人工批准也不执行副作用，授权它没有任何意义，只会让用户以为放开了。
            raise app_errors.permission_grant_invalid(
                detail=f"该目录属于受保护位置（{protected}），不能授权给 Agent。",
                occurred_at=_now(),
            )
        store = self._require_permissions()
        self._sqlite_access(partial(store.grant, target, access))
        self.close_tool_sessions()
        return self.permissions()

    def revoke_external_path(self, path: str) -> PermissionsView:
        """撤销一条授权；不存在也算成功（幂等），返回撤销后的完整视图。"""
        store = self._require_permissions()
        self._sqlite_access(partial(store.revoke, path))
        self.close_tool_sessions()
        return self.permissions()

    def close_tool_sessions(self) -> None:
        """服务退出或目录权限变化时收走旧令牌与浏览器配置会话。"""
        from local_agent.tools.runtime_sessions import RESOURCES

        RESOURCES.close_namespace(self._tool_namespace)

    # ---- 技能管理（技能管理页，2026-10-16；§16.5）---------------------------

    def skills_view(self) -> SkillsView:
        """本机发现到的技能与它们的启用状态（**每次现算**，不缓存；契约 §16.5）。

        为什么不缓存：技能是"在发现根里现扫出来的"——用户刚放进去一个、或在别处删掉一个，下一次
        打开这一页就该看到真实结果。它与模型清单、`skill_load` **同源**：同一组发现根 + 同一份禁用表。

        页面只给**名字、一句描述、来源层与开关状态**，不给技能目录的绝对路径：技能来源常在用户主
        目录下，设置页没有"必须知道主目录结构"的理由（§15.8 的字段门槛）。
        """
        catalog = self._skill_catalog()
        disabled = self._disabled_skills()
        return SkillsView(
            enabled=bool(self.capability_switches().get("skill")),
            store_unavailable=self._skills is None,
            roots=self._skill_roots_view(catalog),
            skills=[
                SkillEntryView(
                    name=skill.name,
                    description=skill.description,
                    source=skill.source,
                    enabled=skill.name not in disabled,
                    model_invocable=skill.model_invocable,
                    user_invocable=skill.user_invocable,
                    declared_capabilities=list(skill.declared_capabilities),
                )
                for skill in catalog.skills
            ],
            diagnostics=list(catalog.diagnostics),
        )

    def set_skill_enabled(self, name: str, enabled: bool) -> SkillsView:
        """启用/禁用一个技能（**保存即生效、作用于下一轮**），返回保存后的完整视图。

        禁用**不删除任何文件**：只让这个技能对模型不可用（清单里不出现、正文也取不到）。它是不是
        仍被"发现"由发现根决定，与这个开关无关——这也是为什么页面上仍要列出被禁用的技能。

        Raises:
            ApplicationError: 名字不在当前发现的技能里（``MODEL_SETTINGS_INVALID``，理由带上可选项），
                或没有可用设置库（``SETTINGS_STORE_UNAVAILABLE``）。
        """
        store = self._require_skills()
        known = [skill.name for skill in self._skill_catalog().skills]
        if name not in known:
            # 与能力开关同一条口径：名字错了就 422 并列出可选项，不静默存进去（存进去等于永久的幽灵开关）。
            raise app_errors.model_settings_invalid(
                detail=(
                    f"没有发现名为 {name} 的技能。"
                    + (f"当前可用的是：{'、'.join(known)}。" if known else "当前没有发现任何技能。")
                ),
                occurred_at=_now(),
            )
        self._sqlite_access(partial(store.update, {name: enabled}))
        return self.skills_view()

    def _skill_catalog(self) -> SkillCatalog:
        """按当前配置发现一次技能（页面与运行期**同一组发现根**）。

        工作区根取**进程当前目录**：项目级发现根本轮不读（§16.5），所以这里没有"该用哪个工作区"
        的问题——不为了凑一个参数去挑一个工作区，那会给人"这一页跟工作区有关"的错觉。

        ``enabled`` 取**能力开关的当前值**（2026-10-20）：打包形态没有 config.yaml，启动配置里
        `skills.enabled` 是出厂默认 False；若这里只读启动配置，用户在「设置 → 能力」里把技能打开后，
        这一页仍然空着——看起来就是"识别不到技能"。运行期同理由 `apply_capability_gates` 回灌，
        两处必须同源。
        """
        config = self._app_config.skills
        enabled = bool(self.capability_switches().get("skill"))
        if enabled != config.enabled:
            config = config.model_copy(update={"enabled": enabled})
        # 用户自己添加的根（2026-10-20）：每轮现取，保存即生效、作用于下一轮。
        extra = [root for root in self._skill_extra_roots() if root not in config.user_dirs]
        if extra:
            config = config.model_copy(update={"user_dirs": [*config.user_dirs, *extra]})
        return discover_skills(config, workspace_root=Path.cwd(), current_cwd=Path.cwd())

    def _skill_extra_roots(self) -> list[str]:
        """用户在技能页添加的发现根（没有设置库时为空）。"""
        store = self._skills
        if store is None:
            return []
        return self._sqlite_access(store.extra_roots)

    def _skill_roots_view(self, catalog: SkillCatalog) -> list[SkillRootView]:
        """把"当前在扫哪些目录"投影成页面 DTO（2026-10-20）。

        顺序：随包 → 个人级 → 自定义。`count` 按**实际发现**统计，所以"路径存在但一个技能都没有"
        也会显示 0——那正是用户判断"路径填错没有"的依据。
        """
        from local_agent import app_paths
        from local_agent.context.skills_loader import USER_ROOTS

        counts: dict[str, int] = {}
        for skill in catalog.skills:
            key = str(skill.root)
            counts[key] = counts.get(key, 0) + 1

        def count_of(path: Path) -> int:
            try:
                return counts.get(str(path.resolve()), 0)
            except OSError:
                return 0

        entries: list[SkillRootView] = []
        bundled = app_paths.bundled_skills_dir()
        if bundled is not None:
            entries.append(SkillRootView(
                path=str(bundled), source="随包技能", count=count_of(bundled),
            ))
        home = Path.home()
        if self._app_config.skills.include_user_skills:
            for _rank, relative, source in USER_ROOTS:
                candidate = home / relative
                if not candidate.is_dir():
                    continue
                entries.append(SkillRootView(
                    path=str(candidate), source=source, count=count_of(candidate),
                ))
        for root in self._skill_extra_roots():
            candidate = Path(root)
            entries.append(SkillRootView(
                path=str(candidate),
                source="自定义根",
                count=count_of(candidate),
                removable=True,
                readable=candidate.is_dir(),
            ))
        return entries

    def add_skill_root(self, path: str) -> SkillsView:
        """把一个已存在的目录加进发现根（**只引用、不复制**，2026-10-20）。"""
        store = self._require_skills()
        text = str(path or "").strip()
        if not text:
            raise app_errors.model_settings_invalid(
                detail="请填写要添加的技能目录（绝对路径）。", occurred_at=_now()
            )
        candidate = Path(text)
        if not candidate.is_absolute():
            raise app_errors.model_settings_invalid(
                detail=(
                    "技能发现根必须是**绝对路径**（例如 C:\\Users\\你\\下载\\skills）："
                    "相对路径的基准在不同形态下不一样，没法可靠解析。"
                ),
                occurred_at=_now(),
            )
        if not candidate.is_dir():
            raise app_errors.model_settings_invalid(
                detail=f"这个目录不存在或不是目录：{candidate.as_posix()}", occurred_at=_now()
            )
        resolved = candidate.resolve()
        known = set()
        from local_agent import app_paths
        from local_agent.context.skills_loader import USER_ROOTS

        bundled = app_paths.bundled_skills_dir()
        if bundled is not None:
            known.add(str(bundled.resolve()))
        if self._app_config.skills.include_user_skills:
            known.update(str((Path.home() / relative).resolve()) for _r, relative, _s in USER_ROOTS)
        if str(resolved) in known:
            raise app_errors.model_settings_invalid(
                detail="这个目录已经在默认发现根里了，不需要重复添加。", occurred_at=_now()
            )
        self._sqlite_access(partial(store.add_root, str(resolved)))
        return self.skills_view()

    def remove_skill_root(self, path: str) -> SkillsView:
        """移除一个**用户自己添加**的根（不删任何文件，只是不再扫描）。"""
        store = self._require_skills()
        resolved = str(Path(str(path or "").strip()).resolve())
        if resolved not in self._skill_extra_roots():
            raise app_errors.model_settings_invalid(
                detail="只能移除自己添加的发现根；随包与个人级根不支持在这里删除。",
                occurred_at=_now(),
            )
        self._sqlite_access(partial(store.remove_root, resolved))
        return self.skills_view()

    def _disabled_skills(self) -> frozenset[str]:
        """用户在设置里禁用的技能名（没有设置库时视为"没有禁用任何一项"）。"""
        store = self._skills
        if store is None:
            return frozenset()
        return self._sqlite_access(store.disabled)

    def _image_context_turns(self) -> int:
        """设置里的「图片上下文轮数」（每轮现取：保存即生效，作用于下一轮）。

        没有设置库、或旧库里没有这一项时用出厂默认。越界值在模型层就被拒（1~10），
        这里再夹一次只是防止手工改库把请求撑爆。
        """
        if self._preferences is None:
            return DEFAULT_IMAGE_CONTEXT_TURNS
        saved = self._sqlite_access(self._preferences.read)
        if saved is None:
            return DEFAULT_IMAGE_CONTEXT_TURNS
        _, values = saved
        attachments = getattr(values, "attachments", None)
        if attachments is None:
            return DEFAULT_IMAGE_CONTEXT_TURNS
        return max(1, min(10, int(attachments.image_context_turns)))

    def _require_skills(self) -> SkillStore:
        if self._skills is None:
            raise app_errors.settings_store_unavailable(
                detail="设置库没有打开", occurred_at=_now()
            )
        return self._skills

    def _require_permissions(self) -> PermissionStore:
        if self._permissions is None:
            raise app_errors.settings_store_unavailable(
                detail="设置库没有打开", occurred_at=_now()
            )
        return self._permissions

    def _require_capabilities(self) -> CapabilityStore:
        if self._capabilities is None:
            raise app_errors.settings_store_unavailable(
                detail="设置库没有打开", occurred_at=_now()
            )
        return self._capabilities

    def _require_store(self) -> ConnectionStore:
        if self._connections is None:
            raise app_errors.settings_store_unavailable(
                detail="设置库没有打开", occurred_at=_now()
            )
        return self._connections

    def _require_connection(self, connection_id: str) -> ModelConnection:
        """取一条可用连接；不存在（或已停用）时给出公开错误码，而不是内部异常。"""
        store = self._require_store()
        try:
            found = self._sqlite_access(partial(store.require, connection_id))
        except ModelSettingsError as exc:
            raise app_errors.model_connection_not_found(
                connection_id=connection_id, occurred_at=_now()
            ) from exc
        if not found.enabled:
            raise app_errors.model_connection_not_found(
                connection_id=connection_id, occurred_at=_now()
            )
        return found

    def _connection_view(self, connection: ModelConnection, *, is_default: bool) -> ModelConnectionView:
        """连接记录 → 读 DTO。**没有密钥字段**，只有 `key_status`（§15.9）。"""
        report = connection.capability
        return ModelConnectionView(
            connection_id=connection.connection_id,
            display_name=connection.display_name,
            provider="openai_compatible",
            base_url=connection.base_url,
            transport=_transport_dto(connection.transport),
            model_id=connection.model,
            context_window_tokens=connection.context_window_tokens,
            max_tokens=connection.max_tokens,
            temperature=connection.temperature,
            timeout_seconds=connection.timeout_seconds,
            retry_count=connection.retry_count,
            streaming=connection.streaming,
            # 逐连接的"支持图片输入"（2026-10-16）：页面上要回显这个勾选。
            supports_vision=connection.supports_vision,
            auth_mode=connection.auth_mode,
            key_status=self._key_status(connection),
            credential_ref=connection.credential_ref,
            status=_connection_status_dto(report.status),
            capabilities=[
                CapabilityCheckView(capability="chat", result=_capability_result_dto(report.chat)),
                CapabilityCheckView(
                    capability="tool_calling", result=_capability_result_dto(report.tools)
                ),
                CapabilityCheckView(
                    capability="streaming", result=_capability_result_dto(report.streaming)
                ),
            ],
            config_version=str(connection.config_version),
            is_default=is_default,
        )

    def _key_status(self, connection: ModelConnection) -> KeyStatus:
        """只回答"已配置 / 未配置 / 仅本次进程内"（§6.8：读取接口不返回完整或部分密钥）。"""
        if connection.auth_mode == "none":
            return "configured"  # 明确不需要 Key 的本地端点：没有"缺密钥"这件事
        store = self._require_store()
        if not store.resolve_api_key(connection):
            return "missing"
        if connection.credential_ref and store.credential_kind == "memory":
            # 系统凭据存储不可用时的临时模式：密钥只在本进程内，重启后要重新录入。
            return "session_only"
        return "configured"

    # ---- 执行状态修改（共用同一互斥边界）------------------------------------

    def _require_attachments(self) -> Any:
        """附件存储没接线时如实说清（与 `_require_credentials` 同一条做法）。"""
        if self._attachments is None:
            raise AttachmentError("这个运行没有启用附件存储：请用带数据目录的宿主（本机应用/CLI）。")
        return self._attachments

    def _attachments_preferences(self) -> Any:
        """图片附件偏好（没有设置库时用出厂默认）。"""
        from local_agent.model_settings.preferences import AttachmentsPreferences

        if self._preferences is None:
            return AttachmentsPreferences()
        saved = self._sqlite_access(self._preferences.read)
        if saved is None:
            return AttachmentsPreferences()
        _, values = saved
        return getattr(values, "attachments", None) or AttachmentsPreferences()

    def _upload_backend(self) -> Any:
        """当前生效的上传后端：本地文件，或 S3 兼容对象存储。

        **不在两者之间静默回退**：选了对象存储但配置或密钥不全时明确报错——否则用户会以为
        图在对象存储里，实际还在本机（之后换机器就"图丢了"）。
        """
        prefs = self._attachments_preferences()
        if str(getattr(prefs, "backend", "local")) != "oss":
            return self._require_attachments()
        keys = None if self._oss_keys is None else self._oss_keys.read()
        if not keys:
            raise app_errors.model_settings_invalid(
                detail="对象存储已启用，但访问密钥还没保存：请到「设置 → 图片与附件」填入 Access Key / Secret Key。",
                occurred_at=_now(),
            )
        from local_agent.attachment_oss import OssAttachmentStore
        from local_agent.oss_storage import OssConfig, S3ObjectStore

        config = OssConfig(
            endpoint=str(getattr(prefs, "oss_endpoint", "") or "").strip(),
            bucket=str(getattr(prefs, "oss_bucket", "") or "").strip(),
            region=str(getattr(prefs, "oss_region", "") or "auto").strip() or "auto",
            public_base_url=str(getattr(prefs, "oss_public_base_url", "") or "").strip(),
            key_prefix=str(getattr(prefs, "oss_key_prefix", "") or ""),
            path_style=bool(getattr(prefs, "oss_path_style", True)),
        )
        return OssAttachmentStore(config=config, store=S3ObjectStore(config, *keys))

    def save_attachment(self, session_id: str, data_base64: str) -> dict[str, Any]:
        """存一张用户上传的图片（2026-10-16）：返回引用，**媒体类型由服务端按魔数判定**。

        为什么不让客户端声明媒体类型：它和扩展名一样不可信（`报告.png` 完全可能是 HTML）。
        单图有字节/像素上限（超限拒绝而不是截断）；解码失败和格式不支持由 :class:`AttachmentError`
        说明白，路由把它翻成 422 的可执行提示。
        """
        store = self._upload_backend()
        self._session_or_raise(session_id)
        try:
            # `validate=True`：不是严格 base64 就当场拒绝，别让它变成"存进去一坨坏数据"。
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AttachmentError(f"上传内容不是合法的 base64：{exc}") from exc
        try:
            saved = store.save(session_id, data)
        except OssStorageError as exc:
            raise AttachmentError(str(exc)) from exc
        return {
            "attachment_id": saved.attachment_id,
            "media_type": saved.media_type,
            "byte_count": saved.byte_count,
            "url": getattr(saved, "url", None),
        }

    def read_attachment(self, session_id: str, attachment_id: str) -> tuple[bytes, str] | str:
        """读回一张已上传的图片（给页面显示缩略图用）。

        - 本地后端：返回 ``(字节, 媒体类型)``；
        - 对象存储后端：引用本身就是公开 URL，**直接返回那个字符串**，路由把它 302 过去
          （服务端不代理字节：多一跳只会更慢，也把外网流量引进本机）。
        """
        self._session_or_raise(session_id)
        ident = str(attachment_id)
        if ident.lower().startswith(("http://", "https://")):
            return ident
        store = self._require_attachments()
        blob = store.read(session_id, ident)
        return blob.data, blob.media_type

    # ---- 对象存储（2026-10-20）--------------------------------------------

    def oss_credential_view(self) -> dict[str, Any]:
        """对象存储密钥状态：**只报有没有**，不回读任何一位密钥。"""
        store = self._oss_keys
        return {
            "configured": bool(store is not None and store.configured()),
            "store": None if store is None else store.store_kind,
            "note": None if store is None else store.store_note,
        }

    def save_oss_credentials(self, access_key: str, secret_key: str) -> dict[str, Any]:
        """保存 Access Key / Secret Key（与模型连接同一套凭据存储）。"""
        if self._oss_keys is None:
            raise app_errors.settings_store_unavailable(
                detail="设置库没有打开：对象存储密钥无法保存", occurred_at=_now()
            )
        try:
            self._sqlite_access(partial(self._oss_keys.set, access_key, secret_key))
        except CredentialStoreError as exc:
            raise app_errors.model_settings_invalid(detail=str(exc), occurred_at=_now()) from exc
        # 刚录入的密钥必须立刻进入脱敏集合：它可能出现在诊断输出里。
        self._refresh_secrets()
        return self.oss_credential_view()

    def clear_oss_credentials(self) -> dict[str, Any]:
        """删除已保存的对象存储密钥（幂等）。"""
        if self._oss_keys is not None:
            self._sqlite_access(self._oss_keys.clear)
        return self.oss_credential_view()

    def test_oss_upload(self) -> dict[str, Any]:
        """上传一张 1×1 测试图并 HEAD 它的公开地址，验证"能写、且公网能读"。

        不保存任何用户数据；失败时把**实际原因**说清（配置、密钥、桶策略分别对应不同改法）。
        """
        from uuid import uuid4

        from local_agent.attachment_oss import PROBE_PNG, OssAttachmentStore
        from local_agent.oss_storage import OssConfig, S3ObjectStore

        prefs = self._attachments_preferences()
        if str(getattr(prefs, "backend", "local")) != "oss":
            return {"ok": False, "detail": "当前存储后端是本地：先把「存储后端」切成对象存储再测试。"}
        keys = None if self._oss_keys is None else self._oss_keys.read()
        if not keys:
            return {"ok": False, "detail": "还没有保存 Access Key / Secret Key。"}
        config = OssConfig(
            endpoint=str(getattr(prefs, "oss_endpoint", "") or "").strip(),
            bucket=str(getattr(prefs, "oss_bucket", "") or "").strip(),
            region=str(getattr(prefs, "oss_region", "") or "auto").strip() or "auto",
            public_base_url=str(getattr(prefs, "oss_public_base_url", "") or "").strip(),
            key_prefix=str(getattr(prefs, "oss_key_prefix", "") or ""),
            path_style=bool(getattr(prefs, "oss_path_style", True)),
        )
        store = S3ObjectStore(config, *keys)
        try:
            saved = OssAttachmentStore(config=config, store=store).save(
                f"probe-{uuid4().hex[:12]}", PROBE_PNG
            )
            status = store.probe(saved.url or "")
        except (AttachmentError, OssStorageError) as exc:
            return {"ok": False, "detail": str(exc)}
        if status == 200:
            return {"ok": True, "detail": "上传成功，公开地址可以访问（HTTP 200）。"}
        return {
            "ok": False,
            "detail": (
                f"上传成功，但公开地址返回 HTTP {status}："
                "检查 public_base_url 是否正确，以及桶是否允许公开读。"
            ),
        }

    def _load_attachments(self, session: Session, env: _RunEnv, ids: Sequence[str]) -> _RunEnv:
        """把随消息提交的**附件引用**读成字节，挂到这次运行的环境上（2026-10-16）。

        三件事放在同一处，因为它们必须一起成立：

        ① **失败闭合**：这条连接没声明支持图片输入就拒绝**这次提交**——不能让模型收到一张它看不见的
           图（白花上下文，还会让它"以为自己看过了"，那比没有图更糟）；
        ② **引用只认本会话**：`AttachmentStore.read` 强校验形状与会话前缀，跨会话/越界/不存在的引用都拒；
        ③ **空列表绝不碰磁盘**：不带图是默认路径，它必须零开销、逐字节不变。
        """
        if not ids:
            return env
        if not env.app_config.llm.supports_vision:
            raise app_errors.model_settings_invalid(
                detail=(
                    "这条模型连接没有声明支持图片输入，所以这次没有把图片发出去："
                    "请在「设置 → 模型连接」里勾上「支持图片输入」，或去掉图片再发一次。"
                ),
                occurred_at=_now(),
            )
        store = self._require_attachments()
        loaded: list[LoadedAttachment] = []
        for attachment_id in ids:
            ident = str(attachment_id)
            if ident.lower().startswith(("http://", "https://")):
                # 对象存储后端（2026-10-20）：引用本身就是公开 URL。**不下载字节**——
                # State 里存 URL，出站请求直接把它交给模型。
                loaded.append(
                    LoadedAttachment(
                        attachment_id=ident,
                        media_type=_media_type_from_url(ident),
                        data=b"",
                        url=ident,
                    )
                )
                continue
            # 字节与**引用**一起带上（2026-10-16）：引用要单独累积进 State，页面靠它把缩略图
            # 挂回那条消息（内容块里只有 `data:` URL ✗，没有引用 ✗）。
            blob = store.read(session.session_id, ident)
            loaded.append(
                LoadedAttachment(
                    attachment_id=ident, media_type=blob.media_type, data=blob.data
                )
            )
        return replace(env, attachments=tuple(loaded))

    def submit_message(self, session_id: str, text: str, client_request_id: str, *,
                       attachments: Sequence[str] = (),
                       workflow: WorkflowRequest | Mapping[str, Any] | None = None,
                       parent_turn_id: str | None = None,
                       expected_branch_id: str | None = None,
                       expected_snapshot_id: str | None = None) -> CommandResultView:
        """提交一条用户消息。"""
        # 附件引用**必须进摘要**（2026-10-16）：否则"同一段文字、换一张图再发"会被判成同一次请求，
        # 幂等键把第二次提交直接当成重放，图就永远发不出去。
        digest = _digest(OPERATION_SUBMIT, session_id, text, *attachments)
        selected = (None if workflow is None else
                    TypeAdapter(WorkflowRequest).validate_python(workflow).model_dump(mode="json"))
        if selected is not None:
            digest = _digest(digest, json.dumps(selected, sort_keys=True, ensure_ascii=False))
        if any(value is not None for value in (parent_turn_id, expected_branch_id, expected_snapshot_id)):
            digest = _digest(digest, parent_turn_id or "", expected_branch_id or "", expected_snapshot_id or "")
        session = self._session_or_raise(session_id)
        # 提交边界解析的结果（连接固定 + 客户端就绪），在预检里算一次、在运行里原样复用。
        prepared: list[_RunEnv] = []
        parents: list[ConversationTurn | None] = []

        def prepare() -> None:
            """幂等判定之后、**落台账之前**的预检：解析连接、备好客户端与密钥。

            为什么不能等 `run()` 里再解析：`_guarded` 会先写一条"执行中"台账。若那时才发现没有可用连接
            或缺 Key，这条请求什么都没跑却留下了"执行中"记录——用户按提示配好连接后拿**同一个请求 id**
            重试，会被当成上一次进程留下的陈旧请求，判成 `RECOVERY_REQUIRED`（复审实证）。
            """
            nonlocal session
            session = self._session_or_raise(session_id)
            if (
                self._jobs is not None
                and self._jobs.background
                and self._jobs.active_for_session(session_id) is not None
            ):
                # 后台模式下"这个会话是不是在忙"由**任务索引**回答：任务可能还在 queued、没有任何
                # 线程持有执行锁，锁挡不住（§17.2）。这条只在 background=true 时生效，默认路径
                # 因此与 v0.5 逐字节一致（既不多一次查询，也不改错误码）。
                raise app_errors.session_busy(
                    session_id=session_id,
                    request_id=client_request_id,
                    occurred_at=_now(),
                )
            self._require_idle(session, client_request_id)
            if expected_snapshot_id is not None:
                current = self._read(session).checkpoint_id or f"{session.session_id}:no-checkpoint"
                if current != expected_snapshot_id:
                    raise conversation.conflict(session_id, "会话已经新增内容，本次提问尚未执行。")
            parents.append(conversation.submission_parent(
                self, session, parent_turn_id, expected_branch_id))
            # 附件在**预检**里读：连接不支持图片输入时，这里就把这次提交拦下（不留一条"执行中"台账）。
            prepared.append(
                self._load_attachments(
                    session, self._run_env(session, self._resolve_binding(session)), attachments
                )
            )
            # 预检失败不得清掉上一轮的取消意图。
            self._reset_cancellation(session_id)

        def run() -> CommandResultView:
            nonlocal session
            env = prepared[0]
            if session.title == DEFAULT_TITLE and self._read(session).checkpoint_id is None:
                session = self._sqlite_access(
                    partial(self._sessions.touch, session_id, title_hint=text)
                )
            conversation.start_path(self, session, parents[0])
            payload: dict[str, Any] = {
                "user_input": text,
                "thread_id": session.thread_id,
                "session_id": session.session_id,
                "session_mode": session.mode,
                "session_kind": session.kind,
                "runtime_preferences": env.preference_binding.model_dump(mode="json"),
                "workflow_request": selected,
                "workflow_limits": env.app_config.workflows.model_dump(mode="json") if selected else None,
            }
            if env.binding is not None:
                # 这次运行从此固定在这条连接的**这个版本**上：之后改设置都不会换掉它（§6.8）。
                payload["model_binding"] = env.binding
            return self._execute(
                session,
                request_id=client_request_id,
                operation=OPERATION_SUBMIT,
                payload=payload,
                resolve_decision=None,
                env=env,
            )

        return self._guarded(
            session,
            request_id=client_request_id,
            operation=OPERATION_SUBMIT,
            arguments_digest=digest,
            run=run,
            precheck=prepare,
        )

    def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        decision: str,
        client_request_id: str,
        *,
        edited_arguments: Mapping[str, Any] | None = None,
        reason: str = "",
        confirm_recursive: bool = False,
    ) -> CommandResultView:
        """对挂起的审批给出决定并恢复该运行。"""
        if decision not in APPROVAL_DECISIONS:
            msg = f"未知的审批决定：{decision}"
            raise ValueError(msg)
        digest = _digest(
            OPERATION_RESOLVE_APPROVAL,
            session_id,
            approval_id,
            decision,
            json.dumps(edited_arguments or {}, sort_keys=True, ensure_ascii=False),
            reason,
            str(confirm_recursive),
        )
        session = self._session_or_raise(session_id)
        resume = {
            "decision": decision,
            "confirm_recursive": confirm_recursive,
            "edited_arguments": dict(edited_arguments) if edited_arguments else None,
            "reason": reason,
        }

        prepared: list[_RunEnv] = []

        def prepare() -> None:
            """审批与密钥都先备好，再落台账（同 `submit_message`：预检失败不该留下"执行中"）。"""
            self._require_pending_approval(session, approval_id)
            # 恢复用**这一轮固定**的连接，而不是当前设置里的（§6.8 的停止条件）。密钥也按**那个版本**取：
            # 之后轮换过密钥也不影响这次恢复（凭据引用带版本）。
            prepared.append(
                self._run_env(session, self._binding_for_resume(session), for_resume=True)
            )

        def run() -> CommandResultView:
            return self._execute(
                session,
                request_id=client_request_id,
                operation=OPERATION_RESOLVE_APPROVAL,
                payload=Command(resume=resume),
                resolve_decision=(approval_id, decision),
                env=prepared[0],
            )

        return self._guarded(
            session,
            request_id=client_request_id,
            operation=OPERATION_RESOLVE_APPROVAL,
            arguments_digest=digest,
            run=run,
            precheck=prepare,
            # 审批恢复不新建 run：绑定的就是**正在恢复的那一轮**（挂起的 interrupt 属于它）。
            bind_run=lambda: self._current_run_id(session),
            # 审批恢复接上"等待用户"的那个任务，而不是新登记一个（§17.2）。
            resume_job=True,
        )

    # ---- 内部：执行前的校验 --------------------------------------------------

    def _require_idle(self, session: Session, request_id: str) -> None:
        """审批还没决定时不允许开新一轮。

        直接 `invoke` 新输入会把挂起的 interrupt 丢掉，让"等待授权的操作"无声消失。
        这种情况按"会话不空闲"处理（页面本来也会用审批卡接管输入区）。
        """
        if self._read(session).has_live_interrupt:
            raise app_errors.session_busy(
                session_id=session.session_id, request_id=request_id, occurred_at=_now()
            )
        if self._sqlite_access(lambda: self._ledger.has_executing(session.session_id)):
            raise app_errors.recovery_required(session_id=session.session_id, request_id=request_id,
                detail="请先核对并结束此前中断的请求，不会自动重发。", occurred_at=_now())

    def _require_pending_approval(self, session: Session, approval_id: str) -> None:
        """这个审批是不是还真的在等这一步决定？不是就别 resume——那会答非所问。"""
        view = self._read(session)
        current = proj.project_approval(
            view.values, has_live_interrupt=view.has_live_interrupt, secrets=self._secrets
        )
        if current is None or current.state != "pending" or current.approval_id != approval_id:
            raise app_errors.approval_not_actionable(
                session_id=session.session_id, approval_id=approval_id, occurred_at=_now()
            )

    # ---- 内部：互斥 + 幂等 ---------------------------------------------------

    def _guarded(
        self,
        session: Session,
        *,
        request_id: str,
        operation: str,
        arguments_digest: str,
        run: Callable[[], CommandResultView],
        precheck: Callable[[], None] | None = None,
        bind_run: Callable[[], str | None] | None = None,
        resume_job: bool = False,
    ) -> CommandResultView:
        """顺序固定：台账（冲突/重放）→ 抢锁（单飞）→ 陈旧台账恢复 → **执行前校验** → 落台账 → 跑图。

        `precheck` 必须排在落台账**之前**：校验没过就不该留下"执行中"的记录，否则下一次重试会
        被当成需要人工核对的陈旧请求。排在幂等判定**之后**则保证"同 id 重试拿原结果"优先于
        "现在还能不能执行"（例如审批已经决定完的重放）。

        `bind_run` 给出"这次请求绑定哪一轮 run"（能在执行前确定时用；提交消息的 run 由 Graph
        生成，登记时还不知道，由 `_execute` 在图交出 run 时补绑定）。

        `jobs.background=true` 时（§17.2）落台账后**不在这里跑图**：先登记任务索引，再把同一次
        执行交给 worker，并立刻返回"已接受"。执行锁**不在这里释放**——它跟着这次执行走，由
        worker 在跑完后释放，因此"同一会话第二个提交"仍然是 `SESSION_BUSY`。
        `resume_job` 区分"新任务"与"接上等待审批的那个任务"。
        """
        existing = self._sqlite_access(partial(self._ledger.get, request_id))
        if existing is not None and existing["arguments_digest"] != arguments_digest:
            raise app_errors.idempotency_conflict(
                session_id=session.session_id, request_id=request_id, occurred_at=_now()
            )
        if existing is not None and existing["status"] in TERMINAL_STATUSES:
            # 同 id 同参数的重试：返回原请求关联结果，**不再跑图**。
            return self._replay(session, request_id=request_id, operation=operation, entry=existing)

        lock = self._lock_for(session.session_id)
        acquired = lock.acquire(blocking=False)
        handed_off = False
        try:
            if not acquired:
                raise app_errors.session_busy(
                    session_id=session.session_id, request_id=request_id, occurred_at=_now()
                )
            # 会话可能在加载与取得执行锁之间被归档；取得锁后再次检查。
            self._session_or_raise(session.session_id)
            if existing is not None and existing["status"] == STATUS_EXECUTING:
                # 台账说在执行，但锁没被占用：上一次进程留下的。先只读核对，绝不自动重跑。
                return self._recover(session, entry=existing, operation=operation)
            if precheck is not None:
                precheck()
            self._sqlite_access(
                partial(
                    self._ledger.start,
                    request_id=request_id,
                    session_id=session.session_id,
                    operation=operation,
                    arguments_digest=arguments_digest,
                    run_id=bind_run() if bind_run is not None else None,
                )
            )
            if self._jobs is None:
                return run()
            jobs = self._jobs
            job = (
                jobs.resume(session_id=session.session_id)
                if resume_job
                else jobs.begin(session_id=session.session_id)
            )
            if not jobs.background:
                # 请求内执行：记账与状态迁移都在这条线程上完成，与 v0.5 的可见行为一致。
                return jobs.run_now(job, run)
            jobs.dispatch(job, run, on_done=lock.release)
            handed_off = True
            return self._accepted(session, request_id=request_id, operation=operation, job=job)
        finally:
            if acquired and not handed_off:
                lock.release()

    def _accepted(
        self, session: Session, *, request_id: str, operation: str, job: Job
    ) -> CommandResultView:
        """后台模式下的提交返回（§17.2）：命令已被接受、任务已进入运行。

        快照取自**当前** Checkpoint——它可能还是上一轮的终态（本轮刚排队），所以 `status`
        明确写 `running`，不拿旧快照冒充本轮结局；调用方凭 `job_id` 或重新拉快照收敛。
        """
        view = self._read(session)
        snapshot = self._snapshot(session, view)
        return self._result(
            request_id=request_id,
            operation=operation,
            session=session,
            snapshot=snapshot,
            status="running",
            replayed=False,
            job_id=job.job_id,
        )

    def _execute(
        self,
        session: Session,
        *,
        request_id: str,
        operation: str,
        payload: Any,
        resolve_decision: tuple[str, str] | None,
        env: _RunEnv,
    ) -> CommandResultView:
        """真正跑一次图：**边跑边投影**，收尾时落台账。

        广播不是等整轮结束才做：每完成一个节点就 flush 一次（`on_update`），否则接上 SSE 也只能
        在收尾时看到"刚刚发生了什么"，而看不到执行中的工具与计划（T28 只交付了收尾版本）。

        ``binding``/``for_resume`` 决定这次用哪条模型连接（§6.8）：新提交按解析出的连接构造客户端，
        恢复按这一轮固定的连接构造，两者都不看"当前设置"。
        """
        announced = _AnnouncedFacts(
            baseline_messages=_assistant_message_ids(self._read(session).values, self._secrets)
        )

        def flush() -> None:
            """把自上次 flush 以来新增的事实投影出来并广播（没有新事实就什么都不发）。

            这是**执行期**投影：终态不在这一批发（见 `_turn_events` 的 `include_terminal`）。
            """
            view = self._read(session)
            with self._locks_guard:
                sink = self._streams.get(session.session_id)
            if sink is not None and sink._run_id is not None and proj.current_run_id(view.values) != sink._run_id:
                return
            self._publish_new_events(
                session,
                snapshot=self._snapshot(session, view),
                view=view,
                resolve_decision=resolve_decision,
                announced=announced,
                include_terminal=False,
            )

        try:
            self._drive(session, request_id=request_id, payload=payload, on_update=flush, env=env)
        except Exception:
            # 异常可能发生在工具副作用**之后**（已经写了文件、还没记录结果就崩了）。这时终态
            # 是**不确定**的：写成 `failed` 等于声称"什么都没发生"，下一次同 id 重试会直接拿
            # 这条结论走人。所以保留 `executing` 并标记需要恢复——重试会先按 Checkpoint 核对
            # （能证明终态就补齐，证明不了就要求人工核对），绝不自动重提（§15.2）。
            # 异常本身照常抛出：调用方需要知道这次提交炸了，且它带着完整信息（台账不记原文）。
            self._sqlite_access(
                lambda: self._ledger.mark_needs_recovery(
                    request_id, error_code=app_errors.RECOVERY_REQUIRED
                )
            )
            raise

        view = self._read(session)
        snapshot = self._snapshot(session, view)
        run_id = proj.current_run_id(view.values) or None
        status = _status_from_snapshot(snapshot)

        self._sqlite_access(
            partial(
                self._ledger.finish,
                request_id,
                status=STATUS_FAILED if status == "failed" else STATUS_COMPLETED,
                run_id=run_id,
                result_ref=snapshot.snapshot_id,
            )
        )
        # 收尾 flush：最后一批事实 + 终态（`task_completed`/`task_failed`）；终态必须是最后一条。
        self._publish_new_events(
            session,
            snapshot=snapshot,
            view=view,
            resolve_decision=resolve_decision,
            announced=announced,
            include_terminal=True,
        )
        return self._result(
            request_id=request_id,
            operation=operation,
            session=session,
            snapshot=snapshot,
            status=status,
            replayed=False,
        )

    def _publish_new_events(
        self,
        session: Session,
        *,
        snapshot: SessionSnapshot,
        view: _ThreadView,
        resolve_decision: tuple[str, str] | None,
        announced: _AnnouncedFacts,
        include_terminal: bool,
    ) -> None:
        """投影"自上次以来新增的事实"并广播；没有新事实就什么都不发（不刷空事件）。"""
        events = self._turn_events(
            session,
            snapshot=snapshot,
            view=view,
            resolve_decision=resolve_decision,
            announced=announced,
            include_terminal=include_terminal,
        )
        if events:
            self._bus.publish(session.session_id, events)

    def _drive(
        self,
        session: Session,
        *,
        request_id: str,
        payload: Any,
        on_update: Callable[[], None] | None = None,
        env: _RunEnv,
    ) -> None:
        """跑一次图；图一交出本轮的 `run_id` 就立刻把它绑定到台账。

        绑定必须**早于任何副作用**（`init` 是第一个节点），否则进程中断后无法证明"这次请求的
        终态属于哪一轮"。所以这里用 `stream` 而不是 `invoke`：`invoke` 只在整轮结束后才返回，
        那时再绑定已经晚于工具执行。

        `on_update` 在每个节点完成后调用一次，用来把执行期的进度投递给已有订阅（§15.4）。
        """
        bound = False
        sink = _StreamingAnswerSink(
            session_id=session.session_id,
            bus=self._bus,
            secrets=self._secrets,
        )
        # Approval resume already has a checkpoint-validated run in the ledger.
        # It skips prepare_context, so its node updates need not repeat run_id;
        # bind before the resumed model can publish an answer or usage record.
        entry = self._sqlite_access(partial(self._ledger.get, request_id))
        resumed_run = entry["run_id"] if entry is not None else None
        if resumed_run is not None:
            # 初次决策的 Think 也有稳定消息 id；恢复不能从 1 重新编号，覆盖带工具调用的历史。
            prefix = f"{resumed_run}:final-message:"
            previous = proj.project_messages_view(self._read(session).values, secrets=self._secrets)
            indices = [int(message.message_id[len(prefix):]) for message in previous
                       if message.message_id.startswith(prefix) and message.message_id[len(prefix):].isdigit()]
            sink.bind_run(resumed_run, call_index=max(indices, default=0))
            if env.usage_recorder is not None:
                env.usage_recorder.bind_run(resumed_run)
            bound = True
        with self._live_stream(
            session.session_id, sink, output_limit=env.app_config.security.shell.max_output_chars,
        ), use_compression_listener(
            partial(self._compression_started, session)
        ):
            source = payload if isinstance(payload, Mapping) else self._read(session).values
            selection = source.get("workflow_request")
            limits = source.get("workflow_limits")
            stream_options: dict[str, Any] = {"subgraphs": True, "durability": "sync"} if selection else {}
            context = (self._context(session, env, workflow_request=selection, workflow_limits=limits)
                       if selection else self._context(session, env))
            for chunk in self._graph.stream(
                payload,
                config=self._conversation_config(session),
                context=context,
                stream_mode="updates",
                **stream_options,
            ):
                if selection:
                    namespace, chunk = chunk
                    if namespace:
                        if on_update is not None:
                            on_update()
                        continue
                if not bound:
                    run_id = _run_id_of(chunk)
                    if run_id:
                        # `partial` 而不是闭包：立刻绑定当前这轮的值，不捕获后变的循环变量。
                        self._sqlite_access(partial(self._ledger.bind_run, request_id, run_id=run_id))
                        sink.bind_run(run_id)
                        # 录音机也要绑定这一轮：绑定后再发生的模型调用才会落到本轮的 `run_id` 下。
                        # 第一个节点是 `prepare_context`（不调模型），所以这里绑定时还没有任何调用发生。
                        if env.usage_recorder is not None:
                            env.usage_recorder.bind_run(run_id)
                        bound = True
                if on_update is not None:
                    conversation.sync(self, session)
                    on_update()
        conversation.sync(self, session)

    @contextmanager
    def _live_stream(
        self, session_id: str, sink: _StreamingAnswerSink, *, output_limit: int = 30_000,
    ) -> Iterator[None]:
        tool_stream = ToolStream(session_id, partial(self._bus.publish, session_id), self._secrets,
                                 limit=output_limit)
        with self._locks_guard:
            self._streams[session_id] = sink
            self._tool_streams[session_id] = tool_stream
        try:
            with use_sink(sink), use_tool_listener(tool_stream.notify):
                yield
        finally:
            with self._locks_guard:
                self._streams.pop(session_id, None)
                self._tool_streams.pop(session_id, None)
                self._compressions.pop(session_id, None)

    def _compression_started(self, session: Session, progress: CompressionProgress) -> None:
        """正在压缩是实时展示，不提前宣告成功，也不修改历史检查点。"""
        view = self._read(session)
        if proj.current_run_id(view.values) != progress["run_id"]:
            return
        with self._locks_guard:
            self._compressions[session.session_id] = progress.copy()
        self._bus.publish(session.session_id, [self._snapshot_event(session, view)])

    def _sqlite_access(self, action: Callable[[], _T]) -> _T:
        """在 **Checkpointer 的锁**里访问共享 SQLite 连接上的业务数据。

        为什么要那把锁：LangGraph 用**线程池**写 Checkpoint（实测线程名 `ThreadPoolExecutor-*`），
        而台账与 Checkpoint 共用同一条 SQLite 连接。两个线程在这条连接上同时执行 DML/commit 会把
        彼此的事务状态踩掉（实测报 `sqlite3.OperationalError: cannot commit - no transaction is
        active`，而且随机出现在任意用例里）。绑定要发生在副作用之前，只能在执行期间写，因此必须
        与 Checkpointer 串行。

        读也必须走这里：同一连接跨线程读写会触发 `sqlite3.InterfaceError`。因此台账
        `get`、会话索引的查询/创建/切换都经这把锁，不能只锁执行期间的 `bind_run`。
        Checkpointer 没有暴露锁时（例如测试用的桩图）直接执行：桩图不会并发写。
        """
        lock = getattr(getattr(self._graph, "checkpointer", None), "lock", None)
        with (lock if lock is not None else nullcontext()):
            return action()

    def _recover(self, session: Session, *, entry: LedgerEntry, operation: str) -> CommandResultView:
        """台账仍为"执行中"：**只读**核对 Checkpoint，能证明终态就补齐，否则要求人工核对。

        证明的前提是**这次请求与某一轮 run 的对应关系可证**：台账绑定的 `run_id` 必须与
        Checkpoint 当前的 `run_id` 相同。任何一侧是空（例如进程在 `init` 之前就中断，来不及
        绑定）都算证明不了——`NULL` 是"不知道"，不是通配符：把"不知道"当成匹配，会让**上一轮**
        的终态被当成本次请求的结果（那正是"重试拿到别人结果"的来源）。
        """
        view = self._read(session)
        snapshot = self._snapshot(session, view)
        reported_run = entry["run_id"]
        state_run = proj.current_run_id(view.values) or None
        run_matches = reported_run is not None and reported_run == state_run
        terminal = (
            str(view.values.get("task_status") or "") in TASK_TERMINAL_STATUSES
            and not view.has_live_interrupt
            and run_matches
        )
        if not terminal:
            self._sqlite_access(
                partial(self._ledger.mark_needs_recovery, entry["request_id"], error_code=app_errors.RECOVERY_REQUIRED)
            )
            if reported_run is None or state_run is None:
                detail = "台账里没有绑定到某一轮 run，无法证明这个请求对应哪一轮终态"
            elif not run_matches:
                detail = "该请求绑定的 run 与 Checkpoint 中当前的 run 不是同一轮"
            else:
                detail = "Checkpoint 里既没有终态、也没有可继续的挂起"
            raise app_errors.recovery_required(
                session_id=session.session_id,
                request_id=entry["request_id"],
                detail=detail,
                occurred_at=_now(),
            )

        # 能证明终态：补齐结果引用，按"重放"返回，不重跑。
        self._sqlite_access(
            partial(
                self._ledger.finish,
                entry["request_id"],
                status=STATUS_FAILED if _status_from_snapshot(snapshot) == "failed" else STATUS_COMPLETED,
                run_id=state_run,
                result_ref=snapshot.snapshot_id,
            )
        )
        # 同一条证明也把**任务索引**收口（§17.5）：否则崩溃后哪怕用户重试成功，索引里那条任务
        # 仍是非终态，这个会话的下一次新提交会被"已有非终态任务"挡住，只能等下一次重启。
        self._adopt_interrupted_job(session, reported_run=entry["run_id"])
        return self._result(
            request_id=entry["request_id"],
            operation=operation,
            session=session,
            snapshot=snapshot,
            status=_status_from_snapshot(snapshot),
            replayed=True,
        )

    def _replay(
        self, session: Session, *, request_id: str, operation: str, entry: LedgerEntry
    ) -> CommandResultView:
        """同 id 同参数的重试：返回**原请求关联的**结果，不执行任何副作用。

        读的是台账的 `result_ref`（当时那一份 Checkpoint），不是"当前快照"：会话后来又跑完几轮
        之后，当前快照属于**新的一轮**，拿它当旧请求的结果会返回别人的 run、状态和内容。
        """
        ref = entry["result_ref"] or ""
        view = self._read_checkpoint(session, ref) if ref else None
        if view is None or not _ref_matches_run(view, entry["run_id"]):
            # 终态但拿不回当时的结果：宁可按"需要人工核对"拒绝，也不拿当前快照冒充原结果。
            # 台账状态仍是终态，所以重试不会重新执行——这里只是拒绝给出一个错误的答案。
            self._sqlite_access(
                partial(self._ledger.mark_needs_recovery, request_id, error_code=app_errors.RECOVERY_REQUIRED)
            )
            raise app_errors.recovery_required(
                session_id=session.session_id,
                request_id=request_id,
                detail="台账里的结果引用已无法还原（Checkpoint 不在了，或与绑定的轮次对不上）",
                occurred_at=_now(),
            )
        snapshot = self._snapshot_at(
            session, view, snapshot_id=ref, created_at=_as_dt(entry["updated_at"])
        )
        status = _status_from_snapshot(snapshot)
        if entry["status"] == STATUS_FAILED and status == "completed":
            # 记过失败的请求不能重放成成功。
            status = "failed"
        job_id = None
        if self._jobs is not None and entry["run_id"]:
            # 台账不存 job_id，按"会话 + 轮次"反查索引，让重放给出与原请求一致的任务标识。
            job_id = self._jobs.job_id_for_run(session.session_id, entry["run_id"])
        # 这条请求**已有终态**（否则走不到重放），按这同一份证据把该会话遗留的非终态任务收口（§17.5）：
        # 崩溃后用户拿同一 id 重试就能自愈，不必等下一次重启。
        self._adopt_interrupted_job(session, reported_run=entry["run_id"])
        return self._result(
            request_id=request_id,
            operation=operation,
            session=session,
            snapshot=snapshot,
            status=status,
            replayed=True,
            job_id=job_id,
        )

    # ---- 内部：读取与投影 ----------------------------------------------------

    def _read(self, session: Session) -> _ThreadView:
        with self._locks_guard:
            sink = self._streams.get(session.session_id)
        if sink is not None and sink._run_id is not None:
            latest = self._checkpoint_view({"configurable": {"thread_id": session.thread_id}})
            if proj.current_run_id(latest.values) == sink._run_id:
                return latest
        branch = self._sqlite_access(lambda: self._conversations.active(session.session_id))
        if branch is not None and branch.checkpoint_id is not None:
            view = self._read_checkpoint(session, branch.checkpoint_id)
            if view is None:
                raise conversation.conflict(session.session_id, "当前分支检查点无法恢复。")
            return view
        return self._checkpoint_view({"configurable": {"thread_id": session.thread_id}})

    def _checkpoint_view(self, config: RunnableConfig) -> _ThreadView:
        snapshot = self._graph.get_state(config)
        if (getattr(snapshot, "values", None) or {}).get("workflow_request"):
            snapshot = self._graph.get_state(config, subgraphs=True)
        return self._view_of(snapshot)

    def _thread_config(self, session: Session) -> RunnableConfig:
        """只按线程取**最新**检查点的配置（不带分支游标）。"""
        return {"configurable": {"thread_id": session.thread_id}}

    def _conversation_config(self, session: Session) -> RunnableConfig:
        branch = self._sqlite_access(lambda: self._conversations.active(session.session_id))
        config: RunnableConfig = {"configurable": {"thread_id": session.thread_id}}
        if branch is not None and branch.checkpoint_id is not None:
            config["configurable"]["checkpoint_id"] = branch.checkpoint_id
        return config

    def _closing_anchor(self, session: Session, view: _ThreadView) -> tuple[_ThreadView, RunnableConfig]:
        """核对收口要写在**哪一份状态**上：(状态, 写入配置)（v0.9 T81 实证，§20.5）。

        为什么不能直接用活动分支游标：游标由 `conversation.sync` 推进，而它在崩溃路径上
        可能还没推进（或推进发生在"恢复之后"的检查点落盘之前），于是游标**落后于线程的
        最新检查点**。实证（2026-10-12，T81）：崩溃恰好发生在"人工决定已落盘、游标仍指
        恢复之前那一份"时，`close_interrupted_turn` 会写在过期祖先上——那份状态里
        `approval_history` 还是空的，收口又清掉 `pending_approval`，于是**唯一的审批来源
        没了**：用户看不到"未完整记录"的核对卡（会话只剩一条模糊的"本轮执行已中断"）。

        这里的判据只放宽到"同一个 run"：执行中/待核对期间会话**不能切换分支**
        （`select_branch` 会拒绝），所以同一 run 内线程最新状态一定属于本轮；不同 run
        一律退回分支游标，绝不用别的轮次的状态收口（`end_interrupted_request` 随后
        还有一道 `run_id` 一致性检查兜底）。
        """
        latest = self._checkpoint_view(self._thread_config(session))
        state_run = proj.current_run_id(latest.values)
        if (
            latest.checkpoint_id != view.checkpoint_id
            and state_run
            and state_run == proj.current_run_id(view.values)
        ):
            return latest, self._thread_config(session)
        return view, self._conversation_config(session)

    def get_conversation_map(self, session_id: str, query: str = "") -> ConversationMapView:
        return conversation.map_view(self, self._session_or_raise(session_id), query)

    def get_turn_snapshot(self, session_id: str, turn_id: str, *,
                          include_ancestors: bool = False) -> SessionSnapshot:
        return conversation.turn_snapshot(self, self._session_or_raise(session_id), turn_id,
                                           include_ancestors=include_ancestors)

    def select_branch(self, session_id: str, branch_id: str) -> SessionSnapshot:
        with self._catalog_session_guard(session_id):
            if self._sqlite_access(lambda: self._ledger.has_executing(session_id)):
                raise conversation.conflict(session_id, "还有执行结果未确认的请求，不能切换分支。")
            session = self._session_or_raise(session_id)
            conversation.initialize(self, session)
            branches = self._sqlite_access(lambda: self._conversations.branches(session_id))
            target = next((branch for branch in branches if branch.branch_id == branch_id), None)
            if target is None:
                raise conversation.conflict(session_id, "找不到该分支。")
            if target.checkpoint_id is not None and self._read_checkpoint(session, target.checkpoint_id) is None:
                raise conversation.conflict(session_id, "目标分支的检查点无法恢复，当前路径没有改变。")
            self._sqlite_access(lambda: self._conversations.select(session_id, branch_id))
            view = self._read(session)
            snapshot = self._snapshot(session, view)
            self._bus.publish(session_id, [self._snapshot_event(session, view)])
            return snapshot

    def arrange_branch(self, session_id: str, branch_id: str, title: str,
                       position: int) -> ConversationMapView:
        with self._catalog_session_guard(session_id):
            session = self._session_or_raise(session_id)
            conversation.initialize(self, session)
            branches = self._sqlite_access(lambda: self._conversations.branches(session_id))
            if not any(branch.branch_id == branch_id for branch in branches):
                raise conversation.conflict(session_id, "找不到该分支。")
            clean = " ".join(title.split())
            if not clean or len(clean) > 80 or position < 0:
                raise conversation.conflict(session_id, "分支名称需为 1 至 80 个字符，位置不能为负数。")
            self._sqlite_access(lambda: self._conversations.arrange(session_id, branch_id, clean, position))
            return conversation.map_view(self, session)

    def _current_run_id(self, session: Session) -> str | None:
        """当前 Checkpoint 里的轮次（审批恢复要绑定"正在恢复的那一轮"）。"""
        return proj.current_run_id(self._read(session).values) or None

    def _read_checkpoint(self, session: Session, checkpoint_id: str) -> _ThreadView | None:
        """读**指定的**历史 Checkpoint（重放原请求的结果引用）；读不到或拿错就返回 `None`。

        拿错也当读不到：如果底层对未知的 `checkpoint_id` 退回"最新一份"，直接接受它就会让旧
        请求返回新会话的结果——那正是要避免的错配。
        """
        config: RunnableConfig = {
            "configurable": {"thread_id": session.thread_id, "checkpoint_id": checkpoint_id}
        }
        try:
            view = self._checkpoint_view(config)
        except Exception:  # noqa: BLE001 — 读不到就按"拿不回这份结果"处理（fail-closed，宁可拒绝）
            return None
        if not view.values:
            return None
        if view.checkpoint_id is not None and view.checkpoint_id != checkpoint_id:
            return None
        return view

    @staticmethod
    def _view_of(snapshot: Any) -> _ThreadView:
        """把 LangGraph 的 StateSnapshot 收敛成只读视图（实时与历史走同一条路径）。"""
        values = getattr(snapshot, "values", None) or {}
        tasks = getattr(snapshot, "tasks", ()) or ()
        selection = values.get("workflow_request")
        if isinstance(selection, Mapping):
            # Only the registered same-run workflow can contribute display facts.
            # Keep parent terminal status, messages and result references authoritative.
            for task in tasks:
                child_values = getattr(getattr(task, "state", None), "values", None) or {}
                if not isinstance(child_values, Mapping):
                    continue
                workflow = child_values.get("workflow")
                if (isinstance(workflow, Mapping) and workflow.get("run_id") == values.get("run_id")
                        and workflow.get("name") == selection.get("name")):
                    values = {**values, "workflow": workflow}
        has_interrupt = any(getattr(task, "interrupts", ()) for task in tasks)
        confirmation_stage = 1
        for task in tasks:
            for interruption in getattr(task, "interrupts", ()):
                value = getattr(interruption, "value", None)
                if isinstance(value, Mapping) and value.get("confirmation_stage") == 2:
                    confirmation_stage = 2
        config = getattr(snapshot, "config", None) or {}
        checkpoint_id = (config.get("configurable") or {}).get("checkpoint_id")
        return _ThreadView(
            values=values,
            has_live_interrupt=bool(has_interrupt),
            checkpoint_id=None if checkpoint_id is None else str(checkpoint_id),
            confirmation_stage=confirmation_stage,
            next_nodes=tuple(getattr(snapshot, "next", ()) or ()),
        )

    def _context(self, session: Session, env: _RunEnv, *,
                 workflow_request: Any = None, workflow_limits: Any = None) -> Any:
        """构造这次调用的运行上下文（客户端与运行参数都在预检时定好，这里只装配）。"""
        selected = (None if workflow_request is None else
                    TypeAdapter(WorkflowRequest).validate_python(workflow_request))
        config = env.app_config
        if selected is not None and workflow_limits is not None:
            limits = WorkflowsConfig.model_validate(workflow_limits)
            limits.enabled = limits.enabled and config.workflows.enabled
            config = config.model_copy(update={"workflows": limits})
        # 能力开关**每一轮现取**（§16.1）：用户刚在能力面板里改完，下一次运行就该按新开关绑定工具与
        # 能力。少了这一步，"页面里能开关"就只是改显示——真正决定绑哪些工具的是
        # `build_runtime_context` 里的 `agent_switches`（§16.1）。
        agent_switches = self.capability_switches()
        choices: dict[str, dict[str, Any]] = (
            {} if selected is None else {selected.name: selected.request.model_dump(mode="json")})
        return builder.build_runtime_context(
            app_config=config,
            model_adapter=env.model,
            workspace_root=Path(session.workspace_root),
            session_scope=f"{self._tool_namespace}:{session.session_id}",
            provider_name=env.provider_name,
            model_capabilities=env.capabilities,
            # 档位与授权**每一轮现取**（"保存即生效、作用于下一轮"，T29A 口径）：用户刚在
            # 「设置 → 权限」里改完，下一次运行就该按新档位判定。
            external_policy=self.external_paths_policy(),
            # 能力开关同样每一轮现取（§16.1）：决定"这次绑哪些工具给模型"的就是它。
            agent_switches=agent_switches,
            # 用户禁用的技能同样每一轮现取（技能管理页，2026-10-16）：清单与技能工具都照它过滤。
            disabled_skills=self._disabled_skills(),
            # 联网检索的服务凭据（Tavily）也在装配这一轮时现取：刚在设置页录入的密钥，下一轮
            # 检索就该用它。**明文只走这条内存通道**，不进快照、DTO 与日志（§6.8 第 2 条）。
            web_search_api_key=self.web_search_api_key(),
            # 录音机挂在上下文上：节点的用量装饰器从 `RuntimeContext` 取它（§15.6）。
            usage_recorder=env.usage_recorder,
            # 取消令牌进两道门（§17.4）：安全 / 审批门与能力可见性门都读它。
            # **审批恢复复用同一个令牌**——已取消的任务不会因为后来点了批准又跑起来。
            cancellation=self._token_for(session.session_id),
            child_index=(None if self._jobs is None else
                         partial(self._jobs.index_children, session.session_id)),
            readonly_research=choices.get("readonly_research"),
            code_change=choices.get("code_change"),
            delegated_research=choices.get("delegated_research"),
            # 随消息提交的图片（2026-10-16）：入口节点把它们写成消息里的**引用**。
            # 从 `env` 取而不是多一个参数：附件本来就是这个**运行环境**的一部分（`_load_attachments`
            # 在预检里读好挂上去的），调用点因此一处都不用改。
            user_attachments=env.attachments,
            # 附件存储（2026-10-20）：出站前把引用物化成 data URL 时按会话读回字节。
            # 不传也没有附件：那时上传附件的引用读不到，会退化成占位文本。
            attachments=self._attachments,
            # 会话 id 必须单独给：`session_scope` 带命名空间前缀，过不了附件引用校验。
            attachment_session=session.session_id,
            # 图片上下文轮数每轮现取（保存即生效、作用于下一轮，与能力开关同一口径）。
            image_context_turns=self._image_context_turns(),
        )

    def external_paths_policy(self) -> ExternalPathPolicy | None:
        """当前生效的外部路径档位与授权；没有设置库时返回 ``None``（下层取配置默认）。"""
        if self._permissions is None:
            return None
        return self._sqlite_access(self._permissions.policy)

    def _run_env(
        self, session: Session, binding: ModelBinding | None, *, for_resume: bool = False
    ) -> _RunEnv:
        """准备这次运行要用的配置与客户端。

        - **连接模式**（有绑定）：按那条连接的**固定版本**构造客户端，运行参数（窗口、输出上限、
          温度、超时、重试）换成连接的值；`app_config.llm` 也随之替换，这样压缩触发比、上下文
          计量器都按**这次真正在用的模型窗口**算，而不是进程启动时那一段。
        - **旧配置模式**（没有设置库）：沿用注入的适配器与 ``app_config.llm``——§6.8 的降级路径，
          页面会看到"设置不可用"的说明。
        """
        if for_resume:
            # 在 resume 补建任务索引之前恢复令牌，否则新行会遮住旧取消记录。
            token = self._token_for(session.session_id)
            run_id = self._current_run_id(session)
            if self._jobs is not None and run_id is not None:
                history = self._jobs.list_for_session(session.session_id)
                for job in reversed(history):
                    if job.state == JOB_STATE_CANCELLED and job.run_id == run_id:
                        # 上一次恢复已补建索引后崩溃，最新行也可能不再是 cancelled。
                        token.cancel(job.detail or DEFAULT_CANCEL_REASON)
                        break
                if (
                    not any(job.run_id == run_id for job in history)
                    and any(job.state == JOB_STATE_CANCELLED and job.run_id is None for job in history)
                ):
                    # 升级前的空绑定取消记录无法证明所属轮次；恢复登记不得使它失效。
                    # 正常新轮次已有自己的任务绑定，不会被旧记录粘住。
                    token.cancel("旧取消记录无法关联当前轮次，已阻止审批执行；核对后请重新提交")
            saved = self._read(session).values.get("runtime_preferences")
            preference_binding = (
                RuntimePreferenceBinding.model_validate(saved) if saved is not None else
                RuntimePreferenceBinding(version=0, values=RuntimePreferences.model_validate(
                    initial_preferences(self._app_config).model_dump(exclude={"general"})
                ))
            )
        elif self._preferences is not None:
            preference_binding = self._sqlite_access(partial(self._preferences.binding, self._app_config))
        else:
            preference_binding = RuntimePreferenceBinding(version=0, values=RuntimePreferences.model_validate(
                initial_preferences(self._app_config).model_dump(exclude={"general"})
            ))
        app_config = apply_runtime_preferences(self._app_config, preference_binding.values)
        # 联网/ Git / 技能三道 config 总闸回灌自**能力开关**（2026-10-14 修订，见 `apply_capability_gates`）：
        # 这两处原本各有一把开关，不一致时会出现"工具对模型可见、执行时却被拒"（实证：面板 web=true，
        # 工具报 web_access.enabled=false）。两处分支（旧配置与设置库连接）共用这一行。
        app_config = apply_capability_gates(app_config, self.capability_switches())
        # 用户添加的技能发现根（2026-10-20）：与能力开关同一处回灌，保证模型清单、`skill_load`、
        # 设置页三处看到的是同一组根。
        extra_roots = self._skill_extra_roots()
        if extra_roots:
            merged_roots = list(app_config.skills.user_dirs)
            merged_roots.extend(root for root in extra_roots if root not in merged_roots)
            app_config = app_config.model_copy(update={
                "skills": app_config.skills.model_copy(update={"user_dirs": merged_roots}),
            })
        if binding is None:
            if self._app_config.llm is None:
                # 既没有设置库、也没有 llm 段：这台机器上确实没有任何可用的模型来源。
                raise app_errors.model_connection_required(
                    session_id=session.session_id, occurred_at=_now()
                )
            run_config = app_config.as_run_config()
            return _RunEnv(
                app_config=run_config,
                model=self._model_adapter,
                provider_name=run_config.llm.model,
                capabilities=None,
                binding=None,
                preference_binding=preference_binding,
                # 旧配置没有连接 id / 版本：如实标成 `legacy-config` / `unversioned`，
                # 端点指纹按 base_url + 模型名现算（§15.6 只要求"缓存基线可比"）。
                usage_recorder=ModelCallRecorder(
                    connection_id=LEGACY_CONNECTION_ID,
                    config_version=LEGACY_CONFIG_VERSION,
                    provider=run_config.llm.provider,
                    model_id=run_config.llm.model,
                    endpoint_fingerprint=endpoint_fingerprint(
                        run_config.llm.base_url, run_config.llm.model
                    ),
                    context_window_tokens=run_config.llm.context_window_tokens,
                ),
            )
        connection = self._connection_for_binding(binding)
        llm = connection.to_llm_config()
        return _RunEnv(
            app_config=app_config.model_copy(update={"llm": llm}).as_run_config(),
            model=self._build_model(llm, self._api_key_for(connection)),
            provider_name=llm.model,
            # 恢复中的运行不重新按能力门禁：它在提交时已经被放行过，拿一份可能是"新版本"的检测结论
            # 去改变一个正在等待审批的运行，等于中途换判据（§6.8 的停止条件）。
            capabilities=None if for_resume else _capabilities_from(connection.capability),
            binding=binding,
            preference_binding=preference_binding,
            # 用量记录用**这一轮固定的连接版本**（不是"当前设置"）：等待审批的运行恢复后，
            # 记录里的连接/窗口必须还是当初那一份。
            usage_recorder=ModelCallRecorder(
                connection_id=connection.connection_id,
                config_version=str(connection.config_version),
                provider=llm.provider,
                model_id=llm.model,
                endpoint_fingerprint=connection.endpoint_fingerprint,
                context_window_tokens=llm.context_window_tokens,
            ),
        )

    def _connection_for_binding(self, binding: ModelBinding) -> ModelConnection:
        """按**固定版本**取连接；版本行不在就报"找不到连接"，绝不拿当前版本顶上。"""
        store = self._require_store()
        connection_id = str(binding.get("connection_id") or "")
        version = int(binding.get("config_version") or 0)
        found = self._sqlite_access(partial(store.version, connection_id, version)) if version else None
        if found is None:
            raise app_errors.model_connection_not_found(
                connection_id=connection_id, occurred_at=_now()
            )
        return found

    def _api_key_for(self, connection: ModelConnection) -> str:
        """这次运行用的密钥：无鉴权端点给占位值，其余从凭据存储 / 旧环境变量取。

        Raises:
            ApplicationError: 需要密钥却取不到（记 `MODEL_AUTH_FAILED`，提示是"更新密钥"）。
        """
        if not connection.requires_key:
            return PLACEHOLDER_API_KEY
        secret = self._sqlite_access(partial(self._require_store().resolve_api_key, connection))
        if not secret:
            raise app_errors.model_key_missing(
                connection_id=connection.connection_id, occurred_at=_now()
            )
        return secret

    def _resolve_binding(self, session: Session) -> ModelBinding | None:
        """提交边界解析"这次用哪条连接"：会话选择 → 默认连接 → 未配置（§6.8）。

        连接模式取的是**当前版本**（编辑后的参数对下一轮生效）；已经在跑的那一轮早已把版本固定
        在自己的 `model_binding` 里，不会被这里改到。
        """
        store = self._connections
        if store is None:
            return None  # 没有设置库：走旧配置（降级路径）
        selection = self._sqlite_access(
            partial(_selected_or_default, store, session.session_id)
        )
        if selection is None or selection.connection_id is None:
            raise app_errors.model_connection_required(
                session_id=session.session_id, occurred_at=_now()
            )
        connection = self._sqlite_access(partial(store.get, selection.connection_id))
        if connection is None or not connection.enabled:
            raise app_errors.model_connection_not_found(
                connection_id=selection.connection_id, occurred_at=_now()
            )
        return binding_of(connection)

    def _binding_for_resume(self, session: Session) -> ModelBinding | None:
        """审批恢复用**这一轮固定**的连接（§6.8：切换不影响等待审批的运行）。"""
        binding = binding_from_state(self._read(session).values)
        if binding is not None:
            return binding
        if self._connections is None:
            return None  # 旧配置模式本来就没有绑定
        # 设置库是权威，但这一轮没有记录所用连接：宁可要求人工核对，也不拿当前设置去接它。
        raise app_errors.recovery_required(
            session_id=session.session_id,
            request_id="model-binding",
            detail="这一轮 Checkpoint 里没有记录所用的模型连接",
            occurred_at=_now(),
        )

    def _ref_for(self, workspace: Path) -> WorkspaceRef:
        return WorkspaceRef(
            workspace_id=scope_key_for(SCOPE_WORKSPACE, workspace_root=workspace),
            display_name=self._sqlite_access(partial(self._sessions.workspace_title, workspace))
            or workspace.name,
        )

    def _workspace_ref(self, session: Session) -> WorkspaceRef:
        return self._ref_for(Path(session.workspace_root))

    def _task_view(self, job: Job, session: Session, workspace: WorkspaceRef) -> TaskView:
        """把索引里的一行任务 + 它的会话投影成任务中心的一行（§17.6）。

        时间戳解析失败（旧行 / 手改过的库）时**如实留空**，不拿当前时间顶替——那会让页面显示一个
        假的"已运行时长"。**不读 Checkpoint**：这里没有任何一条读取会话状态的分支。
        """
        return TaskView(
            task_id=job.job_id,
            session_id=session.session_id,
            session_title=session.title,
            workspace=workspace,
            state=job.state,
            detail=job.detail,
            created_at=_as_dt(job.created_at),
            started_at=_as_dt(job.started_at) if job.started_at else None,
            finished_at=_as_dt(job.finished_at) if job.finished_at else None,
        )

    def _snapshot(self, session: Session, view: _ThreadView) -> SessionSnapshot:
        # 运行期间可修改显示名称；长期 SSE 订阅捕获的旧 Session 不应覆盖新名称。
        session = self._sqlite_access(partial(self._sessions.get, session.session_id)) or session
        snapshot = self._snapshot_at(
            session,
            view,
            snapshot_id=view.checkpoint_id or f"{session.session_id}:no-checkpoint",
            created_at=_now(),
        )
        lock = self._lock_for(session.session_id)
        if lock.acquire(blocking=False):
            try:
                entries = self._sqlite_access(lambda: self._ledger.executing(session.session_id))
                if entries:
                    snapshot = snapshot.model_copy(update={
                        "session_state": "blocked", "interrupted_request_id": entries[-1]["request_id"],
                        "approval": None,
                    })
            finally:
                lock.release()
        with self._locks_guard:
            sink = self._streams.get(session.session_id)
            compression = self._compressions.get(session.session_id)
            tool_stream = self._tool_streams.get(session.session_id)
        if tool_stream is not None:
            snapshot = tool_stream.merge(snapshot)
        recorded = snapshot.context.compression
        if (compression is not None and snapshot.current_task is not None
                and snapshot.session_state == "running"
                and compression["run_id"] == snapshot.current_task.run_id
                and (recorded is None or recorded.operation_id != compression["operation_id"])):
            snapshot = snapshot.model_copy(update={"context": snapshot.context.model_copy(update={
                "compression": CompressionView(operation_id=compression["operation_id"],
                    run_id=compression["run_id"], state=compression["state"]),
                "used_tokens": compression["used_tokens"], "window_tokens": compression["window_tokens"],
                "completion_reserve_tokens": compression["completion_reserve_tokens"], "scope": "current",
            })})
        if sink is not None and snapshot.current_task is not None and snapshot.session_state == "running":
            tail = sink.partial_view(snapshot.current_task.run_id)
            if tail is not None and not any(message.message_id == tail.message_id for message in snapshot.messages):
                return snapshot.model_copy(update={"messages": [*snapshot.messages, tail]})
        return snapshot

    def _snapshot_at(
        self, session: Session, view: _ThreadView, *, snapshot_id: str, created_at: datetime
    ) -> SessionSnapshot:
        """按给定时点与标识投影一份快照（实时快照用当前时间，重放历史用记录里的时间）。"""
        snapshot = proj.project_snapshot(
            view.values,
            session_id=session.session_id,
            workspace=self._workspace_ref(session),
            title=session.title,
            snapshot_id=snapshot_id,
            created_at=created_at,
            has_live_interrupt=view.has_live_interrupt,
            secrets=self._secrets,
            model_selection=self._session_model_selection(session.session_id),
            answer_streaming=self._session_answer_streaming(session),
            workspace_root=Path(session.workspace_root),
            confirmation_stage=view.confirmation_stage,
        )
        branch = self._sqlite_access(lambda: self._conversations.active(session.session_id))
        changes: dict[str, Any] = {"mode": "chat" if session.mode == "chat" else "agent"}
        if snapshot.current_task is not None and snapshot.session_state in {"running", "awaiting_approval"}:
            changes["current_answer_streaming"] = self._current_answer_streaming(view.values)
        if branch is not None:
            changes.update(branch_id=branch.branch_id, branch_title=branch.title)
        return snapshot.model_copy(update=changes)

    def _session_model_selection(self, session_id: str) -> ModelSelectionView | None:
        """会话**自己的**下一轮模型选择（`None` = 跟随默认）。

        快照带上它是为了让页面把"我看到的选择"原样回传给写入做校验（§6.1）。
        读不到就退化成 `None`：展示一个会话不该因为设置库出问题而整份失败——
        代价是那种情况下页面会被当成"没有自己的选择"，写入校验会拒绝并要求刷新，不会静默写错。
        """
        store = self._connections
        if store is None:
            return None
        try:
            selection = self._sqlite_access(partial(store.session_selection, session_id))
        except ModelSettingsError:
            return None
        if selection is None:
            return None
        return ModelSelectionView(
            connection_id=selection.connection_id,
            config_version=str(selection.config_version),
        )

    def _current_answer_streaming(self, values: Mapping[str, Any]) -> bool | None:
        """只读当前轮固定历史版本，不读取凭据，也不使用下一轮选择。"""
        if values.get("model_binding") is None:
            return self._app_config.llm.streaming if self._app_config.llm is not None else None
        store = self._connections
        if store is None:
            return None
        try:
            binding = binding_from_state(values)
            if binding is None:
                return None
            connection = self._sqlite_access(
                partial(store.version, binding["connection_id"], binding["config_version"])
            )
            return None if connection is None else bool(connection.streaming)
        except (ModelSettingsError, sqlite3.Error, TypeError, ValueError):
            return None

    def _session_answer_streaming(self, session: Session) -> bool:
        """本会话**下一轮**是否会按增量流式展示最终回答（§19.4）。

        与提交时**真正生效**的那份配置同源：连接模式按"会话选择 → 默认连接"解析（和
        :meth:`_resolve_binding` 同一条口径），旧配置模式取进程级 ``llm.streaming``。

        两条边界：① **不抛异常**——展示字段不该让整份快照失败（同 :meth:`_session_model_selection`：
        设置库坏了就退回进程级配置，页面宁可少一个提示也不要整页打不开）；② **不读 Checkpoint**，
        它只回答"如果现在发一条消息，会不会流式"，与已跑过的轮次无关。
        """
        llm_streaming = self._app_config.llm.streaming if self._app_config.llm is not None else False
        store = self._connections
        if store is None:
            return bool(llm_streaming)
        try:
            selection = self._sqlite_access(partial(_selected_or_default, store, session.session_id))
            connection = (
                None
                if selection is None or selection.connection_id is None
                else self._sqlite_access(partial(store.get, selection.connection_id))
            )
        except ModelSettingsError:
            return bool(llm_streaming)
        if connection is None or not connection.enabled:
            return bool(llm_streaming)
        return bool(connection.to_llm_config().streaming)

    def _snapshot_event(self, session: Session, view: _ThreadView) -> EventEnvelope:
        snapshot = self._snapshot(session, view)
        task = snapshot.current_task
        identity = snapshot.snapshot_id
        if snapshot.workflow is not None:
            identity = _digest(identity, snapshot.workflow.model_dump_json())
        if snapshot.context.compression is not None:
            identity = _digest(identity, snapshot.context.compression.model_dump_json())
        return SessionSnapshotEvent(
            event_id=event_id_for("session_snapshot", identity),
            session_id=session.session_id,
            task_id=None if task is None else task.task_id,
            run_id=None if task is None else task.run_id,
            occurred_at=_now(),
            stream_sequence=self._sequencer.next(session.session_id),
            payload=snapshot,
        )

    def _result(
        self,
        *,
        request_id: str,
        operation: str,
        session: Session,
        snapshot: SessionSnapshot,
        status: CommandStatus,
        replayed: bool,
        job_id: str | None = None,
    ) -> CommandResultView:
        return CommandResultView(
            request_id=request_id,
            operation=operation,  # type: ignore[arg-type]
            session_id=session.session_id,
            run_id=None if snapshot.current_task is None else snapshot.current_task.run_id,
            snapshot_id=snapshot.snapshot_id,
            status=status,
            session_state=snapshot.session_state,
            replayed=replayed,
            job_id=job_id,
            error=snapshot.errors[0] if snapshot.errors else None,
        )

    def _summary(self, session: Session) -> SessionSummaryView:
        workspace = Path(session.workspace_root)
        return SessionSummaryView(
            session_id=session.session_id,
            thread_id=session.thread_id,
            title=session.title,
            workspace=self._ref_for(workspace),
            # `Session.mode` 来自存储层，类型上是自由字符串（旧库或手工改过的值都可能出现），而 DTO
            # 只认 "chat" / "agent"。这里**显式收敛**，不用 `cast` 骗过类型检查：真出现第三种值时，
            # 页面按 Agent 显示，而不是让一句类型谎话把数据问题藏起来（收敛到 Agent 而不是闲聊，
            # 是因为闲聊只绑定只读工具——把功能误判成闲聊会让会话连该有的工具都拿不到）。
            mode="chat" if session.mode == "chat" else "agent",
            is_active=(session.status == "active"),
            archived=session.archived,
            created_at=_as_dt(session.created_at),
            updated_at=_as_dt(session.updated_at),
            last_checkpoint_id=session.last_checkpoint_id,
        )

    def _session_or_raise(self, session_id: str, *, allow_archived: bool = False) -> Session:
        session = self._sqlite_access(partial(self._sessions.get, session_id))
        if session is None or (session.archived and not allow_archived):
            raise app_errors.session_not_found(session_id=session_id, occurred_at=_now())
        return session

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    # ---- 内部：事件投影 ------------------------------------------------------

    def _turn_events(
        self,
        session: Session,
        *,
        snapshot: SessionSnapshot,
        view: _ThreadView,
        resolve_decision: tuple[str, str] | None,
        announced: _AnnouncedFacts,
        include_terminal: bool,
    ) -> list[EventEnvelope]:
        """把"这一轮**新**发生了什么"投影成事件（增量）。

        事件是 State 的**单向投影**（§15.4）：只把已经落到 Checkpoint 的事实转成事件，不反过来
        驱动任何执行。`announced` 记住本轮已经播过的事实，因此同一个方法在执行期间可以被反复
        调用（每个节点一次）而**不会重复播**，也不需要"收尾时补一遍"的第二套逻辑。

        `include_terminal=False` 表示这是执行期的中间投影：**终态事件只在收尾那次发**。原因不是
        洁癖——回答文本可能被收尾的节点改写（`reply_overrides`，§10.1），中间投影看到的那条消息
        还没有正文；先发终态会让客户端以为"这一轮结束了"，随后又收到一条新的 `message_completed`，
        终态就不再是本轮最后一条事件。

        流式 `message_delta` 不在这里——它需要真实的 `model_call_id`（随 §15.6 的用量记录落地），
        属 T30 的流式通道。
        """
        run_id = proj.current_run_id(view.values)
        if not run_id:
            return []
        events: list[EventEnvelope] = []
        session_id = session.session_id

        if not announced.boundary_snapshot:
            # 提交受理后的**边界快照**：只在执行期的第一次投影里补发一次。
            #
            # 为什么需要它：`POST /messages` 是**同步**接口（跑完整轮才返回，见 `api/routes.py`），
            # 而 `session_snapshot` 原本只在订阅建立时投递一次——于是从"提交成功"到"第一个业务
            # 事件"之间，前端拿不到任何权威数据：用户消息和"正在处理"提示都出不来，页面看着像没
            # 反应（复审实证，2026-09-28）。这一批事件由第一次 `on_update` 触发，那时
            # `prepare_context` 已把本轮用户消息写进 Checkpoint、`task_status` 也已是 `running`，
            # 所以这一份快照是完整可用的。
            announced.boundary_snapshot = True
            events.append(self._snapshot_event(session, view))

        compression = snapshot.context.compression
        if compression is not None and compression.run_id == run_id:
            signature = compression.model_dump_json()
            if signature != announced.compression:
                announced.compression = signature
                if not events:
                    events.append(self._snapshot_event(session, view))

        if snapshot.workflow is not None:
            signature = snapshot.workflow.model_dump_json()
            if signature != announced.workflow:
                announced.workflow = signature
                if not events:
                    events.append(self._snapshot_event(session, view))

        def envelope(event_type: str, identity: str, payload: Any, payload_model: type) -> Any:
            return payload_model(
                event_id=event_id_for(event_type, identity),  # type: ignore[arg-type]
                session_id=session_id,
                task_id=run_id,
                run_id=run_id,
                occurred_at=_now(),
                stream_sequence=self._sequencer.next(session_id),
                payload=payload,
            )

        if snapshot.plan is not None:
            # 计划会**变**（步骤状态推进）：同一 event id 换内容会被客户端当重复丢掉，
            # 所以按修订号发新的 id。修订号只在同一轮的投影序列内递增 → 同一份 Checkpoint
            # 重放得到同一串 id（稳定标识的要求仍然成立）。
            signature = snapshot.plan.model_dump_json()
            if signature != announced.plan:
                announced.plan = signature
                announced.plan_revision += 1
                events.append(
                    envelope(
                        "plan_updated",
                        f"{run_id}:plan:{announced.plan_revision}",
                        app_events.PlanUpdatedPayload(plan=snapshot.plan),
                        app_events.PlanUpdatedEvent,
                    )
                )

        # 只投影**这一轮**的工具行：`tool_events` 是跨轮累积的，不筛轮次会把上一轮的工具行
        # 重新广播一遍（还带着本轮的轮次标识）。事件标识同样按 (run, 调用 id) 作用域，
        # 跨轮复用同一个调用 id 时不会撞 id。
        for tool in proj.project_tools(view.values, secrets=self._secrets, run_id=run_id):
            scope = tool_scope_key(run_id, tool.tool_call_id)
            previous = announced.tools.get(scope)
            if previous is None:
                # 第一次见到这个调用：`tool_started` 必发（即使它已经跑完，客户端也要先知道
                # "做过这件事"，再收到完成）。
                events.append(
                    envelope(
                        "tool_started",
                        scope,
                        app_events.ToolStartedPayload(tool=tool),
                        app_events.ToolStartedEvent,
                    )
                )
            if tool.status in {"ok", "error", "denied"} and previous != tool.status:
                events.append(
                    envelope(
                        "tool_completed",
                        scope,
                        app_events.ToolCompletedPayload(
                            tool_call_id=tool.tool_call_id,
                            status=tool.status,
                            output_digest=tool.output_digest,
                            elapsed_ms=tool.elapsed_ms,
                        ),
                        app_events.ToolCompletedEvent,
                    )
                )
            announced.tools[scope] = tool.status

        # 本轮每次模型调用三条事件（§15.6）：`started` 先发（客户端要能立刻看到"正在调用哪个模型"），
        # 然后是供应商上报的用量，最后是完成。记录只在本轮内筛（`model_call_records` 跨轮累积），
        # 与工具同一套去重口径：`model_call_id` 是稳定标识，重试会拿到**新的** id，因此不会互相吞掉。
        # 事件载荷**不含 Prompt 正文**：只带调用标识、用途、连接与用量数字。
        for call in proj.project_model_calls(view.values, run_id):
            if call.model_call_id in announced.model_calls:
                continue
            announced.model_calls.add(call.model_call_id)
            events.append(
                envelope(
                    "model_call_started",
                    call.model_call_id,
                    app_events.ModelCallStartedPayload(
                        model_call_id=call.model_call_id,
                        purpose=call.purpose,
                        connection_id=call.connection_id,
                        config_version=call.config_version,
                        model_id=call.model_id,
                    ),
                    app_events.ModelCallStartedEvent,
                )
            )
            events.append(
                envelope(
                    "model_usage_reported",
                    call.model_call_id,
                    app_events.ModelUsagePayload(
                        model_call_id=call.model_call_id,
                        actual_uncached_input_tokens=call.actual_uncached_input_tokens,
                        actual_cache_read_tokens=call.actual_cache_read_tokens,
                        actual_cache_write_tokens=call.actual_cache_write_tokens,
                        actual_output_tokens=call.actual_output_tokens,
                        actual_reasoning_tokens=call.actual_reasoning_tokens,
                        cache_metrics_reported=call.cache_metrics_reported,
                    ),
                    app_events.ModelUsageReportedEvent,
                )
            )
            events.append(
                envelope(
                    "model_call_completed",
                    call.model_call_id,
                    app_events.ModelCallCompletedPayload(
                        model_call_id=call.model_call_id,
                        status=call.status,  # type: ignore[arg-type]
                        model_elapsed_ms=call.model_elapsed_ms,
                        time_to_first_token_ms=call.time_to_first_token_ms,
                    ),
                    app_events.ModelCallCompletedEvent,
                )
            )

        if snapshot.memory is not None:
            if not announced.memory:
                announced.memory = True
                events.append(
                    envelope(
                        "memory_recalled",
                        f"{run_id}:memory",
                        app_events.MemoryRecalledPayload(memory=snapshot.memory),
                        app_events.MemoryRecalledEvent,
                    )
                )
            if snapshot.memory.write_outcome is not None and not announced.memory_written:
                announced.memory_written = True
                events.append(
                    envelope(
                        "memory_written",
                        f"{run_id}:memory-write",
                        app_events.MemoryWrittenPayload(
                            outcome=snapshot.memory.write_outcome,
                            detail=snapshot.memory.write_detail or "",
                        ),
                        app_events.MemoryWrittenEvent,
                    )
                )

        for message in snapshot.messages:
            if message.kind != "assistant" or not message.complete or not (message.text or message.reasoning):
                continue
            # 本轮开始前就有的回答不属于这一轮；同一条消息只播一次——它的稳定标识就是
            # message id，文本后续被改写由**快照**负责（§15.3），不再发第二条。
            if message.message_id in announced.baseline_messages or message.message_id in announced.messages:
                continue
            announced.messages.add(message.message_id)
            events.append(
                envelope(
                    "message_completed",
                    message.message_id,
                    app_events.MessageCompletedPayload(
                        message_id=message.message_id,
                        role="assistant",
                        text=message.text,
                        reasoning=message.reasoning,
                        reasoning_truncated=message.reasoning_truncated,
                        complete=message.complete,
                    ),
                    app_events.MessageCompletedEvent,
                )
            )

        if resolve_decision is not None and snapshot.approval is not None:
            approval_id, decision = resolve_decision
            if not announced.approval_resolved:
                announced.approval_resolved = True
                resolved_state: Literal["approved", "rejected", "expired"] = (
                    "rejected"
                    if decision == "reject"
                    else ("expired" if snapshot.approval.state == "expired" else "approved")
                )
                events.append(
                    envelope(
                        "approval_resolved",
                        approval_id,
                        app_events.ApprovalResolvedPayload(approval_id=approval_id, state=resolved_state),
                        app_events.ApprovalResolvedEvent,
                    )
                )

        if view.has_live_interrupt and snapshot.approval is not None and not announced.approval_required:
            announced.approval_required = True
            events.append(
                envelope(
                    "approval_required",
                    snapshot.approval.approval_id,
                    app_events.ApprovalRequiredPayload(approval=snapshot.approval),
                    app_events.ApprovalRequiredEvent,
                )
            )

        if include_terminal and not view.has_live_interrupt and not announced.terminal:
            # 终态只播一次：它是"这一轮结束"的唯一标记，重复播会让客户端以为又结束了一次。
            announced.terminal = True
            if proj.project_turn_state(view.values) == "failed" and snapshot.errors:
                events.append(
                    envelope(
                        "task_failed",
                        run_id,
                        app_events.TaskFailedPayload(error=snapshot.errors[0]),
                        app_events.TaskFailedEvent,
                    )
                )
            elif snapshot.final_result is not None:
                # ``blocked``（本轮已结束、在等用户补充）同样带 ``final_result``，因此也在这里收尾：
                # 终态只认"有没有结果"，不由具体状态决定——否则那条回答又发不出去了。
                events.append(
                    envelope(
                        "task_completed",
                        run_id,
                        app_events.TaskCompletedPayload(final_result=snapshot.final_result),
                        app_events.TaskCompletedEvent,
                    )
                )
        return events


@dataclass
class _AnnouncedFacts:
    """本轮**已经广播过**的事实（增量投影的去重账）。

    只记"说过什么"，不记业务状态：真源始终是 Checkpoint（§15.4）。`baseline_messages` 是本轮
    开始前就存在的回答 id——它们不属于这一轮，不能因为"这次读到了"就再播一遍（旧实现每轮都会
    把历史回答重新广播，还带着本轮的轮次标识）。
    """

    baseline_messages: set[str] = field(default_factory=set)
    messages: set[str] = field(default_factory=set)
    #: 边界快照是否已经播过。提交受理后的第一份快照只播一次：它是"提交成功"与"第一个业务事件"
    #: 之间唯一的权威数据来源（见 `_turn_events`）。
    boundary_snapshot: bool = False
    workflow: str | None = None
    compression: str | None = None
    #: 工具作用域 → 已广播到的状态（`pending`/`running`/`ok`…）。
    tools: dict[str, str] = field(default_factory=dict)
    #: 已广播过的模型调用 id（§15.6 的三条事件一次发完，按 id 去重；重试是新 id，不会互相吞掉）。
    model_calls: set[str] = field(default_factory=set)
    #: 计划的规范化签名：内容变了才发新的一条（同一 event id 换内容会被客户端当重复丢掉）。
    plan: str | None = None
    plan_revision: int = 0
    memory: bool = False
    memory_written: bool = False
    approval_required: bool = False
    approval_resolved: bool = False
    terminal: bool = False


def _assistant_message_ids(values: Mapping[str, Any], secrets: Iterable[str]) -> set[str]:
    """本轮开始前就存在的回答 id。"""
    return {
        view.message_id
        for view in proj.project_messages_view(values, secrets=secrets)
        if view.kind == "assistant"
    }


#: 领域里的传输枚举 → 读 DTO 的枚举（契约里定死的是下划线写法，不是连字符）。
_TRANSPORT_DTO: Final[dict[str, str]] = {
    "https": "https",
    "loopback-http": "loopback_http",
    "plaintext-http": "plaintext_http",
}

#: 领域状态 → 读 DTO 的状态；`testing` 是页面等待探测返回时的临时展示态，服务端不会产生它。
_CONNECTION_STATUS_DTO: Final[dict[str, str]] = {
    "unverified": "unverified",
    "verified": "verified",
    "failed": "failed",
    "unsupported": "unsupported",
}

#: 单项能力结论 → DTO。DTO 里没有 `unsupported` 这一档：端点明确不支持时该项记 `failed`
#: （"不能靠它做这件事"），整体是否 `unsupported` 由连接的 `status` 表达。
_CAPABILITY_RESULT_DTO: Final[dict[str, str]] = {
    "pass": "passed",
    "fail": "failed",
    "unsupported": "failed",
    "unknown": "unknown",
}


def binding_of(connection: ModelConnection) -> ModelBinding:
    """把一条连接固定成"这次运行的绑定"：只有 id、版本与引用，**没有密钥**。"""
    return ModelBinding(
        connection_id=connection.connection_id,
        config_version=connection.config_version,
        model=connection.model,
        credential_ref=connection.credential_ref,
        endpoint_fingerprint=connection.endpoint_fingerprint,
    )


def binding_from_state(values: Mapping[str, Any]) -> ModelBinding | None:
    """从 Checkpoint 的值里读出这一轮固定的连接；没有或形状不对就返回 ``None``。

    形状不对按"没有"处理：把一个可疑值当凭据引用去取密钥，比直说"没有绑定"更糟。
    """
    raw = values.get("model_binding")
    if not isinstance(raw, Mapping) or not raw.get("connection_id"):
        return None
    credential_ref = raw.get("credential_ref")
    return ModelBinding(
        connection_id=str(raw["connection_id"]),
        config_version=int(raw.get("config_version") or 0),
        model=str(raw.get("model") or ""),
        credential_ref=None if credential_ref is None else str(credential_ref),
        endpoint_fingerprint=str(raw.get("endpoint_fingerprint") or ""),
    )


def _capabilities_from(report: CapabilityReport) -> ModelCapabilities:
    """把"这条连接实测到的能力"交给 Graph。

    ``tool_calling`` 只在**端点明确拒绝**（``unsupported``）时才关掉：§6.8 拦的是**缺失**的能力，
    不是"还没测过"——未验证的连接照常能跑工具任务（页面另有"未验证"提示），否则"保存了但还没点测试"
    就成了不可用状态，比不保存还糟。``structured_json_schema`` 没有单独检测项，保持 False，
    让既有的降级链自己走，不假装一项没测过的能力可用。
    """
    return ModelCapabilities(
        tool_calling=report.tools != "unsupported",
        streaming=report.streaming == "pass",
    )


def _selected_or_default(store: ConnectionStore, session_id: str) -> Selection | None:
    """会话显式选择优先，其次默认连接（§6.8：新会话继承当前默认）。"""
    return store.session_selection(session_id) or store.default_selection()


def _configured_model_name(app_config: Any) -> str:
    """旧配置里的模型名；没有 ``llm`` 段时给空串（不能在这里抛，降级路径还要继续跑）。"""
    llm = getattr(app_config, "llm", None)
    return str(getattr(llm, "model", "") or "")


def _transport_dto(transport: str) -> Any:
    return cast(Any, _TRANSPORT_DTO.get(transport, "https"))


def _connection_status_dto(status: str) -> Any:
    return cast(Any, _CONNECTION_STATUS_DTO.get(status, "unverified"))


def _capability_result_dto(outcome: str) -> Any:
    return cast(Any, _CAPABILITY_RESULT_DTO.get(outcome, "unknown"))


def _seconds_since(instant: str, now: datetime) -> float:
    """从索引里的时间戳算到 ``now`` 的秒数（时间戳读不懂时返回 0，即"不判超时"）。"""
    try:
        moment = datetime.fromisoformat(instant)
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (now - moment).total_seconds())


def _status_from_snapshot(snapshot: SessionSnapshot) -> CommandStatus:
    """命令结局由**那一份快照**的会话状态决定：挂起 → 等待审批，失败 → 失败，其余完成。

    **被取消的一轮看 `final_result.state`**（2026-10-13）：取消后会话状态就回到 `idle`（用户要的正是
    "又能用了"），所以只看会话状态会把"你自己停的"报成 `completed`——提交那条命令的响应必须如实
    说是 `cancelled`（§17.4）。
    """
    state = snapshot.session_state
    if state == "awaiting_approval":
        return "awaiting_approval"
    if state == "failed":
        return "failed"
    final = snapshot.final_result
    if final is not None and final.state == "cancelled":
        return "cancelled"
    return "completed"


def _ref_matches_run(view: _ThreadView, run_id: str | None) -> bool:
    """台账绑定的轮次与这份 Checkpoint 的轮次一致；没绑定（旧记录）就只按引用本身。"""
    if run_id is None:
        return True
    return (proj.current_run_id(view.values) or None) == run_id


def _run_id_of(chunk: Any) -> str | None:
    """从一帧图输出里取本轮的 `run_id`（没有就返回 `None`）。

    兼容 `stream_mode="updates"`（`{节点名: 更新}`）与 `"values"`（整份 State）：两者都只是
    "哪里可能有 run_id"，因此按映射逐层找，不写死节点名。
    """
    if not isinstance(chunk, Mapping):
        return None
    candidates: list[Mapping[str, Any]] = [chunk]
    candidates.extend(value for value in chunk.values() if isinstance(value, Mapping))
    for candidate in candidates:
        value = candidate.get("run_id")
        if isinstance(value, str) and value:
            return value
    return None


def _digest(*parts: str) -> str:
    """规范化参数摘要：同样的参数必须得到同样的摘要（幂等判定的基础）。"""
    material = "\x1f".join(parts)
    return hashlib.sha256(material.encode()).hexdigest()


def _media_type_from_url(url: str) -> str:
    """按 URL 路径后缀推断媒体类型（对象存储引用的展示用）。

    只用于 State 里的 `media_type` 字段与页面提示；服务端**不再解码那张图的字节**
    （对象存储引用会原样交给模型）。
    """
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dt(value: str) -> datetime:
    """把库里存的时间字符串转成带时区的 UTC。"""
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

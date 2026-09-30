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
- **图抛异常不写终态**：异常可能发生在工具副作用之后，写成"失败"等于声称"什么都没发生"；
  台账保留"执行中"并标记需要恢复，由下一次重试按 Checkpoint 核对（见 `_execute`）。
- **读取与订阅都不得改业务状态**：`get_session_snapshot` 只读 Checkpoint；事件是 State 的单向投影。
- **模型连接在提交边界解析并在这一轮固定**（§6.8）：新提交按"会话选择 → 默认连接"构造客户端，并把
  `model_binding`（连接 id + 版本 + 不含明文的凭据引用）写进 State；审批恢复按**那一轮固定的**版本
  重建客户端。所以换模型/改参数只影响下一轮，等待审批的运行不会被中途换掉。没有可用设置库时退回
  `app_config.llm` + 注入的适配器（降级路径），一个连接都没有时提交得到 `MODEL_CONNECTION_REQUIRED`。

本模块不做视觉、不做鉴权、不碰 Streamlit/FastAPI（依赖方向见 `tests/test_events.py`）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Final, Literal, TypeVar, cast

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from local_agent import config as config_module
from local_agent.application import errors as app_errors
from local_agent.application import events as app_events
from local_agent.application import projection as proj
from local_agent.application.events import (
    EventEnvelope,
    SessionSnapshotEvent,
    StreamSequencer,
    event_id_for,
)
from local_agent.application.models import (
    CapabilityCheckView,
    CommandResultView,
    ExternalPathAccess,
    ExternalPathGrantView,
    ExternalPathsMode,
    KeyStatus,
    ModelConnectionView,
    ModelSelectionView,
    PermissionsView,
    SessionSnapshot,
    SessionSummaryView,
    WorkspaceRef,
)
from local_agent.config import LlmConfig, RunConfig
from local_agent.exceptions import (
    ConnectionValidationError,
    ConnectionVersionConflict,
    ModelSettingsError,
    SelectionConflict,
)
from local_agent.graph import builder
from local_agent.graph.scope import tool_scope_key
from local_agent.llm import factory as llm_factory
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.stream_context import use_sink
from local_agent.llm.usage_recording import ModelCallRecorder, endpoint_fingerprint
from local_agent.model_settings import (
    PLACEHOLDER_API_KEY,
    CapabilityReport,
    ConnectionDraft,
    ConnectionStore,
    ModelBinding,
    ModelConnection,
    PermissionStore,
    Selection,
    SelectionRef,
)
from local_agent.model_settings import probe as connection_probe
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
from local_agent.persistence.session_store import Session, SessionStore
from local_agent.scope import SCOPE_WORKSPACE, scope_key_for
from local_agent.security.path_guard import ExternalPathPolicy, protected_target_reason

#: 任务的终态，用于"能否证明终态"的判断（§15.2 的恢复口径）。
TASK_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "failed"})

#: 一次命令的结局（与 `CommandResultView.status` 同值）。
CommandStatus = Literal["completed", "awaiting_approval", "failed"]
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


@dataclass(frozen=True)
class _RunEnv:
    """一次运行的模型环境：用哪份配置、哪个客户端、报什么模型名、带什么能力快照。"""

    app_config: RunConfig
    model: Any
    provider_name: str
    capabilities: ModelCapabilities | None
    binding: ModelBinding | None
    #: 本轮用量录音机（§15.6）。与 `app_config` 同时定好：录音机里的连接/窗口信息必须来自
    #: **这次真正在用的**那份配置，不能读"当前设置"——等待审批的运行不该被中途换掉的设置影响。
    usage_recorder: ModelCallRecorder | None = None


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
    """最终回答的临时增量；只有完整回答会进 Checkpoint。

    增量按安全分隔符成段推出，并保留最长已知密钥的重叠后缀：密钥、`sk-` 样式与绝对路径
    即使跨模型 chunk 也不会从 SSE 原样漏出。无分隔符的长串延至结束才显示，安全优先。
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

    def bind_run(self, run_id: str) -> None:
        self._run_id = run_id

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
        self._call_index += 1
        self._model_call_id = f"{self._run_id}:final-call:{self._call_index}"
        self._message_id = f"{self._run_id}:final-message:{self._call_index}"
        self._sequence = 0
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
            ),
            app_events.MessageStartedEvent,
        )
        self._bus.publish(self._session_id, [event])

    def push(self, text: str) -> None:
        if self._message_id is None:
            return
        self._pending += text
        limit = len(self._pending) - self._holdback
        boundary = max((index + 1 for index, char in enumerate(self._pending[:limit]) if char in self._BREAKS), default=0)
        if boundary:
            self._emit(self._pending[:boundary])
            self._pending = self._pending[boundary:]

    def _emit(self, raw: str) -> None:
        if self._message_id is None or self._model_call_id is None:
            return
        cleaned = proj.sanitize_text(raw, self._secrets)
        if not cleaned:
            return
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
        if self._message_id is not None:
            # 同一个稳定消息重新开始（空尾部）：前端立即清掉失败时的半句，不等普通调用结束。
            self._started(reset=True)


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
        model_factory: Callable[[LlmConfig, str], Any] | None = None,
    ) -> None:
        self._graph = graph
        self._app_config = app_config
        self._model_adapter = model_adapter
        #: 模型连接设置（§6.8）。``None`` = 没有可用设置库：按 ``app_config.llm`` + 注入的适配器运行，
        #: 并把降级原因如实告诉页面（不假装设置还在生效）。
        self._connections = connections
        #: 工作区外路径的档位与授权（T38）。与连接同库同生命周期：没有设置库时退回
        #: ``config.yaml`` 的出厂默认（``external_policy()`` 返回 ``None`` 由下层兜底）。
        self._permissions = permissions
        #: 按连接构造客户端的可调用对象；测试注入确定性假模型走同一条路径。
        self._build_model = model_factory or llm_factory.build_chat_model
        self._workspace_root = Path(workspace_root)
        self._sessions = sessions
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

    # ---- 会话 ---------------------------------------------------------------

    def list_sessions(
        self, *, workspace_root: Path | None = None, workspace_id: str | None = None
    ) -> list[SessionSummaryView]:
        """列出会话。`workspace_root` 与 `workspace_id` 二选一。

        给了 `workspace_id` 就只看该工作区（§6.1 的 `list_sessions(workspace_id)`）：HTTP 层
        只拿得到不含路径的稳定标识，路径解析留在服务里。
        """
        root = self._root_of(workspace_root, workspace_id)
        sessions = self._sqlite_access(partial(self._sessions.list_sessions, workspace_root=root))
        return [self._summary(session) for session in sessions]

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
        if self._connections is None:
            return
        values = self._sqlite_access(self._connections.secret_values)
        self._secrets = tuple(sorted(set(self._secrets) | set(values)))

    def credential_store_state(self) -> tuple[str, str | None]:
        """密钥存储的持久性与降级原因：``("os", None)`` 或 ``("memory", 说明)``。

        没有任何可用设置库时按"没有持久凭据方案"处理——那种情况下也确实没有。
        """
        if self._connections is None:
            return "memory", None
        kind = self._connections.credential_kind
        if kind == "os":
            return "os", None
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
        name_counts = Counter(path.name for path in paths)
        return [
            WorkspaceRef(
                workspace_id=ref.workspace_id,
                display_name=f"{ref.display_name} · {ref.workspace_id[:6]}"
                if name_counts[path.name] > 1 else ref.display_name,
            )
            for path, ref in zip(paths, refs)
        ]

    def create_session(
        self,
        *,
        title: str | None = None,
        workspace_root: Path | None = None,
        workspace_id: str | None = None,
    ) -> SessionSummaryView:
        """新建会话并切换过去；`workspace_root` 与 `workspace_id` 二选一。"""
        root = self._root_of(workspace_root, workspace_id)
        session = self._sqlite_access(partial(self._sessions.create, title=title, workspace_root=root))
        return self._summary(session)

    def select_workspace(self, workspace_root: Path, *, title: str | None = None) -> SessionSummaryView:
        """切换工作区：该工作区已有会话就切过去，否则新建一个。

        会话与工作区一对一绑定，所以这里**不改写**已有会话的 `workspace_root`（§10.6）；
        「切工作区」= 切会话。
        """
        session = self._sqlite_access(
            partial(self._sessions.switch_workspace, Path(workspace_root), title=title)
        )
        return self._summary(session)

    def select_workspace_by_id(self, workspace_id: str) -> SessionSummaryView:
        """选回列表中的工作区；前端只需传不含绝对路径的稳定标识。"""
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
            # 校验消息里不该出现密钥或路径，脱敏是最后一道网（§15.8）。
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

    # ---- 权限（T38；§1.5）---------------------------------------------------

    def permissions(self) -> PermissionsView:
        """当前档位与授权条目（"设置 → 权限"的读）。没有设置库时用出厂默认并如实标注。"""
        default_mode = self._app_config.workspace.external_paths.mode
        if self._permissions is None:
            return PermissionsView(
                mode=default_mode, default_mode=default_mode, grants=[], store_unavailable=True
            )
        store = self._permissions

        def read() -> PermissionsView:
            # 档位与授权条目必须在一把锁里读完：两次读之间用户可能刚好改过档位。
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
        return self.permissions()

    def revoke_external_path(self, path: str) -> PermissionsView:
        """撤销一条授权；不存在也算成功（幂等），返回撤销后的完整视图。"""
        store = self._require_permissions()
        self._sqlite_access(partial(store.revoke, path))
        return self.permissions()

    def _require_permissions(self) -> PermissionStore:
        if self._permissions is None:
            raise app_errors.settings_store_unavailable(
                detail="设置库没有打开", occurred_at=_now()
            )
        return self._permissions

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

    def submit_message(self, session_id: str, text: str, client_request_id: str) -> CommandResultView:
        """提交一条用户消息。"""
        digest = _digest(OPERATION_SUBMIT, session_id, text)
        session = self._session_or_raise(session_id)
        # 提交边界解析的结果（连接固定 + 客户端就绪），在预检里算一次、在运行里原样复用。
        prepared: list[_RunEnv] = []

        def prepare() -> None:
            """幂等判定之后、**落台账之前**的预检：解析连接、备好客户端与密钥。

            为什么不能等 `run()` 里再解析：`_guarded` 会先写一条"执行中"台账。若那时才发现没有可用连接
            或缺 Key，这条请求什么都没跑却留下了"执行中"记录——用户按提示配好连接后拿**同一个请求 id**
            重试，会被当成上一次进程留下的陈旧请求，判成 `RECOVERY_REQUIRED`（复审实证）。
            """
            self._require_idle(session, client_request_id)
            prepared.append(self._run_env(session, self._resolve_binding(session)))

        def run() -> CommandResultView:
            env = prepared[0]
            payload: dict[str, Any] = {
                "user_input": text,
                "thread_id": session.thread_id,
                "session_id": session.session_id,
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
    ) -> CommandResultView:
        """顺序固定：台账（冲突/重放）→ 抢锁（单飞）→ 陈旧台账恢复 → **执行前校验** → 落台账 → 跑图。

        `precheck` 必须排在落台账**之前**：校验没过就不该留下"执行中"的记录，否则下一次重试会
        被当成需要人工核对的陈旧请求。排在幂等判定**之后**则保证"同 id 重试拿原结果"优先于
        "现在还能不能执行"（例如审批已经决定完的重放）。

        `bind_run` 给出"这次请求绑定哪一轮 run"（能在执行前确定时用；提交消息的 run 由 Graph
        生成，登记时还不知道，由 `_execute` 在图交出 run 时补绑定）。
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
        try:
            if not acquired:
                raise app_errors.session_busy(
                    session_id=session.session_id, request_id=request_id, occurred_at=_now()
                )
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
            return run()
        finally:
            if acquired:
                lock.release()

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
        with use_sink(sink):
            for chunk in self._graph.stream(
                payload,
                config={"configurable": {"thread_id": session.thread_id}},
                context=self._context(session, env),
                stream_mode="updates",
            ):
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
                    on_update()

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
        return self._result(
            request_id=request_id,
            operation=operation,
            session=session,
            snapshot=snapshot,
            status=status,
            replayed=True,
        )

    # ---- 内部：读取与投影 ----------------------------------------------------

    def _read(self, session: Session) -> _ThreadView:
        return self._view_of(self._graph.get_state({"configurable": {"thread_id": session.thread_id}}))

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
            snapshot = self._graph.get_state(config)
        except Exception:  # noqa: BLE001 — 读不到就按"拿不回这份结果"处理（fail-closed，宁可拒绝）
            return None
        view = self._view_of(snapshot)
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
        )

    def _context(self, session: Session, env: _RunEnv) -> Any:
        """构造这次调用的运行上下文（客户端与运行参数都在预检时定好，这里只装配）。"""
        return builder.build_runtime_context(
            app_config=env.app_config,
            model_adapter=env.model,
            workspace_root=Path(session.workspace_root),
            provider_name=env.provider_name,
            model_capabilities=env.capabilities,
            # 档位与授权**每一轮现取**（"保存即生效、作用于下一轮"，T29A 口径）：用户刚在
            # 「设置 → 权限」里改完，下一次运行就该按新档位判定。
            external_policy=self.external_paths_policy(),
            # 录音机挂在上下文上：节点的用量装饰器从 `RuntimeContext` 取它（§15.6）。
            usage_recorder=env.usage_recorder,
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
        if binding is None:
            if self._app_config.llm is None:
                # 既没有设置库、也没有 llm 段：这台机器上确实没有任何可用的模型来源。
                raise app_errors.model_connection_required(
                    session_id=session.session_id, occurred_at=_now()
                )
            run_config = self._app_config.as_run_config()
            return _RunEnv(
                app_config=run_config,
                model=self._model_adapter,
                provider_name=run_config.llm.model,
                capabilities=None,
                binding=None,
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
            app_config=self._app_config.model_copy(update={"llm": llm}).as_run_config(),
            model=self._build_model(llm, self._api_key_for(connection)),
            provider_name=llm.model,
            # 恢复中的运行不重新按能力门禁：它在提交时已经被放行过，拿一份可能是"新版本"的检测结论
            # 去改变一个正在等待审批的运行，等于中途换判据（§6.8 的停止条件）。
            capabilities=None if for_resume else _capabilities_from(connection.capability),
            binding=binding,
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
            display_name=workspace.name,
        )

    def _workspace_ref(self, session: Session) -> WorkspaceRef:
        return self._ref_for(Path(session.workspace_root))

    def _snapshot(self, session: Session, view: _ThreadView) -> SessionSnapshot:
        return self._snapshot_at(
            session,
            view,
            snapshot_id=view.checkpoint_id or f"{session.session_id}:no-checkpoint",
            created_at=_now(),
        )

    def _snapshot_at(
        self, session: Session, view: _ThreadView, *, snapshot_id: str, created_at: datetime
    ) -> SessionSnapshot:
        """按给定时点与标识投影一份快照（实时快照用当前时间，重放历史用记录里的时间）。"""
        return proj.project_snapshot(
            view.values,
            session_id=session.session_id,
            workspace=self._workspace_ref(session),
            title=session.title,
            snapshot_id=snapshot_id,
            created_at=created_at,
            has_live_interrupt=view.has_live_interrupt,
            secrets=self._secrets,
            model_selection=self._session_model_selection(session.session_id),
            workspace_root=Path(session.workspace_root),
            confirmation_stage=view.confirmation_stage,
        )

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

    def _snapshot_event(self, session: Session, view: _ThreadView) -> EventEnvelope:
        snapshot = self._snapshot(session, view)
        task = snapshot.current_task
        return SessionSnapshotEvent(
            event_id=event_id_for("session_snapshot", snapshot.snapshot_id),
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
            error=snapshot.errors[0] if snapshot.errors else None,
        )

    def _summary(self, session: Session) -> SessionSummaryView:
        workspace = Path(session.workspace_root)
        return SessionSummaryView(
            session_id=session.session_id,
            thread_id=session.thread_id,
            title=session.title,
            workspace=self._ref_for(workspace),
            is_active=(session.status == "active"),
            created_at=_as_dt(session.created_at),
            updated_at=_as_dt(session.updated_at),
            last_checkpoint_id=session.last_checkpoint_id,
        )

    def _session_or_raise(self, session_id: str) -> Session:
        session = self._sqlite_access(partial(self._sessions.get, session_id))
        if session is None:
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
            if message.kind != "assistant" or not message.text:
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


def _status_from_snapshot(snapshot: SessionSnapshot) -> CommandStatus:
    """命令结局由**那一份快照**的会话状态决定：挂起 → 等待审批，失败 → 失败，其余完成。"""
    state = snapshot.session_state
    if state == "awaiting_approval":
        return "awaiting_approval"
    if state == "failed":
        return "failed"
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

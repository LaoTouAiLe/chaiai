"""运行中命令的有界展示副本；完成后的 Checkpoint 始终优先。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from local_agent.application import events, projection
from local_agent.application.models import (
    SessionSnapshot,
    ToolMessageView,
    ToolOutcomeView,
    ToolUseView,
)
from local_agent.graph.scope import tool_scope_key
from local_agent.tools.progress import ToolProgress


class ToolStream:
    def __init__(
        self, session_id: str, publish: Callable[[Iterable[events.EventEnvelope]], None],
        secrets: tuple[str, ...], *, limit: int = 30_000,
    ) -> None:
        self._session_id = session_id
        self._publish = publish
        self._secrets = secrets
        self._limit = limit
        self._holdback = max((len(secret) - 1 for secret in secrets), default=0)
        self._lock = threading.Lock()
        self._tools: dict[str, tuple[str, ToolUseView]] = {}
        self._raw: dict[tuple[str, str], str] = {}
        self._last_emit: dict[str, float] = {}
        self._sequence = 0

    def notify(self, progress: ToolProgress) -> None:
        scope = tool_scope_key(progress.run_id, progress.tool_call_id)
        with self._lock:
            if not progress.stream:
                tool = ToolUseView(
                    tool_call_id=progress.tool_call_id, tool_name="run_shell", status="running",
                    redacted_arguments={key: projection.sanitize_text(value, self._secrets)
                                        for key, value in progress.arguments.items()},
                    started_at=datetime.now(timezone.utc),
                    outcome=ToolOutcomeView(kind="shell", target=projection.sanitize_text(
                        progress.arguments.get("cwd", "."), self._secrets),
                                            stdout="", stderr=""),
                )
                self._tools[scope] = (progress.run_id, tool)
                payload: Any = events.ToolStartedPayload(tool=tool)
                event_type: events.EventType = "tool_started"
                model: Any = events.ToolStartedEvent
            else:
                if scope not in self._tools or progress.stream not in {"stdout", "stderr"}:
                    return
                run_id, tool = self._tools[scope]
                key = (scope, progress.stream)
                raw = self._raw.get(key, "")
                if len(raw) >= self._limit + self._holdback + 4096:
                    return
                raw = (raw + progress.text)[:self._limit + self._holdback + 4096]
                self._raw[key] = raw
                # 保留未完的一段，以及恰好位于末尾的密钥前缀。普通短行不必等够一个密钥的长度。
                boundary = max((i + 1 for i, char in enumerate(raw) if char.isspace()),
                               default=0)
                for secret in self._secrets:
                    for length in range(1, min(len(secret), len(raw)) + 1):
                        if raw.endswith(secret[:length]):
                            boundary = min(boundary, len(raw) - length)
                boundary = projection.safe_stream_boundary(raw, boundary, self._secrets)
                text = projection.sanitize_text(raw[:boundary], self._secrets)[:self._limit]
                assert tool.outcome is not None
                outcome = tool.outcome.model_copy(update={progress.stream: text,
                                                          "truncated": len(raw) > self._limit})
                tool = tool.model_copy(update={"outcome": outcome,
                                               "truncated": outcome.truncated})
                self._tools[scope] = (run_id, tool)
                now = time.monotonic()
                if now - self._last_emit.get(scope, 0) < 0.1:
                    return
                self._last_emit[scope] = now
                payload = events.ToolOutputPayload(tool_call_id=progress.tool_call_id,
                                                   output_preview=text,
                                                   truncated=outcome.truncated)
                event_type = "tool_output"
                model = events.ToolOutputEvent
            self._sequence += 1
            event = model(
                event_id=events.event_id_for(event_type, f"{scope}:live:{self._sequence}"),
                session_id=self._session_id, task_id=progress.run_id, run_id=progress.run_id,
                occurred_at=datetime.now(timezone.utc), stream_sequence=1, payload=payload,
            )
        self._publish([event])

    def merge(self, snapshot: SessionSnapshot) -> SessionSnapshot:
        if snapshot.current_task is None or snapshot.session_state != "running":
            return snapshot
        with self._lock:
            messages = list(snapshot.messages)
            for scope, (run_id, tool) in list(self._tools.items()):
                if run_id != snapshot.current_task.run_id:
                    continue
                identity = f"tool:{scope}"
                index = next((i for i, message in enumerate(messages)
                              if message.message_id == identity), None)
                if index is not None:
                    recorded = messages[index]
                    if isinstance(recorded, ToolMessageView) and recorded.tool.status in {
                        "ok", "error", "denied", "unknown",
                    }:
                        self._tools.pop(scope)
                        for stream in ("stdout", "stderr"):
                            self._raw.pop((scope, stream), None)
                        self._last_emit.pop(scope, None)
                        continue
                    messages[index] = ToolMessageView(message_id=identity, tool=tool)
                else:
                    messages.append(ToolMessageView(message_id=identity, tool=tool))
            return snapshot.model_copy(update={"messages": messages})

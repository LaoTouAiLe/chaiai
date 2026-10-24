"""Local timing spans only: identifiers, stage names and clocks; no task content."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

logger = logging.getLogger(__name__)
_WRITE_LOCK = threading.Lock()
_ACTIVE: ContextVar[TimingTrace | None] = ContextVar("performance_trace", default=None)
_PARENT: ContextVar[str | None] = ContextVar("performance_parent", default=None)
_PRIORITY = ("model", "tool", "checkpoint", "context_build")
_T = TypeVar("_T")


def breakdown(start: int, end: int, records: list[dict[str, Any]]) -> dict[str, float]:
    """Exclusive wall time; concurrent spans use the documented priority order."""
    edges: list[tuple[int, str, int]] = []
    for record in records:
        stage = record["stage"]
        if stage not in _PRIORITY:
            continue
        left, right = max(start, record["start_ns"]), min(end, record["end_ns"])
        if left < right:
            edges.extend(((left, stage, 1), (right, stage, -1)))
    active: Counter[str] = Counter()
    totals = dict.fromkeys((*_PRIORITY, "other"), 0)
    previous = start
    for instant, stage, delta in sorted(edges):
        label = next((item for item in _PRIORITY if active[item] > 0), "other")
        totals[label] += instant - previous
        active[stage] += delta
        previous = instant
    totals["other"] += max(0, end - previous)
    return {key: value / 1_000_000 for key, value in totals.items()}


@dataclass
class TimingTrace:
    path: Path
    session_id: str
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    run_id: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)
    lock: Any = field(default_factory=threading.Lock)
    failed: bool = False

    def write(self, record: dict[str, Any]) -> None:
        if self.failed:
            return
        line = json.dumps({"trace_id": self.trace_id, "session_id": self.session_id, **record}) + "\n"
        try:
            with _WRITE_LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as output:
                    output.write(line)
        except OSError:
            self.failed = True
            logger.warning("Performance JSONL unavailable; task execution continues")


@dataclass
class TimingSpan:
    trace: TimingTrace
    stage: str
    operation: str
    run_id: str | None
    start_ns: int = field(default_factory=time.perf_counter_ns)
    span_id: str = field(default_factory=lambda: uuid4().hex)
    parent_span_id: str | None = field(default_factory=_PARENT.get)
    ended: bool = False

    def finish(self, status: str = "ok") -> None:
        if self.ended:
            return
        self.ended = True
        end = time.perf_counter_ns()
        record = {"kind": "span", "span_id": self.span_id, "parent_span_id": self.parent_span_id,
                  "run_id": self.run_id, "stage": self.stage, "operation": self.operation,
                  "start_ns": self.start_ns, "end_ns": end,
                  "elapsed_ms": (end - self.start_ns) / 1_000_000, "status": status}
        with self.trace.lock:
            self.trace.records.append(record)
        self.trace.write(record)


def start_span(stage: str, operation: str, run_id: str | None = None) -> TimingSpan | None:
    trace = _ACTIVE.get()
    return TimingSpan(trace, stage, operation, run_id) if trace is not None else None


@contextmanager
def span(stage: str, operation: str, run_id: str | None = None) -> Iterator[TimingSpan | None]:
    current = start_span(stage, operation, run_id)
    if current is None:
        yield None
        return
    token = _PARENT.set(current.span_id)
    status = "ok"
    try:
        yield current
    except BaseException:
        status = "error"
        raise
    finally:
        _PARENT.reset(token)
        current.finish(status)


@contextmanager
def trace_turn(path: Path | None, session_id: str) -> Iterator[None]:
    if path is None:
        yield
        return
    trace = TimingTrace(path, session_id)
    token = _ACTIVE.set(trace)
    parent = _PARENT.set(None)
    start = time.perf_counter_ns()
    status = "ok"
    try:
        yield
    except BaseException:
        status = "error"
        raise
    finally:
        end = time.perf_counter_ns()
        _ACTIVE.reset(token)
        _PARENT.reset(parent)
        with trace.lock:
            records = list(trace.records)
        runs = sorted({record["run_id"] for record in records if record["run_id"]})
        trace.write({"kind": "summary", "run_id": trace.run_id or (runs[0] if len(runs) == 1 else None),
                     "run_ids": runs, "start_ns": start, "end_ns": end,
                     "total_ms": (end - start) / 1_000_000, "status": status,
                     "breakdown_ms": breakdown(start, end, records),
                     "overlap_priority": list(_PRIORITY)})


def timed_node(function: Callable[..., _T], *, stage: str, operation: str) -> Callable[..., _T]:
    @wraps(function)
    def measured(state: Any, *args: Any, **kwargs: Any) -> _T:
        # prepare_context replaces the previous run id; bind from its update.
        run_id = None if operation == "prepare_context" else state.get("run_id")
        with span(stage, operation, run_id) as current:
            result = function(state, *args, **kwargs)
            if current is not None and isinstance(result, dict):
                current.run_id = result.get("run_id") or run_id
                if operation == "prepare_context" and current.run_id:
                    current.trace.run_id = current.run_id
            return result
    return measured

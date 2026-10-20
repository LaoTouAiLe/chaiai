"""T59：结构化任务事件（架构 §15.4、§17.3）。

三组用例：

1. **映射不重不漏**：`JOB_EVENT_FOR_STATE` 覆盖状态闭集，五个类型都在正式事件全集里；
2. **构造与解析**：`build_job_event` 的产物能被 `parse_event` 认回具体模型，信封规则（`task_id`
   非空、`run_id` 可空、`job_id` 存在）成立；
3. **只在真迁移时发**：一次正常的内联执行恰好两次迁移；异常路径多出的那次是
   `running → needs_review`（2026-10-20 起它是真迁移，页面靠它把"正在执行"换成"需要人工核对"）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from local_agent.application import events as app_events
from local_agent.application.events import (
    EVENT_TYPES,
    RESERVED_FOR_V06,
    build_job_event,
    event_id_for,
    is_job_event_type,
    parse_event,
)
from local_agent.application.jobs import JOB_EVENT_FOR_STATE, JobRuntime
from local_agent.application.models import CommandResultView
from local_agent.persistence import migrations
from local_agent.persistence.job_store import (
    JOB_STATE_COMPLETED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_REVIEW,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_WAITING_USER,
    JOB_STATES,
    JobStore,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def test_state_to_event_type_mapping_is_total_and_matches_the_closed_set() -> None:
    assert set(JOB_EVENT_FOR_STATE) == set(JOB_STATES)
    assert set(JOB_EVENT_FOR_STATE.values()) == {
        "job_queued",
        "job_progress",
        "job_waiting",
        "job_completed",
        "task_cancelled",
    }
    # 五个类型都已转正，并且都被认作任务事件
    for event_type in JOB_EVENT_FOR_STATE.values():
        assert event_type in EVENT_TYPES
        assert is_job_event_type(event_type)
    # failed 与 completed 共用 job_completed（载荷里的 state 区分）
    assert JOB_EVENT_FOR_STATE[JOB_STATE_FAILED] == JOB_EVENT_FOR_STATE[JOB_STATE_COMPLETED]
    assert RESERVED_FOR_V06 == ()
    # 轮次事件不会被误认成任务事件
    assert not is_job_event_type("task_completed")
    assert not is_job_event_type("session_snapshot")


def _built(event_type: str, *, state: str = "queued", run_id: str | None = None, detail=None):
    return build_job_event(
        event_type=event_type,
        event_id=event_id_for(event_type, "job-1:2026-10-10T12:00:00+00:00"),
        session_id="s1",
        job_id="job-1",
        run_id=run_id,
        occurred_at=NOW,
        stream_sequence=1,
        state=state,
        detail=detail,
    )


def test_built_job_event_round_trips_and_carries_the_envelope_rules() -> None:
    queued = _built("job_queued")
    parsed = parse_event(queued.model_dump(mode="json"))

    assert isinstance(parsed, app_events.JobQueuedEvent)
    assert parsed.payload.state == "queued"
    # 排队时还没有轮次：task_id / job_id 非空，run_id 允许为空（§17.3）
    assert parsed.task_id == "job-1" and parsed.job_id == "job-1" and parsed.run_id is None
    assert parsed.event_id == queued.event_id

    progress = parse_event(_built("job_progress", state="running", run_id="run-7").model_dump(mode="json"))
    assert isinstance(progress, app_events.JobProgressEvent)
    assert progress.run_id == "run-7"

    cancelled = parse_event(
        _built("task_cancelled", state="cancelled", detail="用户取消").model_dump(mode="json")
    )
    assert isinstance(cancelled, app_events.TaskCancelledEvent)
    assert cancelled.payload.detail == "用户取消"


def test_build_job_event_rejects_non_job_types() -> None:
    with pytest.raises(ValueError, match="不是任务生命周期事件"):
        _built("task_completed", state="completed")


def _runtime(tmp_path: Path, listener) -> tuple[JobStore, JobRuntime]:
    connection = sqlite3.connect(tmp_path / "chat.sqlite", check_same_thread=False)
    migrations.apply_migrations(connection)
    store = JobStore(connection)
    return store, JobRuntime(store, background=False, on_transition=listener)


def _result(status: str) -> CommandResultView:
    return CommandResultView(
        request_id="req-1",
        operation="submit_message",
        session_id="s1",
        run_id="run-1",
        snapshot_id="ckpt-1",
        status=status,  # type: ignore[arg-type]
        session_state="completed",
    )


def test_listener_sees_one_notification_per_real_transition(tmp_path: Path) -> None:
    seen: list[tuple[str | None, str]] = []
    _store, runtime = _runtime(
        tmp_path, lambda before, after: seen.append((None if before is None else before.state, after.state))
    )

    job = runtime.begin(session_id="s1")
    runtime.run_now(job, lambda: _result("completed"))

    # 诞生（→queued）→ running → completed：三次通知，没有多余的一次
    assert seen == [
        (None, JOB_STATE_QUEUED),
        (JOB_STATE_QUEUED, JOB_STATE_RUNNING),
        (JOB_STATE_RUNNING, JOB_STATE_COMPLETED),
    ]


def test_exception_emits_a_real_transition_to_needs_review(tmp_path: Path) -> None:
    """异常 → `needs_review` 是**真迁移**：不发事件，页面就永远不知道它已经停了。

    2026-10-20 之前这里只写 `detail`、状态留 `running`；那样任务中心会一直显示"正在执行"，
    这一轮改动让它成为一次真正的迁移。
    """
    seen: list[str] = []
    _store, runtime = _runtime(tmp_path, lambda before, after: seen.append(after.state))

    def boom() -> CommandResultView:
        raise RuntimeError("工具炸了")

    job = runtime.begin(session_id="s1")
    with pytest.raises(RuntimeError):
        runtime.run_now(job, boom)

    assert seen == [JOB_STATE_QUEUED, JOB_STATE_RUNNING, JOB_STATE_NEEDS_REVIEW]


def test_waiting_lifecycle_emits_waiting_then_returns_to_running(tmp_path: Path) -> None:
    seen: list[str] = []
    _store, runtime = _runtime(tmp_path, lambda before, after: seen.append(after.state))

    job = runtime.begin(session_id="s1")
    runtime.run_now(job, lambda: _result("awaiting_approval"))
    runtime.run_now(runtime.resume(session_id="s1"), lambda: _result("completed"))

    assert seen == [
        JOB_STATE_QUEUED,
        JOB_STATE_RUNNING,
        JOB_STATE_WAITING_USER,
        JOB_STATE_RUNNING,
        JOB_STATE_COMPLETED,
    ]

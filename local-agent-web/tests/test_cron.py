"""定时任务（§21.5，2026-10-22 J2）的契约测试。

不启 HTTP：调度用例使用真实 SQLite 与服务替身；结果记账另用真实应用服务、
Graph、审批与 Checkpoint 验证，模型固定为离线假实现。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.application import cron
from local_agent.application.jobs import JobRuntime
from local_agent.application.models import CommandResultView
from local_agent.application.service import ApplicationService
from local_agent.graph import builder
from local_agent.persistence import checkpoint, migrations
from local_agent.persistence.cron_store import CronStore
from local_agent.persistence.job_store import JobStore
from local_agent.persistence.request_ledger import RequestLedger


def _result(**overrides):
    return CommandResultView(**{
        "request_id": "req-1", "operation": "submit_message", "session_id": "sess-1",
        "run_id": "turn-1", "snapshot_id": "checkpoint-1", "status": "completed",
        "session_state": "completed", **overrides,
    })


def _service(tmp_path: Path):
    connection = sqlite3.connect(tmp_path / "chat.sqlite", check_same_thread=False)
    migrations.apply_migrations(connection)
    store = CronStore(connection)
    lock = threading.Lock()
    calls: list[tuple] = []

    def create_session(**kwargs):
        calls.append(("create", kwargs))
        return SimpleNamespace(session_id="sess-1")

    def submit(session_id, text, request_id, **_kwargs):
        calls.append(("submit", session_id, text))
        return _result(request_id=request_id)

    values = {"run_id": "turn-1", "approval_history": []}

    def read_checkpoint(session, checkpoint_id):
        assert session.session_id == "sess-1"
        assert checkpoint_id == "checkpoint-1"
        return SimpleNamespace(values=values)

    service = SimpleNamespace(
        # 生产实现同样用一把锁串行化这条连接（_sqlite_access 的注释解释了原因）。
        _sqlite_access=lambda action: (lock.acquire(), action(), lock.release())[1],
        _cron=store,
        _workspace_root=tmp_path,
        _path_for_workspace_id=lambda workspace_id: tmp_path,
        create_session=create_session,
        submit_message=submit,
        _session_or_raise=lambda session_id: SimpleNamespace(session_id=session_id),
        _read_checkpoint=read_checkpoint,
        cron_scheduler=None,
    )
    return service, store, calls


def _past(minutes: int = 5) -> str:
    return (datetime.now(timezone.utc).astimezone() - timedelta(minutes=minutes)).isoformat()


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_next_run_at_advances_into_future() -> None:
    now = datetime.now(timezone.utc).astimezone()
    upcoming = cron.next_run_at("* * * * *", after=now)
    assert upcoming > now
    assert timedelta(seconds=0) < upcoming - now <= timedelta(minutes=1)


def test_invalid_schedule_is_rejected() -> None:
    with pytest.raises(ValueError, match="cron"):
        cron.next_run_at("每天九点")


def test_create_validates_and_schedules(tmp_path: Path) -> None:
    service, _store, _calls = _service(tmp_path)
    with pytest.raises(ValueError, match="任务名"):
        cron.create_job(service, name="  ", prompt="做事", schedule="0 9 * * *")
    with pytest.raises(ValueError, match="cron"):
        cron.create_job(service, name="早报", prompt="做事", schedule="nope")

    view = cron.create_job(service, name="早报", prompt="汇总昨天的提交", schedule="0 9 * * *")
    assert view.enabled is True
    assert view.next_run_at is not None
    assert datetime.fromisoformat(view.next_run_at) > datetime.now(timezone.utc).astimezone()


def test_tick_runs_due_job_as_detached_scheduled_session(tmp_path: Path) -> None:
    service, store, calls = _service(tmp_path)
    job = store.create_job(name="早报", prompt="汇总", schedule="* * * * *",
                           workspace_root=str(tmp_path), mode="agent",
                           next_run_at=_past(), now=_past())

    scheduler = cron.CronScheduler(service, interval=3600)
    assert scheduler.tick() == 1

    runs, _total = store.list_runs(job.job_id)
    assert runs[0].status in {"running", "succeeded"}
    assert _wait(lambda: store.list_runs(job.job_id)[0][0].status == "succeeded")

    created = [entry for entry in calls if entry[0] == "create"]
    assert created[0][1]["kind"] == "scheduled"
    assert created[0][1]["activate"] is False

    final = store.list_runs(job.job_id)[0][0]
    assert final.session_id == "sess-1"
    # 排期必须已经推到未来，否则下一次扫描会立刻重复触发。
    refreshed = store.get_job(job.job_id)
    assert refreshed is not None
    assert datetime.fromisoformat(refreshed.next_run_at) > datetime.now(timezone.utc).astimezone()


def test_overlapping_run_is_skipped_not_queued(tmp_path: Path) -> None:
    service, store, _calls = _service(tmp_path)
    job = store.create_job(name="长任务", prompt="跑很久", schedule="* * * * *",
                           workspace_root=str(tmp_path), mode="agent",
                           next_run_at=_past(), now=_past())
    store.start_run(job.job_id, status="running", started_at=_past())

    cron.CronScheduler(service, interval=3600).tick()

    _runs, total = store.list_runs(job.job_id)
    assert total == 1, "跳过的运行不应新开一条记录"
    assert "跳过" in (store.get_job(job.job_id).last_error or "")


def test_failed_submit_marks_run_failed(tmp_path: Path) -> None:
    service, store, _calls = _service(tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("模型不可用")

    service.submit_message = boom
    job = store.create_job(name="会失败", prompt="跑", schedule="* * * * *",
                           workspace_root=str(tmp_path), mode="agent",
                           next_run_at=_past(), now=_past())
    cron.CronScheduler(service, interval=3600).tick()

    assert _wait(lambda: store.list_runs(job.job_id)[0][0].status == "failed")
    assert "模型不可用" in (store.list_runs(job.job_id)[0][0].summary or "")


@pytest.mark.parametrize("trigger", ["tick", "now"])
@pytest.mark.parametrize("decisions,expected", [
    ([], "succeeded"),
    ([{"run_id": "turn-1", "decision": "reject"}], "blocked"),
    ([{"run_id": "old-turn", "decision": "reject"}], "succeeded"),
    ([{"run_id": "turn-1", "decision": "approve"}], "succeeded"),
    ([{"run_id": "turn-1", "decision": "reject"},
      {"run_id": "turn-1", "decision": "approve"}], "blocked"),
])
def test_run_outcome_uses_all_approvals_for_its_own_turn(
    tmp_path: Path, trigger: str, decisions: list[dict], expected: str,
) -> None:
    service, store, _calls = _service(tmp_path)
    service._read_checkpoint(SimpleNamespace(session_id="sess-1"), "checkpoint-1").values[
        "approval_history"
    ] = decisions
    job = store.create_job(name="审批记账", prompt="执行", schedule="* * * * *",
                           workspace_root=str(tmp_path), mode="agent",
                           next_run_at=_past(), now=_past())
    if trigger == "tick":
        cron.CronScheduler(service).tick()
    else:
        cron.run_now(service, job.job_id)

    assert _wait(lambda: store.list_runs(job.job_id)[0][0].status != "running")
    run = cron.list_runs(service, job.job_id).runs[0]
    assert run.status == expected
    assert run.finished_at is not None
    assert run.session_id == "sess-1"
    refreshed = cron.list_jobs(service).jobs[0]
    assert refreshed.last_status == expected
    if expected == "blocked":
        assert "审批拒绝" in run.summary
        assert refreshed.last_error == run.summary
    else:
        assert refreshed.last_error is None


@pytest.mark.parametrize("status", ["failed", "cancelled", "awaiting_approval"])
def test_noncompleted_result_cannot_be_success(tmp_path: Path, status: str) -> None:
    service, store, _calls = _service(tmp_path)
    service.submit_message = lambda *_args: _result(status=status)
    # 即使此前发生拒绝，执行自身失败/取消也不能被 blocked 覆盖。
    service._read_checkpoint(SimpleNamespace(session_id="sess-1"), "checkpoint-1").values[
        "approval_history"
    ] = [{"run_id": "turn-1", "decision": "reject"}]
    job = store.create_job(name="未成功", prompt="执行", schedule="* * * * *",
                           workspace_root=str(tmp_path), mode="agent",
                           next_run_at=_past(), now=_past())
    run = store.start_run(job.job_id, status="running", started_at=_past())
    cron.execute_job(service, job, run.run_id)
    assert store.list_runs(job.job_id)[0][0].status == "failed"


@pytest.mark.parametrize("evidence", ["missing", "wrong-turn", "unbound"])
def test_unverifiable_result_cannot_be_success(tmp_path: Path, evidence: str) -> None:
    service, store, _calls = _service(tmp_path)
    if evidence == "missing":
        service._read_checkpoint = lambda *_args: None
    elif evidence == "wrong-turn":
        service._read_checkpoint = lambda *_args: SimpleNamespace(values={"run_id": "later-turn"})
    else:
        service.submit_message = lambda *_args: _result(run_id=None)
    job = store.create_job(name="缺少证据", prompt="执行", schedule="* * * * *",
                           workspace_root=str(tmp_path), mode="agent",
                           next_run_at=_past(), now=_past())
    run = store.start_run(job.job_id, status="running", started_at=_past())
    cron.execute_job(service, job, run.run_id)
    final = store.list_runs(job.job_id)[0][0]
    assert final.status == "failed"
    assert "核对" in final.summary


@pytest.fixture
def real_service(tmp_path, app_config, model_factory):
    """真实执行与持久化链；数据库和 worker 都只存在于本用例。"""
    opened = []

    def create(*, background, **model_options):
        app_config.security.require_approval.delete_file = True
        database = checkpoint.open_database(tmp_path / "real.sqlite", workspace_root=tmp_path)
        assert database.connection.row_factory is None
        runtime = JobRuntime(
            JobStore(database.connection, lock=database.checkpointer.lock),
            background=background, max_concurrent_jobs=1,
        )
        service = ApplicationService(
            graph=builder.build_chat_graph(database.checkpointer), app_config=app_config,
            model_adapter=model_factory(**model_options), workspace_root=tmp_path,
            sessions=database.sessions, ledger=RequestLedger(database.connection), jobs=runtime,
            secrets=("sk-test-secret",),
        )
        opened.append((database, runtime))
        return service

    yield create
    for database, runtime in opened:
        assert runtime.wait_for_idle(timeout=10)
        runtime.close(timeout=5)
        database.connection.close()


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("outcome", ["succeeded", "blocked", "failed"])
def test_real_graph_records_cron_outcome(tmp_path, real_service, background, outcome):
    target = tmp_path / "note.txt"
    target.write_text("保留我", encoding="utf-8")
    options = {"decision": "chat", "reply": "已完成。"}
    if outcome == "blocked":
        options.update(decision="tool", tool_calls=[{
            "name": "delete_file", "args": {"path": "note.txt"}, "id": "call-delete",
        }])
    elif outcome == "failed":
        options["fail_answer"] = True
    service = real_service(background=background, **options)
    job = cron.create_job(service, name="真实链路", prompt="删除 note.txt", schedule="0 9 * * *")
    cron.run_now(service, job.job_id)

    assert _wait(lambda: cron.list_runs(service, job.job_id).runs[0].status != "running", timeout=10)
    run = cron.list_runs(service, job.job_id).runs[0]
    assert run.status == outcome
    assert cron.list_jobs(service).jobs[0].last_status == outcome
    assert target.read_text(encoding="utf-8") == "保留我"
    if outcome == "blocked":
        session = service._session_or_raise(run.session_id)
        values = service._read(session).values
        assert values["session_kind"] == "scheduled"
        assert any(record["decision"] == "reject" for record in values["approval_history"])
        assert values.get("pending_approval") is None
        assert service.get_session_snapshot(run.session_id).session_state != "awaiting_approval"


def test_background_cron_stays_running_until_its_execution_finishes(
    real_service, monkeypatch,
):
    service = real_service(background=True, decision="chat", reply="已完成。")
    entered, release = threading.Event(), threading.Event()
    drive = service._drive
    submit = service.submit_message
    submissions = []

    def held_drive(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return drive(*args, **kwargs)

    def tracked_submit(*args, **kwargs):
        submissions.append(args)
        return submit(*args, **kwargs)

    monkeypatch.setattr(service, "_drive", held_drive)
    monkeypatch.setattr(service, "submit_message", tracked_submit)
    job = cron.create_job(service, name="等待执行", prompt="回答", schedule="0 9 * * *")
    try:
        cron.run_now(service, job.job_id)
        assert entered.wait(timeout=5)
        assert cron.list_runs(service, job.job_id).runs[0].status == "running"
        with pytest.raises(ValueError, match="尚未结束|还没结束"):
            cron.run_now(service, job.job_id)
        service._sqlite_access(lambda: service._cron.update_job(job.job_id, next_run_at=_past()))
        assert cron.CronScheduler(service).tick() == 1
        assert cron.list_runs(service, job.job_id).total == 1
    finally:
        release.set()
    assert _wait(lambda: cron.list_runs(service, job.job_id).runs[0].status == "succeeded", timeout=10)
    assert len(submissions) == 1


@pytest.mark.parametrize("background", [False, True])
def test_graph_crash_does_not_leave_cron_running(real_service, monkeypatch, background):
    service = real_service(background=background, decision="chat", reply="不应成功")

    def crash(*_args, **_kwargs):
        raise RuntimeError("模拟执行中断")

    monkeypatch.setattr(service, "_drive", crash)
    job = cron.create_job(service, name="执行中断", prompt="执行", schedule="0 9 * * *")
    cron.run_now(service, job.job_id)
    assert _wait(lambda: cron.list_runs(service, job.job_id).runs[0].status != "running", timeout=10)
    assert cron.list_runs(service, job.job_id).runs[0].status == "failed"


def test_update_schedule_recomputes_next_run(tmp_path: Path) -> None:
    service, _store, _calls = _service(tmp_path)
    job = cron.create_job(service, name="早报", prompt="汇总", schedule="0 9 * * *")
    before = job.next_run_at
    updated = cron.update_job(service, job.job_id, schedule="0 21 * * *")
    assert updated.next_run_at != before
    assert datetime.fromisoformat(updated.next_run_at).hour == 21


def test_disable_then_enable_pushes_next_run_to_future(tmp_path: Path) -> None:
    service, _store, _calls = _service(tmp_path)
    job = cron.create_job(service, name="早报", prompt="汇总", schedule="0 9 * * *")

    paused = cron.update_job(service, job.job_id, enabled=False)
    assert paused.enabled is False

    resumed = cron.update_job(service, job.job_id, enabled=True)
    assert resumed.enabled is True
    assert datetime.fromisoformat(resumed.next_run_at) > datetime.now(timezone.utc).astimezone()


def test_run_now_does_not_wait_for_schedule(tmp_path: Path) -> None:
    service, store, _calls = _service(tmp_path)
    job = cron.create_job(service, name="现在就跑", prompt="跑", schedule="0 9 * * *")
    scheduled = job.next_run_at

    run = cron.run_now(service, job.job_id)
    assert run.status in {"running", "succeeded"}
    assert _wait(lambda: store.list_runs(job.job_id)[0][0].status == "succeeded")
    # 「立即触发」不改排期。
    assert store.get_job(job.job_id).next_run_at == scheduled


def test_missing_job_raises_lookup_error(tmp_path: Path) -> None:
    service, _store, _calls = _service(tmp_path)
    with pytest.raises(LookupError):
        cron.update_job(service, "job-nope", name="x")
    with pytest.raises(LookupError):
        cron.run_now(service, "job-nope")
    with pytest.raises(LookupError):
        cron.list_runs(service, "job-nope")


@pytest.mark.parametrize("row_factory", [None, sqlite3.Row], ids=["tuple", "row"])
def test_cron_store_round_trip_preserves_shared_connection(tmp_path, row_factory):
    """默认连接与 Row 连接均可持久化；定时任务不能修改共享连接的行格式。"""
    path = tmp_path / "store.sqlite"
    started = "2026-10-22T09:00:00+08:00"
    finished = "2026-10-22T09:01:00+08:00"
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = row_factory
        migrations.apply_migrations(connection)
        store = CronStore(connection)
        assert store.list_jobs() == []
        job = store.create_job(
            name="原任务", prompt="原提示", schedule="0 9 * * *", workspace_root=str(tmp_path),
            mode="agent", next_run_at=started, now=started,
        )
        assert job.enabled is True
        assert job.created_at == job.updated_at == job.next_run_at == started
        assert (job.last_run_at, job.last_status, job.last_error) == (None, None, None)
        updated = store.update_job(
            job.job_id, name="新任务", prompt="新提示", schedule="1 9 * * *", mode="chat",
            enabled=0, updated_at=finished,
        )
        assert (updated.name, updated.prompt, updated.schedule, updated.mode) == (
            "新任务", "新提示", "1 9 * * *", "chat",
        )
        assert updated.enabled is False
        assert store.due_jobs(finished) == []
        enabled = store.update_job(job.job_id, enabled=1)
        assert store.list_jobs() == store.due_jobs(finished) == [enabled]

        run = store.start_run(job.job_id, status="running", started_at=started)
        assert (run.session_id, run.finished_at, run.summary) == (None, None, None)
        assert store.has_running_run(job.job_id)
        store.attach_run_session(run.run_id, "session-1")
        store.finish_run(run.run_id, status="blocked", summary="审批拒绝", finished_at=finished)
        blocked = store.list_runs(job.job_id)[0][0]
        assert (blocked.job_id, blocked.session_id, blocked.status) == (
            job.job_id, "session-1", "blocked",
        )
        assert (blocked.started_at, blocked.finished_at, blocked.summary) == (
            started, finished, "审批拒绝",
        )
        assert not store.has_running_run(job.job_id)
        assert store.get_job(job.job_id).last_error == "审批拒绝"

        second = store.start_run(job.job_id, status="running", started_at=finished,
                                 session_id="session-2")
        store.finish_run(second.run_id, status="succeeded", summary="完成", finished_at=finished)
        persisted = store.get_job(job.job_id)
        assert persisted.workspace_root == str(tmp_path)
        assert persisted.updated_at == persisted.last_run_at == finished
        assert persisted.last_status == "succeeded" and persisted.last_error is None
        runs, total = store.list_runs(job.job_id)
        assert total == 2
        assert [entry.run_id for entry in runs] == [second.run_id, run.run_id]
        assert store.list_runs(job.job_id, offset=1, limit=1) == ([blocked], 2)
        assert connection.row_factory is row_factory
        expected_type = tuple if row_factory is None else sqlite3.Row
        assert isinstance(connection.execute("SELECT 1").fetchone(), expected_type)

    # 重开后使用生产默认行格式，验证记录没有依赖前一次连接的配置。
    with closing(sqlite3.connect(path)) as connection:
        store = CronStore(connection)
        assert store.get_job(job.job_id) == persisted
        assert store.list_runs(job.job_id) == (runs, total)
        assert store.delete_job(job.job_id)
        assert store.get_job(job.job_id) is None
        assert store.list_jobs() == []
        assert not store.delete_job(job.job_id)
        assert connection.row_factory is None

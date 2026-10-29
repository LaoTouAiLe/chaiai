"""T58：后台任务运行时与状态机（架构 §17.2）。

四组用例：

1. **状态机**：迁移表就是契约里画的那七条；终态没有出边；非法迁移被拒。
2. **登记与接续**：同会话最多一个非终态任务；审批恢复接上 ``waiting_user`` 的那个，不新建。
3. **两种调度时机**：``background=false`` 在调用线程内跑完并记账；``true`` 交给 worker，
   排队中的任务**真的**停在 ``queued``。
4. **失败闭合**：执行抛异常时任务留在 ``running``（不假装 ``failed``），且 worker 线程不被打死。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from local_agent.application.jobs import (
    _STATE_FOR_RESULT,
    LEGAL_TRANSITIONS,
    UNRESOLVED_DETAIL,
    JobRuntime,
    can_transition,
)
from local_agent.application.models import CommandResultView
from local_agent.exceptions import JobStoreError
from local_agent.persistence import migrations
from local_agent.persistence.job_store import (
    JOB_STATE_CANCELLED,
    JOB_STATE_COMPLETED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_REVIEW,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_WAITING_USER,
    JOB_STATES,
    TERMINAL_JOB_STATES,
    JobStore,
)


def _runtime(tmp_path: Path, *, background: bool) -> tuple[JobStore, JobRuntime]:
    """一个已迁移的聊天库 + 任务运行时（同库同连接，与会话索引、Checkpoint 一致）。

    连接按 `checkpoint._connect` 的口径开（``check_same_thread=False``），锁用**不可重入**的
    ``threading.Lock``——与生产同源（``SqliteSaver.lock`` 就是它）。用 RLock 会让"索引内部
    嵌套取锁"这种死锁在测试里看不出来（实测踩过一次）。
    """
    connection = sqlite3.connect(tmp_path / "chat.sqlite", check_same_thread=False)
    migrations.apply_migrations(connection)
    store = JobStore(connection, lock=threading.Lock())
    return store, JobRuntime(store, background=background, max_concurrent_jobs=1)


def _result(
    status: str = "completed",
    *,
    run_id: str | None = "run-1",
    session_id: str = "s1",
) -> CommandResultView:
    return CommandResultView(
        request_id="req-1",
        operation="submit_message",
        session_id=session_id,
        run_id=run_id,
        snapshot_id="ckpt-1",
        status=status,  # type: ignore[arg-type]
        session_state="completed",
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---- 1. 状态机 -------------------------------------------------------------


def test_transition_table_is_the_contract() -> None:
    """迁移表逐条对照架构 §17.2 的那张图；终态没有出边。"""
    assert set(LEGAL_TRANSITIONS) == set(JOB_STATES)
    # `queued → failed` 是重启收口用的（排队中的任务**能证明**什么都没跑，见 §17.5）；
    # 反向的 `queued → needs_review` 不合法——"未开始"不该退化成"需核对"。
    assert LEGAL_TRANSITIONS[JOB_STATE_QUEUED] == {
        JOB_STATE_RUNNING,
        JOB_STATE_CANCELLED,
        JOB_STATE_FAILED,
    }
    assert LEGAL_TRANSITIONS[JOB_STATE_RUNNING] == {
        JOB_STATE_WAITING_USER,
        JOB_STATE_COMPLETED,
        JOB_STATE_FAILED,
        JOB_STATE_CANCELLED,
        JOB_STATE_NEEDS_REVIEW,
    }
    assert LEGAL_TRANSITIONS[JOB_STATE_WAITING_USER] == {
        JOB_STATE_RUNNING,
        JOB_STATE_COMPLETED,
        JOB_STATE_FAILED,
        JOB_STATE_CANCELLED,
        JOB_STATE_NEEDS_REVIEW,
    }
    for terminal in TERMINAL_JOB_STATES:
        assert LEGAL_TRANSITIONS[terminal] == frozenset()
    # 同状态不算迁移（只更新 detail / run_id）
    assert can_transition(JOB_STATE_RUNNING, JOB_STATE_RUNNING)
    # 表里没有的组合一律拒绝
    assert not can_transition(JOB_STATE_QUEUED, JOB_STATE_WAITING_USER)
    assert not can_transition(JOB_STATE_QUEUED, JOB_STATE_NEEDS_REVIEW)
    assert not can_transition(JOB_STATE_COMPLETED, JOB_STATE_RUNNING)
    assert not can_transition(JOB_STATE_NEEDS_REVIEW, JOB_STATE_RUNNING)
    assert not can_transition(JOB_STATE_QUEUED, JOB_STATE_COMPLETED)


# ---- 2. 登记与接续 ---------------------------------------------------------


def test_begin_refuses_a_second_live_task_and_allows_a_new_one_after_terminal(
    tmp_path: Path,
) -> None:
    store, runtime = _runtime(tmp_path, background=False)

    job = runtime.begin(session_id="s1")
    assert job.state == JOB_STATE_QUEUED
    with pytest.raises(JobStoreError):
        runtime.begin(session_id="s1")
    # 另一个会话不受影响
    assert runtime.begin(session_id="s2").session_id == "s2"

    runtime.run_now(job, _result)
    assert store.get(job.job_id).state == JOB_STATE_COMPLETED
    assert runtime.active_for_session("s1") is None
    assert runtime.begin(session_id="s1").job_id != job.job_id


def test_awaiting_approval_parks_in_waiting_user_and_resume_reuses_the_task(
    tmp_path: Path,
) -> None:
    store, runtime = _runtime(tmp_path, background=False)

    job = runtime.begin(session_id="s1")
    runtime.run_now(job, lambda: _result("awaiting_approval"))

    parked = store.get(job.job_id)
    assert parked.state == JOB_STATE_WAITING_USER and not parked.is_terminal
    assert runtime.active_for_session("s1").job_id == job.job_id

    resumed = runtime.resume(session_id="s1")
    assert (resumed.job_id, resumed.state) == (job.job_id, JOB_STATE_RUNNING)
    runtime.run_now(resumed, _result)

    assert store.get(job.job_id).state == JOB_STATE_COMPLETED
    # 审批恢复**不新建**任务：一个会话先后只有一个任务
    assert [item.job_id for item in store.list_for_session("s1")] == [job.job_id]


def test_resume_without_a_waiting_task_backfills_one(tmp_path: Path) -> None:
    """v0.5 时代留下的待审批会话在索引里没有行：补建一行，不让用户的审批操作失败。"""
    store, runtime = _runtime(tmp_path, background=False)

    job = runtime.resume(session_id="s1")

    assert job.state == JOB_STATE_RUNNING
    assert [item.job_id for item in store.list_for_session("s1")] == [job.job_id]


# ---- 3. 两种调度时机 -------------------------------------------------------


def test_inline_path_settles_in_the_calling_thread(tmp_path: Path) -> None:
    store, runtime = _runtime(tmp_path, background=False)

    job = runtime.begin(session_id="s1")
    result = runtime.run_now(job, lambda: _result("completed", run_id="run-7"))

    assert result.status == "completed"
    stored = store.get(job.job_id)
    assert stored.state == JOB_STATE_COMPLETED
    assert stored.run_id == "run-7"
    assert stored.started_at is not None and stored.finished_at is not None
    # background=false 不启 worker：wait_for_idle 直接为真
    assert runtime.wait_for_idle(timeout=0.01)


def test_worker_path_settles_and_queued_is_a_real_state(tmp_path: Path) -> None:
    """一个 worker 忙着时，第二个会话的任务**真的**停在 ``queued``（这正是该状态的意义）。"""
    store, runtime = _runtime(tmp_path, background=True)
    gate = threading.Event()

    def blocked() -> CommandResultView:
        assert gate.wait(timeout=5)
        return _result("completed")

    first = runtime.begin(session_id="s1")
    runtime.dispatch(first, blocked)
    assert _wait_until(lambda: store.get(first.job_id).state == JOB_STATE_RUNNING)

    second = runtime.begin(session_id="s2")
    runtime.dispatch(second, _result)
    time.sleep(0.05)  # 观察窗口：唯一能看见 queued 的方式就是它还没被取出
    assert store.get(second.job_id).state == JOB_STATE_QUEUED

    gate.set()
    assert runtime.wait_for_idle(timeout=5)
    assert store.get(first.job_id).state == JOB_STATE_COMPLETED
    assert store.get(second.job_id).state == JOB_STATE_COMPLETED
    assert store.get(second.job_id).run_id == "run-1"
    runtime.close()


def test_close_stops_the_workers(tmp_path: Path) -> None:
    _store, runtime = _runtime(tmp_path, background=True)
    assert runtime.background

    runtime.close(timeout=5)
    runtime.close(timeout=5)  # 幂等


# ---- 4. 失败闭合 -----------------------------------------------------------


def test_exception_marks_needs_review_and_does_not_fake_failure(tmp_path: Path) -> None:
    """异常 → ``needs_review``（终态）：既不写 failed，也不留在 running。

    2026-10-20 改：原来留 running，只有进程重启时 T61 才收口，运行期就永远显示"正在执行"。
    """
    store, runtime = _runtime(tmp_path, background=False)

    def boom() -> CommandResultView:
        raise RuntimeError("工具炸了（副作用可能已经发生）")

    job = runtime.begin(session_id="s1")
    with pytest.raises(RuntimeError):
        runtime.run_now(job, boom)

    stored = store.get(job.job_id)
    assert stored.state == JOB_STATE_NEEDS_REVIEW, "记 failed 等于声称什么都没发生（§15.2）"
    assert stored.is_terminal and stored.finished_at is not None
    assert stored.detail == UNRESOLVED_DETAIL


def test_worker_survives_an_exception_and_keeps_serving(tmp_path: Path) -> None:
    store, runtime = _runtime(tmp_path, background=True)

    def boom() -> CommandResultView:
        raise RuntimeError("工具炸了")

    broken = runtime.begin(session_id="s1")
    runtime.dispatch(broken, boom)
    assert _wait_until(lambda: store.get(broken.job_id).detail == UNRESOLVED_DETAIL)

    # worker 线程没被打死：下一个任务照常完成
    healthy = runtime.begin(session_id="s2")
    runtime.dispatch(healthy, _result)
    assert runtime.wait_for_idle(timeout=5)
    assert store.get(healthy.job_id).state == JOB_STATE_COMPLETED
    runtime.close()


def test_resume_never_rewrites_a_terminal_row(tmp_path: Path) -> None:
    """会话只有终态任务时，``resume`` 补建新行，绝不把终态那行改回 ``running``。"""
    store, runtime = _runtime(tmp_path, background=False)

    job = runtime.begin(session_id="s1")
    runtime.run_now(job, _result)
    assert store.get(job.job_id).state == JOB_STATE_COMPLETED

    backfilled = runtime.resume(session_id="s1")

    assert backfilled.job_id != job.job_id
    assert backfilled.state == JOB_STATE_RUNNING
    assert store.get(job.job_id).state == JOB_STATE_COMPLETED


def test_cancelled_queue_item_releases_its_lock_without_running(tmp_path: Path) -> None:
    store, runtime = _runtime(tmp_path, background=True)
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    called: list[str] = []

    def blocker() -> CommandResultView:
        started.set()
        assert release.wait(5)
        return _result()

    def cancelled_work() -> CommandResultView:
        called.append("run")
        return _result(session_id="s2")

    def cleanup() -> None:
        called.append("cleanup")
        lock.release()

    try:
        runtime.dispatch(runtime.begin(session_id="s1"), blocker)
        assert started.wait(5)
        lock.acquire()
        cancelled = runtime.begin(session_id="s2")
        runtime.dispatch(cancelled, cancelled_work, on_done=cleanup)
        runtime.cancel(cancelled.job_id, detail="取消排队任务")
        release.set()
        assert runtime.wait_for_idle(5)
        assert called == ["cleanup"]
        assert not lock.locked()
        assert store.get(cancelled.job_id).state == JOB_STATE_CANCELLED
    finally:
        release.set()
        runtime.close(timeout=5)


def test_cancel_between_queue_read_and_start_keeps_worker_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, runtime = _runtime(tmp_path, background=True)
    entered, release = threading.Event(), threading.Event()
    original = runtime._execute
    called: list[str] = []

    def paused(job, work, *, reraise):  # type: ignore[no-untyped-def]
        if job.session_id == "s1":
            entered.set()
            assert release.wait(5)
        return original(job, work, reraise=reraise)

    monkeypatch.setattr(runtime, "_execute", paused)
    try:
        job = runtime.begin(session_id="s1")
        runtime.dispatch(job, lambda: called.append("run") or _result(),
                         on_done=lambda: called.append("cleanup"))
        assert entered.wait(5)
        runtime.cancel(job.job_id, detail="启动竞态取消")
        release.set()
        assert runtime.wait_for_idle(5)
        assert called == ["cleanup"]
        assert all(worker.is_alive() for worker in runtime._workers)
        next_job = runtime.begin(session_id="s2")
        runtime.dispatch(next_job, lambda: _result(session_id="s2"))
        assert runtime.wait_for_idle(5)
        assert store.get(next_job.job_id).state == JOB_STATE_COMPLETED
    finally:
        release.set()
        runtime.close(timeout=5)


def test_settlement_failure_still_cleans_up_and_keeps_worker_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, runtime = _runtime(tmp_path, background=True)
    original = runtime._settle
    broken = runtime.begin(session_id="s1")
    cleaned: list[str] = []

    def settle(job_id: str, result: CommandResultView) -> None:
        if job_id == broken.job_id:
            raise RuntimeError("结算异常")
        original(job_id, result)

    monkeypatch.setattr(runtime, "_settle", settle)
    try:
        runtime.dispatch(broken, _result, on_done=lambda: cleaned.append(broken.job_id))
        assert runtime.wait_for_idle(5)
        assert store.get(broken.job_id).detail == UNRESOLVED_DETAIL
        assert cleaned == [broken.job_id]
        healthy = runtime.begin(session_id="s2")
        runtime.dispatch(healthy, _result)
        assert runtime.wait_for_idle(5)
        assert store.get(healthy.job_id).state == JOB_STATE_COMPLETED
    finally:
        runtime.close(timeout=5)


def test_cancel_of_terminal_task_never_signals_the_next_token(tmp_path: Path) -> None:
    store, runtime = _runtime(tmp_path, background=False)
    old = runtime.begin(session_id="s1")
    runtime.run_now(old, _result)
    new = runtime.begin(session_id="s1")
    signals: list[str] = []

    assert runtime.cancel(
        old.job_id, detail="late timeout", before_cancel=lambda job: signals.append(job.job_id)
    ) is None
    assert signals == []
    assert store.get(new.job_id).state == JOB_STATE_QUEUED


def test_cancel_signal_and_settlement_share_the_same_transition_lock(tmp_path: Path) -> None:
    store, runtime = _runtime(tmp_path, background=False)
    job = store.create(session_id="s1", state=JOB_STATE_RUNNING)
    entered = threading.Event()
    release = threading.Event()
    settling = threading.Event()
    settled = threading.Event()
    errors: list[Exception] = []

    def signal(current):
        entered.set()
        assert release.wait(5)
        return "bound-at-cancellation"

    def cancel():
        try:
            runtime.cancel(job.job_id, detail="cancel", before_cancel=signal)
        except Exception as exc:
            errors.append(exc)
            raise

    def settle():
        settling.set()
        try:
            runtime._settle(job.job_id, _result())
        except Exception as exc:
            errors.append(exc)
            raise
        finally:
            settled.set()

    cancelling = threading.Thread(target=cancel)
    completing = threading.Thread(target=settle)
    cancelling.start()
    try:
        assert entered.wait(5)
        completing.start()
        assert settling.wait(5)
        assert not settled.is_set()
    finally:
        release.set()
        cancelling.join(5)
        if completing.ident is not None:
            completing.join(5)
    assert not errors
    assert not cancelling.is_alive() and not completing.is_alive()
    saved = store.get(job.job_id)
    assert saved.state == JOB_STATE_CANCELLED
    assert saved.run_id == "bound-at-cancellation"


# ---- 人工核对结案（§17.1 唯一例外，2026-10-23）-----------------------------
# 背景：`needs_review` 是终态且 `update_state()` 明文"终态不可改写"，于是任务一旦
# 落到这里就**永远出不去**——会话会一直挂在侧栏的「需人工核对」上（实机复现）。
# 例外只开在 `close_review()` 这一个入口。

def _review_job(store):
    job = store.create(session_id="s1")
    store.update_state(job.job_id, "running")
    store.update_state(job.job_id, "needs_review", detail="服务重启时正在执行")
    return job


def test_close_review_completes_the_task(tmp_path: Path):
    store, _ = _runtime(tmp_path, background=False)
    job = _review_job(store)

    closed = store.close_review(job.job_id, outcome="completed", detail="核对后确认已写完")

    assert closed.state == "completed"
    assert closed.finished_at is not None


def test_close_review_can_cancel(tmp_path: Path):
    store, _ = _runtime(tmp_path, background=False)
    job = _review_job(store)
    assert store.close_review(job.job_id, outcome="cancelled").state == "cancelled"


def test_close_review_rejects_other_outcomes(tmp_path: Path):
    store, _ = _runtime(tmp_path, background=False)
    job = _review_job(store)
    with pytest.raises(JobStoreError):
        store.close_review(job.job_id, outcome="running")


def test_close_review_only_accepts_needs_review(tmp_path: Path):
    """别的终态不能借这个入口被改写——例外只对 `needs_review` 生效。"""
    store, _ = _runtime(tmp_path, background=False)
    job = store.create(session_id="s1")
    store.update_state(job.job_id, "running")
    store.update_state(job.job_id, "completed")

    with pytest.raises(JobStoreError):
        store.close_review(job.job_id, outcome="cancelled")


def test_update_state_still_refuses_terminal_rewrite(tmp_path: Path):
    """`update_state` 的规则一个字没改：终态仍然不可改写。"""
    store, _ = _runtime(tmp_path, background=False)
    job = _review_job(store)
    with pytest.raises(JobStoreError):
        store.update_state(job.job_id, "completed")


def test_every_command_status_maps_to_a_job_state() -> None:
    """命令结局 → 任务状态必须齐全（架构 §17.2、§17.3）。

    2026-10-28 实证：新增 `awaiting_input` 时漏了这张表，同步提交在收尾处 KeyError，
    真实服务返回 500——单测的 harness 没有任务运行时照不到这条路径，所以在这里守住。
    """
    from typing import get_args

    from local_agent.application.models import CommandStatus

    # `running` 不在这张表里：它是"命令已被接受、任务还在跑"（§17.2），不经过收尾结算。
    assert set(_STATE_FOR_RESULT) == set(get_args(CommandStatus)) - {"running"}

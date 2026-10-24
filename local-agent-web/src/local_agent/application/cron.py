"""定时任务：cron 计算、CRUD 编排与调度线程（§21.5，2026-10-22 J2）。

三条设计约束（都来自契约，不是实现细节）：

1. **不补跑**：停机期间错过的运行一律丢弃，恢复时把 `next_run_at` 推到未来。
2. **不重叠**：同一任务上一次还是 `running` 就跳过本次，不排队。
3. **不阻塞调度**：触发后把执行丢到独立线程，主循环只负责算时间和记账。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from croniter import croniter

from local_agent.application.cron_models import (
    CronJobsView,
    CronJobView,
    CronRunsPageView,
    CronRunView,
)
from local_agent.application.projection import current_run_id
from local_agent.persistence.request_ledger import TERMINAL_STATUSES

if TYPE_CHECKING:
    from local_agent.application.models import CommandResultView
    from local_agent.application.service import ApplicationService
    from local_agent.persistence.cron_store import CronJob, CronRun

logger = logging.getLogger(__name__)

#: 扫描间隔。30 秒是"分钟级排期"与"不白烧 CPU"之间的折中。
DEFAULT_INTERVAL_SECONDS = 30.0
MAX_NAME = 80
MAX_PROMPT = 4000
MAX_RUNS_LIMIT = 100


def _now() -> datetime:
    """本机本地时间，带时区。用户写"每天 9 点"指的是他所在的 9 点（§21.5）。"""
    return datetime.now().astimezone()


def next_run_at(schedule: str, *, after: datetime | None = None) -> datetime:
    """下一个触发时刻。非法表达式直接 `ValueError`，由路由转 422。"""
    expression = (schedule or "").strip()
    if not expression or not croniter.is_valid(expression):
        raise ValueError(f"不是合法的 cron 表达式：{schedule!r}。形如 `0 9 * * *`（每天 9 点）。")
    return croniter(expression, after or _now()).get_next(datetime)


def _job_view(job: CronJob) -> CronJobView:
    return CronJobView(
        job_id=job.job_id,
        name=job.name,
        prompt=job.prompt,
        schedule=job.schedule,
        workspace_root=job.workspace_root,
        mode=job.mode,
        enabled=job.enabled,
        next_run_at=job.next_run_at,
        last_run_at=job.last_run_at,
        last_status=job.last_status,
        last_error=job.last_error,
    )


def _run_view(run: CronRun) -> CronRunView:
    return CronRunView(
        run_id=run.run_id,
        job_id=run.job_id,
        session_id=run.session_id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        summary=run.summary,
    )


def _validated(name: str, prompt: str, mode: str) -> tuple[str, str, str]:
    clean_name = (name or "").strip()
    clean_prompt = (prompt or "").strip()
    if not 1 <= len(clean_name) <= MAX_NAME:
        raise ValueError(f"任务名需 1–{MAX_NAME} 个字符。")
    if not 1 <= len(clean_prompt) <= MAX_PROMPT:
        raise ValueError(f"提示词需 1–{MAX_PROMPT} 个字符。")
    if mode not in ("chat", "agent"):
        raise ValueError("模式只能是 chat 或 agent。")
    return clean_name, clean_prompt, mode


def list_jobs(service: ApplicationService, *, scheduler_running: bool = False) -> CronJobsView:
    jobs = service._sqlite_access(lambda: service._cron.list_jobs())
    return CronJobsView(jobs=[_job_view(job) for job in jobs], scheduler_running=scheduler_running)


def create_job(
    service: ApplicationService,
    *,
    name: str,
    prompt: str,
    schedule: str,
    workspace_id: str | None = None,
    mode: str = "agent",
) -> CronJobView:
    clean_name, clean_prompt, clean_mode = _validated(name, prompt, mode)
    moment = _now()
    scheduled = next_run_at(schedule, after=moment)
    root = (service._path_for_workspace_id(workspace_id) if workspace_id
            else Path(service._workspace_root))
    job = service._sqlite_access(lambda: service._cron.create_job(
        name=clean_name,
        prompt=clean_prompt,
        schedule=schedule.strip(),
        workspace_root=str(Path(root).resolve()),
        mode=clean_mode,
        next_run_at=scheduled.isoformat(),
        now=moment.isoformat(),
    ))
    return _job_view(job)


def update_job(
    service: ApplicationService,
    job_id: str,
    *,
    name: str | None = None,
    prompt: str | None = None,
    schedule: str | None = None,
    mode: str | None = None,
    enabled: bool | None = None,
) -> CronJobView:
    current = service._sqlite_access(lambda: service._cron.get_job(job_id))
    if current is None:
        raise LookupError("任务不存在，可能已被删除。")
    moment = _now()
    fields: dict[str, Any] = {"updated_at": moment.isoformat()}
    if name is not None or prompt is not None or mode is not None:
        clean_name, clean_prompt, clean_mode = _validated(
            name if name is not None else current.name,
            prompt if prompt is not None else current.prompt,
            mode if mode is not None else current.mode,
        )
        if name is not None:
            fields["name"] = clean_name
        if prompt is not None:
            fields["prompt"] = clean_prompt
        if mode is not None:
            fields["mode"] = clean_mode
    if schedule is not None:
        fields["schedule"] = schedule.strip()
        # 改表达式必须重算；否则会按旧排期触发，用户以为改好了。
        fields["next_run_at"] = next_run_at(schedule, after=moment).isoformat()
    if enabled is not None:
        fields["enabled"] = 1 if enabled else 0
        if enabled:
            # 重新启用不补跑：把排期推到下一个未来时刻（§21.5「漏跑不补」）。
            fields["next_run_at"] = next_run_at(
                schedule if schedule is not None else current.schedule, after=moment
            ).isoformat()
    updated = service._sqlite_access(lambda: service._cron.update_job(job_id, **fields))
    if updated is None:
        raise LookupError("任务不存在，可能已被删除。")
    return _job_view(updated)


def delete_job(service: ApplicationService, job_id: str) -> None:
    removed = service._sqlite_access(lambda: service._cron.delete_job(job_id))
    if not removed:
        raise LookupError("任务不存在，可能已被删除。")


def list_runs(service: ApplicationService, job_id: str, *, offset: int = 0,
              limit: int = 20) -> CronRunsPageView:
    if service._sqlite_access(lambda: service._cron.get_job(job_id)) is None:
        raise LookupError("任务不存在，可能已被删除。")
    runs, total = service._sqlite_access(
        lambda: service._cron.list_runs(job_id, offset=offset, limit=limit)
    )
    return CronRunsPageView(runs=[_run_view(run) for run in runs], total=total,
                            offset=offset, limit=limit)


class CronScheduler:
    """进程内单线程扫描器（§21.5）。多进程由调用方的 filelock 保证只有一个在跑。"""

    def __init__(self, service: ApplicationService, *,
                 interval: float = DEFAULT_INTERVAL_SECONDS) -> None:
        self._service = service
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._guard:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="cron-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._guard:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception:
                logger.exception("定时任务扫描失败")

    def tick(self, *, now: datetime | None = None) -> int:
        """跑一轮扫描，返回到期任务数。可被测试直接调用，不必起线程。"""
        moment = now or _now()
        due = self._service._sqlite_access(lambda: self._service._cron.due_jobs(moment.isoformat()))
        for job in due:
            self._fire(job, moment)
        return len(due)

    # ---- 单个任务 -----------------------------------------------------
    def _fire(self, job: CronJob, moment: datetime) -> None:
        store = self._service._cron
        if self._service._sqlite_access(lambda: store.has_running_run(job.job_id)):
            # 不排队：把排期推到下一次，并留下一条可读的说明。
            self._advance(job, moment, note="上一次运行尚未结束，本次跳过。")
            return
        try:
            upcoming = next_run_at(job.schedule, after=moment).isoformat()
        except ValueError as exc:
            # 表达式失效（比如手改库）：停用而不是每 30 秒报错一次。
            reason = f"排期表达式失效，已自动停用：{exc}"
            self._service._sqlite_access(lambda: store.update_job(
                job.job_id,
                enabled=0,
                last_error=reason,
                updated_at=moment.isoformat(),
            ))
            return
        run = self._service._sqlite_access(lambda: store.start_run(
            job.job_id, status="running", started_at=moment.isoformat()))
        self._service._sqlite_access(lambda: store.update_job(
            job.job_id, next_run_at=upcoming, updated_at=moment.isoformat()))
        # 丢到独立线程：submit_message 是同步执行，留在主循环会卡住整个调度器。
        threading.Thread(target=self._execute, args=(job, run.run_id),
                         name=f"cron-run-{run.run_id}", daemon=True).start()

    def _advance(self, job: CronJob, moment: datetime, *, note: str) -> None:
        try:
            upcoming = next_run_at(job.schedule, after=moment).isoformat()
        except ValueError:
            upcoming = None
        self._service._sqlite_access(lambda: self._service._cron.update_job(
            job.job_id, next_run_at=upcoming, last_error=note, updated_at=moment.isoformat()))

    def _execute(self, job: CronJob, run_id: str) -> None:
        execute_job(self._service, job, run_id)


def _run_outcome(service: ApplicationService, result: CommandResultView) -> tuple[str, str]:
    """只用原请求的终态与同轮审批记账，不借用后来轮次的状态。"""
    session = service._session_or_raise(result.session_id)
    if result.status == "running":
        # 后台 worker 持有这把锁直到收尾；只等本会话，不阻塞调度扫描或其它任务。
        with service._lock_for(result.session_id):
            entry = service._sqlite_access(lambda: service._ledger.get(result.request_id))
            if entry is None or entry["status"] not in TERMINAL_STATUSES:
                raise RuntimeError("本次执行未留下可确认的终态，请打开会话核对。")
            result = service._replay(
                session, request_id=result.request_id, operation=result.operation, entry=entry,
            )

    if result.status == "failed":
        detail = result.error.message if result.error is not None else "请打开会话查看详情。"
        return "failed", f"执行失败：{detail}"[:500]
    if result.status == "cancelled":
        return "failed", "本次执行已取消。"
    if result.status != "completed":
        return "failed", "本次执行未正常结束，请打开会话核对。"

    view = service._read_checkpoint(session, result.snapshot_id)
    if view is None or not result.run_id or current_run_id(view.values) != result.run_id:
        raise RuntimeError("无法核对本次执行的结果快照与轮次，请打开会话核对。")
    rejected = any(
        isinstance(record, Mapping)
        and record.get("run_id") == result.run_id
        and record.get("decision") == "reject"
        for record in view.values.get("approval_history") or []
    )
    if rejected:
        return "blocked", "本轮有动作被审批拒绝；无人值守会话不等待批准，请打开会话查看详情。"
    return "succeeded", "已提交并执行完成。"


def execute_job(service: ApplicationService, job: CronJob, run_id: str) -> None:
    """建会话 → 提交提示词 → 记结果。调度线程与"立即触发"端点共用这一条路径。"""
    store = service._cron
    status, summary = "failed", "执行中断且没有留下原因。"
    try:
        session = service.create_session(
            title=job.name,
            workspace_root=Path(job.workspace_root),
            mode=job.mode,
            # 后台会话：不抢用户焦点，也不把用户正在看的会话置 idle（§21.5）。
            kind="scheduled",
            activate=False,
        )
        service._sqlite_access(lambda: store.attach_run_session(run_id, session.session_id))
        result = service.submit_message(session.session_id, job.prompt, f"cron-{run_id}")
        status, summary = _run_outcome(service, result)
    except Exception as exc:
        status, summary = "failed", f"{type(exc).__name__}: {exc}"[:500]
        logger.warning("定时任务执行失败：%s", job.name, exc_info=True)
    finally:
        service._sqlite_access(lambda: store.finish_run(
            run_id, status=status, summary=summary, finished_at=_now().isoformat()))


def run_now(service: ApplicationService, job_id: str) -> CronRunView:
    """立即触发一次（不等排期）。排期本身不动——这是"补一次"而不是"提前一次"。"""
    job = service._sqlite_access(lambda: service._cron.get_job(job_id))
    if job is None:
        raise LookupError("任务不存在，可能已被删除。")
    if service._sqlite_access(lambda: service._cron.has_running_run(job_id)):
        raise ValueError("这个任务上一次运行还没结束，等它跑完再触发。")
    run = service._sqlite_access(lambda: service._cron.start_run(
        job_id, status="running", started_at=_now().isoformat()))
    threading.Thread(target=execute_job, args=(service, job, run.run_id),
                     name=f"cron-run-{run.run_id}", daemon=True).start()
    return _run_view(run)

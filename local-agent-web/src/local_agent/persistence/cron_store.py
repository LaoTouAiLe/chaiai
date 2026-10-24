"""定时任务的持久化（§21.5，2026-10-22 J2）。

两张表：`cron_jobs`（任务定义与排期）与 `cron_runs`（每次运行的记录）。
这里只管存取，不碰 cron 解析、不碰调度——那两件事在 `application/cron.py`。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

_JOB_COLUMNS = (
    "job_id, name, prompt, schedule, workspace_root, mode, enabled,"
    " created_at, updated_at, next_run_at, last_run_at, last_status, last_error"
)
_RUN_COLUMNS = "run_id, job_id, session_id, status, started_at, finished_at, summary"

RUN_STATUSES = frozenset({"running", "succeeded", "failed", "blocked"})


@dataclass(frozen=True, slots=True)
class CronJob:
    job_id: str
    name: str
    prompt: str
    schedule: str
    workspace_root: str
    mode: str
    enabled: bool
    created_at: str
    updated_at: str
    next_run_at: str | None
    last_run_at: str | None
    last_status: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class CronRun:
    run_id: str
    job_id: str
    session_id: str | None
    status: str
    started_at: str
    finished_at: str | None
    summary: str | None


def _job(row: tuple[object, ...] | sqlite3.Row) -> CronJob:
    # 与 _JOB_COLUMNS 的显式列序对应；共享 Checkpoint 连接默认返回 tuple。
    return CronJob(
        job_id=str(row[0]),
        name=str(row[1]),
        prompt=str(row[2]),
        schedule=str(row[3]),
        workspace_root=str(row[4]),
        mode=str(row[5]),
        enabled=bool(row[6]),
        created_at=str(row[7]),
        updated_at=str(row[8]),
        next_run_at=None if row[9] is None else str(row[9]),
        last_run_at=None if row[10] is None else str(row[10]),
        last_status=None if row[11] is None else str(row[11]),
        last_error=None if row[12] is None else str(row[12]),
    )


def _run(row: tuple[object, ...] | sqlite3.Row) -> CronRun:
    # 与 _RUN_COLUMNS 的显式列序对应，也兼容调用方已有的 sqlite3.Row。
    return CronRun(
        run_id=str(row[0]),
        job_id=str(row[1]),
        session_id=None if row[2] is None else str(row[2]),
        status=str(row[3]),
        started_at=str(row[4]),
        finished_at=None if row[5] is None else str(row[5]),
        summary=None if row[6] is None else str(row[6]),
    )


class CronStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ---- 任务 ---------------------------------------------------------
    def list_jobs(self) -> list[CronJob]:
        rows = self._connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM cron_jobs ORDER BY created_at DESC"
        ).fetchall()
        return [_job(row) for row in rows]

    def get_job(self, job_id: str) -> CronJob | None:
        row = self._connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM cron_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return None if row is None else _job(row)

    def due_jobs(self, now_iso: str) -> list[CronJob]:
        """到期且启用的任务；`next_run_at` 为空的（比如刚修好表达式）也返回，交给上层补算。"""
        rows = self._connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM cron_jobs"
            " WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?)"
            " ORDER BY next_run_at IS NULL DESC, next_run_at ASC",
            (now_iso,),
        ).fetchall()
        return [_job(row) for row in rows]

    def create_job(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str,
        workspace_root: str,
        mode: str,
        next_run_at: str | None,
        now: str,
    ) -> CronJob:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        self._connection.execute(
            "INSERT INTO cron_jobs (job_id, name, prompt, schedule, workspace_root, mode,"
            " enabled, created_at, updated_at, next_run_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (job_id, name, prompt, schedule, workspace_root, mode, now, now, next_run_at),
        )
        self._connection.commit()
        created = self.get_job(job_id)
        assert created is not None
        return created

    def update_job(self, job_id: str, **fields: Any) -> CronJob | None:
        """只允许改白名单字段，避免把 `job_id`/`created_at` 写坏。"""
        allowed = {"name", "prompt", "schedule", "mode", "enabled", "next_run_at",
                   "last_run_at", "last_status", "last_error", "updated_at"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            self._connection.execute(
                f"UPDATE cron_jobs SET {assignments} WHERE job_id = ?",
                (*updates.values(), job_id),
            )
            self._connection.commit()
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> bool:
        cursor = self._connection.execute("DELETE FROM cron_jobs WHERE job_id = ?", (job_id,))
        self._connection.commit()
        return cursor.rowcount > 0

    # ---- 运行记录 -----------------------------------------------------
    def start_run(self, job_id: str, *, status: str, started_at: str,
                  session_id: str | None = None) -> CronRun:
        if status not in RUN_STATUSES:
            raise ValueError(f"未知的运行状态：{status}")
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        self._connection.execute(
            "INSERT INTO cron_runs (run_id, job_id, session_id, status, started_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, job_id, session_id, status, started_at),
        )
        self._connection.execute(
            "UPDATE cron_jobs SET last_run_at = ?, last_status = ?, updated_at = ? WHERE job_id = ?",
            (started_at, status, started_at, job_id),
        )
        self._connection.commit()
        row = self._connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM cron_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row is not None
        return _run(row)

    def attach_run_session(self, run_id: str, session_id: str) -> None:
        """会话建好之后补上关联；建会话失败时这一列保持空，运行记录仍然可读。"""
        self._connection.execute(
            "UPDATE cron_runs SET session_id = ? WHERE run_id = ?", (session_id, run_id)
        )
        self._connection.commit()

    def finish_run(self, run_id: str, *, status: str, summary: str | None,
                   finished_at: str) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"未知的运行状态：{status}")
        self._connection.execute(
            "UPDATE cron_runs SET status = ?, summary = ?, finished_at = ? WHERE run_id = ?",
            (status, summary, finished_at, run_id),
        )
        row = self._connection.execute(
            "SELECT job_id FROM cron_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is not None:
            self._connection.execute(
                "UPDATE cron_jobs SET last_status = ?, last_error = ?, updated_at = ? WHERE job_id = ?",
                (status, None if status == "succeeded" else summary, finished_at, str(row[0])),
            )
        self._connection.commit()

    def list_runs(self, job_id: str, *, offset: int = 0, limit: int = 20) -> tuple[list[CronRun], int]:
        total_row = self._connection.execute(
            "SELECT COUNT(*) FROM cron_runs WHERE job_id = ?", (job_id,)
        ).fetchone()
        total = int(total_row[0]) if total_row is not None else 0
        rows = self._connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM cron_runs WHERE job_id = ?"
            " ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()
        return [_run(row) for row in rows], total

    def has_running_run(self, job_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM cron_runs WHERE job_id = ? AND status = 'running' LIMIT 1", (job_id,)
        ).fetchone()
        return row is not None

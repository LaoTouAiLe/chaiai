"""持久任务索引（架构 §17.1，v0.6 T57）。

职责边界：

- **只回答"任务在哪一步"**：``jobs`` 表是任务的**索引**，业务状态真源仍是 Checkpoint，事件是它的
  单向投影（架构 §15.4、§17.1）。本模块不得被当作第二套任务真源，也不复制任何 Graph 业务字段。
- **与会话索引、Checkpoint 共用同一个 SQLite 连接与事务边界**（由 ``checkpoint.open_database`` 装配）：
  所以这里只接收一个**结构已建好**的 ``sqlite3.Connection``，自己不建库、不跑迁移
  （建表在 :mod:`local_agent.persistence.migrations` 的聊天库 v7）。
- **跨线程必须串行**（T58 起）：后台 worker 会在另一条线程上读写同一份索引，而它与会话索引、
  Checkpoint 共用**同一条连接**。因此调用方要传入与 Checkpoint 相同的那把锁（``lock=``），
  本模块的每个公开方法都在锁内执行——与 ``ApplicationService._sqlite_access`` 的口径一致
  （同一连接跨线程读写会踩掉彼此的事务，实测过 `cannot commit - no transaction is active`
  与 `sqlite3.InterfaceError`）。传入 ``None`` 时不做串行（只在单线程的单元测试里可接受）。
- **只管"取值合法"，不管"能不能这么迁"**：状态闭集与"终态不可改写"在这里（架构 §17.1 冻结）；
  "哪些迁移合法"是 Job Runtime 的规则（T58 的 ``application/jobs.py``）。

三者的分工（架构 §17.1）：一个**会话**可以先后有多个**任务**（``job_id``，跨提交 / 审批 / 重启存活），
每个任务由一至多次**单次运行**（既有 ``run_id``）执行；``attempt`` 记录实际驱动图的次数。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

from local_agent.exceptions import JobStoreError

#: 任务状态的常量与闭集**定义在叶子模块** :mod:`local_agent.job_state`：契约模块
#: （`application/models.py`）不许依赖持久化层，而两边都得引用同一份闭集，所以定义只能落在中间。
#: 这里把它们**再导出**一次（见 `__all__`），既有 `from ...persistence.job_store import JOB_STATES`
#: 之类的引用因此不需要改。
from local_agent.job_state import (
    JOB_STATE_CANCELLED,
    JOB_STATE_COMPLETED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_REVIEW,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_WAITING_USER,
    JOB_STATES,
    TERMINAL_JOB_STATES,
    JobState,
)

__all__ = [
    "JOB_STATES",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_COMPLETED",
    "JOB_STATE_FAILED",
    "JOB_STATE_NEEDS_REVIEW",
    "JOB_STATE_QUEUED",
    "JOB_STATE_RUNNING",
    "JOB_STATE_WAITING_USER",
    "TERMINAL_JOB_STATES",
    "Job",
    "JobState",
    "JobStore",
]

#: 查询列顺序与 :class:`Job` 字段顺序严格一致。
_JOB_COLUMNS = (
    "job_id, session_id, state, run_id, attempt, "
    "created_at, started_at, updated_at, finished_at, detail"
)
_JOB_PLACEHOLDERS = ", ".join("?" * 10)


@dataclass(frozen=True)
class Job:
    """一行任务索引记录（字段按架构 §17.1）。"""

    job_id: str
    session_id: str
    #: 状态取值来自闭集（`JobState`）；写侧由 ``JobStore`` 校验，读侧由库里的 CHECK 保证，
    #: 所以这里可以用精确类型而不是裸 ``str``——上层（DTO / 页面）不必再逐处断言。
    state: JobState
    run_id: str | None
    attempt: int
    created_at: str
    started_at: str | None
    updated_at: str
    finished_at: str | None
    detail: str | None

    @property
    def is_terminal(self) -> bool:
        """是否已进入终态（不可再改写）。"""
        return self.state in TERMINAL_JOB_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _state_of(value: object) -> JobState:
    """把库里读到的状态转成闭集类型（库层有 CHECK，越界值不该出现）。"""
    return cast("JobState", str(value))


def _row_to_job(row: tuple[object, ...]) -> Job:
    return Job(
        job_id=str(row[0]),
        session_id=str(row[1]),
        state=_state_of(row[2]),
        run_id=str(row[3]) if row[3] is not None else None,
        attempt=int(str(row[4])),
        created_at=str(row[5]),
        started_at=str(row[6]) if row[6] is not None else None,
        updated_at=str(row[7]),
        finished_at=str(row[8]) if row[8] is not None else None,
        detail=str(row[9]) if row[9] is not None else None,
    )


class JobStore:
    """``jobs`` 表的读写入口（架构 §17.1）。

    Args:
        connection: 与环境索引、Checkpoint **共用**的那条连接。
        lock: 与 Checkpoint 相同的那把锁（可重入）；``None`` 表示不串行（单线程测试）。
    """

    def __init__(self, connection: sqlite3.Connection, *, lock: Any | None = None) -> None:
        self._connection = connection
        self._lock = lock if lock is not None else nullcontext()

    def link_children(self, job_id: str, run_id: str, child_ids: Sequence[str]) -> None:
        """仅登记冻结身份；终态与执行证据仍只能来自 Checkpoint。"""
        if (not run_id or not child_ids or len(child_ids) > 3 or len(set(child_ids)) != len(child_ids)
                or list(child_ids) != [f"{run_id}:child:{index}" for index in range(1, len(child_ids) + 1)]):
            raise JobStoreError("非法父子身份")
        with self._lock, self._connection:
            parent = self._get_unlocked(job_id)
            if parent is None or (parent.run_id is not None and parent.run_id != run_id):
                raise JobStoreError("父任务不存在或轮次不符")
            old = self._connection.execute(
                "SELECT parent_run_id, child_id FROM job_children WHERE parent_job_id=? ORDER BY child_id", (job_id,)
            ).fetchall()
            desired = [(run_id, child_id) for child_id in child_ids]
            if old:
                if old != desired:
                    raise JobStoreError("已登记的父子身份不可覆盖")
                return
            self._connection.executemany(
                "INSERT INTO job_children(parent_job_id, parent_run_id, child_id) VALUES (?, ?, ?)",
                [(job_id, run_id, child_id) for child_id in child_ids],
            )

    def child_links(self, job_id: str) -> list[tuple[str, str]]:
        """只读身份投影，不据此判断子任务完成。"""
        with self._lock:
            rows = self._connection.execute(
                "SELECT parent_run_id, child_id FROM job_children WHERE parent_job_id=? ORDER BY child_id", (job_id,)
            ).fetchall()
            return [(str(run_id), str(child_id)) for run_id, child_id in rows]

    def create(
        self,
        *,
        session_id: str,
        state: str = JOB_STATE_QUEUED,
        job_id: str | None = None,
    ) -> Job:
        """登记一个新任务。

        Args:
            session_id: 任务所属会话；必须非空（任务不能脱离会话存在）。
            state: 初始状态，默认 ``queued``（"已登记、worker 尚未取出"）。
            job_id: 指定标识；缺省随机生成。调用方一般只在测试里指定。

        Raises:
            JobStoreError: 会话为空或状态取值越界。
        """
        if not session_id:
            raise JobStoreError("任务必须绑定一个会话")
        if state not in JOB_STATES:
            raise JobStoreError(f"未知任务状态：{state}")
        now = _now()
        record = Job(
            job_id=job_id or uuid4().hex,
            session_id=session_id,
            state=_state_of(state),
            run_id=None,
            attempt=1,
            created_at=now,
            started_at=None,
            updated_at=now,
            finished_at=None,
            detail=None,
        )
        with self._lock, self._connection:
            self._connection.execute(
                f"INSERT INTO jobs ({_JOB_COLUMNS}) VALUES ({_JOB_PLACEHOLDERS})",
                (
                    record.job_id,
                    record.session_id,
                    record.state,
                    record.run_id,
                    record.attempt,
                    record.created_at,
                    record.started_at,
                    record.updated_at,
                    record.finished_at,
                    record.detail,
                ),
            )
        return record

    def get(self, job_id: str) -> Job | None:
        """按 ``job_id`` 取一条记录；不存在返回 ``None``。"""
        with self._lock:
            return self._get_unlocked(job_id)

    def _get_unlocked(self, job_id: str) -> Job | None:
        """不加锁的读取，**只给已经持锁的调用方**用。

        Checkpoint 的锁是 `threading.Lock`（不可重入，见 `SqliteSaver.__init__`），所以本模块
        内部绝不能再嵌套获取——`update_state` 里必须用这个私有方法，否则会直接死锁（实测）。
        """
        row = self._connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _row_to_job(row) if row is not None else None

    def list_for_session(self, session_id: str) -> list[Job]:
        """某个会话的全部任务，按登记时间先后排列（"区分会话、任务与单次运行"的直接依据）。"""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE session_id = ? "
                "ORDER BY created_at ASC, job_id ASC",
                (session_id,),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_by_state(self, *states: str) -> list[Job]:
        """按状态筛选（用于重启恢复与页面任务中心）。"""
        if not states:
            return []
        unknown = [state for state in states if state not in JOB_STATES]
        if unknown:
            raise JobStoreError(f"未知任务状态：{', '.join(unknown)}")
        placeholders = ", ".join("?" * len(states))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE state IN ({placeholders}) "
                "ORDER BY updated_at ASC, job_id ASC",
                tuple(states),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_for_sessions(self, session_ids: Sequence[str]) -> list[Job]:
        """一次取多个会话的全部任务，按登记时间倒序（任务中心的聚合读取，§17.6）。

        为什么要按会话批量取：任务中心按**工作区**过滤，而工作区 ↔ 会话的对应关系在会话索引里
        （`sessions.workspace_root`），所以由调用方先取该工作区的会话，再用一次查询把任务捞出来——
        不在任务索引里做跨表 JOIN，也不逐会话发一条查询。

        计数由调用方在**全量结果**上算（不受 `limit` 影响），因此 limit 只截断要展示的那一段。
        """
        ids = [session_id for session_id in session_ids if session_id]
        if not ids:
            return []
        placeholders = ", ".join("?" * len(ids))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE session_id IN ({placeholders}) "
                "ORDER BY created_at DESC, job_id DESC",
                tuple(ids),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def update_state(
        self,
        job_id: str,
        state: str,
        *,
        run_id: str | None = None,
        detail: str | None = None,
    ) -> Job:
        """把任务推进到 ``state``，返回更新后的记录。

        **只做两件事的校验**（架构 §17.1）：``state`` 在闭集内，且**终态不可改写**。
        "哪些迁移合法"属 Job Runtime（T58）。副作用：首次进入 ``running`` 时写 ``started_at``，
        进入任一终态时写 ``finished_at``。

        Args:
            job_id: 目标任务。
            state: 目标状态；终态一经写入即不可再改。
            run_id: 本次实际驱动图的轮次；``None`` 表示不改写已记录的值。
            detail: 终态原因（失败 / 取消 / 需核对的说明）；``None`` 表示不改写已记录的值。

        Raises:
            JobStoreError: 任务不存在、状态越界，或目标任务是终态。
        """
        if state not in JOB_STATES:
            raise JobStoreError(f"未知任务状态：{state}")
        with self._lock:
            current = self._get_unlocked(job_id)
            if current is None:
                raise JobStoreError(f"任务不存在：{job_id}")
            if current.is_terminal:
                raise JobStoreError(f"任务已处于终态 {current.state}，不可改写")
            now = _now()
            started_at = current.started_at
            if started_at is None and state == JOB_STATE_RUNNING:
                started_at = now
            finished_at = now if state in TERMINAL_JOB_STATES else None
            with self._connection:
                self._connection.execute(
                    "UPDATE jobs SET state = ?, run_id = COALESCE(?, run_id), "
                    "started_at = ?, updated_at = ?, finished_at = ?, detail = COALESCE(?, detail) "
                    "WHERE job_id = ?",
                    (state, run_id, started_at, now, finished_at, detail, job_id),
                )
            updated = self._get_unlocked(job_id)
        if updated is None:  # pragma: no cover - 同一事务内刚写过的行不该消失
            raise JobStoreError(f"任务更新后读取失败：{job_id}")
        return updated

    def close_review(self, job_id: str, *, outcome: str, detail: str | None = None) -> Job:
        """人工核对后结案：`needs_review → completed | cancelled`（§17.1 唯一例外）。

        **为什么需要单独的入口**：`needs_review` 是终态，而 `update_state()` 明文
        「终态不可改写」。但 `needs_review` 的语义是"服务重启后证明不了结局"，它
        必须由人收尾——此前**没有任何路径能离开它**，任务会把会话永远钉在侧栏的
        「需人工核对」上（2026-10-23 实机：点了审批、本轮已结束，那行还红着）。

        把例外收进这个方法名里，`update_state()` 的规则一个字不改：
        想改结局只能显式调用它，而它只对 `needs_review` 生效。

        Args:
            job_id: 目标任务。
            outcome: `completed`（核对后视为已完成）或 `cancelled`（确认没做完 / 放弃）。
            detail: 结案理由，写进 `detail` 备查。

        Raises:
            JobStoreError: 任务不存在、不在 `needs_review`、或 outcome 越界。
        """
        if outcome not in (JOB_STATE_COMPLETED, JOB_STATE_CANCELLED):
            raise JobStoreError(f"结案只能转 completed / cancelled，收到 {outcome}")
        with self._lock:
            current = self._get_unlocked(job_id)
            if current is None:
                raise JobStoreError(f"任务不存在：{job_id}")
            if current.state != JOB_STATE_NEEDS_REVIEW:
                raise JobStoreError(
                    f"只有 needs_review 的任务可以人工结案，当前是 {current.state}")
            now = _now()
            with self._connection:
                self._connection.execute(
                    "UPDATE jobs SET state = ?, updated_at = ?, finished_at = ?, "
                    "detail = COALESCE(?, detail) WHERE job_id = ?",
                    (outcome, now, now, detail, job_id),
                )
            updated = self._get_unlocked(job_id)
        if updated is None:  # pragma: no cover - 同一事务内刚写过的行不该消失
            raise JobStoreError(f"任务结案后读取失败：{job_id}")
        return updated

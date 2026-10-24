"""后台任务运行时（架构 §17.2，v0.6 T58）。

职责边界：

- **状态机只有这一处**：合法迁移表在本模块；``persistence.job_store`` 只管"取值合法 + 终态不可改写"。
- **单一执行路径**：``background=false`` 与 ``true`` 的区别只是**在哪个线程跑同一段工作**
  （`ApplicationService._guarded` 包起来的那次执行），不产生第二份图执行代码。
- **任务索引只回答"任务在哪一步"**：业务状态真源仍是 Checkpoint，事件是它的单向投影（§17.1）。
- **失败闭合**：执行中抛异常时**不得**把任务记成 ``failed``——副作用可能已经发生，记 ``failed``
  等于声称"什么都没发生"（§15.2）。这时任务直接落 ``needs_review``（终态），``detail`` 写明待核对。
  **2026-10-20 由"留在 running"改来**：原来只有进程重启时 T61 才会收口，运行期一旦抛异常，
  任务中心就永远显示"正在执行"、那个会话也开不出新任务（实测卡了 28 分钟）。``needs_review``
  本来就是为"可能已部分执行、证不了终态"设计的终态，页面也有对应展示，语义没有变化。
  台账（``request_ledger``）仍保持"执行中"，必须由用户核对后显式结束——那是安全边界，不是页面状态。

**单飞由谁承担**（§17.2）：``background=false`` 时由执行锁承担（一次提交在返回前就跑完，不存在
排队中的任务）；``true`` 时锁不够用——任务可能还在 ``queued`` 而没有任何线程持有锁，所以由
**任务索引**承担（同一会话最多一个非终态任务）。两者是同一语义在不同调度下的实现，不是两套规则。

本任务不做：取消的触发与进程终止（T60）、**恢复与"需人工核对"的判定**（T61，落在
``application/recovery.py``：判定是纯函数，本模块只提供把判定写进索引的 ``recover``）、页面（T62）、
多进程 / 持久队列。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence

from local_agent.application.models import CommandResultView
from local_agent.exceptions import JobStoreError
from local_agent.persistence.job_store import (
    JOB_STATE_CANCELLED,
    JOB_STATE_COMPLETED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_REVIEW,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_WAITING_USER,
    Job,
    JobStore,
)

logger = logging.getLogger(__name__)

#: 合法迁移表（架构 §17.2 冻结；T61 按"先改契约"补上恢复用到的边）。
#: 表里没有的组合一律拒绝；终态的出边为空。
#:
#: 两条口子值得单说：
#:
#: - ``queued → failed``：重启恢复用。**排队中的任务可以证明什么都没跑**（worker 取出前先写
#:   ``running``，所以停在 ``queued`` 就是图从未启动），因此它是可安全收口的那一类（§17.5）。
#:   反向的 ``queued → needs_review`` 不合法——"未开始"是能证明的，不该退化成"需核对"。
#: - ``running / waiting_user → needs_review``：重启恢复用。进程没了、Checkpoint 又证明不了终态。
LEGAL_TRANSITIONS: Mapping[str, frozenset[str]] = {
    JOB_STATE_QUEUED: frozenset(
        {JOB_STATE_RUNNING, JOB_STATE_CANCELLED, JOB_STATE_FAILED}
    ),
    JOB_STATE_RUNNING: frozenset(
        {
            JOB_STATE_WAITING_USER,
            JOB_STATE_COMPLETED,
            JOB_STATE_FAILED,
            JOB_STATE_CANCELLED,
            JOB_STATE_NEEDS_REVIEW,
        }
    ),
    JOB_STATE_WAITING_USER: frozenset(
        {
            JOB_STATE_RUNNING,
            JOB_STATE_COMPLETED,
            JOB_STATE_FAILED,
            JOB_STATE_CANCELLED,
            JOB_STATE_NEEDS_REVIEW,
        }
    ),
    JOB_STATE_COMPLETED: frozenset(),
    JOB_STATE_FAILED: frozenset(),
    JOB_STATE_CANCELLED: frozenset(),
    JOB_STATE_NEEDS_REVIEW: frozenset(),
}

#: 一次执行的命令结果 → 任务状态（架构 §17.2）。``awaiting_approval`` 停在 ``waiting_user``，
#: 由审批恢复把它推回 ``running``，因此它不是终态。
#:
#: ``cancelled``（2026-10-13 追加）：这一轮在运行中被取消并已收敛（§17.4 的第三个动作），
#: 于是**这一轮的结局**就是取消——即使这次提交本身不是从 `cancel_task` 进来的（例如取消发生在
#: 图内部、或令牌在装配前就已触发）。落成 `cancelled` 而不是 `completed`/`failed`：前者会假装
#: 任务做完了，后者会把"被中止"说成"执行失败"。
_STATE_FOR_RESULT: Mapping[str, str] = {
    "completed": JOB_STATE_COMPLETED,
    "awaiting_approval": JOB_STATE_WAITING_USER,
    "failed": JOB_STATE_FAILED,
    "cancelled": JOB_STATE_CANCELLED,
}

#: 任务状态 → 事件类型（架构 §17.3）。**每个状态恰好一个类型**：
#: `completed` / `failed` / `needs_review` 共用 `job_completed`（载荷里的 `state` 区分，`detail`
#: 带原因）——三者都是"任务走到了它的结局"，页面据 `state` 决定怎么呈现；而"取消"有独立类型，
#: 因为它是用户发起的、页面要单独提示。测试用它对照 `JOB_STATES` 守住"不重不漏"。
JOB_EVENT_FOR_STATE: Mapping[str, str] = {
    JOB_STATE_QUEUED: "job_queued",
    JOB_STATE_RUNNING: "job_progress",
    JOB_STATE_WAITING_USER: "job_waiting",
    JOB_STATE_COMPLETED: "job_completed",
    JOB_STATE_FAILED: "job_completed",
    JOB_STATE_CANCELLED: "task_cancelled",
    JOB_STATE_NEEDS_REVIEW: "job_completed",
}

#: 执行异常时的说明。任务落 ``needs_review``：既不声称"什么都没发生"（§15.2），
#: 也不再永远挂在"正在执行"上（2026-10-20）。
UNRESOLVED_DETAIL = (
    "执行中发生异常，副作用是否已经发生尚未核对（按 §15.2 由人工核对）。"
    "任务已结束并标为需核对，不会自动重跑；核对后重新提交就是新任务。"
)

_WAIT_STEP_SECONDS = 0.01


def can_transition(current: str, target: str) -> bool:
    """``current → target`` 是否合法（架构 §17.2）。同状态不算迁移，返回 ``True``。"""
    if current == target:
        return True
    return target in LEGAL_TRANSITIONS.get(current, frozenset())


class JobRuntime:
    """本机单进程任务运行时（架构 §17.2）。

    ``background=false`` 时它退化为"调用线程内执行 + 索引记账"——与 v0.5 的提交行为一致；
    ``true`` 时同一段工作被交给 ``max_concurrent_jobs`` 个 worker 线程。
    """

    def __init__(
        self,
        store: JobStore,
        *,
        background: bool,
        max_concurrent_jobs: int = 1,
        on_transition: Callable[[Job | None, Job], None] | None = None,
    ) -> None:
        if max_concurrent_jobs < 1:
            raise JobStoreError("max_concurrent_jobs 必须 >= 1")
        self._store = store
        self._background = background
        self._on_transition = on_transition
        self._queue: queue.Queue[
            tuple[str, Callable[[], CommandResultView], Callable[[], None] | None] | None
        ] = queue.Queue()
        self._state_lock = threading.RLock()
        self._workers: list[threading.Thread] = []
        self._closed = False
        if background:
            for index in range(max_concurrent_jobs):
                thread = threading.Thread(
                    target=self._loop, name=f"job-worker-{index + 1}", daemon=True
                )
                thread.start()
                self._workers.append(thread)

    @property
    def background(self) -> bool:
        """是否把执行交给 worker（架构 §17.2 的 `jobs.background`）。"""
        return self._background

    def set_transition_listener(self, listener: Callable[[Job | None, Job], None] | None) -> None:
        """注册状态迁移监听器（架构 §17.3）。

        `before` 为 `None` 表示**这次是任务的诞生**（登记即进入 `queued`，此前没有任何状态）。
        监听器**只负责把迁移投影成事件**：它抛出的异常会被吞掉并记日志——事件是投影，投影失败
        不能反过来把任务搞坏（迁移已经落库了，事实比事件重要）。
        """
        self._on_transition = listener

    # ---- 任务登记 ----------------------------------------------------------

    def begin(self, *, session_id: str) -> Job:
        """登记一个新任务；同一会话已有非终态任务时拒绝。

        Raises:
            JobStoreError: 会话已有非终态任务（调用方据此回 `SESSION_BUSY`）。
        """
        active = self.active_for_session(session_id)
        if active is not None:
            raise JobStoreError(f"会话 {session_id} 已有非终态任务 {active.job_id}（{active.state}）")
        created = self._store.create(session_id=session_id)
        # 登记即进入 queued：这是任务生命周期的第一次迁移（`before` 为空），必须有对应事件。
        self._notify(None, created)
        return created

    def resume(self, *, session_id: str) -> Job:
        """接上"等待用户"的任务（审批恢复），把它推回 ``running``。

        找不到等待中的任务时**补建一行**（``running``）：v0.5 时代留下的待审批会话在索引里没有行，
        让用户的操作因为"索引里没有记录"而失败不可接受；补建不伪造任何状态。
        """
        waiting = [
            job
            for job in self._store.list_for_session(session_id)
            if job.state == JOB_STATE_WAITING_USER
        ]
        if not waiting:
            backfilled = self._store.create(session_id=session_id, state=JOB_STATE_RUNNING)
            self._notify(None, backfilled)
            return backfilled
        return self._advance(waiting[-1].job_id, JOB_STATE_RUNNING)

    def jobs_in_flight(self) -> list[Job]:
        """所有还没走到终态的任务（超时看守与重启收口用）。"""
        return self._store.list_by_state(
            JOB_STATE_QUEUED, JOB_STATE_RUNNING, JOB_STATE_WAITING_USER
        )

    def list_for_session(self, session_id: str) -> list[Job]:
        """某个会话的全部任务，按登记先后排列（重启收口核对用，§17.1）。"""
        return self._store.list_for_session(session_id)

    def list_for_sessions(self, session_ids: Sequence[str]) -> list[Job]:
        """一次取多个会话的任务，按登记时间倒序（任务中心按工作区聚合，§17.6）。"""
        return self._store.list_for_sessions(session_ids)

    def job_id_for_run(self, session_id: str, run_id: str) -> str | None:
        """这次运行属于哪个任务（同 id 重放时用来**复原任务标识**）。

        台账只存 `result_ref`（Checkpoint 标识）与 `run_id`，不存 `job_id`；幂等重放要给出
        与原请求一致的引用，只能反查索引——按"会话 + 轮次"找，命中即原任务。
        """
        for job in self._store.list_for_session(session_id):
            if job.run_id == run_id:
                return job.job_id
        return None

    def active_for_session(self, session_id: str) -> Job | None:
        """该会话当前的非终态任务（没有则 ``None``）。同会话至多一条。"""
        active = [job for job in self._store.list_for_session(session_id) if not job.is_terminal]
        return active[-1] if active else None

    # ---- 调度 -------------------------------------------------------------

    def index_children(self, session_id: str, run_id: str, child_ids: list[str]) -> None:
        """执行前登记父子身份，沿原任务迁移锁及同连接锁串行。"""
        with self._state_lock:
            parent = self.active_for_session(session_id)
            if parent is None:
                raise JobStoreError("没有可绑定的父任务")
            self._store.link_children(parent.job_id, run_id, child_ids)

    def child_links(self, job_id: str) -> list[tuple[str, str]]:
        """仅返回父子身份，完成证明须读 Checkpoint。"""
        return self._store.child_links(job_id)

    def run_now(self, job: Job, work: Callable[[], CommandResultView]) -> CommandResultView:
        """在**调用线程**内跑完这次执行（``background=false`` 的路径）。"""
        result = self._execute(job, work, reraise=True)
        assert result is not None  # reraise=True 时只会返回结果或抛出
        return result

    def dispatch(
        self,
        job: Job,
        work: Callable[[], CommandResultView],
        *,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """把这次执行交给 worker（``background=true`` 的路径），立即返回。"""
        if not self._background:
            raise JobStoreError("dispatch 只在 jobs.background=true 时可用")
        if self._closed:
            raise JobStoreError("任务运行时已关闭")
        self._queue.put((job.job_id, work, on_done))

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """等队列与在跑的任务都结束（收尾与测试用）。返回是否已空闲。"""
        if not self._background:
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(_WAIT_STEP_SECONDS)
        return True

    def close(self, timeout: float | None = None) -> None:
        """停掉 worker（装配层收尾与测试用）。未跑完的任务留在索引里，由 T61 处理。"""
        if self._closed:
            return
        self._closed = True
        if not self._background:
            return
        for _ in self._workers:
            self._queue.put(None)
        for thread in self._workers:
            thread.join(timeout=timeout)
        self._workers.clear()

    # ---- 内部 -------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                job_id, work, _on_done = item
                job = self._store.get(job_id)
                if job is None or job.is_terminal:
                    continue
                # worker 线程上不能抛：抛出去会把线程带走，任务永远停在 running。
                self._execute(job, work, reraise=False)
            except Exception:
                logger.exception("后台任务调度异常；worker 继续处理后续任务")
            finally:
                if item is not None and item[2] is not None:
                    try:
                        item[2]()
                    except Exception:
                        logger.exception("后台任务 %s 的清理回调失败", item[0])
                self._queue.task_done()

    def _execute(
        self, job: Job, work: Callable[[], CommandResultView], *, reraise: bool
    ) -> CommandResultView | None:
        """跑一次执行并落任务状态。异常时收口到 ``needs_review``（见模块 docstring）。"""
        try:
            with self._state_lock:
                current = self._store.get(job.job_id)
                if current is None:
                    raise JobStoreError(f"任务不存在：{job.job_id}")
                # 取队列状态后可能刚被取消。启动与取消在同一个互斥区内判定。
                if current.is_terminal and not reraise:
                    return None
                if current.state == JOB_STATE_QUEUED:
                    job = self._advance(job.job_id, JOB_STATE_RUNNING)
            result = work()
            self._settle(job.job_id, result)
            return result
        except Exception:
            try:
                self._mark_needs_review(job.job_id)
            except Exception:
                logger.exception("后台任务 %s 异常后的核对说明写入失败", job.job_id)
            if reraise:
                raise
            logger.exception("后台任务 %s 执行异常；任务已标为需人工核对", job.job_id)
            return None

    def close_review(self, job_id: str, *, outcome: str,
                     detail: str | None = None) -> Job:
        """人工核对结案（§17.1 唯一例外）：`needs_review → completed | cancelled`。

        **不走 `_advance`**：那条路的终点是 `store.update_state`，而它明文拒绝终态改写。
        这里直接落到 `store.close_review`——例外只开在那一处，别的路径照旧不能改结局。
        """
        with self._state_lock:
            job = self._store.close_review(job_id, outcome=outcome, detail=detail)
            notify = self._on_transition
        if notify is not None:
            notify(None, job)
        return job

    def cancel(
        self,
        job_id: str,
        *,
        detail: str,
        before_cancel: Callable[[Job], str | None] | None = None,
    ) -> Job | None:
        """把任务推到终态 ``cancelled``（架构 §17.4）。

        **幂等**：任务已经是终态时返回 `None`，一个字节都不改、也不发事件——取消不撤销已发生的副作用，
        也不允许把已经结束的任务"改写成被取消"。
        `before_cancel` 只对仍活动的目标执行，在同一迁移锁内置信号并返回已核实的轮次。
        因此旧任务的迟到取消不能取得同会话下一轮的新令牌。
        """
        with self._state_lock:
            job = self._store.get(job_id)
            if job is None or job.is_terminal:
                return None
            run_id = before_cancel(job) if before_cancel is not None else job.run_id
            return self._advance(job_id, JOB_STATE_CANCELLED, detail=detail, run_id=run_id)

    def recover(self, job_id: str, *, target: str, detail: str | None = None) -> Job:
        """把**重启前遗留**的非终态任务收口到 ``target``（架构 §17.5，v0.6 T61）。

        与 :meth:`cancel` 的分工：取消是**用户意图**，这里是**代码按证据**给出的判定（判定本身在
        ``application/recovery.py`` 的纯函数里）。它同样**不重提图输入、不执行工具、不改写终态**：
        迁移合法性仍由 :meth:`_advance` 的表把关，终态由 ``JobStore`` 拒绝改写。
        """
        return self._advance(job_id, target, detail=detail)

    def _settle(self, job_id: str, result: CommandResultView) -> None:
        with self._state_lock:
            job = self._store.get(job_id)
            if job is None or job.is_terminal:
                # 任务已被取消：终态不可改写，也不重复发事件（§17.4）。
                return
            self._advance(job_id, _STATE_FOR_RESULT[str(result.status)], run_id=result.run_id)

    def _mark_needs_review(self, job_id: str) -> None:
        """异常后把任务收口到 ``needs_review``：**不写 failed**（副作用可能已经发生），
        但必须是终态——否则页面会永远显示"正在执行"，会话也开不出新任务。"""
        with self._state_lock:
            job = self._store.get(job_id)
            if job is None or job.is_terminal:
                return
            if can_transition(job.state, JOB_STATE_NEEDS_REVIEW):
                self._advance(job_id, JOB_STATE_NEEDS_REVIEW, detail=UNRESOLVED_DETAIL)
                return
            # 理论上到不了这里（执行前已推 running）；真到了也不猜状态，只补说明。
            self._advance(job_id, job.state, detail=UNRESOLVED_DETAIL)

    def _advance(
        self,
        job_id: str,
        target: str,
        *,
        run_id: str | None = None,
        detail: str | None = None,
    ) -> Job:
        """校验迁移合法性后写入索引。同状态只更新 ``detail`` / ``run_id``（不算迁移）。"""
        with self._state_lock:
            current = self._store.get(job_id)
            if current is None:
                raise JobStoreError(f"任务不存在：{job_id}")
            if not can_transition(current.state, target):
                raise JobStoreError(f"非法任务状态迁移：{current.state} → {target}")
            updated = self._store.update_state(job_id, target, run_id=run_id, detail=detail)
            if target != current.state:
                self._notify(current, updated)
            return updated

    def _notify(self, before: Job | None, after: Job) -> None:
        """把一次**真实迁移**交给监听器（只改 ``detail`` 的同状态写入不算迁移，不发事件）。"""
        listener = self._on_transition
        if listener is None:
            return
        try:
            listener(before, after)
        except Exception:
            # 投影失败不能反过来把任务搞坏：迁移已经落库，事实比事件重要。
            logger.exception("任务 %s 的状态迁移事件投影失败（迁移已落库）", after.job_id)

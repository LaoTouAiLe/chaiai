"""重启后的任务分类（架构 §17.5，v0.6 T61）。

服务重启后，任务索引里可能留下 ``queued`` / ``running`` / ``waiting_user`` 三种**非终态**任务。
它们必须被分成三类，且**绝不自动假定恰好执行一次**：

=====================  ==================  ============================================
判定                    写入的状态           凭什么
=====================  ==================  ============================================
``resumable``          不变 / `waiting_user`  Checkpoint 里有**活的挂起**（这一轮在等用户）
``unstarted``          `failed`             任务是 ``queued``：worker 取出前先写 ``running``
``provable``           `completed`/`failed`  可证 run == Checkpoint 当前 run，且该轮已有终态
``needs_review``       `needs_review`        以上都证不了：**可能已部分执行**
=====================  ==================  ============================================

判定是**纯函数**（:func:`classify_interrupted_job`）：不读库、不写库、不碰图；I/O 与写索引在
``ApplicationService.recover_interrupted_jobs``。这样"三类可区分"能用穷举用例钉住，
而不是靠一段只能在集成环境里跑的代码。

**证明的纪律与 §15.2 逐字一致**：`NULL` 是"不知道"，不是通配符。索引里没绑定 run、或 Checkpoint
拿不到当前 run 时**证明不了**，一律落 `needs_review`——把"不知道"当成匹配，就会把**上一轮**的终态
记到这一轮头上。

**证不了的东西也写在这里**：重启窗口内**已启动的进程树是否真的死了**无法从台账或 Checkpoint 证明
（跨平台、也跨进程，§17.4 只保证"取消时"终止）。所以 `needs_review` 的说明里必须出现
"可能仍有残留进程"，交给人工核对。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from local_agent.persistence.job_store import (
    JOB_STATE_COMPLETED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_REVIEW,
    JOB_STATE_QUEUED,
    JOB_STATE_WAITING_USER,
    Job,
)
from local_agent.workflow_contracts import READONLY_RESEARCH

#: 一轮的**终态**：``task_status`` 落到其中之一，就说明"这一轮已经有结局"（§15.2 的恢复口径）。
#: 定义在恢复模块里，因为它只被"能否证明终态"这一件事用到（服务层的 `_recover` 也用它）。
TASK_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "failed"})

#: 判定取值（三类 + 一类"可恢复"）。写进 `detail` 之外的地方会进测试与事件载荷，因此固定。
VERDICT_RESUMABLE: Final[str] = "resumable"
VERDICT_UNSTARTED: Final[str] = "unstarted"
VERDICT_PROVABLE: Final[str] = "provable"
VERDICT_NEEDS_REVIEW: Final[str] = "needs_review"

#: 各类判定的说明（给用户看，不含内部标识与工具名——§15.8 的字段门槛）。
UNSTARTED_DETAIL: Final[str] = (
    "服务重启时这个任务还没开始执行（没有任何工具副作用），已按失败收口，可以直接重新提交。"
)
RESUMABLE_DETAIL: Final[str] = (
    "服务重启前这一轮停在等待用户决定；任务已回到等待状态，可以继续处理待审批的操作。"
)
PROVABLE_DETAIL: Final[str] = (
    "服务重启后按 Checkpoint 核对到这一轮已有结局，已补齐任务结果（没有重新执行任何工具）。"
)
NEEDS_REVIEW_DETAIL: Final[str] = (
    "服务重启时这个任务正在执行，无法证明它跑到哪一步：可能已部分执行（已写出的内容不会回滚）、"
    "可能仍有残留进程。不会自动重跑，请人工核对后重新提交。"
)
UNREADABLE_DETAIL: Final[str] = (
    "服务重启后读不到这个会话的 Checkpoint（会话可能已被删除），无法证明任务结局，标为需人工核对。"
)


@dataclass(frozen=True)
class TurnEvidence:
    """只读核对用到的一轮事实（从 Checkpoint 与台账读，拿不到时为 `None`）。"""

    #: 这一轮的 `task_status`（可能是空串：旧 Checkpoint 没写）。
    turn_state: str | None
    #: Checkpoint 当前的 `run_id`；没绑定就是 `None`（**不是通配符**）。
    current_run_id: str | None
    #: 这一轮是否**还挂着一个活的 interrupt**（在等用户决定）。
    has_live_interrupt: bool
    #: "这一轮属于哪个任务"的可证 run：优先用**任务索引自己绑定的** `run_id`，没有时用**该会话
    #: 台账最新一条**的 `run_id`——崩溃发生在结算之前时索引还没绑定，而台账在流式期间就绑了。
    #: 两个来源都没有就是 `None`：**证明不了**（同 §15.2 的 `NULL` 口径）。
    reported_run: str | None
    completed_children: tuple[str, ...] = ()


def child_completion_proven(child_id: str, values: Mapping[str, Any]) -> bool:
    """只认独立子检查点的完整终态；身份索引不能提供此证明。"""
    if values.get("child_id") != child_id or values.get("status") != "finished":
        return False
    receipt = values.get("receipt")
    if not isinstance(receipt, Mapping):
        return False
    report, state = receipt.get("report"), receipt.get("state")
    if not isinstance(report, Mapping) or not isinstance(state, Mapping):
        return False
    workflow = state.get("workflow")
    return bool(report.get("child_id") == child_id and report.get("status") == "completed"
                and report.get("references") and state.get("run_id") == child_id
                and state.get("task_status") == "completed" and isinstance(workflow, Mapping)
                and workflow.get("name") == READONLY_RESEARCH and workflow.get("phase") == "completed"
                and workflow.get("run_id") == child_id)


@dataclass(frozen=True)
class JobVerdict:
    """对一个非终态任务的判定。

    ``target`` 为 `None` 表示**索引已经是对的、不写库**（不写库就不发事件，见 §17.3 的"只改 detail
    的同状态写入不算迁移"）。
    """

    verdict: str
    target: str | None
    detail: str | None

    @property
    def writes(self) -> bool:
        """这次判定是否需要写索引。"""
        return self.target is not None


def classify_interrupted_job(job: Job, *, evidence: TurnEvidence | None) -> JobVerdict:
    """判定一个**非终态**任务在重启后属于哪一类（架构 §17.5）。

    Args:
        job: 索引里的非终态任务。终态任务传进来是编程错误（终态不可改写）。
        evidence: 该会话 Checkpoint 的只读事实；读不到时为 `None`（等价于"证明不了"）。

    Returns:
        判定与要写入的目标状态；``target`` 为 `None` 表示索引不用改。

    Raises:
        ValueError: 传入的已经是终态任务。
    """
    if job.is_terminal:
        msg = f"只对非终态任务做重启判定，收到终态任务 {job.job_id}（{job.state}）"
        raise ValueError(msg)

    # 1) 排队中 = 图从未启动（worker 取出前先写 running），这是**能证明**的"无副作用"。
    if job.state == JOB_STATE_QUEUED:
        return JobVerdict(VERDICT_UNSTARTED, JOB_STATE_FAILED, UNSTARTED_DETAIL)

    # 2) 读不到 Checkpoint：什么都证明不了，不猜。
    if evidence is None:
        return JobVerdict(VERDICT_NEEDS_REVIEW, JOB_STATE_NEEDS_REVIEW, UNREADABLE_DETAIL)

    # 3) 还挂着活的 interrupt = 这一轮在等用户决定，走既有审批恢复。
    if evidence.has_live_interrupt:
        if (
            evidence.reported_run is not None
            and evidence.current_run_id is not None
            and evidence.reported_run != evidence.current_run_id
        ):
            # 显式矛盾：这个任务（或台账最后一条）指向**另一轮**，而挂着的是别的轮的 interrupt。
            return JobVerdict(VERDICT_NEEDS_REVIEW, JOB_STATE_NEEDS_REVIEW, NEEDS_REVIEW_DETAIL)
        if job.state == JOB_STATE_WAITING_USER:
            return JobVerdict(VERDICT_RESUMABLE, None, None)
        return JobVerdict(VERDICT_RESUMABLE, JOB_STATE_WAITING_USER, RESUMABLE_DETAIL)

    # 4) 能证明终态：可证 run 与 Checkpoint 当前 run 是同一轮，且该轮已有结局。
    if (
        evidence.reported_run is not None
        and evidence.current_run_id is not None
        and evidence.reported_run == evidence.current_run_id
        and (evidence.turn_state or "") in TASK_TERMINAL_STATUSES
    ):
        target = (
            JOB_STATE_COMPLETED if evidence.turn_state == "completed" else JOB_STATE_FAILED
        )
        return JobVerdict(VERDICT_PROVABLE, target, PROVABLE_DETAIL)

    # 5) 其余：可能已经写过文件、可能还有残留进程。标"需人工核对"，**不自动重放**。
    detail = NEEDS_REVIEW_DETAIL
    if (evidence.reported_run is not None and evidence.reported_run == evidence.current_run_id
            and evidence.completed_children):
        detail += f" 已从子检查点识别 {len(evidence.completed_children)} 个完成子任务，记录保留；父任务结局仍需核对。"
    return JobVerdict(VERDICT_NEEDS_REVIEW, JOB_STATE_NEEDS_REVIEW, detail)

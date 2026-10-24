"""重启后的任务分类（架构 §17.5，v0.6 T61）。

这一层只测**判定本身**（纯函数，穷举各类证据组合）：判定"能不能证"是这一片全部的风险所在，
而它不需要数据库、Checkpoint、模型或进程。服务层的端到端（真的崩溃一次再重启）在
`tests/test_api.py` 的"重启收口"用例里，用的是真索引 + 真 Checkpoint + 真台账。
"""

from __future__ import annotations

import pytest

from local_agent.application.jobs import JOB_EVENT_FOR_STATE, can_transition
from local_agent.application.recovery import (
    NEEDS_REVIEW_DETAIL,
    PROVABLE_DETAIL,
    RESUMABLE_DETAIL,
    TASK_TERMINAL_STATUSES,
    UNREADABLE_DETAIL,
    UNSTARTED_DETAIL,
    VERDICT_NEEDS_REVIEW,
    VERDICT_PROVABLE,
    VERDICT_RESUMABLE,
    VERDICT_UNSTARTED,
    TurnEvidence,
    classify_interrupted_job,
)
from local_agent.persistence.job_store import (
    JOB_STATE_CANCELLED,
    JOB_STATE_COMPLETED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_REVIEW,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_WAITING_USER,
    JOB_STATES,
    Job,
)

_NOW = "2026-10-10T00:00:00.000000+00:00"


def _job(state: str, *, run_id: str | None = None) -> Job:
    return Job(
        job_id="job-1",
        session_id="session-1",
        state=state,
        run_id=run_id,
        attempt=1,
        created_at=_NOW,
        started_at=None,
        updated_at=_NOW,
        finished_at=None,
        detail=None,
    )


def _evidence(
    *,
    turn_state: str | None = None,
    current_run_id: str | None = None,
    has_live_interrupt: bool = False,
    reported_run: str | None = None,
) -> TurnEvidence:
    return TurnEvidence(
        turn_state=turn_state,
        current_run_id=current_run_id,
        has_live_interrupt=has_live_interrupt,
        reported_run=reported_run,
    )


def test_queued_is_provably_unstarted_even_without_any_checkpoint() -> None:
    """排队中 = 图从未启动（worker 取出前先写 running），所以读不到 Checkpoint 也不影响这条。"""
    verdict = classify_interrupted_job(_job(JOB_STATE_QUEUED), evidence=None)

    assert (verdict.verdict, verdict.target) == (VERDICT_UNSTARTED, JOB_STATE_FAILED)
    assert verdict.detail == UNSTARTED_DETAIL
    assert "还没开始执行" in UNSTARTED_DETAIL


def test_queued_wins_over_a_terminal_checkpoint_from_an_earlier_task() -> None:
    """上一个任务留下的终态 Checkpoint 不能把"这次还没开始"说成"已完成"。"""
    verdict = classify_interrupted_job(
        _job(JOB_STATE_QUEUED),
        evidence=_evidence(
            turn_state="completed", current_run_id="run-1", reported_run="run-1"
        ),
    )

    assert (verdict.verdict, verdict.target) == (VERDICT_UNSTARTED, JOB_STATE_FAILED)


def test_missing_checkpoint_is_never_a_guess() -> None:
    verdict = classify_interrupted_job(_job(JOB_STATE_RUNNING), evidence=None)

    assert (verdict.verdict, verdict.target) == (VERDICT_NEEDS_REVIEW, JOB_STATE_NEEDS_REVIEW)
    assert verdict.detail == UNREADABLE_DETAIL


def test_live_interrupt_means_resumable() -> None:
    """索引停在 ``running`` 但其实这一轮在等用户：把它校正为"等待用户"，用户就能继续操作。"""
    verdict = classify_interrupted_job(
        _job(JOB_STATE_RUNNING),
        evidence=_evidence(has_live_interrupt=True, current_run_id="run-1"),
    )

    assert (verdict.verdict, verdict.target) == (VERDICT_RESUMABLE, JOB_STATE_WAITING_USER)
    assert verdict.detail == RESUMABLE_DETAIL


def test_waiting_user_with_a_live_interrupt_is_not_rewritten() -> None:
    """索引已经是对的就不写库——不写库就不发事件（§17.3 的同状态不算迁移）。"""
    verdict = classify_interrupted_job(
        _job(JOB_STATE_WAITING_USER),
        evidence=_evidence(has_live_interrupt=True, current_run_id="run-1", reported_run="run-1"),
    )

    assert verdict.verdict == VERDICT_RESUMABLE
    assert verdict.target is None and verdict.detail is None and not verdict.writes


def test_live_interrupt_of_another_run_is_not_this_task_s() -> None:
    """显式矛盾：挂着的是**别的轮**的 interrupt，不能拿它当这个任务的"可恢复"。"""
    verdict = classify_interrupted_job(
        _job(JOB_STATE_RUNNING, run_id="run-2"),
        evidence=_evidence(has_live_interrupt=True, current_run_id="run-1", reported_run="run-2"),
    )

    assert (verdict.verdict, verdict.target) == (VERDICT_NEEDS_REVIEW, JOB_STATE_NEEDS_REVIEW)


@pytest.mark.parametrize(
    ("turn_state", "target"),
    [("completed", JOB_STATE_COMPLETED), ("failed", JOB_STATE_FAILED), ("cancelled", JOB_STATE_CANCELLED)],
)
def test_terminal_turn_with_a_matching_run_proves_the_outcome(turn_state: str, target: str) -> None:
    verdict = classify_interrupted_job(
        _job(JOB_STATE_RUNNING),
        evidence=_evidence(
            turn_state=turn_state, current_run_id="run-1", reported_run="run-1"
        ),
    )

    assert (verdict.verdict, verdict.target) == (VERDICT_PROVABLE, target)
    assert verdict.detail == PROVABLE_DETAIL


@pytest.mark.parametrize(
    "evidence",
    [
        # `NULL` 是"不知道"，不是通配符：没有可证 run 就证明不了（§15.2 的口径）
        _evidence(turn_state="completed", current_run_id="run-1", reported_run=None),
        # 可证 run 有值，但 Checkpoint 没绑定当前 run
        _evidence(turn_state="completed", current_run_id=None, reported_run="run-1"),
        # 可证 run 与当前 run 不是同一轮（正是"拿上一轮的终态冒充这一次"的来源）
        _evidence(turn_state="completed", current_run_id="run-1", reported_run="run-0"),
        # 同一轮，但还没到终态
        _evidence(turn_state="running", current_run_id="run-1", reported_run="run-1"),
        _evidence(turn_state=None, current_run_id="run-1", reported_run="run-1"),
    ],
)
def test_anything_short_of_a_proof_needs_a_human(evidence: TurnEvidence) -> None:
    verdict = classify_interrupted_job(_job(JOB_STATE_RUNNING), evidence=evidence)

    assert (verdict.verdict, verdict.target) == (VERDICT_NEEDS_REVIEW, JOB_STATE_NEEDS_REVIEW)
    assert verdict.detail == NEEDS_REVIEW_DETAIL
    # 说明里必须出现这两件事，页面与用户才知道该核对什么（§17.5）
    assert "可能已部分执行" in NEEDS_REVIEW_DETAIL
    assert "可能仍有残留进程" in NEEDS_REVIEW_DETAIL


def test_needs_review_is_terminal_and_never_rewritten() -> None:
    assert JOB_STATE_NEEDS_REVIEW in JOB_STATES
    assert not can_transition(JOB_STATE_NEEDS_REVIEW, JOB_STATE_RUNNING)
    # 状态 → 事件类型仍然"不重不漏"，`needs_review` 与 failed / completed 共用 `job_completed`
    assert set(JOB_EVENT_FOR_STATE) == set(JOB_STATES)
    assert JOB_EVENT_FOR_STATE[JOB_STATE_NEEDS_REVIEW] == "job_completed"


def test_terminal_states_are_exactly_what_we_can_prove() -> None:
    assert TASK_TERMINAL_STATUSES == {"completed", "failed", "cancelled"}


def test_a_terminal_task_is_a_programming_error() -> None:
    """终态不可改写：拿终态任务来做重启判定是调用方的错，要当场炸出来。"""
    with pytest.raises(ValueError, match="只对非终态任务"):
        classify_interrupted_job(_job(JOB_STATE_COMPLETED), evidence=None)


@pytest.mark.parametrize(
    ("state", "evidence"),
    [
        (JOB_STATE_QUEUED, None),
        (JOB_STATE_RUNNING, None),
        (JOB_STATE_RUNNING, _evidence(has_live_interrupt=True, current_run_id="run-1")),
        (
            JOB_STATE_RUNNING,
            _evidence(turn_state="completed", current_run_id="run-1", reported_run="run-1"),
        ),
        (JOB_STATE_WAITING_USER, _evidence(turn_state="failed", reported_run=None)),
    ],
)
def test_every_verdict_target_is_a_legal_transition(state: str, evidence: TurnEvidence | None) -> None:
    """判定给出的目标状态必须在迁移表里——两条规则分处两个模块，靠这条用例钉住。"""
    verdict = classify_interrupted_job(_job(state), evidence=evidence)

    if verdict.target is not None:
        assert can_transition(state, verdict.target)

"""等待用户决定的两种恢复命令：审批（`resolve_approval`）与提问（`resolve_question`）。

为什么单独一个模块：`ApplicationService` 已经是全仓最大的文件，规模棘轮（`CONTRIBUTING.md`
的"规模与复杂度棘轮"）不允许它继续增长。这两个用例与"提交消息"的性质也不同——它们**不新开
一轮**，只把挂起的图接回来，因此放一起比塞进服务类更清楚。

**本模块不导入 `service`**（那会形成循环）：需要的服务实例由调用方传入，内部成员只在
`TYPE_CHECKING` 下做类型标注；`_digest` 与 `APPROVAL_DECISIONS` 由这里定义，服务模块再从
这里导入（同一份定义，不是第二套）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final, cast

from langgraph.types import Command

from local_agent.application import conversation
from local_agent.application import errors as app_errors
from local_agent.application import events as app_events
from local_agent.application.models import CommandResultView, CommandStatus, SessionSnapshot
from local_agent.application.pending_views import project_approval, project_question
from local_agent.persistence.job_store import JOB_STATE_CANCELLED, Job
from local_agent.persistence.request_ledger import (
    OPERATION_RESOLVE_APPROVAL,
    OPERATION_RESOLVE_QUESTION,
    STATUS_COMPLETED,
    STATUS_EXECUTING,
)

if TYPE_CHECKING:
    from local_agent.application.service import ApplicationService
    from local_agent.persistence.session_store import Session


#: 允许的审批决定（与 `ui/approval_ui.py` 的载荷一致；这里不导入那个模块——它依赖 Streamlit）。
APPROVAL_DECISIONS: Final[frozenset[str]] = frozenset({"approve", "edit", "reject"})
CANCELLED_BEFORE_START: Final[str] = "cancelled_before_start"


def dispatch_guarded(
    service: ApplicationService, session: Session, request_id: str, job: Job,
    run: Callable[[], CommandResultView], release: Callable[[], None],
) -> None:
    """worker 确认未调用执行入口就跳过的取消，可证明没有副作用，需同时结清请求台账。"""
    jobs = service._jobs
    assert jobs is not None
    started = False

    def work() -> CommandResultView:
        nonlocal started
        started = True
        return run()

    def done() -> None:
        try:
            cancelled = any(item.job_id == job.job_id and item.state == JOB_STATE_CANCELLED
                            for item in jobs.list_for_session(session.session_id))
            if not started and cancelled:
                entry = service._sqlite_access(lambda: service._ledger.get(request_id))
                if entry is not None and entry["status"] == STATUS_EXECUTING:
                    service._sqlite_access(lambda: service._ledger.finish(
                        request_id, status=STATUS_COMPLETED, error_code=CANCELLED_BEFORE_START,
                    ))
        finally:
            release()

    jobs.dispatch(job, work, on_done=done)


def _digest(*parts: str) -> str:
    """规范化参数摘要：同样的参数必须得到同样的摘要（幂等判定的基础）。"""
    material = "\x1f".join(parts)
    return hashlib.sha256(material.encode()).hexdigest()


def status_from_snapshot(snapshot: SessionSnapshot) -> CommandStatus:
    """命令结局由那一份快照的会话状态决定：挂起 → 等待用户，失败 → 失败，其余完成。

    两种"等你"（审批 / 提问）都如实回报，不能落成 `completed`——那会让提交方以为这一轮
    已经跑完了（§15.3 的命令结果口径）。被取消的一轮看 `final_result.state`：取消后会话
    状态就回到 `idle`，只看会话状态会把"你自己停的"报成完成（§17.4）。
    """
    state = snapshot.session_state
    if state in {"awaiting_approval", "awaiting_input"}:
        return cast(CommandStatus, state)
    if state == "failed":
        return "failed"
    final = snapshot.final_result
    if final is not None and final.state == "cancelled":
        return "cancelled"
    return "completed"


def question_events(
    snapshot: SessionSnapshot,
    *,
    has_live_interrupt: bool,
    announced: Any,
    envelope: Callable[..., Any],
) -> list[Any]:
    """提问的两条事件（与审批同构）：挂起时播一次 `question_required`，有结局时播一次 `question_resolved`。"""
    question = snapshot.question
    if question is None:
        return []
    events: list[Any] = []
    if has_live_interrupt and not announced.question_required:
        announced.question_required = True
        events.append(
            envelope(
                "question_required",
                question.question_id,
                app_events.QuestionRequiredPayload(question=question),
                app_events.QuestionRequiredEvent,
            )
        )
    elif question.state in {"answered", "skipped"} and not announced.question_resolved:
        announced.question_resolved = True
        events.append(
            envelope(
                "question_resolved",
                question.question_id,
                app_events.QuestionResolvedPayload(
                    question_id=question.question_id, state=question.state
                ),
                app_events.QuestionResolvedEvent,
            )
        )
    return events


def _now() -> datetime:
    return datetime.now(timezone.utc)


def require_pending_approval(service: ApplicationService, session: Session, approval_id: str) -> None:
    """这个审批是不是还真的在等这一步决定？不是就别 resume——那会答非所问。"""
    view = service._read(session)
    current = project_approval(
        view.values, has_live_interrupt=view.has_live_interrupt, secrets=service._secrets
    )
    if current is None or current.state != "pending" or current.approval_id != approval_id:
        raise app_errors.approval_not_actionable(
            session_id=session.session_id, approval_id=approval_id, occurred_at=_now()
        )


def require_pending_question(service: ApplicationService, session: Session, question_id: str) -> None:
    """这个提问是不是还真的在等这次答复？判据与审批同构（失败闭合：不在等就拒绝 resume）。"""
    view = service._read(session)
    current = project_question(
        view.values, has_live_interrupt=view.has_live_interrupt, secrets=service._secrets
    )
    if current is None or current.state != "pending" or current.question_id != question_id:
        raise conversation.conflict(session.session_id, "这个提问已经不在等待答复，请刷新后再试。")


def resolve_approval(
    service: ApplicationService,
    session_id: str,
    approval_id: str,
    decision: str,
    client_request_id: str,
    *,
    edited_arguments: Mapping[str, Any] | None = None,
    reason: str = "",
    confirm_recursive: bool = False,
) -> CommandResultView:
    """对挂起的审批给出决定并恢复该运行。"""
    if decision not in APPROVAL_DECISIONS:
        msg = f"未知的审批决定：{decision}"
        raise ValueError(msg)
    digest = _digest(
        OPERATION_RESOLVE_APPROVAL,
        session_id,
        approval_id,
        decision,
        json.dumps(edited_arguments or {}, sort_keys=True, ensure_ascii=False),
        reason,
        str(confirm_recursive),
    )
    session = service._session_or_raise(session_id)
    resume = {
        "decision": decision,
        "confirm_recursive": confirm_recursive,
        "edited_arguments": dict(edited_arguments) if edited_arguments else None,
        "reason": reason,
    }

    prepared: list[Any] = []

    def prepare() -> None:
        """审批与密钥都先备好，再落台账（同 `submit_message`：预检失败不该留下"执行中"）。"""
        require_pending_approval(service, session, approval_id)
        # 恢复用**这一轮固定**的连接，而不是当前设置里的（§6.8 的停止条件）。密钥也按**那个版本**取：
        # 之后轮换过密钥也不影响这次恢复（凭据引用带版本）。
        prepared.append(
            service._run_env(session, service._binding_for_resume(session), for_resume=True)
        )

    def run() -> CommandResultView:
        return service._execute(
            session,
            request_id=client_request_id,
            operation=OPERATION_RESOLVE_APPROVAL,
            payload=Command(resume=resume),
            resolve_decision=(approval_id, decision),
            env=prepared[0],
        )

    return service._guarded(
        session,
        request_id=client_request_id,
        operation=OPERATION_RESOLVE_APPROVAL,
        arguments_digest=digest,
        run=run,
        precheck=prepare,
        # 审批恢复不新建 run：绑定的就是**正在恢复的那一轮**（挂起的 interrupt 属于它）。
        bind_run=lambda: service._current_run_id(session),
        # 审批恢复接上"等待用户"的那个任务，而不是新登记一个（§17.2）。
        resume_job=True,
    )


def _question_pairs(
    answer: str | None, answers: Sequence[Mapping[str, Any]] | None
) -> list[tuple[int, str]]:
    """把整组答复归一成 `(index, 文本)`；`answer` 是单题写法的兼容入口。"""
    pairs: dict[int, str] = {}
    if answers:
        for entry in answers:
            try:
                index = int(str(entry.get("index")))
            except (TypeError, ValueError):
                continue
            if index < 0:
                continue
            pairs[index] = str(entry.get("answer") or "")
    elif answer:
        pairs[0] = str(answer)
    return sorted(pairs.items())


def resolve_question(
    service: ApplicationService,
    session_id: str,
    question_id: str,
    answer: str | None,
    action: str,
    client_request_id: str,
    *,
    answers: Sequence[Mapping[str, Any]] | None = None,
) -> CommandResultView:
    """回答（或跳过）一个挂起的问题组并恢复该运行（2026-10-28，架构 §6.14）。

    与 :func:`resolve_approval` 完全同构：预检"这个提问是否还在等"→ 落台账 →
    `Command(resume=...)` → 接上原来那条等待中的任务。**整组都是空答复时按跳过处理**
    （失败闭合），与节点里的判定同一口径。`answers` 为空时退化到单题写法的 `answer`。
    """
    normalized = "answer" if str(action or "").strip().lower() == "answer" else "skip"
    pairs = _question_pairs(answer, answers)
    if normalized == "skip":
        pairs = []
    if normalized == "answer" and not any(text.strip() for _, text in pairs):
        normalized = "skip"
    digest = _digest(
        OPERATION_RESOLVE_QUESTION,
        session_id,
        question_id,
        normalized,
        json.dumps(
            [{"index": index, "answer": text} for index, text in pairs],
            sort_keys=True,
            ensure_ascii=False,
        ),
    )
    session = service._session_or_raise(session_id)
    resume = {
        "action": normalized,
        "answers": [{"index": index, "answer": text} for index, text in pairs],
    }

    prepared: list[Any] = []

    def prepare() -> None:
        """先核对"这个提问还在等"，再备好这一轮的连接与密钥（同审批）。"""
        require_pending_question(service, session, question_id)
        prepared.append(
            service._run_env(session, service._binding_for_resume(session), for_resume=True)
        )

    def run() -> CommandResultView:
        return service._execute(
            session,
            request_id=client_request_id,
            operation=OPERATION_RESOLVE_QUESTION,
            payload=Command(resume=resume),
            resolve_decision=None,
            env=prepared[0],
        )

    return service._guarded(
        session,
        request_id=client_request_id,
        operation=OPERATION_RESOLVE_QUESTION,
        arguments_digest=digest,
        run=run,
        precheck=prepare,
        # 恢复不新建 run：绑定的就是**正在恢复的那一轮**（挂起的 interrupt 属于它）。
        bind_run=lambda: service._current_run_id(session),
        # 接上"等待用户"的那个任务，而不是新登记一个（§17.2）。
        resume_job=True,
    )

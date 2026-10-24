"""会话地图与分支上下文。路径索引不执行图，追问仍由应用服务提交新输入。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from local_agent.application import errors
from local_agent.application import projection as proj
from local_agent.application.models import (
    ConversationBranchView,
    ConversationMapView,
    ConversationNodeView,
    SessionSnapshot,
    SideChatView,
)
from local_agent.persistence.conversation_store import Branch, ConversationTurn
from local_agent.persistence.session_store import Session

if TYPE_CHECKING:
    from local_agent.application.service import ApplicationService, _ThreadView


def conflict(session_id: str, detail: str) -> errors.ApplicationError:
    return errors.ApplicationError(errors.ErrorView.for_code(
        errors.CONVERSATION_CONFLICT, title="会话路径需要重新确认", message=detail,
        hint="刷新会话地图，重新选择问答或分支后提交。",
        diagnostic_id=errors.diagnostic_id_for(errors.CONVERSATION_CONFLICT, session_id),
        occurred_at=datetime.now(timezone.utc), source="session",
    ))


def rows_from_view(service: ApplicationService, session: Session, view: _ThreadView,
                   branch_id: str) -> list[ConversationTurn]:
    existing = {row.turn_id: row for row in service._sqlite_access(
        lambda: service._conversations.turns(session.session_id))}
    usages = {turn.user_message_id: turn.usage for turn in proj.project_turn_usages(view.values)}
    rows: list[ConversationTurn] = []
    ordinal = max((row.ordinal for row in existing.values()), default=0)
    for message in proj.project_messages_view(view.values, secrets=service._secrets):
        if message.kind == "user":
            old = existing.get(message.message_id)
            usage = usages.get(message.message_id)
            ordinal += old is None
            current = view.values.get("run_user_message_id") == message.message_id
            state = usage.final_state if usage else (
                proj.project_session_state(view.values, has_live_interrupt=view.has_live_interrupt)
                if current else "unknown")
            # 只有本轮终态的检查点可作追问锚点，不能把祖先误绑定到最新一轮。
            anchor = view.checkpoint_id if current and state in {
                "completed", "failed", "blocked", "cancelled"
            } and not view.has_live_interrupt and not view.next_nodes else None
            rows.append(ConversationTurn(
                message.message_id, old.parent_turn_id if old else (rows[-1].turn_id if rows else None),
                old.origin_branch_id if old else branch_id,
                usage.run_id if usage else (proj.current_run_id(view.values) if current else None),
                message.text, "", state,
                usage.submitted_at.isoformat() if usage else None,
                anchor or (old.checkpoint_id if old else None), old.ordinal if old else ordinal,
            ))
        elif message.kind == "assistant" and rows:
            rows[-1] = replace(rows[-1], answer_text=(rows[-1].answer_text + "\n" + message.text).strip())
    return rows


def collect(service: ApplicationService, session: Session) -> tuple[list[ConversationTurn], list[Branch]]:
    """旧会话按真实顺序形成链；首次浏览也不写库或调用模型。"""
    branches = service._sqlite_access(lambda: service._conversations.branches(session.session_id))
    active = next((branch for branch in branches if branch.active), None)
    view = service._read(session)
    rows = {row.turn_id: row for row in service._sqlite_access(
        lambda: service._conversations.turns(session.session_id))}
    live = rows_from_view(service, session, view, active.branch_id if active else "main")
    rows.update((row.turn_id, row) for row in live)
    missing = {row.turn_id for row in rows.values()
               if row.checkpoint_id is None and row.state not in {"running", "awaiting_approval"}}
    if missing:
        history = getattr(service._graph, "get_state_history", None)
        if callable(history):
            # 仅为旧数据查找真实终态锚点。超过上限仍无锚点的节点如实禁止追问。
            for index, snapshot in enumerate(history({"configurable": {"thread_id": session.thread_id}})):
                if index >= 10000 or not missing:
                    break
                previous = service._view_of(snapshot)
                turn_id = previous.values.get("run_user_message_id")
                if turn_id not in missing or snapshot.next or previous.has_live_interrupt:
                    continue
                if str(previous.values.get("task_status")) not in {"completed", "failed", "cancelled"}:
                    continue
                row = rows[str(turn_id)]
                rows[str(turn_id)] = replace(
                    row, checkpoint_id=previous.checkpoint_id,
                    run_id=row.run_id or proj.current_run_id(previous.values),
                    state=str(previous.values.get("task_status")),
                )
                missing.remove(str(turn_id))
    ordered = sorted(rows.values(), key=lambda row: (row.ordinal, row.turn_id))
    if not branches:
        branches = [Branch("main", "原讨论", None, live[-1].turn_id if live else None,
                           view.checkpoint_id, 0, True)]
    return ordered, branches


def map_view(service: ApplicationService, session: Session, query: str = "") -> ConversationMapView:
    rows, branches = collect(service, session)
    text = query.strip().casefold()
    return ConversationMapView(
        session_id=session.session_id,
        active_branch_id=next(branch.branch_id for branch in branches if branch.active),
        nodes=[ConversationNodeView(
            turn_id=row.turn_id, parent_turn_id=row.parent_turn_id,
            origin_branch_id=row.origin_branch_id, run_id=row.run_id,
            question=proj.sanitize_text(row.question, service._secrets),
            answer_preview=proj.sanitize_text(row.answer_text[:240], service._secrets),
            state=row.state, submitted_at=row.submitted_at, ordinal=row.ordinal,
            can_continue=row.checkpoint_id is not None and row.state in {"completed", "failed", "blocked", "cancelled"},
        ) for row in rows],
        branches=[ConversationBranchView(
            branch_id=branch.branch_id, title=proj.sanitize_text(branch.title, service._secrets),
            fork_turn_id=branch.fork_turn_id, tip_turn_id=branch.tip_turn_id,
            position=branch.position, active=branch.active,
        ) for branch in branches],
        matched_turn_ids=[row.turn_id for row in rows
                          if text and text in (row.question + "\n" + row.answer_text).casefold()],
        # 侧聊（§21.7）：独立会话，但在图上挂成本会话的关联分支。
        # 读失败不让整张地图打不开——侧聊只是附注，不是地图的主体。
        side_chats=[] if session.parent_session_id is not None else [
            SideChatView(
                session_id=item.session_id,
                title=proj.sanitize_text(item.title, service._secrets),
                origin_turn_id=item.origin_turn_id,
                created_at=item.created_at,
            )
            for item in service._sqlite_access(
                lambda: service._sessions.side_chats(session.session_id))
        ],
    )


def initialize(service: ApplicationService, session: Session) -> None:
    rows, branches = collect(service, session)
    if not service._sqlite_access(lambda: service._conversations.branches(session.session_id)):
        service._sqlite_access(lambda: service._conversations.initialize(
            session.session_id, rows, branches[0].checkpoint_id))


def submission_parent(service: ApplicationService, session: Session, parent_id: str | None,
                      expected_branch: str | None) -> ConversationTurn | None:
    rows, branches = collect(service, session)
    active = next(branch for branch in branches if branch.active)
    if expected_branch is not None and expected_branch != active.branch_id:
        raise conflict(session.session_id, "当前分支已经改变，本次提问尚未执行。")
    if parent_id is None:
        return None
    parent = next((row for row in rows if row.turn_id == parent_id), None)
    if parent is None:
        raise conflict(session.session_id, "选中的问答不属于这个会话。")
    if parent.checkpoint_id is None or parent.state not in {"completed", "failed", "blocked", "cancelled"}:
        raise conflict(session.session_id, "该轮尚未结束，或缺少可用的历史检查点。")
    view = service._read_checkpoint(session, parent.checkpoint_id)
    if view is None or view.has_live_interrupt or view.next_nodes or view.values.get("run_user_message_id") != parent_id:
        raise conflict(session.session_id, "该轮的历史检查点已无法准确恢复。")
    return parent


def start_path(service: ApplicationService, session: Session, parent: ConversationTurn | None) -> None:
    initialize(service, session)
    if parent is None:
        return
    branches = service._sqlite_access(lambda: service._conversations.branches(session.session_id))
    active = next(branch for branch in branches if branch.active)
    if active.tip_turn_id == parent.turn_id:
        return
    leaf = next((branch for branch in branches if branch.tip_turn_id == parent.turn_id), None)
    if leaf is not None:
        service._sqlite_access(lambda: service._conversations.select(session.session_id, leaf.branch_id))
    else:
        service._sqlite_access(lambda: service._conversations.fork(session.session_id, uuid4().hex, parent))


def sync(service: ApplicationService, session: Session) -> None:
    """执行锁内推进当前分支游标；保存后再广播，刷新不会读到另一分支的全局最新状态。"""
    branch = service._sqlite_access(lambda: service._conversations.active(session.session_id))
    if branch is None:
        return
    view = service._view_of(service._graph.get_state({"configurable": {"thread_id": session.thread_id}}))
    with service._locks_guard:
        sink = service._streams.get(session.session_id)
    if sink is not None and sink._run_id is not None and proj.current_run_id(view.values) != sink._run_id:
        return
    rows = rows_from_view(service, session, view, branch.branch_id)
    service._sqlite_access(lambda: service._conversations.sync(
        session.session_id, branch.branch_id, rows, view.checkpoint_id))


def turn_snapshot(service: ApplicationService, session: Session, turn_id: str, *,
                  include_ancestors: bool = False) -> SessionSnapshot:
    rows, _ = collect(service, session)
    row = next((item for item in rows if item.turn_id == turn_id), None)
    if row is None:
        raise conflict(session.session_id, "找不到该轮问答。")
    view = service._read_checkpoint(session, row.checkpoint_id) if row.checkpoint_id else service._read(session)
    if view is None:
        raise conflict(session.session_id, "该轮的历史详情已无法恢复。")
    snapshot = service._snapshot_at(session, view, snapshot_id=view.checkpoint_id or "unrecorded",
                                    created_at=datetime.now(timezone.utc))
    if include_ancestors:
        if row.checkpoint_id is None:
            raise conflict(session.session_id, "缺少该轮历史上下文，无法准备追问。")
        return snapshot.model_copy(update={"approval": None})
    selected = []
    in_turn = False
    for message in snapshot.messages:
        if message.kind == "user":
            if in_turn:
                break
            in_turn = message.message_id == turn_id
        if in_turn:
            selected.append(message)
    if not selected:
        raise conflict(session.session_id, "该轮缺少可展示的历史消息。")
    # 缺少历史锚点时只展示该轮消息，不把最新轮的计划、记忆、错误和用量冒充历史详情。
    if row.checkpoint_id is None and snapshot.current_task and snapshot.current_task.run_id != row.run_id:
        snapshot = snapshot.model_copy(update={"plan": None, "memory": None, "usage": None,
            "model_calls": [], "errors": [], "final_result": None, "current_task": None})
    return snapshot.model_copy(update={"messages": selected, "approval": None,
        "turn_usages": [usage for usage in snapshot.turn_usages if usage.user_message_id == turn_id]})

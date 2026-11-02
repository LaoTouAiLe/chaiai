"""文件变更账本的「应用层视图 + 还原调度」（2026-10-30 从 ``service.py`` 整组搬出）。

**为什么要单独一个文件**：这组逻辑原本住在 ``service.py`` 里，把那一个文件撑到 5000 行以上，
超过了规模棘轮给它的豁免值（``scripts/code_size_baseline.json``）。那个棘轮只认"容器有多大"，
不允许把豁免调大（``scripts/check_size.py:274``），所以唯一合规的做法是把职责搬进新容器。

职责边界：这里只做**组装与调度**——账本读写、整文件 diff、落盘都在
``persistence/file_changes.py`` 与 ``persistence/file_restore.py``；这里负责
"把账本翻译成 HTTP 视图"和"还原前先抢锁"。

``service`` 参数用 ``Any``：本模块被 ``ApplicationService`` 反过来导入，标注真实类型会形成
循环导入。实际传进来的永远是那一个 service 实例。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from local_agent.application import errors as app_errors
from local_agent.application.models import (
    FailedFileView,
    FileChangeDiffView,
    FileChangeSummaryView,
    FileDiffHunkView,
    FileDiffLineView,
    RestoredFileView,
    RestoreResultView,
    SessionChangesView,
    SkippedFileView,
    TurnChangesView,
    UnrecordedChangeView,
)
from local_agent.model_settings.preferences import complete_preferences, initial_preferences
from local_agent.persistence import file_restore
from local_agent.tools import git_tools

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- 读取 -------------------------------------------------------------------


def session_changes(service: Any, session_id: str) -> SessionChangesView:
    """会话内的文件变更总览（2026-10-21）。

    按 ``run_id`` 分组、最新的轮次在前：用户问的是"这一次到底改了哪些文件"，
    而不是"整个会话历史上所有文件"。同一文件在一轮里被改多次时，组内只出现一行、
    行数是累计值——一个文件改四次属于正常，不该出四行。
    """
    empty = SessionChangesView(
        session_id=session_id, turns=[], total_files=0, additions=0, deletions=0
    )
    session = service._session_or_raise(session_id)
    ledger = change_ledger(service, session)
    if ledger is None:
        return empty

    ops = ledger.ops()
    if not ops:
        return empty
    summaries = {item.path: item for item in ledger.summary()}
    grouped: dict[str | None, list[Any]] = {}
    for op in ops:
        grouped.setdefault(op.run_id, []).append(op)

    turns: list[TurnChangesView] = []
    for run_id, run_ops in grouped.items():
        merged: dict[str, FileChangeSummaryView] = {}
        for op in run_ops:
            info = summaries.get(op.path)
            previous = merged.get(op.path)
            # 无文本基线的删除不计行数：那个数字来自当时按文本读原文，二进制/超大文件会被
            # 算成上万行（实测一张 27 MB 的图片显示"删除 26903 行"），比不显示更糟。
            counted = not (
                op.kind == "delete" and info is not None and not info.has_baseline
            )
            merged[op.path] = FileChangeSummaryView(
                path=op.path,
                kind=op.kind,
                additions=op.additions + (previous.additions if previous else 0),
                deletions=(op.deletions if counted else 0)
                + (previous.deletions if previous else 0),
                edits=(previous.edits if previous else 0) + 1,
                last_at=op.created_at,
                has_baseline=bool(info is not None and info.has_baseline),
                unavailable_reason=None if info is None else info.unavailable_reason,
            )
        turns.append(TurnChangesView(
            run_id=run_id,
            started_at=min(op.created_at for op in run_ops),
            files=list(merged.values()),
        ))
    turns.reverse()
    return SessionChangesView(
        session_id=session_id,
        turns=turns,
        total_files=len(summaries),
        additions=sum(item.additions for item in summaries.values()),
        # 与上面的逐轮口径一致：无文本基线的删除不把行数算进总计。
        deletions=sum(
            item.deletions
            for item in summaries.values()
            if not (item.kind == "delete" and not item.has_baseline)
        ),
        # 账本之外的写入（命令/代码），单列出来——不并进账本，也不假装有基线。
        unrecorded=unrecorded_changes(service, session, ops),
        unrecorded_note=(
            None
            if service._worktree_baselines.get(session_id) is not None
            else "本轮没有记录工作区基线（非 git 工作区或本轮未接线），无法发现命令/代码写入"
        ),
    )


def session_change_diff(service: Any, session_id: str, path: str) -> FileChangeDiffView:
    """某个文件的**整文件** diff：首次改动前原文 vs 当前磁盘内容。"""
    session = service._session_or_raise(session_id)
    ledger = change_ledger(service, session)
    if ledger is None:
        return FileChangeDiffView(
            path=path, additions=0, deletions=0, hunks=[], has_baseline=False,
            unavailable_reason="变更账本未接线：这次运行没有记录改前内容",
        )
    result = ledger.diff(path)
    return FileChangeDiffView(
        path=result.path,
        additions=result.additions,
        deletions=result.deletions,
        hunks=[
            FileDiffHunkView(
                header=hunk.header,
                lines=[
                    FileDiffLineView(
                        kind=line.kind, old_line=line.old_line,
                        new_line=line.new_line, text=line.text,
                    )
                    for line in hunk.lines
                ],
            )
            for hunk in result.hunks
        ],
        truncated=result.truncated,
        has_baseline=result.has_baseline,
        unavailable_reason=result.unavailable_reason,
        before_sha256=result.before_sha256,
        after_sha256=result.after_sha256,
    )


# ---- 还原 -------------------------------------------------------------------


def restore_session_changes(
    service: Any,
    session_id: str,
    *,
    scope: Literal["file", "turn"],
    path: str | None = None,
    run_id: str | None = None,
    paths: Sequence[str] | None = None,
    force: bool = False,
) -> RestoreResultView:
    """把文件改动退回某个时间点（2026-10-29）。**不回退对话**。

    分层：账本只回答"改前内容在哪"，这里回答"往哪写、写不写、结果给页面什么"；
    真正落盘在 ``persistence/file_restore.py``，它会对每个路径**再做一次**越界校验
    （账本里的行不构成写入许可）。

    ``scope="file"`` 退回**会话开始前**（与右栏 diff 同口径）；``scope="turn"`` 退回
    **本轮开始前**。冲突（AI 写完后被人改过）默认跳过，``force`` 才覆盖。

    **这一轮还在跑时直接拒绝**：否则可能把工具正在写的文件改回去，而那一轮的账本随后又
    记下新的 ``after_sha256``，两边对不上。与目录类操作共用同一把执行锁。
    """
    session = service._session_or_raise(session_id)
    lock = service._lock_for(session_id)
    if not lock.acquire(blocking=False):
        raise app_errors.session_busy(
            session_id=session_id, request_id="changes-restore", occurred_at=_now()
        )
    try:
        if service._sqlite_access(lambda: service._ledger.has_executing(session_id)):
            raise app_errors.session_busy(
                session_id=session_id, request_id="changes-restore", occurred_at=_now()
            )
        return _restore_and_view(
            service, session, scope=scope, path=path, run_id=run_id, paths=paths, force=force
        )
    finally:
        lock.release()


def _restore_and_view(
    service: Any,
    session: Any,
    *,
    scope: Literal["file", "turn"],
    path: str | None,
    run_id: str | None,
    paths: Sequence[str] | None,
    force: bool,
) -> RestoreResultView:
    """执行还原并组装结果视图；调用方负责持有执行锁。"""
    session_id = session.session_id
    ledger = change_ledger(service, session)
    if ledger is None:
        raise app_errors.changes_restore_unavailable(
            detail="这次运行没有接线变更账本，没有记录可用来还原",
            session_id=session_id, occurred_at=_now(),
        )
    try:
        result = file_restore.restore(
            ledger,
            workspace_root=Path(session.workspace_root),
            scope=scope, path=path, run_id=run_id, paths=paths, force=force,
        )
    except file_restore.RestoreError as exc:
        # 请求本身不成立（缺参数 / 这一轮没有改动）：告诉用户"什么都没动"，不是 500。
        raise app_errors.changes_restore_unavailable(
            detail=str(exc), session_id=session_id, occurred_at=_now(),
        ) from exc
    return RestoreResultView(
        scope=result.scope,
        run_id=result.run_id,
        restored=[
            RestoredFileView(
                path=item.path, action=item.action, basis=item.basis, undo_ref=item.undo_ref
            )
            for item in result.restored
        ],
        skipped=[SkippedFileView(path=item.path, reason=item.reason)
                 for item in result.skipped],
        failed=[FailedFileView(path=item.path, error=item.error)
                for item in result.failed],
        changes=session_changes(service, session_id),
    )


# ---- 账本装配 ---------------------------------------------------------------


def change_ledger(service: Any, session: Any) -> Any:
    """按会话取一个账本；未接线或创建失败都返回 ``None``（调用方按空结果处理）。

    三项存储口径（2026-10-29）**每次新建账本时现取**：设置页改完立即生效，不必等新一轮，
    也不必重启——这也是页面文案写"保存后立即生效"的依据。
    """
    factory = service._file_changes
    if factory is None:
        return None
    try:
        return factory(
            session_id=session.session_id,
            workspace_root=Path(session.workspace_root),
            **file_change_options(service),
        )
    except Exception:
        logger.exception("变更账本未能创建：会话 %s", session.session_id)
        return None


def file_change_options(service: Any) -> dict[str, int]:
    """当前生效的账本口径（字节 / 字节 / 轮数）。

    读失败（设置库打不开）不让记账整体失败：退回启动配置，记一条日志就够了——
    账本是可观测性设施，不是安全门。
    """
    group = None
    try:
        store = service._preferences
        saved = None if store is None else service._sqlite_access(store.read)
        values = initial_preferences(service._app_config) if saved is None else saved[1]
        group = complete_preferences(values, service._app_config).file_changes
    except Exception:
        logger.exception("读取文件改动口径失败，按启动配置处理")
    if group is None:
        # 读不到偏好、或这一组还没保存过：用启动配置（出厂默认）。
        settings = service._app_config.file_changes
        baseline_kb, session_mb, keep_turns = (
            settings.max_baseline_kb, settings.max_session_mb, settings.keep_turns)
    else:
        baseline_kb, session_mb, keep_turns = (
            group.max_baseline_kb, group.max_session_mb, group.keep_turns)
    return {
        "max_baseline_bytes": baseline_kb * 1024,
        "max_session_bytes": session_mb * 1024 * 1024,
        "keep_turns": keep_turns,
    }


def attach(service: Any, session: Any, context: Any) -> Any:
    """把本轮的变更账本挂到策略上（2026-10-21）。

    为什么挂在策略上：文件写入有三条独立路径（替换 / 新建 / 删除），它们唯一的共同点是
    都持有本轮策略。挂在这里不必给三个工具再加参数，也不会漏掉其中一条。
    """
    policy = getattr(context, "security_policy", None)
    if policy is None:
        return None
    # 轮初工作区快照（2026-10-22）：轮末凭它发现"账本没记、但文件确实变了"的路径。
    # 不能直接拿 git status 当本轮改动——工作区本来就有大量未提交改动。
    service._worktree_baselines[session.session_id] = snapshot_worktree(policy)
    ledger = change_ledger(service, session)
    if ledger is None:
        return None
    policy.file_change_recorder = ledger
    return ledger


def snapshot_worktree(policy: Any) -> dict[str, int]:
    """当前 git 工作区的 ``(工作区相对路径 -> mtime_ns)``；不是 git 仓库时返回空。

    用 mtime 而不是内容 hash：这一步每轮跑两次，扫工作区全部改动文件算 hash 太贵；
    同一轮里"改了又改回"本来也无法从快照看出来，mtime 足够回答"它被动过没有"。
    """
    try:
        status = git_tools.status(policy)
    except Exception:  # noqa: BLE001 - git 不可用/不是仓库都只是"没有快照"，不能挡住这一轮
        return {}
    root = Path(policy.workspace_root)
    repo_root = Path(status.repo_root) if status.repo_root else root
    snapshot: dict[str, int] = {}
    for entry in status.entries:
        try:
            relative = (repo_root / entry.path).relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            snapshot[relative] = (root / relative).stat().st_mtime_ns
        except OSError:
            snapshot[relative] = 0
    return snapshot


def unrecorded_changes(
    service: Any, session: Any, ops: list[Any]
) -> list[UnrecordedChangeView]:
    """账本之外、本轮被命令/代码动过的文件（2026-10-22）。

    口径：**当前 git 工作区** 与 **本轮开始时** 的快照比，排除账本已经记过的路径。
    为什么不把它们并进账本：它们没有"改前原文"，塞进去会让面板假装自己有基线；
    用户需要知道的是"还有这些文件被动过"，而不是一个看起来完整的假象。
    """
    policy = service._active_policies.get(session.session_id)
    baseline = service._worktree_baselines.get(session.session_id)
    if policy is None or baseline is None:
        return []
    current = snapshot_worktree(policy)
    recorded = {op.path for op in ops}
    changes: list[UnrecordedChangeView] = []
    for path, mtime in sorted(current.items()):
        if path in recorded:
            continue
        if baseline.get(path) == mtime:
            continue
        changes.append(UnrecordedChangeView(path=path, status="modified"))
    return changes

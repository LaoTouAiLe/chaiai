"""把一个会话里的文件改动退回某个时间点（2026-10-29）。

**两个粒度，两套基准**（这是设计上最容易搞错的地方）：

- ``scope="file"``：退回**本会话开始前**。与右栏"净变化"diff 的口径一致
  （那份 diff 就是 ``baseline → 当前``），所以用户点完"还原此文件"后 diff 会变空，
  不会出现"点了还原但还剩半截改动"。
- ``scope="turn"``：把这一轮改过的文件退回**本轮开始前**。基准来自
  ``file_change_ops.before_ref``（v15）；v15 之前的行没有它，只能降级为会话开始前，
  结果里用 ``basis`` 标出来，页面照实说明。

**判定「能不能动」只用一条规则**：该文件当前内容的 sha256 是否等于本会话里它**最后一条 op**
的 ``after_sha256``。相等 = AI 写完之后没人碰过，可以还原；不等 = 期间被人改过，默认跳过
（``force=True`` 才覆盖）。这条规则来自 hermes 的 agent-write ledger 思路，但我们不另存
ledger——``after_sha256`` 记账时本来就在。

**不做的事**：不回退对话（2026-10-29 用户明确要求）、不做跨文件事务（部分成功如实报告，
不假装原子）、不还原账本之外的写入（命令行 / 代码生成的文件没有改前内容）。
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from local_agent.exceptions import WorkspaceBoundaryError
from local_agent.persistence.file_changes import FileChangeLedger, FileChangeRecord
from local_agent.security.path_guard import resolve_within_workspace

logger = logging.getLogger(__name__)

Scope = Literal["file", "turn"]
Basis = Literal["turn", "session"]
Action = Literal["written", "rebuilt", "removed", "unchanged"]


class RestoreError(Exception):
    """还原请求本身不成立（缺参数、轮次不存在）。调用方据此回 4xx，而不是 500。"""


@dataclass(frozen=True)
class RestoredFile:
    """真正动过（或判定无需动）的一个文件。"""

    path: str
    #: ``written`` 覆盖写回 / ``rebuilt`` 文件原本被删、按改前内容重建 /
    #: ``removed`` 删除本次新建文件 / ``unchanged`` 状态已经一致，没有写盘。
    action: Action
    #: 基准来自哪一层：``turn`` = 本轮开始前；``session`` = 会话开始前（v15 前的降级）。
    basis: Basis
    #: 覆盖前把当前内容留了一份（内容寻址引用），便于人工找回。
    undo_ref: str | None = None


@dataclass(frozen=True)
class SkippedFile:
    path: str
    reason: str


@dataclass(frozen=True)
class FailedFile:
    path: str
    error: str


@dataclass(frozen=True)
class RestoreResult:
    scope: Scope
    run_id: str | None
    restored: tuple[RestoredFile, ...]
    skipped: tuple[SkippedFile, ...]
    failed: tuple[FailedFile, ...]


@dataclass(frozen=True)
class _Target:
    """一个待处理的文件：改前内容 + 冲突判定所需的最后一条 op。"""

    path: str
    before: bytes | None
    before_exists: bool
    basis: Basis
    reason: str | None
    last_after_sha: str | None


def restore(
    ledger: FileChangeLedger,
    *,
    workspace_root: Path,
    scope: Scope,
    path: str | None = None,
    run_id: str | None = None,
    paths: Sequence[str] | None = None,
    force: bool = False,
) -> RestoreResult:
    """执行还原。参数校验失败抛 :class:`RestoreError`；单文件失败只进 ``failed``，不中断整批。

    Args:
        ledger: 该会话的账本（提供改前内容与 op 记录）。
        workspace_root: 工作区根；每个路径都按它做越界校验，不信任账本里的行。
        scope: ``file`` 退到会话开始前；``turn`` 退到本轮开始前。
        path: ``scope=file`` 时必须给。
        run_id: ``scope=turn`` 时必须给。
        paths: ``scope=turn`` 时可只还原这一轮里的一部分文件。
        force: 文件在 AI 写完后被改过时，是否仍然覆盖。
    """
    ops = ledger.ops()
    if scope == "file":
        if not path:
            raise RestoreError("还原单个文件需要提供 path")
        targets = _file_targets(ledger, ops, path)
        if not targets:
            # 与整轮分支同一条口径：没有可做的事要说清楚，不能回一个空结果让页面
            # 显示"0 个处理成功"——那读起来像成功，实际什么都没发生（2026-10-29 审查）。
            raise RestoreError("这个文件在当前会话里没有改动记录")
    else:
        if not run_id:
            raise RestoreError("还原本轮需要提供 run_id")
        targets = _turn_targets(ledger, ops, run_id, paths)
        if not targets:
            raise RestoreError("这一轮没有记录到任何文件改动")

    restored: list[RestoredFile] = []
    skipped: list[SkippedFile] = []
    failed: list[FailedFile] = []
    for target in targets:
        try:
            outcome, skip_reason, failure = _apply_one(
                target, workspace_root, force=force, undo_store=ledger.store_undo_snapshot
            )
        except OSError as exc:  # 单文件失败不拖垮整批
            failed.append(FailedFile(path=target.path, error=exc.strerror or str(exc)))
            continue
        if skip_reason is not None:
            skipped.append(SkippedFile(path=target.path, reason=skip_reason))
        elif failure is not None:
            failed.append(FailedFile(path=target.path, error=failure))
        elif outcome is not None:
            restored.append(outcome)
    return RestoreResult(
        scope=scope, run_id=run_id,
        restored=tuple(restored), skipped=tuple(skipped), failed=tuple(failed),
    )


def _file_targets(
    ledger: FileChangeLedger, ops: Sequence[FileChangeRecord], path: str
) -> list[_Target]:
    """单文件：基准取**会话开始前**（v11 基线），与右栏 diff 同一口径。"""
    mine = [op for op in ops if op.path == path]
    if not mine:
        return []
    ref, reason, _sha = ledger.session_baseline(path)
    before = ledger.load_session_baseline(ref) if ref else None
    if before is None and _was_created(mine):
        # 本会话新建的文件，语义上的"改前"是空串——不是"没有记录"。
        before, reason = b"", None
    return [_Target(
        path=path, before=before, before_exists=not _was_created(mine), basis="session",
        reason=None if before is not None else (reason or "这个文件没有记录改前内容"),
        last_after_sha=mine[-1].after_sha256,
    )]


def _turn_targets(
    ledger: FileChangeLedger,
    ops: Sequence[FileChangeRecord],
    run_id: str,
    only: Sequence[str] | None,
) -> list[_Target]:
    """基准取本轮首次记录；冲突判定取本会话该路径最后一次操作。"""
    wanted = set(only) if only is not None else None
    per_path: dict[str, list[FileChangeRecord]] = {}
    latest: dict[str, FileChangeRecord] = {}
    for op in ops:
        if op.path not in latest or op.op_id > latest[op.path].op_id:
            latest[op.path] = op
        if op.run_id != run_id:
            continue
        if wanted is not None and op.path not in wanted:
            continue
        per_path.setdefault(op.path, []).append(op)

    targets: list[_Target] = []
    for path, group in per_path.items():
        group.sort(key=lambda item: item.op_id)
        first = group[0]
        basis: Basis = "turn"
        before_exists = first.kind != "create"
        before: bytes | None = None
        reason: str | None = None
        if first.before_ref is not None:
            before = ledger.load_blob(first.before_ref)
            if before is None:
                reason = "这一轮的改前内容已损坏或不存在"
        else:
            # 这一轮**明确记录过为什么没有改前内容**（超限 / 二进制 / 配额用尽 / 已清理）时，
            # 不能悄悄改用"会话开始前"顶上：那会退得比用户点的更远，还把配额问题藏起来。
            recorded_reason = first.before_reason
            if recorded_reason is not None:
                reason = recorded_reason
            else:
                # 降级：v15 之前的轮次既没有 before_ref、也从未尝试记录过，只能退回会话开始前。
                basis = "session"
                session_ops = [op for op in ops if op.path == path]
                before_exists = not _was_created(session_ops)
                ref, base_reason, _sha = ledger.session_baseline(path)
                if ref:
                    before = ledger.load_session_baseline(ref)
                reason = base_reason if before is None else None
                if before is None and not before_exists:
                    before, reason = b"", None
                if before is None:
                    reason = reason or "没有记录这一轮的改前内容"
        targets.append(_Target(
            path=path, before=before, before_exists=before_exists, basis=basis, reason=reason,
            last_after_sha=latest[path].after_sha256,
        ))
    return targets


def _was_created(group: Sequence[FileChangeRecord]) -> bool:
    """首条操作为新建才说明基准不存在；删除后重建不能抹掉原文件。"""
    return bool(group) and group[0].kind == "create"


def _apply_one(
    target: _Target,
    workspace_root: Path,
    *,
    force: bool,
    undo_store: Callable[[bytes], tuple[str | None, str | None]],
) -> tuple[RestoredFile | None, str | None, str | None]:
    """处理一个文件，返回 ``(结果, 跳过原因, 失败原因)``，三者至多一个非空。"""
    if target.before is None:
        return None, target.reason or "没有记录改前内容", None

    try:
        current_path = resolve_within_workspace(
            target.path, base_dir=workspace_root, workspace_root=workspace_root
        )
    except WorkspaceBoundaryError:
        return None, "这个路径不在当前工作区内", None

    exists = current_path.is_file()
    current = current_path.read_bytes() if exists else None
    current_sha = hashlib.sha256(current).hexdigest() if current is not None else None

    # 冲突判定：AI 写完之后没人碰过才自动还原。
    if exists and not force and target.last_after_sha is None:
        # 这个文件最后一次动作是**删除**（after 为空），现在却又存在——是有人重建的。
        # 之前这里直接放行，会把用户重建的内容覆盖掉（2026-10-29 审查发现）。
        return None, "这个文件在 AI 删除之后又被重建，还原会覆盖它当前的内容", None
    if exists and not force and current_sha != target.last_after_sha:
        return None, "这个文件在 AI 改完之后又被改动过", None

    if target.before_exists and exists and current == target.before:
        return RestoredFile(path=target.path, action="unchanged", basis=target.basis), None, None

    if not exists and not target.before_exists:
        # 基准不存在且当前也不存在；原先为空的文件仍需重建。
        return RestoredFile(path=target.path, action="unchanged", basis=target.basis), None, None

    undo_ref = None
    if current is not None:
        # 回滚前快照：内容留在盘上，误还原还能人工找回（不做自动撤销接口）。
        try:
            undo_ref, _reason = undo_store(current)
        except OSError as exc:
            logger.warning("还原前快照失败：%s（%s）", target.path, exc)

    if not target.before_exists:
        current_path.unlink()
        action: Action = "removed"
    else:
        _write_atomic(current_path, target.before)
        action = "written" if exists else "rebuilt"
    return RestoredFile(
        path=target.path,
        action=action,
        basis=target.basis,
        undo_ref=undo_ref,
    ), None, None


def _write_atomic(target: Path, data: bytes) -> None:
    """同目录临时文件 + ``os.replace``：读方不会看到写了一半的文件。

    覆盖已有文件时**沿用原权限位**：``os.replace`` 换的是 inode，不补这一步会让可执行文件
    丢掉 +x。原文件不存在（重建）时用系统默认权限。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    mode: int | None = None
    if target.is_file():
        try:
            mode = target.stat().st_mode & 0o7777
        except OSError:
            mode = None
    temporary = target.with_name(f".{target.name}.{uuid4().hex[:8]}.tmp")
    try:
        temporary.write_bytes(data)
        if mode is not None:
            try:
                os.chmod(temporary, mode)
            except OSError:
                pass
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

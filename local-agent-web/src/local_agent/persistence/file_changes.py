"""会话内的文件变更账本（2026-10-21）。

**为什么需要它**：工具事件里的 diff 只描述"这一次调用改了什么"，一个文件被
``edit_file`` 连改四次，页面上就是四段互不相干的片段；用户真正要回答的问题是
"这个文件从这次任务开始到结束，到底变成了什么样"。

**做法**（与 Codex 的 ``TurnDiffTracker`` 同构，差别是落盘）：每个会话的每个路径，
在**第一次写入前**留一份原文（baseline）；之后整文件 diff = ``diff(baseline, 当前内容)``。
落盘而非内存，是因为页面要跨轮、刷新后仍能点开"变更前 / 变更后"。

边界：
- 只记**工作区内**的文件（路径由写盘层解析后的真实 ``Path`` 传进来）；
- 二进制与超过上限的文件不存原文，只记 ``unavailable_reason``，页面如实说明；
- 记账失败不影响写盘（调用方吞掉异常并写日志），账本是可观测性设施，不是安全门。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from local_agent.exceptions import WorkspaceBoundaryError
from local_agent.security.diff_guard import build_unified_diff, count_diff_lines
from local_agent.security.path_guard import display_path, resolve_within_workspace

logger = logging.getLogger(__name__)

#: 单个文件基线的大小上限。超过就不存原文（只记账），页面显示"文件过大，未保存基线"。
MAX_BASELINE_BYTES = 512 * 1024

#: 单会话"改前内容"的存储上限（压缩后字节，2026-10-29）。超了就不再存新的，
#: 页面按 ``before_reason`` 如实说明；不静默丢。默认值可在设置页调整。
MAX_SESSION_BYTES = 200 * 1024 * 1024

#: 保留多少轮的改前内容（2026-10-29）：更早的轮次只能查看 diff，不能还原。
#: 与 hermes 的 ``max_snapshots=20`` 同口径；0 或负数表示不限制。
KEEP_TURNS = 20

#: 整文件 diff 的展示上限：超过后截断，并在结果里如实标注。
MAX_DIFF_LINES = 20_000

ChangeKind = Literal["create", "modify", "delete"]


@dataclass(frozen=True)
class FileChangeRecord:
    """一次文件写操作的账目（对应 ``file_change_ops`` 一行）。"""

    op_id: int
    run_id: str | None
    tool: str
    path: str
    kind: str
    additions: int
    deletions: int
    before_sha256: str | None
    after_sha256: str | None
    #: 本行"改前内容"的 blob 引用（2026-10-29）。**只有本轮第一次写该路径的那条 op 有**，
    #: 同轮后续的改动这里是 ``None``——整轮还原取的就是这条。
    before_ref: str | None
    #: 没有 ``before_ref`` 时的原因（超限 / 二进制 / 配额用尽 / 已清理），供页面逐条说明。
    before_reason: str | None
    created_at: str


@dataclass(frozen=True)
class FileChangeSummary:
    """按文件聚合后的一行：页面"改动的文件"列表直接用。"""

    path: str
    kind: str
    additions: int
    deletions: int
    edits: int
    last_at: str
    has_baseline: bool
    unavailable_reason: str | None


@dataclass(frozen=True)
class DiffLine:
    kind: Literal["context", "add", "del"]
    old_line: int | None
    new_line: int | None
    text: str


@dataclass(frozen=True)
class DiffHunk:
    header: str
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True)
class FileChangeDiff:
    """整文件 diff 的结果；``has_baseline=False`` 时页面应显示原因而不是空 diff。"""

    path: str
    additions: int
    deletions: int
    hunks: tuple[DiffHunk, ...]
    truncated: bool
    has_baseline: bool
    unavailable_reason: str | None
    before_sha256: str | None
    after_sha256: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def diff_counts(text: str) -> tuple[int, int]:
    """从 unified diff 文本数出 ``(新增, 删除)``；与工具卡片同一口径。"""
    return count_diff_lines(text)


def parse_unified_diff(text: str, *, path: str) -> tuple[tuple[DiffHunk, ...], bool]:
    """把 unified diff 文本还原成结构化 hunk。

    与 ``application/projection.py`` 的解析同口径，但**不受工具卡片的行数上限约束**：
    这里要展示的是整个文件，截断上限见 :data:`MAX_DIFF_LINES`。
    """
    hunks: list[DiffHunk] = []
    lines: list[DiffLine] = []
    header: str | None = None
    old_no = new_no = 0
    kept = 0
    truncated = False
    for raw in text.splitlines():
        if raw.startswith("@@"):
            if header is not None:
                hunks.append(DiffHunk(header=header, lines=tuple(lines)))
            header = raw
            lines = []
            parsed = _parse_hunk_header(raw)
            old_no, new_no = parsed
            continue
        if header is None or raw.startswith(("--- ", "+++ ", "\\")):
            continue
        if kept >= MAX_DIFF_LINES:
            truncated = True
            break
        prefix, body = raw[:1], raw[1:]
        if prefix == "+":
            lines.append(DiffLine(kind="add", old_line=None, new_line=new_no, text=body))
            new_no += 1
        elif prefix == "-":
            lines.append(DiffLine(kind="del", old_line=old_no, new_line=None, text=body))
            old_no += 1
        elif prefix == " ":
            lines.append(DiffLine(kind="context", old_line=old_no, new_line=new_no, text=body))
            old_no += 1
            new_no += 1
        else:
            continue
        kept += 1
    if header is not None:
        hunks.append(DiffHunk(header=header, lines=tuple(lines)))
    return tuple(hunks), truncated


def _parse_hunk_header(header: str) -> tuple[int, int]:
    import re

    match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", header)
    if match is None:
        return 1, 1
    return int(match.group(1)), int(match.group(2))


class FileChangeLedger:
    """一个会话的文件变更账本；``bind_run`` 之后记账才带轮次标识。

    线程安全靠外部传入的锁（与 Checkpoint 共用同一把，见 ``container.open_runtime``）：
    工具在 executor 线程里执行，账本与快照必须串行，不能各写各的。
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        lock: Any,
        session_id: str,
        baseline_dir: Path,
        workspace_root: Path,
        max_baseline_bytes: int = MAX_BASELINE_BYTES,
        max_session_bytes: int = MAX_SESSION_BYTES,
        keep_turns: int = KEEP_TURNS,
    ) -> None:
        self._connection = connection
        self._lock = lock or threading.Lock()
        self._session_id = session_id
        self._baseline_dir = Path(baseline_dir) / session_id
        self._workspace_root = Path(workspace_root)
        self._max_baseline_bytes = max_baseline_bytes
        self._max_session_bytes = max_session_bytes
        self._keep_turns = keep_turns
        self._run_id: str | None = None
        #: 待记账的首次快照；已处理路径独立保留，避免 record 出队后再次采集中间态。
        self._pending: dict[str, tuple[str | None, str | None]] = {}
        self._captured: set[str] = set()
        self._pruned_this_run = False

    def bind_run(self, run_id: str) -> None:
        self._run_id = run_id
        # 新一轮：本轮的"改前内容"重新按路径各留一份（2026-10-29）。
        self._pending.clear()
        self._captured.clear()
        self._pruned_this_run = False

    # ---- 写入侧 ---------------------------------------------------------

    def capture(self, resolved: Path) -> None:
        """在写盘**之前**留一份原文（2026-10-29 起按轮记）。

        两件事同时做：

        1. **按轮**留一份 ``before``（本轮第一次动这个路径时），供"还原本轮"使用；
        2. **会话级**首份 baseline 照旧维护（:meth:`_capture_session_baseline`），
           ``summary()`` / ``diff()`` 仍读它，行为不变。

        同一轮同一路径只留一次：一轮里改四次，只有第一条 op 带 ``before_ref``——
        整轮还原要的就是"本轮开始前"，中间态没有消费方（2026-10-29 定）。
        """
        path = self._display(resolved)
        if path in self._captured:
            return
        if self._run_id is not None:
            # 恢复同一轮时账本对象可能重建；已记账的首次快照仍是本轮起点。
            with self._lock:
                recorded = self._connection.execute(
                    "SELECT 1 FROM file_change_ops WHERE session_id = ? AND run_id = ?"
                    " AND path = ? LIMIT 1",
                    (self._session_id, self._run_id, path),
                ).fetchone()
            if recorded is not None:
                self._captured.add(path)
                return

        # **先清理、再存这一份**（2026-10-29 审查发现）。顺序反了会出这个 bug：prune 的孤儿
        # 判定只认 `file_change_ops.before_ref`，而刚存的 blob 要到 record() 时才被引用，
        # 于是它被当成孤儿删掉，本轮第一个被改的文件永远"改前内容已损坏"（实测复现）。
        self._prune_once()

        data, reason = self._read_before(resolved)
        ref: str | None = None
        if data is not None:
            try:
                ref, store_reason = self._store_blob(data)
            except OSError as exc:
                logger.warning("保存改前内容失败：%s（%s）", path, exc)
                ref, store_reason = None, "改前内容未能保存"
            reason = store_reason
        self._pending[path] = (ref, reason)

        # 会话级基线：原逻辑，超限/二进制时记一条只有原因的行。
        self._capture_session_baseline(path, data, reason)
        self._captured.add(path)

    def _read_before(self, resolved: Path) -> tuple[bytes | None, str | None]:
        """读取"改前内容"并给出不能保存的原因（抽出来供两条存储路径共用）。"""
        if resolved.is_dir():
            return None, "这是一个目录，没有可对比的改前内容"
        if not resolved.is_file():
            # 新建文件也要留基线——留**空串**，否则"新建"在页面上只剩一句"没有改前内容"。
            return b"", None
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            return None, f"读取原文件失败：{exc.strerror or exc}"
        if len(data) > self._max_baseline_bytes:
            return None, f"文件超过 {self._max_baseline_bytes // 1024} KB，未保存改前内容"
        if b"\x00" in data[:8192]:
            return None, "二进制文件，未保存改前内容"
        return data, None

    def _capture_session_baseline(
        self, path: str, data: bytes | None, reason: str | None
    ) -> None:
        """会话级首份基线（v11 语义，``INSERT OR IGNORE``）：``diff()`` 与页面净变化读它。"""
        with self._lock:
            existing = self._connection.execute(
                "SELECT 1 FROM file_change_baselines WHERE session_id = ? AND path = ?",
                (self._session_id, path),
            ).fetchone()
        if existing is not None:
            return

        ref: str | None = None
        if data is not None:
            try:
                self._baseline_dir.mkdir(parents=True, exist_ok=True)
                ref = f"{uuid4().hex}.baseline"
                (self._baseline_dir / ref).write_bytes(data)
            except OSError as exc:
                logger.warning("保存文件基线失败：%s（%s）", path, exc)
                ref = None
                reason = "改前内容未能保存"

        sha = hashlib.sha256(data).hexdigest() if data is not None else None
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO file_change_baselines"
                " (session_id, path, storage_ref, size_bytes, sha256, unavailable_reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._session_id, path, ref, 0 if data is None else len(data), sha, reason, _now()),
            )

    def _store_blob(self, data: bytes) -> tuple[str | None, str | None]:
        """内容寻址保存一份改前内容，返回 ``(sha256, 失败原因)``。

        - **去重**：同一会话内同内容的 blob 只落一份（改回去、多轮内容相同都不额外占）；
        - **压缩**：zlib，源码文本通常到 1/3~1/5，零新依赖；
        - **配额**：超过 ``max_session_bytes`` 就不再存新内容，返回原因让页面如实说明。
          已存在的内容照常复用（它不增加占用）。
        """
        sha = hashlib.sha256(data).hexdigest()
        with self._lock:
            row = self._connection.execute(
                "SELECT stored_bytes FROM file_change_blobs WHERE session_id = ? AND sha256 = ?",
                (self._session_id, sha),
            ).fetchone()
        if row is not None:
            return sha, None

        payload = zlib.compress(data, 6)
        if self._blob_usage() + len(payload) > self._max_session_bytes:
            return None, (
                f"会话的改前内容已占满上限（{self._max_session_bytes // (1024 * 1024)} MB），"
                "这一处没能保存"
            )

        target = self._blob_path(sha)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{sha}.{uuid4().hex[:8]}.tmp")
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            return None, f"改前内容写入失败：{exc.strerror or exc}"

        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO file_change_blobs"
                " (session_id, sha256, size_bytes, stored_bytes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self._session_id, sha, len(data), len(payload), _now()),
            )
        return sha, None

    def store_undo_snapshot(self, data: bytes) -> tuple[str | None, str | None]:
        """把"还原前的内容"另存一份，返回 ``(引用, 失败原因)``，引用形如 ``undo/<sha>``。

        **刻意不进 ``file_change_blobs`` 账目表**（2026-10-29 审查发现）：那张表的孤儿清理
        只认 ``file_change_ops.before_ref``，救援副本放进去会被下一次清理当孤儿删掉——
        "误还原还能找回"就成了空话。这里单独一个目录，**不参与**配额与保留深度：它只在
        用户主动还原时产生（数量 = 还原次数），会话被删除时随目录一起清掉。
        """
        sha = hashlib.sha256(data).hexdigest()
        target = self._undo_dir() / sha[:2] / f"{sha}.z"
        if target.is_file():
            return f"undo/{sha}", None
        payload = zlib.compress(data, 6)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{sha}.{uuid4().hex[:8]}.tmp")
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            return None, f"还原前内容写入失败：{exc.strerror or exc}"
        return f"undo/{sha}", None

    def load_undo_snapshot(self, ref: str) -> bytes | None:
        """按引用读回"还原前的内容"（:meth:`store_undo_snapshot` 写的那一份）。"""
        name = ref.removeprefix("undo/")
        path = self._undo_dir() / name[:2] / f"{name}.z"
        try:
            data = zlib.decompress(path.read_bytes())
        except (OSError, zlib.error) as exc:
            logger.warning("还原前内容读取失败：%s（%s）", ref, exc)
            return None
        if hashlib.sha256(data).hexdigest() != name:
            logger.warning("还原前内容校验不符：%s", ref)
            return None
        return data

    def _undo_dir(self) -> Path:
        return self._baseline_dir / "undo"

    def _blob_path(self, sha: str) -> Path:
        """内容寻址路径：按前两位分片，避免单目录堆上万文件。"""
        return self._baseline_dir / "blobs" / sha[:2] / f"{sha}.z"

    def load_blob(self, sha: str) -> bytes | None:
        """按引用读回改前内容；文件缺失、损坏或校验不符都返回 ``None``（不抛）。"""
        path = self._blob_path(sha)
        try:
            data = zlib.decompress(path.read_bytes())
        except (OSError, zlib.error) as exc:
            logger.warning("改前内容读取失败：%s（%s）", sha, exc)
            return None
        if hashlib.sha256(data).hexdigest() != sha:
            logger.warning("改前内容校验不符，按不可用处理：%s", sha)
            return None
        return data

    def _blob_usage(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(stored_bytes), 0) FROM file_change_blobs WHERE session_id = ?",
                (self._session_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def storage_usage(self) -> dict[str, int]:
        """页面要显示的占用：原始总字节、压缩后总字节、份数。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(size_bytes), 0), COALESCE(SUM(stored_bytes), 0), COUNT(*)"
                " FROM file_change_blobs WHERE session_id = ?",
                (self._session_id,),
            ).fetchone()
        return {
            "size_bytes": int(row[0]) if row else 0,
            "stored_bytes": int(row[1]) if row else 0,
            "blobs": int(row[2]) if row else 0,
        }

    def _prune_once(self) -> None:
        """每轮第一次 capture 时做一次保留深度清理（成本 = 两条 SQL）。"""
        if self._pruned_this_run:
            return
        self._pruned_this_run = True
        try:
            self.prune()
        except Exception:  # 清理失败不该影响写盘：它只是回收空间
            logger.exception("清理超期改前内容失败：会话 %s", self._session_id)

    def delete_session(self) -> None:
        """删掉**本会话**的基线目录（2026-10-29）。

        无参数：账本本身已经绑定在某个会话上（``self._baseline_dir``）。
        库里的行由 `SessionStore.delete_session` 在同一个事务里删——这里只管磁盘，
        因为基线目录不进库，删了库行之后就没有任何东西能反查出它在哪。
        """
        try:
            shutil.rmtree(self._baseline_dir)
        except FileNotFoundError:
            if self._baseline_dir.exists():
                raise

    def prune(self) -> int:
        """只保留最近 ``keep_turns`` 轮的改前内容，返回被解引用（不再可还原）的 op 数。

        ``keep_turns <= 0`` 表示不限制。``run_id`` 为空的历史行（绑定失败）不动：
        它们本来就不属于任何一轮，按轮清理没有依据。
        """
        if self._keep_turns <= 0:
            return 0
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id FROM file_change_ops WHERE session_id = ? AND run_id IS NOT NULL"
                " GROUP BY run_id ORDER BY MIN(op_id) DESC",
                (self._session_id,),
            ).fetchall()
            stale = [row[0] for row in rows[self._keep_turns:]]
            if not stale:
                return 0
            placeholders = ",".join("?" for _ in stale)
            cursor = self._connection.execute(
                f"UPDATE file_change_ops SET before_ref = NULL,"
                f" before_reason = '已清理（超出保留轮数）'"
                f" WHERE session_id = ? AND run_id IN ({placeholders}) AND before_ref IS NOT NULL",
                (self._session_id, *stale),
            )
            removed = cursor.rowcount
            self._connection.commit()
            # 还没被 record() 认领的 blob 不算孤儿：capture 与 record 之间有一小段时间差，
            # 这期间它是"即将被引用"的内容。
            pending = {ref for ref, _reason in self._pending.values() if ref is not None}
            orphans = [
                row[0]
                for row in self._connection.execute(
                    "SELECT sha256 FROM file_change_blobs WHERE session_id = ?"
                    " AND sha256 NOT IN ("
                    "   SELECT before_ref FROM file_change_ops"
                    "   WHERE session_id = ? AND before_ref IS NOT NULL)",
                    (self._session_id, self._session_id),
                )
                if row[0] not in pending
            ]
            if orphans:
                self._connection.executemany(
                    "DELETE FROM file_change_blobs WHERE session_id = ? AND sha256 = ?",
                    [(self._session_id, sha) for sha in orphans],
                )
                self._connection.commit()
        for sha in orphans:
            self._blob_path(sha).unlink(missing_ok=True)
        return max(0, removed)

    def record(
        self,
        *,
        tool: str,
        resolved: Path,
        kind: ChangeKind,
        additions: int = 0,
        deletions: int = 0,
        before_sha256: str | None = None,
        after_sha256: str | None = None,
    ) -> None:
        path = self._display(resolved)
        # 本轮这一条 op 的"改前内容"：只有本轮第一次写这个路径的那条 op 带引用。
        before_ref, before_reason = self._pending.pop(path, (None, None))
        with self._lock:
            self._connection.execute(
                "INSERT INTO file_change_ops (session_id, run_id, tool, path, kind, additions,"
                " deletions, before_sha256, after_sha256, before_ref, before_reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._session_id, self._run_id, tool, path, kind, max(0, additions),
                 max(0, deletions), before_sha256, after_sha256, before_ref, before_reason, _now()),
            )

    # ---- 读取侧 ---------------------------------------------------------

    def ops(self) -> list[FileChangeRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT op_id, run_id, tool, path, kind, additions, deletions,"
                " before_sha256, after_sha256, before_ref, before_reason, created_at"
                " FROM file_change_ops WHERE session_id = ? ORDER BY op_id",
                (self._session_id,),
            ).fetchall()
        return [
            FileChangeRecord(
                op_id=int(row[0]), run_id=row[1], tool=row[2], path=row[3], kind=row[4],
                additions=int(row[5]), deletions=int(row[6]), before_sha256=row[7],
                after_sha256=row[8], before_ref=row[9], before_reason=row[10], created_at=row[11],
            )
            for row in rows
        ]

    def summary(self) -> list[FileChangeSummary]:
        """按路径聚合：累计增删、改动次数、是否有基线。"""
        baselines = {}
        with self._lock:
            for row in self._connection.execute(
                "SELECT path, storage_ref, unavailable_reason FROM file_change_baselines"
                " WHERE session_id = ?",
                (self._session_id,),
            ):
                baselines[row[0]] = (row[1], row[2])
        merged: dict[str, FileChangeSummary] = {}
        for op in self.ops():
            ref, reason = baselines.get(op.path, (None, "未记录改前内容"))
            previous = merged.get(op.path)
            # 「新建」的语义基线是空串：即使没有基线文件（修复前的历史数据），也能看整份内容。
            # 必须把 previous 一起带上——同一文件"先 create 后 modify"时，后一条 op 的 ref 仍是
            # None，只按当前 op 算会把前面判出来的 True 又改回 False（实测整个文件变成不可点）。
            has_baseline = (
                ref is not None
                or op.kind == "create"
                or (previous is not None and previous.has_baseline)
            )
            merged[op.path] = FileChangeSummary(
                path=op.path,
                kind=op.kind,
                additions=op.additions + (previous.additions if previous else 0),
                deletions=op.deletions + (previous.deletions if previous else 0),
                edits=(previous.edits if previous else 0) + 1,
                last_at=op.created_at,
                has_baseline=has_baseline,
                unavailable_reason=None if has_baseline else reason,
            )
        return list(merged.values())

    def session_baseline(self, path: str) -> tuple[str | None, str | None, str | None]:
        """会话级首份基线 ``(storage_ref, unavailable_reason, sha256)``。

        供还原用的**历史降级**：v15 之前的 op 行没有 ``before_ref``，那时只能退回
        "会话开始前"，调用方据此把基准标成 ``session`` 而不是 ``turn``。
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT storage_ref, unavailable_reason, sha256 FROM file_change_baselines"
                " WHERE session_id = ? AND path = ?",
                (self._session_id, path),
            ).fetchone()
        return (None, None, None) if row is None else (row[0], row[1], row[2])

    def load_session_baseline(self, ref: str) -> bytes | None:
        """读会话级基线的原文（与 blob 分开存放，保留 v11 的路径不动）。"""
        try:
            return (self._baseline_dir / ref).read_bytes()
        except OSError as exc:
            logger.warning("会话基线读取失败：%s（%s）", ref, exc)
            return None

    def _was_created(self, path: str) -> bool:
        """这个路径在本会话里是否被「新建」过（用于兼容没有基线文件的历史记录）。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM file_change_ops"
                " WHERE session_id = ? AND path = ? AND kind = 'create' LIMIT 1",
                (self._session_id, path),
            ).fetchone()
        return row is not None

    def diff(self, path: str) -> FileChangeDiff:
        """用基线 + 当前磁盘内容现算**整文件** diff。"""
        # 先校验路径再查账本：接口参数是外部输入，"账本里有没有这一行"不能替代越界判定。
        try:
            current = resolve_within_workspace(
                path, base_dir=self._workspace_root, workspace_root=self._workspace_root
            )
        except WorkspaceBoundaryError:
            return FileChangeDiff(
                path=path, additions=0, deletions=0, hunks=(), truncated=False,
                has_baseline=False, unavailable_reason="这个路径不在当前工作区内",
                before_sha256=None, after_sha256=None,
            )

        with self._lock:
            row = self._connection.execute(
                "SELECT storage_ref, unavailable_reason, sha256 FROM file_change_baselines"
                " WHERE session_id = ? AND path = ?",
                (self._session_id, path),
            ).fetchone()
        # 首次捕获之前写入的"新建文件"记录没有基线文件（修复前的历史数据）：只要这一行里
        # 出现过 `create`，语义上基线就是空串，照样能把整份内容显示成新增。
        ref = row[0] if row is not None else None
        reason = row[1] if row is not None else "这个文件没有记录改前内容（基线）"
        before_sha = row[2] if row is not None else None
        created = ref is None and self._was_created(path)
        if ref is None and not created:
            return FileChangeDiff(
                path=path, additions=0, deletions=0, hunks=(), truncated=False,
                has_baseline=False, unavailable_reason=reason or "改前内容未保存",
                before_sha256=None, after_sha256=None,
            )

        before = ""
        if ref is not None:
            try:
                # 一律按字节读、手动统一换行：`read_text` 会把 CRLF 悄悄转成 LF，而当前文件走的是
                # `read_bytes`，两边口径不一致会把"只改一行"算成"整文件重写"（实测 2 删 2 增）。
                before = (self._baseline_dir / ref).read_bytes().decode("utf-8", errors="replace")
            except OSError as exc:
                return FileChangeDiff(
                    path=path, additions=0, deletions=0, hunks=(), truncated=False,
                    has_baseline=False, unavailable_reason=f"改前内容读取失败：{exc.strerror or exc}",
                    before_sha256=None, after_sha256=None,
                )

        if current.is_file():
            after_bytes = current.read_bytes()
            after = after_bytes.decode("utf-8", errors="replace")
            after_sha = hashlib.sha256(after_bytes).hexdigest()
        else:
            after, after_sha = "", None

        text = build_unified_diff(
            before.replace("\r\n", "\n"), after.replace("\r\n", "\n"), path=path
        )
        hunks, truncated = parse_unified_diff(text, path=path)
        additions = sum(1 for hunk in hunks for line in hunk.lines if line.kind == "add")
        deletions = sum(1 for hunk in hunks for line in hunk.lines if line.kind == "del")
        return FileChangeDiff(
            path=path, additions=additions, deletions=deletions, hunks=hunks,
            truncated=truncated, has_baseline=True, unavailable_reason=None,
            before_sha256=before_sha, after_sha256=after_sha,
        )

    def _display(self, resolved: Path) -> str:
        return display_path(Path(resolved), self._workspace_root)


class FileChangeStore:
    """账本工厂：按 ``base_dir`` 定位基线目录，按会话创建 :class:`FileChangeLedger`。"""

    def __init__(
        self,
        root: Path,
        *,
        max_baseline_bytes: int = MAX_BASELINE_BYTES,
        max_session_bytes: int = MAX_SESSION_BYTES,
        keep_turns: int = KEEP_TURNS,
    ) -> None:
        self._root = Path(root)
        self._max_baseline_bytes = max_baseline_bytes
        self._max_session_bytes = max_session_bytes
        self._keep_turns = keep_turns

    @classmethod
    def under(cls, data_root: Path, **options: int) -> FileChangeStore:
        """与附件存储同一套定位规则：``<data_root>/file-changes``（2026-10-26）。

        ``options`` 是设置页可调的三项（单文件上限 / 会话配额 / 保留轮数），由装配层按当前
        偏好传进来；不传就用模块默认值，CLI 与测试的轻量路径行为不变。
        """
        return cls(Path(data_root) / "file-changes", **options)

    def ledger(
        self,
        connection: sqlite3.Connection,
        *,
        lock: Any,
        session_id: str,
        workspace_root: Path,
        max_baseline_bytes: int | None = None,
        max_session_bytes: int | None = None,
        keep_turns: int | None = None,
    ) -> FileChangeLedger:
        """建一个会话账本；三项口径可以逐次覆盖（设置页改了立即生效）。"""
        return FileChangeLedger(
            connection, lock=lock, session_id=session_id,
            baseline_dir=self._root, workspace_root=workspace_root,
            max_baseline_bytes=(
                self._max_baseline_bytes if max_baseline_bytes is None else max_baseline_bytes
            ),
            max_session_bytes=(
                self._max_session_bytes if max_session_bytes is None else max_session_bytes
            ),
            keep_turns=self._keep_turns if keep_turns is None else keep_turns,
        )

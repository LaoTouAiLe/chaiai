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
import sqlite3
import threading
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
    ) -> None:
        self._connection = connection
        self._lock = lock or threading.Lock()
        self._session_id = session_id
        self._baseline_dir = Path(baseline_dir) / session_id
        self._workspace_root = Path(workspace_root)
        self._max_baseline_bytes = max_baseline_bytes
        self._run_id: str | None = None

    def bind_run(self, run_id: str) -> None:
        self._run_id = run_id

    # ---- 写入侧 ---------------------------------------------------------

    def capture(self, resolved: Path) -> None:
        """在写盘**之前**留一份原文；同一路径只留第一次（与 Codex 的 tracker 同规则）。"""
        path = self._display(resolved)
        with self._lock:
            existing = self._connection.execute(
                "SELECT 1 FROM file_change_baselines WHERE session_id = ? AND path = ?",
                (self._session_id, path),
            ).fetchone()
        if existing is not None:
            return

        data: bytes | None = None
        reason: str | None = None
        if resolved.is_dir():
            reason = "这是一个目录，没有可对比的改前内容"
        elif not resolved.is_file():
            # 新建文件也要留基线——留**空串**。否则"新建"在页面上只剩一句
            # "没有改前内容"，而用户最想看的恰恰是"这个新文件写了什么"。
            data = b""
        elif resolved.is_file():
            try:
                data = resolved.read_bytes()
            except OSError as exc:  # 读不到就如实说明，不让记账把写盘带崩
                reason = f"读取原文件失败：{exc.strerror or exc}"
        if data is not None:
            if len(data) > self._max_baseline_bytes:
                reason = f"文件超过 {self._max_baseline_bytes // 1024} KB，未保存改前内容"
                data = None
            elif b"\x00" in data[:8192]:
                reason = "二进制文件，未保存改前内容"
                data = None

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
        with self._lock:
            self._connection.execute(
                "INSERT INTO file_change_ops (session_id, run_id, tool, path, kind, additions,"
                " deletions, before_sha256, after_sha256, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._session_id, self._run_id, tool, path, kind, max(0, additions),
                 max(0, deletions), before_sha256, after_sha256, _now()),
            )

    # ---- 读取侧 ---------------------------------------------------------

    def ops(self) -> list[FileChangeRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT op_id, run_id, tool, path, kind, additions, deletions,"
                " before_sha256, after_sha256, created_at FROM file_change_ops"
                " WHERE session_id = ? ORDER BY op_id",
                (self._session_id,),
            ).fetchall()
        return [
            FileChangeRecord(
                op_id=int(row[0]), run_id=row[1], tool=row[2], path=row[3], kind=row[4],
                additions=int(row[5]), deletions=int(row[6]), before_sha256=row[7],
                after_sha256=row[8], created_at=row[9],
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

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @classmethod
    def under(cls, base_dir: Path) -> FileChangeStore:
        """与附件存储同一套定位规则：``data/file-changes``。"""
        return cls(Path(base_dir) / "data" / "file-changes")

    def ledger(
        self, connection: sqlite3.Connection, *, lock: Any, session_id: str, workspace_root: Path
    ) -> FileChangeLedger:
        return FileChangeLedger(
            connection, lock=lock, session_id=session_id,
            baseline_dir=self._root, workspace_root=workspace_root,
        )

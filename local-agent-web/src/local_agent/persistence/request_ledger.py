"""幂等请求台账（ARCHITECTURE §15.2、V0_4_DEVELOPMENT_PLAN §6.1）。

它**只保护命令执行**：记录"哪个请求、做了什么操作、参数摘要、关联 `run_id`、状态、结果引用"，
用来拒绝重复提交并让重试关联到原结果。它**不复制 Graph 业务状态**，也不成为第二套任务真源——
真源始终是 Checkpoint。

为什么必须落库而不是放内存：台账要**跨服务重启**保留到会话删除；进程内内存会让"重启后重试"
变成第二次执行（`V0_4_DEVELOPMENT_PLAN` 的 T28 停止条件明确禁止）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Final, TypedDict

#: 操作名：与 §6.1 的两个"执行状态修改"用例一一对应。
OPERATION_SUBMIT: Final[str] = "submit_message"
OPERATION_RESOLVE_APPROVAL: Final[str] = "resolve_approval"
#: 提交一次提问答复（2026-10-28，架构 §15.3）：与审批决定同构的一次"执行状态修改"。
OPERATION_RESOLVE_QUESTION: Final[str] = "resolve_question"

#: 台账状态。只有这三个：执行中、成功、失败。
STATUS_EXECUTING: Final[str] = "executing"
STATUS_COMPLETED: Final[str] = "completed"
STATUS_FAILED: Final[str] = "failed"

#: 进入终态的状态集合（重试可以直接拿结果，不再执行）。
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({STATUS_COMPLETED, STATUS_FAILED})


class LedgerEntry(TypedDict):
    """一条台账记录。"""

    request_id: str
    session_id: str
    operation: str
    arguments_digest: str
    run_id: str | None
    status: str
    result_ref: str | None
    error_code: str | None
    created_at: str
    updated_at: str


class RequestLedger:
    """`request_ledger` 表的读写入口（与 Checkpoint / 会话索引共用一条连接）。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, request_id: str) -> LedgerEntry | None:
        """按请求 id 读取；不存在返回 `None`。"""
        row = self._connection.execute(
            "SELECT request_id, session_id, operation, arguments_digest, run_id, status,"
            " result_ref, error_code, created_at, updated_at"
            " FROM request_ledger WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return LedgerEntry(
            request_id=str(row[0]),
            session_id=str(row[1]),
            operation=str(row[2]),
            arguments_digest=str(row[3]),
            run_id=None if row[4] is None else str(row[4]),
            status=str(row[5]),
            result_ref=None if row[6] is None else str(row[6]),
            error_code=None if row[7] is None else str(row[7]),
            created_at=str(row[8]),
            updated_at=str(row[9]),
        )

    def executing(self, session_id: str) -> list[LedgerEntry]:
        ids = self._connection.execute(
            "SELECT request_id FROM request_ledger WHERE session_id=? AND status=? ORDER BY created_at",
            (session_id, STATUS_EXECUTING),
        ).fetchall()
        return [entry for row in ids if (entry := self.get(str(row[0]))) is not None]

    def latest(self, session_id: str) -> LedgerEntry | None:
        """该会话**最后**一条台账记录；没有则 `None`（重启收口用，架构 §17.5）。

        为什么需要它：任务索引在"结算"之前崩掉时**没有绑定 `run_id`**（`run_id` 与终态是同一次
        写入，见 §17.1），而台账在流式期间就绑定了。取"最后一条"是与 Checkpoint 当前 run 比对的
        前提——更早的请求绑的是更早的轮次，不可能等于当前 run，因此不会误判。
        """
        row = self._connection.execute(
            "SELECT request_id FROM request_ledger WHERE session_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return self.get(str(row[0])) if row is not None else None

    def has_executing(self, session_id: str) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM request_ledger WHERE session_id=? AND status=? LIMIT 1",
            (session_id, STATUS_EXECUTING),
        ).fetchone() is not None

    def start(
        self,
        *,
        request_id: str,
        session_id: str,
        operation: str,
        arguments_digest: str,
        run_id: str | None = None,
    ) -> None:
        """登记"开始执行"。同一个 request_id 重复登记会报错——调用方必须先 `get`。

        `run_id` 已经知道就**在执行前**写进来：它是"这次请求的终态是哪一轮的"的唯一依据。
        提交消息时 run 由 Graph 的 `init` 节点生成，登记时还拿不到，由 :meth:`bind_run`
        在 run 一出现时补写。
        """
        now = _now()
        self._connection.execute(
            "INSERT INTO request_ledger (request_id, session_id, operation, arguments_digest,"
            " run_id, status, result_ref, error_code, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (request_id, session_id, operation, arguments_digest, run_id, STATUS_EXECUTING, now, now),
        )
        self._connection.commit()

    def bind_run(self, request_id: str, *, run_id: str) -> None:
        """把"执行中"的请求绑定到具体一轮 run；已是终态的记录不再改写。

        绑定必须**早于任何副作用**：绑定之后，陈旧的"执行中"记录才可能被证明（或被判定
        证明不了）。终态行不重新绑定——它的 `result_ref` 已经指向某一轮的结果，回头改绑定
        会让那条记录指向别处。
        """
        self._connection.execute(
            "UPDATE request_ledger SET run_id = ?, updated_at = ? WHERE request_id = ? AND status = ?",
            (run_id, _now(), request_id, STATUS_EXECUTING),
        )
        self._connection.commit()

    def finish(
        self,
        request_id: str,
        *,
        status: str,
        run_id: str | None = None,
        result_ref: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """写入终态（或补上 `error_code` 以标记"需要人工核对"）。"""
        self._connection.execute(
            "UPDATE request_ledger SET status = ?, run_id = COALESCE(?, run_id),"
            " result_ref = COALESCE(?, result_ref), error_code = ?, updated_at = ?"
            " WHERE request_id = ?",
            (status, run_id, result_ref, error_code, _now(), request_id),
        )
        self._connection.commit()

    def mark_needs_recovery(self, request_id: str, *, error_code: str) -> None:
        """台账仍为"执行中"但无法证明终态：**保留 executing**，只记错误码。

        不把它改成失败是有意的：终态无法证明时，人工核对后可能仍能补齐结果引用，
        此时重试应当继续认这条记录，而不是变成第二次执行。
        """
        self._connection.execute(
            "UPDATE request_ledger SET error_code = ?, updated_at = ? WHERE request_id = ?",
            (error_code, _now(), request_id),
        )
        self._connection.commit()

    def count(self, *, session_id: str | None = None) -> int:
        """记录条数（诊断与测试用）。"""
        if session_id is None:
            row = self._connection.execute("SELECT COUNT(*) FROM request_ledger").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM request_ledger WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def delete_for_session(self, session_id: str) -> int:
        """会话删除时一并清理（§15.2：台账保留到会话删除）。"""
        cursor = self._connection.execute(
            "DELETE FROM request_ledger WHERE session_id = ?", (session_id,)
        )
        self._connection.commit()
        return int(cursor.rowcount)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

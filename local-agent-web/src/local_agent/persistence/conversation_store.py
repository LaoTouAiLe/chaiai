"""会话树索引。检查点仍是执行真源；本模块只保存路径、分支游标和检索文本。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Branch:
    branch_id: str
    title: str
    fork_turn_id: str | None
    tip_turn_id: str | None
    checkpoint_id: str | None
    position: int
    active: bool


@dataclass(frozen=True)
class ConversationTurn:
    turn_id: str
    parent_turn_id: str | None
    origin_branch_id: str
    run_id: str | None
    question: str
    answer_text: str
    state: str
    submitted_at: str | None
    checkpoint_id: str | None
    ordinal: int


class ConversationStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def branches(self, session_id: str) -> list[Branch]:
        rows = self._connection.execute(
            "SELECT branch_id,title,fork_turn_id,tip_turn_id,checkpoint_id,position,active "
            "FROM conversation_branches WHERE session_id=? ORDER BY position,branch_id",
            (session_id,),
        ).fetchall()
        return [Branch(str(row[0]), str(row[1]), row[2], row[3], row[4], int(row[5]),
                       bool(row[6])) for row in rows]

    def turns(self, session_id: str) -> list[ConversationTurn]:
        rows = self._connection.execute(
            "SELECT turn_id,parent_turn_id,origin_branch_id,run_id,question,answer_text,state,"
            "submitted_at,checkpoint_id,ordinal FROM conversation_turns "
            "WHERE session_id=? ORDER BY ordinal,turn_id", (session_id,),
        ).fetchall()
        return [ConversationTurn(*row) for row in rows]

    def active(self, session_id: str) -> Branch | None:
        return next((branch for branch in self.branches(session_id) if branch.active), None)

    def initialize(self, session_id: str, turns: list[ConversationTurn],
                   checkpoint_id: str | None) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO conversation_branches VALUES (?,?,?,?,?,?,?,?)",
                (session_id, "main", "原讨论", None,
                 turns[-1].turn_id if turns else None, checkpoint_id, 0, 1),
            )
            self._save_turns(session_id, turns)

    def sync(self, session_id: str, branch_id: str, turns: list[ConversationTurn],
             checkpoint_id: str | None) -> None:
        with self._connection:
            self._save_turns(session_id, turns)
            self._connection.execute(
                "UPDATE conversation_branches SET checkpoint_id=?,tip_turn_id=? "
                "WHERE session_id=? AND branch_id=?",
                (checkpoint_id, turns[-1].turn_id if turns else None, session_id, branch_id),
            )

    def _save_turns(self, session_id: str, turns: list[ConversationTurn]) -> None:
        # 已存在节点的父关系和来源分支不可改写；共享祖先不会复制成新的问答。
        for turn in turns:
            self._connection.execute(
                "INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id,turn_id) DO UPDATE SET "
                "answer_text=excluded.answer_text,state=excluded.state,"
                "run_id=COALESCE(excluded.run_id,conversation_turns.run_id),"
                "submitted_at=COALESCE(excluded.submitted_at,conversation_turns.submitted_at),"
                "checkpoint_id=COALESCE(excluded.checkpoint_id,conversation_turns.checkpoint_id)",
                (session_id, turn.turn_id, turn.parent_turn_id, turn.origin_branch_id, turn.run_id,
                 turn.question, turn.answer_text, turn.state, turn.submitted_at,
                 turn.checkpoint_id, turn.ordinal),
            )

    def fork(self, session_id: str, branch_id: str, parent: ConversationTurn) -> None:
        with self._connection:
            position = len(self.branches(session_id))
            self._connection.execute(
                "UPDATE conversation_branches SET active=0 WHERE session_id=?", (session_id,),
            )
            self._connection.execute(
                "INSERT INTO conversation_branches VALUES (?,?,?,?,?,?,?,?)",
                (session_id, branch_id, f"分支 {position}", parent.turn_id, parent.turn_id,
                 parent.checkpoint_id, position, 1),
            )

    def select(self, session_id: str, branch_id: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE conversation_branches SET active=0 WHERE session_id=?", (session_id,),
            )
            self._connection.execute(
                "UPDATE conversation_branches SET active=1 WHERE session_id=? AND branch_id=?",
                (session_id, branch_id),
            )

    def arrange(self, session_id: str, branch_id: str, title: str, position: int) -> None:
        branches = self.branches(session_id)
        selected = next(branch for branch in branches if branch.branch_id == branch_id)
        ordered = [branch for branch in branches if branch.branch_id != branch_id]
        ordered.insert(min(position, len(ordered)), selected)
        with self._connection:
            self._connection.execute(
                "UPDATE conversation_branches SET title=? WHERE session_id=? AND branch_id=?",
                (title, session_id, branch_id),
            )
            self._connection.executemany(
                "UPDATE conversation_branches SET position=? WHERE session_id=? AND branch_id=?",
                [(index, session_id, branch.branch_id) for index, branch in enumerate(ordered)],
            )

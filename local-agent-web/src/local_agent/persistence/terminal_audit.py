"""Metadata only: never store terminal input, output, or environment values."""

import sqlite3
from datetime import datetime, timezone


class TerminalAudit:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def recover(self) -> None:
        self.connection.execute(
            "UPDATE terminal_audit SET status='stopped', updated_at=? "
            "WHERE status IN ('pending', 'running')", (datetime.now(timezone.utc).isoformat(),),
        )
        self.connection.commit()

    def save(self, *, approval_id: str, terminal_id: str | None, cwd: str, shell: str,
             permission_mode: str, fingerprint: str, decision: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT INTO terminal_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(approval_id) DO UPDATE SET terminal_id=excluded.terminal_id, "
            "decision=excluded.decision, status=excluded.status, updated_at=excluded.updated_at",
            (approval_id, terminal_id, cwd, shell, permission_mode,
             fingerprint, decision, status, now, now),
        )
        self.connection.commit()

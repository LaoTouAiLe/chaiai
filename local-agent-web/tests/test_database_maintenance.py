"""T79: 自检停止、WAL 一致快照、新路径恢复及真实 CLI。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_application import ModelFactory, _harness, _open, _session

from local_agent.api.container import acquire_instance_lock
from local_agent.config import AppConfig
from local_agent.persistence import checkpoint
from local_agent.persistence.database_maintenance import (
    DatabaseIntegrityError,
    check_database,
    copy_database,
    verify_connection,
)

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "database_maintenance.py"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=PROJECT, capture_output=True, check=False,
        encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=30,
    )


def test_corrupt_startup_stops_before_migration_and_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "bad.sqlite"
    source.write_bytes(b"This is not a SQLite database" * 100)
    original = source.read_bytes()
    migrated = MagicMock()
    monkeypatch.setattr(checkpoint.migrations, "apply_migrations", migrated)
    with pytest.raises(DatabaseIntegrityError, match="自检失败") as caught:
        checkpoint.open_database(source, workspace_root=tmp_path)
    assert "memory.short_term.path" in str(caught.value)
    migrated.assert_not_called()
    assert source.read_bytes() == original
    # 连接确实关闭：Windows 下可独占移动文件，不靠 GC。
    source.rename(tmp_path / "retained.sqlite")


def test_non_ok_quick_check_is_not_accepted() -> None:
    connection = MagicMock()
    connection.execute.return_value.fetchall.return_value = [("page 2: invalid cell",)]
    with pytest.raises(DatabaseIntegrityError, match="自检失败"):
        verify_connection(connection)


def test_new_database_keeps_the_existing_wal_mode(tmp_path: Path) -> None:
    database = checkpoint.open_database(tmp_path / "new.sqlite", workspace_root=tmp_path)
    try:
        assert database.connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        verify_connection(database.connection)
    finally:
        database.connection.close()


def test_backup_includes_committed_wal_and_excludes_uncommitted_data(tmp_path: Path) -> None:
    source = tmp_path / "wal.sqlite"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE evidence (value TEXT)")
        connection.execute("INSERT INTO evidence VALUES ('committed-in-wal')")
        connection.commit()
        connection.execute("INSERT INTO evidence VALUES ('not-committed')")
        wal = Path(str(source) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        snapshot = copy_database(source, tmp_path / "backup.sqlite")
        reader = sqlite3.connect(snapshot)
        try:
            assert reader.execute("SELECT value FROM evidence").fetchall() == [("committed-in-wal",)]
            assert reader.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        finally:
            reader.close()
        assert not Path(str(snapshot) + "-wal").exists()
    finally:
        connection.rollback()
        connection.close()


def test_missing_source_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        check_database(missing)
    with pytest.raises(FileNotFoundError):
        copy_database(missing, tmp_path / "output.sqlite")
    assert list(tmp_path.iterdir()) == []


def test_existing_destination_and_source_are_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE kept (value TEXT)")
    connection.commit()
    connection.close()
    old = source.read_bytes()
    target = tmp_path / "existing.sqlite"
    target.write_bytes(b"must remain")
    for output in [source, target]:
        with pytest.raises(FileExistsError):
            copy_database(source, output)
    assert target.read_bytes() == b"must remain"
    assert source.read_bytes() == old


def test_backup_failure_never_publishes_or_leaves_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_agent.persistence import database_maintenance

    source = tmp_path / "source.sqlite"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE kept (value TEXT)")
    connection.commit()
    connection.close()
    original = source.read_bytes()
    monkeypatch.setattr(database_maintenance, "BACKUP_TIMEOUT_SECONDS", 0)
    with pytest.raises(TimeoutError):
        copy_database(source, tmp_path / "output.sqlite")
    assert source.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["source.sqlite"]


def test_cli_refuses_an_active_service_and_reports_bad_database(tmp_path: Path) -> None:
    source = tmp_path / "chat.sqlite"
    database = checkpoint.open_database(source, workspace_root=tmp_path)
    database.connection.close()
    with acquire_instance_lock(source):
        result = _cli("backup", "--database", str(source), "--output", str(tmp_path / "backup.sqlite"))
    assert result.returncode == 1 and "已经有一个" in result.stderr
    assert not (tmp_path / "backup.sqlite").exists()
    assert _cli("check", "--database", str(source)).returncode == 0
    corrupt = tmp_path / "bad.sqlite"
    corrupt.write_bytes(b"broken database" * 100)
    result = _cli("check", "--database", str(corrupt))
    assert result.returncode == 1 and "自检失败" in result.stderr


def test_cli_backup_restore_and_continue_the_restored_session(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
) -> None:
    original = _harness(tmp_path, model_factory, app_config, decision="chat", reply="备份前回答")
    sid = _session(original, mode="chat")
    try:
        assert original.service.submit_message(sid, "备份前问题", "backup-first").status == "completed"
        history = original.service.get_session_snapshot(sid).messages
    finally:
        original.database.connection.close()
    original_bytes = original.database_path.read_bytes()
    backup = tmp_path / "snapshots" / "chat.sqlite"
    restored = tmp_path / "restored" / "chat.sqlite"
    for command, source_flag, source, output in [
        ("backup", "--database", original.database_path, backup),
        ("restore", "--backup", backup, restored),
    ]:
        result = _cli(command, source_flag, str(source), "--output", str(output))
        assert result.returncode == 0, result.stderr
    reopened = _open(tmp_path, model_factory, app_config, restored, decision="chat", reply="恢复后回答")
    try:
        assert reopened.service.get_session_snapshot(sid).messages == history
        assert reopened.service.submit_message(sid, "恢复后问题", "backup-second").status == "completed"
        snapshot = reopened.service.get_session_snapshot(sid)
        assert any(getattr(m, "text", None) == "恢复后回答" for m in snapshot.messages)
        assert len([m for m in snapshot.messages if m.kind == "user"]) == 2
    finally:
        reopened.database.connection.close()
    assert original.database_path.read_bytes() == original_bytes
    assert backup.exists()

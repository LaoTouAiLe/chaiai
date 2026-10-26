import sqlite3
from pathlib import Path

import pytest

from local_agent.persistence import migrations
from local_agent.persistence.terminal_audit import TerminalAudit


def _v13(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    migrations._apply(connection, migrations._CHAT_MIGRATIONS[:-1])
    return connection


def test_v14_accepts_existing_compatible_table_without_losing_audit_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    connection = _v13(path)
    connection.execute(migrations._CHAT_V14_TERMINAL_SQL)
    audit = TerminalAudit(connection)
    audit.save(approval_id="existing", terminal_id=None, cwd="workspace", shell="cmd",
               permission_mode="read-only", fingerprint="fingerprint", decision="reject",
               status="rejected")
    before = connection.execute("SELECT * FROM terminal_audit").fetchall()
    assert migrations.current_version(connection) == 13
    assert migrations.apply_migrations(connection) == 14
    assert connection.execute("SELECT * FROM terminal_audit").fetchall() == before
    connection.close()
    with sqlite3.connect(path) as reopened:
        assert migrations.apply_migrations(reopened) == 14
        assert reopened.execute("SELECT * FROM terminal_audit").fetchall() == before
        assert reopened.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=14").fetchone() == (1,)


@pytest.mark.parametrize("existing", [False, True])
def test_v14_rolls_back_schema_and_version_when_registration_fails(
    tmp_path: Path, existing: bool,
) -> None:
    connection = _v13(tmp_path / "interrupted.sqlite")
    if existing:
        connection.execute(migrations._CHAT_V14_TERMINAL_SQL)
    connection.execute("CREATE TRIGGER fail_v14 BEFORE INSERT ON schema_migrations "
                       "WHEN NEW.version=14 BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
        migrations.apply_migrations(connection)
    assert migrations.current_version(connection) == 13
    assert ("terminal_audit" in migrations.table_names(connection)) is existing
    connection.execute("DROP TRIGGER fail_v14")
    connection.commit()
    assert migrations.apply_migrations(connection) == 14
    connection.close()


def test_v14_rejects_incompatible_existing_table_without_stamping_version(tmp_path: Path) -> None:
    connection = _v13(tmp_path / "incompatible.sqlite")
    connection.execute("CREATE TABLE terminal_audit (approval_id TEXT PRIMARY KEY, content TEXT)")
    connection.execute("INSERT INTO terminal_audit VALUES ('old', 'keep this')")
    connection.commit()
    with pytest.raises(sqlite3.DatabaseError, match="表结构不兼容"):
        migrations.apply_migrations(connection)
    assert migrations.current_version(connection) == 13
    assert connection.execute("SELECT * FROM terminal_audit").fetchall() == [("old", "keep this")]
    connection.close()

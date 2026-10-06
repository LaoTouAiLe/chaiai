"""T39：升级与重启后目录移除、会话归档仍保持，历史绑定不变。"""
import sqlite3
from pathlib import Path

from local_agent.persistence import migrations
from local_agent.persistence.session_store import SessionStore
from local_agent.scope import normalize_path


def test_v4_upgrade_backfills_workspaces_and_preserves_sessions(tmp_path: Path) -> None:
    path = tmp_path / "chat.sqlite"
    root = tmp_path / "project"
    root.mkdir()
    connection = sqlite3.connect(path)
    # 按已发布的 v1-v4 脚本生成旧库，不用当前 SessionStore 写新列。
    migrations._apply(connection, migrations._CHAT_MIGRATIONS[:4])
    connection.execute(
        "INSERT INTO sessions (session_id, thread_id, title, workspace_root, project_id, "
        "created_at, updated_at, status, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("old", "thread-old", "旧历史", normalize_path(root), "project", "2026-01-01", "2026-01-01", "active", "chat"),
    )
    connection.commit()
    assert migrations.apply_migrations(connection) == 5
    assert migrations.apply_migrations(connection) == 5
    store = SessionStore(connection, workspace_root=root)
    before = store.get("old")
    assert before is not None and before.mode == "chat" and not before.archived
    assert store.list_workspaces() == [normalize_path(root)]
    store.rename_workspace(root, "保留的名称")
    store.remove_workspace(root)
    store.set_archived("old", True)
    connection.close()

    reopened = sqlite3.connect(path)
    migrations.apply_migrations(reopened)
    store = SessionStore(reopened, workspace_root=root)
    assert store.list_workspaces() == []
    assert store.list_sessions() == []
    assert store.list_sessions(archived=True)[0].thread_id == before.thread_id
    store.set_archived("old", False)
    store.switch("old")
    assert store.list_workspaces() == []
    assert store.get("old").workspace_root == before.workspace_root
    assert store.switch_workspace(root).session_id == "old"
    assert store.workspace_title(root) == "保留的名称"
    assert store.list_workspaces() == [normalize_path(root)]
    reopened.close()

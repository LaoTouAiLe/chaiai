"""MCP Server 开关的持久化（实施文档 M4）。

口径与能力开关一致：**库里只记用户动过的那些**，其余按默认关处理。这条一旦反过来
（把整张表冻结进库），"新加的 server 默认关"对老用户就失效了。
"""

from __future__ import annotations

import json
from pathlib import Path

from local_agent.mcp.state import ServerState
from local_agent.model_settings.store import META_MCP_SERVERS, open_settings_database


def _store(path: Path):
    return open_settings_database(path)


def test_unknown_server_is_disabled_by_default(tmp_path: Path) -> None:
    database = _store(tmp_path / "settings.sqlite")
    try:
        states = database.mcp.states(known=["lanhu", "other"])
    finally:
        database.connection.close()

    assert states["lanhu"] == ServerState(enabled=False)
    assert states["other"].enabled is False


def test_put_then_read_back(tmp_path: Path) -> None:
    database = _store(tmp_path / "settings.sqlite")
    try:
        database.mcp.put(
            "lanhu", ServerState(enabled=True, enabled_tools=("a",), disabled_tools=("b",))
        )
        states = database.mcp.states(known=["lanhu"])
    finally:
        database.connection.close()

    assert states["lanhu"] == ServerState(
        enabled=True, enabled_tools=("a",), disabled_tools=("b",)
    )


def test_put_does_not_touch_other_servers(tmp_path: Path) -> None:
    database = _store(tmp_path / "settings.sqlite")
    try:
        database.mcp.put("alpha", ServerState(enabled=True))
        database.mcp.put("beta", ServerState(enabled=False))
        states = database.mcp.states(known=["alpha", "beta"])
    finally:
        database.connection.close()

    assert states["alpha"].enabled is True
    assert states["beta"].enabled is False


def test_stale_entry_survives_so_the_page_can_explain_it(tmp_path: Path) -> None:
    """配置文件里删掉的 server：库里记录仍在，页面据此显示"配置已删除"而不是凭空消失。"""
    database = _store(tmp_path / "settings.sqlite")
    try:
        database.mcp.put("gone", ServerState(enabled=True))
        states = database.mcp.states(known=["still-here"])
    finally:
        database.connection.close()

    assert "gone" in states
    assert states["still-here"].enabled is False


def test_dirty_rows_fall_back_to_disabled(tmp_path: Path) -> None:
    """``enabled`` 不是布尔时按"没表态"处理——猜成开着是最坏的选择。"""
    database = _store(tmp_path / "settings.sqlite")
    try:
        database.connection.execute(
            "INSERT INTO settings_meta (key, value, updated_at) VALUES (?, ?, ?)",
            (
                META_MCP_SERVERS,
                json.dumps({"bad": {"enabled": "yes"}}),
                "2026-10-20T00:00:00+00:00",
            ),
        )
        database.connection.commit()
        states = database.mcp.states(known=["bad"])
    finally:
        database.connection.close()

    assert states["bad"].enabled is False


def test_forget_removes_the_record(tmp_path: Path) -> None:
    database = _store(tmp_path / "settings.sqlite")
    try:
        database.mcp.put("tmp", ServerState(enabled=True))
        database.mcp.forget("tmp")
        states = database.mcp.states(known=["tmp"])
    finally:
        database.connection.close()

    assert states["tmp"].enabled is False

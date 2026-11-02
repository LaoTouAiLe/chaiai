"""文件改动账本的三项口径可配置（2026-10-29）：默认值同源、偏好覆盖、老设置补齐。

为什么要单独一个文件：这三项有**两个来源**——``config.file_changes``（出厂默认）与设置页
保存的偏好（运行期覆盖）。两处一旦漂移，页面显示的就不是真正生效的值，所以这里把
"默认值一致 / 覆盖生效 / 老设置补默认"三件事都钉死。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from local_agent.config import AppConfig, FileChangesConfig
from local_agent.model_settings.preferences import (
    FileChangesPreferences,
    apply_runtime_preferences,
    complete_preferences,
    initial_preferences,
)
from local_agent.persistence.file_changes import (
    KEEP_TURNS,
    MAX_BASELINE_BYTES,
    MAX_SESSION_BYTES,
    FileChangeStore,
)
from local_agent.persistence.migrations import (
    _CHAT_V11_FILE_CHANGES_SQL,
    _CHAT_V15_FILE_SNAPSHOTS_SQL,
)


def test_config_defaults_match_the_module_constants() -> None:
    """出厂默认与"没走配置装配"时的兜底常量必须一致，否则 CLI / 测试与页面各说一套。"""
    config = FileChangesConfig()
    assert config.max_baseline_kb * 1024 == MAX_BASELINE_BYTES
    assert config.max_session_mb * 1024 * 1024 == MAX_SESSION_BYTES
    assert config.keep_turns == KEEP_TURNS


def test_preferences_take_initial_values_from_config() -> None:
    app = AppConfig(file_changes=FileChangesConfig(max_baseline_kb=1024, keep_turns=7))
    values = initial_preferences(app)
    assert values.file_changes is not None
    assert values.file_changes.max_baseline_kb == 1024
    assert values.file_changes.keep_turns == 7


def test_saved_values_override_config() -> None:
    app = AppConfig()
    values = initial_preferences(app).model_copy(update={
        "file_changes": FileChangesPreferences(
            max_baseline_kb=256, max_session_mb=64, keep_turns=5
        ),
    })
    applied = apply_runtime_preferences(app, values)
    assert applied.file_changes.max_baseline_kb == 256
    assert applied.file_changes.max_session_mb == 64
    assert applied.file_changes.keep_turns == 5


def test_settings_saved_before_this_group_are_filled_from_config() -> None:
    """老设置里没有这一组（``None``）：按启动配置补齐，而不是让页面读到空值。"""
    app = AppConfig(file_changes=FileChangesConfig(max_session_mb=50))
    legacy = initial_preferences(app).model_copy(update={"file_changes": None})
    filled = complete_preferences(legacy, app)
    assert filled.file_changes is not None
    assert filled.file_changes.max_session_mb == 50


def test_ledger_accepts_per_call_overrides(tmp_path: Path) -> None:
    """账本工厂允许逐次覆盖：设置页改完，下一次新建账本就按新值走（不必重启）。"""
    connection = sqlite3.connect(":memory:")
    connection.executescript(_CHAT_V11_FILE_CHANGES_SQL)
    connection.executescript(_CHAT_V15_FILE_SNAPSHOTS_SQL)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ledger = FileChangeStore.under(tmp_path / "fc").ledger(
        connection, lock=threading.Lock(), session_id="s1",
        workspace_root=workspace, max_baseline_bytes=10,
    )
    target = workspace / "a.py"
    target.write_bytes(b"x" * 20)
    ledger.capture(target)

    result = ledger.diff("a.py")
    assert result.has_baseline is False
    assert result.unavailable_reason is not None
    assert "KB" in result.unavailable_reason

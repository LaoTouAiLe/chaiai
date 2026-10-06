"""应用偏好持久化、旧库升级与配置覆盖的边界。"""
from pathlib import Path

import pytest

from local_agent.config import AppConfig
from local_agent.model_settings.preferences import (
    PreferenceConflict,
    advanced_preferences,
    apply_runtime_preferences,
    initial_preferences,
)
from local_agent.model_settings.store import open_settings_database
from local_agent.persistence import migrations


def test_preferences_persist_restart_and_compare_and_swap(tmp_path: Path, app_config: AppConfig) -> None:
    path = tmp_path / "preferences.sqlite"
    database = open_settings_database(path)
    defaults = initial_preferences(app_config)
    assert database.preferences.read() is None
    defaults.execution.max_tool_loops = 17
    defaults.general.theme = "dark"
    assert database.preferences.write(defaults, expected_version=0) == 1
    with pytest.raises(PreferenceConflict):
        database.preferences.write(initial_preferences(app_config), expected_version=0)
    database.connection.close()
    reopened = open_settings_database(path)
    try:
        saved = reopened.preferences.read()
        assert saved is not None and saved[0] == 1 and saved[1].general.theme == "dark"
        assert reopened.preferences.binding(app_config).values.execution.max_tool_loops == 17
        with pytest.raises(PreferenceConflict):
            reopened.preferences.write(defaults, expected_version=2)
        assert migrations.current_version(reopened.connection) == 3
    finally:
        reopened.connection.close()


def test_runtime_overlay_preserves_compression_and_startup_config(app_config: AppConfig) -> None:
    values = initial_preferences(app_config)
    config_before = app_config.model_dump()
    values.execution.shell_timeout_seconds = 7
    values.retrieval.max_files = 43
    values.memory.enabled = False
    binding = values.model_dump(exclude={"general"})
    from local_agent.model_settings.preferences import RuntimePreferences
    runtime = apply_runtime_preferences(app_config, RuntimePreferences.model_validate(binding))
    assert runtime.security.shell.default_timeout_seconds == 7
    assert runtime.rag.max_files == 43
    assert runtime.memory.long_term.enabled is False
    assert runtime.memory.compression == app_config.memory.compression
    assert runtime.llm == app_config.llm
    assert runtime.security.require_approval == app_config.security.require_approval
    assert app_config.model_dump() == config_before


def test_old_preferences_use_loaded_memory_defaults_and_new_values_persist(
    tmp_path: Path, app_config: AppConfig
) -> None:
    from local_agent.config import EmbeddingConfig
    from local_agent.model_settings.preferences import complete_preferences

    config = app_config.model_copy(update={
        "embedding": EmbeddingConfig(model="configured-model", cache_dir=str(tmp_path / "models")),
        "memory": app_config.memory.model_copy(update={
            "medium_term": app_config.memory.medium_term.model_copy(update={"top_k": 13}),
        }),
    })
    path = tmp_path / "legacy-preferences.sqlite"
    database = open_settings_database(path)
    old = initial_preferences(app_config).model_dump_json(exclude={"medium_term", "embedding"})
    database.connection.execute(
        "INSERT INTO app_preferences(id, version, values_json) VALUES(1, 1, ?)", (old,)
    )
    database.connection.commit()
    saved = database.preferences.read()
    assert saved is not None
    values = complete_preferences(saved[1], config)
    assert values.medium_term is not None and values.medium_term.top_k == 13
    assert values.embedding is not None and values.embedding.model == "configured-model"
    values.medium_term.min_score = .555
    values.embedding.local_files_only = True
    database.preferences.write(values, expected_version=1)
    database.connection.close()
    reopened = open_settings_database(path)
    try:
        bound = reopened.preferences.binding(config)
        runtime = apply_runtime_preferences(config, bound.values)
        assert bound.version == 2 and runtime.memory.medium_term.min_score == .555
        assert runtime.embedding.local_files_only is True
        assert runtime.memory.medium_term.path == config.memory.medium_term.path
        assert runtime.memory.medium_term.collection == config.memory.medium_term.collection
    finally:
        reopened.connection.close()


def test_upgrade_from_v2_preserves_existing_settings(tmp_path: Path) -> None:
    database = open_settings_database(tmp_path / "v2.sqlite")
    connection = database.connection
    connection.execute("INSERT INTO settings_meta(key, value, updated_at) VALUES('existing', 'kept', '2026-10-02T00:00:00Z')")
    connection.execute("DROP TABLE app_preferences")
    connection.execute("DELETE FROM schema_migrations WHERE version=3")
    connection.commit()
    assert migrations.current_version(connection) == 2
    assert migrations.apply_settings_migrations(connection) == 3
    assert connection.execute("SELECT value FROM settings_meta WHERE key='existing'").fetchone()[0] == "kept"
    assert database.preferences.read() is None
    connection.close()


@pytest.mark.parametrize("has_preferences", [False, True])
def test_interrupted_v3_upgrade_recovers_on_restart(
    tmp_path: Path, app_config: AppConfig, has_preferences: bool
) -> None:
    path = tmp_path / "interrupted-v3.sqlite"
    database = open_settings_database(path)
    expected = initial_preferences(app_config)
    expected.general.theme = "dark"
    if has_preferences:
        database.preferences.write(expected, expected_version=0)
    # Simulate an exit after the v3 table exists but before its version is recorded.
    database.connection.execute("DELETE FROM schema_migrations WHERE version=3")
    database.connection.commit()
    database.connection.close()

    reopened = open_settings_database(path)
    try:
        assert migrations.current_version(reopened.connection) == 3
        assert migrations.apply_settings_migrations(reopened.connection) == 3
        saved = reopened.preferences.read()
        assert saved == ((1, expected) if has_preferences else None)
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=3"
        ).fetchone()[0] == 1
    finally:
        reopened.connection.close()


def test_advanced_overview_never_returns_configured_paths(app_config: AppConfig) -> None:
    config = app_config.model_copy(update={
        "agents": app_config.agents.model_copy(update={"filenames": ["E:/private/AGENTS.md"]}),
    })
    overview = advanced_preferences(config)
    assert all(isinstance(value, (int, float, bool)) for value in overview.values())
    assert "private" not in str(overview)

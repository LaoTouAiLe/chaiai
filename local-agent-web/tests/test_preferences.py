"""应用偏好持久化、旧库升级与配置覆盖的边界。"""
from pathlib import Path

import pytest

from local_agent.config import AppConfig
from local_agent.model_settings.preferences import (
    AgentsPreferences,
    CompressionPreferences,
    PreferenceConflict,
    advanced_preferences,
    apply_runtime_preferences,
    complete_preferences,
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
        assert migrations.current_version(reopened.connection) == migrations.SETTINGS_SCHEMA_VERSION
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


def test_web_access_preferences_land_in_the_runtime_config_and_survive_restart(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """联网检索偏好：存得下、重启后读得回、应用后进运行期配置（含域名归一）。

    覆盖三件事，缺一件这个页签就只是"能填但不生效"：
    1. ``web_access`` 组的初值来自 ``config.yaml``；
    2. 写进设置库的值在**重启**（重开库）后仍读得回；
    3. ``apply_runtime_preferences`` 把值搬进运行期配置，且 ``enabled`` 不被偏好改动
       （它归能力开关管）。
    """
    path = tmp_path / "web-preferences.sqlite"
    database = open_settings_database(path)
    values = initial_preferences(app_config)
    assert values.web_access is not None
    assert values.web_access.max_results == app_config.web_access.max_results

    values.web_access.max_results = 9
    values.web_access.max_chars = 12_000
    values.web_access.provider = "tavily"
    values.web_access.domain_policy = "allowlist"
    # 域名按 `clean_domain_list` 归一：大写与前导点都会被收敛成策略实际比较的形式。
    values.web_access.allowed_domains = ("Docs.Example.com", ".api.example.com")
    assert database.preferences.write(values, expected_version=0) == 1
    database.connection.close()

    reopened = open_settings_database(path)
    try:
        binding = reopened.preferences.binding(app_config)
        saved = binding.values.web_access
        assert saved is not None
        assert saved.allowed_domains == ("docs.example.com", "api.example.com")
        runtime = apply_runtime_preferences(app_config, binding.values)
        assert runtime.web_access.max_results == 9
        assert runtime.web_access.max_chars == 12_000
        assert runtime.web_access.provider == "tavily"
        assert runtime.web_access.domain_policy == "allowlist"
        assert runtime.web_access.allowed_domains == ("docs.example.com", "api.example.com")
        # 开关仍由能力开关决定：偏好不碰它，也不碰没暴露的字段。
        assert runtime.web_access.enabled == app_config.web_access.enabled
        assert runtime.web_access.user_agent == app_config.web_access.user_agent
        assert runtime.web_access.tavily_api_key_env == app_config.web_access.tavily_api_key_env
    finally:
        reopened.connection.close()


def test_old_preferences_without_web_access_keep_the_loaded_config(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """升级前的设置库没有 ``web_access`` 组：不能因此报错，初值取加载的配置。"""
    from local_agent.model_settings.preferences import complete_preferences

    config = app_config.model_copy(update={
        "web_access": app_config.web_access.model_copy(update={"max_results": 11}),
    })
    path = tmp_path / "legacy-web.sqlite"
    database = open_settings_database(path)
    old = initial_preferences(app_config).model_dump_json(exclude={"web_access"})
    database.connection.execute(
        "INSERT INTO app_preferences(id, version, values_json) VALUES(1, 1, ?)", (old,)
    )
    database.connection.commit()
    saved = database.preferences.read()
    assert saved is not None
    completed = complete_preferences(saved[1], config)
    assert completed.web_access is not None and completed.web_access.max_results == 11
    database.connection.close()


def test_web_access_preference_bounds_match_the_runtime_config(app_config: AppConfig) -> None:
    """边界与 ``WebAccessConfig`` 同口径：偏好那层是本项目里唯一的写入校验点。"""
    from local_agent.model_settings.preferences import WebAccessPreferences

    with pytest.raises(ValueError):
        WebAccessPreferences(max_results=21)
    with pytest.raises(ValueError):
        WebAccessPreferences(max_chars=499)
    with pytest.raises(ValueError):
        WebAccessPreferences(timeout_seconds=0)
    with pytest.raises(ValueError):
        WebAccessPreferences(denied_domains=("http://example.com/path",))


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
    # 把版本号退回到 v2 之前：删 **>= 3** 而不是只删 3——`current_version` 取的是 MAX，
    # 留着更高版本的行会让这条用例假装成"v2 库"却报 4（v4 就是这条迁移加的）。
    connection.execute("DELETE FROM schema_migrations WHERE version>=3")
    connection.commit()
    assert migrations.current_version(connection) == 2
    # 跟着常量走，不写死数字：每加一条迁移这条断言都得改一次，而它想钉的是"按顺序补齐到当前版本"。
    assert migrations.apply_settings_migrations(connection) == migrations.SETTINGS_SCHEMA_VERSION
    assert connection.execute("SELECT value FROM settings_meta WHERE key='existing'").fetchone()[0] == "kept"
    assert database.preferences.read() is None
    connection.close()


@pytest.mark.parametrize("has_preferences", [False, True])
def test_interrupted_latest_upgrade_recovers_on_restart(
    tmp_path: Path, app_config: AppConfig, has_preferences: bool
) -> None:
    path = tmp_path / "interrupted-v3.sqlite"
    database = open_settings_database(path)
    expected = initial_preferences(app_config)
    expected.general.theme = "dark"
    if has_preferences:
        database.preferences.write(expected, expected_version=0)
    # 模拟"**最新那条迁移**的产物已落库、版本号还没记下"就退出（2026-10-16 起最新那条是 v4：
    # 给两张连接表加 `supports_vision`）。这类中断真会发生（v3 的先例），所以最新那条必须幂等——
    # 这条用例正好钉住它：ALTER 若不做存在性判断，重跑会以 `duplicate column name` 炸掉启动。
    database.connection.execute(
        "DELETE FROM schema_migrations WHERE version=?", (migrations.SETTINGS_SCHEMA_VERSION,)
    )
    database.connection.commit()
    # 删掉版本号之后、重开之前：库确实"看起来"停在上一版（这一步是断言，不是猜测）。
    assert (
        migrations.current_version(database.connection) == migrations.SETTINGS_SCHEMA_VERSION - 1
    )
    database.connection.close()

    # 重开 = 启动时重新补齐，这一条就是"恢复"本身：最新那条迁移**必须幂等**，
    # 否则这里会以 `duplicate column name` 直接炸掉启动。
    reopened = open_settings_database(path)
    try:
        assert migrations.current_version(reopened.connection) == migrations.SETTINGS_SCHEMA_VERSION
        assert migrations.apply_settings_migrations(reopened.connection) == migrations.SETTINGS_SCHEMA_VERSION
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

def test_image_context_turns_is_bounded_and_defaults_to_two() -> None:
    """图片上下文轮数：默认 2，只接受 1~10（越界在模型层就挡住）。

    这个值直接决定"历史上几张图会被重发给模型"，端点对每张图固定按 1024 token 计；
    越界值不能靠页面控件的 min/max 兜底——保存接口必须自己拒。
    """
    from pydantic import ValidationError

    from local_agent.model_settings.preferences import AttachmentsPreferences

    assert AttachmentsPreferences().image_context_turns == 2
    for bad in (0, 11):
        with pytest.raises(ValidationError):
            AttachmentsPreferences(image_context_turns=bad)
    assert AttachmentsPreferences(image_context_turns=1).image_context_turns == 1

def test_oss_backend_requires_endpoint_bucket_and_public_url() -> None:
    """选对象存储时，三项必填且公开前缀必须 https——保存时就拒绝，不留到发图才报错。"""
    from pydantic import ValidationError

    from local_agent.model_settings.preferences import AttachmentsPreferences

    assert AttachmentsPreferences().backend == "local"
    with pytest.raises(ValidationError, match="不能为空"):
        AttachmentsPreferences(backend="oss")
    with pytest.raises(ValidationError, match="https"):
        AttachmentsPreferences(
            backend="oss",
            oss_endpoint="https://s3.example.com",
            oss_bucket="images",
            oss_public_base_url="http://cdn.example.com",
        )
    ok = AttachmentsPreferences(
        backend="oss",
        oss_endpoint="https://s3.example.com",
        oss_bucket="images",
        oss_public_base_url="https://cdn.example.com",
    )
    assert ok.backend == "oss" and ok.oss_key_prefix == "chaiai/"


def test_compression_preferences_reach_the_runtime_config(app_config: AppConfig) -> None:
    """压缩四项从"高级只读"搬进可改之后，必须真的回灌到运行配置（2026-10-26）。

    只改字段表、`apply_runtime_preferences` 没接的话，页面显示"已保存"，每轮却仍在用
    config.yaml 里的值——"存了但不生效"是设置页最坏的一种失败。
    """
    values = complete_preferences(initial_preferences(app_config), app_config)
    assert values.compression is not None
    values = values.model_copy(update={"compression": CompressionPreferences(
        enabled=False, trigger_ratio=0.5, preserve_recent_turns=3, retain_ratio=0.1,
    )})

    config = apply_runtime_preferences(app_config, values)

    assert config.memory.compression.enabled is False
    assert config.memory.compression.trigger_ratio == 0.5
    assert config.memory.compression.preserve_recent_turns == 3
    assert config.memory.compression.retain_ratio == 0.1
    # 没搬出来的预算项保持原值：只搬一半比不搬更坏（用户以为调了，实际没调）
    assert (
        config.memory.compression.summary_max_chars
        == app_config.memory.compression.summary_max_chars
    )


def test_compression_retain_ratio_must_stay_below_the_trigger() -> None:
    """保留比例 ≥ 触发比例等于"压完立刻再触发"：拒绝，而不是静默改成合法值。"""
    with pytest.raises(ValueError, match="保留比例必须小于触发比例"):
        CompressionPreferences(trigger_ratio=0.5, retain_ratio=0.5)


def test_agents_preferences_reach_the_runtime_config(app_config: AppConfig) -> None:
    """项目规则三项回灌到运行配置（2026-10-26）。

    ``filenames`` 不搬：它是"哪些文件算规则"，改错就再也读不到用户写的那份。
    """
    values = complete_preferences(initial_preferences(app_config), app_config)
    assert values.agents is not None
    values = values.model_copy(update={"agents": AgentsPreferences(
        enabled=False, max_file_chars=1_000, max_total_chars=5_000,
    )})

    config = apply_runtime_preferences(app_config, values)

    assert config.agents.enabled is False
    assert config.agents.max_file_chars == 1_000
    assert config.agents.max_total_chars == 5_000
    assert config.agents.filenames == app_config.agents.filenames


def test_agents_total_budget_must_cover_one_file() -> None:
    """总上限小于单份上限 = 一份规则都读不全：拒绝，不给"静默只生效一半"的配置。"""
    with pytest.raises(ValueError, match="不能小于单份上限"):
        AgentsPreferences(max_file_chars=5_000, max_total_chars=1_000)

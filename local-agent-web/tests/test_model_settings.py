"""模型连接设置：领域校验、密钥存储、设置库与旧配置导入（T29A；契约 §6.8）。

只测**这一层**能证明的东西：字段规则、版本与历史、密钥不进库、选择的作用域、导入幂等。
"按提交边界构造客户端""切默认只影响下一轮"属于应用服务与 API，在各自的测试文件里验。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from local_agent.application.errors import ApplicationError
from local_agent.application.service import (
    ApplicationService,
    _capabilities_from,
    binding_from_state,
    binding_of,
)
from local_agent.config import AppConfig, LlmConfig
from local_agent.exceptions import (
    ConnectionValidationError,
    ConnectionVersionConflict,
    CredentialStoreError,
    ModelSettingsError,
    SelectionConflict,
)
from local_agent.graph import builder
from local_agent.model_settings import (
    META_EXTERNAL_PATHS_MODE,
    CapabilityReport,
    ConnectionDraft,
    ConnectionStore,
    InMemoryCredentialStore,
    KeyringCredentialStore,
    ModelConnection,
    PermissionStore,
    SelectionRef,
    credential_ref_for,
    current_settings_version,
    endpoint_fingerprint,
    import_legacy_llm,
    open_credential_store,
    open_settings_database,
    probe,
    secrets_from,
    transport_of,
    validate_draft,
)
from local_agent.persistence import checkpoint, migrations
from local_agent.persistence.checkpoint import ChatDatabase
from local_agent.persistence.request_ledger import RequestLedger
from local_agent.security.path_guard import ExternalPathPolicy, normalize_grant_path

SECRET = "sk-do-not-persist-1234567890"


def _draft(**overrides: object) -> ConnectionDraft:
    base: dict[str, object] = {
        "display_name": "主力模型",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "context_window_tokens": 65536,
    }
    base.update(overrides)
    return ConnectionDraft(**base)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> tuple[sqlite3.Connection, ConnectionStore]:
    credentials = InMemoryCredentialStore()
    database = open_settings_database(tmp_path / "settings.sqlite", credentials=credentials)
    return database.connection, database.connections


# ---- 领域校验 ---------------------------------------------------------------


def test_validate_draft_normalizes_and_accepts_a_valid_connection() -> None:
    clean = validate_draft(_draft(display_name="  主力  ", base_url=" https://api.deepseek.com "))

    assert clean.display_name == "主力"
    assert clean.base_url == "https://api.deepseek.com"
    assert clean.max_tokens == 4096  # 默认输出预留
    assert clean.auth_mode == "api_key"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"display_name": "   "}, "连接名称不能为空"),
        ({"model": ""}, "模型 ID 不能为空"),
        ({"base_url": ""}, "端点地址（base_url）不能为空"),
        ({"base_url": "ftp://example.com"}, "必须以 http:// 或 https:// 开头"),
        ({"base_url": "file:///C:/tmp"}, "必须以 http:// 或 https:// 开头"),
        ({"base_url": "https://user:pass@api.example.com"}, "不能内嵌账号或密钥"),
        ({"base_url": "https://"}, "缺少主机名"),
        ({"base_url": "https://api.example.com:notaport"}, "端口不合法"),
        ({"context_window_tokens": 4096, "max_tokens": 4096}, "必须大于输出上限"),
        ({"context_window_tokens": 0}, "必须为正整数"),
        ({"temperature": 3.0}, "temperature 必须在 0 到 2 之间"),
        ({"timeout_seconds": 0}, "超时（timeout_seconds）必须为正整数"),
        ({"retry_count": 11}, "重试次数（retry_count）必须在 0 到 10 之间"),
        ({"provider": "anthropic"}, "只支持 provider=openai_compatible"),
        ({"auth_mode": "oauth"}, "auth_mode 只能是 api_key 或 none"),
        ({"display_name": "x" * 61}, "不能超过 60 个字符"),
    ],
)
def test_validate_draft_rejects_bad_fields_with_actionable_messages(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(ConnectionValidationError) as exc:
        validate_draft(_draft(**overrides))

    assert expected in str(exc.value)


def test_non_loopback_plaintext_http_requires_explicit_confirmation() -> None:
    """§6.8：非回环默认要 HTTPS，明文只有用户明确确认过才能保存。"""
    with pytest.raises(ConnectionValidationError) as exc:
        validate_draft(_draft(base_url="http://192.168.1.20:11434/v1"))

    assert "必须使用 HTTPS" in str(exc.value)
    # 明确确认后可以保存（页面会照常标出明文风险）。
    confirmed = validate_draft(
        _draft(base_url="http://192.168.1.20:11434/v1", allow_plaintext_http=True)
    )
    assert transport_of(confirmed.base_url) == "plaintext-http"


def test_loopback_plaintext_http_needs_no_confirmation() -> None:
    """本机回环的明文端点不需要确认：流量不出机器。"""
    assert validate_draft(_draft(base_url="http://127.0.0.1:11434/v1")).base_url.endswith("/v1")
    assert transport_of("http://localhost:8000/v1") == "loopback-http"
    assert transport_of("http://[::1]:8000/v1") == "loopback-http"
    assert transport_of("https://api.deepseek.com") == "https"


def test_endpoint_fingerprint_ignores_path_and_never_contains_credentials() -> None:
    """端点安全指纹只认 scheme + host + port：可跨进程复现，且不含密钥或路径。"""
    first = endpoint_fingerprint("https://api.deepseek.com/v1")
    second = endpoint_fingerprint("https://api.deepseek.com/other/path")

    assert first == second
    assert first != endpoint_fingerprint("https://api.deepseek.com:8443/v1")
    assert first != endpoint_fingerprint("https://other.example.com/v1")
    assert SECRET not in endpoint_fingerprint(f"https://api.deepseek.com/{SECRET}")


# ---- 密钥存储 ---------------------------------------------------------------


def test_in_memory_store_keeps_secrets_in_process_only() -> None:
    store = InMemoryCredentialStore()
    ref = credential_ref_for("mc-1", 1)

    store.set(ref, SECRET)
    assert store.get(ref) == SECRET
    assert store.kind == "memory"
    assert store.note is not None and "重新录入" in store.note

    store.delete(ref)
    assert store.get(ref) is None


def test_open_credential_store_falls_back_on_non_windows() -> None:
    store = open_credential_store("auto", platform="linux")

    assert store.kind == "memory"
    assert store.note is not None and "POSIX" in store.note


def test_open_credential_store_prefers_the_os_store_on_windows_with_a_working_backend() -> None:
    store = open_credential_store("auto", platform="win32", module=_FakeKeyring())

    assert store.kind == "os"
    assert store.note is None
    ref = credential_ref_for("mc-1", 1)
    store.set(ref, SECRET)
    assert store.get(ref) == SECRET
    store.delete(ref)
    assert store.get(ref) is None


def test_open_credential_store_degrades_when_the_os_store_refuses() -> None:
    """系统凭据存储探测失败 → 临时模式 + 原因，**不是**静默写明文文件。"""
    store = open_credential_store("auto", platform="win32", module=_BrokenKeyring())

    assert store.kind == "memory"
    assert store.note is not None and "降级" in store.note


def test_keyring_store_rejects_a_ref_that_is_not_ours() -> None:
    """引用必须是我们生成的形状：防止有人把密钥本身塞进 `credential_ref` 字段。"""
    store = KeyringCredentialStore(module=_FakeKeyring())

    with pytest.raises(CredentialStoreError):
        store.set(SECRET, "value")
    with pytest.raises(CredentialStoreError):
        store.set("local-agent-web:", "value")


def test_open_credential_store_can_be_forced_to_memory() -> None:
    store = open_credential_store("memory", platform="win32")

    assert store.kind == "memory"
    assert store.note is not None and "临时模式" in store.note


def test_secrets_from_skips_missing_entries() -> None:
    store = InMemoryCredentialStore()
    ref = credential_ref_for("mc-1", 1)
    store.set(ref, SECRET)

    assert secrets_from(store, [ref, None, credential_ref_for("mc-missing", 7)]) == [SECRET]


# ---- 设置库 -----------------------------------------------------------------


def test_open_settings_database_creates_the_settings_tables(tmp_path: Path) -> None:
    connection, _connections = _store(tmp_path)

    assert migrations.table_names(connection) >= set(migrations.SETTINGS_TABLES)
    assert migrations.current_version(connection) == migrations.SETTINGS_SCHEMA_VERSION
    # 幂等：再升一次不动版本。
    assert migrations.apply_settings_migrations(connection) == migrations.SETTINGS_SCHEMA_VERSION


def test_current_settings_version_does_not_create_the_file(tmp_path: Path) -> None:
    missing = tmp_path / "nothing.sqlite"

    assert current_settings_version(missing) is None
    assert not missing.exists()


def test_create_connection_writes_a_version_and_keeps_the_key_out_of_the_database(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)

    created = store.save(_draft(), api_key=SECRET)

    assert created.config_version == 1
    assert created.credential_ref == credential_ref_for(created.connection_id, 1)
    assert created.status == "unverified"
    assert [item.config_version for item in store.versions(created.connection_id)] == [1]
    # **密钥不在库里**：整份数据库文件里都找不到它（结构上没有能放密钥的列）。
    assert SECRET.encode() not in (tmp_path / "settings.sqlite").read_bytes()
    assert store.resolve_api_key(created) == SECRET


def test_key_can_come_from_an_environment_variable_for_imported_connections(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)
    created = store.save(_draft(api_key_env="LEGACY_KEY"), api_key=None)

    assert created.credential_ref is None
    assert store.resolve_api_key(created, environ={}) is None
    assert store.resolve_api_key(created, environ={"LEGACY_KEY": SECRET}) == SECRET


def test_edit_bumps_the_version_and_resets_the_capability_check(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)
    created = store.save(_draft(), api_key=SECRET)
    store.record_check(
        created.connection_id,
        CapabilityReport(chat="pass", tools="pass", streaming="pass"),
        expected_version=created.config_version,
    )
    assert store.require(created.connection_id).status == "verified"

    edited = store.save(
        _draft(display_name="改名了", connection_id=created.connection_id),
        expected_version=created.config_version,
    )

    assert edited.config_version == 2
    assert edited.display_name == "改名了"
    # 端点/参数变了，上一次对旧端点的检测结论不再成立于这条连接。
    assert edited.status == "unverified"
    # 历史版本都还在，且旧版本能按版本号取回来（挂起运行恢复要按固定版本重取）。
    assert [item.config_version for item in store.versions(created.connection_id)] == [1, 2]
    old = store.version(created.connection_id, 1)
    assert old is not None and old.display_name == "主力模型"


def test_probe_result_is_discarded_if_the_connection_changed_mid_probe(tmp_path: Path) -> None:
    """§6.8：探测要花几秒，期间另一端改了端点/模型 → 结论属于**旧配置**，必须丢弃并报冲突。

    硬写上去等于把"旧端点可用"标在新连接上——页面会拿一个没测过的端点当已验证。
    """
    _connection, store = _store(tmp_path)
    created = store.save(_draft(), api_key=SECRET)
    report = CapabilityReport(chat="pass", tools="pass", streaming="pass")

    # 另一个页面在这次探测期间把它改了（产生新版本）。
    store.save(
        _draft(display_name="别人改了", connection_id=created.connection_id),
        expected_version=created.config_version,
    )

    with pytest.raises(ConnectionVersionConflict):
        store.record_check(created.connection_id, report, expected_version=created.config_version)

    # 结果确实被丢弃：新版本仍是"未验证"，没有继承旧端点的结论。
    assert store.require(created.connection_id).status == "unverified"


def test_edit_without_a_key_keeps_the_saved_key(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)
    created = store.save(_draft(), api_key=SECRET)

    edited = store.save(
        _draft(display_name="改名", connection_id=created.connection_id),
        expected_version=created.config_version,
    )

    assert edited.credential_ref == created.credential_ref
    assert store.resolve_api_key(edited) == SECRET


def test_edit_requires_the_current_version(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)
    created = store.save(_draft(), api_key=SECRET)

    with pytest.raises(ConnectionValidationError):
        store.save(_draft(connection_id=created.connection_id))
    with pytest.raises(ConnectionVersionConflict):
        store.save(
            _draft(display_name="别人先改了", connection_id=created.connection_id),
            expected_version=created.config_version + 1,
        )
    with pytest.raises(ConnectionValidationError):
        store.save(_draft(), expected_version=1)  # 新建不该带版本号


def test_keyless_connection_needs_no_credential_reference(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)

    created = store.save(
        _draft(base_url="http://127.0.0.1:11434/v1", auth_mode="none"), api_key=None
    )

    assert created.credential_ref is None
    assert created.requires_key is False
    assert store.resolve_api_key(created) is None


def test_blank_key_is_rejected_instead_of_saved_as_empty(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)

    with pytest.raises(ConnectionValidationError) as exc:
        store.save(_draft(), api_key="   ")

    assert "密钥不能是空白" in str(exc.value)


def test_retire_disables_instead_of_deleting_and_clears_selections(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)
    created = store.save(_draft(), api_key=SECRET)
    store.select_default(created.connection_id)
    store.select_session("s-1", created.connection_id)

    retired = store.retire(created.connection_id)

    assert retired.enabled is False
    assert store.list_connections() == []
    assert [item.connection_id for item in store.list_connections(include_disabled=True)] == [
        created.connection_id
    ]
    # 历史版本与选择记录不被"删除"牵连：版本还在，选择清掉（不能指向已停用的连接）。
    assert [item.config_version for item in store.versions(created.connection_id)] == [1]
    assert store.default_selection() is None
    assert store.session_selection("s-1") is None


def test_retired_connection_cannot_be_edited_or_selected(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)
    created = store.save(_draft(), api_key=SECRET)
    store.retire(created.connection_id)

    with pytest.raises(ModelSettingsError) as exc:
        store.save(
            _draft(connection_id=created.connection_id), expected_version=created.config_version
        )
    assert "已停用" in str(exc.value)
    with pytest.raises(ModelSettingsError):
        store.select_default(created.connection_id)


def test_selection_writes_are_checked_against_the_current_selection(tmp_path: Path) -> None:
    """选择写入要对着**当前选择**校验：两个页面各自选模型时不能互相覆盖（§6.1）。

    这与"目标连接版本"是**两半**：那一半守"我选的这条连接没被改过"，
    这一半守"我看到的当前选择还是当前选择"。少了这一半，甲页面刚选完就会被乙页面悄悄盖掉。
    """
    _connection, store = _store(tmp_path)
    first = store.save(_draft(), api_key=SECRET)
    second = store.save(_draft(display_name="第二条"), api_key=SECRET)

    # 甲页面：全新安装，看到的"当前默认"是空的。
    store.select_default(first.connection_id, expected_selection=SelectionRef(None, None))
    assert store.current_selection_ref("default", "default") == SelectionRef(
        connection_id=first.connection_id, config_version=first.config_version
    )

    # 乙页面还拿着"没有默认"的旧观测 → 拒绝，且当前默认仍是甲选的那条。
    with pytest.raises(SelectionConflict):
        store.select_default(second.connection_id, expected_selection=SelectionRef(None, None))
    assert store.default_selection().connection_id == first.connection_id

    # 乙页面刷新后拿着**看对了**的观测 → 正常写入。
    observed = store.current_selection_ref("default", "default")
    store.select_default(second.connection_id, expected_selection=observed)
    assert store.default_selection().connection_id == second.connection_id

    # 不传观测（内部调用/脚本）仍然可用：HTTP 层把观测设成必填，页面走的路径一定会校验。
    store.select_default(first.connection_id)
    assert store.default_selection().connection_id == first.connection_id


def test_editing_a_selected_connection_advances_both_selection_refs(tmp_path: Path) -> None:
    """编辑只改下一轮配置，但页面读到的选择版本必须与设置库的 CAS 引用一致。"""
    _connection, store = _store(tmp_path)
    chosen = store.save(_draft(), api_key=SECRET)
    other = store.save(_draft(display_name="备用"), api_key=SECRET)
    store.select_default(chosen.connection_id, expected_selection=SelectionRef(None, None))
    store.select_session("s-1", chosen.connection_id, expected_selection=SelectionRef(None, None))

    updated = store.save(
        _draft(connection_id=chosen.connection_id, display_name="更新后"),
        expected_version=chosen.config_version,
    )
    observed = SelectionRef(updated.connection_id, updated.config_version)
    assert store.current_selection_ref("default", "default") == observed
    assert store.current_selection_ref("session", "s-1") == observed
    with pytest.raises(SelectionConflict):
        store.select_default(other.connection_id, expected_selection=SelectionRef(chosen.connection_id, 1))
    store.select_default(other.connection_id, expected_selection=observed)
    assert store.select_session("s-1", None, expected_selection=observed) is None


def test_clearing_a_session_selection_cannot_wipe_another_pages_choice(tmp_path: Path) -> None:
    """"恢复跟随默认"也是一次写入：观测过期同样拒绝，不能把别的页面刚选的那条抹掉。"""
    _connection, store = _store(tmp_path)
    chosen = store.save(_draft(), api_key=SECRET)
    other = store.save(_draft(display_name="第二条"), api_key=SECRET)

    store.select_session("s-1", chosen.connection_id, expected_selection=SelectionRef(None, None))

    with pytest.raises(SelectionConflict):
        store.select_session("s-1", None, expected_selection=SelectionRef(None, None))
    assert store.session_selection("s-1").connection_id == chosen.connection_id

    cleared = store.select_session(
        "s-1",
        None,
        expected_selection=SelectionRef(
            connection_id=chosen.connection_id, config_version=chosen.config_version
        ),
    )
    assert cleared is None
    assert store.session_selection("s-1") is None

    # 换成另一条也要先证明观测没变：过期观测选 C 一样被拒。
    store.select_session("s-1", chosen.connection_id, expected_selection=SelectionRef(None, None))
    with pytest.raises(SelectionConflict):
        store.select_session(
            "s-1", other.connection_id, expected_selection=SelectionRef(None, None)
        )


def test_device_selection_is_checked_against_the_connection_version(tmp_path: Path) -> None:
    """选择也要版本校验：两个页面同时改，后写的那次不能默默覆盖（§6.1）。"""
    _connection, store = _store(tmp_path)
    created = store.save(_draft(), api_key=SECRET)

    store.select_default(created.connection_id, expected_version=created.config_version)
    with pytest.raises(ConnectionVersionConflict):
        store.select_default(created.connection_id, expected_version=created.config_version + 1)


def test_default_and_session_selection_are_separate(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)
    first = store.save(_draft(display_name="A"), api_key=SECRET)
    second = store.save(_draft(display_name="B", model="other-model"), api_key=SECRET)

    store.select_default(first.connection_id)
    store.select_session("s-1", second.connection_id)

    default = store.default_selection()
    session = store.session_selection("s-1")
    assert default is not None and default.connection_id == first.connection_id
    assert session is not None and session.connection_id == second.connection_id
    # 清掉会话选择 = 跟随默认。
    assert store.select_session("s-1", None) is None
    assert store.session_selection("s-1") is None


def test_unknown_connection_is_reported_clearly(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)

    with pytest.raises(ModelSettingsError) as exc:
        store.require("mc-nope")

    assert "连接不存在" in str(exc.value)


# ---- 旧配置导入 -------------------------------------------------------------


def _legacy_llm(**overrides: object) -> LlmConfig:
    base: dict[str, object] = {
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-flash",
        "context_window_tokens": 65536,
    }
    base.update(overrides)
    return LlmConfig(**base)  # type: ignore[arg-type]


def test_legacy_import_happens_once_and_keeps_the_env_key_path(tmp_path: Path) -> None:
    connection, store = _store(tmp_path)

    first = import_legacy_llm(store, _legacy_llm())
    second = import_legacy_llm(store, _legacy_llm())

    assert first.imported is True
    assert second.imported is False and "已经导入过" in second.reason
    imported = store.require(first.connection_id or "")
    assert imported.api_key_env == "DEEPSEEK_API_KEY"
    assert imported.credential_ref is None  # 密钥仍从环境变量取，不进设置库
    assert imported.status == "unverified"  # 没测过就是没测过
    assert [item.connection_id for item in store.list_connections()] == [imported.connection_id]
    # 导入的那条成为默认连接。
    assert store.default_selection() is not None
    assert store.default_selection().connection_id == imported.connection_id  # type: ignore[union-attr]
    assert SECRET.encode() not in (tmp_path / "settings.sqlite").read_bytes()
    assert connection.execute("SELECT COUNT(*) FROM model_connection_versions").fetchone()[0] == 1


def test_legacy_import_without_an_llm_section_leaves_the_door_open(tmp_path: Path) -> None:
    """全新安装无 YAML：不记"已导入"，之后补上配置仍能导入。"""
    _connection, store = _store(tmp_path)

    skipped = import_legacy_llm(store, None)
    later = import_legacy_llm(store, _legacy_llm())

    assert skipped.imported is False and "没有 llm 段" in skipped.reason
    assert later.imported is True


def test_legacy_import_does_not_touch_a_store_that_already_has_connections(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)
    mine = store.save(_draft(), api_key=SECRET)

    result = import_legacy_llm(store, _legacy_llm())

    assert result.imported is False and "已有连接" in result.reason
    assert [item.connection_id for item in store.list_connections(include_disabled=True)] == [
        mine.connection_id
    ]
    # 记账钉死"不导入"：以后即使连接被停用，也不会突然冒出旧的导入连接。
    store.retire(mine.connection_id)
    assert import_legacy_llm(store, _legacy_llm()).imported is False


def test_legacy_import_marks_a_plaintext_lan_endpoint_as_already_confirmed(tmp_path: Path) -> None:
    _connection, store = _store(tmp_path)

    result = import_legacy_llm(store, _legacy_llm(base_url="http://192.168.1.20:11434/v1"))

    assert result.imported is True
    imported = store.require(result.connection_id or "")
    assert imported.allow_plaintext_http is True
    assert imported.transport == "plaintext-http"


# ---- 服务层：提交边界、版本固定与连接用例（§6.8）----------------------------


@dataclass
class _Harness:
    """一套带设置库的服务：真实 Graph + 假模型 + 进程内凭据存储。"""

    service: ApplicationService
    database: ChatDatabase
    store: ConnectionStore
    built: list[tuple[LlmConfig, str]]
    workspace: Path
    session_id: str


def _service(
    tmp_path: Path,
    model_factory: Callable[..., Any],
    app_config: AppConfig,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    session_only: bool = False,
) -> _Harness:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    database = checkpoint.open_database(
        tmp_path / "data" / "checkpoints.sqlite", workspace_root=workspace
    )
    # 默认用"系统凭据存储可用"的口径（假后端，不碰本机凭据管理器）：密钥重启后仍在。
    # `session_only=True` 换成临时模式，用来验"重启后需重新录入"的提示口径。
    credentials: Any = (
        InMemoryCredentialStore()
        if session_only
        else KeyringCredentialStore(module=_FakeKeyring())
    )
    settings = open_settings_database(
        tmp_path / "data" / "settings.sqlite", credentials=credentials
    )
    built: list[tuple[LlmConfig, str]] = []

    def build_fake() -> Any:
        if tool_calls:
            return model_factory(decision="tool", reply="好的", tool_calls=tool_calls)
        return model_factory(decision="chat", reply="假回答")

    def factory(config: LlmConfig, api_key: str) -> Any:
        built.append((config, api_key))
        return build_fake()

    service = ApplicationService(
        graph=builder.build_chat_graph(database.checkpointer),
        app_config=app_config,
        model_adapter=build_fake(),
        workspace_root=workspace,
        sessions=database.sessions,
        ledger=RequestLedger(database.connection),
        secrets=(SECRET,),
        connections=settings.connections,
        model_factory=factory,
    )
    summary = service.create_session(title="会话", workspace_root=workspace)
    return _Harness(service, database, settings.connections, built, workspace, summary.session_id)


def test_service_lists_saves_selects_and_retires_connections(
    tmp_path: Path, model_factory: Callable[..., Any], app_config: AppConfig
) -> None:
    harness = _service(tmp_path, model_factory, app_config)
    created = harness.service.save_model_connection(_draft(), api_key=SECRET)

    assert created.key_status == "configured"
    assert created.status == "unverified"  # 没测过就是没测过
    assert created.config_version == "1"
    assert created.transport == "https"
    # 没录密钥的连接：状态是 missing；读接口永远不返回密钥本身。
    keyless = harness.service.save_model_connection(_draft(display_name="没钥匙", model="m2"))
    assert keyless.key_status == "missing"

    listed = harness.service.list_model_connections()
    assert [item.connection_id for item in listed] == [created.connection_id, keyless.connection_id]
    assert [item.is_default for item in listed] == [False, False]

    default = harness.service.select_default_model(created.connection_id)
    assert default.is_default is True
    assert harness.service.default_model_connection() == created.connection_id

    harness.service.select_session_model(harness.session_id, keyless.connection_id)
    assert harness.service.session_model_connection(harness.session_id) == keyless.connection_id
    assert harness.service.select_session_model(harness.session_id, None) is None
    assert harness.service.session_model_connection(harness.session_id) is None

    # "删除"= 停用：从可用列表消失，历史与版本仍在库里。
    harness.service.retire_model_connection(created.connection_id)
    assert [item.connection_id for item in harness.service.list_model_connections()] == [
        keyless.connection_id
    ]
    assert len(harness.store.list_connections(include_disabled=True)) == 2


def test_key_status_reports_the_temporary_mode_honestly(
    tmp_path: Path, model_factory: Callable[..., Any], app_config: AppConfig
) -> None:
    """系统凭据存储不可用时，读接口要标"仅本次进程内"，让页面能说"重启后需要重新录入"。"""
    harness = _service(tmp_path, model_factory, app_config, session_only=True)

    created = harness.service.save_model_connection(_draft(), api_key=SECRET)

    assert created.key_status == "session_only"


def test_service_reports_conflicts_and_unknown_connections(
    tmp_path: Path, model_factory: Callable[..., Any], app_config: AppConfig
) -> None:
    harness = _service(tmp_path, model_factory, app_config)
    created = harness.service.save_model_connection(_draft(), api_key=SECRET)

    with pytest.raises(ApplicationError) as conflict:
        harness.service.save_model_connection(
            _draft(display_name="改名", connection_id=created.connection_id),
            expected_version=int(created.config_version) + 1,
        )
    assert conflict.value.code == "MODEL_SETTINGS_CONFLICT"

    with pytest.raises(ApplicationError) as missing:
        harness.service.select_default_model("mc-nope")
    assert missing.value.code == "MODEL_CONNECTION_NOT_FOUND"

    harness.service.retire_model_connection(created.connection_id)
    with pytest.raises(ApplicationError) as retired:
        harness.service.test_model_connection(created.connection_id)
    assert retired.value.code == "MODEL_CONNECTION_NOT_FOUND"


def test_submit_without_any_connection_is_refused_with_a_configuration_error(
    tmp_path: Path, model_factory: Callable[..., Any], app_config: AppConfig
) -> None:
    harness = _service(tmp_path, model_factory, app_config)

    with pytest.raises(ApplicationError) as caught:
        harness.service.submit_message(harness.session_id, "你好", "req-1")

    assert caught.value.code == "MODEL_CONNECTION_REQUIRED"
    assert harness.built == []  # 一个请求都没有发出去

    # 关键：这次失败**不该留下"执行中"台账**。用户按提示配好连接后拿同一个请求 id 重试，
    # 必须正常跑完——而不是被判成上一次进程留下的陈旧请求（RECOVERY_REQUIRED）。
    connection = harness.service.save_model_connection(_draft(), api_key=SECRET)
    harness.service.select_default_model(connection.connection_id)

    retried = harness.service.submit_message(harness.session_id, "你好", "req-1")

    assert retried.status == "completed"
    assert [config.model for config, _key in harness.built] == ["deepseek-flash"]


def test_submit_builds_the_client_from_the_connection_and_pins_it_in_state(
    tmp_path: Path, model_factory: Callable[..., Any], app_config: AppConfig
) -> None:
    harness = _service(tmp_path, model_factory, app_config)
    connection = harness.service.save_model_connection(
        _draft(model="conn-model", temperature=0.9, context_window_tokens=32000, max_tokens=1000),
        api_key=SECRET,
    )
    harness.service.select_default_model(connection.connection_id)

    result = harness.service.submit_message(harness.session_id, "你好", "req-1")

    assert result.status == "completed"
    # 客户端是按**连接**构造的，密钥来自凭据存储（而不是注入的那个旧适配器）。
    assert [config.model for config, _key in harness.built] == ["conn-model"]
    assert harness.built[0][0].temperature == 0.9
    assert harness.built[0][0].context_window_tokens == 32000
    assert harness.built[0][1] == SECRET

    # 绑定写进了 State：只有引用与标识，没有密钥。
    values = harness.service._read(harness.service._session_or_raise(harness.session_id)).values
    binding = values["model_binding"]
    assert binding["connection_id"] == connection.connection_id
    assert binding["config_version"] == 1
    assert binding["model"] == "conn-model"
    assert binding["credential_ref"] == credential_ref_for(connection.connection_id, 1)
    assert SECRET not in json.dumps(binding)


def test_awaiting_approval_keeps_the_pinned_version_when_settings_change(
    tmp_path: Path, model_factory: Callable[..., Any], app_config: AppConfig
) -> None:
    """§6.8 的停止条件：切换/编辑设置**不得**改动正在等待审批的运行。"""
    harness = _service(
        tmp_path,
        model_factory,
        app_config,
        tool_calls=[{"name": "delete_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    connection = harness.service.save_model_connection(_draft(model="v1-model"), api_key=SECRET)
    harness.service.select_default_model(connection.connection_id)
    # 先把它记成"工具能力已验证"，否则需要工具的任务会被明确拒绝（这正是 §6.8 要求的门禁；
    # 探测本身的判定另有专门的用例）。
    harness.store.record_check(
        connection.connection_id,
        CapabilityReport(chat="pass", tools="pass", streaming="pass"),
        expected_version=1,
    )
    (harness.workspace / "note.txt").write_text("保留我", encoding="utf-8", newline="")

    pending = harness.service.submit_message(harness.session_id, "删除 note.txt", "req-1")
    assert pending.status == "awaiting_approval"
    approval = harness.service.get_session_snapshot(harness.session_id).approval
    assert approval is not None
    harness.built.clear()

    # 设置变了：编辑这条连接（产生 v2）、再加一条并设为默认。
    other = harness.service.save_model_connection(_draft(display_name="另一条", model="other-model"), api_key=SECRET)
    # 同时**轮换密钥**：v1 的运行必须仍用 v1 的凭据（凭据引用按版本保存，否则会拿到 v2 的 Key）。
    harness.service.save_model_connection(
        _draft(display_name="改名", model="v2-model", connection_id=connection.connection_id),
        api_key="sk-rotated-key-for-v2",
        expected_version=1,
    )
    harness.service.select_default_model(other.connection_id)

    approved = harness.service.resolve_approval(harness.session_id, approval.approval_id, "approve", "req-2")

    assert approved.status == "completed"
    # 恢复用的是**这一轮固定的 v1**，不是当前的 other / v2。
    assert [config.model for config, _key in harness.built] == ["v1-model"]
    # 密钥也按版本取：轮换后 v1 仍用 v1 的密钥（凭据引用带版本，不是"一条连接一个引用"）。
    assert [key for _config, key in harness.built] == [SECRET]
    assert not (harness.workspace / "note.txt").exists()


def test_connection_without_a_key_is_refused_before_any_request(
    tmp_path: Path, model_factory: Callable[..., Any], app_config: AppConfig
) -> None:
    harness = _service(tmp_path, model_factory, app_config)
    connection = harness.service.save_model_connection(_draft())
    harness.service.select_default_model(connection.connection_id)

    with pytest.raises(ApplicationError) as caught:
        harness.service.submit_message(harness.session_id, "你好", "req-1")

    assert caught.value.code == "MODEL_AUTH_FAILED"
    assert harness.built == []


def test_only_a_proven_unsupported_connection_closes_tool_calling(
    tmp_path: Path, model_factory: Callable[..., Any], app_config: AppConfig
) -> None:
    """只有**端点明确拒绝**工具能力才关掉它（§6.8 拦的是缺失，不是"还没测过"）。"""
    harness = _service(tmp_path, model_factory, app_config)
    created = harness.service.save_model_connection(_draft(), api_key=SECRET)
    harness.service.select_default_model(created.connection_id)

    stored = harness.store.require(created.connection_id)
    assert _capabilities_from(stored.capability).tool_calling is True  # 未验证：照常可用
    unsupported = CapabilityReport(chat="pass", tools="unsupported", streaming="pass")
    assert _capabilities_from(unsupported).tool_calling is False
    verified = CapabilityReport(chat="pass", tools="pass", streaming="pass")
    assert _capabilities_from(verified).tool_calling is True


def test_binding_from_state_ignores_a_malformed_value() -> None:

    assert binding_from_state({}) is None
    assert binding_from_state({"model_binding": "not-a-mapping"}) is None
    assert binding_from_state({"model_binding": {"config_version": 2}}) is None

    connection = ModelConnection(
        connection_id="mc-1",
        display_name="连接",
        base_url="https://api.deepseek.com",
        model="m",
        context_window_tokens=8000,
        max_tokens=1000,
        temperature=0.2,
        timeout_seconds=60,
        retry_count=1,
        streaming=False,
        auth_mode="api_key",
        credential_ref=credential_ref_for("mc-1", 1),
        config_version=3,
    )
    binding = binding_of(connection)
    assert binding_from_state({"model_binding": binding}) == binding
    assert binding["endpoint_fingerprint"] == endpoint_fingerprint("https://api.deepseek.com")


# ---- 测试连接（探测）--------------------------------------------------------


class _FakeProbeClient:
    """只实现探测用到的三个方法：`invoke` / `bind_tools().invoke` / `stream`。"""

    def __init__(
        self,
        *,
        tool_call: bool = False,
        chat_error: BaseException | None = None,
        tools_error: BaseException | None = None,
        stream_chunks: int = 1,
    ) -> None:
        self._tool_call = tool_call
        self._chat_error = chat_error
        self._tools_error = tools_error
        self._chunks = stream_chunks

    def invoke(self, _prompt: str) -> Any:
        if self._chat_error is not None:
            raise self._chat_error
        if self._tool_call:
            return AIMessage(
                content="",
                tool_calls=[{"name": "_probe_tool", "args": {"text": "pong"}, "id": "call-1"}],
            )
        return AIMessage(content="pong")

    def bind_tools(self, _tools: list[Any]) -> _FakeProbeClient:
        if self._tools_error is not None:
            raise self._tools_error
        return self

    def stream(self, _prompt: str) -> list[Any]:
        return [AIMessage(content="chunk")] * self._chunks


def _connection() -> ModelConnection:
    return ModelConnection(
        connection_id="mc-probe",
        display_name="探测用",
        base_url="https://api.deepseek.com",
        model="m",
        context_window_tokens=8000,
        max_tokens=1000,
        temperature=0.2,
        timeout_seconds=60,
        retry_count=1,
        streaming=False,
        auth_mode="api_key",
        credential_ref=None,
        config_version=1,
    )


def test_probe_reports_each_capability_separately() -> None:
    report = probe.probe_connection(
        _connection(),
        build_model=lambda _llm, _key: _FakeProbeClient(tool_call=True),
        api_key="k",
    )

    assert (report.chat, report.tools, report.streaming) == ("pass", "pass", "pass")
    assert report.status == "verified"
    assert report.checked_at is not None


def test_probe_marks_unsupported_tools_and_a_model_that_skips_tools() -> None:
    unsupported = probe.probe_connection(
        _connection(),
        build_model=lambda _llm, _key: _FakeProbeClient(
            tools_error=RuntimeError("400 invalid_request_error: tools not supported")
        ),
        api_key="k",
    )
    assert unsupported.chat == "pass"
    assert unsupported.tools == "unsupported"
    assert unsupported.status == "unsupported"

    # 端点接受 tools 但模型这次没调用：那是模型行为，记 unknown，不当作能力缺陷。
    skipped = probe.probe_connection(
        _connection(), build_model=lambda _llm, _key: _FakeProbeClient(), api_key="k"
    )
    assert skipped.tools == "unknown"
    assert skipped.chat == "pass"
    assert skipped.status == "unverified"


def test_redaction_covers_the_legacy_env_key_and_a_newly_saved_one(
    tmp_path: Path,
    model_factory: Callable[..., Any],
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自定义 `api_key_env` 的连接：环境变量取来的**旧** Key 与页面新录入的 Key **都要**脱敏（复审 #2）。

    为什么单挑"自定义格式"：标准名不写 `api_key_env` 也可能被取到，而用户自己的变量名
    （`MY_COMPANY_LLM_KEY`）只能靠 `api_key_env` 显式解析。两条来源的覆盖面必须都在：
    旧 Key 只存在于环境里（设置库没有它），新 Key 只存在于凭据存储里（启动时收不到）——
    少任何一边，密钥都会漏进快照、SSE 或诊断文案。
    """
    legacy_key = "sk-legacy-env-key-9876543210"
    typed_key = "sk-typed-in-page-1234567890"
    monkeypatch.setenv("MY_COMPANY_LLM_KEY", legacy_key)
    harness = _service(tmp_path, model_factory, app_config)

    created = harness.service.save_model_connection(
        _draft(api_key_env="MY_COMPANY_LLM_KEY"), api_key=None
    )
    # 旧路径（环境变量）取到的密钥已进入脱敏集合；新密钥还没出现。
    assert legacy_key in harness.service._secrets
    assert typed_key not in harness.service._secrets

    harness.service.save_model_connection(
        _draft(connection_id=created.connection_id, api_key_env="MY_COMPANY_LLM_KEY"),
        api_key=typed_key,
        expected_version=int(created.config_version),
    )
    # 新录入的立刻并入；旧的**只增不减**（历史文本里可能还留着它）。
    assert typed_key in harness.service._secrets
    assert legacy_key in harness.service._secrets

    # 两个密钥都没有进设置库文件：脱敏集合是内存里的，不是又抄了一份到磁盘。
    settings_bytes = (tmp_path / "data" / "settings.sqlite").read_bytes()
    assert legacy_key.encode() not in settings_bytes
    assert typed_key.encode() not in settings_bytes


def test_restart_collects_historical_version_keys_for_redaction(tmp_path: Path) -> None:
    """旧版本凭据供审批恢复保留，也必须在重启后继续参与展示脱敏。"""
    credentials = InMemoryCredentialStore()
    path = tmp_path / "settings.sqlite"
    first_database = open_settings_database(path, credentials=credentials)
    first = first_database.connections.save(_draft(), api_key="CUSTOM_OLD_SECRET_12345")
    first_database.connections.save(
        _draft(connection_id=first.connection_id, display_name="轮换后"),
        api_key="CUSTOM_NEW_SECRET_67890",
        expected_version=first.config_version,
    )
    first_database.connection.close()

    reopened = open_settings_database(path, credentials=credentials)
    assert reopened.connections.secret_values() == [
        "CUSTOM_NEW_SECRET_67890",
        "CUSTOM_OLD_SECRET_12345",
    ]
    reopened.connection.close()


def test_probe_redacts_failures_and_classifies_them() -> None:
    report = probe.probe_connection(
        _connection(),
        build_model=lambda _llm, _key: _FakeProbeClient(
            chat_error=RuntimeError(f"401 Unauthorized: invalid api key {SECRET}")
        ),
        api_key=SECRET,
    )

    assert report.chat == "fail"
    assert report.status == "failed"
    # 消息可执行、已脱敏：不回显供应商原文，也不出现密钥。
    assert SECRET not in (report.message or "")
    assert "鉴权" in (report.message or "")


def test_probe_reports_a_construction_failure_instead_of_raising() -> None:
    def exploding(_llm: Any, _key: str) -> Any:
        raise RuntimeError("bad base_url")

    report = probe.probe_connection(_connection(), build_model=exploding, api_key="k")

    assert report.chat == "fail"
    assert report.tools == "unknown" and report.streaming == "unknown"
    assert "无法构造客户端" in (report.message or "")


# ---- 权限：外部路径档位与授权（T38）------------------------------------------


def _permissions(
    tmp_path: Path, **kwargs: Any
) -> tuple[sqlite3.Connection, PermissionStore]:
    database = open_settings_database(
        tmp_path / "settings.sqlite", credentials=InMemoryCredentialStore(), **kwargs
    )
    return database.connection, database.permissions


def test_settings_v2_upgrade_adds_the_grants_table_without_touching_older_rows(
    tmp_path: Path,
) -> None:
    """T38 的结构升级：老库（v1）升到 v2 只是加一张表，既有连接照旧。"""
    connection, store = _store(tmp_path)
    store.save(_draft(api_key_env="LEGACY_KEY"), api_key=None)
    # 造一个"上一次运行留下的 v1 库"：撤掉 v2 的痕迹再升级。
    connection.execute("DROP TABLE external_path_grants")
    connection.execute("DELETE FROM schema_migrations WHERE version >= 2")
    connection.commit()

    assert migrations.apply_settings_migrations(connection) == migrations.SETTINGS_SCHEMA_VERSION
    assert "external_path_grants" in migrations.table_names(connection)
    assert [item.display_name for item in store.list_connections()] == ["主力模型"]


def test_permission_store_prefers_the_user_choice_over_the_factory_default(
    tmp_path: Path,
) -> None:
    """档位的唯一真源是设置库；``config.yaml`` 只在库里还没有用户选择时兜底。"""
    _connection, permissions = _permissions(tmp_path)
    assert permissions.mode() == "whitelist" and permissions.default_mode == "whitelist"

    permissions.set_mode("deny")

    # 重开：出厂默认换了，用户的选择仍然生效（改档位不写回 config.yaml）。
    reopened = open_settings_database(
        tmp_path / "settings.sqlite", default_external_paths_mode="allow"
    )
    assert reopened.permissions.default_mode == "allow"
    assert reopened.permissions.mode() == "deny"
    reopened.connection.close()


def test_permission_store_falls_back_to_the_factory_default_on_unknown_mode(
    tmp_path: Path,
) -> None:
    """库里出现不认识的档位文本：回落到出厂默认，判定层不会拿到非法档位。"""
    connection, permissions = _permissions(tmp_path)
    connection.execute(
        "INSERT INTO settings_meta (key, value, updated_at) VALUES (?, ?, ?)",
        (META_EXTERNAL_PATHS_MODE, "yolo", "2026-01-01T00:00:00+00:00"),
    )

    assert permissions.mode() == "whitelist"


def test_grant_normalizes_rewrites_and_revokes(tmp_path: Path) -> None:
    """同一目录的另一种写法落到同一行：是**改写**访问级别，不是新增两条。"""
    _connection, permissions = _permissions(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    created = permissions.grant(outside, "read")
    assert created.path == normalize_grant_path(outside)

    rewritten = permissions.grant(outside.as_posix(), "read_write")

    assert rewritten.path == created.path
    assert rewritten.access == "read_write"
    assert rewritten.created_at == created.created_at  # 创建时间保留
    assert [item.path for item in permissions.grants()] == [created.path]

    assert permissions.revoke(outside) is True
    assert permissions.grants() == []
    assert permissions.revoke(outside) is False  # 撤销不存在的条目也是成功（幂等）


@pytest.mark.skipif(os.name != "nt", reason="大小写归一化只对 Windows 盘符路径有意义")
def test_grant_normalization_unifies_case_and_separators(tmp_path: Path) -> None:
    _connection, permissions = _permissions(tmp_path)
    outside = tmp_path / "Outside"
    outside.mkdir()

    permissions.grant(str(outside).replace(os.sep, "/"), "read")

    assert [item.path for item in permissions.grants()] == [normalize_grant_path(outside)]
    # 大写写法命中同一条（同一目录两次授权不会变成两行）。
    assert normalize_grant_path(str(outside).upper()) == normalize_grant_path(outside)


def test_grant_with_unknown_access_level_reads_back_as_read_only(tmp_path: Path) -> None:
    """库里出现不认识的级别：按**只读**处理（失败闭合），不放宽成可写。"""
    connection, permissions = _permissions(tmp_path)
    connection.execute(
        "INSERT INTO external_path_grants (path, access, created_at, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (
            normalize_grant_path(tmp_path / "outside"),
            "sudo",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )

    assert [item.access for item in permissions.grants()] == ["read"]


def test_permission_policy_reflects_the_current_mode_and_grants(tmp_path: Path) -> None:
    _connection, permissions = _permissions(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    permissions.grant(outside, "read")

    policy = permissions.policy()

    assert isinstance(policy, ExternalPathPolicy)
    assert policy.mode == "whitelist"
    assert policy.access_for(outside.resolve()) == "read"
    # 档位改成 deny 后，已授权的目标也不再放行（授权条目留在库里，只是当档位不读它）。
    permissions.set_mode("deny")
    assert permissions.policy().access_for(outside.resolve()) is None
    assert len(permissions.grants()) == 1


# ---- 真机（可选）：系统凭据存储 ---------------------------------------------


@pytest.mark.skipif(
    os.environ.get("LOCAL_AGENT_TEST_CREDENTIAL_STORE") != "1",
    reason="真机凭据存储测试：默认跳过（会读写本机凭据管理器），用 LOCAL_AGENT_TEST_CREDENTIAL_STORE=1 打开",
)
def test_real_os_credential_store_round_trip() -> None:
    """在目标 Windows 上真机验证一次写入 / 读回 / 删除；用专用引用，结束后清理。"""
    store = open_credential_store("auto")

    assert store.kind == "os", store.note
    ref = credential_ref_for("mc-selftest")
    try:
        store.set(ref, SECRET)
        assert store.get(ref) == SECRET
    finally:
        store.delete(ref)
    assert store.get(ref) is None


class _FakeKeyring:
    """内存版 keyring，用来测"系统凭据存储可用"这条路径而不碰本机凭据管理器。"""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, ref: str, secret: str) -> None:
        self.data[(service, ref)] = secret

    def get_password(self, service: str, ref: str) -> str | None:
        return self.data.get((service, ref))

    def delete_password(self, service: str, ref: str) -> None:
        self.data.pop((service, ref), None)


class _BrokenKeyring:
    def set_password(self, service: str, ref: str, secret: str) -> None:
        raise RuntimeError("no backend")

    def get_password(self, service: str, ref: str) -> str | None:
        raise RuntimeError("no backend")

    def delete_password(self, service: str, ref: str) -> None:
        raise RuntimeError("no backend")

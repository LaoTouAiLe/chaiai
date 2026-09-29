"""模型连接设置库（T29A；契约 §6.8）。

**独立库文件**（照长期记忆库的约定）：模型设置与聊天库的 Checkpoint 生命周期不同，混在一起会让
"删会话"与"停用连接"互相牵连。默认落在 ``data/model_settings.sqlite``，在 ``.gitignore`` 覆盖范围内。

本模块负责守住这些不变量，不靠调用方自觉：

- 密钥**永远不进这个库**——表结构里没有任何一列能放密钥（见 ``migrations._SETTINGS_V1_SQL``）；
- 编辑产生**新版本**并留历史行；删除只停用（``enabled=0``），**不做物理删除**，否则历史模型标识与
  用量会失去解释；
- 编辑后能力检测结果回到"未验证"：换了端点或模型，上一次对**旧端点**的结论就不再成立于这条连接；
- 写入可携带 ``expected_version``：与当前版本不符即拒绝，避免两个页面同时编辑互相覆盖（§6.1）。
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from local_agent.exceptions import (
    ConnectionValidationError,
    ConnectionVersionConflict,
    ModelSettingsError,
    SelectionConflict,
)
from local_agent.model_settings.credentials import (
    CredentialStore,
    credential_ref_for,
    is_credential_ref,
    secrets_from,
)
from local_agent.model_settings.models import (
    AuthMode,
    CapabilityOutcome,
    CapabilityReport,
    ConnectionDraft,
    ModelConnection,
    validate_draft,
)
from local_agent.persistence import migrations

#: 选择的作用域：默认连接与会话级选择。
SelectionScope = Literal["default", "session"]

#: 默认连接在 ``model_selection`` 里的定位（作用域 + 作用域 id）。
DEFAULT_SCOPE_ID = "default"

#: 旧配置导入的记账键：导入只做一次，重复启动不重复导入（§6.8）。
META_LEGACY_IMPORT = "legacy_import.config_yaml"
META_LEGACY_IMPORT_AT = "legacy_import.imported_at"


def now_iso() -> str:
    """当前时间（UTC、秒级、ISO 文本）：本库所有时间戳的统一写法。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_dt(value: str | None) -> datetime | None:
    return None if not value else datetime.fromisoformat(value)


@dataclass(frozen=True)
class Selection:
    """某个作用域当前选中的连接（默认或某个会话）。"""

    scope: SelectionScope
    scope_id: str
    connection_id: str
    config_version: int
    updated_at: datetime | None


@dataclass(frozen=True)
class SelectionRef:
    """调用方**看到的当前选择**，用于选择写入的乐观校验（§6.1：两个页面不能互相覆盖）。

    ``connection_id=None`` 表示"我当时看到这个作用域**没有**选择"——全新安装还没有默认，
    或某个会话跟随默认。写入前与库里的实际选择逐一比对，不符就拒绝。
    """

    connection_id: str | None
    config_version: int | None


#: 选择冲突提示里的作用域名（给人看的那句话）。
_SCOPE_LABEL = {"default": "默认连接", "session": "这个会话的下一轮模型"}


@dataclass
class SettingsDatabase:
    """一个设置库文件对应的连接与 store。"""

    connection: sqlite3.Connection
    connections: ConnectionStore


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    # 按列名读：本模块的表列很多且会随版本增加，按下标读是等着出错。
    connection.row_factory = sqlite3.Row
    return connection


def open_settings_database(path: Path, *, credentials: CredentialStore | None = None) -> SettingsDatabase:
    """打开（必要时创建）设置库并升到当前结构版本。

    Args:
        path: 库文件路径；父目录不存在时自动创建。
        credentials: 密钥存储；给 ``None`` 时读写密钥的操作会被拒绝（只读设置仍可用）。

    Raises:
        sqlite3.Error: 库无法打开或结构变更失败。
    """
    connection = _connect(path)
    migrations.apply_settings_migrations(connection)
    return SettingsDatabase(connection=connection, connections=ConnectionStore(connection, credentials=credentials))


def current_settings_version(path: Path) -> int | None:
    """只读地读取设置库结构版本；文件不存在返回 ``None``（**不会创建文件**）。"""
    target = Path(path)
    if not target.is_file():
        return None
    connection = sqlite3.connect(str(target))
    try:
        return migrations.current_version(connection)
    finally:
        connection.close()


class ConnectionStore:
    """模型连接的读写；所有 SQL 都收敛在这里。"""

    def __init__(self, connection: sqlite3.Connection, *, credentials: CredentialStore | None = None) -> None:
        self._connection = connection
        self._credentials = credentials

    # ---- 读 -----------------------------------------------------------------

    def list_connections(self, *, include_disabled: bool = False) -> list[ModelConnection]:
        """列出连接；默认只给"可用"的（停用的不出现在选择器里，但历史仍在库里）。

        顺序按 ``rowid``（= **创建顺序**）：时间戳只到秒，同一秒里建的两条谁先谁后就没定义了；
        而编辑走的是 upsert，rowid 不变，所以"新建的排在后面"这条性质一直成立。
        """
        sql = "SELECT * FROM model_connections"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY rowid"
        return [self._row_to_connection(row) for row in self._connection.execute(sql).fetchall()]

    def get(self, connection_id: str) -> ModelConnection | None:
        row = self._connection.execute(
            "SELECT * FROM model_connections WHERE connection_id = ?", (connection_id,)
        ).fetchone()
        return None if row is None else self._row_to_connection(row)

    def require(self, connection_id: str) -> ModelConnection:
        """取一条连接；不存在就抛 :class:`ModelSettingsError`（消息可直接给用户）。"""
        found = self.get(connection_id)
        if found is None:
            raise ModelSettingsError(f"连接不存在：{connection_id}")
        return found

    def version(self, connection_id: str, config_version: int) -> ModelConnection | None:
        """取某个**历史版本**（挂起运行恢复时按固定版本重取客户端要用它）。"""
        row = self._connection.execute(
            "SELECT * FROM model_connection_versions WHERE connection_id = ? AND config_version = ?",
            (connection_id, config_version),
        ).fetchone()
        if row is None:
            return None
        current = self.get(connection_id)
        return self._row_to_connection(row, capability=current.capability if current else None, enabled=True)

    def versions(self, connection_id: str) -> list[ModelConnection]:
        """某条连接的全部历史版本（按版本号升序）。"""
        rows = self._connection.execute(
            "SELECT * FROM model_connection_versions WHERE connection_id = ? ORDER BY config_version",
            (connection_id,),
        ).fetchall()
        current = self.get(connection_id)
        capability = current.capability if current else None
        return [self._row_to_connection(row, capability=capability, enabled=True) for row in rows]

    def default_selection(self) -> Selection | None:
        return self._selection("default", DEFAULT_SCOPE_ID)

    def session_selection(self, session_id: str) -> Selection | None:
        return self._selection("session", session_id)

    @property
    def credential_kind(self) -> str | None:
        """密钥存储的持久性：``os`` = 重启后仍在，``memory`` = 仅本进程内，没有凭据存储时 ``None``。

        页面据此把密钥状态标成"仅本次进程内"（§6.8：不可用时必须明说重启后要重新录入）。
        """
        return None if self._credentials is None else self._credentials.kind

    @property
    def credential_note(self) -> str | None:
        """临时模式的降级说明；持久存储可用时为 ``None``（给设置页显示"重启后需重新录入"）。"""
        return None if self._credentials is None else self._credentials.note

    def meta(self, key: str) -> str | None:
        row = self._connection.execute("SELECT value FROM settings_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    # ---- 写 -----------------------------------------------------------------

    def save(
        self,
        draft: ConnectionDraft,
        *,
        api_key: str | None = None,
        expected_version: int | None = None,
    ) -> ModelConnection:
        """新建或编辑一条连接。

        Args:
            draft: 待保存字段；``connection_id`` 为空即新建。
            api_key: 用户本次录入的密钥。给了就**覆盖写入**凭据存储；不给表示"不改密钥"
                （编辑时不重填 Key 不会把已保存的 Key 抹掉）。
            expected_version: 客户端手上的版本号；与当前不符即拒绝写入（防两个页面互相覆盖）。

        Returns:
            保存后的连接（新建版本号为 1，编辑为原版本 + 1）。

        Raises:
            ConnectionValidationError: 字段不合法。
            ConnectionVersionConflict: 版本不一致。
            ModelSettingsError: 连接不存在、已停用，或缺凭据存储。
        """
        clean = validate_draft(draft)
        if clean.connection_id is None:
            if expected_version is not None:
                raise ConnectionValidationError("新建连接时不应携带版本号")
            return self._create(clean, api_key=api_key)
        if expected_version is None:
            raise ConnectionValidationError("编辑连接必须携带当前版本号（防止覆盖别人的改动）")
        return self._update(clean, api_key=api_key, expected_version=expected_version)

    def _create(self, draft: ConnectionDraft, *, api_key: str | None) -> ModelConnection:
        connection_id = f"mc-{uuid.uuid4().hex[:12]}"
        credential_ref = self._store_secret(
            connection_id, draft, api_key=api_key, config_version=1
        )
        now = now_iso()
        connection = ModelConnection(
            connection_id=connection_id,
            display_name=draft.display_name,
            base_url=draft.base_url,
            model=draft.model,
            context_window_tokens=draft.context_window_tokens,
            max_tokens=draft.max_tokens,
            temperature=draft.temperature,
            timeout_seconds=draft.timeout_seconds,
            retry_count=draft.retry_count,
            streaming=draft.streaming,
            auth_mode=draft.auth_mode,
            credential_ref=credential_ref,
            config_version=1,
            capability=CapabilityReport(),
            enabled=True,
            allow_plaintext_http=draft.allow_plaintext_http,
            provider=draft.provider,
            api_key_env=draft.api_key_env,
            created_at=_as_dt(now),
            updated_at=_as_dt(now),
        )
        self._insert_current(connection, now=now)
        self._insert_version(connection, recorded_at=now)
        self._connection.commit()
        return connection

    def _update(self, draft: ConnectionDraft, *, api_key: str | None, expected_version: int) -> ModelConnection:
        assert draft.connection_id is not None  # save() 已分流
        existing = self.require(draft.connection_id)
        if not existing.enabled:
            raise ModelSettingsError("这条连接已停用：v0.4 不支持恢复已停用的连接，请新建一条")
        if existing.config_version != expected_version:
            raise ConnectionVersionConflict(
                f"这条连接已被改动（当前版本 {existing.config_version}，你的版本 {expected_version}）："
                "请刷新后重试，别让两个页面互相覆盖"
            )

        next_version = existing.config_version + 1
        credential_ref = (
            self._store_secret(
                draft.connection_id, draft, api_key=api_key, config_version=next_version
            )
            or existing.credential_ref
        )
        now = now_iso()
        updated = ModelConnection(
            connection_id=existing.connection_id,
            display_name=draft.display_name,
            base_url=draft.base_url,
            model=draft.model,
            context_window_tokens=draft.context_window_tokens,
            max_tokens=draft.max_tokens,
            temperature=draft.temperature,
            timeout_seconds=draft.timeout_seconds,
            retry_count=draft.retry_count,
            streaming=draft.streaming,
            auth_mode=draft.auth_mode,
            credential_ref=credential_ref,
            config_version=existing.config_version + 1,
            # 端点或参数变了，上一次的检测结论不再成立于这条连接。
            capability=CapabilityReport(),
            enabled=True,
            allow_plaintext_http=draft.allow_plaintext_http,
            provider=draft.provider,
            api_key_env=draft.api_key_env or existing.api_key_env,
            created_at=existing.created_at,
            updated_at=_as_dt(now),
        )
        self._insert_current(updated, now=now)
        self._insert_version(updated, recorded_at=now)
        # 选择表示"下一轮使用这条连接"，并不把未来轮次钉在旧配置上。编辑后同步提升
        # 所有指向它的选择引用，否则列表会展示 v2，而默认选择记录仍是 v1：页面把
        # 列表中看到的 v2 回传做 CAS 时会被误判为另一个页面修改过选择。
        self._connection.execute(
            "UPDATE model_selection SET config_version = ?, updated_at = ? WHERE connection_id = ?",
            (updated.config_version, now, updated.connection_id),
        )
        self._connection.commit()
        return updated

    def retire(self, connection_id: str, *, expected_version: int | None = None) -> ModelConnection:
        """停用一条连接（"删除"）。**不物理删除**，历史版本与用量记录都保留。

        Raises:
            ModelSettingsError: 连接不存在。
            ConnectionVersionConflict: 版本不一致。
        """
        existing = self.require(connection_id)
        if expected_version is not None and existing.config_version != expected_version:
            raise ConnectionVersionConflict(
                f"这条连接已被改动（当前版本 {existing.config_version}，你的版本 {expected_version}）"
            )
        self._connection.execute(
            "UPDATE model_connections SET enabled = 0, updated_at = ? WHERE connection_id = ?",
            (now_iso(), connection_id),
        )
        # 停用后不能再是默认或会话选择：否则选择器会指向一条已停用的连接。
        self._connection.execute("DELETE FROM model_selection WHERE connection_id = ?", (connection_id,))
        self._connection.commit()
        return self.require(connection_id)

    def record_check(
        self, connection_id: str, report: CapabilityReport, *, expected_version: int
    ) -> ModelConnection:
        """记录一次"测试连接"的结论（§6.8：由用户主动触发，分项报告）。

        `expected_version` 是**被探测的那个版本**：探测要花几秒，期间另一个页面可能已经改了端点或模型。
        那种情况下结论属于**旧配置**，硬写上去就等于把"旧端点可用"标在新连接上（复审指出）。
        版本不一致就丢弃结果并报冲突，让用户重新测。

        Raises:
            ConnectionVersionConflict: 这条连接在探测期间被改动过（结果已丢弃，未写入）。
        """
        current = self.require(connection_id)
        if current.config_version != expected_version:
            raise ConnectionVersionConflict(
                f"这条连接在检测期间被改动过（探测的是版本 {expected_version}，当前是 "
                f"{current.config_version}）：这次检测结果已丢弃，请重新检测"
            )
        self._connection.execute(
            "UPDATE model_connections SET capability_chat = ?, capability_tools = ?, capability_streaming = ?,"
            " capability_checked_at = ?, capability_message = ?, updated_at = ? WHERE connection_id = ?",
            (
                report.chat,
                report.tools,
                report.streaming,
                None if report.checked_at is None else report.checked_at.isoformat(timespec="seconds"),
                report.message,
                now_iso(),
                connection_id,
            ),
        )
        self._connection.commit()
        return self.require(connection_id)

    def select_default(
        self,
        connection_id: str,
        *,
        expected_version: int | None = None,
        expected_selection: SelectionRef | None = None,
    ) -> Selection:
        """设置新会话继承的默认连接（§6.8：只影响**下一次新用户提交**）。

        Args:
            connection_id: 要选中的连接。
            expected_version: 这条连接你看到的版本；不符即拒绝（它已被改过）。
            expected_selection: 你看到的**当前默认**；不符即拒绝（另一个页面已经改过，§6.1）。

        Raises:
            ConnectionVersionConflict: 目标连接版本不符。
            SelectionConflict: 当前选择已被别的页面改过。
        """
        return self._select(
            "default",
            DEFAULT_SCOPE_ID,
            connection_id,
            expected_version=expected_version,
            expected_selection=expected_selection,
        )

    def select_session(
        self,
        session_id: str,
        connection_id: str | None,
        *,
        expected_version: int | None = None,
        expected_selection: SelectionRef | None = None,
    ) -> Selection | None:
        """设置某个会话"下一轮"用的连接；``connection_id=None`` 表示恢复为跟随默认。

        `expected_selection` 对"取消选择"同样适用：调用方要先证明它看到的会话选择没被别人改过，
        否则会出现"甲页面改成 B、乙页面想恢复跟随默认，结果把甲的 B 一起抹掉"。
        """
        self._require_expected_selection("session", session_id, expected_selection)
        if connection_id is None:
            self._connection.execute(
                "DELETE FROM model_selection WHERE scope = 'session' AND scope_id = ?", (session_id,)
            )
            self._connection.commit()
            return None
        return self._select(
            "session",
            session_id,
            connection_id,
            expected_version=expected_version,
            expected_selection=expected_selection,
        )

    def set_meta(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO settings_meta (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now_iso()),
        )
        self._connection.commit()

    def secret_values(self) -> list[str]:
        """当前及历史版本可取回的密钥，**只用于脱敏**，不进入任何 DTO。

        轮换后旧版本凭据仍保留供挂起运行恢复；重启时也必须重新收集它，不能只靠
        本进程内 `_refresh_secrets` 的"只增不减"。旧环境变量路径同样按历史版本收集。
        取不到的凭据跳过，不因凭据后端暂时不可用而阻断页面启动。
        """
        rows = self._connection.execute(
            "SELECT auth_mode, credential_ref, api_key_env FROM model_connection_versions "
            "UNION SELECT auth_mode, credential_ref, api_key_env FROM model_connections"
        ).fetchall()
        refs: set[str] = set()
        values: set[str] = set()
        for row in rows:
            if row["auth_mode"] != "api_key":
                continue
            if row["credential_ref"]:
                refs.add(str(row["credential_ref"]))
            if row["api_key_env"]:
                value = os.environ.get(str(row["api_key_env"]), "").strip()
                if value:
                    values.add(value)
        if self._credentials is not None:
            values.update(secrets_from(self._credentials, sorted(refs)))
        return sorted(values)

    def resolve_api_key(
        self, connection: ModelConnection, *, environ: Mapping[str, str] | None = None
    ) -> str | None:
        """按顺序取这条连接的密钥：凭据存储 → 旧 ``api_key_env`` 环境变量 → ``None``。

        顺序是 §6.8 的"保留旧环境变量取密钥路径"：导入进来的旧连接仍可能只配了环境变量。
        ``auth_mode="none"`` 不需要密钥，返回 ``None``（客户端构造用占位值）。

        Returns:
            密钥明文，**只给构造客户端用**；不要把返回值放进任何 DTO、日志或异常消息。
        """
        if connection.auth_mode == "none":
            return None
        if connection.credential_ref:
            secret = self._require_credentials().get(connection.credential_ref)
            if secret:
                return secret
        if connection.api_key_env:
            env = os.environ if environ is None else environ
            value = env.get(connection.api_key_env, "").strip()
            if value:
                return value
        return None

    # ---- 内部 ---------------------------------------------------------------

    def _store_secret(
        self,
        connection_id: str,
        draft: ConnectionDraft,
        *,
        api_key: str | None,
        config_version: int,
    ) -> str | None:
        """把本次录入的密钥写进凭据存储，返回不含明文的引用（**按版本**）。

        - ``auth_mode="none"``：不写密钥，也不产生引用（本地端点不需要）；
        - 给了 Key：写入**这个版本自己的**条目（旧版本的条目保留不动——挂起的运行还要按它取密钥）；
        - 没给 Key：返回 ``None``，调用方保留原有引用——**编辑时不重填 Key 不会把 Key 弄丢**。
        """
        if draft.auth_mode == "none":
            return None
        if api_key is None:
            return None
        secret = api_key.strip()
        if not secret:
            raise ConnectionValidationError("密钥不能是空白：要么填一个，要么选择“不需要 Key”")
        ref = credential_ref_for(connection_id, config_version)
        self._require_credentials().set(ref, secret)
        return ref

    def _require_credentials(self) -> CredentialStore:
        if self._credentials is None:
            raise ModelSettingsError("这个设置在无凭据存储的只读上下文中不可用：请用带凭据存储的设置库")
        return self._credentials

    def _insert_current(self, connection: ModelConnection, *, now: str) -> None:
        self._connection.execute(
            "INSERT INTO model_connections ("
            " connection_id, display_name, provider, base_url, model, context_window_tokens, max_tokens,"
            " temperature, timeout_seconds, retry_count, streaming, auth_mode, api_key_env, credential_ref,"
            " allow_plaintext_http, enabled, capability_chat, capability_tools, capability_streaming,"
            " capability_checked_at, capability_message, config_version, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(connection_id) DO UPDATE SET"
            " display_name = excluded.display_name, provider = excluded.provider, base_url = excluded.base_url,"
            " model = excluded.model, context_window_tokens = excluded.context_window_tokens,"
            " max_tokens = excluded.max_tokens, temperature = excluded.temperature,"
            " timeout_seconds = excluded.timeout_seconds, retry_count = excluded.retry_count,"
            " streaming = excluded.streaming, auth_mode = excluded.auth_mode, api_key_env = excluded.api_key_env,"
            " credential_ref = excluded.credential_ref, allow_plaintext_http = excluded.allow_plaintext_http,"
            " capability_chat = excluded.capability_chat, capability_tools = excluded.capability_tools,"
            " capability_streaming = excluded.capability_streaming,"
            " capability_checked_at = excluded.capability_checked_at,"
            " capability_message = excluded.capability_message, config_version = excluded.config_version,"
            " updated_at = excluded.updated_at",
            (
                connection.connection_id,
                connection.display_name,
                connection.provider,
                connection.base_url,
                connection.model,
                connection.context_window_tokens,
                connection.max_tokens,
                connection.temperature,
                connection.timeout_seconds,
                connection.retry_count,
                int(connection.streaming),
                connection.auth_mode,
                connection.api_key_env,
                connection.credential_ref,
                int(connection.allow_plaintext_http),
                1,
                connection.capability.chat,
                connection.capability.tools,
                connection.capability.streaming,
                None
                if connection.capability.checked_at is None
                else connection.capability.checked_at.isoformat(timespec="seconds"),
                connection.capability.message,
                connection.config_version,
                now,
                now,
            ),
        )

    def _insert_version(self, connection: ModelConnection, *, recorded_at: str) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO model_connection_versions ("
            " connection_id, config_version, display_name, provider, base_url, model, context_window_tokens,"
            " max_tokens, temperature, timeout_seconds, retry_count, streaming, auth_mode, credential_ref,"
            " api_key_env, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                connection.connection_id,
                connection.config_version,
                connection.display_name,
                connection.provider,
                connection.base_url,
                connection.model,
                connection.context_window_tokens,
                connection.max_tokens,
                connection.temperature,
                connection.timeout_seconds,
                connection.retry_count,
                int(connection.streaming),
                connection.auth_mode,
                connection.credential_ref,
                connection.api_key_env,
                recorded_at,
            ),
        )

    def _selection(self, scope: SelectionScope, scope_id: str) -> Selection | None:
        row = self._connection.execute(
            "SELECT * FROM model_selection WHERE scope = ? AND scope_id = ?", (scope, scope_id)
        ).fetchone()
        if row is None:
            return None
        return Selection(
            scope=scope,
            scope_id=scope_id,
            connection_id=str(row["connection_id"]),
            config_version=int(row["config_version"]),
            updated_at=_as_dt(row["updated_at"]),
        )

    def current_selection_ref(self, scope: SelectionScope, scope_id: str) -> SelectionRef:
        """当前选择的引用（读取侧；页面据此把"我看到的选择"回传做校验）。"""
        current = self._selection(scope, scope_id)
        if current is None:
            return SelectionRef(connection_id=None, config_version=None)
        return SelectionRef(connection_id=current.connection_id, config_version=current.config_version)

    def _require_expected_selection(
        self, scope: SelectionScope, scope_id: str, expected: SelectionRef | None
    ) -> None:
        """选择写入的乐观校验：库里的当前选择必须还是调用方看到的那个（§6.1）。

        `expected=None` 表示"调用方没有观测过"（内部调用、脚本），此时不校验；
        HTTP 层把 `expected_selection` 设为必填，所以页面走的路径**一定**会校验。
        """
        if expected is None:
            return
        current = self.current_selection_ref(scope, scope_id)
        if current != expected:
            label = _SCOPE_LABEL.get(scope, "选择")
            raise SelectionConflict(
                f"{label}已被另一个页面改成 {current.connection_id or '未选择'}"
                f"（版本 {current.config_version}），你看到的是 {expected.connection_id or '未选择'}"
                f"（版本 {expected.config_version}）：请刷新后重新选择"
            )

    def _select(
        self,
        scope: SelectionScope,
        scope_id: str,
        connection_id: str,
        *,
        expected_version: int | None,
        expected_selection: SelectionRef | None = None,
    ) -> Selection:
        # 乐观校验放在这里而不是调用方：调用方在校验与落库之间还有别的读，锁外做就失去意义了。
        self._require_expected_selection(scope, scope_id, expected_selection)
        connection = self.require(connection_id)
        if not connection.enabled:
            raise ModelSettingsError(f"这条连接已停用，不能选中：{connection.display_name}")
        if expected_version is not None and connection.config_version != expected_version:
            raise ConnectionVersionConflict(
                f"这条连接已被改动（当前版本 {connection.config_version}，你的版本 {expected_version}）："
                "请刷新后重新选择"
            )
        now = now_iso()
        self._connection.execute(
            "INSERT INTO model_selection (scope, scope_id, connection_id, config_version, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(scope, scope_id) DO UPDATE SET connection_id = excluded.connection_id,"
            " config_version = excluded.config_version, updated_at = excluded.updated_at",
            (scope, scope_id, connection_id, connection.config_version, now),
        )
        self._connection.commit()
        return Selection(
            scope=scope,
            scope_id=scope_id,
            connection_id=connection_id,
            config_version=connection.config_version,
            updated_at=_as_dt(now),
        )

    def _row_to_connection(
        self,
        row: sqlite3.Row,
        *,
        capability: CapabilityReport | None = None,
        enabled: bool | None = None,
    ) -> ModelConnection:
        # 当前表与历史版本表的列不同：按列名判断，而不是假设"两张表一样"。
        keys = set(row.keys())
        report = capability
        if report is None and "capability_chat" in keys:
            report = CapabilityReport(
                chat=_outcome(row["capability_chat"]),
                tools=_outcome(row["capability_tools"]),
                streaming=_outcome(row["capability_streaming"]),
                checked_at=_as_dt(row["capability_checked_at"]),
                message=None if row["capability_message"] is None else str(row["capability_message"]),
            )
        auth_mode: AuthMode = "none" if str(row["auth_mode"]) == "none" else "api_key"
        credential_ref = None if row["credential_ref"] is None else str(row["credential_ref"])
        if credential_ref is not None and not is_credential_ref(credential_ref):
            # 引用格式不对：宁可当成"没有引用"，也不让一个可疑字符串被当引用用。
            credential_ref = None
        return ModelConnection(
            connection_id=str(row["connection_id"]),
            display_name=str(row["display_name"]),
            base_url=str(row["base_url"]),
            model=str(row["model"]),
            context_window_tokens=int(row["context_window_tokens"]),
            max_tokens=int(row["max_tokens"]),
            temperature=float(row["temperature"]),
            timeout_seconds=int(row["timeout_seconds"]),
            retry_count=int(row["retry_count"]),
            streaming=bool(row["streaming"]),
            auth_mode=auth_mode,
            credential_ref=credential_ref,
            config_version=int(row["config_version"]),
            capability=report or CapabilityReport(),
            enabled=bool(row["enabled"]) if enabled is None else enabled,
            allow_plaintext_http=bool(row["allow_plaintext_http"]) if "allow_plaintext_http" in keys else False,
            provider=str(row["provider"]),
            api_key_env=str(row["api_key_env"]) if "api_key_env" in keys and row["api_key_env"] else None,
            created_at=_as_dt(row["created_at"]) if "created_at" in keys else None,
            updated_at=_as_dt(row["recorded_at"]) if "recorded_at" in keys else None,
        )


def _outcome(value: Any) -> CapabilityOutcome:
    """把库里的文本收敛成三态字面量：只认 ``pass`` / ``fail``，其余一律当"不知道"。"""
    text = str(value)
    if text in {"pass", "fail"}:
        return cast(CapabilityOutcome, text)
    return "unknown"


def resolve_api_key(
    connection: ModelConnection,
    *,
    credentials: CredentialStore | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """实现同 :meth:`ConnectionStore.resolve_api_key`，给没有 store 的调用点用（例如评测脚本）。"""
    if connection.auth_mode == "none":
        return None
    if connection.credential_ref and credentials is not None:
        secret = credentials.get(connection.credential_ref)
        if secret:
            return secret
    if connection.api_key_env:
        env = os.environ if environ is None else environ
        value = env.get(connection.api_key_env, "").strip()
        if value:
            return value
    return None


def credential_refs(connections: Sequence[ModelConnection]) -> list[str]:
    """一批连接的凭据引用（给脱敏用：只取引用，不取值）。"""
    return [ref for ref in (item.credential_ref for item in connections) if ref]

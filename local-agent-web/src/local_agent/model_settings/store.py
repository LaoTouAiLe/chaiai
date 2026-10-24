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

import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from local_agent.capabilities import CAPABILITY_TOOLS, resolve_switches
from local_agent.exceptions import (
    ConnectionValidationError,
    ConnectionVersionConflict,
    ModelSettingsError,
    SelectionConflict,
)
from local_agent.mcp.state import DEFAULT_SERVER_STATE, ServerState
from local_agent.model_settings.credentials import (
    CredentialStore,
    DatabaseCredentialStore,
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
from local_agent.model_settings.preferences import PreferenceStore
from local_agent.model_settings.web_search import WebSearchKeyStore
from local_agent.persistence import migrations
from local_agent.security.path_guard import (
    ExternalPathPolicy,
    ExternalPathsMode,
    GrantAccess,
    normalize_grant_path,
    normalize_mode,
)

_LOGGER = logging.getLogger(__name__)

#: 选择的作用域：默认连接与会话级选择。
SelectionScope = Literal["default", "session"]

#: 默认连接在 ``model_selection`` 里的定位（作用域 + 作用域 id）。
DEFAULT_SCOPE_ID = "default"

#: 旧配置导入的记账键：导入只做一次，重复启动不重复导入（§6.8）。
META_LEGACY_IMPORT = "legacy_import.config_yaml"
META_LEGACY_IMPORT_AT = "legacy_import.imported_at"

#: 外部路径**档位**在 ``settings_meta`` 里的键（T38）。授权条目另有专表。
META_EXTERNAL_PATHS_MODE = "permissions.external_paths.mode"

#: **能力开关**在 ``settings_meta`` 里的键（§16.1、§16.6，2026-10-13）。
#:
#: 存的是**用户改过的那几项**（JSON 对象），不是完整开关表：出厂默认以后还会随版本演进，把整张表
#: 冻在库里会让"新增能力默认关"这类改动对老用户失效。
META_AGENT_CAPABILITIES = "agent.capabilities"

#: **被用户禁用的技能名**在 ``settings_meta`` 里的键（技能管理页，2026-10-16；契约 §16.5）。
#:
#: 存"禁用了哪些"而不是"启用了哪些"：**新发现的技能默认可用**（用户把它放进发现根就是为了用），
#: 而"默认可用"这条语义只能靠"只记例外"表达——反过来存会把"装了新技能却用不了"变成默认结果。
#: 这与能力开关同一口径：库里只存用户动过的那几项。
META_SKILL_DISABLED = "agent.skills.disabled"

#: 用户额外添加的技能发现根（2026-10-20）：**绝对路径**数组，只记"加了哪些"。
#: 扫描本身仍每轮现做——目录里新放的技能下一轮就可见，不把技能清单冻进库里。
META_SKILL_ROOTS = "skills.extra_dirs"


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


@dataclass(frozen=True)
class PathGrant:
    """一条"工作区外路径"授权条目（T38）。"""

    path: str
    access: GrantAccess
    created_at: datetime | None
    updated_at: datetime | None


class PermissionStore:
    """外部路径档位与授权条目的读写（T38；架构 §1.5）；所有 SQL 收敛在这里。

    档位是 ``settings_meta`` 里的一行，授权条目是 ``external_path_grants`` 表——**唯一真源**
    在设置库里，``config.yaml`` 只是出厂默认（用户改了档位后，重启读到的仍是用户的选择）。
    写入"保存即生效、作用于下一轮"（T29A 口径），不写回配置文件。
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        default_mode: ExternalPathsMode = "workspace-write",
    ) -> None:
        self._connection = connection
        self._default_mode = normalize_mode(default_mode)

    @property
    def default_mode(self) -> ExternalPathsMode:
        """``config.yaml`` 给的出厂默认档位；库里没有用户选择时用它。"""
        return self._default_mode

    def mode(self) -> ExternalPathsMode:
        """当前生效档位：库里的用户选择优先，没有则回落到出厂默认。"""
        stored = self._mode_value()
        return self._default_mode if stored is None else stored

    def set_mode(self, mode: ExternalPathsMode) -> None:
        """保存档位（"保存即生效"；不要求先存在授权条目）。"""
        mode = normalize_mode(mode)
        self._connection.execute(
            "INSERT INTO settings_meta (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (META_EXTERNAL_PATHS_MODE, mode, now_iso()),
        )
        self._connection.commit()

    def grants(self) -> list[PathGrant]:
        """全部授权条目（按规范化路径排序，输出稳定）。"""
        rows = self._connection.execute(
            "SELECT path, access, created_at, updated_at FROM external_path_grants ORDER BY path"
        ).fetchall()
        return [
            PathGrant(
                path=str(row["path"]),
                access=_grant_access(row["access"]),
                created_at=_as_dt(row["created_at"]),
                updated_at=_as_dt(row["updated_at"]),
            )
            for row in rows
        ]

    def grant_map(self) -> dict[str, GrantAccess]:
        """规范化路径 → 访问级别；构造判定策略用。"""
        return {item.path: item.access for item in self.grants()}

    def policy(self) -> ExternalPathPolicy:
        """按当前档位与授权构造判定策略（每一轮现取，不缓存）。"""
        return ExternalPathPolicy(mode=self.mode(), grants=self.grant_map())

    def grant(self, path: str | Path, access: GrantAccess) -> PathGrant:
        """新增或改写一条授权（**同一个规范化目标只留一行**）。

        Returns:
            写入后的条目（路径已规范化）。
        """
        key = normalize_grant_path(path)
        now = now_iso()
        self._connection.execute(
            "INSERT INTO external_path_grants (path, access, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET access = excluded.access,"
            " updated_at = excluded.updated_at",
            (key, access, now, now),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT path, access, created_at, updated_at FROM external_path_grants WHERE path = ?",
            (key,),
        ).fetchone()
        return PathGrant(
            path=str(row["path"]),
            access=_grant_access(row["access"]),
            created_at=_as_dt(row["created_at"]),
            updated_at=_as_dt(row["updated_at"]),
        )

    def revoke(self, path: str | Path) -> bool:
        """撤销一条授权；返回是否真的删掉了一行。"""
        key = normalize_grant_path(path)
        cursor = self._connection.execute(
            "DELETE FROM external_path_grants WHERE path = ?", (key,)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def _mode_value(self) -> ExternalPathsMode | None:
        row = self._connection.execute(
            "SELECT value FROM settings_meta WHERE key = ?", (META_EXTERNAL_PATHS_MODE,)
        ).fetchone()
        if row is None:
            return None
        value = str(row[0]).strip()
        try:
            # 旧值（deny / whitelist / allow）与三档走同一条归一：老库里存着旧值也要读得回来，
            # 否则"用户改过档位"会被静默判成"没改过"，页面与判定一起退回出厂默认。
            return normalize_mode(value)
        except ValueError:
            # 有记录但无法识别，不等于用户从未选择。默认可能是完全放行，不能因此扩权。
            _LOGGER.warning("设置库的权限档位无效，已按 read-only 处理；请在权限设置中重新保存档位")
            return "read-only"


@dataclass
class CapabilityStore:
    """**能力开关**的读写（§16.1、§16.6）。

    与外部路径档位（T38）**同一口径**：开关是 ``settings_meta`` 里的一行，**唯一真源在设置库**，
    ``config.yaml`` 只是出厂默认——用户改过之后，重启读到的仍是用户的选择（不会被配置文件覆盖）。
    "保存即生效、作用于下一轮"，不写回配置文件。

    库里只存**改过的那几项**（JSON 对象），所以：新增能力时"默认关"对它依然生效；把某项删掉就等于
    回到配置给的出厂默认。

    ``switchable`` 由**服务端**枚举（= 闭集减去不可关闭的两个），页面照它渲染：能力名与工具归属的
    定义只在 ``capabilities.py``（§6.9），不在这里另立一套。
    """

    _connection: sqlite3.Connection
    _defaults: Mapping[str, bool]
    _switchable: tuple[str, ...]

    @property
    def defaults(self) -> Mapping[str, bool]:
        """``config.yaml`` 给的出厂默认开关表；库里没有用户选择时用它。"""
        return self._defaults

    @property
    def switchable(self) -> tuple[str, ...]:
        """页面上可以开关的能力（固定顺序）；不可关闭的能力不在其中。"""
        return self._switchable

    def overrides(self) -> dict[str, bool]:
        """用户改过的那几项（解析失败即当作"没改过"，与 `_mode_value` 同一条失败闭合）。"""
        row = self._connection.execute(
            "SELECT value FROM settings_meta WHERE key = ?", (META_AGENT_CAPABILITIES,)
        ).fetchone()
        if row is None:
            return {}
        try:
            parsed = json.loads(str(row[0]))
        except ValueError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {
            str(name): bool(value)
            for name, value in parsed.items()
            if name in CAPABILITY_TOOLS and isinstance(value, bool)
        }

    def switches(self) -> dict[str, bool]:
        """当前生效的完整开关表：出厂默认 + 库里的用户选择（每一轮现取，不缓存）。"""
        return resolve_switches(self._defaults, self.overrides())

    def update_switches(self, changes: Mapping[str, bool]) -> dict[str, bool]:
        """保存用户这次动过的开关项（**合并式**；调用方负责先校验名字与取值合法）。

        合并而不是覆盖整张表：库里存的是"改过的那几项"，页面只报告它动过的那一项，其余保持原样。
        读-改-写在同一把锁里完成（应用层用 ``_sqlite_access`` 包住），不会与另一次改动互相覆盖。

        Args:
            changes: 本次要固化的项。传空映射等于"什么都不改"。

        Returns:
            保存后的完整开关表（供本次响应直接回显，免一次往返）。
        """
        merged = self.overrides()
        merged.update(
            {
                str(name): bool(value)
                for name, value in changes.items()
                if name in CAPABILITY_TOOLS
            }
        )
        self._connection.execute(
            "INSERT INTO settings_meta (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (META_AGENT_CAPABILITIES, json.dumps(merged, sort_keys=True), now_iso()),
        )
        self._connection.commit()
        return resolve_switches(self._defaults, merged)


#: MCP Server 的开关与工具名单在 ``settings_meta`` 里的键（2026-10-20；契约 §6.13）。
#:
#: 与能力开关同一口径：库只记**用户动过的 server**。``mcp.json`` 里新加的 server 因此天然是关的
#: ——"从别处整份复制配置"不会把别人的开关状态一起带过来。
META_MCP_SERVERS = "mcp.servers"


def _coerce_server_state(payload: object) -> ServerState | None:
    """把库里的一行 JSON 收敛成状态；脏数据返回 ``None``（= 当作没表态 = 关）。"""
    if not isinstance(payload, dict):
        return None
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        # 缺这一位就没法判断"用户到底开没开"，按关处理比按开处理安全。
        return None
    raw_allow = payload.get("enabled_tools")
    allowed: tuple[str, ...] | None
    if raw_allow is None:
        allowed = None
    elif isinstance(raw_allow, list) and all(isinstance(item, str) for item in raw_allow):
        allowed = tuple(raw_allow)
    else:
        return None
    raw_deny = payload.get("disabled_tools")
    if raw_deny is None:
        denied: tuple[str, ...] = ()
    elif isinstance(raw_deny, list) and all(isinstance(item, str) for item in raw_deny):
        denied = tuple(raw_deny)
    else:
        return None
    return ServerState(enabled=enabled, enabled_tools=allowed, disabled_tools=denied)


@dataclass
class McpServerStore:
    """每台 MCP Server 的开关与工具白/黑名单（2026-10-20；契约 §6.13）。

    **开关不写进 ``mcp.json``**：那份文件描述"怎么连"，这份库记录"用户要不要连"。
    分开的直接好处是 `mcp.json` 可以从 CodeBuddy 整份复制而不改一字。

    读的时候对**配置文件里的每台 server** 都返回一行（没记录的就是默认关），这样页面不必
    自己补默认值；库里残留但配置里已删除的 server 也会返回，页面据此显示"配置已删除"。
    """

    _connection: sqlite3.Connection
    #: 这个 store 会被**两个线程**用：主线程（页面读写）与 MCP 预热/回收线程
    #: （`McpManager.state_provider` → `states()`）。共用同一条 sqlite 连接时，CPython 的
    #: sqlite3 虽然在 serialized 模式下能扛住并发，但"能扛住"不是"我们验证过"——
    #: 用一把自己的锁把它变成串行，代价是微秒级，收益是这条风险不再存在。
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def states(self, known: Sequence[str] = ()) -> dict[str, ServerState]:
        """server 名 → 状态；``known`` 里没记录的补默认关。"""
        with self._lock:
            return self._states_unlocked(known)

    def _states_unlocked(self, known: Sequence[str] = ()) -> dict[str, ServerState]:
        stored = self._read_unlocked()
        states = {name: stored.get(name, DEFAULT_SERVER_STATE) for name in known}
        for name, state in stored.items():
            states.setdefault(name, state)
        return states

    def put(self, name: str, state: ServerState) -> ServerState:
        """写入一台 server 的完整状态（读-改-写合并，不覆盖别的 server）。"""
        with self._lock:
            stored = self._read()
            stored[str(name)] = state
            self._write(stored)
        return state

    def forget(self, name: str) -> None:
        """删掉一台 server 的记录（配置文件里已删除它时用）。"""
        with self._lock:
            stored = self._read()
            if stored.pop(str(name), None) is not None:
                self._write(stored)

    def _read(self) -> dict[str, ServerState]:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, ServerState]:
        row = self._connection.execute(
            "SELECT value FROM settings_meta WHERE key = ?", (META_MCP_SERVERS,)
        ).fetchone()
        if row is None:
            return {}
        try:
            parsed = json.loads(str(row[0]))
        except ValueError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        result: dict[str, ServerState] = {}
        for name, payload in parsed.items():
            state = _coerce_server_state(payload)
            if state is not None:
                result[str(name)] = state
        return result

    def _write(self, states: Mapping[str, ServerState]) -> None:
        with self._lock:
            self._write_unlocked(states)

    def _write_unlocked(self, states: Mapping[str, ServerState]) -> None:
        payload = {
            name: {
                "enabled": state.enabled,
                "enabled_tools": (
                    None if state.enabled_tools is None else list(state.enabled_tools)
                ),
                "disabled_tools": list(state.disabled_tools),
            }
            for name, state in states.items()
        }
        self._connection.execute(
            "INSERT INTO settings_meta (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (META_MCP_SERVERS, json.dumps(payload, sort_keys=True), now_iso()),
        )
        self._connection.commit()


@dataclass
class SkillStore:
    """技能的用户级禁用表（技能管理页，2026-10-16；契约 §16.5）。

    与能力开关**同一个文件、同一条口径**：`config.yaml` 与发现根决定"有哪些技能"，这里只回答
    "哪些被用户关掉了"。所以它读起来是一条**例外表**，而不是一份技能清单——技能是每轮现扫盘发现的，
    把它整份冻在库里，明天新装的技能就会"存在但不在表里"，然后被判成不可用。
    """

    _connection: sqlite3.Connection

    def disabled(self) -> frozenset[str]:
        """被禁用的技能名。

        解析失败（坏 JSON、不是数组）一律当作"**没有禁用任何技能**"——与 `CapabilityStore.overrides`
        同一条失败闭合：库里存的是例外表，读不出来时不该顺手把用户的技能全关掉。真正的可见性仍由
        `skills.enabled` 与 `skill` 能力开关把关，这里不是最后一道边界。
        """
        row = self._connection.execute(
            "SELECT value FROM settings_meta WHERE key = ?", (META_SKILL_DISABLED,)
        ).fetchone()
        if row is None:
            return frozenset()
        try:
            parsed = json.loads(str(row[0]))
        except ValueError:
            return frozenset()
        if not isinstance(parsed, list):
            return frozenset()
        return frozenset(str(name) for name in parsed if isinstance(name, str) and name)

    def update(self, changes: Mapping[str, bool]) -> frozenset[str]:
        """按"名字 → 是否启用"保存（**合并式**），返回保存后的完整禁用集。

        Args:
            changes: 本次动过的那几项。``False`` 进禁用集，``True`` 从禁用集移除。
                名字是否真有这个技能由**调用方**校验（它才拿得到当前目录）。
        """
        current = set(self.disabled())
        for name, enabled in changes.items():
            if enabled:
                current.discard(str(name))
            else:
                current.add(str(name))
        self._connection.execute(
            "INSERT INTO settings_meta (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            # 排序后写：同一组禁用得到同一份字节，便于比对与审计。
            (META_SKILL_DISABLED, json.dumps(sorted(current)), now_iso()),
        )
        self._connection.commit()
        return frozenset(current)

    # ---- 额外发现根（2026-10-20）--------------------------------------------

    def extra_roots(self) -> list[str]:
        """用户自己添加的技能发现根（绝对路径，按添加顺序）。

        与"禁用表"同一口径：库里只存**用户动过的那些**，读不出来（坏 JSON、非数组）就当作
        "没有额外根"——不因为一条坏记录把扫描范围清空或乱扫。
        """
        row = self._connection.execute(
            "SELECT value FROM settings_meta WHERE key = ?", (META_SKILL_ROOTS,)
        ).fetchone()
        if row is None:
            return []
        try:
            parsed = json.loads(str(row[0]))
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed if isinstance(item, str) and Path(item).is_absolute()]

    def add_root(self, path: str) -> list[str]:
        """追加一个发现根（去重），返回保存后的完整列表。"""
        roots = self.extra_roots()
        value = str(Path(path))
        if value not in roots:
            roots.append(value)
            self._save_roots(roots)
        return roots

    def remove_root(self, path: str) -> list[str]:
        """移除一个发现根（幂等），返回保存后的完整列表。"""
        roots = [item for item in self.extra_roots() if item != str(Path(path))]
        self._save_roots(roots)
        return roots

    def _save_roots(self, roots: list[str]) -> None:
        self._connection.execute(
            "INSERT INTO settings_meta (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (META_SKILL_ROOTS, json.dumps(roots), now_iso()),
        )
        self._connection.commit()


def _grant_access(value: Any) -> GrantAccess:
    """把库里的文本收敛成访问级别；未知值按**只读**处理（失败闭合）。"""
    return "read_write" if str(value) == "read_write" else "read"


@dataclass
class SettingsDatabase:
    """一个设置库文件对应的连接与 store。"""

    connection: sqlite3.Connection
    connections: ConnectionStore
    permissions: PermissionStore
    preferences: PreferenceStore
    capabilities: CapabilityStore
    #: 联网检索的密钥（Tavily）。与连接共用一个凭据存储，但引用固定、不按版本滚动。
    web_search: WebSearchKeyStore
    #: 技能的用户级禁用表（技能管理页，2026-10-16）。
    skills: SkillStore
    #: 每台 MCP Server 的开关与工具名单（2026-10-20）。
    mcp: McpServerStore
    #: 打开时用的凭据存储（2026-10-20）：对象存储密钥与模型连接共用这一份。
    credentials: Any = None


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    # 按列名读：本模块的表列很多且会随版本增加，按下标读是等着出错。
    connection.row_factory = sqlite3.Row
    return connection


def open_settings_database(
    path: Path,
    *,
    credentials: CredentialStore | None = None,
    default_external_paths_mode: ExternalPathsMode = "workspace-write",
    default_capabilities: Mapping[str, bool] | None = None,
    switchable_capabilities: Sequence[str] = (),
) -> SettingsDatabase:
    """打开（必要时创建）设置库并升到当前结构版本。

    Args:
        path: 库文件路径；父目录不存在时自动创建。
        credentials: 密钥存储；给 ``None`` 时读写密钥的操作会被拒绝（只读设置仍可用）。
        default_external_paths_mode: ``config.yaml`` 里的外部路径档位，作为**出厂默认**：
            库里没有用户选择时用它，用户改过档位后以库里的为准（T38）。
        default_capabilities: ``config.yaml`` 解析出的**出厂默认开关表**（``AppConfig.capability_default_layer``），
            同类出厂默认（§16.1）。省略时用内置默认（``CAPABILITY_DEFAULTS``）。
        switchable_capabilities: 页面上可以开关的能力（= 闭集减去不可关闭的两个），由调用方枚举后传进来。

    Raises:
        sqlite3.Error: 库无法打开或结构变更失败。
    """
    connection = _connect(path)
    migrations.apply_settings_migrations(connection)
    # 落库的密钥存储要先拿到连接（它把密钥写在 settings_meta 里，与连接、能力开关同一个文件）。
    # 装配顺序是"先建存储、后开库"，所以这里回填一次；别的实现（内存）没有这个动作。
    if isinstance(credentials, DatabaseCredentialStore):
        credentials.bind(connection)
    return SettingsDatabase(
        connection=connection,
        connections=ConnectionStore(connection, credentials=credentials),
        permissions=PermissionStore(
            connection,
            default_mode=default_external_paths_mode,
        ),
        preferences=PreferenceStore(connection),
        capabilities=CapabilityStore(
            connection,
            _defaults=resolve_switches({} if default_capabilities is None else default_capabilities),
            _switchable=tuple(switchable_capabilities),
        ),
        web_search=WebSearchKeyStore(credentials),
        skills=SkillStore(connection),
        mcp=McpServerStore(connection),
        # 对象存储密钥与模型连接共用同一份凭据存储（2026-10-20）。
        credentials=credentials,
    )


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
            # 用户勾选的"支持图片输入"（2026-10-16）：跟着**这条连接**走，不跟着 config.yaml 走。
            supports_vision=draft.supports_vision,
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
            # 用户勾选的"支持图片输入"（2026-10-16）：跟着**这条连接**走，不跟着 config.yaml 走。
            supports_vision=draft.supports_vision,
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
            " capability_checked_at, capability_message, config_version, created_at, updated_at,"
            " supports_vision"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
            " updated_at = excluded.updated_at,"
            " supports_vision = excluded.supports_vision",
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
                # 用户声明的"支持图像输入"（2026-10-16）：与实测报告分开存。
                int(connection.supports_vision),
            ),
        )

    def _insert_version(self, connection: ModelConnection, *, recorded_at: str) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO model_connection_versions ("
            " connection_id, config_version, display_name, provider, base_url, model, context_window_tokens,"
            " max_tokens, temperature, timeout_seconds, retry_count, streaming, auth_mode, credential_ref,"
            " api_key_env, recorded_at, supports_vision)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                # 版本快照也要带它：等待审批的运行恢复时读的是**这一版**（§6.8）。
                int(connection.supports_vision),
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
            # 老库/历史版本行可能没有这一列：按"没声明"处理（失败闭合），不假设支持。
            supports_vision=bool(row["supports_vision"]) if "supports_vision" in keys else False,
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

"""密钥存储（T29A；契约 §6.8 第 4 条；2026-10-13 改为**落设置库**）。

三条硬规则：

1. **密钥只进设置库或进程内存**：Checkpoint、请求台账、事件、日志、导出与截图都不许有明文；
2. **读取接口不返回密钥**：对页面只回答"已配置 / 未配置"；:meth:`CredentialStore.get` 是**构造模型客户端**
   用的内部通道，不得进入任何 DTO；
3. **只有一处落盘**：设置库的 ``settings_meta``（与连接、能力开关同一个本地文件）。运行时缺设置库时
   降级为**进程内临时**密钥并明确提示，绝不在别处悄悄写明文。

**为什么从系统凭据存储改成落设置库**（2026-10-13 由项目负责人裁定）：应用要分发给朋友（双击 exe），
而凭据管理器那条路在别人的机器上会因服务、组策略或会话形态而失败，失败的表现还是"重启后密钥没了"——
用户无从判断自己哪里做错了；另一条老路（``.env`` + ``api_key_env``）要求用户手写文件，分发形态根本走不通。
设置库本来就是**本机的单个 SQLite 文件**（``data/model_settings.sqlite``），连接与能力开关都在里面，
密钥放同一处是"暴露面不变"的选择：代价是它在那个文件里是**明文**，收益是"保存即持久、重启仍在"在
所有机器上行为一致。

**配套约束不变**：密钥不得进入任何 DTO、日志、错误消息、导出与截图——脱敏（:func:`secrets_from`）与
"``sk-`` 不出现在响应里"那批检查照旧生效，它们与"密钥存在哪个后端"无关。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final, Literal, Protocol

from local_agent.exceptions import CredentialStoreError

#: 凭据引用的前缀：引用里只放连接 id，不含任何密钥材料。
CREDENTIAL_REF_PREFIX: Final[str] = "local-agent-web:"

#: ``settings_meta`` 里放密钥的键前缀；键的其余部分就是 ``credential_ref`` 本身。
CREDENTIAL_META_PREFIX: Final[str] = "credentials."

CredentialKind = Literal["os", "local", "memory"]


def credential_ref_for(connection_id: str, config_version: int) -> str:
    """一条连接**某个版本**的凭据引用。不含密钥，也不含端点信息。

    为什么带版本（复审实证的缺陷）：引用只由连接 id 派生时，编辑并覆盖密钥会把**旧版本**的引用指向
    新密钥——等待审批的 v1 运行恢复时就会用 v2 的密钥继续跑，而 §6.8 要求"固定版本与凭据"。
    因此每个版本一个条目；旧条目**不删**（引用它的运行可能还没结束）。
    """
    return f"{CREDENTIAL_REF_PREFIX}{connection_id}:v{config_version}"


def is_credential_ref(value: str) -> bool:
    """是不是本模块生成的引用（防止有人把密钥本身塞进 ``credential_ref`` 字段）。"""
    return value.startswith(CREDENTIAL_REF_PREFIX) and len(value) > len(CREDENTIAL_REF_PREFIX)


class CredentialStore(Protocol):
    """密钥存储的最小面：按引用写入 / 读取 / 删除。"""

    #: ``local`` = 本地设置库（重启后仍在）；``memory`` = 仅本进程有效；``os`` = 历史值（系统凭据存储，
    #: v1.0 之前默认，现在没有代码路径会产出它，DTO 里保留仅为兼容）。
    kind: CredentialKind
    #: 给页面显示的降级说明（``kind`` 为持久存储时是 ``None``）。
    note: str | None

    def set(self, ref: str, secret: str) -> None:
        """写入（或覆盖）某个引用的密钥。"""
        ...

    def get(self, ref: str) -> str | None:
        """取出密钥；没有返回 ``None``。**只在构造客户端时调用。**"""
        ...

    def delete(self, ref: str) -> None:
        """删除某个引用的密钥；不存在时静默返回。"""
        ...


@dataclass
class InMemoryCredentialStore:
    """进程内临时密钥；进程结束即消失（降级模式，页面必须提示"重启后需重新录入"）。"""

    note: str | None = "密钥存储不可用：密钥只在本进程内有效，重启后需要重新录入。"
    kind: CredentialKind = "memory"
    _secrets: dict[str, str] = field(default_factory=dict, repr=False)

    def set(self, ref: str, secret: str) -> None:
        self._secrets[ref] = secret

    def get(self, ref: str) -> str | None:
        return self._secrets.get(ref)

    def delete(self, ref: str) -> None:
        self._secrets.pop(ref, None)


def _now_iso() -> str:
    """与设置库其它时间戳同一写法（UTC、秒级、ISO 文本）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DatabaseCredentialStore:
    """把密钥存进**设置库**的 ``settings_meta``（键 ``credentials.<ref>`` → 明文密钥）。

    为什么是 ``settings_meta`` 而不是给连接表加一列：那一列要跟着"每个版本一份凭据"的历史行一起加
    （§6.8 要求固定版本取固定凭据），而通用 KV 表本来就在、且已经承担了同类的运行时偏好
    （能力开关、外部路径档位）。**不新增迁移**，也不动连接表的形状。

    先建对象、后到连接（装配顺序如此）：:meth:`bind` 由
    :func:`~local_agent.model_settings.store.open_settings_database` 在开库后调用；
    绑上前任何读写都按"设置库不可用"**失败闭合**，不是静默返回空。
    """

    note: str | None = None
    kind: CredentialKind = "local"
    _connection: sqlite3.Connection | None = None

    def bind(self, connection: sqlite3.Connection) -> None:
        """绑定已打开的设置库连接（幂等：重复绑定到同一个连接没有副作用）。"""
        self._connection = connection

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise CredentialStoreError("设置库尚未打开：密钥读写不可用")
        return self._connection

    def set(self, ref: str, secret: str) -> None:
        if not is_credential_ref(ref):
            raise CredentialStoreError(f"凭据引用不合法（应以 {CREDENTIAL_REF_PREFIX} 开头）：{ref}")
        connection = self._require()
        try:
            connection.execute(
                "INSERT INTO settings_meta (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (CREDENTIAL_META_PREFIX + ref, secret, _now_iso()),
            )
            connection.commit()
        except sqlite3.Error as exc:  # 只读库、磁盘满等：转成本项目异常，由调用方给可读提示
            raise CredentialStoreError(f"写入设置库失败：{type(exc).__name__}") from exc

    def get(self, ref: str) -> str | None:
        connection = self._require()
        try:
            row = connection.execute(
                "SELECT value FROM settings_meta WHERE key = ?", (CREDENTIAL_META_PREFIX + ref,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise CredentialStoreError(f"读取设置库失败：{type(exc).__name__}") from exc
        return None if row is None else str(row[0])

    def delete(self, ref: str) -> None:
        try:
            connection = self._require()
            connection.execute(
                "DELETE FROM settings_meta WHERE key = ?", (CREDENTIAL_META_PREFIX + ref,)
            )
            connection.commit()
        except (CredentialStoreError, sqlite3.Error):
            # 删除按尽力而为：库短暂不可写不该阻断调用方（与 keyring 时代的 delete 同一口径）。
            return


def open_credential_store(preference: str = "auto") -> CredentialStore:
    """按配置挑一个可用的凭据存储（2026-10-13：默认从系统凭据存储改为**落设置库**）。

    Args:
        preference: ``"auto"``（默认）→ 回落设置库；``"memory"`` 强制临时模式（不想让密钥落盘时用，
            代价是重启后要重新录入）。

    Returns:
        存储对象。``auto`` 返回的 :class:`DatabaseCredentialStore` 需要调用方在开库后
        :meth:`~DatabaseCredentialStore.bind`——`open_settings_database` 会做这件事。
    """
    if preference == "memory":
        return InMemoryCredentialStore(
            note="已按配置使用临时模式：密钥只在本进程内有效，重启后需要重新录入。"
        )
    if preference != "auto":
        return InMemoryCredentialStore(note=f"credential_store={preference} 无法识别：已降级为临时模式。")
    return DatabaseCredentialStore()


def secrets_from(store: CredentialStore, refs: Iterable[str | None]) -> list[str]:
    """把一批引用的密钥值取出来，**只用于脱敏**（页面与日志都不能出现这些值）。

    取不到（条目丢了、库暂时打不开）就跳过：脱敏是尽力而为的最后一道网，不能因为它失败而阻断请求。
    """
    values: list[str] = []
    for ref in refs:
        if not ref:
            continue
        try:
            secret = store.get(ref)
        except CredentialStoreError:
            continue
        if secret:
            values.append(secret)
    return values

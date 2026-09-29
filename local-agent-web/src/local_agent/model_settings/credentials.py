"""密钥存储（T29A；契约 §6.8 第 4 条）。

三条硬规则：

1. **密钥只进系统凭据存储或进程内存**：设置库、Checkpoint、请求台账、事件、日志、导出与截图都不许有明文；
2. **读取接口不返回密钥**：对页面只回答"已配置 / 未配置"；:meth:`CredentialStore.get` 是**构造模型客户端**
   用的内部通道，不得进入任何 DTO；
3. **不可用时不落盘**：系统凭据存储不可用时降级为**进程内临时**密钥，并明确提示"重启后需重新录入"，
   绝不允许悄悄写明文文件。

Windows 走 ``keyring``（后端即凭据管理器）做持久存储；其它平台按 §6.8 只提供临时模式——POSIX 持久凭据
需要真机验证，本轮不做，免得给人一个"看起来保存了、其实没保存"的假象。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from local_agent.exceptions import CredentialStoreError

#: 凭据引用的前缀：引用里只放连接 id，不含任何密钥材料。
CREDENTIAL_REF_PREFIX: Final[str] = "local-agent-web:"

#: 系统凭据存储里的"服务名"（Windows 凭据管理器按 服务 + 账号 定位一条记录）。
KEYRING_SERVICE: Final[str] = "local-agent-web"

#: 启动探测用的引用：只在真实凭据存储里写/读/删一次，用来确认它真的可用。
_PROBE_REF: Final[str] = f"{CREDENTIAL_REF_PREFIX}__probe__"

CredentialKind = Literal["os", "memory"]


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

    #: ``os`` = 系统凭据存储（重启后仍在）；``memory`` = 仅本进程有效。
    kind: CredentialKind
    #: 给页面显示的降级说明（``kind="os"`` 时为 ``None``）。
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

    note: str | None = "系统凭据存储不可用：密钥只在本进程内有效，重启后需要重新录入。"
    kind: CredentialKind = "memory"
    _secrets: dict[str, str] = field(default_factory=dict, repr=False)

    def set(self, ref: str, secret: str) -> None:
        self._secrets[ref] = secret

    def get(self, ref: str) -> str | None:
        return self._secrets.get(ref)

    def delete(self, ref: str) -> None:
        self._secrets.pop(ref, None)


@dataclass
class KeyringCredentialStore:
    """系统凭据存储（Windows 凭据管理器）。``module`` 可注入假后端做测试。"""

    note: str | None = None
    kind: CredentialKind = "os"
    service: str = KEYRING_SERVICE
    #: 被注入的 keyring 模块；``None`` 时用真实 ``keyring``。
    module: object | None = None

    def _backend(self) -> object:
        if self.module is not None:
            return self.module
        import keyring  # 只有真要走系统凭据存储时才导入

        return keyring

    def set(self, ref: str, secret: str) -> None:
        if not is_credential_ref(ref):
            raise CredentialStoreError(f"凭据引用不合法（应以 {CREDENTIAL_REF_PREFIX} 开头）：{ref}")
        try:
            self._backend().set_password(self.service, ref, secret)  # type: ignore[attr-defined]
        except Exception as exc:  # 后端五花八门，统一转成本项目异常
            raise CredentialStoreError(f"写入系统凭据存储失败：{type(exc).__name__}") from exc

    def get(self, ref: str) -> str | None:
        try:
            return self._backend().get_password(self.service, ref)  # type: ignore[attr-defined]
        except Exception as exc:
            raise CredentialStoreError(f"读取系统凭据存储失败：{type(exc).__name__}") from exc

    def delete(self, ref: str) -> None:
        try:
            self._backend().delete_password(self.service, ref)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - keyring 在"条目不存在"时抛后端专有异常，删除按尽力而为处理
            return


def open_credential_store(
    preference: str = "auto", *, platform: str | None = None, module: object | None = None
) -> CredentialStore:
    """按配置与平台挑一个可用的凭据存储。

    Args:
        preference: ``"auto"`` 按平台决定；``"memory"`` 强制临时模式（用户不想写系统凭据存储，或测试）。
        platform: 平台名，默认 ``sys.platform``；测试可注入。
        module: keyring 后端；``None`` 用真实 ``keyring``（测试注入假后端，避免动本机凭据管理器）。

    Returns:
        可用的存储。系统凭据存储**探测失败**时返回临时模式并带上原因，不是静默失败。
    """
    if preference == "memory":
        return InMemoryCredentialStore(
            note="已按配置使用临时模式：密钥只在本进程内有效，重启后需要重新录入。"
        )
    if preference != "auto":
        return InMemoryCredentialStore(note=f"credential_store={preference} 无法识别：已降级为临时模式。")

    current = sys.platform if platform is None else platform
    if not current.startswith("win"):
        return InMemoryCredentialStore(
            note=(
                f"当前平台（{current}）没有可用的持久凭据方案：密钥只在本进程内有效，"
                "重启后需要重新录入（POSIX 持久凭据尚未真机验证）。"
            )
        )

    # 探测一次真写入/读回：只有它能证明"这家机器上真的存得进去"，光看后端类型不算。
    store = KeyringCredentialStore(module=module)
    try:
        store.set(_PROBE_REF, "probe")
        if store.get(_PROBE_REF) != "probe":
            raise CredentialStoreError("系统凭据存储读回校验失败")
    except CredentialStoreError as exc:
        return InMemoryCredentialStore(note=f"{exc}：已降级为临时模式，重启后需要重新录入。")
    store.delete(_PROBE_REF)
    return store


def secrets_from(store: CredentialStore, refs: Iterable[str | None]) -> list[str]:
    """把一批引用的密钥值取出来，**只用于脱敏**（页面与日志都不能出现这些值）。

    取不到（条目丢了、后端坏了）就跳过：脱敏是尽力而为的最后一道网，不能因为它失败而阻断请求。
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

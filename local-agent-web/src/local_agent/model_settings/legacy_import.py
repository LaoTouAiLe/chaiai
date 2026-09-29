"""旧 ``config.yaml`` 的幂等导入（T29A；契约 §6.8 第 2 条）。

规则：

- **只在"还没有任何连接、且从未导入过"时动作**：重复启动不重复导入，也不覆盖用户在页面上的改动；
- 导入的是**非秘密**部分 + 旧取 Key 路径（``api_key_env``）：密钥仍从环境变量取，**不搬进设置库**；
- 导入的连接标记为"未验证"（没测过就是没测过），并设为默认连接；
- 不重写 ``config.yaml``，也不改进程环境变量。

为什么是"只导一次"而不是"每次启动对齐 YAML"：用户在页面上改过之后就以页面为准。反过来（每次启动用
YAML 覆盖）会在用户不知情时把连接选择悄悄退回旧配置。
"""

from __future__ import annotations

from dataclasses import dataclass

from local_agent.config import LlmConfig
from local_agent.model_settings.models import ConnectionDraft, transport_of
from local_agent.model_settings.store import (
    META_LEGACY_IMPORT,
    META_LEGACY_IMPORT_AT,
    ConnectionStore,
    now_iso,
)


@dataclass(frozen=True)
class LegacyImportResult:
    """导入结果；``reason`` 面向用户（"为什么没导入"要能说清）。"""

    imported: bool
    connection_id: str | None
    reason: str


def import_legacy_llm(
    store: ConnectionStore, llm: LlmConfig | None, *, source: str = "config.yaml"
) -> LegacyImportResult:
    """把旧配置的 ``llm`` 段导入成一条默认连接（幂等）。

    Args:
        store: 设置库。
        llm: 旧配置的 ``llm`` 段；全新安装没有它时传 ``None``。
        source: 记账里记下的来源（诊断用）。

    Returns:
        是否导入、导入的连接 id，以及给用户看的原因说明。

    Raises:
        ConnectionValidationError: 旧配置字段不合法（不会静默丢弃，直接暴露）。
    """
    if store.meta(META_LEGACY_IMPORT) is not None:
        return LegacyImportResult(False, None, "已经导入过旧配置，之后以页面上的设置为准。")
    if llm is None:
        # 没有可导入的东西：不记"已导入"，否则以后补上 YAML 反而不会导入了。
        return LegacyImportResult(False, None, "配置里没有 llm 段，没有可导入的连接。")
    if store.list_connections(include_disabled=True):
        # 库里已经有连接却不曾导入过（例如记账被清掉）：不猜、不合并，直接钉死"不要导入"。
        store.set_meta(META_LEGACY_IMPORT, f"skipped:{source}")
        store.set_meta(META_LEGACY_IMPORT_AT, now_iso())
        return LegacyImportResult(False, None, "设置库里已有连接，跳过旧配置导入。")

    transport = transport_of(llm.base_url)
    draft = ConnectionDraft(
        display_name=llm.model,
        base_url=llm.base_url,
        model=llm.model,
        context_window_tokens=llm.context_window_tokens,
        max_tokens=llm.max_tokens,
        temperature=llm.temperature,
        timeout_seconds=llm.timeout_seconds,
        retry_count=llm.retry_count,
        streaming=llm.streaming,
        auth_mode="api_key",
        # 旧配置里的明文端点按"已经确认过"处理：它本来就在这样跑，导入不该把它拦下来。
        # 页面会照常标出明文传输风险。
        allow_plaintext_http=transport == "plaintext-http",
        api_key_env=llm.api_key_env,
    )
    # 不传 api_key：旧配置的密钥仍从 `api_key_env` 指向的环境变量取，不进设置库也不进凭据存储。
    connection = store.save(draft)
    store.select_default(connection.connection_id)
    store.set_meta(META_LEGACY_IMPORT, f"imported:{source}")
    store.set_meta(META_LEGACY_IMPORT_AT, now_iso())
    return LegacyImportResult(
        True,
        connection.connection_id,
        f"已从 {source} 导入一条连接（{llm.model}）：密钥仍从环境变量 {llm.api_key_env} 读取。",
    )

"""模型连接设置（T29A；契约 §6.8）：连接记录、密钥存储、设置库与旧配置导入。

对外只暴露下面这些名字：``api/``、应用服务与 Streamlit 都从这里取，不直接摸子模块内部。
"""

from __future__ import annotations

from local_agent.model_settings.credentials import (
    CredentialStore,
    InMemoryCredentialStore,
    KeyringCredentialStore,
    credential_ref_for,
    is_credential_ref,
    open_credential_store,
    secrets_from,
)
from local_agent.model_settings.legacy_import import LegacyImportResult, import_legacy_llm
from local_agent.model_settings.models import (
    PLACEHOLDER_API_KEY,
    PROVIDER,
    CapabilityReport,
    ConnectionDraft,
    ModelBinding,
    ModelConnection,
    endpoint_fingerprint,
    transport_of,
    validate_draft,
)
from local_agent.model_settings.store import (
    ConnectionStore,
    Selection,
    SelectionRef,
    SettingsDatabase,
    current_settings_version,
    open_settings_database,
    resolve_api_key,
)

__all__ = [
    "PLACEHOLDER_API_KEY",
    "PROVIDER",
    "CapabilityReport",
    "ConnectionDraft",
    "ConnectionStore",
    "CredentialStore",
    "InMemoryCredentialStore",
    "KeyringCredentialStore",
    "LegacyImportResult",
    "ModelBinding",
    "ModelConnection",
    "Selection",
    "SelectionRef",
    "SettingsDatabase",
    "credential_ref_for",
    "current_settings_version",
    "endpoint_fingerprint",
    "import_legacy_llm",
    "is_credential_ref",
    "open_credential_store",
    "open_settings_database",
    "resolve_api_key",
    "secrets_from",
    "transport_of",
    "validate_draft",
]

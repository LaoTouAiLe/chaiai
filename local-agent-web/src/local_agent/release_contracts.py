"""T84：从代码定义生成兼容基线；不读取用户配置、数据库或凭据。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from local_agent import __version__
from local_agent.api.app import API_VERSION
from local_agent.api.openapi import openapi_document
from local_agent.application.events import COMMON_EVENT_FIELDS, EVENT_TYPES
from local_agent.application.models import SCHEMA_VERSION
from local_agent.config import AppConfig
from local_agent.persistence.migrations import (
    CHAT_SCHEMA_VERSION,
    LONG_TERM_SCHEMA_VERSION,
    SETTINGS_SCHEMA_VERSION,
)


def canonical_document(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_document(value).encode("utf-8")).hexdigest()


def compatibility_manifest() -> dict[str, Any]:
    """同一份代码与依赖生成相同结果；运行中的用户设置不参与基线。"""
    return {
        "manifest_version": 1,
        "release_target": "1.0.0",
        "release_status": "candidate",
        "package_version": __version__,
        "api_version": API_VERSION,
        "dto_schema_version": SCHEMA_VERSION,
        "database_versions": {
            "chat": CHAT_SCHEMA_VERSION,
            "long_term": LONG_TERM_SCHEMA_VERSION,
            "settings": SETTINGS_SCHEMA_VERSION,
        },
        "event_types": list(EVENT_TYPES),
        "event_common_fields": list(COMMON_EVENT_FIELDS),
        "config_schema_sha256": fingerprint(AppConfig.model_json_schema()),
        "config_defaults_sha256": fingerprint(AppConfig().model_dump(mode="json")),
        "openapi_sha256": fingerprint(openapi_document()),
    }


def changed_contracts(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """只返回变更键名，不把配置或载荷正文放进诊断输出。"""
    return sorted(key for key in expected.keys() | actual.keys()
                  if key not in expected or key not in actual or expected[key] != actual[key])

"""对象存储的访问密钥（2026-10-20）：与模型连接共用同一套凭据机制。

三条规则与 :mod:`local_agent.model_settings.credentials` 一致：

1. 密钥只进设置库或进程内存（Checkpoint、日志、DTO 都不许有明文）；
2. 页面只看到"已配置 / 未配置"，**不回读**；
3. 一条固定引用（对象存储没有"按版本滚动"这回事），值为 JSON
   ``{"access_key": ..., "secret_key": ...}``。

为什么是 JSON 而不是两条引用：两者必须成对出现，分开存会出现"换了一半"的中间态；
一个条目写入即整体覆盖，读出来要么齐全、要么当作没配。
"""

from __future__ import annotations

import json

from local_agent.exceptions import CredentialStoreError
from local_agent.model_settings.credentials import CredentialKind, CredentialStore

#: 对象存储凭据的引用：固定一条，不含任何密钥材料。
OSS_CREDENTIAL_REF = "local-agent-web:attachments:oss"


class OssKeyStore:
    """Access Key / Secret Key 的读写。没有凭据存储时按"不可用"失败闭合。"""

    def __init__(self, credentials: CredentialStore | None = None) -> None:
        self._credentials = credentials

    @property
    def available(self) -> bool:
        return self._credentials is not None

    @property
    def store_kind(self) -> CredentialKind | None:
        return None if self._credentials is None else self._credentials.kind

    @property
    def store_note(self) -> str | None:
        return None if self._credentials is None else self._credentials.note

    def read(self) -> tuple[str, str] | None:
        """运行期取密钥对；没配、或内容坏了都返回 ``None``（失败闭合）。"""
        if self._credentials is None:
            return None
        raw = self._credentials.get(OSS_CREDENTIAL_REF)
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        access = str(parsed.get("access_key") or "").strip()
        secret = str(parsed.get("secret_key") or "").strip()
        return (access, secret) if access and secret else None

    def configured(self) -> bool:
        return self.read() is not None

    def set(self, access_key: str, secret_key: str) -> None:
        access = str(access_key or "").strip()
        secret = str(secret_key or "").strip()
        if not access or not secret:
            raise CredentialStoreError("Access Key 与 Secret Key 都要填：留空表示保持原样")
        if self._credentials is None:
            raise CredentialStoreError("没有可用的凭据存储：密钥无法保存")
        self._credentials.set(
            OSS_CREDENTIAL_REF,
            json.dumps({"access_key": access, "secret_key": secret}),
        )

    def clear(self) -> None:
        if self._credentials is not None:
            self._credentials.delete(OSS_CREDENTIAL_REF)

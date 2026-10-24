"""联网检索的密钥（Tavily；与模型连接共用同一套凭据机制）。

三条硬规则与 :mod:`local_agent.model_settings.credentials` 完全一致：

1. **密钥只进设置库或进程内存**：Checkpoint、请求台账、事件、日志与导出都不许有明文；
2. **读取接口不返回密钥**：对页面只回答"已配置 / 未配置"；:meth:`WebSearchKeyStore.resolve` 是
   **装配运行期工具**用的内部通道，不得进入任何 DTO；
3. **只有一处落盘**：设置库的 ``settings_meta``（与模型连接、能力开关同库同表）。

与模型连接的两点不同：

- 引用是**固定**的一条（不按连接 id + 版本滚动）：检索没有"某一轮固定用某个版本的凭据"这回事；
- 取值顺序保留**旧环境变量路径**（``web_access.tavily_api_key_env``，默认 ``TAVILY_API_KEY``）：
  升级前只有环境变量可配，这条路径不拆（与 ``resolve_api_key`` 同一口径）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from local_agent.exceptions import CredentialStoreError
from local_agent.model_settings.credentials import CredentialKind, CredentialStore

#: Tavily 的凭据引用：固定一条，不含任何密钥材料。
TAVILY_CREDENTIAL_REF = "local-agent-web:web-search:tavily"


class WebSearchKeyStore:
    """Tavily 密钥：写入 / 清除 / 组装运行期取值。没有凭据存储时按"不可用"失败闭合。"""

    def __init__(self, credentials: CredentialStore | None = None) -> None:
        self._credentials = credentials

    @property
    def available(self) -> bool:
        """有没有可用的凭据存储（``False`` 时写入会被拒绝，读取只能靠环境变量）。"""
        return self._credentials is not None

    @property
    def store_kind(self) -> CredentialKind | None:
        """凭据存储的持久性（``local`` 落设置库 / ``memory`` 仅本进程）；没有存储时 ``None``。"""
        return None if self._credentials is None else self._credentials.kind

    @property
    def store_note(self) -> str | None:
        """降级说明（持久存储时为 ``None``）。"""
        return None if self._credentials is None else self._credentials.note

    def stored(self) -> str | None:
        """设置库里存着的密钥明文；没有返回 ``None``。**只给装配工具用，不进任何 DTO。**"""
        if self._credentials is None:
            return None
        return self._credentials.get(TAVILY_CREDENTIAL_REF)

    def set(self, secret: str) -> None:
        """写入（覆盖）密钥。

        Raises:
            CredentialStoreError: 密钥为空白，或本进程没有可用的凭据存储。
        """
        value = str(secret or "").strip()
        if not value:
            raise CredentialStoreError("密钥不能是空白：要么填一个，要么保持原样")
        if self._credentials is None:
            raise CredentialStoreError("没有可用的凭据存储：密钥无法保存")
        self._credentials.set(TAVILY_CREDENTIAL_REF, value)

    def clear(self) -> None:
        """删除已保存的密钥；没有条目时静默返回（幂等）。"""
        if self._credentials is not None:
            self._credentials.delete(TAVILY_CREDENTIAL_REF)

    def resolve(self, *, env_name: str, environ: Mapping[str, str] | None = None) -> str | None:
        """运行期取值：**凭据存储 → 旧环境变量 → ``None``**。

        Returns:
            密钥明文，**只给构造工具用**；不要把返回值放进任何 DTO、日志或异常消息。
        """
        stored = self.stored()
        if stored:
            return stored
        env = os.environ if environ is None else environ
        value = env.get(env_name, "").strip()
        return value or None

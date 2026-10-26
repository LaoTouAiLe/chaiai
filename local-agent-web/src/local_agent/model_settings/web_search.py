"""联网检索的密钥（Tavily / DeepSeek；与模型连接共用同一套凭据机制）。

三条硬规则与 :mod:`local_agent.model_settings.credentials` 完全一致：

1. **密钥只进设置库或进程内存**：Checkpoint、请求台账、事件、日志与导出都不许有明文；
2. **读取接口不返回密钥**：对页面只回答"已配置 / 未配置"；:meth:`WebSearchKeyStore.resolve` 是
   **装配运行期工具**用的内部通道，不得进入任何 DTO；
3. **只有一处落盘**：设置库的 ``settings_meta``（与模型连接、能力开关同库同表）。

与模型连接的两点不同：

- 引用是**按后端固定**的一条（不按连接 id + 版本滚动）：检索没有"某一轮固定用某个版本的凭据"
  这回事。后端之间**各占一条引用**（2026-10-26 加入 DeepSeek 时定的口径）：换后端不会覆盖
  另一个后端的密钥，删掉一条也只影响它自己；
- 取值顺序保留**旧环境变量路径**（``web_access.tavily_api_key_env`` / ``deepseek_api_key_env``）：
  升级前只有环境变量可配，这条路径不拆（与 ``resolve_api_key`` 同一口径）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from local_agent.exceptions import CredentialStoreError
from local_agent.model_settings.credentials import CredentialKind, CredentialStore

#: Tavily 的凭据引用：固定一条，不含任何密钥材料。
TAVILY_CREDENTIAL_REF = "local-agent-web:web-search:tavily"

#: DeepSeek 搜索的凭据引用：与 Tavily 平行的一条，互不覆盖。
DEEPSEEK_CREDENTIAL_REF = "local-agent-web:web-search:deepseek"

#: 后端 → 凭据引用。键与 ``config.ApiSearchProvider`` 一一对应（两处各写一份时，
#: "页面能填的后端"与"存得下的后端"必须同时改，否则会出现填了存不进去的空格）。
PROVIDER_CREDENTIAL_REFS: Mapping[str, str] = {
    "tavily": TAVILY_CREDENTIAL_REF,
    "deepseek": DEEPSEEK_CREDENTIAL_REF,
}

#: 不传 ``provider`` 时的后端：升级前只有 Tavily，旧调用点的语义因此保持不变。
DEFAULT_PROVIDER = "tavily"


def credential_ref(provider: str) -> str:
    """按后端取凭据引用。

    Raises:
        CredentialStoreError: 后端名不在闭集里——不猜、也不回落到 Tavily（那会把一条密钥
            写到用户没选的后端名下）。
    """
    try:
        return PROVIDER_CREDENTIAL_REFS[provider]
    except KeyError as exc:
        raise CredentialStoreError(f"未知的检索后端：{provider}") from exc


class WebSearchKeyStore:
    """检索服务密钥：写入 / 清除 / 组装运行期取值。没有凭据存储时按"不可用"失败闭合。

    ``provider`` 一律默认 ``tavily``：升级前只有这一个后端，不传参的调用点行为不变。
    """

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

    def stored(self, provider: str = DEFAULT_PROVIDER) -> str | None:
        """设置库里存着的密钥明文；没有返回 ``None``。**只给装配工具用，不进任何 DTO。**"""
        if self._credentials is None:
            return None
        return self._credentials.get(credential_ref(provider))

    def set(self, secret: str, provider: str = DEFAULT_PROVIDER) -> None:
        """写入（覆盖）某个后端的密钥。

        Raises:
            CredentialStoreError: 密钥为空白，或本进程没有可用的凭据存储。
        """
        value = str(secret or "").strip()
        if not value:
            raise CredentialStoreError("密钥不能是空白：要么填一个，要么保持原样")
        if self._credentials is None:
            raise CredentialStoreError("没有可用的凭据存储：密钥无法保存")
        self._credentials.set(credential_ref(provider), value)

    def clear(self, provider: str = DEFAULT_PROVIDER) -> None:
        """删除某个后端已保存的密钥；没有条目时静默返回（幂等）。"""
        if self._credentials is not None:
            self._credentials.delete(credential_ref(provider))

    def resolve(
        self, *, env_name: str, provider: str = DEFAULT_PROVIDER,
        environ: Mapping[str, str] | None = None,
    ) -> str | None:
        """运行期取值：**凭据存储 → 旧环境变量 → ``None``**。

        Returns:
            密钥明文，**只给构造工具用**；不要把返回值放进任何 DTO、日志或异常消息。
        """
        stored = self.stored(provider)
        if stored:
            return stored
        env = os.environ if environ is None else environ
        value = env.get(env_name, "").strip()
        return value or None

"""模型连接的领域记录与校验（T29A；契约 §6.8、§15.9）。

这一层只回答两件事：**一条连接记录长什么样**、**什么样的连接算合法**。它不碰数据库、不碰网络、
不碰凭据存储——同一套校验在 API、应用服务与旧配置导入三处都要用，写进路由就会各自漂移。

口径都能追溯到 §6.8：

- **上下文窗口不能猜**：``context_window_tokens`` 必填且必须大于 ``max_tokens``（与 ``LlmConfig`` 同一条规则，
  压缩触发比以"窗口 − 输出预留"为分母）；
- **非回环端点默认要 HTTPS**：明文 HTTP 只有用户**明确确认**过（``allow_plaintext_http``）才可保存，
  并在页面上标出明文风险；拒绝 URL 内嵌凭据、非 HTTP(S) scheme；
- **密钥不进记录**：记录里只有不含明文的 ``credential_ref``；``auth_mode="none"`` 表示本地端点不需要 Key，
  协议库要的占位参数只在内存里（:data:`PLACEHOLDER_API_KEY`）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final, Literal, TypedDict, cast
from urllib.parse import urlsplit

from local_agent.config import LlmConfig, is_loopback_host
from local_agent.exceptions import ConnectionValidationError

#: v0.4 只管理聊天模型，且只支持 OpenAI 兼容端点（§6.8）。
PROVIDER: Final[str] = "openai_compatible"

#: 无鉴权端点交给协议库的**占位**密钥。它只存在于内存中的客户端构造参数里，
#: 绝不写进设置库、凭据存储或任何请求体（§6.8 明确禁止把占位值保存成用户密钥）。
PLACEHOLDER_API_KEY: Final[str] = "local-agent-web:no-key-required"

#: 构造运行期 ``LlmConfig`` 时给 ``api_key_env`` 的占位名（见 :meth:`ModelConnection.to_llm_config`）。
#: 它**不会**被读取：页面创建的连接由凭据存储提供密钥。取这个名字是为了"万一被读到也读不到东西"。
PLACEHOLDER_KEY_ENV: Final[str] = "LOCAL_AGENT_WEB_CONNECTION_KEY"

#: 鉴权模式：``api_key`` 需要一个密钥（凭据存储或旧环境变量），``none`` 是本地端点明确不需要。
AuthMode = Literal["api_key", "none"]

#: 能力检测的单项结论：
#:
#: - ``unknown``：还没测，或测了但判断不了（**不等于**通过）；
#: - ``pass`` / ``fail``：明确通过 / 明确失败；
#: - ``unsupported``：端点明确拒绝这项能力（例如带上 `tools` 就 400）——这与"测失败"是两件事：
#:   §6.8 要求"缺失工具能力时允许普通聊天"，页面要能区分"用不了"和"没测通"。
CapabilityOutcome = Literal["unknown", "pass", "fail", "unsupported"]

#: 连接整体状态，由三项能力**派生**（不单独存一份，避免两份真源打架）。
#:
#: `testing` 不在其中：它是页面在等"测试连接"返回时的临时展示态，不是连接持久状态。
#: DTO 的 `unsupported` 与这里的派生规则对齐（任一项 unsupported → unsupported）。
ConnectionStatus = Literal["unverified", "verified", "failed", "unsupported"]

#: 端点传输性质：HTTPS / 回环明文（本机回环，风险可接受）/ 非回环明文（必须用户确认）。
TransportKind = Literal["https", "loopback-http", "plaintext-http"]

_HTTP_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


def transport_of(base_url: str) -> TransportKind:
    """端点属于哪种传输（供页面标"明文传输"）。"""
    parts = urlsplit(base_url.strip())
    if parts.scheme == "https":
        return "https"
    return "loopback-http" if is_loopback_host(parts.hostname or "") else "plaintext-http"


def endpoint_fingerprint(base_url: str) -> str:
    """端点的**安全指纹**：只有 scheme + host + port，不含路径、账号与任何密钥。

    用于事后把"某次调用打到哪个端点"讲清楚（§15.7 的端点安全指纹），因此它必须是稳定的、
    可跨进程复现的，且**不得**包含密钥或凭据。
    """
    parts = urlsplit(base_url.strip())
    host = (parts.hostname or "").lower()
    port = parts.port
    origin = f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")
    return hashlib.sha256(origin.encode("utf-8")).hexdigest()[:16]


class ModelBinding(TypedDict, total=False):
    """一次运行**固定**的模型连接（§6.8、§15.9）。

    为什么要有它：切换模型只影响"下一次新用户提交"，已开始、等待审批或恢复中的运行必须继续用
    **提交时**那条连接的版本与参数。"固定"要能跨服务重启成立，所以它随 Checkpoint 落盘
    （`graph.state.AgentState.model_binding`）——否则重启后恢复一个挂起审批，就会用当前设置里的
    新连接去接着跑。

    **这里只有引用，没有密钥**：``credential_ref`` 是不含明文的引用，恢复时按它去系统凭据存储取。
    端点安全指纹只含 scheme + host + port，用来事后说明"那一次打到了哪个端点"。
    """

    connection_id: str
    config_version: int
    model: str
    credential_ref: str | None
    endpoint_fingerprint: str


@dataclass(frozen=True)
class CapabilityReport:
    """一条连接最近一次"测试连接"的结论（§6.8：由用户主动触发，分项报告）。

    ``message`` 是可执行的脱敏说明（例如"鉴权失败：检查 Key"），不放大段供应商原文。
    """

    chat: CapabilityOutcome = "unknown"
    tools: CapabilityOutcome = "unknown"
    streaming: CapabilityOutcome = "unknown"
    checked_at: datetime | None = None
    message: str | None = None

    @property
    def status(self) -> ConnectionStatus:
        """三项全过才算 ``verified``；任一项失败即 ``failed``；端点明确不支持 → ``unsupported``；
        其余（含没测过）是 ``unverified``。"""
        outcomes = (self.chat, self.tools, self.streaming)
        if all(outcome == "pass" for outcome in outcomes):
            return "verified"
        if any(outcome == "fail" for outcome in outcomes):
            return "failed"
        if any(outcome == "unsupported" for outcome in outcomes):
            return "unsupported"
        return "unverified"

    @property
    def verifies_chat(self) -> bool:
        """凡是要模型参与，至少聊天能力得测过并且通过。"""
        return self.chat == "pass"

    @property
    def verifies_tools(self) -> bool:
        """需要工具的任务：工具能力没测过或没过就不能接（不能静默降级成别的模型）。"""
        return self.tools == "pass"


@dataclass(frozen=True)
class ModelConnection:
    """一条已保存的模型连接（当前版本）。

    ``enabled=False`` 表示"已停用"（删除只停用，历史与用量不删，§6.8）。
    """

    connection_id: str
    display_name: str
    base_url: str
    model: str
    context_window_tokens: int
    max_tokens: int
    temperature: float
    timeout_seconds: int
    retry_count: int
    streaming: bool
    auth_mode: AuthMode
    credential_ref: str | None
    config_version: int
    capability: CapabilityReport = field(default_factory=CapabilityReport)
    enabled: bool = True
    allow_plaintext_http: bool = False
    provider: str = PROVIDER
    #: 旧 ``config.yaml`` 的取 Key 路径（``api_key_env``）。只在幂等导入时保留，
    #: 页面不编辑它：它说明"这条连接原本从哪个环境变量取密钥"。
    api_key_env: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def transport(self) -> TransportKind:
        return transport_of(self.base_url)

    @property
    def endpoint_fingerprint(self) -> str:
        return endpoint_fingerprint(self.base_url)

    @property
    def requires_key(self) -> bool:
        return self.auth_mode == "api_key"

    @property
    def status(self) -> ConnectionStatus:
        return self.capability.status

    def with_version(self, version: int) -> ModelConnection:
        """同一条连接的另一版本（版本是身份的一部分，不能就地改）。"""
        return replace(self, config_version=version)

    def to_llm_config(self) -> LlmConfig:
        """按这条连接构造**运行期**的 ``LlmConfig``（§6.8：按提交边界构造客户端）。

        为什么要有它：运行的上下文窗口、输出上限、温度、超时与重试都取自**这条连接**，而不是
        ``config.yaml`` 里那一段；复用 ``LlmConfig`` 是为了让"连接参数"和"运行参数"共用同一套校验，
        不出现两份规则。

        ``api_key_env`` 只对"从环境变量取密钥"的旧连接有意义；页面创建的连接由凭据存储提供密钥，
        因此这里填一个不会被读取的占位名。**密钥值永远不在这里。**
        """
        return LlmConfig(
            provider=cast('Literal["openai_compatible"]', self.provider),
            base_url=self.base_url,
            api_key_env=self.api_key_env or PLACEHOLDER_KEY_ENV,
            model=self.model,
            context_window_tokens=self.context_window_tokens,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            timeout_seconds=self.timeout_seconds,
            retry_count=self.retry_count,
            streaming=self.streaming,
        )


@dataclass(frozen=True)
class ConnectionDraft:
    """待保存的连接字段（新建或编辑的**输入**）。

    ``connection_id`` 为 ``None`` 表示新建；给了就是编辑那条（会产生新版本）。
    ``context_window_tokens`` 没有默认值：§6.8 要求由用户填写或由可信检测确认，不能猜。
    """

    display_name: str
    base_url: str
    model: str
    context_window_tokens: int
    max_tokens: int = 4096
    temperature: float = 0.2
    timeout_seconds: int = 120
    retry_count: int = 2
    streaming: bool = False
    auth_mode: AuthMode = "api_key"
    allow_plaintext_http: bool = False
    connection_id: str | None = None
    provider: str = PROVIDER
    api_key_env: str | None = None


def validate_draft(draft: ConnectionDraft) -> ConnectionDraft:
    """校验并归一化一条待保存连接；不合法就抛 :class:`ConnectionValidationError`。

    消息面向用户，必须说清"哪里不对、怎么改"，且不含任何密钥。

    Raises:
        ConnectionValidationError: 任一规则不满足（字段为空、URL 不安全、窗口不大于输出预留等）。
    """
    clean = replace(
        draft,
        display_name=draft.display_name.strip(),
        base_url=draft.base_url.strip(),
        model=draft.model.strip(),
    )

    if not clean.display_name:
        raise ConnectionValidationError("连接名称不能为空")
    if len(clean.display_name) > 60:
        raise ConnectionValidationError("连接名称不能超过 60 个字符")
    if clean.provider != PROVIDER:
        raise ConnectionValidationError(f"v0.4 只支持 provider={PROVIDER}")
    if clean.auth_mode not in ("api_key", "none"):
        raise ConnectionValidationError("auth_mode 只能是 api_key 或 none")
    if not clean.model:
        raise ConnectionValidationError("模型 ID 不能为空")
    _validate_base_url(clean.base_url, allow_plaintext_http=clean.allow_plaintext_http)

    if clean.context_window_tokens <= 0:
        raise ConnectionValidationError("上下文窗口（context_window_tokens）必须为正整数")
    if clean.max_tokens <= 0:
        raise ConnectionValidationError("输出上限（max_tokens）必须为正整数")
    if clean.context_window_tokens <= clean.max_tokens:
        raise ConnectionValidationError(
            f"上下文窗口（{clean.context_window_tokens}）必须大于输出上限（{clean.max_tokens}）："
            "max_tokens 是为回答预留的输出额度，输入可用预算 = 窗口 − 输出预留，必须为正数"
        )
    if not 0.0 <= clean.temperature <= 2.0:
        raise ConnectionValidationError("temperature 必须在 0 到 2 之间")
    if clean.timeout_seconds <= 0:
        raise ConnectionValidationError("超时（timeout_seconds）必须为正整数")
    if not 0 <= clean.retry_count <= 10:
        raise ConnectionValidationError("重试次数（retry_count）必须在 0 到 10 之间")
    return clean


def _validate_base_url(base_url: str, *, allow_plaintext_http: bool) -> None:
    """URL 规则（§6.8 第 5 条）：只允许 HTTP(S)、不内嵌凭据、非回环明文需明确确认。

    这里比"能不能连上"更严：读 DTO（``ModelConnectionView``）只接受不带查询/片段的纯端点，
    所以**保存时**就按同一口径拒绝，否则会出现一条存得住、却渲染不出来的连接。
    """
    if not base_url:
        raise ConnectionValidationError("端点地址（base_url）不能为空")
    if "\\" in base_url or any(char.isspace() for char in base_url):
        raise ConnectionValidationError("端点地址不能包含反斜杠或空白字符")
    parts = urlsplit(base_url)
    if parts.scheme not in _HTTP_SCHEMES:
        raise ConnectionValidationError(
            f"端点地址必须以 http:// 或 https:// 开头（当前是 {parts.scheme or '空 scheme'}）"
        )
    if parts.username or parts.password:
        raise ConnectionValidationError("端点地址不能内嵌账号或密钥：请把鉴权信息填到 Key 里")
    if not parts.hostname:
        raise ConnectionValidationError("端点地址缺少主机名")
    if parts.query or parts.fragment or "?" in base_url or "#" in base_url:
        raise ConnectionValidationError("端点地址不能带查询参数（?）或片段（#）")
    try:
        parts.port  # noqa: B018 - 读一次触发端口解析
    except ValueError as exc:
        raise ConnectionValidationError(f"端点地址的端口不合法：{exc}") from exc

    transport = transport_of(base_url)
    if transport == "plaintext-http" and not allow_plaintext_http:
        raise ConnectionValidationError(
            "非本机回环的端点必须使用 HTTPS：明文 HTTP 会把请求与密钥暴露在网络上。"
            "确认这条端点在可信局域网内、愿意承担明文风险时，显式勾选“允许明文传输”再保存"
        )

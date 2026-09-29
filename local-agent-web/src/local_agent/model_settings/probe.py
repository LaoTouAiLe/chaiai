"""测试连接：分项探测鉴权、聊天、工具调用与流式能力（T29A；契约 §6.8）。

几点口径：

- **由用户主动触发**，只在用户明确保存过的端点上发请求，页面加载时不自动探测（§6.8 最后一条）；
- 会产生**一次少量付费调用**，因此页面必须先说明这一点（前端负责文案，本模块只管事实）；
- **分项报告** `pass` / `fail` / `unknown` / `unsupported`：
  "模型没调用工具" 记 `unknown`（那是模型行为，不是能力缺陷），"端点带 `tools` 就 400" 记 `unsupported`；
- 失败消息必须**可执行且脱敏**：不回显供应商原文、不含密钥与私人绝对路径（§15.5、§15.8）。

本模块不读配置、不碰设置库：连接记录、构造器与密钥都由调用方给，方便单独测。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any, Final

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from local_agent.config import redact_secrets
from local_agent.model_settings.models import CapabilityOutcome, CapabilityReport, ModelConnection

#: 探测用的一句话：只要求最小回答，避免拉长输出（省时间也省费用）。
_PROBE_PROMPT: Final[str] = "ping"

#: 关键短语 → 结论。判定顺序有意义：先鉴权，再限流，最后才是"能力不支持"。
_AUTH_HINTS: Final[tuple[str, ...]] = (
    "401",
    "403",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "permission",
)
_RATE_HINTS: Final[tuple[str, ...]] = ("429", "rate limit", "too many requests")
_TIMEOUT_HINTS: Final[tuple[str, ...]] = ("timeout", "timed out", "deadline")
_UNSUPPORTED_HINTS: Final[tuple[str, ...]] = (
    "tools",
    "tool_choice",
    "function calling",
    "not supported",
    "unsupported",
    "invalid_request_error",
)


@tool
def _probe_tool(text: str) -> str:
    """回显给定文本。

    只用于"测试连接"：模型需要看到一个**真实**的工具 schema 才可能发起工具调用。它的函数体
    永远不执行（探测只发一次请求，不跑工具）。
    """
    return text


def probe_connection(
    connection: ModelConnection,
    *,
    build_model: Any,
    api_key: str,
    secrets: Iterable[str] = (),
) -> CapabilityReport:
    """对一条连接做分项能力探测。

    Args:
        connection: 待测连接（只读）。
        build_model: 构造客户端的可调用对象，签名同 ``factory.build_chat_model(config, api_key)``。
        api_key: 已解析的密钥；无鉴权端点传占位值。
        secrets: 仅用于脱敏的密钥取值集合。

    Returns:
        分项结论；``checked_at`` 是这次探测的时间。**任何失败都不抛异常**——失败本身就是结论，
        要能落到连接上给用户看。
    """
    llm = connection.to_llm_config()
    checked_at = datetime.now(timezone.utc)
    try:
        client = build_model(llm, api_key)
    except Exception as exc:  # noqa: BLE001 - 构造失败（配置错、依赖缺）也是"测不通"，不该把栈抛给页面
        return CapabilityReport(
            chat="fail",
            checked_at=checked_at,
            message=_describe(exc, secrets=secrets, prefix="无法构造客户端"),
        )

    try:
        client.invoke(_PROBE_PROMPT)
    except Exception as exc:  # noqa: BLE001 - 失败本身就是结论，要落到连接上给用户看
        # 鉴权与聊天在同一个调用里：这一步不通，后面两项没有意义，记 `unknown` 而不是假装失败。
        return CapabilityReport(
            chat="fail",
            tools="unknown",
            streaming="unknown",
            checked_at=checked_at,
            message=_describe(exc, secrets=secrets, prefix="聊天调用失败"),
        )

    tools_outcome, tools_note = _probe_tools(client, secrets=secrets)
    streaming_outcome, streaming_note = _probe_streaming(client, secrets=secrets)
    notes = [note for note in (tools_note, streaming_note) if note]
    return CapabilityReport(
        chat="pass",
        tools=tools_outcome,
        streaming=streaming_outcome,
        checked_at=checked_at,
        message="；".join(notes) or None,
    )


def _probe_tools(client: BaseChatModel, *, secrets: Iterable[str]) -> tuple[CapabilityOutcome, str | None]:
    """工具能力：让模型**有机会**调用一个工具。

    - 端点明确拒绝 `tools` → `unsupported`（这决定"能不能进需要工具的任务"，§6.8）；
    - 端点接受但模型没调用 → `unknown`：那是模型行为，不能当成能力缺陷；
    - 其他失败 → `fail`。
    """
    try:
        bound = client.bind_tools([_probe_tool])
        reply = bound.invoke("调用 _probe_tool，参数 text 填 pong。")
    except Exception as exc:  # noqa: BLE001 - 失败本身就是结论，分类后写进报告
        note = _describe(exc, secrets=secrets, prefix="工具调用失败")
        return ("unsupported" if _looks_unsupported(exc) else "fail"), note
    if isinstance(reply, AIMessage) and reply.tool_calls:
        return "pass", None
    return "unknown", "端点接受 tools，但这次回答没有发起工具调用（能力仍按未确认处理）。"


def _probe_streaming(client: BaseChatModel, *, secrets: Iterable[str]) -> tuple[CapabilityOutcome, str | None]:
    """流式能力：至少要收到一个增量块；一块都没有既不算通过也不算失败。"""
    try:
        chunks: Sequence[Any] = list(client.stream(_PROBE_PROMPT))
    except Exception as exc:  # noqa: BLE001 - 失败本身就是结论，分类后写进报告
        return "fail", _describe(exc, secrets=secrets, prefix="流式调用失败")
    return ("pass" if chunks else "unknown"), (None if chunks else "流式调用没有返回任何增量块。")


def _describe(exc: BaseException, *, secrets: Iterable[str], prefix: str) -> str:
    """把异常转成可执行、已脱敏的一句话（**不回显供应商原文**，只给判据）。"""
    kind = _classify(exc)
    detail = {
        "auth": "鉴权被拒绝，请检查这条连接的密钥",
        "rate": "被限流，稍后重试",
        "timeout": "请求超时，检查端点可达性或调大超时",
        "network": "连不上端点，检查地址与网络",
        "unsupported": "端点不接受这项能力",
        "unknown": f"失败类型 {type(exc).__name__}",
    }[kind]
    return redact_secrets(f"{prefix}：{detail}", list(secrets))


def _classify(exc: BaseException) -> str:
    """按 HTTP 状态码与关键短语判定失败种类；判定不到就 `unknown`（不猜）。"""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    text = f"{type(exc).__name__} {exc}".lower()
    if status in (401, 403) or any(hint in text for hint in _AUTH_HINTS):
        return "auth"
    if status == 429 or any(hint in text for hint in _RATE_HINTS):
        return "rate"
    if any(hint in text for hint in _TIMEOUT_HINTS):
        return "timeout"
    if any(hint in text for hint in _UNSUPPORTED_HINTS):
        return "unsupported"
    if any(hint in text for hint in ("connection", "connect", "resolve", "name or service")):
        return "network"
    return "unknown"


def _looks_unsupported(exc: BaseException) -> bool:
    """端点是否**明确**不接受工具调用（与 `_classify` 同一套判据）。"""
    return _classify(exc) == "unsupported"

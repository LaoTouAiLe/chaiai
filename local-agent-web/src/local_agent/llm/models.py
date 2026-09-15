"""模型能力描述与探测（架构 §11.7）。

启动时用最小契约检查探测能力，结果写入日志与 ``RuntimeContext``：

- 探测只做**构造层面**的检查（不产生真实请求，不烧 token）；
- 调用过程中若发现某能力实际不可用，把它降级为 ``False``（失败闭合）；
- Tool Calling 是工具模式的硬要求，不支持时图必须给出明确提示，不得静默退化成闲聊。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

#: 探测用的最小工具（只为确认 ``bind_tools`` 可用，不会被真正执行）。
PROBE_TOOL_NAME = "probe_capability"

TOOL_CALLING_UNAVAILABLE_REPLY = (
    "当前模型不支持工具调用（Tool Calling），无法执行需要访问文件或执行命令的请求。"
    "请更换支持 Tool Calling 的模型，或只用闲聊模式提问。"
)


@dataclass
class ModelCapabilities:
    """架构 §11.7 的模型能力集合。"""

    tool_calling: bool = False
    structured_json_schema: bool = False
    streaming: bool = False
    vision: bool = False
    max_context_tokens: int | None = None
    provider_name: str = ""

    def degrade(self, capability: str, *, reason: str = "") -> None:
        """把某项能力降级为不支持（失败闭合）。

        Args:
            capability: ``tool_calling`` / ``structured_json_schema`` / ``streaming``。
            reason: 触发降级的原因，只记录类型或简短说明，不记录密钥。
        """
        if not hasattr(self, capability):
            return
        setattr(self, capability, False)
        logger.warning("模型能力降级：%s -> False（%s）", capability, reason or "调用失败")

    def allows_tool_calling(self) -> bool:
        """工具模式是否可以继续尝试（未探测时不阻断）。"""
        return self.tool_calling

    def as_dict(self) -> dict[str, Any]:
        """便于写日志的字典形式（只含能力字段，不含密钥）。"""
        return asdict(self)


def probe_capabilities(model: BaseChatModel, *, provider_name: str = "") -> ModelCapabilities:
    """探测模型能力（构造层面，不发真实请求）。

    Args:
        model: 已构造的模型实例。
        provider_name: 写入能力的提供方标识（例如模型名）。

    Returns:
        探测结果；单项探测失败只影响该字段。
    """
    capabilities = ModelCapabilities(provider_name=provider_name)
    capabilities.tool_calling = _supports_tool_binding(model)
    capabilities.structured_json_schema = _supports_json_schema(model)
    capabilities.streaming = _supports_streaming(model)
    logger.info("模型能力探测结果：%s", capabilities.as_dict())
    return capabilities


def _supports_tool_binding(model: BaseChatModel) -> bool:
    probe_tool = _probe_tool()
    try:
        model.bind_tools([probe_tool])
    except Exception as exc:  # noqa: BLE001 — 探测失败只意味着该能力不可用
        logger.info("工具绑定探测失败：%s", type(exc).__name__)
        return False
    return True


def _supports_json_schema(model: BaseChatModel) -> bool:
    try:
        model.with_structured_output(_probe_schema(), method="json_schema")
    except Exception as exc:  # noqa: BLE001 — 探测失败只意味着该能力不可用
        logger.info("JSON Schema 探测失败：%s", type(exc).__name__)
        return False
    return True


def _supports_streaming(model: BaseChatModel) -> bool:
    return hasattr(model, "stream")


def _probe_tool() -> Any:
    from langchain_core.tools import tool

    @tool
    def probe_capability(value: str = "") -> str:
        """能力探测用的最小工具，不会被执行。"""
        return value

    return probe_capability


def _probe_schema() -> type[Any]:
    from pydantic import BaseModel

    class _ProbeSchema(BaseModel):
        ok: bool = True

    return _ProbeSchema

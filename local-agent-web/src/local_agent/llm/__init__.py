"""模型适配层：工厂、能力探测与结构化输出降级链（架构 §11.7）。"""

from local_agent.llm.models import (
    TOOL_CALLING_UNAVAILABLE_REPLY,
    ModelCapabilities,
    probe_capabilities,
)
from local_agent.llm.protocol import StructuredOutputError, call_structured, extract_json

__all__ = [
    "TOOL_CALLING_UNAVAILABLE_REPLY",
    "ModelCapabilities",
    "StructuredOutputError",
    "call_structured",
    "extract_json",
    "probe_capabilities",
]

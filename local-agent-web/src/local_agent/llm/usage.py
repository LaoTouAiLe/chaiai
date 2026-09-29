"""供应商实际用量的提取与归一化（架构 §15.6）。

**估算量与供应实际量是两个对象**：本模块只负责把一条模型回复里**供应商真正返回**的用量
读出来，绝不生成、补齐或猜测数值。拿不到就是 ``None``（未知），存在但为零才写 0。

三条硬规则（§15.6 的停止条件都落在它们上面）：

1. **未上报 ≠ 上报为零**。``langchain-openai`` 的 ``_create_usage_metadata`` 会把值为
   ``None`` 的缓存键**整个丢掉**（``{k: v for k, v in ... if v is not None}``），因此
   "键不存在"代表供应商没报，必须与"键存在且为 0"区分开。
2. **DeepSeek 的 ``prompt_tokens`` 含缓存命中**：未缓存输入 = ``prompt_tokens - cache_read``，
   不能把 ``prompt_tokens`` 直接当成"未缓存输入"，否则缓存命中会被重复计数。
3. **兼容两种拼写**：优先 ``usage_metadata.input_token_details.cache_read``（LangChain 从
   ``prompt_tokens_details.cached_tokens`` 归一而来），其次读原始
   ``response_metadata.token_usage`` 里的 ``prompt_cache_hit_tokens``（DeepSeek 旧拼写，
   LangChain 不识别它）。

流式路径为什么也能拿到原始字典：``ChatOpenAIWithReasoning`` 在流式收尾时会把最后一个
带 ``usage`` 的分片原样写进聚合消息的 ``response_metadata["token_usage"]``（见 compat 模块），
否则 DeepSeek 的 ``prompt_cache_hit_tokens`` 会在流式链路上整个丢失。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["ActualUsage", "extract_usage"]


@dataclass(frozen=True)
class ActualUsage:
    """一次模型调用**供应商实际返回**的用量（未知字段为 ``None``，不是 0）。"""

    #: 未缓存输入：``prompt_tokens - cache_read``；两者任一未知时为 ``None``。
    uncached_input_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    #: 供应商是否上报了**任何**缓存字段。``False`` 时页面显示"未上报"，而不是显示 0%。
    cache_metrics_reported: bool


def extract_usage(message: Any) -> ActualUsage | None:
    """从一条模型回复里读出实际用量。

    Args:
        message: ``AIMessage`` / ``AIMessageChunk``（或任何带 ``usage_metadata`` 的对象）。

    Returns:
        归一化后的用量；**完全没有用量信息**（供应商一个字段都没回、或响应被桩替换）时
        返回 ``None``——调用方据此写"未上报"，而不是写一排 0。
    """
    usage_metadata = getattr(message, "usage_metadata", None)
    raw = _raw_token_usage(message)
    if not isinstance(usage_metadata, Mapping) and not isinstance(raw, Mapping):
        return None

    input_tokens = _int(usage_metadata.get("input_tokens") if isinstance(usage_metadata, Mapping) else None)
    output_tokens = _int(usage_metadata.get("output_tokens") if isinstance(usage_metadata, Mapping) else None)
    input_details = _mapping(usage_metadata, "input_token_details")
    output_details = _mapping(usage_metadata, "output_token_details")
    cache_read = _int(input_details.get("cache_read")) if input_details else None
    cache_write = _int(input_details.get("cache_creation")) if input_details else None
    reasoning = _int(output_details.get("reasoning")) if output_details else None

    # 原始字典兜底：DeepSeek 的 prompt_cache_hit_tokens 不在 usage_metadata 里。
    raw_map = raw if isinstance(raw, Mapping) else {}
    if input_tokens is None:
        input_tokens = _int(raw_map.get("prompt_tokens"))
    if output_tokens is None:
        output_tokens = _int(raw_map.get("completion_tokens"))
    if cache_read is None:
        cache_read = _int(raw_map.get("prompt_cache_hit_tokens"))
    if cache_read is None:
        cache_read = _int(_mapping(raw_map, "prompt_tokens_details").get("cached_tokens"))
    if cache_write is None:
        cache_write = _int(raw_map.get("prompt_cache_write_tokens"))
    if reasoning is None:
        reasoning = _int(_mapping(raw_map, "completion_tokens_details").get("reasoning_tokens"))

    if (
        input_tokens is None
        and output_tokens is None
        and cache_read is None
        and cache_write is None
        and reasoning is None
    ):
        return None

    # 未缓存输入必须由 prompt_tokens 减去缓存命中；任一未知就整体未知（不猜）。
    if input_tokens is None or cache_read is None:
        uncached = None
    else:
        uncached = max(0, input_tokens - cache_read)

    return ActualUsage(
        uncached_input_tokens=uncached,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        cache_metrics_reported=cache_read is not None or cache_write is not None,
    )


def _raw_token_usage(message: Any) -> Any:
    """原始用量字典：``response_metadata["token_usage"]``（LangChain 保留了供应商原样）。"""
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    return metadata.get("token_usage")


def _mapping(container: Any, key: str) -> Mapping[str, Any]:
    """取嵌套字典；不是字典时返回空字典（调用方按"未知"处理）。"""
    if not isinstance(container, Mapping):
        return {}
    value = container.get(key)
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int | None:
    """尽量取整数；``bool`` 不算整数，非数或不合法返回 ``None``（未知）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
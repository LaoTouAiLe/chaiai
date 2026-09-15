"""OpenAI 兼容模型的构造（架构 §11.7 的适配层入口）。

字段名以已安装的 ``langchain-openai`` 为准：``model``、``base_url``、``api_key``、
``timeout``、``max_completion_tokens`` 是该类字段的初始化别名。T09 会在这里补能力
探测与结构化输出降级链。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from local_agent.config import LlmConfig


def build_chat_model(config: LlmConfig, api_key: str) -> ChatOpenAI:
    """按配置构造聊天模型。

    Args:
        config: ``llm`` 配置节。
        api_key: 已解析的密钥。只传给客户端，不写日志、不写入 State。

    Returns:
        可用于 ``invoke`` 的聊天模型实例（本阶段不启用流式输出）。
    """
    return ChatOpenAI(
        model=config.model,
        base_url=config.base_url,
        api_key=SecretStr(api_key),
        temperature=config.temperature,
        max_completion_tokens=config.max_tokens,
        timeout=config.timeout_seconds,
        max_retries=config.retry_count,
        streaming=False,
    )

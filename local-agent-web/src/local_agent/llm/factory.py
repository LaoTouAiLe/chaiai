"""OpenAI 兼容模型的构造（架构 §11.7 的适配层入口）。

字段名以已安装的 ``langchain-openai`` 为准：``model``、``base_url``、``api_key``、
``timeout``、``max_completion_tokens`` 是该类字段的初始化别名。T09 会在这里补能力
探测与结构化输出降级链。

返回的是 :class:`ChatOpenAIWithReasoning`（``ChatOpenAI`` 的子类）：它只多一件事——把
供应商扩展字段 ``reasoning_content`` 留住并在带 ``tool_calls`` 的消息上回传，否则 DeepSeek
思考模式的工具循环第二次请求就会 400（详见 :mod:`local_agent.llm.compat`）。对没有该字段的
模型等价于原类。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from local_agent.config import LlmConfig
from local_agent.llm.compat import ChatOpenAIWithReasoning


def build_chat_model(config: LlmConfig, api_key: str) -> ChatOpenAI:
    """按配置构造聊天模型。

    Args:
        config: ``llm`` 配置节。
        api_key: 已解析的密钥。只传给客户端，不写日志、不写入 State。

    Returns:
        可用于 ``invoke`` 的聊天模型实例（本阶段不启用流式输出）。
    """
    return ChatOpenAIWithReasoning(
        model=config.model,
        base_url=config.base_url,
        api_key=SecretStr(api_key),
        temperature=config.temperature,
        max_completion_tokens=config.max_tokens,
        timeout=config.timeout_seconds,
        max_retries=config.retry_count,
        # **刻意不传 `streaming`**（T23 实测踩过两次坑，改动前先读这段）：
        # - 传 `streaming=False`：langchain-core 的 `_streaming_disabled()` 会因"实例上显式设了
        #   streaming=False"而永久判定为不可流式，`.stream()` 退化成一次 `invoke`（永远只有一个块）；
        # - 传 `streaming=True`：它会进 `_default_params`，于是**所有** `invoke()` 请求都带
        #   `stream=true`，非流式解析路径拿不到供应商字段——思考模式的 `reasoning_content` 会消失，
        #   下一轮请求直接被 400 拒绝（真是用真实模型冒烟抓到的）。
        # 不传时：`invoke()` 照常带 `stream=false`（字段完整），`.stream()` 走真正的增量路径；
        # "要不要把增量展示给用户"由 `llm.streaming` + `ui.streaming` 那一层决定。
    )

"""供应商扩展字段的往返兼容（DeepSeek 思考模式；架构 §11.7）。

``ChatOpenAI`` 的定位是**官方 OpenAI 规范**：安装版本的 ``base.py`` docstring 原文写着
"Non-standard response fields added by third-party providers (e.g., ``reasoning_content``)
are **not** extracted or preserved"，并建议对 DeepSeek 这类端点改用 provider 专属子类。
本项目**不加 provider 专属依赖**，用一个最小的子类把这条链路接上。

为什么必须接：DeepSeek 的思考模式下，**带 ``tool_calls`` 的 assistant 消息在下一轮请求里必须
原样带回它的 ``reasoning_content``**，否则第二次请求直接 HTTP 400
（``The `reasoning_content` in the thinking mode must be passed back to the API.``）。
实测 ``deepseek-flash`` 与 ``deepseek-v4-pro`` 都会在第一次工具往返之后失败，工具模式因此
根本跑不完一轮任务。

两处改动，都很窄：

1. **响应侧**（``_create_chat_result``）：把原始响应里的 ``reasoning_content`` 收进
   ``AIMessage.additional_kwargs``。它随 Checkpoint 一起保存，恢复后的历史才拼得回原请求
   （否则一恢复就又缺字段，工具循环照样 400）。
2. **请求侧**（``_get_request_payload``）：把**凡是有这个字段的 assistant 消息**都补回 payload
   ——供应商要求的是"它给过的字段原样传回"，不只是带 ``tool_calls`` 的那条。实测两级踩坑：
   工具调用消息缺字段时第二次请求 400；补上之后，普通回答消息缺字段又让第三次请求 400。
   没有该字段的消息（非思考模型、旧 Checkpoint）不加任何字段。

**空串也必须留住**（实测踩过）：DeepSeek 会返回 ``reasoning_content: ""``——尤其是"不需要思考、
直接给工具调用"的那种响应。服务端要求的是**字段存在并原样传回**，不是"内容非空"；按"非空才保留"
处理会让这类响应丢掉字段，第二次请求照样 400。所以判据是"字段**存在**"（``None`` 才表示不存在，
即非思考模型），空串照传。

普通非思考模型（响应里没有 ``reasoning_content``）行为完全不变：不新增字段、不改内容。

口径边界：``reasoning_content`` 只进 Checkpoint 的**消息元数据**（与 ``additional_kwargs`` 里
已有的其它字段同级），不进 ``decision_trace`` / ``tool_events`` / ``error_events`` /
``final_evidence``，也不在页面渲染——架构 §10.1 的消息原文与 §11 的轨迹口径都不变。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

#: 供应商扩展字段名（DeepSeek 思考模式）。
REASONING_CONTENT_KEY = "reasoning_content"


class ChatOpenAIWithReasoning(ChatOpenAI):
    """保留并在需要时回传 ``reasoning_content`` 的 ``ChatOpenAI``。

    只影响 OpenAI 兼容端点的供应商扩展字段；官方 OpenAI 模型上等价于原类。
    """

    #: 这个供应商**确实会用**该字段（见过一次非流式响应带它）。流式路径要用它决定
    #: "要不要补一个空值占位"——`stream_placeholder_fields` 只用它，不猜供应商名字。
    _uses_reasoning_content: bool = PrivateAttr(default=False)

    def _create_chat_result(
        self, response: Any, generation_info: dict[str, Any] | None = None
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        for generation, reasoning in zip(result.generations, _response_reasoning(response)):
            # ``None`` = 响应里没有这个字段（非思考模型）；空串是 DeepSeek 的真实返回值，
            # 必须原样留住——服务端要的是"字段存在并传回"，不是"内容非空"。
            if reasoning is not None:
                self._uses_reasoning_content = True
                generation.message.additional_kwargs[REASONING_CONTENT_KEY] = reasoning
        return result

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        wire = payload.get("messages")
        if not isinstance(wire, list) or len(wire) != len(messages):
            # 消息与出站列表对不上（理论上不会发生）：宁可让服务端按原样报错，
            # 也不要把 reasoning_content 配到另一条消息上。
            return payload
        for source, target in zip(messages, wire):
            if not isinstance(source, AIMessage) or not isinstance(target, dict):
                continue
            reasoning = source.additional_kwargs.get(REASONING_CONTENT_KEY)
            # 空串也要传（字段存在即为真）：DeepSeek 会返回空串，服务端按"字段是否存在"判定。
            # 带 tool_calls 的消息与普通回答消息一视同仁——两种都实测触发过同一个 400。
            if isinstance(reasoning, str):
                target[REASONING_CONTENT_KEY] = reasoning
        return payload


def stream_placeholder_fields(model: Any) -> dict[str, str]:
    """流式回答需要补齐的供应商字段占位（不需要时返回空字典）。

    为什么需要：**真增量流式时框架不会把 ``reasoning_content`` 装进块里**（它只在非流式的响应
    解析路径上被本模块提取），于是流式回答落库后会缺这个字段，下一轮请求被服务端 400 拒绝
    （T23 真实模型冒烟实测：`The reasoning_content in the thinking mode must be passed back`）。

    规则：只有"这个供应商确实用过该字段"（见过一次非流式响应带它）才补一个**空值**占位——
    服务端判定的是"字段是否存在"，空串是它自己也发过的值。没见过就不动，避免给官方 OpenAI
    这类不认识该字段的端点发非标准参数。
    """
    if bool(getattr(model, "_uses_reasoning_content", False)):
        return {REASONING_CONTENT_KEY: ""}
    return {}


def _response_reasoning(response: Any) -> list[str | None]:
    """按 choice 顺序取原始响应里的 ``reasoning_content``。

    返回 ``None`` 表示该响应**没有**这个字段（非思考模型或官方 OpenAI 端点）；
    返回空串表示供应商确实回了空串——两者必须区分，见模块 docstring。
    """
    choices = (
        response.get("choices")
        if isinstance(response, Mapping)
        else getattr(response, "choices", None)
    )
    values: list[str | None] = []
    for choice in choices or []:
        message = (
            choice.get("message")
            if isinstance(choice, Mapping)
            else getattr(choice, "message", None)
        )
        value = _field(message, REASONING_CONTENT_KEY)
        values.append(value if isinstance(value, str) else None)
    return values


def _field(value: Any, key: str) -> Any:
    """兼容取值：``dict``、pydantic v2 的 ``model_extra``（未声明字段）与普通属性。

    openai SDK 的响应对象对未声明字段的处理取决于其 ``model_config``：可能落在
    ``model_extra`` 里，也可能根本不保留。三条路径都试一遍，取不到就当没有这个字段。
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(key)
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, Mapping) and key in extra:
        return extra[key]
    return getattr(value, key, None)

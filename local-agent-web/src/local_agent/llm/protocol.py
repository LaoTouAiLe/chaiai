"""结构化输出与降级链（架构 §11.7）。

降级顺序：

1. 原生 JSON Schema（``with_structured_output(..., method="json_schema")``）；
2. 不支持时用 Prompt 约束 JSON 输出；
3. 用 Pydantic 校验解析结果；
4. 校验失败最多重试一次；
5. 仍然失败则抛 :class:`StructuredOutputError`，不把不完整结果当成有效 State。

错误信息只包含结构名与失败类型，不回显模型原始输出，避免把敏感内容写进日志或页面。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from functools import partial
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, ValidationError

from local_agent.context.token_budget import estimate_messages_tokens
from local_agent.exceptions import LocalAgentError
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.usage_recording import (
    begin_model_call,
    finish_model_call,
    record_model_call,
)

logger = logging.getLogger(__name__)

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

#: Prompt 约束 JSON 时追加的指令。
_PROMPT_JSON_INSTRUCTION = (
    "只输出一个 JSON 对象，不要输出任何解释、Markdown 代码块或额外文字。"
    "JSON 必须符合这个结构：{schema}"
)

_RETRY_INSTRUCTION = (
    "上一次输出无法解析或不符合结构（{error}）。请只输出符合结构的 JSON 对象。"
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class StructuredOutputError(LocalAgentError):
    """结构化输出在降级链全部失败后仍不可用。"""


def call_structured(
    model: BaseChatModel | Runnable[Any, Any],
    messages: Sequence[BaseMessage],
    schema: type[SchemaT],
    *,
    capabilities: ModelCapabilities | None = None,
    max_attempts: int = 2,
    instruction_position: str = "head",
    use_native_schema: bool = True,
    estimated_tools_tokens: int = 0,
    tools_schema_fingerprint: str | None = None,
) -> SchemaT:
    """按降级链取得结构化输出。

    Args:
        model: 模型实例。
        messages: 对话消息（第一条通常是系统指令）。
        schema: 期望的 Pydantic 结构。
        capabilities: 模型能力；``structured_json_schema`` 为真时优先原生 JSON Schema。
        instruction_position: Prompt 约束 JSON 的那条指令放哪——``"head"``（默认，前置）或
            ``"tail"``（追加在消息末尾）。**要与主对话共享前缀的调用必须用 ``"tail"``**：指令放在头部
            会把 ``messages`` 整体往后挤一格，而供应商的前缀缓存是从**第一个 token** 开始比对的，
            于是那之后的全部内容（系统前缀、历史、快照）统统失去缓存资格。
        max_attempts: 总尝试次数（默认 2：首次 + 最多一次重试）。

    Returns:
        校验通过的结构化结果。

    Raises:
        StructuredOutputError: 所有尝试都失败。
    """
    attempts = max(1, max_attempts)
    last_error = "未尝试"

    if use_native_schema and capabilities is not None and capabilities.structured_json_schema:
        try:
            # ``include_raw=True`` 是为了拿到原始消息：结构化调用的返回值是解析后的对象，
            # 用量只在 ``raw`` 上，必须借它记录实际用量（§15.6：所有调用路径都要有实际量）。
            native_factory = getattr(model, "with_structured_output", None)
            if native_factory is None:
                raise NotImplementedError("绑定后的模型不提供原生 schema 接口")
            structured = native_factory(schema, method="json_schema", include_raw=True)
            handle = begin_model_call(estimated_prompt_tokens=estimate_messages_tokens(messages), messages=messages)
            try:
                envelope: Any = structured.invoke(list(messages))
            except BaseException:
                finish_model_call(handle, status="error")
                raise
            raw = envelope.get("raw") if isinstance(envelope, dict) else None
            if handle is not None and raw is not None:
                handle.usage_from(raw)
            finish_model_call(handle, status="ok")
            result = envelope.get("parsed") if isinstance(envelope, dict) else envelope
            if isinstance(result, schema):
                return result
            last_error = "原生 JSON Schema 返回类型不符"
        except Exception as exc:  # noqa: BLE001 — 失败即降级到 Prompt JSON
            last_error = type(exc).__name__
            capabilities.degrade("structured_json_schema", reason=last_error)
            logger.info("原生 JSON Schema 不可用，改用 Prompt JSON：%s", last_error)

    instruction = SystemMessage(
        content=_PROMPT_JSON_INSTRUCTION.format(schema=_describe_schema(schema))
    )

    for attempt in range(attempts):
        # ``tail`` 是为了让调用方**保住自己的前缀**（见 ``instruction_position`` 的说明）：指令追加在
        # 末尾时，前面的消息序列逐字节不变，供应商的前缀缓存才用得上。
        payload = [*messages, instruction] if instruction_position == "tail" else [instruction, *messages]
        if attempt > 0:
            payload = [*payload, SystemMessage(content=_RETRY_INSTRUCTION.format(error=last_error))]
        # `partial` 把本次 payload 固定进可调用对象：不用闭包捕获循环变量（ruff B023）。
        response = record_model_call(
            partial(model.invoke, payload),
            estimated_prompt_tokens=estimate_messages_tokens(payload) + estimated_tools_tokens,
            tools_schema_fingerprint=tools_schema_fingerprint,
            messages=payload,
        )
        try:
            if getattr(response, "tool_calls", None):
                raise ValueError("JSON 答复包含未执行的工具调用")
            return parse_structured(response, schema)
        except (ValueError, ValidationError) as exc:
            last_error = type(exc).__name__
            logger.info("结构化输出第 %d 次解析失败：%s", attempt + 1, last_error)

    raise StructuredOutputError(
        f"结构化输出失败：{schema.__name__}（尝试 {attempts} 次，最后一次失败类型：{last_error}）"
    )


def parse_structured(response: Any, schema: type[SchemaT]) -> SchemaT:
    """从模型回复中解析并校验结构化结果。

    Raises:
        ValueError: 找不到 JSON 对象。
        ValidationError: JSON 不符合结构。
    """
    text = getattr(response, "text", None)
    if not isinstance(text, str):
        text = str(response)
    return schema.model_validate(extract_json(text))


def extract_json(text: str) -> Any:
    """从文本中提取 JSON 对象（容忍 ``` 围栏与前后说明文字）。"""
    candidate = text.strip()
    fenced = _FENCE_PATTERN.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("模型输出中没有 JSON 对象")
    return json.loads(candidate[start : end + 1])


def _describe_schema(schema: type[BaseModel]) -> str:
    """把结构描述成紧凑的字段说明，用于 Prompt 约束。"""
    fields = []
    for name, field in schema.model_fields.items():
        annotation = getattr(field.annotation, "__name__", str(field.annotation))
        fields.append(f'"{name}": {annotation}')
    return "{" + ", ".join(fields) + "}"

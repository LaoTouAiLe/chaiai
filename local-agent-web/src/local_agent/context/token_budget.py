"""上下文预算：估算口径与 State 里记录的结构（架构 §4.2、§11.4；开发文档 T16）。

估算方法：CJK 字符按 1 token、其余字符按 4 字符 1 token，属于保守上界，用于"是否接近
上限"的粗判断，不是精确计数。

预算口径（T16 定稿）：

```text
prompt_budget = context_window_tokens - max_tokens     # max_tokens 是给回答预留的输出额度
usage_ratio   = estimated_prompt_tokens / prompt_budget
usage_ratio >= compression.trigger_ratio 时触发摘要压缩
```

``context_window_tokens`` 是必填配置（``llm.context_window_tokens``，没有默认值），并校验
大于 ``max_tokens``：给一个固定缺省窗口会让"达到多少比例"这句话随模型变化而失真。

``estimated_prompt_tokens`` 必须覆盖**实际出站的完整 Prompt**：系统提示（含 AGENTS、召回
记忆与会话摘要）与本轮送模型的消息窗口。因此压缩节点与出站节点用同一套组装函数，估算口径
不会和真实 Prompt 脱节。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass

#: CJK 及全角标点等按 1 token 估算的码点区间。
_WIDE_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x115F),  # 韩文字母
    (0x2E80, 0x303E),  # CJK 部首与符号
    (0x3041, 0x33FF),  # 平假名、片假名、CJK 兼容
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xAC00, 0xD7A3),  # 韩文音节
    (0xF900, 0xFAFF),  # CJK 兼容表意
    (0xFF00, 0xFF60),  # 全角字符
    (0xFFE0, 0xFFE6),
)

#: 每条消息的固定包装开销（role、分隔符等）的保守近似。
MESSAGE_OVERHEAD_TOKENS = 4

#: 消息内容里**非文本**部分的保守占位估算。
#:
#: 当前链路的工具结果与对话都是文本，用不到它；留这个常量是为了"多模态内容被当成 0 token"——
#: 那会低估出站 Prompt、让压缩晚触发。宁可高估。
NON_TEXT_PART_TOKENS = 1024


def is_wide_char(char: str) -> bool:
    """该字符是否按 1 token 估算（CJK 与全角字符）。"""
    code = ord(char)
    return any(start <= code <= end for start, end in _WIDE_RANGES)


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数量（保守上界）。

    Args:
        text: 待估算文本。

    Returns:
        估算的 token 数；空文本为 0。
    """
    if not text:
        return 0
    wide = sum(1 for char in text if is_wide_char(char))
    narrow = len(text) - wide
    return wide + math.ceil(narrow / 4)


def fits_budget(text: str, budget_tokens: int) -> bool:
    """文本的估算 token 数是否在预算内。

    Args:
        text: 待检查文本。
        budget_tokens: 预算上限，非正数表示无可用预算。

    Returns:
        在预算内返回 True。
    """
    return estimate_tokens(text) <= budget_tokens


@dataclass(frozen=True)
class TokenUsage:
    """估算的 token 用量（架构 §4.2 的 ``token_usage``）。

    ``prompt_tokens`` 是**完整出站 Prompt** 的估算量，等于各分项之和：
    ``system_prompt_tokens + window_messages_tokens + tool_definition_tokens``（工具定义只在
    工具模式下非零，收尾合成不绑定工具时为 0）。分项与总数对不上会让指标失去意义，
    因此加总关系是这里的不变量。

    ``completion_reserve_tokens`` 是给回答预留的额度；它与 ``prompt_tokens`` 相加才是对
    上下文窗口的占用。
    """

    prompt_tokens: int
    system_prompt_tokens: int
    window_messages_tokens: int
    completion_reserve_tokens: int
    #: 绑定给模型的工具定义（名称 + 描述 + 参数 schema）占用的估算量。
    tool_definition_tokens: int = 0


@dataclass(frozen=True)
class ContextBudget:
    """一次 Prompt 构造的预算状态（架构 §4.2 的 ``context_budget``）。

    ``window_messages`` / ``total_messages`` 一起说明"窗口裁掉了多少"；
    ``summary_chars`` 是当前会话摘要的长度（没有摘要时为 0）。
    """

    context_window_tokens: int
    completion_reserve_tokens: int
    prompt_budget_tokens: int
    estimated_prompt_tokens: int
    usage_ratio: float
    trigger_ratio: float
    triggered: bool
    window_messages: int
    total_messages: int
    summary_chars: int
    #: 已被会话摘要覆盖的消息条数：这些消息不再进入 Prompt 窗口（架构 §10.5 第 5 条
    #: "窗口选择器使用摘要替代旧消息"）。它是"摘要覆盖边界"，不是 Checkpoint 的删除标记。
    summary_covered_messages: int = 0


def prompt_budget_tokens(*, context_window_tokens: int, completion_reserve_tokens: int) -> int:
    """输入 Prompt 的可用预算 = 上下文窗口 − 输出预留（不小于 0）。"""
    return max(0, int(context_window_tokens) - int(completion_reserve_tokens))


def usage_ratio(*, estimated_prompt_tokens: int, prompt_budget_tokens: int) -> float:
    """使用比例 ``estimated / budget``。

    预算为 0（或负数）时返回 ``1.0``：视为"已经满了"，触发压缩，而不是抛除零错误或
    返回 0 把超限悄悄藏起来。
    """
    if prompt_budget_tokens <= 0:
        return 1.0
    return estimated_prompt_tokens / prompt_budget_tokens


def estimate_message_tokens(message: object) -> int:
    """估算单条消息的 token（含固定包装开销与**工具调用参数**）。

    用鸭子类型读取 ``content`` 与 ``tool_calls``（不导入 langchain 的消息类）：``str`` 直接估算，
    ``list`` 只累加其中的文本部分，非文本部分按 :data:`NON_TEXT_PART_TOKENS` 保守计入。

    工具调用参数必须计入：``edit_file`` 的整段新内容、``write_file`` 的文件正文都在
    ``tool_calls[].args`` 里，而它们同样是**出站** Prompt 的一部分——漏算会让大参数的工具调用
    被严重低估（复审实测：约 10 万字符的参数只算到 4 token）。
    """
    content = getattr(message, "content", "")
    total = MESSAGE_OVERHEAD_TOKENS + _tool_calls_tokens(message)
    if isinstance(content, str):
        return total + estimate_tokens(content)
    if isinstance(content, Sequence):
        for part in content:
            if isinstance(part, str):
                total += estimate_tokens(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                total += estimate_tokens(part["text"])
            else:
                total += NON_TEXT_PART_TOKENS
    return total


def estimate_messages_tokens(messages: Sequence[object]) -> int:
    """估算一串消息的 token 总量。"""
    return sum(estimate_message_tokens(message) for message in messages)


def estimate_tools_tokens(tools: Sequence[object]) -> int:
    """估算**绑定给模型的工具定义**占用的 token（名称、描述与参数 schema）。

    工具分支每次都会把这些定义发给模型，因此它们属于出站 Prompt 的一部分；不计入会让
    工具分支的预算偏乐观。
    """
    return sum(estimate_tokens(_tool_schema_text(tool)) for tool in tools)


def _tool_calls_tokens(message: object) -> int:
    """消息里工具调用（名称 + 参数）的 token 估算；没有调用时返回 0。"""
    calls = getattr(message, "tool_calls", None)
    if not calls:
        return 0
    try:
        payload = json.dumps(calls, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # 极端情况下退回 repr，宁可略微高估
        payload = repr(calls)
    return estimate_tokens(payload)


def _tool_schema_text(tool: object) -> str:
    """工具定义的文本形式：名称 + 描述 + 参数 JSON Schema。"""
    parts = [str(getattr(tool, "name", "")), str(getattr(tool, "description", ""))]
    schema = getattr(tool, "args_schema", None)
    if schema is not None:
        with suppress(Exception):  # 取不到 schema 时按名称与描述估算，不因工具实现报错
            parts.append(json.dumps(schema.model_json_schema(), ensure_ascii=False, default=str))
    return "\n".join(parts)

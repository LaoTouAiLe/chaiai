"""上下文预算的粗略估算（架构 §11.4）。

当前只提供估算工具，不写入 State：``ContextBudget`` 的结构要等 T16（Prompt 窗口与
摘要压缩）明确后再定，避免臆造字段。

估算方法：CJK 字符按 1 token、其余字符按 4 字符 1 token，属于保守上界，
用于“是否接近上限”的粗判断，不是精确计数。
"""

from __future__ import annotations

import math

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

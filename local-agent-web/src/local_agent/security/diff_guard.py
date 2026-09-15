"""diff 预览与变更范围校验（架构 §6.3、§11.2）。

T06 只需要"生成预览 + 统计变更行数"，用于免审上限判定与反思阶段的机械校验；
T22 会在这里加 Patch 输入模式、冲突检测与 hunk 校验。
"""

from __future__ import annotations

import difflib

DEFAULT_CONTEXT_LINES = 3


def build_unified_diff(
    old_text: str,
    new_text: str,
    *,
    path: str = "file",
    context_lines: int = DEFAULT_CONTEXT_LINES,
) -> str:
    """生成 unified diff 文本（用户看到的审计产物统一用它）。

    Args:
        old_text: 修改前的内容。
        new_text: 修改后的内容。
        path: 展示用的文件名，出现在 diff 头。
        context_lines: 上下文行数。

    Returns:
        unified diff 文本；没有差异时为空串。换行统一为 ``\\n`` 便于展示。
    """
    diff_lines = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context_lines,
    )
    text = "".join(line if line.endswith("\n") else f"{line}\n" for line in diff_lines)
    return text.replace("\r\n", "\n")


def count_changed_lines(old_text: str, new_text: str) -> int:
    """变更行数 = 新增行数 + 删除行数（不含上下文行）。

    用于判断一次修改是否需要人工审批（架构 §6.3、§8 的 ``security.large_edit``）。
    """
    matcher = difflib.SequenceMatcher(
        None, old_text.splitlines(), new_text.splitlines(), autojunk=False
    )
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed += (i2 - i1) + (j2 - j1)
    return changed


def diff_is_empty(old_text: str, new_text: str) -> bool:
    """新旧内容是否完全一致。"""
    return old_text == new_text

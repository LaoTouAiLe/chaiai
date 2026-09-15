"""审批控件（架构 §5.3、§6.5）：批准 / 修改参数 / 拒绝。

职责边界：只负责把已算好的审批请求渲染成控件并返回人工决策，
不做安全判断、不直接改图状态（由 ``web.py`` 用 ``Command(resume=...)`` 恢复）。

递归删除的二次确认在这里体现：第二阶段单独渲染，且必须有显式勾选才算确认。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import streamlit as st


@dataclass(frozen=True)
class ApprovalInput:
    """人工决策（会作为 ``Command(resume=...)`` 的载荷）。"""

    decision: str
    confirm_recursive: bool = False
    edited_arguments: dict[str, Any] | None = None
    reason: str = ""

    def as_payload(self) -> dict[str, Any]:
        """转成传给 ``Command(resume=...)`` 的字典。"""
        return {
            "decision": self.decision,
            "confirm_recursive": self.confirm_recursive,
            "edited_arguments": self.edited_arguments,
            "reason": self.reason,
        }


def render_approval(
    request: Mapping[str, Any], *, key_prefix: str = "approval"
) -> ApprovalInput | None:
    """渲染审批卡片；用户点击按钮时返回决策，否则返回 ``None``。

    Args:
        request: 审批请求载荷（``interrupt()`` 的 value）。
        key_prefix: 控件 key 前缀，避免同一页面内多个控件冲突。

    Returns:
        人工决策，或 ``None`` 表示还没点。
    """
    stage = int(request.get("confirmation_stage") or 1)
    tool_name = str(request.get("tool_name") or "")
    summary = str(request.get("impact_summary") or "")

    st.warning(f"需要你确认的动作（{tool_name}）：{summary}")
    if stage == 2:
        st.error(
            "这是**递归删除的二次确认**：第一次确认不构成授权，"
            "只有在这里再次确认才会真正执行；刷新或取消都必须重新确认。"
        )

    with st.expander("查看完整参数与指纹", expanded=stage == 1):
        st.json(request.get("arguments") or {})
        st.caption(f"动作指纹：{request.get('action_fingerprint')}")

    edited_text = st.text_area(
        "修改后的参数（JSON，仅“修改参数”时使用）",
        value=json.dumps(request.get("arguments") or {}, ensure_ascii=False, indent=2),
        key=f"{key_prefix}-args",
        height=140,
    )

    approve_label = "确认删除" if stage == 2 else "批准执行"
    confirm_recursive = False
    if stage == 2:
        confirm_recursive = st.checkbox(
            "我确认删除该目录及其全部内容", key=f"{key_prefix}-recursive"
        )

    col_approve, col_edit, col_reject = st.columns(3)
    if col_approve.button(
        approve_label, key=f"{key_prefix}-approve", disabled=stage == 2 and not confirm_recursive
    ):
        return ApprovalInput(decision="approve", confirm_recursive=confirm_recursive)
    if col_reject.button("拒绝", key=f"{key_prefix}-reject"):
        return ApprovalInput(decision="reject", reason="用户拒绝了该动作")
    if col_edit.button("修改参数", key=f"{key_prefix}-edit"):
        parsed = _parse_arguments(edited_text)
        if parsed is None:
            st.error("参数不是合法 JSON 对象，无法提交修改")
            return None
        return ApprovalInput(
            decision="edit", edited_arguments=parsed, reason="用户修改了动作参数"
        )
    return None


def _parse_arguments(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None

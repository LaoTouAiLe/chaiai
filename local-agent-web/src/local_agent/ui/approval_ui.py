"""审批控件（架构 §5.3、§6.5）：批准 / 拒绝。

职责边界：只把应用服务（T28）给出的 `ApprovalView` DTO 渲染成控件并返回人工决定，
不做安全判断、也不直接改图状态（由 ``web.py`` 调 ``ApplicationService.resolve_approval`` 恢复）。
因此本模块**不导入** LangGraph、`Command` 或 Graph 的 State。

递归删除的二次确认：第一阶段只推进到第二阶段；第二阶段要求用户显式勾选，
并把 ``confirm_recursive=True`` 交给服务。每阶段独立提交，由图判断授权。

关于 `edit`：应用服务支持"改写参数后批准"，但 DTO 只带**已脱敏的展示字符串**
（`redacted_arguments`），拿它当参数提交会把展示文本变成真实参数。因此页面不提供这一入口，
与前端一致（该路径由 API 契约测试覆盖）。
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from local_agent.application.models import ApprovalView

#: `ApprovalState` → 展示文字。
_STATE_LABELS = {
    "pending": "等待决策",
    "executing": "已批准，正在执行",
    "approved": "已批准",
    "rejected": "已拒绝",
    "expired": "已失效",
}


@dataclass(frozen=True)
class ApprovalInput:
    """人工决定（交给 ``ApplicationService.resolve_approval`` 的字段）。"""

    decision: str
    confirm_recursive: bool = False
    reason: str = ""


def render_approval(approval: ApprovalView, *, key_prefix: str = "approval") -> ApprovalInput | None:
    """渲染审批卡片；用户点击按钮时返回决定，否则返回 ``None``。

    Args:
        approval: 快照里的审批视图（`SessionSnapshot.approval`）。
        key_prefix: 控件 key 前缀，避免同一页面内多个控件冲突。

    Returns:
        人工决定，或 ``None`` 表示还没点、或该审批已经不可操作。
    """
    if approval.state != "pending":
        # 已经不是待决状态：只如实展示结论，不给按钮（结论来自服务，页面不推断）。
        label = _STATE_LABELS.get(approval.state, approval.state)
        note = approval.note or ""
        st.info(f"这次审批当前状态：{label}。{note}".strip())
        return None

    st.warning(f"需要你确认的动作（{approval.tool_name}）：{approval.impact_summary}")
    if approval.outside_workspace:
        st.error("目标位于工作区外：请核对完整路径。")
    high_risk = approval.risk == "high"
    second_stage = high_risk and approval.confirmation_stage == 2
    if high_risk:
        st.error(
            "第二次确认：请再次核对目标和影响范围。"
            if second_stage else "第一次确认只会进入二次确认，不会执行删除。"
        )

    with st.expander("查看动作参数与指纹", expanded=True):
        if approval.target:
            st.caption(f"目标：{approval.target}")
        st.json(approval.redacted_arguments or {})
        st.caption(f"动作指纹：{approval.action_fingerprint}")

    confirm_recursive = False
    if second_stage:
        confirm_recursive = st.checkbox(
            "我确认删除该目录及其全部内容", key=f"{key_prefix}-{approval.approval_id}-stage-2-recursive"
        )

    col_approve, col_reject = st.columns(2)
    approve_label = "确认删除" if second_stage else "进入二次确认" if high_risk else "允许一次"
    approve_key = f"{key_prefix}-stage-{approval.confirmation_stage}-approve" if high_risk else f"{key_prefix}-approve"
    if col_approve.button(
        approve_label, key=approve_key,
        disabled=second_stage and not confirm_recursive,
    ):
        return ApprovalInput(decision="approve", confirm_recursive=confirm_recursive)
    if col_reject.button("拒绝", key=f"{key_prefix}-reject"):
        return ApprovalInput(decision="reject", reason="用户拒绝了该动作")
    st.caption("批准只对本次调用生效；不会加入免审名单。")
    return None

"""无人值守会话的审批语义（§21.4，2026-10-22 J1）。

核心断言只有一条：**这类会话遇到需审批动作时不会挂起**，而是当场拒绝并留下可审计的记录。
交互式会话的行为由既有的 148 条审批用例守住，这里不重复。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from local_agent.graph.nodes import approval_interrupt_node


def _request() -> dict[str, object]:
    return {
        "approval_id": "a1",
        "run_id": "r1",
        "tool_call_id": "c1",
        "tool_name": "delete_file",
        "action_fingerprint": "fp-1",
        "created_at": "2026-10-22T00:00:00+00:00",
        "arguments": {"path": "notes.txt"},
        # 二阶确认也要被跳过：不能因为"需要二次确认"就退化成等待。
        "requires_second_confirmation": True,
    }


def _state(kind: str) -> dict[str, object]:
    return {
        "session_kind": kind,
        "run_id": "r1",
        "pending_approval": _request(),
        "pending_tool_calls": [],
        "approval_history": [],
        "decision_trace": [],
    }


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(security_policy=None))


def test_scheduled_session_rejects_without_waiting() -> None:
    update = approval_interrupt_node(_state("scheduled"), _runtime())

    assert update["pending_approval"] is None
    history = update["approval_history"]
    assert history[-1]["decision"] == "reject"
    assert "无人值守" in history[-1]["reason"]
    # 审计上必须分得清"系统拒的"
    summaries = [str(record["summary"]) for record in update["decision_trace"]]
    assert any("无人值守拒绝" in text for text in summaries)
    assert not any("人工决策" in text for text in summaries)


def test_rejected_call_still_gets_a_tool_message() -> None:
    """被拒的调用必须有成对 ToolMessage：否则下一轮发给端点会 400（T37 实证）。"""
    update = approval_interrupt_node(_state("heartbeat"), _runtime())

    messages = [item for item in update.get("messages", []) if isinstance(item, ToolMessage)]
    assert len(messages) == 1
    assert messages[0].tool_call_id == "c1"
    assert "未执行" in str(messages[0].content)


def test_no_second_confirmation_wait_for_unattended() -> None:
    """请求带 `requires_second_confirmation` 也不该走第二次中断。

    这里直接断言"一次调用就结束"：如果代码试图 interrupt，测试会因缺少图上下文而报错。
    """
    update = approval_interrupt_node(_state("subagent"), _runtime())
    assert update["pending_approval"] is None
    assert update["approval_history"][-1]["decision"] == "reject"


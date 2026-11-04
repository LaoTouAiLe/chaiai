"""委派实时进度表的边界（2026-11-04）。

这个模块的输入来自图 State，而 State 里的工具事件是 **dataclass 列表**——
第一版只认 `dict`，`steps` 永远是空，页面上那几行只显示"进行中…"，一个工具调用都不滚。
"""

from __future__ import annotations

from local_agent import delegation_live
from local_agent.graph.state import ToolEvent


def test_sync_steps_accepts_tool_event_dataclasses() -> None:
    delegation_live.reset()
    delegation_live.start_lane("child-1", "src/local_agent/tools")
    delegation_live.sync_steps(
        "child-1",
        [
            ToolEvent(
                tool_call_id="t1",
                tool_name="read_file",
                redacted_arguments={"path": "src/local_agent/tools/delegate_tools.py"},
                status="ok",
            ),
            ToolEvent(
                tool_call_id="t2",
                tool_name="grep",
                redacted_arguments={"pattern": "def build"},
                status="running",
            ),
        ],
    )

    lanes = delegation_live.snapshot()
    assert len(lanes) == 1
    steps = lanes[0].steps
    assert [step.name for step in steps] == ["read_file", "grep"]
    # 目标优先取路径，其次模式串——都是"用户能一眼认出在查哪"的东西。
    assert steps[0].target == "src/local_agent/tools/delegate_tools.py"
    assert steps[1].target == "def build"


def test_sync_steps_is_idempotent_and_tolerates_odd_input() -> None:
    delegation_live.reset()
    delegation_live.start_lane("child-2", "docs")
    event = ToolEvent(tool_call_id="t1", tool_name="read_file", status="ok")
    # 同一个完整列表同步两次，不该出现重复行（页面按整体重建，不做追加）。
    delegation_live.sync_steps("child-2", [event])
    delegation_live.sync_steps("child-2", [event])
    assert len(delegation_live.snapshot()[0].steps) == 1

    # 根本不是列表：完全忽略，保留上一次的视图。
    delegation_live.sync_steps("child-2", None)
    assert len(delegation_live.snapshot()[0].steps) == 1
    # 是列表但元素都认不出：按"这一路现在没有可见步骤"重建（整体重建语义，不是追加）。
    delegation_live.sync_steps("child-2", ["not-an-event"])
    assert len(delegation_live.snapshot()[0].steps) == 0


def test_finish_lane_keeps_the_lane_readable() -> None:
    """跑完不立刻删：客户端可能晚一两秒才停轮询，那时还要能看到最终状态。"""
    delegation_live.reset()
    delegation_live.start_lane("child-3", "src")
    delegation_live.finish_lane("child-3", "completed")

    lanes = delegation_live.snapshot()
    assert len(lanes) == 1
    assert lanes[0].status == "completed"

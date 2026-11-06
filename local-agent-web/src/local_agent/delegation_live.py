"""正在跑的委派子任务的实时进度（2026-11-04）。

**为什么需要单独一个模块**：委派在独立线程里跑，父图的 state 只在节点返回时才更新一次。
所以"每一路此刻正在调什么工具"在跑的过程中**不在任何可读的地方**——页面只能等全部结束。

子循环里 `graph.stream(..., stream_mode="values")` 每完成一个节点就产出一份完整 state，
本模块就在那里把 `tool_events` 摊成可读的步骤列表，供 `GET /api/delegation/live` 查询。

**这不是事件通道**：不新增 SSE 事件类型、不动 `events.py` 契约、不碰"快照是权威"那条线。
前端只在委派卡还在跑的时候轮询这个只读端点，跑完就不再请求。
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: 进度记录保留多久（秒）。
#:
#: **30 分钟**（2026-11-04）：它不只是"客户端晚几秒停轮询"的缓冲，更是**完成之后的回看来源**。
#: 子任务的工具事件目前没有并进 Checkpoint（`delegation_audit` 那条链没打通），所以快照里
#: 每路的 `tools` 是空的，投影层要到这里回填。服务重启后这些记录消失——那时历史会话只剩统计，
#: 这一点在页面上如实体现（没有链路就显示"没有记录到工具调用"）。
RETENTION_SECONDS: float = 1800.0

#: 目标字段的优先级：先看路径，再看命令，最后看模式串。都是"用户能一眼认出在查哪"的东西。
_TARGET_KEYS: tuple[str, ...] = ("path", "command", "pattern", "glob", "query", "url")


@dataclass(frozen=True)
class LiveStep:
    """某一路已经发起过的一次工具调用。"""

    name: str
    target: str
    status: str


@dataclass
class LiveLane:
    """一路子任务的当前状态。"""

    child_id: str
    scope: str
    #: 这一路要查的问题（截断后的原文）。
    #:
    #: 抄 Hermes / Codex 的取舍：块头显示**"要做什么"**而不是"在哪个目录"——
    #: `src/local_agent/mobile` 对用户没有信息量，`这目录有几个文件、各自职责`才有。
    #: 它只在实时表里（不进 Checkpoint），过期后降级成只显示目录。
    goal: str = ""
    status: str = "running"
    steps: list[LiveStep] = field(default_factory=list)
    updated_at: float = 0.0


_LOCK = threading.Lock()
_LANES: dict[str, LiveLane] = {}


def _target_of(event: dict[str, Any]) -> str:
    """从工具事件里挑一个"在查哪"的可读字段。挑不到就空着，**不编造**。"""
    arguments = event.get("redacted_arguments")
    if not isinstance(arguments, dict):
        return ""
    for key in _TARGET_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def _step_of(event: dict[str, Any]) -> LiveStep:
    name = str(event.get("tool_name") or "")
    status = str(event.get("status") or "")
    return LiveStep(name=name, target=_target_of(event), status=status)


def start_lane(child_id: str, scope: str, goal: str = "") -> None:
    """登记一路子任务开始跑。同一个 id 重复调用是幂等的。"""
    with _LOCK:
        _LANES[child_id] = LiveLane(
            child_id=child_id, scope=scope, goal=goal[:200], updated_at=time.time()
        )


def sync_steps(child_id: str, events: Any) -> None:
    """用子任务**当前完整的**工具事件列表重建这一路的步骤。

    每次都整体重建而不是追加：`state["tool_events"]` 本身是累积列表，
    重建天然幂等，也不会出现"同一次调用被记两遍"这种只有并发下才复现的错。

    **事件是 dataclass，不是 dict**（2026-11-04 实测）：第一版只认 `dict`，
    结果 `steps` 永远是空——页面上那几行只显示"进行中…"，一个工具调用都不滚。
    """
    if not isinstance(events, list):
        return
    steps: list[LiveStep] = []
    for event in events:
        if isinstance(event, Mapping):
            steps.append(_step_of(dict(event)))
        elif dataclasses.is_dataclass(event) and not isinstance(event, type):
            steps.append(_step_of(dataclasses.asdict(event)))
    with _LOCK:
        lane = _LANES.get(child_id)
        if lane is None:
            return
        lane.steps = steps
        lane.updated_at = time.time()


def finish_lane(child_id: str, status: str) -> None:
    """标记这一路跑完（``completed`` / ``failed``）。记录保留一会儿再清，见 `RETENTION_SECONDS`。"""
    with _LOCK:
        lane = _LANES.get(child_id)
        if lane is None:
            return
        lane.status = status
        lane.updated_at = time.time()


def snapshot() -> list[LiveLane]:
    """当前所有在跑的委派路（含刚跑完、还在保留期内的）。"""
    cutoff = time.time() - RETENTION_SECONDS
    with _LOCK:
        for child_id in [key for key, lane in _LANES.items() if lane.updated_at < cutoff]:
            _LANES.pop(child_id, None)
        return [LiveLane(child_id=lane.child_id, scope=lane.scope, goal=lane.goal,
                         status=lane.status, steps=list(lane.steps), updated_at=lane.updated_at)
                for lane in _LANES.values()]


def lanes_for(scopes: Any) -> dict[str, LiveLane]:
    """按目录取最近一次记录的整条 lane（步骤 + goal），供投影层回填。

    同一个目录在两次不同委派里出现时，取**最近更新**的那次——用户刚跑完就看，看到的一定是
    刚才那次；而历史更早的同名目录本就无法从统计里区分，不假装能区分。
    """
    wanted = {str(scope) for scope in scopes} if isinstance(scopes, (list, tuple, set)) else set()
    if not wanted:
        return {}
    cutoff = time.time() - RETENTION_SECONDS
    with _LOCK:
        latest: dict[str, LiveLane] = {}
        for lane in _LANES.values():
            if lane.updated_at < cutoff or lane.scope not in wanted:
                continue
            current = latest.get(lane.scope)
            if current is None or lane.updated_at > current.updated_at:
                latest[lane.scope] = lane
        return {
            scope: LiveLane(child_id=lane.child_id, scope=lane.scope, goal=lane.goal,
                            status=lane.status, steps=list(lane.steps), updated_at=lane.updated_at)
            for scope, lane in latest.items()
        }


def reset() -> None:
    """清空全部（测试用）。"""
    with _LOCK:
        _LANES.clear()


__all__ = [
    "RETENTION_SECONDS",
    "LiveLane",
    "LiveStep",
    "finish_lane",
    "lanes_for",
    "reset",
    "snapshot",
    "start_lane",
    "sync_steps",
]

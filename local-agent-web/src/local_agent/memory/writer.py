"""按落库计划执行记忆写入（架构 §10.4，开发文档 T18）。

本模块只负责"执行"：把 :class:`~local_agent.reflection.memory_candidates.WriteIntent` 落到中期
向量库或长期 SQLite。候选的校验与映射在 :mod:`local_agent.reflection.memory_candidates`。

三条不变式：

- **不新增 State 字段**：结果以 :class:`WriteOutcome` 返回，由 ``memory_write_node`` 折算成
  ``decision_trace`` / ``error_events``（架构 §4.2 的字段集合不变）。
- **按来源事件幂等**：同一条候选的 ``source_event_ids`` 只写一次。中期记录与长期任务结论都用
  **稳定 id**（中期是同 id 覆盖，结论是 ``INSERT OR IGNORE``），长期事实沿用 T15 的"同键同值即
  ``duplicate``"。稳定 id 让"数据库已写、Checkpoint 提交前重放"变成幂等重试——只靠 State 判重
  挡不住这种情况（复审探针：同一来源的结论被写了两条）。
- **未启用的层按透传处理**：对应 store 为 ``None`` 时跳过并给出原因，不抛异常——架构 §10 允许
  任一层单独关闭，记忆写不进去不该拖垮本轮对话。
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from local_agent.graph.state import MemoryCandidate
from local_agent.memory.long_term import LongTermStore
from local_agent.memory.medium_term import MediumTermStore
from local_agent.memory.schemas import MEDIUM_TERM_STATUS_ACTIVE, MediumTermRecord
from local_agent.reflection.memory_candidates import WriteIntent, WriteTarget
from local_agent.scope import SCOPE_PROJECT, SCOPE_USER, stable_hash

#: 长期事实的 ``source`` 列取值（T15 的字段，标明这条事实来自反思写入链路）。
WRITE_SOURCE = "reflection"

#: 中期记录 id 的前缀。
_MEDIUM_ID_PREFIX = "mt"

#: 长期任务结论 id 的前缀（稳定 id，见 :func:`conclusion_memory_id`）。
_CONCLUSION_ID_PREFIX = "conclusion"

#: 一次落库动作的结果类别。
#:
#: - ``written``：真的写进去了（事实冲突时也归这里——按语义保留当前值并记 ``conflicted`` 行）；
#: - ``duplicate``：同一来源已经写过（幂等跳过，**不算失败**）；
#: - ``disabled``：目标层未启用，或该目标对这种候选不适用（**不算失败**）；
#: - ``conflict``：与现有事实冲突，保留当前值；
#: - ``failed``：真实失败（异常、缺少载荷）——调用方据此记错误事件。
WriteOutcomeKind = Literal["written", "duplicate", "disabled", "conflict", "failed"]


@dataclass(frozen=True, kw_only=True)
class WriteOutcome:
    """一次落库动作的结果。"""

    target: WriteTarget
    memory_type: str
    written: bool
    kind: WriteOutcomeKind = "written"
    #: 未写入或被降级时的原因（写进决策轨迹，不能静默丢弃）。
    reason: str | None = None
    memory_id: str | None = None

    @property
    def satisfied(self) -> bool:
        """该目标是"已经满足"（真写入 / 同来源已写过 / 该层未启用），而不是"失败"。

        ``memory_write_node`` 用它判定候选能否登记为完成：只有一个候选的**所有**目标都 satisfied
        才登记。否则 ``task_fragment`` 的中期写成功、长期写失败时会把候选永久标成已完成，
        长期结论再也补不上（复审实证）。
        """
        return self.kind != "failed"


def write_intents(
    intents: Sequence[WriteIntent],
    *,
    seen_keys: Collection[str] = (),
    failed_layers: Collection[str] = (),
    medium_store: MediumTermStore | None = None,
    long_store: LongTermStore | None = None,
    source_thread_id: str | None = None,
    now: datetime | None = None,
) -> list[WriteOutcome]:
    """执行落库计划。

    Args:
        intents: :func:`~local_agent.reflection.memory_candidates.plan_writes` 产出的落库动作。
        seen_keys: 已经写完的**候选判重键**（:func:`candidate_write_key`），用于跳过重复写入。
            键里含候选身份，不是只看来源事件——同一事件下的其它候选不会被误伤。
        failed_layers: **配置已启用但打不开**的层（``"medium"`` / ``"long"``）。这些层上的目标算
            ``failed`` 而不是"未启用"：把"打不开"当成 ``disabled`` 会让零写入的候选也登记完成。
        medium_store: 已打开的中期库；``None`` 表示该层不可用（未启用或打开失败）。
        long_store: 已打开的长期库；``None`` 表示该层不可用（未启用或打开失败）。
        source_thread_id: 来源会话，写进中期记录的元数据（架构 §10.2）。
        now: 当前时间，默认取 UTC 现在；测试可注入。

    Returns:
        与 ``intents`` 等长的结果列表；写入失败不会抛异常，而是记入 ``reason``。
    """
    outcomes: list[WriteOutcome] = []
    # 判重只看**调用方传入的**已写候选，本批内不互相阻挡：同一条 ``task_fragment`` 候选会同时
    # 产生"中期片段"和"长期结论"两个动作，它们在同一个来源事件下本来就该都写进去。
    blocked = {str(key) for key in seen_keys}
    unusable = {str(layer) for layer in failed_layers}
    for intent in intents:
        if candidate_write_key(intent.candidate) in blocked:
            outcomes.append(
                _skipped(intent, "这条候选已写过记忆，跳过重复写入", kind="duplicate")
            )
            continue
        try:
            outcome = _apply(
                intent,
                medium_store=medium_store,
                long_store=long_store,
                failed_layers=unusable,
                source_thread_id=source_thread_id,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 —— 记忆写入失败不能拖垮本轮对话
            outcome = _skipped(
                intent, f"写入失败：{type(exc).__name__}: {exc}", kind="failed"
            )
        outcomes.append(outcome)
    return outcomes


def candidate_write_key(candidate: MemoryCandidate) -> str:
    """候选的写入判重键：**候选身份 + 来源事件**（不是只看来源事件）。

    只看来源事件会误伤同一事件下的其它候选：同一次工具调用可能同时产生候选 A、B，A 成功登记后，
    重试 B 会因为"来源已写过"直接变成 ``duplicate``，永远补不上（复审实证）。键里含类型、主题与
    内容，只有**同一条候选**才判重；真正的幂等仍由中期稳定 id、长期结论稳定 id 与
    事实的"同键同值"语义兜底。
    """
    payload = json.dumps(
        {
            "type": candidate.memory_type,
            "subject": candidate.subject.strip(),
            "content": candidate.content,
            "sources": sorted(str(item) for item in candidate.source_event_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return stable_hash(payload)


def medium_memory_id(intent: WriteIntent) -> str:
    """中期记录的**稳定 id**：同一候选（同作用域、同主题、同内容、同来源）重复写入得到同一个 id。

    用稳定 id 而不是随机 id，是因为 Chroma 的 ``upsert`` 按 id 覆盖——重复写入会变成幂等更新，
    而不是留下两条几乎相同的记忆。``subject``（任务标题）必须进载荷：判重键 :func:`candidate_write_key`
    已经含它，漏掉会让"同来源、同内容、不同标题"的两条候选算出**同一个 id**——两条都报告写入成功，
    中期库里却只剩一条（复审实证）。
    """
    payload = json.dumps(
        {
            "type": intent.candidate.memory_type,
            "subject": intent.candidate.subject.strip(),
            "scope": f"{intent.scope}:{intent.scope_key}",
            "content": intent.candidate.content,
            "sources": sorted(str(item) for item in intent.candidate.source_event_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"{_MEDIUM_ID_PREFIX}-{stable_hash(payload)}"


def conclusion_memory_id(intent: WriteIntent) -> str:
    """长期任务结论的**稳定 id**：同项目、同来源、同内容重复写入得到同一个 id。

    ``task_conclusions`` 没有版本与状态字段，写入即新增一行；稳定 id + ``INSERT OR IGNORE``
    让"数据库写成功、Checkpoint 提交前重放"等价于一次幂等重试，而不是多出一条记忆。
    """
    payload = json.dumps(
        {
            "kind": "task_conclusion",
            "scope": f"{intent.scope}:{intent.scope_key}",
            # 任务标题也进载荷：同内容、不同标题的两条结论是**两条合法记忆**，漏掉它会让
            # 第二条被 INSERT OR IGNORE 丢掉（复审探针：T18/已完成 与 T19/已完成 撞同一个 id）。
            "task_title": intent.candidate.subject.strip(),
            "content": intent.candidate.content,
            "sources": sorted(str(item) for item in intent.candidate.source_event_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"{_CONCLUSION_ID_PREFIX}-{stable_hash(payload)}"


def _apply(
    intent: WriteIntent,
    *,
    medium_store: MediumTermStore | None,
    long_store: LongTermStore | None,
    failed_layers: Collection[str],
    source_thread_id: str | None,
    now: datetime | None,
) -> WriteOutcome:
    """按目标分派到具体写入路径；store 为 ``None`` 时区分"未启用"与"启用但打不开"。"""
    if intent.target == "medium_term":
        if medium_store is None:
            return _unavailable(intent, layer="medium", failed_layers=failed_layers)
        return _write_medium(
            intent, store=medium_store, source_thread_id=source_thread_id, now=now
        )
    if long_store is None:
        return _unavailable(intent, layer="long", failed_layers=failed_layers)
    if intent.target == "long_term_fact":
        return _write_fact(intent, store=long_store, now=now)
    return _write_conclusion(intent, store=long_store, now=now)


def _unavailable(
    intent: WriteIntent, *, layer: str, failed_layers: Collection[str]
) -> WriteOutcome:
    """该层没有可用 store：**启用但打不开**算失败，配置关闭才算"未启用"。

    两者混为一谈的后果很实际：长期库打不开时目标被当成 ``disabled``（satisfied），零写入的候选
    照样登记完成（复审实证）。
    """
    name = "中期记忆" if layer == "medium" else "长期记忆"
    config_key = "memory.medium_term.enabled" if layer == "medium" else "memory.long_term.enabled"
    if layer in failed_layers:
        return _skipped(
            intent, f"{name}已启用但当前不可用（打开失败），本轮跳过写入", kind="failed"
        )
    return _skipped(intent, f"{name}未启用（{config_key}=false）", kind="disabled")


def _write_medium(
    intent: WriteIntent,
    *,
    store: MediumTermStore | None,
    source_thread_id: str | None,
    now: datetime | None,
) -> WriteOutcome:
    """写入中期向量库（跨会话召回用；与 T16 的 State 会话摘要不是同一份数据）。"""
    if store is None:
        return _skipped(
            intent, "中期记忆未启用（memory.medium_term.enabled=false）", kind="disabled"
        )
    candidate = intent.candidate
    record = MediumTermRecord(
        id=medium_memory_id(intent),
        content=candidate.content,
        scope=intent.scope,
        scope_key=intent.scope_key,
        user_id=intent.scope_key if intent.scope == SCOPE_USER else None,
        project_id=intent.scope_key if intent.scope == SCOPE_PROJECT else None,
        source_thread_id=source_thread_id,
        source_event_id=(
            str(candidate.source_event_ids[0]) if candidate.source_event_ids else None
        ),
        importance=float(candidate.importance),
        confidence=float(candidate.confidence),
        created_at=_now_iso(now),
        expires_at=_timestamp(candidate.expires_at),
        status=MEDIUM_TERM_STATUS_ACTIVE,
    )
    store.upsert([record])
    return WriteOutcome(
        target=intent.target,
        memory_type=candidate.memory_type,
        written=True,
        memory_id=record.id,
    )


def _write_fact(intent: WriteIntent, *, store: LongTermStore, now: datetime | None) -> WriteOutcome:
    """写入长期事实：先复用/创建实体身份，再按 ``key`` upsert 值。"""
    payload = intent.payload
    if payload is None:  # plan_writes 已保证非 None，这里只做防御
        return _skipped(intent, "entity_fact 缺少解析后的载荷", kind="failed")
    candidate = intent.candidate
    entity = store.record_entity(
        entity_type=payload.entity_type,
        name=candidate.subject,
        scope=intent.scope,
        user_id=intent.scope_key if intent.scope == SCOPE_USER else None,
        project_id=intent.scope_key if intent.scope == SCOPE_PROJECT else None,
        now=now,
    )
    result = store.upsert_fact(
        entity_id=entity.id,
        key=payload.key,
        value=payload.value,
        confidence=float(candidate.confidence),
        importance=float(candidate.importance),
        source=WRITE_SOURCE,
        expires_at=_timestamp(candidate.expires_at),
        now=now,
    )
    kind: WriteOutcomeKind = "written"
    # 计划阶段的说明（例如"实体名由系统推导"）作为默认写入说明；冲突时以冲突说明为准。
    reason = intent.note
    if result.outcome == "conflicted":
        kind = "conflict"
        kept = result.previous.id if result.previous is not None else "现有值"
        reason = f"与现有事实冲突：保留当前值（{kept}），新说法记为 conflicted"
    return WriteOutcome(
        target=intent.target,
        memory_type=candidate.memory_type,
        written=True,
        kind=kind,
        memory_id=result.record.id,
        reason=reason,
    )


def _write_conclusion(
    intent: WriteIntent, *, store: LongTermStore, now: datetime | None
) -> WriteOutcome:
    """写入长期任务结论（该表没有版本与状态字段，写入即长期有效）。

    用 :func:`conclusion_memory_id` 的**稳定 id** 幂等写入：同一来源重放不会多出一条结论。
    """
    if intent.scope != SCOPE_PROJECT:
        return _skipped(
            intent,
            "任务结论只按项目作用域召回，非 project 作用域的候选不写长期结论",
            kind="disabled",
        )
    candidate = intent.candidate
    record, inserted = store.upsert_task_conclusion(
        conclusion_id=conclusion_memory_id(intent),
        task_title=candidate.subject.strip() or "未命名任务",
        conclusion=candidate.content,
        project_scope=intent.scope_key,
        confidence=float(candidate.confidence),
        evidence=[
            {"source_event_id": str(event_id)} for event_id in candidate.source_event_ids
        ],
        now=now,
    )
    if not inserted:
        return WriteOutcome(
            target=intent.target,
            memory_type=candidate.memory_type,
            written=False,
            kind="duplicate",
            memory_id=record.id,
            reason="同一项目与来源的结论已存在，按稳定 id 幂等跳过",
        )
    return WriteOutcome(
        target=intent.target,
        memory_type=candidate.memory_type,
        written=True,
        memory_id=record.id,
    )


def _skipped(intent: WriteIntent, reason: str, *, kind: WriteOutcomeKind = "disabled") -> WriteOutcome:
    """跳过或失败的统一构造；``kind`` 决定调用方是记错误事件还是只记决策轨迹。"""
    return WriteOutcome(
        target=intent.target,
        memory_type=intent.candidate.memory_type,
        written=False,
        kind=kind,
        reason=reason,
    )


def _timestamp(moment: datetime | None) -> str | None:
    """把时间点格式化成字符串；``None`` 保持不变（可选字段无值时不写）。"""
    if moment is None:
        return None
    return _iso(moment)


def _now_iso(moment: datetime | None) -> str:
    """必填时间列：``None`` 时取当前 UTC 时间。"""
    return _iso(moment or datetime.now(timezone.utc))


def _iso(moment: datetime) -> str:
    """补齐时区后输出 ISO-8601（库里统一带时区，便于比较与排序）。"""
    value = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return value.isoformat()


__all__ = [
    "WRITE_SOURCE",
    "WriteOutcome",
    "WriteOutcomeKind",
    "candidate_write_key",
    "conclusion_memory_id",
    "medium_memory_id",
    "write_intents",
]

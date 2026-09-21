"""中期与长期记忆召回与打分（架构 §10.2–§10.3，开发文档 T14B、T15）。

中期（向量）召回顺序（架构 §10.2 末段）：

1. **作用域精确过滤**：只用本次运行可见的 ``(scope, scope_key)`` 组合查询
   （``global`` / 逻辑用户 / 项目 / 工作区），不把别的项目或工作区的私有记忆混进 Prompt；
2. **相似度下限**：语义相似度低于 ``memory.medium_term.min_score`` 的不召回；
3. **排序分数**：``相似度 × 作用域匹配系数 × 重要度系数 × 时间衰减系数 × 置信度系数``；
4. 取 ``memory.medium_term.top_k`` 条，按分数倒序。

两个设计取舍（都写进 docstring，避免后来者按另一种读法改坏）：

- ``min_score`` 卡在**语义相似度**上，而不是四个系数相乘后的分数：四个系数都 ≤1，叠乘之后会把
  相关结果整体压到阈值以下（默认的 0.5 重要度/置信度相乘只剩四分之一），任何配置都会把全部
  记忆筛掉——架构 §10.2 的乘积公式是"排序"建议，不是阈值定义。两条分数都保持"越大越相关"。
  ``min_score`` 的量级取决于具体 embedding 模型，默认值按 ``BAAI/bge-small-zh-v1.5`` 实测标定
  （见 ``config.MediumTermMemoryConfig.min_score`` 的说明），换模型必须重新标定。
- 系数口径（不引入新配置键；T16 做统一预算时再定权重）：
  作用域匹配系数在精确过滤后恒为 ``1.0``（保留在公式里以对齐架构 §10.2）；
  重要度/置信度系数为 ``0.5 + 0.5 × 值``（[0.5, 1.0]，避免单个低分把相关结果直接压没）；
  时间衰减系数为 ``0.5 ** (age_days / decay_half_life_days)``，用 ``decay_half_life_days``。

过期与状态：``status`` 不是 ``active`` 的由存储层的 where 条件挡掉；``expires_at`` 已过期的
在这里丢掉。``expires_at`` 存在但解析不了时**按已过期处理**——宁可少召回，也不把"读不出有效期"
的记忆注入 Prompt（架构 §11.6：已删除/过期的记忆不得再召回）。

长期（结构化）记忆（T15，架构 §10.3）与中期不同：事实**没有向量**，因此
:func:`recall_long_term` 不接收查询文本、也没有相似度下限。它按作用域（事实走四种作用域，
任务结论只按当前项目）过滤后，用 ``confidence × importance`` 排序；任务结论没有重要度字段，
按**有效重要度 1.0** 处理（分数就等于 ``confidence``）。
``memory.long_term`` 的 ``confidence_threshold`` / ``importance_threshold`` **只做召回过滤**，
不影响写入，也不参与冲突裁决。

**自动老化在召回时实时生效**（T18 裁定）：``aging_enabled=true`` 时，超过 ``forget_after_days``
的事实当场排除，其余事实用线性衰减后的**有效重要度**做阈值判定、返回字段与排序分数。只在
``run_forgetting`` 里老化是不够的——维护任务跑之前，超期事实仍会以满分被召回（复审实证：
20 天旧事实、``forget_after_days=10`` 仍返回 ``importance=1.0``、``score=1.0``）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.config import LongTermMemoryConfig, MediumTermMemoryConfig
from local_agent.memory.forgetting import effective_importance, is_recallable_fact
from local_agent.memory.long_term import LongTermStore
from local_agent.memory.medium_term import MediumTermHit, MediumTermStore
from local_agent.memory.schemas import (
    EntityRecord,
    FactRecord,
    LongTermReference,
    MemoryReference,
    TaskConclusionRecord,
)
from local_agent.scope import (
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    SCOPES,
    MemoryScope,
    project_id_for,
    visible_scope_keys,
)

#: 精确过滤之后作用域必然匹配，系数恒为 1.0（保留项，见模块 docstring）。
SCOPE_MATCH_FACTOR = 1.0

#: 重要度/置信度系数的下界：`0.5 + 0.5 × 值`。
FACTOR_FLOOR = 0.5

#: 多取多少倍候选：过滤掉过期/低分后再截断到 ``top_k``，避免筛完不够数。
OVERFETCH_FACTOR = 4

#: 候选条数硬上限，避免一次查询拉回过多记录。
MAX_CANDIDATES = 200

#: 元数据缺失时的默认重要度/置信度。
_DEFAULT_VALUE = 0.5

#: 长期记忆一次召回的默认条数上限（``memory.long_term`` 配置节没有 ``top_k``，由调用方决定）。
DEFAULT_TOP_K = 8

#: ``task_conclusions`` 没有 ``importance`` 列，召回时统一按有效重要度 1.0 处理。
CONCLUSION_IMPORTANCE = 1.0


def recall_memory(
    store: MediumTermStore,
    *,
    query: str,
    config: MediumTermMemoryConfig,
    workspace_root: Path,
    project_id: str | None = None,
    user_id: str | None = None,
    now: datetime | None = None,
) -> list[MemoryReference]:
    """按作用域召回相关记忆，返回按分数倒序的引用列表。

    Args:
        store: 已打开的中期记忆库。
        query: 查询文本（通常是本轮的用户请求）。
        config: ``memory.medium_term`` 配置节，提供 ``top_k`` / ``min_score`` /
            ``decay_half_life_days``。
        workspace_root: 工作区根目录，用于推导工作区作用域键。
        project_id: 项目标识；省略时按 ``workspace_root`` 现算（与会话索引同一规则）。
        user_id: 逻辑用户；``None`` 时按 ``local`` 处理。
        now: 当前时间，默认取 UTC 现在；测试可注入。

    Returns:
        当前轮可注入 Prompt 的记忆引用；没有可用结果时返回空列表。
    """
    text = query.strip()
    if not text or config.top_k <= 0:
        return []
    moment = _moment(now)
    scope_keys = visible_scope_keys(
        user_id=user_id,
        project_id=project_id or project_id_for(workspace_root),
        workspace_root=workspace_root,
    )
    hits = store.query(
        query_vector=store.embed_query(text),
        scope_keys=scope_keys,
        n_results=min(config.top_k * OVERFETCH_FACTOR, MAX_CANDIDATES),
    )
    references = [
        reference
        for reference in (_to_reference(hit, config, moment) for hit in hits)
        if _is_usable(reference, config, moment)
    ]
    return _top_references(references, config.top_k)


def _to_reference(
    hit: MediumTermHit, config: MediumTermMemoryConfig, now: datetime
) -> MemoryReference:
    """把一条命中转换成引用，并算出排序分数。"""
    similarity = _similarity(hit.distance)
    importance = _value(hit.metadata.get("importance"))
    confidence = _value(hit.metadata.get("confidence"))
    created_at = str(hit.metadata.get("created_at") or "")
    score = (
        similarity
        * SCOPE_MATCH_FACTOR
        * (FACTOR_FLOOR + (1 - FACTOR_FLOOR) * importance)
        * _time_factor(created_at, config.decay_half_life_days, now)
        * (FACTOR_FLOOR + (1 - FACTOR_FLOOR) * confidence)
    )
    return MemoryReference(
        memory_id=hit.memory_id,
        content=hit.content,
        scope=_scope(hit.metadata.get("scope")),
        scope_key=str(hit.metadata.get("scope_key") or ""),
        similarity=round(similarity, 6),
        score=round(score, 6),
        importance=importance,
        confidence=confidence,
        created_at=created_at,
        source_thread_id=_optional_text(hit.metadata.get("source_thread_id")),
        expires_at=_optional_text(hit.metadata.get("expires_at")),
    )


def _is_usable(
    reference: MemoryReference, config: MediumTermMemoryConfig, now: datetime
) -> bool:
    """是否允许进入 Prompt：相似度达标、未过期、内容非空。"""
    if not reference["content"].strip():
        return False
    if reference["similarity"] < config.min_score:
        return False
    return not _is_expired(reference["expires_at"], now)


def _top_references(references: list[MemoryReference], top_k: int) -> list[MemoryReference]:
    """去重后按分数排序，取前 ``top_k`` 条（架构 §4.2：``memory_refs`` 需去重）。"""
    unique: dict[str, MemoryReference] = {}
    for reference in references:
        unique.setdefault(reference["memory_id"], reference)
    ordered = sorted(
        unique.values(),
        key=lambda item: (-item["score"], -item["similarity"], item["memory_id"]),
    )
    return ordered[:top_k]


def recall_long_term(
    store: LongTermStore,
    *,
    config: LongTermMemoryConfig,
    workspace_root: Path,
    project_id: str | None = None,
    user_id: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    now: datetime | None = None,
) -> list[LongTermReference]:
    """按作用域召回长期事实与任务结论，返回按分数倒序的引用列表。

    事实按本次运行可见的四种作用域精确过滤（与中期记忆同一套 ``scope_key`` 规则）；
    任务结论**只按当前项目的 ``project_scope``** 召回，不做其他作用域扩展。

    Args:
        store: 已打开的长期记忆库（:func:`~local_agent.memory.long_term.open_store`）。
        config: ``memory.long_term`` 配置节，提供两个召回阈值。
        workspace_root: 工作区根目录，用于推导工作区作用域键。
        project_id: 项目标识；省略时按 ``workspace_root`` 现算（与会话索引同一规则）。
        user_id: 逻辑用户；``None`` 时按 ``local`` 处理。
        top_k: 结果条数上限（与中期结果合并进 Prompt 时由调用方决定）。
        now: 当前时间，默认取 UTC 现在；测试可注入。

    Returns:
        当前轮可注入 Prompt 的长期记忆引用；没有可用结果时返回空列表。
    """
    if top_k <= 0:
        return []
    moment = _moment(now)
    project_scope = project_id or project_id_for(workspace_root)
    scope_keys = visible_scope_keys(
        user_id=user_id, project_id=project_scope, workspace_root=workspace_root
    )
    references: list[LongTermReference] = []
    for entity, fact in store.active_facts_with_entities(scope_keys, now=moment):
        if not is_recallable_fact(fact, config=config, now=moment):
            continue
        importance = effective_importance(fact, config=config, now=moment)
        if _long_term_usable(fact.confidence, importance, config):
            references.append(_fact_reference(entity, fact, importance=importance))
    for conclusion in store.list_task_conclusions(project_scope=project_scope):
        if conclusion.confidence >= config.confidence_threshold:
            references.append(_conclusion_reference(conclusion, project_scope))
    return _top_long_term(references, top_k)


def _long_term_usable(
    confidence: float, importance: float, config: LongTermMemoryConfig
) -> bool:
    """两个阈值都满足才召回（阈值只做过滤，不阻止写入、也不参与冲突裁决）。"""
    return (
        confidence >= config.confidence_threshold
        and importance >= config.importance_threshold
    )


def _fact_reference(
    entity: EntityRecord, fact: FactRecord, *, importance: float
) -> LongTermReference:
    """事实 → 引用：内容带上实体名与键，否则只看值判断不出说的是什么。

    ``importance`` 是**有效重要度**（老化后），不是库里的原值：阈值、返回字段与排序分数都用它，
    否则超期事实会以满分被召回。
    """
    return LongTermReference(
        memory_id=fact.id,
        kind="fact",
        content=f"{entity.name} 的 {fact.key}：{_value_text(fact.value_json)}",
        scope=entity.scope,
        scope_key=entity.scope_key,
        importance=importance,
        confidence=fact.confidence,
        created_at=fact.created_at,
        updated_at=fact.updated_at,
        expires_at=fact.expires_at,
        score=round(fact.confidence * importance, 6),
    )


def _conclusion_reference(
    conclusion: TaskConclusionRecord, project_scope: str
) -> LongTermReference:
    """任务结论 → 引用：没有 ``importance`` 列，按有效重要度 1.0（分数即 ``confidence``）。"""
    return LongTermReference(
        memory_id=conclusion.id,
        kind="task_conclusion",
        content=f"{conclusion.task_title}：{conclusion.conclusion}",
        scope=SCOPE_PROJECT,
        scope_key=project_scope,
        importance=CONCLUSION_IMPORTANCE,
        confidence=conclusion.confidence,
        created_at=conclusion.created_at,
        updated_at=conclusion.updated_at,
        expires_at=None,
        score=round(conclusion.confidence * CONCLUSION_IMPORTANCE, 6),
    )


def as_memory_reference(reference: LongTermReference) -> MemoryReference:
    """把长期召回结果映射成统一的 :class:`MemoryReference`（T19 的三层合并口径）。

    长期库没有向量，``similarity`` 位置放它的综合分（``confidence × importance``，任务结论即
    ``confidence``）——只用于排序与展示，**不是语义相似度**（见 ``schemas.MemoryReference``）。
    """
    return MemoryReference(
        memory_id=reference["memory_id"],
        content=reference["content"],
        scope=reference["scope"],
        scope_key=reference["scope_key"],
        similarity=reference["score"],
        score=reference["score"],
        importance=reference["importance"],
        confidence=reference["confidence"],
        created_at=reference["created_at"],
        source_thread_id=None,
        expires_at=reference["expires_at"],
        kind="long_term_fact" if reference["kind"] == "fact" else "task_conclusion",
    )


def _top_long_term(
    references: list[LongTermReference], top_k: int
) -> list[LongTermReference]:
    """去重后按分数倒序取前 ``top_k`` 条（``memory_refs`` 需去重）。"""
    unique: dict[str, LongTermReference] = {}
    for reference in references:
        unique.setdefault(reference["memory_id"], reference)
    ordered = sorted(unique.values(), key=lambda item: (-item["score"], item["memory_id"]))
    return ordered[:top_k]


def _value_text(value_json: str) -> str:
    """把 ``value_json`` 变成可读文本：字符串值原样，其余用 JSON 文本。"""
    try:
        value: Any = json.loads(value_json)
    except ValueError:
        return value_json
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _similarity(distance: float) -> float:
    """把 Chroma 的 cosine 距离换成"越大越相关"的相似度（架构 §10.2）。"""
    return _clamp(1.0 - distance)


def _time_factor(created_at: str, half_life_days: int, now: datetime) -> float:
    """时间衰减系数：``0.5 ** (age_days / half_life_days)``；读不出时间就不衰减。"""
    created = _parse_time(created_at)
    if created is None or half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (now - created).total_seconds() / 86400.0)
    return float(0.5 ** (age_days / half_life_days))


def _is_expired(expires_at: str | None, now: datetime) -> bool:
    """``expires_at`` 是否已过期；有值但解析不了时按已过期处理（宁可少召回）。"""
    if not expires_at:
        return False
    expires = _parse_time(expires_at)
    if expires is None:
        return True
    return expires <= now


def _moment(now: datetime | None) -> datetime:
    """当前时刻；显式传入的**朴素时间按 UTC 解释**。

    与 :func:`local_agent.memory.long_term._moment` 同一口径。不归一化的话，调用方传一个
    无时区的 ``now`` 会和带时区的 ``expires_at`` 比较时直接抛 ``TypeError``（生产路径传的是
    ``None``，所以只有测试注入或外部调用会踩到，但公开 API 不该有这种不一致）。
    """
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _parse_time(text: str) -> datetime | None:
    """解析 ISO-8601 时间；失败返回 ``None``；无时区信息按 UTC 处理。"""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _value(raw: object) -> float:
    """把 metadata 里的数值归一化到 [0, 1]；缺失或非法时用默认值。

    统一走字符串解析：``None`` 或列表不会静默变成 0，``bool`` 也不会被当成 0/1。
    """
    try:
        return _clamp(float(str(raw)))
    except ValueError:
        return _DEFAULT_VALUE


def _scope(raw: object) -> MemoryScope:
    """读回作用域；非法值按 ``global`` 处理（写入侧只会写四种合法值）。"""
    text = str(raw)
    if text in SCOPES:  # mypy 能把 `in SCOPES` 收窄成 Literal，无需 cast
        return text
    return SCOPE_GLOBAL


def _optional_text(raw: object) -> str | None:
    """metadata 里的可选文本字段。"""
    return str(raw) if raw else None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


__all__ = [
    "CONCLUSION_IMPORTANCE",
    "DEFAULT_TOP_K",
    "FACTOR_FLOOR",
    "MAX_CANDIDATES",
    "OVERFETCH_FACTOR",
    "SCOPE_MATCH_FACTOR",
    "recall_long_term",
    "recall_memory",
    "visible_scope_keys",
]

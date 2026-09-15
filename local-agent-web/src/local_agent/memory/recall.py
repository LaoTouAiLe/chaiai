"""中期记忆召回与打分（架构 §10.2，开发文档 T14B）。

召回顺序（架构 §10.2 末段）：

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
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from local_agent.config import MediumTermMemoryConfig
from local_agent.memory.medium_term import MediumTermHit, MediumTermStore
from local_agent.memory.schemas import MemoryReference
from local_agent.scope import (
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    SCOPE_USER,
    SCOPE_WORKSPACE,
    SCOPES,
    MemoryScope,
    project_id_for,
    scope_key_for,
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


def visible_scope_keys(
    *, user_id: str | None, project_id: str, workspace_root: Path
) -> list[tuple[MemoryScope, str]]:
    """本次运行可见的作用域组合（架构 §10.2 的四种作用域）。"""
    return [
        (SCOPE_GLOBAL, scope_key_for(SCOPE_GLOBAL)),
        (SCOPE_USER, scope_key_for(SCOPE_USER, user_id=user_id)),
        (SCOPE_PROJECT, scope_key_for(SCOPE_PROJECT, project_id=project_id)),
        (SCOPE_WORKSPACE, scope_key_for(SCOPE_WORKSPACE, workspace_root=workspace_root)),
    ]


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
    moment = now or datetime.now(timezone.utc)
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
    "FACTOR_FLOOR",
    "MAX_CANDIDATES",
    "OVERFETCH_FACTOR",
    "SCOPE_MATCH_FACTOR",
    "recall_memory",
    "visible_scope_keys",
]

"""记忆遗忘：显式过期与自动老化（架构 §10.2–§10.4，开发文档 T18）。

口径（T18 裁定）：

- **不把衰减后的重要度反复落库**：老化的效果在**每次读取时实时计算**，库里始终保留原始
  ``importance``。否则"维护任务多久跑一次"会让同一条记忆得到不同的复利结果。
- 中期记忆沿用召回层的半衰期公式 ``0.5 ** (age_days / decay_half_life_days)``；这里的遗忘只做
  **显式过期**（``expires_at`` 到期后删除——中期库没有版本历史语义）。
- 长期事实在 ``aging_enabled=true`` 时用**不修改原值的线性有效重要度**：
  ``importance × max(0, 1 - age_days / forget_after_days)``；``age_days`` 的基准是
  ``last_accessed_at or updated_at or created_at``。达到 ``forget_after_days`` 后**实时排除**，
  并由 :func:`run_forgetting` 显式落库为 ``expired``（T15 的软删除语义，历史行保留）。
- **显式 ``expires_at`` 始终优先**；``aging_enabled=false`` 只关闭自动老化，不关闭显式过期。
- ``task_conclusions`` 不参与软删除与自动老化（该表没有状态字段，见 schemas.py 的说明）。
- 纯查询路径**不写** ``last_accessed_at``：要不要 touch 由调用方（T19 接线时）显式决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from local_agent.config import AppConfig, LongTermMemoryConfig
from local_agent.memory.long_term import LongTermStore
from local_agent.memory.medium_term import MediumTermStore
from local_agent.memory.schemas import FactRecord

#: 一天有多少秒（年龄按天计算）。
_SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True, kw_only=True)
class ForgetReport:
    """一次遗忘任务的结果（供调用方记录轨迹）。"""

    #: 显式 ``expires_at`` 到期、被落库为 ``expired`` 的事实条数。
    expired_facts: int = 0
    #: 超过 ``forget_after_days``、被落库为 ``expired`` 的事实条数。
    aged_facts: int = 0
    #: 从中期库删除的过期记忆条数。
    deleted_medium: int = 0

    @property
    def total(self) -> int:
        """本次遗忘一共处理了多少条记忆。"""
        return self.expired_facts + self.aged_facts + self.deleted_medium


def fact_age_days(record: FactRecord, *, now: datetime) -> float:
    """事实的年龄（天），基准是 ``last_accessed_at or updated_at or created_at``。

    读不出时间（旧数据里的非法值）时返回 ``0``：**不因为读不出年龄就把它判成要遗忘**，
    与召回层"宁可少召回"的 fail-closed 方向相反——这里宁可不淘汰。
    """
    moment = _parse_time(record.last_accessed_at or record.updated_at or record.created_at)
    if moment is None:
        return 0.0
    return max(0.0, (now - moment).total_seconds() / _SECONDS_PER_DAY)


def effective_importance(
    record: FactRecord, *, config: LongTermMemoryConfig, now: datetime
) -> float:
    """事实的**有效重要度**：不修改库里的原值，只在读取时按线性衰减折算。

    ``aging_enabled=false`` 时等于原始重要度；``forget_after_days`` 之后为 0（并会被实时排除）。
    """
    if not config.aging_enabled:
        return float(record.importance)
    ratio = fact_age_days(record, now=now) / float(config.forget_after_days)
    return float(record.importance) * max(0.0, 1.0 - ratio)


def is_aged_out(record: FactRecord, *, config: LongTermMemoryConfig, now: datetime) -> bool:
    """是否已超过 ``forget_after_days``。

    显式 ``expires_at`` 优先：带有效期的行交给 :meth:`~local_agent.memory.long_term.LongTermStore.expire_overdue`
    处理，这里不重复判定；``aging_enabled=false`` 时永不老化。
    """
    if not config.aging_enabled or record.expires_at:
        return False
    return fact_age_days(record, now=now) >= float(config.forget_after_days)


def is_expired(expires_at: str | None, *, now: datetime) -> bool:
    """``expires_at`` 是否已过期；有值但解析不了时按**已过期**处理（宁可少召回）。

    与 ``long_term._is_expired`` / ``recall._is_expired`` 是同一口径的多份实现：三处都要判
    "读不出来算不算过期"，行为必须一致。
    """
    if not expires_at:
        return False
    expires = _parse_time(expires_at)
    if expires is None:
        return True
    return expires <= now


def is_recallable_fact(
    record: FactRecord, *, config: LongTermMemoryConfig, now: datetime
) -> bool:
    """该事实当前**是否会被召回**：未过期、未超龄。

    给"人工查看"（命令行 ``memory list``、页面侧栏计数）用。两处必须与召回层同一口径，否则会出现
    "命令行看得到、模型召回不到"（复审实证：已过期事实在 CLI 里可见）。注意这里只判**可见性**；
    相似度、阈值与 ``top_k`` 仍然取决于具体查询。
    """
    return not is_expired(record.expires_at, now=now) and not is_aged_out(
        record, config=config, now=now
    )


def run_forgetting(
    *,
    config: AppConfig,
    long_store: LongTermStore | None = None,
    medium_store: MediumTermStore | None = None,
    now: datetime | None = None,
) -> ForgetReport:
    """执行**显式遗忘任务**：显式过期 + 自动老化落库 + 中期过期清理。

    Args:
        config: 完整配置（长期用 ``aging_enabled`` / ``forget_after_days``）。
        long_store: 已打开的长期库；``None`` 表示该层未启用（跳过）。
        medium_store: 已打开的中期库；``None`` 表示该层未启用（跳过）。
        now: 当前时间，默认取 UTC 现在；测试可注入。

    Returns:
        :class:`ForgetReport`。任一层的异常都会向上抛（这是显式维护任务，失败要看得见），
        与"召回/写入失败按透传处理"的节点路径不同。
    """
    moment = now or datetime.now(timezone.utc)
    long_config = config.memory.long_term
    expired_facts = 0
    aged_facts = 0
    deleted_medium = 0
    if long_store is not None:
        expired_facts = long_store.expire_overdue(now=moment)
        aged_facts = long_store.expire_aged(
            forget_after_days=long_config.forget_after_days,
            aging_enabled=long_config.aging_enabled,
            now=moment,
        )
    if medium_store is not None:
        expired_ids = medium_store.expired_ids(now=moment)
        deleted_medium = medium_store.delete(expired_ids) if expired_ids else 0
    return ForgetReport(
        expired_facts=expired_facts, aged_facts=aged_facts, deleted_medium=deleted_medium
    )


def _parse_time(text: str | None) -> datetime | None:
    """解析 ISO-8601 时间；解析不了返回 ``None``（调用方按"读不出年龄"处理）。"""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def age_threshold(now: datetime, *, forget_after_days: int) -> datetime:
    """``forget_after_days`` 对应的截止时刻（供存储层做范围更新）。"""
    return now - timedelta(days=forget_after_days)


__all__ = [
    "ForgetReport",
    "age_threshold",
    "effective_importance",
    "fact_age_days",
    "is_aged_out",
    "is_expired",
    "is_recallable_fact",
    "run_forgetting",
]

"""记忆记录的字段契约（架构 §10.2、§10.3）。

这里只定义"一条记忆长什么样"，**不含任何存储实现，也不依赖第三方库**：

- :class:`MediumTermRecord`：中期向量记忆的元数据（架构 §10.2）。字段顺序与架构逐条对应；
  ``embedding`` 由向量库在写入时填充，契约里保留是为了不与架构字段表脱节。
- :class:`EntityRecord` / :class:`FactRecord` / :class:`TaskConclusionRecord`：长期 SQLite 的
  三张表（架构 §10.3），字段名必须与 ``local_agent.persistence.migrations`` 的建表语句逐字一致
  （``tests/test_memory.py`` 会拿 dataclass 字段与 ``PRAGMA table_info`` 对比）。

作用域键（``scope`` / ``scope_key`` / ``project_id`` / 逻辑用户 ID）的算法在
:mod:`local_agent.scope`，本模块只引用，不重复实现。

T13 未定的语义（现已定稿）：

- ``status`` 的取值集合：中期记忆由 T14B 定为下面的 ``MEDIUM_TERM_STATUS_*``；长期事实
  （``facts``）由 T15 定为 ``FACT_STATUS_*``（见下），历史行**保留在库里**，不物理删除。
- ``entities`` 的 ``(scope, scope_key, entity_type, name)`` 唯一性由 T15 的
  :func:`~local_agent.memory.long_term.record_entity` 在事务里保证（先查后插），
  **不加唯一约束**：旧库可能已有重复行，加约束会让迁移直接失败。
- ``value_json`` / ``evidence_json`` 是 **JSON 文本**（列名即契约），不是已解析的对象；
  解析与序列化由 T15 负责（``json.dumps``/``json.loads``，不引第三方）。
- 同键的重复/冲突判定按 **JSON 语义**比较（``json.loads`` 后递归比，键顺序无关，但
  ``true`` 与 ``1`` 不相等），不能直接比原始字符串，否则键顺序不同会造成假冲突。
- ``task_conclusions`` 没有 ``importance`` 与 ``status`` 字段：召回时按**有效重要度 1.0**
  处理（见 :class:`LongTermReference` 的 ``score``），它也不参与软删除。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from local_agent.scope import MemoryScope

#: 中期记忆的 ``status`` 取值（架构 §10.2 的 ``status`` 字段，T14B 定稿）。
#: 召回只取 ``active``，软删除用 ``deleted``；物理淘汰（忘掉、压缩）由 T18 负责。
MEDIUM_TERM_STATUS_ACTIVE = "active"
MEDIUM_TERM_STATUS_DELETED = "deleted"

#: 长期事实（``facts``）的 ``status`` 取值（架构 §10.3 的 ``status`` 字段，T15 定稿）。
#:
#: 只有 ``active`` 参与召回；其余是"历史或有争议"的行，**保留在库里**供审计与冲突解释
#: （物理清理属于 T18 的遗忘逻辑）：
#:
#: - ``active``：当前有效值。
#: - ``superseded``：被同键的新值取代（新来源置信度更高时），旧值留作版本历史。
#: - ``conflicted``：与当前 ``active`` 值不同但**未被采纳**（置信度不高于现有值）——
#:   冲突不静默覆盖，这一行就是"有争议的另一种说法"。
#: - ``expired``：``expires_at`` 已过（由
#:   :func:`~local_agent.memory.long_term.expire_overdue` 落库，查询时也会再判一次）。
#: - ``deleted``：人工删除（软删除）。
FACT_STATUS_ACTIVE = "active"
FACT_STATUS_SUPERSEDED = "superseded"
FACT_STATUS_CONFLICTED = "conflicted"
FACT_STATUS_EXPIRED = "expired"
FACT_STATUS_DELETED = "deleted"

#: 参与召回的长期事实状态。
FACT_STATUSES_RECALLABLE: tuple[str, ...] = (FACT_STATUS_ACTIVE,)


class MemoryReference(TypedDict):
    """一条召回结果（架构 §4.2 的 ``memory_refs`` 元素；字段由 T14B 定义，架构 §4.3 未固定）。

    只放"当前轮 Prompt 与页面需要"的最小集合：``content`` 是原文，
    ``similarity`` 是"越大越相关"的排序依据，``score`` 是叠加作用域/重要度/时间/置信度
    系数后的排序分数（同样越大越相关）。两者都只在当前轮有效，不写回记忆库。

    ``kind`` 标出来源——T19 起中期与长期召回**都进这一个列表**：

    - ``medium_term``：中期向量记忆，``similarity`` 是**真实余弦相似度**；
    - ``long_term_fact`` / ``task_conclusion``：长期结构化记忆。长期库没有向量，``similarity``
      位置放它的综合分（``confidence × importance``；任务结论即 ``confidence``），**只作为排序
      与展示依据，不要当成语义相似度解释**。
    """

    memory_id: str
    content: str
    scope: MemoryScope
    scope_key: str
    similarity: float
    score: float
    importance: float
    confidence: float
    created_at: str
    source_thread_id: str | None
    expires_at: str | None
    #: 记忆来源；缺省按中期向量记忆处理（兼容 T14B 产出的结果）。
    kind: NotRequired[Literal["medium_term", "long_term_fact", "task_conclusion"]]


class LongTermReference(TypedDict):
    """一条长期记忆召回结果。

    长期库是结构化事实，**没有向量**，所以这里不带 ``similarity``：排序用
    ``confidence × importance``（越大越优先），过期或非 ``active`` 的行不出现在结果里
    （``memory.long_term`` 的 ``confidence_threshold`` / ``importance_threshold`` 只做召回
    过滤，不阻止写入、也不参与冲突裁决）。
    ``kind='task_conclusion'`` 来自 ``task_conclusions``：该表没有 ``importance`` 列，
    按**有效重要度 1.0** 处理（分数即 ``confidence``），且只按当前项目的 ``project_scope``
    召回，映射成 ``scope='project'``。
    与中期 :class:`MemoryReference` 的合并（以及是否进 Prompt）留给 T18/T19，
    现在不把两种排序口径混成一种，免得后面分不开。
    """

    memory_id: str
    kind: Literal["fact", "task_conclusion"]
    content: str
    scope: MemoryScope
    scope_key: str
    importance: float
    confidence: float
    created_at: str
    updated_at: str
    expires_at: str | None
    score: float


@dataclass(frozen=True, kw_only=True)
class MediumTermRecord:
    """一条中期向量记忆（架构 §10.2 的字段表，顺序一致）。

    可选元数据字段没有值时**不写入向量库记录**（架构 §10.2 末段），因此它们的默认值是
    ``None``；召回时按 ``(scope, scope_key)`` 精确过滤后再排序。
    """

    id: str
    content: str
    #: 向量；由向量库在写入时生成，构造契约时通常为 ``None``。
    embedding: list[float] | None = None
    scope: MemoryScope
    scope_key: str
    user_id: str | None = None
    project_id: str | None = None
    workspace_root: str | None = None
    source_thread_id: str | None = None
    source_event_id: str | None = None
    importance: float
    confidence: float
    created_at: str
    last_accessed_at: str | None = None
    expires_at: str | None = None
    status: str


@dataclass(frozen=True, kw_only=True)
class EntityRecord:
    """长期记忆的实体行（架构 §10.3 ``entities``）。"""

    id: str
    entity_type: str
    name: str
    scope: MemoryScope
    scope_key: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, kw_only=True)
class FactRecord:
    """长期记忆的事实行（架构 §10.3 ``facts``）。

    事实通过 ``entity_id`` 继承实体作用域，自己不带 ``scope`` / ``scope_key``。
    """

    id: str
    entity_id: str
    key: str
    #: JSON 文本。
    value_json: str
    confidence: float
    importance: float
    source: str
    created_at: str
    updated_at: str
    last_accessed_at: str | None = None
    expires_at: str | None = None
    status: str


#: 一次 fact 写入的结果（T15 的"版本记录 + 冲突不静默覆盖"语义）：
#:
#: - ``created``：此前没有当前有效值，新行成为 ``active``；
#: - ``duplicate``：同键同值（按 JSON 语义比较），**不新增行**，返回现有 ``active`` 行；
#: - ``replaced``：新值置信度**严格更高**才取代，旧行置 ``superseded`` 留作版本历史；
#: - ``conflicted``：值不同但置信度不高于当前值（含相等），**不覆盖当前值**，新行记 ``conflicted``。
FactWriteOutcome = Literal["created", "duplicate", "replaced", "conflicted"]


@dataclass(frozen=True, kw_only=True)
class FactWriteResult:
    """一次 :meth:`~local_agent.memory.long_term.LongTermStore.upsert_fact` 的结果。

    ``record`` 是"本次与写入者相关的那一行"：``created`` / ``replaced`` 是新行，
    ``duplicate`` 是现有 ``active`` 行，``conflicted`` 是新记下的冲突行。
    ``previous`` 是本次被取代（``replaced``）或被判定冲突（``conflicted``）的原 ``active`` 行；
    其余情况为 ``None``。
    """

    outcome: FactWriteOutcome
    record: FactRecord
    previous: FactRecord | None = None


@dataclass(frozen=True, kw_only=True)
class TaskConclusionRecord:
    """长期记忆的任务结论行（架构 §10.3 ``task_conclusions``）。

    ``project_scope`` 保存稳定 ``project_id``（架构 §10.3 末段）。
    """

    id: str
    task_title: str
    conclusion: str
    #: JSON 文本。
    evidence_json: str
    project_scope: str
    confidence: float
    created_at: str
    updated_at: str

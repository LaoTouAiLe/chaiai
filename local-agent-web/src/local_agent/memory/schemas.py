"""记忆记录的字段契约（架构 §10.2、§10.3）。

这里只定义"一条记忆长什么样"，**不含任何存储实现，也不依赖第三方库**：

- :class:`MediumTermRecord`：中期向量记忆的元数据（架构 §10.2）。字段顺序与架构逐条对应；
  ``embedding`` 由向量库在写入时填充，契约里保留是为了不与架构字段表脱节。
- :class:`EntityRecord` / :class:`FactRecord` / :class:`TaskConclusionRecord`：长期 SQLite 的
  三张表（架构 §10.3），字段名必须与 ``local_agent.persistence.migrations`` 的建表语句逐字一致
  （``tests/test_memory.py`` 会拿 dataclass 字段与 ``PRAGMA table_info`` 对比）。

作用域键（``scope`` / ``scope_key`` / ``project_id`` / 逻辑用户 ID）的算法在
:mod:`local_agent.scope`，本模块只引用，不重复实现。

T13 未定的语义（不在这里猜，留给后续任务）：

- ``status`` 的取值集合由写入方（T14 中期、T15 长期）确定，本模块只声明它是字符串。
- ``entities`` 的 ``(scope, scope_key, entity_type, name)`` 是否唯一属于 T15 的 CRUD 语义，
  因此 T13 只建普通索引，不建唯一约束。
- ``value_json`` / ``evidence_json`` 是 **JSON 文本**（列名即契约），不是已解析的对象；
  解析与序列化由 T15 负责。
"""

from __future__ import annotations

from dataclasses import dataclass

from local_agent.scope import MemoryScope


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

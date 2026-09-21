"""长期结构化记忆（架构 §10.3，开发文档 T13 建库 / T15 CRUD）。

T13 交付：长期记忆使用**独立**的 SQLite 文件、按版本迁移、连接与结构自检
（:func:`open_store`），供 T15 的 CRUD、T18 的遗忘逻辑和 ``scripts/init_db.py`` 复用。

T15 交付：``entities`` / ``facts`` / ``task_conclusions`` 的写入、查询、更新、
**版本记录**、**冲突检测**、过期处理与人工删除，以及召回用的作用域查询原语。

语义口径（T15 定稿，改动前先看这里）：

- **版本记录**：同 ``(entity_id, key)`` 用"同键多行 + ``status`` 区分"实现（取值见
  :mod:`local_agent.memory.schemas`）。新值取代旧值时旧行置 ``superseded`` 并**保留在库里**
  当版本历史，不物理删除。
- **冲突不静默覆盖**：值不同时，只有新来源置信度**严格更高**才取代当前 ``active`` 行；
  置信度不高于（含相等）当前值时，当前值不变，新说法记成 ``conflicted`` 行并回传结果。
- **同值判定按 JSON 语义**（键顺序无关；``true`` 与 ``1`` 不相等）：同值视为重复写入，
  不新增行，返回现有 ``active`` 行。
- **过期只处理显式 ``expires_at``**：:meth:`LongTermStore.expire_overdue` 落库改状态；
  查询路径**同时**实时排除已到期记录，因此定时处理还没跑也不会召回过期数据。
  **写入**要求 ``expires_at`` 是可解析的 ISO-8601，否则直接拒绝（``_optional_expires_at``）；
  **读取**（列表、召回、过期处理）对库里**已有的**非法值按已过期处理（宁可少召回）。
  自动老化（重要度衰减、超期淘汰）属于 T18，本模块不做。
- **人工删除是软删除**：``facts`` 置 ``deleted``（保留审计）。``entities`` 与
  ``task_conclusions`` 都**没有状态字段**，所以删除实体只保留其身份行、把它的 facts 全部
  软删除，而任务结论不提供删除接口（不新增数据库字段）。
- **写入与阈值无关**：``confidence_threshold`` / ``importance_threshold`` 只用于召回过滤，
  不阻止写入、也不参与冲突裁决。

时间戳为 UTC ISO-8601 秒级（与 ``persistence.migrations`` 的 ``applied_at`` 同格式），
id 为带类型前缀的 ``uuid4``。除时间解析外本模块只依赖标准库与 ``scope`` 的作用域规则。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.exceptions import MemoryStoreError
from local_agent.memory.schemas import (
    FACT_STATUS_ACTIVE,
    FACT_STATUS_CONFLICTED,
    FACT_STATUS_DELETED,
    FACT_STATUS_EXPIRED,
    FACT_STATUS_SUPERSEDED,
    EntityRecord,
    FactRecord,
    FactWriteResult,
    TaskConclusionRecord,
)
from local_agent.persistence import migrations
from local_agent.scope import SCOPES, MemoryScope, scope_key_for

_ENTITY_COLUMN_NAMES: tuple[str, ...] = (
    "id",
    "entity_type",
    "name",
    "scope",
    "scope_key",
    "created_at",
    "updated_at",
)
_FACT_COLUMN_NAMES: tuple[str, ...] = (
    "id",
    "entity_id",
    "key",
    "value_json",
    "confidence",
    "importance",
    "source",
    "created_at",
    "updated_at",
    "last_accessed_at",
    "expires_at",
    "status",
)
_CONCLUSION_COLUMN_NAMES: tuple[str, ...] = (
    "id",
    "task_title",
    "conclusion",
    "evidence_json",
    "project_scope",
    "confidence",
    "created_at",
    "updated_at",
)

#: ``facts.status`` 的合法取值；读库时校验，未知状态明确失败（不静默当成 ``active``）。
_FACT_STATUSES: frozenset[str] = frozenset(
    {
        FACT_STATUS_ACTIVE,
        FACT_STATUS_SUPERSEDED,
        FACT_STATUS_CONFLICTED,
        FACT_STATUS_EXPIRED,
        FACT_STATUS_DELETED,
    }
)


def _select_list(names: Sequence[str], *, alias: str | None = None) -> str:
    """列清单（统一加引号，``key`` / ``status`` 这类名字不用特殊处理）。"""
    prefix = f"{alias}." if alias else ""
    return ", ".join(f'{prefix}"{name}"' for name in names)


_ENTITY_COLUMNS = _select_list(_ENTITY_COLUMN_NAMES)
_FACT_COLUMNS = _select_list(_FACT_COLUMN_NAMES)
_CONCLUSION_COLUMNS = _select_list(_CONCLUSION_COLUMN_NAMES)
_JOINED_COLUMNS = (
    f"{_select_list(_ENTITY_COLUMN_NAMES, alias='e')}, "
    f"{_select_list(_FACT_COLUMN_NAMES, alias='f')}"
)

#: 各写方法在库里读到的行顺序：最近变化/创建的在前。
_NEWEST_FIRST = '"updated_at" DESC, "created_at" DESC, "id" DESC'

#: 三张表的字段契约来源（架构 §10.3）：打开库时用它校验列是否齐全。
_TABLE_CONTRACTS: tuple[tuple[str, type[Any]], ...] = (
    ("entities", EntityRecord),
    ("facts", FactRecord),
    ("task_conclusions", TaskConclusionRecord),
)


def _missing_columns(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """按字段契约检查三张表缺哪些列。

    只看"缺不缺"，不比较顺序与类型：所有查询都用显式列名清单（``_FACT_COLUMNS`` 等），
    行到 dataclass 的映射按我们自己 SELECT 的顺序取值，与表里的物理顺序无关。

    必要性：``schema_migrations`` 的版本号是文件**自称**的，一个被别的程序改写、或来自不兼容
    版本的库完全可以声称自己是 v2。只在表名层面检查的话，这种库要等到第一次 CRUD 才裸抛
    ``no such column``。
    """
    problems: dict[str, list[str]] = {}
    for table, record in _TABLE_CONTRACTS:
        present = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        missing = [item.name for item in fields(record) if item.name not in present]
        if missing:
            problems[table] = missing
    return problems


@dataclass
class LongTermStore:
    """长期记忆库的连接与表信息，并提供 T15 的 CRUD 方法。

    连接由调用方持有并在进程存活期内复用（与 Checkpoint 的做法一致），因此需要
    ``check_same_thread=False``：Streamlit 每次重绘可能在不同线程执行。
    """

    connection: sqlite3.Connection
    path: Path
    schema_version: int
    #: **同一个实例**的读写互斥：本类所有使用 ``connection`` 的读写与 :meth:`close` 都走它。
    #: 写路径在锁内跑完整个"查询—判定—写入"事务，因此读路径不会看到"旧值已置 ``superseded``、
    #: 新值还没插入"的中间态（那会让一条记忆瞬间从库里消失）。
    #: 用 ``RLock`` 而不是 ``Lock``：写方法内部会调用同样加锁的公开读方法（``delete_entity`` →
    #: ``get_entity``、``find_entity`` → ``_find_entity``），不可重入会直接自锁死。
    #: **它管不到别的实例**：跨连接/跨进程由 :meth:`_transaction` 的 ``BEGIN IMMEDIATE`` 兜住。
    _connection_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )

    def table_names(self) -> frozenset[str]:
        """本库现有的表名。"""
        with self._connection_lock:
            return migrations.table_names(self.connection)

    def close(self) -> None:
        """关闭连接（重复调用无副作用）。"""
        with self._connection_lock:
            self.connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """把"查询—判定—写入"整段放进**一个**写事务，并持有连接锁直到事务收尾。

        必须用 ``BEGIN IMMEDIATE`` 而不能依赖 sqlite3 的隐式事务：后者要到第一条 DML 才把事务
        升级成写锁，于是两个连接（两个 :class:`LongTermStore` 实例，或多进程）会先各自读出旧快照
        再各写一行 —— 同键两条 ``active``、或同一实体两行。``IMMEDIATE`` 在事务开始时就取写锁
        （等待上限由连接超时决定，默认 5 秒），第二个写入者一定读到前一个的结果。

        ``_connection_lock`` 负责**同一实例**内的读写串行（读方法也走它，所以看不到事务中间态）；
        跨实例靠上面这条 SQL 写锁。

        Raises:
            sqlite3.OperationalError: 等锁超时（``database is locked``）或库文件只读。
        """
        with self._connection_lock:
            if self.connection.in_transaction:  # 上一次写入异常且回滚失败时的兜底，避免"嵌套事务"怪错
                self.connection.rollback()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                with suppress(sqlite3.Error):  # 回滚失败不能掩盖原始异常
                    self.connection.rollback()
                raise
            self.connection.commit()

    # ------------------------------------------------------------------ 实体

    def record_entity(
        self,
        *,
        entity_type: str,
        name: str,
        scope: MemoryScope,
        user_id: str | None = None,
        project_id: str | None = None,
        workspace_root: Path | None = None,
        now: datetime | None = None,
    ) -> EntityRecord:
        """写入实体；同 ``(scope, scope_key, entity_type, name)`` 已存在时**复用**该行。

        ``entities`` 没有唯一约束（旧库可能已有重复行，加约束会让迁移失败），所以这里在
        事务里"先查后插"来保证身份唯一。作用域键由 :func:`~local_agent.scope.scope_key_for`
        按 ``scope`` 与对应参数推导；无法确定时抛错而不写入。

        Args:
            entity_type: 实体类型（如 ``project`` / ``user`` / ``concept``）。
            name: 实体名。
            scope: 架构 §10.2 的四种作用域之一。
            user_id: ``user`` 作用域用。
            project_id: ``project`` 作用域用。
            workspace_root: ``workspace`` 作用域用。
            now: 当前时间，默认取 UTC 现在；测试可注入。

        Returns:
            已存在或新建的实体行。

        Raises:
            MemoryStoreError: 作用域非法/缺参数，或类型与名称是空白。
        """
        clean_scope, scope_key = _resolve_scope_key(
            scope, user_id=user_id, project_id=project_id, workspace_root=workspace_root
        )
        identity = _require_text(entity_type, "entity_type")
        label = _require_text(name, "name")
        # "先查后插"必须在同一个锁里：只锁 INSERT 挡不住两个线程同时查不到、各自插一行
        with self._transaction():
            existing = self._find_entity(
                entity_type=identity, name=label, scope=clean_scope, scope_key=scope_key
            )
            if existing is not None:
                return existing
            moment = _timestamp(_moment(now))
            record = EntityRecord(
                id=_new_id("entity"),
                entity_type=identity,
                name=label,
                scope=clean_scope,
                scope_key=scope_key,
                created_at=moment,
                updated_at=moment,
            )
            self.connection.execute(
                f"INSERT INTO entities ({_ENTITY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.entity_type,
                    record.name,
                    record.scope,
                    record.scope_key,
                    record.created_at,
                    record.updated_at,
                ),
            )
        return record

    def find_entity(
        self,
        *,
        entity_type: str,
        name: str,
        scope: MemoryScope,
        user_id: str | None = None,
        project_id: str | None = None,
        workspace_root: Path | None = None,
    ) -> EntityRecord | None:
        """按"作用域 + 身份"查实体，找不到返回 ``None``（不写入）。"""
        clean_scope, scope_key = _resolve_scope_key(
            scope, user_id=user_id, project_id=project_id, workspace_root=workspace_root
        )
        with self._connection_lock:
            return self._find_entity(
                entity_type=_require_text(entity_type, "entity_type"),
                name=_require_text(name, "name"),
                scope=clean_scope,
                scope_key=scope_key,
            )

    def get_entity(self, entity_id: str) -> EntityRecord | None:
        """按 id 取实体，找不到返回 ``None``。"""
        with self._connection_lock:
            row = self.connection.execute(
                f"SELECT {_ENTITY_COLUMNS} FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return _entity_from_row(row) if row is not None else None

    def delete_entity(self, entity_id: str) -> int:
        """人工删除实体：保留身份行，把它的 facts **全部软删除**，返回软删条数。

        ``entities`` 表没有状态字段（架构 §10.3 的字段表不新增列），因此这里不能声称
        "实体行本身已被软删除"——能在库里看到的效果只有关联 facts 变成 ``deleted``，
        于是该实体不再有任何可召回内容。实体不存在时返回 ``0``。
        """
        with self._transaction():
            if self.get_entity(entity_id) is None:
                return 0
            cursor = self.connection.execute(
                'UPDATE facts SET "status" = ?, "updated_at" = ?'
                ' WHERE "entity_id" = ? AND "status" <> ?',
                (
                    FACT_STATUS_DELETED,
                    _timestamp(_moment(None)),
                    entity_id,
                    FACT_STATUS_DELETED,
                ),
            )
            return cursor.rowcount

    # ------------------------------------------------------------------ 事实

    def upsert_fact(
        self,
        *,
        entity_id: str,
        key: str,
        value: Any,
        confidence: float,
        importance: float,
        source: str,
        expires_at: str | None = None,
        now: datetime | None = None,
    ) -> FactWriteResult:
        """写入一条事实，并按口径处理版本与冲突（见模块 docstring）。

        整个"读当前值 → 决定版本状态 → 写入"在**一个事务**里完成，避免并发写入留下两条
        ``active``。同键的历史行不删除：被取代的置 ``superseded``，未被采纳的新说法置
        ``conflicted``，两者都留在库里供审计与冲突解释。

        Args:
            entity_id: 所属实体；实体不存在时抛错（facts 通过它继承作用域）。
            key: 事实键（如 ``language``）；按原样去除首尾空白后使用。
            value: 任意可 JSON 序列化的值；序列化时排序键，比较按 JSON 语义。
            confidence: 来源置信度，0~1。
            importance: 重要度，0~1。
            source: 来源标识（供审计与冲突解释）。
            expires_at: 可选有效期，必须是可解析的 ISO-8601；解析不了时**写入即拒绝**
                （否则会留下"写得进、永远不召回"的记录）。库里已有的非法值仍按已过期处理。
            now: 当前时间，默认取 UTC 现在；测试可注入。

        Returns:
            :class:`~local_agent.memory.schemas.FactWriteResult`，``outcome`` 取值
            ``created`` / ``duplicate`` / ``replaced`` / ``conflicted``。

        Raises:
            MemoryStoreError: 实体不存在、字段空白、置信度/重要度超出 0~1、值不可序列化。
        """
        if self.get_entity(entity_id) is None:
            raise MemoryStoreError(
                f"实体不存在（{entity_id}）：facts 通过 entity_id 继承作用域，"
                "请先用 record_entity 建立实体"
            )
        fact_key = _require_text(key, "key")
        origin = _require_text(source, "source")
        payload = _normalize_json(value, "value")
        score = _require_score(confidence, "confidence")
        weight = _require_score(importance, "importance")
        moment = _moment(now)
        stamp = _timestamp(moment)
        expiry = _optional_expires_at(expires_at)

        with self._transaction():
            current, stale = self._active_facts_for_key(entity_id, fact_key, moment=moment)
            # 已到期的 active 行顺手落库为 expired：它们本来就不该参与召回，
            # 留着 active 状态会让"同键当前值"出现多个候选。
            self._set_status(stale, FACT_STATUS_EXPIRED, stamp)
            if current is not None and _same_json(current.value_json, payload):
                return FactWriteResult(outcome="duplicate", record=current, previous=None)
            if current is None:
                record = self._insert_fact(
                    entity_id=entity_id,
                    key=fact_key,
                    payload=payload,
                    confidence=score,
                    importance=weight,
                    source=origin,
                    expires_at=expiry,
                    status=FACT_STATUS_ACTIVE,
                    stamp=stamp,
                )
                return FactWriteResult(outcome="created", record=record, previous=None)
            if score > current.confidence:
                self._set_status([current], FACT_STATUS_SUPERSEDED, stamp)
                record = self._insert_fact(
                    entity_id=entity_id,
                    key=fact_key,
                    payload=payload,
                    confidence=score,
                    importance=weight,
                    source=origin,
                    expires_at=expiry,
                    status=FACT_STATUS_ACTIVE,
                    stamp=stamp,
                )
                return FactWriteResult(outcome="replaced", record=record, previous=current)
            record = self._insert_fact(
                entity_id=entity_id,
                key=fact_key,
                payload=payload,
                confidence=score,
                importance=weight,
                source=origin,
                expires_at=expiry,
                status=FACT_STATUS_CONFLICTED,
                stamp=stamp,
            )
            return FactWriteResult(outcome="conflicted", record=record, previous=current)

    def get_fact(self, fact_id: str) -> FactRecord | None:
        """按 id 取事实（任何状态），找不到返回 ``None``。"""
        with self._connection_lock:
            row = self.connection.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
        return _fact_from_row(row) if row is not None else None

    def list_facts(
        self,
        *,
        entity_id: str,
        statuses: Sequence[str] = (FACT_STATUS_ACTIVE,),
        include_expired: bool = False,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> list[FactRecord]:
        """按状态列出一个实体的事实，最近变化的在前。

        过期过滤在 Python 侧做（``expires_at`` 解析不了要按已过期处理，SQL 比较做不到），
        所以 ``limit`` 是**过滤之后**的条数上限，不会因为先截断而少给结果。
        """
        items = list(statuses)
        if not items:
            return []
        placeholders = ", ".join("?" for _ in items)
        with self._connection_lock:
            rows = self.connection.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts"
                f' WHERE "entity_id" = ? AND "status" IN ({placeholders})'
                f" ORDER BY {_NEWEST_FIRST}",
                (entity_id, *items),
            ).fetchall()
        records = [_fact_from_row(row) for row in rows]
        if not include_expired:
            moment = _moment(now)
            records = [record for record in records if not _is_expired(record.expires_at, moment)]
        return records[:limit] if limit is not None and limit >= 0 else records

    def fact_history(self, *, entity_id: str, key: str) -> list[FactRecord]:
        """同键的**全部版本**（含 ``superseded`` / ``conflicted`` / ``expired`` / ``deleted``）。

        人工删除当前值是软删除，**不会**自动把旧的 ``superseded`` 版本恢复成 ``active``；
        后续再次写入会按同键规则产生新的 ``active`` 行。
        """
        with self._connection_lock:
            rows = self.connection.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts"
                ' WHERE "entity_id" = ? AND "key" = ?'
                f" ORDER BY {_NEWEST_FIRST}",
                (entity_id, _require_text(key, "key")),
            ).fetchall()
        return [_fact_from_row(row) for row in rows]

    def delete_fact(self, fact_id: str) -> bool:
        """人工删除一条事实（软删除，保留审计）：改动成功返回 ``True``。"""
        with self._transaction():
            cursor = self.connection.execute(
                'UPDATE facts SET "status" = ?, "updated_at" = ?'
                ' WHERE "id" = ? AND "status" <> ?',
                (FACT_STATUS_DELETED, _timestamp(_moment(None)), fact_id, FACT_STATUS_DELETED),
            )
            return cursor.rowcount > 0

    def expire_overdue(self, *, now: datetime | None = None) -> int:
        """把已到期的 ``active`` 事实落库改为 ``expired``，返回条数。

        查询路径本来就会实时排除已到期记录，本方法是"定时处理"的显式入口（T18 会在合适
        时机调用）。口径与查询一致：``expires_at`` 有值但解析不了时按**已过期**处理。
        """
        moment = _moment(now)
        stamp = _timestamp(moment)
        with self._transaction():
            rows = self.connection.execute(
                f'SELECT {_FACT_COLUMNS} FROM facts'
                ' WHERE "status" = ? AND "expires_at" IS NOT NULL',
                (FACT_STATUS_ACTIVE,),
            ).fetchall()
            overdue = [
                record
                for record in (_fact_from_row(row) for row in rows)
                if _is_expired(record.expires_at, moment)
            ]
            self._set_status(overdue, FACT_STATUS_EXPIRED, stamp)
        return len(overdue)

    def expire_aged(
        self,
        *,
        forget_after_days: int,
        aging_enabled: bool = True,
        now: datetime | None = None,
    ) -> int:
        """把超过 ``forget_after_days`` 的 ``active`` 事实落库改为 ``expired``，返回条数。

        与 :meth:`expire_overdue` 的分工是**显式有效期优先**：带 ``expires_at`` 的行交给
        :meth:`expire_overdue`，这里只处理**没有有效期**、纯靠年龄淘汰的行，两条规则不会互相
        覆盖。年龄基准是 ``last_accessed_at or updated_at or created_at``，与
        :func:`local_agent.memory.forgetting.fact_age_days` 同一口径（改一处要同步另一处）。

        ``aging_enabled=false`` 时什么都不做——它只关闭自动老化，不关闭显式过期；
        ``task_conclusions`` 不参与老化（该表没有状态字段）。
        """
        if not aging_enabled or forget_after_days <= 0:
            return 0
        moment = _moment(now)
        stamp = _timestamp(moment)
        with self._transaction():
            rows = self.connection.execute(
                f'SELECT {_FACT_COLUMNS} FROM facts'
                ' WHERE "status" = ? AND "expires_at" IS NULL',
                (FACT_STATUS_ACTIVE,),
            ).fetchall()
            aged = [
                record
                for record in (_fact_from_row(row) for row in rows)
                if _age_days(record, moment) >= forget_after_days
            ]
            self._set_status(aged, FACT_STATUS_EXPIRED, stamp)
        return len(aged)

    def list_active_facts(self, *, limit: int | None = None) -> list[FactRecord]:
        """列出**整库**的 ``active`` 事实（不做作用域与时间过滤）。

        给"整库维护与预演"用：``expire_overdue()`` / ``expire_aged()`` 都是整库操作，预演必须与
        执行同范围，否则会出现"预览 0 条、实际过期 1 条"的错觉（复审实证）。时间判定不在存储层做，
        由调用方用 :func:`~local_agent.memory.forgetting.is_expired` / ``is_aged_out`` 判定。
        """
        with self._connection_lock:
            rows = self.connection.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts"
                f' WHERE "status" = ? ORDER BY {_NEWEST_FIRST}',
                (FACT_STATUS_ACTIVE,),
            ).fetchall()
        records = [_fact_from_row(row) for row in rows]
        return records[:limit] if limit is not None and limit >= 0 else records

    def active_facts_with_entities(
        self, scope_keys: Sequence[tuple[MemoryScope, str]], *, now: datetime | None = None
    ) -> list[tuple[EntityRecord, FactRecord]]:
        """召回用原语：列出这些 ``(scope, scope_key)`` 下的 ``active`` 事实及其实体。

        Returns:
            ``(实体, 事实)`` 对，按 ``(entity_id, key)`` 分组无序；**包含时间上已到期但状态
            还是 ``active``** 的行——到期判定由召回层实时完成（见 :func:`~local_agent.memory.recall.recall_long_term`）。
        """
        pairs = list(scope_keys)
        if not pairs:
            return []
        condition = " OR ".join('(e."scope" = ? AND e."scope_key" = ?)' for _ in pairs)
        params: list[object] = []
        for scope, scope_key in pairs:
            params.extend([_require_scope(scope), scope_key])
        params.append(FACT_STATUS_ACTIVE)
        with self._connection_lock:
            rows = self.connection.execute(
                f"SELECT {_JOINED_COLUMNS} FROM facts f JOIN entities e"
                ' ON e."id" = f."entity_id"'
                f' WHERE ({condition}) AND f."status" = ?',
                params,
            ).fetchall()
        entity_count = len(_ENTITY_COLUMN_NAMES)
        return [
            (_entity_from_row(row[:entity_count]), _fact_from_row(row[entity_count:]))
            for row in rows
        ]

    # -------------------------------------------------------------- 任务结论

    def record_task_conclusion(
        self,
        *,
        task_title: str,
        conclusion: str,
        project_scope: str,
        confidence: float,
        evidence: Any = None,
        now: datetime | None = None,
    ) -> TaskConclusionRecord:
        """写入一条任务结论（``task_conclusions`` 没有版本与状态字段，写入即长期有效）。"""
        record, _ = self.upsert_task_conclusion(
            conclusion_id=_new_id("conclusion"),
            task_title=task_title,
            conclusion=conclusion,
            project_scope=project_scope,
            confidence=confidence,
            evidence=evidence,
            now=now,
        )
        return record

    def upsert_task_conclusion(
        self,
        *,
        conclusion_id: str,
        task_title: str,
        conclusion: str,
        project_scope: str,
        confidence: float,
        evidence: Any = None,
        now: datetime | None = None,
    ) -> tuple[TaskConclusionRecord, bool]:
        """按**调用方给出的稳定 id** 幂等写入任务结论，返回 ``(记录, 是否插入了新行)``。

        为什么需要它：任务结论表没有版本与状态字段，写入即新增一行。数据库写入成功、但 Checkpoint
        提交前发生重放时会重复插入（复审探针：连续写同一来源得到 2 条结论）；写入方按"项目 + 来源
        事件"算出稳定 id 后，重放等价于一次幂等重试，而不是多出一条记忆。
        """
        moment = _timestamp(_moment(now))
        record = TaskConclusionRecord(
            id=_require_text(conclusion_id, "conclusion_id"),
            task_title=_require_text(task_title, "task_title"),
            conclusion=_require_text(conclusion, "conclusion"),
            evidence_json=_normalize_json(evidence, "evidence"),
            project_scope=_require_text(project_scope, "project_scope"),
            confidence=_require_score(confidence, "confidence"),
            created_at=moment,
            updated_at=moment,
        )
        with self._transaction():
            cursor = self.connection.execute(
                f"INSERT OR IGNORE INTO task_conclusions ({_CONCLUSION_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.task_title,
                    record.conclusion,
                    record.evidence_json,
                    record.project_scope,
                    record.confidence,
                    record.created_at,
                    record.updated_at,
                ),
            )
            if not cursor.rowcount:
                row = self.connection.execute(
                    f'SELECT {_CONCLUSION_COLUMNS} FROM task_conclusions WHERE "id" = ?',
                    (record.id,),
                ).fetchone()
                if row is None:  # pragma: no cover —— INSERT OR IGNORE 之外的失败
                    raise MemoryStoreError(f"任务结论写入未生效：{record.id}")
                return _conclusion_from_row(row), False
        return record, True

    def list_task_conclusions(
        self, *, project_scope: str, limit: int | None = None
    ) -> list[TaskConclusionRecord]:
        """列出某个项目作用域下的任务结论，最近写入的在前。"""
        with self._connection_lock:
            rows = self.connection.execute(
                f"SELECT {_CONCLUSION_COLUMNS} FROM task_conclusions"
                ' WHERE "project_scope" = ? ORDER BY "created_at" DESC, "id" DESC',
                (_require_text(project_scope, "project_scope"),),
            ).fetchall()
        records = [_conclusion_from_row(row) for row in rows]
        return records[:limit] if limit is not None and limit >= 0 else records

    # ------------------------------------------------------------------ 内部

    def _find_entity(
        self, *, entity_type: str, name: str, scope: MemoryScope, scope_key: str
    ) -> EntityRecord | None:
        row = self.connection.execute(
            f"SELECT {_ENTITY_COLUMNS} FROM entities"
            ' WHERE "entity_type" = ? AND "name" = ? AND "scope" = ? AND "scope_key" = ?'
            ' ORDER BY "created_at", "id" LIMIT 1',
            (entity_type, name, scope, scope_key),
        ).fetchone()
        return _entity_from_row(row) if row is not None else None

    def _active_facts_for_key(
        self, entity_id: str, key: str, *, moment: datetime
    ) -> tuple[FactRecord | None, list[FactRecord]]:
        """同键的当前值与"状态还是 active 但已到期"的行。

        同一键出现多条未到期的 ``active`` 行属于脏数据（写入已由 :meth:`_transaction` 串行化，
        正常情况下产生不了）。收敛规则与写入时的取代规则**一致**：先取**置信度最高**的一条作为
        当前值（平票再按最近变化时间、再按 id 打破），其余归入"已被取代"——若只按"最近更新"选，
        一次脏数据修复就可能把 0.9 的当前值降级、让 0.5 的旧说法变成 active，正好违背
        "只有置信度严格更高才取代"。
        """
        rows = self.connection.execute(
            f'SELECT {_FACT_COLUMNS} FROM facts'
            ' WHERE "entity_id" = ? AND "key" = ? AND "status" = ?'
            f' ORDER BY "confidence" DESC, {_NEWEST_FIRST}',
            (entity_id, key, FACT_STATUS_ACTIVE),
        ).fetchall()
        records = [_fact_from_row(row) for row in rows]
        stale: list[FactRecord] = []
        fresh: list[FactRecord] = []
        for record in records:
            (stale if _is_expired(record.expires_at, moment) else fresh).append(record)
        if len(fresh) > 1:
            self._set_status(fresh[1:], FACT_STATUS_SUPERSEDED, _timestamp(moment))
        return (fresh[0] if fresh else None), stale

    def _insert_fact(
        self,
        *,
        entity_id: str,
        key: str,
        payload: str,
        confidence: float,
        importance: float,
        source: str,
        expires_at: str | None,
        status: str,
        stamp: str,
    ) -> FactRecord:
        """插入一行事实（事务由调用方管理）。"""
        record = FactRecord(
            id=_new_id("fact"),
            entity_id=entity_id,
            key=key,
            value_json=payload,
            confidence=confidence,
            importance=importance,
            source=source,
            created_at=stamp,
            updated_at=stamp,
            last_accessed_at=None,
            expires_at=expires_at,
            status=status,
        )
        self.connection.execute(
            f"INSERT INTO facts ({_FACT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.entity_id,
                record.key,
                record.value_json,
                record.confidence,
                record.importance,
                record.source,
                record.created_at,
                record.updated_at,
                record.last_accessed_at,
                record.expires_at,
                record.status,
            ),
        )
        return record

    def _set_status(
        self, records: Sequence[FactRecord], status: str, stamp: str
    ) -> int:
        """批量改状态并把 ``updated_at`` 记为状态变化时间（事务由调用方管理）。"""
        if not records:
            return 0
        self.connection.executemany(
            'UPDATE facts SET "status" = ?, "updated_at" = ? WHERE "id" = ?',
            [(status, stamp, record.id) for record in records],
        )
        return len(records)

def open_store(database_path: Path) -> LongTermStore:
    """打开（必要时创建）长期记忆库并升级到当前结构版本。

    Args:
        database_path: 长期记忆库文件路径；父目录不存在时自动创建。应与
            ``memory.short_term.path``（聊天库）指向**不同**的文件。

    Returns:
        可复用的 :class:`LongTermStore`。

    Raises:
        MemoryStoreError: 迁移后仍看不到 :data:`~local_agent.persistence.migrations.LONG_TERM_TABLES`，
            通常是把长期记忆指向了聊天库（该文件已有 ``schema_migrations`` 记录，长期表不会建立）；
            或三张表的列与架构 §10.3 的字段契约不符（文件被其他程序改写、或来自不兼容的版本）。
        sqlite3.Error: 数据库无法打开或建表失败（例如库文件只读）。
    """
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    try:
        _reject_foreign_database(connection, path)
        try:
            version = migrations.apply_long_term_migrations(connection)
        except sqlite3.Error as exc:
            raise MemoryStoreError(
                f"长期记忆库结构升级失败（{path.as_posix()}）：{exc}。"
                "请确认 memory.long_term.path 指向独立的数据库文件，"
                "而不是 memory.short_term.path 的聊天库文件。"
            ) from exc
        present = migrations.table_names(connection)
        missing = [name for name in migrations.LONG_TERM_TABLES if name not in present]
        if missing:
            raise MemoryStoreError(
                f"长期记忆库缺少表：{'、'.join(missing)}。"
                f"请确认 memory.long_term.path 指向独立的数据库文件（当前是 {path.as_posix()}），"
                "而不是 memory.short_term.path 的聊天库文件。"
            )
        try:
            broken = _missing_columns(connection)
        except sqlite3.Error as exc:  # 文件损坏时 PRAGMA 也会失败
            raise MemoryStoreError(
                f"长期记忆库无法读取结构信息（{path.as_posix()}）：{exc}。"
                "文件可能已损坏、或不是 SQLite 数据库；请确认路径无误，必要时从备份恢复。"
            ) from exc
        if broken:
            detail = "；".join(
                f"{table} 缺少列 {'、'.join(names)}" for table, names in broken.items()
            )
            raise MemoryStoreError(
                f"长期记忆库结构与当前版本不一致（{path.as_posix()}）：{detail}。"
                "schema_migrations 里的版本号在这里不可信（文件可能被其他程序改写、或来自不兼容"
                "的版本），继续使用会在第一次读写时抛出难懂的错误。请确认内容无用后删除该文件"
                "（或改用其他路径），再执行 `uv run python scripts/init_db.py` 重建。"
            )
    except BaseException:  # 建库失败（含 Ctrl-C）也要先关连接，不留悬挂句柄
        connection.close()
        raise
    return LongTermStore(connection=connection, path=path, schema_version=version)


def _reject_foreign_database(connection: sqlite3.Connection, path: Path) -> None:
    """在跑迁移**之前**识别"这个文件不是长期记忆库"，且不改动它。

    只靠"迁移版本号"识别会误伤：两个库各自持有 ``schema_migrations``，聊天库已经把版本 1
    用掉了，长期库的版本 1 会被当成"已应用"跳过，接着版本 2 直接对不存在的 ``entities``
    建索引 —— 抛出来的是 sqlite 原生错误，而不是"你指错文件了"。所以先看表：

    - 文件里一张我们的表都没有（全新文件 / 空库）→ 交给迁移建表；
    - 已经有 ``schema_migrations`` 却缺长期表 → 明确失败，并保持文件原样。

    Raises:
        MemoryStoreError: 该文件受迁移管理但不是长期记忆库。
    """
    present = migrations.table_names(connection)
    if not present:
        return
    missing = [name for name in migrations.LONG_TERM_TABLES if name not in present]
    if missing and "schema_migrations" in present:
        raise MemoryStoreError(
            f"长期记忆库缺少表：{'、'.join(missing)}，但该文件已有 schema_migrations 记录"
            f"（{path.as_posix()}）。请确认 memory.long_term.path 指向独立的数据库文件，"
            "而不是 memory.short_term.path 的聊天库文件。"
        )


def current_schema_version(database_path: Path) -> int | None:
    """只读地读取长期记忆库的结构版本。

    Args:
        database_path: 长期记忆库文件路径。

    Returns:
        已应用的最高版本；文件还不存在时返回 ``None``（**不会创建文件**）。
    """
    path = Path(database_path)
    if not path.is_file():
        return None
    connection = sqlite3.connect(str(path))
    try:
        return migrations.current_version(connection)
    finally:
        connection.close()


def _new_id(kind: str) -> str:
    """带类型前缀的 id（前缀便于在日志与手工排查时区分记忆种类）。"""
    return f"{kind}-{uuid.uuid4().hex}"


def _moment(now: datetime | None) -> datetime:
    """当前时刻；朴素时间按 UTC 解释（库里的时间统一是 UTC）。"""
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _timestamp(moment: datetime) -> str:
    """UTC ISO-8601 秒级时间戳（与 ``persistence.migrations.applied_at`` 同格式）。"""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(text: str) -> datetime | None:
    """解析 ISO-8601 时间；失败返回 ``None``；无时区信息按 UTC 处理。

    与 :func:`local_agent.memory.recall._parse_time` 是**同一口径**的两份实现：两处都要判
    "``expires_at`` 读不出来算不算过期"，行为必须一致（宁可少召回）。
    """
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _is_expired(expires_at: str | None, now: datetime) -> bool:
    """``expires_at`` 是否已过期；有值但解析不了时按**已过期**处理。"""
    if not expires_at:
        return False
    expires = _parse_time(expires_at)
    if expires is None:
        return True
    return expires <= now


def _require_text(value: str, field: str) -> str:
    """必填文本字段：去除首尾空白后不能为空。"""
    text = str(value).strip()
    if not text:
        raise MemoryStoreError(f"{field} 不能为空：无法确定时不写入记忆")
    return text


def _optional_text(value: str | None) -> str | None:
    """可选文本字段：空白视作未提供。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _age_days(record: FactRecord, moment: datetime) -> float:
    """事实的年龄（天），基准是 ``last_accessed_at or updated_at or created_at``。

    与 :func:`local_agent.memory.forgetting.fact_age_days` 同一口径（那个是给调用方的公开版本，
    这里避免两个模块互相导入）；改一处要同步另一处。读不出时间时返回 ``0``——不因为读不出年龄
    就淘汰记忆（与召回层"宁可少召回"的方向相反，这里宁可不遗忘）。
    """
    parsed = _parse_time(record.last_accessed_at or record.updated_at or record.created_at)
    if parsed is None:
        return 0.0
    return max(0.0, (moment - parsed).total_seconds() / 86_400.0)


def _optional_expires_at(value: str | None) -> str | None:
    """可选有效期：必须是可解析的 ISO-8601，否则**写入时直接拒绝**。

    拒绝的理由：非法值会因为"解析不了按已过期处理"而永远不会被召回，变成"写入成功却查不到"
    的静默失效。库里**已有**的非法值仍按已过期处理（见 :func:`_is_expired`），那是为了应对
    旧数据，不再通过写入路径产生。
    """
    text = _optional_text(value)
    if text is None:
        return None
    if _parse_time(text) is None:
        raise MemoryStoreError(
            f"expires_at 必须是 ISO-8601 时间（当前是 {value!r}）；"
            "无法解析的有效期会让这条记忆永远不参与召回"
        )
    return text


def _require_score(value: float, field: str) -> float:
    """0~1 的分数（``confidence`` / ``importance``）；``NaN`` 也会被拒绝。"""
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise MemoryStoreError(f"{field} 必须在 0~1 之间（当前 {number}）")
    return number


def _require_scope(scope: MemoryScope | str) -> MemoryScope:
    """校验作用域取值（与 :func:`~local_agent.scope.scope_key_for` 同一集合）。"""
    if scope in SCOPES:  # mypy 能把 `in SCOPES` 收窄成 Literal，无需 cast
        return scope
    allowed = "、".join(SCOPES)
    raise MemoryStoreError(f"未知作用域：{scope!r}（只能是 {allowed}）")


def _resolve_scope_key(
    scope: MemoryScope,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
    workspace_root: Path | None = None,
) -> tuple[MemoryScope, str]:
    """把"作用域 + 作用域参数"解析成 ``(scope, scope_key)``；缺参数时抛错。"""
    clean_scope = _require_scope(scope)
    return clean_scope, scope_key_for(
        clean_scope, user_id=user_id, project_id=project_id, workspace_root=workspace_root
    )


def _normalize_json(value: Any, field: str) -> str:
    """序列化成 JSON 文本（排序键，便于同值比较与人工查看）。

    ``allow_nan=False``：``NaN`` / ``Infinity`` 不是合法 JSON，写进库里别的解析器会读不了；而且
    ``NaN != NaN`` 会让"重复写入"变成假冲突（每次写都新增一行）。
    """
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MemoryStoreError(
            f"{field} 必须是可 JSON 序列化的值（不接受 NaN / Infinity）：{exc}"
        ) from exc


def _same_json(left_text: str, right_text: str) -> bool:
    """两段 JSON 文本是否**语义相同**（键顺序无关，``true`` 与 ``1`` 不相等）。"""
    try:
        left: Any = json.loads(left_text)
        right: Any = json.loads(right_text)
    except ValueError:
        return left_text == right_text  # 读不出 JSON（脏数据）只能退回文本比较
    return _json_equal(left, right)


def _json_equal(left: Any, right: Any) -> bool:
    """按 JSON 类型规则递归比较。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right  # 布尔只与同值布尔相等：JSON 的 true 不等于 1
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(item, other) for item, other in zip(left, right)
        )
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return False


def _entity_from_row(row: Sequence[Any]) -> EntityRecord:
    try:
        return EntityRecord(
            id=str(row[0]),
            entity_type=str(row[1]),
            name=str(row[2]),
            # 作用域是隔离边界：库里读到非法值时不猜（不当作 global），明确失败
            scope=_require_scope(str(row[3])),
            scope_key=str(row[4]),
            created_at=str(row[5]),
            updated_at=str(row[6]),
        )
    except (IndexError, TypeError, ValueError) as exc:
        raise MemoryStoreError(f"entities 表存在无法解码的行：{exc}") from exc


def _fact_from_row(row: Sequence[Any]) -> FactRecord:
    """**解码失败必须转成领域异常**：合法 SQLite 文件里的脏值（例如 ``confidence`` 存了文本）
    会让 ``float(...)`` 抛 ``ValueError``；调用方只隔离 ``MemoryStoreError`` / ``sqlite3`` /
    ``OSError``，一条脏行就能拖垮整轮对话（复审实证）。

    分数列复用 :func:`_require_score`（非数字、``NaN``、``Infinity``、负数或 >1 都会被拒绝），
    ``status`` 也必须是已知取值——否则会算出一个无穷大的排序分，却不触发单层降级。
    """
    try:
        return FactRecord(
            id=str(row[0]),
            entity_id=str(row[1]),
            key=str(row[2]),
            value_json=str(row[3]),
            confidence=_require_score(float(row[4]), "confidence"),
            importance=_require_score(float(row[5]), "importance"),
            source=str(row[6]),
            created_at=str(row[7]),
            updated_at=str(row[8]),
            last_accessed_at=str(row[9]) if row[9] is not None else None,
            expires_at=str(row[10]) if row[10] is not None else None,
            status=_require_fact_status(str(row[11])),
        )
    except MemoryStoreError:
        raise
    except (IndexError, TypeError, ValueError) as exc:
        raise MemoryStoreError(f"facts 表存在无法解码的行：{exc}") from exc


def _require_fact_status(status: str) -> str:
    """校验长期事实的 ``status`` 取值（库里读到未知状态时明确失败，不静默当成 active）。"""
    if status in _FACT_STATUSES:
        return status
    allowed = "、".join(sorted(_FACT_STATUSES))
    raise MemoryStoreError(f"facts 行存在未知状态：{status!r}（只能是 {allowed}）")


def _conclusion_from_row(row: Sequence[Any]) -> TaskConclusionRecord:
    try:
        return TaskConclusionRecord(
            id=str(row[0]),
            task_title=str(row[1]),
            conclusion=str(row[2]),
            evidence_json=str(row[3]),
            project_scope=str(row[4]),
            confidence=float(row[5]),
            created_at=str(row[6]),
            updated_at=str(row[7]),
        )
    except (IndexError, TypeError, ValueError) as exc:
        raise MemoryStoreError(f"task_conclusions 表存在无法解码的行：{exc}") from exc

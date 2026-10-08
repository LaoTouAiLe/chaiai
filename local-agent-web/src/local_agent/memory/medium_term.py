"""中期向量记忆：Chroma 存储层（架构 §10.2，开发文档 T13、T14B）。

``chromadb`` 是可选依赖（``pyproject.toml`` 的 ``memory`` extra）。本模块**不在导入期**
碰它，只有真正要打开向量库时才导入：

- ``memory.medium_term.enabled = false`` → 不导入 ``chromadb``、不建目录、不建 collection，
  抛 :class:`MemoryDisabledError`，调用方（节点）按"关闭即透传"处理；
- 已启用但没装 extra → 抛 :class:`MemoryUnavailableError`，消息里给出 ``uv sync --extra memory``，
  而不是裸 ``ImportError``；
- 已启用但没给 embedding 函数 → 抛 :class:`MemoryStoreError`，避免 Chroma 退回到自带默认模型
  （那会联网下载模型，与"本机、显式缓存"的约定冲突）。

已核实的 Chroma 事实（1.5.9，实测）：

- 自定义 embedding 函数**必须有 ``name()``**（只有 ``__call__`` 会直接 ``AttributeError``），
  因此 T14A 的适配器补了 ``name()`` / ``get_config()``（见 ``llm/embedding.py``）。
- ``get_or_create_collection(metadata=...)`` 对**已存在**的 collection **不会覆盖** metadata，
  所以兼容性必须由本模块自己读 ``collection.metadata`` 比对（架构 §10.2 要求"任一必需信息
  不一致时停止读写并提示重建索引"）。
- 距离度量走 ``metadata={"hnsw:space": "cosine"}``；``cosine`` 距离 = ``1 - 余弦相似度``，
  所以相关度是 ``1 - distance``（架构 §10.2 要求统一成"越大越相关"）。
- ``where`` 支持 ``$and`` / ``$or`` / ``$eq``；维度不一致时写入报
  ``InvalidArgumentError: Collection expecting embedding with dimension of N, got M``，
  本模块把它包装成带"换过模型就重建索引"提示的可读错误。

作用域过滤与打分在 :mod:`local_agent.memory.recall`，本模块只做"按 scope 精确过滤 + 取候选"。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_agent.config import MediumTermMemoryConfig
from local_agent.exceptions import MemoryDisabledError, MemoryStoreError, MemoryUnavailableError
from local_agent.llm.embedding import EmbeddingFunction, EmbeddingInfo
from local_agent.memory.schemas import MEDIUM_TERM_STATUS_ACTIVE, MediumTermRecord
from local_agent.scope import MemoryScope

#: 缺依赖时的安装提示（消息面向使用者，必须能照做）。
CHROMADB_INSTALL_HINT = "未安装 chromadb：请先执行 `uv sync --extra memory` 安装可选依赖"

#: 缺 embedding 时的配置提示。
EMBEDDING_REQUIRED_HINT = (
    "未提供 embedding 函数：中期记忆需要显式 Embedding 适配器（T14A 的 llm/embedding.py），"
    "并且不使用 Chroma 自带的默认模型"
)

#: collection 的距离度量；与适配器的 L2 归一化配套（架构 §10.2 的"距离度量"兼容信息）。
DISTANCE_SPACE = "cosine"

#: **Chroma 自己读取**的距离度量元数据键（实测 1.5.9）。
#: 只写我们自己的 ``embedding_distance`` 是不生效的：Chroma 会退回默认的 ``l2``，
#: 于是"1 - distance"换算出来的根本不是余弦相似度（2026-09-15 复审抓到的真问题）。
CHROMA_SPACE_METADATA_KEY = "hnsw:space"

#: collection 元数据里记录的本模块结构版本，供将来迁移判断。
#:
#: 2 = 修正距离度量（显式设置 ``hnsw:space``）之后的版本：1 建的索引可能按默认 L2 建过，
#: 其向量与分数不可信，必须报"重建索引"而不是继续复用。
MEMORY_SCHEMA_VERSION = 2

#: 写入 Chroma metadata 的可选字段（值为 ``None`` 时**不写**，架构 §10.2 末段）。
_OPTIONAL_METADATA_FIELDS = (
    "user_id",
    "project_id",
    "workspace_root",
    "source_thread_id",
    "source_event_id",
    "last_accessed_at",
    "expires_at",
)


@dataclass(frozen=True)
class MediumTermHit:
    """一次查询命中的原始结果（打分与过滤在 recall 层，本层不解释分数）。"""

    memory_id: str
    content: str
    metadata: dict[str, Any]
    distance: float


@dataclass(frozen=True)
class MediumTermStore:
    """已打开的 Chroma collection 及其 embedding 适配器。

    生命周期由调用方管理（进程内复用见 ``graph.nodes`` 的商店缓存）；
    Chroma 的 ``PersistentClient`` 没有 ``close()``，同一个路径重复创建是安全的（实测）。
    """

    client: Any
    collection: Any
    path: Path
    collection_name: str
    embedding: EmbeddingFunction

    @property
    def info(self) -> Any:
        """embedding 适配器的元信息（provider / 模型 / 维度 / 归一化）。"""
        return self.embedding.info

    def count(self) -> int:
        """当前 collection 里的记忆条数。"""
        return int(self.collection.count())

    def embed_query(self, text: str) -> list[float]:
        """把查询文本转成向量（走适配器的查询路径，含 L2 归一化）。"""
        vectors = self.embedding.embed_query(text)
        return vectors[0] if vectors else []

    def upsert(self, records: Sequence[MediumTermRecord]) -> int:
        """写入或覆盖记忆（同 ``id`` 覆盖），返回写入条数。

        Args:
            records: 待写入记录；可选元数据字段为 ``None`` 时不会写进向量库记录。
                记录里自带的 ``embedding`` 只在"与当前适配器算出来的一致"时才接受
                （见 :meth:`_embeddings_for`）。

        Returns:
            实际提交的条数。

        Raises:
            MemoryStoreError: 写入被拒绝（自带向量来源不明、维度与现有 collection 不一致等）。
        """
        items = list(records)
        if not items:
            return 0
        embeddings = self._embeddings_for(items)  # 一律由当前适配器计算，见 _embeddings_for
        try:
            self.collection.upsert(
                ids=[record.id for record in items],
                embeddings=embeddings,
                documents=[record.content for record in items],
                metadatas=[_record_metadata(record) for record in items],
            )
        except Exception as exc:  # 统一包装成带重建提示的可读错误
            raise MemoryStoreError(self._failure_text("写入", exc)) from exc
        return len(items)

    def query(
        self,
        *,
        query_vector: Sequence[float],
        scope_keys: Sequence[tuple[MemoryScope, str]],
        n_results: int,
    ) -> list[MediumTermHit]:
        """按作用域精确过滤后取相似度最高的候选。

        Args:
            query_vector: 查询向量（应已归一化，与 collection 的距离度量配套）。
            scope_keys: 允许的 ``(scope, scope_key)`` 组合；空表示无可召回作用域。
            n_results: 最多取多少条候选。

        Returns:
            命中的原始结果；collection 为空时返回空列表。

        Raises:
            MemoryStoreError: 查询被拒绝（例如 collection 的向量维度与查询不一致）。
        """
        if not scope_keys or n_results <= 0:
            return []
        try:
            result = self.collection.query(
                query_embeddings=[list(query_vector)],
                n_results=max(1, n_results),
                where=_scope_where(scope_keys),
                include=["metadatas", "documents", "distances"],
            )
        except Exception as exc:  # 统一包装成带重建提示的可读错误
            raise MemoryStoreError(self._failure_text("查询", exc)) from exc
        return _hits_from_result(result)

    def list_records(
        self, *, scope_keys: Sequence[tuple[MemoryScope, str]], limit: int = 20
    ) -> list[MediumTermHit]:
        """按**作用域**列出记忆，供人工查看与删除（不做相似度计算）。

        为什么需要它：:meth:`query` 必须给定查询向量，人工排查"库里到底有什么"时无从下手；
        :meth:`expired_ids` 又只给 id。但列出必须与召回同口径——过滤比 :meth:`query` 少一项，人工
        列表就会把别的项目、已过期的记录列出来（复审实证：当前工作区列出了其它项目的记录）。因此
        作用域条件复用 ``_scope_where``（含 ``status=active``），过期项在取回后排除；因为过滤发生在
        取回之后，这里按 ``limit`` 的若干倍多取一些再截断。

        ``distance`` 在列出场景没有意义（不是检索），统一置 ``1.0``。
        """
        if limit <= 0 or not scope_keys:
            return []
        try:
            result = self.collection.get(
                limit=max(1, limit) * _LIST_OVERFETCH,
                where=_scope_where(scope_keys),
                include=["metadatas", "documents"],
            )
        except Exception as exc:  # 统一包装成带重建提示的可读错误
            raise MemoryStoreError(self._failure_text("列出", exc)) from exc
        moment = datetime.now(timezone.utc)
        visible = [hit for hit in _hits_from_get(result) if not _hit_expired(hit, moment)]
        return visible[:limit]

    def find_record(self, memory_id: str) -> MediumTermHit | None:
        """按 id 取一条记忆（人工删除前的**作用域校验**用），没有则 ``None``。

        只看 id，不做作用域与状态过滤：调用方需要先拿到记录的作用域，才能判断"该不该允许删除"。
        """
        try:
            result = self.collection.get(ids=[memory_id], include=["metadatas", "documents"])
        except Exception as exc:  # 统一包装成带重建提示的可读错误
            raise MemoryStoreError(self._failure_text("查询", exc)) from exc
        hits = _hits_from_get(result)
        return hits[0] if hits else None

    def delete(self, memory_ids: Sequence[str]) -> int:
        """按 id 删除记忆，返回**实际删除**的条数（不存在的 id 不计入）。

        原先直接返回请求条数，于是删除一个不存在的 id 也会被报成"删除成功"（复审实证：实际删除
        0 条、返回 1 条）。人工入口要给出可信结果，所以先核实存在性再删。
        """
        items = list(dict.fromkeys(str(item) for item in memory_ids))
        if not items:
            return 0
        try:
            existing = [
                str(item) for item in (self.collection.get(ids=items, include=[])["ids"] or [])
            ]
            if not existing:
                return 0
            self.collection.delete(ids=existing)
        except Exception as exc:  # 统一包装成带重建提示的可读错误
            raise MemoryStoreError(self._failure_text("删除", exc)) from exc
        return len(existing)

    def expired_ids(self, *, now: datetime | None = None) -> list[str]:
        """已过期的记忆 id（``expires_at`` 有值且不晚于 ``now``），供显式遗忘任务删除。

        读不出有效期（非法值）的**不在这里删**：中期库没有版本历史、删除不可逆，宁可留到人工
        处理，也不因为一次解析失败悄悄删掉记忆。判定口径与召回层一致（``expires_at <= now``）。
        """
        moment = now or datetime.now(timezone.utc)
        try:
            result = self.collection.get(include=["metadatas"])
        except Exception as exc:  # 统一包装成带重建提示的可读错误
            raise MemoryStoreError(self._failure_text("查询过期记忆", exc)) from exc
        ids = [str(item) for item in (result.get("ids") or [])]
        metadatas = result.get("metadatas") or []
        expired: list[str] = []
        for index, memory_id in enumerate(ids):
            metadata = dict(metadatas[index]) if index < len(metadatas) else {}
            expiry = _parse_moment(metadata.get("expires_at"))
            if expiry is not None and expiry <= moment:
                expired.append(memory_id)
        return expired

    def _embeddings_for(self, records: Sequence[MediumTermRecord]) -> list[list[float]]:
        """向量一律由**当前 store 的适配器**计算。

        调用方自带的 ``record.embedding`` 只在"与适配器算出来的一致"时才接受（等价于冗余），
        否则拒绝：同维度但来自另一个模型的向量会悄悄混进 collection，而 collection 元数据
        完全检测不到（架构 §10.2 明确禁止混用不同模型产生的向量）。
        """
        computed = [
            _as_float_list(vector)
            for vector in self.embedding.embed_documents([record.content for record in records])
        ]
        _reject_foreign_vectors(records, computed)
        return computed

    def _failure_text(self, action: str, error: BaseException) -> str:
        detail = str(error).strip() or type(error).__name__
        return (
            f"中期记忆{action}失败：collection={self.collection_name}；"
            f"embedding={self.info.model}；原因：{detail}。"
            "如果最近更换了 embedding 模型（或向量维度），同一 collection 里的旧向量不再可比，"
            f"请删除该 collection 或整个数据目录（{self.path.as_posix()}）后重建索引。"
        )


def chromadb_available() -> bool:
    """``chromadb`` 是否已安装（**不导入**，因此不会触发任何副作用）。"""
    return importlib.util.find_spec("chromadb") is not None


def expected_collection_metadata(embedding: EmbeddingFunction) -> dict[str, str | int | bool]:
    """计算 collection 应记录的 Embedding 兼容信息（架构 §10.2）。

    至少包含 provider、模型标识、向量维度、归一化方式与距离度量。维度未知时**主动探测一次**
    （``resolve_dimension``）——"不混用不同模型的向量"这条要求没有维度就无从校验；API provider
    还会带上端点身份（同名模型在不同端点可能是完全不同的向量空间）。
    """
    metadata: dict[str, str | int | bool] = dict(embedding.info.collection_metadata())
    metadata["memory_schema_version"] = MEMORY_SCHEMA_VERSION
    # Chroma 认的键；必须写它，否则 collection 会按默认 l2 建，后面的相似度换算全是错的
    metadata[CHROMA_SPACE_METADATA_KEY] = DISTANCE_SPACE
    dimension = embedding.info.dimension
    if dimension is None:
        dimension = embedding.resolve_dimension()
    if dimension is None:
        raise MemoryStoreError(
            f"无法确定 embedding 维度（provider={embedding.info.provider}，"
            f"model={embedding.info.model}）：中期记忆需要维度来校验 collection 兼容性"
        )
    metadata["embedding_dimension"] = dimension
    return metadata


def open_store(
    config: MediumTermMemoryConfig, *, embedding_function: EmbeddingFunction | None = None
) -> MediumTermStore:
    """按配置打开中期向量库（持久化到 ``config.path``）。

    前置条件按"配置 → 依赖"的顺序检查，两类错误都不需要真的导入 ``chromadb``，
    因此未装可选依赖时也能看到准确的原因。

    Args:
        config: ``memory.medium_term`` 配置节。
        embedding_function: 文本转向量的适配器（T14A 的 ``build_embedding_function``）。
            必须显式提供：``None`` 时会拒绝，而不是让 Chroma 使用自带默认模型。

    Returns:
        已打开的 :class:`MediumTermStore`。

    Raises:
        MemoryDisabledError: ``config.enabled`` 为 ``false``。
        MemoryStoreError: 没有提供 ``embedding_function``，或现有 collection 的
            Embedding 兼容信息与当前配置不一致（会提示重建索引）。
        MemoryUnavailableError: 已启用但环境里没有 ``chromadb``。
    """
    if not config.enabled:
        raise MemoryDisabledError(
            "memory.medium_term.enabled 为 false：中期记忆未启用，不会创建向量索引"
        )
    if embedding_function is None:
        raise MemoryStoreError(EMBEDDING_REQUIRED_HINT)
    chromadb = _import_chromadb()
    path = Path(config.path)
    expected = expected_collection_metadata(embedding_function)
    client = chromadb.PersistentClient(path=str(path))
    try:
        collection = client.get_or_create_collection(
            name=config.collection, embedding_function=embedding_function, metadata=expected
        )
    except Exception as exc:  # 统一包装：Chroma 自己也会因 embedding 函数冲突拒绝复用旧 collection
        raise MemoryStoreError(
            f"打开中期记忆 collection「{config.collection}」失败：{exc}。"
            "常见原因：该 collection 是别的 embedding 函数或旧版本建的，或者数据目录损坏。"
            f"确认无用后请删除该 collection 或整个数据目录（{path.as_posix()}）后重建索引。"
        ) from exc
    # 已存在的 collection 不会被 metadata 覆盖（实测），所以这里必须自己比对
    _check_compatibility(collection, expected, path=path)
    return MediumTermStore(
        client=client,
        collection=collection,
        path=path,
        collection_name=config.collection,
        embedding=embedding_function,
    )


def _check_compatibility(
    collection: Any,
    expected: dict[str, str | int | bool],
    *,
    path: Path,
) -> None:
    """比对 collection 里记录的 Embedding 兼容信息与**实际生效**的距离度量，不一致就拒绝读写。"""
    actual = dict(collection.metadata or {})
    mismatched = [
        f"{key}：期望 {expected_value}，实际 {actual.get(key, '未记录')}"
        for key, expected_value in expected.items()
        if actual.get(key) != expected_value
    ]
    # 不信我们自己写的键，直接读 Chroma 实际生效的配置（默认 l2 会让相似度换算全错）
    actual_space = configured_distance_space(collection)
    if actual_space != DISTANCE_SPACE:
        mismatched.append(
            f"距离度量：期望 {DISTANCE_SPACE}，实际 {actual_space or '未设置（Chroma 默认 l2）'}"
        )
    if not mismatched:
        return
    detail = "；".join(mismatched)
    raise MemoryStoreError(
        f"中期记忆 collection「{collection.name}」与当前 embedding 配置不兼容：{detail}。"
        "同一个 collection 不能混用不同模型、不同维度或不同距离度量的向量（会得到伪相关结果）。"
        f"请删除该 collection 或整个数据目录（{path.as_posix()}）后重建索引"
        f"（当前 embedding：{expected.get('embedding_provider')}/"
        f"{expected.get('embedding_model')}）。"
    )


def check_existing_collection(
    config: MediumTermMemoryConfig, info: EmbeddingInfo
) -> int | None:
    """Check an existing collection without creating a collection or probing a model.

    Unknown dimensions are omitted for static save checks; the explicit connection
    test supplies the measured dimension. Normal runtime opens still check all fields.
    """
    path = Path(config.path)
    # Chroma's persisted SQLite filename (also used by our existing memory tests).
    if not (path / "chroma.sqlite3").is_file():
        return None
    chromadb = _import_chromadb()
    client = chromadb.PersistentClient(path=str(path))
    try:
        collection = client.get_collection(name=config.collection, embedding_function=None)
    except chromadb.errors.NotFoundError:
        return None
    expected = dict(info.collection_metadata())
    expected["memory_schema_version"] = MEMORY_SCHEMA_VERSION
    expected[CHROMA_SPACE_METADATA_KEY] = DISTANCE_SPACE
    _check_compatibility(collection, expected, path=path)
    return int(collection.count())


def configured_distance_space(collection: Any) -> str:
    """读 Chroma **实际生效**的距离度量（不是我们写进 metadata 的那个键）。"""
    configuration = getattr(collection, "configuration", None)
    if isinstance(configuration, dict):
        hnsw = configuration.get("hnsw")
        if isinstance(hnsw, dict):
            return str(hnsw.get("space") or "")
    return ""


def _scope_where(scope_keys: Sequence[tuple[MemoryScope, str]]) -> dict[str, Any]:
    """构造"这些作用域里处于 active 状态"的 where 条件。"""
    clauses: list[dict[str, Any]] = [
        {
            "$and": [
                {"scope": {"$eq": scope}},
                {"scope_key": {"$eq": scope_key}},
                {"status": {"$eq": MEDIUM_TERM_STATUS_ACTIVE}},
            ]
        }
        for scope, scope_key in scope_keys
    ]
    if len(clauses) == 1:
        return clauses[0]
    return {"$or": clauses}


def _record_metadata(record: MediumTermRecord) -> dict[str, Any]:
    """把记录映射成 Chroma metadata：可选字段无值时不写（不能写 ``None``）。

    ``origin`` 放进**固定键**（T52）：它永远有值，而且召回侧要按它决定"能不能自动进 Prompt"
    （架构 §16.2）。加这个键**不** bump :data:`MEMORY_SCHEMA_VERSION`：它不改向量语义，
    老 collection 照旧可用（老记录没有该键，读取时按 ``LEGACY_ORIGIN`` 处理），
    那些版本号只用于"距离度量变了必须重建索引"这类真正影响向量可信度的变更。
    """
    metadata: dict[str, Any] = {
        "scope": record.scope,
        "scope_key": record.scope_key,
        "importance": float(record.importance),
        "confidence": float(record.confidence),
        "created_at": record.created_at,
        "status": record.status,
        "origin": record.origin,
    }
    for name in _OPTIONAL_METADATA_FIELDS:
        value = getattr(record, name)
        if value:
            metadata[name] = value
    return metadata


#: 比较调用方自带向量时的容差。
_VECTOR_TOLERANCE = 1e-6


def _as_float_list(vector: Any) -> list[float]:
    """把 numpy 数组或任意序列转成 float 列表。"""
    values = vector.tolist() if hasattr(vector, "tolist") else vector
    return [float(value) for value in values]


def _reject_foreign_vectors(
    records: Sequence[MediumTermRecord], computed: Sequence[list[float]]
) -> None:
    """拒绝来源不明的预计算向量（同维度也可能来自另一个模型）。"""
    for record, vector in zip(records, computed):
        provided = record.embedding
        if provided is None:
            continue
        differs = len(provided) != len(vector) or any(
            abs(float(left) - float(right)) > _VECTOR_TOLERANCE
            for left, right in zip(provided, vector)
        )
        if differs:
            raise MemoryStoreError(
                f"拒绝写入记录「{record.id}」：它自带的向量与当前 embedding 适配器算出的不一致。"
                "中期记忆不允许混用不同模型产生的向量（同维度也检测不出来），"
                "请去掉自带的 embedding，交给适配器统一计算。"
            )


def _parse_moment(value: Any) -> datetime | None:
    """解析 ISO-8601 时间字符串；解析不了返回 ``None``（与 forgetting 的口径一致）。"""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _hits_from_result(result: Any) -> list[MediumTermHit]:
    """把 Chroma 的查询结果解析成命中列表（空结果安全返回空列表）。"""
    ids = (result.get("ids") or [[]])[0] or []
    if not ids:
        return []
    documents = (result.get("documents") or [[]])[0] or []
    metadatas = (result.get("metadatas") or [[]])[0] or []
    distances = (result.get("distances") or [[]])[0] or []
    hits: list[MediumTermHit] = []
    for index, memory_id in enumerate(ids):
        hits.append(
            MediumTermHit(
                memory_id=str(memory_id),
                content=str(documents[index]) if index < len(documents) else "",
                metadata=dict(metadatas[index]) if index < len(metadatas) else {},
                distance=float(distances[index]) if index < len(distances) else 1.0,
            )
        )
    return hits


#: 人工列出时按 ``limit`` 多取几倍：过期过滤发生在取回之后，不多取会被提前截断。
_LIST_OVERFETCH = 4


def _hit_expired(hit: MediumTermHit, now: datetime) -> bool:
    """中期记忆是否已过期（与召回层 ``_is_expired`` 同一口径：读不出来按已过期处理）。"""
    expires_at = hit.metadata.get("expires_at")
    if not expires_at:
        return False
    moment = _parse_moment(expires_at)
    return True if moment is None else moment <= now


def _hits_from_get(result: Any) -> list[MediumTermHit]:
    """把 Chroma ``get`` 的结果解析成命中列表（形状与查询结果不同：字段是**扁平**列表）。"""
    ids = list(result.get("ids") or [])
    documents = list(result.get("documents") or [])
    metadatas = list(result.get("metadatas") or [])
    hits: list[MediumTermHit] = []
    for index, memory_id in enumerate(ids):
        document = documents[index] if index < len(documents) else None
        metadata = metadatas[index] if index < len(metadatas) else None
        hits.append(
            MediumTermHit(
                memory_id=str(memory_id),
                content=str(document) if document is not None else "",
                metadata=dict(metadata) if metadata else {},
                distance=1.0,  # 列出不是检索：距离没有意义
            )
        )
    return hits


def _import_chromadb() -> Any:
    """延迟导入 ``chromadb``；缺少时转成带安装提示的领域异常。"""
    try:
        import chromadb
    except ImportError as exc:
        raise MemoryUnavailableError(CHROMADB_INSTALL_HINT) from exc
    return chromadb

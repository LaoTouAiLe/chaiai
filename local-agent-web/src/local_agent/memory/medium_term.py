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
from pathlib import Path
from typing import Any

from local_agent.config import MediumTermMemoryConfig
from local_agent.exceptions import MemoryDisabledError, MemoryStoreError, MemoryUnavailableError
from local_agent.llm.embedding import EmbeddingFunction
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

    def delete(self, memory_ids: Sequence[str]) -> int:
        """按 id 删除记忆（遗忘的判定在 T18，这里只提供原语）。"""
        items = list(memory_ids)
        if not items:
            return 0
        try:
            self.collection.delete(ids=items)
        except Exception as exc:  # 统一包装成带重建提示的可读错误
            raise MemoryStoreError(self._failure_text("删除", exc)) from exc
        return len(items)

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
    _check_compatibility(collection, expected, path=path, embedding=embedding_function)
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
    embedding: EmbeddingFunction,
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
        f"（当前 embedding：{embedding.info.provider}/{embedding.info.model}）。"
    )


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
    """把记录映射成 Chroma metadata：可选字段无值时不写（不能写 ``None``）。"""
    metadata: dict[str, Any] = {
        "scope": record.scope,
        "scope_key": record.scope_key,
        "importance": float(record.importance),
        "confidence": float(record.confidence),
        "created_at": record.created_at,
        "status": record.status,
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


def _import_chromadb() -> Any:
    """延迟导入 ``chromadb``；缺少时转成带安装提示的领域异常。"""
    try:
        import chromadb
    except ImportError as exc:
        raise MemoryUnavailableError(CHROMADB_INSTALL_HINT) from exc
    return chromadb

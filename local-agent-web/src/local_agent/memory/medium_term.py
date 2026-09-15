"""中期向量记忆的惰性适配层（架构 §10.2，开发文档 T13）。

``chromadb`` 是可选依赖（``pyproject.toml`` 的 ``memory`` extra）。本模块因此**不在导入期**
碰它，只有真正要打开向量库时才导入：

- ``memory.medium_term.enabled = false`` → 不导入 ``chromadb``、不建目录、不建 collection，
  抛 :class:`MemoryDisabledError`，调用方（节点）按"关闭即透传"处理；
- 已启用但没装 extra → 抛 :class:`MemoryUnavailableError`，消息里给出 ``uv sync --extra memory``，
  而不是裸 ``ImportError``；
- 已启用但没给 embedding 函数 → 抛 :class:`MemoryStoreError`，避免 Chroma 退回到自带默认模型
  （那会联网下载模型，与"本机、离线优先"的假设冲突）。

尚未核实：``chromadb.PersistentClient`` / ``get_or_create_collection`` 的调用签名需要按安装版本
确认（开发文档 §2：不把未核实的 API 写成既定事实）。本机 ``chromadb`` 未安装，所以 T13 只保证
"未安装/已关闭"两条路径可测；真正打开向量库的路径由 T14 在 ``uv sync --extra memory`` 之后核实。

T13 只做适配层，不含写入与召回（那是 T14）；字段契约见 :mod:`local_agent.memory.schemas`。
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.config import MediumTermMemoryConfig
from local_agent.exceptions import MemoryDisabledError, MemoryStoreError, MemoryUnavailableError

#: 缺依赖时的安装提示（消息面向使用者，必须能照做）。
CHROMADB_INSTALL_HINT = "未安装 chromadb：请先执行 `uv sync --extra memory` 安装可选依赖"

#: 缺 embedding 时的配置提示。
EMBEDDING_REQUIRED_HINT = (
    "未提供 embedding 函数：中期记忆需要 embedding.* 配置（base_url / api_key_env / model），"
    "并且不使用 Chroma 自带的默认模型"
)


@dataclass(frozen=True)
class MediumTermStore:
    """已打开的 Chroma collection 的薄封装。

    只持有句柄，不实现读写；T14 在此基础上加写入、召回与排序。
    """

    client: Any
    collection: Any
    path: Path
    collection_name: str


def chromadb_available() -> bool:
    """``chromadb`` 是否已安装（**不导入**，因此不会触发任何副作用）。"""
    return importlib.util.find_spec("chromadb") is not None


def open_store(
    config: MediumTermMemoryConfig, *, embedding_function: Any | None = None
) -> MediumTermStore:
    """按配置打开中期向量库（持久化到 ``config.path``）。

    前置条件按"配置 → 依赖"的顺序检查，两类错误都不需要真的导入 ``chromadb``，
    因此未装可选依赖时也能看到准确的原因。

    Args:
        config: ``memory.medium_term`` 配置节。
        embedding_function: 把文本转成向量的可调用对象（由 T14 用 ``embedding.*`` 配置构造）。
            必须显式提供：``None`` 时会拒绝，而不是让 Chroma 使用自带默认模型。

    Returns:
        已打开的 :class:`MediumTermStore`。

    Raises:
        MemoryDisabledError: ``config.enabled`` 为 ``false``。
        MemoryStoreError: 没有提供 ``embedding_function``。
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
    client = chromadb.PersistentClient(path=str(path))
    collection = client.get_or_create_collection(
        name=config.collection, embedding_function=embedding_function
    )
    return MediumTermStore(
        client=client,
        collection=collection,
        path=path,
        collection_name=config.collection,
    )


def _import_chromadb() -> Any:
    """延迟导入 ``chromadb``；缺少时转成带安装提示的领域异常。"""
    try:
        import chromadb
    except ImportError as exc:
        raise MemoryUnavailableError(CHROMADB_INSTALL_HINT) from exc
    return chromadb

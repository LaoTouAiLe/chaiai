"""显式 Embedding 适配器（架构 §10.2，开发文档 T14A）。

职责：把 ``embedding.*`` 配置变成一个"文本 → 向量"的可调用对象，供中期记忆使用。
本模块**与 Chroma 无关**（不导入 ``chromadb``）：记忆层关闭时连 ``fastembed`` 也不会导入。

两个 provider：

- ``onnx_local``（v0.2 默认）：FastEmbed 在本机 CPU 上执行 ONNX 推理。模型权重只落在配置的
  ``cache_dir``（绝对路径），既不入 Git 也不进项目源码目录；``local_files_only=true`` 时
  禁止任何下载，缺模型直接失败。模型实例**按进程惰性创建并复用**——首次构造可能下载，
  所以不能发生在页面启动或配置加载阶段。
- ``openai_compatible``：保留 OpenAI 兼容 Embedding API 接入，Key 由环境变量提供。

已核实的 FastEmbed 事实（0.8.0；``pyproject.toml`` 因此按小版本锁定）：

- ``TextEmbedding(model_name=..., cache_dir=..., local_files_only=..., lazy_load=...)``：
  ``local_files_only`` 经 ``**kwargs`` 传到文本模型基类后确实生效（实测断网可加载）。
- ``embed(documents)`` / ``query_embed(query)`` 返回 numpy 数组的**可迭代对象**（要 ``list()``）。
- ``list_supported_models()`` 给出 ``dim`` 等元数据且**不触发下载**，本模块用它填
  :attr:`EmbeddingInfo.dimension`。
- 未显式给 ``cache_dir`` 时默认目录跟随临时目录（``define_cache_dir(None)``），所以本项目
  强制要求绝对 ``cache_dir``，避免权重落到系统盘。
- 缺模型且 ``local_files_only=True`` 时 FastEmbed 只抛
  ``ValueError("Could not load model ... from any source.")``，本模块包装成带 ``cache_dir``
  与"下一步怎么办"的 :class:`~local_agent.exceptions.EmbeddingLoadError`。

归一化：两种 provider 的输出都由本模块做 L2 归一化，因此 :attr:`EmbeddingInfo.normalized`
恒为 ``True``，不依赖具体模型是否自带归一化（写进 collection 元数据时才有意义，架构 §10.2）。

Chroma 协议（T14B 实测 1.5.9，三条都是硬要求）：

1. 必须有 ``name()``——只有 ``__call__`` 会直接 ``AttributeError``；
2. ``__call__`` 的**参数名必须叫 ``input``**——Chroma 逐个比对 ``__call__`` 的参数名，
   叫 ``texts`` 之类会直接 ``ValueError``；
3. 持久化 collection 配置时读 ``get_config()``。

三个成员都不导入 ``chromadb``，返回内容也不含任何密钥（``openai_compatible`` 只给 model/base_url）。
距离度量是 collection 创建参数，不在本模块。

**已知的 DeprecationWarning**：Chroma 把"只实现 ``__call__`` + ``name`` + ``get_config``"的 EF 判为
legacy 并告警。实测这条路是**半截协议**——补上 ``is_legacy()`` 后它接着要 ``default_space``，后面还有
``supported_spaces`` / ``validate_config`` / ``build_from_config``。本任务只接 legacy 路径（功能正常，
仅一条告警）；要接非 legacy 协议需要单独一次改动 + 契约测试。
"""

from __future__ import annotations

import importlib.util
import math
import os
import re
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from local_agent.config import ConfigError, EmbeddingConfig, redact_secrets
from local_agent.exceptions import EmbeddingLoadError, EmbeddingUnavailableError

#: 缺少可选依赖时的安装提示（消息面向使用者，必须能照做）。
FASTEMBED_INSTALL_HINT = "未安装 fastembed：请先执行 `uv sync --extra memory` 安装可选依赖"

#: HuggingFace 附属缓存的环境变量：不显式设置时 huggingface_hub 会写**用户默认缓存目录**
#: （Windows 上就是 C 盘），它**不受 FastEmbed 的 cache_dir 管**。实测默认值：
#: ``HF_HUB_CACHE=%USERPROFILE%\.cache\huggingface\hub``、``HF_XET_CACHE=…\xet``。
_HF_CACHE_ENV = ("HF_HOME", "HF_HUB_CACHE", "HF_XET_CACHE")

#: 环境变量 → 相对 ``cache_dir`` 的落点（同一份布局也用于文档与页面展示）。
_HF_CACHE_LAYOUT = {
    "HF_HOME": "hf-home",
    "HF_HUB_CACHE": "hf-home/hub",
    "HF_XET_CACHE": "hf-home/xet",
}

#: 复核时要比对的 ``huggingface_hub.constants`` 成员（含 agent 探测缓存）。
_HF_CONSTANTS = ("HF_HOME", "HF_HUB_CACHE", "HF_XET_CACHE", "AGENT_HARNESSES_PATH")

#: 探测向量维度时用的占位文本（只在维度未知时才真的调用模型）。
_DIMENSION_PROBE_TEXT = "维度探测"


@dataclass(frozen=True)
class HfCachePlan:
    """HuggingFace 附属缓存的实际落点，以及被本项目覆盖掉的外部设置。"""

    #: 环境变量名 → 实际写入的路径（全部位于 ``cache_dir`` 之下）。
    paths: dict[str, str]
    #: 被覆盖的外部设置，形如 ``HF_HOME=E:/elsewhere``（供页面与日志展示）。
    conflicts: tuple[str, ...]


def prepare_hf_cache(cache_dir: Path) -> HfCachePlan:
    """把 HuggingFace 的附属缓存钉在显式 ``cache_dir`` 下（配置是唯一真值）。

    **一律覆盖** ``HF_HOME`` / ``HF_HUB_CACHE`` / ``HF_XET_CACHE``（不再用 ``setdefault``）：
    外部环境变量一旦生效，附属缓存就会落到 ``cache_dir`` 之外——最典型是用户目录（Windows 上
    即 C 盘），而且页面展示的目标目录会与实际落点不符。被覆盖掉的外部值记在
    :attr:`HfCachePlan.conflicts` 里，便于展示与排查。

    必须在**导入 ``huggingface_hub`` 之前**调用：该库在导入时就把这些路径读成模块常量，之后再改
    环境变量对已固化的常量无效。真正加载模型前由 :func:`ensure_hf_cache_effective` 复核。

    Args:
        cache_dir: ``onnx_local`` 配置的模型缓存目录。

    Returns:
        实际写入的环境变量值（都在 ``cache_dir`` 下）与被覆盖的外部设置。
    """
    paths: dict[str, str] = {}
    conflicts: list[str] = []
    for name, suffix in _HF_CACHE_LAYOUT.items():
        value = (cache_dir / suffix).as_posix()
        current = os.environ.get(name)
        if current is not None and current != value:
            conflicts.append(f"{name}={current}")
        os.environ[name] = value
        paths[name] = value
    return HfCachePlan(paths=paths, conflicts=tuple(conflicts))


def ensure_hf_cache_effective(cache_dir: Path) -> None:
    """复核 ``huggingface_hub`` 实际生效的缓存常量都落在 ``cache_dir`` 下。

    ``huggingface_hub`` 一旦被本进程导入，缓存常量就按**当时的**环境变量固化了；之后再改环境
    变量也搬不回来（实测：先导入再设置时 ``AGENT_HARNESSES_PATH`` 仍指向用户目录）。这种"配置
    管不住实际落点"的情况必须明确失败，而不是让模型加载把附属缓存写到配置之外（架构 §11.6：
    不允许静默回退）。

    Args:
        cache_dir: ``onnx_local`` 配置的模型缓存目录。

    Raises:
        EmbeddingLoadError: 常量指向 ``cache_dir`` 之外，且本次调用无法修正。
    """
    constants = _hf_constants()
    if constants is None:
        return  # 还没导入 → 环境变量仍然有效，无需复核
    prefix = cache_dir.as_posix()
    stale = [
        f"{name}={value}"
        for name in _HF_CONSTANTS
        if isinstance(value := getattr(constants, name, None), str)
        and value
        and not _is_under(value, prefix)
    ]
    if not stale:
        return
    raise EmbeddingLoadError(
        "HuggingFace 缓存已在本进程导入时固化，无法改到 embedding.cache_dir 下："
        + "、".join(stale)
        + f"。请保存新缓存目录后重启后端；或把 HF_HOME / HF_HUB_CACHE / HF_XET_CACHE 指到 {prefix} 下，"
        "或确保在导入 fastembed / huggingface_hub 之前先构造 Embedding 适配器"
        "（本机不允许把缓存写到配置之外的位置）。"
    )


def _is_under(value: str, prefix: str) -> bool:
    """路径是否**真的**位于 ``prefix`` 之下（比较规范化后的绝对路径）。

    不能用"统一转小写 + 字符串前缀"：POSIX 上大小写不同就是不同目录（``/tmp/Cache/hub`` 不在
    ``/tmp/cache`` 下），而 ``/tmp/cache/../outside`` 也能靠字符串前缀逃逸——两种误判都会让缓存
    落点校验把"其实在配置目录之外"当成安全。这里先 :meth:`pathlib.Path.resolve` 归一化再比较，
    分隔符与大小写按当前平台语义处理（Windows 不敏感、POSIX 敏感）。

    Args:
        value: 待判断的路径（例如 ``huggingface_hub`` 的缓存常量）。
        prefix: 允许的父目录（例如 ``embedding.cache_dir``）。

    Returns:
        归一化后 ``value`` 等于 ``prefix`` 或位于其下；路径非法时返回 ``False``（失败闭合）。
    """
    try:
        return Path(value).resolve(strict=False).is_relative_to(
            Path(prefix).resolve(strict=False)
        )
    except (OSError, ValueError):
        return False


def _hf_constants() -> Any | None:
    """取 ``huggingface_hub.constants``；没导入时返回 ``None``。

    先看包属性，再回退 ``sys.modules``：``huggingface_hub`` 1.31 的惰性 ``__getattr__`` 不认
    ``constants`` 这个名字（属性访问会抛 ``AttributeError``），而子模块导入后会出现在
    ``sys.modules`` 里。
    """
    module = sys.modules.get("huggingface_hub")
    if module is not None:
        constants = getattr(module, "constants", None)
        if constants is not None:
            return constants
    return sys.modules.get("huggingface_hub.constants")


@dataclass(frozen=True)
class EmbeddingInfo:
    """一次嵌入的元信息，用于写进 collection 并拒绝不兼容的旧索引（架构 §10.2）。"""

    provider: str
    model: str
    #: 向量维度；未知时为 ``None``（要写 collection 元数据时先调 :meth:`resolve_dimension`）。
    dimension: int | None
    #: 输出是否已 L2 归一化。本适配器恒为 ``True``。
    normalized: bool
    #: ``onnx_local`` 的模型缓存目录；``openai_compatible`` 为 ``None``。
    cache_dir: str | None
    local_files_only: bool
    #: API provider 的端点标识（不是密钥）；``onnx_local`` 为 ``None``。
    #: 同一个模型名在不同兼容端点上可能代表完全不同的向量空间，所以兼容身份必须带上它。
    endpoint: str | None = None

    def collection_metadata(self) -> dict[str, str | int | bool]:
        """可写入 collection metadata 的键值（维度未知时不写该键）。

        "距离度量"由 :mod:`local_agent.memory.medium_term` 负责写进 Chroma 认的键，
        因为它是 collection 创建参数，不属于 embedding 本身。
        """
        metadata: dict[str, str | int | bool] = {
            "embedding_provider": self.provider,
            "embedding_model": self.model,
            "embedding_normalized": self.normalized,
        }
        if self.dimension is not None:
            metadata["embedding_dimension"] = self.dimension
        if self.endpoint:
            metadata["embedding_endpoint"] = self.endpoint
        return metadata


class EmbeddingFunction(Protocol):
    """本项目对 embedding 可调用对象的最小约定（含 Chroma 要求的 ``name`` / ``get_config``）。"""

    @property
    def info(self) -> EmbeddingInfo:
        """嵌入元信息。"""
        ...

    def name(self) -> str:
        """Chroma 用的函数标识（不含路径与密钥）。"""
        ...

    def get_config(self) -> dict[str, Any]:
        """Chroma 持久化用配置（**不含任何密钥**）。"""
        ...

    def resolve_dimension(self) -> int | None:
        """返回向量维度；拿不到就探测一次（会真的调用模型）并记住结果。"""
        ...

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        """文档向量（Chroma 的 ``embedding_function`` 契约；参数名必须是 ``input``）。"""
        ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文档向量。"""
        ...

    def embed_query(self, texts: str | Sequence[str]) -> list[list[float]]:
        """查询向量（单条或批量）。"""
        ...

    def prepare(self) -> None:
        """显式加载模型（首次可能下载）；失败抛 ``EmbeddingLoadError``。"""
        ...


class OnnxLocalEmbedding:
    """FastEmbed 的惰性包装：进程内只创建一次模型，之后复用。

    惰性：首次构造可能下载模型，页面启动/配置加载阶段不应触发。
    加锁：Streamlit 重绘可能在不同线程，重复构造会重复下载并占双份内存。
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        if config.provider != "onnx_local":
            raise EmbeddingUnavailableError(
                f"provider 为 {config.provider}，不能用本地 ONNX 适配器"
            )
        model = config.model.strip()
        if not model:
            raise EmbeddingUnavailableError(
                "onnx_local 需要 embedding.model（FastEmbed 支持的模型名，"
                "例如 BAAI/bge-small-zh-v1.5）"
            )
        cache_dir = config.cache_dir.strip()
        if not cache_dir or not Path(cache_dir).is_absolute():
            raise EmbeddingUnavailableError(
                "onnx_local 需要绝对路径的 embedding.cache_dir：模型权重只落在该目录"
            )
        self._model_name = model
        self._cache_dir = Path(cache_dir)
        self._local_files_only = config.local_files_only
        self._model: Any | None = None
        self._lock = threading.Lock()
        # 必须**最先**做：下面读模型注册表就会 import fastembed（连带 huggingface_hub），
        # 而 hf_hub 的缓存常量是导入时读环境变量的——晚一步设置，附属缓存就落到用户默认目录。
        self._hf_cache = prepare_hf_cache(self._cache_dir)
        self._dimension = _registry_dimension(model)

    @property
    def info(self) -> EmbeddingInfo:
        """嵌入元信息（不改动模型状态）。"""
        return EmbeddingInfo(
            provider="onnx_local",
            model=self._model_name,
            dimension=self._dimension,
            normalized=True,
            cache_dir=self._cache_dir.as_posix(),
            local_files_only=self._local_files_only,
        )

    def resolve_dimension(self) -> int | None:
        """返回向量维度：先看注册表，拿不到就真的编码一次量出来（并记住）。"""
        if self._dimension is None:
            vectors = self._embed([_DIMENSION_PROBE_TEXT], query=False)
            if vectors:
                self._dimension = len(vectors[0])
        return self._dimension

    @property
    def model_name(self) -> str:
        """FastEmbed 模型名（页面展示用）。"""
        return self._model_name

    @property
    def hf_cache_paths(self) -> dict[str, str]:
        """HuggingFace 附属缓存的实际落点（都在 ``cache_dir`` 下；T19 展示用）。"""
        return dict(self._hf_cache.paths)

    @property
    def hf_cache_conflicts(self) -> tuple[str, ...]:
        """被本项目覆盖掉的外部 ``HF_*`` 设置（没有则为空；T19 可展示为提示）。"""
        return self._hf_cache.conflicts

    def name(self) -> str:
        """Chroma 要求的标识；只含 provider 与模型 slug，不含路径。"""
        return f"local_agent_{self.info.provider}_{_slug(self._model_name)}"

    def get_config(self) -> dict[str, Any]:
        """Chroma 持久化用配置（模型权重目录是公开信息，不含任何密钥）。"""
        return {
            "provider": self.info.provider,
            "model": self._model_name,
            "cache_dir": self._cache_dir.as_posix(),
            "local_files_only": self._local_files_only,
        }

    def prepare(self) -> None:
        """显式加载模型（首次可能下载）。

        Raises:
            EmbeddingLoadError: 下载、缓存或加载失败；消息含 ``cache_dir`` 与下一步提示。
            EmbeddingUnavailableError: 环境里没有 ``fastembed``（未装 ``memory`` extra）。
        """
        self._ensure_model()

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        """文档向量；满足 Chroma ``embedding_function`` 的可调用契约。"""
        return self.embed_documents(input)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """把文本转成 L2 归一化后的向量。"""
        return self._embed(list(texts), query=False)

    def embed_query(self, texts: str | Sequence[str]) -> list[list[float]]:
        """查询向量；``bge`` 系列的查询/文档前缀由 FastEmbed 内部处理。"""
        items = [texts] if isinstance(texts, str) else list(texts)
        return self._embed(items, query=True)

    def _embed(self, items: list[str], *, query: bool) -> list[list[float]]:
        if not items:
            return []
        model = self._ensure_model()
        try:
            raw = model.query_embed(items) if query else model.embed(items)
            return [_l2_normalize(_to_floats(vector)) for vector in raw]
        except Exception as exc:
            raise EmbeddingLoadError(self._failure_text(exc)) from exc

    def _ensure_model(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            # 幂等兜底（构造阶段已经设过一次）：只传 cache_dir 管不住 HuggingFace 的
            # hub/xet 附属缓存，它们会落到用户默认目录（Windows 上就是 C 盘）。
            self._hf_cache = prepare_hf_cache(self._cache_dir)
            # 真正要下载/加载之前复核一次：hf_hub 若已被提前导入，它的常量已经固化，
            # 这时必须明确失败，而不是把附属缓存写到配置之外。
            ensure_hf_cache_effective(self._cache_dir)
            text_embedding = _import_fastembed()
            try:
                self._model = text_embedding(
                    # 统一用 posix 形式：与元信息、错误消息里的 cache_dir 保持同一种写法，
                    # 免得同一个目录在日志与 metadata 里出现两种分隔符。
                    model_name=self._model_name,
                    cache_dir=self._cache_dir.as_posix(),
                    local_files_only=self._local_files_only,
                )
            except Exception as exc:
                raise EmbeddingLoadError(self._failure_text(exc)) from exc
            return self._model

    def _failure_text(self, error: BaseException) -> str:
        detail = str(error).strip() or type(error).__name__
        hint = (
            "缓存里没有该模型且 local_files_only=true（不会联网）：把 local_files_only 改成 "
            "false 允许首次下载，或把模型文件放进 cache_dir"
            if self._local_files_only
            else "请检查网络与 cache_dir 的写入权限；失败不会回退到别的模型或 provider"
        )
        return (
            f"加载本地 ONNX Embedding 模型失败：{self._model_name}；"
            f"cache_dir={self._cache_dir.as_posix()}；原因：{detail}。{hint}"
        )


class OpenAiCompatibleEmbedding:
    """OpenAI 兼容 Embedding API 适配（保留 provider，v0.2 默认不使用）。"""

    def __init__(
        self, config: EmbeddingConfig, *, api_key: str,
        request_timeout_seconds: float | None = None, max_retries: int = 2,
    ) -> None:
        if config.provider != "openai_compatible":
            raise EmbeddingUnavailableError(
                f"provider 为 {config.provider}，不能用 OpenAI 兼容适配器"
            )
        if not api_key.strip():
            raise ConfigError(
                "provider 为 openai_compatible 时必须提供 embedding API Key："
                f"用 resolve_api_key 从环境变量 {config.api_key_env} 读取"
            )
        self._model_name = config.model
        self._base_url = config.base_url
        self._api_key = api_key
        self._dimension: int | None = None
        self._embeddings = OpenAIEmbeddings(
            model=config.model, base_url=config.base_url, api_key=SecretStr(api_key),
            timeout=request_timeout_seconds, max_retries=max_retries,
            # Compatible providers can reject OpenAI token IDs and base64 output.
            # Send original strings; let the provider reject overlong input rather
            # than silently splitting with an unrelated tokenizer/model limit.
            check_embedding_ctx_length=False,
            model_kwargs={"encoding_format": "float"},
        )

    @property
    def info(self) -> EmbeddingInfo:
        """嵌入元信息；维度由 API 决定，未探测前为 ``None``（不猜）。"""
        return EmbeddingInfo(
            provider="openai_compatible",
            model=self._model_name,
            dimension=self._dimension,
            normalized=True,
            cache_dir=None,
            local_files_only=False,
            endpoint=self._base_url,
        )

    def resolve_dimension(self) -> int | None:
        """探测一次真实维度并记住：同名模型在不同端点可能是不同的向量空间。"""
        if self._dimension is None:
            vectors = self.embed_documents([_DIMENSION_PROBE_TEXT])
            if vectors:
                self._dimension = len(vectors[0])
        return self._dimension

    def name(self) -> str:
        """Chroma 要求的标识；只含 provider 与模型 slug。"""
        return f"local_agent_{self.info.provider}_{_slug(self._model_name)}"

    def get_config(self) -> dict[str, Any]:
        """Chroma 持久化用配置；**只给模型与端点，不含 API Key**。"""
        return {
            "provider": self.info.provider,
            "model": self._model_name,
            "base_url": self._base_url,
        }

    def prepare(self) -> None:
        """探测一次 API 是否可用（远端 provider 没有本地加载阶段）。"""
        self.embed_query("ping")

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        """文档向量；满足 Chroma ``embedding_function`` 的可调用契约。"""
        return self.embed_documents(input)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文档向量。"""
        items = list(texts)
        if not items:
            return []
        try:
            vectors = self._embeddings.embed_documents(items)
        except Exception as exc:
            raise EmbeddingLoadError(self._failure_text(exc)) from exc
        return [_l2_normalize([float(value) for value in vector]) for vector in vectors]

    def embed_query(self, texts: str | Sequence[str]) -> list[list[float]]:
        """查询向量（单条或批量）。"""
        items = [texts] if isinstance(texts, str) else list(texts)
        results: list[list[float]] = []
        for item in items:
            try:
                vector = self._embeddings.embed_query(item)
            except Exception as exc:
                raise EmbeddingLoadError(self._failure_text(exc)) from exc
            results.append(_l2_normalize([float(value) for value in vector]))
        return results

    def _failure_text(self, error: BaseException) -> str:
        detail = redact_secrets(str(error).strip(), (self._api_key,)) or type(error).__name__
        return (
            f"调用 OpenAI 兼容 Embedding 失败：model={self._model_name}；"
            f"base_url={self._base_url}；原因：{detail}。"
            "请检查端点、Key 与该模型是否提供 /embeddings 接口"
        )


def build_embedding_function(
    config: EmbeddingConfig, *, api_key: str | None = None,
    request_timeout_seconds: float | None = None, max_retries: int = 2,
) -> EmbeddingFunction:
    """按配置构造 embedding 可调用对象（架构 §10.2：不使用 Chroma 自带默认模型）。

    Args:
        config: ``embedding`` 配置节。
        api_key: 已解析的 Key；仅 ``openai_compatible`` 需要（与 ``build_chat_model`` 同样
            由调用方先用 :func:`local_agent.config.resolve_api_key` 取值）。
        request_timeout_seconds: API 请求超时；显式设置测试使用 30 秒，常规运行沿用默认值。
        max_retries: API 重试次数；显式设置测试为 0，常规运行沿用默认 2 次。

    Returns:
        满足 :class:`EmbeddingFunction` 的对象；模型此时**尚未**加载（``onnx_local`` 惰性）。

    Raises:
        EmbeddingUnavailableError: ``enabled=false`` 或缺 ``fastembed``。
        ConfigError: provider 与 ``api_key`` 不匹配。
    """
    if not config.enabled:
        raise EmbeddingUnavailableError(
            "embedding.enabled 为 false：未启用 embedding，调用方应先检查开关再走关闭分支"
        )
    if config.provider == "onnx_local":
        if api_key:
            raise ConfigError("provider 为 onnx_local 时不使用 API Key（embedding.api_key_env 应为空）")
        return OnnxLocalEmbedding(config)
    if api_key is None:
        raise ConfigError(
            "provider 为 openai_compatible 时需要 API Key："
            f"用 resolve_api_key 从环境变量 {config.api_key_env} 读取"
        )
    return OpenAiCompatibleEmbedding(
        config, api_key=api_key,
        request_timeout_seconds=request_timeout_seconds, max_retries=max_retries,
    )


@dataclass(frozen=True, kw_only=True)
class EmbeddingReadiness:
    """Embedding 的**准备状态**（架构 §1.4、§10.2；T19 的页面展示用）。

    只做"静态可判定"的判断：**不加载模型、不联网、不建缓存目录**，因此页面可以随时调用。

    ``state`` 取值：

    - ``disabled``：``embedding.enabled=false``；
    - ``unavailable``：缺可选依赖（``fastembed``）；
    - ``not_ready``：配置不完整（缺 ``cache_dir`` / 不是绝对路径 / 目录不可写 / 远端缺端点或 Key）
      ——**预先发现**，不用等第一次召回失败；
    - ``ready``：具备启用条件（``onnx_local`` 首次真正使用时才加载模型，可能耗时）。
    """

    state: Literal["disabled", "unavailable", "not_ready", "ready"]
    provider: str
    model: str
    cache_dir: str | None
    #: 面向用户的说明；失败时必须说清原因与下一步（架构 §11.6：不允许静默回退）。
    detail: str
    restart_required: bool = False


def fastembed_available() -> bool:
    """``fastembed``（可选依赖 ``memory``）是否已安装。

    与 :func:`local_agent.memory.medium_term.chromadb_available` 同一用途：让调用方（就绪状态、
    页面、测试）能区分"没装可选组"和"装了但就绪检查没过"，而不是靠捕获 ``ImportError``。
    """
    return importlib.util.find_spec("fastembed") is not None


def readiness(config: EmbeddingConfig, *, api_key: str | None = None) -> EmbeddingReadiness:
    """判断 embedding 是否具备启用条件；**不加载模型、不写磁盘**。

    Args:
        config: ``embedding`` 配置节。
        api_key: API provider 的 Key（``onnx_local`` 用不到）。

    Returns:
        :class:`EmbeddingReadiness`；页面据此展示模型名、目标缓存目录与当前状态。
    """
    provider = str(config.provider)
    model = str(config.model)
    cache_dir = (config.cache_dir or "").strip() or None
    if not config.enabled:
        return EmbeddingReadiness(
            state="disabled",
            provider=provider,
            model=model,
            cache_dir=cache_dir,
            detail="embedding.enabled=false：不使用 embedding，也不加载本地模型",
        )
    if provider == "openai_compatible":
        if not str(config.base_url or "").strip() or not (api_key or "").strip():
            return EmbeddingReadiness(
                state="not_ready",
                provider=provider,
                model=model,
                cache_dir=None,
                detail=(
                    "远端 embedding 需要 base_url 与 API Key"
                    f"（环境变量 {config.api_key_env}）；补全后重试"
                ),
            )
        return EmbeddingReadiness(
            state="ready",
            provider=provider,
            model=model,
            cache_dir=None,
            detail="使用远端 embedding 接口，无需本地模型",
        )
    if not fastembed_available():
        return EmbeddingReadiness(
            state="unavailable",
            provider=provider,
            model=model,
            cache_dir=cache_dir,
            detail=f"{FASTEMBED_INSTALL_HINT}（当前 provider={provider} 需要本地模型）",
        )
    if cache_dir is None:
        return EmbeddingReadiness(
            state="not_ready",
            provider=provider,
            model=model,
            cache_dir=None,
            detail="embedding.cache_dir 未配置：本地模型必须指定一个本机可写的绝对路径",
        )
    path = Path(cache_dir)
    if not path.is_absolute():
        return EmbeddingReadiness(
            state="not_ready",
            provider=provider,
            model=model,
            cache_dir=cache_dir,
            detail=f"embedding.cache_dir 必须是绝对路径（当前是 {cache_dir}）",
        )
    parent = _writable_parent(path, provider=provider, model=model)
    if isinstance(parent, EmbeddingReadiness):
        return parent
    try:
        ensure_hf_cache_effective(path)
    except EmbeddingLoadError:
        return EmbeddingReadiness(
            state="not_ready", provider=provider, model=model, cache_dir=cache_dir,
            restart_required=True,
            detail="本进程已加载其他 HuggingFace 缓存路径；新缓存目录需保存后重启后端生效。"
            "重启前不会测试加载该目录，请在重启后再次测试。",
        )
    if config.local_files_only:
        return EmbeddingReadiness(
            state="ready",
            provider=provider,
            model=model,
            cache_dir=cache_dir,
            detail=(
                "离线模式（local_files_only=true）：只验证了配置与 "
                f"{parent.as_posix()} 可写，**模型缓存尚未验证**——首次加载若缺权重会失败，"
                "那时会以错误事件出现在对话里"
            ),
        )
    return EmbeddingReadiness(
        state="ready",
        provider=provider,
        model=model,
        cache_dir=cache_dir,
        detail=(
            f"首次使用时加载本地模型「{model}」，权重与 HuggingFace 附属缓存都落在 "
            f"{path.as_posix()}（首次可能需要联网下载，之后可离线使用）"
        ),
    )


def _writable_parent(
    path: Path, *, provider: str, model: str
) -> Path | EmbeddingReadiness:
    """校验缓存目录（或其最近的存在父目录）可写。

    只检查"已存在的路径是否可写"是不够的：路径存在但不是目录、或者上级目录不可写时，页面会显示
    ``ready``，直到首次加载才发现既建不了目录也下不了模型（复审实证）。返回可写的**目录**；
    不可用时返回一条 ``not_ready`` 的 :class:`EmbeddingReadiness`。
    """
    if path.exists():
        if not path.is_dir():
            return _not_ready(path, provider=provider, model=model,
                              detail=f"缓存路径存在但不是目录：{path.as_posix()}；请删掉它或换一个目录")
        if not os.access(path, os.W_OK):
            return _not_ready(path, provider=provider, model=model,
                              detail=f"缓存目录不可写：{path.as_posix()}；请改权限或换一个本机可写目录")
        return path
    parent = next((item for item in path.parents if item.exists()), None)
    if parent is None or not parent.is_dir():
        return _not_ready(path, provider=provider, model=model,
                          detail=f"缓存目录的上级路径不存在：{path.as_posix()}；请先创建它")
    if not os.access(parent, os.W_OK):
        return _not_ready(
            path,
            provider=provider,
            model=model,
            detail=(
                f"缓存目录尚未创建，而它的上级目录不可写：{parent.as_posix()}；"
                "请改权限或换一个本机可写目录"
            ),
        )
    return parent


def _not_ready(path: Path, *, provider: str, model: str, detail: str) -> EmbeddingReadiness:
    """一条 ``not_ready`` 结果（provider/model 照常回填，页面才能显示是哪套配置的问题）。"""
    return EmbeddingReadiness(
        state="not_ready",
        provider=provider,
        model=model,
        cache_dir=str(path),
        detail=detail,
    )


def _import_fastembed() -> Any:
    """延迟导入 ``TextEmbedding``；缺少时转成带安装提示的领域异常。"""
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise EmbeddingUnavailableError(FASTEMBED_INSTALL_HINT) from exc
    return TextEmbedding


def _registry_dimension(model_name: str) -> int | None:
    """从 FastEmbed 注册表读维度（不加载模型、不下载）；拿不到时返回 ``None``。

    这里故意吞掉导入/读取失败：元信息不该因为可选依赖缺失或注册表变动就让适配器构造失败。
    """
    try:
        text_embedding = _import_fastembed()
        for entry in text_embedding.list_supported_models():
            name = str(entry.get("model") or entry.get("model_name") or "")
            if name.lower() == model_name.lower():
                dimension = entry.get("dim")
                return int(dimension) if isinstance(dimension, int) else None
    except Exception:  # noqa: BLE001 — 元信息读取失败不该拖垮适配器构造（有意吞掉）
        return None
    return None


def _to_floats(vector: Any) -> list[float]:
    """把 FastEmbed 返回的 numpy 数组（或任意序列）转成 float 列表。"""
    values = vector.tolist() if hasattr(vector, "tolist") else vector
    return [float(value) for value in values]


def _slug(text: str) -> str:
    """把模型名压成只含字母数字与连字符的 slug（供 Chroma 的 ``name()`` 使用）。"""
    return re.sub(r"[^0-9A-Za-z]+", "-", text).strip("-").lower()


def _l2_normalize(vector: list[float]) -> list[float]:
    """L2 归一化；零向量原样返回（避免除零）。

    两种 provider 都走这里，因此 :attr:`EmbeddingInfo.normalized` 是适配器给出的保证，
    而不是"相信模型自带归一化"。
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]

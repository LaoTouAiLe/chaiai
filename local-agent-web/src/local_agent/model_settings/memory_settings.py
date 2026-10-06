"""Memory resource status and explicit, non-writing embedding probes."""

from __future__ import annotations

import math
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError

from local_agent.config import AppConfig, ConfigError, EmbeddingConfig, resolve_api_key
from local_agent.exceptions import EmbeddingLoadError, MemoryStoreError
from local_agent.llm import embedding
from local_agent.memory import medium_term
from local_agent.model_settings.preferences import MemoryProbeResult, MemoryResourcesView


def api_key_for(config: EmbeddingConfig) -> str | None:
    if config.provider != "openai_compatible":
        return None
    try:
        return resolve_api_key(config)
    except ConfigError:
        return None


def resources_view(config: AppConfig) -> MemoryResourcesView:
    readiness = embedding.readiness(config.embedding, api_key=api_key_for(config.embedding))
    vector = config.memory.medium_term
    return MemoryResourcesView(
        backend=vector.backend, path=vector.path, collection=vector.collection,
        vector_available=medium_term.chromadb_available(),
        vector_initialized=(Path(vector.path) / "chroma.sqlite3").is_file(),
        embedding_state=readiness.state, embedding_detail=readiness.detail,
        key_configured=bool(api_key_for(config.embedding)),
        restart_required=readiness.restart_required,
    )


def check_configuration_compatibility(config: AppConfig) -> None:
    """No model loading/network; check known identity against existing metadata."""
    e = config.embedding
    if not e.enabled:
        return
    info = embedding.EmbeddingInfo(
        provider=e.provider, model=e.model, dimension=None, normalized=True,
        cache_dir=e.cache_dir or None, local_files_only=e.local_files_only,
        endpoint=e.base_url if e.provider == "openai_compatible" else None,
    )
    medium_term.check_existing_collection(config.memory.medium_term, info)


def validate_activation(config: AppConfig) -> None:
    if not config.memory.medium_term.enabled:
        return
    readiness = embedding.readiness(config.embedding, api_key=api_key_for(config.embedding))
    if readiness.state != "ready" and not readiness.restart_required:
        raise ValueError(readiness.detail)
    if not medium_term.chromadb_available():
        raise ValueError(medium_term.CHROMADB_INSTALL_HINT)


def probe(config: AppConfig) -> MemoryProbeResult:
    readiness = embedding.readiness(config.embedding, api_key=api_key_for(config.embedding))
    if readiness.state != "ready":
        return MemoryProbeResult(
            state="disabled" if readiness.state == "disabled" else "not_ready",
            detail=readiness.detail,
            error_category="restart_required" if readiness.restart_required else None,
        )
    if not medium_term.chromadb_available():
        return MemoryProbeResult(state="not_ready", detail=medium_term.CHROMADB_INSTALL_HINT)
    try:
        adapter = embedding.build_embedding_function(
            config.embedding, api_key=api_key_for(config.embedding),
            request_timeout_seconds=30.0, max_retries=0,
        )
        vectors = adapter.embed_query("中期记忆连接测试")
        if len(vectors) != 1:
            raise ValueError("Embedding returned an invalid batch")
        vector = vectors[0]
        if (not vector or not all(math.isfinite(value) for value in vector)
                or not any(value != 0 for value in vector)):
            raise ValueError("Embedding returned an invalid vector")
        info = adapter.info
        # API adapters may only learn dimensions while embedding the query.
        info = embedding.EmbeddingInfo(
            provider=info.provider, model=info.model, dimension=len(vector),
            normalized=info.normalized, cache_dir=info.cache_dir,
            local_files_only=info.local_files_only, endpoint=info.endpoint,
        )
        records = medium_term.check_existing_collection(config.memory.medium_term, info)
        return MemoryProbeResult(
            state="passed", dimension=len(vector), records=records,
            detail=("向量生成成功，已有集合的模型、端点、维度及距离度量兼容。"
                    if records is not None else
                    "向量生成成功；目标集合尚未初始化，首次使用中期记忆时创建。"),
        )
    except MemoryStoreError:
        return MemoryProbeResult(
            state="incompatible", detail="已有向量集合与模型、端点、维度或距离度量不兼容。"
            "请沿用匹配的模型配置；本页面不迁移、重建或清空已有数据。",
        )
    except Exception as exc:  # noqa: BLE001 - sanitize third-party model/API failures
        return _safe_probe_failure(exc, local=config.embedding.provider == "onnx_local")


def _safe_probe_failure(error: Exception, *, local: bool = False) -> MemoryProbeResult:
    """Classify typed SDK failures without exposing their bodies or credentials."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, APITimeoutError):
            return MemoryProbeResult(state="failed", error_category="timeout",
                                     detail="Embedding 请求超时，请检查端点和网络后重试；未写入记忆。")
        if isinstance(current, APIConnectionError):
            return MemoryProbeResult(state="failed", error_category="network",
                                     detail="无法连接 Embedding 接口，请检查网络和端点；未写入记忆。")
        if isinstance(current, APIStatusError):
            status = current.status_code
            category: str
            if status in (401, 403):
                category, hint = "authentication", "鉴权失败，请检查密钥环境变量和访问权限"
            elif status in (400, 413, 422):
                category, hint = "request", "接口拒绝请求，请检查模型、输入格式与供应商文本长度限制"
            elif status == 404:
                category, hint = "model_or_endpoint", "模型或接口不存在，请检查模型名称和端点"
            elif status == 429:
                category, hint = "rate_limit", "接口限流或额度不足，请稍后重试或检查供应商额度"
            else:
                category, hint = "provider", "供应商接口异常，请检查供应商服务状态后重试"
            return MemoryProbeResult.model_validate({
                "state": "failed", "error_category": category,
                "http_status": status if 400 <= status <= 599 else None,
                "detail": f"{hint}（HTTP {status}）；未写入记忆。",
            })
        current = current.__cause__ or current.__context__
    if local and isinstance(error, EmbeddingLoadError):
        return MemoryProbeResult(state="failed", error_category="local_load",
                                 detail="Embedding 加载或生成失败，请检查模型与本地缓存；未写入记忆。")
    return MemoryProbeResult(state="failed", error_category="unknown",
                             detail="Embedding 测试失败，请检查模型配置后重试；未写入记忆。")

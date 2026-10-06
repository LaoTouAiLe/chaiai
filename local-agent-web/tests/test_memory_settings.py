"""Real vector metadata compatibility; model/network probes remain deterministic."""

import json
import math
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from local_agent.config import AppConfig, EmbeddingConfig, MediumTermMemoryConfig
from local_agent.exceptions import EmbeddingLoadError, MemoryStoreError
from local_agent.llm import embedding
from local_agent.memory import medium_term
from local_agent.model_settings import memory_settings


def test_existing_collection_checks_identity_and_dimensions_without_writes(
    tmp_path: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromadb = pytest.importorskip("chromadb")
    vector_config = MediumTermMemoryConfig(enabled=True, path=str(tmp_path / "vectors"))
    embedding_config = EmbeddingConfig(
        enabled=True, model="unit-test", cache_dir=str(tmp_path / "models"), local_files_only=True,
    )
    config = app_config.model_copy(update={
        "embedding": embedding_config,
        "memory": app_config.memory.model_copy(update={"medium_term": vector_config}),
    })
    info = embedding.EmbeddingInfo(
        provider="onnx_local", model="unit-test", dimension=3, normalized=True,
        cache_dir=embedding_config.cache_dir, local_files_only=True,
    )
    metadata = info.collection_metadata() | {
        "memory_schema_version": medium_term.MEMORY_SCHEMA_VERSION,
        medium_term.CHROMA_SPACE_METADATA_KEY: medium_term.DISTANCE_SPACE,
    }
    client = chromadb.PersistentClient(path=vector_config.path)
    collection = client.create_collection(
        name=vector_config.collection, metadata=metadata, embedding_function=None,
    )
    collection.add(ids=["kept"], embeddings=[[1.0, 0.0, 0.0]], documents=["已有记忆"])
    assert medium_term.check_existing_collection(vector_config, info) == 1
    memory_settings.check_configuration_compatibility(config)
    for wrong in (replace(info, model="wrong-model"), replace(info, dimension=4)):
        with pytest.raises(MemoryStoreError, match="不兼容"):
            medium_term.check_existing_collection(vector_config, wrong)
    wrong_config = config.model_copy(update={
        "embedding": embedding_config.model_copy(update={"model": "wrong-model"}),
    })
    with pytest.raises(MemoryStoreError, match="不兼容"):
        memory_settings.check_configuration_compatibility(wrong_config)
    monkeypatch.setattr(embedding, "fastembed_available", lambda: True)
    # 本例验证集合身份/维度，按尚未导入 HuggingFace 的进程准备状态执行。
    monkeypatch.setattr(embedding, "_hf_constants", lambda: None)
    adapter = SimpleNamespace(info=info, embed_query=lambda text: [[1.0, 0.0, 0.0]])
    monkeypatch.setattr(embedding, "build_embedding_function", lambda *args, **kwargs: adapter)
    result = memory_settings.probe(config)
    assert result.state == "passed" and result.dimension == 3 and result.records == 1
    adapter.embed_query = lambda text: [[1.0, 0.0, 0.0, 0.0]]
    assert memory_settings.probe(config).state == "incompatible"
    assert collection.count() == 1 and collection.metadata == metadata


def test_uninitialized_vector_store_is_not_created_or_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_import():
        raise AssertionError("Chroma must not load for an uninitialized store")
    monkeypatch.setattr(medium_term, "_import_chromadb", unexpected_import)
    config = MediumTermMemoryConfig(path=str(tmp_path / "not-created"))
    info = embedding.EmbeddingInfo(
        provider="onnx_local", model="test", dimension=None, normalized=True,
        cache_dir=None, local_files_only=True,
    )
    assert medium_term.check_existing_collection(config, info) is None
    assert not Path(config.path).exists()


def test_runtime_embedding_error_never_contains_the_api_key() -> None:
    secret = "fake-embedding-secret-do-not-store"
    config = EmbeddingConfig(
        enabled=True, provider="openai_compatible", model="test",
        base_url="https://example.com/v1", api_key_env="TEST_EMBEDDING_KEY",
    )
    adapter = embedding.OpenAiCompatibleEmbedding(config, api_key=secret)
    assert secret not in adapter._failure_text(RuntimeError(f"Incorrect API key: {secret}"))
    assert secret not in str(adapter.get_config())


def test_missing_dependencies_and_disabled_embedding_do_not_probe(
    app_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_probe(*args, **kwargs):
        raise AssertionError("Disabled/unavailable embedding must not load")
    monkeypatch.setattr(embedding, "build_embedding_function", unexpected_probe)
    assert memory_settings.probe(app_config).state == "disabled"
    config = app_config.model_copy(update={"embedding": EmbeddingConfig(
        enabled=True, model="test", cache_dir=str(tmp_path / "models"),
    )})
    monkeypatch.setattr(embedding, "fastembed_available", lambda: False)
    assert memory_settings.probe(config).state == "not_ready"
    assert not (tmp_path / "models").exists()


def test_compatible_embedding_sends_original_strings_for_queries_and_documents() -> None:
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["encoding_format"] == "float"
        assert isinstance(payload["input"], list)
        assert all(isinstance(text, str) for text in payload["input"])
        return httpx.Response(200, json={
            "object": "list", "model": "compatible-test",
            "data": [{"object": "embedding", "index": i, "embedding": [3.0, 4.0]}
                     for i in range(len(payload["input"]))],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    config = EmbeddingConfig(enabled=True, provider="openai_compatible", model="compatible-test",
                             base_url="https://example.com/v1", api_key_env="TEST_KEY")
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        sdk = OpenAI(api_key="fake-key", base_url=config.base_url, http_client=client)
        adapter = embedding.OpenAiCompatibleEmbedding(config, api_key="fake-key")
        adapter._embeddings.client = sdk.embeddings
        queries = adapter.embed_query(["中文查询", "second query"])
        documents = adapter.embed_documents(["中文文档", "second document"])
    assert [request["input"] for request in requests] == [
        ["中文查询"], ["second query"], ["中文文档", "second document"],
    ]
    assert len(queries) == len(documents) == 2
    assert all(math.isclose(sum(value * value for value in vector), 1.0)
               for vector in queries + documents)


@pytest.mark.parametrize("query", [False, True])
def test_overlong_compatible_input_is_not_truncated_or_silently_split(query: bool) -> None:
    text = "长文本验证" * 10000
    seen = []

    def handle(request):
        seen.append(json.loads(request.content)["input"])
        return httpx.Response(400, json={"error": {"message": "input too long; fake-secret",
                                                   "type": "invalid_request_error"}})

    config = EmbeddingConfig(enabled=True, provider="openai_compatible", model="test",
                             base_url="https://example.com/v1", api_key_env="TEST_KEY")
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        sdk = OpenAI(api_key="fake-secret", base_url=config.base_url, http_client=client,
                     max_retries=0)
        adapter = embedding.OpenAiCompatibleEmbedding(config, api_key="fake-secret")
        adapter._embeddings.client = sdk.embeddings
        with pytest.raises(EmbeddingLoadError) as caught:
            adapter.embed_query(text) if query else adapter.embed_documents([text])
    assert seen == [[text]]
    result = memory_settings._safe_probe_failure(caught.value)
    assert result.error_category == "request" and result.http_status == 400
    assert "fake-secret" not in result.model_dump_json()
    assert "fake-secret" not in str(caught.value)


@pytest.mark.parametrize("status,category", [
    (400, "request"), (401, "authentication"), (403, "authentication"),
    (404, "model_or_endpoint"), (413, "request"), (422, "request"),
    (429, "rate_limit"), (500, "provider"),
])
def test_probe_http_categories_do_not_expose_raw_bodies(status: int, category: str) -> None:
    request = httpx.Request("POST", "https://example.com/v1/embeddings")
    error = APIStatusError("raw-body-secret", response=httpx.Response(status, request=request),
                           body={"error": "raw-body-secret"})
    wrapped = EmbeddingLoadError("wrapper-secret")
    wrapped.__cause__ = error
    result = memory_settings._safe_probe_failure(wrapped)
    assert result.error_category == category and result.http_status == status
    assert "secret" not in result.model_dump_json()


def test_probe_timeout_and_connection_categories_do_not_expose_urls() -> None:
    request = httpx.Request("POST", "https://secret.example.com/v1/embeddings")
    for error, category in ((APITimeoutError(request), "timeout"),
                            (APIConnectionError(request=request), "network")):
        wrapped = EmbeddingLoadError("raw-secret")
        wrapped.__cause__ = error
        result = memory_settings._safe_probe_failure(wrapped)
        assert result.error_category == category and result.http_status is None
        assert "secret" not in result.model_dump_json()


def test_changed_local_cache_reports_restart_without_loading_or_changing_environment(
    tmp_path: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = tmp_path / "old-cache" / "hf-home"
    monkeypatch.setattr(embedding, "_hf_constants", lambda: SimpleNamespace(HF_HOME=str(frozen)))
    monkeypatch.setattr(embedding, "fastembed_available", lambda: True)
    monkeypatch.setattr(medium_term, "chromadb_available", lambda: True)
    monkeypatch.setattr(embedding, "build_embedding_function",
                        lambda *args, **kwargs: pytest.fail("pending restart must not load a model"))
    config = app_config.model_copy(update={
        "embedding": EmbeddingConfig(enabled=True, model="test", cache_dir=str(tmp_path / "new-cache")),
        "memory": app_config.memory.model_copy(update={
            "medium_term": app_config.memory.medium_term.model_copy(update={"enabled": True}),
        }),
    })
    environment = dict(os.environ)
    resources = memory_settings.resources_view(config)
    assert resources.embedding_state == "not_ready" and resources.restart_required
    result = memory_settings.probe(config)
    assert result.error_category == "restart_required" and result.state == "not_ready"
    assert "重启" in result.detail
    memory_settings.validate_activation(config)  # Save a staged configuration; use it after restart.
    assert dict(os.environ) == environment
    assert not (tmp_path / "new-cache").exists()
    # A fresh backend has no frozen HF module; the saved configuration can load.
    monkeypatch.setattr(embedding, "_hf_constants", lambda: None)
    restarted = memory_settings.resources_view(config)
    assert restarted.embedding_state == "ready" and not restarted.restart_required

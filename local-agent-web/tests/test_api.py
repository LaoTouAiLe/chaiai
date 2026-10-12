"""T29：HTTP 接口层（契约 §15.1、§15.3，计划 §6.2）。

用真实应用服务 + 确定性假模型，不联网；每个用例在 `tmp_path` 下建自己的库。

覆盖：成功/错误响应、幂等与单飞、审批、工作区路径校验、Host/Origin/正文大小边界、
静态托管（缓存头、SPA 回退、路径穿越）、单实例锁、入口的单 worker、依赖方向与 OpenAPI 面。
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from local_agent import config as config_module
from local_agent import directory_picker
from local_agent.api import __main__ as api_main
from local_agent.api.app import create_app
from local_agent.api.container import ApiRuntime, instance_lock_path, open_runtime
from local_agent.api.openapi import OPENAPI_PATH, openapi_json
from local_agent.api.routes import status_for_code
from local_agent.api.security import LocalBoundaryMiddleware, _authority
from local_agent.api.sse import event_stream
from local_agent.application.errors import (
    APPROVAL_EXPIRED,
    DIRECTORY_PICKER_UNAVAILABLE,
    IDEMPOTENCY_CONFLICT,
    MODEL_SETTINGS_INVALID,
    RECOVERY_REQUIRED,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    WORKSPACE_NOT_FOUND,
)
from local_agent.application.jobs import JobRuntime
from local_agent.application.models import SCHEMA_VERSION
from local_agent.application.recovery import (
    NEEDS_REVIEW_DETAIL,
    PROVABLE_DETAIL,
    UNSTARTED_DETAIL,
)
from local_agent.application.service import EventSubscription
from local_agent.capabilities import CAPABILITY_TOOLS
from local_agent.config import AppConfig
from local_agent.persistence.job_store import (
    JOB_STATE_CANCELLED,
    JOB_STATE_COMPLETED,
    JOB_STATE_FAILED,
    JOB_STATE_NEEDS_REVIEW,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_WAITING_USER,
    JOB_STATES,
    JobStore,
)
from local_agent.security.path_guard import normalize_grant_path

ModelFactory = Callable[..., Any]
OpenApi = Callable[..., tuple[TestClient, ApiRuntime]]

WORKSPACE_NAME = "ws"
#: 允许的 Host（与 `WebConfig.allowed_hosts` 的默认值一致）：测试默认从这里发起请求，
#: 这样**默认配置本身**就是被测对象（而不是测试专用的放宽配置）。
LOCAL_BASE_URL = "http://127.0.0.1"
VITE_ORIGIN = "http://127.0.0.1:5173"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: 真实前端构建产物（`frontend/dist`，被 `.gitignore` 排除；没构建过就不存在）。
REAL_DIST = PROJECT_ROOT / "frontend" / "dist"


def _config(tmp_path: Path, app_config: AppConfig, **web_overrides: Any) -> AppConfig:
    """把示例配置改成本用例的临时路径；`web` 段按需覆盖。"""
    workspace = tmp_path / WORKSPACE_NAME
    workspace.mkdir(exist_ok=True)
    web = app_config.web.model_copy(update=web_overrides) if web_overrides else app_config.web
    return app_config.model_copy(
        update={
            "workspace": app_config.workspace.model_copy(update={"root": str(workspace)}),
            "memory": app_config.memory.model_copy(
                update={
                    "short_term": app_config.memory.short_term.model_copy(
                        update={
                            "enabled": True,
                            "path": str(tmp_path / "data" / "checkpoints.sqlite"),
                        }
                    )
                }
            ),
            "web": web,
        }
    )


@pytest.fixture
def open_api(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> Iterator[OpenApi]:
    """打开一套 API：返回 (TestClient, ApiRuntime)，退出时统一释放锁与连接。"""
    runtimes: list[ApiRuntime] = []

    def _open(
        *,
        static_dir: Path | None = None,
        base_dir: Path | None = None,
        client_base_url: str = LOCAL_BASE_URL,
        **model_kwargs: Any,
    ) -> tuple[TestClient, ApiRuntime]:
        runtime = open_runtime(
            _config(tmp_path, app_config, **model_kwargs.pop("web_overrides", {})),
            base_dir=base_dir or tmp_path,
            model=model_factory(**model_kwargs),
            static_dir=static_dir,
        )
        runtimes.append(runtime)
        return TestClient(create_app(runtime), base_url=client_base_url), runtime

    yield _open
    for runtime in runtimes:
        runtime.close()


@pytest.fixture
def api(open_api: OpenApi) -> tuple[TestClient, ApiRuntime]:
    """默认的一套：闲聊假模型。"""
    return open_api(decision="chat", reply="假回答")


def _create_session(client: TestClient, *, title: str = "会话") -> str:
    response = client.post("/api/sessions", json={"title": title})
    assert response.status_code == 200, response.text
    return str(response.json()["session_id"])


def test_switch_mode_keeps_session_and_checks_busy_conflict(api: tuple[TestClient, ApiRuntime]) -> None:
    client, runtime = api
    sid = _create_session(client)
    _submit(client, sid, "历史问题", "history")
    before = client.get(f"/api/sessions/{sid}").json()
    response = client.patch(f"/api/sessions/{sid}/mode", json={"mode": "chat", "expected_mode": "agent"})
    assert response.status_code == 200 and response.json()["session_id"] == sid
    assert response.json()["mode"] == "chat"
    assert len(client.get("/api/sessions").json()) == 1
    assert client.get(f"/api/sessions/{sid}").json()["messages"] == before["messages"]
    assert client.patch(f"/api/sessions/{sid}/mode", json={"mode": "agent", "expected_mode": "agent"}).status_code == 409
    assert client.patch(f"/api/sessions/{sid}/mode", json={"mode": "unknown", "expected_mode": "chat"}).status_code == 422
    lock = runtime.service._lock_for(sid)
    lock.acquire()
    try:
        assert client.patch(f"/api/sessions/{sid}/mode", json={"mode": "agent", "expected_mode": "chat"}).status_code == 409
    finally:
        lock.release()


def test_recovery_requires_review_and_never_runs_unbound_request(api: tuple[TestClient, ApiRuntime]) -> None:
    client, runtime = api
    sid = _create_session(client)
    runtime.service._ledger.start(request_id="lost", session_id=sid, operation="submit_message", arguments_digest="test")
    before = client.get(f"/api/sessions/{sid}").json()
    assert before["interrupted_request_id"] == "lost"
    payload = {"request_id": "lost", "expected_snapshot_id": before["snapshot_id"], "confirm_reviewed": False}
    assert client.post(f"/api/sessions/{sid}/recovery", json=payload).status_code == 422
    payload["confirm_reviewed"] = True
    other = _create_session(client, title="其他会话")
    assert client.post(f"/api/sessions/{other}/recovery", json=payload).status_code == 409
    response = client.post(f"/api/sessions/{sid}/recovery", json=payload)
    assert response.status_code == 200 and response.json()["interrupted_request_id"] is None
    assert response.json()["messages"] == []
    assert runtime.model.decision_calls == 0
    assert _submit(client, sid, "继续", "new").status_code == 200


def _submit(client: TestClient, session_id: str, text: str, request_id: str):
    return client.post(
        f"/api/sessions/{session_id}/messages",
        json={"text": text, "client_request_id": request_id},
    )


# ---- 健康检查与会话 ---------------------------------------------------------


def test_conversation_map_http_fork_detail_switch_and_arrange(api: tuple[TestClient, ApiRuntime]) -> None:
    client, runtime = api
    sid = _create_session(client)
    for index in range(3):
        assert _submit(client, sid, f"问题 {index + 1}", f"map-{index}").status_code == 200
    url = f"/api/sessions/{sid}"
    tree = client.get(f"{url}/conversation-map").json()
    parent = tree["nodes"][0]["turn_id"]
    assert "checkpoint_id" not in tree["nodes"][0]
    detail = client.get(f"{url}/turns/{parent}").json()
    assert [m["text"] for m in detail["messages"] if m["kind"] == "user"] == ["问题 1"]
    old = client.get(url).json()
    assert client.post(f"{url}/messages", json={"text": "新方向", "client_request_id": "map-fork",
        "parent_turn_id": parent, "expected_branch_id": "main", "expected_snapshot_id": old["snapshot_id"]}).status_code == 200
    forked = client.get(f"{url}/conversation-map").json()
    branch = forked["active_branch_id"]
    assert len(forked["branches"]) == 2 and forked["nodes"][-1]["parent_turn_id"] == parent
    arranged = client.patch(f"{url}/branches/{branch}", json={"title": "方案 B", "position": 0})
    assert arranged.status_code == 200 and arranged.json()["branches"][0]["title"] == "方案 B"
    selected = client.post(f"{url}/branches/main/select").json()
    assert [m["text"] for m in selected["messages"] if m["kind"] == "user"] == ["问题 1", "问题 2", "问题 3"]
    bad = client.post(f"{url}/messages", json={"text": "不该执行", "client_request_id": "map-stale",
        "expected_snapshot_id": "stale"})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "CONVERSATION_CONFLICT"
    assert runtime.service._ledger.get("map-stale") is None


def test_preferences_save_conflict_validation_and_safe_overview(api: tuple[TestClient, ApiRuntime]) -> None:
    client, runtime = api
    initial = client.get("/api/preferences").json()
    assert initial["writable"] and initial["version"] == 0
    assert initial["values"]["execution"]["max_tool_loops"] == runtime.service._app_config.agent.max_tool_loops
    assert initial["values"]["retrieval"]["max_files"] == runtime.service._app_config.rag.max_files
    assert "path" not in json.dumps(initial["advanced"])
    values = initial["values"]
    values["general"]["theme"] = "dark"
    values["retrieval"]["max_results"] = 3
    saved = client.put("/api/preferences", json={"values": values, "expected_version": 0})
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert client.put("/api/preferences", json={"values": values, "expected_version": 0}).status_code == 409
    assert client.get("/api/preferences").json()["values"]["retrieval"]["max_results"] == 3
    assert runtime.service._app_config.rag.max_results == initial["defaults"]["retrieval"]["max_results"]
    values["memory"]["confidence_threshold"] = 2
    assert client.put("/api/preferences", json={"values": values, "expected_version": 1}).status_code == 422
    values["memory"]["confidence_threshold"] = .8
    values["advanced"] = {"compression_enabled": False}
    assert client.put("/api/preferences", json={"values": values, "expected_version": 1}).status_code == 422


def test_memory_cache_change_can_be_saved_but_requires_restart_before_probe(
    api: tuple[TestClient, ApiRuntime], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_agent.llm import embedding
    from local_agent.memory import medium_term

    client, _ = api
    monkeypatch.setattr(embedding, "_hf_constants", lambda: SimpleNamespace(HF_HOME=str(tmp_path / "cache-a")))
    monkeypatch.setattr(embedding, "fastembed_available", lambda: True)
    monkeypatch.setattr(medium_term, "chromadb_available", lambda: True)
    monkeypatch.setattr(embedding, "build_embedding_function",
                        lambda *args, **kwargs: pytest.fail("must not load while restart is pending"))
    values = client.get("/api/preferences").json()["values"]
    values["embedding"] = {"enabled": True, "provider": "onnx_local", "model": "test",
                           "cache_dir": str(tmp_path / "cache-b"), "local_files_only": True}
    values["medium_term"]["enabled"] = True
    saved = client.put("/api/preferences", json={"values": values, "expected_version": 0})
    assert saved.status_code == 200, saved.text
    assert saved.json()["resources"]["restart_required"] is True
    assert saved.json()["resources"]["embedding_state"] == "not_ready"
    assert client.get("/api/preferences").json()["resources"]["restart_required"] is True
    tested = client.post("/api/preferences/memory/test", json={"values": values, "expected_version": 1})
    assert tested.status_code == 200
    assert tested.json()["error_category"] == "restart_required"
    assert client.get("/api/preferences").json()["version"] == 1
    assert not (tmp_path / "cache-b").exists()


def test_memory_settings_dependencies_legacy_client_and_readonly_paths(
    api: tuple[TestClient, ApiRuntime]
) -> None:
    client, _ = api
    view = client.get("/api/preferences").json()
    assert view["values"]["medium_term"] and view["resources"]["backend"] == "chroma"
    values = view["values"]
    values["medium_term"]["enabled"] = True
    assert client.put("/api/preferences", json={"values": values, "expected_version": 0}).status_code == 422
    values["medium_term"]["enabled"] = False
    values["medium_term"]["top_k"] = 17
    values["medium_term"]["min_score"] = .555
    values["embedding"]["model"] = "configured-for-later"
    result = client.put("/api/preferences", json={"values": values, "expected_version": 0})
    assert result.status_code == 200, result.text
    legacy = {k: v for k, v in values.items() if k not in {"medium_term", "embedding"}}
    legacy["general"]["theme"] = "dark"
    updated = client.put("/api/preferences", json={"values": legacy, "expected_version": 1})
    assert updated.status_code == 200
    assert updated.json()["values"]["medium_term"]["top_k"] == 17
    assert updated.json()["values"]["embedding"]["model"] == "configured-for-later"
    values["medium_term"]["path"] = "somewhere-else"
    assert client.put("/api/preferences", json={"values": values, "expected_version": 2}).status_code == 422


@pytest.mark.parametrize("base_url", ["file:///tmp", "https://user:password@example.com", "https://example.com?key=x", "http://example.com"])
def test_embedding_endpoint_validation(api: tuple[TestClient, ApiRuntime], base_url: str) -> None:
    client, _ = api
    values = client.get("/api/preferences").json()["values"]
    values["embedding"] = {"enabled": True, "provider": "openai_compatible", "model": "test",
                           "base_url": base_url, "api_key_env": "TEST_EMBEDDING_KEY"}
    assert client.put("/api/preferences", json={"values": values, "expected_version": 0}).status_code == 422


def test_memory_probe_is_explicit_versioned_nonwriting_and_redacts_failures(
    api: tuple[TestClient, ApiRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_agent.llm import embedding
    from local_agent.model_settings import memory_settings

    client, _ = api
    calls: list[dict[str, Any]] = []
    secret = "fake-secret-must-not-leak"
    monkeypatch.setenv("TEST_EMBEDDING_KEY", secret)
    adapter = SimpleNamespace(
        info=embedding.EmbeddingInfo(provider="openai_compatible", model="unit-test", dimension=None,
                                     normalized=True, cache_dir=None, local_files_only=False,
                                     endpoint="https://example.com/v1"),
        embed_query=lambda text: [[1.0, 0.0, 0.0]],
    )
    def factory(config, **kwargs):
        calls.append(kwargs)
        return adapter
    monkeypatch.setattr(embedding, "build_embedding_function", factory)
    monkeypatch.setattr(memory_settings.medium_term, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_settings.medium_term, "check_existing_collection", lambda config, info: None)
    initial = client.get("/api/preferences").json()
    assert calls == []
    values = json.loads(json.dumps(initial["values"]))
    values["embedding"] = {"enabled": True, "provider": "openai_compatible", "model": "unit-test",
                           "base_url": "https://example.com/v1", "api_key_env": "TEST_EMBEDDING_KEY"}
    payload = {"values": values, "expected_version": 0}
    result = client.post("/api/preferences/memory/test", json=payload)
    assert result.status_code == 200 and result.json()["state"] == "passed"
    assert result.json()["dimension"] == 3
    assert len(calls) == 1 and calls[0]["api_key"] == secret
    assert calls[0]["request_timeout_seconds"] == 30 and calls[0]["max_retries"] == 0
    assert client.get("/api/preferences").json()["values"] == initial["values"]
    adapter.embed_query = lambda text: (_ for _ in ()).throw(RuntimeError(secret))
    failure = client.post("/api/preferences/memory/test", json=payload)
    assert failure.json()["state"] == "failed" and secret not in failure.text
    saved = client.put("/api/preferences", json={"values": initial["values"], "expected_version": 0})
    assert saved.status_code == 200
    assert client.post("/api/preferences/memory/test", json=payload).status_code == 409
    assert len(calls) == 2


def test_preferences_pin_across_approval_then_apply_to_new_task(open_api: OpenApi, monkeypatch: pytest.MonkeyPatch) -> None:
    client, runtime = open_api(decision="tool", reply="好的", tool_calls=[
        {"name": "delete_file", "args": {"path": "note.txt"}, "id": "pref-call"},
    ])
    contexts: list[Any] = []
    original = runtime.service._context
    def capture(session, env):
        contexts.append(env)
        return original(session, env)
    monkeypatch.setattr(runtime.service, "_context", capture)
    session_id = _create_session(client)
    initial = client.get("/api/preferences").json()
    assert _submit(client, session_id, "删除 note.txt", "pref-submit").json()["status"] == "awaiting_approval"
    old_limit = contexts[-1].app_config.agent.max_tool_loops
    values = initial["values"]
    values["execution"]["max_tool_loops"] = old_limit + 7
    values["medium_term"]["top_k"] = 11
    values["embedding"]["model"] = "next-task-model"
    values["retrieval"]["enabled"] = False
    assert client.put("/api/preferences", json={"values": values, "expected_version": 0}).status_code == 200
    approval = client.get(f"/api/sessions/{session_id}").json()["approval"]
    response = client.post(f"/api/sessions/{session_id}/approvals/{approval['approval_id']}", json={
        "decision": "reject", "client_request_id": "pref-resume", "reason": "测试取消",
    })
    assert response.status_code == 200, response.text
    assert contexts[-1].app_config.agent.max_tool_loops == old_limit
    assert contexts[-1].preference_binding.version == 0
    assert contexts[-1].app_config.memory.medium_term.top_k == initial["defaults"]["medium_term"]["top_k"]
    assert contexts[-1].app_config.embedding.model == initial["defaults"]["embedding"]["model"]
    assert contexts[-1].app_config.rag.enabled == initial["defaults"]["retrieval"]["enabled"]
    _submit(client, session_id, "继续", "pref-next")
    assert contexts[-1].app_config.agent.max_tool_loops == old_limit + 7
    assert contexts[-1].app_config.rag.enabled is False
    assert contexts[-1].preference_binding.version == 1
    assert contexts[-1].app_config.memory.medium_term.top_k == 11
    assert contexts[-1].app_config.embedding.model == "next-task-model"


def test_preferences_approval_snapshot_survives_process_restart(open_api: OpenApi, monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = {"decision": "tool", "reply": "好的", "tool_calls": [
        {"name": "delete_file", "args": {"path": "note.txt"}, "id": "restart-pref"},
    ]}
    client, runtime = open_api(**kwargs)
    session_id = _create_session(client)
    view = client.get("/api/preferences").json()
    view["values"]["execution"]["max_tool_loops"] = 13
    view["values"]["retrieval"]["max_results"] = 4
    view["values"]["medium_term"]["top_k"] = 12
    view["values"]["embedding"]["model"] = "pinned-model"
    assert client.put("/api/preferences", json={"values": view["values"], "expected_version": 0}).status_code == 200
    assert _submit(client, session_id, "删除 note.txt", "restart-submit").json()["status"] == "awaiting_approval"
    view["values"]["execution"]["max_tool_loops"] = 37
    view["values"]["medium_term"]["top_k"] = 21
    view["values"]["embedding"]["model"] = "new-model"
    assert client.put("/api/preferences", json={"values": view["values"], "expected_version": 1}).status_code == 200
    runtime.close()
    client, reopened = open_api(**kwargs)
    assert client.get("/api/preferences").json()["values"]["execution"]["max_tool_loops"] == 37
    contexts: list[Any] = []
    original = reopened.service._context
    def capture(session, env):
        contexts.append(env)
        return original(session, env)
    monkeypatch.setattr(reopened.service, "_context", capture)
    approval = client.get(f"/api/sessions/{session_id}").json()["approval"]
    result = client.post(f"/api/sessions/{session_id}/approvals/{approval['approval_id']}", json={
        "decision": "reject", "client_request_id": "restart-resume", "reason": "测试取消",
    })
    assert result.status_code == 200, result.text
    assert contexts[-1].preference_binding.version == 1
    assert contexts[-1].app_config.agent.max_tool_loops == 13
    assert contexts[-1].app_config.rag.max_results == 4
    assert contexts[-1].app_config.memory.medium_term.top_k == 12
    assert contexts[-1].app_config.embedding.model == "pinned-model"


def test_preferences_unavailable_store_is_read_only(api: tuple[TestClient, ApiRuntime], monkeypatch: pytest.MonkeyPatch) -> None:
    client, runtime = api
    monkeypatch.setattr(runtime.service, "_preferences", None)
    view = client.get("/api/preferences").json()
    assert view["writable"] is False
    assert view["values"] == view["defaults"]
    assert client.put("/api/preferences", json={"values": view["values"], "expected_version": 0}).status_code == 503


def test_catalog_rename_archive_restore_preserves_history(api: tuple[TestClient, ApiRuntime]) -> None:
    client, runtime = api
    first = _create_session(client, title="第一段")
    assert _submit(client, first, "你好", "catalog-history").status_code == 200
    before = client.get(f"/api/sessions/{first}").json()
    second = _create_session(client, title="第二段")
    assert client.post(f"/api/sessions/{first}/select").status_code == 200
    assert next(row for row in client.get("/api/sessions").json() if row["is_active"])["session_id"] == first
    stale = runtime.database.sessions.get(first)
    assert client.patch(f"/api/sessions/{first}", json={"title": "  新名称  "}).json()["title"] == "新名称"
    assert runtime.service._snapshot(stale, runtime.service._read(stale)).title == "新名称"
    assert client.post(f"/api/sessions/{first}/archive").json()["archived"] is True
    remaining = client.get("/api/sessions").json()
    assert [row["session_id"] for row in remaining] == [second]
    assert remaining[0]["is_active"] is True
    assert client.get(f"/api/sessions/{first}").status_code == 404
    assert client.post(f"/api/sessions/{first}/select").status_code == 404
    assert _submit(client, first, "不能执行", "catalog-blocked").status_code == 404
    assert [row["session_id"] for row in client.get("/api/sessions?archived=true").json()] == [first]
    assert client.post(f"/api/sessions/{first}/restore").json()["archived"] is False
    after = client.get(f"/api/sessions/{first}").json()
    assert after["title"] == "新名称"
    assert after["messages"] == before["messages"]
    assert after["usage"] == before["usage"]
    assert runtime.database.connection.execute("SELECT COUNT(*) FROM request_ledger").fetchone()[0] == 1
    assert client.post(f"/api/sessions/{second}/archive").status_code == 200
    assert client.post(f"/api/sessions/{first}/archive").status_code == 200
    assert client.get("/api/sessions").json() == []


def test_workspace_removal_keeps_history_and_directory(api: tuple[TestClient, ApiRuntime]) -> None:
    client, runtime = api
    session_id = _create_session(client)
    root = Path(runtime.database.sessions.get(session_id).workspace_root)
    (root / "note.txt").write_text("保留我", encoding="utf-8")
    workspace_id = client.get("/api/workspaces").json()[0]["workspace_id"]
    renamed = client.patch(f"/api/workspaces/{workspace_id}", json={"title": "我的项目"})
    assert renamed.status_code == 200
    assert client.get(f"/api/sessions/{session_id}").json()["workspace"]["display_name"] == "我的项目"
    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 204
    assert client.get("/api/workspaces").json() == []
    assert client.get("/api/sessions").json()[0]["session_id"] == session_id
    assert client.get(f"/api/sessions/{session_id}").status_code == 200
    assert client.post(f"/api/sessions/{session_id}/select").status_code == 200
    assert client.get("/api/workspaces").json() == []  # 选择历史会话不重新登记目录
    assert (root / "note.txt").read_text(encoding="utf-8") == "保留我"
    assert client.post("/api/workspaces/select", json={"path": str(root)}).json()["session_id"] == session_id
    assert client.get("/api/workspaces").json()[0]["display_name"] == "我的项目"


@pytest.mark.parametrize("kind", ["sessions", "workspaces"])
@pytest.mark.parametrize("title", [" ", "x" * 61])
def test_catalog_title_validation(api: tuple[TestClient, ApiRuntime], kind: str, title: str) -> None:
    client, _ = api
    session_id = _create_session(client)
    identity = session_id if kind == "sessions" else client.get("/api/workspaces").json()[0]["workspace_id"]
    assert client.patch(f"/api/{kind}/{identity}", json={"title": title}).status_code == 422


def test_catalog_busy_guard_keeps_session_and_workspace(api: tuple[TestClient, ApiRuntime]) -> None:
    client, runtime = api
    session_id = _create_session(client)
    workspace_id = client.get("/api/workspaces").json()[0]["workspace_id"]
    lock = runtime.service._lock_for(session_id)
    with lock:
        for response in (
            client.post(f"/api/sessions/{session_id}/archive"),
            client.delete(f"/api/workspaces/{workspace_id}"),
        ):
            assert response.status_code == 409
            assert response.json()["error"]["code"] == SESSION_BUSY
    assert len(client.get("/api/sessions").json()) == 1
    assert len(client.get("/api/workspaces").json()) == 1


def test_catalog_pending_approval_cannot_be_hidden(open_api: OpenApi) -> None:
    client, _ = open_api(decision="tool", reply="好的", tool_calls=[
        {"name": "delete_file", "args": {"path": "note.txt"}, "id": "catalog-call"},
    ])
    session_id = _create_session(client)
    workspace_id = client.get("/api/workspaces").json()[0]["workspace_id"]
    assert _submit(client, session_id, "删除 note.txt", "catalog-pending").json()["status"] == "awaiting_approval"
    assert client.post(f"/api/sessions/{session_id}/archive").status_code == 409
    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 409
    assert client.get(f"/api/sessions/{session_id}").json()["approval"]["state"] == "pending"


def test_health_reports_readiness_without_paths_or_secrets(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["model_configured"] is True
    assert body["storage"] == "sqlite"
    assert body["static_frontend"] is False  # 本用例没有构建产物
    # 不外泄路径与密钥（§15.8）。
    assert "checkpoints.sqlite" not in response.text
    assert "sk-" not in response.text
    assert "C:\\" not in response.text


def test_capabilities_endpoint_explains_what_is_unavailable(
    api: tuple[TestClient, ApiRuntime],
) -> None:
    """能力状态端点：闭集全覆盖、不可用必须解释 + 给办法，且**不出现工具名**（§16.6）。"""
    client, _runtime = api
    response = client.get("/api/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["profile"]
    rows = {row["capability"]: row for row in body["capabilities"]}
    assert set(rows) == set(CAPABILITY_TOOLS), "闭集里每个能力都要有一行"

    for row in rows.values():
        assert row["label"], row["capability"]
        if row["available"]:
            continue
        # 不可用必须能解释、能照做；"只灰掉不解释"会让用户以为功能坏了
        assert row["hint"], row["capability"]
        assert row["reason"] in {"profile", "disabled", "missing_dependency"}

    # 默认配置里 Git 关着：要说明是"开关没打开"，而不是笼统的不可用
    assert rows["git_write"]["reason"] == "disabled"
    assert "git.enabled" in rows["git_write"]["hint"]
    # coder 不声明 web：这是档位问题，提示指向 profiles.active
    assert rows["web"]["reason"] == "profile"

    # 页面区块不出现原始工具名（§16.6、§15.8 的字段门槛）
    for name in (name for names in CAPABILITY_TOOLS.values() for name in names):
        assert name not in response.text, f"能力状态里泄露了工具名 {name}"


def test_session_lifecycle_over_http(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    session_id = _create_session(client, title="给 README 补一段")

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert [item["session_id"] for item in listed.json()] == [session_id]
    assert listed.json()[0]["title"] == "给 README 补一段"

    snapshot = client.get(f"/api/sessions/{session_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["session_state"] == "idle"
    assert snapshot.json()["messages"] == []

    missing = client.get("/api/sessions/s-nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == SESSION_NOT_FOUND


def test_submit_message_is_idempotent_and_conflicts_on_a_reused_id(
    api: tuple[TestClient, ApiRuntime],
) -> None:
    client, runtime = api
    session_id = _create_session(client)

    first = _submit(client, session_id, "你好", "req-1")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "completed"
    assert body["replayed"] is False
    assert body["run_id"] and body["snapshot_id"]

    replay = _submit(client, session_id, "你好", "req-1")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["snapshot_id"] == body["snapshot_id"]

    conflict = _submit(client, session_id, "换一句", "req-1")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == IDEMPOTENCY_CONFLICT
    # 冲突没有产生第二次执行。
    assert runtime.model.decision_calls == 1  # type: ignore[attr-defined]


def test_second_submit_while_the_session_is_busy_returns_409(
    api: tuple[TestClient, ApiRuntime],
) -> None:
    client, runtime = api
    session_id = _create_session(client)

    lock = runtime.service._lock_for(session_id)  # 测试直接制造"正在跑"
    assert lock.acquire(blocking=False) is True
    try:
        busy = _submit(client, session_id, "再发一条", "req-2")
    finally:
        lock.release()

    assert busy.status_code == 409
    error = busy.json()["error"]
    assert error["code"] == SESSION_BUSY
    assert error["retryable"] is True
    assert error["retry_condition"]  # 条件必须给出，页面不靠猜


def test_approval_flow_over_http(open_api: OpenApi) -> None:
    client, _runtime = open_api(
        decision="tool",
        reply="好的",
        tool_calls=[{"name": "delete_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    session_id = _create_session(client)

    pending = _submit(client, session_id, "删除 note.txt", "req-1")
    assert pending.status_code == 200
    assert pending.json()["status"] == "awaiting_approval"

    approval = client.get(f"/api/sessions/{session_id}").json()["approval"]
    assert approval["state"] == "pending"
    approval_id = approval["approval_id"]

    decided = client.post(
        f"/api/sessions/{session_id}/approvals/{approval_id}",
        json={"decision": "reject", "client_request_id": "req-2", "reason": "先别删"},
        headers={"Origin": VITE_ORIGIN},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "completed"

    again = client.post(
        f"/api/sessions/{session_id}/approvals/{approval_id}",
        json={"decision": "approve", "client_request_id": "req-3"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == APPROVAL_EXPIRED


def test_recursive_delete_requires_two_http_decisions_and_exposes_stage(
    open_api: OpenApi, tmp_path: Path
) -> None:
    folder = tmp_path / WORKSPACE_NAME / "delete-me"
    folder.mkdir(parents=True)
    (folder / "file.txt").write_text("content", encoding="utf-8")
    client, _runtime = open_api(
        decision="tool", reply="好的",
        tool_calls=[{"name": "delete_file", "args": {"path": "delete-me", "recursive": True}, "id": "call-1"}],
    )
    session_id = _create_session(client)
    assert _submit(client, session_id, "删除 delete-me", "req-1").json()["status"] == "awaiting_approval"

    url = f"/api/sessions/{session_id}"
    first = client.get(url).json()["approval"]
    assert first["state"] == "pending"
    assert first["confirmation_stage"] == 1
    assert first["target"] == "delete-me"
    assert first["outside_workspace"] is False
    approval_url = f"{url}/approvals/{first['approval_id']}"

    first_decision = client.post(approval_url, json={
        "decision": "approve", "client_request_id": "req-2", "confirm_recursive": False,
    })
    assert first_decision.status_code == 200
    assert first_decision.json()["status"] == "awaiting_approval"
    assert folder.exists()
    second = client.get(url).json()["approval"]
    assert second["state"] == "pending"
    assert second["confirmation_stage"] == 2

    second_decision = client.post(approval_url, json={
        "decision": "approve", "client_request_id": "req-3", "confirm_recursive": True,
    })
    assert second_decision.status_code == 200
    assert second_decision.json()["status"] == "completed"
    assert not folder.exists()


def test_approval_edit_requires_edited_arguments(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    session_id = _create_session(client)

    # edit 不带参数 → 请求形状问题（422），不是业务错误。
    bad = client.post(
        f"/api/sessions/{session_id}/approvals/ap-1",
        json={"decision": "edit", "client_request_id": "req-1"},
    )
    assert bad.status_code == 422
    # 非 edit 却带了参数 → 同样拒绝，避免"改了但没生效"的歧义。
    mixed = client.post(
        f"/api/sessions/{session_id}/approvals/ap-1",
        json={
            "decision": "approve",
            "client_request_id": "req-2",
            "edited_arguments": {"path": "note.txt"},
        },
    )
    assert mixed.status_code == 422


def test_blank_message_is_rejected_by_the_request_shape(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    session_id = _create_session(client)

    blank = _submit(client, session_id, "   ", "req-1")
    assert blank.status_code == 422
    # 未知字段不会被静默忽略。
    extra = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"text": "你好", "client_request_id": "req-2", "secret": "x"},
    )
    assert extra.status_code == 422


# ---- 工作区 -----------------------------------------------------------------


def test_workspace_selection_by_id_and_path(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    first = _create_session(client, title="会话一")

    refs = client.get("/api/workspaces").json()
    assert [ref["workspace_id"] for ref in refs]
    # 用 workspace_id 选回同一个工作区（前端只拿得到这个不透明标识）。
    selected = client.post("/api/workspaces/select", json={"workspace_id": refs[0]["workspace_id"]})
    assert selected.status_code == 200
    assert selected.json()["session_id"] == first

    unknown = client.post("/api/workspaces/select", json={"workspace_id": "not-a-workspace"})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == WORKSPACE_NOT_FOUND


def test_new_workspace_path_is_validated_and_not_echoed(
    tmp_path: Path, api: tuple[TestClient, ApiRuntime]
) -> None:
    client, _runtime = api
    fresh = tmp_path / "another"
    fresh.mkdir()

    created = client.post("/api/workspaces/select", json={"path": str(fresh)})
    assert created.status_code == 200
    assert created.json()["workspace"]["display_name"] == "another"

    for bad in ("relative/dir", "~/somewhere", str(tmp_path / "missing")):
        rejected = client.post("/api/workspaces/select", json={"path": bad})
        assert rejected.status_code == 404, bad
        error = rejected.json()["error"]
        assert error["code"] == WORKSPACE_NOT_FOUND
        # 用户填的绝对路径属于私人信息：错误文案里不得回显它（§15.8）。
        assert str(tmp_path) not in json.dumps(error, ensure_ascii=False)

    both = client.post("/api/workspaces/select", json={"path": str(fresh), "workspace_id": "x"})
    assert both.status_code == 422
    neither = client.post("/api/workspaces/select", json={})
    assert neither.status_code == 422


def test_pick_directory_returns_the_chosen_path_only(
    api: tuple[TestClient, ApiRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """选中目录：只回路径，**不顺手登记工作区**。

    分两步是为了让"取消"能保持零副作用——登记由前端接着调 `select { path }` 完成。
    """
    client, _runtime = api
    before = client.get("/api/workspaces").json()
    monkeypatch.setattr(directory_picker, "pick_directory", lambda: r"D:\projects\demo")

    response = client.post("/api/workspaces/pick", headers={"origin": LOCAL_BASE_URL})

    assert response.status_code == 200
    assert response.json() == {"path": r"D:\projects\demo"}
    assert client.get("/api/workspaces").json() == before  # 状态没变


def test_pick_directory_cancel_is_not_an_error(
    api: tuple[TestClient, ApiRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消是正常操作：`200 + null`，不是失败、也不改状态。"""
    client, _runtime = api
    before = client.get("/api/workspaces").json()
    monkeypatch.setattr(directory_picker, "pick_directory", lambda: None)

    response = client.post("/api/workspaces/pick", headers={"origin": LOCAL_BASE_URL})

    assert response.status_code == 200
    assert response.json() == {"path": None}
    assert client.get("/api/workspaces").json() == before


def test_pick_directory_failure_is_executable_and_redacted(
    api: tuple[TestClient, ApiRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """宿主弹不出窗口：503 + 可重试提示，且**不回显子进程原文**（它可能带本机路径）。"""
    client, _runtime = api

    def boom() -> str | None:
        raise directory_picker.DirectoryPickerError(r"目录选择器未能打开：C:\secret\boom.exe")

    monkeypatch.setattr(directory_picker, "pick_directory", boom)
    response = client.post("/api/workspaces/pick", headers={"origin": LOCAL_BASE_URL})

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == DIRECTORY_PICKER_UNAVAILABLE
    assert error["retryable"] is True
    assert "boom.exe" not in json.dumps(error, ensure_ascii=False)


def test_pick_directory_rejects_callers_without_a_local_origin(
    api: tuple[TestClient, ApiRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 `Origin`（curl / 本机脚本）要被拒，而且**在被拒时不该真的弹窗**。

    这是本服务唯一会干扰用户桌面的能力；它只有本机页面该调——本机脚本想要原生对话框的话
    自己就能弹，不需要借这条路。
    """
    client, _runtime = api
    calls: list[str] = []
    monkeypatch.setattr(directory_picker, "pick_directory", lambda: calls.append("弹了") or "x")

    for headers in ({}, {"origin": "http://evil.example"}, {"origin": "null"}):
        response = client.post("/api/workspaces/pick", headers=headers)
        assert response.status_code == 403, headers

    assert calls == []  # 403 判定发生在弹窗之前


# ---- 本机边界 ---------------------------------------------------------------


def test_foreign_host_is_rejected(open_api: OpenApi) -> None:
    client, runtime = open_api(decision="chat")

    # DNS rebinding：域名解析到 127.0.0.1，但请求带着外站 Host。
    foreign = TestClient(create_app(runtime), base_url="http://evil.example")
    response = foreign.get("/api/health")
    assert response.status_code == 403
    assert "Host" in response.json()["detail"]

    # 回环的三种写法都放行（含端口；IPv6 用方括号字面量）。
    for host_value in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000"):
        allowed = client.get("/api/health", headers={"Host": host_value})
        assert allowed.status_code == 200, host_value


def test_origin_is_checked_only_for_state_changing_requests(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    session_id = _create_session(client)

    cross_origin = client.post(
        "/api/sessions",
        json={"title": "来自别的站点"},
        headers={"Origin": "https://evil.example"},
    )
    assert cross_origin.status_code == 403
    assert "跨源" in cross_origin.json()["detail"]

    # **同源** POST（生产形态：前端由本服务托管）必须放行。浏览器对所有非 GET/HEAD 请求都带
    # `Origin`（同源也带），漏掉这一条会让生产形态下**任何提交**都变成 403——实测踩过：
    # 页面上"新建会话"一点就失败，输入区因此永远发不出去。
    same_origin_origin = str(client.base_url).rstrip("/")
    same_origin = client.post(
        "/api/sessions", json={"title": "同源提交"}, headers={"Origin": same_origin_origin}
    )
    assert same_origin.status_code == 200, same_origin.text

    # 同主机但端口不同**不是**同源：权威部分含端口。这条直接钉住归一化规则，
    # 不走 HTTP——测试客户端自己的 Host 里没有端口，用请求去构造这个场景不成立。
    assert _authority("http://127.0.0.1:5173") != _authority("127.0.0.1:8000")
    assert _authority("http://127.0.0.1:8000") == _authority("127.0.0.1:8000")
    assert _authority("https://example.com") == _authority("example.com:443")
    assert _authority("null") == "null"  # 沙箱/`file://` 的 Origin 与任何 Host 都不等

    # 没有 Origin（curl / 本机脚本）按非浏览器处理：放行。
    assert _submit(client, session_id, "你好", "req-1").status_code == 200
    # 读接口不受 Origin 限制。
    assert client.get("/api/sessions", headers={"Origin": "https://evil.example"}).status_code == 200


def test_oversized_body_is_rejected_before_the_service(open_api: OpenApi) -> None:
    client, runtime = open_api(decision="chat", reply="假回答", web_overrides={"max_body_bytes": 200})
    session_id = _create_session(client)
    before = runtime.database.connection.execute("SELECT COUNT(*) FROM request_ledger").fetchone()

    # 带 Content-Length 的超限正文。
    declared = _submit(client, session_id, "x" * 500, "req-1")
    assert declared.status_code == 413
    # 没有 Content-Length（chunked）时按流累计，同样拒绝。
    streamed = client.post(
        f"/api/sessions/{session_id}/messages",
        content=iter([b'{"text":"', b"y" * 400, b'","client_request_id":"req-2"}']),
        headers={"Content-Type": "application/json"},
    )
    assert streamed.status_code == 413

    after = runtime.database.connection.execute("SELECT COUNT(*) FROM request_ledger").fetchone()
    assert after == before  # 边界拒绝没有进业务层，也没落台账


def test_single_instance_lock_refuses_a_second_runtime(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    config = _config(tmp_path, app_config)
    first = open_runtime(config, base_dir=tmp_path, model=model_factory(decision="chat"))
    try:
        assert instance_lock_path(tmp_path / "data" / "checkpoints.sqlite").is_file()
        with pytest.raises(RuntimeError, match="已经有一个 local-agent-web 服务在跑"):
            open_runtime(config, base_dir=tmp_path, model=model_factory(decision="chat"))
    finally:
        first.close()

    # 释放之后可以再起一个（进程重启的正常路径）。
    third = open_runtime(config, base_dir=tmp_path, model=model_factory(decision="chat"))
    third.close()


# ---- 静态托管 ---------------------------------------------------------------


def _fake_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (dist / "assets" / "index-a1b2c3d4.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("SENSITIVE", encoding="utf-8")
    return dist


def test_static_frontend_serves_pages_assets_and_cache_headers(
    tmp_path: Path, open_api: OpenApi
) -> None:
    dist = _fake_dist(tmp_path)
    client, _runtime = open_api(decision="chat", static_dir=dist)

    index = client.get("/")
    assert index.status_code == 200
    assert "app" in index.text
    assert index.headers["cache-control"] == "no-cache"

    asset = client.get("/assets/index-a1b2c3d4.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"].startswith("public, max-age=31536000")
    # MIME 必须由我们定，不能靠平台的 `mimetypes`：本机实测它把 `.js` 猜成 `text/plain`，
    # 浏览器随即以 "Expected a JavaScript-or-Wasm module script" 拒绝加载，整页空白
    # （生产同源托管在那种机器上等于不可用）。这条断言就是那个故障的回归门。
    assert asset.headers["content-type"].startswith("text/javascript")

    # SPA 回退：看起来像页面的路径交给前端路由。
    deep = client.get("/sessions/abc")
    assert deep.status_code == 200
    assert "app" in deep.text

    # 不存在的静态资源是 404，而不是把 HTML 当脚本返回。
    missing_asset = client.get("/assets/index-ffff0000.js")
    assert missing_asset.status_code == 404
    assert missing_asset.json()["detail"]


def test_static_hosting_does_not_escape_the_dist_directory(
    tmp_path: Path, open_api: OpenApi
) -> None:
    dist = _fake_dist(tmp_path)
    client, _runtime = open_api(decision="chat", static_dir=dist)

    # 编码过的 `..`：httpx 不会替我们规范掉，服务端必须自己拒绝。
    escaped = client.get("/%2e%2e/secret.txt")
    assert escaped.status_code == 404
    assert "SENSITIVE" not in escaped.text


def test_api_prefix_wins_over_the_spa_fallback(tmp_path: Path, open_api: OpenApi) -> None:
    dist = _fake_dist(tmp_path)
    client, _runtime = open_api(decision="chat", static_dir=dist)

    # `/api/*` 下"没有这条路由"必须是 JSON 的 404/405，**不能**落到 SPA 回退变成 HTML。
    # （模型连接路由自 T29A 起存在，所以这里挑的是仍然不存在的路径与不存在的方法。）
    for path in (
        "/api/does-not-exist",
        "/api/model-connections/mc-nope",
        "/api/model-connections/mc-nope/test",
    ):
        response = client.get(path)
        assert response.status_code in {404, 405}, path
        assert response.headers["content-type"].startswith("application/json"), path


def test_missing_frontend_build_explains_itself(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    response = client.get("/")
    assert response.status_code == 404
    assert "npm run build" in response.json()["detail"]


#: 构建出的 `index.html` 里对带哈希资源的引用（Vite 把它们放在 `assets/` 下）。
_ASSET_REFERENCE = re.compile(r'(?:src|href)="(/assets/[^"]+)"')


def _referenced_assets(html: str) -> list[str]:
    """取出产物真正引用的资源路径（去重、保持出现顺序）。"""
    seen: dict[str, None] = {}
    for match in _ASSET_REFERENCE.finditer(html):
        seen.setdefault(match.group(1), None)
    return list(seen)


def test_the_real_frontend_build_is_served_asset_by_asset(open_api: OpenApi) -> None:
    """生产形态的静态托管集成门（T37）：拿**真实构建产物**跑一遍托管。

    上面几条用的是手工造的假产物——文件名是编的、`index.html` 只有一行标题。真实产物里是带内容
    哈希的资源名，且 `index.html` 引用着它们；"引用的每个文件是否真的取得到、MIME 是否与后缀一致"
    （本机 `mimetypes` 把 `.js` 猜成 `text/plain`、整页空白那次）只有真产物才验得到。

    没有构建过就跳过：`frontend/dist` 被 `.gitignore` 排除，这条门的意义只在"产物存在"时成立，
    而要求跑测试的人先构建前端会把只依赖 node 的用例变成依赖 npm。
    """
    if not (REAL_DIST / "index.html").is_file():
        pytest.skip(f"没有前端产物（{REAL_DIST}）：先在 frontend/ 执行 npm run build")
    client, _runtime = open_api(decision="chat", static_dir=REAL_DIST)

    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert index.headers["cache-control"] == "no-cache"

    referenced = _referenced_assets(index.text)
    assert referenced, "index.html 没有引用任何 assets/ 资源，产物或解析规则不对"
    checked = 0
    for path in referenced:
        asset = client.get(path)
        assert asset.status_code == 200, path
        # 带内容哈希 → 长期缓存；MIME 由我们按后缀定，不靠平台猜。
        assert asset.headers["cache-control"].startswith("public, max-age=31536000"), path
        expected = {"js": "text/javascript", "css": "text/css"}.get(Path(path).suffix.lstrip("."))
        if expected is not None:
            assert asset.headers["content-type"].startswith(expected), path
            checked += 1
    assert checked >= 2, "产物里应当至少有一个 JS 与一个 CSS 被引用"

    # 深链接仍交给前端路由（刷新不能 404）。
    deep = client.get("/sessions/real-deep-link")
    assert deep.status_code == 200
    assert deep.headers["content-type"].startswith("text/html")


# ---- SSE --------------------------------------------------------------------

SSE_WEB = {
    "sse_heartbeat_seconds": 0.05,
    "sse_idle_timeout_seconds": 0.4,
}


def _sse_url(session_id: str) -> str:
    return f"/api/sessions/{session_id}/events"


class _StubSubscription:
    """泵依赖订阅者的 `poll` / `closed` / `overflowed` / `pending_count`。"""

    def __init__(
        self, batches: list[list[Any]], *, overflowed: bool = False, max_pending: int = 200
    ) -> None:
        self._batches = batches
        self.closed = False
        self.max_pending = max_pending
        self._overflowed = overflowed

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    @property
    def pending_count(self) -> int:
        return sum(len(batch) for batch in self._batches)

    def poll(self) -> list[Any]:
        return self._batches.pop(0) if self._batches else []


def _frames(lines: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 行解析成 `(事件名, data)` 列表；`:` 开头的心跳与空行被忽略。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    event: str | None = None
    for line in lines:
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: ") and event is not None:
            frames.append((event, json.loads(line[len("data: ") :])))
            event = None
    return frames


def _sse_scope(path: str, *, host: str = "127.0.0.1:8000") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", host.encode())],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }


async def _collect_stream(
    app: Any, path: str, *, limit: float = 10.0, host: str = "127.0.0.1:8000"
) -> list[tuple[str, dict[str, Any]]]:
    """直接以 ASGI 方式消费一次 GET，返回解析后的事件帧。

    **不用 `TestClient` 读流式响应**：它会把整段响应缓冲到应用结束才返回，实测会永久阻塞
    （应用侧在 `StreamingResponse` 收尾时等不到 `disconnect`）。这里自己当 ASGI 服务器：
    正文发完就补一条 `http.disconnect`，并且**整个收集过程有硬超时**——事件流如果不结束，
    测试应当失败，而不是把整轮 pytest 挂住。
    """
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        return await queue.get()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            return
        chunks.append(message.get("body") or b"")
        if not message.get("more_body", False):
            await queue.put({"type": "http.disconnect"})

    await queue.put({"type": "http.request", "body": b"", "more_body": False})
    try:
        await asyncio.wait_for(app(_sse_scope(path, host=host), receive, send), timeout=limit)
    except TimeoutError:  # pragma: no cover - 只有实现回归时才会走到
        pytest.fail(f"事件流没有在 {limit}s 内结束（响应收尾被挂住）")
    return _frames(b"".join(chunks).decode("utf-8").splitlines())


async def _drive_middleware(method: str, messages: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """把中间件包在一个"读到 disconnect 为止"的应用外面，返回它看到的 `(消息类型, 正文长度)`。"""
    seen: list[tuple[str, int]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            seen.append((str(message["type"]), len(message.get("body") or b"")))
            if message["type"] == "http.disconnect":
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = LocalBoundaryMiddleware(
        app, allowed_hosts=["127.0.0.1"], allowed_origins=[], max_body_bytes=1000
    )
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for message in messages:
        await queue.put(message)
    await queue.put({"type": "http.disconnect"})

    async def receive() -> dict[str, Any]:
        return await queue.get()

    async def send(message: dict[str, Any]) -> None:
        del message

    scope = _sse_scope("/events")
    scope["method"] = method
    await asyncio.wait_for(wrapped(scope, receive, send), timeout=5.0)
    return seen


@pytest.mark.asyncio
async def test_the_boundary_middleware_never_swallows_disconnect() -> None:
    """中间件不得吞掉 `http.disconnect`，也不得给出"永不挂起"的合成消息。

    两条都踩过（实测整条 SSE 请求永久挂住）：`StreamingResponse` 的收尾监听器会一直读
    `receive()`——吞掉 disconnect 响应就永远结束不了；而返回一个立刻可用的合成消息，会让那个
    循环变成死循环并把整个事件循环饿死。

    GET 直接透传（正文不是 GET 的事）；chunked 的 POST 先缓冲、再**合并成一条**回放，之后必须
    把原始 receive 的消息（这里就是 disconnect）原样转下去。
    """
    chunked_post = [
        {"type": "http.request", "body": b"x", "more_body": True},
        {"type": "http.request", "body": b"y", "more_body": False},
    ]
    plain_get = [{"type": "http.request", "body": b"", "more_body": False}]
    for method, messages, replayed_length in (("GET", plain_get, 0), ("POST", chunked_post, 2)):
        seen = await _drive_middleware(method, messages)
        assert seen == [("http.request", replayed_length), ("http.disconnect", 0)], method


@pytest.mark.asyncio
async def test_events_stream_starts_with_a_snapshot_and_heartbeats(open_api: OpenApi) -> None:
    client, runtime = open_api(decision="chat", web_overrides=SSE_WEB)
    session_id = _create_session(client)

    frames = await _collect_stream(create_app(runtime), _sse_url(session_id), limit=5.0)

    kinds = [name for name, _data in frames]
    assert kinds[0] == "session_snapshot"  # 快照优先（§15.3）
    assert kinds[-1] == "stream_idle_timeout"  # 空闲上限到了就主动结束这条连接
    # `data` 是完整信封：前端可以按生成的 TS 类型直接解析。
    envelope = dict(frames[0][1])
    assert envelope["event_type"] == "session_snapshot"
    assert envelope["session_id"] == session_id
    assert envelope["stream_sequence"] >= 1
    assert envelope["payload"]["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_events_stream_projects_the_turn_that_happens_while_it_is_open(open_api: OpenApi) -> None:
    """连接建立后提交一轮：**同一个连接**上就能收到本轮增量，`data` 是完整信封。"""
    client, runtime = open_api(
        decision="chat", reply="假回答", web_overrides={**SSE_WEB, "sse_idle_timeout_seconds": 3.0}
    )
    session_id = _create_session(client)
    app = create_app(runtime)

    stream = asyncio.create_task(_collect_stream(app, _sse_url(session_id), limit=10.0))
    # 等到订阅真的建立（快照已入队）再提交——不靠 sleep 猜时间。
    for _ in range(200):
        if runtime.service._bus.subscriber_count(session_id) >= 1:
            break
        await asyncio.sleep(0.01)
    assert runtime.service._bus.subscriber_count(session_id) == 1
    await asyncio.to_thread(runtime.service.submit_message, session_id, "你好", "req-1")

    frames = await stream
    kinds = [name for name, _data in frames]
    assert kinds[0] == "session_snapshot"
    assert "message_completed" in kinds
    # 终态是本轮最后一条业务事件（之后只剩收尾原因帧）。
    assert kinds.index("task_completed") > kinds.index("message_completed")

    payload = dict(frames[kinds.index("message_completed")][1])
    assert payload["event_type"] == "message_completed"
    assert payload["session_id"] == session_id
    assert payload["payload"]["role"] == "assistant"


@pytest.mark.asyncio
async def test_event_trace_exports_without_keys_or_hidden_reasoning(
    open_api: OpenApi, tmp_path: Path
) -> None:
    """T37 自动门槛：把一次真实运行的**事件轨迹**导出成文件，里面不许有密钥或隐藏思维链。

    轨迹就是这条 SSE 连接上收到的全部帧，而它同时也是页面能看到的全部内容——所以"页面上看不见的
    东西"不该出现在轨迹里；反过来，轨迹干净也就证明投影层没有把密钥或 `reasoning_content` 带出去。
    导出按验收记录要留的形状落盘（一份 JSON），断言因此落在**盘上的字节**上，而不是内存对象上。

    场景取"一次真的执行了工具的问答"：工具参数与输出摘要是最容易被顺手带出密钥与私人路径的地方，
    拿它当样品比纯聊天更有说服力。
    """
    (tmp_path / WORKSPACE_NAME).mkdir(exist_ok=True)
    (tmp_path / WORKSPACE_NAME / "notes.txt").write_text("笔记正文\n", encoding="utf-8")
    client, runtime = open_api(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "notes.txt"}, "id": "call-1"}],
        web_overrides={**SSE_WEB, "sse_idle_timeout_seconds": 3.0},
    )
    session_id = _create_session(client)
    app = create_app(runtime)

    stream = asyncio.create_task(_collect_stream(app, _sse_url(session_id), limit=15.0))
    # 等到订阅真的建立（快照已入队）再提交——不靠 sleep 猜时间。
    for _ in range(200):
        if runtime.service._bus.subscriber_count(session_id) >= 1:
            break
        await asyncio.sleep(0.01)
    await asyncio.to_thread(runtime.service.submit_message, session_id, "读一下 notes.txt", "req-1")
    frames = await stream

    kinds = [name for name, _data in frames]
    assert kinds[0] == "session_snapshot"
    assert {"tool_completed", "message_completed", "task_completed"} <= set(kinds)
    assert kinds[-1] == "stream_idle_timeout"

    trace_path = tmp_path / "event-trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "events": [{"event": name, "data": data} for name, data in frames],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    exported = trace_path.read_text(encoding="utf-8")
    # `sk-test-secret` 是 conftest 给 `llm.api_key_env` 设的那个值：轨迹里连它都不该出现。
    assert "sk-test-secret" not in exported
    lowered = exported.lower()
    for forbidden in ("api_key", "authorization", "reasoning_content", "system_prompt"):
        assert forbidden not in lowered, forbidden


@pytest.mark.asyncio
async def test_reconnect_gets_a_fresh_snapshot_without_re_running_the_turn(open_api: OpenApi) -> None:
    client, runtime = open_api(decision="chat", reply="假回答", web_overrides=SSE_WEB)
    session_id = _create_session(client)
    assert _submit(client, session_id, "你好", "req-1").status_code == 200
    calls = runtime.model.decision_calls
    app = create_app(runtime)

    def snapshot_id(frames: list[tuple[str, dict[str, Any]]]) -> str:
        return str(frames[0][1]["payload"]["snapshot_id"])

    first = await _collect_stream(app, _sse_url(session_id), limit=5.0)
    assert _submit(client, session_id, "第二句", "req-2").status_code == 200
    again = await _collect_stream(app, _sse_url(session_id), limit=5.0)

    # 重连先给**新**快照（内容变了），而不是重放旧事件。
    assert snapshot_id(first) != snapshot_id(again)
    # 重连没有重新跑图、也没有重放工具（幂等只认 client_request_id，不认连接）。
    assert runtime.model.decision_calls == calls + 1
    rows = runtime.database.connection.execute("SELECT COUNT(*) FROM request_ledger").fetchone()
    assert rows is not None and int(rows[0]) == 2


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_slow_consumer_queue_is_capped_by_the_producer(api: tuple[TestClient, ApiRuntime]) -> None:
    """落后就不假装补齐，而且**内存必须有界**。

    复审要求覆盖的是**生产者持续入队**这一侧：客户端停读时，图那边还在推事件。只看"泵排出的一批多长"
    会漏掉两次轮询之间的无限积压——所以封顶发生在 `_push`（生产者路径），这里直接从那一侧灌。
    """
    _client, runtime = api
    subscription = EventSubscription("s-1", max_pending=3)

    for sequence in range(50):
        # 生产者的入队路径（总线 publish 走的就是它）。
        subscription._push(SimpleNamespace(stream_sequence=sequence))

    assert subscription.pending_count == 3  # 封顶，不再增长（生产者视角）
    assert subscription.overflowed is True
    # 注意：这些事件此刻在"边界建立期"的缓冲里，`poll()` 只排投递队列——泵会先发边界快照再放行它们。
    assert subscription.poll() == []

    # 溢出标记只报一次事实：泵看到它就发 stream_overrun 并关掉这条连接（见下面的帧级用例）。
    runtime_stub = SimpleNamespace(
        web=runtime.web.model_copy(update={"sse_max_pending_events": 2}),
        service=SimpleNamespace(unsubscribe_events=lambda _sub: None),
    )
    frames = [
        frame async for frame in event_stream(runtime_stub, _StubSubscription([], overflowed=True))
    ]
    assert "event: stream_overrun" in "\n".join(frames)


@pytest.mark.asyncio
async def test_the_overrun_frame_is_sent_as_soon_as_the_queue_overflows(
    api: tuple[TestClient, ApiRuntime],
) -> None:
    """溢出后泵立刻发 `stream_overrun` 并结束，不继续等事件。"""
    _client, runtime = api
    runtime_stub = SimpleNamespace(
        web=runtime.web.model_copy(update={"sse_max_pending_events": 2}),
        service=SimpleNamespace(unsubscribe_events=lambda _sub: None),
    )

    text = "\n".join(
        [frame async for frame in event_stream(runtime_stub, _StubSubscription([], overflowed=True))]
    )

    assert "event: stream_overrun" in text
    assert "task_completed" not in text


def test_events_for_an_unknown_session_is_a_json_404(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    response = client.get(_sse_url("s-nope"))
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == SESSION_NOT_FOUND


# ---- 模型连接与初始设置模式（T29A；§6.8、§15.9）-----------------------------

CONNECTION_PAYLOAD: dict[str, Any] = {
    "display_name": "局域网网关",
    "base_url": "https://api.example.com/v1",
    "model": "mid-model",
    "context_window_tokens": 32000,
    "max_tokens": 1000,
}


def test_snapshot_reports_whether_the_next_answer_streams(
    api: tuple[TestClient, ApiRuntime]
) -> None:
    """T74（§19.4）：页面判"在不在流式"**只能用**服务端给的 `answer_streaming`。

    这里跑连接模式的两条来源：① 会话选中的连接；② 没有会话选择时跟随**默认连接**（旧配置模式
    取 `llm.streaming` 的那条在 `test_application.py` 里按配置各跑一遍）。两条都对得上，页面才
    敢说"未流式"——否则它只能靠"有没有增量"猜，而那在刚连上、回答极短时是错的。
    """
    client, _ = api
    imported = client.get("/api/model-connections").json()[0]
    assert imported["streaming"] is False  # 旧配置幂等导入的那条默认不开流式

    streamed = client.post(
        "/api/model-connections", json={**CONNECTION_PAYLOAD, "streaming": True}
    )
    assert streamed.status_code == 200, streamed.text
    streamed_id = streamed.json()["connection_id"]

    session_id = _create_session(client)
    # ① 还没选：跟随默认连接（没开流式）⇒ 快照说"未流式"。
    assert client.get(f"/api/sessions/{session_id}").json()["answer_streaming"] is False

    # ② 选中开了流式的连接 ⇒ 快照改口（页面据此不再显示"未流式"）。
    selected = client.put(
        f"/api/sessions/{session_id}/model",
        json={
            "connection_id": streamed_id,
            "expected_selection": {"connection_id": None, "config_version": None},
            "expected_version": int(streamed.json()["config_version"]),
        },
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["answer_streaming"] is True
    assert client.get(f"/api/sessions/{session_id}").json()["answer_streaming"] is True

    # 回到"跟随默认"⇒ 快照也回到默认那条的口径（不是把上一次的选择留在页面上）。
    cleared = client.put(
        f"/api/sessions/{session_id}/model",
        json={
            "connection_id": None,
            "expected_selection": {
                "connection_id": streamed_id,
                "config_version": int(streamed.json()["config_version"]),
            },
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["answer_streaming"] is False


def test_model_connection_routes_cover_the_lifecycle(api: tuple[TestClient, ApiRuntime]) -> None:
    """六条连接路由 + 会话模型路由：读接口不回密钥、编辑要版本号、删除只停用（§6.8）。"""
    client, runtime = api

    imported = client.get("/api/model-connections").json()
    assert len(imported) == 1  # 旧 config.yaml 幂等导入的那条
    assert imported[0]["is_default"] is True
    assert imported[0]["key_status"] == "configured"  # conftest 给 api_key_env 设了值
    assert imported[0]["transport"] == "https"
    assert "sk-test-secret" not in json.dumps(imported)  # 读接口不含密钥

    created = client.post(
        "/api/model-connections", json={**CONNECTION_PAYLOAD, "api_key": "sk-second-secret"}
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "unverified"  # 保存即可用，但状态如实是"未验证"
    assert body["config_version"] == "1"
    # 持久凭据可用时是 configured；安全降级到进程内时必须如实标为 session_only。
    credential_kind, _ = runtime.service.credential_store_state()
    assert body["key_status"] == ("configured" if credential_kind == "os" else "session_only")
    assert "sk-second-secret" not in created.text
    connection_id = body["connection_id"]

    # 编辑必须带当前版本号：缺了给可执行的 422，过期给 409——都不是 500。
    missing = client.patch(f"/api/model-connections/{connection_id}", json=CONNECTION_PAYLOAD)
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "MODEL_SETTINGS_INVALID"

    conflict = client.patch(
        f"/api/model-connections/{connection_id}",
        json={**CONNECTION_PAYLOAD, "expected_version": 99},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "MODEL_SETTINGS_CONFLICT"

    edited = client.patch(
        f"/api/model-connections/{connection_id}",
        json={**CONNECTION_PAYLOAD, "display_name": "改名了", "expected_version": 1},
    )
    assert edited.status_code == 200
    assert edited.json()["config_version"] == "2"

    # 字段规则由服务端裁定：错误消息是脱敏后的规则说明。
    bad = client.post(
        "/api/model-connections",
        json={**CONNECTION_PAYLOAD, "base_url": "http://192.168.1.9:11434/v1"},
    )
    assert bad.status_code == 422
    assert "HTTPS" in bad.json()["error"]["message"]

    # 默认与会话选择分离；写入**必须**带上"我看到的当前选择"（§6.1：两个页面不能互相覆盖）。
    imported_id = imported[0]["connection_id"]
    default_now = {"connection_id": imported_id, "config_version": 1}
    assert client.put(
        "/api/model-connections/default",
        json={
            "connection_id": connection_id,
            "expected_selection": default_now,
            "expected_version": int(edited.json()["config_version"]),
        },
    ).status_code == 200

    # 不填就直接 422：不填等于"我不管别人改了什么，覆盖掉"。
    assert client.put(
        "/api/model-connections/default", json={"connection_id": connection_id}
    ).status_code == 422
    assert client.put(
        "/api/model-connections/default",
        json={"connection_id": connection_id, "expected_selection": default_now},
    ).status_code == 422  # 目标版本也不能省略
    assert client.put(
        "/api/model-connections/default",
        json={
            "connection_id": connection_id,
            "expected_selection": {"connection_id": None},
            "expected_version": int(edited.json()["config_version"]),
        },
    ).status_code == 422  # 观测两字段必须显式给齐，即使值为 null

    # 另一个页面拿着**过期**的观测（仍以为默认是导入那条）来写 → 409，且当前默认没被覆盖。
    stale = client.put(
        "/api/model-connections/default",
        json={
            "connection_id": connection_id,
            "expected_selection": default_now,
            "expected_version": int(edited.json()["config_version"]),
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "MODEL_SETTINGS_CONFLICT"
    assert [item["connection_id"] for item in client.get("/api/model-connections").json() if item["is_default"]] == [
        connection_id
    ]

    session_id = _create_session(client)
    assert client.put(
        f"/api/sessions/{session_id}/model",
        json={
            "connection_id": connection_id,
            "expected_selection": {"connection_id": None, "config_version": None},
        },
    ).status_code == 422  # 非空目标必须带其版本
    selected = client.put(
        f"/api/sessions/{session_id}/model",
        json={
            "connection_id": connection_id,
            "expected_selection": {"connection_id": None, "config_version": None},
            "expected_version": int(edited.json()["config_version"]),
        },
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["session_id"] == session_id
    # 快照带上会话**自己的**选择：页面据此把观测原样回传（`null` 表示跟随默认）。
    # 版本号是**被选中连接当时的版本**（这条连接上面被编辑过，所以不是 1）。
    assert selected.json()["model_selection"] == {
        "connection_id": connection_id,
        "config_version": edited.json()["config_version"],
    }

    # 第二个页面拿着"这个会话还没有自己的选择"的旧观测来改 → 409，原选择仍在。
    stale_session = client.put(
        f"/api/sessions/{session_id}/model",
        json={
            "connection_id": None,
            "expected_selection": {"connection_id": None, "config_version": None},
        },
    )
    assert stale_session.status_code == 409
    assert stale_session.json()["error"]["code"] == "MODEL_SETTINGS_CONFLICT"

    # 拿着**看对了**的观测"恢复跟随默认" → 200，快照里会话自己的选择回到 null。
    cleared = client.put(
        f"/api/sessions/{session_id}/model",
        json={
            "connection_id": None,
            "expected_selection": {
                "connection_id": connection_id,
                "config_version": int(edited.json()["config_version"]),
            },
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["model_selection"] is None
    assert client.put(
        f"/api/sessions/{session_id}/model",
        json={
            "connection_id": None,
            "expected_selection": {"connection_id": None, "config_version": None},
            "expected_version": int(edited.json()["config_version"]),
        },
    ).status_code == 422  # 跟随默认没有目标版本

    # 探测只更新这条连接的检测结果（分项判定的细节在单元测试里，这里验路由形状）。
    tested = client.post(f"/api/model-connections/{connection_id}/test")
    assert tested.status_code == 200
    assert tested.json()["status"] in {"verified", "unverified", "failed"}

    # 删除 = 停用：从可用列表消失（历史与用量仍在库里，见设置库的单元测试）；再操作它得到 404。
    retired = client.delete(f"/api/model-connections/{connection_id}")
    assert retired.status_code == 200
    assert connection_id not in [
        item["connection_id"] for item in client.get("/api/model-connections").json()
    ]
    again = client.post(f"/api/model-connections/{connection_id}/test")
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "MODEL_CONNECTION_NOT_FOUND"


def test_editing_the_selected_default_keeps_the_list_observation_valid(
    api: tuple[TestClient, ApiRuntime],
) -> None:
    """列表给页面的版本须与默认选择的 CAS 引用一致，编辑后不能误报 409。"""
    client, _runtime = api
    initial = client.get("/api/model-connections").json()[0]
    edited = client.patch(
        f"/api/model-connections/{initial['connection_id']}",
        json={
            **CONNECTION_PAYLOAD,
            "expected_version": int(initial["config_version"]),
        },
    )
    assert edited.status_code == 200, edited.text
    listed = client.get("/api/model-connections").json()[0]
    assert listed["is_default"] is True
    assert listed["config_version"] == edited.json()["config_version"]
    selected = client.put(
        "/api/model-connections/default",
        json={
            "connection_id": listed["connection_id"],
            "expected_selection": {
                "connection_id": listed["connection_id"],
                "config_version": int(listed["config_version"]),
            },
            "expected_version": int(listed["config_version"]),
        },
    )
    assert selected.status_code == 200, selected.text


def test_setup_mode_works_without_any_config_and_becomes_usable_after_saving_a_connection(
    tmp_path: Path, model_factory: ModelFactory
) -> None:
    """全新安装（没有 config.yaml、没有 Key）：服务照常起、设置页可用，加完连接**不用重启**就能对话。"""
    workspace = tmp_path / WORKSPACE_NAME
    workspace.mkdir(exist_ok=True)
    app_config = config_module.load_config(tmp_path / "missing.yaml", allow_missing=True)
    runtime = open_runtime(
        app_config,
        base_dir=tmp_path,
        model=model_factory(decision="chat", reply="假回答"),
        instance_lock=False,
    )
    try:
        # `base_url` 必须是本机回环：边界中间件挡外站 Host 是**产品行为**，测试也要从允许的 Host 发起。
        client = TestClient(create_app(runtime), base_url=LOCAL_BASE_URL)
        health = client.get("/api/health").json()
        assert health["model_configured"] is False
        assert health["model_name"] == ""
        assert client.get("/api/model-connections").json() == []

        session_id = _create_session(client)
        refused = _submit(client, session_id, "你好", "req-1")
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "MODEL_CONNECTION_REQUIRED"

        saved = client.post(
            "/api/model-connections", json={**CONNECTION_PAYLOAD, "api_key": "sk-fresh"}
        )
        assert saved.status_code == 200, saved.text
        # 初始设置模式下本来就没有默认连接，所以要如实说"我看到的是空的"（§6.1）。
        assert client.put(
            "/api/model-connections/default",
            json={
                "connection_id": saved.json()["connection_id"],
                "expected_selection": {"connection_id": None, "config_version": None},
                "expected_version": int(saved.json()["config_version"]),
            },
        ).status_code == 200

        # 不重启：立刻建立客户端并完成一轮。
        assert client.get("/api/health").json()["model_configured"] is True
        done = _submit(client, session_id, "你好", "req-2")
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "completed"
    finally:
        runtime.close()


# ---- 权限（T38；§1.5）------------------------------------------------------


def test_permissions_endpoint_reports_the_factory_default(
    api: tuple[TestClient, ApiRuntime],
) -> None:
    client, _runtime = api

    body = client.get("/api/permissions").json()

    assert body["mode"] == "whitelist" and body["default_mode"] == "whitelist"
    assert body["grants"] == []
    # 设置库打得开：改档位与加授权都存得下来。
    assert body["store_unavailable"] is False


def test_permissions_mode_and_grants_round_trip(
    api: tuple[TestClient, ApiRuntime], tmp_path: Path
) -> None:
    """保存即生效：写接口直接回完整视图，GET 读回同一份；撤销幂等。"""
    client, _runtime = api
    outside = tmp_path / "outside"
    outside.mkdir()

    switched = client.put("/api/permissions", json={"mode": "allow"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["mode"] == "allow"

    created = client.post(
        "/api/permissions/grants", json={"path": str(outside), "access": "read"}
    )
    assert created.status_code == 200, created.text
    grants = created.json()["grants"]
    assert len(grants) == 1
    assert grants[0]["path"] == normalize_grant_path(outside)
    assert grants[0]["access"] == "read"
    assert client.get("/api/permissions").json()["grants"] == grants

    # 同一目标的另一种写法是改写（级别刷新），不是加第二条。
    rewritten = client.post(
        "/api/permissions/grants", json={"path": outside.as_posix(), "access": "read_write"}
    )
    assert [item["access"] for item in rewritten.json()["grants"]] == ["read_write"]

    removed = client.delete("/api/permissions/grants", params={"path": str(outside)})
    assert removed.status_code == 200 and removed.json()["grants"] == []
    # 反复撤销同一条也是 200（幂等）。
    assert client.delete(
        "/api/permissions/grants", params={"path": str(outside)}
    ).status_code == 200


def test_permissions_reject_unknown_mode_blank_path_and_protected_target(
    api: tuple[TestClient, ApiRuntime],
) -> None:
    client, _runtime = api

    assert client.put("/api/permissions", json={"mode": "yolo"}).status_code == 422
    assert (
        client.post("/api/permissions/grants", json={"path": "  ", "access": "read"}).status_code
        == 422
    )

    refused = client.post(
        "/api/permissions/grants", json={"path": str(Path.home()), "access": "read_write"}
    )
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    # 复用冻结错误码表里的取值；展示文本里**不回显**用户填的绝对路径（§15.8）。
    assert error["code"] == MODEL_SETTINGS_INVALID
    assert str(Path.home()) not in json.dumps(error, ensure_ascii=False)


# ---- 真机（真 ASGI 服务器）验收 ---------------------------------------------


def _raw_status(port: int, *, host: str, path: str = "/api/health") -> int:
    """用裸 socket 发一条 HTTP/1.1 请求并返回状态码。

    只有这样才能**完全控制** `Host` 头：客户端库（httpx）会在自己的传输层改写它，用它验证
    "外站 Host 必须被拒"会得到一个永远通过的假结论。
    """
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii"))
        status_line = sock.makefile("rb").readline().decode("latin-1")
    return int(status_line.split(" ")[1])


def _start_uvicorn(app: Any) -> tuple[uvicorn.Server, threading.Thread, int]:
    """在后台线程里起一个真 uvicorn，端口交给系统分配；返回 `(server, thread, port)`。"""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", timeout_keep_alive=2)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        if server.started and server.servers:
            return server, thread, int(server.servers[0].sockets[0].getsockname()[1])
        time.sleep(0.02)
    server.should_exit = True
    raise AssertionError("uvicorn 没能在 15s 内启动")


def test_sse_over_a_real_server_streams_the_turn(open_api: OpenApi) -> None:
    """真机验收：真 ASGI 服务器（uvicorn）下能连上事件流、收到本轮增量、自己收尾。

    为什么非要有这条：`TestClient` 会**整体缓冲**流式响应，而生产路径（uvicorn 声明 ASGI spec 2.3）
    走的是 `StreamingResponse` 的任务组收尾分支——"中间件吞掉 `http.disconnect`"这类缺陷只在真服务器
    下才暴露（实测那条请求会永久挂住，还会把事件循环饿死）。本用例顺带覆盖边界中间件在真服务器上的行为。
    """
    client, runtime = open_api(
        decision="chat", reply="假回答", web_overrides={**SSE_WEB, "sse_idle_timeout_seconds": 0.6}
    )
    session_id = _create_session(client)
    server, thread, port = _start_uvicorn(create_app(runtime))
    base = f"http://127.0.0.1:{port}"
    try:
        # 边界中间件在真服务器上照样拦外站 Host（防 DNS rebinding）。这里必须用**裸 socket**：
        # `httpx` 会把自定义 `Host` 头换成 URL 里的主机（实测期望 evil.example、服务端收到
        # 127.0.0.1:<port>），用它写这条断言等于永远通过。
        assert _raw_status(port, host="evil.example") == 403
        assert _raw_status(port, host=f"127.0.0.1:{port}") == 200

        # 显式关掉 `trust_env`：本机服务不该被环境里的代理/凭据配置影响。
        with (
            httpx.Client(trust_env=False, timeout=20.0) as http,
            http.stream("GET", f"{base}{_sse_url(session_id)}") as response,
        ):
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            # 订阅确实建立之后再提交，不靠 sleep 猜时间。
            for _ in range(400):
                if runtime.service._bus.subscriber_count(session_id) >= 1:
                    break
                time.sleep(0.01)
            assert runtime.service._bus.subscriber_count(session_id) == 1

            posted = http.post(
                f"{base}/api/sessions/{session_id}/messages",
                json={"text": "你好", "client_request_id": "req-1"},
            )
            assert posted.status_code == 200, posted.text
            lines = list(response.iter_lines())

        frames = _frames(lines)
        kinds = [name for name, _data in frames]
        assert kinds[0] == "session_snapshot"
        assert "message_completed" in kinds
        assert kinds.index("task_completed") > kinds.index("message_completed")
        assert not [line for line in lines if line.startswith("id:")]
    finally:
        server.should_exit = True
        thread.join(timeout=15)


# ---- 契约面与依赖方向 -------------------------------------------------------


def test_data_center_read_only_api_and_filter_validation(api: tuple[TestClient, ApiRuntime]) -> None:
    client, runtime = api
    calls = runtime.model.decision_calls
    assert client.get("/api/data-center/usage").status_code == 200
    assert client.get("/api/data-center/memories").status_code == 200
    assert client.get("/api/data-center/memories?workspace_id=unknown").status_code == 404
    assert client.get("/api/data-center/usage?workspace_id=unknown").status_code == 404
    for query in ("offset=-1", "limit=101", "since=2026-10-01T00:00:00", "since=2026-10-02T00:00:00Z&until=2026-10-01T00:00:00Z"):
        assert client.get(f"/api/data-center/usage?{query}").status_code == 422
    assert client.get("/api/data-center/memories?kind=bad").status_code == 422
    assert client.post("/api/data-center/memories", json={}).status_code == 405
    assert runtime.model.decision_calls == calls


def test_openapi_surface_is_controlled(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    response = client.get("/api/openapi.json")
    assert response.status_code == 200
    schema = response.json()

    assert set(schema["paths"]) == {
        "/api/health",
        "/api/workspaces",
        "/api/workspaces/pick",
        "/api/workspaces/select",
        "/api/workspaces/{workspace_id}",
        "/api/sessions",
        "/api/sessions/{session_id}",
        "/api/sessions/{session_id}/select",
        "/api/sessions/{session_id}/mode",
        "/api/sessions/{session_id}/recovery",
        # v0.6 T60：取消在跑的任务（幂等，§17.4）
        "/api/sessions/{session_id}/cancel",
        # v0.6 T62：任务中心（只读索引，不读会话 Checkpoint，§17.6）
        "/api/tasks",
        "/api/sessions/{session_id}/archive",
        "/api/sessions/{session_id}/restore",
        "/api/sessions/{session_id}/events",
        "/api/sessions/{session_id}/messages",
        "/api/sessions/{session_id}/conversation-map",
        "/api/sessions/{session_id}/turns/{turn_id}",
        "/api/sessions/{session_id}/branches/{branch_id}",
        "/api/sessions/{session_id}/branches/{branch_id}/select",
        "/api/sessions/{session_id}/approvals/{approval_id}",
        "/api/sessions/{session_id}/model",
        "/api/model-connections",
        "/api/model-connections/{connection_id}",
        "/api/model-connections/{connection_id}/test",
        "/api/model-connections/default",
        "/api/permissions",
        # v0.5 T56：当前档位与能力状态（只讲能力、不讲工具，§16.6）
        "/api/capabilities",
        "/api/preferences",
        "/api/preferences/memory/test",
        "/api/data-center/memories",
        "/api/data-center/usage",
        "/api/permissions/grants",
    }
    text = json.dumps(schema, ensure_ascii=False)
    # 内部**类型名**与模块名不得出现。刻意不查 "checkpoint" 这个词：公开 DTO 里本来就有
    # `last_checkpoint_id` 这样的字段（T27 契约），真正要防的是内部类型与模块泄漏。
    for forbidden in ("AgentState", "AnyMessage", "BaseMessage", "ToolCall", "langchain", "langgraph", "streamlit"):
        assert forbidden not in text, forbidden
    # 私有基类不得进 Schema（`_Dto` 之类）。
    assert not [name for name in schema.get("components", {}).get("schemas", {}) if name.startswith("_")]
    # 错误信封在 OpenAPI 里（前端类型才能生成它）。
    assert "ErrorResponse" in text


def test_the_committed_openapi_matches_the_real_application(api: tuple[TestClient, ApiRuntime]) -> None:
    """落盘 OpenAPI == 真实应用注册的面（前端类型从它生成，不能悄悄漂移）。

    两件事都要守：① 用占位运行时导出的文档与真实应用一致（否则前端会按一份假契约生成类型）；
    ② 再导出与落盘文件**逐字节**一致（否则生成物无法入库、diff 会失真）。
    """
    client, _runtime = api
    served = client.get("/api/openapi.json").json()
    committed_path = Path(__file__).resolve().parents[1] / OPENAPI_PATH
    assert committed_path.is_file(), f"缺受控文档：{OPENAPI_PATH}"

    # 按字节比（`read_text()` 会翻 CRLF，测不出行尾差异）；再比解析后的结构，锚住真实应用的面。
    committed = committed_path.read_bytes().decode("utf-8")
    assert committed == openapi_json()
    assert json.loads(committed) == served


def test_api_layer_does_not_import_page_hosts_or_graph_internals() -> None:
    """`api/` 不得依赖 Streamlit / 页面宿主 / Graph 节点；Graph 依赖走白名单。"""
    api_dir = Path(__file__).resolve().parents[1] / "src" / "local_agent" / "api"
    allowed_graph_imports = (
        "local_agent.graph import builder",
        "local_agent.graph.projection",
        "local_agent.graph.scope",
    )
    for path in sorted(api_dir.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            for forbidden in ("streamlit", "local_agent.ui", "local_agent.web", "local_agent.cli"):
                assert forbidden not in stripped, f"{path.name}：{stripped}"
            if "local_agent.graph" in stripped:
                assert any(ok in stripped for ok in allowed_graph_imports), f"{path.name}：{stripped}"


def test_application_layer_stays_free_of_http_frameworks() -> None:
    """反向：应用层不得导入 HTTP 框架（§15.1 的单向依赖）。"""
    application_dir = Path(__file__).resolve().parents[1] / "src" / "local_agent" / "application"
    for path in sorted(application_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("fastapi", "starlette", "uvicorn"):
            assert forbidden not in text, f"{path.name} 依赖了 {forbidden}"


def test_status_mapping_covers_every_application_code() -> None:
    """每个公开错误码都得有明确状态码：表里没有的会落到 500，那种"未预期"必须是**有意的**。"""
    from local_agent.application import errors as app_errors

    assert status_for_code(SESSION_BUSY) == 409
    assert status_for_code(RECOVERY_REQUIRED) == 409
    assert status_for_code(SESSION_NOT_FOUND) == 404
    assert status_for_code("NOT_A_REAL_CODE") == 500
    # 会话/工作区/审批类的码必须有映射（不能靠 500 兜底）。
    for code in (SESSION_BUSY, IDEMPOTENCY_CONFLICT, RECOVERY_REQUIRED, SESSION_NOT_FOUND, WORKSPACE_NOT_FOUND, APPROVAL_EXPIRED):
        assert status_for_code(code) != 500, code
    assert app_errors.ERROR_CATEGORIES  # 分类集合仍在（契约未变）


# ---- 任务中心：真源与"等你处理"（T62 的**两个补充**，架构 §17.6）------------
#
# 任务中心的基础验收（状态 / 计数 / 顺序 / 字段门槛 / limit / 筛选与归档 / 只读 / 空工作区）在文件末尾
# 那一节，**不在这里重复**。这一节只补它没覆盖的两条口径：
#   ① **不读任何会话的 Checkpoint**——§17.6 明写"数据只来自任务索引 + 会话索引"，所以把那条路封死
#      之后它必须照常工作（"不读"这件事要有证据，不是靠注释声明）；
#   ② **`needs_review` 与 `waiting_user` 一样要被发现**：两者都进入口角标（§17.6），
#      这是 T61 的三类判定在页面上的落点。


def _store(runtime: ApiRuntime) -> JobStore:
    """任务索引的直连读写（与 `_plant` 同一把锁），用于把索引摆成想要的形状。"""
    return JobStore(runtime.database.connection, lock=runtime.database.checkpointer.lock)


def test_task_center_never_reads_a_session_checkpoint(
    api: tuple[TestClient, ApiRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """把"读 Checkpoint"这条路封死，任务中心照样工作——它只有索引这一个真源（§17.6）。"""
    client, runtime = api
    sid = _create_session(client)
    assert _submit(client, sid, "你好", "tasks-no-checkpoint").json()["status"] == "completed"

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("任务中心不应读会话的 Checkpoint")

    monkeypatch.setattr(type(runtime.service), "_read", boom)

    page = _tasks(client)

    assert [item["session_id"] for item in page["tasks"]] == [sid]


def test_task_center_surfaces_what_waits_for_the_user(open_api: OpenApi) -> None:
    """入口角标 = 在等审批 + 需要人工核对；两者要能在同一页里被发现（§17.6 与 T61 的三类判定接通）。"""
    call = {"name": "delete_file", "args": {"path": "note.txt"}, "id": "tasks-badge-call"}
    client, runtime = open_api(decision="tool", reply="好的", tool_calls=[call])
    sid = _create_session(client)
    assert _submit(client, sid, "删除 note.txt", "tasks-badge").json()["status"] == "awaiting_approval"
    # 重启收口留下的 `needs_review`（T61）也必须出现在任务中心：它同样属于"等你核对"
    planted = _plant(runtime, sid, JOB_STATE_RUNNING)
    _store(runtime).update_state(planted.job_id, JOB_STATE_NEEDS_REVIEW, detail=NEEDS_REVIEW_DETAIL)

    page = _tasks(client)

    by_id = {item["task_id"]: item for item in page["tasks"]}
    assert by_id[planted.job_id]["state"] == JOB_STATE_NEEDS_REVIEW
    assert any(item["state"] == JOB_STATE_WAITING_USER for item in page["tasks"])
    assert page["pending_count"] >= 2
    # 原因由服务端写好、页面原样显示（"不自动重跑"那句文案在前端，这里钉住它的依据）
    assert "可能仍有残留进程" in (by_id[planted.job_id]["detail"] or "")


# ---- 入口 -------------------------------------------------------------------


def test_entry_point_starts_in_setup_mode_without_a_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺 `config.yaml` **不再是**启动失败（§15.9 初始设置模式）：服务起来、设置页可用。"""
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)

    # `--no-open-browser`：测试不该在跑的时候弹出真实浏览器（T40 的默认行为是打开）。
    code = api_main.main(
        ["--config", str(tmp_path / "missing.yaml"), "--env", str(tmp_path / ".env"), "--no-open-browser"]
    )

    assert code == 0


def test_entry_point_reports_config_errors_without_starting(tmp_path: Path) -> None:
    """只有"缺文件"才进设置模式；文件在但**不合法**时照旧启动失败并给出原因。"""
    bad = tmp_path / "config.yaml"
    bad.write_text("llm: [这不是映射]\n", encoding="utf-8")

    code = api_main.main(["--config", str(bad), "--env", str(tmp_path / ".env")])

    assert code == 1


def test_entry_point_pins_a_single_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """官方入口必须 `workers=1`（§15.1：多 worker 是不受支持配置）。"""
    source = Path(__file__).resolve().parents[1] / "config.yaml.example"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))

    assert (
        api_main.main(
            ["--config", str(config_path), "--env", str(tmp_path / ".env"), "--no-open-browser"]
        )
        == 0
    )

    assert captured["workers"] == 1
    assert captured["host"] == "127.0.0.1"  # 默认只绑回环
    assert captured["timeout_keep_alive"] == config_module.WebConfig().keepalive_seconds


def test_non_loopback_host_needs_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="allow_non_loopback"):
        config_module.WebConfig(host="0.0.0.0")
    assert config_module.WebConfig(host="0.0.0.0", allow_non_loopback=True).host == "0.0.0.0"
    assert config_module.is_loopback_host("localhost") is True
    assert config_module.is_loopback_host("example.com") is False


# ------------------------------------------------------------- v0.6 T58 后台任务


def _enable_background(runtime: ApiRuntime) -> JobStore:
    """把已装配运行时的任务运行时换成 `background=true`（用的是与 container 同一对装配件）。

    为什么不在配置里开：`open_api` 夹具在 `open_runtime` 之前就固定了配置形状。这里换运行时
    用的是同一对对象（`JobStore` + `JobRuntime`）与**同一把锁**，所以测到的仍是生产路径。
    """
    assert runtime.service._jobs is not None
    runtime.service._jobs.close()
    store = JobStore(runtime.database.connection, lock=runtime.database.checkpointer.lock)
    runtime.service._jobs = JobRuntime(
        store, background=True, max_concurrent_jobs=1, on_transition=runtime.service._on_job_transition
    )
    return store


def _wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _stages(runtime: ApiRuntime, session_id: str) -> list[str]:
    """该会话当前 Checkpoint 里的**决策阶段序列**（架构 §4.2 的 `decision_trace`）。

    节点名本身不落库，但每个阶段都对应一次节点写入，因此它等价于"跑过哪些节点、什么顺序"。
    """
    session = runtime.database.sessions.get(session_id)
    assert session is not None
    state = runtime.service._graph.get_state(
        {"configurable": {"thread_id": session.thread_id}}
    ).values
    return [str(record.get("stage")) for record in state.get("decision_trace", [])]


def _visible(runtime: ApiRuntime, client: TestClient, session_id: str) -> dict[str, Any]:
    """会话的可见结果（只取稳定字段：状态、消息角色序列、正文、决策阶段序列）。"""
    view = client.get(f"/api/sessions/{session_id}").json()
    messages = view["messages"]
    return {
        "state": view["session_state"],
        "roles": [str(item.get("role") or item.get("kind") or "") for item in messages],
        "text": "".join(str(item.get("text") or item.get("content") or "") for item in messages),
        "stages": _stages(runtime, session_id),
    }


def test_background_submit_returns_running_and_converges_through_the_index(
    open_api: OpenApi,
) -> None:
    client, runtime = open_api(decision="chat", reply="后台回答")
    store = _enable_background(runtime)
    sid = _create_session(client)

    response = _submit(client, sid, "你好", "bg-1")

    assert response.status_code == 200, response.text
    body = response.json()
    # 提交立即返回"已被接受、在运行"，并给出任务标识（§17.2）
    assert body["status"] == "running" and body["job_id"]

    # 结局由任务索引 + Checkpoint 快照收敛，不靠 SSE
    assert _wait_for(lambda: store.get(body["job_id"]).state == JOB_STATE_COMPLETED)
    snapshot = client.get(f"/api/sessions/{sid}").json()
    assert snapshot["session_state"] == "completed"
    assert "后台回答" in json.dumps(snapshot, ensure_ascii=False)

    # 幂等不变：同 id 重试拿原结果，不再登记第二个任务
    replay = _submit(client, sid, "你好", "bg-1").json()
    assert replay["job_id"] == body["job_id"]
    assert len(store.list_for_session(sid)) == 1


def test_background_second_submit_of_the_same_session_is_busy(open_api: OpenApi) -> None:
    """单飞不变：同一会话有非终态任务时第二个提交仍 `SESSION_BUSY`，不排队（§15.2）。"""
    client, runtime = open_api(decision="chat", reply="慢回答")
    store = _enable_background(runtime)
    sid = _create_session(client)
    # 直接占住会话：登记一个非终态任务（等价于"任务还在等 worker"）
    store.create(session_id=sid)

    blocked = _submit(client, sid, "再来一次", "bg-busy")

    assert blocked.status_code == 409
    assert SESSION_BUSY in blocked.text


def test_background_and_inline_run_the_same_turn(open_api: OpenApi) -> None:
    """同一固定场景在两种调度时机下**可见结果与决策阶段序列逐项一致**（§17.2 的验收口径）。

    两种时机用**同一套运行时**（单实例锁按聊天库路径加，一个用例只能开一套），先内联跑一个会话，
    再换成后台调度跑另一个会话：调度时机是运行时的属性，不是会话的。
    """
    client, runtime = open_api(decision="chat", reply="一致的回答")

    inline_sid = _create_session(client, title="内联")
    inline_body = _submit(client, inline_sid, "同一句话", "same-1").json()
    assert inline_body["status"] == "completed" and inline_body["job_id"] is None

    store = _enable_background(runtime)
    bg_sid = _create_session(client, title="后台")
    bg_body = _submit(client, bg_sid, "同一句话", "same-2").json()
    assert bg_body["status"] == "running"
    assert _wait_for(lambda: store.get(bg_body["job_id"]).state == JOB_STATE_COMPLETED)

    inline_visible = _visible(runtime, client, inline_sid)
    background_visible = _visible(runtime, client, bg_sid)

    assert "一致的回答" in inline_visible["text"]  # 防止"两边都读空了也算相等"
    assert inline_visible["stages"]
    assert background_visible == inline_visible


@pytest.mark.asyncio
async def test_background_job_lifecycle_reaches_the_same_stream(open_api: OpenApi) -> None:
    """任务生命周期事件与轮次事件走**同一条** SSE（§17.3）：排队 → 运行 → 完成。"""
    client, runtime = open_api(
        decision="chat", reply="后台回答", web_overrides={**SSE_WEB, "sse_idle_timeout_seconds": 3.0}
    )
    _enable_background(runtime)
    session_id = _create_session(client)
    app = create_app(runtime)

    stream = asyncio.create_task(_collect_stream(app, _sse_url(session_id), limit=10.0))
    for _ in range(200):
        if runtime.service._bus.subscriber_count(session_id) >= 1:
            break
        await asyncio.sleep(0.01)
    await asyncio.to_thread(runtime.service.submit_message, session_id, "你好", "req-bg")

    frames = await stream
    job_names = {"job_queued", "job_progress", "job_waiting", "job_completed", "task_cancelled"}
    job_frames = [(name, data) for name, data in frames if name in job_names]

    assert [name for name, _ in job_frames] == ["job_queued", "job_progress", "job_completed"]
    queued = job_frames[0][1]
    # 排队时还没有轮次：task_id / job_id 非空、run_id 为空（§17.3 的信封例外）
    assert queued["task_id"] == queued["job_id"] and queued["run_id"] is None
    assert queued["payload"]["state"] == "queued"
    progress = job_frames[1][1]
    # 进 running 早于图交出轮次，因此这里仍然没有 run_id（§17.3 的信封口径）
    assert progress["payload"]["state"] == "running" and progress["run_id"] is None
    completed = job_frames[2][1]
    assert completed["payload"]["state"] == "completed" and completed["run_id"]
    # 任务结算发生在整轮收尾之后
    kinds = [name for name, _ in frames]
    assert kinds.index("job_completed") > kinds.index("task_completed")


# ------------------------------------------------------------- v0.6 T60 取消


def test_cancelled_turn_never_executes_a_tool(
    open_api: OpenApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消后**一个工具都不执行**（§17.4）：走的是既有的两道门，节点控制流没变。"""
    client, runtime = open_api(
        decision="tool",
        reply="好的",
        tool_calls=[
            {"name": "write_file", "args": {"path": "cancelled.txt", "content": "不该出现"}, "id": "c1"}
        ],
    )
    sid = _create_session(client)
    workspace = Path(runtime.database.sessions.get(sid).workspace_root)
    original = runtime.service._context

    def cancelled_context(session, env):  # type: ignore[no-untyped-def]
        context = original(session, env)
        # 把这一轮的令牌先取消掉：等价于"这一轮已经被取消"，且与真实取消走同一个令牌对象。
        assert context.cancellation is not None
        context.cancellation.cancel("测试取消")
        return context

    monkeypatch.setattr(runtime.service, "_context", cancelled_context)

    response = _submit(client, sid, "写一个文件", "cancel-1")

    assert response.status_code == 200, response.text
    # 副作用从未发生
    assert not (workspace / "cancelled.txt").exists()
    # 而且如实告诉模型为什么没执行（拒绝理由来自安全门，不是伪造的"成功"）
    snapshot = client.get(f"/api/sessions/{sid}").json()
    assert "测试取消" in json.dumps(snapshot, ensure_ascii=False)


def test_cancel_task_marks_the_job_and_is_idempotent(open_api: OpenApi) -> None:
    client, runtime = open_api(decision="chat", reply="后台回答")
    store = _enable_background(runtime)
    sid = _create_session(client)

    # 空操作：这个会话没有非终态任务
    idle = client.post(f"/api/sessions/{sid}/cancel", json={}).json()
    assert idle["status"] != "cancelled"
    assert store.list_for_session(sid) == []

    # 占住会话（等价于"任务还在等 worker"），再取消
    job = store.create(session_id=sid)
    cancelled = client.post(f"/api/sessions/{sid}/cancel", json={"reason": "用户取消"}).json()

    assert cancelled["status"] == "cancelled"
    stored = store.get(job.job_id)
    assert stored.state == JOB_STATE_CANCELLED and "用户取消" in (stored.detail or "")
    # 幂等：再取消一次是空操作，既不新建任务也不改写终态
    again = client.post(f"/api/sessions/{sid}/cancel", json={}).json()
    assert again["status"] != "cancelled"
    assert [item.job_id for item in store.list_for_session(sid)] == [job.job_id]
    assert store.get(job.job_id).detail == stored.detail


def test_expire_overdue_tasks_cancels_past_budget(open_api: OpenApi) -> None:
    """超时走**同一条取消路径**，不另写一套中止逻辑（§17.4）。"""
    client, runtime = open_api(decision="chat", reply="后台回答")
    store = _enable_background(runtime)
    sid = _create_session(client)
    job = store.create(session_id=sid)
    # 把一个任务的登记时刻挪到很久以前：等价于"它已经跑了很久"
    runtime.database.connection.execute(
        "UPDATE jobs SET created_at = ? WHERE job_id = ?",
        ("2020-01-01T00:00:00+00:00", job.job_id),
    )
    runtime.database.connection.commit()
    runtime.service._max_task_seconds = 5.0

    expired = runtime.service.expire_overdue_tasks()

    assert expired == [job.job_id]
    stored = store.get(job.job_id)
    assert stored.state == JOB_STATE_CANCELLED
    assert "max_task_seconds" in (stored.detail or "")
    # 不限时长时什么都不做（默认配置就是这一档）
    runtime.service._max_task_seconds = None
    assert runtime.service.expire_overdue_tasks() == []


# ---- 重启收口（T61，架构 §17.5）--------------------------------------------
#
# 真实"重启"= 关掉这一套运行时（释放单实例锁）再开一套：收口在 `open_runtime` 持锁时执行一次。
# 每个用例都覆盖一类判定，四类合起来就是"重启后能区分可恢复 / 失败 / 需要人工核对"。


def _plant(runtime: ApiRuntime, session_id: str, state: str):  # type: ignore[no-untyped-def]
    """把索引摆成"上一次进程留下的样子"：重启前的磁盘状态就是这个。"""
    store = JobStore(runtime.database.connection, lock=runtime.database.checkpointer.lock)
    return store.create(session_id=session_id, state=state)


def test_restart_closes_a_never_started_task_as_failed(open_api: OpenApi) -> None:
    """`queued` 是**能证明**的"什么都没跑"：重启后按失败收口，并如实说明可以安全重提（§17.5）。"""
    client, runtime = open_api(decision="chat", reply="假回答")
    sid = _create_session(client)
    job = _plant(runtime, sid, JOB_STATE_QUEUED)
    runtime.close()

    _client, reopened = open_api(decision="chat", reply="假回答")

    row = reopened.service._jobs.list_for_session(sid)[-1]
    assert row.job_id == job.job_id
    assert row.state == JOB_STATE_FAILED
    assert row.detail == UNSTARTED_DETAIL
    assert row.finished_at is not None


def test_restart_marks_an_unprovable_task_for_human_review(open_api: OpenApi) -> None:
    """"可能已部分执行"：证明不了就标 `needs_review`，不自动重放，也不假装失败（§17.5）。"""
    client, runtime = open_api(decision="chat", reply="假回答")
    sid = _create_session(client)
    job = _plant(runtime, sid, JOB_STATE_RUNNING)
    runtime.close()

    _client, reopened = open_api(decision="chat", reply="假回答")

    row = reopened.service._jobs.list_for_session(sid)[-1]
    assert row.job_id == job.job_id
    assert row.state == JOB_STATE_NEEDS_REVIEW
    assert row.detail == NEEDS_REVIEW_DETAIL
    # 说明里必须出现"可能仍有残留进程"：重启窗口内进程树是否真的死了，台账证明不了（§17.5）
    assert "可能仍有残留进程" in row.detail
    # 它是**终态**：会话不会因为一条查不清的旧任务被永久卡住
    assert reopened.service._jobs.begin(session_id=sid).state == JOB_STATE_QUEUED


def test_restart_proves_a_terminal_turn_and_settles_the_task(
    open_api: OpenApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """崩溃窗口：图跑完了、Checkpoint 已终态、台账记了轮次，只有索引没结算（§17.5 的"可证明"）。"""
    client, runtime = open_api(decision="chat", reply="假回答")
    sid = _create_session(client)
    jobs = runtime.service._jobs
    assert jobs is not None
    # 模拟"进程在结算任务之前挂掉"：让 settle 什么都不做（图、Checkpoint、台账全是真的）
    monkeypatch.setattr(JobRuntime, "_settle", lambda self, job_id, result: None)

    submitted = _submit(client, sid, "你好", "restart-proof")
    assert submitted.status_code == 200 and submitted.json()["status"] == "completed"
    before_messages = len(client.get(f"/api/sessions/{sid}").json()["messages"])
    stale = jobs.list_for_session(sid)[-1]
    assert stale.state == JOB_STATE_RUNNING and stale.run_id is None

    monkeypatch.undo()
    runtime.close()

    client2, reopened = open_api(decision="chat", reply="假回答")

    row = reopened.service._jobs.list_for_session(sid)[-1]
    assert row.job_id == stale.job_id
    assert row.state == JOB_STATE_COMPLETED
    assert row.detail == PROVABLE_DETAIL
    # **没有**重新跑一轮：消息条数不变（补的是索引，不是执行）
    assert len(client2.get(f"/api/sessions/{sid}").json()["messages"]) == before_messages


def test_replay_proves_the_outcome_and_settles_the_task_without_a_restart(
    open_api: OpenApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """崩溃后**同一 request id 重试**也要收口：不必等重启，会话不被一条旧任务卡住（§17.5）。"""
    client, runtime = open_api(decision="chat", reply="假回答")
    sid = _create_session(client)
    jobs = runtime.service._jobs
    assert jobs is not None
    monkeypatch.setattr(JobRuntime, "_settle", lambda self, job_id, result: None)
    assert _submit(client, sid, "你好", "replay-proof").status_code == 200
    stale = jobs.list_for_session(sid)[-1]
    assert stale.state == JOB_STATE_RUNNING
    monkeypatch.undo()

    # 台账仍为"执行中"，只读核对能证明终态 → 补齐结果，并**顺手把任务索引收口**（同一个纯函数）
    replayed = _submit(client, sid, "你好", "replay-proof")

    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replayed"] is True
    row = jobs.list_for_session(sid)[-1]
    assert row.job_id == stale.job_id
    assert row.state == JOB_STATE_COMPLETED
    assert row.detail == PROVABLE_DETAIL
    # 会话不再被那条旧任务卡住：`begin` 只在"有非终态任务"时拒绝（§17.2），所以这里为空
    # 就等于"下一次提交开得出新任务"。不再真跑一轮新对话：那要另配一次模型鉴权，与本片无关。
    assert jobs.active_for_session(sid) is None
    assert len(jobs.list_for_session(sid)) == 1


# ---- 任务中心（T62，架构 §17.6）---------------------------------------------
#
# 只读端点：字段全部由**任务索引 + 会话索引**推出，不读任何会话的 Checkpoint。索引里的行用
# `_plant` 摆成各种状态（等价于"这些任务真的跑过"），因此这里测的是投影与聚合，不依赖模型。


def _tasks(client: TestClient, **params: object) -> dict:
    response = client.get("/api/tasks", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_task_center_lists_states_counts_and_order(open_api: OpenApi) -> None:
    client, runtime = open_api(decision="chat", reply="假回答")
    sid = _create_session(client, title="跑很久的会话")
    completed = _plant(runtime, sid, JOB_STATE_COMPLETED)
    waiting = _plant(runtime, sid, JOB_STATE_WAITING_USER)
    running = _plant(runtime, sid, JOB_STATE_RUNNING)

    body = _tasks(client)

    # 最新登记的在最前面（契约：按登记时间倒序）
    assert [item["task_id"] for item in body["tasks"]] == [
        running.job_id,
        waiting.job_id,
        completed.job_id,
    ]
    # 会话标题进页面（任务中心靠它认出"哪个会话在跑"）
    assert {item["session_title"] for item in body["tasks"]} == {"跑很久的会话"}
    # 计数覆盖全部任务，且每个状态都有键（没有的是 0，不是缺键）
    assert set(body["counts"]) == set(JOB_STATES)
    assert body["counts"][JOB_STATE_RUNNING] == 1
    assert body["counts"][JOB_STATE_WAITING_USER] == 1
    assert body["counts"][JOB_STATE_COMPLETED] == 1
    assert body["counts"][JOB_STATE_NEEDS_REVIEW] == 0
    # 入口角标 = 等你处理（在等审批 + 需要人工核对）
    assert body["pending_count"] == 1


def test_task_center_has_no_internal_identifiers(open_api: OpenApi) -> None:
    """字段门槛（§15.8）：行里不出现 run / thread / request 标识，也没有指纹或工具名。"""
    client, _runtime = open_api(decision="chat", reply="假回答")
    sid = _create_session(client)
    assert _submit(client, sid, "你好", "task-center-1").status_code == 200

    body = _tasks(client)
    rendered = json.dumps(body, ensure_ascii=False)

    assert body["tasks"], "提交过后应当能看到这一轮的任务"
    assert "run_id" not in rendered
    assert "thread_id" not in rendered
    assert "request_id" not in rendered
    assert "fingerprint" not in rendered
    # 只给页面需要的四组字段：引用、会话、状态、时间
    assert set(body["tasks"][0]) == {
        "task_id",
        "session_id",
        "session_title",
        "workspace",
        "state",
        "detail",
        "created_at",
        "started_at",
        "finished_at",
    }


def test_task_center_counts_are_exact_beyond_the_limit(open_api: OpenApi) -> None:
    client, runtime = open_api(decision="chat", reply="假回答")
    sid = _create_session(client)
    for _ in range(3):
        _plant(runtime, sid, JOB_STATE_COMPLETED)

    body = _tasks(client, limit=1)

    assert len(body["tasks"]) == 1
    assert body["counts"][JOB_STATE_COMPLETED] == 3  # 计数不受 limit 截断


def test_task_center_filters_finished_and_hides_archived_sessions(open_api: OpenApi) -> None:
    client, runtime = open_api(decision="chat", reply="假回答")
    sid = _create_session(client)
    _plant(runtime, sid, JOB_STATE_COMPLETED)
    running = _plant(runtime, sid, JOB_STATE_RUNNING)

    unfinished = _tasks(client, include_finished=False)
    assert [item["task_id"] for item in unfinished["tasks"]] == [running.job_id]
    # 计数仍然覆盖全部（筛的是列表，不是聚合）
    assert unfinished["counts"][JOB_STATE_COMPLETED] == 1

    assert client.post(f"/api/sessions/{sid}/archive").status_code == 200
    assert _tasks(client)["tasks"] == []  # 归档 = 离开视线（§17.6 明写这条口径）


def test_task_center_never_writes(open_api: OpenApi) -> None:
    """只读：连读两次不改索引里的任何一行（`updated_at` 逐项不变）。"""
    client, runtime = open_api(decision="chat", reply="假回答")
    sid = _create_session(client)
    _plant(runtime, sid, JOB_STATE_WAITING_USER)
    store = JobStore(runtime.database.connection, lock=runtime.database.checkpointer.lock)
    before = [(job.job_id, job.state, job.updated_at) for job in store.list_for_session(sid)]

    _tasks(client)
    _tasks(client, include_finished=False)

    after = [(job.job_id, job.state, job.updated_at) for job in store.list_for_session(sid)]
    assert after == before


def test_task_center_is_empty_not_an_error_on_a_fresh_workspace(open_api: OpenApi) -> None:
    client, _runtime = open_api(decision="chat", reply="假回答")
    _create_session(client)

    body = _tasks(client)

    assert body["tasks"] == []
    assert set(body["counts"]) == set(JOB_STATES)
    assert all(count == 0 for count in body["counts"].values())
    assert body["pending_count"] == 0


def test_restart_keeps_a_waiting_task_resumable(open_api: OpenApi) -> None:
    """停在审批上的任务：重启后索引与 Checkpoint 都不动，用户还能接着办（§17.5 的"可恢复"）。"""
    call = {"name": "delete_file", "args": {"path": "note.txt"}, "id": "restart-call"}
    client, runtime = open_api(decision="tool", reply="好的", tool_calls=[call])
    sid = _create_session(client)
    assert _submit(client, sid, "删除 note.txt", "wait-1").json()["status"] == "awaiting_approval"
    waiting = runtime.service._jobs.active_for_session(sid)
    assert waiting is not None and waiting.state == JOB_STATE_WAITING_USER
    before = (waiting.updated_at, waiting.detail)
    runtime.close()

    client2, reopened = open_api(decision="tool", reply="好的", tool_calls=[call])

    row = reopened.service._jobs.list_for_session(sid)[-1]
    # 一个字节都没改：不写库就不发事件（§17.3 的同状态不算迁移）
    assert (row.state, row.updated_at, row.detail) == (JOB_STATE_WAITING_USER, *before)
    # 而且真的还能继续：重启后的审批照常走通，任务随之收敛到终态
    approval = client2.get(f"/api/sessions/{sid}").json()["approval"]
    decided = client2.post(
        f"/api/sessions/{sid}/approvals/{approval['approval_id']}",
        json={"decision": "reject", "client_request_id": "wait-2", "reason": "先别删"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "completed"
    assert reopened.service._jobs.list_for_session(sid)[-1].state == JOB_STATE_COMPLETED


@pytest.mark.parametrize("restart", ["none", "before_resume", "after_registration"])
def test_cancelled_approval_stays_cancelled_after_rejected_submit_and_restart(
    open_api: OpenApi, tmp_path: Path, restart: str
) -> None:
    call = {"name": "delete_file", "args": {"path": "note.txt"}, "id": "cancelled-delete"}
    client, runtime = open_api(decision="tool", reply="好的", tool_calls=[call])
    target = tmp_path / WORKSPACE_NAME / "note.txt"
    target.write_text("保留这个测试文件", encoding="utf-8")
    sid = _create_session(client)
    assert _submit(client, sid, "删除 note.txt", "cancel-wait").json()["status"] == "awaiting_approval"
    approval = client.get(f"/api/sessions/{sid}").json()["approval"]
    assert client.post(f"/api/sessions/{sid}/cancel", json={"reason": "不再删除"}).json()["status"] == "cancelled"
    if restart == "after_registration":
        # 模拟恢复请求登记了补建任务、尚未驱动图时进程退出。
        runtime.service._jobs.resume(session_id=sid)
    if restart != "none":
        runtime.close()
        client, runtime = open_api(decision="tool", reply="好的", tool_calls=[call])
    rejected = _submit(client, sid, "新的请求", "cancel-rejected")
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == SESSION_BUSY
    assert "没有取消接口" not in rejected.text and "不公开取消" not in rejected.text
    decided = client.post(f"/api/sessions/{sid}/approvals/{approval['approval_id']}",
                          json={"decision": "approve", "client_request_id": "cancel-decide"})
    assert decided.status_code == 200, decided.text
    assert target.read_text(encoding="utf-8") == "保留这个测试文件"
    assert runtime.service._token_for(sid).cancelled
    assert runtime.service._jobs.list_for_session(sid)[0].state == JOB_STATE_CANCELLED
    # 新的有效提交可以换令牌；旧取消不会粘住后续任务。
    healthy = _submit(client, sid, "新的一轮", "cancel-new-turn")
    assert healthy.status_code == 200, healthy.text
    assert not runtime.service._token_for(sid).cancelled


def test_queued_cancel_releases_session_for_explicit_recovery(open_api: OpenApi) -> None:
    client, runtime = open_api(decision="chat", reply="新任务完成")
    store = _enable_background(runtime)
    jobs = runtime.service._jobs
    assert jobs is not None
    blocker_sid = _create_session(client)
    sid = _create_session(client)
    started, release = threading.Event(), threading.Event()

    def blocker():  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(10)
        return SimpleNamespace(status="completed", run_id="blocker-run")

    try:
        jobs.dispatch(jobs.begin(session_id=blocker_sid), blocker)
        assert started.wait(5)
        accepted = _submit(client, sid, "排队消息", "queued-cancel")
        assert accepted.status_code == 200, accepted.text
        job_id = accepted.json()["job_id"]
        assert store.get(job_id).state == JOB_STATE_QUEUED
        assert client.post(f"/api/sessions/{sid}/cancel", json={}).status_code == 200
        release.set()
        assert jobs.wait_for_idle(10)
        assert not runtime.service._lock_for(sid).locked()
        assert _stages(runtime, sid) == []  # 被取消的队列项没有启动图。
        snapshot = client.get(f"/api/sessions/{sid}").json()
        recovered = client.post(f"/api/sessions/{sid}/recovery", json={
            "request_id": "queued-cancel", "expected_snapshot_id": snapshot["snapshot_id"],
            "confirm_reviewed": True,
        })
        assert recovered.status_code == 200, recovered.text
        next_request = _submit(client, sid, "新消息", "queued-next")
        assert next_request.status_code == 200, next_request.text
        assert jobs.wait_for_idle(10)
        assert store.get(next_request.json()["job_id"]).state == JOB_STATE_COMPLETED
    finally:
        release.set()


@pytest.mark.parametrize("restart", ["before_resume", "after_registration"])
@pytest.mark.parametrize("legacy_null_binding", [False, True])
def test_cancel_before_job_settlement_persists_the_run_for_approval_restart(
    open_api: OpenApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restart: str,
    legacy_null_binding: bool,
) -> None:
    call = {"name": "delete_file", "args": {"path": "note.txt"}, "id": "before-settle-delete"}
    client, runtime = open_api(decision="tool", reply="好的", tool_calls=[call])
    store = _enable_background(runtime)
    jobs = runtime.service._jobs
    entered = threading.Event()
    release = threading.Event()
    original = jobs._settle

    def settle(job_id, result):
        entered.set()
        assert release.wait(15)
        original(job_id, result)

    monkeypatch.setattr(jobs, "_settle", settle)
    target = tmp_path / WORKSPACE_NAME / "note.txt"
    target.write_text("必须保留", encoding="utf-8")
    sid = _create_session(client)
    submitted = _submit(client, sid, "删除 note.txt", "before-settle-submit")
    try:
        assert submitted.status_code == 200, submitted.text
        assert entered.wait(15)
        approval = client.get(f"/api/sessions/{sid}").json()["approval"]
        assert store.get(submitted.json()["job_id"]).run_id is None
        cancelled = client.post(f"/api/sessions/{sid}/cancel", json={"reason": "保留文件"})
        assert cancelled.status_code == 200, cancelled.text
        saved = store.get(submitted.json()["job_id"])
        session = runtime.database.sessions.get(sid)
        assert saved.state == JOB_STATE_CANCELLED
        assert saved.run_id == runtime.service._current_run_id(session)
        assert saved.run_id is not None
    finally:
        release.set()
    assert jobs.wait_for_idle(15)
    if legacy_null_binding:
        # 升级前已保存的缺失绑定记录：不通过回写终态修复，更不能默认准许执行。
        runtime.database.connection.execute(
            "UPDATE jobs SET run_id=NULL WHERE job_id=?", (submitted.json()["job_id"],),
        )
        runtime.database.connection.commit()
    if restart == "after_registration":
        jobs.resume(session_id=sid)
    runtime.close()

    client, reopened = open_api(decision="tool", reply="好的", tool_calls=[call])
    decided = client.post(
        f"/api/sessions/{sid}/approvals/{approval['approval_id']}",
        json={"decision": "approve", "client_request_id": "before-settle-approve"},
    )
    assert decided.status_code == 200, decided.text
    assert target.read_text(encoding="utf-8") == "必须保留"
    assert reopened.service._token_for(sid).cancelled
    # 即使旧空绑定记录保留，已验收的新一轮可以独立处理自己的审批。
    healthy = _submit(client, sid, "新的一轮", "after-unbound-cancel")
    assert healthy.status_code == 200, healthy.text
    assert not reopened.service._token_for(sid).cancelled


def test_stale_timeout_scan_never_cancels_the_next_submission(
    open_api: OpenApi, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime = open_api(decision="chat", reply="新任务完成")
    store = _enable_background(runtime)
    jobs = runtime.service._jobs
    sid = _create_session(client)
    old = store.create(session_id=sid, state=JOB_STATE_RUNNING)
    runtime.database.connection.execute(
        "UPDATE jobs SET created_at=? WHERE job_id=?",
        ("2020-01-01T00:00:00+00:00", old.job_id),
    )
    runtime.database.connection.commit()
    runtime.service._max_task_seconds = 5.0
    captured = threading.Event()
    release = threading.Event()
    original = jobs.jobs_in_flight
    expired: list[str] = []
    errors: list[Exception] = []

    def scan():
        snapshot = original()
        captured.set()
        assert release.wait(15)
        return snapshot

    def expire():
        try:
            expired.extend(runtime.service.expire_overdue_tasks())
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(jobs, "jobs_in_flight", scan)
    thread = threading.Thread(target=expire)
    thread.start()
    try:
        assert captured.wait(15)
        jobs.recover(old.job_id, target=JOB_STATE_COMPLETED)
        submitted = _submit(client, sid, "新的一轮", "next-after-stale-scan")
        assert submitted.status_code == 200, submitted.text
        token = runtime.service._token_for(sid)
        assert not token.cancelled
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and not errors
    assert expired == []
    assert not token.cancelled
    assert jobs.wait_for_idle(15)
    assert store.get(submitted.json()["job_id"]).state == JOB_STATE_COMPLETED


# ---- v0.8 T75：直接伪造 HTTP 请求的负例（计划 §T75 验收①、§4 硬验收）--------
#
# 这一节只收**之前没有负例**的那几条改状态路由。整份文件本来就都是"绕过页面直打 API"
# （TestClient，不经浏览器、不带页面状态），所以这里的用例同时是"后端不依赖页面的自律"的证明。


def test_new_session_rejects_an_unlisted_workspace_id(api: tuple[TestClient, ApiRuntime]) -> None:
    """`POST /sessions` 的 `workspace_id` 只认**已列出**的工作区。

    没列出就 404，**不**按路径现建：否则"新建会话"会变成一条隐式的、绕过工作区登记的入口
    （§15.8 的工作区边界是登记过的那些根，不是"谁能给我一个路径"）。
    """
    client, _ = api
    _create_session(client)  # 建一条会话，工作区因此被登记进列表

    forged = client.post(
        "/api/sessions", json={"title": "伪造工作区", "workspace_id": "not-a-listed-workspace"}
    )
    assert forged.status_code == 404, forged.text
    assert forged.json()["error"]["code"] == WORKSPACE_NOT_FOUND

    # 同一条路由带**已列出**的 id 仍然可用：拒绝的是那个 id，不是整条路由。
    listed = client.get("/api/workspaces").json()[0]["workspace_id"]
    ok = client.post("/api/sessions", json={"title": "正常", "workspace_id": listed})
    assert ok.status_code == 200, ok.text


def test_branch_routes_reject_an_unknown_branch_and_a_bad_arrangement(
    api: tuple[TestClient, ApiRuntime]
) -> None:
    """分支两条路由的负例：未知分支不能选中/改名，分支名与位置只由**服务端**裁定。

    伪造请求能过的只有 HTTP 形状；空白名（长度合规但内容是空的）照样要被拒——
    否则"名字"就成了可以塞进任何东西的展示字段。
    """
    client, _ = api
    session_id = _create_session(client)
    branch_id = client.get(f"/api/sessions/{session_id}/conversation-map").json()["active_branch_id"]

    unknown = client.post(f"/api/sessions/{session_id}/branches/ghost/select")
    assert unknown.status_code == 409, unknown.text

    renamed = client.patch(
        f"/api/sessions/{session_id}/branches/ghost", json={"title": "改名", "position": 0}
    )
    assert renamed.status_code == 409, renamed.text

    blank = client.patch(
        f"/api/sessions/{session_id}/branches/{branch_id}", json={"title": "   ", "position": 0}
    )
    assert blank.status_code == 409, blank.text

    # 负数位置在**形状**这一层就被拒（422），不会流到服务里被当成"最后一个"。
    negative = client.patch(
        f"/api/sessions/{session_id}/branches/{branch_id}", json={"title": "改名", "position": -1}
    )
    assert negative.status_code == 422, negative.text


def test_restore_rejects_an_unknown_session(api: tuple[TestClient, ApiRuntime]) -> None:
    """`POST /sessions/{id}/restore` 对不存在的会话给 404，而不是"恢复成功"的空操作。

    已归档的会话照旧能恢复——拒绝的是"找不到会话"，不是"没归档"。
    """
    client, _ = api

    missing = client.post("/api/sessions/ghost-session/restore")
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == SESSION_NOT_FOUND

    session_id = _create_session(client)
    assert client.post(f"/api/sessions/{session_id}/archive").status_code == 200
    restored = client.post(f"/api/sessions/{session_id}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["session_id"] == session_id

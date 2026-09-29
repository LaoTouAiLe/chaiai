"""T29：HTTP 接口层（契约 §15.1、§15.3，计划 §6.2）。

用真实应用服务 + 确定性假模型，不联网；每个用例在 `tmp_path` 下建自己的库。

覆盖：成功/错误响应、幂等与单飞、审批、工作区路径校验、Host/Origin/正文大小边界、
静态托管（缓存头、SPA 回退、路径穿越）、单实例锁、入口的单 worker、依赖方向与 OpenAPI 面。
"""

from __future__ import annotations

import asyncio
import json
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
from local_agent.api import __main__ as api_main
from local_agent.api.app import create_app
from local_agent.api.container import ApiRuntime, instance_lock_path, open_runtime
from local_agent.api.openapi import OPENAPI_PATH, openapi_json
from local_agent.api.routes import status_for_code
from local_agent.api.security import LocalBoundaryMiddleware, _authority
from local_agent.api.sse import event_stream
from local_agent.application.errors import (
    APPROVAL_EXPIRED,
    IDEMPOTENCY_CONFLICT,
    RECOVERY_REQUIRED,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    WORKSPACE_NOT_FOUND,
)
from local_agent.application.models import SCHEMA_VERSION
from local_agent.application.service import EventSubscription
from local_agent.config import AppConfig

ModelFactory = Callable[..., Any]
OpenApi = Callable[..., tuple[TestClient, ApiRuntime]]

WORKSPACE_NAME = "ws"
#: 允许的 Host（与 `WebConfig.allowed_hosts` 的默认值一致）：测试默认从这里发起请求，
#: 这样**默认配置本身**就是被测对象（而不是测试专用的放宽配置）。
LOCAL_BASE_URL = "http://127.0.0.1"
VITE_ORIGIN = "http://127.0.0.1:5173"


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


def _submit(client: TestClient, session_id: str, text: str, request_id: str):
    return client.post(
        f"/api/sessions/{session_id}/messages",
        json={"text": text, "client_request_id": request_id},
    )


# ---- 健康检查与会话 ---------------------------------------------------------


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
    assert first["target"] == folder.as_posix()
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


def test_openapi_surface_is_controlled(api: tuple[TestClient, ApiRuntime]) -> None:
    client, _runtime = api
    response = client.get("/api/openapi.json")
    assert response.status_code == 200
    schema = response.json()

    assert set(schema["paths"]) == {
        "/api/health",
        "/api/workspaces",
        "/api/workspaces/select",
        "/api/sessions",
        "/api/sessions/{session_id}",
        "/api/sessions/{session_id}/events",
        "/api/sessions/{session_id}/messages",
        "/api/sessions/{session_id}/approvals/{approval_id}",
        "/api/sessions/{session_id}/model",
        "/api/model-connections",
        "/api/model-connections/{connection_id}",
        "/api/model-connections/{connection_id}/test",
        "/api/model-connections/default",
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


# ---- 入口 -------------------------------------------------------------------


def test_entry_point_starts_in_setup_mode_without_a_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺 `config.yaml` **不再是**启动失败（§15.9 初始设置模式）：服务起来、设置页可用。"""
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)

    code = api_main.main(["--config", str(tmp_path / "missing.yaml"), "--env", str(tmp_path / ".env")])

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

    assert api_main.main(["--config", str(config_path), "--env", str(tmp_path / ".env")]) == 0

    assert captured["workers"] == 1
    assert captured["host"] == "127.0.0.1"  # 默认只绑回环
    assert captured["timeout_keep_alive"] == config_module.WebConfig().keepalive_seconds


def test_non_loopback_host_needs_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="allow_non_loopback"):
        config_module.WebConfig(host="0.0.0.0")
    assert config_module.WebConfig(host="0.0.0.0", allow_non_loopback=True).host == "0.0.0.0"
    assert config_module.is_loopback_host("localhost") is True
    assert config_module.is_loopback_host("example.com") is False

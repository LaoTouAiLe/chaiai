"""HTTP/WS boundary and lifecycle for user terminals.

2026-10-28 起**没有启动审批**：`POST /api/terminals/requests` 创建并立即拉起进程，
所以这里的用例从"申请 → 批准 → running"改成"申请即 running"。旧的 `decision` 接口保留
（旧客户端/审计兼容）：对已启动的终端再批一次是幂等的，想拒绝一个运行中的终端会被拒。
"""

import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_api import _config

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.application import terminals
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import SandboxRequest

TERMINAL_HEADERS = {"origin": "http://127.0.0.1", "host": "127.0.0.1"}


class FakePty:
    def __init__(self):
        self.closed = False
        self.written = []
        self.output = b"hello\r\n"
        self.exit = None
        self.size = None

    def read(self):
        data, self.output = self.output, b""
        return data

    def write(self, data):
        self.written.append(data)

    def resize(self, cols, rows):
        self.size = (cols, rows)

    def poll(self):
        return self.exit

    def close(self):
        self.closed = True
        if self.exit is None:
            self.exit = 1


@pytest.fixture
def terminal_api(tmp_path, app_config, model_factory, monkeypatch):
    spawned = []
    scopes = []

    def spawn(*args, **kwargs):
        process = FakePty()
        spawned.append(process)
        return process

    def sandbox(policy, **kwargs):
        scopes.append(policy.external_policy)
        return SandboxRequest()

    monkeypatch.setattr(terminals, "supported", lambda: True)
    monkeypatch.setattr(terminals, "open_terminal_process", spawn)
    monkeypatch.setattr(SecurityPolicy, "shell_sandbox_request", sandbox)
    runtime = open_runtime(_config(tmp_path, app_config), base_dir=tmp_path, model=model_factory())
    client = TestClient(create_app(runtime), base_url="http://127.0.0.1")
    manager = runtime.service.user_terminals()
    assert manager._thread is None
    with client:
        client.post("/api/sessions", json={"title": "terminal test"})
        workspace = runtime.service._ref_for(runtime.service._workspace_root).workspace_id
        yield client, runtime, workspace, spawned, scopes
    assert all(process.closed for process in spawned)
    assert not manager._thread.is_alive()
    runtime.close()


def open_terminal(client, workspace):
    response = client.post("/api/terminals/requests", json={"workspace_id": workspace})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def decide(client, approval, decision="approve", **fields):
    return client.post(f"/api/terminals/requests/{approval}/decision",
                       json={"decision": decision, **fields})


def test_request_starts_immediately_and_audits_the_implicit_approval(terminal_api):
    """打开即启动：没有 pending 态，审计里仍留一条明确的 approve。"""
    client, runtime, workspace, spawned, _ = terminal_api
    live = open_terminal(client, workspace)

    assert live["status"] == "running" and live["terminal_id"] is not None
    assert len(spawned) == 1
    rows = runtime.database.connection.execute(
        "SELECT decision,status FROM terminal_audit ORDER BY rowid").fetchall()
    assert [tuple(row) for row in rows] == [("approve", "running")]

    # 旧审批接口保留且幂等：再批一次仍返回同一个运行中的终端，不会重复启动。
    again = decide(client, live["approval_id"])
    assert again.status_code == 200
    assert again.json()["terminal_id"] == live["terminal_id"]
    assert len(spawned) == 1


def test_rejecting_a_running_terminal_is_refused(terminal_api):
    client, _, workspace, spawned, _ = terminal_api
    live = open_terminal(client, workspace)

    assert decide(client, live["approval_id"], "reject").status_code == 409
    assert len(spawned) == 1


def test_permission_mode_follows_the_current_tier(terminal_api, monkeypatch):
    client, runtime, workspace, spawned, scopes = terminal_api
    monkeypatch.setattr(runtime.service, "external_paths_policy",
                        lambda: ExternalPathPolicy(mode="read-only", grants={"C:/external": "read_write"}))
    live = open_terminal(client, workspace)

    assert live["status"] == "running" and live["permission_mode"] == "read-only"
    assert scopes[0].grants == {} and scopes[0].mode == "read-only"
    runtime.service.close_tool_sessions()
    assert spawned[0].closed


def test_start_failure_is_reported_without_a_terminal_id(terminal_api, monkeypatch):
    """启动失败不抛 500：落成 failed + 原因，前端据此显示"启动失败 · 原因"。"""
    client, _, workspace, spawned, _ = terminal_api

    def fail(*args, **kwargs):
        raise RuntimeError("no sandbox")

    monkeypatch.setattr(terminals, "open_terminal_process", fail)
    failed = open_terminal(client, workspace)

    assert failed["status"] == "failed" and failed["terminal_id"] is None
    assert "受控终端启动失败" in (failed["detail"] or "")
    assert not spawned


@pytest.mark.parametrize("headers", [{}, {"origin": "https://evil.example"},
                                    {"origin": "null"}, {"origin": "http://127.0.0.1", "host": "evil.example"}])
def test_websocket_denies_bad_origin_or_host(terminal_api, headers):
    client, _, workspace, _spawned, _ = terminal_api
    live = open_terminal(client, workspace)

    with (pytest.raises(WebSocketDisconnect) as error,
          client.websocket_connect(f"/api/terminals/{live['terminal_id']}/stream", headers=headers)):
        pass
    assert error.value.code == 1008


def test_ws_single_controller_reconnect_input_and_stop(terminal_api):
    client, runtime, workspace, spawned, _ = terminal_api
    live = open_terminal(client, workspace)
    tid = live["terminal_id"]
    path = f"/api/terminals/{tid}/stream"

    with client.websocket_connect(path, headers=TERMINAL_HEADERS) as ws:
        assert ws.receive_json() == {"type": "reset"}
        assert ws.receive_bytes() == b"hello\r\n"
        # 同一终端只允许一个控制器。
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(path, headers=TERMINAL_HEADERS):
            pass
        ws.send_bytes(b"echo OK\r")
        ws.send_json({"type": "resize", "cols": 100, "rows": 30})
        deadline = time.monotonic() + 3
        while spawned[0].size is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert spawned[0].written == [b"echo OK\r"] and spawned[0].size == (100, 30)
    assert not spawned[0].closed

    with client.websocket_connect(path, headers=TERMINAL_HEADERS) as ws:
        assert ws.receive_json()["type"] == "reset"
        assert ws.receive_bytes() == b"hello\r\n"
        assert client.delete(f"/api/terminals/{tid}").json()["status"] == "stopped"
        assert ws.receive_json()["type"] == "exit"
    assert spawned[0].closed

    columns = runtime.database.connection.execute("PRAGMA table_info(terminal_audit)").fetchall()
    assert not {"input", "output", "environment"} & {row[1] for row in columns}


@pytest.mark.parametrize("payload", [b"x" * 16385, {"type": "resize", "cols": 0}, {"type": "input", "data": "bad"}],
                         ids=["oversize", "bad-size", "unknown-control"])
def test_ws_rejects_oversize_or_invalid_input(terminal_api, payload):
    client, _, workspace, spawned, _ = terminal_api
    live = open_terminal(client, workspace)

    with client.websocket_connect(f"/api/terminals/{live['terminal_id']}/stream",
                                  headers=TERMINAL_HEADERS) as ws:
        assert ws.receive_json()["type"] == "reset"
        if isinstance(payload, bytes):
            ws.send_bytes(payload)
        else:
            ws.send_json(payload)
        with pytest.raises(WebSocketDisconnect):
            while True:
                ws.receive_bytes()
    assert spawned[0].written == []


def test_capacity_bounded_replay_exit_and_idle(terminal_api, monkeypatch):
    client, runtime, workspace, spawned, _ = terminal_api
    live = [open_terminal(client, workspace) for _ in range(4)]
    # 第 5 个直接 429：上限保护的是资源，不是审批队列。
    assert client.post("/api/terminals/requests",
                       json={"workspace_id": workspace}).status_code == 429

    manager = runtime.service.user_terminals()
    monkeypatch.setattr(terminals, "OUTPUT_LIMIT", 10)
    spawned[0].output = b"1234567890abcdefghij"
    spawned[0].exit = 7
    with manager._lock:
        manager._get(live[1]["terminal_id"]).activity = 0
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        view, data, cursor, truncated = manager.snapshot(live[0]["terminal_id"], 0)
        if view.status == "exited" and spawned[1].closed:
            break
        time.sleep(.02)
    assert view.exit_code == 7 and data == b"abcdefghij" and truncated and cursor >= 20
    assert spawned[1].closed
    assert manager.snapshot(live[1]["terminal_id"], 0)[0].status == "stopped"


def test_invalid_workspace_unsupported_platform_and_dimension_validation(terminal_api, monkeypatch):
    client, _, workspace, spawned, _ = terminal_api
    assert client.post("/api/terminals/requests", json={"workspace_id": "C:/arbitrary"}).status_code == 404

    live = open_terminal(client, workspace)
    # body 校验仍在（cols 超出 20..400），与"要不要启动"无关。
    assert decide(client, live["approval_id"], cols=500).status_code == 422

    monkeypatch.setattr(terminals, "supported", lambda: False)
    assert client.post("/api/terminals/requests", json={"workspace_id": workspace}).status_code == 503
    assert len(spawned) == 1

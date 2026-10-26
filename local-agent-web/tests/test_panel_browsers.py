"""浏览器 API 的同源边界、幂等、输入白名单、重连与清理。"""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_api import _config

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.application import browsers
from local_agent.application.browser_models import BrowserView


class FakeBrowser:
    def __init__(self, policy):
        self.root = policy.workspace_root
        self.view = BrowserView(browser_id=uuid4().hex)
        self.lock = threading.RLock()
        self.commands = queue.Queue(maxsize=128)
        self.controller = None
        self.activity = time.monotonic()
        self.ending = threading.Event()
        self.frame_number = 1
        self.frame = {"type": "frame", "data": "jpeg", "width": 520, "height": 700}

    def close(self):
        self.ending.set()


@pytest.fixture
def browser_api(tmp_path, app_config, model_factory, monkeypatch):
    monkeypatch.setattr(browsers, "PanelBrowser", FakeBrowser)
    runtime = open_runtime(_config(tmp_path, app_config), base_dir=tmp_path, model=model_factory())
    app = create_app(runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, app.state.browsers, runtime
    assert not app.state.browsers.items
    assert not app.state.browsers.thread.is_alive()
    runtime.close()


def create(client, identity="panel-test"):
    response = client.post("/api/browsers", json={"request_id": identity})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["browser_id"]


def test_idempotence_close_before_start_limit_and_profile_scope(browser_api):
    client, manager, runtime = browser_api
    identity = create(client)
    assert create(client) == identity
    item = manager.get(identity)
    assert item.root != runtime.service._workspace_root
    assert item.root.name == "panel-browsers"
    for index in range(5):
        create(client, f"other-{index}")
    assert client.post("/api/browsers", json={"request_id": "overflow"}).status_code == 409
    assert client.delete("/api/browsers/requests/panel-test").status_code == 204
    assert item.ending.is_set()
    assert client.get(f"/api/browsers/{identity}").status_code == 404
    assert client.post("/api/browsers", json={"request_id": "panel-test"}).status_code == 410
    client.delete("/api/browsers/requests/not-started")
    assert client.post("/api/browsers", json={"request_id": "not-started"}).status_code == 410


def test_cross_origin_http_and_websocket_are_rejected(browser_api):
    client, manager, _ = browser_api
    assert client.post("/api/browsers", json={"request_id": "bad"},
                       headers={"origin": "https://example.org"}).status_code == 403
    assert not manager.items
    identity = create(client)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            f"/api/browsers/{identity}/stream", headers={"origin": "https://example.org", "host": "127.0.0.1"}):
        pass


def test_stream_reconnect_preserves_page_and_blocks_protocols(browser_api):
    client, manager, _ = browser_api
    identity = create(client)
    item = manager.get(identity)
    item.view.url = "https://example.org/"
    item.view.title = "Existing page"
    with client.websocket_connect(f"/api/browsers/{identity}/stream", headers={"origin": "http://127.0.0.1", "host": "127.0.0.1"}) as ws:
        assert ws.receive_json()["url"] == "https://example.org/"
        assert ws.receive_json()["type"] == "frame"
        ws.send_json({"action": "navigate", "url": "file:///C:/secret"})
        assert "http/https" in ws.receive_json()["error"]
        assert item.commands.empty()
        ws.send_json({"action": "navigate", "url": "http://localhost:5173"})
        assert item.commands.get(timeout=2).url == "http://localhost:5173"
        ws.send_json({"action": "text", "text": "中文输入"})
        assert item.commands.get(timeout=2).text == "中文输入"
    assert item.controller is None
    assert create(client) == identity
    with client.websocket_connect(f"/api/browsers/{identity}/stream", headers={"origin": "http://127.0.0.1", "host": "127.0.0.1"}) as ws:
        assert ws.receive_json()["title"] == "Existing page"


@pytest.mark.parametrize("body", [
    {"action": "Runtime.evaluate", "text": "alert(1)"},
    {"action": "resize", "width": 99999},
    {"action": "pointer", "x": -1},
    {"action": "key", "script": "evil"},
    {"action": "text", "text": "a" * 8193},
])
def test_invalid_websocket_commands_close_without_execution(browser_api, body):
    client, manager, _ = browser_api
    identity = create(client)
    with client.websocket_connect(f"/api/browsers/{identity}/stream", headers={"origin": "http://127.0.0.1", "host": "127.0.0.1"}) as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json(body)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert manager.get(identity).commands.empty()


def test_expired_or_failed_browser_can_be_created_again(browser_api):
    client, manager, _ = browser_api
    first = create(client)
    manager.get(first).ending.set()
    second = create(client)
    assert second != first
    manager.stop("panel-test", cancelled=False)
    assert create(client) != second


def test_reconnected_browser_is_not_reaped_and_expired_handle_cannot_reconnect(browser_api):
    client, manager, _ = browser_api
    identity = create(client)
    item = manager.get(identity)
    item.activity = time.monotonic() - browsers.IDLE_SECONDS - 1
    connected, controller = manager.connect(identity)
    assert connected is item and controller == item.controller
    manager.reap_idle()
    assert manager.get(identity) is item
    assert not item.ending.is_set()

    with item.lock:
        item.controller = None
        item.activity = time.monotonic() - browsers.IDLE_SECONDS - 1
    manager.reap_idle()
    assert item.ending.is_set()
    with pytest.raises(browsers.BrowserError, match="会话已结束"):
        manager.connect(identity)
    assert create(client) != identity


def test_connect_racing_with_reaper_cannot_attach_to_removed_browser(browser_api):
    client, manager, _ = browser_api
    identity = create(client)
    item = manager.get(identity)
    item.activity = time.monotonic() - browsers.IDLE_SECONDS - 1
    reaper_locked = threading.Event()
    connecting = threading.Event()
    outcomes = []

    def reap():
        with manager.lock:
            reaper_locked.set()
            manager.reap_idle()

    def connect():
        connecting.set()
        try:
            manager.connect(identity)
            outcomes.append("connected")
        except browsers.BrowserError:
            outcomes.append("expired")

    with item.lock:
        reaper = threading.Thread(target=reap)
        reaper.start()
        assert reaper_locked.wait(2)
        connection = threading.Thread(target=connect)
        connection.start()
        assert connecting.wait(2)
    reaper.join(timeout=3)
    connection.join(timeout=3)
    assert not reaper.is_alive() and not connection.is_alive()
    assert outcomes == ["expired"]
    assert item.controller is None
    assert item.ending.is_set()


def test_ended_browser_does_not_accept_a_websocket_controller(browser_api):
    client, manager, _ = browser_api
    identity = create(client)
    item = manager.get(identity)
    item.ending.set()
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
        f"/api/browsers/{identity}/stream", headers={"origin": "http://127.0.0.1", "host": "127.0.0.1"},
    ):
        pass
    assert item.controller is None


def test_waiting_for_browser_manager_lock_does_not_block_other_api_requests(browser_api, monkeypatch):
    client, manager, _ = browser_api
    identity = create(client)
    locked, release, lock_released, connecting = (threading.Event() for _ in range(4))
    original = manager.connect

    @client.app.get('/__test/ping')
    async def ping():
        return {"ok": True}

    client.app.router.routes.insert(0, client.app.router.routes.pop())

    def traced_connect(*args, **kwargs):
        connecting.set()
        return original(*args, **kwargs)

    def hold_startup_lock():
        with manager.lock:
            locked.set()
            release.wait(3)
        lock_released.set()

    def connect():
        with client.websocket_connect(f"/api/browsers/{identity}/stream", headers={
            "origin": "http://127.0.0.1", "host": "127.0.0.1",
        }) as ws:
            assert ws.receive_json()["browser_id"] == identity

    monkeypatch.setattr(manager, "connect", traced_connect)
    holder = threading.Thread(target=hold_startup_lock)
    holder.start()
    assert locked.wait(2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        connection = executor.submit(connect)
        try:
            assert connecting.wait(2)
            response = client.get('/__test/ping')
            assert response.json() == {"ok": True}
            assert not lock_released.is_set(), "API 等待了浏览器管理锁，异步主线程被阻塞"
        finally:
            release.set()
            holder.join(timeout=3)
        connection.result(timeout=5)

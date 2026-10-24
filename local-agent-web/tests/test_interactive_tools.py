"""交互入口的权限、隔离、增量输出与真实 HTTP 下载。浏览器原生验收另存 eval。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from local_agent.cancellation import CancellationToken
from local_agent.exceptions import SecurityError
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.browser_tools import BrowserSession, checked_url
from local_agent.tools.host_tools import build_host_tools, download_to_file, system_processes
from local_agent.tools.runtime_sessions import RESOURCES, scope_key
from local_agent.tools.terminal_tools import build_terminal_tools
from local_agent.tools.tool_registry import ToolRegistry


@pytest.fixture
def policy(tmp_path, app_config, monkeypatch):
    value = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    # 非原生回归只验证会话逻辑；真实 Windows 写边界在独立原生验收中验证。
    monkeypatch.setattr(value, "shell_sandbox_request", lambda **_: None)
    yield value
    RESOURCES.close_scope(scope_key(value))


def invoke(tools, name, **kwargs):
    return json.loads(next(item for item in tools if item.name == name).invoke(kwargs))


def test_terminal_input_eof_exit_and_incremental_output(policy):
    tools = build_terminal_tools(policy)
    state = invoke(tools, "start_shell", command='python -u -c "import sys; print(input(), flush=True); print(sys.stdin.read(), flush=True)"')
    identity = state["session_id"]
    assert state["running"] and state["mode"] == "stdio"
    invoke(tools, "write_shell", session_id=identity, text="hello\n")
    first = invoke(tools, "read_shell", session_id=identity, wait_seconds=5)
    assert "hello" in first["stdout"]
    invoke(tools, "write_shell", session_id=identity, text="tail", eof=True)
    output = ""
    for _ in range(20):
        final = invoke(tools, "read_shell", session_id=identity, wait_seconds=.5)
        output += final["stdout"]
        if not final["running"] and "tail" in output:
            break
    assert "tail" in output and "hello" not in output
    assert final["exit_code"] == 0
    assert invoke(tools, "list_shell_sessions")[0]["session_id"] == identity
    assert not invoke(tools, "stop_shell", session_id=identity)["running"]


def test_sessions_do_not_cross_conversations_or_permission_changes(policy, tmp_path, app_config):
    tools = build_terminal_tools(policy)
    state = invoke(tools, "start_shell", command='python -u -c "import time; time.sleep(90)"')
    identity = state["session_id"]
    other = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    with pytest.raises(ValueError, match="另一段对话"):
        invoke(build_terminal_tools(other), "read_shell", session_id=identity)
    policy.external_policy = ExternalPathPolicy(mode="danger-full-access")
    with pytest.raises(SecurityError, match="权限已变化"):
        invoke(tools, "read_shell", session_id=identity)
    assert RESOURCES.items[(scope_key(policy), identity)].process.handle.poll() is not None


def test_cancel_stops_sessions_even_after_start_returns(policy):
    token = CancellationToken()
    policy.cancellation = token
    tools = build_terminal_tools(policy)
    state = invoke(tools, "start_shell", command='python -u -c "import time; time.sleep(90)"')
    item = RESOURCES.get(policy, state["session_id"])
    token.cancel("test")
    assert item.process.handle.poll() is not None
    assert not any(key[0] == scope_key(policy) for key in RESOURCES.items)


def test_reused_session_binds_the_new_turn_cancellation(policy, tmp_path, app_config):
    state = invoke(build_terminal_tools(policy), "start_shell", command='python -u -c "import time; time.sleep(90)"')
    item = RESOURCES.get(policy, state["session_id"])
    following = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    following.session_scope = policy.session_scope
    following.cancellation = CancellationToken()
    invoke(build_terminal_tools(following), "read_shell", session_id=state["session_id"])
    following.cancellation.cancel("cancel next turn")
    assert item.process.handle.poll() is not None


def test_disabling_commands_also_hides_interactive_command_tools():
    from local_agent.capabilities import capability_status, enabled_capabilities, resolve_switches

    switches = resolve_switches({"shell": False, "terminal": True})
    enabled = enabled_capabilities(switches, git_available=True, vision_available=False)
    assert "terminal" not in enabled
    row = next(row for row in capability_status(switches, git_available=True) if row.capability == "terminal")
    assert not row.available and row.reason == "disabled"


def test_disabling_browser_closes_only_its_own_resources(policy):
    from types import SimpleNamespace

    closed = []
    original_scope = policy.session_scope
    policy.session_scope = "app:conversation"
    terminal = SimpleNamespace(capability="terminal", close=lambda: closed.append("terminal"))
    browser = SimpleNamespace(capability="page_control", close=lambda: closed.append("browser"))
    RESOURCES.add(policy, "terminal", terminal)
    RESOURCES.add(policy, "browser", browser)
    RESOURCES.close_capabilities("app", {"page_control"})
    assert closed == ["browser"]
    RESOURCES.close_scope(scope_key(policy))
    policy.session_scope = original_scope


def test_terminal_logs_can_be_read_after_truncation(policy):
    tools = build_terminal_tools(policy)
    state = invoke(tools, "start_shell", command='python -u -c "print(123456789)"')
    identity = state["session_id"]
    result = ""
    for _ in range(30):
        part = invoke(tools, "read_shell", session_id=identity, wait_seconds=.2, max_chars=2)
        result += part["stdout"]
        if "123456789" in result:
            break
    assert result.startswith("123456789")


@pytest.mark.parametrize("name,args", [
    ("start_shell", {"command": "python --version"}),
    ("write_shell", {"session_id": "a", "text": "x"}),
    ("browser", {"action": "open"}),
    ("download_file", {"url": "https://example.com", "path": "x"}),
])
def test_readonly_mode_rejects_new_write_capabilities(policy, name, args):
    policy.external_policy = ExternalPathPolicy(mode="read-only")
    decision = policy.classify_tool(name, args)
    assert not decision.allowed and not decision.requires_approval


def test_browser_and_terminal_excluded_from_readonly_children(policy):
    from local_agent.capabilities import CAPABILITY_TOOLS, readonly_child_capabilities

    child = readonly_child_capabilities(tuple(CAPABILITY_TOOLS))
    assert "page_control" not in child and "terminal" not in child
    policy.allow_result_spill = False
    assert not policy.classify_tool("start_shell", {"command": "python --version"}).allowed


@pytest.fixture
def http_download():
    body = b"verified dependency\0" * 1000

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/missing":
                self.send_error(404)
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/data")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", body
    server.shutdown()
    server.server_close()
    thread.join()


def test_download_http_hash_atomicity_and_redirect(policy, http_download):
    url, body = http_download
    result = download_to_file(policy, url + "/redirect", "deps/file.bin")
    assert result["bytes"] == len(body) and result["http_status"] == 200
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    target = Path(result["path"])
    assert target.read_bytes() == body
    with pytest.raises(FileExistsError):
        download_to_file(policy, url, "deps/file.bin")
    with pytest.raises(Exception, match="404"):
        download_to_file(policy, url + "/missing", "deps/file.bin", overwrite=True)
    assert target.read_bytes() == body
    assert not list(target.parent.glob("*.part"))


def test_external_download_denied_before_network(policy, tmp_path, monkeypatch):
    policy.external_policy = ExternalPathPolicy(mode="danger-full-access")
    monkeypatch.setattr("urllib.request.build_opener", lambda *_: pytest.fail("must not access network"))
    with pytest.raises(SecurityError):
        download_to_file(policy, "https://example.com", str(tmp_path.parent / "external.bin"))


@pytest.mark.parametrize("url", ["file:///C:/a", "javascript:alert(1)", "https://user:pass@example.com", "data:text/html,x"])
def test_browser_rejects_non_web_navigation(url):
    with pytest.raises(ValueError):
        checked_url(url)


def test_cdp_filters_events_and_correlates_responses(policy):
    class Socket:
        def send(self, text):
            self.request = json.loads(text)

        def recv(self):
            return self.responses.pop(0)

    item = object.__new__(BrowserSession)
    item.ws = Socket()
    item.sequence = 0
    item.downloads = {}
    item.ws.responses = [json.dumps({"method": "Browser.downloadProgress", "params": {"guid": "g", "state": "completed"}}),
                         json.dumps({"id": 99, "result": {"wrong": True}}),
                         json.dumps({"id": 1, "result": {"correct": True}})]
    assert item.call("Page.enable") == {"correct": True}
    assert item.downloads["g"]["state"] == "completed"


def test_system_process_query_finds_current_process(policy):
    rows = system_processes(os.getpid())
    assert len(rows) == 1 and rows[0]["pid"] == os.getpid()
    assert rows[0]["name"]
    result = invoke(build_host_tools(policy), "inspect_system")
    assert result["cpu_count"] and result["disk_total_bytes"] > 0


def test_registry_covers_all_new_tools(policy):
    registry = ToolRegistry.build(policy, git_available=True)
    registry.assert_capability_coverage()
    names = {tool.name for tool in registry.tools_for("tool", ("terminal", "page_control", "files_write"))}
    assert {"start_shell", "read_shell", "write_shell", "stop_shell", "browser", "download_file"} <= names

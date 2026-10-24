"""单台 MCP Server 的连接（实施文档 M2）。

真实子进程 + 真实 stdio，不 mock SDK：这层要验证的正是"进程、握手、超时、崩溃"这些东西，
把它们全换成假对象就什么都没测到。用的是本地 ``python tests/mcp_fake_server.py``，
**不联网、不花钱**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from local_agent.mcp.client import ServerWorker
from local_agent.mcp.config import McpServerSpec
from local_agent.mcp.errors import (
    KIND_CALL_TIMEOUT,
    KIND_COMMAND_NOT_FOUND,
    KIND_HANDSHAKE_FAILED,
    KIND_NOT_CONNECTED,
    KIND_SERVER_EXITED,
    KIND_STARTUP_TIMEOUT,
    McpFailure,
)

FAKE_SERVER = Path(__file__).with_name("mcp_fake_server.py")


def _worker(
    *,
    env: dict[str, str] | None = None,
    startup_timeout: float = 20.0,
    call_timeout: float = 10.0,
    command: str | None = None,
) -> ServerWorker:
    spec = McpServerSpec(
        command=command or sys.executable,
        args=[] if command else [str(FAKE_SERVER)],
        env=env or {},
    )
    return ServerWorker(
        "fake", spec, startup_timeout=startup_timeout, call_timeout=call_timeout
    )


def test_start_reports_tools_and_call_returns_text() -> None:
    worker = _worker()
    try:
        tools = worker.start()
        names = sorted(tool["name"] for tool in tools)

        assert names == ["boom", "echo", "sleep_for"]
        echo = next(tool for tool in tools if tool["name"] == "echo")
        assert echo["description"] == "原样回显给定文本。"
        assert echo["inputSchema"]["type"] == "object"

        outcome = worker.call_tool("echo", {"text": "你好"})
        assert outcome.text == "你好"
        assert outcome.is_error is False
        assert outcome.content_kinds == ("text",)
    finally:
        worker.close()


def test_tool_exception_is_reported_as_error_result() -> None:
    worker = _worker()
    try:
        worker.start()
        outcome = worker.call_tool("boom", {})

        assert outcome.is_error is True
        # SDK 会把工具内部异常折成一句固定文案（细节留在 **server 侧**日志，不回传客户端）。
        # 这里如实按这个事实断言：不要假装我们能拿到原始堆栈。
        assert outcome.text.startswith("Error executing tool")
    finally:
        worker.close()


def test_call_timeout_is_reported_as_call_timeout() -> None:
    worker = _worker(call_timeout=0.5)
    try:
        worker.start()
        with pytest.raises(McpFailure) as excinfo:
            worker.call_tool("sleep_for", {"seconds": 3})

        assert excinfo.value.kind == KIND_CALL_TIMEOUT
    finally:
        worker.close()


def test_missing_command_is_reported_with_install_hint() -> None:
    worker = _worker(command="definitely-not-a-real-binary-xyz")

    with pytest.raises(McpFailure) as excinfo:
        worker.start()

    assert excinfo.value.kind == KIND_COMMAND_NOT_FOUND
    assert "Node.js" in str(excinfo.value)


def test_crashing_server_is_reported_as_exited() -> None:
    worker = _worker(env={"FAKE_MCP_MODE": "crash"})

    with pytest.raises(McpFailure) as excinfo:
        worker.start()

    assert excinfo.value.kind in {KIND_SERVER_EXITED, KIND_HANDSHAKE_FAILED}


def test_startup_timeout_is_reported() -> None:
    worker = _worker(env={"FAKE_MCP_MODE": "slow"}, startup_timeout=0.5)

    with pytest.raises(McpFailure) as excinfo:
        worker.start()

    assert excinfo.value.kind == KIND_STARTUP_TIMEOUT


def test_double_start_is_rejected() -> None:
    worker = _worker()
    try:
        worker.start()
        with pytest.raises(McpFailure) as excinfo:
            worker.start()
        assert excinfo.value.kind == KIND_NOT_CONNECTED
    finally:
        worker.close()


def test_call_before_start_is_rejected() -> None:
    worker = _worker()

    with pytest.raises(McpFailure) as excinfo:
        worker.call_tool("echo", {"text": "x"})

    assert excinfo.value.kind == KIND_NOT_CONNECTED


def test_close_stops_the_connection() -> None:
    worker = _worker()
    worker.start()
    assert worker.alive is True

    worker.close()

    assert worker.alive is False


def test_timeout_does_not_break_the_next_call() -> None:
    worker = _worker(call_timeout=0.25)
    try:
        worker.start()
        with pytest.raises(McpFailure) as error:
            worker.call_tool("sleep_for", {"seconds": 1})
        assert error.value.kind == KIND_CALL_TIMEOUT
        assert worker.call_tool("echo", {"text": "after-timeout"}).text == "after-timeout"
    finally:
        worker.close()


def test_close_cancels_an_active_call_without_waiting_for_its_timeout() -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    worker = _worker(call_timeout=60)
    worker.start()
    thread = worker._thread
    with ThreadPoolExecutor() as pool:
        call = pool.submit(worker.call_tool, "sleep_for", {"seconds": 30})
        time.sleep(0.2)
        started = time.monotonic()
        worker.close()
        assert time.monotonic() - started < 6
        assert thread is not None and not thread.is_alive()
        with pytest.raises(McpFailure):
            call.result(timeout=2)


def test_tool_listing_shares_the_startup_deadline(monkeypatch) -> None:
    import asyncio
    import time

    async def slow_list(self, session):
        await asyncio.sleep(20)
    monkeypatch.setattr(ServerWorker, "_list_all", slow_list)
    worker = _worker(startup_timeout=2)
    started = time.monotonic()
    try:
        with pytest.raises(McpFailure) as error:
            worker.start()
        assert error.value.kind == KIND_STARTUP_TIMEOUT
        assert time.monotonic() - started < 8
    finally:
        worker.close()


def test_large_and_structured_results_are_bounded_and_preserved() -> None:
    from mcp import types

    from local_agent.mcp.client import MAX_RESULT_CHARS, _outcome

    huge = _outcome(types.CallToolResult(content=[types.TextContent(type="text", text="x" * 200_000)]))
    assert len(huge.text) < MAX_RESULT_CHARS + 100
    assert "200000" in huge.text and "截断" in huge.text
    structured = _outcome(types.CallToolResult(content=[], structured_content={"answer": 42}))
    assert '"answer":42' in structured.text


def test_summary_text_keeps_the_structured_design_list() -> None:
    import json

    from mcp import types

    from local_agent.mcp.client import _outcome

    payload = {"totalDesigns": 2, "designs": [{"name": "专家档案"}, {"name": "首页"}]}
    outcome = _outcome(types.CallToolResult(
        content=[types.TextContent(type="text", text="Loaded 2 designs.")],
        structured_content=payload,
    ))
    summary, structured = outcome.text.split("\n", 1)
    assert summary == "Loaded 2 designs."
    assert json.loads(structured) == payload


def test_json_compatibility_text_does_not_duplicate_structured_content() -> None:
    from mcp import types

    from local_agent.mcp.client import _outcome

    text = '{ "answer": 42 }'
    outcome = _outcome(types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content={"answer": 42},
    ))
    assert outcome.text == text


def test_sdk_string_result_wrapper_does_not_duplicate_text() -> None:
    from mcp import types

    from local_agent.mcp.client import _outcome

    outcome = _outcome(types.CallToolResult(
        content=[types.TextContent(type="text", text="你好")],
        structured_content={"result": "你好"},
    ))
    assert outcome.text == "你好"


def test_combined_text_and_structured_results_share_the_length_limit() -> None:
    from mcp import types

    from local_agent.mcp.client import MAX_RESULT_CHARS, _outcome

    outcome = _outcome(types.CallToolResult(
        content=[types.TextContent(type="text", text="summary")],
        structured_content={"payload": "x" * 100_000},
    ))
    assert outcome.text.startswith('summary\n{"payload":')
    assert len(outcome.text) < MAX_RESULT_CHARS + 100
    assert "已截断" in outcome.text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows cmd + Job Object lifecycle")
def test_windows_cmd_process_tree_is_reaped(tmp_path) -> None:
    import ctypes
    from ctypes import wintypes

    from local_agent.mcp.config import McpServerSpec

    pids_path = tmp_path / "pids.txt"
    wrapper = tmp_path / "fake npx.cmd"
    wrapper.write_text(f'@echo off\n"{sys.executable}" "{FAKE_SERVER}"\n')
    worker = ServerWorker("tree", McpServerSpec(command=str(wrapper), env={
        "FAKE_MCP_MODE": "tree", "FAKE_MCP_PIDS": str(pids_path),
    }), startup_timeout=20, call_timeout=10)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    try:
        worker.start()
        pids = [int(value) for value in pids_path.read_text().split(",")]
        handles = [kernel.OpenProcess(0x1000 | 0x100000, False, pid) for pid in pids]
        assert all(handles)
        worker.close()
        assert worker._thread is not None and not worker._thread.is_alive()
        for handle in handles:
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            assert code.value != 259, "MCP parent or descendant survived close"
    finally:
        worker.close()
        for handle in handles:
            if handle:
                kernel.CloseHandle(handle)


def test_protocol_error_does_not_get_misclassified_as_a_process_exit() -> None:
    from mcp import types
    from mcp.shared.exceptions import MCPError

    from local_agent.mcp.client import _describe_failure
    failure = _describe_failure(MCPError(types.INVALID_PARAMS, "bad arguments"), name="fake", ready=True)
    assert failure.kind == "protocol_error"

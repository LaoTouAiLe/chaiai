"""Real ConPTY boundary checks; never substitute an unrestricted process."""

import os
import sys
import time
from pathlib import Path

import pytest

from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import open_terminal_process
from local_agent.security.shell_guard import build_environment
from local_agent.security.windows_pty import supported


def drain_until(process, marker: bytes, timeout=10):
    output = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output.extend(process.read())
        if marker in output:
            return bytes(output)
        time.sleep(.02)
    pytest.fail(f"Missing {marker!r}: exit={process.poll()}, output={output!r}")


@pytest.mark.skipif(not supported(), reason="ConPTY + Windows sandbox required")
@pytest.mark.parametrize("mode", ["workspace-write", "read-only"])
def test_conpty_input_resize_and_write_boundary(tmp_path, app_config, mode):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    protected = workspace / ".git"
    protected.mkdir()
    sentinel = protected / "sentinel"
    sentinel.write_text("protected", encoding="utf-8")
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace,
                                       external_policy=ExternalPathPolicy(mode=mode))
    sandbox = policy.shell_sandbox_request(cwd=workspace)
    assert sandbox is not None
    shell = str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
    process = open_terminal_process([shell, "/D", "/Q"], cwd=workspace,
                                    env=build_environment(()), sandbox=sandbox, cols=80, rows=24)
    output = bytearray()
    try:
        process.resize(100, 35)
        process.write(f'echo inside>inside.txt\r\necho BAD>"{outside}"\r\necho BAD>.git/sentinel\r\necho PTY_DONE\r\n'.encode())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            output.extend(process.read())
            if b"PTY_DONE" in output:
                break
            time.sleep(.02)
        if mode == "workspace-write":
            assert (workspace / "inside.txt").read_text().strip() == "inside", output.decode(errors="replace")
        else:
            assert not (workspace / "inside.txt").exists()
        assert outside.read_text() == "keep"
        assert sentinel.read_text() == "protected"
        assert b"PTY_DONE" in output
        process.write(b"exit 0\r\n")
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            output.extend(process.read())
            time.sleep(.02)
        assert process.poll() == 0
    finally:
        process.close()
        process.close()


@pytest.mark.skipif(not supported(), reason="ConPTY + Windows sandbox required")
def test_conpty_repl_ctrl_c_and_descendant_cleanup(tmp_path, app_config):
    import ctypes
    from ctypes import wintypes

    from local_agent.security import win_sandbox

    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path,
                                       external_policy=ExternalPathPolicy(mode="workspace-write"))
    sandbox = policy.shell_sandbox_request(cwd=tmp_path)
    python = getattr(sys, "_base_executable", sys.executable)
    process = open_terminal_process([python, "-q", "-X", "utf8"], cwd=tmp_path,
                                    env=build_environment(()), sandbox=sandbox, cols=80, rows=24)
    child_handle = None
    try:
        drain_until(process, b">>>")
        process.write(b"print('REPL_' + 'READY')\r")
        drain_until(process, b"REPL_READY")
        process.write(b"import time; time.sleep(60)\r")
        time.sleep(.3)
        process.write(b"\x03")
        drain_until(process, b"KeyboardInterrupt")
        process.write(b"print('AFTER_' + 'CTRL_C')\r")
        drain_until(process, b"AFTER_CTRL_C")
        # The child outlives its command, so cleanup must act on the entire Job.
        command = "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print('CHILD_PID=' + str(p.pid))\r"
        process.write(command.encode())
        output = drain_until(process, b"CHILD_PID=")
        # The echoed command contains the marker too: wait for a numeric result.
        import re
        deadline = time.monotonic() + 5
        while not re.search(rb"CHILD_PID=(\d+)", output) and time.monotonic() < deadline:
            output += process.read()
            time.sleep(.02)
        match = re.search(rb"CHILD_PID=(\d+)", output)
        assert match, output
        kernel = win_sandbox._kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        child_handle = kernel.OpenProcess(0x00100000, False, int(match.group(1)))
        assert child_handle, ctypes.get_last_error()
        process.close()
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        process.close()
        if child_handle:
            win_sandbox._kernel32.CloseHandle(child_handle)

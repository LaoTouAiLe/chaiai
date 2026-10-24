"""Known Schannel errors offer recovery without rerunning commands or changing outcomes."""

import pytest

from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import ProcessOutcome, SandboxRequest
from local_agent.tools import shell_tools

ERROR = b"curl: (35) schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS (0x8009030E)"


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
@pytest.mark.parametrize("signature", [ERROR, ERROR.lower(), ERROR.replace(b"SEC_E_NO_CREDENTIALS ", b"")])
def test_schannel_hint_preserves_failure_and_runs_only_once(
    tmp_path, app_config, monkeypatch, stream, signature,
) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    request = SandboxRequest()
    monkeypatch.setattr(policy, "shell_sandbox_request", lambda **kwargs: request)
    calls = []
    raw = {"stdout": b"", "stderr": b""}
    raw[stream] = signature

    def run(*args, **kwargs):
        calls.append(kwargs["sandbox"])
        return ProcessOutcome(exit_code=35, **raw)

    monkeypatch.setattr(policy.processes, "run", run)
    result = shell_tools.run_shell_command("curl https://example.com/file", policy=policy)
    assert calls == [request]
    assert result.exit_code == 35
    assert result.stdout == raw["stdout"].decode()
    assert result.stderr == raw["stderr"].decode()
    assert not result.timed_out
    assert "urllib.request" in result.note
    assert "当前权限" in result.note
    assert "证书验证" in result.note
    assert "HTTP" in result.note and "哈希" in result.note


@pytest.mark.parametrize("exit_code,stderr,sandboxed,timed_out,cancelled", [
    (0, ERROR, True, False, False),
    (1, ERROR, True, False, False),
    (35, ERROR, False, False, False),
    (35, b"schannel: SEC_E_ALGORITHM_MISMATCH", True, False, False),
    (35, b"AcquireCredentialsHandle: SEC_E_NO_CREDENTIALS", True, False, False),
    (35, b"schannel: AcquireCredentialsHandle failed", True, False, False),
    (35, b"schannel: SEC_E_NO_CREDENTIALS", True, False, False),
    (35, b"SSL certificate problem", True, False, False),
    (35, ERROR, True, True, False),
    (35, ERROR, True, False, True),
])
def test_other_results_do_not_receive_schannel_recovery_hint(
    tmp_path, app_config, monkeypatch, exit_code, stderr, sandboxed, timed_out, cancelled,
) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    monkeypatch.setattr(policy, "shell_sandbox_request", lambda **kwargs: SandboxRequest() if sandboxed else None)
    monkeypatch.setattr(policy.processes, "run", lambda *args, **kwargs: ProcessOutcome(
        exit_code=exit_code, stdout=b"", stderr=stderr, timed_out=timed_out, cancelled=cancelled,
    ))
    result = shell_tools.run_shell_command("curl https://example.com/file", policy=policy)
    assert result.exit_code == exit_code
    assert result.stderr == stderr.decode()
    assert "urllib.request" not in (result.note or "")
    if timed_out:
        assert "已终止" in result.note
    if cancelled:
        assert "已被取消" in result.note

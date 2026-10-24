"""权限三档重构审查：配置往返、损坏档位与真实 Windows 访问边界。"""

import subprocess
import sys
from pathlib import Path

import pytest

from local_agent.config import SecurityConfig, WorkspaceConfig
from local_agent.exceptions import SecurityError
from local_agent.model_settings.store import META_EXTERNAL_PATHS_MODE, open_settings_database
from local_agent.security import win_sandbox
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import ProcessRegistry

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL / 受限令牌")


@pytest.mark.parametrize("mode", [None, "allow", "danger-full-access"])
def test_legacy_full_access_config_survives_validation_round_trip(mode) -> None:
    values = {"allow_external_paths": True}
    if mode is not None:
        values["external_paths"] = {"mode": mode}
    config = WorkspaceConfig.model_validate(values)
    restored = WorkspaceConfig.model_validate(config.model_dump())
    assert restored.external_paths.mode == "danger-full-access"


@pytest.mark.parametrize("default", ["workspace-write", "danger-full-access"])
def test_unknown_stored_mode_never_inherits_a_writable_factory_default(tmp_path, default) -> None:
    database = open_settings_database(tmp_path / "settings.sqlite", default_external_paths_mode=default)
    try:
        database.connection.execute(
            "INSERT INTO settings_meta(key,value,updated_at) VALUES (?,?,?)",
            (META_EXTERNAL_PATHS_MODE, "unrecognized-mode", "2026-10-15T00:00:00Z"),
        )
        database.connection.commit()
        assert database.permissions.mode() == "read-only"
        assert database.permissions.policy().allows_workspace_write() is False
    finally:
        database.connection.close()


SID = "S-1-5-123-456-789-111"


@pytest.mark.parametrize("ace", [
    f"D;OICI;GR;;;{SID}",  # 拒绝读不是拒绝写。
    f"D;OICI;GW;;;{SID}9",  # 相似 SID 不是同一个主体。
    f"D;IO;GWSD;;;{SID}",  # 只继承、缺少子树继承范围。
    f"XD;OICI;GWSD;;;{SID};(FALSE)",  # 条件拒绝不能当无条件拒绝。
])
def test_incomplete_or_different_denials_do_not_satisfy_write_protection(monkeypatch, tmp_path, ace) -> None:
    monkeypatch.setattr(win_sandbox, "_dacl_sddl", lambda _: f"D:({ace})")
    assert not win_sandbox._dacl_denies_write(str(tmp_path), SID)


@pytest.mark.parametrize("ace", [f"D;OICI;GRGW;;;{SID}", f"A;OICI;GW;;;{SID}", f"A;OICI;GRGW;;;{SID}9"])
def test_root_sid_presence_does_not_prove_a_read_write_inheritable_grant(monkeypatch, tmp_path, ace) -> None:
    monkeypatch.setattr(win_sandbox, "_dacl_sddl", lambda _: f"D:({ace})")
    assert not win_sandbox._dacl_mentions_sid(str(tmp_path), SID)


@pytest.mark.parametrize("sddl", [
    f"D:(A;OICI;GRGWSD;;;{SID})",
    f"D:(A;;0x13019f;;;{SID})(A;OICIIO;GWGRSD;;;{SID})",
])
def test_equivalent_windows_grant_encodings_are_reused(monkeypatch, tmp_path, sddl) -> None:
    monkeypatch.setattr(win_sandbox, "_dacl_sddl", lambda _: sddl)
    assert win_sandbox._dacl_mentions_sid(str(tmp_path), SID)


def _policy(root: Path, mode: str) -> SecurityPolicy:
    return SecurityPolicy(workspace_root=root, security=SecurityConfig(), workspace=WorkspaceConfig(),
                          external_policy=ExternalPathPolicy(mode=mode))


@WINDOWS
def test_failure_to_deny_a_protected_subtree_blocks_the_command(tmp_path, monkeypatch) -> None:
    protected = tmp_path / ".ssh"
    protected.mkdir()
    marker = protected / "credential.txt"
    marker.write_text("original", encoding="utf-8")
    original = win_sandbox._set_access

    def refuse_deny(path, entry):
        if Path(path) == protected and entry.grfAccessMode == win_sandbox.DENY_ACCESS:
            raise win_sandbox.SandboxUnavailable("SetNamedSecurityInfoW 失败：5（保护目录）")
        return original(path, entry)

    monkeypatch.setattr(win_sandbox, "_set_access", refuse_deny)
    with pytest.raises(SecurityError, match="受保护"):
        _policy(tmp_path, "workspace-write").shell_sandbox_request()
    assert marker.read_text(encoding="utf-8") == "original"
    monkeypatch.setattr(win_sandbox, "_set_access", original)
    assert _policy(tmp_path, "workspace-write").shell_sandbox_request() is not None
    assert win_sandbox._dacl_denies_write(str(protected), win_sandbox.workspace_sid_text(str(tmp_path)))


@WINDOWS
def test_protected_subtree_remains_readable_while_writes_are_denied(tmp_path) -> None:
    protected = tmp_path / ".git"
    protected.mkdir()
    target = protected / "review.txt"
    target.write_text("original", encoding="utf-8")
    request = _policy(tmp_path, "workspace-write").shell_sandbox_request()
    read = ProcessRegistry().run([sys.executable, "-B", "-c",
        "import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_text())", str(target)],
        sandbox=request, timeout_seconds=30)
    assert read.exit_code == 0, read.stderr
    assert read.stdout.strip() == b"original"
    write = ProcessRegistry().run([sys.executable, "-B", "-c",
        "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('changed')", str(target)],
        sandbox=request, timeout_seconds=30)
    assert write.exit_code != 0
    assert target.read_text(encoding="utf-8") == "original"


@WINDOWS
def test_read_only_tier_can_read_an_owner_rights_workspace_without_enabling_writes(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("readable", encoding="utf-8")
    subprocess.run(["icacls", str(tmp_path), "/inheritance:r", "/grant:r",
                    "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "*S-1-3-4:(OI)(CI)F"],
                   check=True, capture_output=True)
    request = _policy(tmp_path, "read-only").shell_sandbox_request()
    assert request is not None and request.writable_sids == ()
    result = ProcessRegistry().run([sys.executable, "-B", "-c",
        "import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_text())", str(target)],
        cwd=tmp_path, sandbox=request, timeout_seconds=30)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == b"readable"
    marker = tmp_path / "blocked.txt"
    denied = ProcessRegistry().run([sys.executable, "-B", "-c",
        "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('bad')", str(marker)],
        cwd=tmp_path, sandbox=request, timeout_seconds=30)
    assert denied.exit_code != 0 and not marker.exists()


@WINDOWS
def test_sandbox_preserves_the_parent_devnull_stdin_contract(tmp_path) -> None:
    request = _policy(tmp_path, "workspace-write").shell_sandbox_request()
    result = ProcessRegistry().run([sys.executable, "-B", "-c",
        "import sys;assert sys.stdin is not None;assert sys.stdin.read()=='';print('EOF')"],
        sandbox=request, timeout_seconds=30)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == b"EOF"


def test_frozen_runner_uses_an_application_dispatch_flag(monkeypatch) -> None:
    from local_agent.security import win_ipc

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(win_ipc, "prepare_bundle", lambda: Path("verified-ipc-cache"))
    argv = win_sandbox.runner_argv(["cmd.exe", "/c", "echo hi"], writable_sids=(SID,))
    assert argv == [sys.executable, "--internal-win-sandbox-run", "--ipc-dir", "verified-ipc-cache",
                    "--sid", SID, "--", "cmd.exe", "/c", "echo hi"]


def test_frozen_entry_dispatches_runner_before_loading_api(monkeypatch) -> None:
    from local_agent import startup

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["chaiai.exe", "--internal-win-sandbox-run", "--sid", SID, "--", "cmd.exe"])
    seen = []
    monkeypatch.setattr(win_sandbox, "main", lambda argv: seen.append(argv) or 17)
    assert startup.main() == 17
    assert seen == [["--sid", SID, "--", "cmd.exe"]]


@WINDOWS
def test_cached_materialization_keeps_reporting_unchecked_directories(tmp_path, monkeypatch) -> None:
    sid = win_sandbox.workspace_sid_text(str(tmp_path))

    def walk(root, *, followlinks, onerror):
        yield str(root), [], []
        onerror(OSError(5, "denied", str(tmp_path / "unreadable")))

    monkeypatch.setattr(win_sandbox.os, "walk", walk)
    first = win_sandbox.grant_write(str(tmp_path), sid)
    assert first.skipped
    assert win_sandbox.grant_write(str(tmp_path), sid) == first


@WINDOWS
def test_inheritable_grant_does_not_reach_a_junction_target_outside_the_workspace(tmp_path) -> None:
    workspace, outside = tmp_path / "ws", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "junction"
    subprocess.run(["cmd.exe", "/d", "/s", "/c", "mklink", "/J", str(link), str(outside)],
                   capture_output=True, check=True)
    try:
        request = _policy(workspace, "workspace-write").shell_sandbox_request()
        marker = outside / "escaped.txt"
        outcome = ProcessRegistry().run([sys.executable, "-B", "-c",
            "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('bad')", str(marker)],
            sandbox=request, timeout_seconds=30)
        assert outcome.exit_code != 0
        assert not marker.exists()
    finally:
        link.rmdir()  # 只删除本测试刚创建的 junction 本身，不递归进入目标。


@WINDOWS
def test_restart_reuses_real_root_and_protected_aces_without_propagating_again(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    sid = win_sandbox.workspace_sid_text(str(tmp_path))
    win_sandbox.grant_write(str(tmp_path), sid)
    monkeypatch.setattr(win_sandbox, "_MATERIALIZED_ROOTS", {})
    monkeypatch.setattr(win_sandbox, "_set_access", lambda *_: pytest.fail("已完整授权，不应再次传播 ACL"))
    assert win_sandbox.grant_write(str(tmp_path), sid).skipped == ()

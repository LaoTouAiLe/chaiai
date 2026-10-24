"""受保护普通文件无法修改 DACL 时，必须验证真实受限令牌的不可变更性。"""

import subprocess
import sys

import pytest

from local_agent.config import SecurityConfig, WorkspaceConfig
from local_agent.exceptions import SecurityError
from local_agent.security import win_sandbox as sandbox
from local_agent.security.path_guard import ExternalPathPolicy, normalize_grant_path
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import ProcessRegistry

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL 与 AccessCheck")


def policy_for(root, grants=None):
    return SecurityPolicy(workspace_root=root, security=SecurityConfig(), workspace=WorkspaceConfig(),
        external_policy=ExternalPathPolicy(mode="workspace-write", grants=grants or {}))


@pytest.fixture
def protected_file(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    marker = root / ".git"
    marker.write_text("gitdir: example\n", encoding="utf-8")
    # 普通账户可修改文件，但不能改其 DACL；不继承父目录的新授权。
    subprocess.run(["icacls", str(marker), "/inheritance:r", "/grant:r",
        "*S-1-5-32-545:M", "*S-1-3-4:R"], check=True, capture_output=True)
    return root, marker


def test_unmodifiable_but_already_read_only_file_does_not_block_commands(protected_file, tmp_path):
    root, marker = protected_file
    original = sandbox._dacl_sddl(str(marker))
    policy = policy_for(root)
    request = policy.shell_sandbox_request()
    assert request is not None
    assert sandbox._dacl_sddl(str(marker)) == original
    report = sandbox.grant_write(str(root), sandbox.workspace_sid_text(str(root)))
    assert report.read_only_files == (str(marker),)
    assert not sandbox._dacl_denies_write(str(marker), request.writable_sids[0])
    command = [sys.executable, "-B", "-c",
        "from pathlib import Path; print(Path('.git').read_text()); Path('ok.txt').write_text('ok')"]
    result = ProcessRegistry().run(command, cwd=root, sandbox=request, timeout_seconds=30)
    assert result.exit_code == 0, result.stderr
    assert (root / "ok.txt").read_text() == "ok"
    for code in ("p.write_text('changed')", "p.unlink()", "p.rename('moved')"):
        result = ProcessRegistry().run([sys.executable, "-B", "-c",
            f"from pathlib import Path; p=Path('.git'); {code}"],
            cwd=root, sandbox=request, timeout_seconds=30)
        assert result.exit_code != 0
        assert marker.read_text() == "gitdir: example\n"
    outside = tmp_path / "outside.txt"
    result = ProcessRegistry().run([sys.executable, "-B", "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('bad')", str(outside)],
        sandbox=request, timeout_seconds=30)
    assert result.exit_code != 0 and not outside.exists()


def test_failed_deny_is_not_ignored_for_a_writable_file(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("keep")
    original = sandbox._set_access

    def reject_deny(path, entry):
        if entry.grfAccessMode == sandbox.DENY_ACCESS:
            raise sandbox.SandboxUnavailable("simulated WRITE_DAC failure")
        return original(path, entry)

    monkeypatch.setattr(sandbox, "_set_access", reject_deny)
    with pytest.raises(SecurityError, match="只读核验失败"):
        policy_for(tmp_path).shell_sandbox_request()


def test_parent_delete_child_permission_blocks_fallback(protected_file):
    root, _marker = protected_file
    sid = sandbox.workspace_sid_text(str(root))
    sandbox._set_object_access(str(root), sandbox._access_entry(sandbox._current_user_sid_text(),
        sandbox.GRANT_ACCESS, inheritance=0, permissions=0x40))
    sandbox._set_object_access(str(root), sandbox._access_entry(sid, sandbox.GRANT_ACCESS,
        inheritance=0, permissions=0x40))
    with pytest.raises(SecurityError, match="父目录仍允许删除"):
        policy_for(root).shell_sandbox_request()


def test_all_granted_sids_are_checked_after_materialization(protected_file, tmp_path):
    root, _marker = protected_file
    other = tmp_path / "other"
    other.mkdir()
    extra = sandbox.workspace_sid_text(str(other))
    sandbox._set_object_access(str(root), sandbox._access_entry(sandbox._current_user_sid_text(),
        sandbox.GRANT_ACCESS, inheritance=0, permissions=0x40))
    # 只给额外授权根的 SID 删除子项权限；单独的工作区 SID 核验会通过。
    sandbox._set_object_access(str(root), sandbox._access_entry(extra, sandbox.GRANT_ACCESS,
        inheritance=0, permissions=0x40))
    with pytest.raises(SecurityError, match="父目录仍允许删除"):
        policy_for(root, {normalize_grant_path(other): "read_write"}).shell_sandbox_request()


def test_cached_files_are_rechecked_without_rescanning(protected_file, monkeypatch):
    root, _marker = protected_file
    policy = policy_for(root)
    assert policy.shell_sandbox_request() is not None
    monkeypatch.setattr(sandbox.os, "walk", lambda *_a, **_kw: pytest.fail("不应重新扫描"))
    assert policy.shell_sandbox_request() is not None
    sid = sandbox.workspace_sid_text(str(root))
    sandbox._set_object_access(str(root), sandbox._access_entry(sandbox._current_user_sid_text(),
        sandbox.GRANT_ACCESS, inheritance=0, permissions=0x40))
    sandbox._set_object_access(str(root), sandbox._access_entry(sid, sandbox.GRANT_ACCESS,
        inheritance=0, permissions=0x40))
    with pytest.raises(SecurityError, match="父目录仍允许删除"):
        policy.shell_sandbox_request()


def test_access_check_failure_remains_closed(protected_file, monkeypatch):
    root, _marker = protected_file

    def fail(*_args):
        raise sandbox.SandboxUnavailable("AccessCheck failed")

    monkeypatch.setattr(sandbox, "_access_allowed", fail)
    with pytest.raises(SecurityError, match="AccessCheck failed"):
        policy_for(root).shell_sandbox_request()


@pytest.mark.parametrize("permission", [0x2, 0x4, 0x10, 0x100, 0x10000, 0x40000, 0x80000])
def test_every_individual_mutation_right_blocks_fallback(protected_file, monkeypatch, permission):
    root, marker = protected_file
    monkeypatch.setattr(sandbox, "_access_allowed", lambda path, _token, mask:
        path == str(marker) and mask == permission)
    with pytest.raises(sandbox.SandboxUnavailable, match="仍可变更"):
        sandbox.verify_read_only_files((str(marker),), writable_sids=(sandbox.workspace_sid_text(str(root)),))

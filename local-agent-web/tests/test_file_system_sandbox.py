"""统一文件后端的真实 I/O、权限变化与原子发布边界。"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from local_agent.cancellation import CancellationToken
from local_agent.config import AppConfig
from local_agent.exceptions import FileOperationError, SecurityError, WorkspaceBoundaryError
from local_agent.security import file_system
from local_agent.security.approval import action_fingerprint
from local_agent.security.path_guard import ExternalPathPolicy, normalize_grant_path
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.file_tools import (
    delete_file_target,
    edit_file_content,
    list_dir_entries,
    read_file_content,
)
from local_agent.tools.result_spill import save_result
from local_agent.tools.search_tools import glob_files, grep_content


@pytest.fixture
def policy(tmp_path: Path, app_config: AppConfig) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "danger-full-access"])
@pytest.mark.parametrize("access", [None, "read", "read_write"])
def test_external_read_and_write_follow_the_request_policy(
    tmp_path, policy, mode, access,
) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    note = outside / "note.txt"
    note.write_text("external", encoding="utf-8")
    policy.workspace_root = workspace
    grants = {} if access is None else {normalize_grant_path(outside): access}
    policy.external_policy = ExternalPathPolicy(mode=mode, grants=grants)
    assert policy.files.read_bytes(note) == b"external"
    arguments = {"path": str(outside / "new.txt"), "content": "new"}
    if mode != "read-only" and access == "read_write":
        policy.files.create_file(arguments, b"new")
        assert (outside / "new.txt").read_bytes() == b"new"
    else:
        with pytest.raises(SecurityError):
            policy.files.create_file(arguments, b"new")
        assert not (outside / "new.txt").exists()


@pytest.mark.parametrize("tool", ["write_file", "delete_file"])
def test_even_an_existing_grant_does_not_override_read_only(tmp_path, policy, tool) -> None:
    target = tmp_path / "note.txt"
    target.write_text("keep", encoding="utf-8")
    arguments = {"path": "note.txt", "content": "changed", "overwrite": True, "recursive": False}
    policy.grant(action_fingerprint(tool, arguments, policy=policy))
    policy.external_policy = ExternalPathPolicy(mode="read-only")
    if tool != "store":
        decision = policy.classify_tool(tool, {"save_full": True})
        assert not decision.allowed and not decision.requires_approval
    with pytest.raises(SecurityError, match="只读"):
        if tool == "delete_file":
            policy.files.delete(arguments)
        else:
            policy.files.replace_file(tool, arguments, b"changed")
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("operation", ["read", "list", "grep", "glob", "write"])
def test_cancelled_request_stops_file_operations(tmp_path, policy, operation) -> None:
    (tmp_path / "note.txt").write_text("keep", encoding="utf-8")
    token = CancellationToken()
    policy.cancellation = token
    token.cancel("stop")
    with pytest.raises(SecurityError):
        if operation == "read":
            read_file_content("note.txt", policy=policy)
        elif operation == "list":
            list_dir_entries(policy=policy)
        elif operation == "grep":
            grep_content("keep", policy=policy)
        elif operation == "glob":
            glob_files("*.txt", policy=policy)
        else:
            policy.files.create_file({"path": "new.txt", "content": "x"}, b"x")
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.parametrize("change", ["mode", "approval", "cancel"])
def test_overwrite_rechecks_authority_before_atomic_publication(tmp_path, policy, monkeypatch, change) -> None:
    policy.security.require_approval.overwrite_existing_file = True
    target = tmp_path / "note.txt"
    target.write_bytes(b"keep")
    arguments = {"path": "note.txt", "content": "changed", "overwrite": True}
    fingerprint = action_fingerprint("write_file", arguments, policy=policy)
    policy.grant(fingerprint)
    original = file_system.os.chmod

    def change_authority(path, mode):
        original(path, mode)
        if change == "mode":
            policy.external_policy = ExternalPathPolicy(mode="read-only")
        elif change == "approval":
            policy.revoke(fingerprint)
        else:
            policy.cancellation = CancellationToken()
            policy.cancellation.cancel("stop before publish")

    monkeypatch.setattr(file_system.os, "chmod", change_authority)
    with pytest.raises(SecurityError):
        policy.files.replace_file("write_file", arguments, b"changed")
    assert target.read_bytes() == b"keep"
    assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]


def test_edit_rejects_another_writer_after_reading(tmp_path, policy, monkeypatch) -> None:
    target = tmp_path / "note.txt"
    target.write_bytes(b"old\nsecond\n")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    original = file_system.os.chmod

    def concurrent_change(path, mode):
        original(path, mode)
        target.write_bytes(b"another writer\n")

    monkeypatch.setattr(file_system.os, "chmod", concurrent_change)
    with pytest.raises(FileOperationError, match="expected_sha256"):
        edit_file_content("note.txt", policy=policy, old_text="old", new_text="new", expected_sha256=expected)
    assert target.read_bytes() == b"another writer\n"
    assert [path.name for path in tmp_path.iterdir()] == ["note.txt"]


@pytest.mark.parametrize("protected", [".git", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".codex", ".env"])
def test_recursive_approval_cannot_delete_protected_descendants(tmp_path, policy, protected) -> None:
    directory = tmp_path / "tree"
    directory.mkdir()
    (directory / "ordinary.txt").write_text("keep", encoding="utf-8")
    target = directory / protected
    if protected == ".env":
        target.write_text("keep", encoding="utf-8")
    else:
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
    arguments = {"path": "tree", "recursive": True}
    policy.grant(action_fingerprint("delete_file", arguments, policy=policy))
    with pytest.raises(SecurityError):
        delete_file_target("tree", policy=policy, recursive=True)
    assert target.exists()
    assert (directory / "ordinary.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("tool", ["grep", "glob", "store"])
def test_read_only_search_cannot_spill_files(tmp_path, policy, tool) -> None:
    (tmp_path / "note.txt").write_text("keep", encoding="utf-8")
    policy.external_policy = ExternalPathPolicy(mode="read-only")
    with pytest.raises(SecurityError, match="只读"):
        if tool == "grep":
            grep_content("keep", policy=policy, save_full=True)
        elif tool == "glob":
            glob_files("*.txt", policy=policy, save_full=True)
        else:
            save_result(workspace_root=tmp_path, kind="grep", key="test", lines=["keep"], complete=True, policy=policy)
    assert not (tmp_path / ".local-agent").exists()
    assert grep_content("keep", policy=policy).total_matches == 1


def test_result_store_cannot_write_outside_its_owned_directory(tmp_path, policy) -> None:
    with pytest.raises(WorkspaceBoundaryError):
        policy.files.result_path(tmp_path / "user.txt")
    assert not (tmp_path / "user.txt").exists()


@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
def test_backend_cannot_replace_without_the_required_write_intent(tmp_path, policy, tool) -> None:
    target = tmp_path / "note.txt"
    target.write_bytes(b"keep")
    arguments = {"path": "note.txt", "old_text": "keep", "new_text": "changed"}
    with pytest.raises(FileOperationError if tool == "edit_file" else SecurityError):
        policy.files.replace_file(tool, arguments, b"changed")
    assert target.read_bytes() == b"keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction")
def test_retargeted_junction_is_rejected_before_publication(tmp_path, policy, monkeypatch) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    safe = workspace / "safe"
    safe.mkdir(parents=True)
    outside.mkdir()
    (safe / "note.txt").write_bytes(b"inside")
    (outside / "note.txt").write_bytes(b"outside")
    link = workspace / "alias"

    def junction(target):
        subprocess.run(["cmd.exe", "/d", "/s", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)

    junction(safe)
    policy.workspace_root = workspace
    arguments = {"path": "alias/note.txt", "content": "changed", "overwrite": True}
    policy.grant(action_fingerprint("write_file", arguments, policy=policy))
    original = file_system.os.chmod

    def retarget(path, mode):
        original(path, mode)
        link.rmdir()  # 只删除本测试创建的 junction，不递归删除目标。
        junction(outside)

    monkeypatch.setattr(file_system.os, "chmod", retarget)
    try:
        with pytest.raises(SecurityError):
            policy.files.replace_file("write_file", arguments, b"changed")
        assert (safe / "note.txt").read_bytes() == b"inside"
        assert (outside / "note.txt").read_bytes() == b"outside"
        assert list(safe.glob("*.tmp")) == []
    finally:
        link.rmdir()

def test_safe_delete_moves_the_target_into_the_trash(tmp_path: Path, app_config: AppConfig) -> None:
    """安全删除：开关打开时删文件是**移进系统回收站**，不是抹掉。

    这条会真的往本机回收站丢一个临时文件——那正是被测行为本身，不是测试的副作用。
    """
    config = app_config.model_copy(update={
        "security": app_config.security.model_copy(update={"safe_delete": True}),
    })
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})
    target = tmp_path / "note.txt"
    target.write_text("keep me", encoding="utf-8")

    result = delete_file_target("note.txt", policy=policy)

    assert result.trashed is True
    assert not target.exists(), "原位置不该还留着"


def test_safe_delete_off_keeps_the_old_unlink_behaviour(tmp_path: Path, app_config: AppConfig) -> None:
    """关掉开关就是旧行为：真删，返回里明确 `trashed=False`。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    target = tmp_path / "note.txt"
    target.write_text("bye", encoding="utf-8")

    result = delete_file_target("note.txt", policy=policy)

    assert result.trashed is False
    assert not target.exists()

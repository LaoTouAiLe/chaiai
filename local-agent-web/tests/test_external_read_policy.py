"""外部默认读取、限定搜索范围与可撤销写目录的真实 I/O 回归。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from local_agent.exceptions import SecurityError
from local_agent.model_settings import InMemoryCredentialStore, open_settings_database
from local_agent.security.approval import action_fingerprint
from local_agent.security.path_guard import ExternalPathPolicy, normalize_grant_path
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.file_tools import list_dir_entries, read_file_content
from local_agent.tools.search_tools import glob_files, grep_content

MODES = ["read-only", "workspace-write", "danger-full-access"]


@pytest.fixture
def external(tmp_path, app_config):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})
    return policy, outside


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", ["write_file", "edit_file", "delete_file"])
def test_call_approval_cannot_authorize_ungranted_external_mutations(external, mode, tool):
    policy, outside = external
    policy.external_policy = ExternalPathPolicy(mode=mode)
    target = outside / "keep.txt"
    target.write_bytes(b"keep")
    arguments = {"path": str(target), "content": "changed", "overwrite": True}
    policy.grant(action_fingerprint(tool, arguments, policy=policy))
    with pytest.raises(SecurityError):
        if tool == "delete_file":
            policy.files.delete(arguments)
        else:
            policy.files.replace_file(
                tool, arguments, b"changed",
                expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest()
                if tool == "edit_file" else None,
            )
    assert target.read_bytes() == b"keep"
    assert list(outside.iterdir()) == [target]


@pytest.mark.parametrize("mode", MODES)
def test_external_search_paths_can_be_read_back_and_defaults_stay_in_workspace(external, mode):
    policy, outside = external
    policy.external_policy = ExternalPathPolicy(mode=mode)
    expected = []
    for relative in ("src/main.py", "src/nested/main.py"):
        target = outside / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"external-marker\n")
        expected.append(target.as_posix())
    (policy.workspace_root / "local.py").write_text("local-only", encoding="utf-8")

    assert {entry.name for entry in list_dir_entries(str(outside), policy=policy).entries} == {"src"}
    found = grep_content("external-marker", policy=policy, path=str(outside), glob="src/**/*.py")
    listed = glob_files("src/**/*.py", policy=policy, path=str(outside))
    assert {match.path for match in found.matches} == set(expected)
    assert set(listed.files) == set(expected)
    for path in listed.files:
        assert read_file_content(path, policy=policy).content == "external-marker\n"
    assert glob_files("**/*.py", policy=policy).files == ["local.py"]
    assert grep_content("external-marker", policy=policy).total_matches == 0


def test_external_search_respects_its_gitignore_and_sensitive_names(external):
    policy, outside = external
    (outside / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (outside / "ignored").mkdir()
    ignored = outside / "ignored" / "generated.py"
    ignored.write_text("marker", encoding="utf-8")
    sensitive = outside / "secret.key"
    sensitive.write_text("marker", encoding="utf-8")
    safe = outside / "safe.py"
    safe.write_text("marker", encoding="utf-8")
    assert glob_files("*", policy=policy, path=str(outside)).files == [safe.as_posix()]
    assert [hit.path for hit in grep_content("marker", policy=policy, path=str(outside)).hits] == [safe.as_posix()]
    # 显式点名可读忽略文件，敏感文件即便显式点名也不读。
    assert grep_content("marker", policy=policy, path=str(ignored)).total_matches == 1
    with pytest.raises(SecurityError):
        read_file_content(str(sensitive), policy=policy)


@pytest.mark.parametrize("access", ["read", "read_write"])
def test_removing_persisted_grant_only_removes_write_authority(tmp_path, external, access):
    policy, outside = external
    database = open_settings_database(tmp_path / "settings.sqlite", credentials=InMemoryCredentialStore())
    try:
        database.permissions.grant(outside, access)
        policy.external_policy = database.permissions.policy()
        assert policy.external_policy.grants[normalize_grant_path(outside)] == access
        target = outside / "new.txt"
        arguments = {"path": str(target), "content": "created"}
        if access == "read_write":
            policy.files.create_file(arguments, b"created")
        else:
            with pytest.raises(SecurityError):
                policy.files.create_file(arguments, b"created")
            target.write_bytes(b"created")
        assert database.permissions.revoke(outside)
        policy.external_policy = database.permissions.policy()
        assert not policy.external_policy.grants
        assert read_file_content(str(target), policy=policy).content == "created"
        # 逐次批准不能代替目录写授权。
        arguments.update(overwrite=True)
        policy.grant(action_fingerprint("write_file", arguments, policy=policy))
        with pytest.raises(SecurityError):
            policy.files.replace_file("write_file", arguments, b"changed")
        assert target.read_bytes() == b"created"
    finally:
        database.connection.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction 回归")
def test_external_search_and_recursive_list_do_not_follow_junctions(external, tmp_path):
    policy, outside = external
    other = tmp_path / "other"
    other.mkdir()
    (other / "hidden.txt").write_text("junction-marker", encoding="utf-8")
    link = outside / "branch"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(other)], check=True, capture_output=True)
    try:
        assert glob_files("*", policy=policy, path=str(outside)).files == []
        assert grep_content("junction-marker", policy=policy, path=str(outside)).total_matches == 0
        assert all(entry.name != "hidden.txt" for entry in list_dir_entries(str(outside), policy=policy, recursive=True).entries)
    finally:
        link.rmdir()  # 只移除本测试的 junction，不递归删除其目标。


@pytest.mark.parametrize("mode", MODES)
def test_read_and_search_across_real_local_drives(external, mode):
    policy, _outside = external
    policy.external_policy = ExternalPathPolicy(mode=mode)
    local_temp = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "Temp"
    if not local_temp.is_dir():
        pytest.skip("未找到第二本机盘上的临时目录")
    with tempfile.TemporaryDirectory(prefix="chaiai-cross-drive-", dir=local_temp) as temporary:
        outside = Path(temporary)
        if outside.drive.lower() == policy.workspace_root.drive.lower():
            pytest.skip("系统临时目录与测试工作区同盘，无法验证跨盘")
        target = outside / "note.txt"
        target.write_text("cross-drive-marker", encoding="utf-8")
        assert read_file_content(str(target), policy=policy).content == "cross-drive-marker"
        assert [entry.path for entry in list_dir_entries(str(outside), policy=policy).entries] == [target.as_posix()]
        assert glob_files("*.txt", policy=policy, path=str(outside)).files == [target.as_posix()]
        assert grep_content("cross-drive-marker", policy=policy, path=str(outside)).total_matches == 1

"""扫描上下文保持保护规则，并避免重复路径解析和目录类型查询。"""

import collections
import os
import subprocess
from pathlib import Path

import pytest

from local_agent.security import path_guard, win_sandbox


@pytest.mark.parametrize("relative", [
    "normal/file.py", ".git/config", ".aws/credentials", ".ssh/id_rsa", ".codex/auth.json",
    "critical/sub/file", ".GiT/config", ".git /config", "credentials./file",
])
def test_scan_context_matches_normal_path_checks(tmp_path, monkeypatch, relative):
    critical = (tmp_path / "critical").resolve()
    monkeypatch.setattr(path_guard, "_system_directories", lambda: (critical,))
    context = path_guard.ProtectedPathContext.current()
    target = tmp_path / relative
    assert context.reason_for_resolved(target.resolve()) == path_guard.protected_target_reason(target)
    for target in (Path.home(), Path(tmp_path.anchor)):
        assert context.reason_for_resolved(target.resolve()) == path_guard.protected_target_reason(target)


def test_fixed_boundaries_are_resolved_once_per_scan_and_refreshed_between_scans(tmp_path, monkeypatch):
    original = path_guard._resolve
    calls = []

    def resolve(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(path_guard, "_resolve", resolve)
    context = path_guard.ProtectedPathContext.current()
    initial = len(calls)
    assert initial > 0
    for index in range(100):
        assert context.reason_for_resolved(tmp_path / f"ordinary-{index}") is None
    assert len(calls) == initial
    new_critical = (tmp_path / "new-critical").resolve()
    monkeypatch.setattr(path_guard, "_system_directories", lambda: (new_critical,))
    assert context.reason_for_resolved(new_critical) is None
    assert path_guard.ProtectedPathContext.current().reason_for_resolved(new_critical) is not None


def test_scanner_resolves_each_candidate_once_and_reuses_enumerated_directory_type(tmp_path, monkeypatch):
    (tmp_path / "src" / ".ssh" / "nested").mkdir(parents=True)
    (tmp_path / "src" / ".ssh" / "nested" / ".git").write_text("not scanned")
    (tmp_path / ".git").write_text("gitdir: metadata")
    (tmp_path / "src" / "normal").mkdir()
    original = Path.resolve
    calls = collections.Counter()

    def resolve(path, *args, **kwargs):
        if path != tmp_path and tmp_path in path.parents:
            calls[str(path)] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_dir", lambda *_a, **_kw: pytest.fail("目录枚举已给出类型"))
    result = list(win_sandbox._protected_targets(tmp_path, lambda e: pytest.fail(str(e))))
    assert set(result) == {tmp_path / ".git", tmp_path / "src" / ".ssh"}
    assert calls == {str(path): 1 for path in (
        tmp_path / "src", tmp_path / ".git", tmp_path / "src" / ".ssh", tmp_path / "src" / "normal",
    )}


def test_scanner_still_protects_system_and_home_paths_with_ordinary_names(tmp_path, monkeypatch):
    critical = tmp_path / "critical"
    home = tmp_path / "account"
    for directory in (critical, home):
        (directory / "nested").mkdir(parents=True)
        (directory / "nested" / ".git").write_text("not scanned")
    monkeypatch.setattr(path_guard, "_system_directories", lambda: (critical.resolve(),))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert set(win_sandbox._protected_targets(tmp_path, lambda e: pytest.fail(str(e)))) == {critical, home}


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_junction_target_is_not_scanned_or_protected_through_the_link(tmp_path):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    (outside / ".git").mkdir(parents=True)
    link = workspace / "junction"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
        check=True, capture_output=True)
    try:
        assert list(win_sandbox._protected_targets(workspace, lambda e: pytest.fail(str(e)))) == []
    finally:
        link.rmdir()  # 仅移除本测试创建的 junction 本身。


def test_protected_file_symlink_is_not_followed(tmp_path):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("unchanged")
    link = workspace / ".git"
    try:
        link.symlink_to(outside)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("当前 Windows 账户缺少创建符号链接权限")
        raise
    assert list(win_sandbox._protected_targets(workspace, lambda e: pytest.fail(str(e)))) == []
    assert outside.read_text() == "unchanged"

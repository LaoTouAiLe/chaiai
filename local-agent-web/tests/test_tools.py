"""T05–T07 验收：文件工具与安全 Shell 的正常、错误与边界行为。

全部使用临时工作区，不联网、不触碰真实项目文件。
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from local_agent.config import DEFAULT_SHELL_TIMEOUT_SECONDS, AppConfig
from local_agent.exceptions import SecurityError, WorkspaceBoundaryError
from local_agent.security import shell_guard
from local_agent.security.approval import action_fingerprint
from local_agent.security.diff_guard import parse_patch
from local_agent.security.policy import SecurityPolicy
from local_agent.tools import file_tools, search_tools
from local_agent.tools.file_tools import (
    MAX_RECURSIVE_DEPTH,
    build_file_tools,
    delete_file_target,
    edit_file_content,
    list_dir_entries,
    read_file_content,
    write_file_content,
)
from local_agent.tools.shell_tools import build_shell_tools, run_shell_command


@pytest.fixture
def policy(tmp_path: Path, app_config: AppConfig) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})


def _config_with_workspace(app_config: AppConfig, **overrides: object) -> AppConfig:
    data = app_config.model_dump()
    data["workspace"].update(overrides)
    return AppConfig.model_validate(data)


def _config_with_large_edit(app_config: AppConfig, max_changed_lines: int) -> AppConfig:
    data = app_config.model_dump()
    data["security"]["large_edit"]["max_changed_lines_without_approval"] = max_changed_lines
    return AppConfig.model_validate(data)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- grep / glob
#
# 2026-09-30 新增。在它们之前，tool 模式**没有任何检索能力**，Shell 兜底也不可用
# （shell_guard 的白名单要求参数完全匹配，搜索类命令一律落到"需人工审批"），模型只能枚举目录 +
# 通读文件；实测一次"梳理架构"的提问做了 30 次 list_dir + 26 次 read_file。这些用例钉住的是
# "先定位再定点读"这件事本身，以及它与 read_file / 按需 RAG 相同的边界口径。


def test_grep_finds_content_with_line_numbers(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.py").write_text("alpha\nbeta target\ngamma\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("target on first line\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("target\n", encoding="utf-8")

    result = search_tools.grep_content("target", policy=policy, glob="*.py")

    # 行号是"能定位"的最小条件：没有它模型只能整篇读。
    assert [(match.path, match.line_number) for match in result.matches] == [
        ("a.py", 2),
        ("b.py", 1),
    ]
    assert result.total_matches == 2
    assert result.truncated is False
    assert "没有匹配" not in (result.note or "")


def test_grep_files_with_matches_lists_without_content(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """``files_with_matches`` 只给文件名与计数：模型据此决定读哪几个，而不是把命中行全读进来。"""
    (tmp_path / "a.py").write_text("hit\nhit\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("hit\n", encoding="utf-8")

    result = search_tools.grep_content("hit", policy=policy, output_mode="files_with_matches")

    assert result.matches == []
    assert [(hit.path, hit.count) for hit in result.hits] == [("a.py", 2), ("b.py", 1)]
    assert result.total_matches == 3


def test_grep_skips_sensitive_dependency_and_binary_files(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """密钥文件、依赖目录与二进制都不进结果——与 read_file / 按需 RAG 同一口径。"""
    (tmp_path / ".env").write_text("SECRET=hit\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("hit\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"hit\x00\x01")
    (tmp_path / "a.py").write_text("hit\n", encoding="utf-8")

    result = search_tools.grep_content("hit", policy=policy)

    assert [hit.path for hit in result.hits] == ["a.py"]


def test_grep_accepts_a_single_file_as_the_search_root(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """``path`` 指向**文件**时只搜那个文件（``rg pattern file.py`` 的语义）。

    实测缺陷（2026-09-30）：此前要求必须是目录，于是模型最自然的用法——``path='README.md'``
    ——被拒，界面弹出"工具被安全策略拒绝: grep"，文案还误说成"改为在工作区内操作"（路径其实就在
    工作区内）。一轮里被这样拒了 4 次，模型只好改写成 ``glob='README.md'`` 绕过去。
    """
    (tmp_path / "a.py").write_text("target one\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("target two\n", encoding="utf-8")

    result = search_tools.grep_content("target", policy=policy, path="a.py")

    assert [hit.path for hit in result.hits] == ["a.py"]
    assert [(match.path, match.line_number) for match in result.matches] == [("a.py", 1)]


def test_grep_on_a_sensitive_file_as_root_returns_nothing(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """把密钥文件当检索根同样不返回内容：跳过规则在"单文件"这条路径上照旧生效。"""
    (tmp_path / ".env").write_text("SECRET=target\n", encoding="utf-8")

    result = search_tools.grep_content("target", policy=policy, path=".env")

    assert result.hits == []
    assert result.matches == []


def test_grep_missing_path_is_a_parameter_error_not_a_policy_denial(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """不存在的路径是**参数问题**、不是策略问题：抛 ``ValueError``（映射成"工具执行失败／检查工具
    参数"），不要抛 ``SecurityError``——后者会显示成"被安全策略拦截"，把用户引到策略设置上去。"""
    with pytest.raises(ValueError):
        search_tools.grep_content("x", policy=policy, path="nope.py")


def test_grep_refuses_outside_workspace_and_bad_regex(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    with pytest.raises(WorkspaceBoundaryError):
        search_tools.grep_content("x", policy=policy, path="..")
    with pytest.raises(ValueError):
        search_tools.grep_content("([unclosed", policy=policy)


def test_grep_reports_truncation_instead_of_silently_cutting(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """到上限要**如实标注**，让模型知道"还有更多"，而不是以为结果就这些。"""
    (tmp_path / "a.py").write_text("hit\n" * 50, encoding="utf-8")

    result = search_tools.grep_content("hit", policy=policy, max_matches=3)

    assert result.total_matches == 3
    assert result.truncated is True
    assert "命中上限" in (result.note or "")


def test_glob_matches_nested_and_zero_level_paths(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """``src/**/*.py`` 必须也能匹配 ``src/a.py``：``**/`` 的语义是"零个或多个目录"。"""
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "deep" / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "c.md").write_text("", encoding="utf-8")

    result = search_tools.glob_files("src/**/*.py", policy=policy)

    assert result.files == ["src/a.py", "src/deep/b.py"]


def test_glob_refuses_outside_workspace(tmp_path: Path, policy: SecurityPolicy) -> None:
    with pytest.raises(WorkspaceBoundaryError):
        search_tools.glob_files("*.py", policy=policy, path="..")


def test_search_tools_are_registered_as_read_only() -> None:
    """两个工具必须登记进只读白名单：否则安全门会把它们当未知工具拒掉。"""
    from local_agent.security.policy import KNOWN_TOOLS, READ_ONLY_TOOLS

    assert "grep" in READ_ONLY_TOOLS and "glob" in READ_ONLY_TOOLS
    assert "grep" in KNOWN_TOOLS and "glob" in KNOWN_TOOLS


# --------------------------------------------------------------------------- list_dir


def test_list_dir_reports_entries(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")

    result = list_dir_entries(".", policy=policy)

    assert [entry.name for entry in result.entries] == ["a.txt", "sub"]
    assert result.total_entries == 2  # 默认不返回隐藏项
    kinds = {entry.name: entry.kind for entry in result.entries}
    assert kinds == {"a.txt": "file", "sub": "dir"}
    assert not result.truncated
    assert result.path == "."


def test_list_dir_includes_hidden_when_requested(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")

    result = list_dir_entries(".", policy=policy, include_hidden=True)

    assert [entry.name for entry in result.entries] == [".hidden"]
    assert result.total_entries == 1


def test_list_dir_truncates_at_max_entries(tmp_path: Path, policy: SecurityPolicy) -> None:
    for index in range(5):
        (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")

    result = list_dir_entries(".", policy=policy, max_entries=2)

    # 有界枚举：最多读"额度 + 1"项，所以 total_entries 是"已枚举到的条数"，不再是目录真总数
    assert result.total_entries == 3
    assert len(result.entries) == 2
    assert result.truncated
    assert "截断" in (result.note or "")


def test_list_dir_recursive_limits_depth(tmp_path: Path, policy: SecurityPolicy) -> None:
    deep = tmp_path
    for index in range(MAX_RECURSIVE_DEPTH + 2):
        deep = deep / f"d{index}"
        deep.mkdir()

    result = list_dir_entries(".", policy=policy, recursive=True)

    assert result.truncated
    assert "递归深度" in (result.note or "")


def test_list_dir_rejects_parent_traversal(tmp_path: Path, policy: SecurityPolicy) -> None:
    with pytest.raises(WorkspaceBoundaryError):
        list_dir_entries("..", policy=policy)


def test_list_dir_rejects_file_target(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    with pytest.raises(SecurityError):
        list_dir_entries("a.txt", policy=policy)


# --------------------------------------------------------------------------- read_file


def test_read_file_returns_content_hash_and_lines(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    path = tmp_path / "a.txt"
    # newline="" 避免 Windows 把换行转成 \r\n，否则断言的是测试自己的写入行为
    path.write_text("第一行\n第二行\n", encoding="utf-8", newline="")

    result = read_file_content("a.txt", policy=policy)

    assert result.content == "第一行\n第二行\n"
    assert result.line_count == 2
    assert result.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.encoding == "utf-8"
    assert not result.truncated
    assert not result.is_binary
    assert result.path == "a.txt"


def test_read_file_selects_line_range(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.txt").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8", newline="")

    result = read_file_content("a.txt", policy=policy, start_line=2, end_line=3)

    assert result.content == "l2\nl3\n"
    assert result.line_count == 4


def test_read_file_rejects_invalid_line_range(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.txt").write_text("l1\nl2\n", encoding="utf-8")

    with pytest.raises(ValueError):
        read_file_content("a.txt", policy=policy, start_line=3, end_line=2)


def test_read_file_truncates_at_max_chars(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.txt").write_text("x" * 100, encoding="utf-8")

    result = read_file_content("a.txt", policy=policy, max_chars=10)

    assert result.content == "x" * 10
    assert result.truncated
    assert "截断" in (result.note or "")


def test_read_file_marks_binary_without_content(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03")

    result = read_file_content("blob.bin", policy=policy)

    assert result.is_binary
    assert result.content == ""
    assert "二进制" in (result.note or "")


def test_read_file_reports_unknown_encoding(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "weird.txt").write_bytes(b"\xff\xfe\x41\x42")

    result = read_file_content("weird.txt", policy=policy)

    assert not result.is_binary
    assert result.content == ""
    assert "无法按 UTF-8 解码" in (result.note or "")


def test_read_file_respects_max_file_size(tmp_path: Path, app_config: AppConfig) -> None:
    policy = SecurityPolicy.from_config(
        _config_with_workspace(app_config, max_file_size_bytes=4),
        workspace_root=tmp_path,
        environ={},
    )
    (tmp_path / "a.txt").write_text("0123456789", encoding="utf-8")

    result = read_file_content("a.txt", policy=policy)

    assert result.truncated
    assert result.content == "0123"
    assert "超过" in (result.note or "")
    assert result.sha256 == _sha256(tmp_path / "a.txt")


def test_edit_file_preserves_utf8_bom(tmp_path: Path, policy: SecurityPolicy) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfalpha\n")

    edit_file_content(
        "bom.txt",
        policy=policy,
        old_text="alpha",
        new_text="beta",
        expected_sha256=_sha256(target),
    )

    assert target.read_bytes() == b"\xef\xbb\xbfbeta\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX 文件权限位语义")
def test_edit_file_preserves_posix_mode(tmp_path: Path, policy: SecurityPolicy) -> None:
    target = tmp_path / "script.sh"
    target.write_text("echo old\n", encoding="utf-8", newline="")
    target.chmod(0o755)

    edit_file_content(
        "script.sh",
        policy=policy,
        old_text="old",
        new_text="new",
        expected_sha256=_sha256(target),
    )

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_action_fingerprint_streams_existing_file_hash(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审批指纹不能用 ``Path.read_bytes`` 把任意大的已有文件整体装进内存。"""
    target = tmp_path / "large.txt"
    target.write_bytes(b"x" * (2 * 1024 * 1024))

    def _forbidden(_path: Path) -> bytes:
        raise AssertionError("动作指纹不应整体读取文件")

    monkeypatch.setattr(Path, "read_bytes", _forbidden)
    fingerprint = action_fingerprint(
        "write_file",
        {"path": "large.txt", "content": "replacement", "overwrite": True},
        policy=policy,
    )

    assert fingerprint


def test_read_file_blocks_secret_files(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-real", encoding="utf-8")

    with pytest.raises(SecurityError) as excinfo:
        read_file_content(".env", policy=policy)

    assert "禁止读取" in str(excinfo.value)
    assert "sk-real" not in str(excinfo.value)


def _counting_iterdir(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """统计 ``Path.iterdir()`` 实际产出的条目：验证"枚举"有界，而不仅是返回值有界。"""
    seen: list[Path] = []
    original = Path.iterdir

    def counting(self: Path):
        for child in original(self):
            seen.append(child)
            yield child

    monkeypatch.setattr(Path, "iterdir", counting)
    return seen


def test_list_dir_stops_early_at_entry_limit(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大目录：只读"额度 + 1"项就停，既不遍历整棵树也不枚举整个目录（复审要求）。"""
    for index in range(40):
        (tmp_path / f"f{index:02d}.txt").write_text("x", encoding="utf-8", newline="")
    deep = tmp_path / "deep"
    deep.mkdir()
    (deep / "deep.txt").write_text("x", encoding="utf-8", newline="")
    seen = _counting_iterdir(monkeypatch)

    result = list_dir_entries(".", policy=policy, recursive=True, max_entries=5)

    assert len(result.entries) == 5
    assert result.total_entries == 5  # 没有继续枚举，所以"总数"就是已枚举到的条数
    assert result.truncated and result.note is not None
    assert "未继续遍历" in result.note
    assert "deep.txt" not in "|".join(str(entry) for entry in result.entries)
    assert len(seen) <= 6  # 40 个条目里最多读"额度 + 1"项


def test_list_dir_non_recursive_also_bounds_enumeration(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非递归分支同样有界：目录很大也不会先把整个目录枚举出来（复审要求）。"""
    for index in range(40):
        (tmp_path / f"f{index:02d}.txt").write_text("x", encoding="utf-8", newline="")
    seen = _counting_iterdir(monkeypatch)

    result = list_dir_entries(".", policy=policy, max_entries=5)

    assert len(result.entries) == 5
    assert result.truncated
    assert len(seen) <= 6


def test_list_dir_does_not_descend_past_depth_limit(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """不进入 ``MAX_RECURSIVE_DEPTH`` 以下的层级，且明确告知被省略（复审要求）。"""
    current = tmp_path
    for level in range(1, MAX_RECURSIVE_DEPTH + 2):
        current = current / f"l{level}"
        current.mkdir()
        (current / f"file-{level}.txt").write_text("x", encoding="utf-8", newline="")

    result = list_dir_entries(".", policy=policy, recursive=True, max_entries=500)

    rendered = "|".join(str(entry) for entry in result.entries)
    assert f"file-{MAX_RECURSIVE_DEPTH}.txt" in rendered
    assert f"file-{MAX_RECURSIVE_DEPTH + 1}.txt" not in rendered
    # 超深目录**本身**也不该出现在结果里（与提示口径一致）
    assert f"l{MAX_RECURSIVE_DEPTH + 1}" not in rendered
    assert result.note is not None and "递归深度超过" in result.note


def _link_directory(link: Path, target: Path) -> bool:
    """尽量创建指向 ``target`` 的目录链接：先试符号链接，Windows 上退回 Junction。

    Junction（``mklink /J``）不需要管理员或开发者模式，这样主开发平台不会总是跳过该用例。
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import subprocess

        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0 and link.exists():
            return True
    return False


def test_list_dir_does_not_follow_directory_links_outside_workspace(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """T14C 复审：严格模式递归列目录不得跟着目录符号链接/Junction 越出工作区。

    关键是场景要塞对：``outside`` 必须是 **workspace 的兄弟目录**，否则它本来就在工作区内，
    遍历它是合法行为。只看 ``is_symlink()`` 也不够：Windows Junction 未必被识别为符号链接，
    所以实现必须在入栈前解析真实目标并确认仍位于本次列举根目录内。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("外部机密", encoding="utf-8", newline="")
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})
    link = workspace / "link"
    if not _link_directory(link, outside):
        pytest.skip("当前环境无法创建目录链接（符号链接需权限，Junction 也失败）")

    result = list_dir_entries(".", policy=policy, recursive=True)

    rendered = "|".join(str(entry) for entry in result.entries)
    assert "secret.txt" not in rendered  # 外部的名字/大小/时间都不能泄露
    assert "link" in rendered  # 链接本身仍是工作区内的一项


def test_delete_hard_rejected_target_does_not_scan_or_fingerprint(
    tmp_path: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T14C 复审：硬拒绝的删除立即失败——不算动作指纹、不描述目标（两者都会遍历目录）。"""
    config = _config_with_workspace(app_config, external_paths={"mode": "allow"})
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})
    calls: list[str] = []
    monkeypatch.setattr(
        file_tools,
        "action_fingerprint",
        lambda *args, **kwargs: calls.append("fingerprint") or "stub",
    )
    monkeypatch.setattr(
        file_tools, "_describe_target", lambda *args, **kwargs: calls.append("describe") or ""
    )

    with pytest.raises(SecurityError) as excinfo:
        delete_file_target(Path.home().as_posix(), policy=policy, recursive=True)

    assert "受保护目标" in str(excinfo.value)
    assert calls == []


def test_read_file_rejects_outside_path(tmp_path: Path, policy: SecurityPolicy) -> None:
    with pytest.raises(WorkspaceBoundaryError):
        read_file_content("../outside.txt", policy=policy)


def test_read_file_reports_missing_file(tmp_path: Path, policy: SecurityPolicy) -> None:
    with pytest.raises(SecurityError):
        read_file_content("nope.txt", policy=policy)


def test_external_paths_allow_mode_keeps_hard_constraints(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """T38：``allow`` 档只放行**显式绝对路径**，且不放宽四项硬约束。

    "读写授权视同工作区内"改变的是常规小改动是否逐次询问；覆盖已有文件、删除文件、
    破坏性 Shell、大范围编辑仍然必须人工批准（架构 §1.5）。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    note = outside / "note.txt"
    note.write_text("外部内容", encoding="utf-8")
    config = _config_with_workspace(app_config, external_paths={"mode": "allow"})
    policy = SecurityPolicy.from_config(config, workspace_root=workspace, environ={})

    # 只读：绝对路径的外部目标放行，并且能被识别为"外部"
    assert policy.resolve_readable_file(str(note)).read_text(encoding="utf-8") == "外部内容"
    assert policy.is_external(note)
    assert not policy.is_external(workspace / "inside.txt")

    # `~` 与相对路径逃逸不能当作外部路径放行（任何档位都不行）
    for raw in ("~/x.txt", "../outside/note.txt"):
        with pytest.raises(WorkspaceBoundaryError):
            policy.resolve_path(raw)

    # 读写档下的常规动作：新建外部文件不再逐次询问
    created = policy.classify_tool(
        "write_file", {"path": str(outside / "new.txt"), "content": "新"}
    )
    assert created.allowed and not created.requires_approval

    # 硬约束一：覆盖已有文件仍然必须人工审批（reason 不再以"工作区外"为由，但改不了这一条）
    decision = policy.classify_tool(
        "write_file", {"path": str(note), "content": "改", "overwrite": True}
    )
    assert decision.requires_approval and not decision.allowed
    assert "覆盖已有文件" in decision.reason

    # 硬约束二：删除文件仍然必须人工审批
    removed = policy.classify_tool("delete_file", {"path": str(note)})
    assert removed.requires_approval and not removed.allowed

    # 受保护目标（当前用户主目录）即使批准也不执行
    with pytest.raises(SecurityError):
        policy.resolve_writable_path(str(Path.home()))

    # 出厂默认（``whitelist`` + 空白名单）下同一个绝对外部路径直接拒绝，提示指向设置页
    strict = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})
    with pytest.raises(WorkspaceBoundaryError) as excinfo:
        strict.resolve_path(str(note))
    assert "设置" in str(excinfo.value) and "权限" in str(excinfo.value)


# --------------------------------------------------------------------------- 工具包装


def test_built_tools_match_architecture_signatures(policy: SecurityPolicy) -> None:
    tools = {tool.name: tool for tool in build_file_tools(policy) + build_shell_tools(policy)}

    assert list(tools) == [
        "list_dir",
        "read_file",
        "edit_file",
        "write_file",
        "delete_file",
        "run_shell",
    ]
    assert list(tools["list_dir"].args) == ["path", "recursive", "include_hidden", "max_entries"]
    assert list(tools["read_file"].args) == ["path", "start_line", "end_line", "max_chars"]
    assert list(tools["edit_file"].args) == [
        "path",
        "old_text",
        "new_text",
        "patch",
        "replace_all",
        "expected_sha256",
        "reason",
    ]
    assert list(tools["write_file"].args) == ["path", "content", "overwrite", "reason"]
    assert list(tools["delete_file"].args) == ["path", "recursive", "reason"]
    assert list(tools["run_shell"].args) == ["command", "cwd", "timeout_seconds", "env_keys"]


def test_tool_invocation_returns_json(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.txt").write_text("内容", encoding="utf-8")
    tools = {tool.name: tool for tool in build_file_tools(policy)}

    payload = tools["read_file"].invoke({"name": "read_file", "args": {"path": "a.txt"}, "id": "c1", "type": "tool_call"})

    assert "内容" in payload.content
    assert payload.tool_call_id == "c1"


def test_tool_invocation_still_enforces_boundary(policy: SecurityPolicy) -> None:
    tools = {tool.name: tool for tool in build_file_tools(policy)}

    with pytest.raises(WorkspaceBoundaryError):
        tools["read_file"].invoke({"name": "read_file", "args": {"path": "../x"}, "id": "c1", "type": "tool_call"})


# --------------------------------------------------------------------------- edit_file


def test_edit_file_replaces_and_reports_diff(tmp_path: Path, policy: SecurityPolicy) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\ny = 2\n", encoding="utf-8", newline="")
    sha = _sha256(target)

    result = edit_file_content(
        "a.py",
        policy=policy,
        old_text="x = 1",
        new_text="x = 42",
        expected_sha256=sha,
        reason="改配置",
    )

    assert target.read_text(encoding="utf-8") == "x = 42\ny = 2\n"
    assert result.replaced_count == 1
    assert result.changed_lines == 2
    assert result.old_sha256 == sha
    assert result.new_sha256 == _sha256(target)
    assert result.new_sha256 != result.old_sha256
    assert "-x = 1" in result.diff and "+x = 42" in result.diff
    assert result.reason == "改配置"
    assert result.path == "a.py"


def test_edit_file_requires_expected_sha256(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content("a.py", policy=policy, old_text="x = 1", new_text="x = 2")

    assert "expected_sha256" in str(excinfo.value)


def test_edit_file_rejects_stale_hash(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "a.py", policy=policy, old_text="x = 1", new_text="x = 2", expected_sha256="0" * 64
        )

    assert "不一致" in str(excinfo.value)


def test_edit_file_rejects_missing_and_ambiguous_match(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    target = tmp_path / "a.py"
    target.write_text("alpha\nbeta\nalpha\n", encoding="utf-8", newline="")
    sha = _sha256(target)

    with pytest.raises(SecurityError) as missing:
        edit_file_content(
            "a.py", policy=policy, old_text="gamma", new_text="x", expected_sha256=sha
        )
    assert "没有精确匹配" in str(missing.value)

    with pytest.raises(SecurityError) as ambiguous:
        edit_file_content(
            "a.py", policy=policy, old_text="alpha", new_text="x", expected_sha256=sha
        )
    assert "匹配到 2 处" in str(ambiguous.value)


def test_edit_file_replace_all_rewrites_every_match(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    target = tmp_path / "a.py"
    target.write_text("alpha\nbeta\nalpha\n", encoding="utf-8", newline="")

    result = edit_file_content(
        "a.py",
        policy=policy,
        old_text="alpha",
        new_text="omega",
        replace_all=True,
        expected_sha256=_sha256(target),
    )

    assert result.replaced_count == 2
    assert target.read_text(encoding="utf-8") == "omega\nbeta\nomega\n"


def test_edit_file_rejects_whole_file_replacement(tmp_path: Path, policy: SecurityPolicy) -> None:
    content = "x = 1\ny = 2\n"
    target = tmp_path / "a.py"
    target.write_text(content, encoding="utf-8", newline="")

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "a.py",
            policy=policy,
            old_text=content,
            new_text="z = 3\n",
            expected_sha256=_sha256(target),
        )

    assert "整份文件" in str(excinfo.value)


def test_edit_file_rejects_identical_text(tmp_path: Path, policy: SecurityPolicy) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "a.py",
            policy=policy,
            old_text="x = 1",
            new_text="x = 1",
            expected_sha256=_sha256(target),
        )

    assert "没有需要修改的内容" in str(excinfo.value)


def test_edit_file_rejects_binary_sensitive_and_outside(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01")
    with pytest.raises(SecurityError):
        edit_file_content(
            "blob.bin",
            policy=policy,
            old_text="a",
            new_text="b",
            expected_sha256=_sha256(tmp_path / "blob.bin"),
        )

    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8", newline="")
    with pytest.raises(SecurityError) as secret:
        edit_file_content(
            ".env",
            policy=policy,
            old_text="A=1",
            new_text="A=2",
            expected_sha256=_sha256(tmp_path / ".env"),
        )
    assert "禁止修改" in str(secret.value)

    with pytest.raises(WorkspaceBoundaryError):
        edit_file_content(
            "../x.py", policy=policy, old_text="a", new_text="b", expected_sha256="0" * 64
        )


def test_edit_file_rejects_large_change(tmp_path: Path, app_config: AppConfig) -> None:
    policy = SecurityPolicy.from_config(
        _config_with_large_edit(app_config, max_changed_lines=2),
        workspace_root=tmp_path,
        environ={},
    )
    target = tmp_path / "a.py"
    target.write_text("line\n", encoding="utf-8", newline="")

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "a.py",
            policy=policy,
            old_text="line",
            new_text="\n".join(f"l{index}" for index in range(20)),
            expected_sha256=_sha256(target),
        )

    assert "大范围修改" in str(excinfo.value)


def test_edit_file_preserves_crlf_line_endings(tmp_path: Path, policy: SecurityPolicy) -> None:
    target = tmp_path / "crlf.py"
    target.write_bytes(b"a = 1\r\nb = 2\r\n")

    edit_file_content(
        "crlf.py",
        policy=policy,
        old_text="a = 1",
        new_text="a = 9",
        expected_sha256=_sha256(target),
    )

    assert target.read_bytes() == b"a = 9\r\nb = 2\r\n"


# --------------------------------------------------------------------------- T22 patch 模式


def _patch_text(*lines: str) -> str:
    """把若干行拼成 patch 文本：保留每行原文（只有一个空格的上下文空行也保持原样）。"""
    return "".join(f"{line}\n" for line in lines)


def test_parse_patch_rejects_a_blank_patch() -> None:
    """空 Patch 在解析层直接拒绝（工具层的互斥判定会先给出更通用的缺参提示）。"""
    with pytest.raises(SecurityError) as excinfo:
        parse_patch("   \n")

    assert "patch 为空" in str(excinfo.value)


def test_edit_patch_applies_hunks_and_reports_preview(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """合法 patch：改动落盘，返回 diff 预览、hunk 数与新旧 Hash（架构 §6.3）。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8", newline="")
    sha = _sha256(target)

    result = edit_file_content(
        "a.py",
        policy=policy,
        patch=_patch_text(
            "--- a/a.py",
            "+++ b/a.py",
            "@@ -1,3 +1,4 @@",
            " x = 1",
            "-y = 2",
            "+y = 22",
            "+w = 4",
            " z = 3",
        ),
        expected_sha256=sha,
        reason="补丁替换",
    )

    assert target.read_text(encoding="utf-8") == "x = 1\ny = 22\nw = 4\nz = 3\n"
    # patch 模式下 replaced_count 是 hunk 数（不是替换处数），语义写在 note 里
    assert result.replaced_count == 1
    assert result.note is not None and "hunk" in result.note
    assert result.changed_lines == 3  # 删 1 行 + 增 2 行
    assert result.old_sha256 == sha
    assert result.new_sha256 == _sha256(target)
    assert result.new_sha256 != result.old_sha256
    assert "-y = 2" in result.diff and "+y = 22" in result.diff
    assert result.reason == "补丁替换"


def test_edit_patch_applies_multiple_hunks_for_a_subdirectory_file(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """多个 hunk 各自定位；文件头只写文件名（本项目 diff 预览的形态）也必须被接受。"""
    (tmp_path / "sub").mkdir()
    target = tmp_path / "sub" / "a.py"
    target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8", newline="")

    result = edit_file_content(
        "sub/a.py",
        policy=policy,
        patch=_patch_text(
            "--- a/a.py",
            "+++ b/a.py",
            "@@ -1,2 +1,2 @@",
            "-one",
            "+ONE",
            " two",
            "@@ -4,2 +4,2 @@",
            " four",
            "-five",
            "+FIVE",
        ),
        expected_sha256=_sha256(target),
    )

    assert target.read_text(encoding="utf-8") == "ONE\ntwo\nthree\nfour\nFIVE\n"
    assert result.replaced_count == 2


def test_edit_patch_inserts_at_the_beginning_of_an_empty_file(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """计数为 0 的纯插入 hunk 按 GNU 约定定位；空文件插入后以换行结束。"""
    target = tmp_path / "empty.py"
    target.write_bytes(b"")

    edit_file_content(
        "empty.py",
        policy=policy,
        patch=_patch_text("@@ -0,0 +1,2 @@", "+a = 1", "+b = 2"),
        expected_sha256=_sha256(target),
    )

    assert target.read_bytes() == b"a = 1\nb = 2\n"


def test_edit_patch_keeps_crlf_and_a_file_without_a_final_newline(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """匹配忽略 CRLF 的 ``\\r``，写回保留每一行原有的结束符（架构 §6.3）。"""
    crlf = tmp_path / "crlf.py"
    crlf.write_bytes(b"a = 1\r\nb = 2\r\n")

    edit_file_content(
        "crlf.py",
        policy=policy,
        patch=_patch_text("@@ -1,2 +1,2 @@", "-a = 1", "+a = 9", " b = 2"),
        expected_sha256=_sha256(crlf),
    )

    assert crlf.read_bytes() == b"a = 9\r\nb = 2\r\n"

    # 末尾没有换行的文件：patch 没碰末行时原样保留，新增行仍然带换行
    tail = tmp_path / "tail.py"
    tail.write_bytes(b"a\nb")

    edit_file_content(
        "tail.py",
        policy=policy,
        patch=_patch_text("@@ -1,2 +1,3 @@", "-a", "+A", " b", "+c"),
        expected_sha256=_sha256(tail),
    )

    assert tail.read_bytes() == b"A\nb\nc\n"


def test_edit_patch_tolerates_trailing_blank_lines(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """patch 末尾的排版空行不算内容；但 hunk 体内的无前缀空行仍然拒绝。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")

    edit_file_content(
        "a.py",
        policy=policy,
        patch="@@ -1 +1 @@\n-x = 1\n+x = 2\n\n",
        expected_sha256=_sha256(target),
    )

    assert target.read_text(encoding="utf-8") == "x = 2\n"


def test_edit_patch_rejects_conflict_and_keeps_the_file(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """上下文不匹配即冲突：拒绝且原文件一字不动（架构 §6.3）。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\ny = 2\n", encoding="utf-8", newline="")
    before = _sha256(target)

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "a.py",
            policy=policy,
            patch=_patch_text("@@ -1,2 +1,2 @@", "-x = 1", "+x = 9", " zzz"),
            expected_sha256=before,
        )

    assert "上下文不匹配" in str(excinfo.value)
    assert _sha256(target) == before


def test_edit_patch_rejects_out_of_range_hunk(tmp_path: Path, policy: SecurityPolicy) -> None:
    """hunk 声明的行号超出文件长度：按冲突拒绝，不猜位置。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")
    before = _sha256(target)

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "a.py",
            policy=policy,
            patch=_patch_text("@@ -9,1 +9,1 @@", "-nope", "+nope2"),
            expected_sha256=before,
        )

    assert "定位越界" in str(excinfo.value)
    assert _sha256(target) == before


def test_edit_patch_rejects_overlapping_hunks(tmp_path: Path, policy: SecurityPolicy) -> None:
    """hunk 乱序或重叠一律拒绝（架构 §6.3）。"""
    target = tmp_path / "a.py"
    target.write_text("a\nb\nc\n", encoding="utf-8", newline="")
    before = _sha256(target)

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "a.py",
            policy=policy,
            patch=_patch_text(
                "@@ -1,3 +1,3 @@", " a", " b", " c", "@@ -2,2 +4,0 @@", "-b", "-c"
            ),
            expected_sha256=before,
        )

    assert "顺序或位置非法" in str(excinfo.value)
    assert _sha256(target) == before


def test_edit_patch_rejects_bad_structure_and_unsupported_forms(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """空 hunk、计数不符、无前缀行、Git 扩展头与 No-newline 标记都拒绝且不写盘。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\ny = 2\n", encoding="utf-8", newline="")
    before = _sha256(target)
    cases = {
        "只有文件头": (_patch_text("--- a/a.py", "+++ b/a.py"), "没有任何 hunk"),
        "计数不符": (_patch_text("@@ -1,5 +1,5 @@", "-x = 1", "+x = 9"), "计数与内容不符"),
        "空行缺空格": (_patch_text("@@ -1,2 +1,2 @@", "-x = 1", "", "+x = 9"), "无前缀行"),
        "hunk 头非法": (_patch_text("@@ 1 5 @@", "-x = 1", "+x = 9"), "hunk 头格式非法"),
        "Git 扩展头": (
            _patch_text("diff --git a/a.py b/a.py", "@@ -1 +1 @@", "-x = 1", "+x = 9"),
            "不支持的 diff 头",
        ),
        "No newline 标记": (
            _patch_text("@@ -1 +1 @@", "-x = 1", "\\ No newline at end of file", "+x = 9"),
            "No newline",
        ),
    }

    for name, (patch, expected) in cases.items():
        with pytest.raises(SecurityError) as excinfo:
            edit_file_content("a.py", policy=policy, patch=patch, expected_sha256=before)
        assert expected in str(excinfo.value), name
        assert _sha256(target) == before, name


def test_edit_patch_rejects_a_file_header_for_another_file(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """文件头指向别的文件（basename 不同）时拒绝，避免把补丁落到错的文件上。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")
    before = _sha256(target)

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "a.py",
            policy=policy,
            patch=_patch_text("--- a/b.py", "+++ b/b.py", "@@ -1 +1 @@", "-x = 1", "+x = 9"),
            expected_sha256=before,
        )

    assert "文件头指向 b.py" in str(excinfo.value)
    assert _sha256(target) == before


def test_edit_patch_rejects_an_unchanged_result(tmp_path: Path, policy: SecurityPolicy) -> None:
    """应用后内容没变（等价于空 diff）也拒绝，不产生一次没有意义的写入。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")
    before = _sha256(target)

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "a.py",
            policy=policy,
            patch=_patch_text("@@ -1 +1 @@", "-x = 1", "+x = 1"),
            expected_sha256=before,
        )

    assert "没有变化" in str(excinfo.value)
    assert _sha256(target) == before


def test_edit_patch_requires_expected_sha256_and_rejects_stale_hash(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """patch 模式与精确替换一样必须带正确的旧 Hash（架构 §6.3）。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")
    before = _sha256(target)
    patch = _patch_text("@@ -1 +1 @@", "-x = 1", "+x = 9")

    with pytest.raises(SecurityError) as missing:
        edit_file_content("a.py", policy=policy, patch=patch)
    assert "expected_sha256" in str(missing.value)

    with pytest.raises(SecurityError) as stale:
        edit_file_content("a.py", policy=policy, patch=patch, expected_sha256="0" * 64)
    assert "不一致" in str(stale.value)

    assert _sha256(target) == before


def test_edit_rejects_mixed_or_missing_input_modes(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """patch 与 old_text/new_text 互斥；两种都给或都没给都拒绝（架构 §6.3）。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")
    before = _sha256(target)

    with pytest.raises(SecurityError) as mixed:
        edit_file_content(
            "a.py",
            policy=policy,
            old_text="x = 1",
            new_text="x = 9",
            patch=_patch_text("@@ -1 +1 @@", "-x = 1", "+x = 9"),
            expected_sha256=before,
        )
    assert "不能同时使用" in str(mixed.value)

    with pytest.raises(SecurityError) as neither:
        edit_file_content("a.py", policy=policy, expected_sha256=before)
    assert "old_text 与 new_text" in str(neither.value)

    # 只有空白的 patch 视为"没有给 patch"，落到同一个缺参提示（不会当成空 Patch 执行）
    with pytest.raises(SecurityError) as blank:
        edit_file_content("a.py", policy=policy, patch="   \n", expected_sha256=before)
    assert "old_text 与 new_text" in str(blank.value)

    assert _sha256(target) == before


def test_edit_patch_over_limit_requires_approval(tmp_path: Path, app_config: AppConfig) -> None:
    """patch 模式的改动行数同样走大范围修改审批（架构 §6.3）。"""
    policy = SecurityPolicy.from_config(
        _config_with_large_edit(app_config, max_changed_lines=2),
        workspace_root=tmp_path,
        environ={},
    )
    target = tmp_path / "a.py"
    target.write_text("line\n", encoding="utf-8", newline="")
    patch = _patch_text("@@ -1 +1,9 @@", "-line", *[f"+l{index}" for index in range(9)])

    with pytest.raises(SecurityError) as excinfo:
        edit_file_content("a.py", policy=policy, patch=patch, expected_sha256=_sha256(target))

    assert "大范围修改" in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == "line\n"


# --------------------------------------------------------------------------- write_file


def test_write_file_creates_new_file(tmp_path: Path, policy: SecurityPolicy) -> None:
    result = write_file_content("new.txt", policy=policy, content="你好\n", reason="新建")

    created = tmp_path / "new.txt"
    assert created.read_text(encoding="utf-8") == "你好\n"
    assert result.sha256 == _sha256(created)
    assert result.size_bytes > 0
    assert result.line_count == 1
    assert result.path == "new.txt"
    assert result.reason == "新建"


def test_write_file_refuses_existing_file(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "exists.txt").write_text("old", encoding="utf-8", newline="")

    with pytest.raises(SecurityError) as excinfo:
        write_file_content("exists.txt", policy=policy, content="new")

    message = str(excinfo.value)
    assert "文件已存在" in message
    assert "edit_file" in message
    assert (tmp_path / "exists.txt").read_text(encoding="utf-8") == "old"


def test_write_file_refuses_overwrite_flag(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "exists.txt").write_text("old", encoding="utf-8", newline="")

    with pytest.raises(SecurityError) as excinfo:
        write_file_content("exists.txt", policy=policy, content="new", overwrite=True)

    assert "审批" in str(excinfo.value)


def test_write_file_refuses_bad_targets(tmp_path: Path, policy: SecurityPolicy) -> None:
    with pytest.raises(SecurityError) as parent:
        write_file_content("no/such/dir/f.txt", policy=policy, content="x")
    assert "父目录不存在" in str(parent.value)

    with pytest.raises(SecurityError):
        write_file_content(".env", policy=policy, content="A=1")

    with pytest.raises(WorkspaceBoundaryError):
        write_file_content("../x.txt", policy=policy, content="x")

    (tmp_path / "adir").mkdir()
    with pytest.raises(SecurityError):
        write_file_content("adir", policy=policy, content="x")


def test_write_file_enforces_size_limit(tmp_path: Path, app_config: AppConfig) -> None:
    policy = SecurityPolicy.from_config(
        _config_with_workspace(app_config, max_file_size_bytes=4),
        workspace_root=tmp_path,
        environ={},
    )

    with pytest.raises(SecurityError) as excinfo:
        write_file_content("big.txt", policy=policy, content="12345")

    assert "超过上限" in str(excinfo.value)
    assert not (tmp_path / "big.txt").exists()


# --------------------------------------------------------------------------- delete_file


def test_delete_file_always_refuses_and_keeps_the_file(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("保留我", encoding="utf-8", newline="")

    with pytest.raises(SecurityError) as excinfo:
        delete_file_target("note.txt", policy=policy)

    assert target.is_file()
    message = str(excinfo.value)
    assert "审批" in message
    assert target.resolve().as_posix() in message


def test_delete_file_reports_recursive_scope(tmp_path: Path, policy: SecurityPolicy) -> None:
    folder = tmp_path / "pkg"
    folder.mkdir()
    (folder / "a.txt").write_text("a", encoding="utf-8", newline="")
    (folder / "sub").mkdir()

    with pytest.raises(SecurityError) as excinfo:
        delete_file_target("pkg", policy=policy, recursive=True)

    message = str(excinfo.value)
    assert "1 个文件" in message
    assert "1 个子目录" in message
    assert folder.is_dir()


# --------------------------------------------------------------------------- run_shell


def _shell_policy(
    tmp_path: Path,
    app_config: AppConfig,
    *,
    whitelist: list[dict[str, object]],
    max_output_chars: int = 30_000,
    default_timeout_seconds: int = DEFAULT_SHELL_TIMEOUT_SECONDS,
) -> SecurityPolicy:
    data = app_config.model_dump()
    data["security"]["shell"]["whitelist"] = whitelist
    data["security"]["shell"]["max_output_chars"] = max_output_chars
    data["security"]["shell"]["default_timeout_seconds"] = default_timeout_seconds
    return SecurityPolicy.from_config(
        AppConfig.model_validate(data), workspace_root=tmp_path, environ={}
    )


def _entry(*args: str, executable: str = "python") -> dict[str, object]:
    return {
        "platform": ["windows", "posix"],
        "executable": executable,
        "args": list(args),
        "match": "exact",
    }


def test_run_shell_executes_whitelisted_command(tmp_path: Path, app_config: AppConfig) -> None:
    policy = _shell_policy(tmp_path, app_config, whitelist=[_entry("--version")])

    result = run_shell_command("python --version", policy=policy)

    assert result.exit_code == 0
    assert "Python" in result.stdout
    assert not result.timed_out
    assert result.duration_ms >= 0
    assert Path(result.cwd) == tmp_path.resolve()
    # 执行的是判定阶段解析出的绝对路径，而不是再搜一次 PATH
    assert Path(result.argv[0]).is_file()
    assert Path(result.argv[0]).name.casefold().startswith("python")
    assert result.argv[1:] == ["--version"]
    assert result.executor == "argv"  # 免审命令不做 Shell 适配


def test_run_shell_keeps_semicolon_inside_quotes(tmp_path: Path, app_config: AppConfig) -> None:
    """引号内的分号是普通参数内容，不应被当成 Shell 语法拒绝。"""
    policy = _shell_policy(tmp_path, app_config, whitelist=[_entry("-c", "import sys; sys.exit(3)")])

    result = run_shell_command('python -c "import sys; sys.exit(3)"', policy=policy)

    assert result.exit_code == 3


def test_run_shell_marks_timeout(tmp_path: Path, app_config: AppConfig) -> None:
    policy = _shell_policy(
        tmp_path,
        app_config,
        whitelist=[_entry("-c", "import time; time.sleep(5)")],
        default_timeout_seconds=2,
    )

    result = run_shell_command(
        'python -c "import time; time.sleep(5)"', policy=policy, timeout_seconds=1
    )

    assert result.timed_out
    assert result.exit_code is None
    assert result.note is not None and "未结束" in result.note


def test_run_shell_truncates_output(tmp_path: Path, app_config: AppConfig) -> None:
    policy = _shell_policy(
        tmp_path,
        app_config,
        whitelist=[_entry("-c", "print('x' * 500)")],
        max_output_chars=50,
    )

    result = run_shell_command("python -c \"print('x' * 500)\"", policy=policy)

    assert result.truncated
    assert len(result.stdout) == 50
    assert result.note is not None and "截断" in result.note


def test_run_shell_refuses_non_whitelisted_command(
    tmp_path: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _shell_policy(tmp_path, app_config, whitelist=[_entry("--version")])
    # 固定解析结果：这条断言只关心"不在白名单 → 需要审批"，不该依赖启动方式（PATH）
    monkeypatch.setattr(shell_guard.shutil, "which", lambda name: str(tmp_path / name))

    with pytest.raises(SecurityError) as excinfo:
        # 可解析但不在白名单（参数与白名单不一致）→ 需要审批
        run_shell_command("python -V", policy=policy)

    assert "白名单" in str(excinfo.value)


def test_run_shell_does_not_inherit_api_key(
    tmp_path: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子进程环境不得带上 API Key，且只透传基础变量。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-leak")
    policy = _shell_policy(
        tmp_path,
        app_config,
        whitelist=[_entry("-c", "import os; print('|'.join(sorted(os.environ)))")],
    )

    result = run_shell_command(
        "python -c \"import os; print('|'.join(sorted(os.environ)))\"", policy=policy
    )

    assert "DEEPSEEK_API_KEY" not in result.stdout
    assert "PATH" in result.stdout
    assert "sk-should-not-leak" not in result.stdout


def test_new_file_creation_is_atomic_and_never_overwrites(tmp_path: Path) -> None:
    """原子新建：目标已存在时必须失败，绝不覆盖（架构 §6.4）。"""
    target = tmp_path / "note.txt"
    file_tools._create_new_file(target, b"first")  # 直接验证原子创建语义

    with pytest.raises(SecurityError):
        file_tools._create_new_file(target, b"second")

    assert target.read_bytes() == b"first"


def test_write_file_refuses_file_created_after_existence_check(
    tmp_path: Path, policy: SecurityPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检查与落盘之间被别的进程创建同名文件时，不得无审批覆盖（TOCTOU）。"""
    target = tmp_path / "note.txt"
    target.write_text("别人写的", encoding="utf-8", newline="")

    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        return False if self == target else real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)

    with pytest.raises(SecurityError) as excinfo:
        write_file_content("note.txt", policy=policy, content="我的内容")

    assert "文件已存在" in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == "别人写的"


# --------------------------------------------------------------------------- T12 批准后执行


def _fingerprint(policy: SecurityPolicy, command: str) -> str:
    return action_fingerprint(
        "run_shell", {"command": command, "cwd": ".", "env_keys": []}, policy=policy
    )


def test_run_shell_uses_config_default_timeout(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """省略超时时用配置值：配置调小到 30 秒，白名单命令照样直接执行。

    修前这里会按出厂常量 60 秒判定，直接报"超时不能超过 30 秒"。
    """
    policy = _shell_policy(
        tmp_path, app_config, whitelist=[_entry("--version")], default_timeout_seconds=30
    )

    result = run_shell_command("python --version", policy=policy)

    assert result.exit_code == 0


def test_run_shell_refuses_shell_syntax_without_approval(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """管道类命令即使不在白名单也不能直接执行，必须走人工审批（架构 §6.6）。"""
    policy = _shell_policy(tmp_path, app_config, whitelist=[])
    command = "echo hi | findstr hi" if os.name == "nt" else "echo hi | grep hi"

    with pytest.raises(SecurityError) as excinfo:
        run_shell_command(command, policy=policy)

    assert "Shell 语法" in str(excinfo.value)


def test_run_shell_executes_approved_shell_syntax_command(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """批准后由平台适配器真执行（T12 验收：当前平台真执行、另一平台解析单测）。"""
    policy = _shell_policy(tmp_path, app_config, whitelist=[])
    command = "echo hi | findstr hi" if os.name == "nt" else "echo hi | grep hi"
    fingerprint = _fingerprint(policy, command)

    policy.grant(fingerprint)
    try:
        result = run_shell_command(command, policy=policy)
    finally:
        policy.revoke(fingerprint)

    assert result.exit_code == 0
    assert "hi" in result.stdout
    if os.name == "nt":
        assert result.executor == "cmd"
        assert Path(result.argv[0]).name.casefold() == "cmd.exe"
        assert result.argv[1:4] == ["/d", "/s", "/c"]
    else:
        assert result.executor == "sh"
        assert result.argv[1] == "-c"


def test_run_shell_approval_is_one_shot(tmp_path: Path, app_config: AppConfig) -> None:
    """批准是一次性的：撤销后同一条命令重新被拒（架构 §6.5 的“恰好执行一次”）。"""
    policy = _shell_policy(tmp_path, app_config, whitelist=[])
    command = "echo hi | findstr hi" if os.name == "nt" else "echo hi | grep hi"
    fingerprint = _fingerprint(policy, command)

    policy.grant(fingerprint)
    policy.revoke(fingerprint)

    with pytest.raises(SecurityError):
        run_shell_command(command, policy=policy)


def test_run_shell_approved_absolute_path_runs_that_path(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """带路径的命令批准后按解析出的 argv 执行，免审白名单不参与放行。"""
    policy = _shell_policy(tmp_path, app_config, whitelist=[])
    command = f'"{sys.executable}" --version'
    fingerprint = _fingerprint(policy, command)

    with pytest.raises(SecurityError):
        run_shell_command(command, policy=policy)

    policy.grant(fingerprint)
    try:
        result = run_shell_command(command, policy=policy)
    finally:
        policy.revoke(fingerprint)

    assert result.exit_code == 0
    assert "Python" in result.stdout
    assert result.argv == [str(Path(sys.executable).resolve()), "--version"]
    assert result.executor == "argv"


def test_run_shell_approval_is_bound_to_the_command(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """批准绑在动作指纹上：换一条命令不能复用同一份批准。"""
    policy = _shell_policy(tmp_path, app_config, whitelist=[])
    approved = "echo hi | findstr hi" if os.name == "nt" else "echo hi | grep hi"
    other = "echo bye | findstr bye" if os.name == "nt" else "echo bye | grep bye"

    policy.grant(_fingerprint(policy, approved))
    try:
        with pytest.raises(SecurityError):
            run_shell_command(other, policy=policy)
    finally:
        policy.revoke(_fingerprint(policy, approved))

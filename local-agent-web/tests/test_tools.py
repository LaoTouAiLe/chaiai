"""T05–T07 验收：文件工具与安全 Shell 的正常、错误与边界行为。

全部使用临时工作区，不联网、不触碰真实项目文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from local_agent.config import DEFAULT_SHELL_TIMEOUT_SECONDS, AppConfig
from local_agent.exceptions import FileOperationError, SecurityError, WorkspaceBoundaryError
from local_agent.security import shell_guard
from local_agent.security.approval import action_fingerprint
from local_agent.security.diff_guard import parse_patch
from local_agent.security.file_system import _create_new_file
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
from local_agent.tools.result_spill import offload_tool_result
from local_agent.tools.shell_tools import build_shell_tools, run_shell_command


@pytest.fixture
def policy(tmp_path: Path, app_config: AppConfig) -> SecurityPolicy:
    # 这组恢复用例显式启用可选文件审批；产品默认免审批由 test_autonomy_policy 覆盖。
    app_config.security.require_approval.delete_file = True
    app_config.security.require_approval.overwrite_existing_file = True
    app_config.security.require_approval.large_batch_edit = True
    return SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})


def _config_with_workspace(app_config: AppConfig, **overrides: object) -> AppConfig:
    data = app_config.model_dump()
    data["workspace"].update(overrides)
    return AppConfig.model_validate(data)


def _config_with_large_edit(app_config: AppConfig, max_files: int) -> AppConfig:
    """把"一轮内免审可改几个文件"调成指定值（0 = 第一个新文件就需要审批）。"""
    data = app_config.model_dump()
    data["security"]["require_approval"]["large_batch_edit"] = True
    data["security"]["large_edit"]["max_files_without_approval"] = max_files
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
    # 合并入口（2026-10-22）：同一组工具、同一只读门。
    assert "search_files" in READ_ONLY_TOOLS and "search_files" in KNOWN_TOOLS


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

    with pytest.raises(FileOperationError):
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


def test_read_file_is_independent_of_write_size_budget(tmp_path: Path, app_config: AppConfig) -> None:
    policy = SecurityPolicy.from_config(
        _config_with_workspace(app_config, max_file_size_bytes=4),
        workspace_root=tmp_path,
        environ={},
    )
    (tmp_path / "a.txt").write_text("0123456789", encoding="utf-8")

    result = read_file_content("a.txt", policy=policy)

    assert not result.truncated
    assert result.content == "0123456789"
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
    config = _config_with_workspace(app_config, external_paths={"mode": "danger-full-access"})
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
    with pytest.raises(FileOperationError):
        read_file_content("nope.txt", policy=policy)


def test_external_paths_danger_mode_keeps_hard_constraints(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """「完全放行」档只放行**显式绝对路径**，且不放宽四项硬约束。

    "读写授权视同工作区内"改变的是常规小改动是否逐次询问；覆盖已有文件、删除文件、
    破坏性 Shell、大范围编辑仍然必须人工批准（架构 §1.5）。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    note = outside / "note.txt"
    note.write_text("外部内容", encoding="utf-8")
    config = _config_with_workspace(app_config, external_paths={"mode": "danger-full-access"})
    policy = SecurityPolicy.from_config(config, workspace_root=workspace, environ={})

    # 只读：绝对路径的外部目标放行，并且能被识别为"外部"
    assert policy.resolve_readable_file(str(note)).read_text(encoding="utf-8") == "外部内容"
    assert policy.is_external(note)
    assert not policy.is_external(workspace / "inside.txt")

    # `~` 与相对路径逃逸不能当作外部路径放行（任何档位都不行）
    for raw in ("~/x.txt", "../outside/note.txt"):
        with pytest.raises(WorkspaceBoundaryError):
            policy.resolve_path(raw)

    # 这一档只放开读：新建外部文件同样是**硬拒绝**（理由指向"去授权那个目录"），
    # 不是"不再逐次询问"——收窄前那一条比 codex / dsh 都宽（2026-10-14）。
    created = policy.classify_tool(
        "write_file", {"path": str(outside / "new.txt"), "content": "新"}
    )
    assert not created.allowed and not created.requires_approval
    assert "只允许读" in created.reason

    # 硬约束在**已授权读写**的外部目标上照样生效（工作区外视同工作区内判，四项一条不放）
    from local_agent.security.path_guard import ExternalPathPolicy, normalize_grant_path

    writable = SecurityPolicy.from_config(
        config,
        workspace_root=workspace,
        environ={},
        external_policy=ExternalPathPolicy(
            mode="danger-full-access", grants={normalize_grant_path(outside): "read_write"}
        ),
    )

    # 硬约束一：覆盖已有文件必须人工审批
    decision = writable.classify_tool(
        "write_file", {"path": str(note), "content": "改", "overwrite": True}
    )
    assert decision.requires_approval and not decision.allowed
    assert "覆盖已有文件" in decision.reason

    # 硬约束二：删除文件必须人工审批
    removed = writable.classify_tool("delete_file", {"path": str(note)})
    assert removed.requires_approval and not removed.allowed

    # 受保护目标（当前用户主目录）即使批准也不执行
    with pytest.raises(SecurityError):
        writable.resolve_writable_path(str(Path.home()))

    # 默认档位可直接读取同一外部路径，但没有目录写授权时仍拒绝写入。
    strict = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})
    assert strict.resolve_path(str(note)) == note.resolve()
    with pytest.raises(SecurityError):
        strict.resolve_writable_path(str(note))


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
        # 2026-10-21：转后台之后的两条查询/结束工具（追加在末尾，前六个顺序不变）。
        "read_command",
        "stop_command",
        # 2026-10-22：一次调用跑完"写脚本 → 执行 → 清理"的入口。
        "execute_code",
    ]
    assert list(tools["list_dir"].args) == ["path", "recursive", "include_hidden", "max_entries"]
    assert list(tools["read_file"].args) == [
        "path", "start_line", "end_line", "max_chars", "start_char", "expected_sha256", "encoding",
    ]
    assert list(tools["edit_file"].args) == [
        "path",
        "old_text",
        "new_text",
        "patch",
        "replace_all",
        "expected_sha256",
        "reason",
        "encoding",
    ]
    assert list(tools["write_file"].args) == ["path", "content", "overwrite", "reason"]
    assert list(tools["delete_file"].args) == ["path", "recursive", "reason"]
    # 2026-10-14：新增 `escalate_sandbox`（运行中提权）——**追加在末尾**，前四个参数的顺序不变。
    assert list(tools["run_shell"].args) == [
        "command",
        "cwd",
        "timeout_seconds",
        "env_keys",
        "escalate_sandbox",
    ]
    assert list(tools["read_command"].args) == ["command_id", "cursor", "max_chars"]
    assert list(tools["stop_command"].args) == ["command_id"]
    assert list(tools["execute_code"].args) == ["code", "cwd", "timeout_seconds"]


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


def test_edit_file_accepts_missing_expected_sha256(tmp_path: Path, policy: SecurityPolicy) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")
    edit_file_content("a.py", policy=policy, old_text="x = 1", new_text="x = 2")
    assert target.read_text() == "x = 2\n"


def test_edit_file_rejects_stale_hash(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")

    with pytest.raises(FileOperationError) as excinfo:
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

    with pytest.raises(FileOperationError) as missing:
        edit_file_content(
            "a.py", policy=policy, old_text="gamma", new_text="x", expected_sha256=sha
        )
    # 文案已改为"没有匹配：已按 CRLF/LF 归一化重试仍找不到"（2026-10-22）。
    assert "没有匹配" in str(missing.value)

    with pytest.raises(FileOperationError) as ambiguous:
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


def test_edit_file_accepts_whole_file_replacement(tmp_path: Path, policy: SecurityPolicy) -> None:
    content = "x = 1\ny = 2\n"
    target = tmp_path / "a.py"
    target.write_text(content, encoding="utf-8", newline="")
    edit_file_content("a.py", policy=policy, old_text=content, new_text="z = 3\n")
    assert target.read_text() == "z = 3\n"


def test_edit_file_rejects_identical_text(tmp_path: Path, policy: SecurityPolicy) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")

    with pytest.raises(FileOperationError) as excinfo:
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
    with pytest.raises(FileOperationError):
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


def test_edit_file_counts_touched_files_not_lines(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """大范围编辑按**本轮改过的文件数**判，不按行数（2026-10-15）。

    这条同时钉住两个方向：① 单次改 500 行**不再**要审批（原来 200 行就拦，实证里
    "重写一个函数"被逐次弹卡）；② 额度用完后再改**新**文件才要审批。
    """
    policy = SecurityPolicy.from_config(
        _config_with_large_edit(app_config, max_files=2),
        workspace_root=tmp_path,
        environ={},
    )
    target = tmp_path / "a.py"
    target.write_text("line\n", encoding="utf-8", newline="")
    target_b = tmp_path / "b.py"
    target_b.write_text("line\n", encoding="utf-8", newline="")
    target_c = tmp_path / "c.py"
    target_c.write_text("line\n", encoding="utf-8", newline="")

    # 一次改 500 行：在免审额度内就照做，不再按行数拦。
    edit_file_content(
        "a.py",
        policy=policy,
        old_text="line",
        new_text="\n".join(f"l{index}" for index in range(500)),
        expected_sha256=_sha256(target),
    )
    assert policy.edited_file_count() == 1

    edit_file_content(
        "b.py", policy=policy, old_text="line", new_text="x", expected_sha256=_sha256(target_b)
    )
    # 用满额度（2 个）之后，第三个**新**文件需要一次人工批准。
    with pytest.raises(SecurityError) as excinfo:
        edit_file_content(
            "c.py", policy=policy, old_text="line", new_text="x", expected_sha256=_sha256(target_c)
        )
    assert "大范围修改" in str(excinfo.value)
    assert target_c.read_text(encoding="utf-8") == "line\n"  # 没批就不动文件

    # 同一轮内批准一次之后不再逐个问（`note_large_edit_approved` 的语义）。
    policy.note_large_edit_approved()
    edit_file_content(
        "c.py", policy=policy, old_text="line", new_text="x", expected_sha256=_sha256(target_c)
    )
    assert target_c.read_text(encoding="utf-8") == "x\n"


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
    with pytest.raises(FileOperationError) as excinfo:
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

    with pytest.raises(FileOperationError) as excinfo:
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

    with pytest.raises(FileOperationError) as excinfo:
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

    with pytest.raises(FileOperationError) as excinfo:
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
        with pytest.raises(FileOperationError) as excinfo:
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

    with pytest.raises(FileOperationError) as excinfo:
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

    with pytest.raises(FileOperationError) as excinfo:
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

    with pytest.raises(FileOperationError) as stale:
        edit_file_content("a.py", policy=policy, patch=patch, expected_sha256="0" * 64)
    assert "不一致" in str(stale.value)

    assert _sha256(target) == before
    edit_file_content("a.py", policy=policy, patch=patch)
    assert target.read_text() == "x = 9\n"


def test_edit_rejects_mixed_or_missing_input_modes(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """patch 与 old_text/new_text 互斥；两种都给或都没给都拒绝（架构 §6.3）。"""
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8", newline="")
    before = _sha256(target)

    with pytest.raises(FileOperationError) as mixed:
        edit_file_content(
            "a.py",
            policy=policy,
            old_text="x = 1",
            new_text="x = 9",
            patch=_patch_text("@@ -1 +1 @@", "-x = 1", "+x = 9"),
            expected_sha256=before,
        )
    assert "不能同时使用" in str(mixed.value)

    with pytest.raises(FileOperationError) as neither:
        edit_file_content("a.py", policy=policy, expected_sha256=before)
    assert "old_text 与 new_text" in str(neither.value)

    # 只有空白的 patch 视为"没有给 patch"，落到同一个缺参提示（不会当成空 Patch 执行）
    with pytest.raises(FileOperationError) as blank:
        edit_file_content("a.py", policy=policy, patch="   \n", expected_sha256=before)
    assert "old_text 与 new_text" in str(blank.value)

    assert _sha256(target) == before


def test_edit_patch_over_the_file_limit_requires_approval(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """patch 模式走同一条大范围审批（判据是本轮改过的文件数，不是补丁行数）。"""
    policy = SecurityPolicy.from_config(
        _config_with_large_edit(app_config, max_files=0),
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


def test_read_then_write_round_trip_keeps_crlf_bytes(tmp_path: Path, policy: SecurityPolicy) -> None:
    """读→写往返**保字节**：产品不会自作主张改换行（2026-10-09 澄清的一条事实）。

    来由：一次真实运行里模型把 CRLF 文件"原样复制"成 LF（289 → 283 字节），看起来像工具改了换行。
    查证后不是——`read_file_content` 用 ``data.decode("utf-8-sig")``（**不做换行翻译**），
    `write_file_content` 用 ``content.encode("utf-8")``（**原样落盘**）；差异出在**模型复述时**把 ``\\r\\n``
    写成了 ``\\n``，不是产品行为。这条把产品这一半钉住，免得日后有人"顺手"加一层换行归一化。
    """
    source = tmp_path / "crlf.txt"
    source.write_bytes("第一行\r\n第二行\r\n".encode())

    read = read_file_content("crlf.txt", policy=policy)
    assert "\r\n" in read.content  # 读进来时换行没有被翻译掉

    write_file_content("copy.txt", policy=policy, content=read.content, reason="往返保字节")

    assert (tmp_path / "copy.txt").read_bytes() == source.read_bytes()


def test_write_file_refuses_existing_file(tmp_path: Path, policy: SecurityPolicy) -> None:
    (tmp_path / "exists.txt").write_text("old", encoding="utf-8", newline="")

    with pytest.raises(FileOperationError) as excinfo:
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


def test_write_file_creates_missing_parent_directories(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """父目录缺失时**自动创建并如实报告**（2026-10-15）。

    原来这里直接拒绝，于是"产出一个文件"要绕成"先跑一条 `mkdir` 命令 → 人工审批 → 沙箱"，
    实证中一次普通任务就在这里原地失败。建目录不覆盖、不删除，主流 Agent 也是默认允许的。
    """
    result = write_file_content("site/assets/css/main.css", policy=policy, content="body{}")

    assert result.path == "site/assets/css/main.css"
    assert result.created_directories == ["site", "site/assets", "site/assets/css"]
    assert result.note is not None and "顺带新建了 3 个目录" in result.note
    # 由外到内创建，且内容真的落盘
    assert (tmp_path / "site" / "assets" / "css" / "main.css").read_text(encoding="utf-8") == "body{}"

    # 父目录已存在时不报"新建"（结果字段恒定，不制造第二种形状）
    again = write_file_content("site/second.txt", policy=policy, content="x")
    assert again.created_directories == [] and again.note is None


def test_write_file_refuses_bad_targets(tmp_path: Path, policy: SecurityPolicy) -> None:
    with pytest.raises(SecurityError):
        write_file_content(".env", policy=policy, content="A=1")

    with pytest.raises(WorkspaceBoundaryError):
        write_file_content("../x.txt", policy=policy, content="x")

    (tmp_path / "adir").mkdir()
    with pytest.raises(FileOperationError):
        write_file_content("adir", policy=policy, content="x")


def test_write_file_never_creates_protected_or_sensitive_parent_directories(
    tmp_path: Path, policy: SecurityPolicy
) -> None:
    """自动建目录**不是**放开受保护目标与密钥位置：中间层逐级判，判不过就整条拒绝。

    这是自动创建这条捷径唯一的风险面，所以单独钉住：`.git/`、`.ssh/` 这类中间目录一个都不许建。
    """
    root = tmp_path / ".git"
    root.mkdir()
    with pytest.raises(SecurityError):
        write_file_content(".git/objects/aa/bb.txt", policy=policy, content="x")
    # 判据是**副作用**：受保护目标的中间层一个都不该被建出来（错误文案由别的用例负责）。
    assert not (root / "objects").exists(), "受保护目录的中间层一个都不该被建出来"

    with pytest.raises(SecurityError):
        write_file_content("nested/.ssh/id_rsa", policy=policy, content="x")
    assert not (tmp_path / "nested").exists(), "判不过的整条链都不该留下半截目录"


def test_write_file_enforces_size_limit(tmp_path: Path, app_config: AppConfig) -> None:
    policy = SecurityPolicy.from_config(
        _config_with_workspace(app_config, max_file_size_bytes=4),
        workspace_root=tmp_path,
        environ={},
    )

    with pytest.raises(FileOperationError) as excinfo:
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
    # 展示路径（§6.6 / §15.8，v0.8 T73）：工作区内给**相对**路径，绝不把工作区绝对路径写给模型与页面。
    assert result.cwd == "."
    assert not Path(result.cwd).is_absolute()
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
    # v0.6 T60 起超时也按**进程树**终止（架构 §17.4）：进程被真正回收，因此退出码是**真实**值
    # （v0.5 的 `None` 表示"没等到它结束"，现在是"等到了，它是被终止的"），终止手段写进 note。
    assert result.exit_code is not None
    assert result.note is not None and "未结束" in result.note
    assert "taskkill" in result.note or "killpg" in result.note


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


def test_run_shell_does_not_ask_for_a_non_whitelisted_command(
    tmp_path: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不在白名单的命令**不再逐条问**（2026-10-15 权限模型重构）。

    旧模型里"不在白名单 ⇒ 需要审批"是主要摩擦源。白名单仍在 `prepare_shell` 里做分类
    （`test_security.py` 按层各自验），但不再影响放行。
    """
    policy = _shell_policy(tmp_path, app_config, whitelist=[_entry("--version")])
    # 固定解析结果：这条断言只关心"在不在白名单"，不该依赖启动方式（PATH）
    monkeypatch.setattr(shell_guard.shutil, "which", lambda name: str(tmp_path / name))

    # `prepare_shell` 层：可解析、但参数与白名单不一致 → 仍标"需要审批"，理由里带着白名单
    plan = shell_guard.prepare_shell(
        "python -V", config=policy.security.shell, workspace_root=tmp_path
    )
    assert plan.requires_approval and "白名单" in plan.reason

    # 判定层：不再因此弹审批（常规命令免审）。
    decision = policy.classify_tool("run_shell", {"command": "python -V"})
    assert decision.allowed and not decision.requires_approval


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
    _create_new_file(target, b"first")  # 直接验证原子创建语义

    with pytest.raises(FileOperationError):
        _create_new_file(target, b"second")

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

    with pytest.raises(FileOperationError) as excinfo:
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


def test_shell_syntax_is_not_an_approval_trigger_at_the_judgment_layer(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """含 Shell 语法的命令**不再自动升级为审批**（2026-10-15 权限模型重构）。

    危险的是"命令做了什么"（破坏性 / 提权 / 工作区外 cwd），不是"有没有管道"；这三类各有专门分支。
    `prepare_shell` 层照旧标它需要审批（未命中白名单），判定层不再因此弹卡。
    """
    policy = _shell_policy(tmp_path, app_config, whitelist=[])
    command = "echo hi | findstr hi" if os.name == "nt" else "echo hi | grep hi"

    plan = shell_guard.prepare_shell(
        command, config=policy.security.shell, workspace_root=tmp_path
    )
    assert plan.requires_approval
    assert plan.shell_syntax is not None, "确实是 Shell 语法那条分支"

    decision = policy.classify_tool("run_shell", {"command": command})
    assert decision.allowed and not decision.requires_approval


def test_platform_builtin_is_not_refused_as_missing_executable(tmp_path: Path) -> None:
    """平台内建命令（`time` / `date` / `dir`…）**不是"环境不可用"**：改由适配器执行。

    2026-10-14 实测修正：它们没有独立可执行文件，argv 模式下 `resolve_executable` 必然解析不到，
    以前因此被硬拒、**连审批入口都不给**（用户视角就是"查个时间被拦两次"）。
    """
    if os.name != "nt":
        pytest.skip("内建命令表按平台区分，这条只验 Windows 的 cmd 内建")

    from local_agent.config import ShellSecurityConfig
    from local_agent.security.shell_guard import build_shell_invocation, prepare_shell

    # 只读安全清单里的内建（`time /t`）：2026-10-14 起**直接免审**，但仍是"经适配器执行"，
    # 不是"找不到可执行文件"。
    safe = prepare_shell(
        "time /t", config=ShellSecurityConfig(), workspace_root=tmp_path, platform="windows"
    )
    assert safe.safe_read_only is True and safe.allowed is True
    assert safe.environment_unavailable is False
    assert safe.executor != "argv", "内建必须经适配器执行"

    # 不在清单里的内建（`dir`）：口径**没有放宽**，仍然要审批。
    plan = prepare_shell(
        "dir", config=ShellSecurityConfig(), workspace_root=tmp_path, platform="windows"
    )
    assert plan.requires_approval is True and plan.allowed is False
    assert plan.environment_unavailable is False, "内建 ≠ 环境不可用"
    assert plan.executor == "cmd"
    assert plan.executable_path is not None, "适配器自身必须解析得到（cmd.exe）"
    invocation = build_shell_invocation(plan)
    assert invocation[:4] == [plan.executable_path, "/d", "/s", "/c"]
    assert invocation[4] == "dir", "原始命令必须原样交给适配器"


def _tier_policy(tmp_path: Path, mode: str, grants: dict[str, str] | None = None):
    """按档位构造策略（与设置页的三档同名：``read-only`` / ``workspace-write`` / ``danger-full-access``）。"""
    from local_agent.config import SecurityConfig, WorkspaceConfig
    from local_agent.security.path_guard import ExternalPathPolicy
    from local_agent.security.policy import SecurityPolicy

    return SecurityPolicy(
        workspace_root=tmp_path,
        security=SecurityConfig(),
        workspace=WorkspaceConfig(),
        external_policy=ExternalPathPolicy(mode=mode, grants=grants or {}),  # type: ignore[arg-type]
    )


def test_danger_tier_skips_shell_approval_for_ordinary_commands(tmp_path: Path) -> None:
    """「完全放行」档 = 常规命令**免审批**：这正是"选了最宽的一档还什么都要问"的修复（2026-10-14）。

    档位的设计意图本来就写着"决定常规小改动是否需要逐次询问"，但在此之前 Shell 判定完全没读过
    档位——于是这一档下 `echo hi > note.txt` 也要点一次批准。
    """
    decision = _tier_policy(tmp_path, "danger-full-access").classify_tool(
        "run_shell", {"command": "echo hi > note.txt"}
    )

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_danger_tier_ordinary_command_reaches_execution(tmp_path: Path) -> None:
    policy = _tier_policy(tmp_path, "danger-full-access")
    result = run_shell_command("echo REVIEW_OK > review.txt", policy=policy)
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "review.txt").read_text(encoding="utf-8").strip() == "REVIEW_OK"


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_safe_command_escalation_requires_approval(tmp_path: Path, mode: str) -> None:
    """提权仍要人工批准——**只在沙箱真的生效的两档**（2026-10-15 起沙箱由档位派生）。

    「完全放行」档不套沙箱，带 `escalate_sandbox` 是**直接拒绝**（没有边界就没有"退出边界"），
    那条由 `test_escalation_is_refused_when_no_sandbox_is_active` 守。
    """
    policy = _tier_policy(tmp_path, mode)
    decision = policy.classify_tool("run_shell", {
        "command": "echo REVIEW_OK", "escalate_sandbox": True,
    })
    assert not decision.allowed
    assert decision.requires_approval
    with pytest.raises(SecurityError, match="人工审批"):
        run_shell_command("echo REVIEW_OK", policy=policy, escalate_sandbox=True)


@pytest.mark.skipif(sys.platform != "win32", reason="cmd 单引号规则")
def test_echo_single_quotes_cannot_hide_cmd_operators(tmp_path: Path) -> None:
    """`echo` 的"只读安全"身份不能被单引号夹带的写操作偷走（cmd 不认单引号）。

    2026-10-15 起这条的边界由**只读档的沙箱**兜：命令不再被硬拒绝（常规命令一律免审），
    但它写不进工作区——判据从"抛 SecurityError"换成"副作用没有发生"。
    """
    command = "echo 'x & echo REVIEW > readonly-bypass.txt & rem '"
    policy = _tier_policy(tmp_path, "read-only")
    plan = shell_guard.prepare_shell(command, config=policy.security.shell, workspace_root=tmp_path)
    assert not plan.safe_read_only
    assert plan.requires_approval

    # 判据只看**副作用有没有发生**：cmd 对失败的写重定向仍可能回 0，退出码在这里不是好判据。
    run_shell_command(command, policy=policy)
    assert not (tmp_path / "readonly-bypass.txt").exists()


@pytest.mark.parametrize("executor,command", [
    ("pwsh", 'echo "$(New-Item marker.txt)"'),
    ("sh", 'pwd "$(touch marker.txt)"'),
])
def test_double_quoted_expansion_never_gets_read_only_immunity(
    tmp_path: Path, monkeypatch, executor: str, command: str,
) -> None:
    from local_agent.config import ShellSecurityConfig

    monkeypatch.setattr(shell_guard, "resolve_executable", lambda *_args, **_kwargs: "/adapter")
    plan = shell_guard.prepare_shell(
        command, config=ShellSecurityConfig(executor=executor), workspace_root=tmp_path,
        platform="windows" if executor == "pwsh" else "posix",
    )
    assert not plan.safe_read_only
    assert plan.requires_approval


def test_missing_shell_adapter_is_an_environment_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shell_guard, "resolve_executable", lambda *_args, **_kwargs: None)
    decision = _tier_policy(tmp_path, "workspace-write").classify_tool(
        "run_shell", {"command": "echo hi > marker.txt"},
    )
    assert not decision.allowed
    assert not decision.requires_approval
    assert decision.reason_kind == "environment"


def test_danger_tier_opens_external_reads_but_never_external_writes(tmp_path: Path) -> None:
    """「完全放行」档**只放开读**：写仍然只由逐条授权决定（2026-10-14 收窄，对齐 codex / dsh）。

    两家在"放开"这件事上是同一句：**读不受限、写由可写根围栏**（codex 的 `workspace-write`、
    dsh 的"每个模式都允许读，写由 `writableRoots` 管"）。收窄前我们那一档是"外部一律读写"，比两家都宽。
    现在的四格：

    - 外部目标**可读**（不必逐目录授权）——这才是这一档真正放开的；
    - 外部**写**（新建与覆盖都算）→ **硬拒绝**（不是"等批准"），理由指向"去授权那个目录"；
    - 授权为**读写**之后写得进去（授权压过档位）；
    - **受保护目标**任何时候都不给写，连审批入口都没有。
    """
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    target = outside / "note.txt"
    target.write_text("existing", encoding="utf-8")
    policy = _tier_policy(tmp_path, "danger-full-access")

    assert policy.resolve_target(str(target)).access == "read", "放行档的读是无条件的"

    for args in (
        {"path": str(target), "content": "hi"},
        {"path": str(target), "content": "hi", "overwrite": True},
    ):
        denied = policy.classify_tool("write_file", args)
        assert denied.allowed is False, args
        assert denied.requires_approval is False, "写外部不是'等批准'，是要先去授权"
        assert "只允许读" in denied.reason

    from local_agent.security.path_guard import normalize_grant_path

    granted = _tier_policy(
        tmp_path, "danger-full-access", {normalize_grant_path(str(outside)): "read_write"}
    )
    newly = granted.classify_tool(
        "write_file", {"path": str(outside / "new.txt"), "content": "hi"}
    )
    assert newly.allowed is True, "逐条授权为读写之后，放行档也写得进去"

    protected = policy.classify_tool(
        "write_file", {"path": str(Path.home() / ".git" / "config"), "content": "x"}
    )
    assert protected.allowed is False
    assert protected.requires_approval is False, "受保护目标连审批入口都不给"


def test_escalation_asks_in_every_tier_even_for_whitelisted_commands(tmp_path: Path) -> None:
    """运行中提权（escalation）：不管哪一档、命令本身是否在白名单里，都要人工批准。

    用**白名单命令**做判据是关键：它在三档下本来都免审，带上 `escalate_sandbox` 之后必须转为
    审批——这才证明拦下来的是"提权"这件事，而不是那条命令本身的风险。
    """
    from local_agent.config import (
        SecurityConfig,
        ShellSecurityConfig,
        ShellWhitelistEntry,
        WorkspaceConfig,
    )
    from local_agent.security.path_guard import ExternalPathPolicy
    from local_agent.security.policy import SecurityPolicy

    def policy_for(mode: str):
        return SecurityPolicy(
            workspace_root=tmp_path,
            security=SecurityConfig(
                shell=ShellSecurityConfig(
                    whitelist=[
                        ShellWhitelistEntry(
                            platform=["windows", "posix"], executable="python", args=["--version"]
                        )
                    ],
                )
            ),
            workspace=WorkspaceConfig(),
            external_policy=ExternalPathPolicy(mode=mode),  # type: ignore[arg-type]
        )

    # 沙箱生效的两档：提权一律要人工批准。
    for mode in ("read-only", "workspace-write"):
        # 对照：不带提权的白名单命令本来就免审（证明这条命令本身不是被拦的原因）
        plain = policy_for(mode).classify_tool("run_shell", {"command": "python --version"})
        assert plain.allowed is True and plain.requires_approval is False, mode

        decision = policy_for(mode).classify_tool(
            "run_shell", {"command": "python --version", "escalate_sandbox": True}
        )
        assert decision.allowed is False, mode
        assert decision.requires_approval is True, mode
        assert "退出写侧沙箱" in decision.reason, mode


def test_safe_read_only_commands_skip_approval_in_every_tier(tmp_path: Path) -> None:
    """`echo` 这类只读安全命令**三档都免审**——连「只读」档也不拦。

    它是代码层清单（不是用户白名单）：用户没配任何白名单时，"看一眼就走"的读命令也不该被拦。
    """
    for mode in ("read-only", "workspace-write", "danger-full-access"):
        decision = _tier_policy(tmp_path, mode).classify_tool("run_shell", {"command": "echo hi"})
        assert decision.allowed is True, mode
        assert decision.requires_approval is False, mode


def test_escalation_is_refused_when_no_sandbox_is_active(tmp_path: Path) -> None:
    """沙箱没开时带 `escalate_sandbox` 被**直接拒绝**，不静默忽略。

    没有边界就没有"退出边界"这回事；静默忽略会让模型以为自己拿到了更宽的执行环境。
    「完全放行」档正是"不套沙箱"的那一档。
    """
    decision = _tier_policy(tmp_path, "danger-full-access").classify_tool(
        "run_shell", {"command": "python --version", "escalate_sandbox": True}
    )

    assert decision.allowed is False
    assert decision.requires_approval is True
    assert "退出写侧沙箱" in decision.reason


def test_destructive_commands_still_ask_in_every_tier(tmp_path: Path) -> None:
    """破坏性命令**任何档位**都要人工批准——包括「放行」（四项硬约束，靠代码层底线保证）。

    底线必须由代码给：``security.shell.blocked_patterns`` 出厂是空列表，没有底线的话
    "破坏性命令要问"在默认配置下等于没落地，放行档一开 `format C:` 就会直接执行。
    """
    from local_agent.config import ShellSecurityConfig
    from local_agent.security.shell_guard import prepare_shell

    assert ShellSecurityConfig().blocked_patterns == [], "出厂配置确实一个模式都没配"

    plan = prepare_shell(
        "rmdir build", config=ShellSecurityConfig(), workspace_root=tmp_path, platform="windows"
    )
    assert plan.destructive is True, "底线要认出删除类命令（`rmdir`）"

    for mode in ("read-only", "workspace-write", "danger-full-access"):
        decision = _tier_policy(tmp_path, mode).classify_tool(
            "run_shell", {"command": "rmdir build"}
        )
        assert decision.allowed is False, mode
        assert decision.requires_approval is True, mode


def test_no_tier_asks_just_because_the_whitelist_missed(tmp_path: Path) -> None:
    """三档都**不再**因为"命令不在白名单里"而逐条问（2026-10-15 权限模型重构）。

    旧模型里「手动审批」档连白名单命中也问、其余档"不在白名单就问"；现在"问不问"只由
    提权 / 破坏性 / 工作区外 cwd 三件事决定，档位只回答"能写到哪"。白名单命中与否仍会被
    `prepare_shell` 分类（那条在 `test_security.py` 里按层各自验），只是不再影响审批。
    """
    from local_agent.config import (
        SecurityConfig,
        ShellSecurityConfig,
        ShellWhitelistEntry,
        WorkspaceConfig,
    )
    from local_agent.security.path_guard import ExternalPathPolicy
    from local_agent.security.policy import SecurityPolicy

    def policy_for(mode: str):
        return SecurityPolicy(
            workspace_root=tmp_path,
            security=SecurityConfig(
                shell=ShellSecurityConfig(
                    whitelist=[ShellWhitelistEntry(executable="python", args=["--version"])]
                )
            ),
            workspace=WorkspaceConfig(),
            external_policy=ExternalPathPolicy(mode=mode),  # type: ignore[arg-type]
        )

    for mode in ("read-only", "workspace-write", "danger-full-access"):
        # 命中白名单的那条
        hit = policy_for(mode).classify_tool("run_shell", {"command": "python --version"})
        assert hit.allowed is True and hit.requires_approval is False, mode
        # 没命中白名单的那条：结果一样——白名单不再是审批开关。
        miss = policy_for(mode).classify_tool("run_shell", {"command": "python --version --help"})
        assert miss.allowed is True and miss.requires_approval is False, mode


def test_environment_unavailable_is_not_a_policy_refusal(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """判定层把"**环境不可用**"与"策略拒绝"分开：前者不该被当成策略拦截（2026-10-14）。

    两者的建议完全不同：环境不可用 → 换一条等价命令；策略拒绝 → 改请求或走人工审批。
    """
    if os.name != "nt":
        pytest.skip("这条用 cmd 内建做对照，只在 Windows 上有意义")

    from local_agent.security.policy import SecurityPolicy

    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    missing = policy.classify_tool("run_shell", {"command": "definitely-not-a-real-tool-xyz"})
    assert missing.allowed is False and missing.requires_approval is False
    assert missing.reason_kind == "environment", "环境里没有这个命令 ≠ 策略拦截"

    builtin = policy.classify_tool("run_shell", {"command": "dir"})
    assert builtin.reason_kind == "policy", "内建命令能跑，只是不在白名单里 → 仍走策略口径"
    # 2026-10-15：这一档下"不在白名单"不再触发审批，所以这条命令直接免审执行。
    assert builtin.allowed is True and builtin.requires_approval is False

    # 只读安全清单里的内建是例外：2026-10-14 起直接免审（`time /t` 正是用户问时间走的命令）。
    readonly = policy.classify_tool("run_shell", {"command": "time /t"})
    assert readonly.allowed is True and readonly.requires_approval is False


def test_unknown_external_command_is_still_refused_without_approval(tmp_path: Path) -> None:
    """真正解析不到的外部命令仍是"环境不可用"：如实拒绝、**不给审批入口**（批准了也起不来）。"""
    from local_agent.config import ShellSecurityConfig
    from local_agent.security.shell_guard import prepare_shell

    plan = prepare_shell(
        "definitely-not-a-real-tool-xyz --version",
        config=ShellSecurityConfig(),
        workspace_root=tmp_path,
        platform="windows",
    )

    assert plan.allowed is False and plan.requires_approval is False
    assert "找不到可执行文件" in plan.reason
    assert plan.executor == "argv"


def test_whitelisted_builtin_actually_runs_through_the_adapter(tmp_path: Path) -> None:
    """内建命令能进白名单免审，而且**真的跑得起来**。

    这条也是 `del` / `rd` 那类"既命中破坏性模式、又永远执行不了"的命令的回归：以前批准了也起不来。
    """
    if os.name != "nt":
        pytest.skip("内建命令表按平台区分，这条只验 Windows 的 cmd 内建")

    from local_agent.config import (
        SecurityConfig,
        ShellSecurityConfig,
        ShellWhitelistEntry,
        WorkspaceConfig,
    )
    from local_agent.security.policy import SecurityPolicy

    policy = SecurityPolicy(
        workspace_root=tmp_path,
        security=SecurityConfig(
            shell=ShellSecurityConfig(
                whitelist=[ShellWhitelistEntry(executable="time", args=["/t"])]
            )
        ),
        workspace=WorkspaceConfig(),
    )

    result = run_shell_command("time /t", policy=policy)

    assert result.exit_code == 0, result.stderr
    assert result.argv[1:4] == ["/d", "/s", "/c"], "经 cmd 适配器执行"
    assert result.stdout.strip(), "内建命令的执行结果必须真的返回（以前根本起不来）"


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
    """批准是一次性的：撤销后同一条命令重新被拒（架构 §6.5 的“恰好执行一次”）。

    2026-10-15 起常规命令免审，所以判据换成**破坏性命令**——那类任何档位都要人工批准，
    "批准一次"这件事仍然必须是一次性的。
    """
    policy = _shell_policy(tmp_path, app_config, whitelist=[])
    command = "rmdir build" if os.name == "nt" else "rm -rf build"
    fingerprint = _fingerprint(policy, command)

    policy.grant(fingerprint)
    policy.revoke(fingerprint)

    with pytest.raises(SecurityError):
        run_shell_command(command, policy=policy)


def test_run_shell_absolute_path_runs_the_resolved_path(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """带路径的命令按**解析出的 argv** 执行：执行对象就是判定对象（架构 §5.3、§6.6）。

    2026-10-15 起"带路径"不再单独触发人工批准（常规命令一律免审），所以这条只守
    "跑起来的确实是解析出的那个绝对路径"，不再守审批。
    """
    policy = _shell_policy(tmp_path, app_config, whitelist=[])
    command = f'"{sys.executable}" --version'

    result = run_shell_command(command, policy=policy)

    assert result.exit_code == 0
    assert "Python" in result.stdout
    assert result.argv == [str(Path(sys.executable).resolve()), "--version"]
    assert result.executor == "argv"


def test_run_shell_approval_is_bound_to_the_command(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """批准绑在动作指纹上：换一条命令不能复用同一份批准。

    判据用两条**破坏性命令**（都要人工批准）：批准了删 `build` 不等于能删 `dist`。
    """
    policy = _shell_policy(tmp_path, app_config, whitelist=[])
    approved = "rmdir build" if os.name == "nt" else "rm -rf build"
    other = "rmdir dist" if os.name == "nt" else "rm -rf dist"

    policy.grant(_fingerprint(policy, approved))
    try:
        with pytest.raises(SecurityError):
            run_shell_command(other, policy=policy)
    finally:
        policy.revoke(_fingerprint(policy, approved))


def test_read_file_dedupes_the_same_range_within_one_turn(
    tmp_path: Path, policy: SecurityPolicy,
) -> None:
    """同一轮里重复读同一段、文件未变：不再回传原文（2026-10-22，对齐 hermes 的 read 去重）。"""
    (tmp_path / "a.txt").write_text("第一行\n第二行\n", encoding="utf-8")

    first = file_tools.read_file_content("a.txt", policy=policy)
    # 不断言换行：Windows 上 `write_text` 会把 \n 落成 \r\n，这里只关心"读到没读到"。
    assert "第一行" in first.content and "第二行" in first.content
    assert "已经读过" not in (first.note or "")

    second = file_tools.read_file_content("a.txt", policy=policy)
    assert second.content == ""
    assert "已经读过" in (second.note or "")
    # 行区间/总行数照旧给出，模型据此决定下一步，而不是重读。
    assert second.line_count == first.line_count
    assert second.sha256 == first.sha256

    # 换参数不命中：模型要原文时永远有出口。
    third = file_tools.read_file_content("a.txt", policy=policy, start_line=2)
    assert "第二行" in third.content


def test_read_file_dedupe_is_invalidated_by_a_content_change(
    tmp_path: Path, policy: SecurityPolicy,
) -> None:
    """文件一变 sha 就不同：去重不能把新内容也挡掉。"""
    target = tmp_path / "b.txt"
    target.write_text("v1\n", encoding="utf-8")
    assert "v1" in file_tools.read_file_content("b.txt", policy=policy).content

    target.write_text("v2\n", encoding="utf-8")
    again = file_tools.read_file_content("b.txt", policy=policy)
    assert "v2" in again.content


def test_tool_result_budget_accumulates_and_flags_only_the_overflow(
    policy: SecurityPolicy,
) -> None:
    """轮级结果预算（2026-10-22）：只记账、只置标记，**不截断**。"""
    assert policy.result_budget_used == 0
    assert policy.result_budget_exceeded is False

    policy.note_tool_result(120_000)
    assert policy.result_budget_used == 120_000
    assert policy.result_budget_exceeded is False

    policy.note_tool_result(90_000)
    assert policy.result_budget_used == 210_000
    assert policy.result_budget_exceeded is True

    # 空结果不占预算。
    policy.note_tool_result(0)
    assert policy.result_budget_used == 210_000


def test_edit_file_tolerates_crlf_and_keeps_the_original_line_endings(
    tmp_path: Path, policy: SecurityPolicy,
) -> None:
    """文件是 CRLF、模型用 \\n 拼 old_text：归一化后要能改中，且不能把整个文件改成 LF。"""
    target = tmp_path / "c.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    result = file_tools.edit_file_content(
        "c.txt", policy=policy, old_text="alpha\nbeta", new_text="alpha\nBETA"
    )

    data = target.read_bytes()
    assert b"BETA" in data
    assert data.count(b"\r\n") == 3, data          # 换行风格没被改掉
    assert b"\r\n" in data and data.replace(b"\r\n", b"").count(b"\n") == 0
    assert "已按 CRLF/LF" not in (result.note or "")  # 归一化成功不算失败


def test_edit_file_escalates_after_three_misses(
    tmp_path: Path, policy: SecurityPolicy,
) -> None:
    """同一文件连续失败到第 3 次：错误信息要明确“不要换写法重试”（2026-10-22）。"""
    (tmp_path / "d.txt").write_text("hello\n", encoding="utf-8")
    messages: list[str] = []
    for _ in range(3):
        with pytest.raises(FileOperationError) as exc:
            file_tools.edit_file_content("d.txt", policy=policy, old_text="nope", new_text="x")
        messages.append(str(exc.value))

    assert "不要再换一种写法重试" not in messages[0]
    assert "不要再换一种写法重试" not in messages[1]
    assert "不要再换一种写法重试" in messages[2]


def test_offload_tool_result_spills_the_longest_field_only(
    tmp_path: Path, policy: SecurityPolicy, app_config: AppConfig,
) -> None:
    """超预算的正文落盘，换成"预览 + locator"；小结果与非可写策略原样返回（2026-10-22）。"""
    body = "行" * 5000  # 5000 字符，超过 4000 的落盘门槛
    content = json.dumps(
        {"path": "a.txt", "content": body, "sha256": "abc"}, ensure_ascii=False
    )

    out = offload_tool_result(content, policy=policy, tool_name="read_file")
    assert isinstance(out, str)
    payload = json.loads(out)
    assert payload["spilled"] is True
    assert payload["path"] == "a.txt"           # 其它字段原样保留
    assert "完整内容已保存到" in payload["content"]
    assert len(payload["content"]) < len(body)  # 正文确实变短了

    spill = tmp_path / payload["spill_path"]
    assert spill.is_file()
    assert body in spill.read_text(encoding="utf-8", errors="replace")

    # 小结果不落盘：4000 字符以下原样返回。
    small = json.dumps({"content": "短内容"}, ensure_ascii=False)
    assert offload_tool_result(small, policy=policy, tool_name="read_file") == small

    # 只读子策略不能写盘：原样返回，不假装保存。
    strict = SecurityPolicy.from_config(
        app_config, workspace_root=tmp_path, environ={}, allow_result_spill=False
    )
    assert offload_tool_result(content, policy=strict, tool_name="read_file") == content


def test_search_files_covers_both_modes(tmp_path: Path, policy: SecurityPolicy) -> None:
    """合并入口（2026-10-22）：同一个工具既能搜内容，也能按名字找。"""
    (tmp_path / "a.py").write_text("hit\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hit\n", encoding="utf-8")
    tools = {tool.name: tool for tool in search_tools.build_search_tools(policy)}

    content = json.loads(
        tools["search_files"].invoke({"pattern": "hit", "glob": "*.py"})
    )
    assert content["mode"] == "content"
    assert [hit["path"] for hit in content["hits"]] == ["a.py"]

    files = json.loads(
        tools["search_files"].invoke({"pattern": "*.txt", "mode": "files"})
    )
    assert files["mode"] == "files"
    assert files["files"] == ["b.txt"]

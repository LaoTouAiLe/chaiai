"""Git 工具的四条边界（v0.5 T54，架构 §6.11 / §16.4）。

全部用**假 runner**：不依赖机器上装没装 git，也不碰真实仓库——真实 git 只做一次人工验收。
覆盖的是契约里写死的东西：作用域是工作区、参数闭集（任何路径都不出现 push）、缺 git 时明确失败、
提交必须经人工审批且默认分支上有额外确认，以及失败/超时/截断都如实汇报。
"""

from __future__ import annotations

import locale
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from local_agent.config import AppConfig
from local_agent.exceptions import SecurityError
from local_agent.security.policy import SecurityPolicy
from local_agent.tools import git_tools


@pytest.mark.parametrize("path", [":(top)outside.txt", ":/outside.txt", ":(exclude)ws", "./:(top)outside.txt"])
def test_git_pathspec_magic_is_rejected_before_any_read(
    tmp_path: Path, app_config: AppConfig, path: str
) -> None:
    fake = FakeGit()
    with pytest.raises(SecurityError, match="pathspec"):
        git_tools.diff(_policy(tmp_path, app_config), path=path, runner=fake)
    assert fake.calls == []


def test_real_git_runner_uses_literal_pathspecs(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("本机未安装 Git")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    for arguments in [["init", "--quiet"], ["add", "--", "ws/inside.txt", "outside.txt"]]:
        subprocess.run(["git", "-C", str(tmp_path), *arguments], check=True, capture_output=True)
    escaped = git_tools.run_git(["diff", "--cached", "--", ":(top)outside.txt"], workspace, 10)
    assert escaped.exit_code == 0 and escaped.stdout == ""
    ordinary = git_tools.run_git(["diff", "--cached", "--", "inside.txt"], workspace, 10)
    assert ordinary.exit_code == 0 and "inside.txt" in ordinary.stdout


def test_git_commit_magic_is_rejected_before_any_write(tmp_path: Path, app_config: AppConfig) -> None:
    fake = FakeGit()
    with pytest.raises(SecurityError, match="pathspec"):
        git_tools.commit(_policy(tmp_path, app_config), paths=[":(top)outside.txt"],
                         message="不得越界提交", confirm_on_default_branch=True, runner=fake)
    assert not fake.saw("add") and not fake.saw("commit")


def _policy(tmp_path: Path, app_config: AppConfig) -> SecurityPolicy:
    return SecurityPolicy(
        workspace_root=tmp_path, security=app_config.security, workspace=app_config.workspace
    )


class FakeGit:
    """按 argv 关键字匹配返回脚本化结果，并记录收到的每一次调用。"""

    def __init__(self, replies: Sequence[tuple[tuple[str, ...], git_tools.GitRun]] = ()) -> None:
        self.replies = list(replies)
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], cwd: Path, timeout: float) -> git_tools.GitRun:
        argv = list(args)
        self.calls.append(argv)
        for needles, run in self.replies:
            if all(needle in argv for needle in needles):
                return run
        return git_tools.GitRun(exit_code=0, stdout="", stderr="")

    def saw(self, *needles: str) -> bool:
        """是否发生过一次同时含这些片段的调用。"""
        return any(all(needle in argv for needle in needles) for argv in self.calls)

    def flat(self) -> list[str]:
        return [token for argv in self.calls for token in argv]


def _ok(stdout: str = "") -> git_tools.GitRun:
    return git_tools.GitRun(exit_code=0, stdout=stdout, stderr="")


def _fail(stderr: str, code: int = 128) -> git_tools.GitRun:
    return git_tools.GitRun(exit_code=code, stdout="", stderr=stderr)


# ---- 可用性与装配 ----------------------------------------------------------


def test_missing_git_explains_what_to_install() -> None:
    availability = git_tools.git_availability(which=lambda _name: None)

    assert availability.available is False
    hint = availability.hint or ""
    assert "git-scm.com" in hint  # 可照做的安装提示
    # 这段提示会出现在页面的能力状态区块，那里**只讲能力、不点工具名**（§16.6 的字段门槛）。
    assert "Git 相关能力不可用" in hint
    assert "git_status" not in hint and "git_commit" not in hint


def test_git_available_when_which_finds_it() -> None:
    availability = git_tools.git_availability(which=lambda name: f"/usr/bin/{name}")

    assert availability.available is True
    assert availability.hint is None


def test_tools_are_always_built_even_without_git(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """工具总是构造（能力覆盖校验要求登记的工具都在注册表里）；"能不能用"由配置与能力决定。"""
    tools = git_tools.build_git_tools(_policy(tmp_path, app_config), runner=FakeGit())

    assert [tool.name for tool in tools] == [
        "git_status",
        "git_diff",
        "git_log",
        "git_commit",
    ]


# ---- 只读三件套 ------------------------------------------------------------


def test_status_reports_scope_when_workspace_sits_inside_a_bigger_repo(
    tmp_path: Path, app_config: AppConfig
) -> None:
    fake = FakeGit(
        [
            (("rev-parse", "--show-toplevel"), _ok(f"{tmp_path.parent.as_posix()}\n")),
            (("rev-parse", "--abbrev-ref"), _ok("feat/git\n")),
            (
                ("status", "--porcelain=v1"),
                _ok(" M src/app.py\0?? new.txt\0R  new.md\0old.md\0"),
            ),
        ]
    )

    result = git_tools.status(_policy(tmp_path, app_config), runner=fake)

    assert result.repo_root == tmp_path.parent.as_posix()
    assert result.repo_root_is_ancestor is True
    assert result.branch == "feat/git"
    assert result.clean is False
    assert [(entry.code, entry.path, entry.original_path) for entry in result.entries] == [
        (" M", "src/app.py", None),
        ("??", "new.txt", None),
        ("R ", "new.md", "old.md"),
    ]
    # 必须说明"只覆盖工作区"，不能让人以为看到了整个仓库的改动
    assert "只覆盖工作区" in (result.note or "")
    # 作用域限定：`-- .`（cwd 就是工作区）
    assert fake.saw("status", "--", ".")


def test_status_on_a_clean_workspace_inside_the_repo_root(
    tmp_path: Path, app_config: AppConfig
) -> None:
    fake = FakeGit(
        [
            (("rev-parse", "--show-toplevel"), _ok(f"{tmp_path.as_posix()}\n")),
            (("rev-parse", "--abbrev-ref"), _ok("master\n")),
            (("status", "--porcelain=v1"), _ok("")),
        ]
    )

    result = git_tools.status(_policy(tmp_path, app_config), runner=fake)

    assert result.clean is True and result.entries == []
    assert result.repo_root_is_ancestor is False
    assert result.note is None


def test_not_a_repository_says_what_to_do(tmp_path: Path, app_config: AppConfig) -> None:
    fake = FakeGit(
        [(("rev-parse", "--show-toplevel"), _fail("fatal: not a git repository\n"))]
    )

    with pytest.raises(git_tools.GitError) as excinfo:
        git_tools.status(_policy(tmp_path, app_config), runner=fake)

    assert "不是 Git 仓库" in str(excinfo.value)
    assert "git init" in str(excinfo.value)  # 可照做


def test_diff_distinguishes_empty_from_truncated(tmp_path: Path, app_config: AppConfig) -> None:
    policy = _policy(tmp_path, app_config)
    repo = [(("rev-parse", "--show-toplevel"), _ok(f"{tmp_path.as_posix()}\n"))]

    empty = git_tools.diff(policy, runner=FakeGit([*repo, (("diff",), _ok(""))]))
    assert empty.empty is True
    assert "没有差异" in (empty.note or "")

    long_diff = "+" + "x" * 200
    clipped = git_tools.diff(
        policy, runner=FakeGit([*repo, (("diff",), _ok(long_diff))]), max_output_chars=20
    )
    assert clipped.empty is False
    assert clipped.truncated is True
    assert len(clipped.text) == 20
    # 触达上限必须说明"结果不完整"，不得返回"不完整但没有原因"
    assert "不完整" in (clipped.note or "")


def test_diff_staged_uses_the_cached_flag(tmp_path: Path, app_config: AppConfig) -> None:
    fake = FakeGit(
        [
            (("rev-parse", "--show-toplevel"), _ok(f"{tmp_path.as_posix()}\n")),
            (("diff",), _ok("--- a\n+++ b\n")),
        ]
    )

    result = git_tools.diff(_policy(tmp_path, app_config), staged=True, runner=fake)

    assert result.staged is True
    assert fake.saw("diff", "--cached", "--", ".")


def test_log_parses_entries_and_validates_limit(tmp_path: Path, app_config: AppConfig) -> None:
    policy = _policy(tmp_path, app_config)
    payload = "abc123\x1fabc12\x1f张三\x1f2026-10-09\x1f补 Git 工具\n\x1e"
    fake = FakeGit(
        [
            (("rev-parse", "--show-toplevel"), _ok(f"{tmp_path.as_posix()}\n")),
            (("log",), _ok(payload)),
        ]
    )

    result = git_tools.log(policy, limit=5, runner=fake)

    assert [(entry.short, entry.author, entry.subject) for entry in result.entries] == [
        ("abc12", "张三", "补 Git 工具")
    ]
    assert fake.saw("log", "-n5")

    with pytest.raises(git_tools.GitError) as excinfo:
        git_tools.log(policy, limit=0, runner=FakeGit())
    assert "1~100" in str(excinfo.value)


# ---- 路径边界：越界与 .git 在**任何** git 调用之前就被拒 --------------------


@pytest.mark.parametrize("path", ["../outside.txt", ".git/config"])
def test_paths_outside_scope_are_rejected_before_any_git_call(
    path: str, tmp_path: Path, app_config: AppConfig
) -> None:
    fake = FakeGit()

    with pytest.raises(SecurityError):
        git_tools.diff(_policy(tmp_path, app_config), path=path, runner=fake)

    assert fake.calls == []


# ---- 受控提交 --------------------------------------------------------------


def test_commit_on_default_branch_requires_no_confirmation(
    tmp_path: Path, app_config: AppConfig
) -> None:
    fake = FakeGit([(("rev-parse", "--abbrev-ref"), _ok("main\n"))])

    result = git_tools.commit(
        _policy(tmp_path, app_config), paths=["notes.md"], message="补一段说明", runner=fake
    )

    assert result.requires_confirmation is False
    assert result.committed is False and result.commit is None
    assert result.branch == "main"
    # 关键：停在确认上时**一个 git 写操作都没发生**
    assert fake.saw("add")
    assert not fake.saw("commit")


def test_commit_on_a_feature_branch_adds_commits_and_reads_back(
    tmp_path: Path, app_config: AppConfig
) -> None:
    fake = FakeGit(
        [
            (("rev-parse", "--abbrev-ref"), _ok("feat/git\n")),
            (("add",), _ok()),
            (("diff", "--cached", "--quiet"), git_tools.GitRun(exit_code=1, stdout="", stderr="")),
            (("commit",), _ok()),
            (("log", "-1"), _ok("deadbeef\x1f补一段说明")),
        ]
    )

    result = git_tools.commit(
        _policy(tmp_path, app_config), paths=["notes.md"], message="补一段说明", runner=fake
    )

    assert result.committed is True
    assert result.commit == "deadbeef"
    assert result.files == ["notes.md"]
    assert fake.saw("add", "--", "notes.md")
    assert fake.saw("commit", "-m", "补一段说明", "--", "notes.md")
    # 参数闭集：没有 -A、没有 --no-verify、更没有 push
    assert "-A" not in fake.flat()
    assert "--no-verify" not in fake.flat()
    assert "push" not in fake.flat()


def test_commit_with_nothing_staged_does_not_create_a_commit(
    tmp_path: Path, app_config: AppConfig
) -> None:
    fake = FakeGit(
        [
            (("rev-parse", "--abbrev-ref"), _ok("feat/git\n")),
            (("add",), _ok()),
            (("diff", "--cached", "--quiet"), _ok("")),
        ]
    )

    result = git_tools.commit(
        _policy(tmp_path, app_config), paths=["notes.md"], message="空改动", runner=fake
    )

    assert result.committed is False
    assert "没有可提交的改动" in (result.note or "")
    assert not fake.saw("commit", "-m")


def test_commit_hook_failure_is_reported_verbatim(tmp_path: Path, app_config: AppConfig) -> None:
    fake = FakeGit(
        [
            (("rev-parse", "--abbrev-ref"), _ok("feat/git\n")),
            (("add",), _ok()),
            (("diff", "--cached", "--quiet"), git_tools.GitRun(exit_code=1, stdout="", stderr="")),
            (("commit",), _fail("pre-commit 钩子失败：ruff 报错")),
        ]
    )

    with pytest.raises(git_tools.GitError) as excinfo:
        git_tools.commit(
            _policy(tmp_path, app_config), paths=["notes.md"], message="提交", runner=fake
        )

    assert "ruff 报错" in str(excinfo.value)  # 原样转述，不吞掉 hook 的报错


@pytest.mark.parametrize(
    ("paths", "message", "expected"),
    [
        (["notes.md"], "   ", "提交信息不能为空"),
        (["notes.md"], "x" * (git_tools.MESSAGE_MAX_CHARS + 1), "提交信息过长"),
        ([], "有信息但没有路径", "必须显式列出要提交的路径"),
    ],
)
def test_commit_validates_inputs_before_touching_git(
    paths: list[str], message: str, expected: str, tmp_path: Path, app_config: AppConfig
) -> None:
    fake = FakeGit()

    with pytest.raises(git_tools.GitError) as excinfo:
        git_tools.commit(_policy(tmp_path, app_config), paths=paths, message=message, runner=fake)

    assert expected in str(excinfo.value)
    assert fake.calls == []


# ---- 参数闭集与失败汇报 ----------------------------------------------------


def test_defense_scan_rejects_remote_or_environment_switches() -> None:
    """兜底扫描：即使今天是代码拼参数，也不允许"以后改成拼接字符串"这条路。"""
    for argv in (
        ["push"],
        ["commit", "--no-verify"],
        ["commit", "--amend"],
        ["status", "--git-dir=/tmp/other"],
        ["-c", "core.pager=cat", "push"],
    ):
        with pytest.raises(git_tools.GitError):
            git_tools._assert_safe(argv)

    # 代码自己的前缀（决定路径怎么显示）是允许的
    git_tools._assert_safe([*git_tools._CODE_PREFIX, "status", "--porcelain=v1"])


def test_git_output_is_decoded_as_utf8_not_the_system_encoding() -> None:
    """中文提交信息必须按 UTF-8 解。

    2026-10-09 真实 git 验收抓到的缺陷：本机（Windows + GBK）用系统首选编码解码，提交信息变成
    ``璁板繂搴撹矾寰勬寜 base_dir``。它不会抛异常——只会静默给出错字，所以这里把它钉住。
    """
    raw = "补 Git 工具".encode()

    assert git_tools._decode(raw) == "补 Git 工具"

    system = locale.getpreferredencoding(False)
    if system.lower().replace("-", "") not in ("utf8", "utf-8"):
        # 非 UTF-8 环境：按系统编码解一定解错，这正是当初踩到的坑
        assert raw.decode(system, errors="replace") != "补 Git 工具"


def test_timeout_is_a_failure_not_an_empty_result(
    tmp_path: Path, app_config: AppConfig
) -> None:
    fake = FakeGit(
        [
            (("rev-parse", "--show-toplevel"), _ok(f"{tmp_path.as_posix()}\n")),
            (("status",), git_tools.GitRun(exit_code=-1, stdout="", stderr="超时：20 秒内没有结束", timed_out=True)),
        ]
    )

    with pytest.raises(git_tools.GitError) as excinfo:
        git_tools.status(_policy(tmp_path, app_config), runner=fake)

    assert "超时" in str(excinfo.value)


def test_git_stderr_is_surfaced_instead_of_being_swallowed(
    tmp_path: Path, app_config: AppConfig
) -> None:
    fake = FakeGit(
        [
            (("rev-parse", "--show-toplevel"), _ok(f"{tmp_path.as_posix()}\n")),
            (("diff",), _fail("fatal: ambiguous argument 'x'")),
        ]
    )

    with pytest.raises(git_tools.GitError) as excinfo:
        git_tools.diff(_policy(tmp_path, app_config), path="x", runner=fake)

    assert "ambiguous argument" in str(excinfo.value)

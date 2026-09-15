"""T04/T05 验收：分支边界、路径安全与工具放行。

覆盖：闲聊分支不绑定工具、工具调用不能越出工作区、AGENTS 扫描不越过
``workspace.root``、敏感文件与密钥脱敏、未放行工具失败闭合。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from local_agent.config import AgentsConfig, AppConfig
from local_agent.context import agents_loader
from local_agent.exceptions import WorkspaceBoundaryError
from local_agent.graph import builder
from local_agent.graph.state import RuntimeContext
from local_agent.security import shell_guard
from local_agent.security.approval import action_fingerprint
from local_agent.security.path_guard import is_sensitive_path
from local_agent.security.policy import SecurityPolicy
from local_agent.security.shell_guard import (
    EXECUTOR_ARGV,
    EXECUTOR_CMD,
    EXECUTOR_PWSH,
    EXECUTOR_SH,
    build_shell_invocation,
    match_blocked_patterns,
    prepare_shell,
    split_command,
)
from local_agent.tools.tool_registry import ToolRegistry

ModelFactory = Callable[..., object]
OUTSIDE_SECRET = "SECRET-OUTSIDE-WORKSPACE"


def _context(workspace: Path, model: object, app_config: AppConfig) -> RuntimeContext:
    return builder.build_runtime_context(
        app_config=app_config,
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=workspace,
        current_cwd=workspace,
    )


@pytest.fixture(autouse=True)
def _stable_executables(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把"解析不到"的可执行名补成稳定路径。

    白名单语义的用例不该依赖"这次是怎么启动 pytest 的"：``uv run python -m pytest``
    时 ``pytest`` 在 PATH 上，直接跑 ``.venv\\Scripts\\python.exe -m pytest`` 时不在，
    同一个用例就会时好时坏。这里只补齐解析不到的名字，能解析到的仍用真实结果；
    需要验证"解析失败"语义的用例用 ``@pytest.mark.real_resolution`` 退出该行为。
    """
    if request.node.get_closest_marker("real_resolution") is not None:
        return
    real_which = shell_guard.shutil.which

    def fake_which(name: str, *args: object, **kwargs: object) -> str:
        return real_which(name, *args, **kwargs) or f"/stable/{name}"

    monkeypatch.setattr(shell_guard.shutil, "which", fake_which)


def _snapshot(root: Path) -> list[tuple[str, int, float]]:
    """工作区快照：相对路径、大小与修改时间。"""
    entries: list[tuple[str, int, float]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            entries.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime))
    return entries


# --------------------------------------------------------------------------- AGENTS 边界


def test_agents_scan_never_reads_outside_workspace(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工作区外的 AGENTS.md 不得进入运行上下文或模型提示。"""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".git").mkdir()
    (outer / "AGENTS.md").write_text(OUTSIDE_SECRET, encoding="utf-8")

    model = model_factory(decision="chat", reply="好的")
    context = _context(inner, model, app_config)
    builder.build_chat_graph().invoke({"user_input": "你好"}, context=context)

    assert context.agents_context is None
    assert OUTSIDE_SECRET not in "\n".join(model.seen_prompts)


def test_agents_config_disabled_reads_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("规则", encoding="utf-8")

    loaded = agents_loader.load_agents(
        AgentsConfig.model_validate({"enabled": False}),
        workspace_root=workspace,
        current_cwd=workspace,
    )

    assert loaded.is_empty


# --------------------------------------------------------------------------- 路径与密钥


def test_path_guard_blocks_symlink_escape(tmp_path: Path, app_config: AppConfig) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("外部内容", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 需要开发者模式
        pytest.skip("当前环境不支持创建符号链接")

    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})

    with pytest.raises(WorkspaceBoundaryError):
        policy.resolve_path("link.txt")


def test_sensitive_name_patterns() -> None:
    assert is_sensitive_path(Path(".env"))
    assert is_sensitive_path(Path("/tmp/server.key"))
    assert is_sensitive_path(Path("id_rsa"))
    assert not is_sensitive_path(Path("README.md"))
    assert not is_sensitive_path(Path("main.py"))


def test_policy_redacts_secret_values(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy(
        workspace_root=tmp_path,
        security=app_config.security,
        workspace=app_config.workspace,
        secret_values=("sk-secret-value",),
    )

    assert policy.redact("key=sk-secret-value;") == "key=***;"
    assert policy.redact("nothing") == "nothing"


def test_unknown_tool_is_denied(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    assert policy.classify_tool("list_dir").allowed
    denied = policy.classify_tool("delete_file")
    assert not denied.allowed
    assert denied.requires_approval
    assert "尚未接入" in denied.reason or "审批" in denied.reason
    assert not policy.classify_tool("some_unknown_tool").allowed


def test_write_tools_need_approval_when_risky(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    small_edit = policy.classify_tool("edit_file", {"old_text": "a", "new_text": "b"})
    assert small_edit.allowed and not small_edit.requires_approval

    huge_edit = policy.classify_tool(
        "edit_file", {"old_text": "a", "new_text": "\n".join(str(i) for i in range(500))}
    )
    assert not huge_edit.allowed
    assert huge_edit.requires_approval
    assert "大范围修改" in huge_edit.reason

    assert policy.classify_tool("edit_file", {}).allowed is False
    assert policy.classify_tool("write_file", {}).allowed is True
    assert policy.classify_tool("write_file", {"overwrite": True}).requires_approval is True
    assert policy.classify_tool("delete_file", {"recursive": True}).requires_approval is True


# --------------------------------------------------------------------------- Shell 判定


def test_split_command_respects_quotes_and_keeps_backslashes() -> None:
    assert split_command('python "a b.py" --flag') == ["python", "a b.py", "--flag"]
    assert split_command(r"python C:\work\x.py") == ["python", r"C:\work\x.py"]
    assert split_command("   ") == []


def test_shell_whitelist_allows_exact_command(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool("run_shell", {"command": "python --version"})

    assert decision.allowed
    assert not decision.requires_approval
    assert "白名单" in decision.reason


def test_shell_whitelist_requires_approval_for_extra_args(
    app_config: AppConfig, tmp_path: Path
) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command in ("pytest -q", "git diff --stat", "ruff check --fix"):
        decision = policy.classify_tool("run_shell", {"command": command})
        assert not decision.allowed, command
        assert decision.requires_approval, command
        assert "白名单" in decision.reason


def test_shell_rejects_shell_syntax(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command in (
        "python --version | head -1",
        "git status > out.txt",
        "echo $(whoami)",
        "echo %PATH%",
        "pytest --version & echo done",
    ):
        decision = policy.classify_tool("run_shell", {"command": command})
        assert not decision.allowed, command
        assert decision.requires_approval, command
        assert "Shell 语法" in decision.reason


@pytest.mark.real_resolution
def test_blocked_pattern_with_unresolvable_executable_is_rejected(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """命中破坏性模式、但当前环境根本没有这个可执行文件 → 直接拒绝，不进入审批。

    否则用户批准的是一个必然启动失败的命令，而且判定对象与执行对象仍然不一致。
    """
    config = _with_shell_config(app_config, blocked_patterns=["no-such-destructive-tool"])
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool(
        "run_shell", {"command": "no-such-destructive-tool --now"}
    )

    assert not decision.allowed
    assert not decision.requires_approval  # 不可执行 → 不给审批机会
    assert "破坏性命令模式" in decision.reason  # 危险原因仍然要说清楚
    assert "找不到可执行文件" in decision.reason


def test_blocked_pattern_with_resolvable_executable_needs_approval(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """命中破坏性模式、且执行对象可解析 → 保留审批通道（架构 §6.6）。"""
    config = _with_shell_config(app_config, blocked_patterns=["python"])
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool("run_shell", {"command": "python --version"})

    assert not decision.allowed
    assert decision.requires_approval
    assert "破坏性命令模式" in decision.reason


@pytest.mark.real_resolution
def test_shell_blocked_patterns_force_approval(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool("run_shell", {"command": "rm -rf build"})

    assert not decision.allowed
    assert "破坏性命令模式" in decision.reason


def test_shell_executable_name_matching_is_normalized(
    app_config: AppConfig, tmp_path: Path
) -> None:
    data = app_config.model_dump()
    data["security"]["shell"]["whitelist"] = [
        {"platform": ["windows", "posix"], "executable": "python", "args": ["--version"], "match": "exact"}
    ]
    policy = SecurityPolicy.from_config(
        AppConfig.model_validate(data), workspace_root=tmp_path, environ={}
    )

    assert policy.classify_tool("run_shell", {"command": "PYTHON.EXE --version"}).allowed
    # 不同可执行名不能因为前缀相同而命中
    assert not policy.classify_tool("run_shell", {"command": "python3 --version"}).allowed


def test_shell_platform_filter_is_respected(app_config: AppConfig, tmp_path: Path) -> None:
    data = app_config.model_dump()
    data["security"]["shell"]["whitelist"] = [
        {"platform": ["posix"], "executable": "python", "args": ["--version"], "match": "exact"}
    ]
    config = AppConfig.model_validate(data).security.shell

    on_windows = prepare_shell(
        "python --version", config=config, workspace_root=tmp_path, platform="windows"
    )
    on_posix = prepare_shell(
        "python --version", config=config, workspace_root=tmp_path, platform="posix"
    )

    assert not on_windows.allowed and on_windows.requires_approval
    assert on_posix.allowed


def test_shell_cwd_must_stay_inside_workspace(app_config: AppConfig, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})

    outside = policy.classify_tool("run_shell", {"command": "python --version", "cwd": ".."})
    assert not outside.allowed
    assert "工作目录不合法" in outside.reason

    missing = policy.classify_tool("run_shell", {"command": "python --version", "cwd": "nope"})
    assert not missing.allowed
    assert "不存在" in missing.reason

    inside = policy.classify_tool("run_shell", {"command": "python --version", "cwd": "."})
    assert inside.allowed


def test_shell_env_keys_block_secrets(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for key in ("DEEPSEEK_API_KEY", "MY_TOKEN", "APP_SECRET", "db_password"):
        decision = policy.classify_tool(
            "run_shell", {"command": "python --version", "env_keys": [key]}
        )
        assert not decision.allowed, key
        assert "禁止透传" in decision.reason

    ok = policy.classify_tool("run_shell", {"command": "python --version", "env_keys": ["MY_MODE"]})
    assert ok.allowed


def test_shell_timeout_cannot_exceed_limit(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool(
        "run_shell", {"command": "python --version", "timeout_seconds": 99999}
    )

    assert not decision.allowed
    assert "超时时间" in decision.reason


# --------------------------------------------------------------------------- T12 跨平台 Shell


def _with_shell_config(app_config: AppConfig, **overrides: object) -> AppConfig:
    """按覆盖项构造一份 ``security.shell`` 变体配置。"""
    data = app_config.model_dump()
    data["security"]["shell"].update(overrides)
    return AppConfig.model_validate(data)


def test_shell_syntax_command_uses_platform_adapter(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """含 Shell 语法的命令：提级审批 + 平台适配器，两个平台各用各的解析规则（架构 §6.6）。"""
    config = app_config.security.shell

    on_windows = prepare_shell(
        "echo hi | findstr hi", config=config, workspace_root=tmp_path, platform="windows"
    )
    on_posix = prepare_shell(
        "echo hi | grep hi", config=config, workspace_root=tmp_path, platform="posix"
    )

    assert not on_windows.allowed and on_windows.requires_approval
    assert on_windows.executor == EXECUTOR_CMD
    assert on_windows.uses_shell_adapter
    assert on_windows.shell_syntax == "|"
    assert on_windows.shell_command == "echo hi | findstr hi"
    windows_invocation = build_shell_invocation(on_windows)
    assert Path(windows_invocation[0]).name.casefold() == "cmd.exe"
    assert windows_invocation[1:] == ["/d", "/s", "/c", "echo hi | findstr hi"]
    assert "cmd.exe /d /s /c" in on_windows.reason

    assert on_posix.executor == EXECUTOR_SH
    # 当前平台没有 sh，解析不到就退回适配器名（执行结果与判定时的环境一致）
    assert build_shell_invocation(on_posix)[1:] == ["-c", "echo hi | grep hi"]
    assert "/bin/sh -c" in on_posix.reason

    # 重定向同样交给适配器，原始命令原样保留（不能被切开重排）
    redirect = prepare_shell(
        "echo hi > out.txt", config=config, workspace_root=tmp_path, platform="windows"
    )
    assert not redirect.allowed and redirect.requires_approval
    assert redirect.shell_syntax == ">"
    assert redirect.shell_command == "echo hi > out.txt"
    assert build_shell_invocation(redirect)[-1] == "echo hi > out.txt"


@pytest.mark.real_resolution
def test_executor_config_can_force_pwsh_adapter(app_config: AppConfig, tmp_path: Path) -> None:
    """``executor`` 可显式指定适配器；与运行平台冲突或本机没装时都失败闭合。"""
    config = _with_shell_config(app_config, executor="pwsh").security.shell

    plan = prepare_shell(
        "Get-ChildItem | Select-String x",
        config=config,
        workspace_root=tmp_path,
        platform="windows",
    )

    assert plan.executor == EXECUTOR_PWSH
    if shutil.which("pwsh") is None:
        # 本机没有 pwsh：判定阶段就拒绝，不先让用户批准、执行时才报错
        assert not plan.allowed and not plan.requires_approval
        assert "找不到" in plan.reason
    else:
        assert not plan.allowed and plan.requires_approval
        assert plan.executable_path is not None
        assert Path(plan.executable_path).is_file()
    invocation = build_shell_invocation(plan)
    assert Path(invocation[0]).name.casefold().startswith("pwsh")
    assert invocation[1:] == [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-ChildItem | Select-String x",
    ]

    wrong_platform = prepare_shell(
        "a | b", config=config, workspace_root=tmp_path, platform="posix"
    )
    assert not wrong_platform.allowed and not wrong_platform.requires_approval
    assert "不适用于 posix" in wrong_platform.reason


def test_argv_commands_stay_without_shell_adapter(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """没有 Shell 语法就 argv 直执行，不经任何适配器。"""
    plan = prepare_shell(
        "python --version", config=app_config.security.shell, workspace_root=tmp_path
    )

    assert plan.allowed and plan.executor == EXECUTOR_ARGV
    assert not plan.uses_shell_adapter
    assert plan.shell_command is None
    invocation = build_shell_invocation(plan)
    assert Path(invocation[0]).name.casefold().startswith("python")
    assert invocation[1:] == ["--version"]


def test_absolute_path_executables_never_get_free_pass(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """带路径的可执行文件即使参数与白名单一致也不能免审（架构 §6.6）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    # 参数与白名单完全一致，但执行对象是显式路径 → 只能走审批
    decision = policy.classify_tool(
        "run_shell", {"command": f'"{sys.executable}" --version'}
    )

    assert not decision.allowed
    assert decision.requires_approval
    assert "路径" in decision.reason


def test_path_qualified_executable_needs_approval(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """工作区内的相对路径可执行文件：能解析到就提级审批（按该路径执行）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "tool.exe").write_bytes(b"stub")
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})

    decision = policy.classify_tool("run_shell", {"command": "./tool.exe --version"})

    assert not decision.allowed and decision.requires_approval
    assert "路径" in decision.reason


@pytest.mark.real_resolution
def test_unresolvable_executable_is_rejected_at_judgment(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """解析不到执行对象时在判定阶段就失败闭合，不能先批准、执行时才报错。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command in ("./python --version", "bin/python --version", "no-such-tool --flag"):
        decision = policy.classify_tool("run_shell", {"command": command})
        assert not decision.allowed, command
        assert not decision.requires_approval, command
        assert "找不到可执行文件" in decision.reason, command


def test_executable_matching_is_normalized_without_aliases(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """大小写与 Windows 后缀归一化；别名不共享免审资格（架构 §6.6）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    assert policy.classify_tool("run_shell", {"command": "PYTHON.EXE --version"}).allowed
    assert policy.classify_tool("run_shell", {"command": "python --version"}).allowed

    for command in ("python3 --version", "py --version", "pip --version", "giti status"):
        assert not policy.classify_tool("run_shell", {"command": command}).allowed, command


def test_whitelist_never_grants_extra_arguments(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """``pytest`` 带任意参数、``git diff`` 带额外参数都不得免审（T12 验收）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command in (
        "pytest",
        "pytest -q",
        "pytest tests/test_security.py",
        "git diff --stat",
        "git diff HEAD~1",
        "git status --short",
        "ruff check --fix",
    ):
        decision = policy.classify_tool("run_shell", {"command": command})
        assert not decision.allowed, command
        assert decision.requires_approval, command

    # 参数完全一致的那一条仍然免审
    assert policy.classify_tool("run_shell", {"command": "pytest --version"}).allowed
    assert policy.classify_tool("run_shell", {"command": "git diff"}).allowed


def test_whitelist_entry_can_limit_cwd(app_config: AppConfig, tmp_path: Path) -> None:
    """白名单条目可用 ``cwd`` 限定生效目录（架构 §6.6 的匹配维度之一）。"""
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    config = _with_shell_config(
        app_config,
        whitelist=[
            {
                "platform": ["windows", "posix"],
                "executable": "python",
                "args": ["--version"],
                "match": "exact",
                "cwd": "sub",
            }
        ],
    )
    policy = SecurityPolicy.from_config(config, workspace_root=workspace, environ={})

    inside = policy.classify_tool("run_shell", {"command": "python --version", "cwd": "sub"})
    assert inside.allowed

    outside = policy.classify_tool("run_shell", {"command": "python --version", "cwd": "."})
    assert not outside.allowed and outside.requires_approval


@pytest.mark.real_resolution
def test_blocked_patterns_match_parsed_structure(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """黑名单按解析后的结构匹配：大小写、Windows 后缀与绝对路径一样拦得住（架构 §6.6）。

    命中破坏性模式后：执行对象可解析 → 进入审批；不可解析 → 直接拒绝（两者都要报危险原因）。
    """
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    for command, executable in (
        ("rm -rf build", "rm"),
        ("format C:", "format"),
        (r"C:\Windows\System32\FORMAT.COM C:", "format"),
        ("shutdown /s /t 0", "shutdown"),
        ("del /s /q dist", "del"),
        ("chmod 777 script.sh", "chmod"),
    ):
        decision = policy.classify_tool("run_shell", {"command": command})
        assert not decision.allowed, command
        assert "破坏性命令模式" in decision.reason, command
        if shutil.which(executable) is None:
            # 环境里没有这个可执行文件（例如 Windows 上的 rm/del/chmod）→ 直接拒绝
            assert not decision.requires_approval, command
            assert "找不到可执行文件" in decision.reason, command
        else:
            assert decision.requires_approval, command


@pytest.mark.real_resolution
def test_shell_plan_resolves_executable_path(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """判定阶段解析出可执行文件绝对路径，执行阶段固定用它（架构 §6.6）。"""
    plan = prepare_shell(
        "python --version", config=app_config.security.shell, workspace_root=tmp_path
    )

    assert plan.executable_path is not None
    assert Path(plan.executable_path).is_file()
    assert build_shell_invocation(plan)[0] == plan.executable_path


@pytest.mark.real_resolution
def test_shell_adapter_invocation_uses_resolved_path(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """Shell 适配器同样按解析出的绝对路径启动，不靠 PATH 二次搜索。"""
    plan = prepare_shell(
        "echo hi | findstr hi",
        config=app_config.security.shell,
        workspace_root=tmp_path,
        platform="windows",
    )

    assert plan.executable_path is not None
    assert Path(plan.executable_path).is_file()
    invocation = build_shell_invocation(plan)
    assert invocation[0] == plan.executable_path
    assert invocation[1:4] == ["/d", "/s", "/c"]


def test_shell_fingerprint_includes_resolved_executable(
    app_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """可执行文件解析结果变了指纹就得变：批准不能跨执行对象生效（架构 §5.3）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    arguments = {"command": "python --version", "cwd": ".", "env_keys": []}
    before = action_fingerprint("run_shell", arguments, policy=policy)

    monkeypatch.setattr(
        shell_guard.shutil, "which", lambda name: str(tmp_path / "python.exe")
    )
    after = action_fingerprint("run_shell", arguments, policy=policy)

    assert before != after


def test_fingerprint_separates_replace_all(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """``replace_all`` 影响执行语义（改一处 vs 全部），必须体现在指纹里（架构 §5.3）。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    base = {"path": "AGENTS.md", "old_text": "a", "new_text": "b"}

    once = action_fingerprint("edit_file", {**base, "replace_all": False}, policy=policy)
    every = action_fingerprint("edit_file", {**base, "replace_all": True}, policy=policy)

    assert once != every
    # 省略参数与显式传默认值必须等价，否则同一个动作的批准会莫名其妙失效
    assert once == action_fingerprint("edit_file", base, policy=policy)


def test_fingerprint_includes_timeout(app_config: AppConfig, tmp_path: Path) -> None:
    """超时是执行计划的一部分：不同超时的指纹必须不同，省略值与**配置默认值**必须相同。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    command = {"command": "python --version"}
    default_timeout = app_config.security.shell.default_timeout_seconds

    short = action_fingerprint("run_shell", {**command, "timeout_seconds": 1}, policy=policy)
    longer = action_fingerprint("run_shell", {**command, "timeout_seconds": 30}, policy=policy)

    assert short != longer
    assert action_fingerprint("run_shell", command, policy=policy) == action_fingerprint(
        "run_shell", {**command, "timeout_seconds": default_timeout}, policy=policy
    )


def test_shell_timeout_default_follows_config(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """省略超时时有效默认值取**配置值**，不是配置的出厂常量 60。

    配置调小到 30 秒时：判定按 30 秒放行；指纹与显式 30 秒一致、与显式 60 秒不同。
    """
    config = _with_shell_config(app_config, default_timeout_seconds=30)
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})
    command = {"command": "python --version"}

    assert policy.classify_tool("run_shell", command).allowed

    omitted = action_fingerprint("run_shell", command, policy=policy)
    assert omitted == action_fingerprint(
        "run_shell", {**command, "timeout_seconds": 30}, policy=policy
    )
    assert omitted != action_fingerprint(
        "run_shell", {**command, "timeout_seconds": 60}, policy=policy
    )


def test_fingerprint_keeps_shell_operators(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """Shell 运算符决定命令语义，不能被 ``split_command`` 丢掉后再算指纹。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    greater = action_fingerprint(
        "run_shell", {"command": "echo hi > target.txt"}, policy=policy
    )
    less = action_fingerprint("run_shell", {"command": "echo hi < target.txt"}, policy=policy)

    assert greater != less


def test_blocked_patterns_keep_substring_fallback() -> None:
    """pattern 无法做结构匹配时退回子串兜底，且不能被相似名字误触发。"""
    # 结构匹配负责子串匹配抓不到的形态：绝对路径 + Windows 后缀 + 大写
    assert (
        match_blocked_patterns(
            [r"C:\Windows\System32\FORMAT.COM", "C:"],
            r"C:\Windows\System32\FORMAT.COM C:",
            ["format "],
        )
        == "format "
    )
    # pattern 不是合法命令形态（这里含子串但整串匹配才成立）时退回子串兜底
    assert (
        match_blocked_patterns(
            ["python", "-c", "print('x')"], 'python -c "print(\'rm -rf\')"', ["rm -rf"]
        )
        == "rm -rf"
    )
    # 不误伤：可执行名与参数结构都对不上、也没有原样子串
    assert (
        match_blocked_patterns(["python", "scripts/format.py"], "python scripts/format.py", ["format "])
        is None
    )
    assert match_blocked_patterns(["echo", "hi"], "echo hi", ["rm -rf"]) is None


def test_registry_exposes_no_tools_in_chat_mode(app_config: AppConfig, tmp_path: Path) -> None:
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    registry = ToolRegistry.build(policy)

    assert registry.tools_for_mode("chat") == []
    assert [tool.name for tool in registry.tools_for_mode("tool")] == [
        "list_dir",
        "read_file",
        "edit_file",
        "write_file",
        "delete_file",
        "run_shell",
    ]
    assert registry.get("nope") is None


# --------------------------------------------------------------------------- 工具分支


def test_chat_branch_produces_no_tool_events(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    model = model_factory(decision="chat", reply="你好")

    result = builder.build_chat_graph().invoke(
        {"user_input": "你好"}, context=_context(tmp_path, model, app_config)
    )

    assert result["mode"] == "chat"
    assert not result.get("pending_tool_calls")
    assert not result.get("tool_events")
    assert not result.get("tool_results")
    assert not result.get("final_evidence")


def test_chat_mode_binds_no_tools(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """闲聊模式不把任何工具交给模型（架构 §1.3-3）。"""
    model = model_factory(decision="chat", reply="你好")
    context = _context(tmp_path, model, app_config)
    model.bound_tools = []  # 忽略能力探测阶段的绑定记录

    builder.build_chat_graph().invoke({"user_input": "你好"}, context=context)

    assert model.bound_tools == []


def test_tool_mode_binds_registered_tools(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工具模式绑定全部已实现工具；能不能执行由安全门决定，不是"不告诉模型"。"""
    model = model_factory(decision="tool", reply="好的")

    builder.build_chat_graph().invoke(
        {"user_input": "列出当前目录"}, context=_context(tmp_path, model, app_config)
    )

    assert model.bound_tools == [
        "list_dir",
        "read_file",
        "edit_file",
        "write_file",
        "delete_file",
        "run_shell",
    ]


def test_tool_call_cannot_read_outside_workspace(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工具调用指向工作区外：被拒绝、内容不泄漏、工作区不受影响。"""
    outside = tmp_path / "outside.txt"
    outside.write_text(OUTSIDE_SECRET, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    before = _snapshot(workspace)

    model = model_factory(
        decision="tool",
        reply="无法读取该文件",
        tool_calls=[
            {"name": "read_file", "args": {"path": outside.as_posix()}, "id": "call_1"}
        ],
    )
    result = builder.build_chat_graph().invoke(
        {"user_input": "读取工作区外的文件"}, context=_context(workspace, model, app_config)
    )

    assert result["tool_results"][0]["status"] == "denied"
    assert OUTSIDE_SECRET not in json.dumps(result["tool_results"], ensure_ascii=False, default=str)
    assert OUTSIDE_SECRET not in "\n".join(model.seen_prompts)
    assert _snapshot(workspace) == before
    assert result["final_response"] == "无法读取该文件"


def test_secret_file_tool_call_is_denied(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-real-secret", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="无法读取密钥文件",
        tool_calls=[{"name": "read_file", "args": {"path": ".env"}, "id": "call_1"}],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读一下 .env"}, context=_context(tmp_path, model, app_config)
    )

    assert result["tool_results"][0]["status"] == "denied"
    assert "sk-real-secret" not in json.dumps(result, ensure_ascii=False, default=str)

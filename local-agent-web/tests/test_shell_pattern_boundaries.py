"""真实开发验收 F2：避免命令名子串和默认下载规则误触发审批。"""

from pathlib import Path

import pytest

from local_agent.security import shell_guard
from local_agent.security.policy import SecurityPolicy


@pytest.mark.parametrize("text", [
    "shadow below card ref/page2:", "guard output", "discard output",
    "model data", "term output", "reformat report", "permission shutdownish",
])
def test_builtin_patterns_do_not_match_inside_longer_names(text: str) -> None:
    command = f'python -c "print(\'{text}\')"'
    argv = shell_guard.split_command(command)
    assert shell_guard.match_blocked_patterns(argv, command, []) is None


@pytest.mark.parametrize("command,pattern", [
    ("rd /s /q build", "rd "),
    ("rmdir build", "rmdir "),
    ("del /q build.txt", "del "),
    ("rm -rf build", "rm "),
    ("format C:", "format "),
    (r"C:\Windows\System32\FORMAT.COM C:", "format "),
    ("echo ready & rd /s /q build", "rd "),
    ("echo ready&&DEL /q build.txt", "del "),
    ("echo ready;rm -rf build", "rm "),
    ("echo ready\nrd /s /q build", "rd "),
    ('cmd /c "rd /s /q build"', "rd "),
    ('sh -c "rm -rf build"', "rm "),
    ('python -c "import os; os.system(\'rd /s /q build\')"', "rd "),
])
def test_builtin_destructive_patterns_keep_matching(command: str, pattern: str) -> None:
    # 只判定，不执行这些命令。
    argv = shell_guard.split_command(command)
    assert shell_guard.match_blocked_patterns(argv, command, []) == pattern


@pytest.mark.parametrize("command,patterns,expected", [
    ('python -c "print(\'card ref/page2:\')"', ["card ref"], "card ref"),
    ('python -c "print(\'rm -rf\')"', ["rm -rf"], "rm -rf"),
    ("curl https://example.com/", ["curl "], "curl "),
])
def test_custom_substring_rules_remain_explicitly_enforced(command, patterns, expected) -> None:
    assert shell_guard.match_blocked_patterns(
        shell_guard.split_command(command), command, patterns,
    ) == expected


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "danger-full-access"])
@pytest.mark.parametrize("command", [
    'python -c "print(\'shadow below card ref/page2:\')"',
    "curl -sS --max-time 25 -o robots.txt https://www.python.org/robots.txt",
    "mkdir logs 2>nul & curl -sS -o robots.txt https://www.python.org/robots.txt",
    "wget -O robots.txt https://www.python.org/robots.txt",
])
def test_ordinary_commands_do_not_require_approval(
    tmp_path: Path, app_config, monkeypatch, mode: str, command: str,
) -> None:
    config = app_config.model_copy(deep=True)
    config.workspace.external_paths.mode = mode
    # 此用例只验决策；真实解析与执行在独立 Windows 复验中验证。
    monkeypatch.setattr(shell_guard, "resolve_executable", lambda name, **_: tmp_path / name)
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})

    decision = policy.classify_tool("run_shell", {"command": command, "cwd": "."})

    assert decision.allowed and not decision.requires_approval, decision.reason


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "danger-full-access"])
def test_actual_delete_still_requires_approval(tmp_path, app_config, monkeypatch, mode) -> None:
    config = app_config.model_copy(deep=True)
    config.workspace.external_paths.mode = mode
    monkeypatch.setattr(shell_guard, "resolve_executable", lambda name, **_: tmp_path / name)
    policy = SecurityPolicy.from_config(config, workspace_root=tmp_path, environ={})
    decision = policy.classify_tool("run_shell", {"command": "rd /s /q build"})
    assert not decision.allowed and decision.requires_approval
    assert "rd " in decision.reason

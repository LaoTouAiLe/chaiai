"""Final shell reasons must agree with the actual permission/approval decision."""

import pytest

from local_agent.security import shell_guard, win_sandbox
from local_agent.security.path_guard import ExternalPathPolicy, normalize_grant_path
from local_agent.security.policy import SecurityPolicy

MODES = ("read-only", "workspace-write", "danger-full-access")


@pytest.fixture
def policy_factory(tmp_path, app_config, monkeypatch):
    monkeypatch.setattr(shell_guard, "resolve_executable", lambda name, **kwargs: str(tmp_path / name))
    monkeypatch.setattr(win_sandbox, "is_supported", lambda: True)

    def make(mode, *, grants=None):
        config = app_config.model_copy(deep=True)
        config.security.shell.platform = "windows"
        config.security.shell.executor = "cmd"
        config.workspace.external_paths.mode = mode
        return SecurityPolicy.from_config(
            config, workspace_root=tmp_path, environ={},
            external_policy=ExternalPathPolicy(mode=mode, grants=grants or {}),
        )

    return make


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("command,execution", [
    ('python -c "print(123)"', "argv"),
    ('python -c "print(123)" | more', "cmd.exe /d /s /c"),
    ('python -c "print(123)" > result.txt', "cmd.exe /d /s /c"),
    ('"E:/tools/python.exe" -c "print(123)"', "argv"),
])
def test_ordinary_command_reason_matches_final_allowance(policy_factory, mode, command, execution):
    decision = policy_factory(mode).classify_tool("run_shell", {"command": command})
    assert decision.allowed and not decision.requires_approval
    assert mode in decision.reason and "常规命令免审批" in decision.reason
    assert execution in decision.reason
    for obsolete in ("需要人工审批", "需要人工批准", "已提高风险等级", "不在安全白名单"):
        assert obsolete not in decision.reason


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", ["destructive", "external-cwd", "escalation"])
def test_mandatory_approval_reasons_stay_explicit(policy_factory, tmp_path, mode, case):
    policy = policy_factory(mode)
    arguments = {"command": 'python -c "print(123)"'}
    if case == "destructive":
        arguments["command"] = "rd /s /q canary"
        expected = "破坏性命令任何档位都需要人工批准"
    elif case == "external-cwd":
        outside = tmp_path.parent / (tmp_path.name + "-external")
        outside.mkdir()
        policy = policy_factory(mode, grants={normalize_grant_path(outside): "read_write"})
        arguments["cwd"] = str(outside)
        expected = "必须人工审批"
    else:
        arguments["escalate_sandbox"] = True
        expected = "任何档位都需要人工批准"
    decision = policy.classify_tool("run_shell", arguments)
    if case == "external-cwd":
        assert decision.allowed and not decision.requires_approval
        return
    assert not decision.allowed
    assert decision.requires_approval
    assert expected in decision.reason
    assert "常规命令免审批" not in decision.reason


def test_read_only_without_os_boundary_keeps_approval_reason(policy_factory, monkeypatch):
    policy = policy_factory("read-only")
    monkeypatch.setattr(win_sandbox, "is_supported", lambda: False)
    decision = policy.classify_tool("run_shell", {"command": 'python -c "print(123)"'})
    assert not decision.allowed and decision.requires_approval
    assert "没有 OS 级写边界" in decision.reason and "需要人工批准" in decision.reason


def test_missing_executable_keeps_environment_failure(policy_factory, monkeypatch):
    policy = policy_factory("workspace-write")
    monkeypatch.setattr(shell_guard, "resolve_executable", lambda *args, **kwargs: None)
    decision = policy.classify_tool("run_shell", {"command": "missing-tool --version"})
    assert not decision.allowed and not decision.requires_approval
    assert decision.reason_kind == "environment"
    assert "找不到可执行文件" in decision.reason


def test_safe_read_and_whitelisted_reasons_remain_specific(policy_factory):
    policy = policy_factory("workspace-write")
    read = policy.classify_tool("run_shell", {"command": "echo ready"})
    whitelist = policy.classify_tool("run_shell", {"command": "python --version"})
    assert read.allowed and not read.requires_approval and "只读安全命令" in read.reason
    assert whitelist.allowed and not whitelist.requires_approval and "命中白名单" in whitelist.reason

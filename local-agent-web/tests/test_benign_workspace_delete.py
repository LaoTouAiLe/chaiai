"""单目标删除的免审判定（§6.6，2026-10-23）。

放宽的边界必须逐条锁住：**只豁免"工作区内、单目标、非递归"**，其余一律保留审批。
这批用例的作用是防止日后有人把条件改宽——它们全是"应该仍然要审批"的反例。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.config import AppConfig
from local_agent.security.policy import SecurityPolicy
from local_agent.security.shell_guard import ShellPlan


def _policy(app_config: AppConfig, root: Path) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=root, environ={})


def _plan(command: str, cwd: Path) -> ShellPlan:
    """构造计划：真实路径下 argv 的最后一个元素就是完整命令行。"""
    return ShellPlan(allowed=True, requires_approval=False, reason="test",
                     argv=(command,), cwd=cwd)


@pytest.mark.parametrize("command", [
    'del /q "note.txt"',
    "del note.txt",
    "rm note.txt",
    "erase note.txt",
])
def test_single_target_delete_inside_workspace_is_exempt(
    app_config: AppConfig, tmp_path: Path, command: str
) -> None:
    policy = _policy(app_config, tmp_path)
    assert policy._is_benign_workspace_delete(_plan(command, tmp_path)) is True


@pytest.mark.parametrize("command", [
    "del /s note.txt",            # 递归
    "rm -rf build",               # 递归
    "del *.txt",                  # 通配
    "del a.txt b.txt",            # 多目标
    'del a.txt | more',           # 串联
    "del a.txt && del b.txt",     # 串联
    "format D:",                  # 硬拦截
    "shutdown /s",                # 硬拦截
    "taskkill /F /PID 1",         # 硬拦截
])
def test_dangerous_or_ambiguous_delete_still_needs_approval(
    app_config: AppConfig, tmp_path: Path, command: str
) -> None:
    policy = _policy(app_config, tmp_path)
    assert policy._is_benign_workspace_delete(_plan(command, tmp_path)) is False


def test_delete_outside_workspace_is_not_exempt(app_config: AppConfig, tmp_path: Path) -> None:
    """工作区外的删除仍要审批：授权目录之外本来就该拦。"""
    policy = _policy(app_config, tmp_path)
    outside = tmp_path.parent / "elsewhere.txt"
    assert policy._is_benign_workspace_delete(_plan(f'del "{outside}"', tmp_path)) is False
    assert policy._is_benign_workspace_delete(_plan("del ../elsewhere.txt", tmp_path)) is False


def test_no_cwd_means_no_exemption(app_config: AppConfig, tmp_path: Path) -> None:
    """相对路径判不出基准时，不做豁免——保守优先。"""
    policy = _policy(app_config, tmp_path)
    plan = ShellPlan(allowed=True, requires_approval=False, reason="test",
                     argv=("del note.txt",), cwd=None)
    assert policy._is_benign_workspace_delete(plan) is False


def test_windows_adapter_argv_takes_last_element(app_config: AppConfig, tmp_path: Path) -> None:
    """`cmd /d /s /c del x` 里 cmd 自己的 `/s` 不代表递归。"""
    policy = _policy(app_config, tmp_path)
    plan = ShellPlan(allowed=True, requires_approval=False, reason="test",
                     argv=("cmd.exe", "/d", "/s", "/c", "del note.txt"), cwd=tmp_path)
    assert policy._is_benign_workspace_delete(plan) is True

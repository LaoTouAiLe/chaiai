"""终端 shell 的选择与环境放行（2026-10-28：终端要像用户自己开的那个）。

两个决定都在这里钉住：
- **shell 可配置、找不到就退回 cmd**：不在"该用哪个 shell"上抛错，否则终端整个不可用；
- **环境只补工具链提示**：conda/venv 的 `(base)` 要能显示，但密钥类变量一个都不许带出去。
"""

import os
from pathlib import Path

import pytest

from local_agent.security import shell_guard
from local_agent.security.shell_guard import (
    TERMINAL_ENV_KEYS,
    build_environment,
    resolve_terminal_shell,
    terminal_argv,
)

POWERSHELL_NAMES = ("powershell.exe", "pwsh.exe")


@pytest.mark.parametrize("preference", ["auto", "powershell", "pwsh"])
def test_resolved_shell_exists_and_reports_its_kind(preference):
    """解析结果必须是真实存在的文件，且 `is_powershell` 与文件名一致。

    不写死"一定是 PowerShell"：机器上有没有 `pwsh` 不由测试决定，但**两者必须自洽**，
    并且永远不能返回一个不存在的路径。
    """
    shell, is_powershell = resolve_terminal_shell(preference)

    assert Path(shell).is_file()
    assert is_powershell == (Path(shell).name.casefold() in POWERSHELL_NAMES)


def test_cmd_preference_is_honoured():
    shell, is_powershell = resolve_terminal_shell("cmd")

    assert Path(shell).name.casefold() == "cmd.exe"
    assert is_powershell is False


def test_missing_pwsh_falls_back_instead_of_raising(monkeypatch):
    """配了 `pwsh` 但机器上没有：退回系统自带 PowerShell，而不是让终端不可用。"""
    monkeypatch.setattr(shell_guard.shutil, "which", lambda name: None)

    shell, is_powershell = resolve_terminal_shell("pwsh")

    assert Path(shell).is_file()
    # 装不了的机器上退回 cmd 也算合格——关键是"能起来"。
    assert is_powershell == (Path(shell).name.casefold() in POWERSHELL_NAMES)


def test_pwsh_wins_when_it_is_on_path(monkeypatch, tmp_path):
    fake = tmp_path / "pwsh.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(shell_guard.shutil, "which", lambda name: str(fake) if name == "pwsh" else None)

    shell, is_powershell = resolve_terminal_shell("auto")

    assert shell == str(fake)
    assert is_powershell is True


@pytest.mark.parametrize("shell", [r"C:\Windows\System32\cmd.exe", "cmd.exe"])
def test_cmd_gets_quiet_flags(shell):
    assert terminal_argv(shell, load_profile=True) == [shell, "/D", "/Q"]


@pytest.mark.parametrize("shell", [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "pwsh.exe"])
def test_powershell_loads_profile_by_default_and_can_skip_it(shell):
    assert terminal_argv(shell, load_profile=True) == [shell]
    assert terminal_argv(shell, load_profile=False) == [shell, "-NoProfile"]


def test_terminal_environment_carries_conda_hints_but_never_secrets(monkeypatch):
    """`(base)` 靠 `CONDA_*`；同一份环境里绝不能出现密钥。"""
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    monkeypatch.setenv("CONDA_PREFIX", r"C:\miniconda3")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-must-not-leak")

    environment = build_environment(TERMINAL_ENV_KEYS)

    assert environment["CONDA_DEFAULT_ENV"] == "base"
    assert environment["CONDA_PREFIX"] == r"C:\miniconda3"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment


def test_missing_keys_are_skipped_instead_of_written_as_empty(monkeypatch):
    """进程里没有的变量**不要**写成空串（2026-10-28 实测）。

    conda 的 profile 钩子会拿 `$env:CONDA_EXE` 去调程序；一个空值会让它自己崩在启动阶段，
    `(base)` 也就不会出现。"变量不存在"走的是另一条分支，是安全的。
    """
    monkeypatch.delenv("CONDA_EXE", raising=False)

    environment = build_environment(("CONDA_EXE",))

    assert "CONDA_EXE" not in environment


def test_secret_named_keys_are_dropped_even_when_explicitly_allowed(monkeypatch):
    """放行名单也可能来自代码常量：`build_environment` 自己再兜一层。"""
    monkeypatch.setenv("MY_API_KEY", "sk-should-be-dropped")
    monkeypatch.setenv("CUSTOM_SECRET", "should-be-dropped")
    monkeypatch.setenv("HARMLESS_FLAG", "1")

    environment = build_environment(("MY_API_KEY", "CUSTOM_SECRET", "HARMLESS_FLAG"))

    assert "MY_API_KEY" not in environment
    assert "CUSTOM_SECRET" not in environment
    assert environment["HARMLESS_FLAG"] == "1"


def test_base_keys_still_include_path_so_commands_resolve():
    """终端要能跑命令：PATH 一直在基础名单里。"""
    assert "PATH" in shell_guard.BASE_ENV_KEYS
    assert os.name == "nt" or "PATH" in build_environment(())

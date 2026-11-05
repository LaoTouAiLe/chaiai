"""没有 OS 级写边界时，这件事必须被**说出来**（2026-11-02）。

背景：`SecurityPolicy.sandbox_boundary_note()` 早就存在，但在 `src/` 里从未被调用——非 Windows 上
"命令照常跑、但没有内核强制边界"对用户和模型都是静默的。这类 fail-open 比功能缺失更危险：用户以为
「工作区可写」是内核强制的，实际命令内部可以写到任意地方。

这里锁四件事：
1. 说明文本是**共用的一份**（策略层 / 权限视图 / 系统提示不各写一遍）；
2. 有边界时一个字节都不加（Windows 上稳定前缀逐字不变）；
3. 默认仍然照常跑（保持既有行为，不偷偷改成拒绝）；
4. 用户可以要求"没有边界就别执行"——`security.require_sandbox`，那时**必须抛错**而不是返回 None
   （返回 None 在调用点等于"进沙箱"，两者长得一样，是最难查的那类错）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from local_agent import platform_caps
from local_agent.config import AppConfig
from local_agent.context.prompt_context import build_stable_system_prompt
from local_agent.security import policy as policy_module
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SANDBOX_BOUNDARY_NOTE, SecurityError, SecurityPolicy


def _policy(tmp_path: Path, *, require_sandbox: bool) -> SecurityPolicy:
    return SecurityPolicy.from_config(
        AppConfig.model_validate({"workspace": {"root": str(tmp_path)}}),
        workspace_root=tmp_path,
        environ={},
        external_policy=ExternalPathPolicy(mode="workspace-write"),
        require_sandbox=require_sandbox,
    )


def _prompt(policy: SecurityPolicy, tmp_path: Path) -> str:
    """只用到 `RuntimeContext` 的几个字段，用替身足够（函数按鸭子类型读）。"""
    context: Any = SimpleNamespace(
        workspace_root=tmp_path,
        agents_context=None,
        curated_snapshot={},
        curated_available=False,
        agent_capabilities=("files_read", "shell"),
        security_policy=policy,
    )
    return build_stable_system_prompt(mode="tool", context=context).system_prompt


def test_note_is_one_shared_string(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(policy_module.win_sandbox, "is_supported", lambda: False)
    monkeypatch.setattr(platform_caps, "writable_sandbox_available", lambda: False)

    policy = _policy(tmp_path, require_sandbox=False)

    assert policy.sandbox_boundary_note() == SANDBOX_BOUNDARY_NOTE
    # 说明里必须同时讲清"不是内核强制"和"怎么改成拒绝"——只说前者的告警等于废话。
    assert "不是内核强制" in SANDBOX_BOUNDARY_NOTE
    assert "require_sandbox" in SANDBOX_BOUNDARY_NOTE


def test_default_still_runs_the_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """默认行为不变：没有边界照常跑（2026-10-15 的裁定），只是现在会被说出来。"""
    monkeypatch.setattr(policy_module.win_sandbox, "is_supported", lambda: False)
    monkeypatch.setattr(platform_caps, "writable_sandbox_available", lambda: False)

    policy = _policy(tmp_path, require_sandbox=False)

    assert policy.shell_sandbox_request() is None


def test_require_sandbox_refuses_instead_of_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(policy_module.win_sandbox, "is_supported", lambda: False)
    monkeypatch.setattr(platform_caps, "writable_sandbox_available", lambda: False)

    policy = _policy(tmp_path, require_sandbox=True)

    with pytest.raises(SecurityError) as excinfo:
        policy.shell_sandbox_request()
    message = str(excinfo.value)
    assert "require_sandbox" in message
    assert "拒绝" in message


def test_from_config_reads_the_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """配置开了就必须生效：`from_config` 是生产路径，参数默认 `None` 跟随 `config.yaml`。

    这条守的是"开关接好了但没人读"——上一轮真机验收里 `workflows.enabled` 就是这样被我漏掉的。
    """
    monkeypatch.setattr(policy_module.win_sandbox, "is_supported", lambda: False)
    monkeypatch.setattr(platform_caps, "writable_sandbox_available", lambda: False)
    config = AppConfig.model_validate({
        "workspace": {"root": str(tmp_path)},
        "security": {"require_sandbox": True},
    })

    policy = SecurityPolicy.from_config(
        config, workspace_root=tmp_path, environ={},
        external_policy=ExternalPathPolicy(mode="workspace-write"),
    )

    assert policy.require_sandbox is True
    with pytest.raises(SecurityError):
        policy.shell_sandbox_request()


def test_prompt_states_the_missing_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(policy_module.win_sandbox, "is_supported", lambda: False)
    monkeypatch.setattr(platform_caps, "writable_sandbox_available", lambda: False)

    text = _prompt(_policy(tmp_path, require_sandbox=False), tmp_path)

    assert "## 执行边界" in text
    assert "不是内核强制" in text


def test_prompt_is_byte_identical_when_a_boundary_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """有边界时不加任何字节：Windows 上的稳定前缀（缓存基线）必须逐字不变。"""
    monkeypatch.setattr(policy_module.win_sandbox, "is_supported", lambda: True)
    monkeypatch.setattr(platform_caps, "writable_sandbox_available", lambda: True)

    text = _prompt(_policy(tmp_path, require_sandbox=False), tmp_path)

    assert "## 执行边界" not in text
    assert "不是内核强制" not in text

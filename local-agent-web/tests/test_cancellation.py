"""T60：取消令牌与它的两道门（架构 §17.4）。

取消**不新增拒绝分支**：它只是给既有的两道门换了一个输入——
`capabilities.is_visible(..., cancelled=True)` 与 `SecurityPolicy.classify_tool`。
本文件把这两道门单独钉住；"整轮一个工具都不执行"的端到端证明在 `tests/test_api.py`。
"""

from __future__ import annotations

from pathlib import Path

from local_agent.cancellation import DEFAULT_CANCEL_REASON, CancellationToken
from local_agent.capabilities import PROFILES, is_visible, tool_names_for
from local_agent.config import AppConfig
from local_agent.security.policy import SecurityPolicy


def test_token_is_idempotent_and_keeps_the_first_reason() -> None:
    token = CancellationToken()
    assert not token.cancelled and token.reason is None

    assert token.cancel("用户取消") is True
    assert token.cancelled and token.reason == "用户取消"
    # 第二次不生效，也**不覆盖**第一次的原因
    assert token.cancel("另一个原因") is False
    assert token.reason == "用户取消"
    assert "用户取消" in token.refusal_reason()


def test_token_without_reason_falls_back_to_the_default_wording() -> None:
    token = CancellationToken()
    token.cancel()

    assert token.reason is None
    assert token.refusal_reason() == DEFAULT_CANCEL_REASON


def test_visibility_door_refuses_every_tool_when_cancelled() -> None:
    mode = "tool"
    capabilities = PROFILES["coder"]
    names = tool_names_for(mode, capabilities)
    assert names, "对照组：未取消时这一档确实有工具"

    for name in names:
        assert is_visible(mode, capabilities, name) is True
        assert is_visible(mode, capabilities, name, cancelled=True) is False
    # 闲聊模式同样：取消后连只读工具也不可见
    assert is_visible("chat", None, "read_file", cancelled=True) is False


def test_security_door_denies_every_tool_when_cancelled(
    tmp_path: Path, app_config: AppConfig
) -> None:
    token = CancellationToken()
    policy = SecurityPolicy.from_config(
        app_config, workspace_root=tmp_path, cancellation=token
    )

    # 对照组：取消之前，只读工具是放行的
    before = policy.classify_tool("list_dir", {"path": "."})
    assert before.allowed and not before.requires_approval

    token.cancel("测试取消")
    for name, args in (
        ("list_dir", {"path": "."}),
        ("read_file", {"path": "a.txt"}),
        ("write_file", {"path": "a.txt", "content": "x"}),
        ("run_shell", {"command": "echo hi"}),
    ):
        decision = policy.classify_tool(name, args)
        assert decision.allowed is False, name
        # 取消是**拒绝**，不是"走审批"：取消后不该再弹审批卡
        assert decision.requires_approval is False, name
        assert "测试取消" in decision.reason


def test_policy_without_a_token_behaves_exactly_as_before(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """没挂令牌时（CLI、绝大多数测试）安全门的行为一字不变。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path)

    assert policy.cancellation is None
    assert policy.classify_tool("list_dir", {"path": "."}).allowed is True

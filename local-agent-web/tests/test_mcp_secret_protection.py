"""``mcp.json`` 里的凭据不许进模型上下文（2026-10-20）。

这条用例来自一次真实端到端验证：模型确实用 ``read_file`` 打开过 ``mcp.json``（它只是没把内容
说出去）。"这次没说"不是防护——文件里是 cookie 与 token，任何一次转述都是泄露。

两道口径一起守：文件名命中敏感模式，读入口直接拒绝（不是脱敏后放行）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.config import AppConfig
from local_agent.exceptions import SecurityError
from local_agent.security.path_guard import SENSITIVE_NAME_PATTERNS, is_sensitive_path
from local_agent.security.policy import SecurityPolicy


def _policy(app_config: AppConfig, workspace: Path) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})


def test_mcp_config_name_is_sensitive() -> None:
    assert "mcp.json" in SENSITIVE_NAME_PATTERNS
    assert is_sensitive_path(Path("mcp.json")) is True
    # 大小写不敏感：Windows 上 `MCP.JSON` 是同一个文件。
    assert is_sensitive_path(Path("MCP.JSON")) is True


def test_read_is_refused_instead_of_redacted(app_config: AppConfig, tmp_path: Path) -> None:
    """拒绝而不是"读了但打码"：打码要保证不漏一个字段，而拒绝只有一种结果。"""
    (tmp_path / "mcp.json").write_text(
        '{"mcpServers": {"x": {"command": "npx", "env": {"TOKEN": "secret"}}}}',
        encoding="utf-8",
    )
    policy = _policy(app_config, tmp_path)

    with pytest.raises(SecurityError) as excinfo:
        policy.resolve_readable_file("mcp.json")

    message = str(excinfo.value)
    assert "敏感" in message or "mcp.json" in message
    # 错误消息本身也不能带出文件内容。
    assert "secret" not in message


def test_other_json_files_are_still_readable(app_config: AppConfig, tmp_path: Path) -> None:
    """只挡这一个文件名：普通配置与数据文件不该被误伤。"""
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    policy = _policy(app_config, tmp_path)

    assert policy.resolve_readable_file("config.json").name == "config.json"
    assert policy.resolve_readable_file("package.json").name == "package.json"


def test_atomic_mcp_save_temporary_files_are_also_sensitive() -> None:
    assert is_sensitive_path(Path("mcp.json.a1b2c3d4.tmp"))

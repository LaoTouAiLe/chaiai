"""``mcp.json`` 的解析与校验（实施文档 M1）。

守住四件事：

1. **CodeBuddy 原样格式能被接受**——"现有配置整份粘过来"是这次接入的主要动机。
2. 错误**指到具体 server 与字段**，不只回一句"配置不合法"。
3. 不支持的传输（``sse``）有专门文案，而不是一句通用字段错误；``http`` 自 2026-11-02 起**支持**，
   两种传输各自的字段互斥由模型契约守住。
4. 解析阶段**没有任何副作用**（本模块不 import 进程相关的东西，这里用行为断言兜住）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_agent.config import ConfigError
from local_agent.mcp.config import (
    check_server_name,
    dump_mcp_servers,
    load_mcp_servers,
    save_mcp_servers,
)


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_codebuddy_format_is_accepted_as_is(tmp_path: Path) -> None:
    """本机 ``~/.codebuddy/mcp.json`` 的形状（值已替换）。"""
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "lanhu": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "mcp-lanhu"],
                    "env": {"LANHU_COOKIE": "placeholder"},
                }
            }
        },
    )

    config = load_mcp_servers(path)

    assert config.names() == ("lanhu",)
    spec = config.mcp_servers["lanhu"]
    assert spec.type == "stdio"
    assert spec.command == "npx"
    assert spec.args == ("-y", "mcp-lanhu")
    assert spec.env == {"LANHU_COOKIE": "placeholder"}
    # 不设白名单 = 全放（是否 Direct 由配额决定，不由这里决定）。
    assert spec.enabled_tools is None
    assert spec.disabled_tools == ()


def test_missing_file_is_an_empty_config_by_default(tmp_path: Path) -> None:
    config = load_mcp_servers(tmp_path / "absent.json")
    assert config.names() == ()


def test_missing_file_can_be_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="未找到 MCP 配置"):
        load_mcp_servers(tmp_path / "absent.json", allow_missing=False)


def test_empty_servers_object_is_valid(tmp_path: Path) -> None:
    assert load_mcp_servers(_write(tmp_path, {"mcpServers": {}})).names() == ()


def test_json_error_reports_line_and_column(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": {,}}', encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_mcp_servers(path)

    message = str(excinfo.value)
    assert "不是合法 JSON" in message
    assert "第 1 行" in message


def test_top_level_must_be_an_object(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="顶层必须是对象"):
        load_mcp_servers(_write(tmp_path, [1, 2, 3]))


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不认识的顶层键.*servers"):
        load_mcp_servers(_write(tmp_path, {"mcpServers": {}, "servers": {}}))


def test_server_name_must_be_safe_for_tool_names(tmp_path: Path) -> None:
    """名字要拼进 ``mcp__<server>__<tool>``，放行中文就只能在绑定时炸。"""
    path = _write(tmp_path, {"mcpServers": {"蓝图": {"command": "npx"}}})

    with pytest.raises(ConfigError) as excinfo:
        load_mcp_servers(path)

    assert "server 名" in str(excinfo.value)
    assert "mcp__<server>__<tool>" in str(excinfo.value)


def test_sse_transport_has_its_own_message(tmp_path: Path) -> None:
    """旧 ``sse`` 配置仍然常见：要直接说"已由 http 取代"，而不是报字段类型错。"""
    path = _write(
        tmp_path,
        {"mcpServers": {"remote": {"type": "sse", "url": "https://example.com/mcp"}}},
    )

    with pytest.raises(ConfigError) as excinfo:
        load_mcp_servers(path)

    message = str(excinfo.value)
    assert "stdio / http" in message
    assert "http 取代" in message


def test_http_server_is_accepted(tmp_path: Path) -> None:
    """远程 server 只走 http（2026-11-02）：url + headers，不启动任何本机进程。"""
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer placeholder"},
                }
            }
        },
    )

    spec = load_mcp_servers(path).mcp_servers["remote"]

    assert spec.type == "http"
    assert spec.url == "https://example.com/mcp"
    assert spec.command is None
    assert spec.headers == {"Authorization": "Bearer placeholder"}


@pytest.mark.parametrize(
    "body, fragment",
    [
        # 明文 http 只允许回环：远程明文等于把工具参数公开在网线上。
        ({"type": "http", "url": "http://example.com/mcp"}, "必须用 https"),
        # 凭据写在 URL 里会随日志与错误文案外泄。
        ({"type": "http", "url": "https://u:p@example.com/mcp"}, "不要把凭据写在 url 里"),
        # 两种传输的字段混着写，多半是从别处粘过来的配置。
        ({"type": "http", "url": "https://example.com/mcp", "command": "npx"}, "不接受 command"),
        ({"type": "http", "url": "https://example.com/mcp", "env": {"A": "b"}}, "args / env"),
        ({"type": "stdio", "command": "npx", "url": "https://example.com/mcp"}, "不接受 url"),
        ({"type": "stdio", "command": "npx", "headers": {"A": "b"}}, "不接受 headers"),
        # 头注入：值里带换行等于往请求里塞新头。
        (
            {"type": "http", "url": "https://example.com/mcp", "headers": {"A": "x\ny"}},
            "不能有换行",
        ),
    ],
)
def test_http_spec_contract_is_enforced(
    tmp_path: Path, body: dict[str, object], fragment: str
) -> None:
    path = _write(tmp_path, {"mcpServers": {"s": body}})

    with pytest.raises(ConfigError) as excinfo:
        load_mcp_servers(path)

    assert fragment in str(excinfo.value)


def test_http_spec_round_trips(tmp_path: Path) -> None:
    """写回文件时只写该传输的字段；读回来逐字相等，且不多出 command。"""
    path = _write(
        tmp_path,
        {"mcpServers": {"remote": {"type": "http", "url": "https://example.com/mcp"}}},
    )
    config = load_mcp_servers(path)

    save_mcp_servers(path, config)

    assert load_mcp_servers(path) == config
    assert "command" not in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["remote"]


def test_blank_command_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, {"mcpServers": {"s": {"command": "   "}}})

    with pytest.raises(ConfigError, match="command"):
        load_mcp_servers(path)


def test_blank_argument_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, {"mcpServers": {"s": {"command": "npx", "args": ["-y", ""]}}})

    with pytest.raises(ConfigError, match="args"):
        load_mcp_servers(path)


def test_env_key_must_be_an_environment_variable_name(tmp_path: Path) -> None:
    path = _write(
        tmp_path, {"mcpServers": {"s": {"command": "npx", "env": {"BAD-KEY": "x"}}}}
    )

    with pytest.raises(ConfigError, match="env"):
        load_mcp_servers(path)


def test_unknown_server_field_is_rejected(tmp_path: Path) -> None:
    """``commnd`` 这类拼写错误会表现成"server 起不来"，静默忽略更难查。"""
    path = _write(tmp_path, {"mcpServers": {"s": {"commnd": "npx"}}})

    with pytest.raises(ConfigError) as excinfo:
        load_mcp_servers(path)

    assert "commnd" in str(excinfo.value)


def test_allow_and_deny_lists_must_not_overlap(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "s": {
                    "command": "npx",
                    "enabled_tools": ["a", "b"],
                    "disabled_tools": ["b"],
                }
            }
        },
    )

    with pytest.raises(ConfigError, match="同时列出了"):
        load_mcp_servers(path)


def test_duplicate_tool_name_in_one_list_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"mcpServers": {"s": {"command": "npx", "enabled_tools": ["a", "a"]}}},
    )

    with pytest.raises(ConfigError, match="重复"):
        load_mcp_servers(path)


def test_server_order_follows_the_file(tmp_path: Path) -> None:
    """顺序即绑定顺序与配额分配顺序，不能被 dict 排序打乱。"""
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "zebra": {"command": "npx"},
                "alpha": {"command": "npx"},
                "middle": {"command": "npx"},
            }
        },
    )

    assert load_mcp_servers(path).names() == ("zebra", "alpha", "middle")


def test_saved_file_round_trips(tmp_path: Path) -> None:
    """写回后再读，语义完全一致；且不留下临时文件。"""
    path = _write(
        tmp_path,
        {"mcpServers": {"lanhu": {"type": "stdio", "command": "npx", "args": ["-y", "mcp-lanhu"]}}},
    )
    config = load_mcp_servers(path)

    save_mcp_servers(path, config)

    assert load_mcp_servers(path) == config
    assert [item.name for item in tmp_path.iterdir()] == ["mcp.json"]


def test_check_server_name_rejects_unsafe_names() -> None:
    assert check_server_name("lanhu-2") == "lanhu-2"
    with pytest.raises(ValueError, match="不合法"):
        check_server_name("蓝图")


def test_save_overwrites_atomically(tmp_path: Path) -> None:
    """写到一半失败时，原文件必须还在（不允许出现截断的 JSON）。"""
    path = _write(tmp_path, {"mcpServers": {}})
    original = path.read_text(encoding="utf-8")

    with pytest.raises(OSError):
        # 让临时文件写不进去：把目标目录换成一个同名文件，父目录创建会失败。
        save_mcp_servers(tmp_path / "mcp.json" / "nested", load_mcp_servers(path))

    assert path.read_text(encoding="utf-8") == original


def test_dump_round_trips(tmp_path: Path) -> None:
    payload = {
        "mcpServers": {
            "lanhu": {"type": "stdio", "command": "npx", "args": ["-y", "mcp-lanhu"]}
        }
    }
    config = load_mcp_servers(_write(tmp_path, payload))

    assert dump_mcp_servers(config) == payload


def test_duplicate_json_keys_and_non_utf8_are_rejected(tmp_path) -> None:
    from local_agent.config import ConfigError
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers":{"same":{"command":"a"},"same":{"command":"b"}}}')
    with pytest.raises(ConfigError, match="重复"):
        load_mcp_servers(path)
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError, match="UTF-8"):
        load_mcp_servers(path)

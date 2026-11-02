"""``mcp.json`` 的解析与校验（架构 §6.13）。

**格式与 CodeBuddy 一致**（顶层 ``mcpServers``，每项 ``type`` + ``command``/``args``/``env`` 或
``url``/``headers``），这样用户已有的配置可以整份粘过来，不改一字。与 CodeBuddy 的两处差异都是**加法**：

- ``enabled_tools`` / ``disabled_tools``：每 server 的工具白/黑名单（照 Codex 的形状）。
- **没有** ``enabled`` 字段：server 的开关是**用户状态**，存设置库，不写在这份文件里。
  写进文件会让"从别处复制的配置"带着别人的开关状态过来。

**解析阶段绝不启动进程**：本模块全程只读文件。启动进程在 M2 的连接管理里，且只对已启用的
server 做——50 条配置打开设置页就 spawn 50 个 node 是不可接受的。

**未知字段一律报错**（``extra="forbid"``）：MCP 配置里 ``commnd`` 这类拼写错误会表现为
"server 起不来"，而静默忽略会让用户以为配置生效了。宁可启动即失败。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from local_agent.config import ConfigError

#: 默认位置：与 ``config.yaml`` 同目录（装配层负责把两者放在一起）。
DEFAULT_MCP_PATH = Path("mcp.json")

#: server 名允许的字符集。**必须限制**：它要拼进工具名 ``mcp__<server>__<tool>``，
#: 而模型侧工具名只接受 ``[A-Za-z0-9_-]``；放行中文或空格会在绑定时才失败，那时已经离配置很远了。
_SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 环境变量名的合法形式（``npx`` 之类子进程的 ``env`` 键）。
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 本版支持的传输。
#:
#: 2026-11-02 加 ``http``（MCP 的 Streamable HTTP）：远程 MCP server 只能这样接，
#: ``stdio`` 那条路要求用户在本机装好运行时并让 server 作为子进程跑起来。
#: **SSE 那条旧传输不做**：它已被 Streamable HTTP 取代，两套都留等于永远要维护两套断线语义。
SUPPORTED_TRANSPORTS: tuple[str, ...] = ("stdio", "http")

#: 允许明文 HTTP 的主机名（回环）。与模型连接的判定同一条口径：明文只允许本机回环，
#: 别处一律要求 https——远程 MCP 的请求体里通常带工具参数，明文等于把它们公开在网线上。
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

#: HTTP 头名的合法形式（RFC 7230 的 token）。
_HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


class McpServerSpec(BaseModel):
    """一个 MCP Server 的**连接方式**（不含开关；开关在设置库）。

    两种传输共用一张模型，字段按 ``type`` 分开校验（见 :meth:`_transport_contract`）：

    - ``stdio``：``command`` 必填，``args`` / ``env`` 可选；
    - ``http``：``url`` 必填，``headers`` 可选（这里通常放 token）。

    **不用联合类型**：``command`` / ``url`` 在装配层与页面上都要读，拆成两个模型会让每一处
    都得先判断类型；而"哪个字段该有"本就是一条可校验的契约，写在模型里比散在调用点更可靠。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    #: 远程 server 的端点（``https://`` 或回环上的 ``http://``）。
    url: str | None = None
    #: 随每次请求发出的 HTTP 头。**值可能是凭据**：视图只回读键名，日志与错误文案里要脱敏。
    headers: dict[str, str] = Field(default_factory=dict)
    #: 显式白名单：设了就只有这些工具会出现（照 Codex ``enabled_tools``）。
    enabled_tools: tuple[str, ...] | None = None
    #: 黑名单：在白名单**之后**生效，即"先白后黑"。
    disabled_tools: tuple[str, ...] = ()

    @field_validator("command")
    @classmethod
    def _command_is_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("command 不能是空白字符串")
        return text

    @field_validator("url")
    @classmethod
    def _url_is_usable(cls, value: str | None) -> str | None:
        """只做**机械可判**的检查：协议、主机、凭据、端口。

        这里不联网、不解析 DNS——"这个地址现在通不通"不是配置校验该回答的问题，
        它由连接阶段以 ``kind`` 报出来（``KIND_HANDSHAKE_FAILED`` 等）。
        """
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("url 不能是空白字符串")
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"url 必须以 http:// 或 https:// 开头（当前是 {parsed.scheme or '空'}）")
        if not parsed.hostname:
            raise ValueError("url 里没有主机名")
        if parsed.username or parsed.password:
            # 凭据写在 URL 里会随日志/错误文案一起外泄，而且多数 server 也不看它。
            raise ValueError("不要把凭据写在 url 里：请改用 headers（例如 Authorization）")
        if parsed.scheme == "http" and parsed.hostname.lower() not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"{parsed.hostname} 不是本机回环地址：远程 server 必须用 https，"
                "明文 http 只允许 localhost / 127.0.0.1"
            )
        return text

    @field_validator("headers")
    @classmethod
    def _headers_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        for name, item in value.items():
            if not _HEADER_NAME_PATTERN.match(name):
                raise ValueError(f"请求头名 {name!r} 不合法（只允许字母、数字与连字符）")
            if "\n" in item or "\r" in item:
                # 头注入：值里带换行等于让用户往请求里塞新头。
                raise ValueError(f"请求头 {name!r} 的值里不能有换行")
        return value

    @model_validator(mode="after")
    def _transport_contract(self) -> McpServerSpec:
        """每种传输只接受自己那组字段：混着写多半是从别处粘贴过来的配置。"""
        if self.type == "stdio":
            if not self.command:
                raise ValueError("stdio 传输必须给 command")
            if self.url:
                raise ValueError("stdio 传输不接受 url（远程 server 请把 type 设为 http）")
            if self.headers:
                raise ValueError("stdio 传输不接受 headers（凭据请写进 env）")
        else:
            if not self.url:
                raise ValueError("http 传输必须给 url")
            if self.command:
                raise ValueError("http 传输不接受 command（它是要本机启动的子进程）")
            if self.args or self.env:
                raise ValueError("http 传输不接受 args / env（那是子进程的参数与环境）")
        return self

    @field_validator("args")
    @classmethod
    def _args_have_no_blank_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # 空串参数几乎总是多写了一个逗号，而它会让子进程收到一个意外的空 argv。
        for item in value:
            if item == "":
                raise ValueError("args 里有空字符串（多写了一个逗号？）")
        return value

    @field_validator("env")
    @classmethod
    def _env_keys_are_identifiers(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not _ENV_NAME_PATTERN.match(key):
                raise ValueError(f"env 的键 {key!r} 不是合法环境变量名")
        return value

    @field_validator("enabled_tools", "disabled_tools")
    @classmethod
    def _tool_names_are_not_blank(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        seen: set[str] = set()
        for name in value:
            if not name.strip():
                raise ValueError("工具名单里有空项")
            if name in seen:
                raise ValueError(f"工具名单里 {name!r} 重复出现")
            seen.add(name)
        return value

    @model_validator(mode="after")
    def _lists_do_not_overlap(self) -> McpServerSpec:
        """同时出现在白名单与黑名单里，是"以为开了却被黑"的经典来源。

        Codex 的口径是"先白后黑"，所以重叠在技术上有确定含义；但用户写重时多半是配置写错了，
        而这条错误只在"这个工具怎么不出现"时才显形。这里直接拒绝，让歧义无法进到运行期。
        """
        if self.enabled_tools is None:
            return self
        both = sorted(set(self.enabled_tools) & set(self.disabled_tools))
        if both:
            raise ValueError(
                "enabled_tools 与 disabled_tools 同时列出了：" + "、".join(both)
            )
        return self


class McpServersFile(BaseModel):
    """整份 ``mcp.json``：server 名 → 连接方式。**顺序即配置顺序**（前缀稳定的前提）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 与 CodeBuddy 相同的键名；**不接受** ``servers`` 这类别名，避免一份文件两种写法。
    mcp_servers: dict[str, McpServerSpec] = Field(default_factory=dict, alias="mcpServers")

    def names(self) -> tuple[str, ...]:
        """server 名（配置顺序）。"""
        return tuple(self.mcp_servers)


#: pydantic 常见错误类型的中文说法。**只翻这几类**：其余原样透出，不编造解释。
_ERROR_LABELS: Mapping[str, str] = {
    "extra_forbidden": "不认识的字段（拼写错误？）",
    "missing": "必填字段缺失",
    "string_too_short": "内容太短",
    "literal_error": "取值不在允许范围内",
}


def _format_server_error(display: str, server: str, exc: ValidationError) -> str:
    """把 pydantic 的错误翻成"哪个 server 的哪个字段错了"。

    **列出全部错误**而不是第一条：``commnd`` 这种拼写错误通常同时表现为"多了个字段"和
    "command 缺失"，只报后者会让用户去补 command 而不是去改拼写。
    """
    details: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        kind = str(error.get("type", ""))
        message = _ERROR_LABELS.get(kind) or str(error.get("msg", "字段不合法"))
        details.append(f"{location}（{message}）" if location else message)
    return f"{display} 的 mcpServers.{server} 不合法：" + "；".join(details)


def load_mcp_servers(
    path: Path = DEFAULT_MCP_PATH, *, allow_missing: bool = True
) -> McpServersFile:
    """读取并校验 ``mcp.json``。

    Args:
        path: 配置文件路径。
        allow_missing: 文件不存在时返回空配置（全新安装没有这份文件是正常状态）。

    Returns:
        校验通过的配置；server 顺序与文件里一致。

    Raises:
        ConfigError: 文件缺失（且未允许缺省）、JSON 语法错误、结构或字段不合法。
            消息可直接展示给用户，且**指明是哪个 server 的哪个字段**。
    """
    display = path.as_posix()
    if not path.is_file():
        if allow_missing:
            return McpServersFile()
        raise ConfigError(f"未找到 MCP 配置 {display}（可新建该文件并写入 mcpServers 段）")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取 {display}：{exc.strerror}") from exc
    except UnicodeError as exc:
        raise ConfigError(f"{display} 必须使用 UTF-8 编码") from exc

    try:
        def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ConfigError(f"{display} 存在重复的 JSON 键：{key}")
                result[key] = value
            return result
        raw = json.loads(text, object_pairs_hook=unique_keys)
    except json.JSONDecodeError as exc:
        # 行号列号必须给：这份文件是手写的，只回一句 "invalid JSON" 等于让用户自己数括号。
        raise ConfigError(
            f"{display} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列（{exc.msg}）"
        ) from exc

    if not isinstance(raw, Mapping):
        raise ConfigError(f"{display} 的顶层必须是对象（形如 {{ \"mcpServers\": {{...}} }}）")

    unknown_top = sorted(set(raw) - {"mcpServers"})
    if unknown_top:
        raise ConfigError(
            f"{display} 有不认识的顶层键：" + "、".join(unknown_top) + "（只接受 mcpServers）"
        )

    servers_raw = raw.get("mcpServers", {})
    if not isinstance(servers_raw, Mapping):
        raise ConfigError(f"{display} 的 mcpServers 必须是对象（server 名 → 连接配置）")

    specs: dict[str, McpServerSpec] = {}
    for name, body in servers_raw.items():
        if not isinstance(name, str) or not _SERVER_NAME_PATTERN.match(name):
            # 名字要进工具名，放行非法字符会在绑定时才失败。
            raise ConfigError(
                f"{display} 的 server 名 {name!r} 不合法：只允许字母、数字、下划线与连字符，"
                "长度 1–64（它会拼进工具名 mcp__<server>__<tool>）"
            )
        if not isinstance(body, Mapping):
            raise ConfigError(f"{display} 的 mcpServers.{name} 必须是对象")

        transport = body.get("type", "stdio")
        if transport not in SUPPORTED_TRANSPORTS:
            # 专门文案：从别处粘贴配置时，"类型不认识"比"字段不合法"有用得多。
            raise ConfigError(
                f"{display} 的 mcpServers.{name}.type={transport!r}："
                f"本版只支持 {' / '.join(SUPPORTED_TRANSPORTS)}"
                "（旧的 sse 传输已由 http 取代，请改成 type: http 并填 url）"
            )

        try:
            specs[name] = McpServerSpec.model_validate(dict(body))
        except ValidationError as exc:
            raise ConfigError(_format_server_error(display, name, exc)) from exc

    # 用 alias 校验构造（字段名是 ``mcp_servers``，对外键名是 ``mcpServers``）：
    # 直接传关键字会让类型检查把它当成未知参数。
    return McpServersFile.model_validate({"mcpServers": specs})


def check_server_name(name: str) -> str:
    """校验 server 名（与 :func:`load_mcp_servers` 同一套规则），返回原名。

    单独暴露是因为"页面新增一个 server"要在**写文件之前**给出可照做的错误，而不是等下次
    启动解析时才失败。

    Raises:
        ValueError: 名字不合法（消息可直接展示）。
    """
    if not _SERVER_NAME_PATTERN.match(name):
        raise ValueError(
            f"server 名 {name!r} 不合法：只允许字母、数字、下划线与连字符，长度 1–64"
            "（它会拼进工具名 mcp__<server>__<tool>）"
        )
    return name


def spec_to_body(spec: McpServerSpec) -> dict[str, Any]:
    """一台 server → 写回文件的结构（**空字段不输出**，往返后逐字相等）。

    两种传输各写各的字段：把 ``url`` 写进 stdio 项会让下一次解析直接报错（那是设计如此），
    所以这里不能"全字段尝试输出"。
    """
    body: dict[str, Any] = {"type": spec.type}
    if spec.type == "http":
        body["url"] = spec.url
        if spec.headers:
            body["headers"] = dict(spec.headers)
    else:
        body["command"] = spec.command
        if spec.args:
            body["args"] = list(spec.args)
        if spec.env:
            body["env"] = dict(spec.env)
    if spec.enabled_tools is not None:
        body["enabled_tools"] = list(spec.enabled_tools)
    if spec.disabled_tools:
        body["disabled_tools"] = list(spec.disabled_tools)
    return body


def save_mcp_servers(path: Path, config: McpServersFile) -> None:
    """把配置**原子地**写回 ``mcp.json``。

    原子性用"同目录临时文件 + ``os.replace``"：直接覆盖时，如果进程在写到一半退出，
    用户会得到一份截断的 JSON——而这份文件里通常还存着凭据，恢复它比恢复代码麻烦得多。

    **不尝试保留原文件的排版**（JSON 没有注释，保留空白没有意义），但调用方必须先把
    未知/未改动的 server 合并进 ``config``——这个函数只负责"把给它的东西写下去"。

    Raises:
        OSError: 写盘失败（权限、磁盘满）。
    """
    payload = dump_mcp_servers(config)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件名带随机后缀：两个保存动作同时发生时不至于互相覆盖 tmp。
    tmp = path.with_name(f"{path.name}.{uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def dump_mcp_servers(config: McpServersFile) -> dict[str, Any]:
    """把配置还原成可写回文件的结构（供测试与"添加 server"表单复用）。

    **只给结构，不写文件**：写文件属于设置层的决定（要不要保留注释、要不要备份），
    这里不替它做。

    空字段不输出（``args``/``env``/``disabled_tools``）：写一堆 ``[]`` 只会让手写文件变吵，
    而且"往返后逐字相等"是这份文件能被安全改写的验收口径。
    """
    return {
        "mcpServers": {
            name: spec_to_body(spec) for name, spec in config.mcp_servers.items()
        }
    }

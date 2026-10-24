"""MCP 工具目录：命名规范化、三档暴露与检索（架构 §6.13）。

**纯函数 + 不可变数据**，不碰网络、不碰数据库、不读配置对象。三个理由：

1. 三档判定（``Direct`` / ``Deferred`` / ``Hidden``）是**验收对象**，必须能脱离真实 server 测。
2. 排序结果要**字节稳定**：工具名序列进系统前缀，顺序一变缓存整段失效（架构 §6.7）。
3. 检索将来可能换实现（关键词 → 向量），换的时候不该牵动连接层。

规模口径（四个不等于，见实施文档 §3）：配置 N 个 ≠ 连 N 个；连 N 个 ≠ 给模型 N×K 个；
给模型 N×K 个 ≠ 每轮都发；每轮都发 ≠ 全量进提示。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: 模型侧工具名上限（OpenAI 兼容接口的硬约束，超了绑定即失败）。
MAX_CANONICAL_LENGTH = 64

#: 规范名前缀。**唯一**的"外部工具"标识，安全门与能力归属都靠它识别。
CANONICAL_PREFIX = "mcp"

_SEPARATOR = "__"

#: 检索用的分词符：工具名常见 ``list_files`` / ``fetch-page`` / ``a.b`` 三种写法。
_QUERY_SPLIT = re.compile(r"[\s_\-./]+")


class ToolExposure(StrEnum):
    """工具对模型的可见档位。

    - ``DIRECT``：进初始工具表，模型直接看得到、直接调。
    - ``DEFERRED``：不进初始工具表，只能经检索被发现（规模控制的主力）。
    - ``HIDDEN``：连检索都不进（关闭的 server、黑名单、超大 schema）。
    """

    DIRECT = "direct"
    DEFERRED = "deferred"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """一个外部工具的**元数据快照**。

    ``input_schema`` 与 ``description`` 来自 server，是本系统无法审查的外部内容——它们会
    随工具定义进入模型上下文，因此既受字节闸约束，也在提示层被声明为不可信来源。
    """

    server: str
    #: server 报出的原始工具名（可能是 ``lanhu_design``，也可能含点号）。
    name: str
    #: 模型可见名（``mcp__<server>__<tool>``，超长已截断并加哈希）。
    canonical_name: str
    description: str
    input_schema: Mapping[str, Any]
    #: 描述 + schema 的 UTF-8 字节数（Codex 同口径：用字节而不是 token 估，生成时就知道）。
    schema_bytes: int


@dataclass(frozen=True, slots=True)
class ServerCatalog:
    """一台 server 的工具清单，**已按工具名升序**。"""

    server: str
    tools: tuple[ToolEntry, ...]


@dataclass(frozen=True, slots=True)
class ExposureRow:
    """一个工具的分档结果与**原因**（界面要能解释"为什么它不可见"）。"""

    entry: ToolEntry
    exposure: ToolExposure
    #: 分档原因；``DIRECT`` / ``DEFERRED`` 时也为空或说明。
    reason: str | None = None


#: ``ExposureRow.reason`` 的闭集取值（DTO 直接复用，页面因此不会遇到没见过的原因）。
REASON_SERVER_DISABLED = "server_disabled"
REASON_NOT_IN_ENABLED_TOOLS = "not_in_enabled_tools"
REASON_IN_DISABLED_TOOLS = "in_disabled_tools"
REASON_SCHEMA_TOO_LARGE = "schema_too_large"
REASON_OVER_DIRECT_QUOTA = "over_direct_quota"




def _sanitize(fragment: str) -> str:
    """把任意片段收敛到模型侧工具名允许的字符集。

    非 ASCII 与标点一律变 ``_``：``isalnum()`` 这里只会命中 ASCII（``isascii()`` 先行），
    所以中文工具名会变成下划线而不是保留——保留会让绑定直接失败。
    """
    cleaned = "".join(
        char if (char.isascii() and (char.isalnum() or char in "_-")) else "_"
        for char in fragment
    )
    return cleaned or "_"


def _short_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def canonical_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>``，超过 64 字符时截断并追加 8 位哈希。

    哈希是**必需**的而不是美化：两个长名字截断到同一前缀时若不加区分，后一个会覆盖前一个——
    表现为"某个工具怎么调都是另一个工具的结果"。
    """
    base = f"{CANONICAL_PREFIX}{_SEPARATOR}{_sanitize(server)}{_SEPARATOR}{_sanitize(tool)}"
    ambiguous = "__" in server or "__" in tool
    if len(base) <= MAX_CANONICAL_LENGTH and not ambiguous:
        return base
    digest = _short_digest(server + "\0" + tool)
    if len(base) <= MAX_CANONICAL_LENGTH - 9:
        return f"{base}_{digest}"
    head = base[: MAX_CANONICAL_LENGTH - len(digest) - 1]
    return f"{head}_{digest}"


def schema_bytes(description: str, input_schema: Mapping[str, Any]) -> int:
    """工具定义的字节数（描述 + schema），用于字节闸与界面展示。"""
    try:
        payload = json.dumps(
            input_schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    except (TypeError, ValueError):
        # server 报了个不可序列化的 schema：按"最坏情况"计，宁可被字节闸挡下，
        # 也不要在绑定模型时才炸。
        payload = repr(input_schema)
    return len(payload.encode("utf-8")) + len(description.encode("utf-8"))


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_schema(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def build_server_catalog(server: str, raw_tools: Sequence[Mapping[str, Any]]) -> ServerCatalog:
    """由 ``tools/list`` 的原始条目构造目录。

    - 缺名或名字非字符串的条目被**跳过**：没有名字就无法被调用，留一个占位名只会让工具表
      多出一个永远调不动的条目。
    - 先按原始工具名排序，再为规范化冲突追加哈希后缀；同名条目只保留一条。
    - 最终按**原始工具名升序**排序：这是"重启后结果一致"的前提。
    """
    used: set[str] = set()
    entries: list[ToolEntry] = []
    seen: set[str] = set()
    for raw in sorted(raw_tools, key=lambda item: _as_text(item.get("name"))):
        raw_name = raw.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        name = raw_name
        if name in seen:
            continue
        seen.add(name)
        canonical = canonical_tool_name(server, name)
        base = canonical
        collision = 0
        while canonical in used:
            collision += 1
            digest = _short_digest(f"{name}\0{collision}")
            canonical = f"{base[: MAX_CANONICAL_LENGTH - 9]}_{digest}"
        used.add(canonical)
        description = _as_text(raw.get("description"))
        schema = _as_schema(raw.get("inputSchema") or raw.get("input_schema"))
        entries.append(
            ToolEntry(
                server=server,
                name=name,
                canonical_name=canonical,
                description=description,
                input_schema=schema,
                schema_bytes=schema_bytes(description, schema),
            )
        )
    entries.sort(key=lambda entry: (entry.name, entry.canonical_name))
    return ServerCatalog(server=server, tools=tuple(entries))


def expose_all(
    servers: Sequence[ServerCatalog],
    *,
    enabled: Mapping[str, bool],
    enabled_tools: Mapping[str, Sequence[str] | None] | None = None,
    disabled_tools: Mapping[str, Sequence[str]] | None = None,
    max_direct: int = 20,
    max_schema_bytes: int = 8_192,
) -> tuple[ExposureRow, ...]:
    """按固定顺序把全部工具分档。

    判定顺序（**不能改**，它决定了"同一份配置每次得到同一张工具表"）：

    1. server 开关关 → 整台 ``HIDDEN``
    2. 配了白名单而工具不在其中 → ``HIDDEN``
    3. 命中黑名单 → ``HIDDEN``
    4. 描述+schema 超过字节闸 → ``HIDDEN``
    5. 仍有余量 → ``DIRECT``（余量**跨 server 累计**）
    6. 其余 → ``DEFERRED``

    Args:
        servers: 目录，**顺序即 server 配置顺序**。
        enabled: server 名 → 是否启用（缺省视为关：失败闭合）。
        enabled_tools: server 名 → 白名单；``None`` 表示不设限。
        disabled_tools: server 名 → 黑名单。
        max_direct: Direct 总配额。0 表示全部走检索。
        max_schema_bytes: 单条工具定义的字节上限。

    Returns:
        按输入顺序排列的分档结果（同输入恒得同输出）。
    """
    allow_map = enabled_tools or {}
    deny_map = disabled_tools or {}
    remaining = max(0, max_direct)
    rows: list[ExposureRow] = []
    for catalog in servers:
        if not enabled.get(catalog.server, False):
            rows.extend(
                ExposureRow(entry=entry, exposure=ToolExposure.HIDDEN, reason=REASON_SERVER_DISABLED)
                for entry in catalog.tools
            )
            continue
        allow = allow_map.get(catalog.server)
        allow_set = None if allow is None else set(allow)
        deny_set = set(deny_map.get(catalog.server, ()))
        for entry in catalog.tools:
            if allow_set is not None and entry.name not in allow_set:
                rows.append(
                    ExposureRow(
                        entry=entry,
                        exposure=ToolExposure.HIDDEN,
                        reason=REASON_NOT_IN_ENABLED_TOOLS,
                    )
                )
                continue
            if entry.name in deny_set:
                rows.append(
                    ExposureRow(
                        entry=entry, exposure=ToolExposure.HIDDEN, reason=REASON_IN_DISABLED_TOOLS
                    )
                )
                continue
            if entry.schema_bytes > max_schema_bytes:
                rows.append(
                    ExposureRow(
                        entry=entry, exposure=ToolExposure.HIDDEN, reason=REASON_SCHEMA_TOO_LARGE
                    )
                )
                continue
            if remaining > 0:
                remaining -= 1
                rows.append(ExposureRow(entry=entry, exposure=ToolExposure.DIRECT))
                continue
            rows.append(
                ExposureRow(
                    entry=entry, exposure=ToolExposure.DEFERRED, reason=REASON_OVER_DIRECT_QUOTA
                )
            )
    return tuple(rows)


def direct_names(rows: Sequence[ExposureRow]) -> tuple[str, ...]:
    """``Direct`` 档的规范名（**顺序即绑定顺序**）。"""
    return tuple(row.entry.canonical_name for row in rows if row.exposure is ToolExposure.DIRECT)


def deferred_entries(rows: Sequence[ExposureRow]) -> tuple[ToolEntry, ...]:
    """可被检索发现的工具（``DEFERRED``）；``HIDDEN`` 永远不出现。"""
    return tuple(row.entry for row in rows if row.exposure is ToolExposure.DEFERRED)


def search(
    entries: Sequence[ToolEntry], query: str, *, limit: int = 8
) -> tuple[ToolEntry, ...]:
    """按关键词检索工具（第一版不做向量）。

    打分是**可解释**的：整串命中规范名最高，其次是逐词命中名字、描述、server 名。
    一个词都命不中的工具不入选——"返回最像的 8 个"会让模型以为库里有它要的东西。

    Args:
        entries: 候选（通常是 ``deferred_entries`` 的结果）。
        query: 模型给的检索词。
        limit: 最多返回几条（0 或负数返回空）。

    Returns:
        按分数降序、同分按规范名升序；空查询返回空元组。
    """
    text = query.strip().lower()
    if not text or limit <= 0:
        return ()
    words = [word for word in _QUERY_SPLIT.split(text) if word]
    if not words:
        return ()

    scored: list[tuple[int, str, ToolEntry]] = []
    for entry in entries:
        name = entry.name.lower()
        canonical = entry.canonical_name.lower()
        description = entry.description.lower()
        server = entry.server.lower()
        score = 0
        if text in canonical:
            score += 100
        for word in words:
            if word in name:
                score += 40
            if word in description:
                score += 10
            if word in server:
                score += 5
        if score <= 0:
            continue
        scored.append((score, canonical, entry))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(entry for _, _, entry in scored[:limit])

"""MCP 工具目录：命名规范化、三档暴露与检索（实施文档 M1）。

这组用例守住的是**规模与稳定性**，不是"能连上"：

1. 工具名进模型侧前被规范化，超长截断后仍**两两不同**（截断重合会表现为"调 A 得 B"）。
2. 同一份目录重复分档，结果**逐项一致**（前缀缓存的前提）。
3. Direct 配额**跨 server 累计**，且按配置顺序消耗——不是每台 server 各给 20。
4. 检索一个词都命不中时返回空，而不是"最像的 8 个"。
"""

from __future__ import annotations

from typing import Any

from local_agent.mcp.catalog import (
    MAX_CANONICAL_LENGTH,
    REASON_IN_DISABLED_TOOLS,
    REASON_NOT_IN_ENABLED_TOOLS,
    REASON_OVER_DIRECT_QUOTA,
    REASON_SCHEMA_TOO_LARGE,
    REASON_SERVER_DISABLED,
    ToolExposure,
    build_server_catalog,
    canonical_tool_name,
    deferred_entries,
    direct_names,
    expose_all,
    schema_bytes,
    search,
)


def _raw(name: str, description: str = "", schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema if schema is not None else {"type": "object", "properties": {}},
    }


# --- 命名 -------------------------------------------------------------------


def test_canonical_name_shape() -> None:
    assert canonical_tool_name("lanhu", "lanhu_design") == "mcp__lanhu__lanhu_design"


def test_illegal_characters_become_underscores() -> None:
    # 点号与中文都不能进模型侧工具名。
    assert canonical_tool_name("my.server", "设计 工具") == "mcp__my_server_______"


def test_long_names_are_truncated_and_stay_distinct() -> None:
    long_a = "a" * 80
    long_b = "a" * 79 + "b"

    first = canonical_tool_name("s", long_a)
    second = canonical_tool_name("s", long_b)

    assert len(first) <= MAX_CANONICAL_LENGTH
    assert len(second) <= MAX_CANONICAL_LENGTH
    # 截断重合就会"调 A 得 B"，哈希后缀是必需的。
    assert first != second


# --- 目录 -------------------------------------------------------------------


def test_catalog_sorts_tools_by_name() -> None:
    catalog = build_server_catalog("s", [_raw("zeta"), _raw("alpha"), _raw("mike")])
    assert [entry.name for entry in catalog.tools] == ["alpha", "mike", "zeta"]


def test_unnamed_tools_are_skipped() -> None:
    """没有名字就无法被调用，留占位名只会让工具表多一个调不动的条目。"""
    catalog = build_server_catalog(
        "s", [{"description": "no name"}, {"name": "   "}, _raw("ok")]
    )
    assert [entry.name for entry in catalog.tools] == ["ok"]


def test_colliding_sanitized_names_get_a_suffix() -> None:
    catalog = build_server_catalog("s", [_raw("a.b"), _raw("a_b")])
    names = [entry.canonical_name for entry in catalog.tools]

    assert len(names) == len(set(names))
    assert names[0] == "mcp__s__a_b"
    assert names[1] != names[0]
    assert len(names[1]) <= MAX_CANONICAL_LENGTH


def test_schema_bytes_counts_description_and_schema() -> None:
    bare = schema_bytes("", {"type": "object"})
    with_description = schema_bytes("说明" * 10, {"type": "object"})
    assert with_description > bare


# --- 三档 -------------------------------------------------------------------


def _catalog(server: str, count: int, *, prefix: str = "tool") -> Any:
    return build_server_catalog(
        server, [_raw(f"{prefix}{index:02d}", description="d") for index in range(count)]
    )


def test_disabled_server_hides_everything() -> None:
    rows = expose_all([_catalog("s", 3)], enabled={"s": False})

    assert {row.exposure for row in rows} == {ToolExposure.HIDDEN}
    assert {row.reason for row in rows} == {REASON_SERVER_DISABLED}
    assert direct_names(rows) == ()
    assert deferred_entries(rows) == ()


def test_enabled_is_required_explicitly() -> None:
    """``enabled`` 缺省视为关：失败闭合，不是"没说就是开"。"""
    rows = expose_all([_catalog("s", 2)], enabled={})
    assert {row.exposure for row in rows} == {ToolExposure.HIDDEN}


def test_whitelist_hides_the_rest() -> None:
    rows = expose_all(
        [_catalog("s", 3)],
        enabled={"s": True},
        enabled_tools={"s": ["tool01"]},
    )

    direct = [row for row in rows if row.exposure is ToolExposure.DIRECT]
    hidden = [row for row in rows if row.exposure is ToolExposure.HIDDEN]

    assert [row.entry.name for row in direct] == ["tool01"]
    assert {row.reason for row in hidden} == {REASON_NOT_IN_ENABLED_TOOLS}


def test_blacklist_hides_matching_tools() -> None:
    rows = expose_all(
        [_catalog("s", 3)],
        enabled={"s": True},
        disabled_tools={"s": ["tool02"]},
    )

    assert [row.entry.name for row in rows if row.exposure is ToolExposure.HIDDEN] == ["tool02"]
    assert {row.reason for row in rows if row.exposure is ToolExposure.HIDDEN} == {
        REASON_IN_DISABLED_TOOLS
    }


def test_schema_gate_hides_oversized_tools() -> None:
    catalog = build_server_catalog(
        "s",
        [_raw("small"), _raw("huge", description="x" * 500)],
    )

    rows = expose_all([catalog], enabled={"s": True}, max_schema_bytes=100)

    hidden = [row for row in rows if row.exposure is ToolExposure.HIDDEN]
    assert [row.entry.name for row in hidden] == ["huge"]
    assert hidden[0].reason == REASON_SCHEMA_TOO_LARGE


def test_direct_quota_is_shared_across_servers() -> None:
    """配额是全局的：A 用掉 15 个，B 只剩 5 个 Direct。"""
    first = _catalog("a", 15)
    second = _catalog("b", 10)

    rows = expose_all([first, second], enabled={"a": True, "b": True}, max_direct=20)

    assert len(direct_names(rows)) == 20
    # A 用掉 15 个名额，B 只剩 5 个：B 的 tool00–tool04 是 Direct，其余进检索。
    assert [row.entry.name for row in rows if row.exposure is ToolExposure.DIRECT][-5:] == [
        "tool00",
        "tool01",
        "tool02",
        "tool03",
        "tool04",
    ]
    deferred = deferred_entries(rows)
    assert [entry.name for entry in deferred] == [
        "tool05",
        "tool06",
        "tool07",
        "tool08",
        "tool09",
    ]
    assert {row.reason for row in rows if row.exposure is ToolExposure.DEFERRED} == {
        REASON_OVER_DIRECT_QUOTA
    }


def test_zero_quota_sends_everything_to_search() -> None:
    rows = expose_all([_catalog("s", 3)], enabled={"s": True}, max_direct=0)
    assert direct_names(rows) == ()
    assert len(deferred_entries(rows)) == 3


def test_exposure_is_stable_across_calls() -> None:
    """同一份目录重复分档必须逐项一致——顺序一变，系统前缀缓存整段失效。"""
    servers = [_catalog("a", 25), _catalog("b", 25)]
    first = expose_all(servers, enabled={"a": True, "b": True}, max_direct=20)
    second = expose_all(servers, enabled={"a": True, "b": True}, max_direct=20)

    assert [(row.entry.canonical_name, row.exposure) for row in first] == [
        (row.entry.canonical_name, row.exposure) for row in second
    ]


def test_direct_names_follow_configuration_order() -> None:
    first = _catalog("a", 2)
    second = _catalog("b", 2)

    rows = expose_all([second, first], enabled={"a": True, "b": True}, max_direct=10)

    assert direct_names(rows) == (
        "mcp__b__tool00",
        "mcp__b__tool01",
        "mcp__a__tool00",
        "mcp__a__tool01",
    )


# --- 检索 -------------------------------------------------------------------


def _searchable() -> tuple[Any, ...]:
    return (
        build_server_catalog(
            "lanhu",
            [
                _raw("lanhu_design", "读取设计稿"),
                _raw("lanhu_page", "读取页面标注"),
                _raw("lanhu_resolve_invite_link", "解析邀请链接"),
            ],
        ).tools
    )


def test_search_matches_name_and_description() -> None:
    entries = _searchable()

    assert [entry.name for entry in search(entries, "design")] == ["lanhu_design"]
    assert [entry.name for entry in search(entries, "设计稿")] == ["lanhu_design"]


def test_search_returns_nothing_when_no_word_matches() -> None:
    """返回"最像的 8 个"会让模型以为库里有它要的东西。"""
    assert search(_searchable(), "kubernetes") == ()


def test_search_on_empty_query_returns_nothing() -> None:
    assert search(_searchable(), "   ") == ()


def test_search_respects_limit() -> None:
    entries = _searchable()
    assert len(search(entries, "lanhu", limit=2)) == 2
    assert search(entries, "lanhu", limit=0) == ()


def test_separator_ambiguity_cannot_collide_across_servers() -> None:
    assert canonical_tool_name("a__b", "c") != canonical_tool_name("a", "b__c")


def test_catalog_names_are_stable_when_server_reorders_collisions() -> None:
    raw = [_raw("a.b"), _raw("a_b"), _raw("a_b"), _raw("a/b")]
    assert build_server_catalog("s", raw) == build_server_catalog("s", list(reversed(raw)))
    catalog = build_server_catalog("s", raw)
    assert len(catalog.tools) == 3
    assert len({entry.canonical_name for entry in catalog.tools}) == 3

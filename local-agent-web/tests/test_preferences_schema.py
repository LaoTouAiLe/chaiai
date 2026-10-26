"""设置页字段清单的反射（2026-10-26）。

这份清单存在的唯一理由是**让"后端加了可改字段、页面没接"变成一次失败的测试**。所以这里
盯的不是"函数能跑"，而是三件会让它失去作用的事：

1. 少一组或漏字段（清单不全 → 页面照旧看不到）；
2. 类型/边界反射错（页面允许一个保存必然 422 的值）；
3. 枚举候选漏掉新值（用户选不到后端已经支持的后端，2026-10-26 的 `deepseek` 就是这一类）。
"""

from __future__ import annotations

from local_agent.model_settings.preferences import RuntimePreferences
from local_agent.model_settings.preferences_schema import GROUP_ORDER, preferences_schema


def test_schema_covers_every_group_and_every_field() -> None:
    """组与字段必须与 ``RuntimePreferences`` **逐个对上**，不多不少。"""
    schema = preferences_schema()

    assert [group.group for group in schema.groups] == list(GROUP_ORDER)
    for group in schema.groups:
        sub_model, _nullable = _unwrap(RuntimePreferences.model_fields[group.group].annotation)
        assert [field.key for field in group.fields] == list(sub_model.model_fields), group.group


def _unwrap(annotation):
    """和被测模块同一套拆法：四组偏好是 ``X | None``（没保存过时为空）。"""
    import types
    from typing import Union, get_args, get_origin

    if get_origin(annotation) in {Union, types.UnionType}:
        args = [item for item in get_args(annotation) if item is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def test_nullable_and_exclusive_bounds_survive_the_reflection() -> None:
    """``int | None`` 与 ``Field(gt=0)`` 的组合最容易反射错。

    ``max_tool_loops`` 是 "留空表不限、填了必须 ≥1"。把它反射成 ``minimum=0``（丢掉
    "严格"）会让页面允许 0，而保存时被 422 拒绝——那正是页面与模型漂移的典型症状。
    """
    fields = {field.key: field for group in preferences_schema().groups for field in group.fields}

    loops = fields["max_tool_loops"]
    assert loops.kind == "int"
    assert loops.nullable is True
    assert loops.minimum == 0.0 and loops.minimum_exclusive is True

    retries = fields["max_retries_per_step"]
    assert retries.nullable is False and retries.minimum_exclusive is False

    # 上下界都有的字段：两边都要保住
    chars = fields["max_chars"]
    assert (chars.minimum, chars.maximum) == (500.0, 100_000.0)


def test_enums_carry_every_accepted_value() -> None:
    """枚举字段的候选必须与闭集一致——页面下拉少一项，用户就选不到那个后端。"""
    fields = {field.key: field for group in preferences_schema().groups for field in group.fields}

    provider = fields["provider"]
    assert provider.kind == "choice"
    assert provider.options == ("auto", "tavily", "deepseek", "bing_html", "duckduckgo_html")

    assert fields["fetch_provider"].options == ("local", "deepseek")
    assert fields["domain_policy"].options == ("allow", "allowlist")


def test_text_like_fields_are_not_mistaken_for_arrays() -> None:
    """``tuple[str, ...]``（域名清单）在页面上是**一行逗号分隔文本**，不是数组控件。"""
    fields = {field.key: field for group in preferences_schema().groups for field in group.fields}

    assert fields["denied_domains"].kind == "text"
    assert fields["denied_domains"].nullable is False
    assert fields["oss_endpoint"].kind == "text"

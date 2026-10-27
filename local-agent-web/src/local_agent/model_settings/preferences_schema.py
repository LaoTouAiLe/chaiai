"""把 ``RuntimePreferences`` 反射成"设备页能改什么"的权威清单（2026-10-26）。

## 为什么要有这个文件

设置页的字段表（前端 `FIELDS`）和偏好模型（后端 `RuntimePreferences`）是**两份手写清单**，
它们必须一致，但此前**没有任何机制能发现不一致**：后端加一个可改字段，页面不会出现输入框，
用户改不了，测试也全绿。这类"加了但没接上"的缺陷只能靠人肉记忆。

这里把后端的机械信息（键、类型、上下界、可选项、是否允许留空）反射出来，作为一份可导出的
契约（`docs/api/preferences-schema.json`）。前端测试拿它比对 `FIELDS`，漏一个就红。

## 边界：这里**不**产出文案

`label` / `note` / 条件显示（如"选对象存储后才出现端点输入框"）是**产品语义**，不是类型信息，
pydantic 推不出来。它们仍写在前端字段表里。所以本模块的目标不是"加字段零改动"，而是
**"加了字段会被立刻发现"**——把沉默的遗漏变成一次失败的测试。

## 刻意不做的事

- 不导出 `default`：默认值会随产品调整，把它写进契约会诱使调用方把它当"当前值"用；
  当前值走 `GET /preferences`。
- 不导出 `general` 组：那几项（主题、字号、动画、快捷键）是**前端本地偏好**，不在
  `RuntimePreferences` 里，也不进设置库。
"""

from __future__ import annotations

import types
from typing import Any, Literal, Union, get_args, get_origin

from local_agent.application.models import (
    PreferenceFieldView,
    PreferenceGroupView,
    PreferencesSchemaView,
)
from local_agent.model_settings.preferences import RuntimePreferences

#: 反射格式的版本。字段形状变了就 +1，消费方据此判断"这份清单我读不读得懂"。
#: 与 `RuntimePreferences` 的字段增减**无关**——那不需要改版本。
SCHEMA_VERSION = 1

#: 固定顺序：与设置页左栏的分组顺序一致。`RuntimePreferences` 的字段顺序变化不影响这里，
#: 否则一次无害的字段重排就会让导出文件整体 diff。
GROUP_ORDER: tuple[str, ...] = (
    "execution",
    "agents",
    "retrieval",
    "memory",
    "medium_term",
    # 常驻记忆（2026-10-27）：排在中期之后、压缩之前——按"必到 → 按需 → 压缩"的阅读顺序。
    "curated",
    "compression",
    "embedding",
    "web_access",
    "attachments",
)


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """拆开 ``X | None``：返回 (X, 是否可空)。

    只拆一层：``int | None`` 是设置页真实存在的形状（"留空表示不限"）。更深的联合类型
    在这里没有语义，原样返回并视为不可空，让消费方按 `text` 处理而不是猜。
    """
    origin = get_origin(annotation)
    if origin in {Union, types.UnionType}:
        args = [item for item in get_args(annotation) if item is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _kind(annotation: Any) -> tuple[Literal["bool", "int", "float", "text", "choice"], tuple[str, ...]]:
    """把 Python 注解归成设置页认识的五种输入，枚举顺带给出候选项。"""
    origin = get_origin(annotation)
    if origin is Literal:
        return "choice", tuple(str(item) for item in get_args(annotation))
    if annotation is bool:
        return "bool", ()
    if annotation is int:
        return "int", ()
    if annotation is float:
        return "float", ()
    # ``tuple[str, ...]``（域名清单）在页面上就是一行逗号分隔文本，不是数组控件。
    return "text", ()


def _bounds(field: Any) -> tuple[float | None, float | None, bool, bool]:
    """取 ``Field(ge=…)`` 这些约束：返回 (最小值, 最大值, 最小是否严格, 最大是否严格)。

    pydantic v2 把它们放在 ``field.metadata`` 里（``annotated_types`` 的实例），而不是模型
    属性上——所以只能遍历。严格边界单独标出来：``gt=0`` 在整数上是"最小 1"，写成
    ``minimum=0`` 会让页面允许一个保存必然 422 的值。
    """
    low: float | None = None
    high: float | None = None
    low_exclusive = False
    high_exclusive = False
    for item in getattr(field, "metadata", ()):
        value = getattr(item, "ge", None)
        if value is not None:
            low, low_exclusive = float(value), False
        value = getattr(item, "gt", None)
        if value is not None:
            low, low_exclusive = float(value), True
        value = getattr(item, "le", None)
        if value is not None:
            high, high_exclusive = float(value), False
        value = getattr(item, "lt", None)
        if value is not None:
            high, high_exclusive = float(value), True
    return low, high, low_exclusive, high_exclusive


def _field_view(key: str, field: Any) -> PreferenceFieldView:
    annotation, nullable = _unwrap_optional(field.annotation)
    kind, options = _kind(annotation)
    minimum, maximum, minimum_exclusive, maximum_exclusive = _bounds(field)
    return PreferenceFieldView(
        key=key,
        kind=kind,
        options=options,
        minimum=minimum,
        maximum=maximum,
        minimum_exclusive=minimum_exclusive,
        maximum_exclusive=maximum_exclusive,
        nullable=nullable,
    )


def preferences_schema() -> PreferencesSchemaView:
    """当前偏好模型的完整可改清单，按固定分组顺序排列。

    Returns:
        ``PreferencesSchemaView``：分组 → 字段（键、输入类型、上下界、候选项、是否可空）。

    Raises:
        ImportError: 分组名与 :data:`GROUP_ORDER` 对不上时（改了 `RuntimePreferences` 的
            字段名却忘了同步这里）。**故意抛错而不是跳过**：静默跳过会让清单悄悄少一组，
            正是这个模块要消灭的那类问题。
    """
    fields = RuntimePreferences.model_fields
    missing = [name for name in GROUP_ORDER if name not in fields]
    if missing:
        msg = f"RuntimePreferences 缺少分组 {missing}：GROUP_ORDER 与实际字段必须一致"
        raise ImportError(msg)
    groups: list[PreferenceGroupView] = []
    for group in GROUP_ORDER:
        # 四组是 ``X | None``（"从没保存过这一组"用 None 表示，读取时回落到默认）。可空是**整组**
        # 的空态，不改变字段本身的形状——所以要拆掉外层再反射。
        sub_model, _group_nullable = _unwrap_optional(fields[group].annotation)
        sub_fields = getattr(sub_model, "model_fields", None)
        if sub_fields is None:
            msg = f"RuntimePreferences.{group} 不是子模型，无法逐字段反射"
            raise ImportError(msg)
        groups.append(
            PreferenceGroupView(
                group=group,
                fields=tuple(_field_view(key, field) for key, field in sub_fields.items()),
            )
        )
    return PreferencesSchemaView(version=SCHEMA_VERSION, groups=tuple(groups))

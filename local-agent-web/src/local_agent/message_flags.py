"""消息级的**结构标记**（叶子模块，2026-10-20）。

为什么单独一个模块：同一个标记有三个消费者，分处三层——

- ``graph/nodes.py``（写入：工具读到的图片被拆成一条 user 消息时打标）；
- ``graph/projection.py``（读：找"本轮最后一条 AI 消息"时跳过它，否则它会被当成轮次边界）；
- ``memory/short_term.py``（读：按 ``HumanMessage`` 切轮次时跳过它，否则一轮会被算成两轮）；
- ``application/projection.py``（读：页面投影时跳过它，否则它会显示成一条用户发言）。

标记本身的字面量只能有一份：四处各写一遍，改一处漏三处就是"页面多一条假用户消息"这类缺陷。
它不导入任何项目内模块（叶子），因此谁都可以引用，不会形成依赖环。
"""

from __future__ import annotations

from typing import Any

#: ``additional_kwargs`` 里的键：这条 ``HumanMessage`` 承载的是**工具读到的图片**，
#: 不是用户发言。端点只允许 user 消息带图（tool / system / assistant 带图会 400），所以图片
#: 被包成 user 消息发出；但它在语义上属于上一条工具结果。
TOOL_IMAGE_FLAG = "local_agent_tool_image"

#: 出站请求默认保留图片的**最近轮数**（含当前轮）。可在「设置 → 应用偏好」里改（1~10）。
#: 定义放在这个叶子模块：设置层（`model_settings.preferences`）与图执行层（`graph`）都要用它，
#: 谁也不该为了一个默认值去依赖对方。
DEFAULT_IMAGE_CONTEXT_TURNS: int = 2


def is_tool_image_message(message: Any) -> bool:
    """这条消息是不是"工具读到的图片"（用鸭子类型读 ``additional_kwargs``，不导入消息类）。"""
    meta = getattr(message, "additional_kwargs", None) or {}
    return bool(meta.get(TOOL_IMAGE_FLAG))

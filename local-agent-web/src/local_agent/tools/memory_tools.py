"""常驻记忆工具：``MEMORY.md`` / ``USER.md`` 的读写入口（架构 §10.7，2026-10-27）。

模型拿到的只有三个动作：

- ``add``：追加一条；
- ``patch``：把某一条里的**一段文字**换掉，条目其余内容原样保留；
- ``remove``：删掉整条（按 ``old_text`` 定位）。

多个改动必须放进**一个** ``operations`` 批量：上限只在批量的最终结果上校验，所以"删旧 + 加新"
可以一次完成——这是"记忆满了还能改"的关键，也是参照实现的同一条规则。

失败封顶（参照实现同款）：一轮里累计 3 次整合失败（超限 / 匹配不到）之后返回终止性结果，
不再要求模型重试。原因很实际：模型会为了存一条记忆反复调用，把一轮的额度耗光，
用户只会看到"回答没出来"。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from local_agent.config import CuratedMemoryConfig
from local_agent.memory import curated

#: 一轮里允许的整合失败次数（超限、匹配不到都算）。超过就返回终止性结果。
MAX_CONSOLIDATION_FAILURES = 3

#: 两个 target 的说明（schema 里按开关收窄，模型因此不会写一个关掉的库）。
_TARGET_NOTE = {
    "memory": "「memory」= 我的笔记：环境事实、跨项目的约定、工具怪癖、踩过的坑",
    "user": "「user」= 用户画像：称呼、角色、偏好、沟通风格",
}

_DESCRIPTION = (
    "把**跨会话都要用**的少量事实存进常驻记忆。这两份内容会进入每一轮的系统提示，"
    "所以要短、要稳定、要去掉任务相关的过程。\n\n"
    "HOW：一次调用里改完一个 target。多改动放进 operations 批量（每项 {action, content?, old_text?}），"
    "整批原子生效，而且字符上限只校验**最终结果**——所以“删掉旧条目 + 加新条目”必须放在同一批，"
    "单独一个 add 在满库时会被拒绝。返回后不要重复调用。\n\n"
    "WHEN：只存对**每个会话**都成立的东西。做任务过程中学到、只对这个项目/这次工作有用的内容，"
    "不属于这里（那是记忆库与工作区文档的事）。\n\n"
    "FULL：返回里会带当前条目；请在同一次调用里删除或缩短旧条目再添加。\n\n"
    "TARGETS：memory = 我的笔记；user = 用户画像。\n\n"
    "SKIP：临时任务状态、进度日志、命令输出、已经在 AGENTS.md 或代码里写清的内容。"
)


class MemoryOperation(BaseModel):
    """批量里的一个操作。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "patch", "remove"]
    content: str | None = Field(default=None, description="add/patch 的新文字；patch 时替换 old_text 那一段。")
    new_text: str | None = Field(default=None, description="content 的别名（两个都给时以 content 为准）。")
    old_text: str | None = Field(default=None, description="patch/remove 用来定位条目的原文片段。")


class MemoryArgs(BaseModel):
    """工具入参：单动作形态与批量形态二选一。"""

    model_config = ConfigDict(extra="forbid")

    target: Literal["memory", "user"] = Field(description="写哪个 store。一次调用只能写一个。")
    action: Literal["add", "patch", "remove"] | None = Field(
        default=None, description="单动作形态；与 operations 二选一。")
    content: str | None = Field(default=None, description="add/patch 的新内容（完整条目或替换后的那段文字）。")
    old_text: str | None = Field(default=None, description="patch/remove 的定位片段，必须来自一条条目。")
    new_text: str | None = Field(default=None, description="content 的别名。")
    operations: list[MemoryOperation] | None = Field(
        default=None, description="批量形态：原子应用，上限只校验最终结果。")

    @model_validator(mode="after")
    def _require_one_shape(self) -> MemoryArgs:
        if self.operations is not None and not self.operations:
            raise ValueError("operations 不能是空列表；不需要改动就不要调用这个工具。")
        if self.operations is None and self.action is None:
            raise ValueError("请提供 action（add/patch/remove）或 operations 批量。")
        return self


def build_memory_tools(
    config: CuratedMemoryConfig,
    *,
    writes_allowed: bool = True,
    store: curated.CuratedStore | None = None,
) -> list[BaseTool]:
    """构造 ``memory`` 工具。

    Args:
        config: ``memory.curated`` 配置节（开关与预算）。
        writes_allowed: 本次运行是否允许写常驻记忆。``session_kind != interactive`` 的会话
            传 ``False``（架构 §10.7 的来源约束）——工具仍然存在，但调用会被明确拒绝。
        store: 注入用的 store（测试/CLI 可以指定目录）；省略时按配置解析。

    Returns:
        工具列表（当前只有一个 ``memory``）。
    """
    target_store = store or curated.CuratedStore(config, curated.resolve_directory(config))
    #: 一轮里的整合失败计数（成功一次就清零）；用可变容器是因为 ``@tool`` 的函数无法改闭包变量。
    failures = {"count": 0}

    def _count_failure() -> dict[str, Any] | None:
        failures["count"] += 1
        if failures["count"] <= MAX_CONSOLIDATION_FAILURES:
            return None
        return {
            "success": False,
            "done": True,
            "error": (
                f"这一轮已经 {failures['count']} 次没能整理好常驻记忆。**不要再重试这个工具**，"
                "先把用户的问题答完；这条事实可以下一轮再存。"
            ),
        }

    @tool("memory", args_schema=MemoryArgs, description=_DESCRIPTION)
    def memory(
        target: str,
        action: str | None = None,
        content: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        operations: list[Any] | None = None,
    ) -> str:
        """读写常驻记忆（MEMORY.md / USER.md）。内容会进入每一轮的系统提示。"""
        if not target_store.enabled(target):
            label = "USER.md" if target == "user" else "MEMORY.md"
            return _json({
                "success": False,
                "error": f"{label} 已在设置里关闭写入（记忆 → 常驻记忆）。本次没有写入任何内容。",
            })
        if not writes_allowed:
            return _json({
                "success": False,
                "error": (
                    "本次会话来自定时任务/心跳/子 Agent，不允许写常驻记忆："
                    "这两份文件会进入每一轮的提示，非交互来源写入等于给它开一条持续注入的通道。"
                ),
            })
        if operations is not None:
            result = target_store.apply_batch(target, [_as_dict(item) for item in operations])
        else:
            text = content if content is not None else new_text
            if action == "add":
                result = target_store.add(target, text or "")
            elif action == "patch":
                result = target_store.patch(target, old_text or "", text or "")
            elif action == "remove":
                result = target_store.remove(target, old_text or "")
            else:
                return _json({"success": False, "error": f"未知 action「{action}」，只能用 add / patch / remove。"})
        if result.get("success"):
            failures["count"] = 0
        elif (terminal := _count_failure()) is not None:
            # 任何失败都计数：真实验证里模型先被整批拒绝、又逐条重试，只数"超限"那一类
            # 会让它在一轮里反复撞墙（2026-10-27 实测）。
            return _json(terminal)
        return _json(result)

    return [memory]


def _as_dict(item: Any) -> dict[str, Any]:
    """批量项可能是 pydantic 模型（schema 校验后）或字典（直接调用），两种都接受。"""
    if isinstance(item, dict):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    return dict(dump()) if callable(dump) else {"action": str(item)}


def _json(payload: dict[str, Any]) -> str:
    """统一序列化：中文不转义（模型读中文比读 \\uXXXX 省 token）。"""
    return json.dumps(payload, ensure_ascii=False)

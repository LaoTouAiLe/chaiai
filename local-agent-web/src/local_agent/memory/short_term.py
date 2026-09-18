"""短期记忆的 Prompt 窗口选择（架构 §10.1、§10.5；开发文档 T16）。

Checkpoint 里的 ``messages`` 是审计与恢复用的原始记录，**不因摘要压缩而删除**（架构 §10.1）；
送进模型的是单独裁剪出来的**窗口**，由本模块决定。

裁剪规则：

1. ``compression.enabled = false`` → **原样返回全部消息**（关闭即透传，不做任何裁剪）；
2. 打开时取末尾 ``prompt_window_messages`` 条，再与"最近 ``preserve_recent_turns`` 轮的起点"
   取**更早**的那个起点，最后向前对齐到 ``HumanMessage`` 边界；
3. 对齐只让窗口变大（消息变多），不会丢消息：宁可多带几条，也不产生模型无法解释的残缺工具
   序列——**不能从 ``AIMessage(tool_calls)`` 与对应 ``ToolMessage`` 中间切开**（架构 §4.2
   要求每个调用都有处理结果）；
4. 最后按"工具步"收缩：只保留最近 ``preserve_recent_tool_steps`` 个完整工具步，更早的整步
   省略并用一条说明占位（同样不切开 tool 对）。

"轮次"的定义：一条 ``HumanMessage`` 到下一个 ``HumanMessage`` 之前的所有消息算一轮；开头若有
不以人类消息起始的片段（例如上一轮工具任务中断后恢复），整体算作一个前置轮次。

"工具步"的定义：一条 ``AIMessage(tool_calls)`` 及其紧随其后的 ``ToolMessage``。**只按轮次裁剪
是不够的**：一个用户请求内的长工具循环只有 1 个人类轮次，轮次保底会把整条工具链原样带进窗口
（复审探针：21 条消息仍发 21 条），单个长任务因此完全无法收缩。所以窗口还有一层**工具步**
裁剪：只保留最近 ``preserve_recent_tool_steps`` 步，更早的整步省略并用一条说明占位。
被省略的消息仍在 Checkpoint 里，等轮次推进后会被会话摘要覆盖。

本模块只管**窗口**；摘要内容由 :mod:`local_agent.memory.summarizer` 生成，两者叠加进同一个
系统提示与消息序列（架构 §10.5）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

#: 省略较早工具步时插入的说明头：让模型知道中间有内容被省略，而不是以为什么都没做过。
#: 占位只带**可信元数据**（工具名、状态、输出长度），不带工具输出原文——它是以系统角色出站的，
#: 原始输出里的注入文本不能因此获得系统指令的权重。
TOOL_STEP_HEADER = "（为控制上下文，这里省略了 {count} 个较早的工具调用及其结果，概况如下）"
#: 占位里最多列出多少条被省略步骤的元数据（更早的只计数）。
MAX_TOOL_STEP_SUMMARIES = 5
#: 占位里一条被省略步骤最多列几个工具名。
MAX_TOOL_NAMES = 5
#: 合法的工具名：字母、数字、下划线、点、连字符，长度有限。
_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
#: 工具名不合法时的替代文本。
UNKNOWN_TOOL_NAME = "未知工具"


@dataclass(frozen=True)
class PromptWindow:
    """裁剪结果：送模型的消息、裁剪前的总条数，以及已被摘要覆盖的条数。"""

    messages: list[AnyMessage]
    total_messages: int
    covered_messages: int = 0
    #: 被"工具步"规则整步省略的步数（这些消息仍在 Checkpoint 里，只是本轮不进窗口）。
    omitted_tool_steps: int = 0
    #: 被整步省略的**原始**消息条数（不含代替它们的占位说明）。
    omitted_messages: int = 0

    @property
    def dropped_messages(self) -> int:
        """窗口相对原始序列的**净**减少条数（``原始条数 − 窗口条数``；占位说明算在窗口里）。

        含被摘要覆盖与整步省略的消息；它们仍在 Checkpoint 里。注意净减少量比"实际删掉的原始
        消息数"少（占位说明补回了一条），要精确数字用 :attr:`omitted_messages`。
        """
        return self.total_messages - len(self.messages)


def select_prompt_window(
    messages: Sequence[AnyMessage],
    *,
    enabled: bool,
    max_messages: int,
    preserve_recent_turns: int = 0,
    preserve_recent_tool_steps: int = -1,
    covered_messages: int = 0,
) -> PromptWindow:
    """选出本轮送模型的消息窗口。

    Args:
        messages: Checkpoint 里的完整消息序列（本函数不修改它）。
        enabled: ``memory.compression.enabled``；``false`` 时原样返回全部消息。
        max_messages: ``memory.short_term.prompt_window_messages``，窗口条数上限。
        preserve_recent_turns: ``memory.compression.preserve_recent_turns``，至少保留的最近轮次；
            小于等于 0 表示不按轮次保底（只按条数裁剪）。
        preserve_recent_tool_steps: ``memory.compression.preserve_recent_tool_steps``，窗口里最多
            保留的**最近完整工具步**数量；更早的工具步整步省略（见 :func:`_omit_older_tool_steps`）。
            **默认 ``-1`` 表示不裁剪**（安全默认）；``0`` 表示把工具步全省略（只留一条说明占位），
            配置项的默认值由 ``CompressionConfig`` 决定（当前 ``3``）。
        covered_messages: 已经被会话摘要覆盖的消息条数
            （``context_budget.summary_covered_messages``）。这些消息**不再进窗口**——摘要已经
            代表它们（架构 §10.5 第 5 条）；不排除的话每轮都会重发同一批历史，压缩反而让
            Prompt 变长。

    Returns:
        :class:`PromptWindow`；窗口首条一定是轮次起点或工具步边界（或整个序列为空）。
    """
    items = list(messages)
    total = len(items)
    if not enabled or total == 0:
        return PromptWindow(messages=items, total_messages=total)
    floor = max(0, min(int(covered_messages), total))
    start = max(floor, total - max(1, int(max_messages)))
    if preserve_recent_turns > 0:
        start = max(floor, min(start, recent_turns_start(items, preserve_recent_turns)))
    start = _align_to_turn_start(items, start, floor=floor)
    window, omitted_steps, omitted_messages = _omit_older_tool_steps(
        items[start:], keep=preserve_recent_tool_steps
    )
    return PromptWindow(
        messages=window,
        total_messages=total,
        covered_messages=floor,
        omitted_tool_steps=omitted_steps,
        omitted_messages=omitted_messages,
    )


def turn_starts(messages: Sequence[AnyMessage]) -> list[int]:
    """各轮次的起始下标；序列开头若不是人类消息，则 0 也算一个起始。"""
    starts = [index for index, message in enumerate(messages) if isinstance(message, HumanMessage)]
    if starts and starts[0] != 0:
        starts.insert(0, 0)
    elif not starts:
        starts = [0] if messages else []
    return starts


def recent_turns_start(messages: Sequence[AnyMessage], turns: int) -> int:
    """最近 ``turns`` 轮的起点下标；轮次不足时返回 0。

    ``compression_node`` 也用它来切分"该被摘要覆盖的旧消息"与"保留原文的最近轮次"，
    因此窗口与摘要用的是**同一个边界**，不会出现"既没进窗口、也没进摘要"的消息。
    """
    starts = turn_starts(messages)
    if not starts:
        return 0
    return starts[max(0, len(starts) - turns)]


def _align_to_turn_start(
    messages: Sequence[AnyMessage], start: int, *, floor: int = 0
) -> int:
    """把下标向前对齐到 ``[floor, start]`` 内最近的轮次起点；没有候选时返回 ``floor``。

    摘要覆盖边界本身就是上一次算出的轮次起点，所以它一定在候选里——不会出现"为了对齐又退回到
    已被摘要覆盖的区域"。
    """
    candidates = [index for index in turn_starts(messages) if floor <= index <= start]
    return candidates[-1] if candidates else floor


def tool_step_spans(messages: Sequence[AnyMessage]) -> list[tuple[int, int]]:
    """每个**完整工具步**的下标区间 ``[起, 止)``：``AIMessage(tool_calls)`` + 它的工具结果。

    完整 = 这条 AI 消息的**每个**调用 id 都由紧随其后、id **匹配且不重复**的 ``ToolMessage``
    覆盖（架构 §4.2 要求每个调用都有处理结果）。因此：

    - 还没拿到结果的调用（执行中断、审批挂起）**不算一步**，整段原样保留；
    - 结果 id 不匹配（例如别的调用的结果）或同一个 id 出现两次 → 立刻停止配对，这些消息不属于
      这一步、原样保留；
    - 前面没有对应调用的**孤立** ``ToolMessage`` 不在任何区间里，永远不会被单独裁掉。

    按 id 严格配对是必须的：只按"连续 ToolMessage"分组会把待处理调用连带删掉，也会顺手吞掉紧跟
    在正常结果后面的不匹配结果（复审探针：ghost 结果消失）。
    """
    spans: list[tuple[int, int]] = []
    index = 0
    total = len(messages)
    while index < total:
        expected_ids = _call_ids(messages[index])
        if expected_ids is None:
            index += 1
            continue
        expected = set(expected_ids)
        end = index + 1
        seen: set[str] = set()
        while end < total:
            candidate = messages[end]
            if not isinstance(candidate, ToolMessage):
                break
            call_id = str(candidate.tool_call_id)
            if call_id not in expected or call_id in seen:
                break
            seen.add(call_id)
            end += 1
        if len(seen) == len(expected_ids):
            spans.append((index, end))
            index = end
        else:
            # 有调用没拿到匹配结果：不是完整步，整段留给窗口（不裁、也不产生占位）
            index += 1
    return spans


def _call_ids(message: AnyMessage) -> list[str] | None:
    """消息里工具调用的 id **列表**；无法唯一配对时返回 ``None``。

    返回列表而不是集合是必须的：``[c1, c1]``（同一 id 出现两次）转集合会被折叠成一条，
    于是"还有一个调用没回填"的步被误判完整并被裁掉；调用**没有 id** 时同样无法配对，
    也必须整步保留（复审探针复现）。所以任一 id 为空或不唯一时一律返回 ``None``。
    """
    if not isinstance(message, AIMessage):
        return None
    calls = message.tool_calls or []
    if not calls:
        return None
    ids = [str(call["id"]) if call.get("id") is not None else "" for call in calls]
    if any(not call_id for call_id in ids) or len(set(ids)) != len(ids):
        return None
    return ids


def _omit_older_tool_steps(
    messages: Sequence[AnyMessage], *, keep: int
) -> tuple[list[AnyMessage], int, int]:
    """只保留最近 ``keep`` 个完整工具步，更早的整步省略（在原位插一条说明占位）。

    返回 ``(窗口消息, 被省略的步数, 被省略的原始消息条数)``。``keep`` 为负或步骤数不超过
    ``keep`` 时原样返回。

    为什么需要它：轮次保底对"一个用户请求内的长工具循环"无效（只有 1 个人类轮次），整条工具链
    会原样进窗口。省略是**整步**的（调用与结果同去同留），占位插在被省略区段的起点，因此不会落进
    其它步骤的"调用—结果"之间。
    """
    items = list(messages)
    if keep < 0:
        return items, 0, 0
    spans = tool_step_spans(items)
    if len(spans) <= keep:
        return items, 0, 0
    dropped = spans[: len(spans) - keep] if keep else spans
    cutting = {index for begin, end in dropped for index in range(begin, end)}
    notice = _tool_step_notice(items, dropped)
    result: list[AnyMessage] = []
    notice_added = False
    for index, message in enumerate(items):
        if index in cutting:
            if not notice_added:
                result.append(SystemMessage(content=notice))
                notice_added = True
            continue
        result.append(message)
    return result, len(dropped), len(cutting)


def _tool_step_notice(
    messages: Sequence[AnyMessage], spans: Sequence[tuple[int, int]]
) -> str:
    """被省略步骤的占位说明：条数 + 每条**可信元数据**（工具名、状态、输出长度）。

    这里刻意**不放工具输出原文**：占位是以 ``SystemMessage`` 出站的，把文件/命令的原始文本放进去
    等于把其中可能存在的注入内容提升到系统指令层，绕过反思链路已有的 ``<<<TOOL_OUTPUT>>>`` 隔离
    （复审意见）。工具名、状态与长度都由代码产生，属于可信元数据。

    只列最近 :data:`MAX_TOOL_STEP_SUMMARIES` 条（``spans[-N:]``）：被省略的是更早的步骤，越靠后
    的越新、对后续决策越有用；先前的 ``spans[:N]`` 会把较新的步骤藏起来、只显示更陈旧的。
    """
    shown = spans[-MAX_TOOL_STEP_SUMMARIES:]
    lines = [_tool_step_line(messages, begin, end) for begin, end in shown]
    if len(spans) > len(shown):
        lines.insert(0, f"- 另有 {len(spans) - len(shown)} 个更早的步骤未列出")
    return "\n".join([TOOL_STEP_HEADER.format(count=len(spans)), *lines])


def _tool_step_line(messages: Sequence[AnyMessage], begin: int, end: int) -> str:
    """一个被省略步骤的一行元数据：``- 工具名（状态，输出 N 字符）``（不含输出原文）。"""
    first = messages[begin]
    calls = (first.tool_calls or []) if isinstance(first, AIMessage) else []
    results = list(messages[begin + 1 : end])
    statuses = "、".join(_tool_status(item) for item in results) or "无结果"
    sizes = sum(len(str(getattr(item, "content", ""))) for item in results)
    return f"- {_tool_names(calls)}（{statuses}，输出 {sizes} 字符）"


def _tool_names(calls: Sequence[Any]) -> str:
    """被省略步骤里涉及的工具名：**过滤 + 去重 + 限量**后再进占位。

    工具名是模型生成的 ``tool_calls[].name``，不是代码产生的可信值：带换行或超长内容的名字
    原样写进 ``SystemMessage`` 就等于把注入文本提升到了系统角色（复审探针复现换行指令被原样
    写入），也会让占位无限膨胀。因此只接受 :data:`_TOOL_NAME_PATTERN` 允许的短名字，其余记为
    :data:`UNKNOWN_TOOL_NAME`。
    """
    seen: list[str] = []
    for call in calls:
        raw = call.get("name") if isinstance(call, dict) else None
        name = _safe_tool_name(raw)
        if name not in seen:
            seen.append(name)
    if not seen:
        return "工具"
    shown = "、".join(seen[:MAX_TOOL_NAMES])
    if len(seen) > MAX_TOOL_NAMES:
        return f"{shown} 等 {len(seen)} 个工具"
    return shown


def _safe_tool_name(name: object) -> str:
    """工具名过滤：只接受受限字符集与长度，其余一律记为"未知工具"。"""
    text = str(name) if name is not None else ""
    return text if _TOOL_NAME_PATTERN.fullmatch(text) else UNKNOWN_TOOL_NAME


def _tool_status(message: AnyMessage) -> str:
    """工具结果的状态：``ToolMessage.status`` 优先，缺失时按有无内容判定。"""
    status = getattr(message, "status", None)
    if isinstance(status, str) and status:
        return status
    return "ok" if str(getattr(message, "content", "")).strip() else "empty"

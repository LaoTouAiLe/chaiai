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

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from local_agent.context.token_budget import estimate_messages_tokens
from local_agent.memory.tool_output import project_file_tool_output
from local_agent.message_flags import is_tool_image_message

#: 压力阶段的头部字符预算；沿用已有配置口径，不以首次出站裁剪换缓存比例。
TOOL_RESULT_HEAD_CHARS = 3072
#: 压中段时保留的尾部字符预算；文件投影在该预算内按行保留。
TOOL_RESULT_TAIL_CHARS = 768
#: 超过这个长度的**正文字段**才值得压（对标 dsh 的 ``thresholdChars``）。
TOOL_RESULT_SQUEEZE_CHARS = 8192

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
    #: 本次压力阶段新增的实际替换正文，由压缩节点保存后跨轮重放。
    tool_result_overrides: dict[str, str] = field(default_factory=dict)

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
    preserve_recent_tool_step_chars: int = 0,
    tool_result_keep_chars: int = 0,
    covered_messages: int = 0,
    prune_tool_steps: bool = True,
    prune_tool_results: bool = False,
    tool_output_overrides: Mapping[str, str] | None = None,
    retain_tokens: int = 0,
) -> PromptWindow:
    """选出本轮送模型的消息窗口。

    Args:
        messages: Checkpoint 里的完整消息序列（本函数不修改它）。
        enabled: ``memory.compression.enabled``；``false`` 时原样返回全部消息。
        max_messages: ``memory.short_term.prompt_window_messages``，窗口条数上限。
        preserve_recent_turns: ``memory.compression.preserve_recent_turns``，至少保留的最近轮次；
            小于等于 0 表示不按轮次保底（只按条数裁剪）。
        retain_tokens: ``memory.compression.retain_ratio × llm.context_window_tokens`` 的 token
            预算；大于 0 时把 ``preserve_recent_turns`` 收紧成"token 装得下的最近轮数"（下限 1 轮），
            使保留下限与窗口联动。``0`` 表示不做联动，即改动前的行为。
        preserve_recent_tool_steps: ``memory.compression.preserve_recent_tool_steps``，窗口里**至少**
            保留的**最近完整工具步**数量（下限）；更早的工具步按预算省略
            （见 :func:`_omit_older_tool_steps`）。**默认 ``-1`` 表示不裁剪**（安全默认）；
            ``0`` 表示至少保留 0 步（仍受预算下限约束）。
        preserve_recent_tool_step_chars: ``memory.compression.tool_step_budget_chars``，工具步内容的
            **字符预算**：从最新一步往回累计，超预算的更早步骤才整步省略。``0`` 表示不按预算
            多留（退化成只按 ``preserve_recent_tool_steps`` 条数省略，即 2026-09-30 之前的行为）。
        tool_result_keep_chars: 压力阶段的单条正文阈值，``0`` 禁止正文裁剪。
        prune_tool_results: 调用方已确认上下文压力达标时才允许新裁剪。
        tool_output_overrides: 已保存的模型正文替换；即使本次不再有压力也持续重放。
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
    # 先重放明确保存的压力替换；正常读取只添加行号，不预先删除证据。
    items, result_overrides = project_tool_messages(
        items, tool_result_keep_chars if prune_tool_results else 0,
        overrides=tool_output_overrides,
    )
    floor = max(0, min(int(covered_messages), total))
    start = max(floor, total - max(1, int(max_messages)))
    if preserve_recent_turns > 0:
        # 保留下限与窗口联动：先按 token 预算收紧轮数，再取该轮数的起点。
        # **必须用原始 ``messages``**（不是投影后的 ``items``）：摘要边界（compression_node）
        # 也用同一份原始序列做同样的计算，否则窗口与摘要的边界会漂移。
        turns = turns_fitting_tokens(
            messages, max_turns=preserve_recent_turns, retain_tokens=retain_tokens
        )
        start = max(floor, min(start, recent_turns_start(items, turns)))
    start = _align_to_turn_start(items, start, floor=floor)
    window, omitted_steps, omitted_messages = (
        _omit_older_tool_steps(
            items[start:], keep=preserve_recent_tool_steps,
            max_chars=preserve_recent_tool_step_chars,
        ) if prune_tool_steps else (items[start:], 0, 0)
    )
    return PromptWindow(
        messages=window,
        total_messages=total,
        covered_messages=floor,
        omitted_tool_steps=omitted_steps,
        omitted_messages=omitted_messages,
        tool_result_overrides=result_overrides,
    )


def turn_starts(messages: Sequence[AnyMessage]) -> list[int]:
    """各轮次的起始下标；序列开头若不是人类消息，则 0 也算一个起始。

    "工具读到的图片"那条 user 消息**不算轮次起点**（2026-10-20）：它在语义上属于上一条工具
    结果，算成一轮会让压缩的"保留最近 N 轮"多留半轮。
    """
    starts = [
        index for index, message in enumerate(messages)
        if isinstance(message, HumanMessage) and not is_tool_image_message(message)
    ]
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


def turns_fitting_tokens(
    messages: Sequence[AnyMessage], *, max_turns: int, retain_tokens: int
) -> int:
    """把 ``max_turns`` 按 token 预算收紧后的**实际保留轮数**（下限 1 轮）。

    起因：``preserve_recent_turns`` 是硬编码条数，窗口很小时"至少保留 8 轮"会恒等于"整段历史
    都留下"——保留下限大于窗口，压缩因此**触发也压不动**，且没有任何可见信号。这里把它与窗口
    联动：从**最新**一轮往回累计 token，装得下就继续留，装不下就在那里停住。

    ``retain_tokens <= 0`` 时不做联动，直接返回 ``max_turns``（即改动前的行为）。
    最近一轮**无条件保留**：即使它单独就超过预算，也至少留 1 轮——否则窗口会把当前问题也裁掉。

    ``compression_node`` 与 :func:`select_prompt_window` 都用它，因此窗口与摘要边界仍共用同一个
    结果（用**同一份原始消息序列**调用）。
    """
    if max_turns <= 0:
        return 0
    if retain_tokens <= 0:
        return max_turns
    starts = turn_starts(messages)
    if not starts:
        return 1
    used = 0
    kept = 0
    for index in range(len(starts) - 1, -1, -1):
        begin = starts[index]
        end = starts[index + 1] if index + 1 < len(starts) else len(messages)
        cost = estimate_messages_tokens(messages[begin:end])
        if kept >= 1 and used + cost > retain_tokens:
            break
        used += cost
        kept += 1
        if kept >= max_turns:
            break
    return max(1, kept)


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
    messages: Sequence[AnyMessage], *, keep: int, max_chars: int = 0
) -> tuple[list[AnyMessage], int, int]:
    """保留最近的一批完整工具步，更早的整步省略（在原位插一条说明占位）。

    返回 ``(窗口消息, 被省略的步数, 被省略的原始消息条数)``。``keep`` 为负或步骤数不超过
    ``keep`` 时原样返回。

    为什么需要它：轮次保底对"一个用户请求内的长工具循环"无效（只有 1 个人类轮次），整条工具链
    会原样进窗口。省略是**整步**的（调用与结果同去同留），占位插在被省略区段的起点，因此不会落进
    其它步骤的"调用—结果"之间。

    ``max_chars``（2026-09-30 新增）是**字符预算**：从最新一步往回累计，装得下就继续留；
    一旦装上会超预算，就在那里停住，更早的整步省略。``keep`` 变成**下限**——预算再紧也至少留
    ``keep`` 步（避免把最近的关键上下文省掉）。``max_chars <= 0`` 时只看 ``keep``，即改动前的行为。

    为什么加预算：只按条数省会在**每一步**都重写窗口的中段，而窗口一变，供应商的前缀缓存就整段
    失效。实测一个 20 步循环因此命中率只有 19.9%（只有 ``[system]`` 那段还能复用），未命中输入
    635,714 token。改成预算驱动后，短循环一个字都不省——历史保持**追加式**，前缀稳定因而可缓存。
    """
    items = list(messages)
    if keep < 0:
        return items, 0, 0
    spans = tool_step_spans(items)
    if len(spans) <= keep:
        return items, 0, 0
    #: 第一个**保留**步在 ``spans`` 里的下标；它之前的都要省略。
    first_kept = _first_kept_by_budget(items, spans, keep=keep, max_chars=max_chars)
    dropped = spans[:first_kept]
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


def _first_kept_by_budget(
    items: Sequence[AnyMessage],
    spans: Sequence[tuple[int, int]],
    *,
    keep: int,
    max_chars: int,
    max_tokens: int = 0,
) -> int:
    """从最新一步往回累计，返回第一个**保留**步在 ``spans`` 里的下标（更早的都要省/压）。

    ``keep`` 是**下限**：下限之内无条件保留（避免把最近的关键上下文省掉）。``max_chars <= 0``
    时只看 ``keep``，即"只按条数省"的旧行为。
    """
    if max_chars <= 0 and max_tokens <= 0:
        return len(spans) - keep
    used = 0
    used_tokens = 0
    index = len(spans)
    while index > 0:
        begin, end = spans[index - 1]
        cost = sum(len(str(getattr(item, "content", ""))) for item in items[begin:end])
        token_cost = estimate_messages_tokens(items[begin:end])
        # 下限之内无条件保留；之外须同时装入字符和实际 token 预算。
        if index <= len(spans) - keep and (
            (max_chars > 0 and used + cost > max_chars)
            or (max_tokens > 0 and used_tokens + token_cost > max_tokens)
        ):
            break
        used += cost
        used_tokens += token_cost
        index -= 1
    return index


def tool_output_key(message: ToolMessage) -> str:
    """按消息身份和原文指纹定位，跨轮重复调用 ID 或分支不会错用旧替换。"""
    raw = json.dumps([message.name, message.tool_call_id, message.content], ensure_ascii=False)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{message.id or 'tool'}:{digest}"


def project_tool_messages(
    items: Sequence[AnyMessage], threshold_chars: int = 0,
    *, overrides: Mapping[str, str] | None = None,
) -> tuple[list[AnyMessage], dict[str, str]]:
    """重放保存的工具视图，只返回本次实际新裁剪的正文；原始消息不变。"""
    projected = list(items)
    replacements: dict[str, str] = {}
    for index, message in enumerate(items):
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        key = tool_output_key(message)
        if overrides is not None and key in overrides:
            text = overrides[key]
        else:
            full = _squeeze_tool_payload(message.content, 0, tool_name=message.name)
            text = (_squeeze_tool_payload(message.content, threshold_chars, tool_name=message.name)
                    if threshold_chars > 0 else full)
            # 极小阈值下，缺口元数据可能比原正文更长；无收益就保持完整证据。
            if len(text) >= len(full):
                text = full
            if text != full:
                replacements[key] = text
        if text != message.content:
            projected[index] = message.model_copy(update={"content": text})
    return projected, replacements


def tool_summary_boundary(
    messages: Sequence[AnyMessage], *, keep: int, max_chars: int, max_tokens: int = 0,
) -> int:
    """压力下可摘要的完整工具前缀；保留最近工具证据，不能拆开调用和结果。"""
    spans = tool_step_spans(messages)
    if keep < 0 or len(spans) <= keep:
        return 0
    first_kept = _first_kept_by_budget(
        messages, spans, keep=keep, max_chars=max_chars, max_tokens=max_tokens,
    )
    return spans[first_kept - 1][1] if first_kept > 0 else 0


def _squeeze_tool_payload(
    text: str, threshold_chars: int, *, tool_name: str | None = None,
) -> str:
    """**JSON 感知**的中段裁剪：只压正文字段，保持 JSON 合法与数据边界完整。

    为什么不直接切字符串：工具结果是 JSON（元数据在前、正文在后），按字符切会把结构切坏，模型看到
    的是半个对象，连 ``path`` / ``sha256`` 都读不出来；而工具结果的正文又**必须留在 ``ToolMessage``
    里**——``<<<TOOL_OUTPUT>>>`` 边界之外才是可执行指令，把它挪进系统占位是复审明确否掉的做法
    （见 :func:`_tool_step_notice` 的说明）。所以只能原地、按字段压。

    为什么保留**尾部**：只留头会让"文件最后一段讲了什么"永远看不到（长文件用例就是靠
    ``next_start_line`` 续读才拿到末尾的）。两端都留，模型至少知道这一步的结果长什么样；
    中间那条说明是可执行的（告诉它怎么拿回完整内容），不是一句感叹。
    """
    try:
        payload = json.loads(text)
    except ValueError:
        return (_head_and_tail(text, threshold_chars) if threshold_chars > 0
                and len(text) > threshold_chars else text)
    if not isinstance(payload, dict):
        return text
    projected = project_file_tool_output(
        payload, tool_name=tool_name, threshold_chars=threshold_chars,
        head_chars=TOOL_RESULT_HEAD_CHARS, tail_chars=TOOL_RESULT_TAIL_CHARS,
    )
    if projected is not None:
        return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    if threshold_chars <= 0 or len(text) <= threshold_chars:
        return text
    changed = False
    for key, value in list(payload.items()):
        if isinstance(value, str) and len(value) > threshold_chars:
            payload[key] = _head_and_tail(value, threshold_chars)
            changed = True
    if not changed:
        return text
    return json.dumps(payload, ensure_ascii=False)


def _head_and_tail(text: str, threshold_chars: int = TOOL_RESULT_SQUEEZE_CHARS) -> str:
    """保留头 :data:`TOOL_RESULT_HEAD_CHARS` + 尾 :data:`TOOL_RESULT_TAIL_CHARS`，中间插可执行说明。"""
    budget = max(0, min(TOOL_RESULT_HEAD_CHARS + TOOL_RESULT_TAIL_CHARS, threshold_chars - 128))
    head_chars = budget * TOOL_RESULT_HEAD_CHARS // (TOOL_RESULT_HEAD_CHARS + TOOL_RESULT_TAIL_CHARS)
    tail_chars = budget - head_chars
    head = text[:head_chars]
    tail = text[-tail_chars:] if tail_chars else ""
    omitted = len(text) - len(head) - len(tail)
    return (
        f"{head}\n"
        f"……（中间 {omitted} 字符已裁剪；需要完整内容请用 start_line/end_line 或更小的范围重新读取）……\n"
        f"{tail}"
    )


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

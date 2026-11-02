"""上下文压缩的**只读基准**：量化"压缩之后，哪些事实还能被模型看到"。

为什么要这个脚本（2026-11-02）：

压缩策略（``trigger_ratio`` / ``retain_ratio`` / ``preserve_recent_turns`` /
``preserve_recent_tool_steps``）现在只能靠"感觉上下文还够"来判断，改一个数不知道丢了什么。
参照实现 hermes 的 ``evals/compaction`` 用真实模型做"压缩后还能答对吗"的打分卡；这里先做
**不花钱的那一半**：结构账。也就是把一轮压缩拆成三桶，逐条数事实落在哪一桶：

- ``window``：本轮真的发进模型的消息——事实在这里，模型一定看得到；
- ``summary``：将被摘要覆盖的区间——事实能不能留下来，取决于摘要写得好不好（要真模型才能测）；
- ``gap``：既不在窗口、也没被摘要覆盖——**这一桶必须恒为 0**，非 0 就是结构性丢失。

分工要写清楚，别把两件事混成一件：

- 本脚本证明的是"边界没漏"与"token 账"，**不证明摘要写得好**；
- 摘要保真度（压缩后回忆准确率）必须真模型跑：见 ``docs/benchmarks/`` 里记录的口径与
  ``--judge-*`` 一节（本版不实现，避免在没有预算授权时偷偷调用接口）。
- **所有场景都假设压缩已经触发**（预算比 ≥ ``trigger_ratio``）。触发判定只看 token 账、
  不看事实，没有可测的语义成分，所以这里不模拟它；被它挡掉的轮次本来就不产生丢失面。

边界（有意为之）：

- **不调模型、不花 API 费用**：只用合成语料 + 生产侧的窗口函数；
- 事实是脚本注入的唯一标记（``[[FACT-M012]]``），是**代理指标**，不是人工标注；
- **手动执行，不进 CI**：语料是合成的，默认值一改就要人工重跑并留数字；
- 允许 import 私有路径：这里要复刻 ``compression_node`` 的判序，不能自己另写一套边界算法。

用法::

    python scripts/compaction_bench.py
    python scripts/compaction_bench.py --json docs/benchmarks/compaction-20261102.json
    python scripts/compaction_bench.py --turns 40 --tool-steps 4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from local_agent.config import CompressionConfig, ShortTermMemoryConfig
from local_agent.context.token_budget import estimate_messages_tokens
from local_agent.memory.short_term import (
    recent_turns_start,
    select_prompt_window,
    turns_fitting_tokens,
)

#: 每条消息的身份标记。**必须放在正文最前面**：工具正文被"压中段"时头尾保留，标记才不会丢。
_MARK = re.compile(r"\[\[M(\d{3})\]\]")
#: 事实标记。一条带内容的消息恰好一条事实，编号与消息下标一致，便于逐桶对账。
_FACT = re.compile(r"\[\[FACT-M(\d{3})\]\]")


@dataclass(frozen=True)
class Scenario:
    """一个待测策略组合。名字直接出现在表头，改策略必须改名，避免新旧数字混在同一行。"""

    name: str
    prompt_window_messages: int
    preserve_recent_turns: int
    preserve_recent_tool_steps: int


#: 默认四组。**只能追加**：删掉行等于把历史数字一起抹掉，之后说不清是哪一版退化了。
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("默认（100 条 / 留 8 轮 / 3 工具步）", 100, 8, 3),
    Scenario("小窗口（40 条 / 留 8 轮 / 3 工具步）", 40, 8, 3),
    Scenario("紧保留（100 条 / 留 2 轮 / 3 工具步）", 100, 2, 3),
    Scenario("不留工具步（100 条 / 留 8 轮 / 0 步）", 100, 8, 0),
)


def build_transcript(*, turns: int, tool_steps: int, pad_chars: int) -> list[AnyMessage]:
    """造一段"很长且有事实"的会话：每轮 = 提问 + N 个工具步 + 结论。

    事实只放在工具结果与结论里——提问本身不承载事实，这样"事实进了哪一桶"才是可判定的。
    """
    messages: list[AnyMessage] = []
    filler = "背景说明" * max(1, pad_chars // 8)

    for turn in range(turns):
        index = len(messages)
        messages.append(HumanMessage(content=f"{_mark(index)} 第{turn + 1}轮：帮我核对这份清单 {filler}"))

        for step in range(tool_steps):
            call_id = f"call-{turn}-{step}"
            index = len(messages)
            messages.append(
                AIMessage(
                    content="",
                    tool_calls=[{"name": "read_file", "args": {"path": f"mod_{step}.py"}, "id": call_id}],
                )
            )
            index = len(messages)
            messages.append(
                ToolMessage(
                    content=f"{_mark(index)} {_fact(index)} 第{turn + 1}轮第{step + 1}步读到：{filler}",
                    tool_call_id=call_id,
                )
            )

        index = len(messages)
        messages.append(
            AIMessage(content=f"{_mark(index)} {_fact(index)} 第{turn + 1}轮结论：{filler}")
        )
    return messages


def _mark(index: int) -> str:
    return f"[[M{index:03d}]]"


def _fact(index: int) -> str:
    return f"[[FACT-M{index:03d}]]"


def _indices_in(messages: list[AnyMessage]) -> set[int]:
    """窗口里实际出现了哪些原始下标。

    只看正文：工具步占位说明里没有标记，因此"被整步省略"的消息不会因为占位而误判为在场。
    """
    found: set[int] = set()
    for message in messages:
        found.update(int(match) for match in _MARK.findall(str(message.content)))
    return found


def _fact_indices(messages: list[AnyMessage]) -> set[int]:
    found: set[int] = set()
    for message in messages:
        found.update(int(match) for match in _FACT.findall(str(message.content)))
    return found


@dataclass(frozen=True)
class Row:
    """一个场景的结果。``gap_*`` 是唯一的硬失败项，其余是账。"""

    scenario: str
    summary_ok: bool
    total_messages: int
    window_messages: int
    total_tokens: int
    window_tokens: int
    covered_messages: int
    boundary: int
    window_start: int
    facts_total: int
    facts_window: int
    facts_summary: int
    facts_gap: int
    omitted_tool_steps: int

    def as_dict(self) -> dict[str, object]:
        return self.__dict__ | {"scenario": self.scenario}


def measure(
    messages: list[AnyMessage],
    *,
    scenario: Scenario,
    compression: CompressionConfig,
    short_term: ShortTermMemoryConfig,
    context_window_tokens: int,
    summary_ok: bool,
) -> Row:
    """复刻 ``compression_node`` 的判序：先按已覆盖边界取窗口，再看摘要是否推进了边界。

    ``summary_ok=False`` 是**摘要失败**那一轮：``covered`` 不推进，而窗口仍按自己的规则裁剪，
    于是 ``[covered, window_start)`` 既不进窗口、也没有摘要代表——这正是要盯的丢失面。
    """
    preserve_turns = scenario.preserve_recent_turns
    retain_tokens = int(context_window_tokens * compression.retain_ratio)
    fitted_turns = turns_fitting_tokens(
        messages, max_turns=preserve_turns, retain_tokens=retain_tokens
    )
    boundary = recent_turns_start(messages, fitted_turns)

    covered = 0  # 这段会话此前没有任何摘要，是压缩的第一轮（对丢失面最不利的起点）
    covered_after = boundary if summary_ok else covered
    #: 这一轮**该被摘要**的区间。成功时它就是摘要真正覆盖的区间；失败时它是"本该覆盖却没覆盖"，
    #: 落在其中的事实只能靠窗口兜住——兜不住的部分就是 ``gap``。
    summary_start, summary_end = covered, boundary

    def select(covered_messages: int):
        return select_prompt_window(
            messages,
            enabled=compression.enabled,
            max_messages=scenario.prompt_window_messages,
            preserve_recent_turns=preserve_turns,
            retain_tokens=retain_tokens,
            preserve_recent_tool_steps=scenario.preserve_recent_tool_steps,
            preserve_recent_tool_step_chars=compression.tool_step_budget_chars,
            tool_result_keep_chars=compression.tool_result_keep_chars,
            covered_messages=covered_messages,
            # 压力阶段的正文替换要额外一次预算测量，本脚本不模拟那一步：关掉它，
            # 让数字只反映"窗口/摘要边界"这一条，混进来就说不清是哪个机制丢的。
            prune_tool_results=False,
        )

    window = select(covered_after)
    window_indices = _indices_in(window.messages)
    facts = _fact_indices(messages)
    window_start = min(window_indices) if window_indices else len(messages)

    facts_window = len(facts & window_indices)
    # 「只能靠摘要保住」的事实：落在该被摘要的区间里。摘要失败时这些事实没有摘要代表，
    # 其中又被窗口漏掉的部分进 ``gap``——两个数一起看，才知道一次失败究竟伤了多深。
    facts_summary = len({i for i in facts if summary_start <= i < summary_end})
    facts_gap = len({i for i in facts if covered_after <= i < window_start})

    return Row(
        scenario=scenario.name,
        summary_ok=summary_ok,
        total_messages=len(messages),
        window_messages=len(window.messages),
        total_tokens=estimate_messages_tokens(messages),
        window_tokens=estimate_messages_tokens(window.messages),
        covered_messages=covered_after,
        boundary=boundary,
        window_start=window_start,
        facts_total=len(facts),
        facts_window=facts_window,
        facts_summary=facts_summary,
        facts_gap=facts_gap,
        omitted_tool_steps=window.omitted_tool_steps,
    )


def render(rows: list[Row]) -> str:
    header = (
        f"{'场景':<34}{'摘要':<6}{'条数':>6}{'窗口':>6}"
        f"{'token 前后':>18}{'事实(总)':>10}{'窗口内':>8}{'靠摘要':>8}{'丢失':>6}"
    )
    lines = [header, "-" * 112]
    for row in rows:
        tokens = f"{row.total_tokens:,}→{row.window_tokens:,}"
        lines.append(
            f"{row.scenario:<34}{'成功' if row.summary_ok else '失败':<6}"
            f"{row.total_messages:>6}{row.window_messages:>6}{tokens:>18}"
            f"{row.facts_total:>10}{row.facts_window:>8}{row.facts_summary:>8}{row.facts_gap:>6}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="上下文压缩的只读基准（不调模型）")
    parser.add_argument("--turns", type=int, default=24, help="轮数（默认 24）")
    parser.add_argument("--tool-steps", type=int, default=3, help="每轮工具步数（默认 3）")
    parser.add_argument("--pad", type=int, default=400, help="每条消息的填充字符数（默认 400）")
    parser.add_argument(
        "--context-window",
        type=int,
        default=61_440,
        help="llm.context_window_tokens；只影响 retain_ratio 折算的保留预算（默认 61440）",
    )
    parser.add_argument("--json", type=Path, default=None, help="把结果写成 JSON 的路径")
    args = parser.parse_args(argv)

    messages = build_transcript(turns=args.turns, tool_steps=args.tool_steps, pad_chars=args.pad)
    compression = CompressionConfig()
    short_term = ShortTermMemoryConfig()

    rows: list[Row] = []
    for scenario in SCENARIOS:
        for summary_ok in (True, False):
            rows.append(
                measure(
                    messages,
                    scenario=scenario,
                    compression=compression,
                    short_term=short_term,
                    context_window_tokens=args.context_window,
                    summary_ok=summary_ok,
                )
            )

    print(render(rows))
    print()
    print("口径：所有行都假设压缩已触发（不模拟 trigger_ratio 判定）；不调模型，摘要保真度不在本表内。")

    gap_ok = [row for row in rows if row.summary_ok and row.facts_gap]
    print()
    print(
        f"结构不变量：摘要成功时 gap=0 —— "
        f"{'通过' if not gap_ok else '失败：' + '、'.join(r.scenario for r in gap_ok)}"
    )
    leaked = [row for row in rows if not row.summary_ok and row.facts_gap]
    print(
        "摘要失败那一轮："
        + (
            f"{len(leaked)}/{len(SCENARIOS)} 组丢事实，共 {sum(r.facts_gap for r in leaked)} 条"
            if leaked
            else "没有事实落在缺口里"
        )
    )

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "corpus": {
                "turns": args.turns,
                "tool_steps": args.tool_steps,
                "pad_chars": args.pad,
                "messages": len(messages),
            },
            "config": {
                "context_window_tokens": args.context_window,
                "trigger_ratio": compression.trigger_ratio,
                "retain_ratio": compression.retain_ratio,
                "preserve_recent_turns": compression.preserve_recent_turns,
                "prompt_window_messages": short_term.prompt_window_messages,
            },
            "note": (
                "只测窗口/摘要边界与 token 账；摘要保真度需要真实模型，未在本脚本内测量。"
                "所有行都假设压缩已触发（不模拟 trigger_ratio 判定）。"
            ),
            "rows": [row.as_dict() for row in rows],
        }
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.json}")

    return 0 if not gap_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

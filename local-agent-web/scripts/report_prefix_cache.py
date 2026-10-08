"""T51 缓存前缀报告：把一次真实基线整理成"逐调用 + 回落归因"（契约 §16.1）。

验收观测量是**逐调用缓存读的回落次数**（目标 0~1 次），不是命中率——命中率会被轮次长度稀释，
回落次数才直接对应"前缀被重写了几次"（同口径见 `memory.compression.tool_step_budget_chars`）。

输入是 `measure_usage_baseline.py` 产出的 `baseline_result.json`（真实模型跑出来的原生记录）。
本脚本**不发起任何模型调用**：只读 JSON、算指标、给出归因。

工具 Schema 指纹随 Profile 变化是**预期变化**（换 Profile 就是换前缀），所以报告把
"工具/系统提示变了"与"同一前缀内的意外回落"分开列：前者是解释，后者才是问题。

用法::

    uv run python scripts/report_prefix_cache.py --result <data>/baseline_result.json
    uv run python scripts/report_prefix_cache.py --result ... --max-fallbacks 1 --json

退出码：0 达标；2 输入缺失或无法解析；3 回落次数超出阈值。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: 四段指纹：字段名与报告用标签（顺序即报告顺序）。
FINGERPRINT_FIELDS: tuple[tuple[str, str], ...] = (
    ("tools_schema_fingerprint", "工具Schema"),
    ("system_prompt_fingerprint", "系统提示"),
    ("history_prefix_fingerprint", "历史前缀"),
    ("dynamic_context_fingerprint", "动态上下文"),
)

#: 会影响"前缀"的两段：它们变了，供应商缓存必然重算（动态上下文只增不改前缀）。
PREFIX_FIELDS: tuple[str, ...] = (
    "tools_schema_fingerprint",
    "system_prompt_fingerprint",
)


def _int_or_none(value: object) -> int | None:
    """只接受真正的整数；``None`` / 其它类型一律当"未知"，不补零（架构：未知不伪装成 0）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def flatten_calls(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按轮次顺序展平 ``turns[].model_calls[]``，补上 ``turn`` 与全局 ``seq``。"""
    calls: list[dict[str, Any]] = []
    for turn in payload.get("turns") or []:
        if not isinstance(turn, Mapping):
            continue
        for call in turn.get("model_calls") or []:
            if not isinstance(call, Mapping):
                continue
            item = dict(call)
            item["turn"] = turn.get("turn")
            item["seq"] = len(calls) + 1
            calls.append(item)
    return calls


def changed_segments(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> list[str]:
    """两段记录之间哪几段指纹变了。

    只在一**边为空时不算"变"**：空值表示"这次没记到"，把它当成变化会制造假归因。
    """
    changed: list[str] = []
    for field, label in FINGERPRINT_FIELDS:
        before, after = previous.get(field), current.get(field)
        if before is None or after is None:
            continue
        if before != after:
            changed.append(label)
    return changed


def build_report(payload: Mapping[str, Any], *, max_fallbacks: int = 1) -> dict[str, Any]:
    """算逐调用指标、回落归因与判定。

    Args:
        payload: `baseline_result.json` 的内容（或任何同形状的 dict）。
        max_fallbacks: 允许的回落次数上限（v0.4 起的目标是 0~1）。

    Returns:
        报告字典：``rows`` / ``fallbacks`` / ``summary`` / ``verdict``。
    """
    calls = flatten_calls(payload)
    rows: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    unknown_cache = 0
    previous: Mapping[str, Any] | None = None
    previous_read: int | None = None

    for call in calls:
        read = _int_or_none(call.get("actual_cache_read_tokens"))
        uncached = _int_or_none(call.get("actual_uncached_input_tokens"))
        reported = bool(call.get("cache_metrics_reported"))
        changed = [] if previous is None else changed_segments(previous, call)
        fell_back = previous_read is not None and read is not None and read < previous_read
        if read is None or not reported:
            unknown_cache += 1
        row = {
            "seq": call["seq"],
            "turn": call.get("turn"),
            "purpose": call.get("purpose"),
            "model_call_id": call.get("model_call_id"),
            "status": call.get("status"),
            "cache_read": read,
            "uncached": uncached,
            "reported": reported,
            "changed": changed,
            "fallback": fell_back,
        }
        rows.append(row)
        if fell_back:
            fallbacks.append(
                {
                    "seq": call["seq"],
                    "turn": call.get("turn"),
                    "purpose": call.get("purpose"),
                    "from": previous_read,
                    "to": read,
                    "changed": changed,
                }
            )
        previous = call
        if read is not None:
            previous_read = read

    prefix_resets = sum(
        1
        for index, row in enumerate(rows)
        if index > 0
        and any(
            label in row["changed"]
            for label in ("工具Schema", "系统提示")
        )
    )
    summary = {
        "calls": len(rows),
        "fallbacks": len(fallbacks),
        "max_fallbacks": max_fallbacks,
        "prefix_resets": prefix_resets,
        "unknown_cache_calls": unknown_cache,
        "profile": payload.get("profile"),
        "tool_names": payload.get("profile_tool_names"),
    }
    return {
        "rows": rows,
        "fallbacks": fallbacks,
        "summary": summary,
        "verdict": "ok" if len(fallbacks) <= max_fallbacks else "exceeded",
    }


def format_report(report: Mapping[str, Any]) -> str:
    """把报告排成人能读的文本（中文标签，不泄露任何正文与密钥）。"""
    lines: list[str] = []
    profile = report["summary"].get("profile")
    if profile:
        lines.append(f"Profile：{profile}")
        names = report["summary"].get("tool_names")
        if names:
            lines.append(f"可见工具：{'、'.join(str(name) for name in names)}")
    lines.append("")
    lines.append("调用  轮次  用途                    缓存读      未缓存    状态    变化段")
    for row in report["rows"]:
        read = "未上报" if row["cache_read"] is None or not row["reported"] else str(row["cache_read"])
        uncached = "—" if row["uncached"] is None else str(row["uncached"])
        changed = "、".join(row["changed"]) or "—"
        mark = "（回落）" if row["fallback"] else ""
        lines.append(
            f"{row['seq']:>4}  {row['turn']!s:>4}  {row['purpose']!s:<22} "
            f"{read:>10}  {uncached:>8}  {row['status']!s:<6}  {changed}{mark}"
        )
    summary = report["summary"]
    lines.append("")
    lines.append(
        f"小结：调用 {summary['calls']} 次；缓存回落 {summary['fallbacks']} 次"
        f"（阈值 ≤{summary['max_fallbacks']}）；前缀重置 {summary['prefix_resets']} 次；"
        f"未上报缓存字段 {summary['unknown_cache_calls']} 次"
    )
    if report["fallbacks"]:
        lines.append("回落明细：")
        for item in report["fallbacks"]:
            changed = "、".join(item["changed"]) or "（指纹无变化）"
            lines.append(
                f"  #{item['seq']}（轮次 {item['turn']}，{item['purpose']}）："
                f"缓存读 {item['from']} → {item['to']}，同时变化的段：{changed}"
            )
    else:
        lines.append("回落明细：无")
    lines.append("判定：" + ("达标" if report["verdict"] == "ok" else "超出阈值"))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T51 缓存前缀报告（只读 JSON，不调模型）")
    parser.add_argument("--result", required=True, help="measure_usage_baseline.py 产出的结果 JSON")
    parser.add_argument(
        "--max-fallbacks",
        type=int,
        default=1,
        help="允许的缓存回落次数上限（默认 1，v0.4 起的目标是 0~1）",
    )
    parser.add_argument("--json", action="store_true", help="额外输出机器可读的报告 JSON")
    args = parser.parse_args(argv)

    path = Path(args.result)
    if not path.is_file():
        print(f"找不到结果文件：{path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"无法解析 {path}：{exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, Mapping):
        print(f"{path} 的顶层不是对象", file=sys.stderr)
        return 2

    report = build_report(payload, max_fallbacks=max(0, args.max_fallbacks))
    print(format_report(report))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(main())

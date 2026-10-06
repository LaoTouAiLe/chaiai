"""Audit saved live runs against provider usage captured by the local relay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def total(rows: list[dict]) -> dict:
    hit = sum(row["cache_read_tokens"] for row in rows)
    miss = sum(row["uncached_input_tokens"] for row in rows)
    return {"cache_read_tokens": hit, "uncached_input_tokens": miss,
            "cache_hit_ratio": hit / (hit + miss) if hit + miss else None}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variant", default="variant2")
    args = parser.parse_args()
    wire = []
    for path in sorted((args.out / "wire").glob("*request.json")):
        response = path.with_name(path.name.replace("request.json", "response.json"))
        if response.exists():
            wire.append((path.name, read(path), read(response)))
    groups = {}
    for group in ["baseline", "dsh", args.variant]:
        rows = []
        for index in range(1, 4):
            path = (args.out / f"dsh-sdk-turn-{index}.json" if group == "dsh"
                    else args.out / group / f"chaiai-turn-{index}.json")
            if not path.exists():
                continue
            data = read(path)
            if group == "dsh":
                start, end = data["startedAt"], data["finishedAt"]
                usages = [event["data"]["usage"] for event in data["events"]
                          if event["type"] == "assistant/message" and event["data"].get("usage")]
                # SDK inputTokens is uncached input, NOT total prompt tokens.
                hit = sum(u.get("cacheReadTokens", 0) for u in usages)
                miss = sum(u["inputTokens"] for u in usages)
                state = [event["data"]["reason"]["kind"] for event in data["events"]
                         if event["type"] == "turn/end"][-1]
                errors, memory = [], None
                answer = data["finalResponse"]
                calls = len(usages)
            else:
                start, end = data["started_at"], data["finished_at"]
                snapshot = data["snapshot"]
                usage = snapshot["usage"]
                hit, miss = usage["actual_cache_read_tokens"], usage["actual_uncached_input_tokens"]
                state = snapshot["final_result"]["state"]
                errors = [e["code"] for e in snapshot["errors"]]
                memory = snapshot["memory"]
                answer = snapshot["final_result"]["summary"]
                calls = len(usage["model_call_ids"])
                (args.out / group / f"chaiai-answer-{index}.md").write_text(answer, encoding="utf-8")
            captured = [(name, request, response) for name, request, response in wire
                        if request["client"] == ("dsh" if group == "dsh" else "chaiai")
                        and start <= request["started_at"] <= end
                        and request["route"].endswith("chat/completions")]
            raw_usages = [u for _, _, r in captured for u in r["usage"]]
            raw_hit = sum(u["prompt_cache_hit_tokens"] for u in raw_usages)
            raw_miss = sum(u["prompt_cache_miss_tokens"] for u in raw_usages)
            title_usages = [u for _, request, response in captured
                            if group == "dsh" and request["request"].get("max_tokens") == 64
                            and not request["request"].get("tools") for u in response["usage"]]
            title_hit = sum(u["prompt_cache_hit_tokens"] for u in title_usages)
            title_miss = sum(u["prompt_cache_miss_tokens"] for u in title_usages)
            matches = (raw_hit - title_hit, raw_miss - title_miss) == (hit, miss)
            if group == "dsh":
                hit, miss = raw_hit, raw_miss
            row = {"turn": index, "state": state, "errors": errors, "memory": memory,
                   "model_call_records": calls, "cache_read_tokens": hit,
                   "uncached_input_tokens": miss, "cache_hit_ratio": hit / (hit + miss),
                   "answer_chars": len(answer), "wire_requests": len(captured),
                   "wire_failures": [n for n, _, r in captured if r["status"] != 200],
                   "wire_cache_read_tokens": raw_hit, "wire_uncached_input_tokens": raw_miss,
                   "usage_matches_wire": matches,
                   "title_uncached_input_tokens": title_miss,
                   "title_cache_read_tokens": title_hit}
            # Title generation may be outside assistant/message events; keep it visible.
            row["wire_extra_uncached_input_tokens"] = title_miss
            rows.append(row)
        groups[group] = {"turns": rows, "total": total(rows)}
    report = {"model": "deepseek-flash", "upstream": "https://api.deepseek.com",
              "output_cap": 4096, "fixture": "fixture-manifest.json", "groups": groups,
              "limitations": ["Sequential runs share provider cache warmth; trajectories differ.",
                              "DSH omits temperature while chaiai sends 0.2; tool sets differ.",
                              "First diagnostic run used relative memory paths; excluded from comparison.",
                              "DSH max-tokens and baseline errors are quality failures, not successes."]}
    (args.out / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 同题真实缓存对照", "", "统计使用实际供应商用量，包含本项目任务记忆调用。", "",
             "| 项目 | 第一问 | 第二问 | 第三问 | 加权合计 | 未命中输入 |", "|---|---:|---:|---:|---:|---:|"]
    for name, group in groups.items():
        values = [f"{row['cache_hit_ratio']:.1%}" for row in group["turns"]]
        values += ["未运行"] * (3 - len(values))
        overall = group["total"]
        ratio = f"{overall['cache_hit_ratio']:.1%}" if overall["cache_hit_ratio"] is not None else "未运行"
        lines.append(f"| {name} | {' | '.join(values)} | {ratio} | {overall['uncached_input_tokens']:,} |")
    lines += ["", "逐题终态、降级、记忆结果与透明代理校核见 comparison.json。",
              "DSH 后两问因 4096 输出上限截断；基线存在记忆审核/工具失败。不可把高命中率当作质量通过。",
              "首次诊断与公平对照分别保存，不混合。短任务及冷缓存不保证 90%。"]
    (args.out / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, group in groups.items():
        print(name, group["total"], [(r["state"], r["errors"], r["usage_matches_wire"]) for r in group["turns"]])


if __name__ == "__main__":
    main()

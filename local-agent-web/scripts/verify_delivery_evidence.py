"""Offline verification of the frozen T43 delivery evidence; never calls a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from local_agent.llm.usage_recording import tools_fingerprint

CITATION = re.compile(r"([A-Za-z0-9_./\\-]+\.(?:py|md|ya?ml(?:\.example)?|tsx?|jsx?|json|toml|ps1|sh|txt)):(\d+)(?:-(\d+))?")


def read(path: Path) -> Any:
    """Read a saved UTF-8 JSON evidence file."""
    return json.loads(path.read_text(encoding="utf-8"))



def history_consistency(
    requests: list[dict[str, Any]], call_ids: list[str], epochs: dict[str, Any],
) -> tuple[bool, bool, list[dict[str, Any]]]:
    """Allow history resets only when persisted pressure evidence explains them."""
    changes = []
    for index, (old, new) in enumerate(pairwise(requests)):
        if new["messages"][:len(old["messages"])] == old["messages"]:
            continue
        prior = epochs.get(call_ids[index]) if len(call_ids) == len(requests) else None
        current = epochs.get(call_ids[index + 1]) if len(call_ids) == len(requests) else None
        explained = False
        if isinstance(prior, dict) and isinstance(current, dict):
            old_outputs = prior.get("tool_output_sha256", {})
            new_outputs = current.get("tool_output_sha256", {})
            summaries_advanced = current.get("summary_covered_messages", 0) > prior.get("summary_covered_messages", 0)
            outputs_added = (len(new_outputs) > len(old_outputs)
                             and all(new_outputs.get(key) == value for key, value in old_outputs.items()))
            explained = summaries_advanced or outputs_added
        changes.append({"after_request_index": index, "persisted_pressure_explains_change": explained})
    return not changes, all(change["persisted_pressure_explains_change"] for change in changes), changes

def audit(data: Path, phase: str, workspace_subdir: str = "", expected_runs: int = 3) -> dict[str, Any]:
    """Check frozen source, native usage and task gates without network access."""
    manifest = read(data / f"fixture-{phase}.json")
    workspace = data / f"workspace-{phase}"
    files = {item["path"]: item for item in manifest}
    immutable = all(hashlib.sha256((workspace / name).read_bytes()).hexdigest() == item["sha256"]
                    for name, item in files.items())
    prefix = workspace_subdir.strip("/")
    if prefix and (Path(prefix).is_absolute() or ".." in Path(prefix).parts):
        raise ValueError("workspace-subdir must stay inside the frozen fixture")
    citation_files = {name.removeprefix(prefix + "/"): item for name, item in files.items()
                      if not prefix or name.startswith(prefix + "/")}
    citation_root = workspace / prefix
    rejection_file = data / "local-rejections.json"
    rejections = read(rejection_file) if rejection_file.exists() else []
    rejected = {r["request_path"]: r["sha256"] for r in rejections
                if r.get("classification") == "observed local guard rejection before upstream"}
    unanswered: list[str] = []
    excluded_rejections: list[str] = []
    responses = []
    for directory in sorted(data.glob("wire*")):
        for request_path in sorted(directory.glob("*request.json")):
            response_path = request_path.with_name(request_path.name.replace("request", "response"))
            if response_path.exists():
                responses.append((read(request_path), read(response_path)))
            else:
                name = request_path.relative_to(data).as_posix()
                if rejected.get(name) == hashlib.sha256(request_path.read_bytes()).hexdigest():
                    excluded_rejections.append(name)
                else:
                    unanswered.append(name)
    price_upper = sum((u["prompt_cache_hit_tokens"] * .04 + u["prompt_cache_miss_tokens"] * 2
                       + u["completion_tokens"] * 8) / 1_000_000
                      for _, response in responses for u in response["usage"])
    tariff_upper = 0.0
    for request, response in responses:
        local = datetime.fromtimestamp(request["started_at"], tz=timezone.utc) + timedelta(hours=8)
        peak = local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18)
        tariff_upper += sum((u["prompt_cache_hit_tokens"] * .04 + u["prompt_cache_miss_tokens"] * 2
                            + u["completion_tokens"] * 8) / 1_000_000 for u in response["usage"]) * (1 if peak else .5)
    unknown = sum(bool(req["request"]) and res["status"] == 200 and not res["usage"] for req, res in responses)
    rows = []
    for path in sorted(data.glob(f"{phase}*-q*/turn.json")):
        value = read(path)
        state = value["snapshot"]
        internal = path.parent / "internal-model-calls.json"
        calls = read(internal) if internal.exists() else state["model_calls"]
        used = state["usage"]
        begin = datetime.fromisoformat(used["submitted_at"].replace("Z", "+00:00")).timestamp()
        end = value["finished_at"]
        wire = [(req["request"], res) for req, res in responses if begin <= req["started_at"] <= end]
        wire_usage = [u for _, res in wire for u in res["usage"]]
        hit = sum(u["prompt_cache_hit_tokens"] for u in wire_usage)
        miss = sum(u["prompt_cache_miss_tokens"] for u in wire_usage)
        output = sum(u["completion_tokens"] for u in wire_usage)
        main = [req for req, _ in wire if req.get("tools")]
        schema = {tools_fingerprint(req["tools"]) for req in main}
        system = {str(req["messages"][0]) for req in main}
        schema_matches = bool(main) and schema == {
            c.get("tools_schema_fingerprint") for c in calls if c.get("tools_schema_fingerprint")
        } if any(c.get("tools_schema_fingerprint") for c in calls) else None
        epoch_path = path.parent / "compression-epochs.json"
        epochs = read(epoch_path) if epoch_path.exists() else {}
        call_ids = [c.get("model_call_id", "") for c in calls if c.get("tools_schema_fingerprint")]
        append_only, history_ok, history_changes = history_consistency(main, call_ids, epochs)
        answer = state["final_result"]["summary"]
        citations = []
        for match in CITATION.finditer(answer):
            name, first, last = match.groups()
            name = name.replace("\\", "/")
            first, last = int(first), int(last or first)
            exists = name in citation_files
            length = len((citation_root / name).read_text(encoding="utf-8").splitlines()) if exists else 0
            citations.append({"citation": match.group(), "valid_path_and_range": exists and 1 <= first <= last <= length})
        steps = state["plan"]["steps"]
        complete = state["session_state"] == "completed" and value["command"]["status"] == "completed"
        usage_matches = (hit, miss, output) == (
            used["actual_cache_read_tokens"], used["actual_uncached_input_tokens"], used["actual_output_tokens"])
        (path.parent / "answer.md").write_text(answer + "\n", encoding="utf-8")
        is_pressure = "pressure" in path.parent.name
        plan_ok = (bool(steps) or "-q1-" in path.parent.name) and all(s["status"] == "completed" for s in steps)
        shortened = re.findall(r"`:\d+(?:-\d+)?", answer)
        for reference in CITATION.finditer(answer):
            bare = re.match(r"[ \t`]*[,、][ \t`]*(\d+(?:-\d+)?)(?![\w.:\-]|\s+[A-Za-z])",
                            answer[reference.end():])
            if bare:
                shortened.append(reference.group() + bare.group())
        citation_ok = bool(citations) and not shortened and all(c["valid_path_and_range"] for c in citations)
        errors = [e["code"] for e in state["errors"]]
        output_ok = bool(wire_usage) and all(u["completion_tokens"] <= req.get("max_tokens", 0)
            for req, res in wire for u in res["usage"])
        display_ok = any(m.get("kind") == "assistant" and m.get("text") == answer
                         for m in state["messages"])
        rows.append({"run": path.parent.name, "complete": complete, "errors": errors,
                     "calls": len(calls), "plan_complete": plan_ok, "citation_count": len(citations),
                     "invalid_citations": [c["citation"] for c in citations if not c["valid_path_and_range"]],
                     "unresolved_short_citations": shortened,
                     "cache_read": hit, "cache_miss": miss, "output": output,
                     "cache_ratio": hit / (hit + miss) if hit + miss else None,
                     "peak_cost_cny": (hit * .04 + miss * 2 + output * 8) / 1_000_000,
                     "usage_matches_wire": usage_matches, "system_stable": len(system) == 1,
                     "schema_stable": len(schema) == 1, "recorded_schema_matches": schema_matches,
                     "append_only": append_only, "history_changes_explained": history_ok,
                     "history_changes": history_changes, "pressure": is_pressure,
                     "compression_model_calls": sum(c["purpose"] == "summarize" for c in calls),
                     "all_output_within_limit": output_ok, "display_matches_final": display_ok,
                     "mechanical_gate_pass": complete and not errors and usage_matches and citation_ok
                         and plan_ok and len(system) == 1 and len(schema) == 1 and history_ok
                         and output_ok and display_ok and schema_matches is not False})
    return {"phase": phase, "workspace_subdir": prefix, "expected_runs": expected_runs, "frozen_files": len(files), "workspace_unchanged": immutable,
            "all_attempts_peak_price_upper_cny": price_upper, "successful_responses_missing_usage": unknown,
            "unanswered_requests_without_local_rejection_record": unanswered,
            "manually_observed_local_rejections": excluded_rejections, "authorization_budget_cny": 5, "all_attempts_tariff_upper_cny": tariff_upper, "budget_pass": tariff_upper <= 5 and unknown == 0 and not unanswered,
            "runs": rows, "all_mechanical_gates_pass": len(rows) == expected_runs and immutable
                and all(r["mechanical_gate_pass"] for r in rows),
            "limits": "Citation checks prove paths/ranges only; semantic accuracy requires source review. "
                      "Shared cache and differing trajectories do not prove universal benefit. "
                      "Pressure runs inject a smaller configured window; provider capacity stays 1M."}


def main() -> int:
    """Write the offline audit and fail on missing or invalid delivery evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--phase", default="acceptance")
    parser.add_argument("--workspace-subdir", default="local-agent-web")
    parser.add_argument("--expected-runs", type=int, default=3)
    args = parser.parse_args()
    result = audit(args.data, args.phase, args.workspace_subdir, args.expected_runs)
    (args.data / f"audit-{args.phase}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_mechanical_gates_pass"] and result["budget_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

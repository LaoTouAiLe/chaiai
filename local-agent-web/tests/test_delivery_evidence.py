"""Delivery evidence must fail closed on missing runs, budget and citations."""
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "delivery_evidence", Path(__file__).resolve().parents[1] / "scripts/verify_delivery_evidence.py"
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def evidence(tmp_path):
    source = b"one\ntwo\n"
    root = tmp_path / "workspace-test/local-agent-web"
    root.mkdir(parents=True)
    (root / "config.yaml.example").write_bytes(source)
    save(tmp_path / "fixture-test.json", [{"path": "local-agent-web/config.yaml.example",
         "sha256": hashlib.sha256(source).hexdigest()}])
    start = datetime(2026, 10, 9, 13, tzinfo=timezone.utc).timestamp()
    answer = "config.yaml.example:1-2"
    state = {"model_calls": [], "usage": {"submitted_at": "2026-10-09T13:00:00Z",
        "actual_cache_read_tokens": 100, "actual_uncached_input_tokens": 50,
        "actual_output_tokens": 10}, "session_state": "completed", "errors": [],
        "final_result": {"summary": answer}, "messages": [{"kind": "assistant", "text": answer}],
        "plan": {"steps": [{"status": "completed"}]}}
    save(tmp_path / "test-q2/turn.json", {"snapshot": state, "command": {"status": "completed"},
         "finished_at": start + 5})
    save(tmp_path / "wire-test/1-request.json", {"started_at": start + 1,
         "request": {"messages": [{"role": "system", "content": "rules"}], "max_tokens": 16,
         "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}]}})
    save(tmp_path / "wire-test/1-response.json", {"status": 200, "usage": [{
         "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 50, "completion_tokens": 10}]})
    return tmp_path


def test_matching_frozen_evidence_passes_and_missing_run_fails(evidence):
    assert module.audit(evidence, "test", "local-agent-web", 1)["all_mechanical_gates_pass"]
    assert not module.audit(evidence, "test", "local-agent-web", 2)["all_mechanical_gates_pass"]
    save(evidence / "test-q2/internal-model-calls.json", [{"purpose": "tool_decision",
         "tools_schema_fingerprint": "mismatch"}])
    result = module.audit(evidence, "test", "local-agent-web", 1)
    assert result["runs"][0]["recorded_schema_matches"] is False
    assert not result["all_mechanical_gates_pass"]


def test_invalid_citation_and_changed_source_fail(evidence):
    turn_path = evidence / "test-q2/turn.json"
    turn = module.read(turn_path)
    turn["snapshot"]["final_result"]["summary"] = "config.yaml.example:3"
    save(turn_path, turn)
    result = module.audit(evidence, "test", "local-agent-web", 1)
    assert result["runs"][0]["invalid_citations"] == ["config.yaml.example:3"]
    assert not result["all_mechanical_gates_pass"]
    (evidence / "workspace-test/local-agent-web/config.yaml.example").write_text("changed")
    assert not module.audit(evidence, "test", "local-agent-web", 1)["workspace_unchanged"]


def test_budget_missing_usage_and_output_limit_fail(evidence):
    path = evidence / "wire-test/1-response.json"
    response = module.read(path)
    response["usage"][0]["completion_tokens"] = 2_000_000
    save(path, response)
    result = module.audit(evidence, "test", "local-agent-web", 1)
    assert not result["budget_pass"]
    assert not result["runs"][0]["all_output_within_limit"]
    response["usage"] = []
    save(path, response)
    assert not module.audit(evidence, "test", "local-agent-web", 1)["budget_pass"]

    path.unlink()
    result = module.audit(evidence, "test", "local-agent-web", 1)
    assert result["unanswered_requests_without_local_rejection_record"] == ["wire-test/1-request.json"]
    assert not result["budget_pass"]


def test_history_reset_requires_persisted_compression_or_new_saved_tool_projection():
    requests = [{"messages": ["system", "old evidence"]},
                {"messages": ["system", "summary", "recent evidence"]}]
    ids = ["old", "new"]
    assert module.history_consistency(requests, ids, {})[:2] == (False, False)
    unchanged = {"summary_covered_messages": 0, "tool_output_sha256": {}}
    assert module.history_consistency(requests, ids, {"old": unchanged, "new": unchanged})[:2] == (False, False)
    summary = {"summary_covered_messages": 3, "tool_output_sha256": {}}
    assert module.history_consistency(requests, ids, {"old": unchanged, "new": summary})[:2] == (False, True)
    saved = {"summary_covered_messages": 0, "tool_output_sha256": {"tool": "hash"}}
    assert module.history_consistency(requests, ids, {"old": unchanged, "new": saved})[:2] == (False, True)
    changed = {"summary_covered_messages": 0, "tool_output_sha256": {"tool": "different"}}
    assert module.history_consistency(requests, ids, {"old": saved, "new": changed})[:2] == (False, False)
    appended = [{"messages": ["system"]}, {"messages": ["system", "new evidence"]}]
    assert module.history_consistency(appended, [], {})[:2] == (True, True)


def test_short_citations_are_not_hidden_by_valid_full_citations(evidence):
    path = evidence / "test-q2/turn.json"
    turn = module.read(path)
    answer = "config.yaml.example:1；后文（`:2`）"
    turn["snapshot"]["final_result"]["summary"] = answer
    turn["snapshot"]["messages"] = [{"kind": "assistant", "text": answer}]
    save(path, turn)
    result = module.audit(evidence, "test", "local-agent-web", 1)
    assert result["runs"][0]["unresolved_short_citations"] == ["`:2"]
    assert not result["all_mechanical_gates_pass"]


def test_bare_citation_lists_fail_even_with_a_valid_first_reference(evidence):
    path = evidence / "test-q2/turn.json"
    turn = module.read(path)
    answer = "`config.yaml.example:1`、`2`"
    turn["snapshot"]["final_result"]["summary"] = answer
    turn["snapshot"]["messages"] = [{"kind": "assistant", "text": answer}]
    save(path, turn)
    result = module.audit(evidence, "test", "local-agent-web", 1)
    assert result["runs"][0]["unresolved_short_citations"]
    assert not result["all_mechanical_gates_pass"]

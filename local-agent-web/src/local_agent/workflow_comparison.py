"""T70 同题离线对照：真实工具、冻结 T64 校验、保留全部失败。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from local_agent.config import AppConfig
from local_agent.graph import builder, code_workflow
from local_agent.graph.state import ToolEvent
from local_agent.llm.usage_recording import ModelCallRecorder
from local_agent.workflow_eval import (
    CODE_CASE,
    WORKFLOW_CASES,
    WorkflowCase,
    _config,
    _script,
    _ScriptedModel,
    validate_outcome,
    workspace_hashes,
)

_PROPOSAL = {"old_text": "    return value\n", "new_text": "    return min(upper, max(lower, value))\n"}
_MODES = ("free_loop", "subgraph")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _model_spec(case: WorkflowCase) -> dict[str, Any]:
    return {
        "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "free_loop_script": [{"content": message.content, "tool_calls": message.tool_calls}
                             for message in _script(case)],
        "proposal_instruction": code_workflow._INSTRUCTION,
        "proposal": _PROPOSAL, "usage_metadata": None,
    }


class _ComparisonModel(_ScriptedModel):
    """两臂同一模型；仅依据实际消息协议选固定应答，没有臂标志。"""

    requests: list[dict[str, Any]] = Field(default_factory=list)
    max_retries: int = 2

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: Any = None, **kwargs: Any,
    ) -> ChatResult:
        proposal = bool(messages and isinstance(messages[0], SystemMessage)
                        and messages[0].content == code_workflow._INSTRUCTION)
        entry: dict[str, Any] = {
            "protocol": "proposal" if proposal else "free_loop",
            "input_sha256": _fingerprint([{
                "type": message.type, "content": message.content,
                "tool_calls": getattr(message, "tool_calls", []),
            } for message in messages]),
            "output_sha256": None,
        }
        # 工作流会浅复制模型以关闭 SDK 重试；共享请求列表记录实际调用次数。
        self.requests.append(entry)
        if proposal:
            if len(self.requests) != 1:
                raise RuntimeError("固定提议协议不允许额外模型调用")
            result = ChatResult(generations=[ChatGeneration(message=AIMessage(
                content=json.dumps(_PROPOSAL),
            ))])
        else:
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        response = result.generations[0].message
        entry["output_sha256"] = _fingerprint({
            "content": response.content, "tool_calls": getattr(response, "tool_calls", []),
        })
        return result


def _selection(case: WorkflowCase) -> dict[str, Any]:
    if case.case_id == CODE_CASE.case_id:
        return {"source_path": "bounds.py", "test_paths": ["test_bounds.py"],
                "expected_test_count": 3}
    return {"pattern": "docs/*.md", "keywords": ["取消", "执行结果", "能力"]}


def _configuration(root: Path, case: WorkflowCase) -> tuple[AppConfig, str]:
    config = _config(root, case.profile)
    config.workflows.enabled = True
    public = config.model_dump(mode="json", exclude={"workspace": {"root"}})
    return config, _fingerprint(public)


def _seed_hashes(case: WorkflowCase) -> dict[str, str]:
    return {path: hashlib.sha256(content.encode()).hexdigest() for path, content in case.seeds}


def _sanitize(value: Any, root: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(root), "<workspace>").replace(root.as_posix(), "<workspace>")
    if isinstance(value, list):
        return [_sanitize(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize(item, root) for key, item in value.items()}
    return value


def _run_arm(case: WorkflowCase, mode: str, attempt: int) -> dict[str, Any]:
    with TemporaryDirectory(prefix="chaiai-v07-comparison-") as directory:
        root = Path(directory)
        for path, content in case.seeds:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode())
        config, config_fingerprint = _configuration(root, case)
        model = _ComparisonModel(messages=iter([]), script=_script(case))
        recorder = ModelCallRecorder(
            connection_id="offline", config_version="v07-t70", provider="scripted",
            model_id="scripted-offline", endpoint_fingerprint="offline",
            context_window_tokens=config.require_llm().context_window_tokens,
        )
        selection = _selection(case) if mode == "subgraph" else None
        context = builder.build_runtime_context(
            app_config=config.as_run_config(), model_adapter=model,
            workspace_root=root, current_cwd=root, usage_recorder=recorder,
            code_change=selection if case.case_id == CODE_CASE.case_id else None,
            readonly_research=selection if case.case_id != CODE_CASE.case_id else None,
        )
        before = workspace_hashes(root)
        start = time.perf_counter()
        state: Mapping[str, Any] = {}
        error = None
        try:
            state = builder.build_chat_graph().invoke(
                {"user_input": case.prompt, "session_mode": "agent"}, context=context,
            )
        except Exception as exc:  # noqa: BLE001 — 每个尝试独立保留失败，不生成成功结果。
            error = type(exc).__name__
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        failures = validate_outcome(case, root, state)
        if error:
            failures.append(f"运行异常：{error}")
        events = [event for event in state.get("tool_events", []) if isinstance(event, ToolEvent)]
        outputs = []
        for message in state.get("messages", []):
            if isinstance(message, ToolMessage) and isinstance(message.content, str):
                try:
                    body = json.loads(message.content)
                except ValueError:
                    body = {"unparsed_sha256": hashlib.sha256(message.content.encode()).hexdigest()}
                outputs.append({"tool_call_id": message.tool_call_id, "name": message.name,
                                "output": _sanitize(body, root)})
        workflow = state.get("workflow") or {}
        return {
            "case_id": case.case_id, "attempt": attempt, "mode": mode,
            "task_fingerprint": case.fingerprint, "profile": case.profile,
            "model": "scripted-offline", "model_fingerprint": _fingerprint(_model_spec(case)),
            "evaluation_config_fingerprint": config_fingerprint,
            "seed_sha256": before, "final_file_sha256": workspace_hashes(root),
            "workflow_request": selection, "status": state.get("task_status"),
            "passed": not failures, "failures": failures, "runtime_error": error,
            "model_calls": len(model.requests), "model_requests": model.requests,
            "tool_calls": len(events) if error is None else None,
            "tool_audit_complete": error is None, "tool_events": [{
                "tool_name": event.tool_name, "tool_call_id": event.tool_call_id,
                "status": event.status, "arguments": _sanitize(event.redacted_arguments, root),
            } for event in events],
            "tool_outputs": outputs, "final_response": _sanitize(state.get("final_response"), root),
            "loop_count": state.get("loop_count"), "workflow_steps": workflow.get("steps"),
            "workflow_model_tokens_reserved": workflow.get("model_tokens_reserved"),
            "configured_limits": {"free_loop": {"max_tool_loops": config.agent.max_tool_loops},
                                  "workflows": config.workflows.model_dump(mode="json")},
            "elapsed_ms": elapsed_ms, "actual_input_tokens": None,
            "actual_output_tokens": None, "actual_cache_read_tokens": None,
        }


def summarize_comparison(rows: list[dict[str, Any]], repetitions: int) -> dict[str, Any]:
    """缺样本、失败、公平性或审计门不通过时，禁止声明离线优势。"""
    failures: list[str] = []
    cases = []
    expected_keys = {(case.case_id, attempt, mode) for case in WORKFLOW_CASES
                     for attempt in range(1, repetitions + 1) for mode in _MODES}
    keys = [(row.get("case_id"), row.get("attempt"), row.get("mode")) for row in rows]
    if repetitions < 1 or len(keys) != len(expected_keys) or set(keys) != expected_keys:
        failures.append("样本缺失、重复或出现未登记样本")
    for case in WORKFLOW_CASES:
        selected = [row for row in rows if row.get("case_id") == case.case_id]
        case_failures: list[str] = []
        _, expected_config = _configuration(Path("."), case)
        for row in selected:
            label = f"{case.case_id}/{row.get('attempt')}/{row.get('mode')}"
            if (row.get("task_fingerprint") != case.fingerprint or row.get("profile") != case.profile
                    or row.get("model") != "scripted-offline"
                    or row.get("model_fingerprint") != _fingerprint(_model_spec(case))
                    or row.get("evaluation_config_fingerprint") != expected_config
                    or row.get("seed_sha256") != _seed_hashes(case)
                    or row.get("workflow_request") != (
                        _selection(case) if row.get("mode") == "subgraph" else None)):
                case_failures.append(f"{label}：同题/配置/种子/模型/选择不一致")
            if row.get("passed") is not True or row.get("failures") != [] or row.get("status") != "completed":
                case_failures.append(f"{label}：机械验收未通过")
            expected_files = {path: hashlib.sha256(content.encode()).hexdigest()
                              for path, content in case.expected_files}
            if row.get("final_file_sha256") != expected_files:
                case_failures.append(f"{label}：最终文件清单或 Hash 不符合固定期望")
            events = row.get("tool_events", [])
            allowed = ({"read_file", "edit_file", "run_shell"} if case.test_command
                       else {"read_file", "grep", "glob", "list_dir"})
            allowed.add("task_control")
            edits = [event for event in events if event.get("tool_name") == "edit_file"]
            if (any(event.get("tool_name") not in allowed or event.get("status") != "ok"
                    for event in events) or len(edits) != (1 if case.test_command else 0)
                    or any(event.get("arguments", {}).get("path") != "bounds.py" for event in edits)
                    or any(event.get("tool_name") == "run_shell"
                           and event.get("arguments", {}).get("command") != case.test_command
                           for event in events)):
                case_failures.append(f"{label}：工具权限或重复副作用门失败")
            if (row.get("tool_calls") != len(events)
                    or row.get("model_calls") != len(row.get("model_requests", []))
                    or row.get("tool_audit_complete") is not True):
                case_failures.append(f"{label}：调用计数与实际审计不一致")
        free = [row for row in selected if row.get("mode") == "free_loop"]
        workflow = [row for row in selected if row.get("mode") == "subgraph"]
        means = {mode: {
            metric: (sum(row[metric] for row in group) / len(group)
                     if group and all(isinstance(row.get(metric), (int, float)) for row in group)
                     else None)
            for metric in ("model_calls", "tool_calls", "elapsed_ms")
        } for mode, group in (("free_loop", free), ("subgraph", workflow))}
        improvement = bool(not failures and not case_failures and len(free) == repetitions
                           and len(workflow) == repetitions and all(
                               next(row["model_calls"] for row in workflow if row["attempt"] == attempt)
                               < next(row["model_calls"] for row in free if row["attempt"] == attempt)
                               for attempt in range(1, repetitions + 1)))
        cases.append({"case_id": case.case_id, "attempts_per_arm": {"free_loop": len(free),
                      "subgraph": len(workflow)}, "completion_counts": {
                          mode: sum(row.get("passed") is True for row in group)
                          for mode, group in (("free_loop", free), ("subgraph", workflow))},
                      "means": means, "failures": case_failures,
                      "model_calls_improved_in_every_pair": improvement})
        failures.extend(case_failures)
    advantage = not failures and all(case["model_calls_improved_in_every_pair"] for case in cases)
    return {"cases": cases, "hard_gates_passed": not failures, "failures": failures,
            "offline_subgraph_advantage_proven": advantage, "passed": advantage}


def run_comparison(repetitions: int = 3) -> dict[str, Any]:
    """交替顺序跑两题；只证明离线调用编排改善，保留每臂完整机械证据。"""
    if not 1 <= repetitions <= 10:
        raise ValueError("repetitions 必须为 1–10")
    rows = []
    for case in WORKFLOW_CASES:
        for attempt in range(1, repetitions + 1):
            for mode in (_MODES if attempt % 2 else tuple(reversed(_MODES))):
                rows.append(_run_arm(case, mode, attempt))
    project = Path(__file__).resolve().parents[2]
    sources = sorted((project / "src").rglob("*.py")) + [
        project / "scripts" / "run_workflow_baseline.py",
        project / "scripts" / "run_workflow_comparison.py",
        project / "tests" / "test_workflow_eval.py",
        project / "tests" / "test_workflow_comparison.py",
    ]
    return {
        "schema_version": 1, "task_set": "v07-complex-v1", "model_kind": "scripted",
        "repetitions": repetitions, "execution_order": [
            [row["case_id"], row["attempt"], row["mode"]] for row in rows],
        "source_sha256": {path.relative_to(project).as_posix(): hashlib.sha256(
            path.read_bytes()).hexdigest() for path in sources},
        "paid_model_calls": 0, "semantic_quality_reviewed": False,
        "real_model_advantage_proven": False,
        "limitations": ["固定脚本已知正确修复；仅证明离线编排调用改善",
                        "只读工作流整理原文，不进行模型语义归纳",
                        "两臂配置相同；自由循环与工作流预算执行门仍各用原实现",
                        "耗时仅记录；未知实际 Token/缓存为 null，预留不是实际用量"],
        "runs": rows, **summarize_comparison(rows, repetitions),
    }

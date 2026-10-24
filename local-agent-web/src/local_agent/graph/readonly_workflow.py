"""T65 固定只读调查 Subgraph；工具执行复用既有两道门（架构 §18.2）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, TypedDict, cast

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from local_agent.graph.nodes import (
    SAFETY_GATE_NODE,
    TOOL_EXECUTION_NODE,
    safety_gate_node,
    tool_execution_node,
)
from local_agent.graph.state import AgentState, EvidenceReference, ReflectionResult, RuntimeContext
from local_agent.llm.protocol import describe_parse_failure
from local_agent.llm.usage_recording import TurnRecord
from local_agent.session_kind import SessionKind
from local_agent.workflow_contracts import (
    READONLY_RESEARCH,
    ReadonlyResearchRequest,
    ReadonlyWorkflowState,
    WorkflowReference,
)


class _Input(TypedDict, total=False):
    # 故意不输入追加型工具事件/结果/轨迹：子图只返回新增记录，避免父 reducer 重复追加历史。
    run_id: str
    user_input: str
    mode: str
    session_mode: str
    session_kind: SessionKind
    messages: list[AnyMessage]
    agent_loop_version: int
    run_user_message_id: str
    run_submitted_at: datetime
    turn_records: dict[str, TurnRecord]


class _Output(TypedDict, total=False):
    workflow: ReadonlyWorkflowState | None
    messages: list[AnyMessage]
    tool_events: list[Any]
    tool_results: list[Any]
    decision_trace: list[Any]
    error_events: list[Any]
    model_call_records: list[Any]
    turn_records: dict[str, TurnRecord]
    pending_tool_calls: list[Any]
    pending_approval: Any
    final_response: str | None
    task_status: str
    reflection_result: ReflectionResult | None
    final_evidence: list[EvidenceReference]


def _failed(data: ReadonlyWorkflowState, reason: str) -> AgentState:
    data = {**data, "phase": "failed", "error": reason}
    text = f"只读调查未完成：{reason}"
    return {"workflow": data, "task_status": "failed", "pending_tool_calls": [],
            "pending_approval": None, "final_response": text,
            "messages": [AIMessage(content=text)],
            "reflection_result": ReflectionResult(goal_achieved=False, result_trust="failed", next_action="ask_user")}


def _tick(state: AgentState, runtime: Runtime[RuntimeContext]) -> ReadonlyWorkflowState | None:
    data = cast(ReadonlyWorkflowState | None, state.get("workflow"))
    if data is None or data["steps"] >= runtime.context.app_config.workflows.max_steps:
        return None
    return {**data, "steps": data["steps"] + 1}


def initialize(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """先核对配置、输入与能力；无副作用、无模型调用。"""
    data: ReadonlyWorkflowState = {
        "name": READONLY_RESEARCH, "run_id": state["run_id"], "phase": "locate",
        "request": {"pattern": "", "keywords": []}, "steps": 1, "tool_calls": 0, "cursor": 0,
        "files": [], "references": [], "error": None,
    }
    if not runtime.context.app_config.workflows.enabled:
        return _failed(data, "工作流未启用")
    if state.get("session_mode") != "agent":
        return _failed(data, "只读工作流需要 Agent 模式")
    try:
        request = ReadonlyResearchRequest.model_validate(runtime.context.readonly_research)
    except ValidationError as exc:
        # 带上脱敏原因（字段 + 失败类型）：只说"输入不合法"的话，调用方无从判断该改哪个字段
        # （2026-10-14 与委派子任务同一批修的同一种折叠）。
        return _failed(
            data, f"输入不合法，请提供工作区内相对文件模式与字面关键词（{describe_parse_failure(exc)}）"
        )
    if not {"files_read", "code_search"} <= set(runtime.context.agent_capabilities):
        return _failed(data, "父 Profile 缺少 files_read 或 code_search 能力")
    data["request"] = {"pattern": request.pattern, "keywords": list(request.keywords)}
    return {"workflow": data, "task_status": "running", "mode": "tool", "pending_tool_calls": []}


def prepare_call(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """在发起每个工具调用前检查并预留步骤与工具额度。"""
    data = _tick(state, runtime)
    if data is None:
        return _failed(state["workflow"], "达到工作流步骤上限")  # type: ignore[arg-type]
    config = runtime.context.app_config.workflows
    if data["tool_calls"] >= config.max_tool_calls:
        return _failed(data, "达到工作流工具调用上限")
    arguments: dict[str, Any]
    if data["phase"] == "locate":
        name, arguments = "glob", {"pattern": data["request"]["pattern"],
                                   "max_files": config.max_source_files + 1}
    else:
        name, arguments = "read_file", {"path": data["files"][data["cursor"]],
                                        "max_chars": config.max_evidence_chars}
    data["tool_calls"] += 1
    call: ToolCall = {"name": name, "args": arguments,
            "id": f"{data['run_id']}:readonly:{data['tool_calls']}", "type": "tool_call"}
    return {"workflow": data, "pending_tool_calls": [call],
            "messages": [AIMessage(content="", tool_calls=[call])]}


def _restricted(runtime: Runtime[RuntimeContext]) -> Runtime[RuntimeContext]:
    return replace(runtime, context=replace(runtime.context, agent_capabilities=tuple(
        capability for capability in runtime.context.agent_capabilities
        if capability in {"files_read", "code_search"}
    )))


def readonly_safety(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """用权限交集调用原安全门，不能因父 Profile 可写而允许子图写。"""
    return safety_gate_node(state, _restricted(runtime))


def readonly_execution(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """用同一权限交集调用原执行节点，保留取消与安全纵深检查。"""
    return tool_execution_node(state, _restricted(runtime))


def after_safety(state: AgentState) -> str:
    """需额外审批或全部拒绝时只观察失败，不另建审批恢复链。"""
    return "observe" if state.get("pending_approval") or not state.get("pending_tool_calls") else "execute"


def observe(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """核对当前调用的实际成功事件与正文；历史记录和预览不能充当证据。"""
    data = _tick(state, runtime)
    if data is None:
        return _failed(state["workflow"], "达到工作流步骤上限")  # type: ignore[arg-type]
    identity = f"{data['run_id']}:readonly:{data['tool_calls']}"
    event = next((event for event in state.get("tool_events", [])
                  if event.run_id == data["run_id"] and event.tool_call_id == identity), None)
    message = next((message for message in reversed(state.get("messages", []))
                    if isinstance(message, ToolMessage) and message.tool_call_id == identity), None)
    if event is None or event.status != "ok" or message is None or message.name != event.tool_name:
        return _failed(data, "工具未成功或需要额外审批，请使用普通审批流程处理")
    try:
        payload = json.loads(message.content) if isinstance(message.content, str) else None
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and data["phase"] == "locate" and payload.get("truncated") is True:
        return _failed(data, "定位结果不完整，文件数或检索量达到上限")
    if not isinstance(payload, dict) or payload.get("truncated") is not False:
        return _failed(data, "工具返回不完整或缺少可核对正文")
    if data["phase"] == "locate":
        files = payload.get("files")
        if (event.tool_name != "glob" or not isinstance(files, list) or not files
                or any(not isinstance(path, str) for path in files)
                or len(set(files)) != len(files)):
            return _failed(data, "未定位到完整有效的文件清单")
        if len(files) > runtime.context.app_config.workflows.max_source_files:
            return _failed(data, "定位文件数超过工作流上限")
        data["files"] = files
        data["phase"] = "read"
    else:
        path = data["files"][data["cursor"]]
        content, first, last = payload.get("content"), payload.get("first_line"), payload.get("last_line")
        digest = payload.get("sha256")
        if (event.tool_name != "read_file" or payload.get("path") != path
                or not isinstance(content, str) or not isinstance(first, int) or first < 1
                or not isinstance(last, int) or not isinstance(digest, str)
                or len(content.splitlines()) != last - first + 1):
            return _failed(data, "读取正文、路径与行号不一致")
        keywords = data["request"].get("keywords", [])
        refs: list[WorkflowReference] = [
            {"path": path, "line": number, "text": line, "tool_call_id": identity, "sha256": digest}
            for number, line in enumerate(content.splitlines(), first)
            if line.strip() and (not keywords or any(word in line for word in keywords))
        ]
        references = [*data["references"], *refs]
        if sum(len(ref["text"]) for ref in references) > runtime.context.app_config.workflows.max_evidence_chars:
            return _failed(data, "证据正文超过工作流上限")
        data["references"] = references
        data["cursor"] += 1
        if data["cursor"] == len(data["files"]):
            data["phase"] = "verify"
    return {"workflow": data, "pending_tool_calls": []}


def verify(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """每个关键词都必须有真实原文，不用模型猜测补齐。"""
    data = _tick(state, runtime)
    if data is None:
        return _failed(state["workflow"], "达到工作流步骤上限")  # type: ignore[arg-type]
    if not data["references"] or any(
        not any(word in ref["text"] for ref in data["references"])
        for word in data["request"].get("keywords", [])
    ):
        return _failed(data, "缺少覆盖调查关键词的原文证据")
    data["phase"] = "report"
    return {"workflow": data}


def report(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """输出有界原文引用，不调用模型、不解释源文件中的指令。"""
    data = _tick(state, runtime)
    if data is None:
        return _failed(state["workflow"], "达到工作流步骤上限")  # type: ignore[arg-type]
    if runtime.context.cancellation is not None and runtime.context.cancellation.cancelled:
        return _failed(data, "本轮已取消")
    text = "原文调查结果：\n" + "\n".join(
        f"{ref['text']}（{ref['path']}:{ref['line']}）" for ref in data["references"]
    )
    if len(text) > runtime.context.app_config.workflows.max_report_chars:
        return _failed(data, "报告超过工作流上限")
    data["phase"] = "completed"
    sources = {ref["tool_call_id"]: ref for ref in data["references"]}
    evidence = [EvidenceReference(
        source_event_id=identity, evidence_type="file_hash",
        summary=f"{ref['path']} sha256={ref['sha256']}", verified=True,
        created_at=datetime.now(timezone.utc),
    ) for identity, ref in sources.items()]
    return {"workflow": data, "final_response": text, "messages": [AIMessage(content=text)],
            "final_evidence": evidence,
            "task_status": "completed", "reflection_result": ReflectionResult(
                goal_achieved=True, result_trust="verified", next_action="finish",
                evidence=tuple(evidence),
            )}


def next_phase(state: AgentState) -> str:
    """固定的闭集边，不接受输入提供的下一节点名。"""
    data = state.get("workflow")
    if data is None or data["phase"] == "failed":
        return "end"
    return "verify" if data["phase"] == "verify" else "prepare"


def build_readonly_subgraph(wrap: Callable[[str, Callable[..., Any]], Callable[..., Any]]) -> Any:
    """编译固定子图；复用父图的用量装饰器和原工具节点。"""
    graph = StateGraph(AgentState, context_schema=RuntimeContext, input_schema=_Input, output_schema=_Output)
    for name, node in (("initialize", initialize), ("prepare", prepare_call),
                       (SAFETY_GATE_NODE, readonly_safety), (TOOL_EXECUTION_NODE, readonly_execution),
                       ("observe", observe), ("verify", verify), ("report", report)):
        graph.add_node(name, wrap(name, node))
    graph.add_edge(START, "initialize")
    graph.add_conditional_edges("initialize", next_phase, {"prepare": "prepare", "end": END})
    graph.add_conditional_edges("prepare", next_phase, {"prepare": SAFETY_GATE_NODE, "end": END})
    graph.add_conditional_edges(SAFETY_GATE_NODE, after_safety,
                                {"observe": "observe", "execute": TOOL_EXECUTION_NODE})
    graph.add_edge(TOOL_EXECUTION_NODE, "observe")
    graph.add_conditional_edges("observe", next_phase, {"prepare": "prepare", "verify": "verify", "end": END})
    graph.add_conditional_edges("verify", lambda state: "end" if state["workflow"]["phase"] == "failed" else "report",
                                {"end": END, "report": "report"})
    graph.add_edge("report", END)
    return graph.compile()

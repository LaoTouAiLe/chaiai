"""图构建（开发文档 T05）。

模型、配置与安全策略都不放在图里，而是随每次调用的 ``RuntimeContext`` 注入
（架构 §4.1）：同一张编译好的图既能用真实模型，也能用测试假模型与假策略。
"""

from __future__ import annotations

import inspect
import logging
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from local_agent.app_paths import data_dir
from local_agent.attachments import LoadedAttachment
from local_agent.cancellation import CancellationToken
from local_agent.capabilities import enabled_capabilities, resolve_switches
from local_agent.config import RunConfig, apply_capability_gates, redact_secrets
from local_agent.graph.code_workflow import build_code_subgraph
from local_agent.graph.delegated_workflow import (
    build_delegated_subgraph,
    delegation_audit_update,
    make_delegation_runner,
)
from local_agent.graph.edges import ChatStateGraph, add_minimal_chat_edges
from local_agent.graph.nodes import (
    AGENT_DECISION_NODE,
    APPROVAL_INTERRUPT_NODE,
    CHAT_NODE,
    COMPRESSION_NODE,
    FINAL_RESPONSE_NODE,
    LOAD_AGENTS,
    MEMORY_WRITE_NODE,
    PLANNER_NODE,
    PREPARE_CONTEXT,
    RECALL_MEMORY,
    REFLECTION_NODE,
    RETRIEVE_CODE_NODE,
    SAFETY_GATE_NODE,
    TASK_MEMORY_NODE,
    TOOL_EXECUTION_NODE,
    TOOL_FEEDBACK_NODE,
    agent_decision_node,
    approval_interrupt_node,
    chat_node,
    compression_node,
    final_response_node,
    load_agents_node,
    memory_write_node,
    planner_node,
    prepare_context_node,
    recall_memory_node,
    reflection_node,
    retrieve_code_node,
    safety_gate_node,
    task_memory_node,
    tool_execution_node,
    tool_feedback_node,
)
from local_agent.graph.readonly_workflow import build_readonly_subgraph
from local_agent.graph.routers import (
    COMPLETION_ROUTER,
    MODE_ROUTER,
    completion_router_node,
    mode_router_node,
)
from local_agent.graph.scope import tool_scope_key
from local_agent.graph.state import (
    AgentState,
    RuntimeContext,
    ToolEvent,
    make_error_event,
    merge_appended,
    model_call_key,
)
from local_agent.llm.models import ModelCapabilities, probe_capabilities
from local_agent.llm.usage_recording import (
    DEFAULT_PURPOSE,
    PURPOSE_BY_NODE,
    ModelCallRecorder,
    TurnRecord,
    use_model_purpose,
    use_model_recorder,
)
from local_agent.mcp.binding import McpBinding, McpManagerLike
from local_agent.mcp.catalog import ToolEntry, ToolExposure
from local_agent.media import (
    MAX_IMAGE_EDGE,
    MAX_UPLOAD_IMAGE_BYTES,
    detect_image_media_type,
    image_dimensions,
)
from local_agent.message_flags import DEFAULT_IMAGE_CONTEXT_TURNS
from local_agent.performance import timed_node
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.mcp_tools import build_mcp_tools
from local_agent.tools.tool_registry import ToolRegistry
from local_agent.workflow_contracts import CODE_CHANGE, DELEGATED_RESEARCH, READONLY_RESEARCH

#: 当前阶段的图类型：State 为 AgentState，运行上下文为 RuntimeContext。
ChatGraph = CompiledStateGraph[AgentState, RuntimeContext, AgentState, AgentState]


def build_chat_graph(checkpointer: BaseCheckpointSaver | None = None) -> ChatGraph:
    """构建当前阶段的图（架构 §5.2 闲聊分支 + §5.3 工具分支）。

    Args:
        checkpointer: 传入 SQLite saver 时启用会话持久化（T03）；为 ``None`` 时图无状态。

    Returns:
        已编译的图；调用时需通过 ``context=`` 传入 :class:`RuntimeContext`，
        并在 ``config={"configurable": {"thread_id": ...}}`` 中给出会话键。
    """
    graph: ChatStateGraph = StateGraph(AgentState, context_schema=RuntimeContext)

    def add(name: str, node: Callable[..., Any]) -> None:
        # 每个节点都经用量装饰器：它是"所有模型调用共用同一记录入口"的接线点。
        graph.add_node(name, _usage_aware(name, node))

    add(PREPARE_CONTEXT, prepare_context_node)
    add(LOAD_AGENTS, load_agents_node)
    add(RECALL_MEMORY, recall_memory_node)
    add(MODE_ROUTER, mode_router_node)
    add(RETRIEVE_CODE_NODE, retrieve_code_node)
    add(COMPRESSION_NODE, compression_node)
    add(PLANNER_NODE, planner_node)
    add(AGENT_DECISION_NODE, agent_decision_node)
    add(SAFETY_GATE_NODE, safety_gate_node)
    add(APPROVAL_INTERRUPT_NODE, approval_interrupt_node)
    add(TOOL_EXECUTION_NODE, tool_execution_node)
    add(TOOL_FEEDBACK_NODE, tool_feedback_node)
    add(REFLECTION_NODE, reflection_node)
    add(COMPLETION_ROUTER, completion_router_node)
    add(MEMORY_WRITE_NODE, memory_write_node)
    add(TASK_MEMORY_NODE, task_memory_node)
    add(CHAT_NODE, chat_node)
    add(FINAL_RESPONSE_NODE, final_response_node)
    graph.add_node(READONLY_RESEARCH, build_readonly_subgraph(_usage_aware))
    graph.add_node(CODE_CHANGE, build_code_subgraph(_usage_aware))
    graph.add_node(DELEGATED_RESEARCH, build_delegated_subgraph(_usage_aware, checkpointer))
    add_minimal_chat_edges(graph)
    return graph.compile(checkpointer=checkpointer).with_config({"recursion_limit": sys.maxsize})


def close_interrupted_turn(graph: ChatGraph, config: RunnableConfig,
                           state: Mapping[str, Any], request_id: str) -> None:
    """核对后只写终态检查点；不执行节点、模型或工具。"""
    run_id = str(state.get("run_id") or "run")
    text = "本轮执行已中断，已由你核对后结束。已执行的工具操作保留，未继续或重复执行工具。"
    missing: dict[str, Any] = {}
    for message in state.get("messages") or []:
        if isinstance(message, HumanMessage):
            missing.clear()
        elif isinstance(message, AIMessage):
            missing.update((str(call["id"]), call) for call in message.tool_calls if call.get("id"))
        elif isinstance(message, ToolMessage):
            missing.pop(message.tool_call_id, None)
    unknown = "工具执行结果未完整记录；本轮已由用户核对后结束，不要重放该工具。已发生的副作用保留。"
    replies = [ToolMessage(content=unknown, tool_call_id=call_id, name=call.get("name"),
                           id=f"{run_id}:interrupted-result:{call_id}") for call_id, call in missing.items()]
    recorded = {tool_scope_key(event.run_id, event.tool_call_id) for event in state.get("tool_events") or []}
    events = [ToolEvent(tool_call_id=call_id, tool_name=str(call.get("name") or ""),
                       run_id=run_id, status="unknown", output_preview=unknown)
              for call_id, call in missing.items() if tool_scope_key(run_id, call_id) not in recorded]
    update: AgentState = {
        "task_status": "failed", "pending_tool_calls": [], "pending_approval": None,
        "final_response": text, "messages": [*replies, AIMessage(content=text)],
        "tool_events": events,
        "error_events": [make_error_event(run_id, stage="interrupted_turn",
            message="执行结果曾需核对，用户确认核对后结束本轮；未重放任何工具。", salt=request_id)],
    }
    turn = (state.get("turn_records") or {}).get(run_id)
    if turn is not None and turn.final_at is None:
        now = datetime.now(timezone.utc)
        update["turn_records"] = {run_id: replace(turn,
            final_at=now, final_state="failed",
            turn_elapsed_ms=max(0, round((now - turn.submitted_at).total_seconds() * 1000)),
        )}
    checkpoint_config = graph.get_state(config).config
    # SQLite saver 读取根图时默认空命名空间，但 update_state 的写入要求显式携带它。
    checkpoint_config["configurable"].setdefault("checkpoint_ns", "")
    graph.update_state(checkpoint_config, update, as_node=FINAL_RESPONSE_NODE)


logger = logging.getLogger(__name__)

#: 诊断版（2026-10-20）：模型调用失败的原因写在这里。冻结形态落在
#: ``%LOCALAPPDATA%/chaiai/模型调用失败.log``（与设置库同一个数据目录），源码形态落在当前目录。
#: 只写异常类型、原因首段与消息条数——不写密钥、不写上下文正文。
_MODEL_FAILURE_LOG = "模型调用失败.log"
_KEY_LIKE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")


def _record_model_failure(exc: BaseException, state: Mapping[str, Any], node: str) -> None:
    """把一次模型调用失败的原因记到控制台与数据目录。

    为什么需要它：页面上的"本轮模型调用失败"只带异常**类型名**（``error_events`` 的 message
    刻意不回显供应商原文），于是形如 400 的字段级原因在分发出去的 exe 上完全拿不到——
    2026-10-20 的排查就卡在这里。这里把 ``str(exc)``（供应商返回的 message）脱敏后落盘，
    与页面上"不回显原文"的口径不冲突：这是本机日志，不是页面。
    """
    detail = redact_secrets(f"{type(exc).__name__}: {exc}", ())
    detail = _KEY_LIKE.sub("***", detail)[:4000]
    logger.warning("模型调用失败（%s）：%s", node, detail)
    base = data_dir()
    if base is None:
        # 源码形态没有独立的可写数据目录：只打控制台，不往当前目录丢文件（也免得测试落盘）。
        return
    path = base / _MODEL_FAILURE_LOG
    block = "\n".join((
        f"时间: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"阶段: {node}",
        f"运行: {state.get('run_id')}",
        f"消息数: {len(state.get('messages') or [])}",
        f"异常: {detail}",
        "",
    ))
    try:
        base.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(block + "\n")
    except OSError:
        logger.warning("模型失败日志写不进去：%s", path)


def _usage_aware(name: str, node: Callable[..., Any]) -> Callable[..., Any]:
    """把节点包成"会记录用量"的版本（架构 §15.6）。

    三件事，都只在这一处发生：

    1. **设置记录上下文**：把 ``RuntimeContext.usage_recorder`` 与产品级 ``purpose`` 放进
       contextvar，节点内部的任何 ``record_model_call`` 都取得到它们（无需改调用点签名）；
    2. **把新记录并回 State**：``model_call_records`` 是明细真源，按 ``model_call_id`` 去重后追加；
    3. **更新整轮记录**：写入本轮的 ``TurnRecord``（身份、时间线、关联调用与工具事件），
       终态由 :func:`merge_turn_records` 保证不被改写。
    """
    purpose = PURPOSE_BY_NODE.get(name, DEFAULT_PURPOSE)
    takes_runtime = len(inspect.signature(node).parameters) >= 2

    def wrapped(state: AgentState, runtime: Any) -> AgentState:
        context = getattr(runtime, "context", None)
        recorder = getattr(context, "usage_recorder", None)
        if isinstance(recorder, ModelCallRecorder):
            recorder.bind_dynamic_context(state)
        with use_model_recorder(recorder), use_model_purpose(purpose):
            try:
                update = node(state, runtime) if takes_runtime else node(state)
            except Exception as exc:
                # 仅闭合不执行工具的两个模型节点；工具/持久化异常仍交应用层核对副作用。
                if name not in {AGENT_DECISION_NODE, CHAT_NODE}:
                    raise
                # 诊断（2026-10-20）：先把真实原因留给本机日志，再用统一文案闭合本轮。
                _record_model_failure(exc, state, name)
                text = "本轮模型调用失败，已结束本轮。请检查模型连接后再提问；此前已执行的工具操作保留。"
                update = {
                    "messages": [AIMessage(content=text)],
                    "final_response": text,
                    "task_status": "failed",
                    "pending_tool_calls": [],
                    "error_events": [make_error_event(
                        str(state.get("run_id") or "run"), stage=name,
                        message=f"模型调用失败（{type(exc).__name__}），未继续执行本次决策。",
                        loop_count=int(state.get("loop_count", 0)),
                    )],
                }
        # 委派审计（§18.7）：工具只能返回字符串，所以子任务的工具事件与模型调用明细先落在
        # `delegation_audit` 槽里，由**节点收尾**改写成父轮次的 run_id 后并回父 State——
        # 于是轨迹页与用量页都看得到这次委派，整轮汇总也不会漏掉它（§18.3 的"不建第二份真源"）。
        # 放在这里而不是工具函数里：工具函数不知道父 `run_id`，也无法把列表交回状态。
        audit = getattr(context, "delegation_audit", None)
        if audit:
            merged_audit = delegation_audit_update(audit, str(state.get("run_id") or "run"))
            audit.clear()
            if merged_audit:
                combined = dict(update or {})
                for key, value in merged_audit.items():
                    existing = combined.get(key)
                    combined[key] = value if existing is None else [*value, *existing]
                update = combined
        if not update or not isinstance(recorder, ModelCallRecorder):
            return update
        merged: dict[str, Any] = {**state, **update}
        # 录音机的轮次以 State 为准（`run_id` 由 `prepare_context` 生成）：不一致就跟随 State，
        # 这样"同一张图连跑几轮"或"不经应用服务直接跑图"都能得到正确的轮次归属。
        # 应用服务已经提前绑过时，两者相同，这里不会发生任何改变。
        state_run_id = merged.get("run_id")
        if isinstance(state_run_id, str) and state_run_id and recorder.run_id != state_run_id:
            recorder.bind_run(state_run_id)
        # 委派节点返回的调用也来自原 record_model_call；先纳入明细，再追加父录音机的新记录。
        incoming = [*update.get("model_call_records", []), *recorder.finished_records()]
        result: AgentState = {
            **update,
            "model_call_records": merge_appended(
                state.get("model_call_records", []), incoming, model_call_key
            ),
        }
        if recorder.run_id:
            result["turn_records"] = {recorder.run_id: _build_turn_record(merged, recorder)}
        return result

    wrapped.__name__ = getattr(node, "__name__", name)
    context_nodes = {PREPARE_CONTEXT, LOAD_AGENTS, RECALL_MEMORY, RETRIEVE_CODE_NODE, COMPRESSION_NODE}
    stage = "context_build" if name in context_nodes else "tool" if name == TOOL_EXECUTION_NODE else "node"
    return timed_node(wrapped, stage=stage, operation=name)


def _build_turn_record(state: Mapping[str, Any], recorder: ModelCallRecorder) -> TurnRecord:
    """从当前 State 与录音机构造整轮记录（只存事实，Token 汇总留给投影层加总）。"""
    run_id = recorder.run_id
    submitted = state.get("run_submitted_at")
    if not isinstance(submitted, datetime):
        submitted = datetime.now(timezone.utc)
    final_state = _turn_state(state)
    existing = _existing_turn(state, run_id)
    final_at = getattr(existing, "final_at", None) if existing is not None else None
    if final_state in {"completed", "failed", "blocked", "cancelled"} and final_at is None:
        final_at = datetime.now(timezone.utc)
    calls = merge_appended([], [*(state.get("model_call_records") or []), *recorder.finished_records()], model_call_key)
    calls = [record for record in calls if record.run_id == run_id]
    elapsed = None
    if isinstance(final_at, datetime):
        elapsed = max(0, round((final_at - submitted).total_seconds() * 1000))
    return TurnRecord(
        run_id=run_id,
        submitted_at=submitted,
        final_at=final_at,
        final_state=final_state,
        model_call_ids=tuple(record.model_call_id for record in calls),
        tool_event_ids=tuple(
            tool_scope_key(event.run_id, event.tool_call_id)
            for event in (state.get("tool_events") or [])
            if getattr(event, "run_id", "") == run_id
        ),
        approval_wait_ms=_approval_wait_ms(state, run_id),
        turn_elapsed_ms=elapsed,
        usage_recorded=bool(calls),
        user_message_id=state.get("run_user_message_id"),
    )


def _existing_turn(state: Mapping[str, Any], run_id: str) -> Any:
    records = state.get("turn_records")
    if isinstance(records, Mapping):
        return records.get(run_id)
    return None


def _turn_state(state: Mapping[str, Any]) -> str:
    status = str(state.get("task_status") or "running")
    if status in {"completed", "failed", "blocked", "cancelled"}:
        return status
    if status == "waiting_approval":
        return "awaiting_approval"
    return "running"


def _approval_wait_ms(state: Mapping[str, Any], run_id: str) -> int | None:
    """本轮已完成审批区间之和；待决区间不以零冒充已完成等待。"""
    decisions = [
        item for item in state.get("approval_history") or []
        if isinstance(item, Mapping) and str(item.get("run_id") or "") == run_id
    ]
    if not decisions:
        return None
    total = 0
    for decision in decisions:
        requested = decision.get("requested_at")
        # 升级前的单次审批只在 State 留有最近请求时刻；多次旧审批无法可靠还原。
        if not isinstance(requested, datetime) and len(decisions) == 1:
            requested = state.get("approval_requested_at")
        decided = decision.get("created_at")
        if not isinstance(requested, datetime) or not isinstance(decided, datetime):
            return None
        total += max(0, round((decided - requested).total_seconds() * 1000))
    return total


def _make_image_reader(
    policy: Any, attachments: Any, attachment_session: str | None
) -> Callable[[Mapping[str, Any]], tuple[bytes, str] | str | None]:
    """构造"按引用取图片"的读取器（2026-10-20）。

    三种引用：``attachment``（本地附件，按会话读回并做归属校验）、``file``（`view_image`
    读过的本机文件，重新走一次路径策略）、``url``（对象存储的公开地址，**不下载**）。
    读不到、超限、不是图片都返回 ``None``——调用方用占位文本代替，不让一张旧图把整轮请求搞挂。
    """

    def read(ref: Mapping[str, Any]) -> tuple[bytes, str] | str | None:
        kind = str(ref.get("kind") or "")
        if kind == "url":
            url = str(ref.get("url") or "")
            return url if url.lower().startswith(("http://", "https://")) else None
        if kind == "attachment":
            ident = str(ref.get("id") or "")
            # 对象存储后端把公开 URL 直接放在引用里：识别出来就不必再读本地文件。
            if ident.lower().startswith(("http://", "https://")):
                return ident
            if attachments is None or not attachment_session:
                return None
            blob = attachments.read(attachment_session, ident)
            return blob.data, blob.media_type
        if kind == "file":
            path = str(ref.get("path") or "")
            if not path:
                return None
            resolved = policy.resolve_readable_file(path)
            data = resolved.read_bytes()
            if len(data) > MAX_UPLOAD_IMAGE_BYTES:
                return None
            media_type = detect_image_media_type(data)
            if media_type is None:
                return None
            size = image_dimensions(data)
            if size is not None and max(size) > MAX_IMAGE_EDGE:
                return None
            return data, media_type
        return None

    return read


def _mcp_search_provider(
    manager: McpManagerLike, registry_holder: list[ToolRegistry],
) -> Callable[[], Sequence[ToolEntry]]:
    """检索时同步当前目录，后台新连上的工具也能在同次任务内调用。"""
    def _provider() -> Sequence[ToolEntry]:
        entries = tuple(row.entry for row in manager.snapshot().rows
                        if row.exposure != ToolExposure.HIDDEN)
        if registry_holder:
            registry_holder[0].replace_mcp_tools(build_mcp_tools(entries, manager))
        return entries
    return _provider


def build_runtime_context(
    *,
    app_config: RunConfig,
    model_adapter: BaseChatModel,
    workspace_root: Path,
    #: 用户在设置里禁用的技能名（技能管理页，2026-10-16）：清单与技能工具都照它过滤。
    #: 由应用层每轮现取（设置库），与能力开关同源——两处不同源就会出现"清单里有、取不到正文"。
    disabled_skills: Iterable[str] = (),
    current_cwd: Path | None = None,
    security_policy: SecurityPolicy | None = None,
    external_policy: ExternalPathPolicy | None = None,
    agent_switches: Mapping[str, bool] | None = None,
    web_search_api_key: str | None = None,
    tool_registry: ToolRegistry | None = None,
    model_capabilities: ModelCapabilities | None = None,
    user_id: str | None = None,
    config_profile: str = "default",
    request_id: str | None = None,
    provider_name: str | None = None,
    usage_recorder: ModelCallRecorder | None = None,
    cancellation: CancellationToken | None = None,
    readonly_research: Mapping[str, Any] | None = None,
    code_change: Mapping[str, Any] | None = None,
    delegated_research: Mapping[str, Any] | None = None,
    child_index: Callable[[str, list[str]], None] | None = None,
    #: 随消息提交的图片（2026-10-16）。默认空 ⇒ 不带图的调用点一处都不用改。
    user_attachments: Iterable[LoadedAttachment] = (),
    #: 附件存储（2026-10-20）：出站前把图片引用物化成 `data:` URL 时按会话读回字节。
    #: 不传表示没有附件后端（CLI/测试的轻量路径）：上传附件的引用读不到，会退化成占位文本。
    attachments: Any = None,
    #: 附件归属的**会话 id**（不是 policy 的 `session_scope`：后者带命名空间前缀，
    #: 过不了 `AttachmentStore` 的引用校验）。没有它就读不回上传附件。
    attachment_session: str | None = None,
    #: 出站请求保留图片的最近轮数（含当前轮）。设置里的「图片上下文轮数」落到这里。
    image_context_turns: int = DEFAULT_IMAGE_CONTEXT_TURNS,
    session_scope: str | None = None,
    #: MCP 绑定（2026-10-20；契约 §6.13）。``None`` 表示这次运行不碰 MCP——与"开关关着"等价，
    #: 但**不加载** ``mcp`` SDK：委托子图与多数测试走的就是这条路径。
    mcp: McpBinding | None = None,
) -> RuntimeContext:
    """构造单次调用的运行上下文（架构 §4.1）。

    Args:
        app_config: 本次运行使用的完整配置。
        model_adapter: 本次调用使用的模型实例。
        workspace_root: 工作区根目录；AGENTS 扫描与文件工具的边界。
        current_cwd: 当前工作目录，默认取进程当前目录。
        security_policy: 安全策略；省略时按配置构造。
        external_policy: 工作区外路径的档位与授权（T38）。界面上可改的档位存在设置库里，
            应用层每轮现取后从这里传进来；省略时取 ``config.yaml`` 的出厂默认。
            **写侧沙箱由这一档派生**（不再有独立开关）。
        agent_switches: **能力开关表**（§16.1）。界面上改过的开关存在设置库里，应用层每轮现取后
            从这里传进来；省略时取 ``config.yaml`` 的出厂默认（``AppConfig.capability_default_layer``）。
            git 开关还要与本机有没有 ``git`` 相与，那一步在 :func:`~local_agent.capabilities.enabled_capabilities`。
        web_search_api_key: 检索服务（Tavily）的密钥明文，应用层每轮现取后传进来（凭据存储 →
            旧环境变量）。**它只进工具闭包**，不放进 ``RuntimeContext``：上下文会随委派子任务
            ``replace``、也会被诊断读取，密钥没有理由在那里露面。
        tool_registry: 工具注册表；省略时按策略构造。
        model_capabilities: 模型能力；省略时现探测一次（架构 §11.7）。
        user_id: 逻辑用户；单机单用户时为 ``None``。
        config_profile: 配置档位名。
        request_id: 本次请求标识，默认自动生成。
        provider_name: 能力探测用的模型名；省略时用 ``app_config.llm.model``。
            **按连接运行时（§6.8）必须显式传连接里的模型 ID**：那才是这次真正要打的模型。
        usage_recorder: 本轮用量录音机（§15.6）；省略时所有模型调用只走估算、不落实际用量。

    Returns:
        可直接传给 ``graph.invoke(..., context=...)`` 的上下文对象。

    Raises:
        ConfigError: 配置要求放开工作区边界（本版本不支持）。
    """
    # 开关表**每轮现取**（§16.1）：用户刚在能力面板里改完，下一次运行就该按新开关绑定工具与能力。
    # 省略时用 config.yaml 的出厂默认；传进来的那份（来自设置库）叠在其上，缺项由出厂默认补齐。
    switches = resolve_switches(app_config.capability_default_layer(), agent_switches or {})
    app_config = apply_capability_gates(app_config, switches)
    policy = security_policy or SecurityPolicy.from_config(
        app_config,
        workspace_root=workspace_root,
        external_policy=external_policy,
        # 取消令牌同时进两道门：安全 / 审批门（策略）与能力可见性门（节点的 `is_visible`）——§17.4。
        cancellation=cancellation,
    )
    if session_scope is not None:
        policy.session_scope = session_scope
    # 自主委派（§18.7）：工具执行节点拿不到 `RuntimeContext`，所以委派入口必须在**装配时**闭包进
    # 注册表（A 方案）。但顺序有个死结——注册表要先建（`agent_capabilities` 要用
    # `registry.git_available`），而入口又要上下文。用一个"运行期才填入"的持有者解开：
    # 调用发生在图跑起来之后，那时上下文早就定稿了。
    holder: list[RuntimeContext] = []
    # 这条连接能不能收**图像输入**（`llm.supports_vision` 声明，2026-10-16）：它决定 `image_read`
    # 是否进有效能力集、以及 `view_image` 会不会绑给模型。**失败闭合**——没声明就是收不了，
    # 免得工具里塞一张模型看不见的图（那既浪费上下文，又会让它以为自己看过了）。
    vision_available = bool(getattr(app_config.llm, "supports_vision", False))

    def _delegate(tasks: Sequence[Mapping[str, str]], concurrency: int) -> str:
        return make_delegation_runner(holder[0])(tasks, concurrency)

    # MCP 外部工具（2026-10-20；契约 §6.13）：只在本轮 ``mcp`` 能力开着、且调用方没注入注册表时装配。
    # 目录取连接管理器的**当轮快照**——装配本身不发起连接（未连上的 server 只是暂时没有工具）。
    mcp_registry_holder: list[ToolRegistry] = []
    mcp_tools: list[BaseTool] = []
    mcp_direct: list[str] = []
    mcp_search_provider: Callable[[], Sequence[ToolEntry]] | None = None
    if mcp is not None and tool_registry is None and switches.get("mcp", False):
        snapshot = mcp.manager.snapshot()
        entries = tuple(row.entry for row in snapshot.rows if row.exposure != ToolExposure.HIDDEN)
        mcp_tools = build_mcp_tools(entries, mcp.manager)
        known = {entry.canonical_name for entry in entries}
        mcp_direct = [name for name in snapshot.direct if name in known]
        # 本会话已经检索到过的工具继续可用：它们可能超出当轮 Direct 配额，所以单独补进来。
        # 这会让"搜索过工具之后"的系统前缀与本会话早先的轮次不同——**有意的**，
        # 与 Codex 的 deferred→visible 同一个代价。
        for name in mcp.discovered:
            if name in known and name not in mcp_direct:
                mcp_direct.append(name)
        mcp_search_provider = _mcp_search_provider(mcp.manager, mcp_registry_holder)

    registry = tool_registry or ToolRegistry.build(
        policy,
        web_access=app_config.web_access,
        # 联网检索的服务凭据（Tavily）：应用层在装配这一轮时现取（凭据存储 → 旧环境变量），
        # 这里只把它转交给工具闭包——**不进 RuntimeContext、不进快照**，密钥没有落点。
        web_search_api_key=web_search_api_key,
        git=app_config.git,
        # 技能段必须传进来：漏传的话工具会退回"默认关闭"的配置，而快照读的是真实配置——
        # 2026-10-09 的真实运行验收撞到的正是这个：**清单里列出了技能，`skill_load` 却说没有**。
        skills=app_config.skills,
        disabled_skills=disabled_skills,
        vision_available=vision_available,
        # 注入的注册表（测试/脚本）自带它自己的委派入口或明确的失败，这里不覆盖。
        delegate=_delegate,
        mcp_tools=mcp_tools,
        mcp_direct=mcp_direct,
        mcp_search_provider=mcp_search_provider,
        mcp_search_limit=app_config.mcp.max_search_results,
    )
    mcp_registry_holder.append(registry)
    capabilities = model_capabilities or probe_capabilities(
        model_adapter, provider_name=provider_name or app_config.llm.model
    )
    context = RuntimeContext(
        workspace_root=workspace_root,
        current_cwd=Path.cwd() if current_cwd is None else current_cwd,
        model_adapter=model_adapter,
        app_config=app_config,
        user_id=user_id,
        config_profile=config_profile,
        agent_switches=switches,
        # 有效能力 = 开关表（∩ Git 的**环境**条件）：关掉的能力对模型完全不可见（§16.3、§16.4）。
        agent_capabilities=enabled_capabilities(
            switches, git_available=registry.git_available, vision_available=vision_available,
            browser_available=registry.browser_available,
            # 这次运行没接 MCP 基础设施（CLI、委派子图、多数测试）时，``mcp`` 不进能力集：
            # 免得模型拿到一个搜不到任何东西的检索工具。
            mcp_available=mcp is not None,
        ),
        # 用户禁用的技能：随上下文带到执行侧，清单与 `skill_load` 因此看到同一份事实。
        disabled_skills=frozenset(str(name) for name in disabled_skills),
        security_policy=policy,
        tool_registry=registry,
        model_capabilities=capabilities,
        current_time=datetime.now(timezone.utc),
        request_id=request_id or uuid4().hex,
        usage_recorder=usage_recorder,
        cancellation=cancellation,
        readonly_research=readonly_research,
        code_change=code_change,
        delegated_research=delegated_research,
        child_index=child_index,
        user_attachments=tuple(user_attachments),
        image_reader=_make_image_reader(policy, attachments, attachment_session),
        image_context_turns=image_context_turns,
    )
    holder.append(context)
    return context


def read_thread_state(graph: ChatGraph, thread_id: str) -> dict[str, Any]:
    """读取某个 ``thread_id`` 的当前状态值（Checkpoint 恢复）。

    Args:
        graph: 已编译的图，需带 checkpointer。
        thread_id: 会话键。

    Returns:
        当前状态值字典；该 thread 没有 Checkpoint 时返回空字典。
    """
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    return dict(snapshot.values or {})

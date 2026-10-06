"""State / Checkpoint → DTO 的**单向**投影（ARCHITECTURE §15.4、§6.7）。

本模块是纯函数集合：输入是一个 State 映射（`Mapping[str, Any]`），输出是 T27 的 DTO。
它**不写回 State、不调用模型、不恢复 Graph、不执行工具**，也不读数据库——需要读 Checkpoint 的
事情由 `service.py` 做，投影只认已经拿到的 State。

几条必须一起看的口径：

- **消息文本走读时投影**：`reply_overrides` 是"回答文本改动"的唯一通道（架构 §10.1），
  页面渲染、Prompt 窗口、摘要输入三条路径必须同一套，所以这里调 `graph.projection.project_messages`，
  不自己拼文本。
- **不暴露内部标识**：`error_events.stage` 是节点名，`memory_refs.content`、`similarity`、`score`
  是记忆正文与内部评分——这些只用于映射，不进 DTO。节点名经 `_STAGE_ERRORS` 换成公开错误码。
- **不确定就不写确定值**：没有时间戳就是 `None`；没有用量记录就 `usage_recorded=False`，
  不伪造估算。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from local_agent.application import errors as app_errors
from local_agent.application.errors import ErrorCategory, ErrorView
from local_agent.application.models import (
    SCHEMA_VERSION,
    ApprovalState,
    ApprovalView,
    AssistantMessageView,
    ContextMeterView,
    ErrorMessageView,
    FileDiffHunkView,
    FileDiffLineView,
    FileDiffView,
    FinalResultState,
    FinalResultView,
    MemoryMessageView,
    MemoryUseView,
    MessageView,
    ModelCallRecordView,
    ModelSelectionView,
    PlanStepView,
    PlanView,
    RetrievalSourceView,
    RuntimeContextSnapshotView,
    RuntimeSourceVersionView,
    SearchMatchView,
    SessionSnapshot,
    SessionState,
    TaskRef,
    ToolEvidenceView,
    ToolMessageView,
    ToolOutcomeView,
    ToolStatus,
    ToolUseView,
    TurnFinalState,
    TurnRecordView,
    TurnUsageView,
    UserMessageView,
    WorkspaceRef,
    cache_hit_ratio,
    context_usage_ratio,
)
from local_agent.config import redact_secrets
from local_agent.exceptions import WorkspaceBoundaryError
from local_agent.graph.projection import message_id, project_messages
from local_agent.graph.scope import tool_scope_key
from local_agent.security.path_guard import ExternalPathPolicy, resolve_target

#: 审批卡的目标归一与"是否工作区外"只用于**展示**，与访问授权无关：无论档位是什么，
#: 审批卡都要能显示出完整目标。因此这里固定用最宽的档位做解析，判定权限不在这里。
_DISPLAY_EXTERNAL_POLICY = ExternalPathPolicy(mode="allow")

#: 记忆召回失败的 stage（与 `graph.nodes.RECALL_MEMORY` 同值；这里不导入 nodes，
#: 避免把整个 Graph 节点模块拖进投影层，一致性由 `tests/test_projection.py` 对照断言）。
STAGE_RECALL_MEMORY: Final[str] = "recall_memory"
STAGE_RETRIEVE_CODE: Final[str] = "retrieve_code_node"
STAGE_COMPRESSION: Final[str] = "compression_node"
STAGE_MEMORY_WRITE: Final[str] = "memory_write_node"
STAGE_TOOL_EXECUTION: Final[str] = "tool_execution"
STAGE_SAFETY_GATE: Final[str] = "safety_gate"
STAGE_TOOL_START: Final[str] = "tool_start"
STAGE_APPROVAL: Final[str] = "approval_interrupt"
STAGE_SIDE_EFFECT_RECHECK: Final[str] = "side_effect_recheck"

#: 带 `:后缀` 的 stage（同一节点里的不同失败/降级来源）。**每一个都必须有映射**：落进兜底会让
#: 正常的降级与"证据不足"被读成崩溃——复审实证（2026-09-28），一轮正常的机械校验被显示成
#: "本轮出现未分类的异常 / 该步骤已中止"，而它既不是异常、也没有中止。
STAGE_CHAT_STREAM: Final[str] = "chat_node:stream"
STAGE_AGENT_DECISION: Final[str] = "agent_decision"
STAGE_REFLECTION_MECHANICAL: Final[str] = "reflection_node:mechanical"
STAGE_REFLECTION_LLM: Final[str] = "reflection_node:llm"
STAGE_REFLECTION_FINAL_ANSWER: Final[str] = "reflection_node:final_answer"
STAGE_REFLECTION_STREAM: Final[str] = "reflection_node:stream"
STAGE_REFLECTION_SIDE_EFFECT: Final[str] = "reflection_node:side_effect"
STAGE_REFLECTION_MEMORY_INTENT: Final[str] = "reflection_node:memory_intent"
STAGE_TASK_MEMORY_INTENT: Final[str] = "task_memory_node:memory_intent"
STAGE_DECISION_STREAM: Final[str] = "agent_decision_node:stream"
STAGE_REFLECTION_MEMORY_FINAL: Final[str] = "reflection_node:memory_final"

#: 旧 Checkpoint 里确实没有这个事实时的写法：**如实说没有记录**，不猜一个看起来合理的值。
_NOT_RECORDED: Final[str] = "（未记录）"

#: stage → 公开错误码。**节点名不进 DTO**：页面只看到这里映射出来的码与文案。
#:
#: 这张表必须**穷尽** `graph.nodes` 里实际用到的 stage（含 `:后缀` 形式）：漏一个就落进
#: `RUN_STAGE_FAILED` 的"未分类异常"，把正常的降级与"证据不足"读成崩溃（复审实证，2026-09-28）。
#: 一致性由 `tests/test_projection.py` 对照断言。
_STAGE_ERRORS: Final[dict[str, tuple[str, ErrorCategory]]] = {
    STAGE_RECALL_MEMORY: (app_errors.MEMORY_RETRIEVAL_FAILED, "system"),
    STAGE_RETRIEVE_CODE: (app_errors.RAG_RETRIEVAL_FAILED, "system"),
    STAGE_COMPRESSION: (app_errors.CONTEXT_COMPRESSION_FAILED, "system"),
    STAGE_MEMORY_WRITE: (app_errors.MEMORY_WRITE_FAILED, "system"),
    STAGE_TOOL_EXECUTION: (app_errors.TOOL_CALL_FAILED, "tool"),
    # 被安全策略拒绝一律走 `safety_gate`（无论在安全门还是在工具内部捕获）：它不是"执行失败"，
    # 用户要改的是请求本身或路径，不是工具参数。
    STAGE_SAFETY_GATE: (app_errors.TOOL_CALL_BLOCKED, "security"),
    STAGE_TOOL_START: (app_errors.TOOL_CALL_BLOCKED, "security"),
    STAGE_APPROVAL: (app_errors.APPROVAL_FLOW_FAILED, "approval"),
    STAGE_SIDE_EFFECT_RECHECK: (app_errors.SIDE_EFFECT_UNCONFIRMED, "recovery"),
    STAGE_CHAT_STREAM: (app_errors.RUN_DEGRADED, "system"),
    STAGE_AGENT_DECISION: (app_errors.MODEL_CAPABILITY_UNSUPPORTED, "model"),
    STAGE_REFLECTION_MECHANICAL: (app_errors.EVIDENCE_NOT_VERIFIED, "system"),
    STAGE_REFLECTION_LLM: (app_errors.RUN_DEGRADED, "system"),
    STAGE_REFLECTION_FINAL_ANSWER: (app_errors.RUN_DEGRADED, "system"),
    STAGE_REFLECTION_STREAM: (app_errors.RUN_DEGRADED, "system"),
    STAGE_REFLECTION_SIDE_EFFECT: (app_errors.SIDE_EFFECT_UNCONFIRMED, "recovery"),
    STAGE_REFLECTION_MEMORY_INTENT: (app_errors.RUN_DEGRADED, "system"),
    STAGE_TASK_MEMORY_INTENT: (app_errors.RUN_DEGRADED, "system"),
    STAGE_DECISION_STREAM: (app_errors.RUN_DEGRADED, "system"),
    STAGE_REFLECTION_MEMORY_FINAL: (app_errors.RUN_DEGRADED, "system"),
}

#: 公开标题与可执行提示。与 `stage` 无关的通用兜底放在 `RUN_STAGE_FAILED`。
_ERROR_TEXT: Final[dict[str, tuple[str, str]]] = {
    app_errors.MEMORY_RETRIEVAL_FAILED: (
        "记忆召回失败",
        "本轮没有查到记忆，也没有据此回答。稍后重新发送即可重试。",
    ),
    app_errors.RAG_RETRIEVAL_FAILED: (
        "代码检索失败",
        "本轮没有检索到代码片段。确认工作区可读后重新发送即可重试。",
    ),
    app_errors.CONTEXT_COMPRESSION_FAILED: (
        "上下文压缩失败",
        "本轮按未压缩的上下文继续；若再次发生，可缩短对话或调小保留窗口。",
    ),
    app_errors.MEMORY_WRITE_FAILED: (
        "写入长期记忆失败",
        "本轮回答不受影响。整理好要记住的内容后可以再发一次。",
    ),
    app_errors.TOOL_CALL_FAILED: (
        "工具执行失败",
        "检查工具参数或目标路径后重新发送；已产生的副作用不会自动重放。",
    ),
    app_errors.TOOL_CALL_BLOCKED: (
        "工具调用被安全策略拦截",
        "调整请求本身或改为在工作区内操作；被拦截的调用没有执行。",
    ),
    app_errors.APPROVAL_FLOW_FAILED: (
        "审批流程异常",
        "本轮未执行被审批的操作。重新发起请求并重新确认即可。",
    ),
    # 能力不支持：`agent_decision` 这个 stage 表示工具模式已失败闭合（§11.7），
    # 它不是"异常"，而是"这条连接干不了这件事"。
    app_errors.MODEL_CAPABILITY_UNSUPPORTED: (
        "当前模型不支持工具调用",
        "这一轮没有执行任何工具。改用支持工具调用的连接后重新发送。",
    ),
    app_errors.RUN_STAGE_FAILED: (
        "本轮出现未分类的异常",
        # 去掉原先的"该步骤已中止"：它是假的。记一条错误事件不等于中止——大多数情况下本轮照常
        # 给出了回答（复审实证，2026-09-28）。
        "可重新发送请求；本地日志里可用诊断编号定位细节。",
    ),
    app_errors.RUN_DEGRADED: (
        "本轮有一步降级",
        "回答仍可用，只是有一处退回了兜底路径（例如流式回退或回答合成降级）。需要完整路径时可以重新发送。",
    ),
    app_errors.EVIDENCE_NOT_VERIFIED: (
        "本轮没有拿到可验证的证据",
        "回答里没有工具结果支撑的部分请以文件实际内容为准；补充必要信息后可重新发送。",
    ),
    app_errors.SIDE_EFFECT_UNCONFIRMED: (
        "存在未确认的副作用",
        "先人工核对上一次运行的记录，确认没有未收尾的改动后，再把它作为新的请求重新提交。",
    ),
}

#: 只隐藏私人目录；项目所在的其他盘符/系统路径不能一概变成不可核对的占位符。
#: 空格属于路径的一部分，私人路径遇到空格时宁可多隐藏一段说明，也不能只隐藏前半截。
_PRIVATE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|/(?:home|Users)/)"
    r"[^\"'，。；：、（）()\[\]\r\n]*"
)
#: 密钥**样式**（不只是已知密钥值）：即使调用方没把环境里的密钥传进来，这里也要抹掉，
#: 否则 `ErrorView` 的 fail-closed 校验会让整份快照构造失败——那比少显示一段文字更糟。
_KEY_LIKE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_HIDDEN_PATH = "[已隐藏的绝对路径]"
_MASK = "***"

_OPERATION_NAMES: Final[dict[str, str]] = {
    "list_dir": "查看目录", "read_file": "读取文件", "edit_file": "修改文件",
    "write_file": "写入文件", "delete_file": "删除文件", "run_shell": "运行命令",
    "grep": "搜索内容", "glob": "查找文件", "update_plan": "更新计划",
}
_ERROR_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|Users|tmp|var)/)[^\"'，。；：、（）()\[\]\r\n]*")


def _public_error_text(text: str, secrets: Iterable[str]) -> str:
    """Keep actionable failure reasons; raw operation names and machine paths stay local."""
    public = sanitize_text(text, secrets)
    for name, label in _OPERATION_NAMES.items():
        public = re.sub(rf"(?<![\w]){name}(?![\w])", label, public)
    return _ERROR_ABSOLUTE_PATH.sub(_HIDDEN_PATH, public)[:TOOL_DETAIL_STREAM_CHARS]

#: 工具详情的展示上限（§15.8）：超长内容有界折叠，完整正文留在本地工具事件里，不进入页面。
TOOL_DETAIL_TEXT_CHARS: Final[int] = 4000
TOOL_DETAIL_STREAM_CHARS: Final[int] = 2000
TOOL_DETAIL_PATHS: Final[int] = 200
#: 结构化 diff 的展示上限（行）：超出就不再累积，并如实标记 `truncated`。
TOOL_DIFF_LINES: Final[int] = 400

#: unified diff 的 hunk 头：`@@ -old_start[,old_count] +new_start[,new_count] @@`。
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def sanitize_text(
    text: str, secrets: Iterable[str] = (), *, hide_private_paths: bool = True
) -> str:
    """展示文本的清洗：已知密钥值 → `***`，密钥样式串 → `***`，绝对私人路径 → 占位符。

    节点侧的 `error_events` 文案是给内部看的，进入页面之前必须过这一层（§15.5、§15.8）。
    这里**不静默吞掉**别的内容，只做这两类替换，其余原样保留。
    """
    cleaned = redact_secrets(text, list(secrets))
    cleaned = _KEY_LIKE.sub(_MASK, cleaned)
    return _PRIVATE_PATH.sub(_HIDDEN_PATH, cleaned) if hide_private_paths else cleaned


REASONING_DISPLAY_LIMIT = 16_000


def safe_stream_boundary(text: str, boundary: int, secrets: Iterable[str] = ()) -> int:
    """分段不能切开待脱敏的完整值（密钥也可能包含空格等分隔符）。"""
    spans = [(match.start(), match.end()) for pattern in (_KEY_LIKE, _PRIVATE_PATH)
             for match in pattern.finditer(text)]
    # 私人路径尚未遇到结束分隔符时，继续保留整个路径，不能先遮盖前缀再泄漏下一块的后缀。
    spans.extend((match.start(), match.end() + 1) for match in _PRIVATE_PATH.finditer(text)
                 if match.end() == len(text))
    for secret in secrets:
        if not secret:
            continue
        start = text.find(secret)
        while start >= 0:
            spans.append((start, start + len(secret)))
            start = text.find(secret, start + 1)
    # 重叠值的保护范围可能相接；直到边界不再穿过任何敏感值。
    while any(start < boundary < end for start, end in spans):
        boundary = min(start for start, end in spans if start < boundary < end)
    return boundary


def reasoning_view(message: AIMessage, secrets: Iterable[str] = ()) -> tuple[str | None, bool]:
    """供应商思考的有限展示副本；完整扩展字段继续留在原消息中。"""
    raw = message.additional_kwargs.get("reasoning_content")
    if not isinstance(raw, str) or not raw:
        return None, False
    cleaned = sanitize_text(raw, secrets)
    return cleaned[:REASONING_DISPLAY_LIMIT] or None, len(cleaned) > REASONING_DISPLAY_LIMIT


def _as_utc(value: Any) -> datetime | None:
    """把 State 里的时间转成带时区的 UTC。

    旧 Checkpoint 里可能是裸 `datetime`（无时区）或 ISO 字符串——按 UTC 解读并**如实**返回，
    不因为格式不同就丢掉时间。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        candidate = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


# Only declared operation inputs needed to review an action enter browser DTOs.
# Hashes, vendor metadata, prompts and arbitrary extension fields stay local.
_DISPLAY_ARGUMENT_NAMES: Final[frozenset[str]] = frozenset({
    "path", "cwd", "command", "timeout_seconds", "recursive", "overwrite",
    "old_text", "new_text", "patch", "content", "replace_all", "reason",
    "start_line", "end_line", "pattern", "glob", "output_mode", "ignore_case",
    "include_hidden", "max_entries", "max_chars", "max_matches", "max_files",
    "env_keys", "explanation",
})
DISPLAY_ARGUMENT_CHARS: Final[int] = 2000


def _display_arguments(arguments: Any, secrets: Iterable[str] = ()) -> dict[str, str]:
    """把工具参数渲染成**已脱敏的展示字符串**。

    值一律转成字符串（嵌套结构直接转字符串），因此 DTO 结构上不可能携带任意对象；
    再统一过一遍 :func:`sanitize_text`：工具层脱敏过的是**已经执行过**的调用，而
    `pending_tool_calls` 与审批请求里的参数还没有经过工具层，那是原文。两者都要在
    进入 DTO 之前抹掉密钥样式串与绝对路径（§15.8）。
    """
    if not isinstance(arguments, Mapping):
        return {}
    rendered: dict[str, str] = {}
    for key, value in arguments.items():
        if key not in _DISPLAY_ARGUMENT_NAMES:
            continue
        if value is None:
            rendered[str(key)] = ""
        else:
            text = sanitize_text(str(value), secrets)
            rendered[str(key)] = text if len(text) <= DISPLAY_ARGUMENT_CHARS else text[:DISPLAY_ARGUMENT_CHARS] + "…（已截断）"
    return rendered


def current_run_id(state: Mapping[str, Any]) -> str:
    return str(state.get("run_id") or "")


def _run_errors(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """本轮（同 `run_id`）的错误事件，按 `event_id` 去重后保持出现顺序。"""
    run_id = current_run_id(state)
    seen: set[str] = set()
    picked: list[Mapping[str, Any]] = []
    for event in _mappings(state.get("error_events")):
        if run_id and str(event.get("run_id") or "") != run_id:
            continue
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen:
            continue
        if event_id:
            seen.add(event_id)
        picked.append(event)
    return picked


def _stage_failed(state: Mapping[str, Any], stage: str) -> bool:
    """本轮是否在该 stage 上失败过。

    与 `graph.nodes._recall_failed` / `_rag_search_failed` 同一判据（只用 `error_events` + `run_id`
    推导，不新增 State 字段）；两边一致性由测试对照。
    """
    return any(str(event.get("stage") or "") == stage for event in _run_errors(state))


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    """把 State 里的序列字段统一成**映射视图**。

    State 里同时存在 TypedDict（`ErrorEvent` / `ToolResultRecord` / `ApprovalDecision`…，运行时就是 dict）
    与 dataclass（`PlanStep` / `ToolEvent` / `EvidenceReference` / `MemoryCandidate`）。投影只按字段名读，
    不关心载体，所以这里统一成"能 `.get` 的东西"——**不能只认 dict**：实测 `tool_events` 是 dataclass 列表，
    只按 dict 读会让工具行全部退回 `pending`（页面把已完成的工具显示成等待中）。
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    items: list[Mapping[str, Any]] = []
    for item in value:
        normalized = _as_mapping(item)
        if normalized is not None:
            items.append(normalized)
    return items


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    """单个 State 值 → 映射视图（同样兼容 dataclass）。"""
    if isinstance(value, Mapping):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    if hasattr(value, "__dict__"):
        return {str(key): item for key, item in vars(value).items()}
    return None


# ---- 消息 ------------------------------------------------------------------


def project_messages_view(state: Mapping[str, Any], *, secrets: Iterable[str] = ()) -> list[MessageView]:
    """把 State 的消息流投影成页面 DTO。

    - AI 文本先过 `reply_overrides`（架构 §10.1），再脱敏；
    - 每条 AI 消息的 `tool_calls` 变成紧随其后的工具行，按调用顺序；工具行按
      `(run_id, tool_call_id)` 作用域配对，因此跨轮复用同一个调用 id 时各自配到自己那条记录；
    - 还没进 `messages` 的 `pending_tool_calls`（只属于当前轮）追加在末尾（状态为 `pending`）；
    - 本轮的 `error_events` 追加在末尾，`message_id` 就是 `event_id`，与 `project_errors` 一一对应。

    LangChain 消息不带时间戳，所以用户/AI 消息的 `created_at` 为 `None`——等用量记录落地后
    再由 `model_call_records` 补齐，现在不伪造（§15.6）。
    """
    raw = state.get("messages")
    messages: list[AnyMessage] = list(raw) if isinstance(raw, list) else []
    overrides = state.get("reply_overrides")
    projected = project_messages(messages, overrides if isinstance(overrides, Mapping) else {})

    tools = project_tool_rows(state, secrets=secrets)
    # 同一个 `tool_call_id` 可能出现在**多轮**里（模型可以复用调用 id）：按出现顺序排队逐条配对，
    # 这样每一轮的 AI 消息拿到的是自己那一行。用裸 `tool_call_id` 建字典配对会让先出现的那一轮
    # 被后一轮顶掉——那是投影丢数据，不是 State 里没有。
    queues: dict[str, list[int]] = {}
    for index, (_row_run, row) in enumerate(tools):
        queues.setdefault(row.tool_call_id, []).append(index)
    paired: set[int] = set()
    views: list[MessageView] = []

    for message in projected:
        if isinstance(message, HumanMessage):
            views.append(
                UserMessageView(
                    message_id=message_id(message),
                    # 用户自己提交过的路径已在其浏览器中；回显时只隐藏密钥。
                    text=sanitize_text(_text_of(message), secrets, hide_private_paths=False),
                    created_at=None,
                )
            )
        elif isinstance(message, AIMessage):
            reasoning, reasoning_truncated = reasoning_view(message, secrets)
            views.append(
                AssistantMessageView(
                    message_id=message_id(message),
                    text=sanitize_text(_text_of(message), secrets),
                    reasoning=reasoning,
                    reasoning_truncated=reasoning_truncated,
                    created_at=None,
                    complete=True,
                )
            )
            for call in _tool_calls_of(message):
                call_id = str(call.get("id") or "")
                queue = queues.get(call_id)
                if not queue:
                    continue
                index = queue.pop(0)
                paired.add(index)
                row_run, row = tools[index]
                views.append(ToolMessageView(message_id=_tool_row_id(row_run, call_id), tool=row))
        elif isinstance(message, ToolMessage):
            # 工具结果的正文已经由工具行（`ToolUseView.output_preview`）承载，
            # 这里不再产生第二条展示项，避免同一结果出现两次。
            continue

    for index, (row_run, row) in enumerate(tools):
        if index in paired or row.status != "pending":
            continue
        views.append(ToolMessageView(message_id=_tool_row_id(row_run, row.tool_call_id), tool=row))

    # 检索/记忆行插在本轮用户提问之后（与页面既有版式一致）。
    #
    # 已知边界：State 只保存**当前轮**的检索结果，所以刷新后能看到的是最后一轮的检索行，
    # 历史轮的检索行不会回来——不是投影丢数据，而是 Checkpoint 里没有这份展示数据。
    memory = project_memory(state, secrets=secrets)
    if memory is not None:
        last_user = next(
            (index for index in range(len(views) - 1, -1, -1) if views[index].kind == "user"),
            None,
        )
        if last_user is not None:
            views.insert(
                last_user + 1,
                MemoryMessageView(message_id=f"memory:{current_run_id(state)}", memory=memory),
            )

    for event in _run_errors(state):
        view = _error_view(event, secrets=secrets)
        if view is not None:
            # 行 id 用**公开**诊断编号，不用内部 event_id：后者含节点名，回显等于把它送到页面。
            views.append(ErrorMessageView(message_id=view.diagnostic_id, error=view))

    return views


def _text_of(message: AnyMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # 多段内容（文本 + 附件块）只取文本段，附件与图片留给后续版本。
        parts = [part if isinstance(part, str) else str(part.get("text", "")) for part in content]
        return "".join(parts)
    return str(content)


def _tool_calls_of(message: AIMessage) -> list[Mapping[str, Any]]:
    calls = getattr(message, "tool_calls", None) or []
    return [call for call in calls if isinstance(call, Mapping)]


# ---- 计划 / 工具 -----------------------------------------------------------


def project_plan(state: Mapping[str, Any], *, secrets: Iterable[str] = ()) -> PlanView | None:
    """计划条：没有计划就返回 `None`，不用空计划占位。"""
    goal = sanitize_text(str(state.get("task_goal") or "").strip(), secrets)
    steps: list[PlanStepView] = []
    known = {
        "pending",
        "running",
        "completed",
        "failed",
        "skipped",
        "blocked",
    }
    for step in _mappings(state.get("plan")):
        title = sanitize_text(str(step.get("description") or step.get("title") or "").strip(), secrets)
        if not title:
            continue
        status = str(step.get("status") or "pending")
        steps.append(
            PlanStepView(
                step_id=str(step.get("id") or step.get("step_id") or ""),
                title=title,
                status=status if status in known else "pending",  # type: ignore[arg-type]
            )
        )
    if not steps and not goal:
        return None
    return PlanView(goal=goal or "（未记录任务目标）", steps=steps)


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    """有界正文：超过 `limit` 就截断并如实说"这里还有更多"。"""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _int_of(value: Any) -> int | None:
    """整数取值；`bool` 是 `int` 的子类，但"真/假"不是数字，不能当计数用。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_sanitized(value: Any, secrets: Iterable[str]) -> str | None:
    """非空字符串 → 已脱敏文本；空值 → `None`（不伪造空串）。"""
    text = _optional_str(value)
    return sanitize_text(text, secrets) if text is not None else None


def _tool_payload_queues(messages: Iterable[AnyMessage]) -> dict[str, deque[Mapping[str, Any]]]:
    """从 `ToolMessage` 的**全量**文本里解析工具返回的 JSON，按出现顺序排成队列。

    结构化详情必须从这里解析，不能从 `tool_results`：后者只存 2000 字符预览，JSON 可能因此
    被截断而解析不出来（与 `graph.nodes._tool_payloads` 同一判据）。用队列而不是字典——
    跨轮复用同一个调用 id 时按出现顺序逐条配对，先出现的轮次不会被后一轮顶掉。
    """
    queues: dict[str, deque[Mapping[str, Any]]] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            parsed = json.loads(str(message.text))
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, Mapping):
            queues.setdefault(str(message.tool_call_id or ""), deque()).append(parsed)
    return queues


def _take_payload(
    queues: dict[str, deque[Mapping[str, Any]]], tool_call_id: str
) -> Mapping[str, Any] | None:
    """取走该调用 id 的下一个载荷（与工具事件同序）；没有解析成功的载荷时为 `None`。"""
    queue = queues.get(tool_call_id)
    return queue.popleft() if queue else None


def _parse_unified_diff(
    text: str, *, path: str | None, secrets: Iterable[str]
) -> tuple[FileDiffView | None, bool]:
    """把 unified diff 文本还原成**结构化** hunk（§15.8、§6.1）。

    页面必须直接拿到结构，不能自己解析一段文本：这里按标准 unified diff 的形状
    （`--- `/`+++ ` 文件头、`@@ … @@` 分段、行首 `+`/`-`/空格）还原每行的类型与新旧行号。
    解析不出任何 hunk 时返回 `None`——调用方退回原有的文本渲染，而不是给一个空结构。

    返回 `(视图, 是否因展示上限截断)`：达到 :data:`TOOL_DIFF_LINES` 后不再累积，
    计数只统计实际展示的行，避免"计数说 500 行、页面只有 400 行"这种自相矛盾。
    """
    if not text.strip():
        return None, False
    hunks: list[FileDiffHunkView] = []
    lines: list[FileDiffLineView] = []
    header: str | None = None
    old_no = new_no = 0
    additions = deletions = 0
    kept = 0
    truncated = False
    for raw in text.splitlines():
        if raw.startswith("@@"):
            if header is not None:
                hunks.append(FileDiffHunkView(header=header, lines=lines))
            header = sanitize_text(raw, secrets)
            lines = []
            parsed = _HUNK_HEADER.match(raw)
            old_no = int(parsed.group(1)) if parsed else 1
            new_no = int(parsed.group(2)) if parsed else 1
            continue
        # 文件头与 "\ No newline at end of file" 不是内容行。
        if header is None or raw.startswith(("--- ", "+++ ", "\\")):
            continue
        prefix, body = raw[:1], raw[1:]
        line: FileDiffLineView
        if kept >= TOOL_DIFF_LINES:
            truncated = True
            break
        if prefix == "+":
            line = FileDiffLineView(kind="add", new_line=new_no, text=sanitize_text(body, secrets))
            new_no += 1
            additions += 1
        elif prefix == "-":
            line = FileDiffLineView(kind="del", old_line=old_no, text=sanitize_text(body, secrets))
            old_no += 1
            deletions += 1
        elif prefix == " ":
            line = FileDiffLineView(
                kind="context", old_line=old_no, new_line=new_no, text=sanitize_text(body, secrets)
            )
            old_no += 1
            new_no += 1
        else:
            continue
        kept += 1
        lines.append(line)
    if header is not None:
        hunks.append(FileDiffHunkView(header=header, lines=lines))
    if not hunks:
        return None, False
    return (
        FileDiffView(path=path or "", additions=additions, deletions=deletions, hunks=hunks),
        truncated,
    )


def _tool_outcome(
    tool_name: str, payload: Mapping[str, Any] | None, secrets: Iterable[str]
) -> ToolOutcomeView | None:
    """把一个工具的 JSON 结果渲染成**有界、脱敏**的结构化分区（§15.8、§6.7）。

    认不出的工具或解析不出的载荷一律返回 `None`：调用方必须退回安全的通用渲染器，
    不能把一段任意文本当成已知工具的结构。
    """
    if payload is None:
        return None
    path = _optional_sanitized(payload.get("path"), secrets)
    note = _optional_str(payload.get("note"))

    if tool_name == "glob" or (tool_name == "grep" and payload.get("output_mode", "content") != "content"):
        raw_paths = payload.get("files") if tool_name == "glob" else [hit.get("path") for hit in _mappings(payload.get("hits"))]
        search_paths = [sanitize_text(item, secrets) for item in raw_paths if isinstance(item, str)][:TOOL_DETAIL_PATHS] if isinstance(raw_paths, list) else []
        total = _int_of(payload.get("total_files" if tool_name == "glob" else "total_matches"))
        return ToolOutcomeView(
            kind="list", target=path, paths=search_paths, total=total,
            summary=f"找到 {total if total is not None else len(search_paths)} {'个文件' if tool_name == 'glob' else '处匹配'}",
            truncated=bool(payload.get("truncated")) or (isinstance(raw_paths, list) and len(raw_paths) > len(search_paths)),
        )

    if tool_name == "grep":
        matches: list[SearchMatchView] = []
        budget = TOOL_DETAIL_TEXT_CHARS
        clipped = False
        for item in _mappings(payload.get("matches")):
            match_path = _optional_sanitized(item.get("path"), secrets)
            line = _int_of(item.get("line_number"))
            if not match_path or line is None or line < 1:
                continue
            if budget <= 0 or len(matches) >= TOOL_DETAIL_PATHS:
                clipped = True
                break
            match_text = sanitize_text(str(item.get("text") or ""), secrets)
            clipped = clipped or len(match_text) > budget
            matches.append(SearchMatchView(path=match_path, line_number=line, text=match_text[:budget]))
            budget -= len(match_text)
        total = _int_of(payload.get("total_matches"))
        return ToolOutcomeView(
            kind="search", target=path, matches=matches, total=total,
            summary=f"找到 {total if total is not None else len(matches)} 处匹配",
            truncated=bool(payload.get("truncated")) or clipped,
        )

    if tool_name == "list_dir":
        entries = payload.get("entries")
        paths: list[str] = []
        if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                entry_path = _optional_sanitized(entry.get("path"), secrets)
                if entry_path:
                    paths.append(entry_path)
                if len(paths) >= TOOL_DETAIL_PATHS:
                    break
        total = _int_of(payload.get("total_entries"))
        summary = f"{total if total is not None else len(paths)} 个条目"
        if note:
            summary = f"{summary} · {note}"
        return ToolOutcomeView(
            kind="list",
            target=path,
            summary=sanitize_text(summary, secrets),
            paths=paths,
            total=total,
            truncated=bool(payload.get("truncated")) or (total is not None and len(paths) < total),
        )

    if tool_name == "read_file":
        lines = _int_of(payload.get("line_count"))
        summary = f"{lines if lines is not None else 0} 行"
        if note:
            summary = f"{summary} · {note}"
        text: str | None = None
        clipped = False
        if payload.get("is_binary"):
            summary = f"{summary} · 二进制，未展示正文"
        else:
            text, clipped = _bounded(sanitize_text(str(payload.get("content") or ""), secrets), TOOL_DETAIL_TEXT_CHARS)
            text = text or None
        return ToolOutcomeView(
            kind="read",
            target=path,
            summary=sanitize_text(summary, secrets),
            text=text,
            total=lines,
            truncated=clipped or bool(payload.get("truncated")),
        )

    if tool_name == "edit_file":
        replaced = _int_of(payload.get("replaced_count")) or 0
        changed = _int_of(payload.get("changed_lines")) or 0
        summary = f"替换 {replaced} 处 · {changed} 行变化"
        reason = _optional_str(payload.get("reason"))
        if reason:
            summary = f"{summary} · {reason}"
        text, clipped = _bounded(sanitize_text(str(payload.get("diff") or ""), secrets), TOOL_DETAIL_TEXT_CHARS)
        # 同一份 diff 再解析一次成结构：页面渲染 hunk 与行号，`text` 只作兜底。
        diff, diff_truncated = _parse_unified_diff(str(payload.get("diff") or ""), path=path, secrets=secrets)
        return ToolOutcomeView(
            kind="edit",
            target=path,
            summary=sanitize_text(summary, secrets),
            text=text or None,
            diff=diff,
            total=_int_of(payload.get("line_count")),
            truncated=clipped or diff_truncated,
        )

    if tool_name == "write_file":
        size = _int_of(payload.get("size_bytes"))
        lines = _int_of(payload.get("line_count"))
        summary = f"{size if size is not None else 0} 字节 · {lines if lines is not None else 0} 行"
        reason = _optional_str(payload.get("reason"))
        if reason:
            summary = f"{summary} · {reason}"
        return ToolOutcomeView(
            kind="write",
            target=path,
            summary=sanitize_text(summary, secrets),
            total=lines,
        )

    if tool_name == "delete_file":
        files = _int_of(payload.get("removed_files")) or 0
        dirs = _int_of(payload.get("removed_dirs")) or 0
        summary = f"删除 {files} 个文件 · {dirs} 个目录"
        reason = _optional_str(payload.get("reason"))
        if reason:
            summary = f"{summary} · {reason}"
        return ToolOutcomeView(
            kind="delete",
            target=path,
            summary=sanitize_text(summary, secrets),
        )

    if tool_name == "run_shell":
        exit_code = _int_of(payload.get("exit_code"))
        timed_out = bool(payload.get("timed_out"))
        stdout, out_clipped = _bounded(
            sanitize_text(str(payload.get("stdout") or ""), secrets), TOOL_DETAIL_STREAM_CHARS
        )
        stderr, err_clipped = _bounded(
            sanitize_text(str(payload.get("stderr") or ""), secrets), TOOL_DETAIL_STREAM_CHARS
        )
        summary = "超时" if timed_out else f"退出码 {exit_code if exit_code is not None else '（未记录）'}"
        if note:
            summary = f"{summary} · {note}"
        return ToolOutcomeView(
            kind="shell",
            target=_optional_sanitized(payload.get("cwd"), secrets),
            summary=sanitize_text(summary, secrets),
            stdout=stdout or None,
            stderr=stderr or None,
            exit_code=exit_code,
            timed_out=timed_out,
            truncated=bool(payload.get("truncated")) or out_clipped or err_clipped,
        )

    return None


def _evidence_map(state: Mapping[str, Any], secrets: Iterable[str]) -> dict[str, ToolEvidenceView]:
    """按 `tool_call_id` 索引本轮的工具证据。

    `final_evidence` 由反思层按**当前任务**全量重算（`graph.nodes.reflection_node`），其中
    `source_event_id` 存的就是 `tool_call_id`。证据本身没有轮次字段，所以只在本轮的行上使用
    （见 `project_tool_rows`），避免把本轮的证据贴到历史轮次的行上。
    """
    evidence: dict[str, ToolEvidenceView] = {}
    for reference in _mappings(state.get("final_evidence")):
        call_id = str(reference.get("source_event_id") or "")
        if not call_id:
            continue
        summary = sanitize_text(str(reference.get("summary") or "").strip(), secrets)
        if not summary:
            continue
        evidence[call_id] = ToolEvidenceView(
            evidence_type=str(reference.get("evidence_type") or "stdout"),
            summary="本次操作的结果",
            verified=bool(reference.get("verified")),
        )
    return evidence


def project_tool_rows(state: Mapping[str, Any], *, secrets: Iterable[str] = ()) -> list[tuple[str, ToolUseView]]:
    """工具行 + 它所属的轮次：`(run_id, 行)`，按 State 里的出现顺序。

    工具调用的作用域是 `(run_id, tool_call_id)`（`graph.scope.tool_scope_key`，架构 §4.2）。
    不同轮次复用同一个 `tool_call_id` 时它们是**两行**：只用裸 `tool_call_id` 建字典会让先
    出现的那一轮展示被后一轮顶掉。升级前的 Checkpoint 没有 `run_id`，按空轮次处理（与
    `tool_result_key` 同一约定），因此不会与任何真实轮次混在一起。

    结构化详情（`outcome`）来自 `ToolMessage` 的完整 JSON，按出现顺序逐条配对；机械证据
    （`evidence`）只贴在本轮的行上。
    """
    results: dict[str, Mapping[str, Any]] = {}
    for record in _mappings(state.get("tool_results")):
        results[tool_scope_key(str(record.get("run_id") or ""), str(record.get("tool_call_id") or ""))] = record

    raw_messages = state.get("messages")
    payloads = _tool_payload_queues(raw_messages if isinstance(raw_messages, list) else [])
    evidence = _evidence_map(state, secrets)
    current_run = current_run_id(state)

    rows: dict[str, tuple[str, ToolUseView]] = {}
    for event in _mappings(state.get("tool_events")):
        call_id = str(event.get("tool_call_id") or "")
        if not call_id:
            continue
        run = str(event.get("run_id") or "")
        result = results.get(tool_scope_key(run, call_id))
        started = _as_utc(event.get("started_at"))
        finished = _as_utc(event.get("finished_at"))
        preview = _optional_str((result or {}).get("output_preview") or event.get("output_preview"))
        tool_name = (
            _optional_str(event.get("tool_name"))
            or _optional_str((result or {}).get("tool_name"))
            or ""
        )
        outcome = _tool_outcome(tool_name, _take_payload(payloads, call_id), secrets)
        verification = evidence.get(call_id) if run == current_run else None
        if verification is not None and outcome is not None and outcome.summary:
            verification = verification.model_copy(update={"summary": outcome.summary})
        # The original JSON contains hashes and internal keys. Known operations
        # expose a summary plus typed detail; unrecognized JSON stays local.
        if outcome is not None:
            preview = outcome.summary
        elif preview is not None and preview.lstrip().startswith(("{", "[")):
            preview = "操作已返回结果；该结果格式暂无可展示的详情。"
        if preview is not None:
            preview = preview[:TOOL_DETAIL_STREAM_CHARS]
            if _tool_status(event, result) in {"error", "denied"}:
                preview = _public_error_text(preview, secrets)
        rows[tool_scope_key(run, call_id)] = (
            run,
            ToolUseView(
                tool_call_id=call_id,
                tool_name=tool_name,
                status=_tool_status(event, result),
                redacted_arguments=_display_arguments(event.get("redacted_arguments"), secrets),
                approval_id=_optional_str(event.get("approval_id")),
                output_preview=sanitize_text(preview, secrets) if preview is not None else None,
                output_digest=_optional_str(
                    (result or {}).get("output_digest") or event.get("output_digest")
                ),
                truncated=bool((result or {}).get("truncated", False)),
                started_at=started,
                finished_at=finished,
                elapsed_ms=_elapsed_ms(started, finished),
                outcome=outcome,
                # 证据没有轮次字段：只在本轮的行上贴，历史轮次的行保持为 `None`。
                evidence=verification,
            ),
        )

    # 待执行调用属于**当前轮**（这个字段每轮复位，不是累积字段）。
    run = current_run_id(state)
    for call in _mappings(state.get("pending_tool_calls")):
        call_id = str(call.get("id") or "")
        if not call_id or tool_scope_key(run, call_id) in rows:
            continue
        rows[tool_scope_key(run, call_id)] = (
            run,
            ToolUseView(
                tool_call_id=call_id,
                tool_name=str(call.get("name") or ""),
                status="pending",
                redacted_arguments=_display_arguments(call.get("args"), secrets),
            ),
        )
    return list(rows.values())


def project_tools(
    state: Mapping[str, Any], *, secrets: Iterable[str] = (), run_id: str | None = None
) -> list[ToolUseView]:
    """工具行视图；给定 `run_id` 时只返回该轮次的行。

    事件投影用 `run_id` 取"这一轮的工具进度"：不筛轮次的话，第二轮会把第一轮的工具行
    重新广播一遍（还带着第二轮的轮次标识）。
    """
    return [
        view
        for row_run, view in project_tool_rows(state, secrets=secrets)
        if run_id is None or row_run == run_id
    ]


def _tool_row_id(run_id: str, tool_call_id: str) -> str:
    """工具行的展示 id：带轮次作用域，跨轮复用同一个调用 id 时不会撞 id。"""
    return f"tool:{tool_scope_key(run_id, tool_call_id)}"


def _tool_status(event: Mapping[str, Any], result: Mapping[str, Any] | None) -> ToolStatus:
    """结果记录优先；没有结果时按事件的时间字段判断"还未开始/进行中"。"""
    known = {"pending", "running", "ok", "error", "denied"}
    if result is not None:
        status = str(result.get("status") or "")
        if status in known:
            return status  # type: ignore[return-value]
    status = str(event.get("status") or "")
    if status in known:
        return status  # type: ignore[return-value]
    return "running" if _as_utc(event.get("started_at")) is not None else "pending"


def _elapsed_ms(started: datetime | None, finished: datetime | None) -> int | None:
    if started is None or finished is None:
        return None
    return max(int((finished - started).total_seconds() * 1000), 0)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _tool_name_of(state: Mapping[str, Any], run_id: str, tool_call_id: str) -> str | None:
    """从工具事件/待执行调用里补工具名（审批决定本身不带它）。

    按 `(run_id, tool_call_id)` 找：跨轮复用同一个调用 id 时，只按调用 id 会拿到**别的轮次**
    的工具名，那比"未记录"更糟——它是错的。
    """
    if not tool_call_id:
        return None
    scope = tool_scope_key(run_id, tool_call_id)
    for event in _mappings(state.get("tool_events")):
        if tool_scope_key(str(event.get("run_id") or ""), str(event.get("tool_call_id") or "")) == scope:
            name = _optional_str(event.get("tool_name"))
            if name:
                return name
    for call in _mappings(state.get("pending_tool_calls")):
        if tool_scope_key(current_run_id(state), str(call.get("id") or "")) == scope:
            return _optional_str(call.get("name"))
    return None


# ---- 记忆 / RAG ------------------------------------------------------------


def project_memory(state: Mapping[str, Any], *, secrets: Iterable[str] = ()) -> MemoryUseView | None:
    """检索与长期记忆：命中/未命中/失败三者分开，来源只给定位。

    "查了没有"（`miss`）与"看不了"（`failed`）必须分开：只要本轮有召回或代码检索失败的记录，
    就按 `failed` 说——两者都会让 `sources` 为空，混说等于告诉用户"确实没有"。
    """
    sources: list[RetrievalSourceView] = []

    for reference in _mappings(state.get("rag_refs")):
        path = str(reference.get("path") or "")
        if not path:
            continue
        line_start = reference.get("line_start")
        line_end = reference.get("line_end")
        location = f"{path}:{line_start}-{line_end}" if line_start and line_end else path
        sources.append(
            RetrievalSourceView(
                kind="workspace_file",
                reference=sanitize_text(location, secrets),
                usage="代码片段已注入本轮上下文",
            )
        )

    for reference in _mappings(state.get("memory_refs")):
        memory_id = str(reference.get("memory_id") or "")
        if not memory_id:
            continue
        created = str(reference.get("created_at") or "")[:10]
        kind = str(reference.get("kind") or "medium_term")
        sources.append(
            RetrievalSourceView(
                kind="long_term_memory" if kind in {"long_term_fact", "task_conclusion"} else "session_history",
                reference=sanitize_text(
                    f"记忆 {memory_id}" + (f" · 写入于 {created}" if created else ""), secrets
                ),
                usage="已注入本轮上下文",
            )
        )

    write_outcome: str | None = None
    write_detail: str | None = None
    candidates = len(_mappings(state.get("memory_write_candidates")))
    if _stage_failed(state, STAGE_MEMORY_WRITE):
        write_outcome = "failed"
        write_detail = "写入失败 · 不影响本轮回答，也未写入任何内容"
    elif candidates > 0:
        write_outcome = "written"
        write_detail = f"{candidates} 条 · 来源：本轮对话"

    if sources:
        note = "只显示来源定位与用途：不展示正文、向量或内部评分。"
        outcome = "hit"
    elif _stage_failed(state, STAGE_RECALL_MEMORY) or _stage_failed(state, STAGE_RETRIEVE_CODE):
        outcome = "failed"
        note = "本轮没有成功检索（与「查了但没有结果」不同）：回答中没有引用来源，恢复后重新发送即可重试。"
    elif not _retrieval_available(state):
        return None
    else:
        outcome = "miss"
        note = "本轮没有可引用的工作区文件或长期记忆。回答只基于当前对话，不会为来源留空位。"

    return MemoryUseView(
        retrieval_outcome=outcome,  # type: ignore[arg-type]
        sources=sources,
        retrieval_note=sanitize_text(note, secrets),
        write_outcome=write_outcome,  # type: ignore[arg-type]
        write_detail=_optional_str(write_detail),
    )


def _retrieval_available(state: Mapping[str, Any]) -> bool:
    """本轮是否真的走过检索。

    没有任何检索迹象（既无来源、也无失败）时不产出这一行，避免给不相关的轮次加一条空洞的
    "未命中"——那是编出来的状态。
    """
    if state.get("memory_refs") is not None or state.get("rag_refs") is not None:
        return True
    return _stage_failed(state, STAGE_RECALL_MEMORY) or _stage_failed(state, STAGE_RETRIEVE_CODE)


# ---- 审批 ------------------------------------------------------------------


def _approval_target(
    tool_name: Any, arguments: Any, secrets: Iterable[str], workspace_root: Path | None
) -> tuple[str | None, bool]:
    """审批的规范化展示目标：**从结构化请求参数取**，不从影响摘要文本里反推。

    认不出的工具或没有相应参数的请求一律 `None`：宁可让卡片少一行"目标"，
    也不要猜一个看起来合理的路径（§15.8）。
    """
    if not isinstance(arguments, Mapping):
        return None, False
    name = str(tool_name or "")
    raw: Any = None
    if name in {"edit_file", "write_file", "delete_file"}:
        raw = arguments.get("path")
    elif name == "run_shell":
        raw = arguments.get("cwd") or "."
    if raw is None:
        return None, False
    if workspace_root is None:
        return _optional_sanitized(raw, secrets), False
    try:
        resolved = resolve_target(
            str(raw),
            base_dir=workspace_root,
            workspace_root=workspace_root,
            external_policy=_DISPLAY_EXTERNAL_POLICY,
        )
    except WorkspaceBoundaryError:
        return None, False
    # 审批卡必须能让用户核对完整目标；这里只抹密钥，不套用普通输出的路径隐藏规则。
    path = resolved.path.as_posix() if resolved.external else resolved.path.relative_to(workspace_root.resolve()).as_posix()
    target = _KEY_LIKE.sub(_MASK, redact_secrets(path, list(secrets)))
    return target, resolved.external


def project_approval(
    state: Mapping[str, Any], *, has_live_interrupt: bool, secrets: Iterable[str] = (),
    workspace_root: Path | None = None, confirmation_stage: int = 1,
) -> ApprovalView | None:
    """审批的派生视图状态。

    底层只有 `pending_approval`（当前请求）与 `approval_history`（决定），没有状态机字段：
    `pending`/`executing`/`approved`/`rejected`/`expired` 全部在这里推导（规则见
    `models.ApprovalView` 的 docstring）。`edit` 归入 `approved` 并保留被改写的参数。
    """
    pending = _as_mapping(state.get("pending_approval"))
    decisions = _mappings(state.get("approval_history"))

    request: Mapping[str, Any] | None = pending
    last_decision = decisions[-1] if decisions else None
    approval_id = str(
        (request or {}).get("approval_id") or (last_decision or {}).get("approval_id") or ""
    )
    if not approval_id:
        return None

    decided = next(
        (item for item in reversed(decisions) if str(item.get("approval_id") or "") == approval_id),
        None,
    )
    if decided is not None:
        decision = str(decided.get("decision") or "")
        call_id = str(decided.get("tool_call_id") or (request or {}).get("tool_call_id") or "")
        # 决定与请求都带轮次：作用域用它 + 调用 id，跨轮复用同一个调用 id 时不会互相冒充。
        run = str(decided.get("run_id") or (request or {}).get("run_id") or "")
        if decision == "reject":
            state_value: ApprovalState = "rejected"
            note = _optional_str(decided.get("reason")) or "该操作未执行。"
        else:
            scope = tool_scope_key(run, call_id)
            finished = any(
                tool_scope_key(str(result.get("run_id") or ""), str(result.get("tool_call_id") or "")) == scope
                for result in _mappings(state.get("tool_results"))
            )
            state_value = "approved" if finished else "executing"
            note = None
        target, outside = _approval_target(
            (request or {}).get("tool_name"), (request or {}).get("arguments"), secrets, workspace_root
        )
        return ApprovalView(
            approval_id=approval_id,
            # `run_id` / `tool_call_id` 在旧 Checkpoint 里可能确实没有（决定记录早于这两个字段，或
            # `pending_approval` 已被清掉）。DTO 要求非空，直接填空串会让**整个会话详情接口 500**——
            # 实测一个会话因此完全打不开（`GET /api/sessions/{id}` → `ApprovalView` 校验失败）。
            # 按这一层既有口径写 `_NOT_RECORDED`：如实说没有记录，不猜一个看起来合理的值。
            run_id=run or _NOT_RECORDED,
            tool_call_id=call_id or _NOT_RECORDED,
            # 决定本身不带工具名，`pending_approval` 又可能已被清掉：从工具事件里补；
            # 旧 Checkpoint 两处都没有时写"未记录"，而不是猜一个名字。
            tool_name=_tool_name_of(state, run, call_id) or _NOT_RECORDED,
            action_fingerprint=str(decided.get("action_fingerprint") or (request or {}).get("action_fingerprint") or "")
            or _NOT_RECORDED,
            redacted_arguments=_display_arguments((request or {}).get("arguments"), secrets),
            edited_arguments=_display_arguments(decided.get("edited_arguments"), secrets),
            # 目标只能从原始请求参数来；请求已被清掉时如实为空，不去别处猜。
            target=target,
            outside_workspace=outside,
            impact_summary=_approval_impact(request, secrets, workspace_root),
            risk="high" if bool((request or {}).get("requires_second_confirmation")) else "normal",
            state=state_value,
            created_at=_as_utc((request or {}).get("created_at")) or _as_utc(decided.get("created_at")) or _epoch(),
            note=sanitize_text(note, secrets) if note is not None else None,
        )

    if request is None:
        return None

    approved_now = has_live_interrupt
    approval_state: ApprovalState = "pending" if approved_now else "expired"
    target, outside = _approval_target(request.get("tool_name"), request.get("arguments"), secrets, workspace_root)
    return ApprovalView(
        approval_id=approval_id,
        # 与上面的"已决定"分支同一口径：缺失写 `_NOT_RECORDED`，绝不填空串——DTO 要求非空，
        # 空串会让整个会话详情接口 500，把"某个字段没有记录"升级成"这个会话打不开"。
        run_id=str(request.get("run_id") or "") or _NOT_RECORDED,
        tool_call_id=str(request.get("tool_call_id") or "") or _NOT_RECORDED,
        tool_name=str(request.get("tool_name") or ""),
        action_fingerprint=str(request.get("action_fingerprint") or ""),
        redacted_arguments=_display_arguments(request.get("arguments"), secrets),
        target=target,
        outside_workspace=outside,
        confirmation_stage=2 if confirmation_stage == 2 else 1,
        impact_summary=_approval_impact(request, secrets, workspace_root),
        risk="high" if bool(request.get("requires_second_confirmation")) else "normal",
        state=approval_state,
        created_at=_as_utc(request.get("created_at")) or _epoch(),
        note=None if approved_now else "该审批已失效，未执行任何操作。请重新发起请求。",
    )


def _approval_impact(request: Mapping[str, Any] | None, secrets: Iterable[str], workspace_root: Path | None) -> str:
    text = str((request or {}).get("impact_summary") or "（未记录影响摘要）")
    if workspace_root is not None:
        root = workspace_root.resolve()
        for prefix in (root.as_posix() + "/", str(root) + "\\"):
            text = text.replace(prefix, "")
    return sanitize_text(text.replace("（T10）", "").replace("(T10)", ""), secrets)[:DISPLAY_ARGUMENT_CHARS]


def _epoch() -> datetime:
    """没有时间戳时的占位：用 Unix epoch 而不是"现在"，否则同一 State 每次投影都不同。"""
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---- 错误 ------------------------------------------------------------------


def project_errors(state: Mapping[str, Any], *, secrets: Iterable[str] = ()) -> list[ErrorView]:
    """本轮错误的公开视图：节点名换成公开错误码，文本先脱敏。"""
    views: list[ErrorView] = []
    for event in _run_errors(state):
        view = _error_view(event, secrets=secrets)
        if view is not None:
            views.append(view)
    return views


def _error_view(event: Mapping[str, Any], *, secrets: Iterable[str]) -> ErrorView | None:
    event_id = str(event.get("event_id") or "")
    if not event_id:
        return None
    stage = str(event.get("stage") or "")
    code, _category = _STAGE_ERRORS.get(stage, (app_errors.RUN_STAGE_FAILED, "system"))
    title, hint = _ERROR_TEXT[code]
    message = _public_error_text(str(event.get("message") or "").strip() or title, secrets)
    return ErrorView.for_code(
        code,
        title=title,
        message=message,
        hint=hint,
        diagnostic_id=_diagnostic_id(code, event_id, stage, str(event.get("run_id") or "")),
        occurred_at=_as_utc(event.get("created_at")) or _epoch(),
        source="run",
    )


def _diagnostic_id(code: str, event_id: str, stage: str, run_id: str) -> str:
    """Stable public correlation number; internal run, node and tool IDs stay local.

    Local error records can reproduce this number from their original identity.
    It is a diagnostic locator, not a model-prompt or tool-result fingerprint.
    """
    identity = json.dumps([event_id, stage, run_id], ensure_ascii=False)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{code}:{digest}"


# ---- 用量 / 上下文 / 结果 --------------------------------------------------


def project_usage(state: Mapping[str, Any], run_id: str | None = None) -> TurnRecordView | None:
    """本轮的用量汇总（§15.6）：**由持久记录确定性加总**，不读时钟、不猜值。

    - 明细 = 本轮（`run_id` 相同）的 `model_call_records`；汇总字段逐项加总得出。
    - 任一明细缺某字段 → 该字段汇总为 `None`（未知即不显示，绝不把"缺"当 0）。
    - **失败尝试不参与加总**（§15.6，2026-09-30）：没成功、且没有报告任何实际用量的调用按"没有消耗"
      处理（见 :func:`_counts_toward_usage`），但仍逐条留在 `calls` 明细里。
    - 没有任何明细记录（旧 Checkpoint）→ `usage_recorded=False`，页面显示"历史版本未记录"。
    """
    run_id = run_id or current_run_id(state)
    if not run_id:
        return None
    records = [record for record in _model_call_records(state) if getattr(record, "run_id", "") == run_id]
    # **加总只用"计费的调用"**（§15.6）。改这一处的原因是实测到的后果：被端点拒绝的请求（例如不支持
    # `response_format: json_schema` 的端点回 400，客户端会降级重试）留下一条 `status="error"` 的记录，
    # 它的"缺"是**已知的零**（没计费），可 `_sum_optional` 把它当未知，于是**整轮**的输入、命中率、输出
    # 全部塌成未知——页面上的"缓存命中 未知"就是这么来的。T34 基线的结论 3 曾把这个行为当成预期
    # （"有一次调用失败且没拿到用量，整轮汇总就是未知"），现按 §15.6 新增的口径改成"失败尝试不毒掉整轮"。
    counted = [record for record in records if _counts_toward_usage(record)]
    turn = _turn_record(state, run_id)
    context = project_context(state) if run_id == current_run_id(state) else ContextMeterView()
    latest = records[-1] if records else None
    uncached = _sum_optional([record.actual_uncached_input_tokens for record in counted])
    cache_read = _sum_optional([record.actual_cache_read_tokens for record in counted])
    cache_write = _sum_optional([record.actual_cache_write_tokens for record in counted])
    output = _sum_optional([record.actual_output_tokens for record in counted])
    reasoning = _sum_optional([record.actual_reasoning_tokens for record in counted])
    # 估算量**仍然算上失败尝试**：它描述的是"本轮组装并发出过多少请求"，失败的那次确实组装并发了；
    # 实际量描述的是"计费消耗"，失败的那次不计费。两者口径不同，不是不一致。
    estimated = _sum_optional([record.estimated_prompt_tokens for record in records])
    if not records:
        # 旧 Checkpoint：一条用量记录都没有。估算值仍然可用——它就是本轮组装请求的估算输入
        # （与计量器同源）。这里**只补估算**，实际用量仍留空（`usage_recorded=False`）。
        estimated = context.used_tokens
    ttfts = [
        record.time_to_first_token_ms
        for record in records
        if record.time_to_first_token_ms is not None
    ]
    submitted = getattr(turn, "submitted_at", None) if turn is not None else None
    final_at = getattr(turn, "final_at", None) if turn is not None else None
    used = latest.context_used_tokens if latest is not None else context.used_tokens
    window = latest.context_window_tokens if latest is not None else context.window_tokens
    return TurnRecordView(
        run_id=run_id,
        submitted_at=submitted if isinstance(submitted, datetime) else _epoch(),
        final_at=final_at if isinstance(final_at, datetime) else None,
        final_state=_turn_final_state(turn, state),
        model_call_ids=[record.model_call_id for record in records],
        tool_event_ids=list(_tool_event_ids(state, run_id)),
        approval_wait_ms=getattr(turn, "approval_wait_ms", None) if turn is not None else None,
        turn_elapsed_ms=_elapsed_ms(submitted, final_at),
        model_elapsed_ms=sum(record.model_elapsed_ms for record in records) if records else None,
        tool_elapsed_ms=_tool_elapsed_ms(state, run_id),
        time_to_first_token_ms=min(ttfts) if ttfts else None,
        estimated_prompt_tokens=estimated,
        actual_uncached_input_tokens=uncached,
        actual_cache_read_tokens=cache_read,
        actual_cache_write_tokens=cache_write,
        actual_output_tokens=output,
        actual_reasoning_tokens=reasoning,
        cache_metrics_reported=any(record.cache_metrics_reported for record in records),
        cache_hit_ratio=cache_hit_ratio(uncached, cache_read, cache_write),
        context_used_tokens=used,
        context_window_tokens=window,
        context_usage_ratio=context_usage_ratio(used, window),
        usage_recorded=bool(records),
    )


def project_turn_usages(state: Mapping[str, Any]) -> list[TurnUsageView]:
    """只投影有稳定用户消息锚点的轮次；旧 Checkpoint 不猜轮次归属。"""
    turns = state.get("turn_records")
    if not isinstance(turns, Mapping):
        return []
    result: list[TurnUsageView] = []
    for run_id, turn in turns.items():
        user_message_id = getattr(turn, "user_message_id", None)
        if not isinstance(run_id, str) or not isinstance(user_message_id, str) or not user_message_id:
            continue
        usage = project_usage(state, run_id)
        if usage is not None:
            result.append(TurnUsageView(
                user_message_id=user_message_id,
                usage=usage,
                model_calls=project_model_calls(state, run_id),
            ))
    return result


def project_model_calls(state: Mapping[str, Any], run_id: str) -> list[ModelCallRecordView]:
    """本轮（`run_id` 相同）的全部调用明细，按发生顺序（实时事件与页面明细共用）。"""
    return [
        project_model_call(record)
        for record in _model_call_records(state)
        if getattr(record, "run_id", "") == run_id
    ]


def project_model_call(record: Any) -> ModelCallRecordView:
    """一条模型调用记录 → 公开视图（事件载荷与页面明细共用）。"""
    return ModelCallRecordView(
        model_call_id=record.model_call_id,
        run_id=record.run_id,
        purpose=record.purpose,
        connection_id=record.connection_id,
        config_version=record.config_version,
        provider=record.provider,
        model_id=record.model_id,
        endpoint_fingerprint=record.endpoint_fingerprint,
        retry_of=record.retry_of,
        started_at=record.started_at,
        first_token_at=record.first_token_at,
        finished_at=record.finished_at,
        status=record.status,
        estimated_prompt_tokens=record.estimated_prompt_tokens,
        actual_uncached_input_tokens=record.actual_uncached_input_tokens,
        actual_cache_read_tokens=record.actual_cache_read_tokens,
        actual_cache_write_tokens=record.actual_cache_write_tokens,
        actual_output_tokens=record.actual_output_tokens,
        actual_reasoning_tokens=record.actual_reasoning_tokens,
        cache_metrics_reported=record.cache_metrics_reported,
        model_elapsed_ms=record.model_elapsed_ms,
        time_to_first_token_ms=record.time_to_first_token_ms,
        context_window_tokens=record.context_window_tokens,
        context_used_tokens=record.context_used_tokens,
        tools_schema_fingerprint=record.tools_schema_fingerprint,
        system_prompt_fingerprint=getattr(record, "system_prompt_fingerprint", None),
        history_prefix_fingerprint=getattr(record, "history_prefix_fingerprint", None),
        dynamic_context_fingerprint=getattr(record, "dynamic_context_fingerprint", None),
    )


def _model_call_records(state: Mapping[str, Any]) -> list[Any]:
    value = state.get("model_call_records")
    return list(value) if isinstance(value, Sequence) else []


def _turn_record(state: Mapping[str, Any], run_id: str) -> Any:
    value = state.get("turn_records")
    if isinstance(value, Mapping):
        return value.get(run_id)
    return None


def _turn_final_state(turn: Any, state: Mapping[str, Any]) -> TurnFinalState:
    """整轮状态优先取自持久记录（含 `awaiting_approval`），没有记录时回退到 `task_status`。"""
    value = getattr(turn, "final_state", None) if turn is not None else None
    if value in ("running", "awaiting_approval", "blocked", "completed", "failed"):
        return value
    return project_turn_state(state)


def _sum_optional(values: Sequence[int | None]) -> int | None:
    """逐项加总；有任何一项未知就整体未知（未知 + 已知 ≠ 已知）。"""
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _counts_toward_usage(record: Any) -> bool:
    """这条调用记录是否参与本轮用量的**加总**（§15.6「失败尝试不毒掉整轮」）。

    只有一种不参与：**没成功、且没有报告任何实际用量**的调用。它与"成功但供应商没上报某个字段"
    是两件不同的事：

    - 后者（成功、字段缺失）仍按**未知**处理——那才是"未上报 ≠ 0"，会如实把该字段的汇总抹成未知；
    - 前者（被拒绝的请求）按**没有消耗**处理——请求根本没被受理，供应商也没有产生计费。

    **已知边界**：记录里没有错误种类，无法区分"被拒绝"与"生成中途失败"。后者若供应商不回报用量，
    这里会**低估**——所以失败调用必须继续出现在明细里（标失败、用量未知），页面不得把它当作不存在。
    """
    if getattr(record, "status", "ok") == "ok":
        return True
    return any(
        getattr(record, field, None) is not None
        for field in (
            "actual_uncached_input_tokens",
            "actual_cache_read_tokens",
            "actual_cache_write_tokens",
            "actual_output_tokens",
            "actual_reasoning_tokens",
        )
    )


def _run_tool_events(state: Mapping[str, Any], run_id: str) -> list[Any]:
    events = state.get("tool_events")
    if not isinstance(events, Sequence):
        return []
    return [event for event in events if getattr(event, "run_id", "") == run_id]


def _tool_event_ids(state: Mapping[str, Any], run_id: str) -> tuple[str, ...]:
    return tuple(
        tool_scope_key(event.run_id, event.tool_call_id) for event in _run_tool_events(state, run_id)
    )


def _tool_elapsed_ms(state: Mapping[str, Any], run_id: str) -> int | None:
    """本轮的工具有效耗时之和；没有任何一对完整时间戳时为 ``None``（未知）。"""
    total = 0
    found = False
    for event in _run_tool_events(state, run_id):
        started = getattr(event, "started_at", None)
        finished = getattr(event, "finished_at", None)
        if isinstance(started, datetime) and isinstance(finished, datetime):
            total += max(0, round((finished - started).total_seconds() * 1000))
            found = True
    return total if found else None


def project_context(state: Mapping[str, Any]) -> ContextMeterView:
    """计量器口径：只认已组装请求的估算输入占用与已确认窗口；未知就是未知。

    输出预留**单列**（`completion_reserve_tokens`），不重复计入已使用输入（§15.6）。
    """
    usage_map = _as_mapping(state.get("token_usage")) or {}
    budget_map = _as_mapping(state.get("context_budget")) or {}
    used = usage_map.get("prompt_tokens")
    reserve = usage_map.get("completion_reserve_tokens")
    window = budget_map.get("context_window_tokens")
    known = isinstance(used, int) and isinstance(window, int) and window > 0
    return ContextMeterView(
        used_tokens=int(used) if isinstance(used, int) else None,
        window_tokens=int(window) if isinstance(window, int) and window > 0 else None,
        scope=("current" if state.get("task_status") == "running" else "previous") if known else "unknown",
        completion_reserve_tokens=int(reserve) if isinstance(reserve, int) else None,
    )


def project_runtime_context(state: Mapping[str, Any]) -> RuntimeContextSnapshotView | None:
    """投影**最新有效**的运行上下文快照视图（架构 §15.7、§15.8）。

    只给出"存在哪个版本的上下文快照、来源各自是什么版本、正文有多长"：``canonical_text``
    属于**明确不展示**的一类（§15.8），因此视图里只有长度，没有正文。

    投影层按既有约定**只读 State 数据**（不导入 ``local_agent.graph.state``，见
    ``tests/test_events.py`` 的边界用例），"最新有效 = ``context_version`` 最大、并列取最后追加"
    的口径与 :func:`local_agent.graph.state.latest_runtime_snapshot` 一致。

    旧 Checkpoint（引入快照前）没有这个字段 → 返回 ``None``，**不伪造**一条空快照。
    """
    latest_version: int | None = None
    latest: Mapping[str, Any] | None = None
    for snapshot in state.get("runtime_context_snapshots") or []:
        payload = _as_mapping(snapshot)
        if payload is None:
            continue
        version = payload.get("context_version")
        if not isinstance(version, int):
            continue
        if latest_version is None or version >= latest_version:
            latest_version, latest = version, payload
    if latest is None:
        return None
    snapshot_id = latest.get("snapshot_id")
    run_id = latest.get("run_id")
    content_hash = latest.get("content_hash")
    if not (isinstance(snapshot_id, str) and isinstance(run_id, str) and isinstance(content_hash, str)):
        return None
    return RuntimeContextSnapshotView(
        schema_version=(
            latest["schema_version"] if isinstance(latest.get("schema_version"), int) else SCHEMA_VERSION
        ),
        snapshot_id=snapshot_id,
        run_id=run_id,
        context_version=int(latest_version or 0),
        created_at=_as_utc(latest.get("created_at")) or _epoch(),
        content_hash=content_hash,
        text_length=len(str(latest.get("canonical_text") or "")),
        source_versions=[
            RuntimeSourceVersionView(source=source, version=version)
            for source, version in _source_version_pairs(latest.get("source_versions"))
        ],
    )


def _source_version_pairs(value: Any) -> list[tuple[str, str]]:
    """``source_versions`` 解析：``(来源名, 版本指纹)`` 二元组序列，非字符串/空串的条目丢掉。

    msgpack 往返会把 tuple 变成 list（见 ``tests/test_checkpoint_serde.py``），因此这里不假设容器类型；
    视图对两个字段都要求非空（``min_length=1``），所以只放真正是字符串且有值的条目，不硬凑占位值。
    """
    pairs: list[tuple[str, str]] = []
    for entry in value or ():
        if not (isinstance(entry, (tuple, list)) and len(entry) == 2):
            continue
        source, version = entry
        if isinstance(source, str) and source and isinstance(version, str) and version:
            pairs.append((source, version))
    return pairs


def project_turn_state(state: Mapping[str, Any]) -> TurnFinalState:
    """本轮状态。运行中/等待审批/等待用户/完成/失败由 `task_status` 与是否还有未确认调用共同决定。

    ``blocked`` 与 ``running`` 必须分开：前者是**已结束**（在等用户补充），后者是**还在跑**。
    混成一个取值会让"等用户补充"的轮既发不出回答、也不发终态事件（复审实证，2026-09-28）。
    """
    task_status = str(state.get("task_status") or "")
    if task_status == "completed":
        return "completed"
    if task_status == "failed":
        return "failed"
    if task_status == "blocked":
        return "blocked"
    return "running"


def project_session_state(state: Mapping[str, Any], *, has_live_interrupt: bool) -> SessionState:
    """会话状态只由"是否有待决审批""`task_status`""是否已有 run"决定。

    刻意不看 `final_response`：新一轮开始时上一轮的回答仍在 State 里，用"有没有回答"判断会把
    正在运行的一轮说成空闲。
    """
    if has_live_interrupt:
        return "awaiting_approval"
    task_status = str(state.get("task_status") or "")
    if task_status == "completed":
        return "completed"
    if task_status == "failed":
        return "failed"
    if task_status == "blocked":
        return "blocked"
    if task_status == "running" or current_run_id(state):
        return "running"
    return "idle"


def project_final_result(
    state: Mapping[str, Any], *, secrets: Iterable[str] = ()
) -> FinalResultView | None:
    """最终结果：只给可读摘要与证据引用，不给内部对象。

    只有 ``running`` 才"还没有最终结果"——``blocked`` 同样是终态：这一轮不会再往下走，回答
    已经生成，必须发出去。原先只放行 ``completed``/``failed``，于是"等用户补充"的轮把回答
    留在 State 里，页面永远停在"正在生成回答…"。
    """
    response = state.get("final_response")
    text = response if isinstance(response, str) else ""
    run_id = current_run_id(state)
    if not run_id or not text:
        return None
    state_value = project_turn_state(state)
    if state_value == "running":
        return None
    evidence = [
        str(item.get("reference_id") or item.get("summary") or "")
        for item in _mappings(state.get("final_evidence"))
    ]
    return FinalResultView(
        run_id=run_id,
        state=_final_result_state(state_value),
        summary=sanitize_text(text, secrets),
        evidence_references=[sanitize_text(item, secrets) for item in evidence if item],
    )


def _final_result_state(value: TurnFinalState) -> FinalResultState:
    """轮次状态 → 最终结果状态。走到这里已经不可能是 ``running``。

    ``awaiting_approval`` 不会终结一轮（它由中断挂着），万一在这里出现按 ``failed`` 处理：
    宁可说失败，也不冒充完成。
    """
    if value == "completed":
        return "completed"
    if value == "blocked":
        return "blocked"
    return "failed"


# ---- 会话快照 --------------------------------------------------------------


def project_snapshot(
    state: Mapping[str, Any],
    *,
    session_id: str,
    workspace: WorkspaceRef,
    title: str,
    snapshot_id: str,
    created_at: datetime,
    has_live_interrupt: bool = False,
    secrets: Iterable[str] = (),
    model_selection: ModelSelectionView | None = None,
    workspace_root: Path | None = None,
    confirmation_stage: int = 1,
) -> SessionSnapshot:
    """把一个 State 投影成 `session_snapshot` 载荷。

    `created_at` 由调用方给出（实时快照用当前时间，重放历史用记录里的时间）：投影层不读时钟，
    否则同一 Checkpoint 两次投影会得到不同字节。

    `model_selection` 同理由调用方（设置库的读取者）给出：投影层只投影 State，
    不自己去读设置库——否则同一份 Checkpoint 会因为设置变化而投影出不同字节。
    """
    run_id = current_run_id(state)
    approval = project_approval(
        state, has_live_interrupt=has_live_interrupt, secrets=secrets,
        workspace_root=workspace_root, confirmation_stage=confirmation_stage,
    )
    return SessionSnapshot(
        schema_version=SCHEMA_VERSION,
        snapshot_id=snapshot_id,
        session_id=session_id,
        workspace=workspace,
        title=title,
        session_state=project_session_state(state, has_live_interrupt=has_live_interrupt),
        current_task=TaskRef(task_id=run_id, run_id=run_id) if run_id else None,
        messages=project_messages_view(state, secrets=secrets),
        plan=project_plan(state, secrets=secrets),
        approval=approval,
        memory=project_memory(state, secrets=secrets),
        model_selection=model_selection,
        errors=project_errors(state, secrets=secrets),
        usage=project_usage(state),
        model_calls=project_model_calls(state, run_id),
        turn_usages=project_turn_usages(state),
        context=project_context(state),
        runtime_context=project_runtime_context(state),
        final_result=project_final_result(state, secrets=secrets),
        created_at=created_at,
    )

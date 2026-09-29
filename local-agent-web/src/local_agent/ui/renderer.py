"""应用服务 DTO → 页面展示（v0.4 T33）。

职责边界：本模块只把**应用服务（T28）已经算好的 DTO** 渲染出来，不做业务判断，也不调用模型
或工具。它**不导入** LangChain 消息类、LangGraph 或 Graph 的 State——那是旧入口（T11 之前）
才直接解释的东西；T33 之后页面唯一的业务来源是 `local_agent.application.models` 的契约。

依赖 Streamlit 的函数只在页面运行时执行；``to_chat_messages`` / ``visible_errors`` /
``diff_text`` / ``tool_summary`` / ``config_error_text`` 是纯函数，便于单元测试。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import streamlit as st

from local_agent.application.models import (
    ErrorView,
    FileDiffView,
    MemoryUseView,
    MessageView,
    SessionSnapshot,
    ToolUseView,
    UserMessageView,
)

ChatRole = Literal["user", "assistant"]

#: 工具行状态 → 展示文字（与应用服务的 `ToolStatus` 一一对应）。
_STATUS_LABELS: Mapping[str, str] = {
    "pending": "待执行",
    "running": "执行中",
    "ok": "成功",
    "error": "失败",
    "denied": "被拒绝",
}

#: 结构化结果分区 → 展示文字（对应 `ToolOutcomeKind`）。
_KIND_LABELS: Mapping[str, str] = {
    "read": "读取",
    "list": "列目录",
    "edit": "编辑",
    "write": "写入",
    "delete": "删除",
    "shell": "命令",
}

#: unified diff 每行的前缀（与 `FileDiffLineKind` 对应）。
_DIFF_PREFIX: Mapping[str, str] = {"context": " ", "add": "+", "del": "-"}


@dataclass(frozen=True)
class ChatMessage:
    """一条待展示的聊天消息（只承载已脱敏的展示文本）。"""

    role: ChatRole
    content: str


def to_chat_messages(messages: Sequence[MessageView]) -> list[ChatMessage]:
    """把消息 DTO 里的用户与助手**文本**取出来（工具/记忆/错误行由各自的渲染器处理）。"""
    rendered: list[ChatMessage] = []
    for message in messages:
        if isinstance(message, UserMessageView):
            role: ChatRole = "user"
        elif message.kind == "assistant":
            role = "assistant"
        else:
            continue
        text = message.text.strip()
        if text:
            rendered.append(ChatMessage(role=role, content=text))
    return rendered


def visible_errors(snapshot: SessionSnapshot) -> list[ErrorView]:
    """快照里**尚未**作为消息行出现的错误。

    与前端（`LiveApp.tsx`）同一口径：会话级错误按 ``diagnostic_id`` 与消息行去重后追加成行，
    这样同一件失败既不会被渲染两次，也不会因为"它只在 `errors` 里"而被漏掉。
    """
    shown = {
        message.error.diagnostic_id
        for message in snapshot.messages
        if message.kind == "error"
    }
    return [error for error in snapshot.errors if error.diagnostic_id not in shown]


def diff_text(diff: FileDiffView) -> str:
    """把**结构化** diff 还原成 unified diff 文本供展示。

    行号与内容都来自 DTO（页面不从自然语言或文本里重新解析，架构 §6.1）；这里拼回文本只是
    为了让 Streamlit 用等宽高亮显示，不是再解析一遍。
    """
    lines = [f"--- {diff.path}", f"+++ {diff.path}"]
    for hunk in diff.hunks:
        lines.append(hunk.header)
        for line in hunk.lines:
            lines.append(f"{_DIFF_PREFIX.get(line.kind, ' ')}{line.text}")
    return "\n".join(lines)


def tool_heading(tool: ToolUseView) -> str:
    """工具行的展开标题：名称、类别与状态。"""
    label = f"工具调用：{tool.tool_name}"
    if tool.outcome is not None:
        label = f"{label}（{_KIND_LABELS.get(tool.outcome.kind, tool.outcome.kind)}）"
    return f"{label}（{_STATUS_LABELS.get(tool.status, tool.status)}）"


def tool_summary(tool: ToolUseView) -> str:
    """工具行的一行摘要：目标、结果、退出码、耗时与机械校验证据。"""
    parts: list[str] = []
    outcome = tool.outcome
    if outcome is not None:
        if outcome.target:
            parts.append(f"目标 {outcome.target}")
        if outcome.summary:
            parts.append(outcome.summary)
        if outcome.exit_code is not None:
            parts.append(f"退出码 {outcome.exit_code}")
        if outcome.timed_out:
            parts.append("超时")
        if outcome.total is not None:
            parts.append(f"共 {outcome.total} 项")
        if outcome.truncated:
            parts.append("这里只是有界预览")
    if tool.elapsed_ms is not None:
        parts.append(f"{tool.elapsed_ms} ms")
    if tool.evidence is not None:
        verdict = "已核对" if tool.evidence.verified else "未核对"
        parts.append(f"证据：{tool.evidence.summary}（{verdict}）")
    return "；".join(parts)


def config_error_text(reason: str, hint: str | None = None) -> str:
    """把配置检查结果拼成面向用户的提示文本，不涉及模型调用。"""
    text = f"模型尚未配置：{reason}"
    if hint:
        text = f"{text}\n\n{hint}"
    return text


def session_state_text(snapshot: SessionSnapshot) -> str:
    """会话当前状态的短说明（与应用服务的 `SessionState` 一致）。"""
    labels = {
        "idle": "空闲",
        "running": "运行中",
        "awaiting_approval": "等待审批",
        "completed": "已完成",
        "failed": "失败",
    }
    return labels.get(snapshot.session_state, snapshot.session_state)


# --------------------------------------------------------------------------- 渲染


def render_message(message: ChatMessage) -> None:
    """渲染单条文本消息。"""
    with st.chat_message(message.role):
        st.markdown(message.content)


def render_messages(messages: Sequence[ChatMessage]) -> None:
    """按顺序渲染文本消息。"""
    for message in messages:
        render_message(message)


def render_tool_row(tool: ToolUseView) -> None:
    """渲染一次工具调用：参数、结构化结果与证据（架构 §6.3、§6.7）。"""
    with st.expander(tool_heading(tool), expanded=False):
        if tool.redacted_arguments:
            st.json(tool.redacted_arguments)
        outcome = tool.outcome
        if outcome is not None:
            if outcome.diff is not None:
                st.code(diff_text(outcome.diff), language="diff")
            if outcome.text:
                st.code(outcome.text, language="text")
            if outcome.paths:
                st.code("\n".join(outcome.paths), language="text")
            if outcome.stdout:
                st.code(outcome.stdout, language="text")
            if outcome.stderr:
                st.code(outcome.stderr, language="text")
        elif tool.output_preview:
            # 认不出的工具：退回通用预览（在服务里已被截断并标 truncated）。
            st.code(tool.output_preview, language="text")
        summary = tool_summary(tool)
        if summary:
            st.caption(summary)
        if tool.approval_id:
            st.caption(f"审批编号：{tool.approval_id}")


def render_memory_row(memory: MemoryUseView) -> None:
    """渲染本轮的检索/长期记忆结果（miss 与 failed 是两个不同事实）。"""
    outcome_labels = {"hit": "命中", "miss": "未命中", "failed": "检索失败"}
    parts = [f"记忆检索：{outcome_labels.get(memory.retrieval_outcome, memory.retrieval_outcome)}"]
    if memory.write_outcome is not None:
        parts.append("已写入" if memory.write_outcome == "written" else "写入失败")
    text = "；".join(parts)
    detail = [f"{source.reference}（{source.usage}）" for source in memory.sources]
    body = f"{text}\n{memory.retrieval_note}"
    if detail:
        body = f"{body}\n" + "\n".join(f"- {line}" for line in detail)
    st.info(body)


def render_error_row(error: ErrorView) -> None:
    """渲染一条错误/降级（文本在服务里已脱敏，页面不再改写）。"""
    text = f"{error.title}\n\n{error.message}\n\n{error.hint}"
    if error.severity == "warning":
        st.warning(text)
    else:
        st.error(text)
    tail = f"错误码 {error.code}；诊断编号 {error.diagnostic_id}"
    if error.retry_condition:
        tail = f"{tail}；{error.retry_condition}"
    st.caption(tail)


def render_message_view(message: MessageView) -> None:
    """按消息种类分派到对应渲染器。"""
    if message.kind == "tool":
        render_tool_row(message.tool)
    elif message.kind == "memory":
        render_memory_row(message.memory)
    elif message.kind == "error":
        render_error_row(message.error)
    else:
        for chat_message in to_chat_messages([message]):
            render_message(chat_message)


def render_history(messages: Sequence[MessageView], *, start_index: int = 0) -> None:
    """按顺序渲染会话消息（文本气泡、工具行、记忆行、错误行）。"""
    for index, message in enumerate(messages):
        if index < start_index:
            continue
        render_message_view(message)


def render_conversation(snapshot: SessionSnapshot) -> None:
    """渲染一份会话快照的对话区：消息行 + 未重复出现的会话级错误。"""
    render_history(snapshot.messages)
    for error in visible_errors(snapshot):
        render_error_row(error)


def render_status(snapshot: SessionSnapshot) -> None:
    """渲染会话状态与最终结果摘要（结论只来自快照，页面不自算）。"""
    st.caption(
        f"会话状态：{session_state_text(snapshot)}"
        + (f"；工作区：{snapshot.workspace.display_name}")
    )
    if snapshot.plan is not None and snapshot.plan.steps:
        with st.expander(f"计划：{snapshot.plan.goal}", expanded=False):
            for step in snapshot.plan.steps:
                st.markdown(f"- [{step.status}] {step.title}")
    if snapshot.final_result is not None:
        result = snapshot.final_result
        text = f"本轮结果（{result.state}）：{result.summary}"
        if result.state == "failed":
            st.error(text)
        else:
            st.success(text)


def render_model_status(*, configured: bool, text: str) -> None:
    """在页面上展示模型配置状态。"""
    if configured:
        st.success(text)
    else:
        st.error(text)


def render_memory_status(*, lines: Sequence[str], warn: bool = False) -> None:
    """在侧栏展示记忆与 Embedding 的准备状态（架构 §1.4）。

    每行一个主题（短期 / 中期 / 长期 / 压缩）；需要用户处理时用 warning 样式，避免出现
    "看起来一切正常、实际某层是关的或被降级"的误读。
    """
    if not lines:
        return
    text = "\n".join(f"- {line}" for line in lines)
    if warn:
        st.warning(text)
    else:
        st.info(text)


# --------------------------------------------------------------------------- 会话侧栏


@dataclass(frozen=True)
class SessionOption:
    """侧栏里的一条会话（展示数据由页面层从 DTO 映射，本模块不做业务判断）。"""

    session_id: str
    thread_id: str
    title: str
    detail: str
    is_active: bool


SessionActionKind = Literal["create", "open", "select_workspace"]


@dataclass(frozen=True)
class SessionAction:
    """用户在侧栏做出的选择；落到应用服务的动作由页面层执行。"""

    kind: SessionActionKind
    session_id: str | None = None
    #: ``select_workspace`` 的目标：列表里选中的稳定标识，或用户输入的原始路径。
    workspace_id: str | None = None
    workspace_path: str | None = None


def render_workspace_picker(
    *,
    workspaces: Sequence[tuple[str, str]],
    current_workspace_id: str | None,
    current_workspace_name: str | None,
    key_prefix: str = "session",
) -> SessionAction | None:
    """工作区选择器：已使用工作区列表 + 绝对路径输入（架构 §1.5、§10.6）。

    ``workspaces`` 是 ``(workspace_id, display_name)`` 列表——页面只拿得到不含路径的稳定标识
    （`WorkspaceRef` 的契约），路径仍通过"输入绝对路径添加"这条入口进入服务端校验。

    自己负责进侧栏：它既被 :func:`render_session_sidebar` 调用（那里已经在 ``st.sidebar`` 里，
    嵌套无副作用），也可能被单独调用。
    """
    with st.sidebar:
        st.subheader("工作区")
        if current_workspace_name:
            st.caption(f"当前工作区：{current_workspace_name}")

        known = list(workspaces)
        if known:
            identifiers = [item[0] for item in known]
            labels = {item[0]: item[1] for item in known}
            selected = st.selectbox(
                "已使用的工作区（切换即恢复该工作区的最近会话）",
                options=identifiers,
                index=identifiers.index(current_workspace_id)
                if current_workspace_id in identifiers
                else 0,
                format_func=labels.__getitem__,
                key=f"{key_prefix}-workspace-pick-{current_workspace_id or 'none'}-{len(known)}",
            )
            if selected and selected != current_workspace_id:
                return SessionAction(kind="select_workspace", workspace_id=str(selected))

        typed = st.text_input(
            "工作区绝对路径",
            placeholder="例如 E:/projects/demo",
            help="目录不存在或不可读时只提示，不会改变当前会话。",
            key=f"{key_prefix}-workspace-path",
        )
        if st.button("切换到此工作区", key=f"{key_prefix}-workspace-apply"):
            return SessionAction(kind="select_workspace", workspace_path=typed)
    return None


def render_session_sidebar(
    options: Sequence[SessionOption],
    *,
    key_prefix: str = "session",
    workspaces: Sequence[tuple[str, str]] = (),
    current_workspace_id: str | None = None,
    current_workspace_name: str | None = None,
) -> SessionAction | None:
    """渲染侧栏：**工作区选择器 + 会话列表**（架构 §1.5、§10.6）。

    Args:
        options: 该工作区可见的会话列表（含当前打开的会话）。
        key_prefix: 控件 key 前缀，避免同一页面内多个控件冲突。
        workspaces: 已使用过的 ``(workspace_id, display_name)``。
        current_workspace_id: 当前会话绑定工作区的稳定标识。
        current_workspace_name: 当前会话绑定工作区的展示名。

    Returns:
        用户本次做出的选择；没有操作时返回 ``None``。

    会话的"打开"只是页面选择（应用服务按提交参数定位会话，没有全局激活项），所以这里只回
    ``open``；重命名等索引写操作 v0.4 没有对应的服务用例，页面因此不提供（只经应用服务）。
    """
    with st.sidebar:
        workspace_action = render_workspace_picker(
            workspaces=workspaces,
            current_workspace_id=current_workspace_id,
            current_workspace_name=current_workspace_name,
            key_prefix=key_prefix,
        )
        if workspace_action is not None:
            return workspace_action

        st.subheader("会话")
        if st.button("新建会话", key=f"{key_prefix}-new"):
            return SessionAction(kind="create")
        if not options:
            st.caption("还没有可用会话（先在服务库里新建一个）")
            return None

        # `is_active` 由页面层标在**当前打开**的那条上（不是库里的全局激活项）。
        current = next((option for option in options if option.is_active), options[0])
        # 每条会话一个按钮，而不是单选框：单选框是有状态的控件，浏览器会把上一次的选择原样回传，
        # 页面在"新建会话"里改的当前会话会被它覆盖，表现为"新建后又悄悄切回旧会话"。按钮只在点击
        # 的那一轮为真，"当前打开哪个会话"就只由页面的 session_state 决定，行为可预期。
        for option in options:
            marker = "●" if option.is_active else "○"
            if st.button(
                f"{marker} {option.title}｜{option.detail}",
                key=f"{key_prefix}-open-{option.session_id}",
                use_container_width=True,
            ):
                return SessionAction(kind="open", session_id=option.session_id)
        st.caption(f"会话 ID：{current.session_id}；thread_id：{current.thread_id}")
    return None
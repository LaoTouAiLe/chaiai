"""聊天消息、工具活动与配置状态的展示。

职责边界（开发文档 §3.1）：本模块只负责把已经算好的内容渲染出来，不做业务判断，
也不调用模型或工具。依赖 Streamlit 的函数只在页面运行时执行；
``config_error_text``、``to_chat_messages``、``tool_activities`` 是纯函数，便于单元测试。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import streamlit as st
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

ChatRole = Literal["user", "assistant"]

#: 展示用的截断长度（完整内容在工具结果里，页面只做预览）。
ARGUMENT_PREVIEW_CHARS = 200
RESULT_PREVIEW_CHARS = 2000

#: 侧栏重命名输入框的长度上限（与会话索引的校验保持一致）。
SIDEBAR_TITLE_MAX_CHARS = 60

_STATUS_LABELS: Mapping[str, str] = {"ok": "成功", "denied": "被拒绝", "error": "失败"}


@dataclass(frozen=True)
class ChatMessage:
    """一条待展示的聊天消息。"""

    role: ChatRole
    content: str


@dataclass(frozen=True)
class ToolActivity:
    """一次工具调用及其结果预览。"""

    tool_call_id: str
    tool_name: str
    arguments: str
    result_preview: str | None = None


def to_chat_messages(messages: Sequence[AnyMessage]) -> list[ChatMessage]:
    """把 LangChain 消息映射为可展示的文本消息。

    只展示有文本的用户与 AI 消息；工具调用与工具结果由 :func:`tool_activities` 处理。
    """
    rendered: list[ChatMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            role: ChatRole = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        else:
            continue
        text = message.text.strip()
        if text:
            rendered.append(ChatMessage(role=role, content=text))
    return rendered


def tool_activities(messages: Sequence[AnyMessage]) -> list[ToolActivity]:
    """按出现顺序提取工具调用，并把对应的工具结果挂到同一条活动上。

    配对按"同一 ``tool_call_id`` 的第几次出现"进行：跨轮复用同一个调用 ID 时，
    第 N 次调用配第 N 条结果，不会互相顶掉。
    """
    return [activity for _, activity in paired_tool_activities(messages)]


def paired_tool_activities(
    messages: Sequence[AnyMessage],
) -> list[tuple[int, ToolActivity]]:
    """把工具调用与结果配对，返回 ``(消息下标, 活动)`` 列表（按消息顺序）。

    结果通过"最早的未配对同 ID 调用"配对，因此同一 ID 的多次调用各拿各的结果。
    """
    paired: list[tuple[int, ToolActivity]] = []
    position_of: dict[int, int] = {}
    waiting: dict[str, list[ToolActivity]] = {}

    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                activity = ToolActivity(
                    tool_call_id=str(call.get("id") or ""),
                    tool_name=call["name"],
                    arguments=json.dumps(call.get("args") or {}, ensure_ascii=False)[
                        :ARGUMENT_PREVIEW_CHARS
                    ],
                )
                waiting.setdefault(activity.tool_call_id, []).append(activity)
                position_of[id(activity)] = len(paired)
                paired.append((index, activity))
        elif isinstance(message, ToolMessage):
            queue = waiting.get(str(message.tool_call_id or ""))
            if not queue:
                continue
            activity = queue.pop(0)
            place = position_of[id(activity)]
            message_index, previous = paired[place]
            paired[place] = (
                message_index,
                ToolActivity(
                    tool_call_id=previous.tool_call_id,
                    tool_name=previous.tool_name,
                    arguments=previous.arguments,
                    result_preview=message.text.strip()[:RESULT_PREVIEW_CHARS],
                ),
            )

    return paired


def render_message(message: ChatMessage) -> None:
    """渲染单条文本消息。"""
    with st.chat_message(message.role):
        st.markdown(message.content)


def render_messages(messages: Sequence[ChatMessage]) -> None:
    """按顺序渲染文本消息。"""
    for message in messages:
        render_message(message)


def render_tool_activity(activity: ToolActivity, *, status: str | None = None) -> None:
    """渲染一次工具调用：参数、结果预览；编辑类结果直接展示 diff（架构 §6.3）。"""
    label = f"工具调用：{activity.tool_name}"
    if status:
        label = f"{label}（{_STATUS_LABELS.get(status, status)}）"
    with st.expander(label, expanded=False):
        st.code(activity.arguments, language="json")
        if activity.result_preview is None:
            return
        payload = _parse_payload(activity.result_preview)
        diff = payload.get("diff") if payload else None
        if isinstance(diff, str) and diff.strip():
            st.code(diff, language="diff")
            summary = _edit_summary(payload)
        else:
            st.code(activity.result_preview, language="json")
            summary = _shell_summary(payload)
        if summary:
            st.caption(summary)


def _parse_payload(preview: str) -> Mapping[str, Any] | None:
    """尝试把工具返回预览解析成 JSON；解析失败（例如预览被截断）时返回 ``None``。"""
    try:
        parsed = json.loads(preview)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _shell_summary(payload: Mapping[str, Any] | None) -> str:
    """Shell 结果的摘要：退出码、耗时与超时状态（架构 §6.6 要求展示退出码）。"""
    if not payload or "exit_code" not in payload:
        return ""
    parts: list[str] = []
    exit_code = payload.get("exit_code")
    parts.append(f"退出码 {exit_code}" if exit_code is not None else "退出码不可用（命令被终止）")
    if payload.get("timed_out"):
        parts.append("超时")
    duration = payload.get("duration_ms")
    if isinstance(duration, int):
        parts.append(f"{duration} ms")
    return "；".join(parts)


def _edit_summary(payload: Mapping[str, Any] | None) -> str:
    """编辑类结果的摘要：改动行数与前后 Hash（便于肉眼确认修改真的落盘）。"""
    if not payload:
        return ""
    parts: list[str] = []
    changed = payload.get("changed_lines")
    if isinstance(changed, int):
        parts.append(f"改动 {changed} 行")
    old_hash = str(payload.get("old_sha256") or "")
    new_hash = str(payload.get("new_sha256") or "")
    if old_hash and new_hash:
        parts.append(f"hash {old_hash[:8]} → {new_hash[:8]}")
    return "；".join(parts)


def render_history(
    messages: Sequence[AnyMessage],
    *,
    start_index: int = 0,
    tool_results: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    """按顺序渲染消息历史：文本消息照常展示，工具调用折叠展示（含结果与状态）。

    Args:
        messages: **完整**消息序列。配对与状态都基于整段序列计算——只把"这次要画的
            几条"传进来会算不出每个调用属于哪一轮，页面增量渲染请用 ``start_index``。
        start_index: 从该下标开始画（前面的消息已经画过了）。
        tool_results: State 的 ``tool_results`` 记录，按出现顺序给每次调用标状态。

    配对按"同一 ``tool_call_id`` 的第几次出现"计算：跨轮复用同一个调用 ID 时，
    每次调用都拿到自己的结果与状态，不会串到别的轮次（架构 §4.2）。
    """
    sequences = _status_sequences(tool_results or [])
    consumed: dict[str, int] = {}
    by_message: dict[int, list[tuple[ToolActivity, str | None]]] = {}
    for message_index, activity in paired_tool_activities(messages):
        occurrence = consumed.get(activity.tool_call_id, 0)
        consumed[activity.tool_call_id] = occurrence + 1
        ordered = sequences.get(activity.tool_call_id, ())
        status = ordered[occurrence] if occurrence < len(ordered) else None
        by_message.setdefault(message_index, []).append((activity, status))

    for index, message in enumerate(messages):
        if index < start_index:
            continue
        for activity, status in by_message.get(index, []):
            render_tool_activity(activity, status=status)
        for chat_message in to_chat_messages([message]):
            render_message(chat_message)


def _status_sequences(
    tool_results: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """``tool_call_id`` → 按记录顺序排列的状态列表。"""
    sequences: dict[str, list[str]] = {}
    for record in tool_results:
        call_id = str(record.get("tool_call_id") or "")
        sequences.setdefault(call_id, []).append(str(record.get("status") or ""))
    return sequences


def config_error_text(reason: str, hint: str | None = None) -> str:
    """把配置检查结果拼成面向用户的提示文本，不涉及模型调用。"""
    text = f"模型尚未配置：{reason}"
    if hint:
        text = f"{text}\n\n{hint}"
    return text


def render_model_status(*, configured: bool, text: str) -> None:
    """在页面上展示模型配置状态。"""
    if configured:
        st.success(text)
    else:
        st.error(text)


# --------------------------------------------------------------------------- 会话侧栏


def render_workspace_picker(
    *,
    workspaces: Sequence[str],
    current_workspace: str | None,
    key_prefix: str = "session",
) -> SessionAction | None:
    """工作区选择器：已使用工作区列表 + 绝对路径输入（架构 §1.5、§10.6）。

    列表用 ``selectbox`` 而不是单选控件：会话列表仍占着页面上的单选控件，两个同级单选会让
    用户分不清哪个是"切会话"、哪个是"切工作区"。

    关闭短期记忆时没有持久会话索引，页面仍然渲染它（只传 ``workspaces=()``），选择结果只写在
    ``st.session_state`` 里，重启回到配置的默认工作区。

    自己负责进侧栏：它既被 :func:`render_session_sidebar` 调用（那里已经在 ``st.sidebar`` 里，
    嵌套无副作用），也被页面在"关闭短期记忆"分支单独调用——那时没有外层侧栏上下文，不自己进
    侧栏就会把控件渲染到主区域。
    """
    with st.sidebar:
        st.subheader("工作区")
        if current_workspace:
            st.caption(f"当前工作区：{current_workspace}")

        known = list(workspaces)
        if known:
            selected = st.selectbox(
                "已使用的工作区（切换即恢复该工作区的最近会话）",
                options=known,
                index=known.index(current_workspace) if current_workspace in known else 0,
                key=f"{key_prefix}-workspace-pick-{current_workspace or 'none'}-{len(known)}",
            )
            if selected and selected != current_workspace:
                return SessionAction(kind="select_workspace", workspace_path=str(selected))

        typed = st.text_input(
            "工作区绝对路径",
            placeholder="例如 E:/projects/demo",
            help="目录不存在或不可读时只提示，不会改变当前会话。",
            key=f"{key_prefix}-workspace-path",
        )
        if st.button("切换到此工作区", key=f"{key_prefix}-workspace-apply"):
            return SessionAction(kind="select_workspace", workspace_path=typed)
    return None


@dataclass(frozen=True)
class SessionOption:
    """侧栏里的一条会话（展示数据由页面层算好，本模块不做业务判断）。"""

    session_id: str
    thread_id: str
    title: str
    detail: str
    is_active: bool


SessionActionKind = Literal["create", "switch", "rename", "select_workspace"]


@dataclass(frozen=True)
class SessionAction:
    """用户在侧栏做出的选择；落到会话索引的动作由页面层执行。"""

    kind: SessionActionKind
    session_id: str | None = None
    title: str | None = None
    #: ``select_workspace`` 的目标目录（用户输入的原始文本或列表里选中的路径）。
    workspace_path: str | None = None


def render_session_sidebar(
    options: Sequence[SessionOption],
    *,
    key_prefix: str = "session",
    workspaces: Sequence[str] = (),
    current_workspace: str | None = None,
) -> SessionAction | None:
    """渲染侧栏：**工作区选择器 + 当前工作区的会话列表**（架构 §1.5、§10.6）。

    Args:
        options: **当前工作区**的会话列表（含当前会话）。
        key_prefix: 控件 key 前缀，避免同一页面内多个控件冲突。
        workspaces: 已使用过的工作区（去重，最近在前）。
        current_workspace: 当前会话绑定的工作区（规范化路径）。

    Returns:
        用户本次做出的选择；没有操作时返回 ``None``。

    两级展示：工作区决定会话列表的展示范围，所以工作区选择器放在上面，且一旦有选择就立即
    返回（页面层切换成功后会重跑脚本，再渲染新工作区的会话列表）。

    控件 key 里带上当前会话与选项数量：新建或切换后 key 变化，单选控件会按新的
    当前会话重新初始化，不会残留失效的旧值。
    """
    with st.sidebar:
        workspace_action = render_workspace_picker(
            workspaces=workspaces, current_workspace=current_workspace, key_prefix=key_prefix
        )
        if workspace_action is not None:
            return workspace_action

        st.subheader("会话")
        if st.button("新建会话", key=f"{key_prefix}-new"):
            return SessionAction(kind="create")
        if not options:
            st.caption("还没有可用会话（未启用短期记忆时不会保存会话）")
            return None

        identifiers = [option.session_id for option in options]
        labels = {
            option.session_id: f"{option.title}｜{option.detail}" for option in options
        }
        active = next((option for option in options if option.is_active), options[0])
        index = identifiers.index(active.session_id)
        picked = st.radio(
            "选择会话（切换即恢复该会话的历史与挂起状态）",
            options=identifiers,
            index=index,
            format_func=labels.__getitem__,
            key=f"{key_prefix}-pick-{active.session_id}-{len(options)}",
        )
        st.caption(f"会话 ID：{active.session_id}；thread_id：{active.thread_id}")

        new_title = st.text_input(
            "重命名当前会话",
            value=active.title,
            max_chars=SIDEBAR_TITLE_MAX_CHARS,
            key=f"{key_prefix}-title-{active.session_id}",
        )
        if st.button("保存名称", key=f"{key_prefix}-rename"):
            if new_title.strip() == active.title:
                st.caption("名称没有变化")
            else:
                return SessionAction(kind="rename", session_id=active.session_id, title=new_title)

        if picked != active.session_id:
            return SessionAction(kind="switch", session_id=picked)
    return None

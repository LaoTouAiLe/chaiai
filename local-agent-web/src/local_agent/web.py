"""Streamlit 网页入口（开发文档 T03、T11）。

范围：``.env`` + 配置 → 模型 → 会话索引与 Checkpoint → 闲聊分支与工具分支。

- 会话事实来源是数据库，不是浏览器：``sessions`` 表决定当前会话及其 ``thread_id``，
  LangGraph Checkpoint 按该 ``thread_id`` 恢复历史（架构 §1.4、§10.1、§10.6）；
  ``st.session_state`` 只保存页面临时状态。
- 调用方每次只提交本轮 ``user_input``；历史消息由 State 维护（T03 起）。
- 工具调用由模型提出、安全门放行、执行后进入反思（T05）；写/改/删与 Shell 在 T06/T07。
- 待审批的会话只能经 ``Command(resume=...)`` 恢复，不能用普通用户输入覆盖挂起状态。

交互约定（架构 §1.4）：一次用户提交只处理一次；页面普通重绘只读取状态，
不重复调用模型或工具。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import streamlit as st
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.types import Command

from local_agent import config as config_module
from local_agent.config import AppConfig, ConfigError
from local_agent.exceptions import SessionStoreError
from local_agent.graph import builder, nodes
from local_agent.graph.state import AgentState
from local_agent.llm import factory
from local_agent.llm.models import ModelCapabilities, probe_capabilities
from local_agent.persistence import checkpoint as checkpoint_module
from local_agent.persistence.session_store import Session, SessionStore
from local_agent.ui import approval_ui
from local_agent.ui.renderer import (
    ChatMessage,
    SessionAction,
    SessionOption,
    config_error_text,
    render_history,
    render_message,
    render_model_status,
    render_session_sidebar,
)

DEFAULT_CONFIG_PATH = config_module.DEFAULT_CONFIG_PATH
DEFAULT_ENV_PATH = config_module.DEFAULT_ENV_PATH
DEFAULT_THREAD_ID = checkpoint_module.DEFAULT_THREAD_ID
SESSION_HISTORY_KEY = "history"


@dataclass(frozen=True)
class ChatApp:
    """页面上复用的配置、模型、图与会话索引；每次提交时新建 RuntimeContext。"""

    app_config: AppConfig
    model: BaseChatModel
    graph: builder.ChatGraph
    model_name: str
    workspace_root: Path
    secret_values: tuple[str, ...]  # 仅用于错误消息脱敏，不参与任何展示
    checkpointer_enabled: bool
    capabilities: ModelCapabilities
    #: 会话索引；未启用短期记忆时为 ``None``（此时不保存会话）。
    sessions: SessionStore | None = None


@st.cache_resource(show_spinner=False)
def load_chat_app(
    config_path: str,
    env_path: str,
    config_mtime: float,
    env_mtime: float,
) -> ChatApp:
    """加载 ``.env`` 与配置、构造模型、Checkpoint 和图。

    两个路径必须传**绝对路径**，且修改时间参与缓存键：这样不同工作目录不会互相命中，
    改动配置或 ``.env`` 后会自动重建，而不是继续用旧客户端。

    注意：Streamlit 会忽略下划线开头的参数，因此缓存键参数不能用 ``_`` 前缀，
    否则每次重绘都会误命中同一个已构建的客户端。``config_mtime`` 与 ``env_mtime``
    只参与缓存键，不在函数体内使用。

    Raises:
        ConfigError: 配置缺失、校验失败或环境变量中没有 API Key。
    """
    config_module.load_dotenv_file(Path(env_path))
    app_config = config_module.load_config(Path(config_path))
    api_key = config_module.resolve_api_key(app_config.llm)
    model = factory.build_chat_model(app_config.llm, api_key)
    workspace_root = Path(app_config.workspace.root).resolve()

    checkpointer: Any = None
    sessions: SessionStore | None = None
    if app_config.memory.short_term.enabled:
        # Checkpoint 与会话索引放在同一个库里、共用连接（架构 §10.6）
        database = checkpoint_module.open_database(
            Path(app_config.memory.short_term.path), workspace_root=workspace_root
        )
        checkpointer = database.checkpointer
        sessions = database.sessions

    return ChatApp(
        app_config=app_config,
        model=model,
        graph=builder.build_chat_graph(checkpointer),
        model_name=app_config.llm.model,
        workspace_root=workspace_root,
        secret_values=(api_key,),
        checkpointer_enabled=checkpointer is not None,
        # 架构 §11.7：启动时探测模型能力
        capabilities=probe_capabilities(model, provider_name=app_config.llm.model),
        sessions=sessions,
    )


def run_chat_turn(
    app: ChatApp,
    *,
    thread_id: str,
    user_text: str,
    session_id: str | None = None,
    prior_messages: list[AnyMessage] | None = None,
) -> dict[str, Any]:
    """调用一次图，返回本轮结束后的状态值。

    Args:
        app: 已构建好的图与模型。
        thread_id: Checkpoint 恢复键；开启 Checkpoint 时用于恢复历史。
        user_text: 用户本次输入。
        session_id: 面向用户的会话标识；缺省与 ``thread_id`` 相同（架构 §10.6）。
        prior_messages: 未开启 Checkpoint 时由页面补上的历史消息。

    Returns:
        状态值字典（``messages``、``tool_results``、``final_response`` 等）。
    """
    context = builder.build_runtime_context(
        app_config=app.app_config,
        model_adapter=app.model,
        workspace_root=app.workspace_root,
        current_cwd=Path.cwd(),
        model_capabilities=app.capabilities,
        request_id=uuid4().hex,
    )
    payload: AgentState = {
        "user_input": user_text,
        "thread_id": thread_id,
        "session_id": session_id or thread_id,
    }
    if not app.checkpointer_enabled:
        # 没有 Checkpoint 时状态不跨轮保留，由页面把本会话已知消息一并提交。
        payload["messages"] = list(prior_messages or [])

    result = app.graph.invoke(
        payload,
        config={"configurable": {"thread_id": thread_id}},
        context=context,
    )
    return dict(result)


def thread_state(app: ChatApp, thread_id: str) -> dict[str, Any]:
    """读取当前会话的状态值：开启 Checkpoint 时以图为事实来源，否则退回页面临时历史。"""
    if app.checkpointer_enabled:
        return builder.read_thread_state(app.graph, thread_id)
    return {"messages": _session_history(), "tool_results": []}


def thread_interrupt(app: ChatApp, thread_id: str) -> dict[str, Any] | None:
    """读取挂起中的人工审批请求（架构 §5.3）；没有则返回 ``None``。"""
    if not app.checkpointer_enabled:
        return None
    snapshot = app.graph.get_state({"configurable": {"thread_id": thread_id}})
    for task in snapshot.tasks:
        for item in task.interrupts:
            value = item.value
            if isinstance(value, Mapping):
                return dict(value)
    return None


def resume_turn(
    app: ChatApp, *, thread_id: str, resume_value: Mapping[str, Any]
) -> dict[str, Any]:
    """按人工决策恢复图（``Command(resume=...)``）。

    只在存在挂起审批、且用户明确点击按钮时调用：按钮双击不会重复执行，
    因为恢复之后图不再处于挂起状态。
    """
    context = builder.build_runtime_context(
        app_config=app.app_config,
        model_adapter=app.model,
        workspace_root=app.workspace_root,
        current_cwd=Path.cwd(),
        model_capabilities=app.capabilities,
        request_id=uuid4().hex,
    )
    result = app.graph.invoke(
        Command(resume=dict(resume_value)),
        config={"configurable": {"thread_id": thread_id}},
        context=context,
    )
    return dict(result)


def current_session(app: ChatApp | None) -> Session | None:
    """取当前会话；未启用短期记忆或配置不可用时返回 ``None``。

    首次运行时在这里创建会话：如果库里已经有 v0.1a 固定 ``thread_id`` 的 Checkpoint，
    第一个会话就沿用它，旧历史不会因为升级而失联。
    """
    if app is None or app.sessions is None:
        return None
    return app.sessions.ensure_active(legacy_thread_id=DEFAULT_THREAD_ID)


def thread_checkpoint_id(app: ChatApp, thread_id: str) -> str | None:
    """读取该会话当前的 Checkpoint 编号；没有 Checkpoint 时返回 ``None``。"""
    if not app.checkpointer_enabled:
        return None
    snapshot = app.graph.get_state({"configurable": {"thread_id": thread_id}})
    config = snapshot.config or {}
    value = config.get("configurable", {}).get("checkpoint_id")
    return str(value) if value else None


def session_options(store: SessionStore, active: Session) -> list[SessionOption]:
    """把会话记录映射成侧栏展示数据（时间按本机时区显示）。"""
    return [
        SessionOption(
            session_id=record.session_id,
            thread_id=record.thread_id,
            title=record.title,
            detail=f"最后活动 {_display_time(record.updated_at)}",
            is_active=record.session_id == active.session_id,
        )
        for record in store.list_sessions()
    ]


def apply_session_action(store: SessionStore, action: SessionAction) -> None:
    """把侧栏选择落到会话索引；成功后重跑脚本，让页面显示新会话。

    会话索引写入失败（例如名字为空）时只提示，不改动当前会话。
    """
    try:
        if action.kind == "create":
            store.create()
        elif action.kind == "switch" and action.session_id:
            store.switch(action.session_id)
        elif action.kind == "rename" and action.session_id:
            store.rename(action.session_id, action.title or "")
    except SessionStoreError as exc:
        st.error(f"会话操作失败：{exc}")
        return
    st.rerun()


def record_session_activity(
    app: ChatApp, session: Session, *, title_hint: str | None = None
) -> None:
    """一轮结束后更新会话索引：最后活动时间、Checkpoint 指针与首次自动命名。"""
    if app.sessions is None:
        return
    app.sessions.touch(
        session.session_id,
        last_checkpoint_id=thread_checkpoint_id(app, session.thread_id),
        title_hint=title_hint,
    )


def _display_time(iso_text: str) -> str:
    """把库里的 UTC ISO 时间转成本机时区的短格式；解析失败时原样返回。"""
    try:
        return datetime.fromisoformat(iso_text).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return iso_text


def tool_results_of(state: Mapping[str, Any]) -> list[Any]:
    """取状态里的工具结果记录；渲染层按出现顺序把它们配对到工具调用上。"""
    records = state.get("tool_results") or []
    return [record for record in records if isinstance(record, Mapping)]


def _file_mtime(path: Path) -> float:
    return path.stat().st_mtime if path.is_file() else 0.0


def _load_app() -> tuple[ChatApp | None, str | None]:
    """返回（可用应用, 配置错误说明），两者必有其一。"""
    config_path = DEFAULT_CONFIG_PATH.resolve()
    env_path = DEFAULT_ENV_PATH.resolve()
    try:
        app = load_chat_app(
            config_path.as_posix(),
            env_path.as_posix(),
            _file_mtime(config_path),
            _file_mtime(env_path),
        )
    except ConfigError as exc:
        return None, str(exc)
    return app, None


def _session_history() -> list[AnyMessage]:
    """页面临时消息列表；仅在配置不可用或未启用短期记忆时使用。"""
    if SESSION_HISTORY_KEY not in st.session_state:
        st.session_state[SESSION_HISTORY_KEY] = []
    history: list[AnyMessage] = st.session_state[SESSION_HISTORY_KEY]
    return history


def main() -> None:
    """渲染页面并处理一次用户提交。"""
    st.set_page_config(page_title="Local Agent Web")
    st.title("Local Agent Web")
    st.caption(
        "本机学习型 Agent（v0.1b / T11）。服务只监听 127.0.0.1；"
        "会话索引与消息都保存在本机 SQLite，刷新与重启后按当前会话恢复。"
    )

    app, config_reason = _load_app()
    if app is None:
        render_model_status(configured=False, text=config_error_text(config_reason or "未知原因"))
    else:
        storage = "SQLite Checkpoint" if app.checkpointer_enabled else "页面临时状态（未启用短期记忆）"
        tool_state = "支持" if app.capabilities.tool_calling else "不支持（工具模式会失败闭合）"
        render_model_status(
            configured=True,
            text=(
                f"模型已就绪：{app.model_name}；工具调用：{tool_state}；会话存储：{storage}"
            ),
        )

    # 会话侧栏：先处理用户选择，再按当前会话的 thread_id 读取状态。
    session = current_session(app)
    store = app.sessions if app is not None else None
    if store is not None and session is not None:
        action = render_session_sidebar(session_options(store, session))
        if action is not None:
            apply_session_action(store, action)
            return
    thread_id = session.thread_id if session is not None else DEFAULT_THREAD_ID

    state: dict[str, Any] = (
        thread_state(app, thread_id)
        if app is not None
        else {"messages": _session_history()}
    )
    history: list[AnyMessage] = list(state.get("messages") or [])
    render_history(history, tool_results=tool_results_of(state))

    unconfirmed = (
        nodes.unconfirmed_calls(cast(AgentState, state)) if app is not None else []
    )
    if unconfirmed:
        # 架构 §4.2：未获 Checkpoint 确认的副作用视为不确定，不自动重放
        st.warning(
            "上次运行没有正常结束："
            f"{'、'.join(unconfirmed)} 已放行但没有确认结果，可能留下了未完成的改动。"
            "为避免重复执行，我不会自动重放这些调用；请先核对文件内容或命令影响，再继续对话。"
        )

    if app is not None:
        pending_approval = thread_interrupt(app, thread_id)
        if pending_approval is not None:
            st.info(
                "有一个动作正在等待你确认。页面刷新不会自动执行，也不会沿用它之前的确认结果。"
            )
            decision = approval_ui.render_approval(pending_approval)
            if decision is not None:
                try:
                    result = resume_turn(
                        app,
                        thread_id=thread_id,
                        resume_value=decision.as_payload(),
                    )
                except Exception as exc:  # noqa: BLE001 — 页面边界：恢复失败也要给提示
                    detail = config_module.redact_secrets(str(exc), app.secret_values)
                    st.error(f"恢复审批失败：{detail}")
                    return
                messages = list(result.get("messages") or [])
                render_history(
                    messages,
                    start_index=len(history),
                    tool_results=tool_results_of(result),
                )
                if session is not None:
                    record_session_activity(app, session)
                if result.get("final_response") is None and not result.get("__interrupt__"):
                    st.error("模型没有返回文本内容，请重试或换个说法")
                # 重新执行脚本，让下一个待确认阶段（例如递归删除的二次确认）立即出现
                st.rerun()
            return

    prompt = st.chat_input("输入消息")
    if prompt is None:
        return

    user_text = prompt.strip()
    if not user_text:
        st.warning("输入为空，未提交任何内容")
        return

    if app is None:
        # 配置不可用时仍然回答，但只回答配置问题，不调用模型。
        reply = config_error_text(config_reason or "未知原因")
        _session_history().extend([HumanMessage(content=user_text), AIMessage(content=reply)])
        render_message(ChatMessage(role="user", content=user_text))
        render_message(ChatMessage(role="assistant", content=reply))
        return

    previous_count = len(history)
    try:
        result = run_chat_turn(
            app,
            thread_id=thread_id,
            session_id=session.session_id if session is not None else None,
            user_text=user_text,
            prior_messages=history,
        )
    except Exception as exc:  # noqa: BLE001 — 页面边界要把任何调用失败变成提示，而不是 500
        render_message(ChatMessage(role="user", content=user_text))
        detail = config_module.redact_secrets(str(exc), app.secret_values)
        st.error(
            f"模型调用失败：{detail}"
            "（配置检查已通过，问题可能出在网络、额度、Key 有效性或模型名）"
        )
        return

    messages = list(result.get("messages") or [])
    if not app.checkpointer_enabled:
        _session_history()[:] = messages
    if session is not None:
        # 更新最后活动时间与 Checkpoint 指针；会话还是默认名时用首条消息命名
        record_session_activity(app, session, title_hint=user_text)
    # 只渲染本次新增的部分；历史已在上方渲染，普通重绘不会重复提交。
    render_history(
        messages, start_index=previous_count, tool_results=tool_results_of(result)
    )

    if result.get("__interrupt__"):
        # 本轮停在人工审批：审批卡片渲染在页面顶部，这里必须重跑脚本才会出现
        st.rerun()

    if result.get("final_response") is None:
        st.error("模型没有返回文本内容，请重试或更换模型")


if __name__ == "__main__":
    main()

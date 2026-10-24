"""Streamlit 调试入口（v0.4 T33）。

范围：只经**应用服务**（T28）读 DTO / 事件并渲染。本入口不再直接解释 Graph 的 State、
不导入 LangChain 消息类，也不自己拼 ``Command``——那是旧入口（T11 之前）的做法。

- 装配与 HTTP 入口共用 :func:`local_agent.api.container.open_runtime`：同一份配置、同一个聊天库、
  同一个模型连接设置库（T29A 的共同设置服务），因此两个 UI 对同一事实不会各算一套。
- **单实例锁**（架构 §15.1）：v0.4 固定单进程单 worker；HTTP 服务正在运行时本入口会明确报错，
  而不是两个进程同时写同一个 SQLite 库。
- 会话的事实来源是数据库：页面只保存"当前打开哪个会话"这一项页面临时状态，其余全部来自
  ``service.get_session_snapshot``。刷新/重启按快照恢复，挂起的审批本来就在 Checkpoint 里，
  不需要额外机制。
- 提交与审批决定都带 ``client_request_id``：失败后重试复用同一个键（幂等），成功即换新键；
  同样的内容再次发送因此是**新的一轮**，不是重放。

页面只做展示与转发：安全判定、审批、幂等、每会话单飞都在服务里。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st

from local_agent import config as config_module
from local_agent.api.container import (
    ApiRuntime,
    open_runtime,
    resolve_memory_paths,
    resolve_skill_paths,
    resolve_under,
)
from local_agent.application.errors import ApplicationError
from local_agent.application.models import SessionSnapshot, SessionSummaryView
from local_agent.config import AppConfig, ConfigError
from local_agent.exceptions import MemoryStoreError, SessionStoreError
from local_agent.llm import embedding as embedding_module
from local_agent.memory import long_term, medium_term
from local_agent.memory.forgetting import is_recallable_fact
from local_agent.scope import project_id_for, visible_scope_keys
from local_agent.ui import approval_ui
from local_agent.ui.renderer import (
    SessionAction,
    SessionOption,
    config_error_text,
    render_conversation,
    render_memory_status,
    render_model_status,
    render_session_sidebar,
    render_status,
)
from local_agent.workspaces import validate_workspace

DEFAULT_CONFIG_PATH = config_module.DEFAULT_CONFIG_PATH
DEFAULT_ENV_PATH = config_module.DEFAULT_ENV_PATH

#: 页面临时状态：当前打开哪个会话（应用服务按会话定位，没有全局"激活项"这回事）。
OPEN_SESSION_KEY = "open_session_id"
#: 幂等键台账：`scope → client_request_id`（成功即删除，见 :func:`_forget_request`）。
_REQUEST_IDS_KEY = "request_ids"


@dataclass(frozen=True)
class DebugApp:
    """页面复用的运行时：应用服务 + 已加载配置 + 配置相对路径解析出的工作区根。"""

    runtime: ApiRuntime
    app_config: AppConfig
    workspace_root: Path


@st.cache_resource(show_spinner=False)
def load_debug_app(
    config_path: str,
    env_path: str,
    config_mtime: float,
    env_mtime: float,
) -> DebugApp:
    """加载 ``.env`` 与配置，并按**与 HTTP 入口相同的装配**打开运行时。

    两个路径必须传**绝对路径**，且修改时间参与缓存键：不同工作目录不会互相命中，改动配置或
    ``.env`` 后自动重建（Streamlit 会忽略下划线开头的参数，所以缓存键参数不能加 ``_`` 前缀，
    ``config_mtime`` / ``env_mtime`` 只参与缓存键，不在函数体内使用）。

    Raises:
        ConfigError: 配置缺失、校验失败，或未启用短期记忆（会话与幂等台账都在那个库里）。
        RuntimeError: 已有另一个服务进程占着同一个库。
    """
    config_module.load_dotenv_file(Path(env_path))
    app_config = config_module.load_config(Path(config_path))
    base_dir = Path(config_path).resolve().parent
    runtime = open_runtime(app_config, base_dir=base_dir)
    return DebugApp(
        runtime=runtime,
        # 侧栏"记忆管理"自己开长期库，因此必须与服务用**同一套解析结果**（§8）：
        # 否则页面会去读另一个库，把"记忆是空的"显示成事实。
        # 两条路径解析同一口径（2026-10-16）：记忆库与技能发现根都钉到 base_dir。
        app_config=resolve_skill_paths(resolve_memory_paths(app_config, base_dir), base_dir),
        # 与 `open_runtime` 同一套相对路径规则（相对配置文件目录，而不是进程当前目录）。
        workspace_root=resolve_under(base_dir, app_config.workspace.root),
    )


def _file_mtime(path: Path) -> float:
    return path.stat().st_mtime if path.is_file() else 0.0


def _load_app() -> tuple[DebugApp | None, str | None]:
    """返回（可用页面运行时, 失败说明），两者必有其一。"""
    config_path = DEFAULT_CONFIG_PATH.resolve()
    env_path = DEFAULT_ENV_PATH.resolve()
    try:
        app = load_debug_app(
            config_path.as_posix(),
            env_path.as_posix(),
            _file_mtime(config_path),
            _file_mtime(env_path),
        )
    except (ConfigError, RuntimeError) as exc:
        return None, str(exc)
    return app, None


# --------------------------------------------------------------------------- 展示辅助


def _display_time(value: datetime) -> str:
    """把服务给的 UTC 时间转成本机时区的短格式。"""
    return value.astimezone().strftime("%m-%d %H:%M")


def session_options(
    sessions: Sequence[SessionSummaryView], *, open_session_id: str | None
) -> list[SessionOption]:
    """会话列表 → 侧栏展示数据（``is_active`` 表示页面**当前打开**的那条）。"""
    return [
        SessionOption(
            session_id=session.session_id,
            thread_id=session.thread_id,
            title=session.title,
            detail=f"最后活动 {_display_time(session.updated_at)}",
            is_active=session.session_id == open_session_id,
        )
        for session in sessions
    ]


def memory_status_lines(app_config: AppConfig) -> tuple[list[str], bool]:
    """侧栏要展示的记忆状态：（状态行, 是否有需要用户处理的问题）。

    三层各自成行：短期（Checkpoint）、中期（Chroma + embedding）、长期（SQLite）。
    Embedding **只做静态探测**（不加载模型、不联网），所以这一块不会让页面卡住。
    """
    memory = app_config.memory
    lines = [
        "短期：" + ("Checkpoint 已启用" if memory.short_term.enabled else "已关闭（重启即清空）")
    ]
    warn = False
    if not memory.medium_term.enabled:
        lines.append("中期：已关闭（不访问向量库、不加载模型）")
    else:
        embedding_config = app_config.embedding
        # 只有远端 provider 才读 Key：本地模型不需要，读它反而会因为"Key 为空"报错。
        # 这里**必须用非抛出式读取**：缺 Key 时抛 `ConfigError` 会让整个侧栏报错，
        # 而不是展示一条可处理的警告。
        api_key = (
            config_module.optional_api_key(embedding_config)
            if embedding_config.provider == "openai_compatible"
            else None
        )
        state = embedding_module.readiness(embedding_config, api_key=api_key)
        lines.append(f"中期：已启用（Embedding {state.provider}/{state.model}）")
        lines.append(f"缓存目录：{state.cache_dir or '（远端接口，无本地缓存）'}")
        lines.append(f"状态：{state.detail}")
        if state.state in {"disabled", "unavailable", "not_ready"}:
            warn = True
        if not medium_term.chromadb_available():
            lines.append("chromadb 未安装：执行 `uv sync --extra memory` 后再启用中期记忆")
            warn = True
    lines.append(
        "长期："
        + (f"已启用（{memory.long_term.path}）" if memory.long_term.enabled else "已关闭")
    )
    lines.append("压缩：" + ("已启用" if memory.compression.enabled else "已关闭"))
    return lines, warn


def memory_management_lines(
    app_config: AppConfig, workspace_root: Path
) -> tuple[list[str], bool]:
    """侧栏"记忆管理"区块：（展示行, 是否有需要处理的问题）。

    只读且**只打开长期库**（SQLite，微秒级）：中期库要加载 embedding 模型，在页面上做这件事会让
    每次交互都变慢，因此中期内容统一走命令行查看。人工删除与遗忘任务也在命令行，页面只负责
    "看得见"。作用域用**配置解析出的工作区根**（页面拿不到会话的真实路径——`WorkspaceRef`
    刻意不含绝对路径，§15.8）。
    """
    memory = app_config.memory
    if not memory.long_term.enabled:
        return (["长期：已关闭（没有可管理的长期记忆）"], False)
    path = Path(memory.long_term.path)
    try:
        store = long_term.open_store(path)
    except (MemoryStoreError, sqlite3.Error, OSError) as exc:
        return ([f"长期库打不开：{type(exc).__name__}: {exc}"], True)
    try:
        workspace = Path(workspace_root)
        project = project_id_for(workspace)
        moment = datetime.now().astimezone()
        facts = [
            (entity, fact)
            for entity, fact in store.active_facts_with_entities(
                visible_scope_keys(user_id=None, project_id=project, workspace_root=workspace),
                now=moment,
            )
            # 与召回同一口径的可见性过滤：过期与超龄的事实不该算进"可管理的条数"。
            if is_recallable_fact(fact, config=memory.long_term, now=moment)
        ]
        conclusions = store.list_task_conclusions(project_scope=project) if project else []
    except (MemoryStoreError, sqlite3.Error, OSError) as exc:
        return ([f"长期库读取失败：{type(exc).__name__}: {exc}"], True)
    finally:
        store.close()
    lines = [
        f"长期：事实 {len(facts)} 条、任务结论 {len(conclusions)} 条（本项目 / 本用户 / 全局）"
    ]
    if memory.medium_term.enabled:
        lines.append("中期：内容与条数请用命令行查看（页面不加载 embedding 模型）")
    lines.append(
        "人工操作：python -m local_agent.cli memory list ｜ delete <id> --yes ｜ forget"
    )
    return (lines, False)


# --------------------------------------------------------------------------- 幂等键


def _request_id(scope: str) -> str:
    """同一操作（同一内容）的重试复用同一个键（§6.1）。"""
    ids: dict[str, str] = st.session_state.setdefault(_REQUEST_IDS_KEY, {})
    if scope not in ids:
        ids[scope] = uuid4().hex
    return ids[scope]


def _forget_request(scope: str) -> None:
    """操作成功后清掉这条键：同样的内容下次发送是**新的一轮**，不是重放。"""
    st.session_state.setdefault(_REQUEST_IDS_KEY, {}).pop(scope, None)


# --------------------------------------------------------------------------- 交互


def _report_error(exc: BaseException) -> None:
    """页面边界统一渲染失败：应用错误用它的可执行文案，其它异常如实给出类型。"""
    if isinstance(exc, ApplicationError):
        view = exc.view
        st.error(f"{view.title}\n\n{view.message}\n\n{view.hint}")
        st.caption(f"错误码 {view.code}；诊断编号 {view.diagnostic_id}")
        return
    st.error(f"操作失败：{type(exc).__name__}: {exc}")


def run_with_stream(
    service: Any,
    session_id: str,
    call: Callable[[], Any],
    *,
    placeholder: Any,
) -> Any:
    """跑一次命令用例，同时把 ``message_delta`` 逐字渲染到 ``placeholder``。

    为什么要在工作线程里跑：Streamlit 的 ``st.*`` 只能在脚本线程调用，而增量要在命令执行**期间**
    就画出来。于是分成两个线程——脚本线程只负责渲染，工作线程只负责调服务；两者之间只通过
    事件订阅的进程内队列传递数据（不共享 Streamlit 状态）。

    订阅的是**与 React 相同的那批事件**（§15.4），因此两个 UI 的实时输出来自同一投影。
    """
    subscription = service.subscribe_events(session_id)
    outcome: list[Any] = []
    live_text = ""

    def worker() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:  # noqa: BLE001 — 交给脚本线程重新抛出，页面边界统一处理
            outcome.append(exc)

    thread = threading.Thread(target=worker, daemon=True, name="streamlit-service-call")
    thread.start()
    try:
        while True:
            live_text = _paint_deltas(subscription, placeholder, live_text)
            if not thread.is_alive():
                break
            thread.join(timeout=0.05)
        # 线程结束后队列里可能还剩最后几条增量，画完再收尾。
        _paint_deltas(subscription, placeholder, live_text)
    finally:
        service.unsubscribe_events(subscription)
        # 占位符只做"直播"：结束后统一按快照渲染，不会重复显示。
        placeholder.empty()
    result = outcome[0] if outcome else None
    if isinstance(result, BaseException):
        raise result
    return result


def _paint_deltas(subscription: Any, placeholder: Any, live_text: str) -> str:
    """把队列里的增量事件拼进占位符，返回新的累计文本（其余事件由快照接管）。"""
    appended = False
    for event in subscription.poll():
        if event.event_type == "message_delta":
            live_text += str(event.payload.text_delta)
            appended = True
    if appended:
        placeholder.markdown(live_text)
    return live_text


def _run_command(
    service: Any, session_id: str, call: Callable[[], Any], *, placeholder: Any
) -> bool:
    """跑一次命令用例并处理结果：成功返回 ``True``，失败已渲染提示并返回 ``False``。"""
    try:
        run_with_stream(service, session_id, call, placeholder=placeholder)
    except ApplicationError as exc:
        _report_error(exc)
        return False
    except Exception as exc:  # noqa: BLE001 — 页面边界：任何调用失败都要变成提示，而不是 500
        st.error(f"模型调用失败：{type(exc).__name__}: {exc}")
        return False
    return True


def _submit(service: Any, session_id: str, text: str, *, placeholder: Any) -> None:
    scope = f"submit:{session_id}:{text}"
    request_id = _request_id(scope)
    if not _run_command(
        service,
        session_id,
        lambda: service.submit_message(session_id, text, request_id),
        placeholder=placeholder,
    ):
        return
    _forget_request(scope)
    st.rerun()


def _resolve(service: Any, session_id: str, snapshot: SessionSnapshot, *, placeholder: Any) -> None:
    approval = snapshot.approval
    if approval is None:
        return
    decision = approval_ui.render_approval(approval)
    if decision is None:
        return
    scope = (
        f"approval:{session_id}:{approval.approval_id}:{decision.decision}"
        f":{int(decision.confirm_recursive)}"
    )
    request_id = _request_id(scope)
    ok = _run_command(
        service,
        session_id,
        lambda: service.resolve_approval(
            session_id,
            approval.approval_id,
            decision.decision,
            request_id,
            reason=decision.reason,
            confirm_recursive=decision.confirm_recursive,
        ),
        placeholder=placeholder,
    )
    if not ok:
        return
    _forget_request(scope)
    st.rerun()


def _apply_session_action(service: Any, action: SessionAction) -> None:
    """把侧栏选择落到应用服务；成功后重跑脚本，让页面显示新会话。"""
    try:
        if action.kind == "create":
            summary = service.create_session()
        elif action.kind == "open" and action.session_id:
            st.session_state[OPEN_SESSION_KEY] = action.session_id
            st.rerun()
            return
        elif action.kind == "select_workspace" and action.workspace_id:
            summary = service.select_workspace_by_id(action.workspace_id)
        elif action.kind == "select_workspace":
            summary = service.select_workspace(validate_workspace(action.workspace_path or ""))
        else:  # pragma: no cover - 侧栏只会产出上面几种动作
            return
    except (ApplicationError, SessionStoreError) as exc:
        _report_error(exc)
        return
    st.session_state[OPEN_SESSION_KEY] = summary.session_id
    st.rerun()


def _open_session_id(sessions: Sequence[SessionSummaryView]) -> str | None:
    """当前打开的会话：页面记住的那条，失效时回落到最近活动的会话。"""
    stored = st.session_state.get(OPEN_SESSION_KEY)
    identifiers = {session.session_id for session in sessions}
    if isinstance(stored, str) and stored in identifiers:
        return stored
    if not sessions:
        return None
    active = next((session for session in sessions if session.is_active), sessions[0])
    return active.session_id


def main() -> None:
    """渲染页面并处理一次用户提交或审批决定。"""
    st.set_page_config(page_title="Local Agent Web（调试入口）")
    st.title("Local Agent Web")
    st.caption(
        "v0.4 / T33 调试入口：与 Web 应用共用同一应用服务、同一设置库与同一会话库；"
        "只监听本机，会话与消息都保存在本机 SQLite。"
    )

    debug_app, reason = _load_app()
    if debug_app is None:
        render_model_status(configured=False, text=config_error_text(reason or "未知原因"))
        return
    service = debug_app.runtime.service
    if service.model_configured():
        credential, _note = service.credential_store_state()
        render_model_status(
            configured=True,
            text=(
                f"模型已就绪：{service.current_model_name()}；"
                f"可用连接 {len(service.list_model_connections())} 条；凭据存储：{credential}"
            ),
        )
    else:
        render_model_status(
            configured=False,
            text=config_error_text(
                "还没有可用的模型连接",
                hint="在 Web 应用的设置页添加并测试一条连接后，这里即可直接对话。",
            ),
        )

    with st.sidebar:
        status_lines, status_warn = memory_status_lines(debug_app.app_config)
        render_memory_status(lines=status_lines, warn=status_warn)
        manage_lines, manage_warn = memory_management_lines(
            debug_app.app_config, debug_app.workspace_root
        )
        render_memory_status(lines=manage_lines, warn=manage_warn)

    try:
        sessions = service.list_sessions()
        workspaces = service.list_workspaces()
    except ApplicationError as exc:
        _report_error(exc)
        return

    open_id = _open_session_id(sessions)
    current = next((item for item in sessions if item.session_id == open_id), None)
    workspace_id = current.workspace.workspace_id if current is not None else None
    visible = (
        [item for item in sessions if item.workspace.workspace_id == workspace_id]
        if workspace_id is not None
        else []
    )
    action = render_session_sidebar(
        session_options(visible, open_session_id=open_id),
        workspaces=[(item.workspace_id, item.display_name) for item in workspaces],
        current_workspace_id=workspace_id,
        current_workspace_name=current.workspace.display_name if current is not None else None,
    )
    if action is not None:
        _apply_session_action(service, action)
        return

    if open_id is None:
        st.info("还没有会话：请在左侧「新建会话」，或选择/添加一个工作区开始。")
        return

    try:
        snapshot = service.get_session_snapshot(open_id)
    except ApplicationError as exc:
        _report_error(exc)
        st.session_state.pop(OPEN_SESSION_KEY, None)
        return

    render_status(snapshot)
    render_conversation(snapshot)

    approval = snapshot.approval
    if approval is not None and approval.state == "pending":
        st.info(
            "有一个动作正在等待你确认。页面刷新不会自动执行，也不会沿用它之前的确认结果。"
        )
        # 流式占位符建在按钮上方：审批通过后的增量回答出现在对话区底部。
        live = st.empty()
        _resolve(service, open_id, snapshot, placeholder=live)
        return

    # 流式占位符先建好再放输入框：增量出现在对话区底部，而不是输入框下面。
    # 无提交时它保持为空（不可见），不影响渲染。
    live = st.empty()
    prompt = st.chat_input("输入消息")
    if prompt is None:
        return
    user_text = prompt.strip()
    if not user_text:
        st.warning("输入为空，未提交任何内容")
        return
    _submit(service, open_id, user_text, placeholder=live)


__all__ = [
    "DebugApp",
    "load_debug_app",
    "main",
    "memory_management_lines",
    "memory_status_lines",
    "run_with_stream",
    "session_options",
]


if __name__ == "__main__":
    main()
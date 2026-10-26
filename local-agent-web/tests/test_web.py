"""T33 验收：Streamlit 调试入口只经应用服务（T28）读写，不再解释 State/Graph。

两条硬口径：

1. **页面层只认 DTO 与事件**：`web.py` / `ui/renderer.py` / `ui/approval_ui.py` 不导入 LangGraph、
   LangChain 消息类、Graph 节点或 ``Command``。于是 Graph 内部节点改名、State 字段变化都不会
   波及页面代码与这些测试（这正是 T33 要拿掉的那层耦合）。
2. **两个 UI 同一份事实**：同一 fixture 经 HTTP（React 的通道）、直接应用服务与 Streamlit 页面
   得到等价业务结果；两边读的是同一个模型设置库；刷新/重启按快照恢复；提交与审批都幂等。

自动测试不联网、不依赖真实密钥：模型是 `conftest.py` 的确定性假模型，装配走
:func:`local_agent.api.container.open_runtime`（与 HTTP 入口同一套）。
"""

from __future__ import annotations

import gc
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from local_agent import web
from local_agent.api.app import create_app
from local_agent.api.container import open_runtime, resolve_data_paths
from local_agent.application.errors import ErrorView
from local_agent.application.models import (
    AssistantMessageView,
    ErrorMessageView,
    FileDiffHunkView,
    FileDiffLineView,
    FileDiffView,
    MemoryMessageView,
    MemoryUseView,
    SessionSnapshot,
    SessionSummaryView,
    ToolEvidenceView,
    ToolMessageView,
    ToolOutcomeView,
    ToolUseView,
    UserMessageView,
    WorkspaceRef,
)
from local_agent.config import AppConfig, load_config
from local_agent.llm import embedding as embedding_module
from local_agent.llm import factory
from local_agent.model_settings.models import ConnectionDraft
from local_agent.persistence import checkpoint
from local_agent.ui import renderer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_REPLY = "你好，我是测试模型"

#: 页面用的完整配置：工作区与库都按**配置文件目录**的相对路径写，页面自己解析（与 HTTP 入口同规）。
#: 长期记忆与压缩关掉——它们会多出模型调用与磁盘库，与"页面是否走应用服务"这件事无关。
PAGE_CONFIG = """\
llm:
  base_url: "https://api.deepseek.com"
  api_key_env: "DEEPSEEK_API_KEY"
  model: "deepseek-flash"
  context_window_tokens: 65536
workspace:
  root: "."
memory:
  short_term:
    enabled: true
    path: "data/checkpoints.sqlite"
  long_term:
    enabled: false
  compression:
    enabled: false
"""


# --------------------------------------------------------------------------- 夹具与辅助


def _write_config(directory: Path, body: str, name: str = "config.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _run_page(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """在临时目录里真实运行页面脚本。

    一个目录只能跑一次：`open_runtime` 会占住单实例锁（§15.1），而页面进程按设计在退出前
    不释放它——这正是"第二个进程必须失败"的语义。要再跑一次就换一个目录。
    """
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    monkeypatch.chdir(directory)
    st.cache_resource.clear()
    gc.collect()
    page = AppTest.from_file(str(Path(web.__file__)), default_timeout=60)
    page.run()
    return page


def _rendered_text(page: Any) -> str:
    """页面上所有文本型元素的内容（含折叠区里的代码块）。"""
    parts = [element.value for element in page.markdown]
    parts += [element.value for element in page.code]
    return "\n".join(parts)


def _new_session(page: Any) -> Any:
    """点侧栏「新建会话」——页面不自建会话，会话一律由应用服务创建。"""
    return page.sidebar.button(key="session-new").click().run()


def _sessions(directory: Path) -> Any:
    """按页面的工作区重开会话索引，等价于重启服务后再读（只读，不占单实例锁）。

    路径**从配置解析**而不是自己拼：2026-10-26 起数据落在数据根下（默认平台标准目录），
    再拼 ``directory/data`` 就会打开另一个空库，把"重启后看不到会话"误报成缺陷。
    """
    config = resolve_data_paths(load_config(directory / "config.yaml"))
    database = checkpoint.open_database(
        Path(config.memory.short_term.path), workspace_root=directory.resolve()
    )
    return database.sessions


def _error(diagnostic_id: str, code: str, title: str) -> ErrorView:
    return ErrorView(
        code=code,
        category="system",
        title=title,
        message="这一步失败了",
        hint="稍后重新发送即可重试。",
        retryable=False,
        diagnostic_id=diagnostic_id,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _snapshot(*, messages: list[Any], errors: list[Any]) -> SessionSnapshot:
    return SessionSnapshot(
        snapshot_id="snap-1",
        session_id="s-1",
        workspace=WorkspaceRef(workspace_id="w-1", display_name="demo"),
        title="会话",
        session_state="idle",
        messages=messages,
        errors=errors,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _summary(session_id: str, *, title: str = "会话") -> SessionSummaryView:
    moment = datetime(2026, 3, 4, 6, 30, tzinfo=timezone.utc)
    return SessionSummaryView(
        session_id=session_id,
        thread_id=f"thread-{session_id}",
        title=title,
        workspace=WorkspaceRef(workspace_id="w-1", display_name="demo"),
        created_at=moment,
        updated_at=moment,
    )


def _tool(**overrides: Any) -> ToolUseView:
    fields: dict[str, Any] = {
        "tool_call_id": "call-1",
        "tool_name": "read_file",
        "status": "ok",
    }
    fields.update(overrides)
    return ToolUseView(**fields)


# --------------------------------------------------------------------------- 渲染层：只看 DTO


def test_to_chat_messages_takes_only_chat_text_from_dtos() -> None:
    """只有用户/助手两种消息进气泡；空回答、工具行、记忆行、错误行各走自己的渲染器。"""
    messages = [
        UserMessageView(message_id="m1", text=" 你好 "),
        AssistantMessageView(message_id="m2", text="回答"),
        AssistantMessageView(message_id="m3", text="   "),  # 空回答不渲染成空气泡
        ToolMessageView(message_id="m4", tool=_tool()),
        MemoryMessageView(
            message_id="m5",
            memory=MemoryUseView(retrieval_outcome="miss", retrieval_note="本轮没有召回相关记忆"),
        ),
        ErrorMessageView(message_id="m6", error=_error("d1", "TOOL_CALL_FAILED", "工具执行失败")),
    ]

    assert [(item.role, item.content) for item in renderer.to_chat_messages(messages)] == [
        ("user", "你好"),
        ("assistant", "回答"),
    ]


def test_visible_errors_skips_rows_already_shown_in_the_conversation() -> None:
    """同一件失败不能既作为消息行、又作为会话级错误再显示一次（与前端同一口径）。"""
    shown = _error("d1", "TOOL_CALL_FAILED", "工具执行失败")
    pending = _error("d2", "RUN_STAGE_FAILED", "本轮出现未分类的异常")
    snapshot = _snapshot(
        messages=[
            UserMessageView(message_id="m1", text="你好"),
            ErrorMessageView(message_id="m2", error=shown),
        ],
        errors=[shown, pending],
    )

    assert [item.diagnostic_id for item in renderer.visible_errors(snapshot)] == ["d2"]


def test_diff_text_renders_the_structured_hunks_as_given() -> None:
    """页面拼 diff 文本只是为了等宽显示：行号与类型都来自 DTO，不再解析一遍。"""
    diff = FileDiffView(
        path="note.py",
        additions=1,
        deletions=1,
        hunks=[
            FileDiffHunkView(
                header="@@ -1 +1 @@",
                lines=[
                    FileDiffLineView(kind="del", old_line=1, text="value = 1"),
                    FileDiffLineView(kind="add", new_line=1, text="value = 2"),
                ],
            )
        ],
    )

    assert renderer.diff_text(diff).splitlines() == [
        "--- note.py",
        "+++ note.py",
        "@@ -1 +1 @@",
        "-value = 1",
        "+value = 2",
    ]


def test_tool_row_describes_target_status_and_evidence() -> None:
    tool = _tool(
        tool_name="edit_file",
        redacted_arguments={"path": "note.py"},
        elapsed_ms=12,
        outcome=ToolOutcomeView(kind="edit", target="note.py", summary="替换 1 处", total=1),
        evidence=ToolEvidenceView(
            evidence_type="sha256", summary="文件哈希已核对", verified=True
        ),
    )

    assert renderer.tool_heading(tool) == "工具调用：edit_file（编辑）（成功）"
    summary = renderer.tool_summary(tool)
    assert "目标 note.py" in summary
    assert "替换 1 处" in summary
    assert "12 ms" in summary
    assert "已核对" in summary


def test_status_and_config_text_are_plain_language() -> None:
    snapshot = _snapshot(messages=[], errors=[])

    assert renderer.session_state_text(snapshot) == "空闲"
    assert renderer.config_error_text("未找到配置文件").startswith("模型尚未配置：")
    assert "请先创建配置" in renderer.config_error_text("未找到配置文件", "请先创建配置")


# --------------------------------------------------------------------------- 审批卡：只消费 ApprovalView


APPROVAL_SCRIPT = """
import json

import streamlit as st

from local_agent.application.models import ApprovalView
from local_agent.ui import approval_ui

approval = ApprovalView(**PAYLOAD)
decision = approval_ui.render_approval(approval)
st.write(
    "DECISION:"
    + json.dumps(
        None
        if decision is None
        else {
            "decision": decision.decision,
            "confirm_recursive": decision.confirm_recursive,
            "reason": decision.reason,
        },
        ensure_ascii=False,
    )
)
"""


def _approval_payload(*, risk: str = "normal", state: str = "pending") -> dict[str, Any]:
    return {
        "approval_id": "ap-1",
        "run_id": "run-1",
        "tool_call_id": "call-1",
        "tool_name": "delete_file",
        "action_fingerprint": "sha256:abc",
        "redacted_arguments": {"path": "aa.txt"},
        "target": "aa.txt",
        "impact_summary": "删除项目内文件 aa.txt",
        "risk": risk,
        "state": state,
        "created_at": "2026-01-01T00:00:00+00:00",
        "note": None,
    }


def _approval_page(payload: dict[str, Any]) -> Any:
    from streamlit.testing.v1 import AppTest

    page = AppTest.from_string(f"PAYLOAD = {payload!r}\n" + APPROVAL_SCRIPT, default_timeout=30).run()
    assert not page.exception, page.exception
    return page


def _decision_text(page: Any) -> str:
    return next(
        (
            element.value
            for element in page.markdown
            if str(element.value).startswith("DECISION:")
        ),
        "",
    )


def test_approval_card_offers_approve_and_reject_while_pending() -> None:
    page = _approval_page(_approval_payload())

    assert _decision_text(page) == "DECISION:null"
    assert [element.label for element in page.button] == ["允许一次", "拒绝"]

    page = page.button(key="approval-approve").click().run()

    assert '"decision": "approve"' in _decision_text(page)
    assert '"confirm_recursive": false' in _decision_text(page)


def test_approval_card_reject_reports_the_reason() -> None:
    page = _approval_page(_approval_payload())

    page = page.button(key="approval-reject").click().run()

    assert '"decision": "reject"' in _decision_text(page)
    assert "用户拒绝" in _decision_text(page)


def test_recursive_delete_needs_an_explicit_second_confirmation() -> None:
    """第一阶段只推进；第二阶段必须重新勾选并提交显式确认。"""
    first = _approval_page(_approval_payload(risk="high") | {"confirmation_stage": 1})
    assert [element.label for element in first.button] == ["进入二次确认", "拒绝"]
    assert len(first.checkbox) == 0
    first = first.button(key="approval-stage-1-approve").click().run()
    assert '"confirm_recursive": false' in _decision_text(first)

    second = _approval_page(_approval_payload(risk="high") | {"confirmation_stage": 2})
    assert [element.label for element in second.button] == ["确认删除", "拒绝"]
    assert second.button(key="approval-stage-2-approve").disabled is True
    second = second.checkbox(key="approval-ap-1-stage-2-recursive").set_value(True).run()
    second = second.button(key="approval-stage-2-approve").click().run()
    assert '"decision": "approve"' in _decision_text(second)
    assert '"confirm_recursive": true' in _decision_text(second)


def test_decided_approval_only_reports_the_state_without_buttons() -> None:
    page = _approval_page(_approval_payload(state="rejected"))

    assert not page.button
    assert any("已拒绝" in element.value for element in page.info)
    assert _decision_text(page) == "DECISION:null"


# --------------------------------------------------------------------------- web.py 的纯函数


@pytest.mark.skipif(
    not embedding_module.fastembed_available(),
    reason="需要可选依赖 memory（fastembed）：uv sync --extra memory",
)
def test_memory_status_lines_describe_each_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """侧栏状态块：三层各一行；默认开关与配置问题都必须如实展示（架构 §1.4）。"""
    monkeypatch.setattr(embedding_module, "_hf_constants", lambda: None)
    base = load_config(PROJECT_ROOT / "config.yaml.example")

    default_lines, default_warn = web.memory_status_lines(base)
    default_text = "\n".join(default_lines)
    assert "短期" in default_text
    assert "中期：已关闭" in default_text
    assert "长期：已启用" in default_text
    assert default_warn is False

    data = base.model_dump()
    data["memory"]["medium_term"].update(
        {"enabled": True, "path": str(tmp_path / "data" / "chroma")}
    )
    data["memory"]["long_term"].update(
        {"enabled": True, "path": str(tmp_path / "data" / "memories.sqlite")}
    )
    data["embedding"].update(
        {
            "enabled": True,
            "provider": "onnx_local",
            "model": "BAAI/bge-small-zh-v1.5",
            "cache_dir": str(tmp_path / "models"),
        }
    )
    config = AppConfig.model_validate(data)

    lines, warn = web.memory_status_lines(config)

    text = "\n".join(lines)
    assert "中期：已启用" in text
    assert "长期：已启用" in text
    assert "缓存目录" in text
    assert f"状态：{embedding_module.readiness(config.embedding).detail}" in text
    assert warn is False  # 合法配置：依赖已装、缓存目录是绝对路径

    # "中期已启用但 embedding 关闭"会被配置校验拦住；页面仍要能报出来（防御分支）
    broken = config.model_copy(
        update={"embedding": config.embedding.model_copy(update={"enabled": False})}
    )
    _, warn_broken = web.memory_status_lines(broken)
    assert warn_broken is True


def test_memory_status_lines_report_missing_api_key_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """远端 embedding 缺 Key：状态块展示可处理的警告，而不是整块抛 ``ConfigError``。"""
    base = load_config(PROJECT_ROOT / "config.yaml.example")
    data = base.model_dump()
    data["memory"]["medium_term"].update(
        {"enabled": True, "path": str(tmp_path / "data" / "chroma")}
    )
    data["embedding"] = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "https://example.invalid/v1",
        "model": "text-embedding-3-small",
        "api_key_env": "CHAT_EMBEDDING_KEY",
    }
    config = AppConfig.model_validate(data)
    monkeypatch.delenv("CHAT_EMBEDDING_KEY", raising=False)

    lines, warn = web.memory_status_lines(config)

    assert warn is True
    assert any("API Key" in line for line in lines)


def test_session_options_mark_the_currently_open_session() -> None:
    options = web.session_options([_summary("s-1"), _summary("s-2")], open_session_id="s-2")

    assert [item.session_id for item in options] == ["s-1", "s-2"]
    assert [item.is_active for item in options] == [False, True]
    assert all(item.detail.startswith("最后活动 ") for item in options)


class _FakeSubscription:
    """每次 `poll()` 只给一条事件，用来断言增量是**逐条**画上去的。"""

    def __init__(self, events: list[Any]) -> None:
        self._queue = list(events)

    def poll(self) -> list[Any]:
        return [self._queue.pop(0)] if self._queue else []


class _FakeStreamService:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.subscribed: list[str] = []
        self.unsubscribed: list[Any] = []

    def subscribe_events(self, session_id: str) -> _FakeSubscription:
        self.subscribed.append(session_id)
        return _FakeSubscription(self._events)

    def unsubscribe_events(self, subscription: Any) -> None:
        self.unsubscribed.append(subscription)


class _FakePlaceholder:
    def __init__(self) -> None:
        self.rendered: list[str] = []
        self.emptied = 0

    def markdown(self, text: str) -> None:
        self.rendered.append(text)

    def empty(self) -> None:
        self.emptied += 1


def _delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        event_type="message_delta", payload=SimpleNamespace(text_delta=text)
    )


def test_run_with_stream_accumulates_deltas_and_returns_the_result() -> None:
    """增量逐条画进占位符、拼起来是完整文本；结束后清空占位符（快照接管最终渲染）。"""
    service = _FakeStreamService([_delta("你"), _delta("好")])
    placeholder = _FakePlaceholder()

    result = web.run_with_stream(service, "s-1", lambda: "OK", placeholder=placeholder)

    assert result == "OK"
    assert service.subscribed == ["s-1"]
    assert placeholder.rendered == ["你", "你好"]
    assert placeholder.emptied == 1
    assert service.unsubscribed  # 无论成功失败都要退订


def test_run_with_stream_reraises_failures_from_the_worker_thread() -> None:
    """工作线程里的失败必须在脚本线程重新抛出，页面边界才能统一渲染提示。"""
    service = _FakeStreamService([])

    def _boom() -> Any:
        raise ValueError("boom（测试故意触发）")

    with pytest.raises(ValueError, match="boom"):
        web.run_with_stream(service, "s-1", _boom, placeholder=_FakePlaceholder())

    assert service.unsubscribed


# --------------------------------------------------------------------------- 页面：服务契约


def test_page_reports_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    page = _run_page(tmp_path, monkeypatch)

    assert not page.exception
    errors = "\n".join(element.value for element in page.error)
    assert "模型尚未配置" in errors
    assert "未找到配置文件" in errors
    assert not page.chat_input  # 没有可用服务时不给输入框（也不自建会话）


def test_page_drives_the_application_service_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory: Any
) -> None:
    """装配、会话、提交、渲染都经应用服务：页面自己不再碰图与 State。"""
    _write_config(tmp_path, PAGE_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(decision="chat", reply=FAKE_REPLY),
    )

    page = _run_page(tmp_path, monkeypatch)
    assert not page.exception
    assert any("模型已就绪" in element.value for element in page.success)

    page = _new_session(page)
    assert not page.exception
    assert page.chat_input, "有会话之后必须给出输入框"

    page.chat_input[0].set_value("你好").run()

    assert not page.exception
    rendered = _rendered_text(page)
    assert "你好" in rendered
    assert FAKE_REPLY in rendered
    assert not page.error


def test_page_shows_the_tool_activity_and_the_edit_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory: Any
) -> None:
    """工具行与结构化 diff 都来自 DTO：页面不解析工具原文。"""
    _write_config(tmp_path, PAGE_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    target = tmp_path / "note.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(
            decision="tool",
            reply="改好了",
            tool_calls=[
                {
                    "name": "edit_file",
                    "args": {
                        "path": "note.py",
                        "old_text": "value = 1",
                        "new_text": "value = 2",
                        "expected_sha256": sha,
                    },
                    "id": "call-1",
                }
            ],
        ),
    )

    page = _run_page(tmp_path, monkeypatch)
    page = _new_session(page)
    page.chat_input[0].set_value("把 value 改成 2").run()

    assert not page.exception
    rendered = _rendered_text(page)
    assert "+value = 2" in rendered
    labels = [getattr(element, "label", "") for element in page.expander]
    assert any("edit_file" in label for label in labels)
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_page_surfaces_the_approval_card_and_executes_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory: Any
) -> None:
    """挂起 → 卡片 → 批准后才真正执行（架构 §5.3、§6.5）。"""
    _write_config(tmp_path, PAGE_CONFIG + "\nsecurity:\n  require_approval:\n    delete_file: true\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(
            decision="tool",
            reply="好的",
            tool_calls=[{"name": "delete_file", "args": {"path": "aa.txt"}, "id": "call-1"}],
        ),
    )
    target = tmp_path / "aa.txt"
    target.write_text("别删我", encoding="utf-8", newline="")

    page = _run_page(tmp_path, monkeypatch)
    page = _new_session(page)
    page.chat_input[0].set_value("删除 aa.txt").run()

    assert not page.exception
    assert target.is_file()  # 审批之前不会执行
    warnings = "\n".join(element.value for element in page.warning)
    assert "需要你确认的动作" in warnings
    assert not page.chat_input  # 审批接管输入区，不能再提交新消息

    page = page.button(key="approval-approve").click().run()

    assert not page.exception
    assert not target.exists()


def test_page_rejecting_an_approval_keeps_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory: Any
) -> None:
    _write_config(tmp_path, PAGE_CONFIG + "\nsecurity:\n  require_approval:\n    delete_file: true\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(
            decision="tool",
            reply="好的",
            tool_calls=[{"name": "delete_file", "args": {"path": "bb.txt"}, "id": "call-1"}],
        ),
    )
    target = tmp_path / "bb.txt"
    target.write_text("别删我", encoding="utf-8", newline="")

    page = _run_page(tmp_path, monkeypatch)
    page = _new_session(page)
    page.chat_input[0].set_value("删除 bb.txt").run()
    page = page.button(key="approval-reject").click().run()

    assert not page.exception
    assert target.read_text(encoding="utf-8") == "别删我"


def test_pending_approval_survives_a_refresh_and_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory: Any
) -> None:
    """刷新从快照恢复，重启也一样：挂起的审批在 Checkpoint 里，页面上没有任何"已决策"状态。

    页面进程按设计一直占着单实例锁（§15.1），所以"重启"用一个 `instance_lock=False` 的只读
    运行时来读同一份库表示——那正是新进程启动后会看到的东西。
    """
    _write_config(tmp_path, PAGE_CONFIG + "\nsecurity:\n  require_approval:\n    delete_file: true\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(
            decision="tool",
            reply="好的",
            tool_calls=[{"name": "delete_file", "args": {"path": "cc.txt"}, "id": "call-1"}],
        ),
    )
    target = tmp_path / "cc.txt"
    target.write_text("别删我", encoding="utf-8", newline="")

    page = _run_page(tmp_path, monkeypatch)
    page = _new_session(page)
    page.chat_input[0].set_value("删除 cc.txt").run()

    assert not page.exception
    assert "需要你确认的动作" in "\n".join(element.value for element in page.warning)

    # 刷新：重跑脚本（不点任何按钮），审批卡必须重新出现，且不自动执行
    refreshed = page.run()

    assert not refreshed.exception
    assert "需要你确认的动作" in "\n".join(element.value for element in refreshed.warning)
    assert target.is_file()

    # 重启：新进程按同一份配置读同一个库，看到的仍是"等待决策"
    session = _sessions(tmp_path).active()
    assert session is not None
    config = load_config(tmp_path / "config.yaml")
    restarted = open_runtime(config, base_dir=tmp_path, instance_lock=False)
    try:
        snapshot = restarted.service.get_session_snapshot(session.session_id)
        assert snapshot.approval is not None
        assert snapshot.approval.state == "pending"
    finally:
        restarted.close()
    assert target.is_file()  # 重启不会自动执行，也不会沿用之前的确认结果


def test_plain_rerun_does_not_submit_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory: Any
) -> None:
    """普通重绘只读快照：不重新调用模型，也不重复显示答案（架构 §1.4）。"""
    _write_config(tmp_path, PAGE_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    model = model_factory(decision="chat", reply=FAKE_REPLY)
    monkeypatch.setattr(factory, "build_chat_model", lambda _config, _api_key: model)

    page = _run_page(tmp_path, monkeypatch)
    page = _new_session(page)
    page.chat_input[0].set_value("你好").run()
    calls = model.decision_calls

    page.run()

    assert model.decision_calls == calls
    assert _rendered_text(page).count(FAKE_REPLY) == 1


def test_new_session_isolates_history_and_switching_back_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory: Any
) -> None:
    _write_config(tmp_path, PAGE_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(
            decision="chat",
            reply=FAKE_REPLY,
            response_by_input={"问题甲": "回答甲", "问题乙": "回答乙"},
        ),
    )

    page = _run_page(tmp_path, monkeypatch)
    page = _new_session(page)
    page.chat_input[0].set_value("问题甲").run()
    assert "问题甲" in _rendered_text(page)
    first = _sessions(tmp_path).active()
    assert first is not None

    page = _new_session(page)

    assert not page.exception
    assert "问题甲" not in _rendered_text(page)  # 新会话不继承旧会话的消息

    page.chat_input[0].set_value("问题乙").run()
    rendered = _rendered_text(page)
    assert "问题乙" in rendered
    assert "问题甲" not in rendered
    assert len(_sessions(tmp_path).list_sessions()) == 2

    # 切回第一个会话：历史恢复（页面记的只是"当前打开哪个会话"）
    page = page.sidebar.button(key=f"session-open-{first.session_id}").click().run()

    rendered = _rendered_text(page)
    assert "问题甲" in rendered
    assert "问题乙" not in rendered


def test_session_sidebar_has_no_rename_because_the_service_has_no_such_use_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """页面只提供应用服务确实有的用例：v0.4 没有"改名/切换"用例，侧栏就不放这些控件。"""
    _write_config(tmp_path, PAGE_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")

    page = _run_page(tmp_path, monkeypatch)

    keys = [element.key for element in page.sidebar.button]
    assert "session-new" in keys
    assert "session-rename" not in keys
    assert [element.key for element in page.sidebar.text_input] == ["session-workspace-path"]


# --------------------------------------------------------------------------- 两个 UI 同一份事实


def _config(directory: Path, app_config: AppConfig) -> AppConfig:
    """把示例配置改成本用例目录下的路径（工作区 + 聊天库），与 `test_api.py` 同一套改法。"""
    workspace = directory / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    return app_config.model_copy(
        update={
            "workspace": app_config.workspace.model_copy(update={"root": str(workspace)}),
            "memory": app_config.memory.model_copy(
                update={
                    "short_term": app_config.memory.short_term.model_copy(
                        update={
                            "enabled": True,
                            "path": str(directory / "data" / "checkpoints.sqlite"),
                        }
                    )
                }
            ),
        }
    )


def test_same_fixture_gives_the_same_business_result_on_every_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_config: AppConfig, model_factory: Any
) -> None:
    """同一份输入经 HTTP（React 的通道）、直接应用服务与 Streamlit 页面得到等价结论。

    三个通道各用一份库（真实运行不允许两个进程共用一把锁，§15.1），但**同一份配置、
    同一套装配、同一个假模型脚本**——因此业务结论必须一致。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")

    api_dir = tmp_path / "api"
    api_runtime = open_runtime(
        _config(api_dir, app_config),
        base_dir=api_dir,
        model=model_factory(decision="chat", reply=FAKE_REPLY),
    )
    try:
        client = TestClient(create_app(api_runtime), base_url="http://127.0.0.1")
        session_id = client.post("/api/sessions", json={"title": "会话"}).json()["session_id"]
        client.post(
            f"/api/sessions/{session_id}/messages",
            json={"text": "你好", "client_request_id": "req-1"},
        )
        snapshot = client.get(f"/api/sessions/{session_id}").json()
        api_messages = [
            (item["kind"], item["text"])
            for item in snapshot["messages"]
            if item["kind"] in {"user", "assistant"}
        ]
        api_state = snapshot["session_state"]
    finally:
        api_runtime.close()

    service_dir = tmp_path / "service"
    service_runtime = open_runtime(
        _config(service_dir, app_config),
        base_dir=service_dir,
        model=model_factory(decision="chat", reply=FAKE_REPLY),
    )
    try:
        session = service_runtime.service.create_session(title="会话")
        service_runtime.service.submit_message(session.session_id, "你好", "req-1")
        service_snapshot = service_runtime.service.get_session_snapshot(session.session_id)
        service_messages = [
            (item.kind, item.text)
            for item in service_snapshot.messages
            if item.kind in {"user", "assistant"}
        ]
        service_state = service_snapshot.session_state
    finally:
        service_runtime.close()

    page_dir = tmp_path / "page"
    _write_config(page_dir, PAGE_CONFIG)
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(decision="chat", reply=FAKE_REPLY),
    )
    page = _run_page(page_dir, monkeypatch)
    page = _new_session(page)
    page.chat_input[0].set_value("你好").run()

    assert not page.exception
    assert not page.error
    rendered = _rendered_text(page)

    assert api_messages == service_messages == [("user", "你好"), ("assistant", FAKE_REPLY)]
    # 一轮跑完后的会话状态：两个通道读到同一个结论（这里是 "completed"，不是 "idle"）。
    assert api_state == service_state == "completed"
    assert "你好" in rendered and FAKE_REPLY in rendered


def test_both_uis_read_one_model_settings_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_config: AppConfig
) -> None:
    """两个 UI 读的是**同一个设置库**：一边换默认连接，另一边立刻看到同一版本（§6.8）。

    `instance_lock=False` 只是为了让本用例在同一进程里打开两套运行时来代表两个通道；
    单实例锁本身在 `test_api.py` 里单独覆盖。
    """
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    config = _config(base, app_config)

    left = open_runtime(config, base_dir=base, instance_lock=False)
    right = open_runtime(config, base_dir=base, instance_lock=False)
    try:
        # 两边读同一个库：初始看到的是同一份默认（含检查是否已同步过的重试）
        for _ in range(3):
            if left.service.current_model_name() == right.service.current_model_name():
                break
            left.service.list_model_connections()
        assert left.service.current_model_name() == "deepseek-flash"

        created = left.service.save_model_connection(
            ConnectionDraft(
                display_name="备用连接",
                base_url="https://api.deepseek.com",
                model="deepseek-reasoner",
                context_window_tokens=65536,
                api_key_env="DEEPSEEK_API_KEY",
            ),
            api_key="sk-test-fake",
        )
        left.service.select_default_model(created.connection_id)

        assert right.service.current_model_name() == "deepseek-reasoner"
    finally:
        left.close()
        right.close()


# --------------------------------------------------------------------------- 依赖方向


PAGE_MODULES = ("web.py", "ui/renderer.py", "ui/approval_ui.py")
#: 页面层不该出现的导入：Graph / State / LangChain 消息 / LangGraph 的 `Command`。
FORBIDDEN_IMPORTS = ("langgraph", "langchain_core", "local_agent.graph")


def test_page_layer_does_not_import_the_graph_or_langchain_messages() -> None:
    """T33 的停止条件：页面层不导入 Graph 节点、State、LangChain 消息类或 LangGraph。

    因此 Graph 内部节点改名、State 字段调整都不会波及页面代码与 UI 测试（T33 拿掉的就是这层耦合）。
    """
    package = Path(web.__file__).resolve().parent
    offenders: list[str] = []
    for relative in PAGE_MODULES:
        source = (package / relative).read_text(encoding="utf-8")
        for raw in source.splitlines():
            line = raw.strip()
            if not line.startswith(("import ", "from ")):
                continue
            if any(token in line for token in FORBIDDEN_IMPORTS):
                offenders.append(f"{relative}: {line}")
            if line == "from langgraph.types import Command" or line.endswith("Command"):
                offenders.append(f"{relative}: {line}")

    assert offenders == []


def test_page_modules_import_streamlit_so_they_cannot_be_used_by_the_api() -> None:
    """反向也是硬约束：API 层不得依赖页面模块（架构 §15.1 的依赖方向）。"""
    package = Path(web.__file__).resolve().parent
    for relative in PAGE_MODULES:
        source = (package / relative).read_text(encoding="utf-8")
        assert "import streamlit" in source, relative

"""T02/T03/T04 验收：配置加载、模型工厂、图与网页交互。

自动测试不依赖网络、不依赖真实密钥：图与页面测试用假模型（见 conftest.py），
配置测试用临时目录与注入的环境变量。真实模型的手动验收步骤见 README。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from local_agent import web
from local_agent.config import ConfigError, load_config, redact_secrets, resolve_api_key
from local_agent.graph import builder, nodes
from local_agent.graph.state import make_decision_record
from local_agent.llm import factory
from local_agent.persistence import checkpoint
from local_agent.persistence.session_store import SessionStore
from local_agent.ui.renderer import config_error_text, to_chat_messages

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MINIMAL_CONFIG = """\
llm:
  base_url: "https://api.deepseek.com"
  api_key_env: "DEEPSEEK_API_KEY"
  model: "deepseek-flash"
"""
FAKE_REPLY = "你好，我是测试模型"


def _write_config(tmp_path: Path, body: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _context(tmp_path: Path, model: object, app_config: object) -> object:
    return builder.build_runtime_context(
        app_config=app_config,  # type: ignore[arg-type]
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=tmp_path,
        current_cwd=tmp_path,
    )


def _run_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """在临时工作目录里真实运行网页脚本。

    先清空 Streamlit 的全局缓存，等价于重启服务：避免上一个用例缓存的
    模型/数据库连接被复用。
    """
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    monkeypatch.chdir(tmp_path)
    st.cache_resource.clear()
    page = AppTest.from_file(str(Path(web.__file__)), default_timeout=60)
    page.run()
    return page


# --------------------------------------------------------------------------- 配置层


def test_config_example_is_valid() -> None:
    config = load_config(PROJECT_ROOT / "config.yaml.example")

    assert config.llm.model == "deepseek-flash"
    assert config.llm.api_key_env == "DEEPSEEK_API_KEY"
    assert config.memory.medium_term.enabled is False
    assert config.memory.compression.enabled is False
    assert config.security.require_approval.delete_file is True


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "config.yaml")

    assert "未找到配置文件" in str(excinfo.value)


def test_invalid_yaml_is_reported(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "llm: [unclosed\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "不是合法 YAML" in str(excinfo.value)


def test_non_mapping_top_level_is_reported(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "- a\n- b\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "顶层必须是" in str(excinfo.value)


def test_empty_config_file_is_reported(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "空文件" in str(excinfo.value)


def test_missing_llm_section_is_reported(tmp_path: Path) -> None:
    path = _write_config(tmp_path, 'workspace:\n  root: "."\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "配置校验失败" in message
    assert "llm" in message


def test_missing_required_llm_fields_are_listed(tmp_path: Path) -> None:
    path = _write_config(tmp_path, 'llm:\n  base_url: "https://api.deepseek.com"\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "llm.model" in message
    assert "llm.api_key_env" in message


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    path = _write_config(tmp_path, MINIMAL_CONFIG + '  modell: "typo"\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "llm.modell" in str(excinfo.value)


def test_require_approval_cannot_be_disabled(tmp_path: Path) -> None:
    body = MINIMAL_CONFIG + "security:\n  require_approval:\n    delete_file: false\n"
    path = _write_config(tmp_path, body)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "delete_file" in message
    assert "不能关闭" in message


def test_embedding_requires_fields_when_enabled(tmp_path: Path) -> None:
    body = MINIMAL_CONFIG + 'embedding:\n  enabled: true\n  provider: "openai_compatible"\n'
    path = _write_config(tmp_path, body)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "embedding" in message
    assert "base_url" in message


def test_resolve_api_key_reports_missing_variable_without_value(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, MINIMAL_CONFIG))

    with pytest.raises(ConfigError) as excinfo:
        resolve_api_key(config.llm, environ={})

    message = str(excinfo.value)
    assert "DEEPSEEK_API_KEY" in message
    assert ".env" in message


def test_resolve_api_key_returns_value(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, MINIMAL_CONFIG))

    assert resolve_api_key(config.llm, environ={"DEEPSEEK_API_KEY": "sk-test"}) == "sk-test"


def test_redact_secrets_replaces_key_values() -> None:
    assert redact_secrets("boom sk-secret end", ["sk-secret"]) == "boom *** end"
    assert redact_secrets("k=sk-a", {"DEEPSEEK_API_KEY": "sk-a"}) == "k=***"


def test_redact_secrets_ignores_empty_values() -> None:
    assert redact_secrets("没有密钥", [""]) == "没有密钥"


def test_config_error_text_includes_reason_and_hint() -> None:
    text = config_error_text("未找到配置文件 config.yaml", "请先创建配置")

    assert text.startswith("模型尚未配置：")
    assert "请先创建配置" in text


# --------------------------------------------------------------------------- 模型工厂


def test_build_chat_model_maps_config(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, MINIMAL_CONFIG))

    model = factory.build_chat_model(config.llm, "sk-test")

    assert model.model_name == "deepseek-flash"
    assert model.openai_api_base == "https://api.deepseek.com"
    assert model.temperature == pytest.approx(0.2)
    assert model.max_tokens == 4096
    assert model.request_timeout == 120
    assert model.max_retries == 2
    # 架构 §11.7：Streaming 不是必须能力，统一按非流式输出保证正确性
    assert model.streaming is False


# --------------------------------------------------------------------------- 图


def test_chat_graph_answers_without_tools(tmp_path: Path, model_factory, app_config) -> None:
    graph = builder.build_chat_graph()
    context = _context(tmp_path, model_factory(decision="chat", reply=FAKE_REPLY), app_config)

    result = graph.invoke({"user_input": "  你好  "}, context=context)

    assert result["final_response"] == FAKE_REPLY
    assert result["normalized_request"] == "你好"
    assert result["mode"] == "chat"
    assert result["task_status"] == "completed"
    assert result["thread_id"]
    assert result["session_id"]
    assert result["run_id"]
    assert len(result["messages"]) == 2
    # 闲聊路径不得产生任何工具事件或待执行调用
    assert not result.get("tool_events")
    assert not result.get("tool_results")
    assert not result.get("pending_tool_calls")
    assert result.get("reflection_result") is None


def test_chat_graph_keeps_history_across_turns(tmp_path: Path, model_factory, app_config) -> None:
    graph = builder.build_chat_graph()
    context = _context(tmp_path, model_factory(decision="chat", reply=FAKE_REPLY), app_config)

    first = graph.invoke({"user_input": "甲"}, context=context)
    # 未启用 Checkpoint 时状态不跨轮保留，由调用方把上一轮消息一并提交
    second = graph.invoke({"user_input": "乙", "messages": first["messages"]}, context=context)

    assert [message.text for message in second["messages"]] == [
        "甲",
        FAKE_REPLY,
        "乙",
        FAKE_REPLY,
    ]


def test_final_response_node_marks_failed_without_ai_text() -> None:
    result = nodes.final_response_node({"messages": [HumanMessage(content="没有回答")]})

    assert result["final_response"] is None
    assert result["task_status"] == "failed"


def test_to_chat_messages_maps_roles_and_skips_empty() -> None:
    messages = to_chat_messages(
        [HumanMessage(content="问"), AIMessage(content="答"), AIMessage(content="")]
    )

    assert [(message.role, message.content) for message in messages] == [
        ("user", "问"),
        ("assistant", "答"),
    ]


def test_tool_activities_pair_each_occurrence_with_its_own_result() -> None:
    """同一 ``tool_call_id`` 出现多次时，每次调用配自己的结果与状态（架构 §4.2）。"""
    from streamlit.testing.v1 import AppTest

    script = """
from langchain_core.messages import AIMessage, ToolMessage

from local_agent.ui.renderer import render_history

call = {"name": "read_file", "args": {"path": "a"}, "id": "call-1", "type": "tool_call"}
messages = [
    AIMessage(content="", tool_calls=[call]),
    ToolMessage(content="第一轮结果", tool_call_id="call-1"),
    AIMessage(content="", tool_calls=[call]),
    ToolMessage(content="第二轮结果", tool_call_id="call-1"),
]
render_history(
    messages,
    tool_results=[
        {"tool_call_id": "call-1", "status": "denied"},
        {"tool_call_id": "call-1", "status": "ok"},
    ],
)
"""
    page = AppTest.from_string(script, default_timeout=30).run()

    assert not page.exception
    codes = "\n".join(element.value for element in page.code)
    assert "第一轮结果" in codes
    assert "第二轮结果" in codes
    labels = [getattr(element, "label", "") for element in page.expander]
    assert any("被拒绝" in label for label in labels), labels
    assert any("成功" in label for label in labels), labels


# --------------------------------------------------------------------------- 页面


def test_page_reports_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    page = _run_page(tmp_path, monkeypatch)

    assert not page.exception
    assert any("模型尚未配置" in element.value for element in page.error)

    page.chat_input[0].set_value("你好").run()

    assert not page.exception
    rendered = [element.value for element in page.markdown]
    assert "你好" in rendered
    assert any("未找到配置文件" in text for text in rendered)


def test_page_reports_missing_api_key_without_leaking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    page = _run_page(tmp_path, monkeypatch)

    assert not page.exception
    assert any("DEEPSEEK_API_KEY" in element.value for element in page.error)


def test_page_answers_with_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(decision="chat", reply=FAKE_REPLY),
    )
    page = _run_page(tmp_path, monkeypatch)

    assert not page.exception
    assert any("模型已就绪" in element.value for element in page.success)

    page.chat_input[0].set_value("你好").run()

    assert not page.exception
    rendered = [element.value for element in page.markdown]
    assert "你好" in rendered
    assert FAKE_REPLY in rendered
    assert not page.error


def test_page_shows_approval_card_and_delete_needs_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """需要审批的动作必须弹出审批卡片，点批准后才真正删除（架构 §5.3、§6.5）。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
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
    _run_page(tmp_path, monkeypatch)  # 先建立 Checkpoint 与工作区
    target = tmp_path / "aa.txt"
    target.write_text("别删我", encoding="utf-8", newline="")

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("删除 aa.txt").run()

    assert not page.exception
    assert target.is_file()  # 审批之前不会执行
    notices = [element.value for element in page.warning]
    assert any("需要你确认的动作" in text for text in notices)

    # 批准执行（卡片里的第一个按钮）
    page.button[0].click().run()

    assert not page.exception
    assert not target.exists()


def test_page_approval_reject_keeps_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    _write_config(tmp_path, MINIMAL_CONFIG)
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
    _run_page(tmp_path, monkeypatch)
    target = tmp_path / "bb.txt"
    target.write_text("别删我", encoding="utf-8", newline="")

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("删除 bb.txt").run()
    # 拒绝按钮在第二位
    page.button[1].click().run()

    assert not page.exception
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "别删我"


def test_page_reports_capability_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(
            decision="chat", reply=FAKE_REPLY, fail_bind_tools=True
        ),
    )

    page = _run_page(tmp_path, monkeypatch)

    assert any("工具调用：不支持" in element.value for element in page.success)


def test_page_reports_tool_calling_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """模型不支持 Tool Calling 时，工具请求必须给出明确提示（架构 §11.7）。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(
            decision="tool", reply="不该走到这里", fail_bind_tools=True
        ),
    )
    page = _run_page(tmp_path, monkeypatch)

    page.chat_input[0].set_value("列出当前目录").run()

    assert not page.exception
    assert "不支持工具调用" in _rendered_text(page)


def _rendered_text(page: object) -> str:
    """页面上所有文本型元素的内容（含折叠区里的代码块）。"""
    parts = [element.value for element in page.markdown]
    parts += [element.value for element in page.code]
    return "\n".join(parts)


def test_page_shows_tool_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """工具分支：模型调用 read_file，页面展示工具活动与最终回答。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    (tmp_path / "note.txt").write_text("文件内容甲", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(
            decision="tool",
            reply="我读到的是：文件内容甲",
            tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        ),
    )
    page = _run_page(tmp_path, monkeypatch)

    page.chat_input[0].set_value("读取 note.txt").run()

    assert not page.exception
    text = _rendered_text(page)
    assert "我读到的是：文件内容甲" in text
    assert "note.txt" in text


def _saved_messages(tmp_path: Path, thread_id: str) -> list[AnyMessage]:
    """从磁盘重开 Checkpoint 连接读取消息，等价于重启服务后再看历史。"""
    database = tmp_path / "data" / "checkpoints.sqlite"
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    return list(builder.read_thread_state(graph, thread_id).get("messages", []))


def test_page_history_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """提交一轮后模拟刷新页面 + 重启服务：历史仍在，不需要重新调用模型。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(decision="chat", reply=FAKE_REPLY),
    )

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("第一轮").run()
    assert not page.exception

    # _run_page 会清掉 cache_resource 并重新运行脚本，等价于刷新页面后再重启服务
    restarted = _run_page(tmp_path, monkeypatch)

    assert not restarted.exception
    rendered = [element.value for element in restarted.markdown]
    assert "第一轮" in rendered
    assert FAKE_REPLY in rendered


def test_page_shows_edit_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """编辑类工具结果必须在页面上展示 diff（架构 §6.3）。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
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
    page.chat_input[0].set_value("把 value 改成 2").run()

    assert not page.exception
    rendered = _rendered_text(page)
    assert "--- a/note.py" in rendered  # diff 真的画出来了，而不是只显示 JSON
    assert "+value = 2" in rendered
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_page_warns_about_unconfirmed_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """上次运行异常结束时，页面必须提示人工核对，且不自动重放工具（架构 §4.2）。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(decision="chat", reply=FAKE_REPLY),
    )
    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("第一轮").run()
    assert not page.exception

    # 直接往 Checkpoint 里塞一条“已放行但没有结果”的记录，模拟进程在副作用后被杀
    graph = builder.build_chat_graph(
        checkpoint.open_checkpointer(tmp_path / "data" / "checkpoints.sqlite")
    )
    graph.update_state(
        {"configurable": {"thread_id": web.DEFAULT_THREAD_ID}},
        {
            "run_id": "run-crashed",
            "decision_trace": [
                make_decision_record(
                    "run-crashed", stage="tool_start", summary="放行", tool_call_id="call-crashed"
                )
            ],
        },
    )

    reloaded = _run_page(tmp_path, monkeypatch)

    assert not reloaded.exception
    warnings = [element.value for element in reloaded.warning]
    assert any("没有正常结束" in text for text in warnings)
    assert any("call-crashed" in text for text in warnings)


def test_plain_rerun_does_not_invoke_model_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """页面普通重绘不得再写消息（架构 §1.4 的幂等要求）。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(decision="chat", reply=FAKE_REPLY),
    )

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("第一轮").run()
    before = _saved_messages(tmp_path, web.DEFAULT_THREAD_ID)

    page.run()  # 普通重绘
    after = _saved_messages(tmp_path, web.DEFAULT_THREAD_ID)

    assert [message.text for message in before] == [message.text for message in after]
    assert len(after) == 2


# --------------------------------------------------------------------------- T11 会话侧栏


def _session_store(tmp_path: Path) -> SessionStore:
    """按页面的工作区重开会话索引，等价于重启服务后再读。"""
    database = checkpoint.open_database(
        tmp_path / "data" / "checkpoints.sqlite", workspace_root=tmp_path.resolve()
    )
    return database.sessions


def _has_pending_approval(page: object) -> bool:
    return any("需要你确认的动作" in element.value for element in page.warning)


def test_page_keeps_one_session_across_rerun_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """刷新与重启都不新建重复会话，当前会话跨重启恢复（架构 §10.6）。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")

    page = _run_page(tmp_path, monkeypatch)
    page.run()  # 普通重绘
    page = _run_page(tmp_path, monkeypatch)  # 等价重启服务

    assert not page.exception
    sessions = _session_store(tmp_path).list_sessions()
    assert len(sessions) == 1
    assert sessions[0].is_active
    assert sessions[0].thread_id == web.DEFAULT_THREAD_ID


def test_page_new_session_isolates_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """两个会话各自的消息互相隔离；切回旧会话后历史与重启恢复都要成立。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(decision="chat", reply=FAKE_REPLY),
    )

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("问题甲").run()
    assert "问题甲" in _rendered_text(page)
    first = _session_store(tmp_path).active()
    assert first is not None

    page = page.sidebar.button(key="session-new").click().run()

    assert not page.exception
    assert "问题甲" not in _rendered_text(page)  # 新会话不继承旧会话的消息

    page.chat_input[0].set_value("问题乙").run()
    rendered = _rendered_text(page)
    assert "问题乙" in rendered
    assert "问题甲" not in rendered
    assert len(_session_store(tmp_path).list_sessions()) == 2

    # 切回第一个会话：历史恢复；再重启一次仍然停在它上面
    page = page.sidebar.radio[0].set_value(first.session_id).run()
    rendered = _rendered_text(page)
    assert "问题甲" in rendered
    assert "问题乙" not in rendered

    restarted = _run_page(tmp_path, monkeypatch)
    assert "问题甲" in _rendered_text(restarted)
    assert "问题乙" not in _rendered_text(restarted)
    active = _session_store(tmp_path).active()
    assert active is not None and active.session_id == first.session_id


def test_page_pending_approval_is_isolated_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """挂起的审批属于产生它的会话：新会话不弹卡片，切回去才重新出现（架构 §10.6）。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
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
    _run_page(tmp_path, monkeypatch)
    target = tmp_path / "aa.txt"
    target.write_text("别删我", encoding="utf-8", newline="")

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("删除 aa.txt").run()
    assert _has_pending_approval(page)
    first = _session_store(tmp_path).active()
    assert first is not None

    page = page.sidebar.button(key="session-new").click().run()

    assert not page.exception
    assert not _has_pending_approval(page)  # 新会话没有挂起动作
    assert target.is_file()  # 也没有任何文件被改动

    page = page.sidebar.radio[0].set_value(first.session_id).run()

    assert not page.exception
    assert _has_pending_approval(page)  # 切回原会话，挂起状态还在，仍需人工确认


def test_page_renames_current_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")

    page = _run_page(tmp_path, monkeypatch)
    page = page.sidebar.text_input[0].set_value("我的调研会话").run()
    page = page.sidebar.button(key="session-rename").click().run()

    assert not page.exception
    active = _session_store(tmp_path).active()
    assert active is not None and active.title == "我的调研会话"


def test_page_names_session_from_first_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """首条用户消息自动命名会话，避免侧栏里全是“新会话”。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(decision="chat", reply=FAKE_REPLY),
    )

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("帮我看看项目结构").run()

    assert not page.exception
    active = _session_store(tmp_path).active()
    assert active is not None
    assert active.title == "帮我看看项目结构"
    assert active.last_checkpoint_id is not None  # 会话索引记录了本轮 Checkpoint

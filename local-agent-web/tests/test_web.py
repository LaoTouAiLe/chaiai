"""T02/T03/T04 验收：配置加载、模型工厂、图与网页交互。

自动测试不依赖网络、不依赖真实密钥：图与页面测试用假模型（见 conftest.py），
配置测试用临时目录与注入的环境变量。真实模型的手动验收步骤见 README。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from local_agent import web
from local_agent.config import (
    AppConfig,
    ConfigError,
    load_config,
    redact_secrets,
    resolve_api_key,
)
from local_agent.graph import builder, nodes
from local_agent.graph.state import make_decision_record
from local_agent.llm import embedding as embedding_module
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
  context_window_tokens: 65536
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


# 后半段断言的是"依赖已装、缓存目录合法 → 无警告"（`warn is False`）。没装可选组时
# `readiness` 会如实给出 `unavailable`，那条断言测的东西就不存在了——跳过而不是放宽成
# 两种结果都接受（否则真出问题时这条用例反而抓不住）。
@pytest.mark.skipif(
    not embedding_module.fastembed_available(),
    reason="需要可选依赖 memory（fastembed）：uv sync --extra memory",
)
def test_memory_status_lines_describe_each_layer(tmp_path: Path) -> None:
    """侧栏状态块：三层各一行；默认开关与配置问题都必须如实展示（架构 §1.4）。"""
    base = load_config(PROJECT_ROOT / "config.yaml.example")

    default_lines, default_warn = web.memory_status_lines(
        SimpleNamespace(app_config=base)  # type: ignore[arg-type]
    )
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
    lines, warn = web.memory_status_lines(
        SimpleNamespace(app_config=config)  # type: ignore[arg-type]
    )

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
    _, warn_broken = web.memory_status_lines(
        SimpleNamespace(app_config=broken)  # type: ignore[arg-type]
    )
    assert warn_broken is True


def test_memory_status_lines_report_missing_api_key_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """远端 embedding 缺 Key：状态页展示可处理的警告，而不是整块抛 ``ConfigError``。

    复审实证：状态块原来先调用会抛异常的 ``resolve_api_key()``，于是 ``readiness(..., api_key=None)``
    的 ``not_ready`` 分支根本走不到，用户看到的是页面报错而不是"请补 Key"。
    """
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

    lines, warn = web.memory_status_lines(
        SimpleNamespace(app_config=config)  # type: ignore[arg-type]
    )

    assert warn is True
    assert any("API Key" in line for line in lines)


def test_pending_error_lines_are_deduped_and_redacted() -> None:
    """页面要显示本轮的降级/错误事件：按 ``event_id`` 去重，并且先脱敏。

    复审实证：图产生了可读的 ``recall_memory`` 错误事件（Embedding 首次加载失败），页面却只渲染
    消息与工具结果 —— 用户只看到一条正常的聊天回复，看不到失败原因，与侧栏"失败原因会出现在对话里"
    的说法不符。
    """
    state = {
        "error_events": [
            {
                "event_id": "e1",
                "stage": "recall_memory",
                "message": "Embedding 首次加载失败：连接超时（key=sk-secret）",
            },
            {"event_id": "e2", "stage": "memory_write_node", "message": "长期库打不开"},
        ]
    }

    lines, fresh = web.pending_error_lines(state, shown={"e1"}, secrets=["sk-secret"])

    assert lines == ["[memory_write_node] 长期库打不开"]  # 已展示过的不重复渲染
    assert fresh == ["e2"]

    lines, fresh = web.pending_error_lines(state, shown=set(), secrets=["sk-secret"])

    assert len(lines) == 2 and len(fresh) == 2
    assert "sk-secret" not in " ".join(lines)  # 先脱敏再上页面
    assert lines[0].startswith("[recall_memory] Embedding 首次加载失败")


def test_projected_history_applies_reply_corrections() -> None:
    """页面展示走投影：修正过的回答替换掉原文，条数与顺序不变。

    页面上不能出现"我记住了"与"没有保存成功"并排两条（复审实证）；也不能为好看去改 Checkpoint
    里的原文（架构 §10.1）。
    """
    messages = [
        HumanMessage(content="请记住", id="h1"),
        AIMessage(content="好的，我记住了", id="a1"),
    ]

    projected = web.projected_history(
        {"messages": messages, "reply_overrides": {"a1": "（这条记忆没有保存成功。）"}}
    )

    assert [item.content for item in projected] == ["请记住", "（这条记忆没有保存成功。）"]
    assert [item.id for item in projected] == ["h1", "a1"]  # 没有新增或删除消息
    assert messages[1].content == "好的，我记住了"  # 原对象未被修改
    assert web.projected_history({"messages": messages}) == messages  # 没有投影时原样返回

    # 本轮回答为空时：渲染本来会跳过这条空消息，投影后用户才看得到说明（复审实证）
    empty_reply = [HumanMessage(content="请记住", id="h3"), AIMessage(content="", id="a3")]
    assert [item.content for item in to_chat_messages(empty_reply)] == ["请记住"]
    assert [
        item.content
        for item in to_chat_messages(
            web.projected_history(
                {"messages": empty_reply, "reply_overrides": {"a3": "（没有保存成功。）"}}
            )
        )
    ] == ["请记住", "（没有保存成功。）"]


def test_config_example_is_valid() -> None:
    config = load_config(PROJECT_ROOT / "config.yaml.example")

    assert config.llm.model == "deepseek-flash"
    assert config.llm.api_key_env == "DEEPSEEK_API_KEY"
    assert config.memory.medium_term.enabled is False
    assert config.memory.long_term.enabled is True
    assert config.memory.compression.enabled is True
    assert config.security.require_approval.delete_file is True


def test_memory_features_default_on_when_section_is_omitted(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, MINIMAL_CONFIG))

    assert config.memory.long_term.enabled is True
    assert config.memory.compression.enabled is True


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


def test_missing_context_window_tokens_is_reported(tmp_path: Path) -> None:
    """``llm.context_window_tokens`` 是必填键（没有默认值）：缺了必须直接失败，并说清怎么补。"""
    body = 'llm:\n  base_url: "https://api.deepseek.com"\n  api_key_env: "K"\n  model: "m"\n'
    path = _write_config(tmp_path, body)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "context_window_tokens" in message
    # 新增必填键会让既有的 config.yaml 直接失效（升级后典型故障）：提示必须可操作，
    # 不能只回一句 "Field required"。
    assert "缺少必填键" in message
    assert "config.yaml.example" in message


def test_context_window_must_exceed_max_tokens(tmp_path: Path) -> None:
    """上下文窗口必须大于输出预留，否则输入预算为负、压缩触发比失去意义。"""
    path = _write_config(tmp_path, MINIMAL_CONFIG + "  max_tokens: 65536\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "context_window_tokens" in message
    assert "max_tokens" in message


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
    # 闲聊也过 reflection_node（架构 §5.2），但没有任务证据、也不产生记忆候选
    assert not result["reflection_result"].memory_candidates
    assert result["reflection_result"].result_trust == "uncertain"


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


def test_page_shows_patch_edit_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """patch 输入模式：预览照常展示，并标出应用的 hunk 数（架构 §6.3、T22）。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    target = tmp_path / "note.py"
    target.write_text("value = 1\nother = 0\n", encoding="utf-8", newline="")
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
                        "patch": "@@ -1,2 +1,2 @@\n-value = 1\n+value = 2\n other = 0\n",
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
    assert "+value = 2" in rendered
    captions = "\n".join(element.value for element in page.caption)
    assert "hunk" in captions  # patch 模式的 replaced_count 语义在页面上可见
    assert target.read_text(encoding="utf-8") == "value = 2\nother = 0\n"


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


class _FakeGraph:
    """记录 ``invoke`` 收到的上下文（测试替身，不做任何图逻辑）。"""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def invoke(self, payload: object, *, config: object = None, context: object = None) -> dict:
        self.calls.append(context)
        return {"messages": []}


class _FakeChatApp:
    """只满足 ``turn_workspace`` / ``run_chat_turn`` / ``resume_turn`` 需要的最小面。"""

    def __init__(self, *, app_config, workspace_root: Path, sessions: SessionStore | None) -> None:
        self.app_config = app_config
        self.model = object()
        self.workspace_root = workspace_root
        self.capabilities = object()
        self.graph = _FakeGraph()
        self.checkpointer_enabled = False
        self.sessions = sessions


def _switch_workspace(page: object, path: Path) -> object:
    """在页面上输入工作区绝对路径并点击"切换到此工作区"。"""
    page = page.sidebar.text_input(key="session-workspace-path").set_value(str(path)).run()
    return page.sidebar.button(key="session-workspace-apply").click().run()


def test_page_workspace_switch_keeps_pending_approval_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """验收 4：A 中挂起的审批在 B 不展示，切回 A 后仍可继续恢复（架构 §1.5、§10.6）。"""
    workspace_b = tmp_path / "b"
    workspace_b.mkdir()
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
    _run_page(tmp_path, monkeypatch)  # 建立工作区 A 的会话
    target = tmp_path / "aa.txt"
    target.write_text("别删我", encoding="utf-8", newline="")

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("删除 aa.txt").run()
    assert _has_pending_approval(page)
    assert target.is_file()  # 挂起期间不执行

    # 切到 B：新工作区、新会话，A 的挂起审批不在这里出现
    page = _switch_workspace(page, workspace_b)
    assert not page.exception
    assert not _has_pending_approval(page)
    active = _session_store(tmp_path).active()
    assert active is not None
    assert Path(active.workspace_root) == workspace_b.resolve()
    assert target.is_file()

    # 切回 A：恢复 A 的最近会话，审批重新可见，并且可以继续执行
    page = _switch_workspace(page, tmp_path)
    assert not page.exception
    assert _has_pending_approval(page)

    page.button[0].click().run()  # 批准删除

    assert not page.exception
    assert not target.exists()


def test_page_invalid_workspace_keeps_current_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 5：非法工作区只提示，不改变当前 active 会话（架构 §1.5）。"""
    _write_config(tmp_path, MINIMAL_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    page = _run_page(tmp_path, monkeypatch)
    before = _session_store(tmp_path).active()
    assert before is not None

    page = _switch_workspace(page, tmp_path / "missing-dir")

    assert not page.exception
    assert any("会话操作失败" in element.value for element in page.error)
    store = _session_store(tmp_path)
    active = store.active()
    assert active is not None and active.session_id == before.session_id
    assert len(store.list_sessions()) == 1


def test_turns_use_the_session_workspace(tmp_path: Path, monkeypatch, app_config) -> None:
    """验收 6：``run_chat_turn`` 与 ``resume_turn`` 都用会话绑定的工作区构造上下文。"""
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    database = checkpoint.open_database(
        tmp_path / "data" / "checkpoints.sqlite", workspace_root=workspace_a
    )
    try:
        store = database.sessions
        store.ensure_active(legacy_thread_id=checkpoint.DEFAULT_THREAD_ID)
        session_b = store.switch_workspace(workspace_b)

        captured: list[tuple[Path, Path]] = []
        monkeypatch.setattr(
            web.builder,
            "build_runtime_context",
            lambda **kwargs: captured.append(
                (kwargs["workspace_root"], kwargs["current_cwd"])
            )
            or object(),
        )
        app = _FakeChatApp(
            app_config=app_config, workspace_root=workspace_a.resolve(), sessions=store
        )

        web.run_chat_turn(
            app,
            thread_id=session_b.thread_id,
            user_text="你好",
            session_id=session_b.session_id,
        )
        web.resume_turn(
            app,
            thread_id=session_b.thread_id,
            resume_value={"decision": "approve"},
            session_id=session_b.session_id,
        )
    finally:
        database.connection.close()

    expected = workspace_b.resolve()
    assert captured == [(expected, expected), (expected, expected)]


def test_turn_workspace_falls_back_to_config_without_short_term(tmp_path: Path) -> None:
    """验收 7：关闭短期记忆时没有会话索引，工作区回到配置的启动默认值（架构 §10.6）。"""
    app = _FakeChatApp(
        app_config=None, workspace_root=tmp_path.resolve(), sessions=None
    )

    assert web.current_session(app) is None
    assert (
        web.turn_workspace(app, session_id=None, thread_id=web.DEFAULT_THREAD_ID)
        == tmp_path.resolve()
    )


def test_turn_workspace_rejects_unusable_default_without_short_term(tmp_path: Path) -> None:
    """T14C 复审：关闭短期记忆时，配置默认工作区失效也要失败闭合（每轮都校验）。"""
    missing = tmp_path / "gone"
    app = _FakeChatApp(app_config=None, workspace_root=missing, sessions=None)

    with pytest.raises(web.SessionStoreError):
        web.turn_workspace(app, session_id=None, thread_id=web.DEFAULT_THREAD_ID)


def test_page_temporary_workspace_without_short_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关闭短期记忆时仍可选工作区，但只在本页面生命周期内有效（架构 §10.6）。"""
    workspace_b = tmp_path / "b"
    workspace_b.mkdir()
    _write_config(tmp_path, MINIMAL_CONFIG + "memory:\n  short_term:\n    enabled: false\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")

    page = _run_page(tmp_path, monkeypatch)
    assert not page.exception
    assert not page.error, [element.value for element in page.error]
    assert page.sidebar.text_input, "关闭短期记忆时也必须在侧栏渲染工作区选择器"
    assert not (tmp_path / "data" / "checkpoints.sqlite").exists()  # 没有持久会话索引

    page = _switch_workspace(page, workspace_b)
    assert not page.exception
    captions = [element.value for element in page.sidebar.caption]
    assert any(workspace_b.as_posix() in text for text in captions)

    page.run()  # 普通重绘：页面临时选择保留
    captions = [element.value for element in page.sidebar.caption]
    assert any(workspace_b.as_posix() in text for text in captions)

    # 重新启动服务（新的页面会话）→ 回到配置的默认工作区
    restarted = _run_page(tmp_path, monkeypatch)
    captions = [element.value for element in restarted.sidebar.caption]
    assert not any(workspace_b.as_posix() in text for text in captions)


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
    # 侧栏现在有两个输入框（工作区绝对路径 + 会话重命名），按 key 定位，不依赖顺序
    session_before = _session_store(tmp_path).active()
    assert session_before is not None
    page = (
        page.sidebar.text_input(key=f"session-title-{session_before.session_id}")
        .set_value("我的调研会话")
        .run()
    )
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


# --------------------------------------------------------------------------- T23 流式展示

STREAMING_CONFIG = MINIMAL_CONFIG + "  streaming: true\n"


def test_page_streams_the_final_answer_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """T23：流式的增量只进临时占位符，最终回答在页面上**只出现一次**；重绘不再调用模型。

    "不重复显示"是这套实现的直接结果：占位符在本轮调用结束时清空，随后由 ``render_history``
    按 State 里的那一条回答渲染；因此"增量块"既不会留在页面上，也不会进 Checkpoint。
    """
    _write_config(tmp_path, STREAMING_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    model = model_factory(decision="chat", reply="流式回答内容在这里")
    monkeypatch.setattr(factory, "build_chat_model", lambda _config, _api_key: model)

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("你好").run()

    assert not page.exception
    assert _rendered_text(page).count("流式回答内容在这里") == 1
    calls = model.decision_calls
    assert calls == 1

    # 普通重绘只读状态：不重新调用模型（架构 §1.4）
    page.run()
    assert model.decision_calls == calls
    assert _rendered_text(page).count("流式回答内容在这里") == 1


def test_page_shows_stream_degradation_but_keeps_the_full_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_factory
) -> None:
    """T23：流式中途失败 → 不完整投影被丢弃、回答仍是完整的，并留下可见降级事件。"""
    _write_config(tmp_path, STREAMING_CONFIG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-fake")
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda _config, _api_key: model_factory(
            decision="chat", reply="完整回答内容在这里", fail_stream_after_chunks=1
        ),
    )

    page = _run_page(tmp_path, monkeypatch)
    page.chat_input[0].set_value("你好").run()

    assert not page.exception
    assert "完整回答内容在这里" in _rendered_text(page)
    notices = [element.value for element in page.warning]
    assert any("流式回答降级" in text for text in notices)

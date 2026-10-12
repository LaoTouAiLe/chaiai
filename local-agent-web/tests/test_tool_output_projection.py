"""工具原始结果到模型窗口的证据完整性、错误边界与缓存前缀回归。"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from local_agent.memory.short_term import select_prompt_window
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.file_tools import build_file_tools


def _window(messages, *, threshold=8192, enabled=True):
    return select_prompt_window(
        messages, enabled=enabled, max_messages=100, preserve_recent_turns=8,
        tool_result_keep_chars=threshold, prune_tool_steps=False, prune_tool_results=True,
    ).messages


def _history(text, name="read_file", identity="read"):
    return [HumanMessage(content="只读调查"), AIMessage(content="", tool_calls=[{
        "id": identity, "name": name, "args": {"path": "source.py"},
    }]), ToolMessage(content=text, name=name, tool_call_id=identity)]


def _read(tmp_path, app_config, content, *, start_line=None, end_line=None, max_chars=50000):
    (tmp_path / "source.py").write_bytes(content.encode("utf-8"))
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    tool = next(tool for tool in build_file_tools(policy) if tool.name == "read_file")
    args = {"path": "source.py", "max_chars": max_chars}
    if start_line is not None:
        args["start_line"] = start_line
    if end_line is not None:
        args["end_line"] = end_line
    return tool.invoke(args)


def test_read_tool_projects_exact_original_line_numbers_without_mutating_audit(
    tmp_path, app_config,
):
    raw = _read(tmp_path, app_config, "first\r\nsecond\r\nthird\r\n", start_line=2)
    messages = _history(raw)
    result = json.loads(_window(messages)[-1].content)
    assert result["content"] == "2: second\n3: third"
    assert result["visible_ranges"] == [[2, 3]]
    assert result["omitted_ranges"] == result["partial_lines"] == []
    assert not result["truncated"] and not result["has_more"]
    assert result["next_start_line"] is None
    assert messages[-1].content == raw
    assert json.loads(raw)["content"] == "second\r\nthird\r\n"
    assert _window(messages, enabled=False)[-1].content == raw


def test_large_read_reports_missing_middle_and_targeted_reread_recovers_it(
    tmp_path, app_config,
):
    source = "".join(f"def step_{n}(): return 'evidence-{n}-' + 'x' * 120  # " + "x" * 60 + "\n"
                     for n in range(1, 401))
    raw = _read(tmp_path, app_config, source, max_chars=100000)
    original = json.loads(raw)
    result = json.loads(_window(_history(raw))[-1].content)
    assert original["truncated"] is False and original["last_line"] == 400
    assert result["truncated"] and result["has_more"]
    assert result["visible_ranges"][0][0] == 1
    assert result["visible_ranges"][-1][1] == 400
    assert len(result["visible_ranges"]) == 2
    start, end = result["omitted_ranges"][0]
    assert start <= 150 <= end
    assert result["next_start_line"] == start
    assert result["partial_lines"] == []
    reread = _read(tmp_path, app_config, source, start_line=150, end_line=152)
    found = json.loads(_window(_history(reread))[-1].content)
    assert "150: def step_150()" in found["content"]
    assert found["visible_ranges"] == [[150, 152]]
    assert found["omitted_ranges"] == []
    assert _history(raw)[-1].content == raw


def test_character_limited_read_does_not_skip_incomplete_last_line(tmp_path, app_config):
    raw = _read(tmp_path, app_config, "first\n" + "a" * 200 + "\nlast\n", max_chars=30)
    original = json.loads(raw)
    assert original["next_start_line"] == 2
    assert original["partial_lines"] == [2]
    result = json.loads(_window(_history(raw))[-1].content)
    assert result["partial_lines"] == [2]
    assert result["next_start_line"] == 2
    assert result["has_more"] and result["truncated"]


def test_giant_single_line_is_explicitly_partial_and_keeps_real_number(tmp_path, app_config):
    raw = _read(tmp_path, app_config, "short\n" + "a" * 20000 + "TAIL\n", start_line=2)
    result = json.loads(_window(_history(raw))[-1].content)
    assert "2 [行首片段]:" in result["content"]
    assert "2 [行尾片段]:" in result["content"]
    assert result["content"].endswith("TAIL")
    assert result["partial_lines"] == [2]
    assert result["visible_ranges"] == [[2, 2]]
    assert result["next_start_line"] == 2 and result["has_more"]


@pytest.mark.parametrize("threshold", [128, 512, 4096, 12000])
def test_configured_threshold_controls_read_body_with_honest_ranges(
    tmp_path, app_config, threshold,
):
    raw = _read(tmp_path, app_config, "".join(f"line {n} " + "x" * 99 + "\n"
                                          for n in range(300)))
    result = json.loads(_window(_history(raw), threshold=threshold)[-1].content)
    assert result["truncated"] and result["has_more"]
    assert len(result["content"]) < threshold + 128
    assert result["next_start_line"] is not None


def test_zero_threshold_keeps_all_source_but_adds_line_numbers(tmp_path, app_config):
    raw = _read(tmp_path, app_config, "".join(f"evidence{n}\n" for n in range(1200)))
    result = json.loads(_window(_history(raw), threshold=0)[-1].content)
    assert "1200: evidence1199" in result["content"]
    assert result["omitted_ranges"] == [] and not result["truncated"]


def test_directory_projection_preserves_metadata_and_avoids_repeated_keys(tmp_path, app_config):
    for n in range(80):
        (tmp_path / f"f{n}.py").write_text("data", encoding="utf-8")
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})
    tool = next(tool for tool in build_file_tools(policy) if tool.name == "list_dir")
    raw = tool.invoke({"path": "."})
    history = _history(raw, name="list_dir")
    projected = _window(history)[-1].content
    data, original = json.loads(projected), json.loads(raw)
    assert data["entry_columns"] == ["path", "kind", "size_bytes", "modified_at"]
    for row, item in zip(data["entries"], original["entries"], strict=True):
        assert dict(zip(data["entry_columns"], row, strict=True)) == {
            key: value for key, value in item.items() if key != "name"
        }
    assert data["total_entries"] == 80 and not data["truncated"]
    assert len(projected) < len(raw) * 0.7
    assert history[-1].content == raw


@pytest.mark.parametrize("raw", ["not JSON", "[]", '{"error":"denied"}',
                              '{"content":"text","first_line":2,"last_line":999}'])
def test_invalid_or_error_results_are_not_given_fabricated_line_metadata(raw):
    assert _window(_history(raw))[-1].content == raw


def test_non_file_tool_and_rich_output_are_not_projected_as_read_file():
    raw = json.dumps({"path": "a", "content": "evidence", "first_line": 1,
                      "last_line": 1, "line_count": 1, "is_binary": False})
    assert _window(_history(raw, name="run_shell"))[-1].content == raw
    rich = ToolMessage(content=[{"type": "text", "text": "data"}], tool_call_id="rich")
    assert _window([rich])[-1] is rich


def test_projection_keeps_tool_role_and_identical_history_when_more_steps_append(
    tmp_path, app_config,
):
    raw = _read(tmp_path, app_config, "".join(f"line{n}: " + "x" * 100 + "\n"
                                          for n in range(250)))
    history = _history(raw)
    before = _window(history)
    after = _window([*history, AIMessage(content="answer"), HumanMessage(content="follow up")])
    assert [m.model_dump() for m in after[:len(before)]] == [m.model_dump() for m in before]
    assert isinstance(before[-1], ToolMessage)
    assert before[-1].tool_call_id == "read" and before[-1].name == "read_file"
    assert history[-1].content == raw

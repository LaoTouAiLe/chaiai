"""Citation formatting must use observed paths and preserve raw evidence."""
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from local_agent.context.citations import complete_citation_paths
from local_agent.graph.nodes import final_response_node
from local_agent.graph.projection import message_id


def read(path, name="read_file"):
    return ToolMessage(name=name, tool_call_id=path, content=json.dumps({"path": path, "content": "code"}))


def test_unique_suffix_ellipsis_and_explicit_continuation_are_completed():
    messages = [read("local-agent-web/src/local_agent/rag/search.py")]
    assert complete_citation_paths("search.py:7-9、:12；.../rag/search.py:20，同文件 25", messages) == (
        "local-agent-web/src/local_agent/rag/search.py:7-9、local-agent-web/src/local_agent/rag/search.py:12；"
        "local-agent-web/src/local_agent/rag/search.py:20，local-agent-web/src/local_agent/rag/search.py:25"
    )


def test_ambiguous_and_unobserved_files_are_not_guessed():
    messages = [read("a/config.py"), read("b/config.py")]
    text = "config.py:5；missing.py:9；a/config.py:7"
    assert complete_citation_paths(text, messages) == text


def test_untrusted_web_content_and_external_paths_do_not_authorize_completion():
    messages = [read("src/config.py", "web_fetch"), read("E:/other/secret.py"), read("../config.py")]
    text = "config.py:5；secret.py:9"
    assert complete_citation_paths(text, messages) == text


def test_line_numbers_and_unrelated_colons_are_preserved():
    text = "config.py:999，配置值:42"
    assert complete_citation_paths(text, [read("src/config.py")]) == "src/config.py:999，配置值:42"


def test_final_projection_keeps_original_messages_and_ignores_prior_turn_paths():
    original = AIMessage(content="config.py:5", id="answer")
    messages = [read("old/config.py"), HumanMessage(content="read", id="user"), read("src/config.py"), original]
    result = final_response_node({"messages": messages, "run_user_message_id": "user", "mode": "tool"})
    assert result["final_response"] == "src/config.py:5"
    assert result["reply_overrides"] == {message_id(original): "src/config.py:5"}
    assert original.content == "config.py:5"
    assert len(messages) == 4


def test_example_and_toml_paths_and_chinese_continuation():
    messages = [read("config.yaml.example"), read("pyproject.toml"), read("README.md")]
    text = "config.yaml.example:190；pyproject.toml:6-8；README.md:3 和 :88"
    assert complete_citation_paths(text, messages) == (
        "config.yaml.example:190；pyproject.toml:6-8；README.md:3 和 README.md:88"
    )


def test_grep_content_authorizes_paths_but_arguments_and_counts_do_not():
    grep = ToolMessage(name="grep", tool_call_id="g", content=json.dumps({"path": "elsewhere.py",
        "matches": [{"path": "src/local_agent/llm/embedding.py", "line_number": 27,
                     "text": "L2 normalization"},
                    {"path": "../secret.py", "line_number": 1, "text": "external"}],
        "hits": [{"path": "counts.py", "count": 2}]}))
    assert complete_citation_paths("llm/embedding.py:272；embedding.py:27；elsewhere.py:1；counts.py:1；secret.py:1", [grep]) == (
        "src/local_agent/llm/embedding.py:272；src/local_agent/llm/embedding.py:27；elsewhere.py:1；counts.py:1；secret.py:1"
    )


def test_backticked_abbreviations_use_only_the_current_paragraph_file():
    messages = [read("src/rag/search.py"), read("src/tools/web_tools.py")]
    text = "search.py:10；正文退化（`:20-27`、`:77-94`）。配置值:42\n\n网页（`:20`）\n\nweb_tools.py:5；抓正文（`:93-98`、`:196-203`）。"
    assert complete_citation_paths(text, messages) == (
        "src/rag/search.py:10；正文退化（`src/rag/search.py:20-27`、`src/rag/search.py:77-94`）。配置值:42\n\n"
        "网页（`:20`）\n\nsrc/tools/web_tools.py:5；抓正文（`src/tools/web_tools.py:93-98`、`src/tools/web_tools.py:196-203`）。")
    assert complete_citation_paths("search.py:10；missing.py 后文（`:20`）", messages) == (
        "src/rag/search.py:10；missing.py 后文（`:20`）")
    assert complete_citation_paths("search.py:10；时间 `12:20`，网址 https://site:80", messages) == (
        "src/rag/search.py:10；时间 `12:20`，网址 https://site:80")


def test_numeric_citation_lists_expand_only_adjacent_integer_ranges():
    messages = [read("src/memory/recall.py")]
    text = "recall.py:20-22、54、59、146-177；recall.py:147-153、70-77；recall.py:20, 0.5, 2000ms；unknown.py:3、5"
    assert complete_citation_paths(text, messages) == (
        "src/memory/recall.py:20-22、src/memory/recall.py:54、src/memory/recall.py:59、src/memory/recall.py:146-177；"
        "src/memory/recall.py:147-153、src/memory/recall.py:70-77；src/memory/recall.py:20, 0.5, 2000ms；unknown.py:3、5")
    assert complete_citation_paths("recall.py:20`、`54-59`；数值 50；日期 2026-10-10", messages) == (
        "src/memory/recall.py:20`、`src/memory/recall.py:54-59`；数值 50；日期 2026-10-10")
    assert complete_citation_paths("recall.py:20, 5 tokens, 12:20, 2026-10-10", messages) == (
        "src/memory/recall.py:20, 5 tokens, 12:20, 2026-10-10")

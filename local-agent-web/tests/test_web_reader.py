import json

import pytest

from local_agent.config import WebAccessConfig
from local_agent.tools import web_tools
from local_agent.tools.web_errors import WebAccessError
from local_agent.tools.web_reader import content_version, extract_document, select_window


def test_html_preserves_table_order_links_and_removes_navigation() -> None:
    html = '<html><head><title>榜单</title></head><body><nav>广告</nav><main><h1>榜单</h1><table><tr><td>1</td><td><a href="/one">第一项</a></td></tr><tr><td>2</td><td>第二项</td></tr></table><script>secret()</script></main></body></html>'
    title, body, structure = extract_document(html, "text/html", "https://arbitrary.example/board")
    assert title == "榜单" and structure is None
    assert "广告" not in body and "secret" not in body
    assert "https://arbitrary.example/one" in body
    assert body.index("第一项") < body.index("第二项") and " | " in body


def test_plain_text_angle_brackets_are_not_html() -> None:
    assert extract_document("<name>literal</name>", "text/plain", "https://example.com/")[1] == "<name>literal</name>"


def test_json_pointer_and_projection_work_without_business_field_rules() -> None:
    raw = json.dumps({"response": {"arbitrary/key": [
        {"heading": "one", "metrics": {"count": 9007199254740993}, "noise": "x" * 3000},
        {"heading": "two", "metrics": {"count": 7}},
    ]}})
    _, body, structure = extract_document(raw, "application/json", "https://unknown.example/",
        json_pointer="/response/arbitrary~1key", json_fields=["/heading", "/metrics/count"])
    values = json.loads(body)
    assert values == [{"/heading": "one", "/metrics/count": 9007199254740993}, {"/heading": "two", "/metrics/count": 7}]
    assert "noise" not in body and "数组，2 项" in (structure or "")


def test_json_untouched_without_projection_and_missing_path_is_explicit() -> None:
    raw = '{"code":404,"items":[]}'
    assert extract_document(raw, "application/json", "https://example.com/")[1] == raw
    with pytest.raises(WebAccessError, match="不存在"):
        extract_document(raw, "application/json", "https://example.com/", json_pointer="/invented")


def test_query_excerpt_finds_content_beyond_the_prefix_without_changing_source_order() -> None:
    body = "unrelated\n" * 400 + "MCP connects external tools.\n" + "more\n" * 400
    text, start, next_start, selected = select_window(body, query="MCP tools", start=0, limit=700)
    assert selected and "MCP connects" in text and len(text) <= 700
    assert start == next_start == 0


def test_fetch_continuation_requires_the_same_content_version() -> None:
    body = "line\n" * 300
    transport = lambda url, *_: web_tools.WebResponse(url, 200, "text/plain", body)
    config = WebAccessConfig(max_chars=500)
    first = web_tools.fetch("https://93.184.216.34/", config=config, transport=transport)
    second = web_tools.fetch("https://93.184.216.34/", config=config, transport=transport,
                            start_char=first.next_start_char or 0, expected_version=first.content_version)
    assert first.text + second.text == body[:1000]
    assert first.content_version == content_version(body.strip())
    with pytest.raises(WebAccessError, match="已变化"):
        web_tools.fetch("https://93.184.216.34/", config=config, transport=transport, expected_version="old")


def test_json_projection_preserves_high_precision_decimal_values() -> None:
    raw = '{"payload":[{"amount":0.1234567890123456789012345,"rank":9007199254740993}]}'
    _, body, _ = extract_document(raw, "application/json", "https://unknown.example/",
                                json_pointer="/payload", json_fields=["/amount", "/rank"])
    assert "0.1234567890123456789012345" in body and "9007199254740993" in body
    assert '"0.1234567890123456789012345"' not in body


def test_query_can_find_a_match_near_the_end_of_one_long_line() -> None:
    body = "unrelated " * 1000 + "needle important fact" + " trailing" * 100
    text, _, _, selected = select_window(body, query="needle", start=0, limit=500)
    assert selected and "needle important fact" in text


def test_invalid_json_selector_is_rejected_before_any_network_request() -> None:
    def unexpected(*_):
        raise AssertionError("invalid pointer caused a network call")

    with pytest.raises(WebAccessError, match="json_pointer"):
        web_tools.fetch("https://93.184.216.34/", config=WebAccessConfig(), transport=unexpected,
                        json_pointer="null")

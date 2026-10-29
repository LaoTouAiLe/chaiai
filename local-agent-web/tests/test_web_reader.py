import json

import pytest

from local_agent.config import WebAccessConfig
from local_agent.tools import web_tools
from local_agent.tools.web_errors import WebAccessError
from local_agent.tools.web_reader import content_version, extract_document, select_window


def test_html_preserves_table_order_links_and_removes_navigation() -> None:
    html = '<html><head><title>榜单</title></head><body><nav>广告</nav><main><h1>榜单</h1><table><tr><td>1</td><td><a href="/one">第一项</a></td></tr><tr><td>2</td><td>第二项</td></tr></table><script>secret()</script></main></body></html>'
    title, body, structure, _note = extract_document(html, "text/html", "https://arbitrary.example/board")
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
    _, body, structure, _ = extract_document(raw, "application/json", "https://unknown.example/",
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
    _, body, _, _ = extract_document(raw, "application/json", "https://unknown.example/",
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


# ---- 正文抽取库（trafilatura）与回退决策（2026-10-28）-----------------------


def _article_html(words: int = 200) -> str:
    """一段带导航/侧栏/正文的真实形态页面：正文够长，噪声够明显。"""
    body = "".join(f"<p>第{i}段：S16 瑞士轮的赛果与后续对阵说明，属于正文的一部分。</p>" for i in range(words))
    return (
        "<html><head><title>赛果</title></head><body>"
        "<nav>首页 彩票 天气 邮箱 登录</nav>"
        "<div class='sidebar'>热门推荐 广告位</div>"
        f"<article><h1>S16 瑞士轮赛果</h1>{body}</article>"
        "<footer>版权声明 联系我们</footer></body></html>"
    )


def test_html_body_prefers_extracted_markdown_and_drops_boilerplate(monkeypatch) -> None:
    """抽取库给出正文时：用它（Markdown、无导航），并且不带回退说明。"""
    extracted = "## S16 瑞士轮赛果\n\n" + "正文段落。" * 800
    monkeypatch.setattr(
        "local_agent.tools.web_reader._markdown_from_html", lambda raw, url: extracted
    )
    _title, body, structure, note = extract_document(_article_html(), "text/html", "https://a.example/x")
    assert body.startswith("## S16 瑞士轮赛果")
    assert "热门推荐" not in body and structure is None and note is None


@pytest.mark.parametrize(
    ("markdown", "reason"),
    [
        (None, "不可用"),
        ("太短了", "过短"),
        ("正文段落。" * 100, "模板"),  # 500 字：过得了"过短"，但远不到原文的 25%
    ],
)
def test_html_body_falls_back_with_an_explicit_note(monkeypatch, markdown, reason) -> None:
    """抽不出/抽太少/抽太狠：回退到内置解析，并**在 note 里说明**，不静默降级。"""
    monkeypatch.setattr(
        "local_agent.tools.web_reader._markdown_from_html", lambda raw, url: markdown
    )
    title, body, _structure, note = extract_document(_article_html(), "text/html", "https://a.example/x")
    assert "回退到内置解析" in (note or "") and reason in (note or "")
    assert "热门推荐" in body  # 回退后确实是旧的全量文本
    assert title == "赛果"


def test_tiny_pages_do_not_get_a_fallback_note(monkeypatch) -> None:
    """整页就一两行时回退与否结果一样：不加说明，避免每条抓取都挂一句噪声。"""
    monkeypatch.setattr(
        "local_agent.tools.web_reader._markdown_from_html", lambda raw, url: None
    )
    _, body, _, note = extract_document("<html><body>短</body></html>", "text/html", "https://a.example/x")
    assert body == "短" and note is None

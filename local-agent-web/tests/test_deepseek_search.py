"""DeepSeek 搜索后端（2026-10-26）：新增后端不得改变已有后端的选择结果。"""

from datetime import datetime, timezone

import pytest

from local_agent.config import WebAccessConfig
from local_agent.tools.deepseek_search import with_filters
from local_agent.tools.web_tools import WebAccessError, WebResponse, search

_FIXED_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def _transport(payload, key, *_):
    seen_requests.append((payload, key))
    return {"results": [{
        "title": "DeepSeek 文档", "url": "https://api-docs.deepseek.com/zh-cn/guides/search",
        "summary": "长摘要" * 20, "snippet": "短摘要", "date": "2026-10-01T00:00:00+00:00",
        "rerank_score": 2.4, "authority": 3,
    }]}


#: 由各用例自己清空：deepseek 的 transport 契约与 Tavily 同一形状，记录实际发出去的 payload。
seen_requests: list[tuple[dict, str]] = []


def test_deepseek_backend_maps_summary_title_and_date() -> None:
    seen_requests.clear()
    result = search("搜索语法", config=WebAccessConfig(provider="deepseek"),
                    environ={"DEEPSEEK_API_KEY": "ds-from-env"}, api_transport=_transport)

    assert result.provider == "deepseek"
    hit = result.results[0]
    assert hit.title == "DeepSeek 文档"
    # summary 优先：600–800 字的摘要比 snippet 有用，也因此这个后端不做本地预抓取。
    assert hit.snippet.startswith("长摘要")
    assert hit.published_at == "2026-10-01T00:00:00+00:00"
    assert "ds-from-env" not in result.model_dump_json()


def test_deepseek_backend_rewrites_time_and_domain_filters_into_query() -> None:
    seen_requests.clear()
    result = search("搜索语法", config=WebAccessConfig(provider="deepseek"),
                    environ={"DEEPSEEK_API_KEY": "fake"}, api_transport=_transport,
                    time_range="month", domains=["docs.deepseek.com"])

    payload = seen_requests[0][0]
    # 站点在前、时间在后；日期按 after: 语法拼进查询，而不是另设字段。
    assert payload["query"].startswith("搜索语法 (site:docs.deepseek.com) after:")
    assert payload["need_content"] is False
    # 报出来的 executed_query 必须与真正发出去的一致（跨零点也不许差一天）。
    assert result.executed_query == payload["query"]


def test_explicit_deepseek_without_key_does_not_silently_fall_back() -> None:
    with pytest.raises(WebAccessError, match="DeepSeek"):
        search("test", config=WebAccessConfig(provider="deepseek"), environ={})


def test_auto_still_ignores_deepseek_credentials() -> None:
    """``auto`` 的口径不变：只认 Tavily。录了 DeepSeek 密钥不等于换了后端。"""
    html = '<li class="b_algo"><h2><a href="https://example.com/">test</a></h2></li>'
    result = search("test", config=WebAccessConfig(), environ={"DEEPSEEK_API_KEY": "fake"},
                    transport=lambda url, *_: WebResponse(url, 200, "text/html", html))
    assert result.provider == "bing_html"


def test_empty_or_unparsable_results_are_not_success() -> None:
    with pytest.raises(WebAccessError):
        search("test", config=WebAccessConfig(provider="deepseek"), environ={"DEEPSEEK_API_KEY": "fake"},
               api_transport=lambda *_: {"results": []})
    with pytest.raises(WebAccessError):
        search("test", config=WebAccessConfig(provider="deepseek"), environ={"DEEPSEEK_API_KEY": "fake"},
               api_transport=lambda *_: {"results": [{"title": "bad", "url": "http://127.0.0.1/"}]})


def test_filter_rewriting_is_a_pure_mapping() -> None:
    assert with_filters("q", time_range=None, domains=[]) == "q"
    assert with_filters("q", time_range="day", domains=[], now=_FIXED_NOW) == "q after:2026-10-25"
    assert (with_filters("q", time_range="year", domains=["a.com", "b.com"], now=_FIXED_NOW)
            == "q (site:a.com OR site:b.com) after:2025-10-26")


def test_transport_pins_endpoint_and_auth_header(monkeypatch) -> None:
    from local_agent.tools import web_tools

    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return b'{"results":[]}'

    class Opener:
        def open(self, request, **_):
            seen.append(request)
            return Response()

    monkeypatch.setattr(web_tools, "build_opener", lambda *_: Opener())
    web_tools._deepseek_transport({"query": "test"}, "fake-credential", 1, "test-agent")

    assert seen[0].full_url == "https://api.deepseek.com/v1/tools/search"
    assert seen[0].get_method() == "POST"
    assert seen[0].get_header("Authorization") == "Bearer fake-credential"
    assert b"fake-credential" not in seen[0].data

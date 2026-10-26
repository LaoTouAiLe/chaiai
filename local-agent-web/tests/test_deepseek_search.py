"""DeepSeek 搜索后端（2026-10-26）：新增后端不得改变已有后端的选择结果。"""

from datetime import datetime, timezone

import pytest

from local_agent.config import WebAccessConfig
from local_agent.tools.deepseek_search import fitted_query, with_filters
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
    # **不加括号**：2026-10-26 实测，加了括号同一 query 会变成 0 条结果。
    assert payload["query"].startswith("搜索语法 site:docs.deepseek.com after:")
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


def test_empty_results_are_a_normal_answer_not_a_failure() -> None:
    """没有匹配时官方返回 200 + 空数组且不计费（2026-10-26 实测：site:一个不存在的域 → 0 条）。

    这里必须给空列表：抛错会让模型以为搜索坏了，接着换后端或反复重试。
    """
    result = search("test", config=WebAccessConfig(provider="deepseek"),
                    environ={"DEEPSEEK_API_KEY": "fake"}, api_transport=lambda *_: {"results": []})

    assert result.results == []


def test_unparsable_payload_is_still_a_failure() -> None:
    """结构不对（``results`` 不是数组）才是失败：那说明对端换了协议，不能当"没搜到"。"""
    with pytest.raises(WebAccessError):
        search("test", config=WebAccessConfig(provider="deepseek"),
               environ={"DEEPSEEK_API_KEY": "fake"},
               api_transport=lambda *_: {"results": "oops"})


def test_results_without_usable_urls_yield_no_hits() -> None:
    """全部条目都被 SSRF 边界挡掉时同样是"没搜到"，不是错误。"""
    result = search("test", config=WebAccessConfig(provider="deepseek"),
                    environ={"DEEPSEEK_API_KEY": "fake"},
                    api_transport=lambda *_: {"results": [{"title": "bad", "url": "http://127.0.0.1/"}]})

    assert result.results == []


def test_filter_rewriting_is_a_pure_mapping() -> None:
    assert with_filters("q", time_range=None, domains=[]) == "q"
    assert with_filters("q", time_range="day", domains=[], now=_FIXED_NOW) == "q after:2026-10-25"
    # 无括号：加括号会让站内筛选整体失效（2026-10-26 实测为 0 条结果）。
    assert (with_filters("q", time_range="year", domains=["a.com", "b.com"], now=_FIXED_NOW)
            == "q site:a.com OR site:b.com after:2025-10-26")


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

def test_long_query_is_trimmed_without_cutting_the_operators() -> None:
    """官方 query 上限 128 字符：压缩只动基础查询词，`site:` 与 `after:` 必须完好。

    直接截整串的话，掐掉的会是末尾的 `after:`——那等于悄悄取消了时间筛选。
    """
    text = fitted_query(
        "长" * 200, time_range="month", domains=["a.example", "b.example"], now=_FIXED_NOW,
    )

    assert len(text) <= 128
    # 固定 now=2026-10-26，回溯 30 天 ⇒ after:2026-09-26（时间筛选永远在末尾）
    assert text.endswith("after:2026-09-26")
    assert "site:a.example" in text and "site:b.example" in text

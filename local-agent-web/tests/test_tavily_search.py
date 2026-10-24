import pytest

from local_agent.config import WebAccessConfig
from local_agent.tools.web_tools import WebAccessError, search


def test_explicit_key_argument_wins_over_the_environment_and_selects_auto_backend() -> None:
    """设置库里取出的密钥（``api_key``）优先于环境变量，且参与 ``auto`` 的后端判定。

    这是"页面录入的密钥真的生效"这条链路的下半段：装配时把密钥传进 ``search`` 后，即使环境里
    **没有** ``TAVILY_API_KEY``，``provider=auto`` 也必须走 Tavily——否则用户录了密钥却还是被
    当成"没配"。
    """
    seen = []

    def transport(payload, key, *_):
        seen.append(key)
        return {"results": [{"title": "Official", "url": "https://docs.example.com/", "content": "snippet"}]}

    result = search("test query", config=WebAccessConfig(),
                    api_transport=transport, environ={}, api_key="tvly-from-store")
    assert result.provider == "tavily" and seen == ["tvly-from-store"]
    assert "tvly-from-store" not in result.model_dump_json()


def test_auto_uses_tavily_with_credentials_and_passes_structured_filters() -> None:
    seen = []

    def transport(payload, key, *_):
        seen.append((payload, key))
        return {"results": [{"title": "Official", "url": "https://docs.example.com/", "content": "Relevant snippet", "published_date": "2026-10-14"}]}

    result = search("test query", config=WebAccessConfig(), api_transport=transport,
                    environ={"TAVILY_API_KEY": "fake-search-secret"}, time_range="day", domains=["docs.example.com"])
    assert result.provider == "tavily"
    assert seen[0][0]["include_domains"] == ["docs.example.com"] and seen[0][0]["time_range"] == "day"
    assert result.results[0].source_id.startswith("web-")
    assert result.results[0].published_at == "2026-10-14"
    assert "fake-search-secret" not in result.model_dump_json()


def test_explicit_tavily_without_key_does_not_silently_fall_back() -> None:
    with pytest.raises(WebAccessError, match="凭据"):
        search("test", config=WebAccessConfig(provider="tavily"), environ={})


@pytest.mark.parametrize("response", [{"error": "missing"}, {"results": []}, {"results": [{"title": "bad", "url": "http://127.0.0.1/"}]}])
def test_bad_or_private_api_results_do_not_become_successful_sources(response) -> None:
    with pytest.raises(WebAccessError):
        search("test", config=WebAccessConfig(provider="tavily"), environ={"TAVILY_API_KEY": "fake"},
               api_transport=lambda *_: response)


def test_html_compatibility_reports_the_real_backend_and_filter_limit() -> None:
    html = '<li class="b_algo"><h2><a href="https://example.com/">test</a></h2></li>'
    from local_agent.tools.web_tools import WebResponse
    result = search("test", config=WebAccessConfig(), environ={}, transport=lambda url, *_: WebResponse(url, 200, "text/html", html))
    assert result.provider == "bing_html" and "兼容" in (result.note or "")
    with pytest.raises(WebAccessError, match="时间筛选"):
        search("test", config=WebAccessConfig(provider="bing_html"), time_range="day", environ={})


def test_api_transport_pins_endpoint_auth_header_and_disables_redirects(monkeypatch) -> None:
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

    def build(*handlers):
        assert any(isinstance(handler, web_tools._NoApiRedirect) for handler in handlers)
        return Opener()

    monkeypatch.setattr(web_tools, "build_opener", build)
    web_tools._tavily_transport({"query": "test"}, "fake-credential", 1, "test-agent")
    assert seen[0].full_url == "https://api.tavily.com/search"
    assert seen[0].get_method() == "POST"
    assert seen[0].get_header("Authorization") == "Bearer fake-credential"
    assert b"fake-credential" not in seen[0].data


def test_api_http_errors_do_not_echo_provider_body_or_credentials(monkeypatch) -> None:
    from urllib.error import HTTPError

    from local_agent.tools import web_tools

    class Opener:
        def open(self, *_args, **_kwargs):
            raise HTTPError("https://api.tavily.com/search", 401, "echo fake-credential", {}, None)

    monkeypatch.setattr(web_tools, "build_opener", lambda *_: Opener())
    with pytest.raises(WebAccessError, match="401") as error:
        web_tools._tavily_transport({"query": "test"}, "fake-credential", 1, "test-agent")
    assert "fake-credential" not in str(error.value)

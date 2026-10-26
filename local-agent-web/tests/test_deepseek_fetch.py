"""DeepSeek 抓取后端（2026-10-26）：默认仍走本机，选远端时如实交代来源、失败不回退。"""

import pytest

from local_agent.config import WebAccessConfig
from local_agent.tools import web_tools
from local_agent.tools.result_types import WebSearchHit
from local_agent.tools.web_errors import WebAccessError
from local_agent.tools.web_tools import WebResponse, fetch

_REMOTE_ONLY = WebAccessConfig(provider="bing_html", fetch_provider="deepseek")


def _remote(response=None, *, record=None):
    """远端 transport 桩：默认回一份带标题的 Markdown 正文。"""

    def transport(url, key, timeout, user_agent):
        if record is not None:
            record.append((url, key, timeout, user_agent))
        return response if response is not None else {
            "title": "示例页面", "content": "# 示例页面\n\n正文第一段。",
        }

    return transport


def test_remote_fetch_maps_title_and_marks_the_source() -> None:
    result = fetch(
        "https://example.com/a", config=_REMOTE_ONLY, api_key="ds-key",
        api_fetch_transport=_remote(),
    )

    assert result.title == "示例页面"
    assert "正文第一段" in result.text
    # 远端不返回最终地址：不能凭空造一个，也不该把请求地址当成"跟随重定向后的地址"
    assert result.final_url is None
    assert result.json_structure is None
    # 来源必须写进 note：模型与用户都要知道这段正文不是本机读的
    assert "DeepSeek 抓取" in (result.note or "")
    assert "未在本地校验" in (result.note or "")
    assert result.content_version


def test_remote_fetch_does_not_fall_back_to_the_local_transport() -> None:
    """选了远端就按远端的结果报告：失败时不许偷偷用本机再抓一次。"""

    def local_transport(*_args):
        raise AssertionError("远端失败时不该调用本机 transport")

    def failing_remote(*_args):
        raise WebAccessError("DeepSeek 抓取服务返回 HTTP 404，目标不可用")

    with pytest.raises(WebAccessError, match="DeepSeek 抓取"):
        fetch(
            "https://example.com/a", config=_REMOTE_ONLY, api_key="ds-key",
            transport=local_transport, api_fetch_transport=failing_remote,
        )


@pytest.mark.parametrize("response", [{}, {"title": "t", "content": "   "}])
def test_remote_fetch_without_body_is_not_a_success(response) -> None:
    with pytest.raises(WebAccessError, match="DeepSeek 没有返回可读取正文"):
        fetch("https://example.com/a", config=_REMOTE_ONLY, api_key="ds-key",
              api_fetch_transport=_remote(response))


def test_remote_fetch_without_credentials_fails_closed() -> None:
    with pytest.raises(WebAccessError, match="凭据"):
        fetch("https://example.com/a", config=_REMOTE_ONLY, api_key="")


def test_remote_fetch_rejects_json_pointer_instead_of_silently_ignoring_it() -> None:
    with pytest.raises(WebAccessError, match="json_pointer"):
        fetch("https://example.com/a", config=_REMOTE_ONLY, api_key="ds-key",
              api_fetch_transport=_remote(), json_pointer="/data")


def test_local_backend_is_untouched_by_default() -> None:
    """默认 ``fetch_provider="local"``：加这个开关不改任何已有行为。"""
    html = "<html><head><title>本机</title></head><body><p>本机读到的正文</p></body></html>"
    result = fetch(
        "https://example.com/a", config=WebAccessConfig(),
        transport=lambda url, *_: WebResponse(url, 200, "text/html", html),
        api_fetch_transport=lambda *_args: pytest.fail("默认不该走远端抓取"),
    )
    assert "本机读到的正文" in result.text
    assert result.note is None


def test_fetch_transport_posts_url_to_the_pinned_endpoint(monkeypatch) -> None:
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _):
            return b'{"title":"t","content":"c"}'

    class Opener:
        def open(self, request, **_):
            seen.append(request)
            return Response()

    monkeypatch.setattr(web_tools, "build_opener", lambda *_: Opener())
    web_tools._deepseek_fetch_transport("https://example.com/a", "fake-credential", 1, "test-agent")

    assert seen[0].full_url == "https://api.deepseek.com/v1/tools/fetch"
    assert seen[0].get_header("Authorization") == "Bearer fake-credential"
    assert b"example.com" in seen[0].data and b"fake-credential" not in seen[0].data


def test_prefetch_never_uses_the_remote_fetcher(monkeypatch) -> None:
    """预抓取固定走本机：否则每次搜索都会悄悄多出几条按次计费的远端请求。"""
    remote_calls = []
    monkeypatch.setattr(
        web_tools, "_deepseek_fetch_transport",
        lambda *args: remote_calls.append(args) or {"title": "t", "content": "c"},
    )
    html = "<html><body><p>本机预抓取到的正文</p></body></html>"
    hits = [WebSearchHit(title="t", url="https://example.com/a", source="example.com", snippet="原摘要")]

    enriched, filled = web_tools._prefetch_snippets(
        hits, count=1, config=WebAccessConfig(fetch_provider="deepseek"),
        transport=lambda url, *_: WebResponse(url, 200, "text/html", html), resolver=None,
    )

    assert remote_calls == []
    assert filled == 1 and enriched[0].snippet.startswith("本机预抓取到的正文")

def test_remote_fetch_rejects_urls_with_embedded_credentials() -> None:
    """URL 里带用户名/密码时**不外发**：那等于把凭据一起交给对方，官方也不接受这种地址。"""
    with pytest.raises(WebAccessError, match="用户名或密码"):
        fetch(
            "https://user:secret@example.com/a", config=_REMOTE_ONLY, api_key="ds-key",
            api_fetch_transport=_remote(),
        )

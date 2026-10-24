"""Tavily 的协议适配；不按被搜索网站作业务处理。"""

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from local_agent.tools.result_types import WebSearchHit
from local_agent.tools.web_errors import WebAccessError
from local_agent.tools.web_identity import source_id

ApiTransport = Callable[[Mapping[str, Any], str, float, str], Mapping[str, Any]]


def tavily_search(
    query: str, *, key: str, limit: int, timeout: float, user_agent: str,
    time_range: str | None, domains: list[str], transport: ApiTransport,
    validate_url: Callable[[str], str],
) -> list[WebSearchHit]:
    payload: dict[str, Any] = {
        "query": query, "max_results": limit, "search_depth": "basic",
        "include_answer": False, "include_raw_content": False,
    }
    if time_range:
        payload["time_range"] = time_range
    if domains:
        payload["include_domains"] = domains
    response = transport(payload, key, timeout, user_agent)
    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        raise WebAccessError("Tavily 未返回可识别的搜索结果，不能当作搜索成功")
    hits: list[WebSearchHit] = []
    seen: set[str] = set()
    for item in raw_results[:limit]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        try:
            url = validate_url(item["url"])
        except (WebAccessError, ValueError):
            continue
        title = item.get("title")
        if url in seen or not isinstance(title, str) or not title.strip():
            continue
        seen.add(url)
        snippet = item.get("content")
        date = item.get("published_date")
        hits.append(WebSearchHit(
            title=title.strip()[:500], url=url, source=urlsplit(url).netloc,
            snippet=snippet[:2000] if isinstance(snippet, str) else "",
            published_at=date[:100] if isinstance(date, str) else None,
            source_id=source_id(url),
        ))
    if not hits:
        raise WebAccessError("Tavily 没有返回可用来源，本次未取得搜索结果")
    return hits

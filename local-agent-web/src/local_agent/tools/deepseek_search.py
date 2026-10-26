"""DeepSeek 网页搜索（``/v1/tools/search``）的协议适配；不按被搜索网站作业务处理。

与 :mod:`local_agent.tools.tavily_search` 同一形状（请求 → ``WebSearchHit`` 列表，失败抛
``WebAccessError``），差别只在返回字段与筛选方式：

- 摘要取 ``summary``（官方定位 600–800 字符、"适合给模型阅读"），只在它缺失时才退到 ``snippet``；
  因此这个后端沿用 Tavily 的做法**不做本地预抓取**——再抓一遍正文首段是白花时间。
- 时间与站点限定不是独立参数，而是**搜索语法**（``after:`` / ``site:``）拼进查询，
  与官方文档的透传规则一致。
- 结果顺序按服务端的 ``rerank_score`` 排好，这里**不重排也不过滤**：契约是"按相关度排序、
  不替用户丢结果"，跳过的只有解析不成 URL 的条目。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from local_agent.tools.result_types import WebSearchHit
from local_agent.tools.tavily_search import ApiTransport
from local_agent.tools.web_errors import WebAccessError
from local_agent.tools.web_identity import source_id

#: ``time_range`` → 回溯天数。DeepSeek 只认 ``after:YYYY-MM-DD``，所以把档位换算成日期。
_RANGE_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30, "year": 365}


def with_filters(
    query: str, *, time_range: str | None, domains: list[str], now: datetime | None = None
) -> str:
    """把档位与域名改写成 DeepSeek 认识的搜索语法；顺序固定为 域名 → 时间。"""
    parts = [query]
    if domains:
        parts.append("(" + " OR ".join(f"site:{domain}" for domain in domains) + ")")
    if time_range:
        days = _RANGE_DAYS[time_range]
        since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
        parts.append(f"after:{since.date().isoformat()}")
    return " ".join(parts)


def deepseek_search(
    query: str, *, key: str, limit: int, timeout: float, user_agent: str,
    time_range: str | None, domains: list[str], transport: ApiTransport,
    validate_url: Callable[[str], str], now: datetime | None = None,
) -> list[WebSearchHit]:
    """检索并返回结构化来源。

    Args:
        now: 注入的当前时间（测试用），只有 ``time_range`` 换算日期时会用到。
        validate_url: 与 Tavily 同一条：只做 scheme 形状校验，不做 DNS 解析。
    """
    payload: dict[str, Any] = {
        "query": with_filters(query, time_range=time_range, domains=domains, now=now),
        "count": limit,
        # 正文由 web_fetch 单独取：搜索阶段只要摘要，省响应体积也省一次按次计费后的无用载荷。
        "need_content": False,
    }
    response = transport(payload, key, timeout, user_agent)
    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        raise WebAccessError("DeepSeek 未返回可识别的搜索结果，不能当作搜索成功")
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
        snippet = item.get("summary")
        if not (isinstance(snippet, str) and snippet.strip()):
            snippet = item.get("snippet")
        date = item.get("date")
        hits.append(WebSearchHit(
            title=title.strip()[:500], url=url, source=urlsplit(url).netloc,
            snippet=snippet[:2000] if isinstance(snippet, str) else "",
            published_at=date[:100] if isinstance(date, str) else None,
            source_id=source_id(url),
        ))
    if not hits:
        raise WebAccessError("DeepSeek 没有返回可用来源，本次未取得搜索结果")
    return hits

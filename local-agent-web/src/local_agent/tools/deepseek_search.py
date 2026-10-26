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

#: 官方上限：``count`` 1～50（超出直接 400，错误码 `invalid_request_error`），
#: ``query`` 1～128 个 Unicode 字符。两者都不能由用户配置直接顶上去。
_MAX_COUNT = 50
_MAX_QUERY_CHARS = 128


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


def fitted_query(
    query: str, *, time_range: str | None, domains: list[str], now: datetime | None = None,
) -> str:
    """拼好筛选语法后压到官方的 128 字符上限。

    **不能直接截整串**：`after:2026-10-25` 与 `site:` 是操作符，从后面截会把它们切坏。
    所以先算出后缀（筛选项），只压缩基础查询词；后缀本身超长就如实报错，不发一个必然 400 的请求。
    """
    suffix = with_filters("", time_range=time_range, domains=domains, now=now).strip()
    base = (query or "").strip()
    if not suffix:
        return base[:_MAX_QUERY_CHARS]
    room = _MAX_QUERY_CHARS - len(suffix) - 1
    if room < 1:
        raise WebAccessError(
            f"筛选条件过长（{len(suffix)} 字符），压不进 128 字符的查询上限；请减少站点或改用更短的时间范围"
        )
    return f"{base[:room]} {suffix}"


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
        "query": fitted_query(query, time_range=time_range, domains=domains, now=now),
        # 官方 `count` 是 1～50：用户把 `max_results` 调大时钳到上限，别让配置顶出 400。
        "count": max(1, min(int(limit), _MAX_COUNT)),
        # 正文由 web_fetch 单独取。注意：按官方的计费口径，**按请求次数计费**，`count` 与
        # `need_content` 都不影响单次价格（只有 200 且 results 非空才计费）——这里只为省响应体积，
        # 不省费用；真要省钱只能少发请求。
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

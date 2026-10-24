"""搜索结果按查询词面相关度分档（2026-10-23）。

**不按分数重排**，只把"明显跑题"的沉到末尾：搜索引擎自己的排序里已经含了它知道、
而我们看不到的信号（站点权重、点击率、时效）。按词面得分重排会把"Python 3.14 新特性"
这种本来排得好好的两条顺序拧过来——2026-10-23 的既有用例就抓到了这一点，所以改成
"最高分的三分之一以下算跑题，沉底"，档内保持原序。

不做过滤：跑题条目照样留在末尾。理由与同站点限条一致——"搜到 3 条但都不相关"和
"只搜到 0 条"是两种不同的事实，不能被这里抹平成一种。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from local_agent.tools.result_types import WebSearchHit

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z0-9]{2,}")

#: 同一站点在前排最多占几条；其余挪到末尾。
DEFAULT_PER_HOST = 3
#: 得分低于"最高分 × 这个比例"的算跑题，沉到末尾。
RELEVANCE_FLOOR = 0.34


def query_tokens(query: str) -> set[str]:
    """拆成可比对的词元：英文/数字按整词，中文按 2-gram。

    中文没有空格，按字拆会把"长"和"征"当成两个弱信号；按 2-gram 至少能区分
    "长征"与"长城"。代价是 token 多一些，但这里每次只有几十条结果，成本可忽略。
    """
    tokens = set(_WORD.findall(query.lower()))
    for run in _CJK_RUN.findall(query):
        if len(run) == 1:
            tokens.add(run)
            continue
        for index in range(len(run) - 1):
            tokens.add(run[index:index + 2])
    return tokens


def _score(hit: WebSearchHit, tokens: set[str]) -> int:
    """命中词元的加权分：2 字词元比 1 字词元更有信息量，所以按长度累加。"""
    haystack = f"{hit.title} {hit.snippet}".lower()
    return sum(len(token) for token in tokens if token in haystack)


def _host(hit: WebSearchHit) -> str:
    return (urlsplit(hit.url).hostname or "").lower()


def best_score(hits: list[WebSearchHit], query: str) -> int:
    """这批结果里与查询词面最沾的一条得多少分；空列表或查询没词元时为 0。

    调用方用它判断"这份结果到底沾不沾边"：得分为 0 表示**一条都不沾**，
    通常意味着查询本身有问题（而不是结果排序不好）。
    """
    tokens = query_tokens(query)
    if not tokens:
        return 0
    return max((_score(hit, tokens) for hit in hits), default=0)


def rank_hits(
    hits: list[WebSearchHit], query: str, *, per_host: int = DEFAULT_PER_HOST
) -> list[WebSearchHit]:
    """把明显跑题的条目沉到末尾（档内保持原序），同站点前排最多 ``per_host`` 条。"""
    if len(hits) <= 1:
        return list(hits)

    tokens = query_tokens(query)
    if not tokens:
        return list(hits)

    scored = [(_score(hit, tokens), hit) for hit in hits]
    best = max(score for score, _ in scored)
    if best <= 0:
        # 一条都没命中：说明不是"跑题排序"的问题（可能查询本身写得怪）。
        # 这时保留引擎原序，由模型自己判断要不要换词。
        return list(hits)

    floor = best * RELEVANCE_FLOOR
    ordered = [hit for score, hit in scored if score >= floor]
    ordered += [hit for score, hit in scored if score < floor]

    if per_host <= 0:
        return ordered

    primary: list[WebSearchHit] = []
    overflow: list[WebSearchHit] = []
    counts: dict[str, int] = {}
    for hit in ordered:
        host = _host(hit)
        counts[host] = counts.get(host, 0) + 1
        if counts[host] <= per_host:
            primary.append(hit)
        else:
            overflow.append(hit)
    return primary + overflow

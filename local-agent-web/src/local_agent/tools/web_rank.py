"""搜索结果按查询词面相关度分档（2026-10-23）。

**不按分数重排**，只把"明显跑题"的沉到末尾：搜索引擎自己的排序里已经含了它知道、
而我们看不到的信号（站点权重、点击率、时效）。按词面得分重排会把"Python 3.14 新特性"
这种本来排得好好的两条顺序拧过来——2026-10-23 的既有用例就抓到了这一点，所以改成
"最高分的三分之一以下算跑题，沉底"，档内保持原序。

不做过滤：跑题条目照样留在末尾。理由与同站点限条一致——"搜到 3 条但都不相关"和
"只搜到 0 条"是两种不同的事实，不能被这里抹平成一种。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal
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


# ---- 相关性判据与"够不够用"（2026-10-28）------------------------------------
#
# 上一版判据的问题是**单 token 命中即得分**（``_score`` 按字符长度累加）：查询「瑞士轮」拆成
# 2-gram 后命中"瑞士"，瑞士旅游/政府日历就能拿满分并进上下文。2026-10-28 的端到端实测里，
# 一次 S16 战绩提问最终引用了 20 多个无关域名，模型被迫开了 20 次浏览器自救。
#
# 这一层只做两件事，都不改 ``rank_hits`` 的既有语义（那里仍然是"只沉底不过滤"）：
#   1. 用**结果集自身的文档频率**给词元加权（IDF），让"瑞士""2026"这类到处都是的词自动掉权；
#   2. 给出**覆盖度结论**（high/low/none），让调用方能如实说"这次只找到弱相关来源"。
# 数据就在这 5–20 条结果里，不需要任何外部语料或模型。

#: 一条结果最多采信它命中多少个词元；再多的边际收益很小，只会让长摘要占便宜。
_MAX_MATCHED_TERMS = 8
#: 标题命中比摘要命中更可信（标题是这段内容的主语，摘要是站点自己写的简介）。
_TITLE_WEIGHT = 2.0
#: 只命中整词片段时的权重系数（见 :func:`_weighted_score`）。
_PARTIAL_RUN_WEIGHT = 0.35
#: "整词"只对**短语长度**成立：中文连续段没有词边界，一整句中文（"北京到上海高铁最快几小时"）
#: 也会被当成一个 run，把它当整词会让所有片段命中一起被降权（2026-10-28 实测踩过）。
_MIN_RUN_CHARS = 3
_MAX_RUN_CHARS = 5
#: ``low`` 的最低门槛：命中的查询词元占全部词元的比例。
#: 低于它就不再叫"部分沾边"，而是 ``none``——2026-10-28 实测里 ``1/8`` 词元也会被报成 low，
#: 让模型以为"可以据此作答"。**取 0.15 而不是 0.25**：同一批实测里 0.25 会把 4/5 相关的
#: 查询（北京到上海高铁）整体判死，收紧过度。
_MIN_LOW_RATIO = 0.15


@dataclass(frozen=True, slots=True)
class Selection:
    """一批结果的去留与覆盖度结论。"""

    hits: list[WebSearchHit]
    dropped: int
    coverage: Literal["high", "low", "none"]
    #: 命中的**不同**查询词元数 / 结果集里出现过的词元数：用来解释"为什么说覆盖度低"。
    matched_terms: int = 0
    total_terms: int = 0
    #: 被丢掉的条目里最沾边那条的得分；便于排查"是不是阈值太狠"。
    dropped_best_score: float = 0.0


def _terms_with_hits(hits: list[WebSearchHit], tokens: set[str]) -> dict[str, int]:
    """每个词元出现在多少条结果里（文档频率）。"""
    frequency: dict[str, int] = {}
    for token in tokens:
        seen = 0
        for hit in hits:
            haystack = f"{hit.title} {hit.snippet}".lower()
            if token in haystack:
                seen += 1
        if seen:
            frequency[token] = seen
    return frequency


def _idf(frequency: dict[str, int], total: int) -> dict[str, float]:
    """平滑 IDF：出现得越普遍权重越低；``1 +`` 保证至少还有一点权重。"""
    return {token: math.log(1 + total / (1 + seen)) for token, seen in frequency.items()}


def _weighted_score(
    hit: WebSearchHit, idf: dict[str, float], full_runs: frozenset[str] = frozenset(),
) -> tuple[float, int, frozenset[str]]:
    """这条结果命中了哪些词元、加权多少分。返回 ``(得分, 命中词元数, 全额计分的词元)``。

    ``full_runs`` 是查询里长度 3–5 的中文整词（"英伟达"）。当一条结果**只命中这个词的 2-gram
    片段、却没有整词**时按部分分计（2026-10-28 实测：查询"英伟达财报"会把"NVISION 英伟芯"
    判成相关，因为切成"英伟/伟达"后只有"英伟"命中——那不是同一家公司）。
    这些"只沾片段"的词元不会进入覆盖度统计，否则它们会把 `none` 顶成 `low`。
    """
    title = hit.title.lower()
    snippet = hit.snippet.lower()
    haystack = f"{title} {snippet}"
    score = 0.0
    matched = 0
    counted: set[str] = set()
    for token, weight in idf.items():
        in_title = token in title
        in_snippet = token in snippet
        if not (in_title or in_snippet):
            continue
        partial = len(token) == 2 and any(
            token in run and run not in haystack for run in full_runs
        )
        # 片段命中但整词缺失：降权而不是丢弃——整词是更强的证据，片段只是可能相关。
        if partial:
            weight *= _PARTIAL_RUN_WEIGHT
        else:
            counted.add(token)
        score += weight * (_TITLE_WEIGHT if in_title else 1.0)
        matched += 1
        if matched >= _MAX_MATCHED_TERMS:
            break
    return score, matched, frozenset(counted)


def select_relevant(
    hits: list[WebSearchHit], query: str, *, limit: int, per_host: int = DEFAULT_PER_HOST,
) -> Selection:
    """挑出"确实沾边"的结果，并给出覆盖度结论（**不做分数重排**，保留引擎原序）。

    三条保守规则，都是为了"宁可少丢、不要错杀"：
    - 一条都不沾查询（最高分为 0）时**不过滤**：这说明问题在查询本身，把结果全丢掉只会让
      模型看不出"到底搜到了什么"。
    - 只有一个词元的查询按 1 个词元判定，不强求 2 个。
    - 覆盖度 ``none`` 不会把结果清空，它只是把"这次没搜到像样的东西"变成可读的结论。
    """
    if not hits:
        return Selection(hits=[], dropped=0, coverage="none")
    tokens = query_tokens(query)
    if not tokens:
        # 查询里没有可比对的词元（纯标点等）：无从判断相关性，如实说"覆盖度不确定"。
        return Selection(hits=list(hits)[:limit], dropped=max(0, len(hits) - limit), coverage="low")

    frequency = _terms_with_hits(hits, tokens)
    if not frequency:
        # 一条都不沾：保持原样（与 ``rank_hits`` 同一判断），只是把结论标成 none。
        return Selection(
            hits=list(hits)[:limit], dropped=max(0, len(hits) - limit), coverage="none",
        )
    idf = _idf(frequency, len(hits))
    full_runs = frozenset(
        run for run in _CJK_RUN.findall(query) if _MIN_RUN_CHARS <= len(run) <= _MAX_RUN_CHARS
    )
    scored = [(_weighted_score(hit, idf, full_runs), hit) for hit in hits]
    best = max(score for (score, _m, _c), _ in scored)
    if best <= 0:  # pragma: no cover - frequency 非空时不可能全为 0
        return Selection(
            hits=list(hits)[:limit], dropped=max(0, len(hits) - limit), coverage="none",
        )

    threshold = best * RELEVANCE_FLOOR
    # 只用**加权得分**做取舍，不再额外要求"命中 N 个词元"（2026-10-28 实测教训）：
    # 一条只命中实体的结果（"Download Python" 对查询 "python 3.14"）是正常且有用的，
    # 硬性要求两个词元会把它误杀；而"只命中泛词"的条目已经被 IDF 自动压低分。
    strong: list[WebSearchHit] = []
    weak_scores: list[float] = []
    for (score, _matched, _counted), hit in scored:
        if score >= threshold:
            strong.append(hit)
        else:
            weak_scores.append(score)

    # 覆盖度只统计"全额计分"的词元：只沾到整词片段的（英伟芯 vs 英伟达）不算覆盖。
    union: set[str] = set()
    for (score, _matched, counted), hit in scored:
        if score >= threshold:
            union.update(counted)

    # 覆盖度按**查询的全部词元**算，而不是"在结果里出现过的词元"：后者会把"整页只命中
    # 一个泛词"算成全覆盖（实测反例：查询里 6 个词元、结果只命中"如何"，不能叫覆盖度高）。
    # 而且 low 也有地板：低于 :data:`_MIN_LOW_RATIO` 就直接是 none——"1/8 词元"不叫沾边。
    ratio = len(union) / max(1, len(tokens))
    # 中文的 2-gram 会把一句长问句撑成十几个词元（"北京到上海高铁最快几小时" → 11 个），
    # 命中"北京/上海/高铁"这种明显对题的结果也只有 27%，比例门槛会失真。所以中文再按
    # **字符覆盖面**算一遍取大者——它对问句长短不敏感。
    cjk_chars = {ch for run in _CJK_RUN.findall(query) for ch in run}
    if cjk_chars:
        covered = {ch for token in union for ch in token if ch in cjk_chars}
        ratio = max(ratio, len(covered) / len(cjk_chars))
    if not strong:
        coverage: Literal["high", "low", "none"] = "none"
        kept = list(hits)  # 一条都不沾：保留，让调用方看得见"到底搜到了什么"
    elif ratio < _MIN_LOW_RATIO:
        # 只沾到零星泛词（如 8 个词元命中 1 个）：这批评分最高的条目**本身也是噪声**，
        # 与"一条都不沾"一样按 none 处理，但不保留——否则上下文又被垃圾占满。
        coverage = "none"
        kept = []
    elif ratio >= 0.5 and len(strong) >= min(2, len(hits)):
        coverage = "high"
        kept = strong
    else:
        coverage = "low"
        kept = strong
    # 同站点限条只在"够强"的那批里做，避免把唯一的不同来源挤掉。
    capped: list[WebSearchHit] = []
    counts: dict[str, int] = {}
    for hit in kept:
        host = _host(hit)
        counts[host] = counts.get(host, 0) + 1
        if counts[host] <= per_host:
            capped.append(hit)
    dropped = len(hits) - len(capped[:limit])
    return Selection(
        hits=capped[:limit], dropped=max(0, dropped), coverage=coverage,
        matched_terms=len(union), total_terms=len(tokens),
        dropped_best_score=max(weak_scores, default=0.0),
    )

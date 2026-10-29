"""结果重排（2026-10-23）：只排序不过滤，同站点前排限条但不丢。"""

from __future__ import annotations

from local_agent.tools.result_types import WebSearchHit
from local_agent.tools.web_rank import best_score, rank_hits, select_relevant


def hit(title: str, url: str, snippet: str = "") -> WebSearchHit:
    return WebSearchHit(title=title, url=url, source=url.split("/")[2], snippet=snippet)


def test_puts_on_topic_results_first() -> None:
    hits = [
        hit("语言词典：纪念", "https://zdic.net/a"),
        hit("纪念长征胜利90周年大会在京举行", "https://gov.cn/a"),
        hit("网易云音乐", "https://music.163.com/a"),
    ]
    ranked = rank_hits(hits, "纪念长征胜利90周年大会")
    assert ranked[0].url == "https://gov.cn/a"
    # 跑题的没有被删，只是排到后面
    assert len(ranked) == 3


def test_keeps_original_order_for_equal_scores() -> None:
    hits = [
        hit("无关甲", "https://a.example/1"),
        hit("无关乙", "https://b.example/2"),
        hit("无关丙", "https://c.example/3"),
    ]
    ranked = rank_hits(hits, "完全不相干的词")
    assert [item.url for item in ranked] == [item.url for item in hits]


def test_caps_one_host_in_the_front_but_keeps_the_rest() -> None:
    hits = [
        hit(f"转载 第{i}篇", f"https://same.example/{i}") for i in range(1, 6)
    ] + [hit("另一个来源", "https://other.example/x")]

    ranked = rank_hits(hits, "转载", per_host=2)

    # 前两条仍是同站点（限条阈值内），其余同站条目挪到末尾但不消失
    assert [item.url for item in ranked[:2]] == [
        "https://same.example/1",
        "https://same.example/2",
    ]
    assert len(ranked) == 6
    assert ranked[-1].url == "https://same.example/5"


def test_returns_input_when_there_is_nothing_to_rank() -> None:
    single = [hit("只有一条", "https://a.example/1")]
    assert rank_hits(single, "随便") == single
    # query 全是标点 → 没有词元 → 原样返回
    many = [hit("甲", "https://a.example/1"), hit("乙", "https://b.example/2")]
    assert rank_hits(many, "!!!") == many


def test_best_score_reports_whether_anything_sticks() -> None:
    hits = [
        hit("一加手机官网", "https://oneplus.example/"),
        hit("纪念长征胜利90周年大会在京举行", "https://gov.cn/a"),
    ]
    assert best_score(hits, "纪念长征胜利90周年大会") > 0
    assert best_score(hits, "完全不相干的词") == 0
    assert best_score([], "纪念长征") == 0
    assert best_score(hits, "!!!") == 0  # 查询没有词元


# ---- IDF 判据与覆盖度（2026-10-28）----------------------------------------


def test_generic_word_only_hit_is_dropped_by_idf() -> None:
    """「瑞士轮」被拆成 2-gram 后，只命中"瑞士"的旅游页不该进上下文（2026-10-28 实测）。"""
    hits = [
        hit("瑞士旅游攻略", "https://travel.example/ch", "瑞士 2026 最佳旅行季节"),
        hit("S16 瑞士轮 战绩汇总", "https://esports.example/s16", "瑞士轮三轮打完"),
    ]
    selection = select_relevant(hits, "S16 瑞士轮 战绩", limit=5)
    assert [item.url for item in selection.hits] == ["https://esports.example/s16"]
    assert selection.dropped == 1
    assert selection.matched_terms >= 2


def test_nothing_relevant_keeps_results_and_says_none() -> None:
    """一条都不沾时**不过滤**（那是查询本身的问题），但结论必须说 none。"""
    hits = [
        hit("英雄联盟官方网站", "https://lol.qq.com/"),
        hit("WeGame 商店", "https://wegame.example/26"),
    ]
    selection = select_relevant(hits, "S16 瑞士轮 战绩", limit=5)
    assert [item.url for item in selection.hits] == [item.url for item in hits]
    assert selection.coverage == "none" and selection.dropped == 0


def test_single_hit_that_matches_the_query_counts_as_covered() -> None:
    selection = select_relevant([hit("MCP protocol", "https://docs.example.com/", "Model Context Protocol")],
                                "Model Context Protocol MCP", limit=5)
    assert selection.coverage == "high" and len(selection.hits) == 1


def test_empty_result_set_reports_none() -> None:
    selection = select_relevant([], "任意查询", limit=5)
    assert selection.hits == [] and selection.coverage == "none"


def test_partial_name_does_not_impersonate_the_whole_word() -> None:
    """实测反例：查询「英伟达财报」，"NVISION 英伟芯"因 2-gram 命中"英伟"被当成相关。

    中文没有词边界，2-gram 会把一个公司名切碎；所以只命中整词片段而缺整词时要降权。
    """
    hits = [
        hit("英伟达 2026 财年第三季度财报 营收", "https://news.example/nvda", "财报显示营收同比增长"),
        hit("NVISION 英伟芯｜硅光光电集成", "https://other.example/x", "数据中心光互连方案"),
    ]
    selection = select_relevant(hits, "英伟达 2026 财年第三季度财报 营收", limit=5)
    assert [item.url for item in selection.hits] == ["https://news.example/nvda"]


def test_one_matched_token_out_of_many_is_none_not_low() -> None:
    """8 个查询词元只命中 1 个泛词：不能叫"部分沾边"，也不能把这条留在结果里。"""
    hits = [hit("NVISION 英伟芯", "https://other.example/x", "数据中心光互连方案")]
    selection = select_relevant(hits, "英伟达 2026 财年第三季度财报 营收", limit=5)
    assert selection.coverage == "none"
    assert selection.hits == []

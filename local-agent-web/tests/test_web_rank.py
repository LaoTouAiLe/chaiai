"""结果重排（2026-10-23）：只排序不过滤，同站点前排限条但不丢。"""

from __future__ import annotations

from local_agent.tools.result_types import WebSearchHit
from local_agent.tools.web_rank import best_score, rank_hits


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

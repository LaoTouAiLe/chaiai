"""查询清洗（2026-10-23）。

用例分三类：
1. **该删的**：榜单整行、圈号、项目符号、时间戳、分享链接；
2. **绝不能删的**：以数字开头的真查询（量词/疑问）、短查询、正常长句；
3. **边界**：删过头要整段回退；NFKC 只做规范化不改语义。
"""

from __future__ import annotations

import pytest

from local_agent.tools.web_query import clean_query, keyword_variants


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 2026-10-23 实测案例：原样交给 Bing 会坍缩到"1"，删掉序号与热度后全部命中。
        ("1 纪念长征胜利90周年大会隆重举行 790.5 万", "纪念长征胜利90周年大会隆重举行"),
        ("2. 纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        ("3、纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        ("4) 纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        ("③ 纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        ("- 纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        ("No.1 纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        ("Top 1 纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        # 热度值只在"已确认是榜单行"（序号已删）时才删。
        ("1 今日热搜 3.4w", "今日热搜"),
        ("10:12 今天的会议纪要", "今天的会议纪要"),
        ("今天的会议纪要 10:12", "今天的会议纪要"),
        ("今日新闻 https://example.com/a/b", "今日新闻"),
    ],
)
def test_strips_list_and_share_noise(raw: str, expected: str) -> None:
    cleaned = clean_query(raw)
    assert cleaned.text == expected
    assert cleaned.changed
    assert cleaned.removed  # 说明里要能列出删了什么


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("【1】纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        ("一、纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        ("第一篇 纪念长征胜利90周年大会隆重举行", "纪念长征胜利90周年大会隆重举行"),
        ("#今日热搜# 纪念长征胜利90周年大会", "纪念长征胜利90周年大会"),
        ("转发微博 @央视新闻 纪念长征胜利90周年大会", "纪念长征胜利90周年大会"),
        # 多行分享文本：取首行（标题），其余行是副标题 / 来源 / 时间
        (
            "纪念长征胜利90周年大会隆重举行\n习近平发表重要讲话\n来源：新华社",
            "纪念长征胜利90周年大会隆重举行",
        ),
    ],
)
def test_handles_common_share_formats(raw: str, expected: str) -> None:
    assert clean_query(raw).text == expected


@pytest.mark.parametrize(
    "raw",
    [
        "100 个为什么",          # 序号后是量词：真查询
        "3 步搞定 Docker 部署",  # 量词表里必须有"步"
        "10 大最佳实践",         # 必须有"大"
        "1 号文件解读",          # 必须有"号"
        "5 best practices for Python",  # 裸数字后是英文：按数量处理
        "1 加 1 等于几",         # 整句是提问
        "1 体",                  # 太短，每个字都可能是关键词
        "1 人工智能",            # 不足 8 个字，不进清洗
        "Model Context Protocol MCP",  # 正常英文查询：一个字都不该动
        "纪念长征胜利90周年大会隆重举行",  # 本来就没有噪声
        "1纪念长征胜利90周年大会",  # 没有分隔符，不按序号处理
    ],
)
def test_keeps_queries_that_only_look_like_lists(raw: str) -> None:
    cleaned = clean_query(raw)
    assert cleaned.text == raw.strip()
    assert not cleaned.changed


def test_does_not_touch_a_standalone_heat_number() -> None:
    """没有序号时，"票房 790 万"是数值查询，不能把 790 万当热度删掉。"""
    cleaned = clean_query("电影票房 790 万")
    assert cleaned.text == "电影票房 790 万"


def test_normalizes_fullwidth_and_zero_width_characters() -> None:
    cleaned = clean_query("１\u200b 纪念长征胜利90周年大会 ７９０.５ 万")
    assert cleaned.text == "纪念长征胜利90周年大会"


def test_falls_back_when_cleanup_would_leave_almost_nothing() -> None:
    # 时间戳后面只剩两个字：删完不足 4 个字，宁可原样返回。
    cleaned = clean_query("10:12 啊啊")
    assert cleaned.text == "10:12 啊啊"
    assert not cleaned.changed


# ---- 关键词变体（2026-10-28）------------------------------------------------


def test_variant_drops_the_question_opening_that_collapses_the_search() -> None:
    """实测：整句「如何用 pandas…」在 Bing 上只召回"如何"的词典义，去掉问法就正常。"""
    variants = keyword_variants("如何用 pandas 读取 excel 文件")
    assert variants[0] == "pandas 读取 excel 文件"


def test_variant_moves_a_bare_year_off_the_front() -> None:
    """实测：以「2026年…」开头的查询会被坍缩到"2026年"的百科/放假日历。"""
    variants = keyword_variants("2026年诺贝尔文学奖得主是谁")
    assert variants and variants[0].startswith("诺贝尔文学奖得主")
    assert "2026年" in variants[0]


def test_variant_is_not_invented_when_it_would_change_meaning() -> None:
    """「1 加 1 等于几」没有可删的首词：宁可不生成变体，也不发一句面目全非的查询。"""
    assert keyword_variants("1 加 1 等于几") == []


def test_variant_removes_question_particles_in_the_middle() -> None:
    """实测：`…线程池 什么区别` 在 Bing 上 0/5，去掉"什么"两字后同一时刻 5/5。"""
    variants = keyword_variants("python asyncio 和线程池有什么区别")
    assert "asyncio 线程池 区别" in variants
    assert all("什么" not in variant for variant in variants)
    assert all("区别" in variant for variant in variants)  # "区别"是有信息量的词，不能删


def test_variants_are_never_empty_or_overlong() -> None:
    for raw in ("Model Context Protocol MCP", "明天的天气", "LOL的S16全球赛目前各个战队的战绩怎么样？"):
        for variant in keyword_variants(raw):
            assert 4 <= len(variant) <= 120
            assert variant != raw.strip()

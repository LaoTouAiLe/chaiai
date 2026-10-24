"""搜索查询的噪声清理（2026-10-23，2026-10-23 扩到通用形态）。

**只做减法，不做改写**：删掉能机械识别的榜单/分享噪声，其余原样保留。关键词不改、
顺序不改、不扩写——"实际搜了什么"必须能从 ``WebSearchResult.executed_query`` 一眼看出来。

为什么需要它（2026-10-23 实测）：把热搜整行 ``1 纪念长征胜利90周年大会隆重举行 790.5 万``
原样交给 Bing，返回的是"百度百科：1（自然数之一）""一加手机官网""CCTV-1"——Bing 会把
以数字开头的长查询坍缩到那个数字上。只去掉序号与热度值、其余一字不改，同一时刻同一 UA
返回的 8 条全部命中官方报道。所以清洗的收益是确定的，而改写没有证据。

**通用性靠"形态覆盖 + 宁可不删"，不靠语义猜测**：
- 覆盖：方括号序号（【1】）、中文数字序号（一、）、第 N 篇、圈号、项目符号、#话题#、
  @提及、分享前缀（转发微博/来源：）、带头尾时间戳、结尾分享链接、榜单热度值、多行分享文本；
- 不删：数字后紧跟量词/单位（"3 步""10 大""1 号"）、数字后紧跟英文单词（"5 best practices"）、
  整句是提问（"1 加 1 等于几"）、短查询（< 8 字符）、删完不足 4 字符；
- 热度值只在**已经剥离过前缀**时才删：没有别的噪声信号时，"票房 790 万"是数值查询。

已知边界（有意为之）：纯规则无法区分"1 号文件"（数量）与"1 号榜单"（序号）这类语义歧义。
这种情况一律保留——漏删只是效果差一点，误删会让整句查询跑偏。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: 短查询里每个字都可能是关键词，不做任何清理。
MIN_LENGTH_FOR_CLEANUP = 8
#: 清洗后至少保留的字符数。
MIN_RESULT_CHARS = 4

_ZERO_WIDTH = re.compile("[\u200b-\u200f\u2060\ufeff]")
_SPACES = re.compile(r"\s+")
_CJK = re.compile(r"[\u4e00-\u9fff]")

#: 方括号序号：【1】/［2］/「3」/『4』。
_LEADING_BRACKET = re.compile(r"^\s*[【\[［「『]\s*\d{1,3}\s*[】\]］」』]\s*")
#: 中文数字序号：一、/ 二./ 三）。
_LEADING_CN_ORDINAL = re.compile(r"^\s*[一二三四五六七八九十]{1,3}\s*[、.．)）]\s*")
#: 第 N 篇 / 第 3 条 / 第二期。
_LEADING_CN_SERIAL = re.compile(r"^\s*第\s*[一二三四五六七八九十百千\d]{1,4}\s*[篇条期章节轮场次]\s*")
#: 话题标签：#今日热搜#。
_LEADING_TOPIC = re.compile(r"^\s*#[^#\s]{1,24}#\s*")
#: @提及：@央视新闻（后面必须有空白，避免吞掉 "user@example.com"）。
_LEADING_MENTION = re.compile(r"^\s*@[^\s@]{1,30}\s+")
#: 分享前缀：转发微博 / 来源：/ 来自：。
_LEADING_SHARE = re.compile(r"^\s*(?:转发微博|转发了微博|来源[:：]|来自[:：])\s*")
#: 圈号序号 ①②…⑳。
_LEADING_CIRCLED = re.compile(r"^\s*[\u2460-\u2473]\s*")
#: 项目符号（``- `` / ``• `` / ``· ``）。
_LEADING_BULLET = re.compile(r"^\s*[-•·—–]\s+")
#: 带标点的阿拉伯序号：``1.`` / ``1、`` / ``1)`` / ``#1`` / ``No.1`` / ``Top 1``。
#: 分隔符里不含冒号：``10:12`` 是时间戳，不能被切成 ``12``。
_LEADING_ORDINAL_PUNCT = re.compile(
    r"^\s*(?:no\.?|top|#)?\s*\d{1,3}\s*[.、,，)）\]】\-—]\s*", re.IGNORECASE
)
#: 裸数字 + 空白。只有后面紧跟中文时才按序号处理（``1 纪念…``）；
#: 后面是英文时多半是数量（``5 best practices``），不动。
_LEADING_ORDINAL_SPACE = re.compile(r"^\s*(?:no\.?|top|#)?\s*\d{1,3}\s+", re.IGNORECASE)

#: 结尾热度值：``790.5 万`` / ``1.2亿`` / ``3.4w``（可带"热度"之类的尾缀）。
_TRAILING_HEAT = re.compile(
    r"\s*[（(【\[]?\s*\d+(?:\.\d+)?\s*(?:万|亿|w|k|千)\s*"
    r"(?:热度|人看过|次播放|次阅读|讨论|阅读)?\s*[）)】\]]?\s*$",
    re.IGNORECASE,
)
_LEADING_TIMESTAMP = re.compile(r"^\s*[\[(（]?\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\])）]?\s*")
_TRAILING_TIMESTAMP = re.compile(r"\s*[\[(（]?\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\])）]?\s*$")
_TRAILING_URL = re.compile(r"\s*https?://\S+\s*$", re.IGNORECASE)

#: 数字后面紧跟这些字时说明它是数量而不是榜单序号。
#: 这张表是"宁可多收"的：漏收一个字会误删真实查询，多收一个字只是漏洗一次榜单。
_MEASURE_HEADS = frozenset(
    "个只次天年月日元块人条张页分秒米克斤岁步大号对招款级倍成折篇幅章节段项种位名"
    "家所台辆架艘颗粒双套组批本份期届轮场回趟顿杯瓶盒箱包袋吨升度寸尺里亩顷"
)

#: 整句以这些收尾时按提问处理，不动开头数字（"1 加 1 等于几"）。
_QUESTION_TAILS = ("几", "多少", "什么", "吗", "呢", "如何", "怎么", "为什么", "哪")

#: 前缀剥离顺序：越具体的越先，避免 ``#1 xxx`` 被当成话题标签、``来源：1 xxx`` 被当成序号。
_PREFIX_PATTERNS = (
    _LEADING_SHARE,
    _LEADING_TOPIC,
    _LEADING_MENTION,
    _LEADING_BRACKET,
    _LEADING_CN_SERIAL,
    _LEADING_CN_ORDINAL,
    _LEADING_CIRCLED,
    _LEADING_BULLET,
)


@dataclass(frozen=True, slots=True)
class QueryCleanup:
    """一次清洗结果；``removed`` 只用来生成说明，不参与搜索。"""

    text: str
    original: str
    removed: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.text != self.original


def _normalize(raw: str) -> str:
    """NFKC（全角转半角）+ 去零宽字符 + 折叠行内空白。"""
    text = unicodedata.normalize("NFKC", raw or "")
    text = _ZERO_WIDTH.sub("", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _is_question(text: str) -> bool:
    tail = text.rstrip("？?！!。. ")
    return tail.endswith(_QUESTION_TAILS)


def _first_line(text: str) -> tuple[str, str | None]:
    """多行分享文本取首行；只有一行时原样返回。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return text.replace("\n", " ").strip(), None

    head = lines[0].strip()
    if len(head) < 6:
        # 首行太短（"热搜"之类），前两行合起来更像一句可搜的话。
        head = " ".join(lines[:2]).strip()
    return head, f"多行分享文本（取首行，其余 {len(lines) - 1} 行）"


def _strip_prefixes(text: str) -> tuple[str, list[str]]:
    """循环剥离开头的标记；每个标记都要给剩下的内容留够字数。"""
    removed: list[str] = []

    while True:
        matched = False
        for pattern in _PREFIX_PATTERNS:
            match = pattern.match(text)
            if match is not None and len(text) - match.end() >= MIN_RESULT_CHARS:
                removed.append(match.group(0).strip())
                text = text[match.end():].strip()
                matched = True
                break
        if matched:
            continue

        # 带标点的阿拉伯序号：后面紧跟量词时按数量处理（"10. 大促销"没有量词才算序号）。
        match = _LEADING_ORDINAL_PUNCT.match(text)
        if match is not None and len(text) - match.end() >= MIN_RESULT_CHARS:
            remainder = text[match.end():]
            if remainder[:1] not in _MEASURE_HEADS:
                removed.append(match.group(0).strip())
                text = remainder.strip()
                continue

        # 裸数字 + 空白：只认"后面是中文"的形态。
        match = _LEADING_ORDINAL_SPACE.match(text)
        if match is not None and len(text) - match.end() >= MIN_RESULT_CHARS:
            remainder = text[match.end():]
            head = remainder[:1]
            if _CJK.match(head) and head not in _MEASURE_HEADS:
                removed.append(match.group(0).strip())
                text = remainder.strip()
                continue

        return text, removed


def clean_query(raw: str) -> QueryCleanup:
    """清理查询里的榜单/分享噪声。

    返回的 ``text`` 可以直接拿去请求；调用方必须把它写进 ``executed_query``，
    并保留原始 ``query``——否则"实际搜了什么"就不可核对。
    """
    original = _normalize(raw)
    if len(original) < MIN_LENGTH_FOR_CLEANUP:
        return QueryCleanup(original, original)

    removed: list[str] = []

    text, multiline = _first_line(original)
    if multiline is not None:
        removed.append(multiline)

    # 开头时间戳先处理：``10:12 xxx`` 里的 "10" 不能被当成榜单序号切成 "12 xxx"。
    match = _LEADING_TIMESTAMP.match(text)
    if match is not None and len(text) - match.end() >= MIN_RESULT_CHARS:
        removed.append(match.group(0).strip())
        text = text[match.end():].strip()

    if _is_question(text):
        # 提问句不剥前缀：把"1 加 1 等于几"的 1 删掉会毁掉整句。
        return QueryCleanup(text, original, tuple(removed))

    text, prefixes = _strip_prefixes(text)
    removed.extend(prefixes)

    # 热度值只在"已经确认是噪声文本"时才删：没有别的噪声信号时，"票房 790 万"是数值查询。
    if removed:
        match = _TRAILING_HEAT.search(text)
        if match is not None and len(text) - len(match.group(0)) >= MIN_RESULT_CHARS:
            removed.append(match.group(0).strip())
            text = text[: match.start()].strip()

    match = _TRAILING_TIMESTAMP.search(text)
    if match is not None and match.start() >= MIN_RESULT_CHARS:
        removed.append(match.group(0).strip())
        text = text[: match.start()].strip()

    match = _TRAILING_URL.search(text)
    if match is not None and match.start() >= MIN_RESULT_CHARS:
        removed.append(match.group(0).strip())
        text = text[: match.start()].strip()

    text = _SPACES.sub(" ", text).strip()
    if len(text) < MIN_RESULT_CHARS:
        return QueryCleanup(original, original)
    return QueryCleanup(text, original, tuple(removed))

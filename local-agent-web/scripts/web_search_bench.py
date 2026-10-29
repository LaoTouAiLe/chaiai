"""网页搜索/抓取的**只读基准**：同一批查询，比"旧行为"与"现在的 search()"。

为什么单独一个脚本（2026-10-28）：

``web_rank`` / ``web_query`` 里的判据是**经验阈值**（IDF 权重、覆盖度地板、整词长度范围、
变体上限等 8 个常量），它们互相耦合，改一个常常影响另一个。没有固定基准时，"感觉效果不好"
就只能凭直觉改数字——上一轮就这么把两条本来 4/5 相关的查询改成了 0/0。

所以约定：**任何判据改动前后都跑这个脚本，把两列数字留在 PR 描述里**；有任何一条退化就不合。

边界（有意为之）：
- **手动执行**，不进 CI：它依赖真实网络与当时的搜索引擎返回，天然不可复现；
- **不调模型、不花 API 费用**：只走 ``web_search`` 的 HTML 兼容后端与 ``web_fetch`` 本机抓取；
- 相关性判据是脚本里手写的关键词（``CASES`` 的第二列），是**代理指标**，不是人工标注；
- 允许 import 私有函数（``_default_transport`` 等）：这里要复刻"加这些判据之前"的行为。

用法::

    python scripts/web_search_bench.py              # 搜索对比
    python scripts/web_search_bench.py --fetch      # 另跑 6 个已知难题 URL
    python scripts/web_search_bench.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_agent.config import load_config
from local_agent.tools.web_rank import rank_hits
from local_agent.tools.web_tools import (
    _default_transport,
    _quote,
    check_url,
    clear_search_cache,
    fetch,
    parse_search_results,
    search,
)

#: ``(查询, 期望出现的实体词)``——任一命中即算相关。**新增样本只能追加，不许为让数字好看而删。**
CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("LOL的S16全球赛目前各个战队的战绩怎么样？", ("S16", "瑞士轮", "战绩")),
    ("2026年诺贝尔文学奖得主是谁", ("诺贝尔",)),
    ("明天上海天气怎么样", ("上海", "天气")),
    ("英伟达 2026 财年第三季度财报 营收", ("英伟达", "NVIDIA", "财报")),
    ("python asyncio 和线程池有什么区别", ("asyncio", "线程池", "并发")),
    ("Excel 如何冻结首行", ("冻结", "首行", "窗格")),
    ("特斯拉 Model Y 最新价格", ("Model Y", "特斯拉")),
    ("北京到上海高铁最快几小时", ("高铁", "北京", "上海")),
    ("如何用 pandas 读取 excel 文件", ("pandas", "Excel", "读取")),
    ("S16 瑞士轮 战绩", ("瑞士轮", "战绩", "S16")),
    ("高血压患者日常饮食要注意什么", ("高血压", "饮食", "血压")),
    ("劳动合同到期不续签有赔偿吗", ("劳动合同", "赔偿", "经济补偿")),
    ("vue3 ref 和 reactive 的区别", ("ref", "reactive", "Vue")),
    ("故宫博物院 预约 门票 开放时间", ("故宫", "预约", "门票")),
    ("2026年国庆放假安排", ("国庆", "放假", "假期")),
    ("红烧肉家常做法 步骤", ("红烧肉", "做法", "五花肉")),
    ("cat /etc/os-release 查看系统版本", ("os-release", "系统版本", "Linux")),
    ("iPhone 17 发布日期 参数", ("iPhone", "苹果")),
    ("考研数学一 大纲 变化", ("考研", "数学", "大纲")),
    ("如何判断一段英文文本的可读性", ("可读性", "readability", "阅读")),
)

#: 已知难题 URL：用来盯"字符集 / 403 / 超时"三类失败有没有变化（不判断内容质量）。
FETCH_URLS: tuple[tuple[str, str], ...] = (
    ("官网 GBK", "https://lol.qq.com/main.shtml"),
    ("中文新闻", "https://www.dzwww.com/news/wtxw/202610/t20261027_18242588.htm"),
    ("英文文档", "https://docs.python.org/3/library/asyncio-task.html"),
    ("需浏览器", "https://liquipedia.net/leagueoflegends/World_Championship/2026"),
    ("接口 403", "https://esports-api.lolesports.com/persisted/gw/getStandings?hl=zh-CN"),
    ("超时站点", "https://lol.fandom.com/wiki/2026_Season_World_Championship"),
)


def _related(hits, terms: tuple[str, ...]) -> int:
    return sum(
        1 for hit in hits if any(term.lower() in f"{hit.title} {hit.snippet}".lower() for term in terms)
    )


def _old_behavior(query: str, config) -> list:
    """复刻"加判据之前"的链路：原句一次请求 + 只按词面重排（不丢、不换词）。"""
    url = f"{check_url('https://www.bing.com/search', resolve=False)}?q={_quote(query)}"
    response = _default_transport(url, float(config.timeout_seconds), config.user_agent)
    return rank_hits(parse_search_results(response.text, max_results=5), query)


def main() -> int:
    parser = argparse.ArgumentParser(description="网页搜索/抓取只读基准")
    parser.add_argument("--fetch", action="store_true", help="另跑 6 个已知难题 URL")
    parser.add_argument("--provider", default="bing_html", help="搜索后端（默认 bing_html）")
    parser.add_argument("--json", type=Path, default=None, help="把明细写到这个文件")
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml").web_access
    html = config.model_copy(update={"provider": args.provider, "prefetch_pages": 0})

    print(f"{'查询':<26}{'旧':>8}{'新':>8}{'旧s':>7}{'新s':>7}  覆盖度   动作")
    totals = {"old_related": 0, "old_n": 0, "new_related": 0, "new_n": 0, "old_s": 0.0, "new_s": 0.0}
    rows: list[dict] = []

    for query, terms in CASES:
        clear_search_cache()
        started = time.perf_counter()
        old_hits = _old_behavior(query, config)
        old_cost = time.perf_counter() - started

        clear_search_cache()
        started = time.perf_counter()
        try:
            result = search(query, config=html, max_results=5)
            new_hits, new_cost = result.results, time.perf_counter() - started
            coverage = result.coverage or "-"
            note = result.note or ""
            executed = result.executed_query or query
        except Exception as exc:  # noqa: BLE001 - 基准脚本如实记录失败
            new_hits, new_cost, coverage, note, executed = [], 0.0, "错误", str(exc), query

        old_related = _related(old_hits, terms)
        new_related = _related(new_hits, terms)
        totals["old_related"] += old_related
        totals["old_n"] += len(old_hits)
        totals["new_related"] += new_related
        totals["new_n"] += len(new_hits)
        totals["old_s"] += old_cost
        totals["new_s"] += new_cost

        actions = [
            label for label, marker in
            (("变体", "自动改用"), ("丢弃", "不沾边"), ("零命中", "没有找到"))
            if marker in note
        ]
        print(f"{query[:24]:<26}{f'{old_related}/{len(old_hits)}':>8}{f'{new_related}/{len(new_hits)}':>8}"
              f"{old_cost:7.2f}{new_cost:7.2f}  {coverage:<8}{'/'.join(actions) or '-'}")
        if executed != query:
            print(f"      → 实际检索词: {executed}")
        rows.append({
            "query": query, "terms": terms, "old": old_related, "old_n": len(old_hits),
            "new": new_related, "new_n": len(new_hits), "coverage": coverage,
            "executed": executed, "actions": actions, "note": note,
        })

    old_rate = totals["old_related"] / max(1, totals["old_n"])
    new_rate = totals["new_related"] / max(1, totals["new_n"])
    print(f"\n合计相关：旧 {totals['old_related']}/{totals['old_n']} ({old_rate:.0%})  →  "
          f"新 {totals['new_related']}/{totals['new_n']} ({new_rate:.0%})")
    print(f"合计耗时：旧 {totals['old_s']:.1f}s → 新 {totals['new_s']:.1f}s")
    print("注意：相关性由本脚本的关键词代理判定，不是人工标注；改动判据后请与上一版数字逐条对比，"
          "**任何一条退化都应视为不合入**。")

    fetches: list[dict] = []
    if args.fetch:
        print("\n抓取（只判断可用性，不判断内容质量）")
        fetch_config = config.model_copy(
            update={"fetch_provider": "local", "max_chars": 800, "timeout_seconds": 12}
        )
        for label, url in FETCH_URLS:
            started = time.perf_counter()
            try:
                page = fetch(url, config=fetch_config)
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {label:<8}{url[:52]:<54}{time.perf_counter()-started:5.1f}s "
                      f"{type(exc).__name__}: {str(exc)[:60]}")
                fetches.append({"label": label, "url": url, "ok": False, "error": str(exc)})
                continue
            text = page.text or ""
            print(f"  ✓ {label:<8}{url[:52]:<54}{time.perf_counter()-started:5.1f}s "
                  f"字={len(text)} 乱码={'�' in text} note={(page.note or '-')[:40]}")
            fetches.append({"label": label, "url": url, "ok": True, "chars": len(text),
                            "mojibake": "\ufffd" in text, "note": page.note})

    if args.json is not None:
        args.json.write_text(
            json.dumps({"summary": totals, "search": rows, "fetch": fetches},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n明细写出：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

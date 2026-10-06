"""T23 精度层：真实模型 + **硬阈值**的定位 / 引用 / 泄漏断言（默认跳过）。

为什么需要单独一层
------------------
现有两层都不回答"精度会不会被打回去"：

- 自动化层（``test_agent_eval.py``）用**替身模型**，能测终点与安全不变量，但测不出"模型有没有真的
  引用行号"——那是真实模型的行为；
- 冒烟层（``test_agent_smoke.py``）用真实模型，但**只记录质量、不作断言**（它自己写明了定位：
  价值是"发现协议级/兼容性问题"，不是打分）。

于是在 2026-09-30 的成本/精度优化里出现了这样的处境：一轮改动把"行号级引用"从 0–3 处提到 22–41
处（工具集补上 ``grep`` / ``glob`` 之后），但**没有任何机制能证明下一轮改动不会把它打回去**——
只能靠人工每次看结果。这一层补的就是它。

跑法
----
```powershell
$env:DS_PRECISION = "1"          # 也用 DS_PRECISION_MODEL 覆盖模型名
uv run pytest tests/test_agent_eval_precision.py -q
```

它跑四条**合成工作区**用例（标准答案由代码生成、不依赖真实仓库的状态），把结果写成
``eval/precision-latest.json`` / ``.md``。

断言口径（取下限，避免真实模型的随机性造成假失败）
--------------------------------------------------
1. 回答里**不得出现内部自审或内部术语**——这是硬规则，不是打分。2026-09-30 实测过一次泄漏：
   反思模型的 ``anomaly_explanation``（"问题出在模型回答的行为：它把本应执行的最后一步…"）
   被原样拼进了面向用户的追问；
2. 定位类用例：**行号级引用条数 ≥ 阈值**，且必须提到指定的文件；
3. 定位类用例：**至少一次检索调用**（``grep`` / ``glob``）——"先定位再定点读"是本轮优化的核心
   行为，退化成"通读"必须被看见；
4. 事实正确性：回答里必须出现工作区里**唯一**的标准答案串（不是"像不像"，是"对不对"）；
5. 沿用冒烟层的安全口径：不抛异常、到达终点。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

from local_agent import config as config_module
from local_agent.application.models import cache_hit_ratio
from local_agent.application.projection import _counts_toward_usage, project_usage
from local_agent.config import AppConfig
from local_agent.graph import builder
from local_agent.llm import factory
from local_agent.llm.usage_recording import ModelCallRecorder, endpoint_fingerprint
from local_agent.persistence import checkpoint
from local_agent.ui.streaming import use_sink

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / "eval"
REPORT_JSON = REPORT_DIR / "precision-latest.json"
REPORT_MD = REPORT_DIR / "precision-latest.md"

#: 检索类工具：定位类用例至少要用到其中一个。
SEARCH_TOOLS: tuple[str, ...] = ("grep", "glob")

#: 回答里一旦出现就说明内部机制外显了（§15.8：页面只展示**可验证的执行过程**摘要）。
#:
#: 前两条是 2026-09-30 实测到的那次泄漏的原文特征，这一层把它固化成硬断言。
#:
#: **「机械校验」「证据等级」也曾被刻意排除**：它们确实像内部术语，但应用自己的用户可见文案里
#: 就在用（``_ask_user_text`` 的证据说明句、兜底摘要里的评级行），列为标记会让这一层去评判应用的
#: 文案而不是模型的自审，必然误报。**2026-09-30 晚些时候那句文案已收紧**（改成
#: :func:`local_agent.graph.nodes._trust_note` 的面向用户说明），排除的前提没了，因此这两个词
#: 现在**收进标记**——否则"应用的文案又退回去"这件事永远不会被任何一层抓到。
LEAK_MARKERS: tuple[str, ...] = (
    "问题出在模型回答的行为",
    "本应执行的最后一步",
    "证据被截断",
    "空转",
    "反思节点",
    "内部诊断",
    "机械校验",
    "证据等级",
    "anomaly",
    "reasoning_content",
)

#: 行号级引用：``path/to/file.py:123`` 或 ``path/to/file.py:12-34``。
#:
#: 行区间后缀必须一起吃进来：模型实际就是按区间引用的（``config.yaml:51-60``），只认到第一个数字
#: 会把区间引用的形态丢掉——离线用例当场抓到了这一点。
CITATION_PATTERN = re.compile(
    r"[\w./\\-]+\.(?:py|md|json|yaml|yml|txt)\s*:\s*\d+(?:\s*-\s*\d+)?"
)

#: 长文件用例的目标体量：**必须明显超过** ``read_file`` 的单次默认上限（20000 字符），
#: 这样"末尾哨兵"只有靠 ``next_start_line`` 续读才拿得到。
LONG_FILE_CHARS = 26_000

#: "必须给出结论"的**总体下限**（条数）。
#:
#: 刻意**不按单条硬断言**：这一层用真实模型，同一条用例两次运行可能一次收尾、一次追问——单条
#: flaky 会让门槛随机变红，而随机变红的门槛等于没有门槛。改成总体下限后，**系统性退化**
#: （例如模型不再收尾、三条全变 ``ask_user``）仍然会红，单条抖动则不会。
#:
#: 实测依据：2026-09-30 修完空转保护后，``locate_all`` 由 ``ask_user`` 变 ``completed``，而
#: 另外两条在同一轮里由 ``completed`` 变 ``ask_user``——同一条用例、同一份代码，两次运行结果不同。
MIN_COMPLETED_CASES = 2


def citations_in(text: str) -> list[str]:
    """回答里的行号级引用（去重、排序）。**评分口径本身也是被测对象**（见离线用例）。"""
    return sorted({match.strip() for match in CITATION_PATTERN.findall(text)})


def leaked_markers(text: str) -> list[str]:
    """回答里出现的内部机制标记；空列表表示干净。"""
    return [marker for marker in LEAK_MARKERS if marker in text]


def denied_tools(state: Mapping[str, Any]) -> list[str]:
    """本轮**被安全策略拒掉**的工具调用（``status == "denied"``）。

    为什么这条必须是硬断言（2026-09-30 加入）：检索工具刚上线时，``grep(path='README.md')``
    因为我把"检索根必须是目录"写死而被拒——一轮里被拒 4 次、界面弹出 4 个红框，模型只好自己改写成
    ``glob='README.md'`` 绕过去。而这一层当时只断言"用过检索工具"，**没有任何一条断言能发现"工具
    被拒"**，于是缺陷从单元测试和评测两层底下漏了过去，直到用户看截图才发现。

    工具被拒**永远不是正常路径**：要么接线错了（白名单没登记、路径判据过严），要么模型的这种用法
    本就该被支持。所以它是硬失败，不是"记录一下"。

    ``status == "error"`` 是另一回事——那是工具真的执行了但失败，属于正常结果，不在这里报。
    """
    return [
        str(record.get("tool_name"))
        for record in (state.get("tool_results") or [])
        if str(record.get("status") or "") == "denied"
    ]


def _long_tail_file() -> str:
    """造一个超过单次读取上限的文件：哨兵常量**只在文件末尾**出现。

    它顺带回答一个一直悬着的问题——``read_file`` 的单次默认上限（20000 字符）会不会让模型
    **读不到文件尾部**。只要模型会用返回里的 ``next_start_line`` 续读，末尾的哨兵就能被找到；
    找不到就说明"读不全"仍然存在，且这次是被自动测出来的。

    结构上有意分三段：干扰常量落在**前 20000 字符之内**（只读开头就会答错，用它识别这种情形），
    目标常量落在 26000 字符之后（不续读就拿不到）。离线用例同时钉住这两条位置约束。
    """

    def chars(items: list[str]) -> int:
        return sum(len(item) + 1 for item in items)

    lines = ["# 长文件用例：内容填充，真正的目标常量在文件末尾。", ""]
    index = 0
    while chars(lines) < 15_000:
        lines.append(f"# 填充行 {index:04d} " + "x" * 30)
        index += 1
    lines += [
        "",
        "# 干扰常量：它**不是**答案。回答里出现它，说明只读了开头就作答。",
        "MAX_HEAD_SENTINEL = 111111",
        "",
    ]
    while chars(lines) < LONG_FILE_CHARS:
        lines.append(f"# 填充行 {index:04d} " + "x" * 30)
        index += 1
    lines += [
        "",
        "# ---- 文件末尾 ----",
        "MAX_TAIL_SENTINEL = 987654",
        "",
    ]
    return "\n".join(lines)


#: 为了让"检索"这一步真的有意义：除目标文件外再铺一批同名/近名的干扰文件。
_DISTRACTORS: tuple[tuple[str, str], ...] = tuple(
    (f"pkg/part_{index:02d}.py", f"# 干扰模块 {index}\nVALUE_{index} = {index * 11}\n")
    for index in range(18)
) + (
    ("docs/notes.md", "# 说明\n\n常量集中在 `pkg/` 下，命名形如 `MAX_*`。\n"),
    ("docs/limits.md", "# 上限\n\n这里只记录说明，不放常量定义。\n"),
)

_BASE_FILES: tuple[tuple[str, str], ...] = (
    ("pkg/alpha.py", "# 模块 alpha\n\nMAX_ALPHA = 7\n\n\ndef use() -> int:\n    return MAX_ALPHA\n"),
    ("pkg/beta.py", "import os\n\n\n# 关键上限\nMAX_BETA = 42\n\n\ndef limit() -> int:\n    return MAX_BETA\n"),
    (
        "pkg/mode.py",
        (
            "# 模块 mode\n\n"
            'FILE_HINTS = ("read", "write", "list")\n\n\n'
            "def decide_mode(request: str) -> str:\n"
            '    """请求里出现 FILE_HINTS 的任意词就走工具分支，否则闲聊。"""\n'
            "    lowered = request.lower()\n"
            "    if any(word in lowered for word in FILE_HINTS):\n"
            '        return "tool"\n'
            '    return "chat"\n'
        ),
    ),
    ("pkg/long_tail.py", _long_tail_file()),
    *_DISTRACTORS,
)


@dataclass(frozen=True, eq=False)
class PrecisionCase:
    """一条精度用例：合成工作区 + 可机械判定的期望。"""

    case_id: str
    title: str
    user_input: str
    #: 回答里必须出现的**标准答案**串（工作区里唯一，错了就是错了）。
    must_contain: tuple[str, ...]
    #: 回答里必须提到的文件（相对路径）。
    must_cite: tuple[str, ...]
    #: 行号级引用的条数下限。
    min_citations: int
    #: 是否要求出现检索调用。
    require_search: bool = True
    #: 是否把这一条计入"必须给出结论"的**总体**统计（见 :data:`MIN_COMPLETED_CASES`）。
    #:
    #: 单独成一条，因为它与"定位准不准"是两件事：2026-09-30 的基线里，用例**内容答对了却以
    #: ``ask_user`` 收尾**——用户拿到的不是答案，而是"请确认下一步要做什么"。那是已知缺陷
    #: （空转保护那条路径），这一层负责让它不再隐形。
    counts_toward_completion: bool = True


CASES: tuple[PrecisionCase, ...] = (
    PrecisionCase(
        case_id="locate_one",
        title="单点定位",
        user_input="MAX_BETA 的值是多少？它定义在哪个文件的第几行？",
        must_contain=("42",),
        must_cite=("pkg/beta.py",),
        min_citations=1,
    ),
    PrecisionCase(
        case_id="locate_all",
        title="汇总定位（必须检索，通读不可行）",
        user_input="项目里所有形如 MAX_ALPHA / MAX_BETA 的上限常量分别定义在哪个文件的哪一行？值是多少？",
        must_contain=("7", "42"),
        must_cite=("pkg/alpha.py", "pkg/beta.py"),
        min_citations=2,
    ),
    PrecisionCase(
        case_id="read_to_tail",
        title="读全（文件超过单次读取上限，末尾常量）",
        user_input="pkg/long_tail.py 里 MAX_TAIL_SENTINEL 的值是多少？它在第几行？",
        must_contain=("987654",),
        must_cite=("pkg/long_tail.py",),
        min_citations=0,
        require_search=False,
    ),
    PrecisionCase(
        case_id="natural_question",
        title="自然问法（必须解释行为，不是报行号）",
        # 与 `test_agent_eval.py::rag-natural-question-asks` **同一个问题形态、同一类已知缺口**：
        # 那边记录的是「检索、注入、来源定位都对，但回答不完整 → 诚实追问」，用假模型回放。
        # 这里补上真实模型下的同一问法，作为"检索盲区修好之后是否改善"的对照。
        user_input="pkg/mode.py 里的 decide_mode 是怎么判断走工具分支还是闲聊的？",
        # 机械判据取**最独特的符号**（`FILE_HINTS`），**不**取分支字符串 `"tool"` / `"chat"`：
        # 中文回答极可能写成"工具分支 / 闲聊"，拿字面比对会假失败——这一层的口径本就是"取下限"。
        must_contain=("FILE_HINTS",),
        must_cite=("pkg/mode.py",),
        min_citations=1,
        # **不要求检索调用**：提问**直接点名了文件**，模型打开它就够（与 `read_to_tail` 同理）。
        # 第一版漏了这条，真实运行拿到 4 处行号引用、`completed`、0 泄漏，却因为"没调 grep"变红——
        # 那是**用例写错**，不是系统的问题。自然问法要测的是"回答完不完整"，不是"用不用检索工具"。
        require_search=False,
    ),
)


@dataclass
class _RecordingSink:
    """记录流式增量块（``llm.streaming`` 打开时才用得上）。"""

    pushed: list[str] = field(default_factory=list)

    def push(self, text: str) -> None:
        self.pushed.append(text)

    def discard(self) -> None:  # pragma: no cover - 本层不关心丢弃次数
        pass


@dataclass
class CaseResult:
    case: PrecisionCase
    endpoint: str = "unknown"
    tools: list[str] = field(default_factory=list)
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    leaks: list[str] = field(default_factory=list)
    #: 被安全策略拒掉的工具名（硬失败，见 :func:`denied_tools`）。
    denied: list[str] = field(default_factory=list)
    #: 用量取值失败时的现场（见 :func:`_usage_totals`）。**只在取不到时才有内容**：用量列显示 0
    #: 时，"到底是一条记录都没有，还是投影没取到"必须能分辨——否则下次又是靠猜。
    usage_diagnostics: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    uncached_input: int = 0
    cache_read: int = 0
    output_tokens: int = 0
    cache_hit: float | None = None
    #: 用量明细的覆盖度：``usage_missing``/``usage_expected``。两者不等时上面三个数是**部分和**。
    #: ``usage_records`` 是明细总条数（含被 §15.6 排除的失败尝试），只作参考。
    usage_records: int = 0
    usage_expected: int = 0
    usage_missing: int = 0
    hard_failures: list[str] = field(default_factory=list)

    @property
    def search_calls(self) -> int:
        return sum(1 for name in self.tools if name in SEARCH_TOOLS)


def required_answer_problems(item: CaseResult) -> list[str]:
    """事实断言独立于终态：标记 completed 不能掩盖缺失的标准答案。"""
    label = item.case.case_id
    problems = [
        f"{label}: 回答里没有提到必须命中的文件 {path}"
        for path in item.case.must_cite if path not in item.answer
    ]
    problems.extend(
        f"{label}: 回答里没有出现标准答案 {text!r}"
        for text in item.case.must_contain if text not in item.answer
    )
    return problems


def _load_local_config() -> AppConfig:
    """加载本机配置：优先 ``config.yaml``，否则示例配置；``DS_PRECISION_MODEL`` 可覆盖模型名。"""
    config_module.load_dotenv_file(PROJECT_ROOT / ".env")
    local = PROJECT_ROOT / "config.yaml"
    config = config_module.load_config(
        local if local.is_file() else PROJECT_ROOT / "config.yaml.example"
    )
    override = os.environ.get("DS_PRECISION_MODEL", "").strip()
    if override:
        data = config.model_dump()
        data["llm"]["model"] = override
        config = AppConfig.model_validate(data)
    return config


def _build_workspace(tmp_path: Path, case: PrecisionCase) -> Path:
    workspace = tmp_path / case.case_id
    workspace.mkdir(parents=True, exist_ok=True)
    for relative, content in _BASE_FILES:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    return workspace


def _endpoint_of(state: Mapping[str, Any]) -> str:
    if state.get("pending_approval") is not None:
        return "awaiting_approval"
    if state.get("task_status") == "completed":
        return "completed"
    action = getattr(state.get("reflection_result"), "next_action", None)
    if action:
        return str(action)
    return str(state.get("task_status") or "unknown")


@dataclass
class UsageTotals:
    """本轮用量 + **明细覆盖度**（2026-09-30）。

    ``with_usage`` < ``expected`` 时上面的数字是**部分和**（只累加有实际用量的那些明细）。这一点必须
    在报表里能看出来——否则"部分和"和"整轮实际值"长得一模一样。

    ``records`` 与 ``expected`` 的区别：前者是明细总条数，后者是**按 §15.6 应当有用量**的条数——
    没成功且没报告用量的调用（例如被端点 400 拒掉的尝试）不算缺口，投影层本来就排除它。
    """

    uncached: int = 0
    cache_read: int = 0
    output: int = 0
    cache_hit: float | None = None
    records: int = 0
    expected: int = 0
    with_usage: int = 0

    @property
    def missing(self) -> int:
        return max(0, self.expected - self.with_usage)


def _usage_totals(state: Mapping[str, Any]) -> UsageTotals:
    """本轮用量；**两级口径，都不伪造**。

    1. 首选应用层投影 :func:`project_usage`（页面同源，§15.6 的"汇总 = 明细加总"唯一实现）；
    2. 投影把某个字段汇总成 ``None`` 时，退化为**只累加有该字段的明细**，并如实记下覆盖度。

    为什么要第 2 级：``project_usage`` 在**任一**明细缺某字段时把该字段汇总为 ``None``——那对页面是
    对的（"未知即不显示"，绝不把缺当 0），但对"盯成本"这个目的太严：10 条明细里只要 1 条缺用量，
    整轮就显示 0，看起来像"根本没跑"。**不能因为一条缺就把已知的九条丢掉**，但**也不能把部分和
    冒充整轮值**，所以覆盖度跟着一起报（:class:`UsageTotals` 的 ``missing``）。

    前提是 :func:`_run_case` 给运行上下文**绑了录音机**——没有录音机一条记录都不会落（见那里），
    这一支会静默返回全 0，报表看起来"有值"其实是空的。这就是本函数配 :func:`_usage_diagnostics`
    的原因：0 必须能被分辨成"没记录"还是"取不到"。
    """
    # 投影要一个 ``run_id``。**不靠 ``state["run_id"]``**：实测直接调 ``project_usage(state)``
    # 在本层取不到本轮（返回 ``None``，报表里用量全是 0），而 ``turn_records`` 的键就是本轮 run_id，
    # 且按插入顺序排列——取最后一个即是刚跑完的那一轮。
    turns = state.get("turn_records")
    run_id = next(reversed(turns), None) if isinstance(turns, Mapping) and turns else None
    usage = project_usage(state, run_id) if run_id else project_usage(state)

    records = [
        record
        for record in (state.get("model_call_records") or [])
        if run_id is None or getattr(record, "run_id", "") == run_id
    ]
    totals = UsageTotals(records=len(records))

    for name, attribute in (
        ("uncached", "actual_uncached_input_tokens"),
        ("cache_read", "actual_cache_read_tokens"),
        ("output", "actual_output_tokens"),
    ):
        projected = None if usage is None else getattr(usage, attribute, None)
        if projected is not None:
            setattr(totals, name, int(projected))
        else:
            setattr(
                totals,
                name,
                sum(
                    int(getattr(record, attribute))
                    for record in records
                    if getattr(record, attribute, None) is not None
                ),
            )

    # 覆盖度按"**应当**有用量的明细"算，**直接复用投影层那条规则**（不在这里重写一遍"什么算失败
    # 尝试"）：被 400 拒掉的调用不算缺口，它留着只是为了让失败可见。
    totals.expected = sum(1 for record in records if _counts_toward_usage(record))
    totals.with_usage = sum(
        1
        for record in records
        if _counts_toward_usage(record)
        and getattr(record, "actual_uncached_input_tokens", None) is not None
    )
    # 命中率**用应用层那个函数**算（不自己写一遍除法），输入就是上面已定好的两个数。
    totals.cache_hit = cache_hit_ratio(
        totals.uncached if totals.with_usage else None,
        totals.cache_read if totals.with_usage else None,
        None,
    )
    return totals


def _usage_diagnostics(state: Mapping[str, Any]) -> dict[str, Any]:
    """取不到用量时的现场：0 必须能被分辨成"没记录"还是"投影没取到"。

    2026-09-30 加。此前用量一列永远显示 0，两种完全不同的原因（没绑录音机 / 投影字段为空）看起来
    一样，只能靠猜——上一次我就猜错了一次。
    """
    turns = state.get("turn_records")
    records = list(state.get("model_call_records") or [])
    return {
        "model_call_records": len(records),
        "turn_record_keys": list(turns) if isinstance(turns, Mapping) else None,
        "state_run_id": state.get("run_id"),
        # **缺用量的是哪一类调用**：按 purpose 列出来。实测每次都恰好缺 1 条，而"整轮汇总塌成
        # None"与"页面缓存命中显示未知"是同一个后果，所以这一条必须能一眼看到是哪条路径丢的。
        "missing_purposes": [
            str(getattr(item, "purpose", None))
            for item in records
            if getattr(item, "actual_uncached_input_tokens", None) is None
        ],
        # 被 §15.6 排除的失败尝试（没成功且没报告用量）：它们**不算缺口**，但必须仍然可见——否则
        # "按规则排除"和"整条记录丢了"在报表里长得一样。
        "excluded_failed_purposes": [
            str(getattr(item, "purpose", None)) for item in records if not _counts_toward_usage(item)
        ],
    }


def _run_case(
    case: PrecisionCase,
    *,
    tmp_path: Path,
    base: AppConfig,
    model: Any,
) -> CaseResult:
    workspace = _build_workspace(tmp_path, case)
    data = base.model_dump()
    # 检索默认开着：定位类用例的前提就是"模型有检索手段可用"。
    data["rag"]["enabled"] = True
    # **必须自己建录音机**（2026-09-30 修）：`builder._usage_aware` 取不到
    # `RuntimeContext.usage_recorder` 时直接返回、不合并 `model_call_records`，而那份录音机是
    # **应用服务层**（`service.py`）建好传进来的——进程内评测拿不到，于是报表里用量一列全是 0，
    # "顺带盯成本"这个目的完全落空。这里按 `service.py` 同样的字段建一份。
    context = builder.build_runtime_context(
        app_config=AppConfig.model_validate(data),
        model_adapter=model,
        workspace_root=workspace,
        current_cwd=workspace,
        usage_recorder=ModelCallRecorder(
            connection_id="precision-eval",
            config_version="1",
            provider=base.llm.provider,
            model_id=base.llm.model,
            endpoint_fingerprint=endpoint_fingerprint(base.llm.base_url, base.llm.model),
            context_window_tokens=base.llm.context_window_tokens,
        ),
    )
    thread_id = f"precision-{case.case_id}"
    thread = {"configurable": {"thread_id": thread_id}}
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(tmp_path / f"{case.case_id}.sqlite"))

    failure: str | None = None
    started = time.monotonic()
    try:
        with use_sink(_RecordingSink()):
            result = graph.invoke(
                {"user_input": case.user_input, "thread_id": thread_id},
                config=thread,
                context=context,
            )
            # 只读用例本不该弹审批；真弹了就一律拒绝，把"模型选了需要审批的路"记成失败。
            for _ in range(4):
                pending = result.get("pending_approval")
                if not pending:
                    break
                result = graph.invoke(
                    Command(resume={"decision": "reject"}),
                    config=thread,
                    context=context,
                )
    except Exception as exc:  # noqa: BLE001 —— 失败要原样记下来，不能吞
        failure = f"{type(exc).__name__}: {exc}"
        result = dict(graph.get_state(thread).values or {})

    state = dict(result)
    answers = [
        str(message.text).strip()
        for message in (state.get("messages") or [])
        if type(message).__name__ == "AIMessage" and str(message.text).strip()
    ]
    answer = "\n\n".join(answers)
    totals = _usage_totals(state)
    denied = denied_tools(state)
    # 有缺口、**或**有被排除的失败尝试时才留现场：后者不算缺口，但"按规则排除"与"记录丢了"
    # 必须在报表里分得开。
    diagnostics = (
        _usage_diagnostics(state) if totals.missing or totals.records != totals.expected else {}
    )
    outcome = CaseResult(
        case=case,
        endpoint=_endpoint_of(state),
        tools=[str(record.get("tool_name")) for record in (state.get("tool_results") or [])],
        answer=answer,
        citations=citations_in(answer),
        leaks=leaked_markers(answer),
        denied=denied,
        usage_diagnostics=diagnostics,
        seconds=round(time.monotonic() - started, 1),
        uncached_input=totals.uncached,
        cache_read=totals.cache_read,
        output_tokens=totals.output,
        cache_hit=totals.cache_hit,
        usage_records=totals.records,
        usage_expected=totals.expected,
        usage_missing=totals.missing,
    )

    if failure is not None:
        outcome.hard_failures.append(f"崩溃或接口错误：{failure}")
    if outcome.endpoint == "unknown":
        outcome.hard_failures.append("本轮没有到达任何终点")
    if outcome.endpoint == "awaiting_approval":
        outcome.hard_failures.append("只读用例弹出了审批：模型选了需要审批的路径")
    if denied:
        outcome.hard_failures.append(
            "有工具调用被安全策略拒掉：" + "、".join(denied) + "（工具被拒不是正常路径："
            "要么接线错了，要么这种用法本就该被支持）"
        )
    return outcome


def _write_report(results: list[CaseResult], *, model_name: str) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "cases": [
            {
                "case_id": item.case.case_id,
                "title": item.case.title,
                "endpoint": item.endpoint,
                "seconds": item.seconds,
                "tools": item.tools,
                "search_calls": item.search_calls,
                "citations": item.citations,
                "leaks": item.leaks,
                "denied_tools": item.denied,
                "usage_diagnostics": item.usage_diagnostics,
                "uncached_input_tokens": item.uncached_input,
                "cache_read_tokens": item.cache_read,
                "output_tokens": item.output_tokens,
                "usage_records": item.usage_records,
                "usage_expected": item.usage_expected,
                "usage_missing": item.usage_missing,
                "cache_hit_ratio": item.cache_hit,
                "hard_failures": item.hard_failures,
                "answer_head": item.answer[:400],
            }
            for item in results
        ],
    }
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# T23 精度层最近一次结果",
        "",
        f"- 时间：{payload['generated_at']}",
        f"- 模型：{model_name}",
        "",
        "| 用例 | 终点 | 检索调用 | 被拒 | 行号引用 | 泄漏 | 未命中输入 | 缓存命中 | 输出 | 缺用量 | 耗时(s) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        hit = "未知" if item.cache_hit is None else f"{item.cache_hit:.0%}"
        # 覆盖度跟着一起报：`缺用量` 不为 0 时，左边三个数是**部分和**，不是整轮实际值。
        coverage = f"{item.usage_missing}/{item.usage_expected}"
        lines.append(
            f"| {item.case.case_id} | {item.endpoint} | {item.search_calls} | "
            f"{len(item.denied)} | {len(item.citations)} | {len(item.leaks)} | "
            f"{item.uncached_input:,} | {hit} | {item.output_tokens:,} | {coverage} | {item.seconds} |"
        )
    lines += [
        "",
        "口径：`缺用量` = 没有实际用量的明细条数 / **应当有用量**的明细条数。不为 `0` 时三个 token 数是",
        "**部分和**（只累加有实际用量的明细）——不因一条缺就把已知的丢掉，也不把部分和冒充整轮值。",
        "失败的调用若**没有报告任何用量**不算缺口：按 ARCHITECTURE §15.6 ，被端点拒绝的请求没计费，",
        "它的\"缺\"是已知的零（投影层已排除它）。这类调用仍列在明细里，所以它能被看到、但不能被忽略。",
        "`缺用量` 的分子等于分母说明一条都没记上（通常是运行上下文没绑录音机，见文件内说明）。",
    ]
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- 离线：评分口径本身也要被测（它失效了，整层就白跑） --------------------


def test_citation_pattern_counts_ranges_and_ignores_words() -> None:
    text = "见 `pkg/beta.py:3` 与 src/a.py:12-34；另外 config.yaml:75 也要看。不是引用：python3。"
    found = citations_in(text)
    assert "pkg/beta.py:3" in found
    assert "src/a.py:12-34" in found
    assert "config.yaml:75" in found


@pytest.mark.parametrize("endpoint", ["completed", "ask_user"])
def test_required_facts_checked_regardless_of_completion(endpoint: str) -> None:
    item = CaseResult(case=CASES[0], endpoint=endpoint, answer="已经完成，答案是 41。")
    assert len(required_answer_problems(item)) == 2
    item.answer = "MAX_BETA 为 42，定义在 pkg/beta.py:5。"
    assert required_answer_problems(item) == []


def test_leak_markers_catch_the_2026_09_30_leak() -> None:
    """把实测到的那段泄漏原文固化下来：它必须被判定为泄漏。"""
    leaked = (
        "当前情况：问题出在模型回答的行为：它把本应执行的最后一步（汇总并输出架构说明）"
        "替换成了「你想看哪一块」的选项追问。"
    )
    markers = leaked_markers(leaked)
    assert "问题出在模型回答的行为" in markers
    assert "本应执行的最后一步" in markers
    assert leaked_markers("MAX_BETA 定义在 pkg/beta.py:4，值是 42。") == []


def test_denied_tools_flags_the_2026_09_30_grep_denial() -> None:
    """把实际发生过的"工具被拒"固化成可判定的形态——这条断言本该在缺陷上线时就红。

    实测原文：``grep(path='README.md')`` 因为检索根被写死成"必须是目录"而被拒，一轮里 4 次；
    而当时的评测只断言"用过检索工具"，**没有任何一条能发现"工具被拒"**，于是它从单元测试和评测
    两层底下漏了过去。``status == "error"`` 是另一回事（工具真的跑了但失败），不算被拒。
    """
    state = {
        "tool_results": [
            {"tool_name": "grep", "status": "denied"},
            {"tool_name": "grep", "status": "ok"},
            {"tool_name": "read_file", "status": "error"},
        ]
    }

    assert denied_tools(state) == ["grep"]
    assert denied_tools({"tool_results": []}) == []
    assert denied_tools({}) == []


def test_long_tail_file_really_exceeds_the_single_read_limit() -> None:
    """长文件用例必须真的超过 ``read_file`` 的单次默认上限，否则它测不到"读全"。"""
    body = _long_tail_file()
    assert len(body) > LONG_FILE_CHARS > 20_000
    # 哨兵只在末尾：前 20000 字符里不许出现它（否则不用续读也能答对，用例就空了）。
    assert "MAX_TAIL_SENTINEL" not in body[:20_000]
    assert "MAX_TAIL_SENTINEL = 987654" in body
    # 干扰常量必须落在前段，用来识别"只读了开头就作答"。
    assert "MAX_HEAD_SENTINEL" in body[:20_000]
    # 哨兵行号必须足够靠后，才逼得出 ``next_start_line`` 续读。
    tail_line = body.splitlines().index("MAX_TAIL_SENTINEL = 987654") + 1
    assert tail_line > 400, f"哨兵只落在第 {tail_line} 行，起不到「必须续读」的作用"


# ---- 真实模型：默认跳过（要联网、有费用、输出不稳定） ---------------------


@pytest.mark.skipif(
    os.environ.get("DS_PRECISION") != "1",
    reason="精度层用真实模型，要联网且有费用：显式设置 DS_PRECISION=1 才运行（见文件头说明）",
)
def test_real_model_precision(tmp_path: Path) -> None:
    """跑四条合成用例，断言定位准确性、"不泄漏内部机制"与"自然问法要给出结论"。"""
    base = _load_local_config()
    model = factory.build_chat_model(base.llm, config_module.resolve_api_key(base.llm))

    results = [_run_case(case, tmp_path=tmp_path, base=base, model=model) for case in CASES]
    _write_report(results, model_name=base.llm.model)

    problems: list[str] = []
    asked: list[str] = []
    for item in results:
        label = item.case.case_id
        problems.extend(f"{label}: {detail}" for detail in item.hard_failures)
        problems.extend(required_answer_problems(item))
        for marker in item.leaks:
            problems.append(f"{label}: 回答泄漏内部机制用语「{marker}」")
        if len(item.citations) < item.case.min_citations:
            problems.append(
                f"{label}: 行号级引用 {len(item.citations)} 处，低于下限 {item.case.min_citations}"
                f"（回答里只提到文件、没给行号，等于定位不准）"
            )
        if item.case.require_search and item.search_calls == 0:
            problems.append(
                f"{label}: 一次检索调用都没有（grep/glob）——退化成通读了；工具链：{item.tools}"
            )
        if item.case.counts_toward_completion and item.endpoint != "completed":
            asked.append(f"{label}={item.endpoint}")

    # "必须给出结论"按**总体**卡，不按单条卡（见 :data:`MIN_COMPLETED_CASES`）。
    if len(asked) > len(CASES) - MIN_COMPLETED_CASES:
        problems.append(
            f"只有 {len(CASES) - len(asked)}/{len(CASES)} 条用例给出了结论（下限 {MIN_COMPLETED_CASES}）："
            f"{'、'.join(asked)}——事实与引用可能都对，说明它知道答案却停在追问/中止"
        )

    assert REPORT_JSON.is_file() and REPORT_MD.is_file()
    assert problems == [], "；".join(problems)

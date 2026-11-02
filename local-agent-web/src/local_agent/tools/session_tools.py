"""历史会话检索工具（`session_search`，2026-11-02）。

它回答的问题只有一类：**"我们之前说过什么"**。用户问"上次那个方案怎么定的""之前那台机器的
端口是多少"，模型不该让用户重述——本机所有会话的提问与回答都已经有全文索引
（``persistence/turn_index.py``，FTS5 + CJK 二元组），这里只把检索结果交回去。

三条边界写死在这里，不是自由发挥：

1. **只读、免审批**：不改任何状态，也不发外部请求（登记在 ``security.policy.READ_ONLY_TOOLS``）。
   真正的闸门是能力开关，默认**关**（见 ``capabilities.CAPABILITY_DEFAULTS``）：出厂工具前缀因此
   逐字不变（§16.1），打开它才是有意的前缀变化。
2. **只给片段，不给整份会话**：条数与字符都截断。历史回答可能有几十 KB，整份塞回上下文等于
   用历史把这一轮挤爆——那是"检索"退化成"搬库"。
3. **归档会话也搜得到**：归档是"在侧栏里离开视线"，不是删除；找旧结论时它往往是唯一出处。
   命中的会话带 ``archived`` 标记，模型可以据此说明"这是已归档会话里的结论"。

工具本身不直接查库：它只拿装配时闭包进来的检索入口（与 ``delegate_research`` 同一套 A 方案，
见 ``tools/delegate_tools.py`` 的注释），因为工具执行节点拿不到 ``RuntimeContext``。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, Field

#: 运行侧注入的检索入口：``(query, limit) -> 命中列表``。
#:
#: 返回**纯字典**而不是本模块的结果模型：应用层不该为了喂一个工具去 import 工具模块的模型，
#: 而工具这一层本来就要做"截断与去噪"——两件事分开，谁也别越界。
SessionSearchFn = Callable[[str, int], Sequence[Mapping[str, object]]]

#: 单条命中里问题/回答最多留多少字符。数字不是拍的：一条工具结果整体还要受 `result_spill`
#: 与提示预算约束，5 条 ×（240 + 480）约 3.6k 字符，属于"引用得起"的量级。
MAX_QUESTION_CHARS = 240
MAX_ANSWER_CHARS = 480

#: 一次最多取几条。上限 10 是"够核对"与"别把上下文塞满"之间的折中。
MAX_LIMIT = 10

DOC = (
    "在本机**历史会话**里检索（跨会话、含已归档），返回每一处命中的会话、轮次与原文片段。"
    "适合：用户说'上次/之前/我们讨论过'，或你需要引用他自己早前定过的结论、路径、端口、约定。"
    "不适合：找当前工作区里的事实（用 grep / read_file）、查外部资料（用 web_search）。"
    "结果只是**片段**：引用时必须写明来自哪个会话的哪一轮，不能把片段当成当前代码的事实。"
)


class SessionSearchQuery(BaseModel):
    """工具入参。"""

    query: str = Field(min_length=1, max_length=200)
    #: 最多返回几条；`5` 对绝大多数"回忆一下"足够，核对型问题可调到 10。
    limit: int = Field(default=5, ge=1, le=MAX_LIMIT)


class SessionHit(BaseModel):
    """一条命中：哪个会话的哪一轮，原文片段，以及它是否已归档。"""

    session_id: str
    turn_id: str
    title: str
    asked_at: str | None = None
    archived: bool = False
    question: str
    answer: str


class SessionSearchResult(BaseModel):
    """`session_search` 的结果。

    ``status=failed`` 时 ``hits`` 为空，``note`` 说的是**为什么没查成**（没接上检索入口、
    参数不合法等）；不要把它当成"没有这段历史"。
    """

    status: Literal["completed", "failed"]
    hits: list[SessionHit] = Field(default_factory=list)
    note: str | None = None


_WHITESPACE = re.compile(r"\s+")


def _clip(text: object, limit: int) -> str:
    """压成一行并截断。保留原样的换行没有意义——它只会让一条工具结果占满屏幕。"""
    collapsed = _WHITESPACE.sub(" ", str(text or "")).strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _hit(item: Mapping[str, object]) -> SessionHit:
    return SessionHit(
        session_id=str(item.get("session_id") or ""),
        turn_id=str(item.get("turn_id") or ""),
        title=str(item.get("title") or ""),
        asked_at=None if item.get("asked_at") is None else str(item.get("asked_at")),
        archived=bool(item.get("archived", False)),
        question=_clip(item.get("question"), MAX_QUESTION_CHARS),
        answer=_clip(item.get("answer"), MAX_ANSWER_CHARS),
    )


def build_session_tools(search: SessionSearchFn | None) -> list[BaseTool]:
    """构造 `session_search`。

    与 `delegate_tools` / `skill_tools` 同一口径：**总是被构造**（能力覆盖校验要求登记的工具都在
    注册表里），"能不能用"由能力层决定。``search`` 为 ``None`` 时（CLI、脚本、委派子图这些没有
    会话索引的运行路径）调用会得到一句明确的失败，而不是静默的空结果。
    """

    @langchain_tool("session_search", args_schema=SessionSearchQuery, description=DOC)
    def session_search(query: str, limit: int = 5) -> str:
        """在本机历史会话里检索提问与回答。"""
        if search is None:
            return SessionSearchResult(
                status="failed",
                note="当前运行路径没有接上会话检索入口（CLI / 脚本 / 子 Agent 都是这样），"
                     "这不是你的调用方式问题。",
            ).model_dump_json()
        text = query.strip()
        if not text:
            return SessionSearchResult(status="failed", note="检索词不能为空。").model_dump_json()
        try:
            items = search(text, max(1, min(int(limit), MAX_LIMIT)))
        except (ValueError, TypeError) as exc:
            return SessionSearchResult(status="failed", note=str(exc)[:512]).model_dump_json()
        hits = [_hit(item) for item in items]
        if not hits:
            return SessionSearchResult(
                status="completed",
                note="没有命中任何历史轮次。换个说法再试，或者这条历史确实不在本机。",
            ).model_dump_json()
        return SessionSearchResult(status="completed", hits=hits).model_dump_json()

    return [session_search]

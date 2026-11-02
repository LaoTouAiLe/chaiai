"""历史会话检索的**应用层入口**（2026-11-02；`session_search` 工具的运行侧实现）。

分工：工具（`tools/session_tools.py`）只管**截断与措辞**，不碰数据库；它拿到的检索函数由装配层
闭包进来。这里就是那个函数——把工具要的字段取齐（会话标题、轮次、问答原文片段）。

**不在这里吞异常**：查不动就让它以"工具失败"的形式如实出现（与 `delegate_research` 同一口径），
不能被伪装成"没有这段历史"——那会让模型把"查不到"说成"没发生过"。

为什么单独一个模块而不是 `service.py` 里的方法：那个文件有行数硬上限（`scripts/check_size.py`），
而这段逻辑与 service 只共享两个接缝（`_sqlite_access` 与 `_sessions`）。
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from local_agent.application.service import ApplicationService


def hits(service: ApplicationService, query: str, limit: int) -> list[dict[str, Any]]:
    """``session_search`` 的检索入口：``(query, limit) -> 命中列表``。

    返回**纯字典**：工具层负责截断与措辞，应用层只交事实，两边不互相 import 模型。
    走 ``_sqlite_access`` 是因为会话索引与 Checkpoint 共用一条连接，跨线程访问必须过那把锁。
    """
    details = service._sqlite_access(
        partial(service._sessions.search_turn_details, query, limit=limit)
    )
    return [
        {
            "session_id": item.session_id,
            "turn_id": item.turn_id,
            "title": item.title,
            "asked_at": item.submitted_at,
            "archived": item.archived,
            "question": item.question,
            "answer": item.answer_text,
        }
        for item in details
    ]

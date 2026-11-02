"""`session_search` 工具与能力的契约测试（2026-11-02）。

覆盖：没接入口时**明确失败而不是空结果**、截断口径、命中字段透传、空检索词被入参校验挡住、
能力默认关且可开关、工具登记为只读、以及跨会话检索（含已归档）能取回轮次明细。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from local_agent import capabilities
from local_agent.persistence import migrations
from local_agent.persistence.conversation_store import ConversationTurn
from local_agent.persistence.session_store import SessionStore
from local_agent.security.policy import READ_ONLY_TOOLS
from local_agent.tools.session_tools import MAX_ANSWER_CHARS, build_session_tools

#: 能力键（不是工具名）：两者刻意不同，见 `capabilities.CAPABILITY_TOOLS` 的注释。
CAPABILITY = "session_history"


def _invoke(search: Any, **payload: Any) -> dict[str, Any]:
    tool = build_session_tools(search)[0]
    return json.loads(tool.invoke(payload))


def _store(tmp_path: Path) -> SessionStore:
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    migrations.apply_migrations(connection)
    return SessionStore(connection, workspace_root=tmp_path)


def _turn(store: SessionStore, session_id: str, question: str, answer: str, ordinal: int = 1) -> None:
    store.conversations.sync(
        session_id,
        "main",
        [
            ConversationTurn(
                turn_id=f"{session_id}-t{ordinal}",
                parent_turn_id=None,
                origin_branch_id="main",
                run_id=None,
                question=question,
                answer_text=answer,
                state="completed",
                submitted_at="2026-11-01T10:00:00+08:00",
                checkpoint_id=None,
                ordinal=ordinal,
            )
        ],
        None,
    )


# ---------------------------------------------------------------- 工具外形


def test_tool_without_entry_point_reports_failure() -> None:
    """没有索引的运行路径（CLI / 脚本 / 子 Agent）：必须说清"没接上"，不能给空结果。

    空结果会被模型读成"这段历史不存在"，而事实上是这条路径查不了。
    """
    payload = _invoke(None, query="影子库")

    assert payload["status"] == "failed"
    assert payload["hits"] == []
    assert "没有接上" in payload["note"]


def test_empty_query_is_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        build_session_tools(None)[0].invoke({"query": ""})


def test_hits_are_clipped_and_whitespace_collapsed() -> None:
    long_answer = "结论：" + "很长的正文" * 200
    search = lambda query, limit: [
        {
            "session_id": "s1",
            "turn_id": "t1",
            "title": "回滚方案",
            "asked_at": "2026-11-01T10:00:00+08:00",
            "archived": False,
            "question": "回滚\n 怎么做",
            "answer": long_answer,
        }
    ]

    payload = _invoke(search, query="回滚")

    assert payload["status"] == "completed"
    hit = payload["hits"][0]
    assert hit["question"] == "回滚 怎么做"
    assert len(hit["answer"]) <= MAX_ANSWER_CHARS
    assert hit["answer"].endswith("…")


def test_no_hit_is_completed_with_a_note_not_failed() -> None:
    payload = _invoke(lambda query, limit: [], query="从没讨论过的事")

    # "没命中"是**查成功**的结论，不能和"查不了"混成一个状态。
    assert payload["status"] == "completed"
    assert payload["hits"] == []
    assert payload["note"]


# ---------------------------------------------------------------- 能力与安全登记


def test_capability_is_off_by_default_and_switchable() -> None:
    # 能力键故意不叫 `session_search`：面板文本里不得出现实现工具名（§16.6）。
    assert capabilities.CAPABILITY_DEFAULTS[CAPABILITY] is False
    assert CAPABILITY in capabilities.switchable_capabilities()
    assert CAPABILITY not in capabilities.LOCKED_CAPABILITIES
    # 不进任何内置档位：出厂前缀因此逐字不变（§16.1）。
    for profile in capabilities.PROFILES.values():
        assert CAPABILITY not in profile


def test_capability_follows_the_switch() -> None:
    off = capabilities.enabled_capabilities(
        {**capabilities.CAPABILITY_DEFAULTS}, git_available=True, vision_available=True
    )
    on = capabilities.enabled_capabilities(
        {**capabilities.CAPABILITY_DEFAULTS, CAPABILITY: True},
        git_available=True,
        vision_available=True,
    )

    assert CAPABILITY not in off
    assert CAPABILITY in on


def test_tool_is_registered_read_only() -> None:
    # 只读 = 不需要人工审批；闸门是能力开关，不是审批卡。
    assert "session_search" in READ_ONLY_TOOLS


# ---------------------------------------------------------------- 跨会话检索


def test_searches_across_sessions_including_archived(tmp_path: Path) -> None:
    store = _store(tmp_path)
    live = store.create(title="正在做的项目", workspace_root=tmp_path)
    old = store.create(title="去年的项目", workspace_root=tmp_path)
    _turn(store, live.session_id, "缓存怎么做", "本地加一层缓存就够了。")
    _turn(store, old.session_id, "回滚怎么做", "结论：用影子库逐文件比对后再覆盖。")
    store.set_archived(old.session_id, True)

    details = store.search_turn_details("影子库")

    assert [item.session_id for item in details] == [old.session_id]
    assert details[0].title == "去年的项目"
    assert details[0].archived is True
    assert "影子库" in details[0].answer_text


def test_limits_hits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(4):
        session = store.create(title=f"会话 {index}", workspace_root=tmp_path)
        _turn(store, session.session_id, "回滚怎么做", f"结论 {index}：用影子库。", ordinal=index + 1)

    assert len(store.search_turn_details("影子库", limit=2)) == 2

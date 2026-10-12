"""记忆来源可信级别 ``origin``（架构 §16.2、§10.2/§10.3；T52）。

三条要点各有一组用例：

1. **闭集 + 无默认**：候选必须显式给来源，越界一律拒绝——"判定不出来"不许当 ``owner``；
2. **写得到、读得出、但永不自动注入**：``untrusted`` / ``system`` 照常落库（审计与页面能看），
   只是进不了 Prompt；这是结构性排除，不是打分惩罚；
3. **历史数据按已知来源**：T52 之前的记录没有该字段，读取时按 ``owner``（那时唯一的写入者是
   本机反思链路，属已知来源，不是"判定不出来"）。

端到端用例走长期库（SQLite，不依赖可选依赖）；中期侧的 metadata 与召回过滤在
``tests/test_memory.py``（那里有 Chroma 的固定装置）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from local_agent.config import LongTermMemoryConfig
from local_agent.context import prompt_context
from local_agent.exceptions import MemoryStoreError
from local_agent.graph.nodes import _candidate_origin
from local_agent.graph.state import MemoryCandidate
from local_agent.memory import long_term, recall, writer
from local_agent.memory.schemas import (
    ORIGIN_OWNER,
    ORIGIN_SYSTEM,
    ORIGIN_UNTRUSTED,
    MemoryReference,
    is_auto_injectable,
    resolve_origin,
)
from local_agent.reflection.memory_candidates import RejectedCandidate, plan_writes
from local_agent.scope import SCOPE_PROJECT, project_id_for
from local_agent.session_kind import (
    SESSION_KIND_HEARTBEAT,
    SESSION_KIND_INTERACTIVE,
    SESSION_KIND_SCHEDULED,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

#: 事实候选的规范 JSON（``entity_fact`` 的 `content` 契约）。
FACT_JSON = json.dumps(
    {"entity_type": "project", "key": "language", "value": "中文"}, ensure_ascii=False
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[long_term.LongTermStore]:
    """长期记忆库（不依赖可选依赖，随时可跑）。"""
    path = tmp_path / "data" / "memories.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    opened = long_term.open_store(path)
    try:
        yield opened
    finally:
        opened.close()


def _candidate(*, origin: str, memory_type: str = "entity_fact") -> MemoryCandidate:
    return MemoryCandidate(
        memory_type=memory_type,  # type: ignore[arg-type]
        content=FACT_JSON if memory_type == "entity_fact" else "本轮完成了一件事",
        origin=origin,  # type: ignore[arg-type]
        subject="chaiai",
        importance=0.8,
        confidence=0.9,
        source_event_ids=("evt-1",),
    )


def _write(store: long_term.LongTermStore, workspace_root: Path, *, origin: str, memory_type: str = "entity_fact") -> None:
    """走**真实写入链路**（候选 → plan_writes → writer）：不绕过任何一层。"""
    intents = plan_writes(
        _candidate(origin=origin, memory_type=memory_type),
        workspace_root=workspace_root,
        task_verified=True,
    )
    assert isinstance(intents, tuple), intents
    outcomes = writer.write_intents(intents, long_store=store, now=NOW)
    assert outcomes, "至少要有一条写入结果"
    # task_fragment 会同时产生"中期 + 长期结论"两条意图；这里没给中期库，只有长期那条会真写
    written = [outcome for outcome in outcomes if outcome.written]
    assert written, outcomes
    assert all(outcome.target != "medium_term" or not outcome.written for outcome in outcomes)


def _recalled_ids(store: long_term.LongTermStore, workspace_root: Path) -> list[str]:
    references = recall.recall_long_term(
        store,
        config=LongTermMemoryConfig(),
        workspace_root=workspace_root,
        now=NOW,
    )
    return [str(item["memory_id"]) for item in references]


def _reference(memory_id: str, content: str, *, origin: str) -> MemoryReference:
    return MemoryReference(
        memory_id=memory_id,
        content=content,
        scope=SCOPE_PROJECT,
        scope_key="project-a",
        similarity=0.9,
        score=0.9,
        importance=0.8,
        confidence=0.9,
        created_at="2026-10-08T12:00:00+00:00",
        source_thread_id=None,
        expires_at=None,
        origin=origin,  # type: ignore[typeddict-item]
    )


# --------------------------------------------------------------------- 闭集与归一


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (ORIGIN_OWNER, True),
        (ORIGIN_SYSTEM, False),
        (ORIGIN_UNTRUSTED, False),
        # 闭集之外的值一律不可注入（失败闭合）：脏数据、将来新增的取值都落在这一侧
        (None, False),
        ("", False),
        ("weird", False),
    ],
)
def test_only_owner_is_auto_injectable(origin: object, expected: bool) -> None:
    assert is_auto_injectable(origin) is expected


def test_resolve_origin_separates_legacy_from_garbage() -> None:
    """读回归一：合法值原样；缺失/空白 = 历史记录（已知来源）；越界值 = 不可信（失败闭合）。"""
    assert resolve_origin(ORIGIN_UNTRUSTED) == ORIGIN_UNTRUSTED
    assert resolve_origin(None) == ORIGIN_OWNER
    assert resolve_origin("   ") == ORIGIN_OWNER
    assert resolve_origin("weird") == ORIGIN_UNTRUSTED


def test_candidate_requires_an_explicit_origin() -> None:
    """候选的 ``origin`` 没有默认值：写入链路必须显式声明来源（架构 §16.2）。"""
    with pytest.raises(TypeError):
        MemoryCandidate(  # type: ignore[call-arg]
            memory_type="conversation_summary",
            content="用户偏好中文",
            subject="",
            importance=0.6,
            confidence=0.8,
            source_event_ids=("evt-1",),
        )


def test_plan_writes_rejects_an_unknown_origin(tmp_path: Path) -> None:
    """越界的来源不许被静默归一，而是拒绝整条候选并说明原因。"""
    result = plan_writes(_candidate(origin="weird"), workspace_root=tmp_path)

    assert isinstance(result, RejectedCandidate)
    assert "origin" in result.reason


def test_store_rejects_an_unknown_origin_on_write(store: long_term.LongTermStore) -> None:
    """存储层的写入口同样失败闭合（不替调用方挑默认值）。"""
    entity = store.record_entity(
        entity_type="project", name="chaiai", scope=SCOPE_PROJECT, project_id="p", now=NOW
    )

    with pytest.raises(MemoryStoreError):
        store.upsert_fact(
            entity_id=entity.id,
            key="language",
            value="中文",
            confidence=0.9,
            importance=0.8,
            source="test",
            origin="weird",  # type: ignore[arg-type]
            now=NOW,
        )


# --------------------------------------------------------------------- 写入与召回


def test_owner_fact_is_written_and_recalled(
    store: long_term.LongTermStore, tmp_path: Path
) -> None:
    """对照组：owner 的事实照常召回——证明下面那条不是"整类记忆都不生效"。"""
    _write(store, tmp_path, origin=ORIGIN_OWNER)

    assert _recalled_ids(store, tmp_path) != []


@pytest.mark.parametrize("origin", [ORIGIN_UNTRUSTED, ORIGIN_SYSTEM])
def test_non_owner_fact_is_stored_but_never_recalled(
    store: long_term.LongTermStore, tmp_path: Path, origin: str
) -> None:
    """``untrusted`` / ``system`` 落库成功（审计可见），但一条都不进自动注入。"""
    _write(store, tmp_path, origin=origin)

    facts = store.active_facts_with_entities(
        recall.visible_scope_keys(
            user_id=None, project_id=project_id_for(tmp_path), workspace_root=tmp_path
        ),
        now=NOW,
    )
    assert [fact.origin for _, fact in facts] == [origin], "写入必须落标：库里要能看出它来自哪里"
    assert _recalled_ids(store, tmp_path) == []


@pytest.mark.parametrize("origin", [ORIGIN_OWNER, ORIGIN_UNTRUSTED])
def test_task_conclusion_follows_the_same_rule(
    store: long_term.LongTermStore, tmp_path: Path, origin: str
) -> None:
    """任务结论走另一张表、另一个写入函数，规则必须一致。"""
    _write(store, tmp_path, origin=origin, memory_type="task_fragment")

    recalled = _recalled_ids(store, tmp_path)
    assert bool(recalled) is (origin == ORIGIN_OWNER)


# --------------------------------------------------------------------- 提示词兜底


def test_render_memory_section_skips_non_injectable_sources() -> None:
    """即便有人绕过召回层把不可信引用塞进 State，它也不能出现在 Prompt 里（最后一道）。"""
    references = [
        _reference("m-owner", "本机事实甲", origin=ORIGIN_OWNER),
        _reference("m-web", "网页内容乙", origin=ORIGIN_UNTRUSTED),
        _reference("m-sys", "脚手架说明丙", origin=ORIGIN_SYSTEM),
    ]

    text = prompt_context.render_memory_section(references)

    assert "本机事实甲" in text
    assert "网页内容乙" not in text
    assert "脚手架说明丙" not in text


# ------------------------------------------------- 候选来源的三步优先级链（T57，B-03）


def _origin_state(**overrides: Any) -> dict[str, Any]:
    """``_candidate_origin`` 认得出的最小 State（只用到 ``session_kind`` 与 ``tool_results``）。"""
    state: dict[str, Any] = {
        "session_kind": SESSION_KIND_INTERACTIVE,
        "run_id": "run-1",
        "tool_results": [],
    }
    state.update(overrides)
    return state


def test_candidate_origin_is_owner_for_interactive_sessions() -> None:
    """对照组：本机用户在页面上发起的会话，来源仍是 ``owner``（既有行为不变）。"""
    assert _candidate_origin(_origin_state()) == ORIGIN_OWNER


@pytest.mark.parametrize("kind", [SESSION_KIND_SCHEDULED, SESSION_KIND_HEARTBEAT])
def test_candidate_origin_downgrades_non_interactive_sessions(kind: str) -> None:
    """B-03：定时 / 心跳会话不产生**可自动注入**的持久记忆——落库为 ``system``。"""
    assert _candidate_origin(_origin_state(session_kind=kind)) == ORIGIN_SYSTEM


def test_candidate_origin_prefers_untrusted_over_session_kind() -> None:
    """两个条件同时成立时取最保守的一侧：``untrusted`` 优先于 ``system``。"""
    state = _origin_state(
        session_kind=SESSION_KIND_SCHEDULED,
        tool_results=[
            {
                "tool_name": "web_fetch",
                "status": "ok",
                "run_id": "run-1",
                "tool_call_id": "call-1",
            }
        ],
    )

    assert _candidate_origin(state) == ORIGIN_UNTRUSTED


def test_candidate_origin_treats_missing_kind_as_interactive() -> None:
    """旧 Checkpoint 没有 ``session_kind``：按 ``interactive`` 处理，与聊天库 v7 的列默认值一致。"""
    state = _origin_state()
    state.pop("session_kind")

    assert _candidate_origin(state) == ORIGIN_OWNER


def test_candidate_origin_is_system_even_with_failed_web_call() -> None:
    """被拒 / 失败的外部调用没有带进内容 → 不该升级成 ``untrusted``，但会话来源门仍成立。"""
    state = _origin_state(
        session_kind=SESSION_KIND_HEARTBEAT,
        tool_results=[
            {
                "tool_name": "web_fetch",
                "status": "error",
                "run_id": "run-1",
                "tool_call_id": "call-1",
            }
        ],
    )

    assert _candidate_origin(state) == ORIGIN_SYSTEM

"""后台巩固的确定性校验（2026-11-02）：模型建议必须过一遍代码才落库。

这里不调模型（``decide_promotions`` 需要真实/假模型），只测"模型说了之后会发生什么"：
提示里给模型看什么、哪些建议会被拒、落库写成什么、以及它**只新增不改旧**。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from local_agent.memory import consolidation, long_term
from local_agent.scope import SCOPE_PROJECT, SCOPE_WORKSPACE

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
PROJECT_ID = "project-a"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[long_term.LongTermStore]:
    handle = long_term.open_store(tmp_path / "memories.sqlite")
    try:
        yield handle
    finally:
        handle.close()


def _fragment(
    memory_id: str = "m1",
    *,
    scope: str = SCOPE_PROJECT,
    scope_key: str = PROJECT_ID,
    origin: str = "owner",
    content: str = "任务结论：本项目用 pytest 而不是 unittest",
    created_at: str = "2026-11-02T10:00:00+00:00",
) -> consolidation.MediumFragment:
    return consolidation.MediumFragment(
        memory_id=memory_id,
        content=content,
        scope=scope,
        scope_key=scope_key,
        origin=origin,  # type: ignore[arg-type]
        created_at=created_at,
    )


def _promotion(**overrides: object) -> consolidation.Promotion:
    data: dict[str, object] = {
        "source_memory_id": "m1",
        "subject": "chaiai",
        "entity_type": "project",
        "key": "test_runner",
        "value": "pytest",
        "confidence": 0.9,
        "importance": 0.8,
    }
    data.update(overrides)
    return consolidation.Promotion.model_validate(data)


def _apply(
    store: long_term.LongTermStore,
    promotions: list[consolidation.Promotion],
    fragments: list[consolidation.MediumFragment] | None = None,
    *,
    limit: int = 5,
) -> consolidation.ConsolidationReport:
    return consolidation.apply_promotions(
        consolidation.PromotionPlan(promotions=promotions),
        fragments=fragments if fragments is not None else [_fragment()],
        long_store=store,
        now=NOW,
        max_promotions=limit,
    )


def test_messages_carry_fragment_ids_and_known_facts() -> None:
    """给模型看的是 id + 内容 + 已固化的事实，不是整段对话正文。"""
    messages = consolidation.build_messages(
        [_fragment()],
        [{"subject": "chaiai", "key": "editor", "value": '"vscode"'}],
    )

    payload = json.loads(messages[-1].content)  # type: ignore[arg-type]
    assert payload["fragments"][0]["id"] == "m1"
    assert "pytest" in payload["fragments"][0]["content"]
    assert payload["known_facts"][0]["key"] == "editor"


def test_promotion_becomes_a_project_fact_with_source_and_origin(
    store: long_term.LongTermStore,
) -> None:
    report = _apply(store, [_promotion()])

    assert (report.promoted, report.skipped) == (1, 0)
    rows = store.active_facts_with_entities([(SCOPE_PROJECT, PROJECT_ID)], now=NOW)
    assert len(rows) == 1
    entity, fact = rows[0]
    # 模型给的名字照用作**展示名**；身份由 (entity_type, scope, scope_key) 决定
    assert entity.name == "chaiai"
    assert fact.key == "test_runner"
    assert json.loads(fact.value_json) == "pytest"
    # 来源与可信度只沿用、不提升
    assert fact.source == consolidation.SOURCE_KIND
    assert fact.origin == "owner"


def test_invented_source_id_is_rejected(store: long_term.LongTermStore) -> None:
    report = _apply(store, [_promotion(source_memory_id="m-does-not-exist")])

    assert report.promoted == 0
    assert report.skipped == 1
    assert report.reasons and "不在本次输入" in report.reasons[0]
    assert store.active_facts_with_entities([(SCOPE_PROJECT, PROJECT_ID)], now=NOW) == []


def test_workspace_scope_is_not_promoted(store: long_term.LongTermStore) -> None:
    """整理不得改变可见范围：工作区级碎片不进长期库。"""
    fragment = _fragment(scope=SCOPE_WORKSPACE, scope_key="ws-1")
    report = _apply(store, [_promotion()], [fragment])

    assert report.promoted == 0
    assert report.reasons and "不参与固化" in report.reasons[0]


def test_untrusted_origin_is_not_promoted(store: long_term.LongTermStore) -> None:
    """来源不可信的碎片固化后也不会被召回：与其制造垃圾，不如跳过并说明。"""
    report = _apply(store, [_promotion()], [_fragment(origin="untrusted")])

    assert report.promoted == 0
    assert report.reasons and "来源不可信" in report.reasons[0]


def test_promotions_are_capped(store: long_term.LongTermStore) -> None:
    fragments = [_fragment(memory_id=f"m{index}") for index in range(3)]
    promotions = [
        _promotion(source_memory_id=f"m{index}", key=f"key{index}", value=f"v{index}")
        for index in range(3)
    ]

    report = _apply(store, promotions, fragments, limit=1)

    assert report.promoted == 1
    assert report.skipped == 2
    # reasons 里可能先有"实体名归一"的审计条目，所以按内容找而不是按下标
    assert any("超过单次上限" in reason for reason in report.reasons)


def test_different_subjects_share_one_entity(store: long_term.LongTermStore) -> None:
    """模型换一种叫法不该新建实体（2026-11-02 实测过这次分裂：同一项目出现两份实体）。"""
    _apply(store, [_promotion(subject="local-agent-web", key="test_runner", value="pytest")])
    _apply(store, [_promotion(subject="user", key="reply_language", value="中文")])

    rows = store.active_facts_with_entities([(SCOPE_PROJECT, PROJECT_ID)], now=NOW)

    assert len(rows) == 2
    # 同一个实体（id 相同），展示名沿用第一次写入的那个
    assert len({entity.id for entity, _fact in rows}) == 1
    assert {entity.name for entity, _fact in rows} == {"local-agent-web"}
    assert {entity.entity_type for entity, _fact in rows} == {"project"}


def test_entity_type_follows_scope_not_the_model(
    store: long_term.LongTermStore,
) -> None:
    """作用域由源碎片决定；模型说 user 也不能把这条抬成跨项目可见。"""
    report = _apply(store, [_promotion(entity_type="user")])

    assert report.promoted == 1
    rows = store.active_facts_with_entities([(SCOPE_PROJECT, PROJECT_ID)], now=NOW)
    entity, _fact = rows[0]
    assert entity.scope == SCOPE_PROJECT
    assert entity.entity_type == "project"


def test_existing_facts_are_untouched(store: long_term.LongTermStore) -> None:
    """只新增：巩固不能改已有事实的状态与值。"""
    entity = store.record_entity(
        entity_type="project", name="chaiai", scope=SCOPE_PROJECT, project_id=PROJECT_ID, now=NOW
    )
    existing = store.upsert_fact(
        entity_id=entity.id,
        key="editor",
        value="vscode",
        confidence=0.9,
        importance=0.9,
        source="reflection",
        origin="owner",  # type: ignore[arg-type]
        now=NOW,
    )

    _apply(store, [_promotion(key="test_runner", value="pytest")])

    kept = store.get_fact(existing.record.id)
    assert kept is not None
    assert kept.status == "active"
    assert json.loads(kept.value_json) == "vscode"

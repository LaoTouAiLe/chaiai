"""数据中台：真实存储作用域、无副作用、分支去重与未知用量。"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_application import _harness, _session

from local_agent.application import data_center
from local_agent.application.data_center_models import UsageCallView
from local_agent.application.maintenance import MaintenanceRun
from local_agent.application.models import ModelCallRecordView
from local_agent.config import AppConfig
from local_agent.memory import long_term
from local_agent.memory.forgetting import ForgetReport
from local_agent.scope import project_id_for, scope_key_for


def _config(app_config: AppConfig, root: Path) -> AppConfig:
    values = app_config.model_dump()
    values["memory"]["long_term"].update(enabled=False, path=str(root / "long.sqlite"))
    values["memory"]["medium_term"].update(enabled=False, path=str(root / "vectors"))
    return AppConfig.model_validate(values)


def test_empty_memory_browse_does_not_create_stores(tmp_path, model_factory, app_config):
    h = _harness(tmp_path, model_factory, _config(app_config, tmp_path), decision="chat")
    _session(h)
    before = h.database.connection.total_changes
    result = h.service.get_memory_page()
    assert result.total == 0 and not result.incomplete
    assert {resource.state for resource in result.resources} == {"missing"}
    assert not (tmp_path / "long.sqlite").exists() and not (tmp_path / "vectors").exists()
    assert h.database.connection.total_changes == before and h.model.decision_calls == 0
    h.database.connection.close()


class _FakeMaintenance:
    """只提供 ``status()``：记忆页只读它，不触发任何维护。"""

    def __init__(self, run: MaintenanceRun | None) -> None:
        self._run = run

    def status(self) -> MaintenanceRun | None:
        return self._run


def _maintenance_run(*, report: ForgetReport | None = None, error: str | None = None) -> MaintenanceRun:
    moment = datetime.now(timezone.utc)
    return MaintenanceRun(started_at=moment, finished_at=moment, report=report, error=error)


def test_memory_page_reports_maintenance_status(tmp_path, model_factory, app_config):
    """维护状态进 ``notices``（2026-11-02）：前端无需改渲染，且打开页面**不触发**维护。"""
    values = app_config.model_dump()
    values["memory"]["long_term"].update(enabled=True, path=str(tmp_path / "long.sqlite"))
    values["memory"]["medium_term"].update(enabled=False, path=str(tmp_path / "vectors"))
    h = _harness(tmp_path, model_factory, AppConfig.model_validate(values), decision="chat")
    _session(h)

    # 1) 没挂调度器（未走 lifespan）：不编造"尚未运行"
    assert not any("记忆维护" in note for note in h.service.get_memory_page().notices)

    # 2) 挂了但还没跑过
    h.service.memory_maintenance = _FakeMaintenance(None)
    assert any("尚未运行" in note for note in h.service.get_memory_page().notices)

    # 3) 跑过并淘汰了记录
    h.service.memory_maintenance = _FakeMaintenance(
        _maintenance_run(report=ForgetReport(expired_facts=1, aged_facts=2))
    )
    notes = h.service.get_memory_page().notices
    assert any("显式过期 1 条" in note and "自动老化 2 条" in note for note in notes)
    h.database.connection.close()


def test_memory_page_does_not_leak_maintenance_error_text(
    tmp_path, model_factory, app_config
):
    """失败原因只进日志：页面不能出现异常原文（可能带绝对路径与库结构）。"""
    values = app_config.model_dump()
    values["memory"]["long_term"].update(enabled=True, path=str(tmp_path / "long.sqlite"))
    values["memory"]["medium_term"].update(enabled=False, path=str(tmp_path / "vectors"))
    h = _harness(tmp_path, model_factory, AppConfig.model_validate(values), decision="chat")
    _session(h)
    h.service.memory_maintenance = _FakeMaintenance(
        _maintenance_run(error="PermissionError: C:/secret/path/memories.sqlite")
    )

    notes = h.service.get_memory_page().notices

    assert any("上次失败" in note for note in notes)
    assert all("C:/secret" not in note for note in notes)
    h.database.connection.close()


def test_long_memory_scope_history_expiry_search_and_no_write(tmp_path, model_factory, app_config):
    config = _config(app_config, tmp_path)
    h = _harness(tmp_path, model_factory, config, decision="chat")
    _session(h)
    workspace_id = scope_key_for("workspace", workspace_root=h.workspace)
    store = long_term.open_store(tmp_path / "long.sqlite")
    own_project = project_id_for(h.workspace)
    own = store.record_entity(entity_type="project", name="当前项目", scope="project", project_id=own_project)
    foreign = store.record_entity(entity_type="project", name="其它项目", scope="project", project_id="foreign")
    for entity, value in ((own, "中文 pytest sk-test-secret"), (foreign, "不应显示 FOREIGN_ONLY")):
        store.upsert_fact(entity_id=entity.id, key="test", value=value, confidence=.8, importance=.7, source="人工", origin="owner")
    store.upsert_fact(entity_id=own.id, key="expired", value="已到期", confidence=.8, importance=.6,
    origin="owner",
                      source="人工", expires_at="2020-01-01T00:00:00+00:00")
    store.record_task_conclusion(task_title="缓存对照", conclusion="提示词前缀稳定", project_scope=own_project, confidence=.9, origin="owner")
    store.record_task_conclusion(task_title="其它结论", conclusion="FOREIGN_ONLY", project_scope="foreign", confidence=.9, origin="owner")
    before = (tmp_path / "long.sqlite").read_bytes()
    changes = store.connection.total_changes
    result = h.service.get_memory_page(workspace_id=workspace_id)
    assert result.counts == {"fact": 2, "conclusion": 1}
    assert all("FOREIGN_ONLY" not in row.content for row in result.entries)
    assert "sk-test-secret" not in result.model_dump_json()
    assert any(row.status == "expired" for row in result.entries)
    assert not next(resource for resource in result.resources if resource.kind == "long").enabled
    assert h.service.get_memory_page(workspace_id=workspace_id, query="PYTEST", limit=1).total == 1
    assert h.service.get_memory_page(workspace_id=workspace_id, status="expired").total == 1
    assert h.service.get_memory_page(workspace_id=workspace_id, offset=2, limit=1).entries == result.entries[2:3]
    assert store.connection.total_changes == changes and (tmp_path / "long.sqlite").read_bytes() == before
    assert h.model.decision_calls == 0
    store.close(); h.database.connection.close()


def test_medium_collection_get_reads_saved_data_without_embeddings(tmp_path, model_factory, app_config):
    chromadb = pytest.importorskip("chromadb")
    h = _harness(tmp_path, model_factory, _config(app_config, tmp_path), decision="chat")
    _session(h)
    root = tmp_path / "vectors"
    client = chromadb.PersistentClient(path=str(root))
    collection = client.create_collection("agent_memory", embedding_function=None)
    own_key = scope_key_for("workspace", workspace_root=h.workspace)
    collection.add(ids=["own", "foreign"], documents=["中期总结 PYTEST", "FOREIGN_ONLY"], embeddings=[[1., 0.], [0., 1.]],
                   metadatas=[{"scope": "workspace", "scope_key": own_key, "status": "active", "confidence": .8},
                              {"scope": "workspace", "scope_key": "other", "status": "active"}])
    result = h.service.get_memory_page(workspace_id=own_key, kind="medium", query="pytest")
    assert result.total == 1 and result.entries[0].id == "own"
    assert result.entries[0].content == "中期总结 PYTEST"
    assert "embedding" not in result.model_dump_json() and collection.count() == 2
    assert result.resources[0].state == "available" and not result.resources[0].enabled
    assert h.model.decision_calls == 0
    h.database.connection.close()


def test_corrupt_store_is_reported_instead_of_empty_success(tmp_path, model_factory, app_config):
    (tmp_path / "long.sqlite").write_text("broken", encoding="utf-8")
    h = _harness(tmp_path, model_factory, _config(app_config, tmp_path), decision="chat")
    result = h.service.get_memory_page()
    assert result.incomplete and result.resources[1].state == "unavailable"
    assert str(tmp_path) not in result.model_dump_json()
    assert (tmp_path / "long.sqlite").read_text() == "broken"
    h.database.connection.close()


def test_usage_union_counts_each_branch_call_once_and_includes_archived(tmp_path, model_factory, app_config):
    h = _harness(tmp_path, model_factory, _config(app_config, tmp_path), decision="chat")
    sid = _session(h)
    for i in range(3):
        h.service.submit_message(sid, f"原问题{i}", f"normal{i}")
    original = h.service._read(h.service._session_or_raise(sid))
    root = h.service.get_conversation_map(sid).nodes[0]
    h.service.submit_message(sid, "历史分叉", "fork", parent_turn_id=root.turn_id)
    fork = h.service._read(h.service._session_or_raise(sid))
    expected = {call.model_call_id for view in (original, fork) for call in view.values["model_call_records"]}
    calls, changes = h.model.decision_calls, h.database.connection.total_changes
    result = h.service.get_usage_page(limit=1)
    assert result.total == len(expected) and len(result.entries) == 1
    assert result.totals.calls == result.total
    assert result.totals.input_tokens is None and result.totals.cache_hit_ratio is None
    assert result.totals.missing_usage_calls == result.total
    assert sum(bucket.totals.calls for bucket in result.models) == result.total
    assert h.model.decision_calls == calls and h.database.connection.total_changes == changes
    h.service.archive_session(sid)
    assert h.service.get_usage_page().total == len(expected)
    assert h.service.get_usage_page(model="NON_EXISTENT").total == 0
    assert h.service.get_usage_page(since=datetime(2099, 1, 1, tzinfo=timezone.utc)).total == 0
    h.database.connection.close()


def _call(call_id: str, uncached: int | None, cache: int | None, output: int | None, status="ok") -> UsageCallView:
    return UsageCallView(session_id="s1", session_title="会话", workspace_id="w1", call=ModelCallRecordView(
        model_call_id=call_id, run_id="r1", purpose="chat", connection_id="c1", config_version="v1", provider="fake",
        model_id="fake", started_at=datetime.now(timezone.utc), status=status, cache_metrics_reported=cache is not None,
        actual_uncached_input_tokens=uncached, actual_cache_read_tokens=cache, actual_output_tokens=output))


def test_usage_weighted_totals_unknown_and_failed_retry_records():
    rows = [_call("1", 100, 900, 20), _call("2", 90, 10, 0, "error")]
    total = data_center.totals(rows)
    assert total.calls == 2 and total.failed_calls == 1
    assert total.input_tokens == 1100 and total.output_tokens == 20
    assert total.cache_hit_ratio == pytest.approx(910 / 1100)
    assert total.cache_hit_ratio != .5  # 不平均 90% 与 10%
    rows.append(_call("retry", None, None, None))
    partial = data_center.totals(rows)
    assert partial.input_tokens is None and partial.cache_hit_ratio is None
    assert partial.missing_usage_calls == 1 and partial.calls == 3
    assert data_center.totals([]).input_tokens is None


def test_scan_caps_and_missing_history_are_explicit(tmp_path, model_factory, app_config, monkeypatch):
    h = _harness(tmp_path, model_factory, _config(app_config, tmp_path), decision="chat")
    _session(h)
    monkeypatch.setattr(data_center, "SESSION_SCAN_LIMIT", 0)
    assert h.service.get_usage_page().incomplete
    monkeypatch.setattr(data_center, "SESSION_SCAN_LIMIT", 200)
    result = h.service.get_usage_page()
    assert result.missing_record_sessions == 1 and result.notices
    assert result.total == 0 and result.totals.cache_hit_ratio is None
    h.database.connection.close()

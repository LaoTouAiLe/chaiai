"""记忆维护后台任务（2026-11-02）：判定复用 ``run_forgetting``，这里只验调度与边界。

四条边界：

1. 真的把超龄事实落库淘汰，并如实报告条数；
2. 长期记忆未启用时**跳过**（不是失败），且不起线程；
3. 库打不开时**不抛**（记成 error，线程下一轮继续）；
4. ``start`` / ``stop`` 幂等。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from local_agent.application.maintenance import MemoryMaintenance
from local_agent.config import AppConfig
from local_agent.memory import long_term
from local_agent.memory.consolidation import ConsolidationReport
from local_agent.scope import SCOPE_PROJECT

PROJECT_ID = "project-a"


class _Host:
    """最小宿主：调度器只读配置、按需返回一次巩固结果。"""

    def __init__(self, config: AppConfig, consolidation: Any = None) -> None:
        self._config = config
        self._consolidation = consolidation

    def memory_maintenance_config(self) -> AppConfig:
        return self._config

    def consolidate_memories(self) -> Any:
        return self._consolidation


def _config(app_config: AppConfig, path: Path, **overrides: Any) -> AppConfig:
    return app_config.model_copy(
        update={
            "memory": app_config.memory.model_copy(
                update={
                    "long_term": app_config.memory.long_term.model_copy(
                        update={"enabled": True, "path": str(path), **overrides}
                    )
                }
            )
        }
    )


def _old_fact(config: AppConfig) -> str:
    """写一条 30 天前的事实，返回事实 id（``forget_after_days`` 默认 180，用例里压到 10）。"""
    old = datetime.now(timezone.utc) - timedelta(days=30)
    store = long_term.open_store(Path(config.memory.long_term.path))
    try:
        entity = store.record_entity(
            entity_type="project",
            name="chaiai",
            scope=SCOPE_PROJECT,
            project_id=PROJECT_ID,
            now=old,
        )
        written = store.upsert_fact(
            entity_id=entity.id,
            key="editor",
            value="vscode",
            confidence=0.9,
            importance=0.9,
            source="test",
            origin="owner",  # type: ignore[arg-type]
            now=old,
        )
        return written.record.id
    finally:
        store.close()


def test_run_once_ages_out_overdue_fact_and_reports(
    app_config: AppConfig, tmp_path: Path
) -> None:
    config = _config(
        app_config,
        tmp_path / "memories.sqlite",
        aging_enabled=True,
        forget_after_days=10,
    )
    fact_id = _old_fact(config)

    maintenance = MemoryMaintenance(_Host(config))
    run = maintenance.run_once()

    assert run.ok and not run.skipped
    assert run.report is not None
    assert run.report.aged_facts == 1
    assert run.report.total == 1
    assert maintenance.status() is run

    store = long_term.open_store(Path(config.memory.long_term.path))
    try:
        record = store.get_fact(fact_id)
        assert record is not None and record.status == "expired"
    finally:
        store.close()


def test_run_once_skips_when_long_term_disabled(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """``app_config`` 默认关闭长期记忆（conftest），这里**不**覆盖它。"""
    run = MemoryMaintenance(_Host(app_config)).run_once()

    assert run.ok
    assert run.skipped
    assert run.error is None


def test_start_is_skipped_when_long_term_disabled(app_config: AppConfig) -> None:
    maintenance = MemoryMaintenance(_Host(app_config))

    maintenance.start()

    assert maintenance.running is False
    maintenance.stop()


def test_failure_is_recorded_not_raised(app_config: AppConfig, tmp_path: Path) -> None:
    """库路径指向一个目录：打不开 → 记成 error，不抛给调用方（线程才能继续）。"""
    broken = tmp_path / "not-a-db"
    broken.mkdir()
    config = _config(app_config, broken)

    run = MemoryMaintenance(_Host(config)).run_once()

    assert not run.ok
    assert run.error and ":" in run.error
    assert run.report is None


def test_run_once_carries_consolidation_report(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """巩固与遗忘是同一轮维护的两件事，结果一起留在 ``status()`` 里。"""
    config = _config(app_config, tmp_path / "memories.sqlite")
    report = ConsolidationReport(considered=3, promoted=1, skipped=1, reasons=["越界"])

    run = MemoryMaintenance(_Host(config, report)).run_once()

    assert run.consolidation is report


def test_start_and_stop_are_idempotent(app_config: AppConfig, tmp_path: Path) -> None:
    # 间隔与首次延迟都给大值：本用例只验线程生命周期，不让它真跑一轮。
    maintenance = MemoryMaintenance(
        _Host(_config(app_config, tmp_path / "memories.sqlite")),
        interval=3600.0,
        initial_delay=3600.0,
    )

    maintenance.start()
    maintenance.start()
    assert maintenance.running is True

    maintenance.stop()
    maintenance.stop()
    assert maintenance.running is False

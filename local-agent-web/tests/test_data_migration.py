"""数据根搬迁（2026-11-06）：搬全、校验、切指针、清旧，失败零残留。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from local_agent import app_paths, data_migration
from local_agent.exceptions import FileOperationError


@pytest.fixture(autouse=True)
def _own_data_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件的源根由各用例自己指定：conftest 的 ``CHAIAI_HOME`` 会盖过 ``config.data_root``。"""
    monkeypatch.delenv("CHAIAI_HOME", raising=False)


def _write_sqlite(path: Path, *, wal: bool = False, keep_open: bool = False) -> sqlite3.Connection | None:
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.execute("INSERT INTO notes VALUES ('迁移前写入')")
    connection.commit()
    if keep_open:
        return connection
    connection.close()
    return None


def _source(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _write_sqlite(root / "checkpoints.sqlite")
    attachment = root / "attachments" / "2026" / "11"
    attachment.mkdir(parents=True)
    (attachment / "a.txt").write_text("附件正文", encoding="utf-8")


def _plan(source: Path, target: Path) -> data_migration.MigrationPlan:
    items = (
        data_migration.MigrationItem("会话与检查点库", "checkpoints.sqlite", "sqlite"),
        data_migration.MigrationItem("附件", "attachments", "dir"),
    )
    total = (source / "checkpoints.sqlite").stat().st_size
    for item in (source / "attachments").rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return data_migration.MigrationPlan(
        source_root=source, target_root=target, items=items, external=(), total_bytes=total
    )


def test_moves_everything_and_flips_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    _source(source)
    trashed: list[Path] = []
    monkeypatch.setattr(data_migration, "move_to_trash", lambda path: trashed.append(path))

    stages: list[str] = []
    warnings = data_migration.run(_plan(source, target), progress=lambda stage, _: stages.append(stage))

    assert warnings == []
    assert (target / "attachments" / "2026" / "11" / "a.txt").read_text(encoding="utf-8") == "附件正文"
    with sqlite3.connect(target / "checkpoints.sqlite") as conn:
        assert conn.execute("SELECT body FROM notes").fetchone() == ("迁移前写入",)
    assert app_paths.read_data_root_pointer() == target
    # 清旧只针对清单内的项，且走回收站（可还原）
    assert sorted(path.name for path in trashed) == ["attachments", "checkpoints.sqlite"]
    assert stages[0] == "copying" and stages[-1] == "done"


def test_wal_content_survives_the_copy(tmp_path: Path) -> None:
    """未 checkpoint 的数据在 ``-wal`` 里：直接 copy 主文件会丢掉最近一次提交。"""
    source, target = tmp_path / "old", tmp_path / "new"
    source.mkdir()
    connection = _write_sqlite(source / "checkpoints.sqlite", wal=True, keep_open=True)
    assert connection is not None
    try:
        plan = data_migration.MigrationPlan(
            source_root=source, target_root=target,
            items=(data_migration.MigrationItem("会话与检查点库", "checkpoints.sqlite", "sqlite"),),
            external=(), total_bytes=(source / "checkpoints.sqlite").stat().st_size,
        )
        data_migration.run(plan, delete_source=False)
        with sqlite3.connect(target / "checkpoints.sqlite") as copied:
            assert copied.execute("SELECT count(*) FROM notes").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize("nested", ["inside-source", "inside-target"])
def test_refuses_nested_locations(tmp_path: Path, nested: str) -> None:
    source = tmp_path / "old"
    _source(source)
    target = source / "new" if nested == "inside-source" else source.parent
    plan = data_migration.MigrationPlan(
        source_root=source, target_root=target,
        items=(data_migration.MigrationItem("附件", "attachments", "dir"),),
        external=(), total_bytes=1,
    )

    with pytest.raises(data_migration.MigrationError, match="互相包含"):
        data_migration.preflight(plan)


def test_refuses_when_the_target_already_has_the_same_item(tmp_path: Path) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    _source(source)
    (target / "attachments").mkdir(parents=True)

    with pytest.raises(data_migration.MigrationError, match="同名数据"):
        data_migration.preflight(_plan(source, target))


def test_refuses_when_the_target_disk_is_too_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    _source(source)
    monkeypatch.setattr(
        data_migration.shutil, "disk_usage", lambda _: type("U", (), {"free": 10})()
    )

    with pytest.raises(data_migration.MigrationError, match="空间不足"):
        data_migration.preflight(_plan(source, target))


def test_failure_leaves_no_residue_and_keeps_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    _source(source)
    data_migration.app_paths.set_data_root_pointer(source)

    def boom(*_args: object) -> None:
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(data_migration, "_copy_sqlite", boom)

    with pytest.raises(data_migration.MigrationError, match="搬运失败"):
        data_migration.run(_plan(source, target), delete_source=True)

    assert not (target / "checkpoints.sqlite").exists()
    assert app_paths.read_data_root_pointer() == source  # 指针没动
    assert (source / "checkpoints.sqlite").is_file()  # 旧数据还在


def test_reports_old_data_that_could_not_be_trashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    _source(source)

    def refuse(_path: Path) -> None:
        raise FileOperationError("回收站不可用")

    monkeypatch.setattr(data_migration, "move_to_trash", refuse)

    warnings = data_migration.run(_plan(source, target), delete_source=True)

    assert len(warnings) == 2
    # 清理失败不回滚：数据已经完整地在新位置，指针也已切换
    assert app_paths.read_data_root_pointer() == target
    assert (target / "checkpoints.sqlite").is_file()

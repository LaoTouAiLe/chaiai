"""T87：诊断真实库与官方入口；失败不创建数据库、不读 Key、不启动服务。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from local_agent import config as configuration
from local_agent.api import __main__ as api_main
from local_agent.diagnostics import diagnose
from local_agent.persistence import migrations


def _static(root: Path) -> None:
    static = root / "frontend/dist"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<title>fixture</title>", encoding="utf-8")


def _check(report, name):
    return next(item for item in report["checks"] if item["name"] == name)


def test_first_install_does_not_create_config_or_databases(tmp_path: Path) -> None:
    _static(tmp_path)
    before = set(tmp_path.rglob("*"))
    report = diagnose(tmp_path / "config.yaml")
    assert report["ok"] is True
    assert _check(report, "config")["status"] == "warning"
    assert _check(report, "chat_database")["status"] == "warning"
    assert set(tmp_path.rglob("*")) == before


def test_invalid_configuration_does_not_echo_private_input(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("unknown: PRIVATE-SENTINEL", encoding="utf-8")
    report = diagnose(config)
    assert report["ok"] is False
    assert "PRIVATE-SENTINEL" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("kind", ["corrupt", "future", "current", "old"])
def test_existing_database_is_checked_without_migration(tmp_path: Path, kind: str) -> None:
    _static(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    path = data / "checkpoints.sqlite"
    if kind == "corrupt":
        path.write_bytes(b"PRIVATE-SENTINEL: invalid SQLite")
    else:
        with sqlite3.connect(path) as db:
            migrations.apply_migrations(db)
            if kind == "future":
                db.execute("INSERT INTO schema_migrations VALUES (?, 'future')",
                           (migrations.CHAT_SCHEMA_VERSION + 1,))
            elif kind == "old":
                db.execute("DELETE FROM schema_migrations WHERE version=?",
                           (migrations.CHAT_SCHEMA_VERSION,))
    before = path.read_bytes()
    report = diagnose(tmp_path / "config.yaml")
    expected = {"corrupt": "error", "future": "error", "current": "ok", "old": "warning"}
    assert _check(report, "chat_database")["status"] == expected[kind]
    assert report["ok"] is (kind in {"current", "old"})
    assert path.read_bytes() == before
    assert "PRIVATE-SENTINEL" not in json.dumps(report)
    assert not (data / "model_settings.sqlite").exists()


def test_missing_static_artifact_is_an_actionable_error(tmp_path: Path) -> None:
    report = diagnose(tmp_path / "config.yaml")
    assert report["ok"] is False
    assert "npm run build" in _check(report, "frontend")["message"]


def test_api_diagnostic_branch_never_loads_env_or_opens_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    _static(tmp_path)
    dotenv = Mock(side_effect=AssertionError("must not read credentials"))
    runtime = Mock(side_effect=AssertionError("must not assemble runtime"))
    monkeypatch.setattr(configuration, "load_dotenv_file", dotenv)
    monkeypatch.setattr(api_main, "open_runtime", runtime)
    assert api_main.main(["--config", str(tmp_path / "config.yaml"), "--diagnose"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    dotenv.assert_not_called()
    runtime.assert_not_called()

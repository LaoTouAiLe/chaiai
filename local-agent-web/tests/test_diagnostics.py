"""T87：诊断真实库与官方入口；失败不创建数据库、不读 Key、不启动服务。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from local_agent import app_paths
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
    # 库在**数据根**下（2026-10-26），不是 ``tmp_path/data``：conftest 给每个用例设了独立的
    # ``CHAIAI_HOME``，诊断与运行时必须看同一处。
    data = app_paths.data_root()
    data.mkdir(parents=True, exist_ok=True)
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


def test_reports_the_data_root_and_flags_a_legacy_directory(tmp_path: Path) -> None:
    """「数据在哪」与「有没有旧布局残留」（2026-10-26）。

    `--diagnose` 是「服务起不来」时唯一还能跑的命令，所以它必须回答这个——此前它只说
    「数据库存在/不存在」，不说是哪个目录；而换了数据根之后，两套库并存正是最容易困惑的地方。
    """
    _static(tmp_path)
    # 造一个旧布局残留：`<数据根>/data` 存在，而配置里的路径都不在 data/ 下。
    (app_paths.data_root() / "data").mkdir(parents=True, exist_ok=True)

    report = diagnose(tmp_path / "config.yaml")

    assert _check(report, "data_root")["status"] == "ok"
    assert str(app_paths.data_root()) in _check(report, "data_root")["message"]
    assert _check(report, "legacy_data")["status"] == "warning"
    assert "旧布局" in _check(report, "legacy_data")["message"]


def test_flags_data_still_configured_under_the_legacy_directory(tmp_path: Path) -> None:
    """配置仍写着 `data/...` 时要说"还在用旧布局"，且必须指出是哪些路径。

    这种**混用**状态最坑：会话库在 `data/` 下、设置库已在根级，用户看到的是
    "会话是旧的、设置是新的"，却没有任何提示。判据不能是"有任何一个路径带前缀就沉默"。
    """
    _static(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "memory:\n  short_term:\n    path: data/checkpoints.sqlite\n", encoding="utf-8"
    )
    (app_paths.data_root() / "data").mkdir(parents=True, exist_ok=True)

    report = diagnose(config_path)

    message = _check(report, "legacy_data")["message"]
    assert "仍落在旧布局" in message
    # 必须点名具体文件，否则用户不知道该改哪一行配置。
    assert "data/checkpoints.sqlite" in message


def test_quiet_about_a_data_directory_that_nothing_uses(tmp_path: Path) -> None:
    """没有旧布局目录时**不出现**这条检查：空目录/正常安装不该被警告刷屏。"""
    _static(tmp_path)

    report = diagnose(tmp_path / "config.yaml")

    assert all(item["name"] != "legacy_data" for item in report["checks"])

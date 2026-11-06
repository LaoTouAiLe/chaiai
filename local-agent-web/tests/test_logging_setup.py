"""A7：日志要落盘，且重复配置不能把每行写两遍（2026-11-06）。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from local_agent import app_paths, log_setup
from local_agent.api import __main__ as api_main
from local_agent.diagnostics import diagnose


@pytest.fixture(autouse=True)
def _restore_loggers() -> Iterator[None]:
    """这个模块会改全局 logger（root + uvicorn），跑完必须还原，否则会串到别的用例。"""
    loggers = [logging.getLogger()] + [
        logging.getLogger(name) for name in log_setup._UVICORN_LOGGERS
    ]
    before = [(item, list(item.handlers), item.level) for item in loggers]
    yield
    for item, handlers, level in before:
        for handler in list(item.handlers):
            if handler not in handlers:
                item.removeHandler(handler)
                handler.close()
        for handler in handlers:
            if handler not in item.handlers:
                item.addHandler(handler)
        item.setLevel(level)


def test_writes_under_the_data_root_and_is_idempotent(tmp_path: Path) -> None:
    path = log_setup.configure_logging(tmp_path / "logs")

    assert path == tmp_path / "logs" / "app.log"
    logging.getLogger("chaiai.test").warning("第一次写入")
    assert "第一次写入" in path.read_text(encoding="utf-8")

    # 装配路径走两遍（或测试里连调两次）不能变成"每行写两遍"。
    log_setup.configure_logging(tmp_path / "logs")
    logging.getLogger("chaiai.test").warning("第二次写入")
    hits = [
        line for line in path.read_text(encoding="utf-8").splitlines() if "第二次写入" in line
    ]
    assert len(hits) == 1


def test_uvicorn_loggers_also_reach_the_file(tmp_path: Path) -> None:
    """uvicorn 的 logger 都是 propagate=False，只挂 root 的话启动与访问日志进不了文件。"""
    path = log_setup.configure_logging(tmp_path / "logs")

    logging.getLogger("uvicorn.error").warning("uvicorn-也进文件")

    assert "uvicorn-也进文件" in path.read_text(encoding="utf-8")


def test_entry_point_creates_the_log_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """真正走一遍装配入口（`python -m local_agent.api` 的那条路），而不只是单测配置函数。

    `uvicorn.run` 被替身接走，其余装配照常：这样能证明日志是在**装配之前**挂上的——装配失败时
    也留得下证据，这正是这条改动存在的理由。
    """
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)

    assert api_main.main(["--no-open-browser"]) == 0

    log_file = app_paths.data_root() / "logs" / "app.log"
    assert log_file.is_file()
    logging.getLogger("chaiai.test").warning("入口装配后写入")
    assert "入口装配后写入" in log_file.read_text(encoding="utf-8")


def test_diagnose_reports_the_log_path_without_creating_it(tmp_path: Path) -> None:
    """`--diagnose` 是服务起不来时唯一还能跑的命令，它得回答"日志在哪里"。

    只报告位置、**不创建目录**（诊断不该顺手改用户的数据根）。
    """
    report = diagnose(tmp_path / "config.yaml")

    entry = next(item for item in report["checks"] if item["name"] == "logs")
    assert entry["status"] == "warning"
    assert "app.log" in entry["message"]
    assert not (tmp_path / "logs").exists()

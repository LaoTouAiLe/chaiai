"""冻结形态启动兜底的回归（T40）。

覆盖三件在朋友机器上真实发生过/可能发生的事：导入阶段崩溃、装配失败、日志目录不可写。
不真的打包：把候选目录与入口换成替身即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from local_agent import startup
from local_agent.api import __main__ as api_main


def test_report_failure_writes_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """失败要同时留在控制台与日志里：关掉窗口之后仍然拿得到原因。"""
    monkeypatch.setattr(startup, "_candidate_dirs", lambda: [tmp_path])

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        startup.report_failure(exc)

    text = (tmp_path / startup.LOG_NAME).read_text(encoding="utf-8")
    assert "启动失败" in text
    assert "RuntimeError: boom" in text
    assert "已把上面的原因追加写入" in capsys.readouterr().err


def test_report_failure_falls_back_to_the_next_writable_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首选目录写不进去（比如数据目录被当成文件占着）时，继续试下一个。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("我是一个文件，不是目录", encoding="utf-8")
    monkeypatch.setattr(startup, "_candidate_dirs", lambda: [blocked, tmp_path / "second"])

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        startup.report_failure(exc)

    assert (tmp_path / "second" / startup.LOG_NAME).is_file()


def test_import_failure_is_reported_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导入阶段就崩（真实场景：包里缺 DLL）也要留下证据——这正是"一闪而过"的那一类。"""
    monkeypatch.setattr(startup, "_candidate_dirs", lambda: [tmp_path])
    monkeypatch.setitem(sys.modules, "local_agent.api.__main__", None)

    assert startup.main() == startup.FAILURE_EXIT_CODE

    text = (tmp_path / startup.LOG_NAME).read_text(encoding="utf-8")
    assert "启动失败" in text
    # `None in sys.modules` 抛的是 ImportError 的子类；断言真实类名，避免看起来像"没崩"。
    assert "ModuleNotFoundError" in text


def test_success_path_returns_the_entry_code_without_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(startup, "_candidate_dirs", lambda: [tmp_path])
    monkeypatch.setattr(api_main, "main", lambda: 0)

    assert startup.main() == 0

    assert not (tmp_path / startup.LOG_NAME).exists()


def test_nonzero_entry_code_is_also_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """友好的启动失败（配置损坏、已有实例在跑）也要在日志里留一份。"""
    monkeypatch.setattr(startup, "_candidate_dirs", lambda: [tmp_path])
    monkeypatch.setattr(api_main, "main", lambda: 1)

    assert startup.main() == 1

    assert "退出码 1" in (tmp_path / startup.LOG_NAME).read_text(encoding="utf-8")


def test_keyboard_interrupt_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C 不能被兜底吞掉。"""

    def interrupted() -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(api_main, "main", interrupted)

    with pytest.raises(KeyboardInterrupt):
        startup.main()

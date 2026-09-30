"""宿主原生目录选择器：子进程协议与"自我 spawn"契约。

**不弹真实窗口**。这一层要固定的是协议：命令行长什么样、stdout 怎么表达选中与取消、失败与
超时如何上报、入口怎么分发。对话框的真实长相与 DPI 表现只能人工验证（Windows 上是现代
`IFileDialog`、进程默认 DPI-unaware、弹窗前主动设成 per-monitor-v2——见模块 docstring）。
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from local_agent import directory_picker
from local_agent.api import __main__ as api_main


def _completed(
    stdout: str = "", *, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """伪造子进程结果：`subprocess.run` 的返回值形状。"""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---- 子进程协议 -------------------------------------------------------------


def test_main_requires_the_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert directory_picker.main([]) == 2
    assert directory_picker.PICK_DIRECTORY_FLAG in capsys.readouterr().err


def test_main_prints_the_chosen_path_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """成功时 stdout **只有一行**：路径本身。父进程就是按行解析的。"""
    monkeypatch.setattr(directory_picker, "_ask_directory", lambda: r"D:\projects\demo")

    assert directory_picker.main([directory_picker.PICK_DIRECTORY_FLAG]) == 0

    captured = capsys.readouterr()
    assert captured.out == "D:\\projects\\demo\n"
    # 诊断信息（DPI 等级）只走 stderr，不能混进那一行。
    assert "dpi=" in captured.err


def test_main_prints_a_blank_line_when_cancelled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """取消 = 空行 = 正常退出：父进程据此得到 `None`，而不是一个错误。"""
    monkeypatch.setattr(directory_picker, "_ask_directory", lambda: "")

    assert directory_picker.main([directory_picker.PICK_DIRECTORY_FLAG]) == 0
    assert capsys.readouterr().out == "\n"


def test_main_reports_a_picker_that_cannot_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """打不开窗口（没有桌面会话、Tk 缺失……）：非零退出 + 原因进 stderr。"""

    def boom() -> str:
        raise RuntimeError("no desktop")

    monkeypatch.setattr(directory_picker, "_ask_directory", boom)

    assert directory_picker.main([directory_picker.PICK_DIRECTORY_FLAG]) == 1
    assert "RuntimeError" in capsys.readouterr().err


# ---- 父进程侧 ---------------------------------------------------------------


def test_pick_directory_uses_the_module_entry_in_source_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """源码模式的解释器不认识裸 `--pick-directory`，必须通过 `-m` 进入应用。"""
    seen: list[list[str]] = []
    monkeypatch.setattr(directory_picker.sys, "frozen", False, raising=False)

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return _completed("D:\\projects\\demo\n")

    monkeypatch.setattr(directory_picker.subprocess, "run", fake_run)

    assert directory_picker.pick_directory() == r"D:\projects\demo"
    assert seen == [[sys.executable, "-m", "local_agent.api", directory_picker.PICK_DIRECTORY_FLAG]]


def test_pick_directory_spawns_the_frozen_exe_with_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结后的 exe 自启动；不依赖目标机器安装 Python。"""
    seen: list[list[str]] = []
    monkeypatch.setattr(directory_picker.sys, "frozen", True, raising=False)

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return _completed("D:\\projects\\demo\n")

    monkeypatch.setattr(directory_picker.subprocess, "run", fake_run)

    assert directory_picker.pick_directory() == r"D:\projects\demo"
    assert seen == [[sys.executable, directory_picker.PICK_DIRECTORY_FLAG]]


def test_pick_directory_maps_a_blank_line_to_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(directory_picker.subprocess, "run", lambda *a, **k: _completed("\n"))

    assert directory_picker.pick_directory() is None


def test_pick_directory_raises_on_a_failed_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        directory_picker.subprocess,
        "run",
        lambda *a, **k: _completed("", returncode=1, stderr="目录选择器不可用：RuntimeError\n"),
    )

    with pytest.raises(directory_picker.DirectoryPickerError, match="RuntimeError"):
        directory_picker.pick_directory()


def test_pick_directory_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时兜底：对话框可以无限期开着，所以请求侧不设超时，但子进程侧必须有一个。"""

    def timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=[], timeout=1.0)

    monkeypatch.setattr(directory_picker.subprocess, "run", timeout)

    with pytest.raises(directory_picker.DirectoryPickerError, match="超时"):
        directory_picker.pick_directory()


def test_pick_directory_raises_on_unparsable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """子进程一个字都没输出：宁可报错，也不把空结果当成"用户取消"。"""
    monkeypatch.setattr(directory_picker.subprocess, "run", lambda *a, **k: _completed(""))

    with pytest.raises(directory_picker.DirectoryPickerError, match="没有返回"):
        directory_picker.pick_directory()


def test_pick_directory_survives_a_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """起不来子进程（权限、缺解释器……）也要变成可展示的错误，而不是裸 `OSError`。"""

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("cannot spawn")

    monkeypatch.setattr(directory_picker.subprocess, "run", boom)

    with pytest.raises(directory_picker.DirectoryPickerError, match="无法启动"):
        directory_picker.pick_directory()


# ---- 入口分发 ---------------------------------------------------------------


def test_api_entry_dispatches_the_picker_flag_before_any_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`python -m local_agent.api --pick-directory` 只能弹窗，**不做任何装配**。

    这是"打包后能自我 spawn"的前提：子进程不会去读配置、不会去抢那个单实例库锁，因此
    服务正在运行时也能弹窗。
    """
    handled: list[list[str]] = []

    def fake_picker_main(argv: Any) -> int:
        handled.append(list(argv))
        return 7

    monkeypatch.setattr(directory_picker, "main", fake_picker_main)

    assert api_main.main([directory_picker.PICK_DIRECTORY_FLAG]) == 7
    assert handled == [[directory_picker.PICK_DIRECTORY_FLAG]]

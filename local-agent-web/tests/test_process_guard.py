"""T60 第二片：子进程的启动与**按进程树**终止（架构 §17.4）。

为什么要真跑进程：这一片的存在理由就是"`subprocess.run` 只杀直接子进程"，用假 runner 证明不了。
用例在**当前平台**上真实启动一条 `python → python` 的两级进程树，并用一个**标记文件**判断
孙子进程到底还活着没有（它每 50ms 追加一个字节）：

- 取消：标记文件停止增长 ⇒ 整棵树真的没了；
- 超时：同上，且 `termination` 如实写出手段（Windows `taskkill /F /T`、POSIX `killpg`）；
- 退出码：只写真实值，**不伪造"已停止"**。

孙子进程用标记文件而不是 PID 来判定，是为了不依赖平台原生的进程查询工具（`tasklist` / `ps`）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from local_agent.cancellation import CancellationToken
from local_agent.config import AppConfig
from local_agent.exceptions import SecurityError
from local_agent.security import process_guard
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import (
    PLATFORM_POSIX,
    PLATFORM_WINDOWS,
    ProcessCancelledError,
    ProcessRegistry,
    resolve_platform_name,
)
from local_agent.tools.shell_tools import run_shell_command

#: 平台终止手段在说明里的特征串（如实记录的断言用）。
_MECHANISM_HINT = "taskkill" if os.name == "nt" else "killpg"


def _scripts(tmp_path: Path) -> tuple[Path, Path, Path]:
    """写出一棵两级进程树：parent 启动 grandchild 后自睡，grandchild 持续写标记文件。"""
    marker = tmp_path / "marker.txt"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import sys, time\n"
        "target = sys.argv[1]\n"
        "while True:\n"
        "    with open(target, 'a', encoding='utf-8') as handle:\n"
        "        handle.write('x')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    return parent, grandchild, marker


def _command(parent: Path, grandchild: Path, marker: Path) -> list[str]:
    return [sys.executable, str(parent), str(grandchild), str(marker)]


def _wait_for_marker(marker: Path, *, timeout: float = 15.0) -> int:
    """等标记文件出现内容（证明孙子进程真的跑起来了）；返回当时的字节数。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists() and marker.stat().st_size > 0:
            return marker.stat().st_size
        time.sleep(0.05)
    return 0


def _size_stays(marker: Path, *, seconds: float = 1.0) -> bool:
    """在 `seconds` 内标记文件不再增长 ⇒ 写它的进程已经死了。"""
    before = marker.stat().st_size if marker.exists() else 0
    time.sleep(seconds)
    after = marker.stat().st_size if marker.exists() else 0
    return before == after


def test_spawn_refuses_when_already_cancelled(tmp_path: Path) -> None:
    token = CancellationToken()
    token.cancel("测试取消")
    registry = ProcessRegistry(token)

    with pytest.raises(ProcessCancelledError, match="测试取消"):
        registry.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    # 没有启动过任何进程
    assert registry.live_count() == 0


def test_cancel_hook_runs_immediately_when_already_cancelled() -> None:
    """"先取消、后注册"不能漏掉清理——那正是取消与工具启动竞态的一半。"""
    token = CancellationToken()
    calls: list[str] = []
    token.cancel("已经取消")

    token.add_cancel_hook(lambda reason: calls.append(reason))

    assert calls == ["已经取消"]


def test_cancel_terminates_the_whole_tree(tmp_path: Path) -> None:
    """取消 ⇒ 进程**树**终止（不是只杀直接子进程）。"""
    token = CancellationToken()
    registry = ProcessRegistry(token)
    parent, grandchild, marker = _scripts(tmp_path)
    process = registry.spawn(_command(parent, grandchild, marker), cwd=tmp_path)
    try:
        assert _wait_for_marker(marker) > 0, "对照组：孙子进程本来在写标记文件"
        assert process.handle.poll() is None

        token.cancel("用户取消")

        assert _size_stays(marker), "取消之后孙子进程仍在写：树没有被终止"
        assert process.handle.poll() is not None, "直接子进程也被终止"
        assert process.cancelled is True
        assert process.termination and _MECHANISM_HINT in process.termination
    finally:
        registry.terminate(process, reason="用例收尾")


def test_timeout_terminates_the_whole_tree_and_records_the_mechanism(tmp_path: Path) -> None:
    registry = ProcessRegistry(CancellationToken())
    parent, grandchild, marker = _scripts(tmp_path)

    outcome = registry.run(
        _command(parent, grandchild, marker), cwd=tmp_path, timeout_seconds=1.0
    )

    assert outcome.timed_out is True
    assert outcome.cancelled is False
    assert outcome.termination and outcome.termination.startswith("超过 1 秒未结束")
    assert _MECHANISM_HINT in outcome.termination
    # 退出码是**真实**的（被终止的进程不会给出 0），不是我们编的
    assert outcome.exit_code is not None and outcome.exit_code != 0
    assert _size_stays(marker), "超时之后孙子进程仍在写：树没有被终止"
    assert registry.live_count() == 0


def test_successful_command_reports_real_exit_code_and_no_termination() -> None:
    registry = ProcessRegistry(CancellationToken())

    outcome = registry.run(
        [sys.executable, "-c", "print('hi')"], timeout_seconds=30.0
    )

    assert outcome.exit_code == 0
    assert outcome.stdout.strip() == b"hi"
    assert outcome.termination is None and outcome.timed_out is False


def test_cancel_between_check_and_spawn_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动前预检通过、登记之前发生取消 ⇒ 立刻终止，绝不留孤儿进程（那个线性化点）。"""
    token = CancellationToken()
    registry = ProcessRegistry(token)
    real_popen = subprocess.Popen

    def spawn_then_cancel(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        handle = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        token.cancel("取消与命令启动同时发生")
        return handle

    monkeypatch.setattr(process_guard.subprocess, "Popen", spawn_then_cancel)

    process = registry.spawn([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        assert process.cancelled is True
        assert process.termination
        deadline = time.monotonic() + 10
        while process.handle.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.handle.poll() is not None, "竞态下启动的进程必须被立刻终止"
    finally:
        registry.terminate(process, reason="用例收尾")


def test_policy_registry_is_bound_to_the_cancellation_token(
    tmp_path: Path, app_config: AppConfig
) -> None:
    token = CancellationToken()
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, cancellation=token)

    assert policy.processes.cancelled is False
    token.cancel("用户取消")
    assert policy.processes.cancelled is True
    # 没挂令牌时登记表照样工作（只是不参与取消）
    plain = SecurityPolicy.from_config(app_config, workspace_root=tmp_path)
    assert plain.cancellation is None and plain.processes.cancelled is False


def test_run_shell_refuses_to_start_after_cancel(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """工具层：取消后不启动命令，且**如实**说"未执行"，不伪装成执行失败。"""
    if shutil.which("python") is None:  # pragma: no cover - 取决于本机 PATH
        pytest.skip("本机 PATH 上没有 python，无法构造'已放行'的命令计划")

    token = CancellationToken()
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, cancellation=token)
    token.cancel("用户取消")

    with pytest.raises(SecurityError, match="用户取消"):
        run_shell_command("python --version", policy=policy, cwd=".")


def test_platform_termination_units_are_declared() -> None:
    assert resolve_platform_name() in (PLATFORM_WINDOWS, PLATFORM_POSIX)
    assert resolve_platform_name("posix") == PLATFORM_POSIX
    # POSIX 必须另开会话，否则 killpg 打的是我们自己所在的进程组
    assert process_guard._spawn_kwargs(PLATFORM_POSIX) == {"start_new_session": True}
    assert process_guard._spawn_kwargs(PLATFORM_WINDOWS) == {}

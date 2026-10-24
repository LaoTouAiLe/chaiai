"""T60 第二片：子进程的启动与**按进程树**终止（架构 §17.4）。

为什么要真跑进程：这一片的存在理由就是"`subprocess.run` 只杀直接子进程"，用假 runner 证明不了。
用例在**当前平台**上真实启动一条 `python → python` 的两级进程树，并用一个**标记文件**判断
孙子进程到底还活着没有（它每 50ms 追加一个字节）：

- 取消：标记文件停止增长 ⇒ 整棵树真的没了；
- 超时：同上，且 `termination` 如实写出手段（Windows `taskkill /F /T`、POSIX `killpg`）；
- 退出码：只写真实值，**不伪造"已停止"**。

孙子进程用标记文件而不是 PID 来判定，是为了不依赖平台原生的进程查询工具（`tasklist` / `ps`）。

**v0.9 T76（架构 §20.1）**：本模块同时是**统一执行出口**的验收——`git` / `rg` / 目录选择器也都从
这里出去（源码守卫 + 同一套真跑的树终止 + 路由断言）。三处的两级进程树用 `.cmd` 启动器构造
（`CreateProcess` 认识 `.cmd`），因此那一组是 **Windows 专属**：非 Windows 上**跳过并如实标注**，
不假装跑过。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from local_agent import directory_picker
from local_agent.cancellation import CancellationToken
from local_agent.config import AppConfig
from local_agent.exceptions import SecurityError
from local_agent.rag import search
from local_agent.security import process_guard
from local_agent.security.policy import SecurityPolicy
from local_agent.security.process_guard import (
    PLATFORM_POSIX,
    PLATFORM_WINDOWS,
    ProcessCancelledError,
    ProcessRegistry,
    resolve_platform_name,
)
from local_agent.tools import git_tools
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


def test_exited_launcher_does_not_wait_for_inherited_child_pipes() -> None:
    command = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)']); "
        "print('launcher-done',flush=True)"
    )
    started = time.monotonic()
    outcome = ProcessRegistry().run([sys.executable, "-c", command], timeout_seconds=1.0)
    assert outcome.exit_code == 0 and not outcome.timed_out
    assert outcome.stdout.strip() == b"launcher-done"
    assert time.monotonic() - started < 1.5


def test_output_is_delivered_before_command_finishes(tmp_path: Path) -> None:
    release = tmp_path / "release"
    command = (
        "import pathlib,time,sys; print('ready',flush=True); "
        "p=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+3; "
        "exec('while not p.exists() and time.monotonic()<deadline: time.sleep(0.02)'); "
        "sys.exit(0 if p.exists() else 2)"
    )
    received = []

    def output(stream: str, data: bytes) -> None:
        received.append((stream, data))
        if b"ready" in data:
            release.touch()

    outcome = ProcessRegistry().run([sys.executable, "-c", command, str(release)],
                                   timeout_seconds=5, on_output=output)
    assert outcome.exit_code == 0
    assert received and b"ready" in outcome.stdout


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


# ---- v0.9 T76：统一执行出口（架构 §20.1）-----------------------------------


#: 源码守卫允许直连 `subprocess` 的唯一文件：登记表自己（以及它调用的 `taskkill`）。
_REGISTRY_MODULE = "process_guard.py"

#: 起进程的写法（都算绕过统一出口）：`subprocess` 家族、`os` 的几个入口、asio 的子进程 API。
_SPAWN_CALL = re.compile(
    r"subprocess\.(run|Popen|call|check_output|check_call)\s*\("
    r"|os\.(system|popen|spawn\w*)\s*\("
    r"|create_subprocess\w*\s*\("
)

#: 真跑两级进程树的用例是 **Windows 专属**：`.cmd` 启动器依赖 `CreateProcess` 认识 `.cmd`。
#: 非 Windows 上**跳过并如实标注**——不假装跑过（§20.1 的验收只声明跑过的）。
_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="两级进程树用 .cmd + CreateProcess 构造；非 Windows 上未验证（如实标注，不假装跑过）",
)


def test_only_the_registry_may_spawn_processes() -> None:
    """§20.1：`src/local_agent` 里除登记表自身外，**不得**直接启动子进程。

    这条是**源码守卫**，不是行为断言：四条路径（`run_shell` / `git` / `rg` / 目录选择器）接入登记表
    之后，最大的风险是**下一个**子进程产出点又悄悄直连 `subprocess.run`——那种写法在本机测不出来
    （超时只杀直接子进程照样"能跑通"），只有拿源码当断言才拦得住。所以这里连同 `os.system` /
    `os.spawn*` / `asyncio` 的子进程入口一起扫。
    """
    root = Path(process_guard.__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == _REGISTRY_MODULE:
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _SPAWN_CALL.search(line):
                offenders.append(f"{path.relative_to(root)}:{index}: {line.strip()}")

    assert offenders == [], "这些地方绕过了统一执行出口（§20.1）：\n" + "\n".join(offenders)


def test_git_tools_bind_the_current_run_registry(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """`build_git_tools` 的**默认** runner 绑在本轮登记表上（§20.1），不是模块级无令牌的 runner。

    取消令牌一响，工具就得够得着在跑的 git：这里用"取消后不再启动"这条既有的线性化点当证据——
    它由**登记表**给出，因此这条断言同时证明"默认 runner 真的用了 policy.processes"。
    """
    token = CancellationToken()
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, cancellation=token)
    tools = {tool.name: tool for tool in git_tools.build_git_tools(policy)}
    token.cancel("用户取消")

    with pytest.raises(SecurityError, match="用户取消"):
        tools["git_status"].invoke({})


def test_rg_uses_the_current_run_registry(tmp_path: Path, app_config: AppConfig) -> None:
    """`rg` 用本轮登记表（§20.1）：取消后不启动，并**如实**按"检索未完成"退化。

    退化本身是既有口径（读一次有界扫描，不起进程）；这里钉的是"它确实从本轮的登记表出去"。
    """
    token = CancellationToken()
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, cancellation=token)
    token.cancel("用户取消")

    outcome = search.discover_matches(
        ["decide_mode"],
        workspace_root=tmp_path,
        config=app_config.rag,
        resolve=lambda *_args, **_kwargs: "rg",
        registry=policy.processes,
    )

    assert outcome.source == "python_scan"
    assert any("用户取消" in note for note in outcome.notes)


@_WINDOWS_ONLY
def test_creationflags_reach_the_platform_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """`creationflags` 只在非 0 时透传（§20.1）——目录选择器靠它保住 `CREATE_NO_WINDOW`。"""
    seen: list[dict[str, object]] = []
    real_popen = subprocess.Popen

    def capture(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        seen.append(dict(kwargs))
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(process_guard.subprocess, "Popen", capture)

    registry = ProcessRegistry()
    plain = registry.run([sys.executable, "-c", "print('ok')"], timeout_seconds=30.0)
    flagged = registry.run(
        [sys.executable, "-c", "print('ok')"],
        timeout_seconds=30.0,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert plain.exit_code == 0 and flagged.exit_code == 0
    assert "creationflags" not in seen[0]
    assert seen[1]["creationflags"] == subprocess.CREATE_NO_WINDOW


def _marker_writer(tmp_path: Path, marker: Path, *, name: str = "writer") -> Path:
    """写一个"每 50ms 往标记文件追加一个字节"的脚本——判活用的探针。"""
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import sys, time\n"
        "target = sys.argv[1]\n"
        "while True:\n"
        "    with open(target, 'a', encoding='utf-8') as handle:\n"
        "        handle.write('x')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    return script


def _tree_launcher(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """写一个"父 → 子"两级进程树的启动器（``.cmd``）；返回它与标记文件路径。

    父（``.cmd``，由 ``CreateProcess`` 通过 ``cmd.exe`` 跑）用 ``start /b`` 拉一个持续写标记文件的
    孙子进程，然后自己长睡——于是"杀了父"和"杀了整棵树"在标记文件上看得见区别。
    """
    marker = tmp_path / f"{name}-marker.txt"
    grandchild = _marker_writer(tmp_path, marker, name="grandchild")
    launcher = tmp_path / f"{name}.cmd"
    launcher.write_text(
        "@echo off\r\n"
        f'start /b "" "{sys.executable}" "{grandchild}" "{marker}"\r\n'
        f'"{sys.executable}" -c "import time; time.sleep(30)"\r\n',
        encoding="ascii",
    )
    return launcher, marker


def _git_setup(args: list[str], cwd: Path) -> None:
    """用例自己的准备动作：把仓库摆好。

    **刻意不走统一执行出口**——这里不是排查对象，只是搭台；被测的是 `run_git` 起的进程树。
    """
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _git_repo_with_staged_change(tmp_path: Path) -> Path:
    """摆一个"有暂存内容"的仓库：`git commit` 因此必定打开编辑器（进程树的中间层）。"""
    if shutil.which("git") is None:  # pragma: no cover - 取决于本机 PATH
        pytest.skip("本机 PATH 上没有 git，无法构造真实的 git 进程树")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_setup(["init"], repo)
    (repo / "note.txt").write_text("待提交\n", encoding="utf-8")
    _git_setup(["add", "note.txt"], repo)
    return repo


def _editor_command(tmp_path: Path, marker: Path, *, name: str = "editor") -> str:
    """`core.editor` 用的命令行：指向"持续写标记文件、不会自己退出"的脚本。"""
    writer = _marker_writer(tmp_path, marker, name=name)
    return f'"{sys.executable}" "{writer}" "{marker}"'


def test_git_timeout_kills_the_whole_tree(tmp_path: Path) -> None:
    """`git` 超时按**进程树**终止（§20.1）：git 拉起的编辑器也一起收掉。

    证据用**真实 git** 构造：`git commit` 必定要开编辑器（有暂存内容、没给 `-m`），而编辑器就是
    我们写的标记文件进程——git 在等它退出。只杀直接子进程（git）时，中间那层 shell 与编辑器仍在写，
    于是标记文件能分辨"杀了 git"与"杀了整棵树"。假 runner 证明不了这一条（它根本不起进程）。

    （先试过 `--paginate` 强制 pager：实测 stdout 是管道时 git **仍然不**走 pager，退出码 0、
    看不到任何子进程——所以改用必然开编辑器的 `commit`。）

    顺带钉住退化口径：超时是失败且 ``timed_out=True``，**不是**空结果（空结果会被读成"没有改动"）。
    """
    repo = _git_repo_with_staged_change(tmp_path)
    marker = tmp_path / "git-marker.txt"
    editor = _editor_command(tmp_path, marker)

    run = git_tools.run_git(
        [
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=tester",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.editor={editor}",
            "commit",
        ],
        repo,
        1.0,
    )

    assert run.timed_out is True
    assert "超时" in run.stderr
    assert _wait_for_marker(marker) > 0, "对照组：编辑器本来在写标记文件"
    assert _size_stays(marker), "git 超时之后编辑器仍在写：树没有被终止"


def test_git_cancellation_reaches_the_running_tree(tmp_path: Path) -> None:
    """取消也够得着**正在跑**的 git（§20.1）：不是只在启动前拦一下。

    取消与超时走**同一条**终止路径（§17.4），所以这里验的是"令牌一响，git 的那棵树就停"——
    用真跑的编辑器进程树作证据，而不是看一个内部标志位。
    """
    repo = _git_repo_with_staged_change(tmp_path)
    marker = tmp_path / "cancel-marker.txt"
    editor = _editor_command(tmp_path, marker, name="cancel-editor")

    token = CancellationToken()
    registry = ProcessRegistry(token)
    outcome: list[object] = []

    def drive() -> None:
        outcome.append(
            git_tools.run_git(
                [
                    "-c",
                    "user.email=t@example.com",
                    "-c",
                    "user.name=tester",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    f"core.editor={editor}",
                    "commit",
                ],
                repo,
                120.0,
                registry=registry,
            )
        )

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    try:
        assert _wait_for_marker(marker) > 0, "对照组：编辑器本来在写标记文件"
        token.cancel("用户取消")
        worker.join(timeout=30)
        assert not worker.is_alive(), "取消之后 run_git 还没返回：进程没被终止"
        assert _size_stays(marker), "取消之后编辑器仍在写：树没有被终止"
    finally:
        token.cancel("用例收尾")
        worker.join(timeout=15)


@_WINDOWS_ONLY
def test_rg_timeout_kills_the_whole_tree(tmp_path: Path, app_config: AppConfig) -> None:
    """`rg` 超时按**进程树**终止（§20.1），并仍按既有口径退化到有界扫描。"""
    launcher, marker = _tree_launcher(tmp_path, "rg")
    config = app_config.rag.model_copy(update={"timeout_seconds": 1.0})

    outcome = search.discover_matches(
        ["decide_mode"],
        workspace_root=tmp_path,
        config=config,
        resolve=lambda *_args, **_kwargs: str(launcher),
    )

    assert outcome.source == "python_scan"
    assert any(note.startswith("rg 检索未完成（超过 ") for note in outcome.notes)
    assert _wait_for_marker(marker) > 0, "对照组：孙子进程本来在写标记文件"
    assert _size_stays(marker), "rg 超时之后孙子进程仍在写：树没有被终止"


@_WINDOWS_ONLY
def test_directory_picker_timeout_kills_the_whole_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录选择器超时按**进程树**终止（§20.1）：对话框进程连它派生的一起收掉。

    选择器用一次性登记表（没有取消令牌），所以这里只验超时这一条路径。
    """
    launcher, marker = _tree_launcher(tmp_path, "picker")
    # 顺序要紧：启动器里写的是**真实**解释器路径，先写它再改 sys.executable。
    monkeypatch.setattr(directory_picker.sys, "frozen", True, raising=False)
    monkeypatch.setattr(directory_picker.sys, "executable", str(launcher))

    with pytest.raises(directory_picker.DirectoryPickerError, match="超时"):
        directory_picker.pick_directory(timeout_seconds=1.0)

    assert _wait_for_marker(marker) > 0, "对照组：孙子进程本来在写标记文件"
    assert _size_stays(marker), "选择器超时之后孙子进程仍在写：树没有被终止"






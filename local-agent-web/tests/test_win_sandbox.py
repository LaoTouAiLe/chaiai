"""Windows 写侧沙箱（增量 4）的模块级验收。

只在 Windows 上跑：整条机制依赖受限令牌、每工作区 SID 的 ACL 物化与 Job Object。

判据来自探针 v1–v5 实测过的语义（见 `security/win_sandbox.py` 的模块 docstring）：
    1. 派生的每工作区 SID 确定、合法；
    2. 受限进程**只能写被授权的目录**（根 + 继承到的子目录），目录外被拒；
    3. stdout 能被**匿名管道**捕获（`run_shell` 的命门）；
    4. Job Object 装得上（`job_error is None`），且 runner 退出后**孙进程确实被收走**（带正对照）；
    5. 撤销授权后同一目录也不再可写。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys

import pytest

from local_agent.exceptions import SecurityError

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="写侧沙箱只在 Windows 上有实现"
)


def test_workspace_sid_is_deterministic_and_valid(tmp_path) -> None:
    """同一路径派生出同一个 SID、不同路径不撞（ACE 常驻复用的前提）。"""
    from local_agent.security import win_sandbox

    workspace = tmp_path / "ws"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    first = win_sandbox.workspace_sid_text(str(workspace))
    second = win_sandbox.workspace_sid_text(str(workspace))

    assert first == second
    assert first.startswith("S-1-5-")
    assert first != win_sandbox.workspace_sid_text(str(other))


def _group_only_dir(path) -> None:
    """把目录做成"**只有内置 Users 组**可写"：去掉继承、清掉显式项、只给 Users 授权。

    Users 不在 restricting 列表里，此夹具只用于精确观察目录 SID 的授权与撤销。
    普通目录保留用户显式 ACE 的情形由独立回归覆盖，不能用本夹具代替。
    """
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", "*S-1-5-32-545:(OI)(CI)M"],
        capture_output=True,
        check=True,
    )


def test_restricted_process_writes_only_the_granted_directory(tmp_path) -> None:
    """授权后目录内（含子目录）可写、未授权目录写不出去；输出可捕获；Job 装得上。"""
    from local_agent.security import win_sandbox

    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    for path in (workspace, outside):
        _group_only_dir(path)

    sid = win_sandbox.workspace_sid_text(str(workspace))
    command = [
        "cmd.exe",
        "/d",
        "/s",
        "/c",
        (
            f"echo inside > {workspace}\\in.txt"
            f" & echo deep > {workspace}\\sub\\deep.txt"
            f" & echo outside > {outside}\\o.txt"
            " & echo marker"
        ),
    ]

    # 裸跑对照（**必须有**，2026-10-14）：这两个目录在无沙箱时本来就能写。少了这步，"沙箱内被拒"
    # 什么都证明不了——本仓刚差点因此得出反向结论（当时没做对照，把"被拒"单独当成了证据）。
    bare = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    assert bare.returncode == 0
    assert (workspace / "in.txt").exists() and (outside / "o.txt").exists(), "裸跑对照必须写得出来"
    for leftover in (workspace / "in.txt", workspace / "sub" / "deep.txt", outside / "o.txt"):
        leftover.unlink()

    # 授权前：连"工作区"里也写不了（写权限只来自 Users 组，而组不在 restricting 列表里）
    before = win_sandbox.run_sandboxed(command, writable_sids=(sid,))
    assert before.exit_code == 0
    assert not (workspace / "in.txt").exists(), "未给工作区 SID 授权时不该写得进去"

    win_sandbox.grant_write(str(workspace), sid)
    after = win_sandbox.run_sandboxed(command, writable_sids=(sid,))

    assert after.exit_code == 0
    assert after.stdout, "受限进程的 stdout 必须能被匿名管道捕获"
    assert (workspace / "in.txt").exists()
    assert (workspace / "sub" / "deep.txt").exists(), "继承 ACE 应让子目录同样可写"
    assert not (outside / "o.txt").exists(), "未授权目录必须写不出去"
    assert after.job_error is None, "KILL_ON_JOB_CLOSE 的 Job 必须装得上"


def test_revoke_write_removes_the_grant(tmp_path) -> None:
    """撤销授权后，同一个受限令牌不再能写该目录（授权可控、不是一次性污染）。"""
    from local_agent.security import win_sandbox

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _group_only_dir(workspace)
    sid = win_sandbox.workspace_sid_text(str(workspace))
    command = ["cmd.exe", "/d", "/s", "/c", f"echo x > {workspace}\\in.txt"]

    win_sandbox.grant_write(str(workspace), sid)
    assert win_sandbox.run_sandboxed(command, writable_sids=(sid,)).exit_code == 0
    assert (workspace / "in.txt").exists()
    (workspace / "in.txt").unlink()

    win_sandbox.revoke_write(str(workspace), sid)
    # 撤销后写入被拒：`cmd` 因重定向失败返回非 0，"文件没写出来"才是判据（别把返回码当成功标志）。
    after = win_sandbox.run_sandboxed(command, writable_sids=(sid,))
    assert after.exit_code != 0
    assert not (workspace / "in.txt").exists(), "撤销后不该再写得进去"


def _pid_alive(pid: int) -> bool:
    """PID 还活着吗。

    签名**显式声明**：`OpenProcess` 的默认 `restype` 是 `c_int`，64 位句柄会被截断，判据就变成假的——
    2026-10-14 的探针正是栽在这里（把"打不开"读成了"已经死了"，据此误判过 Job 收口）。
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(0x00100000, False, pid)  # PROCESS_SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def test_runner_exit_takes_its_descendants_with_it(tmp_path) -> None:
    """runner 退出 → Job 关闭 → **孙进程被收走**（不依赖 `taskkill` 的尽力而为）。

    观测方式换过一轮（2026-10-14）：原来靠"孙进程写标记文件"，但那个标记写在**未授权**目录里，而
    实测证明受限进程在那里根本写不进去——两个事实无法同时为真，旧观测作废。现在：

    - 孙进程是 `ping`（不写文件、不碰 ssl/ctypes，避开受限环境的已知缺口）；
    - PID 由子进程写进**已授权**目录（这条路实测可用）；
    - **中途一次正对照**：runner 还握着 Job 句柄时必须观测到"孙进程活着"，否则测量无效；
    - runner 退出后再观测"孙进程没了"——那才是收口判据。
    """
    import time

    from local_agent.security import win_sandbox

    holder = tmp_path / "holder"
    holder.mkdir()
    sid = win_sandbox.workspace_sid_text(str(holder))
    win_sandbox.grant_write(str(holder), sid)
    pid_file = holder / "grandchild.pid"

    child = (
        "import pathlib, subprocess, time;"
        "ping = subprocess.Popen(['ping', '-n', '30', '127.0.0.1'], stdout=subprocess.DEVNULL);"
        f"pathlib.Path(r'{pid_file}').write_text(str(ping.pid), encoding='utf-8');"
        "time.sleep(8)"
    )

    runner = subprocess.Popen(
        [
            sys.executable, "-m", "local_agent.security.win_sandbox",
            "--sid", sid, "--", sys.executable, "-c", child,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    grandchild = 0
    try:
        deadline = time.monotonic() + 25
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert pid_file.exists(), "孙进程 PID 没能写进已授权目录（观测失败，不是收口失败）"
        grandchild = int(pid_file.read_text(encoding="utf-8").strip())

        # 正对照：runner 还在跑（子进程 sleep 8 秒，Job 句柄仍被持有）→ 孙进程必须活着。
        assert _pid_alive(grandchild), "Job 还开着时孙进程就该活着，否则后面的判据不成立"

        runner.terminate()  # 异常退出必须清理后代，区别于正常返回保留后台服务。
        _out, err = runner.communicate(timeout=60)
        assert runner.returncode != 0, err
        assert "windows-acl-run" not in err, "Job 没装上就等于没有兜底"

        time.sleep(2)
        assert not _pid_alive(grandchild), "runner 退出后孙进程必须被 Job 收走"
    finally:
        runner.poll()
        if _pid_alive(grandchild):
            subprocess.run(
                ["taskkill", "/PID", str(grandchild), "/F"], capture_output=True, check=False
            )


def test_runner_failure_is_closed_not_degraded() -> None:
    """runner 建不出令牌（SID 不合法）时**以 127 退出且不执行命令**：fail-closed，不是裸跑。"""
    result = subprocess.run(
        [
            sys.executable, "-m", "local_agent.security.win_sandbox",
            "--sid", "S-1-5-不是SID", "--", "cmd.exe", "/d", "/s", "/c", "echo 不该出现",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )

    assert result.returncode == 127
    assert "windows-acl-run" in result.stderr
    # 命令**一个字都没输出**：这才是"没裸跑"的判据（比"输出里没有某句"更硬）。
    assert result.stdout == ""


def test_sid_from_text_owns_its_memory() -> None:
    """SID 文本转换必须**自带内存**：随后反复申请/释放同尺寸内存，值不能变。

    回归用例（2026-10-14）：原实现返回指向 `ConvertStringSidToSidW` 缓冲的**视图**，`LocalFree`
    之后就是悬垂指针——症状是"单跑绿、和其它用例合跑随机 `SetEntriesInAclW 87`"，因为被释放的
    内存正好被别人复用；更坏的情况是把垃圾当成 SID 授出去（静默的错误授权）。
    """
    from local_agent.security import win_sandbox

    sid_text = win_sandbox.workspace_sid_text("E:/some/workspace-for-sid-test")
    sid = win_sandbox._sid_from_text(sid_text)

    # 制造内存复用：反复分配/释放**同尺寸**的 SID 缓冲（每轮都走 ConvertStringSidToSidW + LocalFree）。
    for _ in range(200):
        win_sandbox._sid_to_text(win_sandbox._sid_from_text("S-1-1-0"))

    assert win_sandbox._advapi32.IsValidSid(ctypes.byref(sid)), "SID 结构本身必须一直合法"
    # **确定性判据**（2026-10-14）：`_b_base_` 不为 None 就说明这是"指向别人缓冲的**视图**"，而那个缓冲
    # 已被 LocalFree——悬垂。只靠"值看起来还对"抓不到它：实测单跑绿、合跑随机 `SetEntriesInAclW 87`。
    assert sid._b_base_ is None, "SID 必须自带内存，不能是指向他人缓冲的视图"
    assert win_sandbox._sid_to_text(sid) == sid_text, "内存被复用后 SID 的值不能变"


def test_read_only_commands_skip_approval_but_a_redirection_does_not(tmp_path) -> None:
    """Windows 上的判定落点：`time /T` 走**只读安全清单**，而 `echo hi > f.txt` 有副作用、照旧审批。

    这条把"清单"与"真判定"接起来：清单只覆盖**没有 Shell 语法**的命令，重定向一律回到审批那条路。
    """
    from local_agent.config import ShellSecurityConfig
    from local_agent.security.shell_guard import prepare_shell

    config = ShellSecurityConfig()

    safe = prepare_shell("time /T", config=config, workspace_root=tmp_path, platform="windows")
    assert safe.safe_read_only is True
    assert safe.allowed is True and safe.requires_approval is False

    redirected = prepare_shell(
        "echo hi > f.txt", config=config, workspace_root=tmp_path, platform="windows"
    )
    assert redirected.safe_read_only is False, "重定向要落盘，不是只读"
    assert redirected.requires_approval is True


def test_runner_argv_wraps_without_touching_the_command() -> None:
    """接线只换"怎么起"：命令本身一字不改地落在 `--` 之后（§20.1）。"""
    from local_agent.security import win_sandbox

    command = ["cmd.exe", "/d", "/s", "/c", "echo hi > out.txt"]
    wrapped = win_sandbox.runner_argv(command, writable_sids=("S-1-5-1", "S-1-5-2"))

    assert wrapped[:4] == [sys.executable, "-I", "-m", "local_agent.security.win_sandbox"]
    assert wrapped.count("--sid") == 2
    assert wrapped[-len(command) :] == command, "命令必须原样落在 -- 之后"
    assert wrapped[-len(command) - 1] == "--"


def test_registry_can_run_a_command_through_the_sandbox(tmp_path) -> None:
    """出口这一层真能把命令送进沙箱：结果形状不变、写只落在被授权的目录。

    这条是**接线的判据**：命令经 runner 转一圈回来后，`ProcessOutcome` 的语义照旧（退出码真实、
    输出可捕获），而写被限制在授权目录内。
    """
    from local_agent.security import win_sandbox
    from local_agent.security.process_guard import ProcessRegistry, SandboxRequest

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _group_only_dir(workspace)
    sid = win_sandbox.workspace_sid_text(str(workspace))
    win_sandbox.grant_write(str(workspace), sid)
    command = ["cmd.exe", "/d", "/s", "/c", f"echo hi > {workspace}\\sandboxed.txt"]

    outcome = ProcessRegistry().run(
        command, timeout_seconds=60, sandbox=SandboxRequest(writable_sids=(sid,))
    )

    assert outcome.exit_code == 0
    assert outcome.timed_out is False and outcome.cancelled is False
    assert (workspace / "sandboxed.txt").exists(), "授权目录内必须写得进去"
    win_sandbox.revoke_write(str(workspace), sid)


def test_run_shell_goes_through_the_sandbox_when_enabled(tmp_path) -> None:
    """**端到端**：打开开关后 `run_shell` 的命令真的经沙箱跑，写落工作区根与专属临时目录。

    这条走的是完整那一路：`run_shell_command` → 安全判定与指纹（含人工批准）→ 探针出口 →
    沙箱 runner → 受限令牌进程。判据只看两件事：命令自身的退出码照旧，写**只**落在被授权的两处。
    """
    from local_agent.security import win_sandbox
    from local_agent.security.approval import action_fingerprint
    from local_agent.tools.shell_tools import run_shell_command

    policy = _policy(tmp_path, sandbox=True)
    command = "echo hi > via-shell.txt & echo tmp > %TEMP%\\via-shell.txt"
    fingerprint = action_fingerprint(
        "run_shell", {"command": command, "cwd": ".", "env_keys": []}, policy=policy
    )

    policy.grant(fingerprint)  # 含 shell 语法的命令走人工批准这条路
    try:
        result = run_shell_command(command, policy=policy)
    finally:
        policy.revoke(fingerprint)

    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "via-shell.txt").exists(), "工作区根必须写得进去"
    temp_dir = win_sandbox.sandbox_temp_dir(tmp_path)
    assert (temp_dir / "via-shell.txt").exists(), "专属临时目录（%TEMP%）必须写得进去"
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_run_shell_sandbox_is_off_under_the_danger_tier(tmp_path) -> None:
    """「完全放行」档下同一条命令**不进**沙箱：工作区外的用户可写目录照样能写（对照组）。

    没有这条对照，"沙箱生效时写不进去"可能只是环境本来如此——本仓在这类判据上已经栽过一次。
    """
    from local_agent.security.approval import action_fingerprint
    from local_agent.tools.shell_tools import run_shell_command

    outside = tmp_path / "outside"
    outside.mkdir()
    command = f"echo hi > {outside}\\plain.txt"
    policy = _policy(tmp_path, sandbox=False)
    fingerprint = action_fingerprint(
        "run_shell", {"command": command, "cwd": ".", "env_keys": []}, policy=policy
    )

    policy.grant(fingerprint)
    try:
        result = run_shell_command(command, policy=policy)
    finally:
        policy.revoke(fingerprint)

    assert result.exit_code == 0, result.stderr
    assert (outside / "plain.txt").exists(), "默认路径（不进沙箱）行为必须一字不变"


def test_escalation_approval_does_not_leak_into_a_normal_call(tmp_path) -> None:
    """提权的批准**不能**用到同一条命令的普通调用上：两者的动作指纹必须不同。

    这是这条机制的**安全要点**：指纹漏掉 `escalate_sandbox`，"在沙箱里跑"的批准就会被复用到
    "跳出沙箱跑"上——一个参数之差，边界就没了。
    """
    from local_agent.security.approval import action_fingerprint

    policy = _policy(tmp_path, sandbox=True)
    common = {"command": "echo hi > out.txt", "cwd": ".", "env_keys": []}

    normal = action_fingerprint("run_shell", common, policy=policy)
    escalated = action_fingerprint(
        "run_shell", {**common, "escalate_sandbox": True}, policy=policy
    )

    assert normal != escalated


def test_escalation_skips_the_sandbox_for_that_call_only(tmp_path) -> None:
    """提权的那一次真的跳出沙箱；**同一条命令不带提权**则照旧写不出去。

    判据两侧都要有：只有"提权后写得进去"证明跳出了沙箱，只有"不带提权写不进去"证明它**没有**
    把边界永久放宽。
    """
    from local_agent.security.approval import action_fingerprint
    from local_agent.tools.shell_tools import run_shell_command

    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _group_only_dir(workspace)
    _group_only_dir(outside)
    policy = _policy(workspace, sandbox=True)

    command = f"echo hi > {outside}\\escalated.txt"

    # 没有批准：提权本身**必须**人工批准，连启动都不该发生。
    with pytest.raises(SecurityError):
        run_shell_command(command, policy=policy, escalate_sandbox=True)
    assert not (outside / "escalated.txt").exists(), "没批准就不该有副作用"

    fingerprint = action_fingerprint(
        "run_shell",
        {"command": command, "cwd": ".", "env_keys": [], "escalate_sandbox": True},
        policy=policy,
    )
    policy.grant(fingerprint)
    try:
        result = run_shell_command(command, policy=policy, escalate_sandbox=True)
    finally:
        policy.revoke(fingerprint)

    assert result.exit_code == 0, result.stderr
    assert (outside / "escalated.txt").exists(), "提权的这一次应当跳出沙箱写成功"

    # 只这一次：同一条命令不带提权 → **沙箱内的写边界照旧生效**。
    #
    # 判据从"被拒绝执行"改成"写不出去"（2026-10-15）：沙箱生效时常规命令免逐条审批，
    # 所以这条命令会真的跑起来，但重定向目标在工作区外，由沙箱当场挡下。
    # 提权跳出去的是"这一次的沙箱"，不是"沙箱这条规则"——这一点由下面的文件不存在来证明。
    (outside / "escalated.txt").unlink()
    plain = run_shell_command(command, policy=policy)

    assert plain.exit_code != 0, "不带提权时，工作区外的重定向必须被沙箱挡下"
    assert not (outside / "escalated.txt").exists(), "沙箱边界没有被永久放宽"


def test_shell_sandbox_also_covers_authorised_read_write_directories(tmp_path) -> None:
    """「工作区可写」档授权的**读写**目录也落在沙箱可写范围内（吸收 codex `--add-dir` 的做法）。

    没有这一格，组合就是自相矛盾的：档位说"这个目录你可以读写"，沙箱却把写挡在外面。
    只读授权**不**受影响——"能读不能写"本来就该由沙箱照旧挡住。
    """
    from local_agent.config import SecurityConfig, ShellSecurityConfig, WorkspaceConfig
    from local_agent.security import win_sandbox
    from local_agent.security.path_guard import ExternalPathPolicy, normalize_grant_path
    from local_agent.security.policy import SecurityPolicy
    from local_agent.security.process_guard import ProcessRegistry

    workspace = tmp_path / "ws"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    _group_only_dir(workspace)
    _group_only_dir(shared)

    policy = SecurityPolicy(
        workspace_root=workspace,
        security=SecurityConfig(shell=ShellSecurityConfig(sandbox=True)),
        workspace=WorkspaceConfig(),
        external_policy=ExternalPathPolicy(
            mode="workspace-write", grants={normalize_grant_path(str(shared)): "read_write"}
        ),
    )
    request = policy.shell_sandbox_request()
    assert request is not None

    outcome = ProcessRegistry().run(
        ["cmd.exe", "/d", "/s", "/c", f"echo hi > {shared}\\shared.txt"],
        timeout_seconds=60,
        sandbox=request,
    )

    assert outcome.exit_code == 0, outcome
    assert (shared / "shared.txt").exists(), "授权为读写的目录必须写得进去"
    shutil.rmtree(win_sandbox.sandbox_temp_dir(workspace), ignore_errors=True)


def test_permission_store_keeps_the_tier_and_normalises_legacy_values(tmp_path) -> None:
    """档位在设置库里的读写口径（2026-10-15 权限模型重构）。

    写侧沙箱的**独立开关已删除**（沙箱由档位派生），这里只守档位本身：新三档存得下、读得回，
    老库里存着的旧值（deny / whitelist / allow）读回来要**归一**成新档位——读不回来就等于
    把用户的选择静默丢掉，页面与判定会一起退回出厂默认。
    """
    from local_agent.model_settings.store import META_EXTERNAL_PATHS_MODE, open_settings_database

    settings = open_settings_database(tmp_path / "settings.sqlite")
    assert settings.permissions.mode() == "workspace-write", "出厂默认是「工作区可写」"

    settings.permissions.set_mode("read-only")
    assert settings.permissions.mode() == "read-only", "新档位存得下、读得回"

    def store_raw(value: str) -> None:
        settings.connection.execute(
            "UPDATE settings_meta SET value = ? WHERE key = ?", (value, META_EXTERNAL_PATHS_MODE)
        )
        settings.connection.commit()

    store_raw("allow")
    assert settings.permissions.mode() == "danger-full-access", "旧值 allow 归一为完全放行"
    store_raw("whitelist")
    assert settings.permissions.mode() == "workspace-write", "旧值 whitelist 归一为工作区可写"

    store_raw("maybe")
    assert settings.permissions.mode() == "read-only", "未知文本不能回落到可写默认档"

    # 开关没了：这些成员不该再存在（留着就会有人以为它还能用）。
    assert not hasattr(settings.permissions, "shell_sandbox")
    assert not hasattr(settings.permissions, "set_shell_sandbox")


def test_non_windows_runs_without_an_os_boundary_but_says_so(tmp_path, monkeypatch) -> None:
    """非 Windows：**照常跑**，但必须如实说明"没有 OS 级写边界"（2026-10-15 用户裁定）。

    此前这里是 fail-closed（非 Windows 直接拒绝执行）。改成照常跑之后，静默跑掉就等于假装
    有边界——那是 dsh 移植笔记里点名的 fail-open 坑，所以配了 `sandbox_boundary_note()`。
    """
    from local_agent.config import ExternalPathsConfig, SecurityConfig, WorkspaceConfig
    from local_agent.security import win_sandbox
    from local_agent.security.policy import SecurityPolicy

    monkeypatch.setattr(win_sandbox, "is_supported", lambda: False)
    policy = SecurityPolicy(
        workspace_root=tmp_path,
        security=SecurityConfig(),
        workspace=WorkspaceConfig(external_paths=ExternalPathsConfig(mode="workspace-write")),
    )

    assert policy.shell_sandbox_request() is None, "没有实现就不套沙箱（不再拒绝执行）"
    note = policy.sandbox_boundary_note()
    assert note is not None and "没有 OS 级写边界" in note
    assert policy.shell_sandbox_active() is False


def test_sandbox_request_is_fail_closed_without_a_platform_implementation(monkeypatch) -> None:
    """没有实现时**拒绝启动**、不静默裸跑（fail-closed）。"""
    from local_agent.security import process_guard, win_sandbox

    monkeypatch.setattr(win_sandbox, "is_supported", lambda: False)
    with pytest.raises(win_sandbox.SandboxUnavailable):
        process_guard.sandboxed_argv(["cmd.exe", "/c", "echo hi"], process_guard.SandboxRequest())


def _policy(workspace, *, sandbox: bool):
    """按**档位**构造策略：`sandbox=True` ⇒「工作区可写」档（沙箱生效），
    `sandbox=False` ⇒「完全放行」档（不套沙箱）。

    这层兼容映射是给本文件里既有的沙箱用例复用的：那些用例要验的是"沙箱存在时命令怎么跑"，
    与"哪个旋钮把它打开"无关——2026-10-15 起旋钮没了，沙箱由档位派生。
    """
    from local_agent.config import ExternalPathsConfig, SecurityConfig, WorkspaceConfig
    from local_agent.security.policy import SecurityPolicy

    return SecurityPolicy(
        workspace_root=workspace,
        security=SecurityConfig(),
        workspace=WorkspaceConfig(
            external_paths=ExternalPathsConfig(
                mode="workspace-write" if sandbox else "danger-full-access"
            )
        ),
    )


def test_shell_sandbox_follows_the_tier(tmp_path) -> None:
    """**沙箱由档位派生，不再有独立开关**（2026-10-15 权限模型重构）。

    三档各自的答案：``read-only`` 与 ``workspace-write`` 套沙箱（前者不给任何写授权）、
    ``danger-full-access`` 不套——那一档的语义就是"不设 OS 边界"。
    """
    from local_agent.config import ExternalPathsConfig, SecurityConfig, WorkspaceConfig
    from local_agent.security import win_sandbox
    from local_agent.security.policy import SecurityPolicy

    def request_for(mode: str):
        policy = SecurityPolicy(
            workspace_root=tmp_path,
            security=SecurityConfig(),
            workspace=WorkspaceConfig(external_paths=ExternalPathsConfig(mode=mode)),
        )
        return policy, policy.shell_sandbox_request(), policy.sandbox_boundary_note()

    _writable, writable_request, note = request_for("workspace-write")
    assert writable_request is not None and writable_request.writable_sids
    assert note is None, "有 OS 级写边界时不该有那句说明"

    read_only, read_only_request, _ = request_for("read-only")
    assert read_only_request is not None, "只读档也要套沙箱：它是「写不进去」的实现手段"
    assert read_only_request.writable_sids == (), "只读档不给任何写授权"
    assert read_only.shell_sandbox_active() is True

    full, full_request, full_note = request_for("danger-full-access")
    assert full_request is not None and full.shell_sandbox_active() is True
    assert full_note is None

    shutil.rmtree(win_sandbox.sandbox_temp_dir(tmp_path), ignore_errors=True)


def test_shell_sandbox_request_covers_workspace_and_its_own_temp(tmp_path) -> None:
    """工作区与专属临时目录使用独立 SID；临时变量指向后者。"""
    from local_agent.security import win_sandbox

    policy = _policy(tmp_path, sandbox=True)
    request = policy.shell_sandbox_request()

    assert request is not None
    expected = win_sandbox.workspace_sid_text(str(tmp_path))
    temp_dir = win_sandbox.sandbox_temp_dir(tmp_path)
    assert request.writable_sids == (expected, win_sandbox.workspace_sid_text(str(temp_dir)))
    assert temp_dir.is_dir(), "临时目录要真的建出来"
    assert dict(request.extra_env) == {
        "TMP": str(temp_dir),
        "TEMP": str(temp_dir),
        "TMPDIR": str(temp_dir),
    }
    assert policy.shell_sandbox_request() == request, "重新扫描不改变可写根的 SID"
    assert policy._sandbox_materialized is True
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_sandboxed_command_can_write_to_its_own_temp(tmp_path) -> None:
    """沙箱命令能写**自己的**临时目录（`%TEMP%` 已被指过去）——否则大量命令会成片失败。

    这条同时是"临时目录"与"接线"两格的判据：命令经 runner 起、`ProcessOutcome` 语义照旧，
    而写落在专属临时目录里；写到系统 `%TEMP%` 本体则会被写限制拒掉（授权只给了这一格）。
    """
    from local_agent.security import win_sandbox
    from local_agent.security.process_guard import ProcessRegistry

    policy = _policy(tmp_path, sandbox=True)
    request = policy.shell_sandbox_request()
    assert request is not None
    temp_dir = win_sandbox.sandbox_temp_dir(tmp_path)

    outcome = ProcessRegistry().run(
        ["cmd.exe", "/d", "/s", "/c", "echo hi > %TEMP%\\from-sandbox.txt"],
        timeout_seconds=60,
        sandbox=request,
    )

    assert outcome.exit_code == 0
    assert (temp_dir / "from-sandbox.txt").exists(), "被指过去的临时目录必须可写"
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_shell_sandbox_fails_closed_when_materialization_fails(monkeypatch, tmp_path) -> None:
    """授权物化失败时**抛错、不裸跑**——"以为有沙箱其实没有"是最危险的失效方式。"""
    from local_agent.exceptions import SecurityError
    from local_agent.security import win_sandbox

    def _boom(*_args, **_kwargs):
        raise OSError("模拟物化失败")

    monkeypatch.setattr(win_sandbox, "grant_write", _boom)
    with pytest.raises(SecurityError, match="无法物化写授权"):
        _policy(tmp_path, sandbox=True).shell_sandbox_request()


def test_grant_write_writes_one_inheritable_ace_at_the_root_only(tmp_path, monkeypatch) -> None:
    """授权只在**根上**写一条可继承 ACE（甲方案）：子目录一条都不写。

    旧实现逐目录写（`OBJECT_INHERIT_ACE` 只传给文件），因此每个目录都得自己写一条，
    代价是每条命令重走整棵树。这里钉住新形状：两次写都落在根上（用户 ACE + 沙箱 SID ACE），
    子目录与更深层目录不被访问。
    """
    from local_agent.security import win_sandbox

    workspace = tmp_path / "ws"
    (workspace / "locked" / "deep").mkdir(parents=True)
    (workspace / "ok").mkdir()
    sid = win_sandbox.workspace_sid_text(str(workspace))
    seen: list[str] = []
    monkeypatch.setattr(win_sandbox, "_MATERIALIZED_ROOTS", {})
    monkeypatch.setattr(win_sandbox, "_dacl_mentions_sid", lambda _path, _sid: False)
    monkeypatch.setattr(
        win_sandbox, "_set_access", lambda path, entry: seen.append(str(path))
    )

    report = win_sandbox.grant_write(str(workspace), sid)

    assert seen == [str(workspace), str(workspace)]
    assert report.skipped == ()


def test_grant_write_is_a_no_op_when_already_materialized_or_already_granted(
    tmp_path, monkeypatch
) -> None:
    """两道跳过：进程内缓存，以及**跨重启**靠 DACL 里已有这条 SID 就不重写。

    第二条是关键（dsh `README.zh.md:55`）：重复应用同一条可继承 ACE 会再次触发"急切的全树
    传播"，大型工作区上以分钟计——所以"已经在了"必须能认出来。
    """
    from local_agent.security import win_sandbox

    workspace = tmp_path / "ws"
    workspace.mkdir()
    sid = win_sandbox.workspace_sid_text(str(workspace))
    writes: list[str] = []
    monkeypatch.setattr(win_sandbox, "_MATERIALIZED_ROOTS", {})
    monkeypatch.setattr(win_sandbox, "_set_access", lambda path, entry: writes.append(str(path)))

    # ① DACL 里已有这条 SID ⇒ 一次都不写
    monkeypatch.setattr(win_sandbox, "_dacl_mentions_sid", lambda _path, _sid: True)
    win_sandbox.grant_write(str(workspace), sid)
    assert writes == []

    # ② 进程内第二次调用：连 DACL 都不再读（缓存命中）
    monkeypatch.setattr(
        win_sandbox,
        "_dacl_mentions_sid",
        lambda *_args: pytest.fail("缓存命中后不该再读 DACL"),
    )
    assert win_sandbox.grant_write(str(workspace), sid).skipped == ()


def test_dacl_mentions_sid_reads_the_real_acl(tmp_path) -> None:
    """幂等检查读的是真实 DACL：授权之后必须能认出自己写进去的 SID。"""
    from local_agent.security import win_sandbox

    workspace = tmp_path / "ws"
    workspace.mkdir()
    sid = win_sandbox.workspace_sid_text(str(workspace))

    assert win_sandbox._dacl_mentions_sid(str(workspace), sid) is False
    win_sandbox._set_access(
        str(workspace),
        win_sandbox._access_entry(
            sid, win_sandbox.GRANT_ACCESS,
            inheritance=win_sandbox.CONTAINER_INHERIT_ACE | win_sandbox.OBJECT_INHERIT_ACE,
        ),
    )
    assert win_sandbox._dacl_mentions_sid(str(workspace), sid) is True


def test_grant_write_still_fails_closed_when_the_writable_root_itself_cannot_be_granted(
    tmp_path, monkeypatch
) -> None:
    """**可写根**授权不了时仍然失败闭合：那时沙箱给不出任何写边界，不能假装它有。"""
    from local_agent.security import win_sandbox

    workspace = tmp_path / "ws"
    workspace.mkdir()
    sid = win_sandbox.workspace_sid_text(str(workspace))

    def refuse(_path, _entry):
        raise OSError(5, "拒绝访问")

    monkeypatch.setattr(win_sandbox, "_set_access", refuse)
    with pytest.raises(win_sandbox.SandboxUnavailable, match="可写根无法授权"):
        win_sandbox.grant_write(str(workspace), sid)


def test_skip_reason_says_what_to_do_instead_of_dumping_a_win32_code() -> None:
    """三类"拒绝访问"来源都要被认出来：我们的 Win32 包装、`OSError.winerror`、`errno`。

    实证里摆到用户面前的是 `SetNamedSecurityInfoW 失败：5（路径 …）`——他没法据此自救。
    """
    from local_agent.security import win_sandbox

    wrapped = win_sandbox.SandboxUnavailable("SetNamedSecurityInfoW 失败：5（路径 D:\\x）")
    winerror = OSError("拒绝访问")
    winerror.winerror = 5

    for error in (wrapped, winerror, OSError(13, "Permission denied")):
        assert "错误 5" in win_sandbox._skip_reason(error), error
    # 别的故障不许被归到"权限不足"里：分类错了会把用户引到错误的修法上。
    assert "RuntimeError" in win_sandbox._skip_reason(RuntimeError("磁盘满了"))


def test_grant_write_skips_a_directory_it_cannot_enumerate(tmp_path, monkeypatch) -> None:
    """`os.walk` 进不去的目录同样只跳过：实证里那台机器正是"连枚举都被拒"（`[WinError 5]`）。"""
    from local_agent.security import win_sandbox

    workspace = tmp_path / "ws"
    locked = workspace / "locked"
    locked.mkdir(parents=True)
    sid = win_sandbox.workspace_sid_text(str(workspace))

    def fake_walk(root, followlinks=False, onerror=None):
        del followlinks
        yield (str(root), ["locked"], [])
        onerror(OSError(5, "拒绝访问", str(locked)))
        yield (str(locked), [], [])

    monkeypatch.setattr(win_sandbox.os, "walk", fake_walk)
    report = win_sandbox.grant_write(str(workspace), sid)

    assert [item.path for item in report.skipped] == [str(locked)]
    assert "错误 5" in report.skipped[0].reason


def test_shell_sandbox_request_surfaces_skipped_directories(tmp_path, monkeypatch) -> None:
    """跳过的事实必须**随请求带走**：`run_shell` 的结果说明要靠它，不然就是静默跳过。"""
    from local_agent.security import win_sandbox

    policy = _policy(tmp_path, sandbox=True)
    locked = win_sandbox.SkippedDirectory(
        str(tmp_path / "locked"), "当前账户没有修改该目录权限的权限（Windows 错误 5：拒绝访问）"
    )
    real_grant = win_sandbox.grant_write
    monkeypatch.setattr(
        win_sandbox,
        "grant_write",
        lambda path, sid: win_sandbox.GrantReport(
            skipped=(locked,) if os.path.samefile(path, tmp_path) else real_grant(path, sid).skipped
        ),
    )

    request = policy.shell_sandbox_request()

    assert request is not None and request.skipped == (locked,)
    shutil.rmtree(win_sandbox.sandbox_temp_dir(tmp_path), ignore_errors=True)


def test_run_shell_note_reports_skipped_sandbox_grants(tmp_path, monkeypatch) -> None:
    """跳过授权是**用户与模型都要看到**的事实：写进 note，而命令照常执行。

    真授权照做（命令得真的能写），只在一个可写根上注入一条"跳过"——这才是实证里的形状：
    **命令跑得起来，但工作区里有目录写不进去**。
    """
    from local_agent.security import win_sandbox
    from local_agent.security.approval import action_fingerprint
    from local_agent.tools.shell_tools import run_shell_command

    policy = _policy(tmp_path, sandbox=True)
    locked = win_sandbox.SkippedDirectory(
        str(tmp_path / "locked"), "当前账户没有修改该目录权限的权限（Windows 错误 5：拒绝访问）"
    )
    real_grant = win_sandbox.grant_write

    def grant_with_one_skip(path, sid):
        report = real_grant(path, sid)
        if os.path.samefile(path, tmp_path):
            return win_sandbox.GrantReport(skipped=(*report.skipped, locked))
        return report

    monkeypatch.setattr(win_sandbox, "grant_write", grant_with_one_skip)
    command = "echo hi > via-shell.txt"
    fingerprint = action_fingerprint(
        "run_shell", {"command": command, "cwd": ".", "env_keys": []}, policy=policy
    )
    policy.grant(fingerprint)
    try:
        result = run_shell_command(command, policy=policy)
    finally:
        policy.revoke(fingerprint)

    assert result.exit_code == 0, result.stderr
    assert result.note is not None
    assert "1 个目录没能授权" in result.note
    assert "locked" in result.note and "错误 5" in result.note
    # 只说"会被拒绝"没有指路（2026-10-15 裁定 B1）：下一步要能照着做。
    assert "escalate_sandbox=true" in result.note
    assert "取得修改该目录权限的权限" in result.note
    # 不替用户建议"关掉沙箱"：那会连整轮的写边界一起去掉。
    assert "关掉" not in result.note and "关闭" not in result.note
    shutil.rmtree(win_sandbox.sandbox_temp_dir(tmp_path), ignore_errors=True)


def test_runner_ignores_packages_and_pythonpath_from_command_workspace(tmp_path) -> None:
    """不可信 cwd/PYTHONPATH 的同名包不能在创建受限令牌之前执行。"""
    from local_agent.security import win_sandbox
    from local_agent.security.process_guard import ProcessRegistry, SandboxRequest
    from local_agent.security.shell_guard import build_environment

    workspace = tmp_path / "ws"
    package = workspace / "local_agent"
    package.mkdir(parents=True)
    marker = tmp_path / "hijacked.txt"
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('hijacked')\n",
        encoding="utf-8",
    )
    env = build_environment(())
    env["PYTHONPATH"] = str(workspace)
    sid = win_sandbox.workspace_sid_text(str(workspace))
    win_sandbox.grant_write(str(workspace), sid)
    result = ProcessRegistry().run(
        ["cmd.exe", "/d", "/s", "/c", "echo TRUSTED_RUNNER"], cwd=workspace,
        env=env, timeout_seconds=30, sandbox=SandboxRequest(writable_sids=(sid,)),
    )
    assert result.exit_code == 0, result.stderr
    assert b"TRUSTED_RUNNER" in result.stdout
    assert not marker.exists()


def test_explicit_current_user_ace_does_not_allow_ungranted_writes(tmp_path) -> None:
    """保留用户显式 ACE 的普通目录也必须挡写，不能先把漏洞条件清掉再测试。"""
    import csv

    from local_agent.security import win_sandbox

    outside = tmp_path / "outside"
    outside.mkdir()
    user = next(csv.reader(subprocess.check_output(
        ["whoami", "/user", "/fo", "csv", "/nh"], text=True,
    ).splitlines()))[1]
    subprocess.run(
        ["icacls", str(outside), "/grant", f"*{user}:(OI)(CI)M"],
        capture_output=True, check=True,
    )
    target = outside / "outside.txt"
    command = ["cmd.exe", "/d", "/s", "/c", f"echo probe > {target}"]
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0
    target.unlink()
    result = win_sandbox.run_sandboxed(command, writable_sids=())
    assert result.exit_code != 0
    assert not target.exists()


@pytest.mark.parametrize("name", [".git", ".aws", ".ssh", ".gnupg", ".kube", ".docker", ".codex"])
def test_shell_sandbox_protects_credential_directories_present_at_scan_time(tmp_path, name) -> None:
    """物化时**已经存在**的凭据目录受保护：里面的旧凭据写不进去、改不掉。

    ⚠️ **扫描之后新建**的凭据目录不再受这份保护（2026-10-15，用户裁定照 dsh 的"根上可继承
    ACE"方案）。旧实现靠"新目录须等下一次调用重新扫描才可写"挡住了"同一条命令 mkdir .ssh
    然后往里写"，代价是**每条命令**都要重走整棵目录树（实测 6787 个目录 ≈ 45 秒）。取舍之后
    这条边界换成：新目录立即继承可写，但要等**下一次进程启动**的扫描才会被加上拒绝项。
    dsh 的同类实现（`sandbox-windows-acl`）没有凭据目录概念，因此我们的边界仍然更紧一档。
    """
    from local_agent.security.process_guard import ProcessRegistry

    protected = tmp_path / name
    protected.mkdir()
    existing = protected / "fake-credential.txt"
    existing.write_text("original", encoding="utf-8")
    policy = _policy(tmp_path, sandbox=True)
    request = policy.shell_sandbox_request()
    assert request is not None
    result = ProcessRegistry().run(
        ["cmd.exe", "/d", "/s", "/c", f"echo changed > {existing}"],
        timeout_seconds=30, sandbox=request,
    )
    assert result.exit_code != 0
    assert existing.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("mode,access", [("workspace-write", None), ("workspace-write", "read"), ("read-only", "read_write")])
def test_removed_or_downgraded_external_grant_is_absent_from_next_token(tmp_path, mode, access) -> None:
    from local_agent.security import win_sandbox
    from local_agent.security.path_guard import ExternalPathPolicy, normalize_grant_path
    from local_agent.security.process_guard import ProcessRegistry

    workspace = tmp_path / "ws"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    key = normalize_grant_path(shared)
    first = _policy(workspace, sandbox=True)
    first.external_policy = ExternalPathPolicy(mode="workspace-write", grants={key: "read_write"})
    request = first.shell_sandbox_request()
    target = shared / "old-file.txt"
    command = ["cmd.exe", "/d", "/s", "/c", f"echo original > {target}"]
    assert ProcessRegistry().run(command, sandbox=request, timeout_seconds=30).exit_code == 0
    assert target.exists()
    second = _policy(workspace, sandbox=True)
    second.external_policy = ExternalPathPolicy(mode=mode, grants={} if access is None else {key: access})
    request = second.shell_sandbox_request()
    result = ProcessRegistry().run(command, sandbox=request, timeout_seconds=30)
    assert result.exit_code != 0
    target.unlink()
    result = ProcessRegistry().run(command, sandbox=request, timeout_seconds=30)
    assert result.exit_code != 0
    assert not target.exists()
    shutil.rmtree(win_sandbox.sandbox_temp_dir(workspace))


def test_new_ordinary_directory_becomes_writable_on_next_call(tmp_path) -> None:
    from local_agent.security.process_guard import ProcessRegistry

    policy = _policy(tmp_path, sandbox=True)
    new = tmp_path / "build"
    request = policy.shell_sandbox_request()
    result = ProcessRegistry().run(
        ["cmd.exe", "/d", "/s", "/c", f"mkdir {new}"], sandbox=request, timeout_seconds=30,
    )
    assert result.exit_code == 0
    request = policy.shell_sandbox_request()
    result = ProcessRegistry().run(
        ["cmd.exe", "/d", "/s", "/c", f"echo built > {new / 'result.txt'}"],
        sandbox=request, timeout_seconds=30,
    )
    assert result.exit_code == 0
    assert (new / "result.txt").exists()


def test_resume_thread_failure_terminates_suspended_process(monkeypatch) -> None:
    from local_agent.security import win_sandbox

    terminated = []
    real_terminate = win_sandbox._kernel32.TerminateProcess

    def terminate(handle, code):
        terminated.append(code)
        return real_terminate(handle, code)

    monkeypatch.setattr(win_sandbox._kernel32, "ResumeThread", lambda _handle: 0xFFFFFFFF)
    monkeypatch.setattr(win_sandbox._kernel32, "TerminateProcess", terminate)
    with pytest.raises(win_sandbox.SandboxUnavailable, match="ResumeThread"):
        win_sandbox.run_sandboxed(["cmd.exe", "/c", "echo SHOULD_NOT_RUN"], writable_sids=())
    assert terminated == [127]


def test_default_dacl_supports_python_extensions_without_user_restricting_sid() -> None:
    from local_agent.security import win_sandbox

    result = win_sandbox.run_sandboxed(
        [sys.executable, "-c", "import ctypes, ssl, hashlib; print('EXTENSIONS_OK')"],
        writable_sids=(),
    )
    assert result.exit_code == 0, result.stdout
    assert b"EXTENSIONS_OK" in result.stdout

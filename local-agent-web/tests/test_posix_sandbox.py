"""POSIX 沙箱的**参数构造**（2026-11-02）。

这些用例在 Windows 上也能跑——它们只验证"要交给 bwrap / sandbox-exec 的 argv 与 profile 长什么样"。
真正的边界是否生效由 `scripts/sandbox_smoke.py` 在对应平台上验（CI 的 sandbox-smoke job）。

为什么值得单测参数：这一层的错法都是**静默**的——少一个 `--ro-bind` 就是"什么都没隔离"，
少一个 `--bind` 就是"工作区写不进去、模型以为是权限问题"，而两者都不会抛异常。
"""

from __future__ import annotations

import pytest

from local_agent.security import posix_sandbox


def test_bwrap_argv_lays_read_only_view_then_workspace() -> None:
    argv = posix_sandbox.build_bwrap_argv(
        ["sh", "-c", "touch a"], writable_roots=["/work", "/data"], cwd="/work",
    )

    joined = " ".join(argv)
    # 只读全景必须先出现：bwrap 里**后写的 bind 覆盖前面的同路径**，顺序反了就变成"全盘可写"。
    assert argv.index("--ro-bind") < argv.index("--bind")
    assert "--unshare-pid" in argv and "--die-with-parent" in argv
    # 私有 tmpfs + TMPDIR：命令写临时文件才不会成片失败。
    assert " --tmpfs /tmp " in joined
    assert " --setenv TMPDIR /tmp " in joined
    # 可写根逐个绑定（重复的会去重）。
    assert joined.count("--bind /work /work") == 1
    assert " --bind /data /data " in joined
    # 命令必须在最后：放中间会被 bwrap 当自己的参数吃掉。
    assert argv[-3:] == ["sh", "-c", "touch a"]


def test_bwrap_argv_allows_no_writable_root() -> None:
    """只读档：一个可写根都不给，但仍然进沙箱（/tmp 是 tmpfs，命令能写临时文件）。"""
    argv = posix_sandbox.build_bwrap_argv(["true"], writable_roots=[])

    assert "--bind" not in argv
    assert "--ro-bind" in argv


def test_relative_roots_are_rejected() -> None:
    """相对路径会相对**沙箱内**的 cwd 解析，等于授权了别的地方——必须拒绝。"""
    with pytest.raises(ValueError):
        posix_sandbox.build_bwrap_argv(["true"], writable_roots=["relative/dir"])
    with pytest.raises(ValueError):
        posix_sandbox.build_bwrap_argv(["true"], cwd="relative/dir")


def test_seatbelt_profile_order_is_deny_then_allow() -> None:
    profile = posix_sandbox.build_seatbelt_profile(["/work"])

    assert profile.startswith("(version 1)")
    # 先全局禁写、再逐路径放行：反序等于全禁（seatbelt 后写的覆盖先写的）。
    assert profile.index("(deny file-write*)") < profile.index("(allow file-write*")
    assert '(subpath "/work")' in profile
    # 设备节点必须放行，否则连 stdout 都写不了。
    assert '(literal "/dev/null")' in profile


def test_seatbelt_argv_shape() -> None:
    argv = posix_sandbox.build_seatbelt_argv(["sh", "-lc", "ls"], writable_roots=["/work"])

    assert argv[0].endswith("sandbox-exec") or argv[0] == "sandbox-exec"
    assert argv[1] == "-p"
    assert "(deny file-write*)" in argv[2]
    assert argv[3:] == ["sh", "-lc", "ls"]


def test_wrap_argv_is_explicit_about_unsupported_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    """在既不是 Linux 也不是 macOS 的机器上必须**抛错**，不能悄悄返回未包装的命令。"""
    monkeypatch.setattr(posix_sandbox.sys, "platform", "win32")

    with pytest.raises(RuntimeError):
        posix_sandbox.wrap_argv(["true"])


def test_linux_without_bwrap_falls_back_to_landlock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ubuntu 24.04 限制非特权 userns 时 bwrap 用不了——那时必须退到 landlock，而不是直接没有边界。"""
    monkeypatch.setattr(posix_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(posix_sandbox.shutil, "which", lambda name: None)
    monkeypatch.setattr(posix_sandbox.landlock, "available", lambda: True)

    assert posix_sandbox.backend() == "landlock"
    argv = posix_sandbox.wrap_argv(["true"], writable_roots=["/work"])
    assert argv[:4] == [posix_sandbox.sys.executable, "-I", "-m", "local_agent.security.landlock_exec"]
    assert argv[4:] == ["--writable", "/work", "--", "true"]


def test_linux_with_bwrap_prefers_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """bwrap 能给"换了个文件系统视图"，比纯访问控制更接近隔离——有就优先用。"""
    monkeypatch.setattr(posix_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(posix_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(posix_sandbox.landlock, "available", lambda: True)

    assert posix_sandbox.backend() == "bwrap"
    assert posix_sandbox.wrap_argv(["true"])[0] == "/usr/bin/bwrap"


def test_linux_without_either_says_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posix_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(posix_sandbox.shutil, "which", lambda name: None)
    monkeypatch.setattr(posix_sandbox.landlock, "available", lambda: False)

    reason = posix_sandbox.unavailable_reason()
    assert reason is not None and "bubblewrap" in reason and "landlock" in reason
    assert posix_sandbox.available() is False


def test_seatbelt_profile_allows_temp_paths() -> None:
    """seatbelt 没有私有 tmpfs，不放行真实临时目录的话命令写临时文件会莫名失败。"""
    profile = posix_sandbox.build_seatbelt_profile(["/work"])
    import tempfile

    assert tempfile.gettempdir() in profile or "/private/var" in profile or "(subpath \"/tmp\")" in profile

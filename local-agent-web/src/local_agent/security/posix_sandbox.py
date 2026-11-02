"""Linux / macOS 的写侧沙箱（2026-11-02）。

**状态：参数构造已实现并有单测，未在真机上跑过**（开发机只有 Windows）。真机边界由 CI 的
`sandbox-smoke` 在 ubuntu / macos 上用真实写探针验（根内可写、根外被拒）。

三条路线的分工（参照 codex 的选择）：

- **Linux + bubblewrap**（首选）：`--ro-bind / /` 建只读全景，再对可写根逐个 `--bind`
  （后者覆盖前者），`--unshare-pid` 隔离进程树，`--tmpfs /tmp` 给私有临时目录，
  `--die-with-parent` 保证 runner 退出时子进程一起走。它对"文件系统长什么样"也有控制权，
  比纯粹的访问控制更接近"换了个环境"。
- **Linux + landlock**（兜底）：Ubuntu 24.04 起默认限制**非特权 user namespace**，bwrap 会以
  "No permissions to create new namespace" 失败。landlock 是内核级路径访问控制（5.13+），
  不需要 namespace、不需要 root，只限制写类操作（读与执行不受影响）——见 `landlock` 模块。
  局限要说清：它只能表达"允许/禁止"，不能伪造挂载视图，也挡不住低 ABI 上未登记的
  `REFER`（跨目录 rename）与 `TRUNCATE`。
- **macOS + seatbelt**：`sandbox-exec` + profile。profile 拆成"默认放行 → 全局禁写 →
  逐路径放行"三段（`_MACOS_*` 常量 + `build_seatbelt_profile` 组合），而不是一个巨型字符串：
  seatbelt 的报错几乎没有信息，能一段段二分是唯一可靠的调试手段。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from local_agent.security import landlock

#: Linux 上用 `--ro-bind` 铺底的全景根。写死的 `/`：沙箱要的是"整台机器的只读视图"，
#: 让用户配一个别的根只会造出"看起来隔离了、其实只是换了个目录"的假象。
_READ_ONLY_ROOT = "/"

#: 沙箱内可写的临时目录（bwrap 的 `--tmpfs`）：每条命令一个私有 tmpfs，退出即消失。
_SANDBOX_TMP = "/tmp"

#: macOS 上必须显式放行的设备节点：`(deny file-write*)` 会把它们一起挡掉，
#: 而任何正常命令都会写 stdout/stderr 与 /dev/null。
_MACOS_WRITE_LITERALS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/dtracehelper")

# ---- macOS seatbelt profile 的三块零件（分开是为了能各自定位）----------------
_MACOS_HEADER = "(version 1)"
#: 第一块：默认全放行。整机隔离不是这一层的目标——这一层只画"写"的边界。
_MACOS_ALLOW_DEFAULT = "(allow default)"
#: 第二块：先全局禁写。**必须排在放行之前**：seatbelt 后写的规则覆盖先写的，反序等于全禁。
_MACOS_DENY_WRITES = "(deny file-write*)"


def platform_kind() -> str | None:
    """``"linux"`` / ``"macos"``；其它平台返回 ``None``（由 Windows 实现或降级路径接手）。"""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return None


def bwrap_path() -> str | None:
    """bwrap 的绝对路径；没装就 ``None``。"""
    return shutil.which("bwrap")


def unavailable_reason() -> str | None:
    """当前平台**能不能**给出 OS 级写边界；不能时返回原因（把可用的替代手段也写进去）。"""
    kind = platform_kind()
    if kind is None:
        return "本平台没有 POSIX 沙箱实现（Windows 走受限令牌，见 win_sandbox）"
    if kind == "linux":
        if bwrap_path() is not None or landlock.available():
            return None
        return ("bwrap 与 landlock 都不可用：安装 bubblewrap（apt install bubblewrap），"
                "或换用 Linux >= 5.13 的内核（landlock 需要它）。"
                "Ubuntu 24.04 默认限制非特权 user namespace，bwrap 会报 "
                "'No permissions to create new namespace'——那种情况下用 landlock 兜底")
    if shutil.which("sandbox-exec") is None:
        return "没有找到 sandbox-exec（macOS 自带的 seatbelt 执行器）"
    return None


def available() -> bool:
    return unavailable_reason() is None


def backend() -> str | None:
    """当前会走哪条路：``bwrap`` / ``landlock`` / ``seatbelt`` / ``None``。

    暴露出来是为了让冒烟脚本与错误文案能说"实际用了哪个"，而不是笼统的"有沙箱"。
    """
    kind = platform_kind()
    if kind == "linux":
        if bwrap_path() is not None:
            return "bwrap"
        return "landlock" if landlock.available() else None
    if kind == "macos":
        return "seatbelt" if shutil.which("sandbox-exec") is not None else None
    return None


def _absolute(paths: Sequence[str | Path]) -> list[str]:
    """可写根必须是绝对路径：bwrap 的 `--bind` 用相对路径会相对**沙箱内**的 cwd，
    那是"授权了工作区、实际绑到别处"的经典错法。"""
    result: list[str] = []
    for item in paths:
        text = str(item).strip()
        # 判定按**目标平台**的语义，而不是按当前宿主的：在 Windows 上拼 Linux 沙箱的 argv
        # （单测、交叉构建）时 `Path("/work").is_absolute()` 是假，不能因此判它不合法。
        if text.startswith("/") or Path(text).expanduser().is_absolute():
            result.append(text)
            continue
        raise ValueError(f"沙箱可写根必须是绝对路径：{item}")
    return result


def build_bwrap_argv(
    command: Sequence[str],
    *,
    writable_roots: Sequence[str | Path] = (),
    cwd: str | Path | None = None,
) -> list[str]:
    """把 `command` 包成"在 bwrap 里跑"的 argv（纯函数，便于在任意平台单测）。"""
    executable = bwrap_path() or "bwrap"
    roots = _absolute(writable_roots)
    argv = [
        executable,
        "--new-session",
        # runner 一退出，沙箱内所有进程跟着走：否则取消/超时后留一堆孤儿。
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        # 只读全景打底，再按可写根逐个覆盖（bwrap 里后写的 bind 覆盖前面的同路径）。
        "--ro-bind", _READ_ONLY_ROOT, _READ_ONLY_ROOT,
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", _SANDBOX_TMP,
        # 只读档没有任何可写根时，`/tmp` 仍是 tmpfs——命令要写临时文件才不会成片失败。
        "--setenv", "TMPDIR", _SANDBOX_TMP,
    ]
    for root in dict.fromkeys(roots):
        argv.extend(["--bind", root, root])
    if cwd is not None:
        argv.extend(["--chdir", _absolute([cwd])[0]])
    argv.extend(command)
    return argv


def build_landlock_argv(
    command: Sequence[str], *, writable_roots: Sequence[str | Path] = (),
) -> list[str]:
    """把 `command` 包成"先施加 landlock、再 exec"的形式。

    用 `-I`（隔离模式）启动：不加它，工作区里的同名包会抢在 `local_agent` 前面被导入——
    那等于让被沙箱约束的对象自己决定沙箱代码。
    """
    roots = _absolute(writable_roots)
    argv = [sys.executable, "-I", "-m", "local_agent.security.landlock_exec"]
    for root in dict.fromkeys(roots):
        argv.extend(["--writable", root])
    argv.append("--")
    argv.extend(command)
    return argv


def _macos_tmp_subpaths() -> list[str]:
    """系统临时目录（含 realpath）。

    macOS 上 `tempfile.gettempdir()` 是 `/var/folders/…`，realpath 是 `/private/var/folders/…`
    —— seatbelt 匹配**真实路径**，两者都得给，否则命令写临时文件会以 "Operation not permitted"
    失败，而报错完全指不出是这里的问题。
    """
    tmp = tempfile.gettempdir()
    paths = [tmp, str(Path(tmp).resolve())]
    return [path for path in dict.fromkeys(paths) if path.startswith("/")]


def build_seatbelt_profile(writable_roots: Sequence[str | Path] = ()) -> str:
    """macOS seatbelt profile：三块零件按"默认放行 → 全局禁写 → 逐路径放行"组合。"""
    roots = _absolute(writable_roots)
    allow_lines = [
        # 可写根：整棵子树放行（与 bwrap 的 `--bind` 同一语义）。
        *(f'  (subpath "{root}")' for root in dict.fromkeys(roots)),
        f'  (subpath "{_SANDBOX_TMP}")',
        # bwrap 那条路有私有 tmpfs；seatbelt 没有，只能放行真实临时目录。
        *(f'  (subpath "{path}")' for path in _macos_tmp_subpaths()),
        # 设备节点必须逐条放行：连 stdout 都写不了的话，命令会以"无输出"的形式失败。
        *(f'  (literal "{path}")' for path in _MACOS_WRITE_LITERALS),
    ]
    return "\n".join([
        _MACOS_HEADER,
        _MACOS_ALLOW_DEFAULT,
        _MACOS_DENY_WRITES,
        "(allow file-write*",
        *allow_lines,
        ")",
    ]) + "\n"


def build_seatbelt_argv(
    command: Sequence[str], *, writable_roots: Sequence[str | Path] = (),
) -> list[str]:
    """把 `command` 包成"在 sandbox-exec 里跑"的 argv（纯函数）。"""
    executable = shutil.which("sandbox-exec") or "sandbox-exec"
    return [executable, "-p", build_seatbelt_profile(writable_roots), *command]


def wrap_argv(
    command: Sequence[str],
    *,
    writable_roots: Sequence[str | Path] = (),
    cwd: str | Path | None = None,
) -> list[str]:
    """按当前平台把命令包进沙箱。

    Raises:
        RuntimeError: 当前平台没有可用实现。**调用方必须 fail-closed**——静默裸跑等于
            用户以为有边界而实际没有。
    """
    kind = platform_kind()
    if kind == "linux":
        if bwrap_path() is not None:
            return build_bwrap_argv(command, writable_roots=writable_roots, cwd=cwd)
        if landlock.available():
            # landlock 不换文件系统视图，所以 cwd 不需要额外处理：命令就在原目录里跑。
            return build_landlock_argv(command, writable_roots=writable_roots)
    if kind == "macos" and shutil.which("sandbox-exec") is not None:
        return build_seatbelt_argv(command, writable_roots=writable_roots)
    raise RuntimeError(unavailable_reason() or "当前平台没有 POSIX 沙箱实现")

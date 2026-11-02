"""Linux / macOS 的写侧沙箱（2026-11-02）。

**状态：已实现参数构造，未在真机上跑过**（开发机只有 Windows）。所以这个模块的定位与
`win_sandbox` 不同：Windows 那份是"在本机验过的边界"，这份是"按 API 文档拼出来的 argv"，
真机验证要等 CI 的 `sandbox-smoke`（`.github/workflows/ci.yml`）在 ubuntu / macos 上跑通。

两条路线的分工（参照 codex 的选择）：

- **Linux**：bubblewrap。`--ro-bind / /` 建只读全景，再对可写根逐个 `--bind`（后者覆盖前者），
  `--unshare-pid` 隔离进程树，`--tmpfs /tmp` 给一个可写的私有临时目录，`--die-with-parent`
  保证 runner 退出时子进程一起走。为什么不用 landlock 打头：landlock 只能表达"允许/禁止什么"，
  而"命令看到的文件系统长什么样"（挂载点、/tmp、/proc）也得管；bwrap 一次把两件事都做了。
  landlock 留给"机器上没有 bwrap 或不允许非特权 userns"的降级路径（后续）。
- **macOS**：`sandbox-exec` + seatbelt profile。profile 拆成"默认放行 → 全局禁写 → 逐路径放行"
  三段，而不是一个巨型字符串——出错时能一段段二分（seatbelt 的报错几乎没有信息，这是唯一
  可靠的调试办法）。

**已知失败形态**（`unavailable_reason` 会如实说出来，不要静默裸跑）：

- Linux 没装 bwrap；
- Ubuntu 24.04 起默认限制非特权 user namespace（AppArmor），bwrap 会以 "No permissions to
  create new namespace" 失败——那台机器需要 `--cap-add` / 内核参数或改用 landlock；
- macOS 的 `sandbox-exec` 已被标记 deprecated（但仍可用，Chromium 与参照实现都在用）。
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

#: Linux 上用 `--ro-bind` 铺底的全景根。写死的 `/`：沙箱要的是"整台机器的只读视图"，
#: 让用户配一个别的根只会造出"看起来隔离了、其实只是换了个目录"的假象。
_READ_ONLY_ROOT = "/"

#: 沙箱内可写的临时目录（`--tmpfs`）：每条命令一个私有 tmpfs，退出即消失。
_SANDBOX_TMP = "/tmp"

#: macOS 上需要显式放行的设备节点：`(deny file-write*)` 会把它们一起挡掉，
#: 而任何正常命令都会写 stdout/stderr 与 /dev/null。
_MACOS_WRITE_LITERALS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/dtracehelper")


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
    """当前平台**能不能**给出 OS 级写边界；不能时返回原因。"""
    kind = platform_kind()
    if kind is None:
        return "本平台没有 POSIX 沙箱实现（Windows 走受限令牌，见 win_sandbox）"
    if kind == "linux" and bwrap_path() is None:
        return ("没有找到 bwrap（bubblewrap）：请安装（Debian/Ubuntu: apt install bubblewrap；"
                "Fedora: dnf install bubblewrap）。Ubuntu 24.04 还需要放开非特权 user namespace，"
                "否则 bwrap 会以 'No permissions to create new namespace' 失败")
    if kind == "macos" and shutil.which("sandbox-exec") is None:
        return "没有找到 sandbox-exec（macOS 自带的 seatbelt 执行器）"
    return None


def available() -> bool:
    return unavailable_reason() is None


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


def build_seatbelt_profile(writable_roots: Sequence[str | Path] = ()) -> str:
    """macOS seatbelt profile：默认放行 → 全局禁写 → 逐路径放行。

    **顺序不能反过来**：seatbelt 是"后写的规则覆盖先写的"，先 allow 再 deny 等于全禁。
    """
    roots = _absolute(writable_roots)
    lines = [
        "(version 1)",
        "(allow default)",
        # 只挡写：读、执行、网络都放行——这一层的语义是"写边界"，不是整机隔离。
        "(deny file-write*)",
        "(allow file-write*",
        *[f'  (subpath "{root}")' for root in dict.fromkeys(roots)],
        *[f'  (literal "{path}")' for path in _MACOS_WRITE_LITERALS],
        f'  (subpath "{_SANDBOX_TMP}")',
        ")",
    ]
    return "\n".join(lines) + "\n"


def build_seatbelt_argv(
    command: Sequence[str],
    *,
    writable_roots: Sequence[str | Path] = (),
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
        return build_bwrap_argv(command, writable_roots=writable_roots, cwd=cwd)
    if kind == "macos":
        return build_seatbelt_argv(command, writable_roots=writable_roots)
    raise RuntimeError(unavailable_reason() or "当前平台没有 POSIX 沙箱实现")

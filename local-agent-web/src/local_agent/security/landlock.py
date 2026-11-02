"""Linux landlock：给"写边界"提供不依赖 user namespace 的实现（2026-11-02）。

为什么需要它：bwrap 要求创建 mount namespace，而 Ubuntu 24.04 起默认限制**非特权 user namespace**
（AppArmor），`bwrap` 会以 "No permissions to create new namespace" 失败。landlock 是内核级的
**路径访问控制**（5.13+），不需要 namespace、不需要 root，代价是它只能管"允许/禁止哪些路径上的
哪些操作"——不能像 bwrap 那样伪造文件系统视图（挂载点、/tmp）。对我们够用：我们要表达的正是
"写入只许发生在这些根下面"。

**只处理写位**（这是关键取舍）：`handled_access_fs` 里不登记 `READ_FILE` / `READ_DIR` / `EXECUTE`，
于是读与执行完全不受影响，只有写类操作被限制。这样"档位说可写、其他都能读"的既有语义一字不差。

**已在真机上验过吗**：没有（开发机是 Windows）。参数与掩码有单测，真实边界靠 CI 的
`sandbox-smoke` 在 ubuntu 上跑真实写探针。这也意味着 `unavailable_reason()` 必须把所有
"这台机器没有 landlock"的情况说清楚，而不是让调用方去猜。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: syscall 号（x86_64 / aarch64 的通用表里这三个号是相同的）。
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446

#: `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` 的 flag。
_CREATE_RULESET_VERSION = 1
#: `landlock_add_rule` 的规则类型：只支持 PATH_BENEATH。
_RULE_PATH_BENEATH = 1

#: `prctl(PR_SET_NO_NEW_PRIVS)`：landlock 的前置条件（不允许 setuid 提权绕过）。
_PR_SET_NO_NEW_PRIVS = 38

# ---- 文件系统访问位（按 ABI 版本逐步增加）-----------------------------------
_ACCESS_WRITE_FILE = 1 << 1
_ACCESS_REMOVE_DIR = 1 << 4
_ACCESS_REMOVE_FILE = 1 << 5
_ACCESS_MAKE_CHAR = 1 << 6
_ACCESS_MAKE_DIR = 1 << 7
_ACCESS_MAKE_REG = 1 << 8
_ACCESS_MAKE_SOCK = 1 << 9
_ACCESS_MAKE_FIFO = 1 << 10
_ACCESS_MAKE_BLOCK = 1 << 11
_ACCESS_MAKE_SYM = 1 << 12
#: ABI v2（Linux 5.19+）：跨目录 rename/link。
_ACCESS_REFER = 1 << 13
#: ABI v3（Linux 6.2+）：`truncate(2)` / `ftruncate(2)`。
_ACCESS_TRUNCATE = 1 << 14

#: 只读档也**必须**放行的设备节点：任何正常命令都会写 stdout/stderr 与 /dev/null。
_DEVICE_LITERALS = ("/dev/null", "/dev/zero", "/dev/full", "/dev/tty", "/dev/stdout", "/dev/stderr")


class LandlockUnavailable(RuntimeError):
    """内核不支持 landlock，或运行时取不到必需的系统调用。**调用方必须 fail-closed 或明说降级。**"""


@dataclass(frozen=True)
class _RulesetAttr(ctypes.Structure):
    handled_access_fs: ctypes.c_uint64


class _PathBeneath(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]


def _libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c") or "libc.so.6"
    try:
        return ctypes.CDLL(name, use_errno=True)
    except OSError as exc:  # pragma: no cover - 只在异常 Linux 环境触发
        raise LandlockUnavailable(f"取不到 libc：{exc}") from exc


def write_access_mask(abi: int) -> int:
    """按内核支持的 ABI 版本给出"我们打算限制的写位"。

    只包含写类：读与执行不登记，因此不受影响。ABI 低的机器缺 `REFER`/`TRUNCATE` 位——
    那两个位没登记就意味着"跨目录 rename"与"truncate"不受限制，这一点必须在文档里如实写明，
    不能假装边界等价。
    """
    mask = (
        _ACCESS_WRITE_FILE
        | _ACCESS_REMOVE_DIR | _ACCESS_REMOVE_FILE
        | _ACCESS_MAKE_CHAR | _ACCESS_MAKE_DIR | _ACCESS_MAKE_REG
        | _ACCESS_MAKE_SOCK | _ACCESS_MAKE_FIFO | _ACCESS_MAKE_BLOCK | _ACCESS_MAKE_SYM
    )
    if abi >= 2:
        mask |= _ACCESS_REFER
    if abi >= 3:
        mask |= _ACCESS_TRUNCATE
    return mask


def abi_version() -> int | None:
    """内核 landlock ABI 版本；不支持时 ``None``（Linux < 5.13 或未启用）。"""
    if sys.platform != "linux":
        return None
    try:
        libc = _libc()
        result = libc.syscall(
            _SYS_LANDLOCK_CREATE_RULESET, ctypes.c_void_p(0), ctypes.c_size_t(0),
            ctypes.c_uint(_CREATE_RULESET_VERSION),
        )
    except (LandlockUnavailable, OSError, AttributeError):
        return None
    return int(result) if result > 0 else None


def unavailable_reason() -> str | None:
    """这台机器能不能用 landlock；不能时返回原因。"""
    if sys.platform != "linux":
        return "landlock 只在 Linux 上存在"
    abi = abi_version()
    if abi is None:
        return ("内核没有 landlock（需要 Linux >= 5.13 且启动时未禁用）："
                "要么升级内核，要么安装 bwrap")
    return None


def available() -> bool:
    return unavailable_reason() is None


#: Linux 专有常量：这个模块只会在 Linux 上真正执行（`unavailable_reason` 先拦住其它平台），
#: 但 mypy 在 Windows 上取不到 `os.O_PATH`，所以用 getattr 兜底而不是硬写。
_O_PATH = getattr(os, "O_PATH", 0o10000000)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0o2000000)


def _open_ro_dir(path: str) -> int:
    # 只要一个指向该路径的 fd，不需要读权限（所以用 O_PATH 而不是 O_RDONLY）。
    return os.open(path, _O_PATH | _O_CLOEXEC)


def apply(writable_roots: Sequence[str | Path] = ()) -> int:
    """在**当前进程**上施加写边界；返回使用的 ABI 版本。

    必须在 `exec` 之前、在子进程里调用（landlock 一旦施加**不可撤销**，而且只影响调用进程及其后代）。
    这也是为什么 Linux 上不能像 Windows 那样"父进程造令牌、子进程继承"——见 `landlock_exec`。

    Raises:
        LandlockUnavailable: 内核不支持，或规则施加失败。**不要在这里静默继续**：
            调用点如果决定降级，必须由它自己把原因说出来。
    """
    abi = abi_version()
    if abi is None:
        raise LandlockUnavailable(unavailable_reason() or "landlock 不可用")

    libc = _libc()
    mask = write_access_mask(abi)
    attr = _RulesetAttr(handled_access_fs=ctypes.c_uint64(mask))
    ruleset_fd = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint(0),
    )
    if ruleset_fd < 0:
        raise LandlockUnavailable(f"landlock_create_ruleset 失败：errno={ctypes.get_errno()}")

    try:
        targets: list[str] = []
        for root in writable_roots:
            text = str(root)
            if not text.startswith("/"):
                raise LandlockUnavailable(f"可写根必须是绝对路径：{text}")
            targets.append(text)
        # 设备节点与临时目录：不给它们，任何命令都会在写 stdout / 临时文件时莫名其妙失败。
        targets.extend(_DEVICE_LITERALS)
        targets.append("/tmp")
        for target in dict.fromkeys(targets):
            try:
                fd = _open_ro_dir(target)
            except OSError:
                # 不存在的设备节点（例如容器里没有 /dev/full）跳过；存在的写不了才是问题。
                continue
            try:
                rule = _PathBeneath(allowed_access=ctypes.c_uint64(mask), parent_fd=fd)
                if libc.syscall(_SYS_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset_fd),
                                ctypes.c_int(_RULE_PATH_BENEATH), ctypes.byref(rule),
                                ctypes.c_uint(0)) < 0:
                    raise LandlockUnavailable(
                        f"landlock_add_rule({target}) 失败：errno={ctypes.get_errno()}"
                    )
            finally:
                os.close(fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            raise LandlockUnavailable(f"PR_SET_NO_NEW_PRIVS 失败：errno={ctypes.get_errno()}")
        if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset_fd),
                        ctypes.c_uint(0)) < 0:
            raise LandlockUnavailable(f"landlock_restrict_self 失败：errno={ctypes.get_errno()}")
    finally:
        os.close(ruleset_fd)
    return abi

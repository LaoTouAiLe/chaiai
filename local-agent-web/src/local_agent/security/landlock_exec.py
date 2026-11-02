"""施加 landlock 写边界后 `exec` 目标命令（2026-11-02）。

为什么要一个"自调用"的入口，而不是在父进程里 preexec：

- landlock 一旦 `restrict_self` 就**不可撤销**，只影响调用进程及其后代；
- 用 `subprocess(preexec_fn=...)` 也能做，但 preexec 里只允许 async-signal-safe 的操作，
  而施加规则要开 fd、跑 syscall、还要处理错误——一旦抛异常，表现是"子进程莫名失败"，
  错误信息全丢。做成 `python -I -m ...` 的入口后，失败原因能原样打到 stderr，退出码也清楚。

用法（由 :func:`local_agent.security.posix_sandbox.build_landlock_argv` 拼出来）::

    python -I -m local_agent.security.landlock_exec \
        --writable /path/to/workspace [--writable ...] -- command arg...

边界施加失败时**不执行命令**并返回非零：静默裸跑等于"用户以为有边界、实际没有"。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from local_agent.security import landlock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="landlock_exec",
        description="施加 landlock 写边界后执行命令（写入只允许发生在给定根、/tmp 与设备节点）",
    )
    parser.add_argument("--writable", action="append", default=[], metavar="PATH",
                        help="可写根（可重复）；不给表示整条命令一个可写位置都没有")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="`--` 之后是要执行的命令")
    args = parser.parse_args(list(argv) if argv is not None else None)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("landlock_exec: 没有给出要执行的命令", file=sys.stderr)
        return 2

    try:
        abi = landlock.apply(args.writable)
    except landlock.LandlockUnavailable as exc:
        # fail-closed：不执行命令。调用方（run_shell）会把 stderr 与退出码一起如实报给模型。
        print(f"landlock_exec: 无法施加写边界，命令未执行：{exc}", file=sys.stderr)
        return 126
    print(f"landlock_exec: 已施加写边界（ABI v{abi}，可写根 {len(args.writable)} 个）",
          file=sys.stderr)

    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        print(f"landlock_exec: 无法执行 {command[0]!r}：{exc}", file=sys.stderr)
        return 127
    return 0  # pragma: no cover - exec 成功时不会走到这里


if __name__ == "__main__":
    raise SystemExit(main())

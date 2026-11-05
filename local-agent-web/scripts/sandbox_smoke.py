"""沙箱冒烟：**在真实机器上**确认"写边界到底有没有物化出来"。

为什么单开一个脚本而不是放在 `tests/`：沙箱装错了，单元测试往往是绿的——它们测的是"我们打算
怎么做"，而这个脚本测的是"这台机器的内核/系统真的拦不拦"。参照实现（codex）同样把这类检查做成
独立脚本（`windows-sandbox-rs/sandbox_smoketests.py`），因为它的失败形态是"环境变了"，不是"代码变了"。

用法::

    python scripts/sandbox_smoke.py            # 当前平台能验就验，不能验就说明原因并以 0 退出
    python scripts/sandbox_smoke.py --require  # 不能验也算失败（CI 里想强制时用）

判定口径：

- **Windows**：`win_sandbox.is_supported()` 为真，且只读档的沙箱请求**没有**任何可写 SID、
  工作区可写档**至少有一个**。这两条覆盖了"受限令牌没建出来"与"授权没物化"两种最常见的错法。
- **Linux / macOS**：目前还没有实现（只有降级路径），这里**如实说明并以 0 退出**；加 `--require`
  则变成失败。等 bwrap / seatbelt 落地后，这里换成真正的写探针（照 Windows 那两条的形状写）。
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_agent.config import AppConfig
from local_agent.security import win_sandbox
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import (
    SecurityPolicy,
    sandbox_boundary_note,
)


def _policy(root: Path, mode: str) -> SecurityPolicy:
    return SecurityPolicy.from_config(
        AppConfig.model_validate({"workspace": {"root": str(root)}}),
        workspace_root=root,
        environ={},
        external_policy=ExternalPathPolicy(mode=mode),  # type: ignore[arg-type]
    )


def check_windows(root: Path) -> list[str]:
    """返回失败原因列表；空列表 = 通过。"""
    problems: list[str] = []
    if not win_sandbox.is_supported():
        return ["win_sandbox.is_supported() 为假：受限令牌建不出来（沙箱整体不可用）"]

    read_only = _policy(root, "read-only").shell_sandbox_request()
    if read_only is None:
        problems.append("只读档没有产出沙箱请求（应当有：只读档靠无可写 SID 的令牌限制写入）")
    elif read_only.writable_sids:
        problems.append(f"只读档带上了 {len(read_only.writable_sids)} 个可写 SID（应当为 0）")

    writable = _policy(root, "workspace-write").shell_sandbox_request()
    if writable is None:
        problems.append("工作区可写档没有产出沙箱请求")
    elif not writable.writable_sids:
        problems.append("工作区可写档没有任何可写 SID（工作区写入会全被拒）")
    # 边界说明必须与平台事实一致：有边界时不该出现那句告警。
    if sandbox_boundary_note() is not None:
        problems.append("平台有沙箱，却仍给出了\"没有边界\"的说明")
    return problems


def _bwrap_userns_error() -> str | None:
    """这个 runner 能不能起 bwrap 的 user namespace；不能时返回原因。

    为什么单独探一下（2026-11-05 CI 实测）：GitHub 的 ubuntu runner 跑在容器里，
    `bwrap --unshare-user` 会以 "setting up uid map: Permission denied" 失败。那是
    **环境不允许**，不是实现坏了——同一份实现换一台普通 Linux 就能过。把它报成
    "沙箱外写入没被拒绝"会指向完全错误的方向。
    """
    from local_agent.security import posix_sandbox

    executable = posix_sandbox.bwrap_path()
    if executable is None:
        return "bwrap 不在 PATH"
    try:
        probe = subprocess.run(
            [executable, "--unshare-user", "--dev-bind", "/", "/", "true"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"探测失败：{exc}"
    if probe.returncode == 0:
        return None
    detail = (probe.stderr or probe.stdout or "").strip().splitlines()
    return detail[0] if detail else f"bwrap 探测退出码 {probe.returncode}"


def check_posix(root: Path) -> list[str]:
    """Linux / macOS 的真实写探针：可写根内能写、根外写不进去。

    两条都要验：只验前者会漏掉"根本没隔离"，只验后者会漏掉"隔离过头、工作区也写不了"。
    """
    from local_agent.security import posix_sandbox

    problems: list[str] = []
    inside = root / ".sandbox-probe"
    inside.mkdir(exist_ok=True)
    # 探针位置必须落在**两条后端放行列表之外**。bwrap 只 bind 可写根；seatbelt 没有私有
    # tmpfs，额外放行了真实 TMPDIR——而 `tempfile` 在 macOS 上正好就落在 TMPDIR 里，
    # 于是"根外写入"永远是允许的，探针测不到边界（2026-11-05 CI 实测）。改到 $HOME 下。
    outside = Path(tempfile.mkdtemp(prefix=".chaiai-sandbox-outside-", dir=str(Path.home())))

    def run(script: str) -> subprocess.CompletedProcess[str]:
        argv = posix_sandbox.wrap_argv(
            ["sh", "-c", script], writable_roots=[str(inside)], cwd=str(inside),
        )
        # `check=False`：非零退出是**预期结果**之一（根外写入必须失败），由调用点判定。
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)

    try:
        allowed = run(f"echo ok > {inside}/probe.txt")
        if allowed.returncode != 0 or not (inside / "probe.txt").exists():
            problems.append(
                f"可写根内写入失败（rc={allowed.returncode}）：{allowed.stderr.strip()[:300]}"
            )
        denied = run(f"echo no > {outside}/nope.txt")
        if denied.returncode == 0 or (outside / "nope.txt").exists():
            problems.append(f"沙箱外的写入**没有被拒绝**（{outside}）：边界没有生效")
    finally:
        shutil.rmtree(outside, ignore_errors=True)
        (inside / "probe.txt").unlink(missing_ok=True)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="沙箱冒烟检查")
    parser.add_argument("--require", action="store_true", help="本平台验不了时也算失败")
    args = parser.parse_args()

    # CI 的 Windows runner 是 cp1252 控制台：打印中文会 UnicodeEncodeError（2026-11-05 实测：
    # 沙箱其实已经通过了，却因为下面那句"OK …"崩掉，看起来像冒烟失败）。显式转 UTF-8，
    # 比依赖 workflow 里的 PYTHONIOENCODING 更可靠——本地 cp936 控制台同样受益。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    system = platform.system()
    root = Path.cwd()
    print(f"[sandbox] platform={system} python={platform.python_version()} cwd={root}")

    if system == "Windows":
        problems = check_windows(root)
        if problems:
            for item in problems:
                print(f"  FAIL {item}")
            return 1
        print("  OK 只读档无可写 SID；工作区可写档有可写 SID；边界说明与平台一致")
        return 0

    # 非 Windows：bwrap / seatbelt 可用就做真实写探针，不可用则如实说明。
    from local_agent.security import posix_sandbox

    if posix_sandbox.available():
        # 说清走的是哪条路：bwrap（换视图）与 landlock（只挡写）能力不同，报错时差很多。
        backend = posix_sandbox.backend()
        print(f"  backend: {backend}")
        if backend == "bwrap":
            userns_error = _bwrap_userns_error()
            if userns_error is not None:
                print(f"  SKIP 这个环境起不了 bwrap 的 user namespace（{userns_error}）。")
                print("       GitHub 的容器化 runner 常禁用 unprivileged userns；")
                print("       这不是实现问题，同样的代码在普通 Linux 上可验。")
                return 1 if args.require else 0
        problems = check_posix(root)
        if problems:
            for item in problems:
                print(f"  FAIL {item}")
            return 1
        print("  OK 可写根内可写、根外写入被拒绝")
        return 0

    reason = posix_sandbox.unavailable_reason()
    print(f"  SKIP 该平台没有可用的 OS 级写边界（{system}）：{reason}")
    print(f"  note: {sandbox_boundary_note()}")
    if args.require:
        print("  FAIL --require 指定了必须验，但该平台给不出边界")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

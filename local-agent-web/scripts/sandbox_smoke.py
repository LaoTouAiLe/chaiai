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
import sys
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


def check_posix(root: Path) -> list[str]:
    """Linux / macOS 的真实写探针：可写根内能写、根外写不进去。

    两条都要验：只验前者会漏掉"根本没隔离"，只验后者会漏掉"隔离过头、工作区也写不了"。
    """
    import subprocess
    import tempfile

    from local_agent.security import posix_sandbox

    problems: list[str] = []
    inside = root / ".sandbox-probe"
    inside.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as outside:
        def run(script: str) -> subprocess.CompletedProcess[str]:
            argv = posix_sandbox.wrap_argv(
                ["sh", "-c", script], writable_roots=[str(inside)], cwd=str(inside),
            )
            # `check=False`：非零退出是**预期结果**之一（根外写入必须失败），由调用点判定。
            return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)

        allowed = run(f"echo ok > {inside}/probe.txt")
        if allowed.returncode != 0 or not (inside / "probe.txt").exists():
            problems.append(
                f"可写根内写入失败（rc={allowed.returncode}）：{allowed.stderr.strip()[:300]}"
            )
        denied = run(f"echo no > {outside}/nope.txt")
        if denied.returncode == 0 or (Path(outside) / "nope.txt").exists():
            problems.append("沙箱外的写入**没有被拒绝**：边界没有生效")
        (inside / "probe.txt").unlink(missing_ok=True)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="沙箱冒烟检查")
    parser.add_argument("--require", action="store_true", help="本平台验不了时也算失败")
    args = parser.parse_args()

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

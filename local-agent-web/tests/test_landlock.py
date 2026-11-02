"""landlock 兜底路径的**掩码与降级行为**（2026-11-02）。

这些用例在 Windows 上也跑：它们验证"打算限制哪些位、限制不了时怎么说"。
真正的边界是否生效由 CI 的 `sandbox-smoke` 在 ubuntu 上验（根内可写 / 根外被拒）。

为什么值得单测掩码：landlock 的错法是**静默**的——多登记一个读位就是"整个工作区读不了"，
少登记一个写位就是"某个写操作没被拦"，两者都不会抛异常，只会表现为"命令行为怪怪的"。
"""

from __future__ import annotations

import sys

import pytest

from local_agent.security import landlock, landlock_exec


def test_mask_covers_write_bits_only() -> None:
    """只登记写类：读与执行必须不受影响（否则"档位说可读"就成了空话）。"""
    mask = landlock.write_access_mask(3)

    read_file = 1 << 2
    read_dir = 1 << 3
    execute = 1 << 0
    assert mask & read_file == 0
    assert mask & read_dir == 0
    assert mask & execute == 0
    # 写类都在。
    for bit in (1 << 1, 1 << 4, 1 << 5, 1 << 7, 1 << 8, 1 << 12, 1 << 13, 1 << 14):
        assert mask & bit, bit


def test_mask_grows_with_abi() -> None:
    """低 ABI 缺 REFER / TRUNCATE：这一点必须体现在掩码上，而不是"看起来一样"。"""
    v1, v2, v3 = landlock.write_access_mask(1), landlock.write_access_mask(2), landlock.write_access_mask(3)

    assert v1 & (1 << 13) == 0 and v1 & (1 << 14) == 0
    assert v2 & (1 << 13) and v2 & (1 << 14) == 0
    assert v3 & (1 << 14)


@pytest.mark.skipif(sys.platform == "linux", reason="这条断言的是非 Linux 的降级路径")
def test_non_linux_is_unavailable_and_says_why() -> None:
    assert landlock.abi_version() is None
    reason = landlock.unavailable_reason()
    assert reason is not None and "Linux" in reason
    assert landlock.available() is False


def test_apply_fails_closed_when_unsupported() -> None:
    """施加失败必须抛错：调用点据此**不执行命令**，而不是裸跑。"""
    if sys.platform == "linux":
        pytest.skip("Linux 上 landlock 可能可用，这条针对不可用平台")

    with pytest.raises(landlock.LandlockUnavailable):
        landlock.apply(["/tmp"])


def test_exec_helper_refuses_when_boundary_cannot_be_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """边界施加失败时返回 126 且**不 exec**——这是 fail-closed 的落点。"""
    def boom(_roots: object = ()) -> int:
        raise landlock.LandlockUnavailable("内核没有 landlock")

    monkeypatch.setattr(landlock, "apply", boom)
    assert landlock_exec.main(["--", "true"]) == 126


def test_exec_helper_requires_a_command() -> None:
    assert landlock_exec.main(["--writable", "/tmp", "--"]) == 2

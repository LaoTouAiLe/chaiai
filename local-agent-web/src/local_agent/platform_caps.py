"""平台能力判断（2026-11-05 从 `security.policy` 下沉）。

**为什么下沉**：`application/models.py` 是纯数据契约（前端类型与 OpenAPI schema 都从它生成），
却要回答"这台机器有没有 OS 级写边界"。它原先直接 ``from local_agent.security.policy import
sandbox_boundary_note``，于是任何人 import 数据模型，就顺带把 ctypes、Win32 DLL 和整个安全层
加载进来——§15.1 的分层图因此破了一边（`test_events` 的分层守卫抓到的就是这一条）。

这里只依赖 ``sys`` 与 ``ctypes``，不引用 security / graph / persistence / ui / api 任何一层，
所以契约层可以放心引用；``security.policy`` 反过来从这里取，两边共用同一份判断与同一句文案，
不会各写一遍后互相漂移。
"""

from __future__ import annotations

import ctypes
import sys

#: 这句话此前**只存在于一个从未被调用的方法里**（`SecurityPolicy.sandbox_boundary_note`），于是
#: 非 Windows 上"命令照常跑、但没有内核强制边界"这件事是静默的——用户不知道，模型也不知道。现在改成
#: 模块级常量：策略、权限视图、系统提示三处共用同一句，避免各写一遍后互相漂移。
SANDBOX_BOUNDARY_NOTE = (
    "当前平台没有 OS 级写边界（写侧沙箱目前只在 Windows 上实现）：路径工具受档位策略限制，"
    "但命令内部的文件访问不由路径工具约束，**不是内核强制**。"
    "要求必须在内核级边界内执行时，把 `security.require_sandbox` 设为 true——那样命令会被直接拒绝，"
    "而不是在看起来有边界的情况下运行。"
)


def _win32_libraries_loadable() -> bool:
    """两块 Win32 库能不能取到。

    与 ``win_sandbox.is_supported()`` 的两条判据同义，但**不 import 它**：契约层不能碰 security。
    打包漏库、受限环境加载被拒时返回 False——那种情况下说"有边界"是谎话，与
    ``_kernel32 is None`` 同一口径。

    这里**不缓存**结果：测试会用 monkeypatch 改 ``sys.platform`` 来模拟非 Windows，
    缓存会让那种替换失效。``ctypes.WinDLL`` 自身有内部缓存，重复调用的开销可以忽略。
    """
    if sys.platform != "win32":
        return False
    try:
        ctypes.WinDLL("kernel32", use_last_error=True)
        ctypes.WinDLL("advapi32", use_last_error=True)
    except (OSError, AttributeError):
        return False
    return True


def writable_sandbox_available() -> bool:
    """这台机器能不能真的把写侧沙箱物化出来。"""
    return sys.platform == "win32" and _win32_libraries_loadable()


def sandbox_boundary_note() -> str | None:
    """这一轮"OS 级写边界到底有没有"；没有时返回给用户与模型看的说明。"""
    return None if writable_sandbox_available() else SANDBOX_BOUNDARY_NOTE


__all__ = [
    "SANDBOX_BOUNDARY_NOTE",
    "sandbox_boundary_note",
    "writable_sandbox_available",
]

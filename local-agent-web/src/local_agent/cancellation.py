"""本轮执行的取消令牌（架构 §17.4，v0.6 T60）。

**这是叶子模块**：只放一个线程安全的小对象，不导入任何项目内模块。三个消费者分处不同层——
`security.policy`（安全 / 审批门）、`graph.nodes`（能力可见性门）与 `application.service`（取消命令、
超时看守），把定义放在任一层都会让另外两层反向依赖它。

它只回答一个问题："这一轮**已经**被取消了吗？"，并提供一个**注册清理动作**的挂点
（`add_cancel_hook`）——"已启动进程的终止"由 `security/process_guard.py` 挂在这里（§17.4）。

它自己**不做**任何清理：不撤销副作用、不回滚文件、不杀进程。回滚是明确不承诺的；
进程终止有它自己的模块与平台差异，混在令牌里会让这个叶子模块变成一个什么都干的上帝对象。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: 取消时给用户看的原因（默认）。
DEFAULT_CANCEL_REASON = "本轮已被取消，不再启动新的工具调用"

#: 取消时要执行的清理动作；参数是取消原因（可能为空串）。**必须不抛异常**（抛了也只会被记日志）。
CancelHook = Callable[[str], None]


class CancellationToken:
    """一次运行的取消信号（线程安全、幂等）。

    一次运行一个令牌；`cancel()` 可以由任意线程调用（HTTP 请求线程、超时看守线程），
    读取方是执行图的线程。
    """

    __slots__ = ("_cancelled", "_hooks", "_lock", "_reason")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._reason: str | None = None
        self._hooks: list[CancelHook] = []

    def cancel(self, reason: str = "") -> bool:
        """置位并执行清理动作。**幂等**：第二次调用返回 `False`，且**不覆盖**第一次的原因。"""
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            self._reason = reason.strip() or None
            hooks = list(self._hooks)
            self._hooks.clear()
            text = self._reason or ""
        # 在锁外执行清理：钩子会回读令牌（`cancelled` / `reason`），持锁执行会直接自锁。
        for hook in hooks:
            _run_hook(hook, text)
        return True

    def add_cancel_hook(self, hook: CancelHook) -> None:
        """注册"取消时要执行的清理"（架构 §17.4）。

        **已经取消时立即执行**：否则"先取消、后注册"会漏掉清理——而那正是"取消与工具启动竞态"
        的一半（进程刚要被启动，取消已经发生）。
        """
        with self._lock:
            if not self._cancelled:
                self._hooks.append(hook)
                return
            text = self._reason or ""
        _run_hook(hook, text)

    @property
    def cancelled(self) -> bool:
        """是否已被取消。"""
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> str | None:
        """取消原因；未取消或调用方没给原因时为 `None`（不编造理由）。"""
        with self._lock:
            return self._reason

    def refusal_reason(self) -> str:
        """拒绝工具调用时用的说明：带原因时带上它，否则用默认说明。"""
        reason = self.reason
        return f"{DEFAULT_CANCEL_REASON}（{reason}）" if reason else DEFAULT_CANCEL_REASON


def _run_hook(hook: CancelHook, reason: str) -> None:
    """执行清理动作；失败只记日志——**取消不能被清理动作的异常打断**（事实已经变了）。"""
    try:
        hook(reason)
    except Exception:
        logger.exception("取消清理动作失败（取消本身已经生效）")

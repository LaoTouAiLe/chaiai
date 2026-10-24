"""按应用/会话隔离的持久资源；不把 PID、句柄或连接写进图 Checkpoint。"""

from __future__ import annotations

import atexit
import hashlib
import json
import threading
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

from local_agent.exceptions import SecurityError
from local_agent.security.policy import SecurityPolicy


def permission_key(policy: SecurityPolicy) -> str:
    """权限改变后拒绝复用旧令牌；重新启动才能取得新权限。"""
    return json.dumps({"mode": policy.external_policy.mode,
                       "grants": policy.external_policy.grants}, sort_keys=True)


def scope_key(policy: SecurityPolicy) -> tuple[str, str]:
    """给同一应用、对话和工作区生成资源键。"""
    return str(policy.workspace_root.resolve()), policy.session_scope


def runtime_directory(policy: SecurityPolicy) -> Path:
    """运行产物位于工作区，逐次经过现有保护目录/外部授权检查。"""
    digest = hashlib.sha256(policy.session_scope.encode()).hexdigest()[:24]
    target = policy.resolve_writable_path(f".agent-runtime/{digest}/session")
    target.parent.mkdir(parents=True, exist_ok=True)
    ignore = policy.resolve_writable_path(".agent-runtime/.gitignore")
    try:
        with ignore.open("x", encoding="utf-8") as handle:
            handle.write("*\n")
    except FileExistsError:
        pass
    return policy.resolve_writable_path(str(target)).parent


class SessionResources:
    """线程安全的进程内资源表，不提供跨服务重启恢复。"""
    def __init__(self) -> None:
        """建立资源表与按运行令牌去重的取消绑定。"""
        self.lock = threading.RLock()
        self.items: dict[tuple[tuple[str, str], str], Any] = {}
        self._bindings: weakref.WeakKeyDictionary[Any, set[tuple[str, str]]] = weakref.WeakKeyDictionary()

    def bind_cancellation(self, policy: SecurityPolicy) -> None:
        """每个令牌与资源范围只绑定一次清理回调。"""
        token = policy.cancellation
        if token is not None:
            scope = scope_key(policy)
            with self.lock:
                scopes = self._bindings.setdefault(token, set())
                if scope in scopes:
                    return
                scopes.add(scope)
            token.add_cancel_hook(lambda _reason: self.close_scope(scope))

    def get(self, policy: SecurityPolicy, identity: str) -> Any:
        """只取当前范围内且权限仍一致的资源，并绑定本轮取消。"""
        with self.lock:
            item = self.items.get((scope_key(policy), identity))
        if item is None:
            raise ValueError("会话不存在或属于另一段对话；服务重启后需要重新启动")
        if item.permission_key != permission_key(policy):
            item.close()
            raise SecurityError("权限已变化，旧会话已关闭，请按当前权限重新启动")
        self.bind_cancellation(policy)
        return item

    def add(self, policy: SecurityPolicy, identity: str, item: Any) -> None:
        """登记已启动资源，取消竞态由令牌的立即回调收口。"""
        with self.lock:
            self.items[(scope_key(policy), identity)] = item
        self.bind_cancellation(policy)

    def close_scope(self, scope: tuple[str, str]) -> None:
        """移除一个对话工作区的资源，在锁外停止实际进程。"""
        with self.lock:
            selected = [(key, value) for key, value in self.items.items() if key[0] == scope]
            for key, _ in selected:
                self.items.pop(key, None)
        for _, item in selected:
            item.close()

    def close_namespace(self, namespace: str) -> None:
        """关闭单个应用实例拥有的全部资源。"""
        with self.lock:
            scopes = {key[0] for key in self.items if key[0][1].startswith(namespace + ":")}
        for scope in scopes:
            self.close_scope(scope)

    def close_capabilities(self, namespace: str, capabilities: set[str]) -> None:
        """只清理已关闭能力对应的资源，保留其它能力会话。"""
        with self.lock:
            selected = [(key, item) for key, item in self.items.items()
                        if key[0][1].startswith(namespace + ":")
                        and item.capability in capabilities]
            for key, _ in selected:
                self.items.pop(key, None)
        for _, item in selected:
            item.close()

    def close_all(self) -> None:
        """正常解释器退出时清理尚存资源。"""
        with self.lock:
            scopes = {key[0] for key in self.items}
        for scope in scopes:
            self.close_scope(scope)


RESOURCES = SessionResources()
atexit.register(RESOURCES.close_all)


def authorize(policy: SecurityPolicy, name: str, arguments: dict[str, Any]) -> None:
    """执行前复核与图安全门相同的策略及一次性审批。"""
    from local_agent.security.approval import action_fingerprint

    decision = policy.classify_tool(name, arguments)
    if not decision.allowed and not (
        decision.requires_approval
        and policy.is_granted(action_fingerprint(name, arguments, policy=policy))
    ):
        raise SecurityError(decision.reason)


def with_cancellation(policy: SecurityPolicy, close: Callable[[], None]) -> Callable[[], None]:
    """每次访问也绑定当前轮令牌，跨轮复用时取消仍能停止资源。"""
    token = policy.cancellation

    def cancel(_reason: str) -> None:
        close()

    if token is not None:
        token.add_cancel_hook(cancel)

    def detach() -> None:
        if token is not None:
            token.remove_cancel_hook(cancel)

    return detach

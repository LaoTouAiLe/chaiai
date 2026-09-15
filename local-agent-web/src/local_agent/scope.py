"""运行作用域与稳定标识（架构 §10.2、§10.3、§10.6）。

为什么单独一个模块：``project_id`` 与 ``scope_key`` 是**跨层共用**的规则 —— ``persistence``
（``sessions`` 表存 ``project_id``）与 ``memory``（中期记录、长期实体的作用域）必须用同一份实现。
放进 ``memory`` 会让持久层反向依赖记忆层，而 T15 起记忆层又要复用 ``persistence.migrations``，
两个包会互相依赖；放进 ``persistence`` 则让记忆层依赖会话索引这一个具体模块。因此放在这里：
本模块**只依赖标准库**，两侧都向下依赖它。

职责边界：只负责"标识怎么算"——路径规范化、稳定 Hash、``project_id``、逻辑用户 ID 与
``scope_key``。记忆记录的字段契约在 :mod:`local_agent.memory.schemas`，表结构在
:mod:`local_agent.persistence.migrations`。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from local_agent.exceptions import MemoryStoreError

#: 作用域取值（架构 §10.2）：全局 / 逻辑用户 / 项目 / 工作区。
MemoryScope = Literal["global", "user", "project", "workspace"]

SCOPE_GLOBAL: MemoryScope = "global"
SCOPE_USER: MemoryScope = "user"
SCOPE_PROJECT: MemoryScope = "project"
SCOPE_WORKSPACE: MemoryScope = "workspace"

#: 全部合法作用域，顺序与架构 §10.2 一致。
SCOPES: tuple[MemoryScope, ...] = (
    SCOPE_GLOBAL,
    SCOPE_USER,
    SCOPE_PROJECT,
    SCOPE_WORKSPACE,
)

#: 单用户本地运行且没有 ``user_id`` 时的逻辑用户 ID（架构 §10.2，不构成身份认证）。
LOCAL_USER_ID = "local"

#: ``global`` 作用域的 ``scope_key`` 固定值（架构 §10.2）。
GLOBAL_SCOPE_KEY = SCOPE_GLOBAL

#: 稳定 Hash 取多少位十六进制字符。
HASH_LENGTH = 16


def find_git_root(path: Path) -> Path | None:
    """向上查找 Git 根目录（``.git`` 目录或 worktree 的 ``.git`` 文件）。"""
    resolved = Path(path).resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def normalize_path(path: Path) -> str:
    """规范化路径文本：先 resolve，再统一成 posix 形式（Windows 用正斜杠）。"""
    return Path(path).resolve().as_posix()


def stable_hash(text: str, *, length: int = HASH_LENGTH) -> str:
    """稳定 Hash：同一文本在任何时候都得到同一结果（架构 §10.2 的稳定 Hash 规则）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def project_id_for(workspace_root: Path) -> str:
    """生成 ``project_id``：优先 Git 根目录，无 Git 时用规范化工作区路径（架构 §10.2）。

    Args:
        workspace_root: 本次运行的工作区根目录（架构 §4.1 的 ``RuntimeContext.workspace_root``）。

    Returns:
        16 位十六进制稳定 Hash；工作区内的子目录与仓库根得到同一个值。
    """
    root = Path(workspace_root).resolve()
    basis = normalize_path(find_git_root(root) or root)
    return stable_hash(basis)


def logical_user_id(user_id: str | None) -> str:
    """把可选的 ``user_id`` 归一成逻辑用户 ID：没有时固定为 ``local``（架构 §10.2）。"""
    return user_id.strip() if user_id and user_id.strip() else LOCAL_USER_ID


def scope_key_for(
    scope: MemoryScope | str,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
    workspace_root: Path | None = None,
) -> str:
    """按作用域算出 ``scope_key``（架构 §10.2）。

    ``scope_key`` 分别取：固定值 ``global``、当前逻辑用户 ID、``project_id``、
    规范化工作区路径的稳定 Hash。召回时先按 ``(scope, scope_key)`` 精确过滤，
    因此不同项目/工作区的私有记忆不会互相串。

    Args:
        scope: :data:`SCOPES` 之一。
        user_id: ``user`` 作用域用；为 ``None`` 时按 :data:`LOCAL_USER_ID` 处理。
        project_id: ``project`` 作用域用，通常由 :func:`project_id_for` 得到。
        workspace_root: ``workspace`` 作用域用；会先规范化再取 Hash。

    Returns:
        该作用域的 ``scope_key``。

    Raises:
        MemoryStoreError: 作用域非法，或该作用域缺少必需的参数（此时**不写入**而不是猜一个值）。
    """
    if scope == SCOPE_GLOBAL:
        return GLOBAL_SCOPE_KEY
    if scope == SCOPE_USER:
        return logical_user_id(user_id)
    if scope == SCOPE_PROJECT:
        if not project_id:
            raise MemoryStoreError("project 作用域需要 project_id，无法确定时不写入记忆")
        return project_id
    if scope == SCOPE_WORKSPACE:
        if workspace_root is None:
            raise MemoryStoreError("workspace 作用域需要 workspace_root，无法确定时不写入记忆")
        return stable_hash(normalize_path(Path(workspace_root)))
    allowed = "、".join(SCOPES)
    raise MemoryStoreError(f"未知作用域：{scope!r}（只能是 {allowed}）")

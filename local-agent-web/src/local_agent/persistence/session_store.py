"""会话索引（架构 §10.6、开发文档 T11）。

职责边界：

- 只维护 ``sessions`` 表（新建、列表、切换、重命名、最后活动时间与 Checkpoint 指针），
  不写 Checkpoint，也不碰挂起中的审批状态。
- 会话事实来源是数据库，不是浏览器：刷新页面或重启服务后按 ``sessions`` 表恢复，
  ``st.session_state`` 只放页面临时状态。
- ``thread_id`` 是 LangGraph Checkpoint 的恢复键，``session_id`` 是面向用户的会话标识；
  恢复流程固定为「先查 sessions 拿 thread_id，再用它调用 Graph」（架构 §10.6）。
- 待审批的会话只能经 ``Command(resume=...)`` 恢复，这个判断在 ``web.py``，本模块不参与。

两个约定（架构未写明、由 T11 确定）：

- ``status`` 取 ``active``（当前会话，同一库内至多一个）与 ``idle``（其他会话）；
  「当前会话」因此能跨刷新与重启恢复。
- ``thread_id`` 首次生成时取 ``thread-<session_id>``；只有第一个会话例外：它沿用
  v0.1a 的固定 ``thread_id``，这样升级后旧 Checkpoint 里的历史不会丢。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from local_agent.exceptions import SessionStoreError

#: 新会话的默认标题；第一次提交消息后会自动命名（之后仍可手动重命名）。
DEFAULT_TITLE = "新会话"
#: 沿用旧版固定 thread_id 时给第一个会话的名字。
ADOPTED_TITLE = "默认会话"
#: 会话名长度上限（含自动命名与手动重命名）。
TITLE_MAX_CHARS = 60
#: 自动命名时从首条用户消息里取的字符数。
AUTO_TITLE_CHARS = 30

STATUS_ACTIVE = "active"
STATUS_IDLE = "idle"

#: 查询列顺序与 :class:`Session` 字段顺序严格一致。
_COLUMNS = (
    "session_id, thread_id, title, workspace_root, project_id, "
    "created_at, updated_at, last_checkpoint_id, status"
)
_PLACEHOLDERS = ", ".join("?" * 9)


@dataclass(frozen=True)
class Session:
    """一行会话索引记录（字段按架构 §10.6）。"""

    session_id: str
    thread_id: str
    title: str
    workspace_root: str
    project_id: str
    created_at: str
    updated_at: str
    last_checkpoint_id: str | None
    status: str

    @property
    def is_active(self) -> bool:
        """是否为当前会话。"""
        return self.status == STATUS_ACTIVE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def stable_hash(text: str, *, length: int = 16) -> str:
    """稳定 Hash：同一文本在任何时候都得到同一结果（架构 §10.2 的稳定 Hash 规则）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def project_id_for(workspace_root: Path) -> str:
    """生成 ``project_id``：优先 Git 根目录，无 Git 时用规范化工作区路径（架构 §10.2）。"""
    root = Path(workspace_root).resolve()
    basis = normalize_path(find_git_root(root) or root)
    return stable_hash(basis)


def _clean_title(title: str) -> str:
    """清洗会话名：压掉换行与多余空白并检查长度。"""
    flattened = " ".join(str(title).split())
    if not flattened:
        raise SessionStoreError("会话名称不能为空")
    if len(flattened) > TITLE_MAX_CHARS:
        raise SessionStoreError(f"会话名称最多 {TITLE_MAX_CHARS} 个字符")
    return flattened


def auto_title(text: str) -> str:
    """按首条用户消息生成会话名：取第一行、压缩空白、必要时截断。"""
    first_line = str(text).splitlines()[0] if str(text).splitlines() else ""
    flattened = " ".join(first_line.split())
    if not flattened:
        return DEFAULT_TITLE
    if len(flattened) <= AUTO_TITLE_CHARS:
        return flattened
    return f"{flattened[:AUTO_TITLE_CHARS]}…"


def _row_to_session(row: tuple[object, ...]) -> Session:
    return Session(
        session_id=str(row[0]),
        thread_id=str(row[1]),
        title=str(row[2]),
        workspace_root=str(row[3]),
        project_id=str(row[4]),
        created_at=str(row[5]),
        updated_at=str(row[6]),
        last_checkpoint_id=str(row[7]) if row[7] is not None else None,
        status=str(row[8]),
    )


class SessionStore:
    """``sessions`` 表的读写；连接由调用方持有（与 Checkpoint 共用一个库）。"""

    def __init__(self, connection: sqlite3.Connection, *, workspace_root: Path) -> None:
        self._connection = connection
        self.workspace_root = Path(workspace_root).resolve()
        self.project_id = project_id_for(self.workspace_root)

    # ------------------------------------------------------------------ 查询

    def list_sessions(self) -> list[Session]:
        """本工作区的会话，按最后活动时间倒序（最近使用的在前面）。"""
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE workspace_root = ? "
            "ORDER BY updated_at DESC, created_at DESC",
            (normalize_path(self.workspace_root),),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def get(self, session_id: str) -> Session | None:
        """按 ``session_id`` 取一条记录；不存在返回 ``None``。"""
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return _row_to_session(row) if row is not None else None

    def active(self) -> Session | None:
        """当前会话；没有（例如库是新建的）返回 ``None``。"""
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE workspace_root = ? AND status = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (normalize_path(self.workspace_root), STATUS_ACTIVE),
        ).fetchone()
        return _row_to_session(row) if row is not None else None

    def is_thread_taken(self, thread_id: str) -> bool:
        """该 ``thread_id`` 是否已被某个会话占用（``thread_id`` 在库内全局唯一）。"""
        row = self._connection.execute(
            "SELECT 1 FROM sessions WHERE thread_id = ? LIMIT 1", (thread_id,)
        ).fetchone()
        return row is not None

    def has_checkpoints(self, thread_id: str) -> bool:
        """该 ``thread_id`` 是否已有 Checkpoint（用于沿用 v0.1a 的固定会话）。"""
        table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'checkpoints'"
        ).fetchone()
        if table is None:
            return False
        row = self._connection.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (thread_id,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ 写入

    def ensure_active(self, *, legacy_thread_id: str | None = None) -> Session:
        """返回当前会话；一个都没有时创建（刷新与重启都不会重复新建）。

        Args:
            legacy_thread_id: v0.1a 的固定 ``thread_id``。库里还没有任何会话时，
                第一个会话直接沿用它，旧版本的 Checkpoint 历史因此不会丢。

        Returns:
            当前会话（新建的或原有的）。
        """
        current = self.active()
        if current is not None:
            return current

        existing = self.list_sessions()
        if existing:
            # 有会话但没有激活项（例如手动改过库）：回到最近活动的那个
            return self.switch(existing[0].session_id)

        # 第一个会话沿用 v0.1a 的固定 thread_id；它已被别的会话占用时（多个工作区
        # 共用一个库）只能退回自动生成的 thread_id，否则会撞 UNIQUE(thread_id)。
        legacy = legacy_thread_id or ""
        adopt = bool(legacy) and not self.is_thread_taken(legacy)
        return self.create(
            title=ADOPTED_TITLE if (adopt and self.has_checkpoints(legacy)) else None,
            thread_id=legacy if adopt else None,
        )

    def create(self, *, title: str | None = None, thread_id: str | None = None) -> Session:
        """新建会话并切换过去（原来的当前会话变为 ``idle``）。

        Args:
            title: 会话名；缺省用 :data:`DEFAULT_TITLE`。
            thread_id: 指定 Checkpoint 恢复键；缺省按 ``thread-<session_id>`` 生成。

        Returns:
            新建的会话记录。
        """
        session_id = uuid4().hex[:12]
        record = Session(
            session_id=session_id,
            thread_id=thread_id or f"thread-{session_id}",
            title=_clean_title(title) if title else DEFAULT_TITLE,
            workspace_root=normalize_path(self.workspace_root),
            project_id=self.project_id,
            created_at=_now(),
            updated_at=_now(),
            last_checkpoint_id=None,
            status=STATUS_ACTIVE,
        )
        with self._connection:
            self._deactivate_all()
            self._connection.execute(
                f"INSERT INTO sessions ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
                (
                    record.session_id,
                    record.thread_id,
                    record.title,
                    record.workspace_root,
                    record.project_id,
                    record.created_at,
                    record.updated_at,
                    record.last_checkpoint_id,
                    record.status,
                ),
            )
        return record

    def switch(self, session_id: str) -> Session:
        """把指定会话设为当前会话（切换即恢复：调用方随后按它的 ``thread_id`` 读图）。"""
        target = self._require(session_id)
        if target.is_active:
            return target
        with self._connection:
            self._deactivate_all()
            self._connection.execute(
                "UPDATE sessions SET status = ? WHERE session_id = ?",
                (STATUS_ACTIVE, session_id),
            )
        return self._require(session_id)

    def rename(self, session_id: str, title: str) -> Session:
        """重命名会话（不改变最后活动时间）。"""
        self._require(session_id)
        cleaned = _clean_title(title)
        self._connection.execute(
            "UPDATE sessions SET title = ? WHERE session_id = ?", (cleaned, session_id)
        )
        self._connection.commit()
        return self._require(session_id)

    def touch(
        self,
        session_id: str,
        *,
        last_checkpoint_id: str | None = None,
        title_hint: str | None = None,
    ) -> Session:
        """一轮对话结束后更新最后活动时间与 Checkpoint 指针。

        Args:
            session_id: 目标会话。
            last_checkpoint_id: 本轮的 Checkpoint 编号；``None`` 表示保持原值。
            title_hint: 本轮的 ``thread_id``；仅当会话还是默认名时用于自动命名。
        """
        current = self._require(session_id)
        title = current.title
        if title_hint and current.title == DEFAULT_TITLE:
            title = auto_title(title_hint)
        self._connection.execute(
            "UPDATE sessions SET updated_at = ?, title = ?, "
            "last_checkpoint_id = COALESCE(?, last_checkpoint_id) WHERE session_id = ?",
            (_now(), title, last_checkpoint_id, session_id),
        )
        self._connection.commit()
        return self._require(session_id)

    # ------------------------------------------------------------------ 内部

    def _require(self, session_id: str) -> Session:
        record = self.get(session_id)
        if record is None:
            raise SessionStoreError(f"会话不存在：{session_id}")
        return record

    def _deactivate_all(self) -> None:
        """把本工作区的所有会话置为 ``idle``（保证当前会话唯一）。"""
        self._connection.execute(
            "UPDATE sessions SET status = ? WHERE workspace_root = ?",
            (STATUS_IDLE, normalize_path(self.workspace_root)),
        )

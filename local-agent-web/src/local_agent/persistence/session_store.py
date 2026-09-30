"""会话索引（架构 §10.6、开发文档 T11）。

职责边界：

- 只维护 ``sessions`` 表（新建、列表、切换、重命名、最后活动时间与 Checkpoint 指针），
  不写 Checkpoint，也不碰挂起中的审批状态。
- 会话事实来源是数据库，不是浏览器：刷新页面或重启服务后按 ``sessions`` 表恢复，
  ``st.session_state`` 只放页面临时状态。
- ``thread_id`` 是 LangGraph Checkpoint 的恢复键，``session_id`` 是面向用户的会话标识；
  恢复流程固定为「先查 sessions 拿 thread_id，再用它调用 Graph」（架构 §10.6）。
- 待审批的会话只能经 ``Command(resume=...)`` 恢复，这个判断在 ``web.py``，本模块不参与。
- ``project_id`` 与路径规范化由 :mod:`local_agent.scope` 统一实现：记忆层要用同一份规则。

两个约定（架构未写明、由 T11 确定）：

- ``status`` 取 ``active``（当前会话，同一库内至多一个）与 ``idle``（其他会话）；
  「当前会话」因此能跨刷新与重启恢复。
- ``thread_id`` 首次生成时取 ``thread-<session_id>``；只有第一个会话例外：它沿用
  v0.1a 的固定 ``thread_id``，这样升级后旧 Checkpoint 里的历史不会丢。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from local_agent.exceptions import SessionStoreError

#: ``project_id`` / ``normalize_path`` 等稳定标识在 :mod:`local_agent.scope` 统一实现：
#: 记忆层要用同一份规则，放在这里会造成两侧各算一次而漂移。
from local_agent.scope import normalize_path, project_id_for

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
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
    """``sessions`` 表的读写；连接由调用方持有（与 Checkpoint 共用一个库）。

    T14C 起的语义（架构 §10.6）：

    - ``status=active`` 是**整个库唯一**的当前会话（聊天库 v2 的部分唯一索引兜底），
      不再"每个工作区各有一个当前会话"；
    - 会话与工作区一对一绑定：切换工作区 = 切到该工作区的会话或新建一个，绝不改写已有会话的
      ``workspace_root``（否则同一 Checkpoint 会跨工作区污染 AGENTS、记忆与文件上下文）；
    - 构造时传入的 ``workspace_root`` 是**启动默认值**（新会话的默认绑定目标），不再是过滤条件。
    """

    def __init__(self, connection: sqlite3.Connection, *, workspace_root: Path) -> None:
        self._connection = connection
        self.workspace_root = Path(workspace_root).resolve()
        self.project_id = project_id_for(self.workspace_root)

    # ------------------------------------------------------------------ 查询

    def list_sessions(self, *, workspace_root: Path | None = None) -> list[Session]:
        """列出会话，按最后活动时间倒序（最近使用的在前面）。

        Args:
            workspace_root: 只看该工作区的会话；``None``（默认）列出**库内全部**会话。

        默认列全部是 T14C 的语义变化：当前会话在库内唯一，切换工作区就是切换会话，所以
        侧栏看到的是整库的会话而不是"当前工作区的会话"。需要按工作区过滤时显式传参。
        """
        if workspace_root is None:
            rows = self._connection.execute(
                f"SELECT {_COLUMNS} FROM sessions ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"SELECT {_COLUMNS} FROM sessions WHERE workspace_root = ? "
                "ORDER BY updated_at DESC, created_at DESC",
                (normalize_path(workspace_root),),
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def list_workspaces(self) -> list[str]:
        """已使用过的工作区（规范化路径，去重，最近使用的在前）。

        T14C 起工作区与会话绑定，列表直接从 ``sessions.workspace_root`` 去重生成：不另建表，
        也不新增 State 字段（架构 §10.6）。
        """
        rows = self._connection.execute(
            "SELECT workspace_root, MAX(updated_at) AS last_used, "
            "MAX(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS is_active "
            "FROM sessions GROUP BY workspace_root "
            "ORDER BY last_used DESC, is_active DESC, workspace_root ASC"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def get(self, session_id: str) -> Session | None:
        """按 ``session_id`` 取一条记录；不存在返回 ``None``。"""
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return _row_to_session(row) if row is not None else None

    def active(self) -> Session | None:
        """当前会话；没有（例如库是新建的）返回 ``None``。

        T14C 起 ``status='active'`` 表示**整个库内唯一**的当前会话，不再按工作区各算一个；
        聊天库 v2 迁移加了部分唯一索引保证这一点（架构 §10.6）。
        """
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE status = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (STATUS_ACTIVE,),
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

    def create(
        self,
        *,
        title: str | None = None,
        thread_id: str | None = None,
        workspace_root: Path | None = None,
    ) -> Session:
        """新建会话并切换过去（原来的当前会话变为 ``idle``）。

        Args:
            title: 会话名；缺省用 :data:`DEFAULT_TITLE`。
            thread_id: 指定 Checkpoint 恢复键；缺省按 ``thread-<session_id>`` 生成。
            workspace_root: 新会话绑定的工作区；缺省用本 store 的启动默认工作区。
                T14C 起一个会话只绑定一个工作区，``project_id`` 由它算出。

        Returns:
            新建的会话记录。
        """
        root = self.workspace_root if workspace_root is None else Path(workspace_root).resolve()
        session_id = uuid4().hex[:12]
        now = _now()
        record = Session(
            session_id=session_id,
            thread_id=thread_id or f"thread-{session_id}",
            title=_clean_title(title) if title else DEFAULT_TITLE,
            workspace_root=normalize_path(root),
            project_id=project_id_for(root),
            created_at=now,
            updated_at=now,
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
        """把指定会话设为当前会话，并记下这次使用（随后按其 ``thread_id`` 读图）。"""
        target = self._require(session_id)
        if target.is_active:
            return target
        with self._connection:
            self._deactivate_all()
            self._connection.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (STATUS_ACTIVE, _now(), session_id),
            )
        return self._require(session_id)

    def switch_workspace(self, workspace_root: Path, *, title: str | None = None) -> Session:
        """把当前会话切到指定工作区：该工作区已有会话就切过去，否则新建一个。

        归一化（``resolve()`` 真实路径解析）后比较，避免 Windows 大小写、分隔符、尾部分隔符
        或可解析的符号链接入口产生重复的工作区身份（架构 §1.5）。会话只绑定一个工作区，
        所以这里**不会**改写已有会话的 ``workspace_root``。

        Args:
            workspace_root: 目标工作区目录。
            title: 新建会话时的名字（已有会话时忽略）。

        Returns:
            切换后的当前会话。
        """
        root = Path(workspace_root).resolve()
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE workspace_root = ? "
            "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            (normalize_path(root),),
        ).fetchone()
        if row is not None:
            return self.switch(_row_to_session(row).session_id)
        return self.create(title=title, workspace_root=root)

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
        """把库内所有会话置为 ``idle``（T14C 起当前会话在整个库内唯一，架构 §10.6）。

        同一事务里"先停用旧记录、再激活目标记录"，与聊天库 v2 的部分唯一索引一起保证
        ``active`` 不会同时出现两条。
        """
        self._connection.execute(
            "UPDATE sessions SET status = ? WHERE status = ?", (STATUS_IDLE, STATUS_ACTIVE)
        )

"""会话索引（架构 §10.6、开发文档 T11）。

职责边界：

- 维护 ``sessions`` 索引及 ``workspaces`` 登记（显示名称、移除标记），
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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from local_agent.exceptions import SessionStoreError
from local_agent.persistence import turn_index, usage_store
from local_agent.persistence.conversation_store import ConversationStore

#: ``project_id`` / ``normalize_path`` 等稳定标识在 :mod:`local_agent.scope` 统一实现：
#: 记忆层要用同一份规则，放在这里会造成两侧各算一次而漂移。
from local_agent.scope import normalize_path, project_id_for
from local_agent.session_kind import DEFAULT_SESSION_KIND, SESSION_KINDS

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
    "created_at, updated_at, last_checkpoint_id, status, mode, archived, kind, "
    # 侧边会话（§21.7，2026-10-23）：两列都可空，普通会话恒为 NULL。
    "parent_session_id, origin_turn_id, "
    # 已读位置（2026-10-30）：用户最后一次打开并看到这条会话的时间。
    # 可空 = 从没打开过（这种会话不算"未读"，否则第一次开应用满屏都是未读）。
    "last_read_at"
)
_PLACEHOLDERS = ", ".join("?" * 15)

#: 列表/搜索用的列清单：会话字段 + **该会话最后一轮对话的提交时间**（2026-10-30）。
#:
#: 为什么不能用 `sessions.updated_at` 判未读：实测它只在建会话时写一次，`touch()` 虽然
#: 存在却**没有任何调用方**（全仓搜索为空）。拿它比"我上次看到的时刻"，结果永远是已读。
#: `conversation_turns.submitted_at` 才是每轮真正会动的那个时间。
_SELECT_COLUMNS = (
    f"{_COLUMNS}, "
    "(SELECT MAX(t.submitted_at) FROM conversation_turns t"
    " WHERE t.session_id = s.session_id) AS last_turn_at"
)


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
    mode: str
    archived: bool = False
    #: 会话来源（架构 §10.6、§17.1）：``interactive`` / ``scheduled`` / ``heartbeat``。
    #: 它只影响记忆候选的**来源级别**，不参与 ``mode`` 决定的工具集与提示段落。
    kind: str = DEFAULT_SESSION_KIND
    #: 侧边会话（§21.7）：从哪个会话开出。普通会话为 ``None``。
    #: **不新增 kind** —— `sessions.kind` 有 CHECK 约束，加值要重建整张表；
    #: 用这一列是否存在表达"是不是侧聊"同样明确。
    parent_session_id: str | None = None
    #: 从父会话的哪一轮开出；`parent_session_id` 为空时它也为空。
    origin_turn_id: str | None = None
    #: 用户最后一次打开这条会话的时间（2026-10-30，与 ``updated_at`` 同一个格式）。
    #: ``None`` = 从没打开过：这种会话不算"未读"（否则第一次开应用满屏都是未读）。
    last_read_at: str | None = None
    #: 这个会话**最后一轮对话**的提交时间（只有列表/搜索查询会填）。
    #:
    #: 未读判据用它而不是 ``updated_at``：后者只在建会话时写一次（`touch()` 其实是死代码），
    #: 拿它比等于永远已读。为空（还没聊过 / 老库）时退回 ``updated_at``。
    last_turn_at: str | None = None

    @property
    def unread(self) -> bool:
        """有没有"我离开之后新增的内容"。

        从没打开过的会话返回 ``False``——"没看过"和"看过了但又有新的"是两件事，
        前者不需要提醒（用户还没建立对它的预期）。
        """
        if self.last_read_at is None:
            return False
        return (self.last_turn_at or self.updated_at) > self.last_read_at

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
        mode=str(row[9]),
        archived=bool(row[10]),
        kind=str(row[11]),
        parent_session_id=str(row[12]) if row[12] is not None else None,
        origin_turn_id=str(row[13]) if row[13] is not None else None,
        last_read_at=str(row[14]) if row[14] is not None else None,
        # 第 16 列只有列表/搜索那条查询会带（见 `_SELECT_COLUMNS`）。
        last_turn_at=str(row[15]) if len(row) > 15 and row[15] is not None else None,
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
        self.conversations = ConversationStore(connection)
        self.workspace_root = Path(workspace_root).resolve()
        self.project_id = project_id_for(self.workspace_root)

    # ------------------------------------------------------------------ 查询

    def list_sessions(
        self, *, workspace_root: Path | None = None, archived: bool = False
    ) -> list[Session]:
        """列出会话，按最后活动时间倒序（最近使用的在前面）。

        Args:
            workspace_root: 只看该工作区的会话；``None``（默认）列出**库内全部**会话。

        默认列全部是 T14C 的语义变化：当前会话在库内唯一，切换工作区就是切换会话，所以
        侧栏看到的是整库的会话而不是"当前工作区的会话"。需要按工作区过滤时显式传参。
        """
        # **侧聊不进列表**（2026-10-23 用户裁定）：它是从某个会话开出来的旁支，
        # 在侧栏各占一行会把真正的主会话挤下去（实机截图里连着 5 条「… · 侧聊 N」）。
        # 侧聊仍可访问：右栏「侧边对话」页签、会话地图里的「侧聊」入口都用 id 直接取。
        # 想让它变成正式会话，用「提升」（清掉 parent_session_id 后自然回到列表）。
        side_chat_filter = " AND parent_session_id IS NULL"
        if workspace_root is None:
            rows = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM sessions s"
                f" WHERE s.archived = ?{side_chat_filter} "
                "ORDER BY s.updated_at DESC, s.created_at DESC",
                (int(archived),),
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM sessions s"
                f" WHERE s.workspace_root = ? AND s.archived = ?"
                f"{side_chat_filter} ORDER BY s.updated_at DESC, s.created_at DESC",
                (normalize_path(workspace_root), int(archived)),
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def search_sessions(
        self, query: str, *, limit: int = 20, archived: bool = False
    ) -> list[Session]:
        """按**标题、用户提问或回答正文**搜索会话（§15.11.2，2026-10-22；2026-11-02 扩到全文）。

        历史口径只匹配 `question`，理由是"回答是长文本，LIKE 扫全表会随历史线性变慢"。
        那条理由没错，但它把"我记得它答过什么"挡在门外——而这正是跨会话最常找的东西。
        回答正文现在走 FTS5 索引（`persistence/turn_index.py`），代价不再随历史线性增长。

        **索引不可用时退回旧口径**（标题 + 提问 LIKE）：本机 SQLite 没编 FTS5 时索引表不存在，
        这里按"表在不在"决定走哪条路，而不是让整条搜索报错。

        排序口径**不变**：仍按 `updated_at` 倒序，不按相关度。侧栏是"最近用过的会话"，
        换成相关度会让搜一次之后整体顺序跳变，用户会以为会话被挪走了。

        `%` 与 `_` 按字面处理（用户输入不当作通配符），否则一次输入 `%` 就会把全部会话捞出来。
        """
        needle = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        hit_ids = turn_index.matched_session_ids(self._connection, query)
        if not hit_ids:
            return self._like_search(needle, limit=limit, archived=archived)
        # 命中会话可能很多：`MAX_HITS` 已经把 id 数压到 SQL 参数上限以内。
        placeholders = ",".join("?" * len(hit_ids))
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM sessions s "
            f"WHERE s.archived = ? AND (s.session_id IN ({placeholders})"
            " OR s.title LIKE ? ESCAPE '\\') "
            "ORDER BY s.updated_at DESC, s.created_at DESC LIMIT ?",
            (int(archived), *hit_ids, needle, limit),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    # ------------------------------------------------------------ 用量账（v18）

    def append_usage(
        self, session_id: str, workspace_root: str, records: Sequence[Any]
    ) -> int:
        """把一批模型调用记录写进用量账（``INSERT OR IGNORE``，重放安全）。

        与轮次表同一条连接、同一个事务边界：调用方都在 ``_sqlite_access`` 里调它，
        避免两条连接在同一个库上互相加锁。
        """
        return usage_store.append_calls(
            self._connection,
            session_id=session_id,
            workspace_root=workspace_root,
            records=records,
        )

    def usage_rows(
        self,
        *,
        workspace_root: str | None = None,
        since: Any = None,
        until: Any = None,
        model: str = "",
    ) -> list[usage_store.UsageRow]:
        """按工作区 / 时间窗 / 模型名取用量账（数据中台用）。"""
        return usage_store.rows(
            self._connection,
            workspace_root=workspace_root,
            since=since,
            until=until,
            model=model,
        )

    def usage_sessions(self, *, workspace_root: str | None = None) -> set[str]:
        """账本里已有记录的会话 id（判断哪些老会话还需要读穿回填）。"""
        return usage_store.sessions_with_rows(self._connection, workspace_root=workspace_root)

    def search_turn_details(
        self, query: str, *, limit: int = 5
    ) -> list[turn_index.TurnDetail]:
        """按相关度返回命中的**轮次明细**（`session_search` 工具用，2026-11-02）。

        与 :meth:`search_sessions` 的差别是粒度与取舍：那个给页面列表用，只要会话；
        这个给模型用，要"哪个会话的哪一轮、原文是什么"，并且**包含已归档会话**——
        归档只是离开侧栏视线，旧结论往往就在里面。
        """
        return turn_index.search_details(self._connection, query, limit=limit)

    def _like_search(self, needle: str, *, limit: int, archived: bool) -> list[Session]:
        """旧的纯 LIKE 口径：标题 + 用户提问。只在全文索引不可用时使用（见 :meth:`search_sessions`）。"""
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM sessions s "
            "WHERE s.archived = ? AND (s.title LIKE ? ESCAPE '\\' OR EXISTS ("
            "SELECT 1 FROM conversation_turns t WHERE t.session_id = s.session_id "
            "AND t.question LIKE ? ESCAPE '\\')) "
            "ORDER BY s.updated_at DESC, s.created_at DESC LIMIT ?",
            (int(archived), needle, needle, limit),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def promote(self, session_id: str) -> None:
        """侧聊转为正式会话（§21.7）：清掉血统两列。

        **不删历史、不改 id**：提升只改"它属于谁"，其余一切照旧——
        改 id 会让已经打开的页面、运行记录里的引用全部失效。
        """
        with self._connection:
            self._connection.execute(
                "UPDATE sessions SET parent_session_id = NULL, origin_turn_id = NULL"
                " WHERE session_id = ?",
                (session_id,),
            )

    def side_chats(self, parent_session_id: str) -> list[Session]:
        """某个会话开出来的侧聊（§21.7）。按创建时间升序，用于给标题编号。"""
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE parent_session_id=? ORDER BY created_at",
            (parent_session_id,),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def list_workspaces(self) -> list[str]:
        """已使用过的工作区（规范化路径，去重，最近使用的在前）。

        T39 起独立登记。移除只改变登记的可见性，不改会话绑定与磁盘目录。
        """
        rows = self._connection.execute(
            "SELECT w.workspace_root, MAX(s.updated_at) AS last_used, "
            "MAX(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS is_active "
            "FROM workspaces w LEFT JOIN sessions s ON s.workspace_root = w.workspace_root "
            "WHERE w.removed = 0 GROUP BY w.workspace_root "
            "ORDER BY last_used DESC, is_active DESC, w.workspace_root ASC"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def workspace_title(self, workspace_root: Path) -> str | None:
        row = self._connection.execute(
            "SELECT title FROM workspaces WHERE workspace_root = ?",
            (normalize_path(workspace_root),),
        ).fetchone()
        return str(row[0]) if row is not None and row[0] is not None else None

    def rename_workspace(self, workspace_root: Path, title: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE workspaces SET title = ? WHERE workspace_root = ? AND removed = 0",
                (_clean_title(title), normalize_path(workspace_root)),
            )

    def remove_workspace(self, workspace_root: Path) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE workspaces SET removed = 1 WHERE workspace_root = ?",
                (normalize_path(workspace_root),),
            )

    def _register_workspace(self, workspace_root: Path) -> None:
        self._connection.execute(
            "INSERT INTO workspaces (workspace_root) VALUES (?) "
            "ON CONFLICT(workspace_root) DO UPDATE SET removed = 0",
            (normalize_path(workspace_root),),
        )

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
        mode: str = "agent",
        kind: str = DEFAULT_SESSION_KIND,
        activate: bool = True,
        parent_session_id: str | None = None,
        origin_turn_id: str | None = None,
    ) -> Session:
        """新建会话并切换过去（原来的当前会话变为 ``idle``）。

        Args:
            title: 会话名；缺省用 :data:`DEFAULT_TITLE`。
            thread_id: 指定 Checkpoint 恢复键；缺省按 ``thread-<session_id>`` 生成。
            workspace_root: 新会话绑定的工作区；缺省用本 store 的启动默认工作区。
                T14C 起一个会话只绑定一个工作区，``project_id`` 由它算出。
            activate: 是否把新会话切成当前会话。缺省 ``True``，与既有行为一致；
                定时任务这类后台会话传 ``False``——它不该抢用户焦点，也不该把
                用户正在看的会话置为 ``idle``（§21.5）。

        Returns:
            新建的会话记录。
        """
        if mode not in ("chat", "agent"):
            raise SessionStoreError(f"未知会话模式：{mode}")
        if kind not in SESSION_KINDS:
            raise SessionStoreError(f"未知会话来源：{kind}")
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
            status=STATUS_ACTIVE if activate else STATUS_IDLE,
            mode=mode,
            kind=kind,
            parent_session_id=parent_session_id,
            origin_turn_id=origin_turn_id,
        )
        with self._connection:
            self._register_workspace(root)
            if activate:
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
                    record.mode,
                    int(record.archived),
                    record.kind,
                    record.parent_session_id,
                    record.origin_turn_id,
                    # 新会话的已读位置是 NULL（2026-10-30）：NULL 表示"从没打开过"，
                    # 不算未读。用户切到它时前端会打一次 `mark_read`。
                    record.last_read_at,
                ),
            )
        return record

    def switch(self, session_id: str) -> Session:
        """把指定会话设为当前会话，并记下这次使用（随后按其 ``thread_id`` 读图）。"""
        target = self._require(session_id)
        if target.archived:
            raise SessionStoreError("请先恢复已归档的会话")
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
        with self._connection:
            self._register_workspace(root)
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE workspace_root = ? AND archived = 0 "
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

    def set_mode(self, session_id: str, mode: str) -> Session:
        """修改下一轮模式，保留线程、标题、工作区和历史路径。"""
        self._require(session_id)
        if mode not in {"chat", "agent"}:
            raise SessionStoreError("未知会话模式")
        self._connection.execute("UPDATE sessions SET mode = ? WHERE session_id = ?", (mode, session_id))
        self._connection.commit()
        return self._require(session_id)

    def set_archived(self, session_id: str, archived: bool) -> Session:
        current = self._require(session_id)
        with self._connection:
            self._connection.execute(
                "UPDATE sessions SET archived = ?, status = CASE WHEN ? THEN 'idle' "
                "ELSE status END WHERE session_id = ?",
                (int(archived), int(archived), session_id),
            )
            if archived and current.is_active:
                remaining = self.list_sessions()
                if remaining:
                    self._connection.execute(
                        "UPDATE sessions SET status = 'active' WHERE session_id = ?",
                        (remaining[0].session_id,),
                    )
        return self._require(session_id)

    def mark_read(self, session_id: str) -> str:
        """记下"用户看到了这里"（2026-10-30），返回写入的时间。

        为什么用**服务端时钟的"此刻"**而不是这条会话的 ``updated_at``：两者比较时
        必须同源，``updated_at`` 也是服务端写的；写"此刻"语义更准——"我看到的是
        这一刻之前的所有内容"，之后再有更新自然就算未读。

        为什么由服务端写而不是让前端报一个时间：本机时钟可能不准，一旦客户端时间
        偏快，写进来的值会永远大于后续的 ``updated_at``，未读提示就再也不出现了。
        """
        self._require(session_id)
        moment = _now()
        with self._connection:
            self._connection.execute(
                "UPDATE sessions SET last_read_at = ? WHERE session_id = ?",
                (moment, session_id),
            )
        return moment

    def delete_session(self, session_id: str) -> None:
        self.delete_sessions((session_id,))

    def delete_sessions(self, session_ids: tuple[str, ...]) -> None:
        """在一个事务中删除指定父子会话在**本库**里的全部行。

        为什么一个事务、逐表显式删：这些表都带 ``REFERENCES sessions(session_id)``，
        但连接上**没有**开 ``PRAGMA foreign_keys``（只有迁移里临时开关），删主表不会
        级联。分几次删一旦中途失败，就会留下一批指向不存在会话的孤儿行——它们在页面上
        表现为"查不到的幽灵数据"，比多删一层难收拾得多。

        Checkpoint、附件目录、变更基线目录**不在这里**：它们分别归 `SqliteSaver`、
        `AttachmentStore` 与 `FileChangeLedger`，由应用层按顺序调用。

        会话级的模型选择（`model_selection`）也不在这里：那张表在**设置库**（另一个
        连接），不在聊天库里。留下的一行孤儿选择不会被任何读路径看到——选择器只对现存
        会话读取它，删掉的会话不会再出现。
        """
        for session_id in session_ids:
            self._require(session_id)
        with self._connection:
            for session_id in session_ids:
                self._delete_session_rows(session_id)

    def _delete_session_rows(self, session_id: str) -> None:
        # 全文索引先清：它没有外键，删掉 conversation_turns 之后就没有任何东西能定位到它的行了。
        turn_index.delete_session(self._connection, session_id)
        # 子表先行：`job_children` 经 job_id 间接引用，不能按 session_id 删。
        self._connection.execute(
            "DELETE FROM job_children WHERE parent_job_id IN "
            "(SELECT job_id FROM jobs WHERE session_id = ?)",
            (session_id,),
        )
        # 表名是写死的常量元组，不是拼进来的输入。
        for table in (
            "jobs",
            "conversation_turns",
            "conversation_branches",
            "file_change_ops",
            "file_change_baselines",
            "file_change_blobs",
            "request_ledger",
        ):
            self._connection.execute(
                f"DELETE FROM {table} WHERE session_id = ?",
                (session_id,),
            )
        self._connection.execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,)
        )

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
            title_hint: 首条用户提问；仅当会话还是默认名时用于自动命名。
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

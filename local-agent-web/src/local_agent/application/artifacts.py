"""产物汇总：只读、有界、不新建表（契约 §15.11.1，2026-10-22）。

三路来源各自独立可缺席：

1. `conversation_turns` 的正文（question / answer_text）里抽 markdown 图片、裸 URL、绝对路径；
2. `file_change_ops` 里 `create` / `modify` 的工作区写操作；
3. 附件目录 `data/attachments/年/月/日/会话/` 下的文件。

**不读 Checkpoint**：工具返回值里的 `saved_to` / `screenshot_path` 只存在于序列化 State 中，
逐会话反序列化整份 messages 的代价远超收益（同 `data_center` 要限制扫描会话数的原因）。
"""

from __future__ import annotations

import base64
import itertools
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal

from local_agent.application import projection as proj
from local_agent.application.data_center_models import (
    ArtifactPreviewView,
    ArtifactsPageView,
    ArtifactView,
)
from local_agent.media import detect_image_media_type

if TYPE_CHECKING:
    from local_agent.application.service import ApplicationService
    from local_agent.persistence.session_store import Session

#: 扫描闸门：任一触顶都要在页面上说清"没扫全"（§15.11.1）。
SESSION_SCAN_LIMIT = 200
#: 正文扫描预算（2026-10-22 由 50_000 降到 8_000）：近 30 天极少超过这个量，
#: 而每一轮都要取一次正文——闸门放太大等于每次翻页都在读全库。
TURN_TEXT_LIMIT = 8_000
OP_SCAN_LIMIT = 5_000
FILE_SCAN_LIMIT = 5_000
TEXT_LIMIT = 20_000

#: 产物页的进程内缓存（2026-10-22）。扫描是 O(会话 × 轮次) 的，
#: 翻页与切类型不该重算——以前每翻一页都重扫 200 个会话，第二页要等好几秒。
CACHE_TTL_SECONDS = 60.0
_CACHE: dict[tuple[int, str, int], tuple[float, list[ArtifactView], int, bool, list[str]]] = {}
DEFAULT_DAYS = 30
MAX_DAYS = 365
#: 预览上限（§15.11.3）。文本读这么多就截断；图片要整份转 data URL，给到 2 MB——
#: 截图常见 300–800 KB，256 KB 会把大部分截图挡在门外（2026-10-22 实测）。
PREVIEW_TEXT_MAX_BYTES = 256 * 1024
PREVIEW_IMAGE_MAX_BYTES = 2 * 1024 * 1024
PREVIEW_TEXT_LIMIT = 20_000

#: 已知二进制格式（2026-10-23）：按**扩展名**判定，不做内容嗅探。
#: 这些文件按 UTF-8 解码出来必然是乱码，与其展示乱码不如明确说不支持。
#: 图片不在其中——它们走魔数判定，能拿到 media_type 正常显示。
BINARY_EXTS = frozenset({
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".pdf", ".zip", ".7z", ".rar", ".tar", ".gz",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc",
    ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".avi",
    ".sqlite", ".db",
})

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"})
#: 内部执行单元：它们的产物属于父会话，单独列出只会制造噪声。
INTERNAL_KINDS = frozenset({"subagent", "heartbeat"})

_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)")
_URL = re.compile(r"https?://[^\s)\]\"'<>，。；、]+")
_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s)\]\"'<>，。；、]+")
#: 2026-10-22 修复。原写法 `(?:[^...]+/)+` 的段内允许 `/`，与末尾的 `+` 构成歧义，
#: 在"带斜杠但没有扩展名"的文本上指数级回溯——实测 140 轮正文因此跑 44 秒。
#: 现在有两个约束让它在最坏输入下也不爆：
#: **每段排除 `/`**（分段确定）+ **段数上限 8、段长上限 80**（每个起点最多比 640 个字符）。
_POSIX_PATH = re.compile(
    r"(?<![\w./])(?:/[^\s)\]\"'<>，。；/]{1,80}){2,8}\.[A-Za-z0-9]{1,8}"
)

ArtifactKind = Literal["image", "file", "link"]


def _moment(value: str | None) -> datetime | None:
    """ISO 字符串 → 带时区的 datetime。解析不了就返回 None（按"不知道时间"处理，不丢数据）。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _kind_of(location: str, hint: ArtifactKind | None = None) -> ArtifactKind:
    if hint is not None:
        return hint
    if location.lower().startswith(("http://", "https://")):
        return "link"
    return "image" if Path(location).suffix.lower() in IMAGE_EXTS else "file"


def _normalize(raw: str, workspace_root: str) -> tuple[str, bool]:
    """绝对路径在工作区内 → 相对路径；工作区外 → 原样保留并标 external（§15.8 脱敏要求）。"""
    candidate = Path(raw)
    if not candidate.is_absolute():
        return raw.replace("\\", "/"), False
    try:
        return candidate.resolve().relative_to(Path(workspace_root).resolve()).as_posix(), False
    except (ValueError, OSError):
        return candidate.as_posix(), True


def _text_entries(answer: str, workspace_root: str) -> list[tuple[ArtifactKind, str, str, bool]]:
    """从一段正文里抽产物：先图片、再 URL、最后路径。返回 (kind, location, 标题, external)。"""
    found: list[tuple[ArtifactKind, str, str, bool]] = []
    for match in _MD_IMAGE.finditer(answer):
        location, external = _normalize(match.group(2), workspace_root)
        found.append(("image", location, (match.group(1) or Path(location).name)[:120], external))
    for match in _URL.finditer(answer):
        found.append(("link", match.group(0), match.group(0)[:120], False))
    for pattern in (_WIN_PATH, _POSIX_PATH):
        for match in pattern.finditer(answer):
            location, external = _normalize(match.group(0), workspace_root)
            found.append((_kind_of(location), location, Path(location).name[:120], external))
    return found


def _attachment_entries(
    service: ApplicationService, titles: dict[str, str], limit: int
) -> tuple[list[tuple[str, Path]], bool]:
    """枚举附件目录，只保留当前扫描范围内会话的文件。"""
    store = service._attachments
    if store is None or not store.root.is_dir():
        return [], False
    found: list[tuple[str, Path]] = []
    for path in itertools.islice(store.root.glob("*/*/*/*/*"), limit):
        if not path.is_file():
            continue
        session_id = path.parent.name
        if session_id in titles:
            found.append((session_id, path))
    return found, len(found) >= limit


def _scan(
    service: ApplicationService,
    *,
    workspace_id: str | None = None,
    days: int = DEFAULT_DAYS,
) -> tuple[list[ArtifactView], int, bool, list[str]]:
    root_path = service._path_for_workspace_id(workspace_id) if workspace_id else None
    sessions = service._sqlite_access(
        lambda: service._sessions.list_sessions(workspace_root=root_path)
        + service._sessions.list_sessions(workspace_root=root_path, archived=True)
    )
    sessions.sort(key=lambda item: (item.updated_at, item.created_at), reverse=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, MAX_DAYS)))
    usable: list[Session] = []
    for session in sessions:
        if session.kind in INTERNAL_KINDS:
            continue
        moment = _moment(session.updated_at) or _moment(session.created_at)
        if moment is not None and moment < cutoff:
            continue
        usable.append(session)

    notices: list[str] = []
    incomplete = False
    if len(usable) > SESSION_SCAN_LIMIT:
        usable = usable[:SESSION_SCAN_LIMIT]
        incomplete = True
        notices.append(f"只扫描了最近 {SESSION_SCAN_LIMIT} 个会话，更早的产物没有列出。")

    titles = {session.session_id: session.title for session in usable}
    by_id = {session.session_id: session for session in usable}
    collected: dict[tuple[str, str, str], ArtifactView] = {}
    #: 工作区路径 → 稳定标识。每条产物都要带上它，前端预览时才能解析到正确的工作区根。
    workspace_ids: dict[str, str] = {}

    def workspace_id_of(root: str) -> str | None:
        if root not in workspace_ids:
            try:
                workspace_ids[root] = service._ref_for(Path(root)).workspace_id
            except Exception:  # noqa: BLE001 — 映射失败不该让整页产物读不出来
                return None
        return workspace_ids[root]

    def add(session: Session, entry_kind: ArtifactKind, location: str, title: str,
            source: Literal["attachment", "file_change", "message"], external: bool = False,
            created_at: str | None = None,
            scope: Literal["workspace", "attachments"] = "workspace") -> None:
        location = location.strip()
        if not location:
            return
        key = (entry_kind, session.session_id, location.casefold())
        if key in collected:
            return
        collected[key] = ArtifactView(
            artifact_id=f"{entry_kind}:{session.session_id}:{abs(hash(key)) & 0xFFFFFFFF:08x}",
            kind=entry_kind,
            title=proj.sanitize_text(title or location, service._secrets)[:120],
            location=proj.sanitize_text(location, service._secrets),
            external=external,
            source=source,
            scope=scope,
            session_id=session.session_id,
            session_title=proj.sanitize_text(session.title, service._secrets),
            workspace_id=workspace_id_of(session.workspace_root),
            created_at=created_at or session.updated_at,
        )

    turn_budget = TURN_TEXT_LIMIT
    op_budget = OP_SCAN_LIMIT
    # 正文：**一次批量 SQL** 取代逐会话查询（2026-10-22）。实测 68 个会话的首轮扫描
    # 从 44 秒降到个位数秒——慢的根源是把整份 answer_text 读进内存读了 68 遍。
    try:
        text_rows = service._sqlite_access(
            lambda: service._conversations.turn_texts(
                [session.session_id for session in usable])) if usable else []
    except Exception:  # noqa: BLE001 — 读失败不能冒充"没有产物"
        text_rows = []
        incomplete = True
        notices.append("会话正文读取失败，产物可能不完整。")

    for session_id, question, answer, submitted_at in text_rows:
        if turn_budget <= 0:
            incomplete = True
            notices.append(f"正文只扫描了前 {TURN_TEXT_LIMIT} 轮，更早的消息没有列出。")
            break
        session = by_id.get(session_id)
        if session is None:
            continue
        text = f"{question}\n{answer}"[:TEXT_LIMIT]
        for entry_kind, location, title, external in _text_entries(text, session.workspace_root):
            add(session, entry_kind, location, title, "message", external=external,
                created_at=submitted_at)
        turn_budget -= 1

    # 文件变更仍逐会话取：每次是一个很小的索引查询，不是瓶颈。
    for session in usable:
        if op_budget <= 0:
            incomplete = True
            notices.append(f"文件变更只扫描了前 {OP_SCAN_LIMIT} 条，更早的没有列出。")
            break
        ledger = service._change_ledger(session)
        ops = ledger.ops() if ledger is not None else []
        for op in ops[:op_budget]:
            if op.kind not in {"create", "modify"}:
                continue
            location, external = _normalize(op.path, session.workspace_root)
            add(session, _kind_of(location), location, Path(location).name,
                "file_change", external=external, created_at=op.created_at)
        op_budget -= len(ops)

    attachment_files, attachments_truncated = _attachment_entries(service, titles, FILE_SCAN_LIMIT)
    if attachments_truncated:
        incomplete = True
        notices.append(f"附件目录只扫描了前 {FILE_SCAN_LIMIT} 个文件，更早的没有列出。")
    for session_id, path in attachment_files:
        session = by_id.get(session_id)
        if session is None:
            continue
        store = service._attachments
        location = path.relative_to(store.root).as_posix() if store is not None else path.name
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        add(session, _kind_of(path.name, "image"), location, path.name, "attachment",
            created_at=stamp, scope="attachments")

    entries = list(collected.values())
    entries.sort(key=lambda row: (row.created_at or "", row.artifact_id), reverse=True)
    return entries, len(usable), incomplete, notices


def artifacts(
    service: ApplicationService,
    *,
    workspace_id: str | None = None,
    kind: ArtifactKind | None = None,
    days: int = DEFAULT_DAYS,
    offset: int = 0,
    limit: int = 50,
    refresh: bool = False,
) -> ArtifactsPageView:
    """产物汇总：**整页只扫一次**，分页与类型过滤都在内存里做（§15.11.1）。

    以前每次翻页都重扫 200 个会话的全部轮次正文，第二页要等好几秒——
    用户看到的就是"第二页压根出不来"（2026-10-22 实机反馈）。
    现在扫描结果缓存 60 秒：翻页、切类型立即响应，新增产物最多延迟一分钟出现。
    """
    normalized_days = max(1, min(days, MAX_DAYS))
    key = (id(service), workspace_id or "", normalized_days)
    moment = monotonic()
    cached = None if refresh else _CACHE.get(key)
    if cached is not None and moment - cached[0] < CACHE_TTL_SECONDS:
        _, entries, scanned, incomplete, notices = cached
    else:
        entries, scanned, incomplete, notices = _scan(
            service, workspace_id=workspace_id, days=normalized_days
        )
        _CACHE[key] = (moment, entries, scanned, incomplete, notices)
    filtered = entries if kind is None else [row for row in entries if row.kind == kind]
    return ArtifactsPageView(
        entries=filtered[offset: offset + limit],
        total=len(filtered),
        offset=offset,
        limit=limit,
        scanned_sessions=scanned,
        incomplete=incomplete,
        notices=notices,
    )


def preview(service: ApplicationService, *, path: str, workspace_id: str | None = None,
            scope: str = "workspace") -> ArtifactPreviewView:
    """预览一条产物的内容（§15.11.3）。

    三条硬边界，缺一条这个接口就会变成"任意文件读取"：

    1. **只读工作区内**：`resolve()` 之后必须仍在工作区根之下（防 `../` 与符号链接逃逸）。
    2. **有上限**：文本超过 256 KB 截断返回，图片超过 2 MB 只回元信息。
    3. **不碰外链**：`http(s)` 交给浏览器窗口打开，服务端不做请求（否则就是 SSRF）。
    """
    if path.lower().startswith(("http://", "https://")):
        raise ValueError("外链产物请直接在浏览器中打开。")
    if scope == "attachments":
        # 附件条目的路径相对**附件根**，不是工作区根（2026-10-22）。
        store = service._attachments
        if store is None:
            raise LookupError("附件存储不可用。")
        root = Path(store.root).resolve()
    else:
        root = Path(service._path_for_workspace_id(workspace_id) if workspace_id
                    else service._workspace_root).resolve()
    candidate = Path(path)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError("只能预览工作区内的文件。") from None
    if not target.is_file():
        raise LookupError("文件不存在或已被删除。")
    size = target.stat().st_size
    with target.open("rb") as handle:
        head = handle.read(32)
    media = detect_image_media_type(head)
    if media is not None:
        # 图片要整份转 data URL，超过上限就只回大小——超大 base64 会把页面拖死。
        if size > PREVIEW_IMAGE_MAX_BYTES:
            return ArtifactPreviewView(kind="too_large", size_bytes=size)
        data = target.read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        return ArtifactPreviewView(kind="image", url=f"data:{media};base64,{encoded}", size_bytes=size)
    # **二进制不能当文本读**（2026-10-23 实机反馈）：docx 是 zip 容器（魔数 `PK\x03\x04`），
    # 之前直接 `decode("utf-8", errors="replace")`，界面上就是一屏乱码。
    # 这里按扩展名先拦一道，明确告诉用户"这是二进制，不支持预览"——
    # 比给一屏乱码诚实，也不会让人以为文件坏了。
    # 判定顺序保证不误伤文本：魔数图片在前，已知二进制扩展名其次，其余都按文本。
    if target.suffix.lower() in BINARY_EXTS:
        return ArtifactPreviewView(kind="binary", size_bytes=size,
                                   suffix=target.suffix.lower().lstrip("."))
    # 文本**不因超限整份拒绝**（2026-10-22 实机反馈）：读前 N 字节照常返回，
    # 只把 truncated 置起来。以前这里回 too_large，用户点开 package-lock.json 只看到一句"超限"，
    # 等于什么都没给。
    with target.open("rb") as handle:
        chunk = handle.read(PREVIEW_TEXT_MAX_BYTES)
    text = proj.sanitize_text(chunk.decode("utf-8", errors="replace"), service._secrets)
    truncated = size > PREVIEW_TEXT_MAX_BYTES or len(text) > PREVIEW_TEXT_LIMIT
    return ArtifactPreviewView(kind="text", text=text[:PREVIEW_TEXT_LIMIT],
                               size_bytes=size, truncated=truncated)




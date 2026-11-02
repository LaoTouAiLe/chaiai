"""有界、只读的数据中台投影。浏览不迁移库、不更新访问时间、不调用模型。"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from contextlib import closing
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from local_agent.application import projection as proj
from local_agent.application.data_center_models import (
    MemoryEntryView,
    MemoryPageView,
    MemoryResourceView,
    UsageBucketView,
    UsageCallView,
    UsagePageView,
    UsageTotalsView,
)
from local_agent.application.models import cache_hit_ratio
from local_agent.model_settings.preferences import apply_runtime_preferences
from local_agent.scope import MemoryScope, normalize_path, project_id_for, visible_scope_keys

if TYPE_CHECKING:
    from local_agent.application.service import ApplicationService
    from local_agent.config import AppConfig

MEMORY_SCAN_LIMIT = 10000
SESSION_SCAN_LIMIT = 200
CHECKPOINT_SCAN_LIMIT = 2000
CALL_SCAN_LIMIT = 100000
CONTENT_LIMIT = 20000


def _text(service: ApplicationService, value: Any) -> str:
    return proj.sanitize_text(str(value), service._secrets)


def _entry(service: ApplicationService, **values: Any) -> MemoryEntryView:
    for key in ("id", "title", "content", "scope", "status", "source", "created_at", "updated_at", "expires_at"):
        if values.get(key) is not None:
            values[key] = _text(service, values[key])
    values["truncated"] = len(values["content"]) > CONTENT_LIMIT
    values["content"] = values["content"][:CONTENT_LIMIT]
    return MemoryEntryView(**values)


def _status(status: str, expires: Any) -> str:
    if status != "active" or not expires:
        return status
    try:
        moment = datetime.fromisoformat(str(expires))
        if moment.tzinfo is None or moment <= datetime.now(timezone.utc):
            return "expired"
    except ValueError:
        return "expired"
    return status


def _long_records(service: ApplicationService, path: Path, scopes: Sequence[tuple[MemoryScope, str]]) -> tuple[list[MemoryEntryView], bool]:
    # mode=ro 不创建文件、不迁移，不复用会写库的 open_store。
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
        connection.row_factory = sqlite3.Row
        where = " OR ".join("(e.scope=? AND e.scope_key=?)" for _ in scopes)
        params: list[Any] = [value for pair in scopes for value in pair]
        facts = connection.execute(
            "SELECT f.id,e.name,e.scope,f.key,f.value_json,f.status,f.source,f.created_at,"
            "f.updated_at,f.expires_at,f.confidence,f.importance FROM facts f "
            f"JOIN entities e ON e.id=f.entity_id WHERE ({where}) "
            "ORDER BY f.updated_at DESC,f.id DESC LIMIT ?", [*params, MEMORY_SCAN_LIMIT + 1],
        ).fetchall()
        projects = [key for scope, key in scopes if scope == "project"]
        conclusions = connection.execute(
            "SELECT id,task_title,conclusion,evidence_json,confidence,created_at,updated_at "
            f"FROM task_conclusions WHERE project_scope IN ({','.join('?' for _ in projects)}) "
            "ORDER BY updated_at DESC,id DESC LIMIT ?", [*projects, MEMORY_SCAN_LIMIT + 1],
        ).fetchall() if projects else []
    entries = [_entry(service, id=row["id"], kind="fact", title=f'{row["name"]} · {row["key"]}',
                      content=row["value_json"], scope=row["scope"],
                      status=_status(row["status"], row["expires_at"]), source=row["source"],
                      created_at=row["created_at"], updated_at=row["updated_at"], expires_at=row["expires_at"],
                      confidence=row["confidence"], importance=row["importance"])
               for row in facts[:MEMORY_SCAN_LIMIT]]
    entries.extend(_entry(service, id=row["id"], kind="conclusion", title=row["task_title"],
                          content=row["conclusion"] + "\n\n证据：" + row["evidence_json"], scope="project",
                          status="active", confidence=row["confidence"], created_at=row["created_at"],
                          updated_at=row["updated_at"])
                   for row in conclusions[:MEMORY_SCAN_LIMIT])
    return entries, len(facts) > MEMORY_SCAN_LIMIT or len(conclusions) > MEMORY_SCAN_LIMIT


def _medium_records(service: ApplicationService, path: Path, collection_name: str,
                    scopes: Sequence[tuple[MemoryScope, str]]) -> tuple[list[MemoryEntryView], bool]:
    # 仅打开已有 collection，embedding_function=None；get 不请求向量或执行相似检索。
    from local_agent.memory.medium_term import _import_chromadb

    client = _import_chromadb().PersistentClient(path=str(path))
    collection = client.get_collection(name=collection_name, embedding_function=None)
    clauses = [{"$and": [{"scope": {"$eq": scope}}, {"scope_key": {"$eq": key}}]} for scope, key in scopes]
    where = clauses[0] if len(clauses) == 1 else {"$or": clauses}
    entries: list[MemoryEntryView] = []
    for offset in range(0, MEMORY_SCAN_LIMIT + 1, 500):
        result = collection.get(where=where, limit=min(500, MEMORY_SCAN_LIMIT + 1 - offset),
                                offset=offset, include=["documents", "metadatas"])
        ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        for index, memory_id in enumerate(ids):
            metadata = metadatas[index] or {}
            # 双重检查作用域，缺字段或外部损坏数据不作为本工作区记忆展示。
            if (metadata.get("scope"), metadata.get("scope_key")) not in scopes:
                continue
            content = documents[index] or ""
            entries.append(_entry(service, id=memory_id, kind="medium", title=content.splitlines()[0][:100] if content else "中期记忆",
                                  content=content, scope=metadata.get("scope"),
                                  status=_status(str(metadata.get("status", "unknown")), metadata.get("expires_at")),
                                  source=metadata.get("source_event_id") or metadata.get("source_thread_id"),
                                  created_at=metadata.get("created_at"), expires_at=metadata.get("expires_at"),
                                  confidence=metadata.get("confidence"), importance=metadata.get("importance")))
        if len(ids) < min(500, MEMORY_SCAN_LIMIT + 1 - offset):
            return entries, False
    return entries[:MEMORY_SCAN_LIMIT], True


def memories(service: ApplicationService, *, workspace_id: str | None = None,
             kind: str = "all", query: str = "", status: str = "all", offset: int = 0, limit: int = 30) -> MemoryPageView:
    root = service._path_for_workspace_id(workspace_id) if workspace_id else service._workspace_root
    config = apply_runtime_preferences(service._app_config, service.preferences_view().values)
    # Web 运行上下文未注入 user_id，与 builder 的默认逻辑用户保持一致。
    scopes = visible_scope_keys(user_id=None, project_id=project_id_for(root), workspace_root=root)
    entries: list[MemoryEntryView] = []
    resources: list[MemoryResourceView] = []
    incomplete = False
    for resource_kind, memory_config in (("medium", config.memory.medium_term), ("long", config.memory.long_term)):
        path = Path(memory_config.path)
        exists = (path / "chroma.sqlite3").is_file() if resource_kind == "medium" else path.is_file()
        state: Any = "missing"
        note = "尚无已保存的记忆库。"
        if exists:
            try:
                if resource_kind == "medium":
                    rows, bounded = _medium_records(service, path, config.memory.medium_term.collection, scopes)
                else:
                    rows, bounded = _long_records(service, path, scopes)
                entries.extend(rows)
                incomplete |= bounded
                state, note = "available", "仅查看已保存数据，不触发召回或更新访问时间。"
            except Exception:  # noqa: BLE001 — 不向页面转发 SDK、路径或原始异常
                state, note = "unavailable", "记忆库暂时无法读取，请检查依赖与库结构；未修改数据。"
                incomplete = True
        resource_label: Literal["medium", "long"] = "medium" if resource_kind == "medium" else "long"
        resources.append(MemoryResourceView(kind=resource_label, enabled=memory_config.enabled, state=state, note=note))
    counts: dict[str, int] = dict(Counter(str(row.kind) for row in entries))
    needle = query.strip().casefold()
    entries = [row for row in entries if (kind == "all" or row.kind == kind)
               and (status == "all" or row.status == status)
               and (not needle or needle in (row.title + "\n" + row.content).casefold())]
    entries.sort(key=lambda row: (row.updated_at or row.created_at or "", row.id), reverse=True)
    notices: list[str] = []
    if incomplete:
        notices.append("读取未完整覆盖：部分库不可读，或单类记忆超过 10,000 条；数量与搜索仅覆盖已读记录。")
    notices.extend(_maintenance_notices(service, config))
    return MemoryPageView(entries=entries[offset:offset + limit], resources=resources, counts=counts,
                          total=len(entries), offset=offset, limit=limit, incomplete=incomplete,
                          notices=notices)


def _maintenance_notices(service: ApplicationService, config: AppConfig) -> list[str]:
    """记忆维护任务的状态行（2026-11-02，架构 §10.3）。

    **只报告，不触发**：打开记忆页不该顺手改库（那会让"看一眼"变成一次写操作）。
    失败原因**不进页面**——异常文本里可能带绝对路径与库结构细节，页面只说明"已记入日志、
    下一轮重试"，与"不向页面转发 SDK、路径或原始异常"的既有口径一致。
    """
    if not config.memory.long_term.enabled:
        return []
    maintenance = getattr(service, "memory_maintenance", None)
    if maintenance is None:
        # 没挂调度器（未走 lifespan 的调用方/测试）：不编一条"尚未运行"——那是另一个事实。
        return []
    status = maintenance.status()
    if status is None:
        return ["记忆维护尚未运行：服务启动约 1 分钟后开始，之后每 6 小时一次。本页只读，不会触发维护。"]
    moment = status.finished_at.astimezone().strftime("%Y-%m-%d %H:%M")
    if status.error:
        return [f"记忆维护上次失败（{moment}）：原因已记入服务端日志，下一轮会自动重试。"]
    report = status.report
    if report is None:
        return []
    if report.total == 0:
        return [f"记忆维护上次运行（{moment}）：本轮无需淘汰。"]
    summary = (
        f"记忆维护上次运行（{moment}）：显式过期 {report.expired_facts} 条、"
        f"自动老化 {report.aged_facts} 条、中期清理 {report.deleted_medium} 条。"
    )
    notes = [summary]
    consolidation = getattr(status, "consolidation", None)
    if consolidation is not None:
        notes.append(
            f"记忆巩固：新增长期事实 {consolidation.promoted} 条"
            f"（丢弃 {consolidation.skipped} 条，原因见服务端日志）。"
        )
    return notes


def _sum(values: Iterable[int | None]) -> int | None:
    items = list(values)
    return None if not items or any(item is None for item in items) else sum(item for item in items if item is not None)


def totals(entries: Sequence[UsageCallView]) -> UsageTotalsView:
    calls = [row.call for row in entries]
    inputs = [None if call.actual_uncached_input_tokens is None or call.actual_cache_read_tokens is None
              else call.actual_uncached_input_tokens + call.actual_cache_read_tokens + (call.actual_cache_write_tokens or 0)
              for call in calls]
    uncached = _sum(call.actual_uncached_input_tokens for call in calls)
    read = _sum(call.actual_cache_read_tokens for call in calls)
    reported = all(call.cache_metrics_reported for call in calls)
    return UsageTotalsView(calls=len(calls), failed_calls=sum(call.status in {"error", "aborted"} for call in calls),
                           missing_usage_calls=sum(value is None or call.actual_output_tokens is None for value, call in zip(inputs, calls)),
                           input_tokens=_sum(inputs), output_tokens=_sum(call.actual_output_tokens for call in calls),
                           uncached_input_tokens=uncached, cache_read_tokens=read,
                           cache_hit_ratio=cache_hit_ratio(uncached, read, _sum(call.actual_cache_write_tokens or 0 for call in calls)) if reported else None)


def usage(service: ApplicationService, *, workspace_id: str | None = None, model: str = "",
          since: datetime | None = None, until: datetime | None = None, offset: int = 0, limit: int = 30) -> UsagePageView:
    """用量页（§15.6）：**以用量账为准**（v18），老会话读穿回填一次。

    2026-11-02 之前这里只有一条路：逐会话反序列化 Checkpoint，从 State 里捡
    `model_call_records`。那条路的上限是 200 会话 / 2,000 检查点，撞上就是"统计不完整"，
    而且每次打开页面都要付一遍代价。现在记录在调用发生时（每轮 sync）就追加进
    `model_call_usage`，统计是一句 SQL。

    **老会话仍在**：升级前产生的调用只存在于 Checkpoint 里，账本里没有它们的行。所以对
    "账本里没有任何记录的会话"保留一次扫描，并把扫到的记录写回账本（读穿）——下次打开就快。
    这条兜底会把 `incomplete` 置真并给出提示，而不是让旧历史静默归零。
    """
    root = service._path_for_workspace_id(workspace_id) if workspace_id else None
    normalized = None if root is None else normalize_path(root)
    sessions = service._sqlite_access(lambda: service._sessions.list_sessions(workspace_root=root)
                                     + service._sessions.list_sessions(workspace_root=root, archived=True))
    sessions.sort(key=lambda session: (session.updated_at, session.created_at), reverse=True)
    by_id = {session.session_id: session for session in sessions}
    incomplete = len(sessions) > SESSION_SCAN_LIMIT
    notices: list[str] = []

    # 1) 账本（真源）。SQL 已经把工作区 / 时间窗 / 模型名筛过一遍，下面仍按同一口径过滤——
    #    读穿来的记录走的是 Python 侧，两处必须用同一套判据，否则新旧数据会走出不同的结果。
    stored = service._sqlite_access(lambda: service._sessions.usage_rows(
        workspace_root=normalized, since=since, until=until, model=model))
    collected: dict[str, tuple[str, str, Any]] = {
        row.record.model_call_id: (row.session_id, row.workspace_root, row.record) for row in stored
    }
    known = service._sqlite_access(lambda: service._sessions.usage_sessions(workspace_root=normalized))

    # 2) 读穿：升级前的老会话没有账本行。
    scanned, missing, incomplete = _backfill_legacy(
        service, sessions, known, collected, incomplete=incomplete)

    # 3) 投影 + 过滤 + 排序（与 v17 之前完全一样的口径）。
    needle = model.strip().casefold()
    entries: list[UsageCallView] = []
    for session_id, workspace_root, record in collected.values():
        if needle and needle not in record.model_id.casefold():
            continue
        if since is not None and record.started_at < since:
            continue
        if until is not None and record.started_at >= until:
            continue
        call = proj.project_model_call(record)
        for field in ("purpose", "provider", "model_id", "connection_id", "config_version"):
            setattr(call, field, _text(service, getattr(call, field)))
        known_session = by_id.get(session_id)
        entries.append(UsageCallView(
            session_id=session_id,
            # 会话被删掉之后账本行仍在（它是"这台机器花过多少"，不随会话消失）：
            # 标题退化成空串，页面按 `session_id` 展示。
            session_title="" if known_session is None else _text(service, known_session.title),
            workspace_id=service._ref_for(Path(workspace_root)).workspace_id,
            call=call,
        ))
    entries.sort(key=lambda row: (row.call.started_at, row.call.model_call_id), reverse=True)

    def buckets(key: Any) -> list[UsageBucketView]:
        groups: dict[str, list[UsageCallView]] = defaultdict(list)
        for row in entries:
            groups[key(row)].append(row)
        return [UsageBucketView(label=label, totals=totals(rows)) for label, rows in sorted(groups.items())]
    if incomplete:
        notices.append("统计未完整覆盖：部分检查点不可读，或达到 200 会话 / 2,000 检查点 / 100,000 次调用记录读取的上限。"
                       "已读到的记录已写进用量账，下次打开会更快。")
    if scanned:
        notices.append(f"{scanned} 个升级前的会话没有用量账，已扫检查点回填；之后不再重复扫描。")
    if missing:
        notices.append(f"{missing} 个已查看会话没有可用调用记录，可能尚未提问或来自未记录用量的旧版本；不计为零消耗。")
    return UsagePageView(totals=totals(entries), daily=buckets(lambda row: row.call.started_at.astimezone(timezone.utc).date().isoformat()),
                         models=buckets(lambda row: row.call.model_id), purposes=buckets(lambda row: row.call.purpose),
                         entries=entries[offset:offset + limit], total=len(entries), offset=offset, limit=limit,
                         scanned_sessions=scanned, missing_record_sessions=missing,
                         incomplete=incomplete, notices=notices, generated_at=datetime.now(timezone.utc))


def _backfill_legacy(
    service: ApplicationService,
    sessions: Sequence[Any],
    known: set[str],
    collected: dict[str, tuple[str, str, Any]],
    *,
    incomplete: bool,
) -> tuple[int, int, bool]:
    """对"账本里没有记录的会话"扫一遍 Checkpoint，把结果补进账本（读穿，只做一次）。

    为什么还需要这条路：升级前产生的调用只存在于 Checkpoint 里。没有它，升级后用量页会**静默归零**
    ——比"慢"更糟。写失败不影响本次展示（展示用内存里这份），下次再看还有机会补上。
    """
    scanned = 0
    missing = 0
    reads = 0
    inspected_calls = 0
    for session in (item for item in sessions[:SESSION_SCAN_LIMIT] if item.session_id not in known):
        if reads >= CHECKPOINT_SCAN_LIMIT or inspected_calls >= CALL_SCAN_LIMIT:
            incomplete = True
            break
        found: dict[str, Any] = {}
        scanned += 1
        try:
            # 累积记录沿路径继承，各分支末端的并集覆盖共享历史与分叉；不扫描每步快照。
            branches = service._sqlite_access(partial(service._conversations.branches, session.session_id))
            ids = {branch.checkpoint_id for branch in branches if branch.checkpoint_id is not None}
            latest = service._view_of(service._graph.get_state(
                {"configurable": {"thread_id": session.thread_id}}))
            reads += 1
            inspected_calls += _collect_records(latest.values, found, CALL_SCAN_LIMIT - inspected_calls)
            for checkpoint_id in sorted(ids):
                if checkpoint_id == latest.checkpoint_id:
                    continue
                if reads >= CHECKPOINT_SCAN_LIMIT or inspected_calls >= CALL_SCAN_LIMIT:
                    incomplete = True
                    break
                view = service._read_checkpoint(session, checkpoint_id)
                reads += 1
                if view is None:
                    incomplete = True
                else:
                    inspected_calls += _collect_records(view.values, found, CALL_SCAN_LIMIT - inspected_calls)
            if inspected_calls >= CALL_SCAN_LIMIT:
                incomplete = True
        except Exception:  # noqa: BLE001 — 单个会话读取失败不能冒充零消耗
            incomplete = True
        if not found:
            missing += 1
            continue
        try:
            service._sqlite_access(partial(
                service._sessions.append_usage,
                session.session_id, session.workspace_root, list(found.values()),
            ))
        except Exception:  # noqa: BLE001
            incomplete = True
        for key, record in found.items():
            collected.setdefault(key, (session.session_id, session.workspace_root, record))
    return scanned, missing, incomplete


def _collect_records(values: Any, collected: dict[str, Any], budget: int) -> int:
    """从一个检查点的 State 里捡出调用记录（读穿用）。键是 ``model_call_id``，天然去重。"""
    records = values.get("model_call_records") or []
    for record in records[:budget]:
        collected.setdefault(str(getattr(record, "model_call_id", "")), record)
    return min(len(records), budget)

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
from local_agent.scope import MemoryScope, project_id_for, visible_scope_keys

if TYPE_CHECKING:
    from local_agent.application.service import ApplicationService
    from local_agent.persistence.session_store import Session

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
    return MemoryPageView(entries=entries[offset:offset + limit], resources=resources, counts=counts,
                          total=len(entries), offset=offset, limit=limit, incomplete=incomplete,
                          notices=["读取未完整覆盖：部分库不可读，或单类记忆超过 10,000 条；数量与搜索仅覆盖已读记录。"] if incomplete else [])


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
    root = service._path_for_workspace_id(workspace_id) if workspace_id else None
    sessions = service._sqlite_access(lambda: service._sessions.list_sessions(workspace_root=root)
                                     + service._sessions.list_sessions(workspace_root=root, archived=True))
    sessions.sort(key=lambda session: (session.updated_at, session.created_at), reverse=True)
    incomplete = len(sessions) > SESSION_SCAN_LIMIT
    collected: dict[tuple[str, str], UsageCallView] = {}
    missing = 0
    reads = 0
    scanned = 0
    inspected_calls = 0
    notices: list[str] = []
    for session in sessions[:SESSION_SCAN_LIMIT]:
        if reads >= CHECKPOINT_SCAN_LIMIT or inspected_calls >= CALL_SCAN_LIMIT:
            incomplete = True
            break
        before = len(collected)
        scanned += 1
        try:
            # 累积记录沿路径继承，各分支末端的并集覆盖共享历史与分叉；不扫描每步快照。
            branches = service._sqlite_access(partial(service._conversations.branches, session.session_id))
            ids = {branch.checkpoint_id for branch in branches if branch.checkpoint_id is not None}
            latest = service._view_of(service._graph.get_state({"configurable": {"thread_id": session.thread_id}}))
            reads += 1
            inspected_calls += _collect_calls(service, session, latest.values, collected, CALL_SCAN_LIMIT - inspected_calls)
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
                    inspected_calls += _collect_calls(service, session, view.values, collected, CALL_SCAN_LIMIT - inspected_calls)
            if inspected_calls >= CALL_SCAN_LIMIT:
                incomplete = True
        except Exception:  # noqa: BLE001 — 单个会话读取失败不能冒充零消耗
            incomplete = True
        missing += len(collected) == before
    needle = model.strip().casefold()
    entries = [row for row in collected.values() if (not needle or needle in row.call.model_id.casefold())
               and (since is None or row.call.started_at >= since) and (until is None or row.call.started_at < until)]
    entries.sort(key=lambda row: (row.call.started_at, row.call.model_call_id), reverse=True)
    def buckets(key: Any) -> list[UsageBucketView]:
        groups: dict[str, list[UsageCallView]] = defaultdict(list)
        for row in entries:
            groups[key(row)].append(row)
        return [UsageBucketView(label=label, totals=totals(rows)) for label, rows in sorted(groups.items())]
    if incomplete:
        notices.append("统计未完整覆盖：部分检查点不可读，或达到 200 会话 / 2,000 检查点 / 100,000 次调用记录读取的上限。")
    if missing:
        notices.append(f"{missing} 个已查看会话没有可用调用记录，可能尚未提问或来自未记录用量的旧版本；不计为零消耗。")
    return UsagePageView(totals=totals(entries), daily=buckets(lambda row: row.call.started_at.astimezone(timezone.utc).date().isoformat()),
                         models=buckets(lambda row: row.call.model_id), purposes=buckets(lambda row: row.call.purpose),
                         entries=entries[offset:offset + limit], total=len(entries), offset=offset, limit=limit,
                         scanned_sessions=scanned, missing_record_sessions=missing,
                         incomplete=incomplete, notices=notices, generated_at=datetime.now(timezone.utc))


def _collect_calls(service: ApplicationService, session: Session, values: Any,
                   collected: dict[tuple[str, str], UsageCallView], budget: int) -> int:
    records = values.get("model_call_records") or []
    # 每个检查点只解析一次名称与工作区，避免逐调用读会话表；处理后立即释放快照。
    title = _text(service, session.title)
    workspace_id = service._ref_for(Path(session.workspace_root)).workspace_id
    for record in records[:budget]:
        call = proj.project_model_call(record)
        for field in ("purpose", "provider", "model_id", "connection_id", "config_version"):
            setattr(call, field, _text(service, getattr(call, field)))
        collected[(session.session_id, call.model_call_id)] = UsageCallView(
            session_id=session.session_id, session_title=title, workspace_id=workspace_id, call=call)
    return min(len(records), budget)

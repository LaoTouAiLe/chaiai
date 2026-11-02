"""模型调用的**用量账**（追加型，2026-11-02；聊天库 v18）。

为什么要有这张表：

用量记录（``llm/usage_recording.ModelCallRecord``）本来只存在 Checkpoint 的 State 里。那是
**执行状态**——要读它必须反序列化整份 State，于是数据中台算一次总账要扫最多 2,000 个检查点，
长历史下既慢，还会撞上限变成"统计不完整"。这张表是它的**投影**：每轮结束把这一轮的调用记录
``INSERT OR IGNORE`` 进来，之后所有统计走 SQL。

三条口径：

1. **追加不改写**：``model_call_id`` 是主键，重放同一批记录不会重复计数（审批恢复、重复 sync 都走这条）。
2. **只增不删**：删除会话时**不删**这里的行——它是"这台机器花过多少"的账，不随会话消失；
   页面上按会话标题展示时，会话没了就显示 id（见 `application/data_center.py`）。
3. **记录与归属分开**：``session_id`` / ``workspace_root`` 是列，不在 :class:`ModelCallRecord` 里——
   记录讲的是"一次调用"，不属于某个会话；同一批记录可能来自子 Agent（归属父轮次）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from local_agent.llm.usage_recording import ModelCallRecord

#: 表名（迁移 v18）。
TABLE = "model_call_usage"

#: 列顺序，与 :func:`_values` 严格一致。
_COLUMNS = (
    "model_call_id", "session_id", "workspace_root", "run_id", "purpose", "connection_id",
    "config_version", "provider", "model_id", "endpoint_fingerprint", "retry_of", "started_at",
    "first_token_at", "finished_at", "status", "estimated_prompt_tokens",
    "actual_uncached_input_tokens", "actual_cache_read_tokens", "actual_cache_write_tokens",
    "actual_output_tokens", "actual_reasoning_tokens", "cache_metrics_reported",
    "model_elapsed_ms", "time_to_first_token_ms", "context_used_tokens", "context_window_tokens",
    "tools_schema_fingerprint", "system_prompt_fingerprint", "history_prefix_fingerprint",
    "dynamic_context_fingerprint",
)

_INSERT_SQL = (
    f"INSERT OR IGNORE INTO {TABLE} ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_COLUMNS))})"
)


@dataclass(frozen=True)
class UsageRow:
    """一行用量账：记录本体 + 它归属的会话与工作区（页面过滤用）。"""

    record: ModelCallRecord
    session_id: str
    workspace_root: str


def _moment(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _values(record: ModelCallRecord, session_id: str, workspace_root: str) -> tuple[object, ...]:
    return (
        record.model_call_id, session_id, workspace_root, record.run_id,
        record.purpose, record.connection_id, record.config_version, record.provider,
        record.model_id, record.endpoint_fingerprint, record.retry_of,
        _moment(record.started_at), _moment(record.first_token_at), _moment(record.finished_at),
        record.status, record.estimated_prompt_tokens,
        record.actual_uncached_input_tokens, record.actual_cache_read_tokens,
        record.actual_cache_write_tokens, record.actual_output_tokens,
        record.actual_reasoning_tokens, int(record.cache_metrics_reported),
        record.model_elapsed_ms, record.time_to_first_token_ms, record.context_used_tokens,
        record.context_window_tokens, record.tools_schema_fingerprint,
        record.system_prompt_fingerprint, record.history_prefix_fingerprint,
        record.dynamic_context_fingerprint,
    )


#: 时间列缺值时用的兜底。**必须带时区**：记录里的时间全部是 UTC-aware，混进一个 naive
#: 的 ``datetime.min`` 会在排序与分桶（`astimezone`）时抛 TypeError。
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _record(row: Sequence[Any]) -> ModelCallRecord:
    """行 → 记录。时间列存的是 ISO 文本，这里还原成 ``datetime``（投影层按它排序与分桶）。"""
    def moment(value: Any) -> datetime | None:
        return None if value is None else datetime.fromisoformat(str(value))

    def optional_int(value: Any) -> int | None:
        return None if value is None else int(value)

    return ModelCallRecord(
        model_call_id=str(row[0]),
        run_id=str(row[3]),
        purpose=str(row[4]),
        connection_id=str(row[5]),
        config_version=str(row[6]),
        provider=str(row[7]),
        model_id=str(row[8]),
        endpoint_fingerprint=str(row[9]),
        retry_of=None if row[10] is None else str(row[10]),
        started_at=moment(row[11]) or _EPOCH,
        first_token_at=moment(row[12]),
        finished_at=moment(row[13]) or _EPOCH,
        status=str(row[14]),  # type: ignore[arg-type]
        estimated_prompt_tokens=int(row[15]),
        actual_uncached_input_tokens=optional_int(row[16]),
        actual_cache_read_tokens=optional_int(row[17]),
        actual_cache_write_tokens=optional_int(row[18]),
        actual_output_tokens=optional_int(row[19]),
        actual_reasoning_tokens=optional_int(row[20]),
        cache_metrics_reported=bool(row[21]),
        model_elapsed_ms=int(row[22]),
        time_to_first_token_ms=optional_int(row[23]),
        context_used_tokens=int(row[24]),
        context_window_tokens=int(row[25]),
        tools_schema_fingerprint=None if row[26] is None else str(row[26]),
        system_prompt_fingerprint=None if row[27] is None else str(row[27]),
        history_prefix_fingerprint=None if row[28] is None else str(row[28]),
        dynamic_context_fingerprint=None if row[29] is None else str(row[29]),
    )


def append_calls(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    workspace_root: str,
    records: Iterable[ModelCallRecord],
) -> int:
    """把一批调用记录写进账本，返回**新增**行数（已存在的按主键忽略）。

    调用方每轮结束都会把这一轮的记录整批交过来（审批恢复、重复 sync 都会重放同一批），
    所以这里必须是幂等的：重复写入既不能报错，也不能把用量算两遍。
    """
    rows = [_values(record, session_id, workspace_root) for record in records]
    if not rows:
        return 0
    before = connection.total_changes
    connection.executemany(_INSERT_SQL, rows)
    return connection.total_changes - before


def rows(
    connection: sqlite3.Connection,
    *,
    workspace_root: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    model: str = "",
) -> list[UsageRow]:
    """按会话（工作区）/ 时间窗 / 模型名筛出账本行，按开始时间倒序。

    ``workspace_root`` 用**规范化后的字符串**比较：它与 ``sessions.workspace_root`` 同一套口径
    （`scope.normalize_path`），否则同一目录的两种写法会把账拆成两份。
    """
    clauses: list[str] = []
    params: list[object] = []
    if workspace_root is not None:
        clauses.append("workspace_root = ?")
        params.append(workspace_root)
    if since is not None:
        clauses.append("started_at >= ?")
        params.append(since.isoformat())
    if until is not None:
        clauses.append("started_at < ?")
        params.append(until.isoformat())
    needle = model.strip().casefold()
    if needle:
        # 模型名大小写不敏感：页面上的过滤框是"帮我找 deepseek"，不该要求用户记住大小写。
        clauses.append("lower(model_id) LIKE ?")
        params.append(f"%{needle}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows_raw = connection.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM {TABLE}{where} ORDER BY started_at DESC",
        params,
    ).fetchall()
    return [
        UsageRow(record=_record(row), session_id=str(row[1]), workspace_root=str(row[2]))
        for row in rows_raw
    ]


def sessions_with_rows(connection: sqlite3.Connection, *, workspace_root: str | None = None) -> set[str]:
    """账本里已经有记录的会话 id。

    用途只有一个：升级前的老会话没有账本行，数据中台要为它们**回填一次**（见
    `application/data_center.py` 的读穿逻辑）——否则升级后用量页面会突然"归零"。
    """
    clause = "" if workspace_root is None else " WHERE workspace_root = ?"
    params: tuple[object, ...] = () if workspace_root is None else (workspace_root,)
    return {
        str(row[0])
        for row in connection.execute(f"SELECT DISTINCT session_id FROM {TABLE}{clause}", params)
    }

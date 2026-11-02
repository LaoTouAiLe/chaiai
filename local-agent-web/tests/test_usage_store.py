"""用量账（v18，2026-11-02）的契约测试。

守住四件事：

1. **幂等**：同一批记录重放（审批恢复、重复 sync）不会把用量算两遍；
2. **字段不失真**：写进去再读回来，投影要用的字段一个不少——少一个就是页面上"未知"；
3. **过滤口径**：工作区 / 时间窗 / 模型名（大小写不敏感）；
4. **会话没了账还在**：删除会话不删账（账是"这台机器花过多少"）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from local_agent.llm.usage_recording import ModelCallRecord
from local_agent.persistence import migrations, usage_store


def _record(
    call_id: str = "run:model:1",
    *,
    started_at: datetime | None = None,
    model_id: str = "deepseek-chat",
    status: str = "ok",
) -> ModelCallRecord:
    moment = started_at or datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    return ModelCallRecord(
        model_call_id=call_id,
        run_id="run",
        purpose="tool_decision",
        connection_id="conn-1",
        config_version="v3",
        provider="deepseek",
        model_id=model_id,
        endpoint_fingerprint="https://api.example.com#deepseek-chat",
        retry_of=None,
        started_at=moment,
        first_token_at=moment + timedelta(milliseconds=120),
        finished_at=moment + timedelta(seconds=2),
        status=status,  # type: ignore[arg-type]
        estimated_prompt_tokens=12_000,
        actual_uncached_input_tokens=1_000,
        actual_cache_read_tokens=11_000,
        actual_cache_write_tokens=0,
        actual_output_tokens=350,
        actual_reasoning_tokens=40,
        cache_metrics_reported=True,
        model_elapsed_ms=2_000,
        time_to_first_token_ms=120,
        context_used_tokens=12_350,
        context_window_tokens=128_000,
        tools_schema_fingerprint="tools-abc",
        system_prompt_fingerprint="sys-abc",
        history_prefix_fingerprint="hist-abc",
        dynamic_context_fingerprint="dyn-abc",
    )


def _connection(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    migrations.apply_migrations(connection)
    return connection


def test_migration_creates_the_table(tmp_path: Path) -> None:
    connection = _connection(tmp_path)

    assert usage_store.TABLE in migrations.table_names(connection)
    assert migrations.current_version(connection) == migrations.CHAT_SCHEMA_VERSION


def test_append_is_idempotent(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    record = _record()

    assert usage_store.append_calls(connection, session_id="s1", workspace_root="E:/w", records=[record]) == 1
    # 审批恢复、重复 sync 都会重放同一批：不能报错，也不能算两遍。
    assert usage_store.append_calls(connection, session_id="s1", workspace_root="E:/w", records=[record]) == 0

    assert len(usage_store.rows(connection)) == 1


def test_round_trip_keeps_every_projected_field(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    original = _record()
    usage_store.append_calls(connection, session_id="s1", workspace_root="E:/w", records=[original])

    restored = usage_store.rows(connection)[0].record

    # 投影要用的字段逐项相等：缺一个，页面上就变成"未知"。
    assert restored == original


def test_filters_by_workspace_model_and_window(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    early = _record("a", started_at=datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc), model_id="DeepSeek-Chat")
    late = _record("b", started_at=datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc), model_id="qwen-max")
    usage_store.append_calls(connection, session_id="s1", workspace_root="E:/one", records=[early])
    usage_store.append_calls(connection, session_id="s2", workspace_root="E:/two", records=[late])

    assert {row.session_id for row in usage_store.rows(connection, workspace_root="E:/one")} == {"s1"}
    # 模型名大小写不敏感：过滤框是"帮我找 deepseek"，不该要求记住大小写。
    assert {row.record.model_call_id for row in usage_store.rows(connection, model="deepseek")} == {"a"}
    assert {row.record.model_call_id for row in usage_store.rows(
        connection, since=datetime(2026, 11, 2, tzinfo=timezone.utc))} == {"b"}
    assert {row.record.model_call_id for row in usage_store.rows(
        connection, until=datetime(2026, 11, 2, tzinfo=timezone.utc))} == {"a"}


def test_rows_are_ordered_newest_first(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    earlier = _record("a", started_at=datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc))
    later = _record("b", started_at=datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc))
    usage_store.append_calls(connection, session_id="s", workspace_root="E:/w", records=[earlier, later])

    assert [row.record.model_call_id for row in usage_store.rows(connection)] == ["b", "a"]


def test_sessions_with_rows_is_per_workspace(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    usage_store.append_calls(connection, session_id="s1", workspace_root="E:/one", records=[_record("a")])
    usage_store.append_calls(connection, session_id="s2", workspace_root="E:/two", records=[_record("b")])

    assert usage_store.sessions_with_rows(connection) == {"s1", "s2"}
    assert usage_store.sessions_with_rows(connection, workspace_root="E:/one") == {"s1"}

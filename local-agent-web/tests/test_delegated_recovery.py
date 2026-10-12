"""T68：真实 SQLite 关闭重开、独立子图终态及身份索引。"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from langgraph.types import Command
from test_code_workflow import _environment as _code_environment
from test_delegated_workflow import _environment

from local_agent.application.jobs import JobRuntime
from local_agent.application.recovery import (
    NEEDS_REVIEW_DETAIL,
    TurnEvidence,
    child_completion_proven,
    classify_interrupted_job,
)
from local_agent.application.service import ApplicationService
from local_agent.exceptions import JobStoreError
from local_agent.graph import builder, code_workflow, delegated_workflow
from local_agent.persistence import migrations
from local_agent.persistence.checkpoint import open_database
from local_agent.persistence.job_store import JobStore
from local_agent.persistence.request_ledger import RequestLedger
from local_agent.workflow_contracts import READONLY_RESEARCH
from local_agent.workflow_eval import BROKEN_CODE, FIXED_CODE


class Crash(BaseException):
    """模拟子终态写入之后、父调度返回之前的进程退出。"""


@pytest.mark.parametrize("count,concurrency", [(1, 1), (2, 1), (2, 2), (3, 2)])
def test_finished_child_tasks_survive_reopen_without_model_or_tool_replay(tmp_path, monkeypatch, count, concurrency):
    context, model = _environment(tmp_path, count=count, concurrency=concurrency)
    path = tmp_path / "chat.sqlite"
    database = open_database(path, workspace_root=tmp_path)
    session = database.sessions.create()
    store = JobStore(database.connection, lock=database.checkpointer.lock)
    job = store.create(session_id=session.session_id, state="running")
    jobs = JobRuntime(store, background=False)
    context.child_index = lambda run, ids: jobs.index_children(session.session_id, run, ids)
    graph = builder.build_chat_graph(database.checkpointer)
    config = {"configurable": {"thread_id": session.thread_id}}
    original = delegated_workflow._validate_references
    monkeypatch.setattr(delegated_workflow, "_validate_references", lambda *args: (_ for _ in ()).throw(Crash()))
    try:
        with pytest.raises(Crash):
            graph.invoke({"user_input": "核对原文", "session_mode": "agent"}, config, context=context)
        assert len(model.requests) == count
        links = store.child_links(job.job_id)
        assert len(links) == count
        threads = {row[0] for row in database.connection.execute("SELECT DISTINCT thread_id FROM checkpoints")}
        assert {child_id for _, child_id in links} <= threads
    finally:
        jobs.close()
        database.connection.close()
    monkeypatch.setattr(delegated_workflow, "_validate_references", original)
    monkeypatch.setattr(delegated_workflow, "_run_child", lambda *args: pytest.fail("完成的子任务不能重跑"))
    reopened = open_database(path, workspace_root=tmp_path)
    store2 = JobStore(reopened.connection, lock=reopened.checkpointer.lock)
    jobs2 = JobRuntime(store2, background=False)
    context.child_index = lambda run, ids: jobs2.index_children(session.session_id, run, ids)
    context.delegated_research = None  # 恢复只能依靠原冻结请求。
    try:
        result = builder.build_chat_graph(reopened.checkpointer).invoke(None, config, context=context)
        assert result["task_status"] == "completed", result["final_response"]
        assert len(model.requests) == count
        assert store2.child_links(job.job_id) == links
        assert len(result["tool_events"]) == (4 if count == 1 else count * 2)
        assert len(result["model_call_records"]) == count
        assert result["workflow"]["model_tokens_reserved"] > 0
    finally:
        jobs2.close()
        reopened.connection.close()


@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_v10_identity_index_migration_preserves_v9_rows_and_is_idempotent(tmp_path, foreign_keys):
    connection = sqlite3.connect(tmp_path / "old.sqlite")
    try:
        connection.execute(f"PRAGMA foreign_keys={foreign_keys}")
        assert migrations._apply(connection, migrations._CHAT_MIGRATIONS[:9]) == 9
        from local_agent.persistence.session_store import SessionStore
        sessions = SessionStore(connection, workspace_root=tmp_path)
        session = sessions.create(kind="subagent")
        old_job = JobStore(connection).create(session_id=session.session_id)
        assert migrations.apply_migrations(connection) == migrations.apply_migrations(connection) == 10
        store = JobStore(connection, lock=threading.Lock())
        store.link_children(old_job.job_id, "run", ["run:child:1", "run:child:2"])
        store.link_children(old_job.job_id, "run", ["run:child:1", "run:child:2"])
        assert store.child_links(old_job.job_id) == [("run", "run:child:1"), ("run", "run:child:2")]
        assert sessions.get(session.session_id) == session and store.get(old_job.job_id) == old_job
        with pytest.raises(JobStoreError):
            store.link_children(old_job.job_id, "other", ["other:child:1"])
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == foreign_keys
    finally:
        connection.close()


@pytest.mark.parametrize("case_input", ["new_request", "cancelled", "lost_capability", "disabled", "moved_directory"])
def test_frozen_reservation_reopens_and_checks_current_authority(tmp_path, case_input):
    context, model = _environment(tmp_path)
    path = tmp_path / "freeze.sqlite"
    database = open_database(path, workspace_root=tmp_path)
    config = {"configurable": {"thread_id": "parent"}}
    graph = delegated_workflow.build_delegated_subgraph(lambda _, node: node, database.checkpointer)
    graph.invoke({"run_id": "r", "session_mode": "agent"}, config, context=context, interrupt_after=["reserve"])
    assert not model.requests
    database.connection.close()
    if case_input == "new_request":
        context.delegated_research = {"tasks": [{"scope": ".", "question": "不得采用的新问题"}]}
    elif case_input == "cancelled":
        context.cancellation.cancel("取消意图")
    elif case_input == "lost_capability":
        context.agent_capabilities = ()
    elif case_input == "disabled":
        context.app_config.workflows.enabled = False
    else:
        (tmp_path / "docs").rename(tmp_path / "old_docs")
    reopened = open_database(path, workspace_root=tmp_path)
    try:
        result = delegated_workflow.build_delegated_subgraph(lambda _, node: node, reopened.checkpointer).invoke(
            None, config, context=context)
        if case_input == "new_request":
            assert result["task_status"] == "completed"
            assert len(model.requests) == 1
            assert "不得采用的新问题" not in str(model.requests)
            assert result["workflow"]["children"][0]["scope"] == "docs"
        else:
            assert result["task_status"] == "failed" and not model.requests and not result.get("tool_events")
    finally:
        reopened.connection.close()


def test_unknown_child_checkpoint_refuses_replay_even_on_explicit_graph_resume(tmp_path, monkeypatch):
    context, model = _environment(tmp_path)
    path = tmp_path / "unknown.sqlite"
    database = open_database(path, workspace_root=tmp_path)
    graph = delegated_workflow.build_delegated_subgraph(lambda _, node: node, database.checkpointer)
    config = {"configurable": {"thread_id": "p"}}
    monkeypatch.setattr(delegated_workflow, "_run_child", lambda *args: (_ for _ in ()).throw(Crash()))
    try:
        with pytest.raises(Crash):
            graph.invoke({"run_id": "r", "session_mode": "agent"}, config, context=context)
        saved = database.checkpointer.get_tuple({"configurable": {"thread_id": "r:child:1"}})
        assert saved is not None and saved.checkpoint["channel_values"]["status"] == "running"
    finally:
        database.connection.close()
    monkeypatch.setattr(delegated_workflow, "_run_child", lambda *args: pytest.fail("未知执行结果不能重跑"))
    reopened = open_database(path, workspace_root=tmp_path)
    try:
        with pytest.raises(ValueError, match="人工核对"):
            delegated_workflow.build_delegated_subgraph(lambda _, node: node, reopened.checkpointer).invoke(
                None, config, context=context)
        assert not model.requests
    finally:
        reopened.connection.close()


def test_unknown_child_prevents_starting_other_children_in_the_batch(tmp_path, monkeypatch):
    context, model = _environment(tmp_path, count=2, concurrency=2)
    path = tmp_path / "partial-batch.sqlite"
    database = open_database(path, workspace_root=tmp_path)
    monkeypatch.setattr(delegated_workflow, "_run_child", lambda *args: (_ for _ in ()).throw(Crash()))
    item = delegated_workflow._prepare({"run_id": "r", "session_mode": "agent"}, context)[0]
    try:
        with pytest.raises(Crash):
            delegated_workflow._durable_child(context, item, database.checkpointer)
    finally:
        database.connection.close()
    reopened = open_database(path, workspace_root=tmp_path)
    monkeypatch.setattr(delegated_workflow, "_run_child", lambda *args: pytest.fail("整批核对前不能启动其他子任务"))
    try:
        with pytest.raises(ValueError, match="人工核对"):
            delegated_workflow.build_delegated_subgraph(lambda _, node: node, reopened.checkpointer).invoke(
                {"run_id": "r", "session_mode": "agent"}, {"configurable": {"thread_id": "parent"}}, context=context)
        assert not model.requests
        assert reopened.checkpointer.get_tuple({"configurable": {"thread_id": "r:child:2"}}) is None
    finally:
        reopened.connection.close()


def test_cached_failed_child_is_not_retried(tmp_path, monkeypatch):
    context, model = _environment(tmp_path, model_options={"raises": True})
    database = open_database(tmp_path / "failed.sqlite", workspace_root=tmp_path)
    try:
        item = delegated_workflow._prepare({"run_id": "r", "session_mode": "agent"}, context)[0]
        first = delegated_workflow._durable_child(context, item, database.checkpointer)
        assert first.report["status"] == "failed" and len(model.requests) == 1
        monkeypatch.setattr(delegated_workflow, "_run_child", lambda *args: pytest.fail("失败记录不可自动重试"))
        cached = delegated_workflow._durable_child(context, item, database.checkpointer)
        assert cached.report == first.report and len(model.requests) == 1
        assert len(cached.records) == 1
    finally:
        database.connection.close()


def test_cached_success_rechecks_disk_in_parent_and_preserves_original_audit(tmp_path, monkeypatch):
    context, model = _environment(tmp_path)
    database = open_database(tmp_path / "changed.sqlite", workspace_root=tmp_path)
    config = {"configurable": {"thread_id": "p"}}
    graph = delegated_workflow.build_delegated_subgraph(lambda _, node: node, database.checkpointer)
    original = delegated_workflow._validate_references
    monkeypatch.setattr(delegated_workflow, "_validate_references", lambda *args: (_ for _ in ()).throw(Crash()))
    try:
        with pytest.raises(Crash):
            graph.invoke({"run_id": "r", "session_mode": "agent"}, config, context=context)
        monkeypatch.setattr(delegated_workflow, "_validate_references", original)
        monkeypatch.setattr(delegated_workflow, "_run_child", lambda *args: pytest.fail("旧结果只允许读取"))
        (tmp_path / "docs" / "cancel.md").write_text("原文已经变化", encoding="utf-8")
        result = graph.invoke(None, config, context=context)
        assert result["task_status"] == "failed" and len(model.requests) == 1
        assert len(result["tool_events"]) == 4 and len(result["model_call_records"]) == 1
        assert not result["final_evidence"]
    finally:
        database.connection.close()


def test_index_failure_starts_no_child(tmp_path):
    context, model = _environment(tmp_path)
    def reject(*args):
        raise sqlite3.OperationalError("index unavailable")
    context.child_index = reject
    result = builder.build_chat_graph().invoke({"user_input": "核对", "session_mode": "agent"}, context=context)
    assert result["task_status"] == "failed" and not model.requests and not result.get("tool_events")


def test_v10_failure_rolls_back_table_index_and_version(tmp_path):
    connection = sqlite3.connect(tmp_path / "rollback.sqlite")
    try:
        migrations._apply(connection, migrations._CHAT_MIGRATIONS[:9])
        connection.execute("CREATE TRIGGER fail_v10 BEFORE INSERT ON schema_migrations WHEN NEW.version=10 "
                           "BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            migrations.apply_migrations(connection)
        assert migrations.current_version(connection) == 9
        assert "job_children" not in migrations.table_names(connection)
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='job_children_run'").fetchall()
        connection.execute("DROP TRIGGER fail_v10")
        connection.commit()
        assert migrations.apply_migrations(connection) == 10
    finally:
        connection.close()


@pytest.mark.parametrize("case_input", ["missing", "running", "wrong_child", "failed", "no_reference", "wrong_state"])
def test_completion_proof_rejects_incomplete_or_mismatched_receipts(case_input):
    receipt = {"report": {"child_id": "c", "status": "completed", "references": [{"path": "a"}]},
               "state": {"run_id": "c", "task_status": "completed",
                         "workflow": {"name": READONLY_RESEARCH, "phase": "completed", "run_id": "c"}}}
    values = {"child_id": "c", "status": "finished", "receipt": receipt}
    assert child_completion_proven("c", values)
    if case_input == "missing":
        values.pop("receipt")
    elif case_input == "running":
        values["status"] = "running"
    elif case_input == "wrong_child":
        receipt["report"]["child_id"] = "other"
    elif case_input == "failed":
        receipt["report"]["status"] = "failed"
    elif case_input == "no_reference":
        receipt["report"]["references"] = []
    else:
        receipt["state"]["run_id"] = "other"
    assert not child_completion_proven("c", values)


def test_completed_child_is_positive_evidence_but_never_proves_parent_completion(tmp_path):
    database = open_database(tmp_path / "classify.sqlite", workspace_root=tmp_path)
    try:
        session = database.sessions.create()
        job = JobStore(database.connection).create(session_id=session.session_id, state="running")
        verdict = classify_interrupted_job(job, evidence=TurnEvidence("running", "r", False, "r", ("r:child:1",)))
        assert verdict.target == "needs_review" and verdict.detail.startswith(NEEDS_REVIEW_DETAIL)
        assert "1 个完成子任务" in verdict.detail
        mismatched = classify_interrupted_job(job, evidence=TurnEvidence("running", "other", False, "r", ("r:child:1",)))
        assert mismatched.detail == NEEDS_REVIEW_DETAIL
    finally:
        database.connection.close()


@pytest.mark.parametrize("completed", [True, False])
def test_application_restart_reads_child_proof_and_only_settles_index(tmp_path, monkeypatch, completed):
    context, model = _environment(tmp_path)
    path = tmp_path / "service.sqlite"
    database = open_database(path, workspace_root=tmp_path)
    session = database.sessions.create()
    store = JobStore(database.connection, lock=database.checkpointer.lock)
    job = store.create(session_id=session.session_id, state="running")
    jobs = JobRuntime(store, background=False)
    context.child_index = lambda run, ids: jobs.index_children(session.session_id, run, ids)
    graph = builder.build_chat_graph(database.checkpointer)
    config = {"configurable": {"thread_id": session.thread_id}}
    target = "_validate_references" if completed else "_run_child"
    monkeypatch.setattr(delegated_workflow, target, lambda *args: (_ for _ in ()).throw(Crash()))
    try:
        with pytest.raises(Crash):
            graph.invoke({"user_input": "核对", "session_mode": "agent"}, config, context=context)
        run = graph.get_state(config).values["run_id"]
        store.update_state(job.job_id, "running", run_id=run)
        assert len(model.requests) == int(completed)
    finally:
        jobs.close()
        database.connection.close()
    reopened = open_database(path, workspace_root=tmp_path)
    store2 = JobStore(reopened.connection, lock=reopened.checkpointer.lock)
    jobs2 = JobRuntime(store2, background=False)
    graph2 = builder.build_chat_graph(reopened.checkpointer)
    service = ApplicationService(graph=graph2, app_config=context.app_config, model_adapter=model,
        workspace_root=tmp_path, sessions=reopened.sessions, ledger=RequestLedger(reopened.connection), jobs=jobs2)
    monkeypatch.setattr(graph2, "invoke", lambda *args, **kwargs: pytest.fail("启动恢复不能驱动图"))
    monkeypatch.setattr(graph2, "stream", lambda *args, **kwargs: pytest.fail("启动恢复不能执行节点"))
    try:
        verdicts = service.recover_interrupted_jobs()
        assert len(verdicts) == 1 and verdicts[0].target == "needs_review"
        row = store2.get(job.job_id)
        assert row.state == "needs_review" and len(model.requests) == int(completed)
        assert ("1 个完成子任务" in row.detail) == completed
        assert len(store2.child_links(job.job_id)) == 1
        assert service.recover_interrupted_jobs() == []
    finally:
        service._watchdog_stop.set()
        jobs2.close()
        reopened.connection.close()


@pytest.mark.parametrize("decision", ["approve", "reject", "cancelled"])
def test_parent_code_approval_reopens_via_original_command_and_persistent_cancel(tmp_path, decision):
    context, model = _code_environment(tmp_path)
    context.app_config.security.large_edit.max_changed_lines_without_approval = 1
    path = tmp_path / "approval.sqlite"
    database = open_database(path, workspace_root=tmp_path)
    session = database.sessions.create()
    store = JobStore(database.connection, lock=database.checkpointer.lock)
    job = store.create(session_id=session.session_id, state="running")
    config = {"configurable": {"thread_id": session.thread_id}}
    graph = builder.build_chat_graph(database.checkpointer)
    try:
        pending = graph.invoke({"user_input": "修复 clamp", "session_mode": "agent"}, config, context=context)
        assert pending["pending_approval"]["tool_name"] == "edit_file" and len(model.requests) == 1
        store.update_state(job.job_id, "cancelled" if decision == "cancelled" else "waiting_user",
                           run_id=pending["run_id"], detail="重启前取消" if decision == "cancelled" else None)
    finally:
        database.connection.close()
    reopened = open_database(path, workspace_root=tmp_path)
    jobs = JobRuntime(JobStore(reopened.connection, lock=reopened.checkpointer.lock), background=False)
    graph2 = builder.build_chat_graph(reopened.checkpointer)
    service = ApplicationService(graph=graph2, app_config=context.app_config, model_adapter=model,
        workspace_root=tmp_path, sessions=reopened.sessions, ledger=RequestLedger(reopened.connection), jobs=jobs)
    try:
        verdicts = service.recover_interrupted_jobs()
        assert verdicts == [] if decision == "cancelled" else verdicts[0].verdict == "resumable"
        token = service._token_for(session.session_id)
        assert token.cancelled == (decision == "cancelled")
        resumed_context = builder.build_runtime_context(app_config=context.app_config, model_adapter=model,
            workspace_root=tmp_path, current_cwd=tmp_path, usage_recorder=context.usage_recorder, cancellation=token)
        result = graph2.invoke(Command(resume={"decision": "approve" if decision == "cancelled" else decision}),
                               config, context=resumed_context)
        assert len(model.requests) == 1
        assert result["task_status"] == ("completed" if decision == "approve" else "failed")
        assert (tmp_path / "bounds.py").read_text() == (FIXED_CODE if decision == "approve" else BROKEN_CODE)
    finally:
        service._watchdog_stop.set()
        jobs.close()
        reopened.connection.close()


def test_parent_write_checkpoint_gap_reopens_as_needs_review_without_replay(tmp_path, monkeypatch):
    context, model = _code_environment(tmp_path)
    path = tmp_path / "write-gap.sqlite"
    database = open_database(path, workspace_root=tmp_path)
    session = database.sessions.create()
    store = JobStore(database.connection, lock=database.checkpointer.lock)
    job = store.create(session_id=session.session_id, state="running")
    config = {"configurable": {"thread_id": session.thread_id}}
    graph = builder.build_chat_graph(database.checkpointer)
    execute = code_workflow.execute
    def crash_after_edit(state, runtime):
        result = execute(state, runtime)
        if state["pending_tool_calls"][0]["name"] == "edit_file":
            raise Crash()
        return result
    monkeypatch.setattr(code_workflow, "execute", crash_after_edit)
    # 图构建时登记节点函数，所以故障注入后重新编译。
    graph = builder.build_chat_graph(database.checkpointer)
    try:
        with pytest.raises(Crash):
            graph.invoke({"user_input": "修复 clamp", "session_mode": "agent"}, config, context=context)
        assert (tmp_path / "bounds.py").read_text() == FIXED_CODE and len(model.requests) == 1
        store.update_state(job.job_id, "running", run_id=graph.get_state(config).values["run_id"])
    finally:
        database.connection.close()
    reopened = open_database(path, workspace_root=tmp_path)
    store2 = JobStore(reopened.connection, lock=reopened.checkpointer.lock)
    jobs = JobRuntime(store2, background=False)
    graph2 = builder.build_chat_graph(reopened.checkpointer)
    monkeypatch.setattr(graph2, "stream", lambda *args, **kwargs: pytest.fail("未知写入结果不得重放"))
    service = ApplicationService(graph=graph2, app_config=context.app_config, model_adapter=model,
        workspace_root=tmp_path, sessions=reopened.sessions, ledger=RequestLedger(reopened.connection), jobs=jobs)
    try:
        verdict = service.recover_interrupted_jobs()[0]
        assert verdict.target == store2.get(job.job_id).state == "needs_review"
        assert "可能已部分执行" in verdict.detail
        assert len(model.requests) == 1 and (tmp_path / "bounds.py").read_text() == FIXED_CODE
    finally:
        service._watchdog_stop.set()
        jobs.close()
        reopened.connection.close()

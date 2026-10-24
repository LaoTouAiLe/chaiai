"""用户2026-10-18裁定：模型控制收尾，移除执行配额与工作区逐次审批。"""

from langchain_core.messages import AIMessage
from test_t43_main_loop import LoopModel, _call, _run, _tools

from local_agent.attachments import AttachmentStore
from local_agent.config import AgentConfig, ShellSecurityConfig, WebAccessConfig, WorkflowsConfig
from local_agent.graph import builder, delegated_workflow
from local_agent.graph.plan_updates import apply_plan_update
from local_agent.graph.state import PlanStep
from local_agent.graph.web_budget import web_limit_reason
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.security.policy import SecurityPolicy
from local_agent.security.shell_guard import prepare_shell
from local_agent.tools import tool_registry, web_tools
from local_agent.tools.file_tools import delete_file_target, edit_file_content, write_file_content
from local_agent.tools.plan_tools import PlanUpdate


def complete(text="已完成"):
    return _tools(_call("task_control", "finish", action="complete", message=text))


def policy_for(tmp_path, app_config):
    return SecurityPolicy.from_config(app_config, workspace_root=tmp_path,
        external_policy=ExternalPathPolicy(mode="danger-full-access"))


def test_unlimited_defaults_and_larger_child_resources():
    assert AgentConfig().max_tool_loops is None
    assert ShellSecurityConfig().default_timeout_seconds is None
    assert WebAccessConfig().max_search_calls is None
    limits = WorkflowsConfig()
    assert (limits.max_children, limits.max_concurrency, limits.child_timeout_seconds) == (8, 4, 900)
    assert limits.max_tool_calls == 256 and limits.max_model_tokens == 1048576


def test_model_can_work_past_twenty_decisions_and_plain_progress_does_not_finish(tmp_path, app_config):
    app_config.agent.max_tool_loops = None
    model = LoopModel(messages=iter([]), explicit_task_control=False, script=[
        *[_tools(_call("read_file", f"read-{i}", path="note.txt")) for i in range(23)],
        AIMessage(content="我继续核对最后一项。"),
        _tools(_call("read_file", "last", path="note.txt")), complete("核对完成"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed" and result["loop_count"] == 26
    assert result["final_response"] == "核对完成"


def test_failures_are_feedback_and_do_not_block_model_completion(tmp_path, app_config):
    app_config.agent.max_tool_loops = None
    model = LoopModel(messages=iter([]), script=[
        *[_tools(_call("read_file", f"missing-{i}", path="missing.txt")) for i in range(4)],
        _tools(_call("update_plan", "plan", plan=[{"step": "模型自行判断", "status": "pending"}])),
        complete("已说明未完成项与实际错误"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed" and model.decision_calls == 6
    assert result["plan"][0].status == "pending"
    assert any("【执行反馈】" in prompt for prompt in model.seen_prompts)


def test_model_can_explicitly_request_input(tmp_path, app_config):
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("task_control", "input", action="need_input", message="请提供服务地址")),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "blocked" and result["final_response"] == "请提供服务地址"


def test_plans_support_many_parallel_steps_retraction_deletion_and_clear():
    update = PlanUpdate(plan=[{"step": f"步骤{i}", "status": "in_progress"} for i in range(12)])
    state = {"run_id": "r", "plan": [PlanStep(id="done", description="已完成", status="completed")],
             "pending_tool_calls": [{"name": "run_shell"}]}
    changed = apply_plan_update(state, update)
    assert len(changed["plan"]) == 12 and all(step.status == "running" for step in changed["plan"])
    retracted = apply_plan_update(state, PlanUpdate(plan=[{"step": "已完成", "status": "pending"}]))
    assert retracted["plan"][0].status == "pending"
    assert apply_plan_update(state, PlanUpdate(plan=[]))["plan"] == []
    completed = apply_plan_update(state, PlanUpdate(plan=[{"step": "新步骤", "status": "completed"}]))
    assert completed["plan"][0].status == "completed"


def test_workspace_overwrite_delete_and_edits_do_not_need_approval_or_hash(tmp_path, app_config):
    policy = policy_for(tmp_path, app_config)
    (tmp_path / "note.txt").write_text("one\ntwo\n", encoding="utf-8", newline="")
    edit_file_content("note.txt", old_text="one\ntwo\n", new_text="three\nfour\n", policy=policy)
    for index in range(6):
        path = f"file-{index}.txt"
        (tmp_path / path).write_text("old", encoding="utf-8")
        edit_file_content(path, old_text="old", new_text="new", policy=policy)
    write_file_content("note.txt", content="replacement", overwrite=True, policy=policy)
    assert (tmp_path / "note.txt").read_text() == "replacement"
    delete_file_target("note.txt", policy=policy)
    assert not (tmp_path / "note.txt").exists()
    assert policy.classify_tool("git_commit", {"paths": ["file-0.txt"], "message": "change"}).allowed


def test_external_cwd_is_free_but_external_write_needs_grant(tmp_path, app_config):
    work = tmp_path / "work"
    outside = tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    policy = policy_for(work, app_config)
    assert policy.classify_tool("run_shell", {"command": "python --version", "cwd": str(outside)}).allowed
    blocked = policy.classify_tool("write_file", {"path": str(outside / "note.txt"), "content": "x"})
    assert not blocked.allowed and not blocked.requires_approval
    assert not policy.classify_tool("delete_file", {"path": ".git"}).allowed


def test_taskkill_is_not_destructive_and_default_shell_has_no_deadline(tmp_path, app_config):
    policy = policy_for(tmp_path, app_config)
    decision = policy.classify_tool("run_shell", {"command": "taskkill /PID 123 /F"})
    assert decision.allowed and not decision.requires_approval
    prepared = prepare_shell("python --version", config=ShellSecurityConfig(), workspace_root=tmp_path)
    assert prepared.timeout_seconds is None


def test_shell_external_writes_follow_directory_grants_even_in_full_mode(tmp_path, app_config):
    from local_agent.security.path_guard import normalize_grant_path
    from local_agent.tools.shell_tools import run_shell_command

    workspace, outside = tmp_path / "work", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "written.txt"
    (outside / "read.txt").write_text("EXTERNAL_READ_OK", encoding="utf-8")
    policy = policy_for(workspace, app_config)
    assert policy.shell_sandbox_active()
    read = run_shell_command('python -c "from pathlib import Path; print(Path(\'read.txt\').read_text())"',
                             cwd=str(outside), policy=policy)
    assert read.exit_code == 0 and "EXTERNAL_READ_OK" in read.stdout
    command = 'python -c "from pathlib import Path; Path(\'written.txt\').write_text(\'authorized\')"'
    blocked = run_shell_command(command, cwd=str(outside), policy=policy)
    assert blocked.exit_code != 0 and not target.exists()
    policy.external_policy = ExternalPathPolicy(mode="danger-full-access",
        grants={normalize_grant_path(outside): "read_write"})
    allowed = run_shell_command(command, cwd=str(outside), policy=policy)
    assert allowed.exit_code == 0 and target.read_text() == "authorized"


def test_writable_shell_can_rename_and_remove_its_files_but_not_protected_metadata(tmp_path, app_config):
    from local_agent.tools.shell_tools import run_shell_command

    (tmp_path / ".git").mkdir()
    protected = tmp_path / ".git" / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    policy = policy_for(tmp_path, app_config)
    command = ('python -c "from pathlib import Path; p=Path(\'temp.txt\'); '
        'p.write_text(\'x\'); p.rename(\'renamed.txt\'); Path(\'renamed.txt\').unlink(); print(\'CLEANUP_OK\')"')
    outcome = run_shell_command(command, policy=policy)
    assert outcome.exit_code == 0 and "CLEANUP_OK" in outcome.stdout
    denied = run_shell_command('python -c "from pathlib import Path; Path(\'.git/keep.txt\').unlink()"', policy=policy)
    assert denied.exit_code != 0 and protected.read_text() == "keep"


def test_web_count_budget_is_optional():
    assert web_limit_reason("web_search", {}, events=[], run_id="r", config=WebAccessConfig()) is None


def test_background_process_survives_successful_shell_runner_exit(tmp_path, app_config):
    import subprocess
    import time

    from test_win_sandbox import _pid_alive

    from local_agent.tools.shell_tools import run_shell_command

    (tmp_path / "launch.py").write_text(
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "creationflags=subprocess.CREATE_NO_WINDOW)\nprint(p.pid)\n", encoding="utf-8")
    result = run_shell_command("python launch.py", policy=policy_for(tmp_path, app_config))
    assert result.exit_code == 0, result
    child_pid = int(result.stdout.strip())
    try:
        time.sleep(1)
        assert _pid_alive(child_pid), "正常命令结束不能杀掉用户启动的后台服务"
        stopped = run_shell_command(f"taskkill /PID {child_pid} /F", policy=policy_for(tmp_path, app_config))
        assert stopped.exit_code == 0, stopped
        assert not _pid_alive(child_pid)
    finally:
        if _pid_alive(child_pid):
            subprocess.run(["taskkill", "/PID", str(child_pid), "/T", "/F"], capture_output=True, check=False)


def test_default_shell_can_run_longer_than_sixty_seconds(tmp_path, app_config):
    from local_agent.tools.shell_tools import run_shell_command

    result = run_shell_command('python -u -c "import time; print(\'START\'); time.sleep(65); print(\'LONG_OK\')"',
        policy=policy_for(tmp_path, app_config))
    assert result.exit_code == 0 and not result.timed_out and "LONG_OK" in result.stdout
    assert result.duration_ms >= 65000


def test_git_commit_on_main_runs_without_approval(tmp_path, app_config):
    import subprocess

    from local_agent.tools.git_tools import commit

    for args in (["init", "--initial-branch=main"], ["config", "user.name", "Autonomy Test"],
                 ["config", "user.email", "autonomy@example.invalid"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "note.txt").write_text("commit test", encoding="utf-8")
    result = commit(policy_for(tmp_path, app_config), paths=["note.txt"], message="Verify autonomous commit")
    assert result.committed and result.commit and not result.requires_confirmation


def test_edit_can_use_gbk_and_utf16_without_hash(tmp_path, app_config):
    policy = policy_for(tmp_path, app_config)
    for encoding in ("gbk", "utf-16"):
        target = tmp_path / f"{encoding}.txt"
        target.write_bytes("原始内容".encode(encoding))
        edit_file_content(target.name, policy=policy, old_text="原始", new_text="修改", encoding=encoding)
        assert target.read_bytes().decode(encoding) == "修改内容"


def test_image_byte_and_daily_count_quotas_removed(tmp_path):
    store = AttachmentStore(tmp_path / "attachments")
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 4_100_000
    assert store.save("abcd1234", data).byte_count == len(data)
    for index in range(25):
        store.save("abcd1234", b"\x89PNG\r\n\x1a\n" + str(index).encode())
    assert len(list((tmp_path / "attachments").rglob("*.png"))) == 26


def test_delegated_child_can_fetch_web_and_run_commands_but_cannot_write_code(tmp_path, app_config, monkeypatch):
    original = web_tools.build_web_tools
    monkeypatch.setattr(tool_registry, "build_web_tools", lambda config, **kwargs: original(config,
        transport=lambda url, *_args: web_tools.WebResponse(url=url, status=200,
            content_type="text/plain", text="CHILD_WEB_OK"),
        resolver=lambda _host: ["93.184.216.34"]))
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("web_fetch", "web", url="https://example.com/")),
        _tools(_call("run_shell", "shell", command='python -c "print(12345)"')),
        _tools(_call("run_shell", "write", command='python -c "from pathlib import Path; Path(\'forbidden.py\').write_text(\'x\')"')),
        complete("联网与只读命令完成；代码写入被拒绝"),
    ])
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = False
    context = builder.build_runtime_context(app_config=app_config, model_adapter=model,
        workspace_root=tmp_path, current_cwd=tmp_path)
    context.delegated_research = {"tasks": [{"scope": ".", "question": "检查网页和命令，核对禁止写代码"}], "concurrency": 1}
    result = delegated_workflow.run_delegation({"run_id": "r", "agent_loop_version": 1,
        "session_mode": "agent", "messages": []}, context)
    assert result["task_status"] == "completed", result.get("final_response")
    assert not (tmp_path / "forbidden.py").exists()
    records = {record["tool_call_id"]: record for record in result["tool_results"]}
    assert records["web"]["status"] == records["shell"]["status"] == "ok", records
    assert records["write"]["status"] != "ok"

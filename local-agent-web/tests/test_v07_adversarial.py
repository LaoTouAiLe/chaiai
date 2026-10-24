"""v0.7 对抗性边界测试（站在"能不能把它弄坏"的角度，而不是复述契约用例）。

挑的都是**边界与极端**，每条只断言用户可见的结论（任务状态 + 明确原因），不绑定内部实现：

1. 输入校验的上下界：长度、数量、重复、路径规范化、平台别名、Unicode；
2. **预算小到不够分**时必须在**任何调用之前**明确失败（不能 0 份额启动、不能先花钱再拒绝）；
3. **"没有证据"的两种极端**——空关键词 × 全是空行的文件、scope 指向空目录——这是最可能出现
   "假完成"的地方（§4 硬验收 2：产物与证据都要核对，模型自述不算）；
4. scope 指向文件 / 越界 / 两个工作流同时给出的处理。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_delegated_workflow import ResearchModel
from test_workflow_application import (  # noqa: F401
    CHILD,
    READ,
    _send,
    _session,
    _wait,
    workflow_api,
)

from local_agent.cancellation import CancellationToken
from local_agent.config import AppConfig, WorkflowsConfig
from local_agent.graph import builder
from local_agent.workflow_contracts import (
    CodeChangeRequest,
    DelegatedResearchRequest,
    ReadonlyResearchRequest,
)

# ---- 一、输入边界（纯校验，秒级）--------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    ["docs/*.md", "**/*.md", "a" * 200, "./docs/x.md", "文档/*.md", "docs/**/x.md"],
)
def test_pattern_accepts_workspace_relative_globs(pattern: str) -> None:
    assert ReadonlyResearchRequest(pattern=pattern, keywords=[]).pattern == pattern


@pytest.mark.parametrize(
    "pattern",
    [
        "",  # 空
        "a" * 201,  # 超长
        "/abs/x.md",  # 绝对路径
        "~/x.md",  # 家目录别名
        "C:/x.md",  # 盘符
        "docs\\x.md",  # 反斜杠
        "../x.md",  # 逃逸
        "docs/..",  # 逃逸（结尾）
        "..",  # 逃逸（整体）
        " docs",  # 首尾空白
        "docs/x.md ",  # 尾空白
    ],
)
def test_pattern_rejects_escape_and_alias(pattern: str) -> None:
    with pytest.raises(ValidationError):
        ReadonlyResearchRequest(pattern=pattern, keywords=[])


def test_keyword_bounds_and_duplicates() -> None:
    eight = [f"关键词{index}" for index in range(8)]
    assert ReadonlyResearchRequest(pattern="*.md", keywords=eight).keywords == eight
    assert ReadonlyResearchRequest(pattern="*.md", keywords=[]).keywords == []
    assert ReadonlyResearchRequest(pattern="*.md", keywords=["甲" * 80]).keywords == ["甲" * 80]

    for bad in (
        eight + ["第九个"],  # 超过 8 个
        ["重复", "重复"],  # 重复
        [""],  # 空串
        ["   "],  # 全空白
        [" 前后有空白 "],  # 首尾空白
        ["甲" * 81],  # 单关键词超长
    ):
        with pytest.raises(ValidationError):
            ReadonlyResearchRequest(pattern="*.md", keywords=bad)


def test_code_request_bounds_and_path_normalisation() -> None:
    ok = {"source_path": "src/a.py", "test_paths": ["tests/test_a.py"], "expected_test_count": 3}
    assert CodeChangeRequest(**ok).expected_test_count == 3
    assert CodeChangeRequest(**{**ok, "expected_test_count": 1}).expected_test_count == 1
    assert CodeChangeRequest(**{**ok, "expected_test_count": 10000}).expected_test_count == 10000
    seven = [f"tests/test_{index}.py" for index in range(7)]
    assert len(CodeChangeRequest(**{**ok, "test_paths": seven}).test_paths) == 7
    # Unicode 文件名必须能过（中文仓库是常态）
    assert CodeChangeRequest(**{**ok, "source_path": "文档/核心.py"}).source_path == "文档/核心.py"

    for bad in (
        {**ok, "expected_test_count": 0},
        {**ok, "expected_test_count": 10001},
        {**ok, "test_paths": []},
        {**ok, "test_paths": seven + ["tests/test_7.py"]},
        {**ok, "test_paths": ["src/a.py"]},  # 与源码重复
        {**ok, "source_path": "src//a.py"},  # 未规范化
        {**ok, "source_path": "src/./a.py"},  # 未规范化
        {**ok, "source_path": "src/"},  # 目录写法
        {**ok, "source_path": "src/a[0].py"},  # 通配符
        {**ok, "source_path": "src/a?.py"},  # 通配符
        {**ok, "source_path": "/abs/a.py"},  # 绝对路径
        {**ok, "source_path": "../a.py"},  # 逃逸
        {**ok, "test_command": "python -m pytest"},  # 命令不可换
        {**ok, "extra": 1},  # 未知字段
    ):
        with pytest.raises(ValidationError):
            CodeChangeRequest(**bad)


def test_child_task_bounds() -> None:
    for scope in (".", "docs", "docs/sub", "文档"):
        task = {"scope": scope, "question": "核对原文"}
        assert DelegatedResearchRequest(tasks=[task]).tasks[0].scope == scope
    assert len(DelegatedResearchRequest(tasks=[{"scope": ".", "question": "问" * 1000}]).tasks) == 1

    for bad_scope in ("", "docs/", "../x", "a\\b", "/abs", "~/x", "a:b", "  docs", "docs/../x"):
        with pytest.raises(ValidationError):
            DelegatedResearchRequest(tasks=[{"scope": bad_scope, "question": "核对"}])
    for bad_question in ("", "   ", "问" * 1001):
        with pytest.raises(ValidationError):
            DelegatedResearchRequest(tasks=[{"scope": ".", "question": bad_question}])
    with pytest.raises(ValidationError):
        DelegatedResearchRequest(tasks=[])
    tasks = [{"scope": ".", "question": "问"}] * 16
    assert len(DelegatedResearchRequest(tasks=tasks, concurrency=8).tasks) == 16
    with pytest.raises(ValidationError):
        DelegatedResearchRequest(tasks=[{"scope": ".", "question": "问"}], concurrency=0)


# ---- 二、运行时边界（真的跑图）----------------------------------------------


def _readonly_context(tmp_path: Path, app_config: AppConfig, model_factory, *, seeds, request, limits=None):
    for path, content in seeds:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = False
    app_config.memory.compression.enabled = False
    app_config.rag.enabled = False
    app_config.agents.enabled = False
    app_config.profiles.active = "reviewer"
    app_config.workflows = WorkflowsConfig(enabled=True, **(limits or {}))
    return builder.build_runtime_context(
        app_config=app_config.as_run_config(), model_adapter=model_factory(fail_answer=True),
        workspace_root=tmp_path, current_cwd=tmp_path, cancellation=CancellationToken(),
        readonly_research=request,
    )


def _run(context):
    return builder.build_chat_graph().invoke(
        {"user_input": "整理原文", "session_mode": "agent"}, context=context
    )


def test_readonly_with_no_keywords_over_a_blank_file_never_claims_an_empty_report(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """空关键词 + 全是空行的文件：**匹配到了文件却整理出零条原文**，不能报成功（假完成）。"""
    context = _readonly_context(
        tmp_path, app_config, model_factory,
        seeds=[("docs/blank.md", "\n\n   \n")],
        request={"pattern": "docs/*.md", "keywords": []},
    )

    result = _run(context)

    if result["task_status"] == "completed":
        assert result["workflow"]["references"], "完成就必须有可核对原文，否则是假完成"
    else:
        assert result["workflow"]["error"], "失败必须给明确原因"


def test_truncated_locate_fails_instead_of_silently_narrowing(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """定位截断（文件数超过 max_source_files）必须失败，不能"悄悄少看几个文件"当完成（§18.2）。"""
    context = _readonly_context(
        tmp_path, app_config, model_factory,
        seeds=[("docs/a.md", "事实甲\n"), ("docs/b.md", "事实乙\n")],
        request={"pattern": "docs/*.md", "keywords": ["事实甲"]},
        limits={"max_source_files": 1},
    )

    result = _run(context)

    assert result["task_status"] == "failed"
    assert result["workflow"]["error"]


def test_tiny_report_budget_fails_instead_of_truncating(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """报告/证据额度小到装不下时：必须**明确失败**，不能截断后写 `completed`（§18.2）。"""
    for limits in ({"max_report_chars": 1}, {"max_evidence_chars": 1}):
        context = _readonly_context(
            tmp_path, app_config, model_factory,
            seeds=[("docs/fact.md", "事实甲的第一段原文\n事实甲的第二段原文\n")],
            request={"pattern": "docs/*.md", "keywords": ["事实甲"]},
            limits=limits,
        )

        result = _run(context)

        assert result["task_status"] == "failed", f"{limits} 下不应报完成"
        assert result["workflow"]["error"]


def test_delegated_budget_too_small_fails_before_any_model_call(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """预算小到不够分（每子份额会 ≤0）时，必须在**任何调用之前**明确失败，不先花钱再拒绝。"""
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = False
    app_config.memory.compression.enabled = False
    app_config.rag.enabled = False
    app_config.agents.enabled = False
    app_config.profiles.active = "coder"
    app_config.workflows = WorkflowsConfig(enabled=True, max_steps=3)  # (3-2)//2-2 = -2
    (tmp_path / "part0").mkdir()
    (tmp_path / "part1").mkdir()
    model = ResearchModel(messages=iter([]))
    context = builder.build_runtime_context(
        app_config=app_config.as_run_config(), model_adapter=model,
        workspace_root=tmp_path, current_cwd=tmp_path, cancellation=CancellationToken(),
        delegated_research={"tasks": [
            {"scope": "part0", "question": "核对 0"},
            {"scope": "part1", "question": "核对 1"},
        ]},
    )

    result = _run(context)

    assert result["task_status"] == "failed"
    assert model.requests == [], "预算不足时不得先发起模型调用"


def test_delegated_child_with_nothing_to_read_fails_the_parent(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """子任务的 scope 里没有可核对原文：子失败，**父不得成功**（§18.3：所有子成功才完成）。"""
    (tmp_path / "empty").mkdir()
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = False
    app_config.memory.compression.enabled = False
    app_config.rag.enabled = False
    app_config.agents.enabled = False
    app_config.profiles.active = "coder"
    app_config.workflows = WorkflowsConfig(enabled=True)
    model = ResearchModel(messages=iter([]), body='{"pattern": "*.md", "keywords": ["任何事实"]}')
    context = builder.build_runtime_context(
        app_config=app_config.as_run_config(), model_adapter=model,
        workspace_root=tmp_path, current_cwd=tmp_path, cancellation=CancellationToken(),
        delegated_research={"tasks": [{"scope": "empty", "question": "核对任何事实"}]},
    )

    result = _run(context)

    assert result["task_status"] == "failed"
    children = result["workflow"]["children"]
    assert children and children[0]["status"] != "completed"


def test_delegated_scope_pointing_at_a_file_is_refused(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """scope 必须是**目录**：指向一个文件时明确失败，不把它当目录用。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "fact.md").write_text("任何事实\n", encoding="utf-8")
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = False
    app_config.memory.compression.enabled = False
    app_config.rag.enabled = False
    app_config.agents.enabled = False
    app_config.profiles.active = "coder"
    app_config.workflows = WorkflowsConfig(enabled=True)
    model = ResearchModel(messages=iter([]))
    context = builder.build_runtime_context(
        app_config=app_config.as_run_config(), model_adapter=model,
        workspace_root=tmp_path, current_cwd=tmp_path, cancellation=CancellationToken(),
        delegated_research={"tasks": [{"scope": "docs/fact.md", "question": "核对"}]},
    )

    result = _run(context)

    assert result["task_status"] == "failed"
    assert model.requests == []


def test_two_workflows_at_once_is_refused(
    tmp_path: Path, app_config: AppConfig, model_factory
) -> None:
    """两个工作流同时给出：明确失败，不猜优先级（§18.2.1）。"""
    _two_workflows(tmp_path, app_config, model_factory)


def _two_workflows(tmp_path: Path, app_config: AppConfig, model_factory) -> None:
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = False
    app_config.memory.compression.enabled = False
    app_config.rag.enabled = False
    app_config.agents.enabled = False
    app_config.profiles.active = "coder"
    app_config.workflows = WorkflowsConfig(enabled=True)
    model = model_factory(fail_answer=True)
    context = builder.build_runtime_context(
        app_config=app_config.as_run_config(), model_adapter=model,
        workspace_root=tmp_path, current_cwd=tmp_path, cancellation=CancellationToken(),
        delegated_research={"tasks": [{"scope": ".", "question": "核对"}]},
        code_change={
            "source_path": "a.py", "test_paths": ["test_a.py"],
            "expected_test_count": 1, "test_command": "python -B -m unittest -v",
        },
    )

    result = _run(context)

    assert result["task_status"] == "failed"


# ---- 三、v0.6 × v0.7 的集成缝隙（后台调度 / 任务级超时）----------------------


def test_workflow_completes_under_background_dispatch(workflow_api: object) -> None:  # noqa: F811
    """`jobs.background=true` + 工作流：要走到终态、任务索引收敛、快照给出工作流投影。

    既有 T69 用例里 `background=True` 只用在"等待审批释放 worker"那一条，**跑完**这条路径没有被直接断言。
    """
    client, runtime = workflow_api(background=True)  # type: ignore[operator]
    sid = _session(client)
    accepted = _send(client, sid, READ)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "running"  # 后台：POST 返回时还没跑完

    snapshot = _wait(client, sid, {"completed", "failed", "awaiting_approval"})

    assert snapshot["session_state"] == "completed", snapshot
    assert snapshot["workflow"]["phase"] == "completed"
    # 快照先到、索引随后结算都算正常；给索引一点时间，然后必须收敛到终态
    deadline = time.monotonic() + 3
    row = runtime.service._jobs.list_for_session(sid)[-1]
    while time.monotonic() < deadline and row.state == "running":
        time.sleep(0.05)
        row = runtime.service._jobs.list_for_session(sid)[-1]
    assert row.state == "completed", (
        f"快照已 completed，但任务索引停在 {row.state}（会话会被自己那条旧任务挡住）"
    )
    # 用户可见后果：下一句必须能被受理（否则就是 v0.6 修过的那类"索引卡住会话"）
    again = client.post(
        f"/api/sessions/{sid}/messages",
        json={"text": "再问一句", "client_request_id": "after-workflow"},
    )
    assert again.status_code == 200, again.text


def test_task_timeout_cancels_a_running_workflow_without_reporting_success(
    workflow_api: object,  # noqa: F811
) -> None:
    """任务级超时落在**工作流**上：取消沿既有路径生效，且**绝不报完成**（§18.2"取消后不得成功"）。"""
    gate = threading.Event()
    model = ResearchModel(messages=iter([]), gate=gate)  # 子任务的模型调用会阻塞在这里
    client, runtime = workflow_api(model=model, background=True)  # type: ignore[operator]
    runtime.service._max_task_seconds = 0  # 立即视为超时
    sid = _session(client)
    assert _send(client, sid, CHILD, key="timeout").status_code == 200

    for _ in range(60):  # 等到看守把在跑任务取消（同一条取消路径，§17.4）
        runtime.service.expire_overdue_tasks()
        if runtime.service._jobs.list_for_session(sid)[-1].state in {"cancelled", "failed"}:
            break
        time.sleep(0.05)
    gate.set()  # 放开被阻塞的模型调用，让这一轮把取消走完
    snapshot = _wait(client, sid, {"failed", "cancelled", "completed", "awaiting_approval"})

    assert snapshot["session_state"] != "completed", "被超时取消的轮次不得报完成"
    assert runtime.service._jobs.list_for_session(sid)[-1].state != "completed"

"""子 Agent 第二档（可写）的契约测试（2026-11-02）。

守住四件事：

1. **只读档下的子 Agent 还是只读**：父自己绑了写能力也不行——档位说"连工作区都不许写"；
2. **工作区可写档下子 Agent 能写，但只到工作区**：外部策略固定 ``workspace_only``，不继承父授权目录；
3. **写必须记进父会话那本账**：子策略与父共用同一个 ``file_change_recorder``，否则「文件改动」
   面板看不到、整轮还原也覆盖不到；
4. **审批类工具不进子绑定**：``delete_file``（ALWAYS_APPROVAL）撞上会让整路子任务静默结束，
   ``download_file`` 没人盯着落盘目标；另外升级前冻结的检查点必须按只读恢复（失败闭合）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import RoutingFakeModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from local_agent.cancellation import CancellationToken
from local_agent.config import WorkflowsConfig
from local_agent.graph import builder, delegated_workflow
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.usage_recording import ModelCallRecorder
from local_agent.security import win_sandbox
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.workflow_eval import _config


class _IdleModel(RoutingFakeModel):
    """这条用例不做模型行为验证，只要一个能装配上下文的假模型。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="{}"))])


class _WritingChildModel(RoutingFakeModel):
    """子 Agent 先写一个文件，再通过控制工具交付；用来验证"真的写进去了"。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        acted = any(isinstance(message, ToolMessage) for message in messages)
        call = (
            {"name": "task_control", "id": "child-finish",
             "args": {"action": "complete", "message": "已把结论写入 notes.md"}}
            if acted else
            {"name": "write_file", "id": "child-write",
             "args": {"path": "notes.md", "content": "子 Agent 写的结论\n"}}
        )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


def _parent(
    tmp_path: Path,
    *,
    mode: str = "workspace-write",
    switches: dict[str, bool] | None = None,
    recorder: ModelCallRecorder | None = None,
    model: Any = None,
) -> Any:
    """真实装配的运行上下文；`external_policy` 决定档位（`build_runtime_context` 支持显式传入）。"""
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    config = _config(tmp_path, "coder")
    config.workflows = WorkflowsConfig(enabled=False)
    context = builder.build_runtime_context(
        app_config=config.as_run_config(),
        model_adapter=model or _IdleModel(messages=iter([])),
        workspace_root=tmp_path,
        current_cwd=tmp_path,
        cancellation=CancellationToken(),
        external_policy=ExternalPathPolicy(mode=mode),
        model_capabilities=ModelCapabilities(tool_calling=True),
        agent_switches=switches if switches is not None else {"delegate": True},
    )
    if recorder is not None:
        context.security_policy.file_change_recorder = recorder
    return context


def _prepared(context: Any) -> list[delegated_workflow._PreparedChild]:
    context.delegated_research = {"tasks": [{"scope": "docs", "question": "核对取消原文"}], "concurrency": 1}
    state = {"run_id": "RUN", "session_mode": "agent", "agent_loop_version": 1, "messages": []}
    return delegated_workflow._prepare(state, context, require_enabled=False)


def _child(tmp_path: Path, parent: Any) -> tuple[Any, delegated_workflow._PreparedChild]:
    context = delegated_workflow._child_context(parent, _prepared(parent)[0], CancellationToken())
    return context, _prepared(parent)[0]


def _bound_names(context: Any) -> set[str]:
    return {tool.name for tool in context.tool_registry.tools_for("tool", context.agent_capabilities)}


def test_read_only_tier_keeps_the_child_read_only(tmp_path: Path) -> None:
    """档位是只读时，父即便绑着写能力，子 Agent 也不能写。"""
    parent = _parent(tmp_path, mode="read-only")

    item = _prepared(parent)[0]
    context, _ = _child(tmp_path, parent)

    assert item.writable is False
    assert "files_write" not in context.agent_capabilities
    assert "write_file" not in _bound_names(context)
    assert context.security_policy.external_policy.mode == "read-only"


def test_workspace_write_tier_lets_the_child_write_inside_the_workspace(tmp_path: Path) -> None:
    parent = _parent(tmp_path, mode="workspace-write")

    item = _prepared(parent)[0]
    context, _ = _child(tmp_path, parent)
    bound = _bound_names(context)

    assert item.writable is True
    assert "files_write" in context.agent_capabilities
    assert {"write_file", "edit_file"} <= bound
    # 审批类与下载不进子绑定：前者没有审批节点可接，后者没人盯落盘目标。
    assert "delete_file" not in bound
    assert "download_file" not in bound
    # 边界只到工作区：不继承父的写授权目录，也不因父是更高档位而放宽。
    assert context.security_policy.external_policy.mode == "workspace-write"
    assert context.security_policy.external_policy.workspace_only is True
    assert context.security_policy.external_policy.grants == {}


def test_parent_without_write_capability_keeps_the_child_read_only(tmp_path: Path) -> None:
    parent = _parent(tmp_path, mode="workspace-write", switches={"delegate": True, "files_write": False})

    context, item = _child(tmp_path, parent)

    assert item.writable is False
    assert "write_file" not in _bound_names(context)


def test_child_writes_land_in_the_parent_ledger(tmp_path: Path) -> None:
    """账本共享：不接线的话子 Agent 改过的文件在「文件改动」里不存在，整轮还原也覆盖不到。"""
    recorder = ModelCallRecorder(
        connection_id="offline", config_version="delegate-write", provider="scripted",
        model_id="scripted-offline", endpoint_fingerprint="offline", context_window_tokens=32768,
    )
    parent = _parent(tmp_path, recorder=recorder)

    context, _ = _child(tmp_path, parent)

    assert context.security_policy.file_change_recorder is recorder


def test_child_actually_writes_and_reports_the_count(tmp_path: Path) -> None:
    """端到端：子 Agent 真的把文件写进 scope 目录，且工具结果里报出改了几次。

    只测"能力集合里有写"是不够的——策略重建、注册表绑定、绑定可见性任何一环漏掉，
    这里都会变成"能力有、工具没有"或"工具在、写被拒"。
    """
    context = _parent(tmp_path, mode="workspace-write", model=_WritingChildModel(messages=iter([])))

    payload = json.loads(
        delegated_workflow.make_delegation_runner(context)(
            [{"scope": "docs", "question": "把结论写进 notes.md"}], 1
        )
    )

    assert payload["status"] == "completed", payload
    assert (tmp_path / "docs" / "notes.md").read_text(encoding="utf-8") == "子 Agent 写的结论\n"
    child = payload["children"][0]
    assert child["changed_files"] == 1
    assert "已把结论写入" in payload["report"]


def test_read_only_tier_child_cannot_write(tmp_path: Path) -> None:
    """同一个脚本在只读档下必须写不进去：文件不出现，计数为 0。"""
    context = _parent(tmp_path, mode="read-only", model=_WritingChildModel(messages=iter([])))

    payload = json.loads(
        delegated_workflow.make_delegation_runner(context)(
            [{"scope": "docs", "question": "把结论写进 notes.md"}], 1
        )
    )

    assert not (tmp_path / "docs" / "notes.md").exists()
    assert payload["children"][0]["changed_files"] == 0


@pytest.mark.skipif(not win_sandbox.is_supported(),
                    reason="没有 OS 级写边界时 run_shell 本来就不受沙箱约束（已在策略里如实说明）")
def test_writable_child_cannot_write_through_shell(tmp_path: Path) -> None:
    """可写子任务的**命令**仍不给写授权：写必须走文件工具，才进父会话的变更账本。

    这条是 2026-11-02 全量测试抓出来的：把子策略放到 ``workspace-write`` 之后，
    ``run_shell`` 也顺手能写盘了——那是一条没有账、也没法整轮还原的写通道。
    """
    parent = _parent(tmp_path, mode="workspace-write")

    context, _ = _child(tmp_path, parent)

    # 文件工具可写（会记进父账本）。
    assert context.security_policy.resolve_writable_path("notes.md")
    # 命令只有读授权：请求里没有任何可写 SID。
    request = context.security_policy.shell_sandbox_request()
    assert request is not None and request.writable_sids == ()


def test_frozen_spec_without_the_flag_resumes_read_only() -> None:
    """升级前冻结的子任务没有 `writable`：必须按只读恢复，不能被"现在的配置"升级成写任务。"""
    spec = {
        "task": {"scope": "docs", "question": "核对"},
        "root": "C:/workspace/docs",
        "child_id": "RUN:child:1",
        "input_bound": 1024,
        "output_bound": 512,
        "limits": WorkflowsConfig().model_dump(),
        "autonomous": True,
    }

    assert delegated_workflow._from_spec(spec).writable is False
    assert delegated_workflow._from_spec({**spec, "writable": True}).writable is True

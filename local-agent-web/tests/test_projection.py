"""T28：State → DTO 单向投影的契约测试。

投影是纯函数：输入是一个 State 映射，输出是 T27 的 DTO。这些测试不建图、不跑模型、不碰数据库。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from local_agent.application import errors as app_errors
from local_agent.application import pending_views
from local_agent.application import projection as proj
from local_agent.application.models import (
    ApprovalView,
    MemoryMessageView,
    SessionState,
    ToolMessageView,
    WorkspaceRef,
    canonical_json,
)
from local_agent.security.diff_guard import build_unified_diff

AT = datetime(2026, 9, 24, 9, 20, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 24, 9, 21, tzinfo=timezone.utc)


def _state(**overrides: Any) -> dict[str, Any]:
    """一个"普通一轮对话"的 State；测试按需覆盖字段。"""
    state: dict[str, Any] = {
        "run_id": "run-1",
        "session_id": "s-1",
        "thread_id": "thread-1",
        "messages": [
            HumanMessage(content="在 README 里补一段最小启动步骤。", id="m1"),
            AIMessage(
                content="我先看一下现有结构。",
                id="m2",
                tool_calls=[{"name": "read_file", "args": {"path": "README.md"}, "id": "t1"}],
            ),
            ToolMessage(content="（已读取）", tool_call_id="t1", id="m3"),
            AIMessage(content="已补上最小启动步骤。", id="m4"),
        ],
        "task_goal": "补一段快速开始",
        "task_status": "completed",
        "plan": [
            {"id": "p1", "description": "读取 README", "status": "completed"},
            {"id": "p2", "description": "写入步骤", "status": "running"},
        ],
        "tool_events": [
            {
                "tool_call_id": "t1",
                "tool_name": "read_file",
                "redacted_arguments": {"path": "README.md", "recursive": True},
                "approval_id": None,
                "started_at": AT,
                "finished_at": LATER,
                "status": "ok",
                "output_preview": "README.md 的前 20 行…",
                "output_digest": "sha256:abc",
                "run_id": "run-1",
            }
        ],
        "tool_results": [
            {
                "tool_call_id": "t1",
                "tool_name": "read_file",
                "status": "ok",
                "truncated": True,
                "output_preview": "README.md 的前 20 行…",
                "output_digest": "sha256:abc",
                "run_id": "run-1",
            }
        ],
        "final_response": "已补上最小启动步骤。",
        "final_evidence": [{"reference_id": "ev-1", "summary": "README.md 已更新"}],
        "token_usage": {"prompt_tokens": 84_000},
        "context_budget": {"context_window_tokens": 128_000},
        "error_events": [],
    }
    state.update(overrides)
    return state


def _snapshot(state: dict[str, Any], **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "session_id": "s-1",
        "workspace": WorkspaceRef(workspace_id="ws-1", display_name="local-agent-web"),
        "title": "给 README 补一段快速开始",
        "snapshot_id": "snap-1",
        "created_at": LATER,
    }
    kwargs.update(overrides)
    return proj.project_snapshot(state, **kwargs)


# ---- 消息、工具行、计划 ----------------------------------------------------


def test_message_flow_pairs_tool_calls_with_their_rows() -> None:
    views = proj.project_messages_view(_state())
    kinds = [view.kind for view in views]
    assert kinds == ["user", "assistant", "tool", "assistant"]

    tool = views[2]
    assert isinstance(tool, ToolMessageView)
    # 展示 id 带轮次作用域：跨轮复用同一个调用 id 时两行不能撞 id。
    assert tool.message_id == "tool:run-1:t1"
    assert tool.tool.tool_name == "read_file"
    assert tool.tool.status == "ok"
    assert tool.tool.truncated is True
    assert tool.tool.elapsed_ms == 60_000
    # 参数被展平成展示字符串，布尔值也不例外。
    assert tool.tool.redacted_arguments == {"path": "README.md", "recursive": "True"}


def test_tool_message_content_is_not_duplicated_as_a_second_row() -> None:
    # ToolMessage 的正文已经由工具行承载，不应再产生一条消息项。
    views = proj.project_messages_view(_state())
    assert sum(1 for view in views if view.kind == "tool") == 1


def test_pending_tool_calls_appear_as_pending_rows() -> None:
    state = _state(
        tool_events=[],
        tool_results=[],
        pending_tool_calls=[{"id": "t9", "name": "run_shell_command", "args": {"command": "ls"}}],
    )
    tools = proj.project_tools(state)
    assert [tool.tool_call_id for tool in tools] == ["t9"]
    assert tools[0].status == "pending"
    assert tools[0].redacted_arguments == {"command": "ls"}


def test_tool_rows_are_scoped_by_run_when_two_turns_reuse_a_call_id() -> None:
    """两轮复用同一个 `tool_call_id`：两条记录都在，且各自的消息配到自己那一行。

    裸 `tool_call_id` 建字典会让先出现的那一轮被后一轮顶掉（复审探针：只剩后一条）。
    """
    state = _state(
        run_id="run-2",
        messages=[
            HumanMessage(content="第一轮", id="m1"),
            AIMessage(
                content="",
                id="m2",
                tool_calls=[{"name": "read_file", "args": {"path": "a.txt"}, "id": "t1"}],
            ),
            ToolMessage(content="（第一轮结果）", tool_call_id="t1", id="m3"),
            HumanMessage(content="第二轮", id="m4"),
            AIMessage(
                content="",
                id="m5",
                tool_calls=[{"name": "read_file", "args": {"path": "b.txt"}, "id": "t1"}],
            ),
        ],
        tool_events=[
            {
                "tool_call_id": "t1",
                "tool_name": "read_file",
                "redacted_arguments": {"path": "a.txt"},
                "status": "ok",
                "output_preview": "第一轮",
                "output_digest": "sha256:first",
                "run_id": "run-1",
            },
            {
                "tool_call_id": "t1",
                "tool_name": "read_file",
                "redacted_arguments": {"path": "b.txt"},
                "status": "ok",
                "output_preview": "第二轮",
                "output_digest": "sha256:second",
                "run_id": "run-2",
            },
        ],
        tool_results=[
            {
                "tool_call_id": "t1",
                "tool_name": "read_file",
                "status": "ok",
                "truncated": False,
                "output_preview": "第一轮",
                "output_digest": "sha256:first",
                "run_id": "run-1",
            },
            {
                "tool_call_id": "t1",
                "tool_name": "read_file",
                "status": "ok",
                "truncated": False,
                "output_preview": "第二轮",
                "output_digest": "sha256:second",
                "run_id": "run-2",
            },
        ],
    )

    tools = [view for view in proj.project_messages_view(state) if view.kind == "tool"]

    # 两行都在，顺序与出现顺序一致：前一轮不再被后一轮覆盖。
    assert [tool.tool.output_digest for tool in tools] == ["sha256:first", "sha256:second"]
    # 每条 AI 消息配到**自己那一轮**的记录，而不是最后一条。
    assert [tool.tool.redacted_arguments["path"] for tool in tools] == ["a.txt", "b.txt"]
    assert len({tool.message_id for tool in tools}) == 2
    # 事件投影只讲本轮：`run_id` 一筛就只剩第二轮那条。
    assert [tool.tool_call_id for tool in proj.project_tools(state, run_id="run-2")] == ["t1"]
    assert proj.project_tools(state, run_id="run-2")[0].output_digest == "sha256:second"


def test_pending_and_approval_arguments_are_sanitized_before_display() -> None:
    """待执行参数与审批参数还没经过工具层，进 DTO 之前必须先脱敏（§15.8）。

    工具层的 `redacted_arguments` 是"已经执行过"的调用；`pending_tool_calls` 与
    `pending_approval.arguments` 是原文，密钥样式串脱敏，绝对路径保留。
    """
    state = _state(
        tool_events=[],
        tool_results=[],
        pending_tool_calls=[
            {
                "id": "t9",
                "name": "write_file",
                "args": {"content": "sk-12345678", "path": "C:\\Users\\me\\.env"},
            }
        ],
    )
    pending = proj.project_tools(state)[0]
    assert pending.redacted_arguments["content"] == "***"
    assert pending.redacted_arguments["path"] == "C:\\Users\\me\\.env"

    request = _pending_request()
    request["arguments"] = {"api_key": "sk-abcdefgh", "target": "C:\\Users\\me\\.env"}
    approval = pending_views.project_approval(_state(pending_approval=request), has_live_interrupt=True)
    assert approval is not None
    assert approval.redacted_arguments == {}


def test_display_text_from_tools_approval_plan_and_result_is_sanitized() -> None:
    """参数之外的自由文本也会进入页面，不能把工具输出或审批说明当成已脱敏。"""
    secret = "sk-12345678"
    configured_secret = "my-private-token"
    state = _state(
        task_goal=f"处理 {secret}",
        plan=[{"id": "p1", "description": f"检查 {secret}", "status": "running"}],
        tool_results=[
            {
                "tool_call_id": "t1", "tool_name": "read_file", "status": "ok",
                "output_preview": f"返回 {secret} 和 {configured_secret}，见 C:\\Users\\me\\.env",
                "run_id": "run-1",
            }
        ],
        final_response=f"已完成 {secret}",
        final_evidence=[{"reference_id": f"证据 {secret}"}],
    )
    request = _pending_request()
    request["impact_summary"] = f"将覆盖 {secret}"
    state["pending_approval"] = request
    snapshot = _snapshot(state, has_live_interrupt=True, secrets=(configured_secret,))
    dumped = canonical_json(snapshot)

    assert secret not in dumped
    assert configured_secret not in dumped
    assert snapshot.plan is not None and "***" in snapshot.plan.goal
    assert snapshot.approval is not None and "***" in snapshot.approval.impact_summary
    assert snapshot.final_result is not None and "***" in snapshot.final_result.summary
    tool = proj.project_tools(state, secrets=(configured_secret,))[0]
    assert tool.output_preview == "返回 *** 和 ***，见 C:\\Users\\me\\.env"


def test_provider_reasoning_is_visible_but_secrets_and_metadata_stay_private() -> None:
    state = _state(messages=[
        HumanMessage(content="你好", id="u1"),
        AIMessage(
            content="你好，我可以帮你读文件。",
            id="a1",
            additional_kwargs={"reasoning_content": "私有思考 sk-hidden-secret C:/Users/me/private.txt"},
        ),
    ])
    dumped = canonical_json(_snapshot(state))
    assert "你好，我可以帮你读文件。" in dumped
    assert "reasoning_content" not in dumped
    assert "私有思考" in dumped
    assert "sk-hidden-secret" not in dumped
    assert "C:/Users/me/private.txt" in dumped
    assert state["messages"][1].additional_kwargs["reasoning_content"].endswith("private.txt")


def test_reply_overrides_are_applied_before_rendering() -> None:
    """回答文本改动走读时投影（架构 §10.1）：页面看到的必须是投影后的文本。"""
    state = _state(reply_overrides={"m4": "（这是修正后的回答）"})
    views = proj.project_messages_view(state)
    assistant = [view for view in views if view.kind == "assistant"][-1]
    assert assistant.text == "（这是修正后的回答）"  # type: ignore[union-attr]


def test_tool_image_message_is_not_rendered_as_a_user_turn() -> None:
    """工具读到的图片被包成 user 消息（端点只允许 user 带图）：它不能显示成用户发言。

    2026-10-20：`view_image` 的图像块改由一条 user 消息承载（ToolMessage 带图会让 DeepSeek 400），
    那条消息带 `local_agent_tool_image` 标记，投影层按标记跳过。
    """
    state = _state(messages=[
        HumanMessage(content="帮我看下这个", id="u1"),
        HumanMessage(
            content=[
                {"type": "text", "text": "（工具图片，不是用户发言）"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
            id="t1:image",
            additional_kwargs={"local_agent_tool_image": True},
        ),
    ])

    views = proj.project_messages_view(state)

    users = [view for view in views if view.kind == "user"]
    assert [user.text for user in users] == ["帮我看下这个"]  # type: ignore[union-attr]


def test_messages_without_ids_are_rejected_instead_of_rendered_vague() -> None:
    state = _state(messages=[HumanMessage(content="没有 id 的消息")])
    with pytest.raises(ValidationError):
        proj.project_messages_view(state)


def test_plan_maps_unknown_status_to_pending_and_keeps_goal() -> None:
    plan = proj.project_plan(_state(plan=[{"id": "p1", "description": "步骤", "status": "搞不懂"}]))
    assert plan is not None
    assert plan.steps[0].status == "pending"
    assert plan.goal == "补一段快速开始"


def test_plan_absent_is_none_not_an_empty_plan() -> None:
    assert proj.project_plan(_state(plan=[], task_goal="")) is None


# ---- 记忆与 RAG ------------------------------------------------------------


def test_retrieval_hit_shows_locators_only() -> None:
    state = _state(
        rag_refs=[
            {
                "path": "local-agent-web/README.md",
                "line_start": 1,
                "line_end": 20,
                "snippet": "这里是**正文**，绝不能出现在 DTO 里",
                "match_type": "literal",
                "source": "python_scan",
                "rank": 1,
            }
        ],
        memory_refs=[
            {
                "memory_id": "memo-1",
                "content": "用户偏好：中文回答（正文不得展示）",
                "scope": "user",
                "scope_key": "u1",
                "similarity": 0.81,
                "score": 0.77,
                "importance": 0.5,
                "confidence": 0.9,
                "created_at": "2026-09-20T08:00:00+00:00",
                "source_thread_id": None,
                "expires_at": None,
                "kind": "long_term_fact",
            }
        ],
    )
    memory = proj.project_memory(state)
    assert memory is not None
    assert memory.retrieval_outcome == "hit"
    assert [source.kind for source in memory.sources] == ["workspace_file", "long_term_memory"]
    assert memory.sources[0].reference == "local-agent-web/README.md:1-20"
    assert memory.sources[1].reference == "记忆 memo-1 · 写入于 2026-09-20"

    dumped = canonical_json(memory)
    for leak in ("这里是**正文**", "用户偏好", "similarity", "0.81", "0.77"):
        assert leak not in dumped


def test_retrieval_miss_and_failure_are_different_statements() -> None:
    miss = proj.project_memory(_state(memory_refs=[], rag_refs=[]))
    assert miss is not None
    assert miss.retrieval_outcome == "miss"
    assert "不会为来源留空位" in miss.retrieval_note

    failed = proj.project_memory(
        _state(
            memory_refs=[],
            rag_refs=[],
            error_events=[
                {
                    "event_id": "run-1:error:recall_memory:0::",
                    "stage": "recall_memory",
                    "message": "记忆召回失败",
                    "tool_call_id": None,
                    "run_id": "run-1",
                    "created_at": AT,
                }
            ],
        )
    )
    assert failed is not None
    assert failed.retrieval_outcome == "failed"
    assert "没有成功检索" in failed.retrieval_note


def test_no_retrieval_row_when_the_turn_never_ran_retrieval() -> None:
    state = _state()
    state.pop("memory_refs", None)
    state.pop("rag_refs", None)
    assert proj.project_memory(state) is None
    # 纯聊天的轮次不该多出一条空洞的"未命中"。
    assert not [view for view in proj.project_messages_view(state) if isinstance(view, MemoryMessageView)]


def test_memory_row_sits_right_after_the_current_user_turn() -> None:
    state = _state(memory_refs=[], rag_refs=[])
    views = proj.project_messages_view(state)
    kinds = [view.kind for view in views]
    assert kinds[:3] == ["user", "memory", "assistant"]


def test_memory_write_failure_is_reported_without_failing_the_turn() -> None:
    state = _state(
        memory_refs=[],
        rag_refs=[],
        error_events=[
            {
                "event_id": "run-1:error:memory_write_node:0::",
                "stage": "memory_write_node",
                "message": "写入失败",
                "tool_call_id": None,
                "run_id": "run-1",
                "created_at": AT,
            }
        ],
    )
    memory = proj.project_memory(state)
    assert memory is not None
    assert memory.write_outcome == "failed"
    assert "不影响本轮回答" in (memory.write_detail or "")


def test_memory_write_row_is_scoped_to_this_run() -> None:
    """写入行只能报**本轮**的落库条数。

    `memory_write_candidates` 是追加型的会话级审计字段。2026-10-08 实测：一个只做算术、完全没有
    记忆候选的轮次，快照仍显示"已写入 6 条 · 来源：本轮对话"——因为它数的是那个累计列表。
    """
    state = _state(
        memory_refs=[],
        rag_refs=[],
        # 会话里曾经写过（累计字段），但**本轮**一条都没落。
        memory_write_candidates=[
            {"memory_type": "entity_fact", "origin": "owner", "content": '{"key": "x"}'}
        ],
        memory_write_count=0,
    )
    memory = proj.project_memory(state)
    assert memory is not None
    assert memory.write_outcome is None
    assert memory.write_detail is None


def test_memory_write_row_reports_this_run_count() -> None:
    state = _state(memory_refs=[], rag_refs=[], memory_write_count=2)
    memory = proj.project_memory(state)
    assert memory is not None
    assert memory.write_outcome == "written"
    assert memory.write_detail == "2 条 · 来源：本轮对话"


def test_memory_write_row_reports_skipped_with_its_reason() -> None:
    """有候选却一条都没落：报 `skipped` 并**带上原因**（§10.4）。

    旧行为是这一行整段消失，于是"这轮没东西可记"与"想记但被跳过了"看起来一模一样。
    """
    state = _state(
        memory_refs=[],
        rag_refs=[],
        memory_write_count=0,
        memory_write_skipped="中期与长期记忆都已关闭，候选没有落库",
    )
    memory = proj.project_memory(state)
    assert memory is not None
    assert memory.write_outcome == "skipped"
    assert memory.write_detail == "中期与长期记忆都已关闭，候选没有落库"


def test_memory_write_row_prefers_written_and_failed_over_skipped() -> None:
    """优先级 failed > written > skipped：三个事实同时具备时只报最严重的那一个。"""
    failed = proj.project_memory(
        _state(
            memory_refs=[],
            rag_refs=[],
            memory_write_count=0,
            memory_write_skipped="中期与长期记忆都已关闭，候选没有落库",
            error_events=[
                {
                    "run_id": "run-1",
                    "stage": proj.STAGE_MEMORY_WRITE,
                    "code": "MEMORY_WRITE_FAILED",
                    "message": "写入失败",
                    "created_at": AT,
                }
            ],
        )
    )
    assert failed is not None and failed.write_outcome == "failed"

    written = proj.project_memory(
        _state(
            memory_refs=[],
            rag_refs=[],
            memory_write_count=1,
            memory_write_skipped="中期与长期记忆都已关闭，候选没有落库",
        )
    )
    assert written is not None and written.write_outcome == "written"


def test_prepare_context_resets_the_per_run_memory_write_count() -> None:
    """复位是"本轮口径"的另一半：没走写入节点的轮次必须回到 `None`。

    不复位的话它会像累计字段一样跨轮残留，界面又会把上一轮的条数报成这一轮的
    ——修复前实测就是这个现象（连只做算术的轮次都显示"已写入 6 条"）。
    """
    from local_agent.graph.nodes import prepare_context_node

    # 入口节点从**运行期上下文**取这次提交的图片（2026-10-16）；本用例不看图，给一个空上下文。
    update = prepare_context_node(
        {"user_input": "1 加 1 等于几？", "memory_write_count": 6},
        cast(Any, SimpleNamespace(context=SimpleNamespace(user_attachments=()))),
    )
    assert update["memory_write_count"] is None


def test_projection_agrees_with_graph_on_recall_failure() -> None:
    """投影与 `graph.nodes._recall_failed` / `_rag_search_failed` 必须同一判据。

    两处各自实现（投影层不导入节点模块），所以用一条测试把两者钉在一起，避免哪天悄悄分叉。
    """
    from local_agent.graph import nodes

    for stage in ("recall_memory", "retrieve_code_node"):
        state = _state(
            error_events=[
                {
                    "event_id": f"run-1:error:{stage}:0::",
                    "stage": stage,
                    "message": "失败",
                    "tool_call_id": None,
                    "run_id": "run-1",
                    "created_at": AT,
                }
            ]
        )
        assert proj._stage_failed(state, stage) is True
        expected = (
            nodes._recall_failed(state) if stage == "recall_memory" else nodes._rag_search_failed(state)
        )
        assert expected is True

    past = _state(
        error_events=[
            {
                "event_id": "run-0:error:recall_memory:0::",
                "stage": "recall_memory",
                "message": "上一轮失败",
                "tool_call_id": None,
                "run_id": "run-0",
                "created_at": AT,
            }
        ]
    )
    assert proj._stage_failed(past, "recall_memory") is False
    assert nodes._recall_failed(past) is False


@pytest.mark.parametrize("status", ["ok", "error", "denied"])
def test_web_sources_come_from_successful_tool_payloads_only(status: str) -> None:
    payload = {"query": "MCP", "retrieved_at": "2026-10-14T12:00:00Z", "results": [
        {"title": "MCP 文档", "url": "https://modelcontextprotocol.io/introduction"},
        {"title": "unsafe", "url": "javascript:alert(1)"},
        {"title": "private", "url": "http://127.0.0.1/"},
    ]}
    state = _state(messages=[ToolMessage(content=json.dumps(payload), tool_call_id="web")],
                   tool_results=[], tool_events=[{
                       "tool_call_id": "web", "tool_name": "web_search", "run_id": "run-1", "status": status,
                   }])
    row = proj.project_tool_rows(state)[0][1]
    assert len(row.web_sources) == (1 if status == "ok" else 0)
    if row.web_sources:
        assert row.web_sources[0].kind == "search"
        assert row.web_sources[0].retrieved_at == payload["retrieved_at"]


def test_web_search_gets_a_structured_outcome_instead_of_the_placeholder() -> None:
    """网页检索的卡片要有内容（2026-10-26）：此前它被当成"认不出的 JSON"，只剩一句占位文案。"""
    payload = {"query": "MCP", "provider": "deepseek", "truncated": False, "results": [
        {"title": "MCP 文档", "url": "https://modelcontextprotocol.io/introduction",
         "source": "modelcontextprotocol.io"},
        {"title": "MCP 规范", "url": "https://spec.example.com/", "source": "spec.example.com"},
    ]}
    state = _state(messages=[ToolMessage(content=json.dumps(payload), tool_call_id="web")],
                   tool_results=[], tool_events=[{
                       "tool_call_id": "web", "tool_name": "web_search", "run_id": "run-1", "status": "ok",
                   }])
    row = proj.project_tool_rows(state)[0][1]

    assert row.outcome is not None and row.outcome.kind == "web"
    assert row.outcome.target == "MCP"
    assert row.outcome.summary == "2 条来源 · 后端 deepseek"
    assert "MCP 文档 · modelcontextprotocol.io" in (row.outcome.text or "")
    # 卡片自己有了摘要，就不再退回"该结果格式暂无可展示的详情"
    assert row.output_preview == "2 条来源 · 后端 deepseek"


def test_web_search_failure_keeps_the_generic_renderer() -> None:
    """失败结果不是成功形状：必须走通用渲染，别用"0 条来源"盖掉真实原因。"""
    message = "DeepSeek 搜索未配置凭据：请在设置页录入"
    state = _state(
        messages=[ToolMessage(content=message, tool_call_id="web")],
        tool_results=[{
            "tool_call_id": "web", "tool_name": "web_search", "run_id": "run-1",
            "status": "error", "truncated": False, "output_preview": message,
        }],
        tool_events=[{
            "tool_call_id": "web", "tool_name": "web_search", "run_id": "run-1", "status": "error",
        }],
    )
    row = proj.project_tool_rows(state)[0][1]

    assert row.outcome is None
    assert "凭据" in (row.output_preview or "")


def test_web_fetch_outcome_reports_size_and_bounded_body() -> None:
    payload = {"url": "https://example.com/a", "title": "示例页", "text": "# 示例页\n\n正文",
               "total_chars": 11, "truncated": False,
               "note": "正文由 DeepSeek 抓取服务返回（Markdown 纯文本）"}
    state = _state(messages=[ToolMessage(content=json.dumps(payload), tool_call_id="page")],
                   tool_results=[], tool_events=[{
                       "tool_call_id": "page", "tool_name": "web_fetch", "run_id": "run-1", "status": "ok",
                   }])
    row = proj.project_tool_rows(state)[0][1]

    assert row.outcome is not None and row.outcome.kind == "web"
    assert row.outcome.target == "示例页"
    assert row.outcome.summary == "11 字符"
    body = row.outcome.text or ""
    assert body.startswith("# 示例页")
    # 来源说明必须跟着正文一起出现：读者要能判断这段正文是谁读的
    assert "DeepSeek 抓取" in body


def test_web_citation_flags_follow_the_turn_when_call_ids_are_reused() -> None:
    from local_agent.tools.web_identity import source_id

    url = "https://docs.example.com/intro"
    payload = json.dumps({"url": url, "title": "Official", "text": "content"})
    messages = [HumanMessage(content="first"),
                ToolMessage(content=payload, tool_call_id="same", name="web_fetch"),
                AIMessage(content=f"Answer [{source_id(url)}]"), HumanMessage(content="second"),
                ToolMessage(content=payload, tool_call_id="same", name="web_fetch"),
                AIMessage(content=f"Example only: `[{source_id(url)}]`")]
    events = [{"tool_call_id": "same", "tool_name": "web_fetch", "run_id": run, "status": "ok"}
              for run in ("run-1", "run-2")]
    rows = proj.project_tool_rows(_state(messages=messages, tool_results=[], tool_events=events))
    assert [(run, row.web_sources[0].cited) for run, row in rows] == [("run-1", True), ("run-2", False)]


def test_rejected_plain_text_does_not_consume_a_later_turns_reused_call_payload() -> None:
    from local_agent.tools.web_identity import source_id

    url = "https://docs.example.com/intro"
    messages = [HumanMessage(content="first"),
                ToolMessage(content="被安全策略拒绝", tool_call_id="same", name="web_fetch"),
                AIMessage(content="未取得来源"), HumanMessage(content="second"),
                ToolMessage(content=json.dumps({"url": url, "text": "actual content"}), tool_call_id="same", name="web_fetch"),
                AIMessage(content=f"已核实 [{source_id(url)}]")]
    events = [{"tool_call_id": "same", "tool_name": "web_fetch", "run_id": run, "status": status}
              for run, status in (("run-1", "denied"), ("run-2", "ok"))]
    rows = proj.project_tool_rows(_state(messages=messages, tool_results=[], tool_events=events))
    assert rows[0][1].web_sources == []
    assert rows[1][1].web_sources[0].url == url
    assert rows[1][1].web_sources[0].cited is True


# ---- 审批 ------------------------------------------------------------------


def _pending_request() -> dict[str, Any]:
    return {
        "approval_id": "ap-1",
        "run_id": "run-1",
        "tool_call_id": "t1",
        "tool_name": "run_shell_command",
        "action_fingerprint": "fp-1",
        "arguments": {"command": "rm -rf dist", "timeout_seconds": 60},
        "impact_summary": "将删除 214 个文件",
        "requires_second_confirmation": True,
        "created_at": AT,
    }


def test_approval_is_pending_while_the_interrupt_is_live() -> None:
    view = pending_views.project_approval(_state(pending_approval=_pending_request()), has_live_interrupt=True)
    assert isinstance(view, ApprovalView)
    assert view.state == "pending"
    assert view.risk == "high"
    assert view.redacted_arguments == {"command": "rm -rf dist", "timeout_seconds": "60"}
    assert view.note is None


def test_approval_expires_when_the_interrupt_is_gone() -> None:
    view = pending_views.project_approval(_state(pending_approval=_pending_request()), has_live_interrupt=False)
    assert view is not None
    assert view.state == "expired"
    assert "未执行任何操作" in (view.note or "")


def test_approval_progresses_from_executing_to_approved() -> None:
    history = [
        {
            "approval_id": "ap-1",
            "run_id": "run-1",
            "tool_call_id": "t1",
            "action_fingerprint": "fp-1",
            "decision": "approve",
            "edited_arguments": None,
            "reason": None,
            "created_at": LATER,
        }
    ]
    executing = pending_views.project_approval(
        _state(pending_approval=None, approval_history=history, tool_results=[]),
        has_live_interrupt=False,
    )
    assert executing is not None
    assert executing.state == "executing"

    approved = pending_views.project_approval(
        _state(pending_approval=None, approval_history=history),
        has_live_interrupt=False,
    )
    assert approved is not None
    assert approved.state == "approved"


def test_rejected_and_edited_approvals_keep_their_outcome() -> None:
    rejected = pending_views.project_approval(
        _state(
            pending_approval=None,
            approval_history=[
                {
                    "approval_id": "ap-1",
                    "run_id": "run-1",
                    "tool_call_id": "t1",
                    "action_fingerprint": "fp-1",
                    "decision": "reject",
                    "edited_arguments": None,
                    "reason": "范围太大",
                    "created_at": LATER,
                }
            ],
        ),
        has_live_interrupt=False,
    )
    assert rejected is not None
    assert rejected.state == "rejected"
    assert rejected.note == "范围太大"

    edited = pending_views.project_approval(
        _state(
            pending_approval=None,
            approval_history=[
                {
                    "approval_id": "ap-1",
                    "run_id": "run-1",
                    "tool_call_id": "t1",
                    "action_fingerprint": "fp-2",
                    "decision": "edit",
                    "edited_arguments": {"command": "rm -rf dist/cache"},
                    "reason": None,
                    "created_at": LATER,
                }
            ],
        ),
        has_live_interrupt=False,
    )
    assert edited is not None
    # `edit` 归入 approved，被改写的参数原样保留，不新增页面状态。
    assert edited.state == "approved"
    assert edited.edited_arguments == {"command": "rm -rf dist/cache"}


def test_no_approval_means_no_row() -> None:
    assert pending_views.project_approval(_state(), has_live_interrupt=False) is None


def test_browser_arguments_are_bounded_and_allowlisted_without_changing_execution_input() -> None:
    arguments = {
        "path": "notes.txt", "old_text": "x" * 5000,
        "expected_sha256": "private-file-hash", "prompt": "private-prompt",
        "reasoning_content": "private-reasoning", "api_key": "private-key",
    }
    state = _state(pending_approval=_pending_request() | {"tool_name": "edit_file", "arguments": arguments})
    view = pending_views.project_approval(state, has_live_interrupt=True)
    assert view is not None
    assert set(view.redacted_arguments) == {"path", "old_text"}
    assert len(view.redacted_arguments["old_text"]) < 2100
    assert "已截断" in view.redacted_arguments["old_text"]
    assert state["pending_approval"]["arguments"] == arguments
    text = canonical_json(view)
    for forbidden in ("private-file-hash", "private-prompt", "private-reasoning", "private-key", "action_fingerprint"):
        assert forbidden not in text


def test_inside_workspace_approval_uses_relative_target_and_impact(tmp_path: Path) -> None:
    view = pending_views.project_approval(
        _state(pending_approval=_pending_request() | {
            "tool_name": "edit_file", "arguments": {"path": "notes.txt"},
            "impact_summary": f"修改 {tmp_path.as_posix()}/notes.txt：202 行（T10）",
        }), has_live_interrupt=True, workspace_root=tmp_path,
    )
    assert view is not None
    assert view.target == "notes.txt"
    assert view.impact_summary == "修改 notes.txt：202 行"


def test_grep_browser_view_retains_source_lines_and_omits_unstructured_internal_fields() -> None:
    payload = {
        "path": ".", "output_mode": "content", "total_matches": 1,
        "matches": [{"path": "src/app.py", "line_number": 72, "text": "token=sk-12345678"}],
        "reasoning_content": "private-reasoning", "sha256": "private-hash",
    }
    tool = _only_tool(_tool_state("grep", payload))
    assert tool.outcome is not None and tool.outcome.kind == "search"
    assert tool.outcome.matches[0].line_number == 72
    assert tool.outcome.matches[0].path == "src/app.py"
    assert tool.outcome.matches[0].text == "token=***"
    assert tool.output_preview == "找到 1 处匹配"
    text = canonical_json(tool)
    for forbidden in ("private-hash", "private-reasoning", "sk-12345678"):
        assert forbidden not in text


def test_approval_without_recorded_ids_does_not_break_the_session_view() -> None:
    """缺 ``run_id`` / ``tool_call_id`` 的审批记录只能"如实说未记录"，**不能把会话详情接口弄 500**。

    实测过一个会话因此**完全打不开**（``GET /api/sessions/{id}`` → ``project_approval`` →
    ``ApprovalView`` 校验失败：DTO 要求这两个字段非空）。这类记录来自旧 Checkpoint——决定记录早于
    这两个字段，或 ``pending_approval`` 已被清掉。把空串直接塞进 DTO，等于**把一个字段缺失升级成
    整页不可用**；这与这一层既有的 ``_NOT_RECORDED`` 口径（"如实说没有记录，不猜一个合理的值"）一致。
    """
    decided = pending_views.project_approval(
        _state(
            pending_approval=None,
            approval_history=[
                {
                    "approval_id": "ap-1",
                    "run_id": "",
                    "tool_call_id": "",
                    "action_fingerprint": "fp-1",
                    "decision": "approve",
                    "edited_arguments": None,
                    "reason": None,
                    "created_at": LATER,
                }
            ],
        ),
        has_live_interrupt=False,
    )
    assert decided is not None
    assert decided.run_id == proj._NOT_RECORDED
    assert decided.tool_call_id == proj._NOT_RECORDED

    request = _pending_request()
    request["run_id"] = ""
    request["tool_call_id"] = ""
    pending = pending_views.project_approval(_state(pending_approval=request), has_live_interrupt=True)
    assert pending is not None
    assert pending.run_id == proj._NOT_RECORDED
    assert pending.tool_call_id == proj._NOT_RECORDED


# ---- 错误 ------------------------------------------------------------------


def _error_event(stage: str, message: str = "出错了", *, event_id: str | None = None) -> dict[str, Any]:
    return {
        "event_id": event_id or f"run-1:error:{stage}:0::",
        "stage": stage,
        "message": message,
        "tool_call_id": None,
        "run_id": "run-1",
        "created_at": AT,
    }


def test_internal_stages_map_to_public_codes_without_leaking_node_names() -> None:
    mapping = {
        "recall_memory": app_errors.MEMORY_RETRIEVAL_FAILED,
        "retrieve_code_node": app_errors.RAG_RETRIEVAL_FAILED,
        "compression_node": app_errors.CONTEXT_COMPRESSION_FAILED,
        "memory_write_node": app_errors.MEMORY_WRITE_FAILED,
        "tool_execution": app_errors.TOOL_CALL_FAILED,
        "safety_gate": app_errors.TOOL_CALL_BLOCKED,
        "approval_interrupt": app_errors.APPROVAL_FLOW_FAILED,
        # 带 `:后缀` 的 stage 各自有专码。漏一个就会落进"未分类的异常"，把正常的降级与
        # "证据不足"读成崩溃（复审实证，2026-09-28）。
        "chat_node:stream": app_errors.RUN_DEGRADED,
        "agent_decision": app_errors.MODEL_CAPABILITY_UNSUPPORTED,
        "agent_decision_node": app_errors.MODEL_CALL_FAILED,
        "chat_node": app_errors.MODEL_CALL_FAILED,
        "interrupted_turn": app_errors.RUN_INTERRUPTED,
        "reflection_node:mechanical": app_errors.EVIDENCE_NOT_VERIFIED,
        "reflection_node:llm": app_errors.RUN_DEGRADED,
        "reflection_node:final_answer": app_errors.RUN_DEGRADED,
        "reflection_node:stream": app_errors.RUN_DEGRADED,
        "reflection_node:side_effect": app_errors.SIDE_EFFECT_UNCONFIRMED,
        "reflection_node:memory_intent": app_errors.RUN_DEGRADED,
        "reflection_node:memory_final": app_errors.RUN_DEGRADED,
        # 真正未知的 stage 仍然走兜底。
        "some_unmapped_stage": app_errors.RUN_STAGE_FAILED,
    }
    for stage, expected in mapping.items():
        views = proj.project_errors(_state(error_events=[_error_event(stage)]))
        assert views[0].code == expected, stage
        assert stage not in canonical_json(views[0]), f"{stage} 泄露进了 DTO"


def test_the_mechanical_failure_notice_reaches_users_without_internal_jargon() -> None:
    """红卡正文来自节点文案、由投影**原样**透出（只换 title/hint），所以这道断言必须在投影这一层。

    2026-10-15 实测原文：「本轮没有拿到可验证的证据」+「机械校验未通过：本轮工具结果不能作为
    已验证证据」——标题是给用户看的，正文却是我们的评级名。断言里同时保留反向：去术语不能把
    "结果没核对通过、不能当依据"这个事实一起删掉。
    """
    from local_agent.graph import nodes

    views = proj.project_errors(_state(error_events=[_error_event(
        "reflection_node:mechanical", nodes.MECHANICAL_FAILURE_MESSAGE,
    )]))

    notice = views[0]
    assert notice.code == app_errors.EVIDENCE_NOT_VERIFIED
    text = f"{notice.title}{notice.message}{notice.hint}"
    for banned in ("机械", "校验", "证据等级", "verified", "failed"):
        assert banned not in text, f"红卡里出现了内部术语：{banned}"
    assert "核对" in notice.message and "依据" in notice.message


def test_errors_are_deduplicated_and_carry_actionable_text() -> None:
    event = _error_event("tool_execution")
    views = proj.project_errors(_state(error_events=[event, dict(event)]))
    assert len(views) == 1
    # 公开诊断编号稳定，但不携带内部轮次或节点名。
    assert views[0].diagnostic_id.startswith("TOOL_CALL_FAILED:")
    assert "run-1" not in views[0].diagnostic_id
    assert "tool_execution" not in views[0].diagnostic_id
    assert len(views[0].hint) > 10
    assert views[0].retryable is False


def test_errors_are_sanitized_before_reaching_the_page() -> None:
    state = _state(
        error_events=[
            _error_event(
                "tool_execution",
                "无法写入 C:\\Users\\asus\\secret.txt，key=sk-abcdefghijklmnop 已失效",
            )
        ]
    )
    view = proj.project_errors(state)[0]
    dumped = canonical_json(view)
    assert "sk-abcdefghijklmnop" not in dumped
    assert "C:\\Users\\asus\\secret.txt" in view.message
    assert "[已隐藏的绝对路径]" not in view.message
    assert "***" in view.message


def test_error_rows_in_the_flow_match_the_error_index() -> None:
    state = _state(error_events=[_error_event("tool_execution")])
    snapshot = _snapshot(state)
    flow_errors = [view for view in snapshot.messages if view.kind == "error"]
    assert len(flow_errors) == len(snapshot.errors) == 1
    assert flow_errors[0].message_id == snapshot.errors[0].diagnostic_id  # type: ignore[union-attr]
    # 行 id 也不得携带内部节点名。
    assert "tool_execution" not in flow_errors[0].message_id


def test_public_failure_text_keeps_reason_and_the_path_to_check() -> None:
    """失败文案要**能用来核对**：换掉内部工具名，但项目路径必须留下（2026-10-15，B-31 收尾）。

    原来这条断言的是"机器路径都不出现"，于是最需要核对的那句变成
    「父目录不存在，未创建文件：[已隐藏的绝对路径]」——用户看不出是哪个路径被拒。
    用户目录同样保留，见 :func:`proj.sanitize_text` 的用例。
    """
    event = _error_event("safety_gate", "工具被安全策略拒绝：read_file；目标不是文件或不存在：E:/project/private/missing.txt")
    view = proj.project_errors(_state(error_events=[event]))[0]
    assert "读取文件" in view.message
    assert "目标不是文件或不存在" in view.message
    assert "read_file" not in view.message
    # 项目路径原样保留：这才是用户拿去核对/改配置的依据。
    assert "E:/project/private/missing.txt" in view.message
    assert "[已隐藏的绝对路径]" not in view.message


def test_environment_unavailable_is_not_reported_as_a_policy_block() -> None:
    """环境不可用（找不到命令）**不能**报成"被安全策略拦截"（2026-10-14 实测）。

    `time /t` 这类命令被拒时，用户能做的是换一条等价命令，而不是"把请求改成合规"——卡片与建议
    必须跟着事实走。这条把投影层的分流钉住：stage `tool_environment` → `TOOL_UNAVAILABLE`。
    """
    event = _error_event(
        "tool_environment", "命令在当前环境不可用：运行命令（当前环境找不到可执行文件：time）"
    )

    view = proj.project_errors(_state(error_events=[event]))[0]

    assert "不可用" in view.message
    assert "安全策略" not in view.message


def test_other_runs_errors_are_not_shown_in_this_turn() -> None:
    past = _error_event("tool_execution", "上一轮的错误", event_id="run-0:error:tool_execution:0::")
    past["run_id"] = "run-0"
    assert proj.project_errors(_state(error_events=[past])) == []


def test_unparseable_error_timestamp_falls_back_to_epoch_not_now() -> None:
    event = _error_event("tool_execution")
    event["created_at"] = "不是时间"
    first = proj.project_errors(_state(error_events=[event]))[0]
    second = proj.project_errors(_state(error_events=[event]))[0]
    assert first.occurred_at == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert canonical_json(first) == canonical_json(second)


# ---- 用量、上下文、结果 ----------------------------------------------------


def test_usage_is_marked_unrecorded_instead_of_faked() -> None:
    usage = proj.project_usage(_state())
    assert usage is not None
    assert usage.usage_recorded is False
    assert usage.cache_metrics_reported is False
    assert usage.actual_output_tokens is None
    assert usage.cache_hit_ratio is None
    # 估算值仍然可用，但它是"估算"字段。
    assert usage.estimated_prompt_tokens == 84_000


def test_context_meter_never_invents_a_ratio() -> None:
    known = proj.project_context(_state())
    assert known.used_tokens == 84_000
    assert known.window_tokens == 128_000
    assert known.scope == "previous"

    unknown = proj.project_context({"run_id": "run-1", "token_usage": {}, "context_budget": {}})
    assert unknown.used_tokens is None
    assert unknown.window_tokens is None
    assert unknown.scope == "unknown"

    current = proj.project_context(_state(task_status="running"))
    assert current.scope == "current"


def test_fresh_session_reports_zero_used_instead_of_unknown() -> None:
    """新会话的「0」是事实，不是猜（2026-10-28：用户反馈"未知会让人以为是 bug"）。

    判据是"state 里连键都没有"——表示**从未组装过请求**；键在但取不到值（旧 Checkpoint、
    失败的那一轮）仍返回 `None`，不补 0。
    """
    fresh = proj.project_context({}, window_hint=128_000)
    assert fresh.used_tokens == 0
    assert fresh.window_tokens == 128_000
    # 还没有请求可描述，口径仍是"尚未组装"。
    assert fresh.scope == "unknown"

    # 连接没声明窗口时，只剩"已用 0"；比例仍然算不出来（分母未知）。
    no_window = proj.project_context({})
    assert no_window.used_tokens == 0
    assert no_window.window_tokens is None
    assert no_window.scope == "unknown"

    # 跑过但没记上用量：保持未知，绝不补 0。
    recorded_but_empty = proj.project_context(
        {"token_usage": {}, "context_budget": {}}, window_hint=128_000
    )
    assert recorded_but_empty.used_tokens is None


def test_final_result_appears_only_when_the_turn_is_terminal() -> None:
    assert proj.project_final_result(_state(task_status="running")) is None
    result = proj.project_final_result(_state())
    assert result is not None
    assert result.state == "completed"
    assert result.evidence_references == ["ev-1"]


def test_a_blocked_turn_still_reports_its_answer() -> None:
    """等用户补充而结束的一轮，回答必须发得出去（复审实证，2026-09-28）。

    ``final_response_node`` 原本对"未达成目标"的轮写 ``task_status="running"``，而 ``running``
    对外的含义是"还在跑"——于是 ``project_final_result`` 把已经生成的回答丢掉、终态事件也不发，
    页面永远停在"正在生成回答…"。触发面很宽：工具被安全策略拒绝、工具报错、重试到达上限，
    只要反思拿不到 ``verified`` 证据就会走到这里。

    ``blocked`` 与 ``running`` 的区别是**这一轮已经结束了**：回答有效，只是目标没达成。
    """
    blocked = _state(task_status="blocked")

    assert proj.project_turn_state(blocked) == "blocked"
    assert proj.project_session_state(blocked, has_live_interrupt=False) == "blocked"

    result = proj.project_final_result(blocked)
    assert result is not None
    assert result.state == "blocked"
    assert result.summary == "已补上最小启动步骤。"


@pytest.mark.parametrize(
    ("overrides", "live", "expected"),
    [
        ({}, True, "awaiting_approval"),
        ({}, False, "completed"),
        ({"task_status": "failed"}, False, "failed"),
        ({"task_status": "running", "final_response": None}, False, "running"),
        ({"task_status": "running"}, False, "running"),
    ],
)
def test_session_state_derivation(overrides: dict[str, Any], live: bool, expected: SessionState) -> None:
    state = _state(**overrides)
    assert proj.project_session_state(state, has_live_interrupt=live) == expected


# ---- 快照与确定性 ----------------------------------------------------------


def test_snapshot_is_byte_identical_for_the_same_state() -> None:
    first = _snapshot(_state())
    second = _snapshot(_state())
    assert canonical_json(first) == canonical_json(second)
    assert first.schema_version == 1
    assert first.current_task is not None
    assert first.current_task.task_id == first.current_task.run_id == "run-1"


def test_snapshot_without_a_run_has_no_task_reference() -> None:
    state = _state(run_id="", messages=[HumanMessage(content="你好", id="m1")], task_status="")
    snapshot = _snapshot(state)
    assert snapshot.current_task is None
    assert snapshot.session_state == "idle"


def test_state_items_may_be_dataclasses_not_dicts() -> None:
    """State 里的 `tool_events` / `plan` / `final_evidence` / `token_usage` 是 **dataclass**，不是 dict。

    实测踩过：只按 dict 读会让已完成的工具行退回 `pending`、计划与证据整段消失。投影必须载体无关，
    所以这条测试直接喂真实 dataclass（而不是像其它用例那样喂 dict）。
    """
    from local_agent.context.token_budget import ContextBudget, TokenUsage
    from local_agent.graph.state import EvidenceReference, PlanStep, ToolEvent

    state = _state(
        plan=[PlanStep(id="p1", description="读取 README", status="completed")],
        tool_events=[
            ToolEvent(
                tool_call_id="t1",
                tool_name="read_file",
                redacted_arguments={"path": "README.md"},
                started_at=AT,
                finished_at=LATER,
                status="ok",
                output_preview="前 20 行…",
                output_digest="sha256:abc",
                run_id="run-1",
            )
        ],
        final_evidence=[EvidenceReference(source_event_id="ev-1", evidence_type="tool", summary="README.md 已更新")],
        token_usage=TokenUsage(
            prompt_tokens=84_000,
            system_prompt_tokens=10_000,
            window_messages_tokens=70_000,
            completion_reserve_tokens=4_000,
            tool_definition_tokens=0,
        ),
        context_budget=ContextBudget(
            context_window_tokens=128_000,
            completion_reserve_tokens=4_000,
            prompt_budget_tokens=124_000,
            estimated_prompt_tokens=84_000,
            usage_ratio=0.68,
            trigger_ratio=0.8,
            triggered=False,
            window_messages=12,
            total_messages=12,
            summary_chars=0,
        ),
    )

    tools = proj.project_tools(state)
    assert [tool.status for tool in tools] == ["ok"]
    assert tools[0].output_digest == "sha256:abc"
    assert tools[0].elapsed_ms == 60_000

    plan = proj.project_plan(state)
    assert plan is not None
    assert [step.title for step in plan.steps] == ["读取 README"]

    result = proj.project_final_result(state)
    assert result is not None
    assert result.evidence_references == ["README.md 已更新"]

    context = proj.project_context(state)
    assert (context.used_tokens, context.window_tokens) == (84_000, 128_000)
    assert context.completion_reserve_tokens == 4_000  # 输出预留单列，不混进已用输入


def test_projection_does_not_mutate_its_input() -> None:
    state = _state(
        pending_approval=_pending_request(),
        memory_refs=[{"memory_id": "memo-1", "kind": "medium_term", "created_at": "2026-09-20T08:00:00+00:00"}],
    )
    before = copy.deepcopy(state)
    snapshot = _snapshot(state, has_live_interrupt=True)
    assert state == before
    # 快照里的列表是投影自己构造的，不回指输入。
    assert snapshot.messages

    dumped = canonical_json(snapshot)
    assert "sk-" not in dumped


# ---- 工具的结构化结果与机械证据（T31） --------------------------------------
#
# 结构化详情必须从 `ToolMessage` 的**完整** JSON 解析：`tool_results` 只存 2000 字符预览，
# 长一点的 JSON 会被截断而解析不出来。以下几组用例就是钉这两件事。


def _tool_state(
    tool_name: str,
    payload: Any,
    *,
    run_id: str = "run-1",
    preview: str | None = None,
) -> dict[str, Any]:
    """一次工具调用的 State；`payload` 是工具的 JSON 结果（字符串原样放，其余序列化）。"""
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    shown = preview if preview is not None else content[:2000]
    return _state(
        messages=[
            HumanMessage(content="跑一下", id="m1"),
            AIMessage(content="", id="m2", tool_calls=[{"name": tool_name, "args": {}, "id": "t1"}]),
            ToolMessage(content=content, tool_call_id="t1", id="m3"),
        ],
        tool_events=[
            {
                "tool_call_id": "t1",
                "tool_name": tool_name,
                "redacted_arguments": {},
                "approval_id": None,
                "started_at": AT,
                "finished_at": LATER,
                "status": "ok",
                "output_preview": shown,
                "output_digest": "sha256:abc",
                "run_id": run_id,
            }
        ],
        tool_results=[
            {
                "tool_call_id": "t1",
                "tool_name": tool_name,
                "status": "ok",
                "truncated": False,
                "output_preview": shown,
                "output_digest": "sha256:abc",
                "run_id": run_id,
            }
        ],
        final_evidence=[],
    )


def _only_tool(state: dict[str, Any]) -> Any:
    tools = proj.project_tools(state)
    assert len(tools) == 1
    return tools[0]


def test_delegation_outcome_lists_each_child_with_its_counts() -> None:
    """自主委派（§18.7）：子任务结局必须是**字段**，不是让用户去读一段 JSON。

    逐项对齐参照实现的任务面板（成员 / 状态 / 工具调用次数 / token 预留），另加证据条数与失败原因：
    用户要判断的是"哪一路查到了、花了多少、哪一路没成"，这三件事都得能被直接渲染。
    """
    tool = _only_tool(
        _tool_state(
            "delegate_research",
            {
                "status": "failed",
                "report": "已核对的子任务原文：\n# 取消（docs/cancel.md:1）",
                "children": [
                    {
                        "scope": "docs", "status": "completed", "tool_calls": 4, "model_calls": 1,
                        "evidence_count": 6, "error": None,
                    },
                    {
                        "scope": "src", "status": "failed", "tool_calls": 0, "model_calls": 1,
                        "evidence_count": 0, "error": "子任务超时",
                    },
                ],
                "note": "本次委派未完成：src 子任务超时。",
            },
        )
    )

    assert tool.outcome is not None
    assert tool.outcome.kind == "delegation"
    assert [(child.scope, child.status) for child in tool.outcome.children] == [
        ("docs", "completed"),
        ("src", "failed"),
    ]
    assert tool.outcome.children[0].tool_calls == 4
    assert tool.outcome.children[0].evidence_count == 6
    assert tool.outcome.children[1].error == "子任务超时"
    # **预留 token 不进这个 DTO**（2026-10-13 裁定）：它是切出来的上限、每路几乎一样，
    # 摆在对话卡里会被读成"这一路花了多少"。这条断言就是那道边界。
    assert "model_tokens_reserved" not in tool.outcome.model_dump()
    assert tool.outcome.summary is not None
    assert "2 个子任务 · 1 个已完成" in tool.outcome.summary
    # 报告正文原样带过去（有界）；页面不解析它，只把它当有界预览摆出来。
    assert "docs/cancel.md:1" in (tool.outcome.text or "")


def test_shell_outcome_separates_stdout_stderr_exit_code_and_timeout() -> None:
    tool = _only_tool(
        _tool_state(
            "run_shell",
            {
                "command": "pytest -q",
                "argv": ["pytest", "-q"],
                "cwd": ".",
                "exit_code": 1,
                "stdout": "1 failed",
                "stderr": "boom",
                "timed_out": False,
                "duration_ms": 120,
                "truncated": False,
                "note": "",
                "executor": "subprocess",
            },
        )
    )

    assert tool.outcome is not None
    assert tool.outcome.kind == "shell"
    # 退出码、超时、两个流各自成字段：不从自然语言里重新解析，也不混成一段文本。
    assert (tool.outcome.exit_code, tool.outcome.timed_out) == (1, False)
    assert (tool.outcome.stdout, tool.outcome.stderr) == ("1 failed", "boom")
    assert tool.outcome.target == "."


def test_read_outcome_is_parsed_from_the_full_message_not_the_truncated_preview() -> None:
    payload = {
        "path": "README.md",
        "encoding": "utf-8",
        "sha256": "x",
        "line_count": 3,
        "truncated": False,
        "is_binary": False,
        "content": "a\nb\nc",
        "note": "",
    }
    # 预览是被截断的 JSON（括号都没闭上），能解析出结构的只有 ToolMessage 里的完整内容。
    state = _tool_state("read_file", payload, preview='{"path": "README.md", "content": "a\\nb')
    tool = _only_tool(state)

    assert tool.outcome is not None
    assert tool.outcome.kind == "read"
    assert tool.outcome.text == "a\nb\nc"
    assert tool.outcome.total == 3


def test_read_outcome_carries_the_first_line_of_the_window() -> None:
    """带 `start_line` 的读取要**如实**带上窗口首行行号（v0.8 T73、§19.3）。

    页面据此把行号还原成文件真实行号：少了它，窗口会被当成"第 1 行起"编号，
    与 §6.7 落盘文件（`路径:行号:内容`）就对不上，也就无法核对。
    """
    payload = {
        "path": "big.txt",
        "encoding": "utf-8",
        "sha256": "x",
        "line_count": 1000,
        "truncated": False,
        "is_binary": False,
        "content": "a\nb",
        "note": "已返回第 200–201 行，共 1000 行",
        "first_line": 200,
        "last_line": 201,
        "has_more": True,
        "next_start_line": 202,
    }
    tool = _only_tool(_tool_state("read_file", payload))

    assert tool.outcome is not None
    assert tool.outcome.first_line == 200
    # 载荷里没有（旧工具载荷）就是 `None`：页面退回"页内序号"的措辞，**不编一个 1** 出来。
    without = {key: value for key, value in payload.items() if key != "first_line"}
    assert _only_tool(_tool_state("read_file", without)).outcome.first_line is None


def test_unknown_tool_and_unparseable_payload_keep_the_generic_renderer() -> None:
    # 认不出的工具不能假装有结构；解析不出也一样，必须退回安全的通用渲染器。
    assert _only_tool(_tool_state("frobnicate", {"whatever": 1})).outcome is None
    assert _only_tool(_tool_state("read_file", "这不是 JSON")).outcome is None


def test_long_bodies_are_bounded_and_marked_truncated() -> None:
    long_text = "\n".join(f"line {index}" for index in range(proj.TOOL_DETAIL_TEXT_CHARS))
    tool = _only_tool(
        _tool_state(
            "read_file",
            {
                "path": "big.txt",
                "content": long_text,
                "line_count": proj.TOOL_DETAIL_TEXT_CHARS,
                "truncated": False,
                "is_binary": False,
                "note": "",
            },
        )
    )

    assert tool.outcome is not None
    assert tool.outcome.text is not None
    assert len(tool.outcome.text) == proj.TOOL_DETAIL_TEXT_CHARS
    assert tool.outcome.truncated is True


def test_shell_streams_are_bounded_on_their_own() -> None:
    tool = _only_tool(
        _tool_state(
            "run_shell",
            {
                "cwd": ".",
                "exit_code": 0,
                "stdout": "x" * 5000,
                "stderr": "y" * 5000,
                "timed_out": False,
                "truncated": False,
                "note": "",
            },
        )
    )

    assert tool.outcome is not None
    assert len(tool.outcome.stdout or "") == proj.TOOL_DETAIL_STREAM_CHARS
    assert len(tool.outcome.stderr or "") == proj.TOOL_DETAIL_STREAM_CHARS
    assert tool.outcome.truncated is True


def test_binary_read_shows_no_body() -> None:
    tool = _only_tool(
        _tool_state(
            "read_file",
            {"path": "logo.png", "content": "", "line_count": 0, "truncated": False, "is_binary": True, "note": ""},
        )
    )

    assert tool.outcome is not None
    assert tool.outcome.text is None
    assert "二进制" in (tool.outcome.summary or "")


def test_outcome_text_and_target_are_sanitized() -> None:
    secret = "sk-" + "a" * 40
    tool = _only_tool(
        _tool_state(
            "run_shell",
            {
                "cwd": "C:\\Users\\someone\\private",
                "exit_code": 0,
                "stdout": f"token={secret}",
                "stderr": "",
                "timed_out": False,
                "truncated": False,
                "note": "",
            },
        )
    )

    assert tool.outcome is not None
    # 路径保留，密钥样式串继续脱敏。
    assert tool.outcome.target == "C:\\Users\\someone\\private"
    assert secret not in (tool.outcome.stdout or "")
    assert "***" in (tool.outcome.stdout or "")


def test_edit_outcome_carries_a_structured_diff_matching_the_unified_text() -> None:
    # 结构化 diff 必须与真正落盘前生成的那份 unified diff **逐行一致**：页面渲染 hunk 与行号，
    # 而不是自己解析一段文本（§6.1）。
    old_text = "one\ntwo\nthree\nfour\n"
    new_text = "one\nTWO\nthree\nfour\nfive\n"
    diff_text = build_unified_diff(old_text, new_text, path="notes.txt")
    tool = _only_tool(
        _tool_state(
            "edit_file",
            {
                "path": "notes.txt",
                "replaced_count": 1,
                "changed_lines": 3,
                "line_count": 5,
                "diff": diff_text,
                "reason": "",
            },
        )
    )

    assert tool.outcome is not None
    assert tool.outcome.diff is not None
    structured = tool.outcome.diff
    assert structured.path == "notes.txt"
    assert structured.additions == 2
    assert structured.deletions == 1

    # 把结构化结果"渲染回" unified diff，再与原文逐行比对。
    rebuilt = ["--- a/notes.txt", "+++ b/notes.txt"]
    for hunk in structured.hunks:
        rebuilt.append(hunk.header)
        for line in hunk.lines:
            prefix = {"add": "+", "del": "-", "context": " "}[line.kind]
            rebuilt.append(f"{prefix}{line.text}")
    assert rebuilt == diff_text.replace("\r\n", "\n").splitlines()

    # 行首 `+`/`-`/空格的语义与两侧行号必须成对：新增行没有旧行号，删除行没有新行号。
    lines = [line for hunk in structured.hunks for line in hunk.lines]
    assert [line.kind for line in lines] == ["context", "del", "add", "context", "context", "add"]
    assert [line.old_line for line in lines] == [1, 2, None, 3, 4, None]
    assert [line.new_line for line in lines] == [1, None, 2, 3, 4, 5]


def test_edit_outcome_keeps_text_when_the_diff_cannot_be_structured() -> None:
    # 认不出的形状（没有 hunk）不能编出空结构：退回文本兜底，`diff` 为 None。
    tool = _only_tool(
        _tool_state(
            "edit_file",
            {"path": "notes.txt", "replaced_count": 1, "changed_lines": 1, "diff": "不是 unified diff"},
        )
    )

    assert tool.outcome is not None
    assert tool.outcome.diff is None
    assert tool.outcome.text == "不是 unified diff"


def test_structured_diff_is_bounded_and_marked_truncated() -> None:
    old_text = "".join(f"line {index}\n" for index in range(proj.TOOL_DIFF_LINES + 200))
    new_text = "".join(f"LINE {index}\n" for index in range(proj.TOOL_DIFF_LINES + 200))
    tool = _only_tool(
        _tool_state("edit_file", {"path": "big.txt", "diff": build_unified_diff(old_text, new_text)})
    )

    assert tool.outcome is not None
    assert tool.outcome.diff is not None
    shown = sum(len(hunk.lines) for hunk in tool.outcome.diff.hunks)
    assert shown == proj.TOOL_DIFF_LINES
    assert tool.outcome.truncated is True


def test_user_message_carries_attachment_refs_and_never_the_bytes() -> None:
    """随消息发出的图片（2026-10-16）：消息 DTO 只回**引用**，绝不放字节。

    为什么要有这条：图像字节在消息的**内容块**里（`data:` URL ✗）——若投影把它当文本回给页面，
    一条消息就能把快照撑大好几 MB ✗（快照每次刷新都要传一遍 ✗）。页面真正需要的只是
    "这条消息带了哪张图" ✔：引用足够它自己去 `GET …/attachments?attachment_id=…` 取缩略图 ✔。
    """

    def ref(attachment_id: str, message_id: str = "run-1:user") -> dict[str, Any]:
        return {
            "run_id": "run-1",
            "message_id": message_id,
            "attachment_id": attachment_id,
            "media_type": "image/png",
        }

    picture = "2026/10/16/" + "a" * 32 + "/" + "b" * 16 + ".png"
    other = "2026/10/16/" + "c" * 32 + "/" + "d" * 16 + ".png"
    state = _state(
        messages=[
            HumanMessage(
                content=[
                    {"type": "text", "text": "这张图是什么颜色？"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                ],
                id="run-1:user",
            )
        ],
        # 引用是**累积字段**（落 Checkpoint）；别的消息的引用不该挂到这条上。
        user_attachment_refs=[ref(picture), ref(other, message_id="run-2:user")],
    )

    views = proj.project_messages_view(state)

    assert [view.kind for view in views] == ["user"]
    user = views[0]
    assert user.attachments == [picture]
    # 文本只取文本段；`data:` URL 一个字都不进 DTO。
    assert user.text == "这张图是什么颜色？"
    assert "base64" not in user.text


def test_a_picture_only_message_projects_with_empty_text() -> None:
    """只发图不发字（2026-10-16）：文本为空是**正常**的，引用照样在——页面据此只画图。"""
    picture = "2026/10/16/" + "a" * 32 + "/" + "b" * 16 + ".jpg"
    state = _state(
        messages=[
            HumanMessage(
                content=[
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}
                ],
                id="run-1:user",
            )
        ],
        user_attachment_refs=[
            {
                "run_id": "run-1",
                "message_id": "run-1:user",
                "attachment_id": picture,
                "media_type": "image/jpeg",
            }
        ],
    )

    user = proj.project_messages_view(state)[0]

    assert user.text == ""
    assert user.attachments == [picture]


def test_trace_events_merge_tool_approval_and_compaction_on_one_timeline() -> None:
    """轨迹事件时间线（2026-10-16）：工具 / 审批 / 压缩合成一条按时刻排序的线。

    这条钉的是**审计面**——审批要给出**等待时长**（请求→决定），压缩要给出"折叠了几条、前后差多少"，
    而不是只留一句"压缩过"。同时钉住两条边界：只投影**本轮**的；State 里是 dataclass 还是 dict
    都要读得出来（反序列化回来的 Checkpoint 是 dict，只看 `getattr` 会静默取空）。
    """
    requested = datetime(2026, 10, 16, 3, 0, 0, tzinfo=timezone.utc)
    decided = datetime(2026, 10, 16, 3, 0, 12, tzinfo=timezone.utc)
    state = _state(
        approval_history=[
            {
                "approval_id": "ap-1",
                "run_id": "run-1",
                "tool_call_id": "t1",
                "action_fingerprint": "fp",
                # 真实决策值就是这三个：`approve` / `edit` / `reject`（`security/approval.py`）。
                "decision": "approve",
                "edited_arguments": None,
                # 实测的真实形状（用户实拍）：客户端把样板文案当 reason 发上来，半角英文、与结论重复。
                "reason": "人工审批: approve",
                "created_at": decided,
                "requested_at": requested,
            }
        ],
        compaction_events=[
            {
                "operation_id": "run-1:compression:1",
                "run_id": "run-1",
                "at": datetime(2026, 10, 16, 3, 1, 0, tzinfo=timezone.utc),
                "folded_messages": 6,
                "covered_messages": 18,
                "tokens_before": 41_000,
                "tokens_after": 21_000,
                "window_tokens": 655_000,
            },
            # 别的轮次的压缩不进本轮时间线。
            {
                "operation_id": "run-2:compression:1",
                "run_id": "run-2",
                "at": datetime(2026, 10, 16, 3, 2, 0, tzinfo=timezone.utc),
                "folded_messages": 3,
                "covered_messages": 9,
                "tokens_before": 10,
                "tokens_after": 5,
                "window_tokens": 655_000,
            },
        ],
    )

    events = proj.project_trace_events(state, "run-1")

    # `_state()` 里的 tool_events 是 dict 形态（生产是 dataclass）：两种都要读出来。
    assert [event.kind for event in events] == ["tool", "approval", "compaction"]
    assert [event.event_id for event in events] == ["t1", "ap-1", "run-1:compression:1"]

    approval = events[1]
    assert approval.wait_ms == 12_000
    assert approval.status == "approve"
    # 结论用**中文枚举**当主文案（2026-10-16）：客户端把 `人工审批: approve` 这样的自由文本
    # 当 reason 发上来时，原样渲染就会漏进中文界面（而且和结论说的是同一件事）——轨迹不复制它。
    assert approval.label == "已批准"
    assert "approve" not in approval.label

    compaction = events[2]
    assert compaction.folded_messages == 6
    assert (compaction.tokens_before, compaction.tokens_after) == (41_000, 21_000)
    assert "6 条" in compaction.label


@pytest.mark.parametrize(
    "value", [None, {}, [], "not-a-number", float("nan"), float("inf"), -float("inf")],
    ids=["missing", "mapping", "list", "text", "nan", "positive-infinity", "negative-infinity"],
)
def test_trace_unknown_token_counts_do_not_break_the_timeline(value: object) -> None:
    """坏事件数字只表示未记录，不能抛异常或伪装成零。"""
    state = _state(tool_events=[], compaction_events=[{
        "operation_id": "run-1:compression:1", "run_id": "run-1", "folded_messages": 3,
        "tokens_before": value, "tokens_after": value,
    }])

    events = proj.project_trace_events(state, "run-1")

    assert len(events) == 1 and events[0].folded_messages == 3
    assert events[0].tokens_before is None and events[0].tokens_after is None
    assert events[0].model_dump(mode="json")["tokens_before"] is None


@pytest.mark.parametrize("value", [0, "0", 42, "42"])
def test_trace_numeric_token_counts_preserve_recorded_zero(value: int | str) -> None:
    state = _state(tool_events=[], compaction_events=[{
        "operation_id": "run-1:compression:1", "run_id": "run-1", "folded_messages": 3,
        "tokens_before": value, "tokens_after": value,
    }])

    event = proj.project_trace_events(state, "run-1")[0]

    assert event.tokens_before == event.tokens_after == int(value)


def test_approval_target_comes_from_the_structured_request() -> None:
    # 目标取结构化参数，不从影响摘要文本里反推；认不出的工具如实为空。
    def target(tool_name: str, arguments: dict[str, Any]) -> str | None:
        view = pending_views.project_approval(
            _state(
                pending_approval={
                    "approval_id": "ap-1",
                    "run_id": "run-1",
                    "tool_call_id": "t1",
                    "tool_name": tool_name,
                    "action_fingerprint": "fp-1",
                    "arguments": arguments,
                    "impact_summary": "影响说明",
                    "requires_second_confirmation": False,
                    "created_at": AT,
                }
            ),
            has_live_interrupt=True,
        )
        assert view is not None
        return view.target

    assert target("edit_file", {"path": "src/a.py"}) == "src/a.py"
    assert target("write_file", {"path": "src/b.py"}) == "src/b.py"
    assert target("delete_file", {"path": "dist"}) == "dist"
    assert target("run_shell", {"command": "pytest", "cwd": "sub"}) == "sub"
    # cwd 省略时与安全层同一语义：默认当前工作目录。
    assert target("run_shell", {"command": "pytest"}) == "."
    assert target("list_dir", {"path": "src"}) is None
    assert target("frobnicate", {"whatever": 1}) is None


def test_approval_shows_resolved_external_target_and_confirmation_stage(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "outside"
    view = pending_views.project_approval(
        _state(pending_approval=_pending_request() | {
            "tool_name": "delete_file",
            "arguments": {"path": external.as_posix(), "recursive": True},
            "requires_second_confirmation": True,
        }),
        has_live_interrupt=True,
        workspace_root=workspace,
        confirmation_stage=2,
    )
    assert view is not None
    assert view.target == external.as_posix()
    assert view.outside_workspace is True
    assert view.confirmation_stage == 2


def test_forward_slash_windows_path_is_preserved() -> None:
    assert proj.sanitize_text("错误位于 C:/Users/someone/private.txt") == "错误位于 C:/Users/someone/private.txt"


@pytest.mark.parametrize("status", ["error", "denied"])
def test_failed_tool_preview_keeps_reason_and_path(status: str) -> None:
    """工具行上的失败原因同样要能核对：换掉内部名，路径留下（2026-10-15，B-31 收尾）。"""
    reason = "read_file：目标不是文件或不存在：E:/project/private/missing.txt"
    state = _tool_state("read_file", reason, preview=reason)
    state["tool_events"][0]["status"] = status
    state["tool_results"][0]["status"] = status
    view = proj.project_tools(state)[0]
    assert "读取文件" in (view.output_preview or "")
    assert "目标不是文件或不存在" in (view.output_preview or "")
    assert "E:/project/private/missing.txt" in (view.output_preview or "")


def test_project_and_user_paths_with_spaces_are_preserved() -> None:
    assert proj.sanitize_text("目标 E:/pylianxi/other project/main.py") == "目标 E:/pylianxi/other project/main.py"
    assert proj.sanitize_text('目标 "C:/Users/Some Name/private.txt"') == '目标 "C:/Users/Some Name/private.txt"'


def test_approval_target_is_sanitized_and_absent_after_the_request_is_gone() -> None:
    pending = pending_views.project_approval(
        _state(
            pending_approval=_pending_request()
            | {"tool_name": "run_shell", "arguments": {"cwd": "C:\\Users\\someone\\private"}}
        ),
        has_live_interrupt=True,
    )
    assert pending is not None
    assert pending.target == "C:\\Users\\someone\\private"

    # 请求已被清掉（只剩决定）时没有原始参数可比对，如实为空而不是去别处猜。
    decided = pending_views.project_approval(
        _state(
            pending_approval=None,
            approval_history=[
                {
                    "approval_id": "ap-1",
                    "run_id": "run-1",
                    "tool_call_id": "t1",
                    "action_fingerprint": "fp-1",
                    "decision": "approve",
                    "edited_arguments": None,
                    "reason": None,
                    "created_at": LATER,
                }
            ],
        ),
        has_live_interrupt=False,
    )
    assert decided is not None
    assert decided.target is None


def test_evidence_is_attached_to_the_current_run_row() -> None:
    state = _state(
        final_evidence=[
            {
                "source_event_id": "t1",
                "evidence_type": "file_hash",
                "summary": "read_file → ok",
                "verified": True,
                "created_at": LATER,
            }
        ]
    )
    tool = _only_tool(state)

    assert tool.evidence is not None
    assert (tool.evidence.evidence_type, tool.evidence.verified) == ("file_hash", True)


def test_current_run_evidence_is_not_pasted_on_an_old_run_row() -> None:
    # 证据没有轮次字段，只按 tool_call_id 索引；贴到历史轮次的行上就等于说"本轮的检查
    # 结果适用于上一轮"。
    state = _state(
        messages=[
            HumanMessage(content="跑一下", id="m1"),
            AIMessage(content="", id="m2", tool_calls=[{"name": "read_file", "args": {}, "id": "t1"}]),
            ToolMessage(content="{}", tool_call_id="t1", id="m3"),
        ],
        tool_events=[
            {
                "tool_call_id": "t1",
                "tool_name": "read_file",
                "redacted_arguments": {},
                "status": "ok",
                "output_preview": "{}",
                "output_digest": None,
                "run_id": "run-0",
            }
        ],
        tool_results=[],
        final_evidence=[
            {"source_event_id": "t1", "evidence_type": "tool", "summary": "本轮证据", "verified": True}
        ],
    )

    assert _only_tool(state).evidence is None


def test_each_run_gets_its_own_payload_when_a_call_id_is_reused() -> None:
    first = {"path": "first.txt", "content": "first", "line_count": 1, "truncated": False, "is_binary": False, "note": ""}
    second = {"path": "second.txt", "content": "second", "line_count": 1, "truncated": False, "is_binary": False, "note": ""}
    state = _state(
        messages=[
            HumanMessage(content="第一轮", id="m1"),
            AIMessage(content="", id="m2", tool_calls=[{"name": "read_file", "args": {}, "id": "t1"}]),
            ToolMessage(content=json.dumps(first), tool_call_id="t1", id="m3"),
            HumanMessage(content="第二轮", id="m4"),
            AIMessage(content="", id="m5", tool_calls=[{"name": "read_file", "args": {}, "id": "t1"}]),
            ToolMessage(content=json.dumps(second), tool_call_id="t1", id="m6"),
        ],
        tool_events=[
            {
                "tool_call_id": "t1",
                "tool_name": "read_file",
                "redacted_arguments": {},
                "status": "ok",
                "output_preview": "",
                "output_digest": None,
                "run_id": "run-1",
            },
            {
                "tool_call_id": "t1",
                "tool_name": "read_file",
                "redacted_arguments": {},
                "status": "ok",
                "output_preview": "",
                "output_digest": None,
                "run_id": "run-2",
            },
        ],
        tool_results=[],
        final_evidence=[],
    )

    tools = proj.project_tools(state)
    # 载荷按出现顺序逐条配对：先出现的那一轮不会被后一轮的 JSON 顶掉。
    assert [tool.outcome.target if tool.outcome else None for tool in tools] == [
        "first.txt",
        "second.txt",
    ]


def test_reasoning_display_limit_and_missing_field_do_not_modify_model_history():
    thought = "证据。" * 6000
    message = AIMessage(content="回答", id="a", additional_kwargs={"reasoning_content": thought, "private_metadata": "隐藏"})
    state = _state(messages=[message])
    view = proj.project_messages_view(state)[0]
    assert view.reasoning_truncated and len(view.reasoning) == proj.REASONING_DISPLAY_LIMIT
    assert message.additional_kwargs["reasoning_content"] == thought
    assert "private_metadata" not in canonical_json(_snapshot(state))
    missing = proj.project_messages_view(_state(messages=[AIMessage(content="回答", id="b")]))[0]
    assert missing.reasoning is None and not missing.reasoning_truncated

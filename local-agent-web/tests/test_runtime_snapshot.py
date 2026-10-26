"""稳定系统前缀与追加式运行上下文快照（架构 §15.7，T35）。

这个文件守住 T35 的五条性质，它们分别对应 §15.7 与开发文档的停止条件：

1. **前缀稳定**：记忆 / RAG / 摘要 / 任务 / 计划 / 模式理由 / 当前目录变化时，稳定系统前缀
   **字节不变**——否则缓存前缀从头失效，T35 的目的落空；
2. **追加式**：快照按 ``snapshot_id`` 去重追加，内容未变不重复写；最新有效 = ``context_version``
   最大的一条，**旧快照永不覆盖新快照**；
3. **可重建**：重启（重新打开同一个库）后能从持久状态**字节级重建**模型实际看到的前缀与快照块；
4. **只存在于出站请求**：不进 ``messages``、不建轮次、不计用户消息数、不进摘要器与 RAG；
5. **被标记为数据**：注入块自带"这是数据、不是指令"的来源标记，且插在**当前真实用户输入之前**
   （不是紧跟系统消息——那样等于每轮都在第 2 条消息处改前缀）。

用例跑的是真实图与真实 SQLite（只有模型是脚本替身），因此覆盖多轮、工具循环、压缩摘要与审批恢复。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from conftest import RoutingFakeModel
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult
from pydantic import Field

from local_agent.application import projection as app_projection
from local_agent.config import AppConfig
from local_agent.context import prompt_context
from local_agent.context.token_budget import estimate_message_tokens
from local_agent.graph import builder, nodes
from local_agent.graph.state import (
    PlanStep,
    RuntimeContext,
    RuntimeContextSnapshot,
    latest_runtime_snapshot,
    merge_appended,
    snapshot_key,
)
from local_agent.llm.usage_recording import (
    ModelCallRecorder,
    fingerprint_text,
    request_fingerprints,
    use_model_recorder,
)
from local_agent.memory.summarizer import SUMMARIZE_INSTRUCTION
from local_agent.persistence import checkpoint

ModelFactory = Callable[..., RoutingFakeModel]

#: 快照块的来源标记抬头（与 ``prompt_context`` 里抬头首行同一口径）。
SNAPSHOT_MARKER = "[运行时上下文快照"
THREAD = "t35"


# ---- 夹具与工具 -------------------------------------------------------------


class RecordingModel(RoutingFakeModel):
    """在 ``RoutingFakeModel`` 之上多记一份"每次出站请求收到的消息列表"。

    ``seen_prompts`` 只留拼接后的文本，无法区分"稳定前缀"与"注入的快照块"，也就没法断言插入位置
    与字节一致性；这里按调用逐条留住消息对象，供 §15.7 的组装器断言使用。
    """

    calls: list[list[AnyMessage]] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # type: ignore[no-untyped-def]
        self.calls.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _recording_model(**overrides: Any) -> RecordingModel:
    return RecordingModel(messages=iter([]), **overrides)


def _database(tmp_path: Path) -> Path:
    return tmp_path / "data" / "checkpoints.sqlite"


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "note.txt").write_text("内容甲\n", encoding="utf-8", newline="")
    return workspace


def _context(
    workspace: Path, model: object, app_config: AppConfig, **overrides: Any
) -> RuntimeContext:
    return builder.build_runtime_context(
        app_config=app_config,
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=workspace,
        current_cwd=overrides.pop("current_cwd", workspace),
        **overrides,
    )


def _compression_config(app_config: AppConfig, **overrides: Any) -> AppConfig:
    """打开压缩：第 2 轮起就会有会话摘要，动态段因此真的变化。

    用 ``model_copy`` 而不是 ``model_dump + model_validate``：``trigger_ratio=0.0`` 是"每轮都压"的
    合法测试配置，但会被字段校验挡下（既有压缩用例同样是这么绕开的）。
    """
    compression = {"enabled": True, "trigger_ratio": 0.0, "preserve_recent_turns": 1}
    compression.update(overrides)
    return app_config.model_copy(
        update={
            "memory": app_config.memory.model_copy(
                update={
                    "short_term": app_config.memory.short_term.model_copy(
                        update={"prompt_window_messages": 100}
                    ),
                    "compression": app_config.memory.compression.model_copy(update=compression),
                }
            )
        }
    )


def _config_dict(thread_id: str = THREAD) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _run(
    graph: builder.ChatGraph, context: RuntimeContext, text: str, thread_id: str = THREAD
) -> dict[str, Any]:
    return dict(
        graph.invoke(
            {"user_input": text, "thread_id": thread_id},
            config=_config_dict(thread_id),
            context=context,
        )
    )


def _is_snapshot(message: AnyMessage) -> bool:
    return isinstance(message, HumanMessage) and str(message.content).startswith(SNAPSHOT_MARKER)


def _snapshot_blocks(model: RecordingModel) -> list[list[str]]:
    """每次出站请求里的快照块正文（一次请求可能有 0 或 1 块）。"""
    return [[str(message.content) for message in call if _is_snapshot(message)] for call in model.calls]


def _system_texts(model: RecordingModel) -> list[str]:
    """每次**带快照块**的出站请求里的系统消息正文（= 稳定系统前缀），按调用顺序。"""
    return [
        next(str(message.content) for message in call if isinstance(message, SystemMessage))
        for call, block in zip(model.calls, _snapshot_blocks(model))
        if block
    ]


# ---- 1. 稳定前缀不随动态上下文变化 -------------------------------------------


def test_stable_prefix_ignores_every_dynamic_field(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """稳定前缀只来自运行上下文与模式：State 里的动态字段怎么变都影响不到它。"""
    workspace = _workspace(tmp_path)
    context = _context(workspace, model_factory(decision="tool", reply="x"), app_config)
    dynamic: dict[str, Any] = {
        "run_id": "run-1",
        "route_reason": "LLM 分类：需要读文件",
        "memory_refs": [{"content": "用户偏好中文", "similarity": 0.9, "kind": "medium_term"}],
        "conversation_summary": "前几轮都在问好",
        "task_goal": "把 note.txt 的内容读出来",
        "plan": [],
        "context_version": 3,
    }

    before = nodes._stable_prompt(dynamic, context).system_prompt
    after = nodes._stable_prompt(
        {
            **dynamic,
            "route_reason": "规则兜底：闲聊",
            "memory_refs": [],
            "conversation_summary": None,
            "task_goal": None,
            "context_version": 7,
        },
        context,
    ).system_prompt

    assert before == after
    # 与此同时动态段**必须**跟着变，否则"移出前缀"就成了信息丢失。
    assert nodes._snapshot_text(dynamic, context) != nodes._snapshot_text(
        {**dynamic, "memory_refs": [], "conversation_summary": None}, context
    )


def test_current_directory_moves_into_the_snapshot(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """当前目录是动态来源之一（§15.7）：只进快照；工作区根目录留在稳定前缀（裁定 B）。"""
    workspace = _workspace(tmp_path)
    child = workspace / "pkg"
    child.mkdir()
    model = model_factory(decision="tool", reply="x")
    root_context = _context(workspace, model, app_config)
    child_context = _context(workspace, model, app_config, current_cwd=child)
    state = {"run_id": "run-1"}

    root_prefix = nodes._stable_prompt(state, root_context).system_prompt
    assert root_prefix == nodes._stable_prompt(state, child_context).system_prompt
    assert workspace.as_posix() in root_prefix
    assert nodes._snapshot_text(state, root_context) != nodes._snapshot_text(state, child_context)
    assert "pkg" in nodes._snapshot_text(state, child_context)


def test_stable_prefix_is_byte_identical_across_turns_while_the_summary_changes(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """真实多轮：第 2 轮起摘要进入快照，稳定前缀仍与前一轮逐字节相同。"""
    workspace = _workspace(tmp_path)
    model = _recording_model(decision="chat", reply="你好")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    context = _context(workspace, model, _compression_config(app_config))

    for text in ("第一句", "第二句", "第三句"):
        _run(graph, context, text)

    # 旧锚点覆盖后保留历史冻结块；末块才是本轮当前快照。
    blocks = [block[-1] for block in _snapshot_blocks(model) if block]
    assert len(blocks) == 3
    assert len(set(_system_texts(model))) == 1  # 三轮的稳定前缀逐字节相同
    # 第 1 轮还没有摘要，第 2 轮起摘要进了快照：动态段确实变了，前缀却没变。
    assert "（本会话还没有摘要）" in blocks[0]
    assert "会话摘要（假模型）" in blocks[1]
    assert "## 项目规则" in _system_texts(model)[0]


# ---- 2. 追加式与去重 ---------------------------------------------------------


def test_same_state_produces_the_same_snapshot_and_is_not_appended_twice(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """同一状态两次构造得到同一个 ``snapshot_id``；``merge_appended`` 因此不会重复追加。"""
    workspace = _workspace(tmp_path)
    context = _context(workspace, model_factory(decision="chat", reply="x"), app_config)
    state = {"run_id": "run-1", "context_version": 0, "route_reason": "规则兜底：闲聊"}

    first = nodes._runtime_snapshot(state, context)
    second = nodes._runtime_snapshot(state, context)

    assert first.snapshot_id == second.snapshot_id
    assert first.content_hash == second.content_hash
    assert first.canonical_text == second.canonical_text
    # ``merge_appended`` 只返回"新增"的条目：同 id 的第二次写入被吃掉，State 里只留一条。
    assert merge_appended([], [first], snapshot_key) == [first]
    assert merge_appended([first], [second], snapshot_key) == []


def test_snapshot_ids_stay_unique_inside_a_real_run(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """真实工具循环里也不会有重复 id（去重靠 ``snapshot_id``，不是靠调用次数）。"""
    workspace = _workspace(tmp_path)
    model = model_factory(
        decision="tool",
        reply="读完了",
        repeated_tool_calls=True,
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
        plan_steps=["读取 note.txt", "总结内容"],
    )
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))

    result = _run(graph, _context(workspace, model, app_config), "读一下 note.txt")

    snapshots = list(result["runtime_context_snapshots"])
    assert snapshots
    ids = [snapshot.snapshot_id for snapshot in snapshots]
    assert len(ids) == len(set(ids))


def test_old_snapshot_never_overrides_the_new_one(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """最新有效 = ``context_version`` 最大的一条；追加顺序不影响结论。"""
    workspace = _workspace(tmp_path)
    context = _context(workspace, model_factory(decision="chat", reply="x"), app_config)
    old = nodes._runtime_snapshot(
        {"run_id": "run-1", "context_version": 1, "conversation_summary": "旧摘要"}, context
    )
    new = nodes._runtime_snapshot(
        {"run_id": "run-1", "context_version": 2, "conversation_summary": "新摘要"}, context
    )

    assert latest_runtime_snapshot([old, new]) is new
    assert latest_runtime_snapshot([new, old]) is new
    # 旧记录留着（审计与重建），只是不再被选中。
    assert latest_runtime_snapshot([old]).canonical_text != new.canonical_text  # type: ignore[union-attr]


# ---- 3. 重启后按持久状态字节级重建 -------------------------------------------


def test_restart_rebuilds_the_same_request_bytes(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """重启后从 Checkpoint 重建：稳定前缀与快照块都与原请求逐字节相同。"""
    workspace = _workspace(tmp_path)
    model = _recording_model(decision="chat", reply="你好")
    database = _database(tmp_path)
    context = _context(workspace, model, app_config)
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    _run(graph, context, "你好")

    seen_prefix = _system_texts(model)[0]
    seen_snapshot = next(block for block in _snapshot_blocks(model) if block)[0]

    reopened = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    state = dict(reopened.get_state(_config_dict()).values)
    latest = latest_runtime_snapshot(state["runtime_context_snapshots"])

    assert latest is not None
    assert latest.canonical_text == seen_snapshot  # 快照块字节级一致
    stable = nodes._stable_prompt(state, _context(workspace, model, app_config)).system_prompt
    # 重建出的稳定前缀必须逐字节等于原请求的**前缀**。
    #
    # 2026-10-26 核对：`chat_node` 在稳定前缀之后追加一段**常量**阶段指令
    # （`_CHAT_ANSWER_INSTRUCTION`，`745cd27` 引入），它不来自 State、也不随重启变化。
    # 拿稳定前缀直接比整条 system，会把这段常量当成"重建丢了内容"——这条用例当时就是
    # 这么红的，代码没问题、期望过时。
    assert seen_prefix == stable + "\n\n" + nodes._CHAT_ANSWER_INSTRUCTION


# ---- 4. 快照只存在于出站请求 -------------------------------------------------


def test_snapshot_stays_out_of_messages_and_turn_accounting(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """快照不进 ``messages``：不显示为用户气泡、不建轮次、不计用户消息数。"""
    workspace = _workspace(tmp_path)
    model = _recording_model(decision="chat", reply="你好")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    context = _context(workspace, model, app_config)

    _run(graph, context, "你好")
    result = _run(graph, context, "再说一句")

    texts = [message.text for message in result["messages"]]
    assert not any(text.startswith(SNAPSHOT_MARKER) for text in texts)
    assert texts[-4:] == ["你好", "你好", "再说一句", "你好"]  # 两轮各 1 条用户 + 1 条回答
    views = app_projection.project_messages_view(result, secrets=())
    # 投影出的消息视图里没有快照日志（视图是判别联合，逐条序列化后统一检查标记）。
    assert not any(SNAPSHOT_MARKER in view.model_dump_json() for view in views)
    # 用户消息计数不受影响：两个用户轮 = 两条 user 角色消息。
    assert sum(1 for view in views if view.kind == "user") == 2


def test_snapshot_never_reaches_the_summarizer(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """摘要器只拿历史消息：注入的快照块不出现在任何一次摘要请求里。"""
    workspace = _workspace(tmp_path)
    model = _recording_model(decision="chat", reply="你好")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    context = _context(workspace, model, _compression_config(app_config))

    for text in ("第一句", "第二句", "第三句"):
        _run(graph, context, text)

    summary_calls = [prompt for prompt in model.seen_prompts if SUMMARIZE_INSTRUCTION in prompt]
    assert summary_calls  # 确实发生过摘要
    assert all(SNAPSHOT_MARKER not in prompt for prompt in summary_calls)


# ---- 5. 数据标记与插入位置 ---------------------------------------------------


class _FakeRagConfig:
    enabled = True
    section_chars = 4000


class _FakeSkillsConfig:
    enabled = True
    include_user_skills = False
    user_dirs: tuple[str, ...] = ()
    max_skill_chars = 20_000
    max_list_chars = 8_000


class _FakeAppConfig:
    rag = _FakeRagConfig()
    skills = _FakeSkillsConfig()


class _FakeContext:
    """只提供渲染快照正文所需的字段（避免为纯文本断言拉起整条运行链路）。"""

    workspace_root = Path("/ws")
    current_cwd = Path("/ws")
    app_config = _FakeAppConfig()
    #: 用户在设置里禁用的技能（2026-10-16）：与 `RuntimeContext` 同名字段对齐，
    #: 否则"技能清单要不要过滤"这条语义在快照用例里根本走不到。
    disabled_skills: frozenset[str] = frozenset()


def test_skills_listing_hides_the_skills_the_user_disabled(tmp_path: Path) -> None:
    """用户在「设置 → 技能」里禁用的技能不进清单（2026-10-16；§16.5）。

    与 `skill_load` 共用同一个判定点（`skills_loader.apply_skill_overrides`）：只过滤清单、正文还能取，
    等于用户以为关掉了其实没关——那一半在 `test_skill_tools.py` 里有对应用例，这里钉清单这一半。
    """
    from local_agent.graph import nodes

    root = tmp_path / "skills-root"
    for name in ("keep-me", "drop-me"):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: 做一件事\n---\n正文\n", encoding="utf-8"
        )

    skills = _FakeSkillsConfig()
    skills.user_dirs = (str(root),)  # type: ignore[assignment]
    config = _FakeAppConfig()
    config.skills = skills  # type: ignore[assignment]

    context = _FakeContext()
    context.workspace_root = tmp_path
    context.current_cwd = tmp_path
    context.app_config = config  # type: ignore[assignment]
    context.agent_capabilities = ("files_read", "skill")  # type: ignore[attr-defined]

    before = nodes._skills_listing(context)  # type: ignore[arg-type]
    assert before is not None and "keep-me" in before and "drop-me" in before

    context.disabled_skills = frozenset({"drop-me"})
    after = nodes._skills_listing(context)  # type: ignore[arg-type]
    assert after is not None and "keep-me" in after and "drop-me" not in after

    # 全禁掉之后没有清单可言：不该在快照里留一段空标题。
    context.disabled_skills = frozenset({"keep-me", "drop-me"})
    assert nodes._skills_listing(context) is None  # type: ignore[arg-type]


def test_snapshot_has_no_skills_section_by_default() -> None:
    """不给技能清单时，快照里既没有那一段，来源列表里也没有 `skills`。

    这条守的是"关闭新增能力后逐字节不变"：默认配置（`skills.enabled=false`）的快照必须与 v0.4 一样。
    """
    text = prompt_context.build_context_snapshot_text(context=_FakeContext())  # type: ignore[arg-type]

    assert "## 可用技能" not in text
    assert ",skills" not in text


def test_environment_section_appears_only_when_given() -> None:
    """运行环境段落（2026-10-14）：**传了才出现**，抬头把 `env` 计入来源；不传时正文保持既有口径。"""
    from local_agent.context import prompt_context

    without = prompt_context.build_context_snapshot_text(context=_FakeContext())  # type: ignore[arg-type]
    assert "## 运行环境" not in without
    assert ",env" not in without

    with_env = prompt_context.build_context_snapshot_text(  # type: ignore[arg-type]
        context=_FakeContext(),
        environment_text="当前时间：2026-10-14（周三）· 本地 UTC+08:00\n",
    )
    assert "## 运行环境" in with_env
    assert ",env" in with_env
    assert "当前时间：2026-10-14" in with_env


def test_environment_text_states_time_boundary_and_network() -> None:
    """三条边界事实由**运行环境**给出，而不是让模型试探（2026-10-14 的三个实证问题）。"""
    from datetime import datetime, timezone
    from pathlib import Path
    from types import SimpleNamespace

    from local_agent.context import prompt_context

    def _context(*, capabilities: tuple[str, ...], mode: str) -> object:
        return SimpleNamespace(
            current_time=datetime(2026, 10, 14, 2, 38, tzinfo=timezone.utc),
            # 中立路径（原来是开发机上的真实路径）：这条断言要能在**任何机器**上跑，
            # 写死盘符与目录名的用例换台机器就红（2026-10-16 修）。
            workspace_root=Path("/ws"),
            agent_capabilities=capabilities,
            security_policy=SimpleNamespace(external_policy=SimpleNamespace(mode=mode)),
        )

    text = prompt_context.build_environment_text(  # type: ignore[arg-type]
        context=_context(capabilities=("files_read", "web"), mode="danger-full-access")
    )
    # 日期按**本地时区**渲染：断言用同一套换算，避免测试机时区不同就挂。
    expected = datetime(2026, 10, 14, 2, 38, tzinfo=timezone.utc).astimezone()
    assert f"当前时间：{expected:%Y-%m-%d}" in text
    # **2026-10-16 补时分**：这一段原来只渲染到日期，标签却写着"当前时间"——用户问"现在几点"时，
    # 模型看到标签以为有时间、实际只有日期，于是去调命令取时钟（还因此撞过审批），拿到结果后又把
    # 命令抄进正文。给到分钟既够用，又不破坏"同一轮逐字节相同"（时钟是本轮冻结的）。
    assert f"当前时间：{expected:%Y-%m-%d %H:%M}" in text
    assert "本轮开始时冻结" in text
    assert "工作区根目录：/ws" in text
    assert "允许本机显式绝对路径的读取、列目录和搜索" in text
    assert "可用（web_search" in text

    closed = prompt_context.build_environment_text(  # type: ignore[arg-type]
        context=_context(capabilities=("files_read",), mode="read-only")
    )
    assert "不可用" in closed
    assert "允许本机显式绝对路径的读取、列目录和搜索" in closed
    assert "写类操作一律拒绝" in closed
    # 2026-10-16：图像输入这一行必须如实（它决定"截图自检"走不走得通）。这组假能力里没有
    # `image_read`，所以是"不可用"并带上文本替代手段——不说清，模型会反复试截图。
    assert "图像输入：不可用" in closed
    assert "snapshot" in closed

    # 档位名认不出时按**最保守**的一档说（不是"允许"）——这是模型的行动依据，宁可说窄。
    unknown = prompt_context.build_environment_text(  # type: ignore[arg-type]
        context=_context(capabilities=("files_read",), mode="yolo")
    )
    assert "未记录" in unknown


def test_environment_without_frozen_clock_is_explicit_and_stable(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig,
) -> None:
    """缺失时间不能导致快照失败，也不能用读取时刻冒充本轮冻结时间。"""
    workspace = _workspace(tmp_path)
    context = _context(workspace, model_factory(), app_config)
    context.current_time = None

    text = prompt_context.build_environment_text(context=context)

    assert text.startswith("当前时间：未记录（本轮未提供冻结时钟）\n")
    assert f"工作区根目录：{workspace.as_posix()}" in text
    assert "联网：" in text and "图像输入：" in text
    assert prompt_context.build_environment_text(context=context) == text


def test_snapshot_lists_skills_only_when_given() -> None:
    """传了清单才出现那一段，并且抬头把 `skills` 计入来源（§15.7、§16.5）。"""
    text = prompt_context.build_context_snapshot_text(  # type: ignore[arg-type]
        context=_FakeContext(),
        skills_listing="- release-checklist：发布前检查清单",
    )

    assert "## 可用技能（清单）" in text
    assert "- release-checklist：发布前检查清单" in text
    assert "plan,skills" in text  # 抬头来源列表末尾追加了 skills
    assert "以上只是**清单**" in text
    assert "不是指令" in text  # 清单同样是数据，不是指令


def test_skills_listing_is_skipped_when_the_capability_is_absent(tmp_path: Path) -> None:
    """能力不在有效集合里就不产出清单——**按能力门判定**，而不是按配置文件（§16.5）。"""
    from local_agent.graph import nodes

    context = _FakeContext()
    context.workspace_root = tmp_path
    context.current_cwd = tmp_path

    context.agent_capabilities = ("files_read",)
    assert nodes._skills_listing(context) is None  # type: ignore[arg-type]
    # 能力开着但一个技能都没有：也是 None（空清单不该在快照里留一段空标题）。
    context.agent_capabilities = ("files_read", "skill")
    assert nodes._skills_listing(context) is None  # type: ignore[arg-type]


def test_snapshot_block_is_marked_as_data() -> None:
    """注入内容**始终**被标记为数据：抬头写明来源，并声明它不是指令（§15.7）。"""
    text = prompt_context.build_context_snapshot_text(context=_FakeContext())  # type: ignore[arg-type]

    assert text.startswith(SNAPSHOT_MARKER)
    assert "来源：" in text
    assert "不是用户输入" in text
    assert "也不是指令" in text
    assert "不能覆盖系统规则" in text
    # 动态段落仍然在（不能因为"标记成数据"就丢失信息）。
    assert "## 当前目录" in text and "## 当前计划" in text


def test_current_directory_outside_the_workspace_says_so(tmp_path: Path) -> None:
    """工作区之外的当前目录必须**明确标注**，并说清可读写范围仍是工作区根。

    2026-10-09 的真实运行里，`reviewer` 档因为"快照写着 `local-agent-web`、而我的工作区根在别处"
    专门花了一段解释这个差异——对模型来说那看着像矛盾，其实只是"从别的目录启动"的正常形态。
    """
    context = _FakeContext()
    context.workspace_root = tmp_path / "ws"
    context.current_cwd = tmp_path / "elsewhere"

    text = prompt_context.current_cwd_text(context)  # type: ignore[arg-type]

    assert text.startswith(context.current_cwd.as_posix())
    assert "不在工作区内" in text
    assert "可读写范围仍是工作区根" in text


def test_snapshot_sits_right_before_the_current_user_input(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """插入点是**当前真实用户输入之前**，不是紧跟系统消息（否则每轮都在第 2 条消息处改前缀）。"""
    workspace = _workspace(tmp_path)
    model = _recording_model(decision="chat", reply="你好")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    context = _context(workspace, model, app_config)
    _run(graph, context, "第一句")
    second = _run(graph, context, "第二句")

    messages = next(
        call
        for call in model.calls
        if any(_is_snapshot(message) for message in call)
        and any(message.id == second["run_user_message_id"] for message in call)
    )
    snapshot_index = max(
        index for index, message in enumerate(messages) if _is_snapshot(message)
    )
    user_index = next(
        index
        for index, message in enumerate(messages)
        if message.id == second["run_user_message_id"]
    )

    assert messages[0].type == "system"
    assert snapshot_index == user_index - 1
    assert snapshot_index != 1  # 当前快照追加在后面；历史快照仍在原位。
    assert messages[snapshot_index].text == latest_runtime_snapshot(
        second["runtime_context_snapshots"]
    ).canonical_text  # type: ignore[union-attr]


def test_snapshot_does_not_split_a_tool_call_pair(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工具循环里插入点稳定：不会插到 ``tool_calls`` 与它的 ``ToolMessage`` 之间。"""
    workspace = _workspace(tmp_path)
    model = _recording_model(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
    )
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))

    result = _run(graph, _context(workspace, model, app_config), "读一下 note.txt")

    assert result["task_status"] == "completed"
    # 决策请求（带工具、带快照块）：快照块后面紧跟的是本轮用户消息，配对不被切开。
    decision = next(
        call
        for call in model.calls
        if any(_is_snapshot(message) for message in call)
        and any(getattr(message, "tool_calls", None) for message in call)
    )
    snapshot_index = next(
        index for index, message in enumerate(decision) if _is_snapshot(message)
    )
    assert decision[snapshot_index + 1].id == result["run_user_message_id"]


# ---- 6. 投影（§15.8）-------------------------------------------------------


def test_runtime_context_view_hides_the_text_and_never_fabricates(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """页面只看得到"存在哪个版本、来源是什么、正文多长"，看不到正文；没有快照就是 ``None``。"""
    workspace = _workspace(tmp_path)
    context = _context(workspace, model_factory(decision="chat", reply="x"), app_config)
    snapshot = nodes._runtime_snapshot(
        {"run_id": "run-1", "context_version": 2, "conversation_summary": "摘要甲"}, context
    )

    assert app_projection.project_runtime_context({}) is None
    assert app_projection.project_runtime_context({"runtime_context_snapshots": []}) is None

    view = app_projection.project_runtime_context(
        {
            "runtime_context_snapshots": [
                nodes._runtime_snapshot({"run_id": "run-1", "context_version": 1}, context),
                snapshot,
            ]
        }
    )

    assert view is not None
    assert view.snapshot_id == snapshot.snapshot_id
    assert view.context_version == 2
    assert view.content_hash == snapshot.content_hash
    assert view.text_length == len(snapshot.canonical_text)
    assert {item.source for item in view.source_versions} >= {"mode_reason", "current_cwd", "plan"}
    # 没有 ``canonical_text`` 字段，正文也不会从别的字段漏出去（§15.8）。
    assert "canonical_text" not in view.model_dump()
    assert snapshot.canonical_text not in view.model_dump_json()


# ---- 7. 用量口径 ------------------------------------------------------------


def test_recorder_prefers_the_snapshot_fingerprint(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """动态段指纹优先取**快照指纹**：State 字段变了、快照没变时，指纹不漂移。"""
    recorder = ModelCallRecorder(
        connection_id="conn-1",
        config_version="1",
        provider="openai_compatible",
        model_id="deepseek-chat",
        endpoint_fingerprint="fp",
        context_window_tokens=65_536,
    )
    messages = [SystemMessage(content="稳定前缀"), HumanMessage(content="同一句提问")]
    with use_model_recorder(recorder):
        recorder.bind_snapshot("hash-1")
        recorder.bind_dynamic_context({"context_version": 1, "plan": []})
        first = recorder.begin(
            purpose="chat", estimated_prompt_tokens=1, tools_schema_fingerprint=None,
            retry_of=None, messages=messages,
        )
        recorder.bind_dynamic_context({"context_version": 2, "plan": ["换了计划"]})
        second = recorder.begin(
            purpose="chat", estimated_prompt_tokens=1, tools_schema_fingerprint=None,
            retry_of=None, messages=messages,
        )

    assert first.dynamic_context_fingerprint == second.dynamic_context_fingerprint
    assert first.system_prompt_fingerprint == second.system_prompt_fingerprint


def test_recorder_gets_the_snapshot_fingerprint_end_to_end(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """真实出站链路上，录音机的动态指纹确实含**快照指纹**（不是只靠 State 字段兜底）。

    这条用例补上"绑定真的生效"这一环：只测 ``bind_snapshot`` 的优先级说明不了节点里有没有调它。
    """
    workspace = _workspace(tmp_path)
    model = _recording_model(decision="chat", reply="你好")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    recorder = ModelCallRecorder(
        connection_id="conn-1",
        config_version="1",
        provider="openai_compatible",
        model_id="deepseek-chat",
        endpoint_fingerprint="fp",
        context_window_tokens=65_536,
    )
    # 录音机走 RuntimeContext（`_usage_aware` 包装器据此设置记录上下文），
    # 而不是在测试里直接 `use_model_recorder`：包装器会用自己的值覆盖外层上下文。
    context = _context(workspace, model, app_config, usage_recorder=recorder)
    result = _run(graph, context, "你好")

    snapshot = latest_runtime_snapshot(result["runtime_context_snapshots"])
    assert snapshot is not None
    outbound = next(call for call in model.calls if any(_is_snapshot(item) for item in call))
    _, _, messages_dynamic = request_fingerprints(outbound)
    chat = next(record for record in recorder.finished_records() if record.purpose == "chat")

    # 口径：hash([快照指纹, 出站消息在 latest_user 之后的指纹])。
    assert chat.dynamic_context_fingerprint == fingerprint_text(
        json.dumps([snapshot.content_hash, messages_dynamic], separators=(",", ":"))
    )


def test_snapshot_tokens_are_inside_the_prompt_budget(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """分项之和 = 总量（T34 口径不回退），且快照块计入系统提示分项。"""
    workspace = _workspace(tmp_path)
    model = _recording_model(decision="chat", reply="你好")
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    context = _context(workspace, model, app_config)

    result = _run(graph, context, "你好")

    usage = result["token_usage"]
    assert (
        usage.system_prompt_tokens + usage.window_messages_tokens + usage.tool_definition_tokens
    ) == usage.prompt_tokens
    state = {"run_id": result["run_id"], "route_reason": result.get("route_reason", "")}
    assert usage.system_prompt_tokens == (
        nodes._stable_prompt(state, context).estimated_tokens
        + nodes._snapshot_tokens(state, context)
    )


# ---- 8. 回归：RAG 开关、召回/检索失败、审批恢复 -------------------------------


def test_snapshot_marks_rag_disabled_failed_and_empty_apart(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """RAG 未启用 / 检索失败 / 检索无结果三种占位在快照里仍然分得开（T35 不得混说）。"""
    workspace = _workspace(tmp_path)
    # **显式关掉检索**：2026-10-01 起 ``rag.enabled`` 默认为 ``true``，不能靠默认值拿"未启用"这一支
    # （否则这条测的是别的东西，而且默认值一变就莫名其妙地红）。
    off_config = app_config.model_copy(
        update={"rag": app_config.rag.model_copy(update={"enabled": False})}
    )
    context = _context(workspace, model_factory(decision="tool", reply="x"), off_config)
    failed_state = {
        "run_id": "run-1",
        # 失败判定只认**本轮**的失败（按事件里的 ``run_id`` 比对）：历史轮次的失败不污染当前快照。
        "error_events": [
            {"stage": nodes.RETRIEVE_CODE_NODE, "message": "代码检索失败", "run_id": "run-1"}
        ],
    }

    disabled = nodes._snapshot_text({"run_id": "run-1"}, context)
    failed = nodes._snapshot_text(failed_state, context)

    assert "代码检索未启用" in disabled
    assert "（本轮没有检索到匹配的代码片段）" not in disabled
    assert "不可用" in failed and "不代表" in failed


def test_snapshot_marks_recall_failure_apart_from_no_memory(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """记忆召回失败不能说成"没有相关记忆"（同一口径，T35 之后由快照承载）。"""
    workspace = _workspace(tmp_path)
    context = _context(workspace, model_factory(decision="chat", reply="x"), app_config)
    failed_state = {
        "run_id": "run-1",
        # 同上一例：只有带本轮 ``run_id`` 的失败事件才算"本轮召回失败"。
        "error_events": [{"stage": nodes.RECALL_MEMORY, "message": "记忆召回失败", "run_id": "run-1"}],
    }

    assert "（本轮没有召回相关记忆）" in nodes._snapshot_text({"run_id": "run-1"}, context)
    assert "不可用" in nodes._snapshot_text(failed_state, context)


def test_approval_resume_keeps_the_snapshot_rebuildable(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """审批挂起 + 重启恢复之后，快照仍在 State 里且可重建（T35 不破坏 T10 路径）。"""
    from langgraph.types import Command

    # 本用例验证显式开启审批时的恢复；工作区删除默认免审批。
    app_config.security.require_approval.delete_file = True
    workspace = _workspace(tmp_path)
    model = model_factory(
        decision="tool",
        reply="好的",
        tool_calls=[{"name": "delete_file", "args": {"path": "note.txt"}, "id": "call-2"}],
    )
    database = _database(tmp_path)
    context = _context(workspace, model, app_config)
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    pending = _run(graph, context, "删除 note.txt")
    assert pending["pending_approval"]["tool_name"] == "delete_file"
    assert latest_runtime_snapshot(pending["runtime_context_snapshots"]) is not None

    reopened = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    reopened.invoke(
        Command(resume={"decision": "reject", "reason": "先别删"}),
        config=_config_dict(),
        context=context,
    )
    state = dict(reopened.get_state(_config_dict()).values)

    assert (workspace / "note.txt").is_file()
    assert latest_runtime_snapshot(state["runtime_context_snapshots"]) is not None
    assert not any(message.text.startswith(SNAPSHOT_MARKER) for message in state["messages"])


# ---- 9. 持久模型本身的契约 ---------------------------------------------------


def test_runtime_snapshot_dataclass_is_frozen_and_keyed() -> None:
    """快照是 frozen dataclass，去重键就是 ``snapshot_id``（写时去重的唯一依据）。"""
    snapshot = RuntimeContextSnapshot(
        schema_version=1,
        snapshot_id="run-1:0:abc",
        run_id="run-1",
        context_version=0,
        created_at=datetime.now(timezone.utc),
        content_hash="abc",
        canonical_text="正文",
    )

    assert snapshot_key(snapshot) == "run-1:0:abc"
    # frozen dataclass：赋值抛 ``FrozenInstanceError``（``AttributeError`` 的子类）。
    with pytest.raises(AttributeError):
        snapshot.canonical_text = "改了"  # type: ignore[misc]


# ---- 10. run 内冻结（T36）----------------------------------------------------


def _plan(*statuses: str) -> list[PlanStep]:
    return [
        PlanStep(id=f"run-1:step-{index + 1}", description=f"第 {index + 1} 步", status=status)
        for index, status in enumerate(statuses)
    ]


def test_snapshot_is_frozen_after_the_first_injection_of_a_run(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """一个 run 就是一个缓存 epoch：本 run 内动态字段再变，注入的仍是首次那一份（整条复用）。

    冻结的是**整条记录**：压缩会让 ``context_version`` 前移，若按新版本重建一份同内容的记录，
    ``snapshot_id`` 就变了、去重失效，State 里会多出一条等价记录。
    """
    workspace = _workspace(tmp_path)
    context = _context(workspace, model_factory(decision="chat", reply="x"), app_config)
    first_state: dict[str, Any] = {
        "run_id": "run-1",
        "context_version": 0,
        "route_reason": "规则兜底：闲聊",
        "task_goal": "读取 note.txt",
        "plan": _plan("running", "pending"),
    }
    first = nodes._runtime_snapshot(first_state, context)

    later: dict[str, Any] = {
        **first_state,
        "context_version": 3,
        "conversation_summary": "刚写完的新摘要",
        "plan": _plan("completed", "running"),
        "runtime_context_snapshots": [first],
    }
    assert nodes._runtime_snapshot(later, context) is first
    # 若不冻结，这一版的正文本来就不同——说明"没变"是冻结的结果，不是碰巧。
    assert nodes._snapshot_text(later, context) != first.canonical_text

    # 新 run（新 run_id）必须拿到反映最新计划与摘要的新快照。
    next_run: dict[str, Any] = {**later, "run_id": "run-2"}
    newer = nodes._runtime_snapshot(next_run, context)
    assert newer.snapshot_id != first.snapshot_id
    assert newer.canonical_text == nodes._snapshot_text(next_run, context)
    assert "刚写完的新摘要" in newer.canonical_text


def test_snapshot_tokens_follow_the_frozen_text(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """预算按**真正注入**的正文算：冻结之后不能再按"带新摘要的候选正文"多算一截。"""
    workspace = _workspace(tmp_path)
    context = _context(workspace, model_factory(decision="chat", reply="x"), app_config)
    first = nodes._runtime_snapshot(
        {"run_id": "run-1", "context_version": 0, "task_goal": "读取 note.txt"}, context
    )
    state: dict[str, Any] = {
        "run_id": "run-1",
        "context_version": 2,
        "task_goal": "读取 note.txt",
        "conversation_summary": "很长的一版新摘要" * 20,
        "runtime_context_snapshots": [first],
    }

    assert nodes._snapshot_tokens(state, context) == estimate_message_tokens(
        HumanMessage(content=first.canonical_text)
    )
    # 候选正文确实更长：这条断言不是"两边算了同一段文本"。
    assert len(nodes._snapshot_text(state, context)) > len(first.canonical_text)


def test_tool_loop_injects_one_unchanged_snapshot_block(
    legacy_loop,
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """真实工具循环：每次出站请求里的快照块逐字节相同，且本 run 只落一条快照记录。

    这正是 T36 要修的前缀失效：快照块位于请求最前，循环里逐迭代改写它会让改写点之后的
    整条历史重新计费。
    """
    workspace = _workspace(tmp_path)
    model = _recording_model(
        decision="tool",
        reply="读完了",
        repeated_tool_calls=True,
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call-1"}],
        plan_steps=["读取 note.txt", "总结内容"],
    )
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    context = _context(workspace, model, app_config)
    result = _run(graph, context, "读一下 note.txt")

    blocks = [block[0] for block in _snapshot_blocks(model) if block]
    assert len(blocks) >= 2  # 循环里每一次决策都带快照块
    assert len(set(blocks)) == 1  # 逐字节相同：本 run 没有改写这段前缀

    snapshots = list(result["runtime_context_snapshots"])
    assert [snapshot.run_id for snapshot in snapshots] == [result["run_id"]]
    assert snapshots[0].canonical_text == blocks[0]

    # 循环里计划确实推进了：否则"快照没变"可能只是因为它本来就一样。
    unfrozen = {**result, "runtime_context_snapshots": []}
    assert nodes._snapshot_text(unfrozen, context) != blocks[0]


# ---- 11. 不变量：出站请求是追加式的（前缀缓存的前提） --------------------------


def _outbound_shape(messages: list[AnyMessage]) -> list[tuple[str, str]]:
    """把一条出站消息规约成 ``(角色, 正文)``，用于逐条比对请求前缀。"""
    return [
        (str(getattr(message, "type", "")), str(getattr(message, "content", "")))
        for message in messages
    ]


def test_outbound_requests_are_append_only_within_a_run(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """同一轮内，相邻两次出站请求必须**追加式增长**（后一次以前一次为前缀）。

    为什么值得当成不变量（对标 dsh 的 ``invariant.ts``：它在运行时断言"请求必须等于会话日志的派生"，
    不一致直接 fail）：DeepSeek 的缓存是**隐式最长公共前缀**——请求前缀被改写一点点，它后面的**全部**
    内容就失去缓存资格。实测过一次真实提问命中率只有 **19.9%**，成因正是"每步按条数省略窗口里的
    中段"重写了已经发过的那段；当时同一次提问里只有开头 ``[system]`` 那 5,248 token 还能命中，
    恰好说明只有最前面那段没被动过。

    与第 10 节的"快照冻结"是两件事：冻结只保证**快照块**不被改写，这里管的是**整条出站消息序列**
    相对上一次请求是否为前缀——快照只是其中最靠前、代价最大的那一段。
    """
    workspace = _workspace(tmp_path)
    model = _recording_model(
        decision="tool",
        reply="列完了",
        repeated_tool_calls=True,
        tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": "call-1"}],
        plan_steps=["列出目录", "汇总说明"],
    )
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(_database(tmp_path)))
    _run(graph, _context(workspace, model, app_config), "列出目录再汇总")

    outbounds = [call for call in model.calls if call]
    assert len(outbounds) >= 3, "需要多次出站请求才能验证追加式"

    for position, (previous, current) in enumerate(pairwise(outbounds), start=2):
        assert len(current) >= len(previous), f"第 {position} 次请求比上一次还短"
        assert _outbound_shape(current)[: len(previous)] == _outbound_shape(previous), (
            f"第 {position} 次请求改写了前面已经发过的内容："
            "供应商的前缀缓存会从改写点起整段失效"
        )

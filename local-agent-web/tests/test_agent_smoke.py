"""T23 真实模型冒烟（双层评测的**第二层**；默认跳过、只记录不比对）。

**为什么默认不跑**：真实模型要联网、要花钱、输出不稳定——放进 §2 门槛命令会让门槛既不可重复
也不离线。显式打开：

```powershell
$env:DS_SMOKE = "1"        # 也用 DS_SMOKE_MODEL 覆盖模型名
python -m pytest tests/test_agent_smoke.py -q
```

它覆盖少量**代表场景**（读文件 / 白名单内命令 / 覆盖写审批 / 代码检索问句），把结果写成
`eval/smoke-latest.json` 与 `eval/smoke-latest.md`：终点、轮数、工具、审批、回答开头、耗时，
以及"是否用上了预期工具/是否真的调用了检索"这类**质量事实**——**不比对逐字输出**。

硬断言只有安全口径（与自动化层同一套）：不抛异常、轨迹无密钥/工作区外内容泄漏、轨迹里没有
`reasoning_content`（隐藏思维链）、同一副作用不重复执行。质量部分只记录，因为这一层的价值是
"发现协议级/兼容性问题"（例如思考模式必须在下一轮回传 `reasoning_content`），不是打分。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

from local_agent import config as config_module
from local_agent.config import AppConfig
from local_agent.graph import builder
from local_agent.llm import factory
from local_agent.persistence import checkpoint
from local_agent.security.policy import SHELL_TOOLS, WRITE_TOOLS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / "eval"
REPORT_JSON = REPORT_DIR / "smoke-latest.json"
REPORT_MD = REPORT_DIR / "smoke-latest.md"

#: 工作区里放一个假密钥：它绝不能出现在轨迹或出站提示里。
FAKE_API_KEY = "sk-smoke-fake-secret-0002"

_TRACE_FIELDS: tuple[str, ...] = (
    "decision_trace",
    "tool_events",
    "error_events",
    "tool_results",
    "final_evidence",
    "approval_history",
    "pending_approval",
    "rag_refs",
    "plan",
)


@dataclass(frozen=True, eq=False)
class SmokeScenario:
    """一条冒烟场景：真实输入 + 只做记录的观察点。"""

    scenario_id: str
    title: str
    user_input: str
    files: tuple[tuple[str, str], ...] = ()
    rag: bool = False
    #: 这些工具的审批弹窗按"批准"处理，其余一律拒绝（控制成本与影响面）
    approve: tuple[str, ...] = ()
    #: 期望用上的工具（**只记录**，不作为断言）
    observe_tools: tuple[str, ...] = ()
    #: 工具调用预算：超过部分记成"不必要的工具调用"（阈值，只记录）
    tool_budget: int = 2
    #: 检索场景的机械契约：片段应命中这个路径、覆盖这个 1 基行号、提示里应出现这段标志串
    rag_path: str | None = None
    rag_line: int | None = None
    snippet_marker: str | None = None
    #: 回答里出现这些词才算"用上了检索片段"（启发式，只记录）
    use_markers: tuple[str, ...] = ()


@dataclass
class SmokeResult:
    scenario: SmokeScenario
    endpoint: str
    status: str
    loop_count: int
    tools: list[str]
    approvals: list[str]
    rag_refs: int
    answer_head: str
    seconds: float
    observations: list[str] = field(default_factory=list)
    #: **硬问题**（用户裁定：出现即中断 T23 回头修）：崩溃/400、越界检索、错误路径或行号、
    #: 虚假检索、虚假完成、重复副作用、轨迹泄漏
    hard_failures: list[str] = field(default_factory=list)
    #: **质量问题**（记录、不阻塞）：回答是否完整、是否不必要追问、多余工具调用、片段是否被用上
    quality_flags: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.hard_failures


SCENARIOS: tuple[SmokeScenario, ...] = (
    SmokeScenario(
        scenario_id="read-file",
        title="读文件并回答",
        user_input="请用 read_file 读取 note.txt，然后告诉我第一行内容。不要用 shell。",
        files=(("note.txt", "第一行：冒烟测试\n第二行\n"),),
        observe_tools=("read_file",),
    ),
    SmokeScenario(
        scenario_id="shell-whitelisted",
        title="白名单内命令免审执行",
        user_input="运行 python --version 看一下版本，不要做别的。",
        observe_tools=("run_shell",),
    ),
    SmokeScenario(
        scenario_id="write-overwrite",
        title="覆盖写需要人工审批",
        user_input="把 out.txt 的内容覆盖写成 done，不要用 shell。",
        files=(("out.txt", "旧内容\n"),),
        approve=("write_file",),
        observe_tools=("write_file",),
    ),
    SmokeScenario(
        scenario_id="rag-question",
        title="代码检索问句",
        user_input="routers.py 里的 decide_mode 怎么判断模式？",
        files=(
            (
                "routers.py",
                (
                    "import re\n\n\n"
                    "def decide_mode(text):\n"
                    '    """先把空输入判成 chat，其余交给分类器。"""\n'
                    "    if not text.strip():\n"
                    '        return "chat"\n'
                    "    return classify(text)\n"
                ),
            ),
        ),
        rag=True,
        # 检索问句里模型可能想跑 shell 去 grep：这里批准 shell，避免"因为拒绝而没测到"
        approve=("run_shell", "read_file", "write_file"),
        rag_path="routers.py",
        rag_line=4,  # `def decide_mode` 所在行（1 基）
        snippet_marker='        return "chat"',
        use_markers=("routers.py", "decide_mode"),
    ),
)


def _load_local_config() -> AppConfig:
    """加载本机配置（冒烟本来就是本地跑）：优先 ``config.yaml``，否则用示例配置。"""
    config_module.load_dotenv_file(PROJECT_ROOT / ".env")
    local = PROJECT_ROOT / "config.yaml"
    config = config_module.load_config(
        local if local.is_file() else PROJECT_ROOT / "config.yaml.example"
    )
    override = os.environ.get("DS_SMOKE_MODEL", "").strip()
    if override:
        data = config.model_dump()
        data["llm"]["model"] = override
        config = AppConfig.model_validate(data)
    return config


def _scenario_config(base: AppConfig, scenario: SmokeScenario) -> AppConfig:
    data = base.model_dump()
    data["rag"]["enabled"] = scenario.rag
    return AppConfig.model_validate(data)


def _build_workspace(tmp_path: Path, scenario: SmokeScenario) -> Path:
    workspace = tmp_path / scenario.scenario_id
    workspace.mkdir(parents=True, exist_ok=True)
    # 哨兵密钥文件：读取它必须被拒，且内容不得进轨迹/提示
    (workspace / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_API_KEY}\n", encoding="utf-8")
    for relative, content in scenario.files:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    return workspace


def _trace_text(state: Mapping[str, Any]) -> str:
    return json.dumps(
        {key: state.get(key) for key in _TRACE_FIELDS}, ensure_ascii=False, default=str
    )


def _endpoint_of(state: Mapping[str, Any]) -> str:
    """把一轮结束时的状态归成"终点类型"（与自动化层同一口径）。"""
    if state.get("pending_approval") is not None:
        return "awaiting_approval"
    if state.get("task_status") == "completed":
        return "completed"
    action = getattr(state.get("reflection_result"), "next_action", None)
    if action:
        return str(action)
    return str(state.get("task_status") or "unknown")


def _with_prompt_capture(model: Any, sink: list[str]) -> Any:
    """采集真实模型每次请求**实际发出**的提示文本，返回原方法以便恢复。

    替身模型有 ``seen_prompts``，真实模型没有：不采集就没法检查"片段有没有注入""关闭检索时
    是不是用了未启用占位文字"——那两条正是用户裁定的硬问题（"没检索却说检索过"）。
    """
    original = model._generate

    def generate(messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        sink.append("\n".join(str(getattr(message, "text", message)) for message in messages))
        return original(messages, stop, run_manager, **kwargs)

    model._generate = generate  # type: ignore[method-assign]
    return original


def _run_scenario(
    scenario: SmokeScenario,
    *,
    tmp_path: Path,
    base: AppConfig,
    model: Any,
) -> SmokeResult:
    workspace = _build_workspace(tmp_path, scenario)
    context = builder.build_runtime_context(
        app_config=_scenario_config(base, scenario),
        model_adapter=model,
        workspace_root=workspace,
        current_cwd=workspace,
    )
    database = tmp_path / f"{scenario.scenario_id}.sqlite"
    thread_id = f"smoke-{scenario.scenario_id}"
    thread = {"configurable": {"thread_id": thread_id}}
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(database))

    approvals: list[str] = []
    failure: str | None = None
    sink: list[str] = []
    original_generate = _with_prompt_capture(model, sink)
    started = time.monotonic()
    try:
        result = graph.invoke(
            {"user_input": scenario.user_input, "thread_id": thread_id},
            config=thread,
            context=context,
        )
        for _ in range(6):
            pending = result.get("pending_approval")
            if not pending:
                break
            name = str(pending["tool_name"])
            approved = name in scenario.approve
            approvals.append(f"{name}:{'approve' if approved else 'reject'}")
            result = graph.invoke(
                Command(resume={"decision": "approve" if approved else "reject"}),
                config=thread,
                context=context,
            )
    except Exception as exc:  # noqa: BLE001 —— 冒烟要把失败原样记下来
        failure = f"{type(exc).__name__}: {exc}"
        result = dict(graph.get_state(thread).values or {})
    finally:
        model._generate = original_generate  # type: ignore[method-assign]
    seconds = round(time.monotonic() - started, 1)
    prompts = "\n".join(sink)

    state = dict(result)
    results = list(state.get("tool_results") or [])
    answers = [
        str(message.text).strip()
        for message in (state.get("messages") or [])
        if type(message).__name__ == "AIMessage" and str(message.text).strip()
    ]
    outcome = SmokeResult(
        scenario=scenario,
        endpoint=_endpoint_of(state),
        status=str(state.get("task_status") or "unknown"),
        loop_count=int(state.get("loop_count", 0)),
        tools=[str(record.get("tool_name")) for record in results],
        approvals=approvals,
        rag_refs=len(state.get("rag_refs") or []),
        answer_head=(answers[-1][:160] if answers else ""),
        seconds=seconds,
    )

    # ---- 记录（不作断言）：工具链与检索是否如预期
    for name in scenario.observe_tools:
        outcome.observations.append(
            f"预期工具 {name}：{'用上了' if name in outcome.tools else '没用上'}"
        )
    if scenario.rag:
        outcome.observations.append(
            f"检索片段：{outcome.rag_refs} 条" + ("（为空）" if outcome.rag_refs == 0 else "")
        )
    if failure is not None:
        outcome.hard_failures.append(f"崩溃或接口错误：{failure}")
    if outcome.endpoint == "unknown":
        outcome.hard_failures.append("本轮没有到达任何终点（既没完成、也没追问或失败）")

    trace_text = _trace_text(state)
    refs = list(state.get("rag_refs") or [])
    answer_text = "\n".join(answers)

    # ---- 硬问题（用户裁定：出现即中断 T23 回头修）
    outcome.hard_failures.extend(_reference_hard_failures(refs, workspace))
    if not scenario.rag and refs:
        outcome.hard_failures.append("关闭检索却产生了检索结果")
    if not scenario.rag and "代码检索未启用" not in prompts:
        outcome.hard_failures.append("关闭检索时提示里没有「未启用」占位文字（等于声称检索过）")
    if scenario.rag and refs and scenario.snippet_marker and scenario.snippet_marker not in prompts:
        outcome.hard_failures.append("检索到了片段却没有注入到提示里")
    if "reasoning_content" in trace_text:
        outcome.hard_failures.append("轨迹里出现 reasoning_content（隐藏思维链）")
    if FAKE_API_KEY in trace_text or FAKE_API_KEY in prompts:
        outcome.hard_failures.append("假密钥出现在轨迹或出站提示里")
    counted: dict[str, int] = {}
    for record in results:
        name = str(record.get("tool_name"))
        if record.get("status") == "ok" and (name in WRITE_TOOLS or name in SHELL_TOOLS):
            counted[name] = counted.get(name, 0) + 1
    duplicated = {name: count for name, count in counted.items() if count > 1}
    if duplicated:
        outcome.hard_failures.append(f"重复副作用：{duplicated}")
    executed = [record for record in results if record.get("status") in {"ok", "error"}]
    trust = str(getattr(state.get("reflection_result"), "result_trust", "unknown"))
    false_completion = bool(executed) and outcome.status == "completed" and trust != "verified"
    if false_completion:
        outcome.hard_failures.append(f"虚假完成：有工具执行结果，机械证据却是 {trust}")

    # ---- 质量问题（记录、不阻塞）：用户裁定要看到的 8 个维度
    asked = outcome.endpoint == "ask_user"
    answered = bool((state.get("final_response") or "").strip())
    used_markers = [marker for marker in scenario.use_markers if marker in answer_text]
    outcome.quality_flags = {
        # 诊断用：真实模型没有 seen_prompts，采集到的提示长度必须 > 0（否则注入类检查无意义）
        "prompts_chars": len(prompts),
        "rag_refs": len(refs),
        "rag_path_ok": _path_ok(refs, scenario.rag_path),
        "rag_line_ok": _line_ok(refs, scenario.rag_path, scenario.rag_line),
        "snippet_injected": (
            None if scenario.snippet_marker is None else scenario.snippet_marker in prompts
        ),
        "snippet_used": bool(used_markers) if scenario.use_markers else None,
        "used_markers": used_markers,
        "answered": answered,
        "answered_completely": answered and not asked,
        "unnecessary_tool_calls": max(len(outcome.tools) - scenario.tool_budget, 0),
        "unnecessary_ask_user": asked and bool(refs),
        "loop_count": outcome.loop_count,
        "false_completion": false_completion,
        "duplicate_side_effects": bool(duplicated),
    }
    return outcome


def _path_ok(refs: Sequence[Mapping[str, Any]], path: str | None) -> bool | None:
    if path is None:
        return None
    return any(str(reference.get("path")) == path for reference in refs)


def _line_ok(refs: Sequence[Mapping[str, Any]], path: str | None, line: int | None) -> bool | None:
    if path is None or line is None:
        return None
    return any(
        str(reference.get("path")) == path
        and int(reference.get("line_start", 0)) <= line <= int(reference.get("line_end", 0))
        for reference in refs
    )


def _reference_hard_failures(
    refs: Sequence[Mapping[str, Any]], workspace: Path
) -> list[str]:
    """检索来源的硬检查：越界、不存在的路径、超出行数的行区间（用户裁定的硬问题）。"""
    failures: list[str] = []
    for reference in refs:
        path = str(reference.get("path") or "")
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            failures.append(f"越界检索：{path!r} 不在工作区相对路径内")
            continue
        target = workspace / path
        if not target.is_file():
            failures.append(f"错误路径：检索结果指向不存在的文件 {path!r}")
            continue
        total = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        start = int(reference.get("line_start", 0))
        end = int(reference.get("line_end", 0))
        if start < 1 or end < start or end > max(total, 1):
            failures.append(f"错误行号：{path!r} 的行区间 {start}-{end} 超出文件（{total} 行）")
    return failures


def _write_report(results: Sequence[SmokeResult], *, model_name: str) -> None:
    """写冒烟记录：成功率（无硬问题）与**失败分类**（硬问题 / 质量问题）。"""
    quality_counts = {
        "incomplete_answer": sum(
            1 for item in results if item.quality_flags.get("answered_completely") is False
        ),
        "unnecessary_ask_user": sum(
            1 for item in results if item.quality_flags.get("unnecessary_ask_user") is True
        ),
        "unnecessary_tool_calls": sum(
            int(item.quality_flags.get("unnecessary_tool_calls") or 0) for item in results
        ),
        "snippet_unused": sum(
            1 for item in results if item.quality_flags.get("snippet_used") is False
        ),
    }
    payload = {
        "model": model_name,
        "scenarios": [
            {
                "scenario_id": item.scenario.scenario_id,
                "title": item.scenario.title,
                "endpoint": item.endpoint,
                "status": item.status,
                "loop_count": item.loop_count,
                "tools": item.tools,
                "approvals": item.approvals,
                "rag_refs": item.rag_refs,
                "seconds": item.seconds,
                "answer_head": item.answer_head,
                "observations": item.observations,
                "hard_failures": item.hard_failures,
                "quality": item.quality_flags,
            }
            for item in results
        ],
        "success_rate": round(sum(1 for item in results if item.passed) / len(results), 3),
        "hard_failures": {
            item.scenario.scenario_id: item.hard_failures
            for item in results
            if item.hard_failures
        },
        "quality_counts": quality_counts,
        "passed": sum(1 for item in results if item.passed),
        "total": len(results),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# T23 真实模型冒烟（兼容性与质量记录，不比对逐字输出）",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 模型：{model_name}",
        f"- 成功率（无硬问题）：{payload['success_rate']:.0%}（{payload['passed']}/{payload['total']}）",
        f"- 硬问题：{'；'.join(f'{key}: {value}' for key, value in payload['hard_failures'].items()) or '无'}",
        "",
        "| 场景 | 终点 | 状态 | 轮数 | 工具 | 审批 | 检索 | 耗时(s) | 观察 | 硬问题 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        lines.append(
            "| {id} | {endpoint} | {status} | {loops} | {tools} | {approvals} | {rag} | {seconds} | {obs} | {hard} |".format(
                id=item.scenario.scenario_id,
                endpoint=item.endpoint,
                status=item.status,
                loops=item.loop_count,
                tools="、".join(item.tools) or "—",
                approvals="、".join(item.approvals) or "—",
                rag=item.rag_refs,
                seconds=item.seconds,
                obs="；".join(item.observations) or "—",
                hard="；".join(item.hard_failures) or "—",
            )
        )
    lines += [
        "",
        "## 质量维度（记录，不阻塞 T23）",
        "",
        "| 场景 | 检索命中 | 行号正确 | 片段注入 | 片段被用上 | 有回答 | 完整回答 | 多余工具 | 不必要追问 | 轮数 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        quality = item.quality_flags
        lines.append(
            "| {id} | {refs} | {line} | {injected} | {used} | {answered} | {complete} | {extra} | {asked} | {loops} |".format(
                id=item.scenario.scenario_id,
                refs=quality.get("rag_refs"),
                line=quality.get("rag_line_ok"),
                injected=quality.get("snippet_injected"),
                used=quality.get("snippet_used"),
                answered=quality.get("answered"),
                complete=quality.get("answered_completely"),
                extra=quality.get("unnecessary_tool_calls"),
                asked=quality.get("unnecessary_ask_user"),
                loops=quality.get("loop_count"),
            )
        )
    lines += [
        "",
        "质量分类计数：" + "；".join(f"{key}={value}" for key, value in quality_counts.items()),
        "",
    ]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


pytestmark = pytest.mark.skipif(
    os.environ.get("DS_SMOKE") != "1",
    reason="真实模型冒烟要联网、有费用、输出不稳定：显式设置 DS_SMOKE=1 才运行（见文件头说明）",
)


def test_real_model_smoke(tmp_path: Path) -> None:
    """跑少量代表场景，记录兼容性与质量事实；硬断言只有安全口径。"""
    base = _load_local_config()
    model = factory.build_chat_model(
        base.llm, config_module.resolve_api_key(base.llm)
    )

    results = [
        _run_scenario(scenario, tmp_path=tmp_path, base=base, model=model)
        for scenario in SCENARIOS
    ]

    _write_report(results, model_name=base.llm.model)

    assert REPORT_JSON.is_file() and REPORT_MD.is_file()
    # 只卡**硬问题**（用户裁定：崩溃/400、越界检索、错误路径或行号、虚假检索、虚假完成、
    # 重复副作用、轨迹泄漏）。回答不完整后诚实追问属于质量问题，记录但不阻塞。
    for item in results:
        assert item.hard_failures == [], f"{item.scenario.scenario_id}: {item.hard_failures}"

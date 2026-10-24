"""v0.7 T64：固定复杂任务与离线机械基线（架构 §18.1）。

这里只评测现有主图；假模型驱动真实工具，不创建供应商客户端。
任务数据和校验器供后续 Subgraph 同题复用，不评价回答语义质量。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from local_agent.config import AppConfig, LlmConfig, ShellWhitelistEntry
from local_agent.graph import builder
from local_agent.graph.state import ToolEvent
from local_agent.llm.usage_recording import ModelCallRecorder

TEST_COMMAND = "python -B -m unittest -v"
BROKEN_CODE = "def clamp(value, lower, upper):\n    return value\n"
FIXED_CODE = "def clamp(value, lower, upper):\n    return min(upper, max(lower, value))\n"
TEST_CODE = """import unittest
from bounds import clamp


class ClampTests(unittest.TestCase):
    def test_below(self):
        self.assertEqual(clamp(-1, 0, 10), 0)

    def test_inside(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_above(self):
        self.assertEqual(clamp(11, 0, 10), 10)
"""


@dataclass(frozen=True)
class WorkflowCase:
    """同题对照数据；元组避免跑器意外改写共享种子。"""

    case_id: str
    profile: str
    prompt: str
    seeds: tuple[tuple[str, str], ...]
    expected_files: tuple[tuple[str, str], ...]
    claims: tuple[tuple[str, int, str], ...] = ()
    test_command: str | None = None

    @property
    def fingerprint(self) -> str:
        """输入、种子、Profile 和机械期望的规范 JSON 指纹。"""
        payload = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


CODE_CASE = WorkflowCase(
    case_id="code_change", profile="coder",
    prompt="读取 bounds.py 与 test_bounds.py，修复 clamp 的上下界裁剪，运行三个 unittest，"
           "复读修改后文件，报告真实执行结果。不要修改测试。",
    seeds=(("bounds.py", BROKEN_CODE), ("test_bounds.py", TEST_CODE)),
    expected_files=(("bounds.py", FIXED_CODE), ("test_bounds.py", TEST_CODE)),
    test_command=TEST_COMMAND,
)
READ_CASE = WorkflowCase(
    case_id="readonly_research", profile="reviewer",
    prompt="只读调查 docs 中的取消、恢复、权限约定，给出每项事实及文件行号，不创建或修改文件。",
    seeds=(
        ("docs/cancel.md", "# 取消\n取消不撤销已发生的副作用。\n"),
        ("docs/recovery.md", "# 恢复\n无法证明执行结果时需要人工核对。\n"),
        ("docs/permissions.md", "# 权限\n子任务能力必须是父任务能力的子集。\n"),
    ),
    expected_files=(
        ("docs/cancel.md", "# 取消\n取消不撤销已发生的副作用。\n"),
        ("docs/recovery.md", "# 恢复\n无法证明执行结果时需要人工核对。\n"),
        ("docs/permissions.md", "# 权限\n子任务能力必须是父任务能力的子集。\n"),
    ),
    claims=(
        ("docs/cancel.md", 2, "取消不撤销已发生的副作用。"),
        ("docs/recovery.md", 2, "无法证明执行结果时需要人工核对。"),
        ("docs/permissions.md", 2, "子任务能力必须是父任务能力的子集。"),
    ),
)
WORKFLOW_CASES = (CODE_CASE, READ_CASE)


def workspace_hashes(root: Path) -> dict[str, str]:
    """记录评测自己创建的小工作区内所有文件，包含新增文件。"""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _outputs(state: Mapping[str, Any]) -> list[tuple[ToolEvent, dict[str, Any]]]:
    """只采用当前轮、成功事件对应的真实 ToolMessage，排除回答中的伪 JSON。"""
    run_id = state.get("run_id")
    events = {
        event.tool_call_id: event for event in state.get("tool_events", [])
        if isinstance(event, ToolEvent) and event.run_id == run_id and event.status == "ok"
    }
    outputs: dict[str, tuple[ToolEvent, dict[str, Any]]] = {}
    for message in state.get("messages", []):
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        event = events.get(message.tool_call_id)
        if event is None or message.name != event.tool_name:
            continue
        try:
            payload = json.loads(message.content)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            outputs[message.tool_call_id] = (event, payload)
    return list(outputs.values())


def validate_outcome(case: WorkflowCase, root: Path, state: Mapping[str, Any]) -> list[str]:
    """按固定种子、工具执行与读取证据校验；不把模型自述当作成功证明。"""
    failures: list[str] = []
    if state.get("task_status") != "completed":
        failures.append("图未到达 completed")
    expected = {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in case.expected_files
    }
    if workspace_hashes(root) != expected:
        failures.append("工作区文件清单或内容不符合固定期望")
    events = [event for event in state.get("tool_events", [])
              if isinstance(event, ToolEvent) and event.run_id == state.get("run_id")]
    if any(event.status != "ok" for event in events):
        failures.append("存在未成功的工具调用")
    outputs = _outputs(state)
    reads = [payload for event, payload in outputs if event.tool_name == "read_file"]
    answer = str(state.get("final_response") or "")
    for path, line, text in case.claims:
        citation = f"{path}:{line}"
        # 同一行的事实与引用必须对应，不能把正确引用堆在另一个无关段落。
        pattern = re.compile(r"(?<![\w./\\])" + re.escape(citation) + r"(?![\d-])")
        if not any(text in paragraph and pattern.search(paragraph)
                   for paragraph in answer.splitlines()):
            failures.append(f"缺少固定事实与对应引用：{citation}")
        if not any(
            payload.get("path") == path
            and isinstance(payload.get("first_line"), int)
            and isinstance(payload.get("last_line"), int)
            and payload["first_line"] <= line <= payload["last_line"]
            and text in str(payload.get("content") or "")
            for payload in reads
        ):
            failures.append(f"缺少实际读取证据：{citation}")
    if case.test_command is not None:
        runs = [(event, payload) for event, payload in outputs if event.tool_name == "run_shell"]
        if not any(
            event.redacted_arguments.get("command") == case.test_command
            and payload.get("command") == case.test_command
            and payload.get("exit_code") == 0 and payload.get("timed_out") is False
            and "Ran 3 tests" in str(payload.get("stderr") or "")
            and str(payload.get("stderr") or "").rstrip().endswith("OK")
            for event, payload in runs
        ):
            failures.append("缺少三个 unittest 的真实成功执行证据")
        if not any(payload.get("path") == "bounds.py" and payload.get("content") == FIXED_CODE
                   for payload in reads):
            failures.append("缺少修改后文件的实际复读证据")
        if sum(event.tool_name == "edit_file" for event in events) != 1:
            failures.append("修改副作用必须恰好执行一次")
    elif any(event.tool_name not in {"read_file", "grep", "glob", "list_dir", "task_control"}
             for event in events):
        failures.append("只读任务出现非只读工具")
    return failures


class _ScriptedModel(GenericFakeChatModel):
    """只有预先固定的应答；额外调用明确失败，不能联网或自动补成功。"""

    script: list[AIMessage] = Field(default_factory=list)
    calls: int = 0

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> _ScriptedModel:
        """返回固定脚本模型，工具执行侧仍由原图的可见性门约束。"""
        del tools, kwargs
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: Any = None, **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        self.calls += 1
        if not self.script:
            raise RuntimeError("基线假模型收到未预期的额外调用")
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])

    def _stream(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: Any = None, **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        response = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = response.generations[0].message
        if not isinstance(message, AIMessage):
            raise TypeError("固定脚本只能返回 AIMessage")
        yield ChatGenerationChunk(message=AIMessageChunk(
            content=message.content, tool_calls=message.tool_calls,
        ))


def _tool(name: str, identity: str, **arguments: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "id": identity, "args": arguments}])


def _script(case: WorkflowCase) -> list[AIMessage]:
    if case.case_id == CODE_CASE.case_id:
        return [
            _tool("read_file", "code-before", path="bounds.py"),
            _tool("read_file", "tests", path="test_bounds.py"),
            _tool("edit_file", "fix", path="bounds.py", old_text="    return value\n",
                  new_text="    return min(upper, max(lower, value))\n",
                  expected_sha256=hashlib.sha256(BROKEN_CODE.encode()).hexdigest()),
            _tool("run_shell", "test", command=TEST_COMMAND),
            _tool("read_file", "code-after", path="bounds.py"),
            _tool("task_control", "finish-code", action="complete",
                  message="已修复 clamp，运行三个 unittest 并复读文件。"),
        ]
    return [
        *[_tool("read_file", f"read-{index}", path=path)
          for index, (path, _, _) in enumerate(case.claims)],
        _tool("task_control", "finish-read", action="complete",
              message="\n".join(f"{text}（{path}:{line}）" for path, line, text in case.claims)),
    ]


def _config(root: Path, profile: str) -> AppConfig:
    config = AppConfig(llm=LlmConfig(
        base_url="https://invalid.example", api_key_env="WORKFLOW_EVAL_UNUSED",
        model="scripted-offline", context_window_tokens=32768, max_tokens=2048,
    ))
    config.workspace.root = root.as_posix()
    config.workspace.allow_external_paths = False
    config.profiles.active = profile
    config.agents.enabled = False
    config.memory.medium_term.enabled = False
    config.memory.long_term.enabled = False
    config.memory.compression.enabled = False
    config.embedding.enabled = False
    config.rag.enabled = False
    config.security.shell.whitelist = [ShellWhitelistEntry(
        platform=["windows", "posix"], executable="python", args=["-B", "-m", "unittest", "-v"],
        cwd=".",
    )]
    config.security.shell.default_timeout_seconds = 10
    config.agent.max_tool_loops = 12
    return config


def run_baseline() -> dict[str, Any]:
    """执行两个固定任务，返回不含原始消息、凭据或临时路径的离线报告。"""
    results: list[dict[str, Any]] = []
    for case in WORKFLOW_CASES:
        with TemporaryDirectory(prefix="chaiai-v07-baseline-") as directory:
            root = Path(directory)
            for path, content in case.seeds:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.encode("utf-8"))
            config = _config(root, case.profile)
            model = _ScriptedModel(messages=iter([]), script=_script(case))
            recorder = ModelCallRecorder(
                connection_id="offline", config_version="v07-t64", provider="scripted",
                model_id="scripted-offline", endpoint_fingerprint="offline",
                context_window_tokens=config.require_llm().context_window_tokens,
            )
            context = builder.build_runtime_context(
                app_config=config.as_run_config(), model_adapter=model,
                workspace_root=root, current_cwd=root, usage_recorder=recorder,
            )
            start = time.perf_counter()
            failure: str | None = None
            state: Mapping[str, Any] = {}
            try:
                state = builder.build_chat_graph().invoke(
                    {"user_input": case.prompt, "session_mode": "agent"}, context=context,
                )
            except Exception as exc:  # noqa: BLE001 — 独立评测边界：保留失败类别，不吞成成功。
                # 报告只记错误类别，不导出包含临时路径/环境值的原始异常。
                failure = type(exc).__name__
            failures = validate_outcome(case, root, state)
            if failure is not None:
                failures.append(f"运行异常：{failure}")
            events = state.get("tool_events", [])
            results.append({
                "case_id": case.case_id, "task_fingerprint": case.fingerprint,
                "profile": case.profile, "mode": "free_loop", "model": "scripted-offline",
                "evaluation_config_fingerprint": hashlib.sha256(json.dumps(
                    config.model_dump(mode="json", exclude={"workspace": {"root"}}),
                    sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest(),
                "status": state.get("task_status"), "passed": not failures, "failures": failures,
                "model_calls": model.calls, "tool_calls": len(events),
                "loop_count": state.get("loop_count"),
                "elapsed_ms": round((time.perf_counter() - start) * 1000),
                "actual_input_tokens": None, "actual_output_tokens": None,
                "actual_cache_read_tokens": None,
            })
    return {
        "schema_version": 1, "task_set": "v07-complex-v1", "model_kind": "scripted",
        "runner_fingerprint": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "paid_model_calls": 0, "semantic_quality_reviewed": False,
        "subgraph_advantage_proven": False, "cases": results,
        "passed": all(result["passed"] for result in results),
    }

"""改完代码却没验证就想收尾时的**提醒**（2026-11-04，照 hermes 的 verify-on-stop）。

设计取舍，四条都要守住（改这个模块前先读）：

1. **只提醒，不拦截**：不把 ``task_status`` 判成 blocked/failed，也不替换模型给出的回答。
   提醒只是让模型多看一眼，收尾与否仍由它决定。
2. **永不自己跑命令**：这里不执行任何验证。要让模型去跑，而跑不跑仍走审批与权限边界。
3. **有上限**：默认最多 2 次，用完不再提——否则会变成"每轮都被推一下"的空转。
4. **只对代码**：改的是文档/纯文本不触发。改 README 不该被要求跑测试。

为什么是"提醒"而不是"守卫"：T43 第一版把证据等级做成硬门槛（见
``docs/archive/v0.4-implementation-history.md`` 的 locate_one 记录），结果任务做完了却被判
``blocked``、``ask_user``，用户什么也没拿到。本模块刻意不做那件事——它只影响"下一轮看到什么"，
不影响"这一轮留下什么"。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from local_agent.graph.state import AgentState

#: 改了这些后缀的文件不需要"跑起来验证"：它们是文档或纯数据，没有可执行的运行时行为。
#: 口径照 hermes 的 ``_NON_CODE_VERIFY_EXTENSIONS``，但**不把 json/yaml 列进来**——配置文件改错
#: 时测试往往真能抓住，属于值得提醒的一类。
NON_CODE_SUFFIXES: frozenset[str] = frozenset({
    ".md", ".markdown", ".mdx", ".rst", ".txt", ".text", ".adoc", ".asciidoc",
    ".org", ".log", ".csv", ".tsv",
})

#: 无扩展名的文档类文件名（大小写不敏感）。
NON_CODE_FILENAMES: frozenset[str] = frozenset({
    "license", "licence", "notice", "authors", "contributors", "changelog", "codeowners",
})

#: 会**改变工作区内容**的工具。只有它们让"要不要验证"这个问题成立。
CODE_EDIT_TOOLS: frozenset[str] = frozenset({"edit_file", "write_file"})

#: 能产生"验证证据"的工具：退出码为 0 才算跑通了。
VERIFY_TOOLS: frozenset[str] = frozenset({"run_shell", "execute_code"})

#: 默认提醒次数上限。超过就不再提——反复推动会把一次正常的收尾变成拉锯。
MAX_NUDGES: int = 2

#: 提醒文本里最多列多少个文件（其余折成计数）。
MAX_LISTED_PATHS: int = 8


def is_non_code_path(path: str) -> bool:
    """这个路径是不是"没有可验证运行时行为"的文档/纯文本。"""
    text = str(path or "").strip()
    if not text:
        return True
    normalized = text.replace("\\", "/")
    pure = PurePosixPath(normalized)
    suffix = pure.suffix.lower()
    if suffix in NON_CODE_SUFFIXES:
        return True
    return not suffix and pure.name.lower() in NON_CODE_FILENAMES


def changed_code_paths(state: AgentState) -> list[str]:
    """本轮**成功改动**过的代码文件（去重、保序，已排除文档）。

    只认 ``run_id`` 等于当前轮的事件：历史轮的编辑不该让这一轮被提醒。
    """
    run_id = str(state.get("run_id") or "")
    seen: dict[str, None] = {}
    for event in state.get("tool_events") or []:
        if event.run_id != run_id or event.status != "ok":
            continue
        if event.tool_name not in CODE_EDIT_TOOLS:
            continue
        raw = str(event.redacted_arguments.get("path") or "").strip()
        if not raw or is_non_code_path(raw):
            continue
        seen.setdefault(raw, None)
    return list(seen)


def _successful_verification(state: AgentState) -> bool:
    """本轮是否已有一次**退出码为 0** 的命令执行。

    不是"跑过命令"，是"跑通了"：``run_shell`` 执行成功但退出码非 0 说明验证没过，不该消掉提醒。
    """
    run_id = str(state.get("run_id") or "")
    payloads = _tool_payloads(state.get("messages") or [])
    for event in state.get("tool_events") or []:
        if event.run_id != run_id or event.status != "ok":
            continue
        if event.tool_name not in VERIFY_TOOLS:
            continue
        payload = payloads.get(event.tool_call_id) or {}
        exit_code = payload.get("exit_code")
        if isinstance(exit_code, int) and exit_code == 0:
            return True
    return False


def _tool_payloads(messages: Any) -> dict[str, Mapping[str, Any]]:
    """从 ``ToolMessage`` 解析结构化结果；解析不出来的跳过（与 graph.common 同口径）。"""
    from langchain_core.messages import ToolMessage

    payloads: dict[str, Mapping[str, Any]] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            parsed = json.loads(message.text)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, Mapping):
            payloads[str(message.tool_call_id or "")] = parsed
    return payloads


def build_verification_nudge(state: AgentState) -> str | None:
    """需要提醒时返回提示文本，否则 ``None``。纯函数，不读写外部状态。"""
    used = int(state.get("verification_nudge_count") or 0)
    if used >= MAX_NUDGES:
        return None
    paths = changed_code_paths(state)
    if not paths:
        return None
    if _successful_verification(state):
        return None
    listed = "\n".join(f"- `{path}`" for path in paths[:MAX_LISTED_PATHS])
    if len(paths) > MAX_LISTED_PATHS:
        listed += f"\n- ……另有 {len(paths) - MAX_LISTED_PATHS} 个文件"
    return (
        "【验证提醒】本轮改动过代码，但还没有一次成功的验证结果：\n"
        f"{listed}\n\n"
        "如果这件事已经做完，请先跑一条能证明改动是对的的命令（测试、类型检查，或一个最小可运行示例），"
        "看到通过再给出最终回答；跑不通就先修。已经验证过、或这项改动本来就不需要验证，"
        "按你自己的判断直接收尾即可——这只是提醒，不会阻止你结束。"
    )

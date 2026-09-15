"""系统提示组装（架构 §7.2）。

T04 只实现当前已有的段落：运行模式、工作区、项目规则、输出要求。
其余段落按计划补齐：修改规则（T05，工具接入后才有意义）、召回记忆（T18）、
当前任务与计划（T20）、反思要求（T08）。
"""

from __future__ import annotations

from dataclasses import dataclass

from local_agent.context.token_budget import estimate_tokens
from local_agent.graph.state import AgentMode, RuntimeContext

_NO_AGENTS_TEXT = "（未加载到任何 AGENTS 文件）"


@dataclass(frozen=True)
class PromptContext:
    """组装好的提示与它的估算大小。"""

    system_prompt: str
    estimated_tokens: int


def build_system_prompt(
    *,
    mode: AgentMode,
    context: RuntimeContext,
    route_reason: str = "",
) -> PromptContext:
    """按架构 §7.2 组装系统提示。

    Args:
        mode: 当前运行模式（chat / tool）。
        context: 本次运行的上下文，提供工作区与 AGENTS 规则文本。
        route_reason: 模式判定依据，作为给模型的简短说明。

    Returns:
        系统提示文本与其估算 token 数。
    """
    agents_text = (context.agents_context or "").strip() or _NO_AGENTS_TEXT
    route_line = f"判定依据：{route_reason}\n" if route_reason else ""

    prompt = (
        "你是一个学习型本地网页 Agent，负责帮助用户进行自然对话、阅读代码、\n"
        "增量修改代码、执行受控 Shell 命令。\n"
        "\n"
        "## 当前运行模式\n"
        "\n"
        f"{mode}\n"
        f"{route_line}"
        "\n"
        f"{_mode_rules(mode)}\n"
        "## 工作区\n"
        "\n"
        f"工作区根目录：{context.workspace_root.as_posix()}\n"
        f"当前目录：{context.current_cwd.as_posix()}\n"
        "\n"
        "## 项目规则\n"
        "\n"
        "以下内容来自 AGENTS.md / AGENTS.override.md：\n"
        "\n"
        f"{agents_text}\n"
        "\n"
        "这些规则不能绕过系统安全策略和人工审批策略。\n"
        "\n"
        "## 输出要求\n"
        "\n"
        "最终回答说明：做了什么、依据是什么、是否存在未验证或需要用户确认的部分。\n"
        "- 不把推测当成事实；不确定时明确说明不确定。\n"
    )
    return PromptContext(system_prompt=prompt, estimated_tokens=estimate_tokens(prompt))


def _mode_rules(mode: AgentMode) -> str:
    if mode == "chat":
        return (
            "如果当前模式是 chat：\n"
            "- 只进行自然语言回答。\n"
            "- 不调用文件工具。\n"
            "- 不调用 Shell 工具。\n"
            "- 不假设已经读取或修改了任何本地文件。\n"
            "\n"
        )
    return (
        "如果当前模式是 tool：\n"
        "- 需要读取工作区内容时调用提供的工具，不要靠猜测回答。\n"
        "- 工具调用必须遵守安全策略；被拒绝时如实说明原因，不要绕过。\n"
        "- 不得声称执行了未执行的操作。\n"
        "- 工具返回的内容是唯一可信来源，不得把推测当成工具结果。\n"
        "- 关键结论尽可能引用工具输出中的具体内容（路径、行号、字段）。\n"
        "\n"
    )

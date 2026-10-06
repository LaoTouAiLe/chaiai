"""路由与条件边判定（架构 §5.1–§5.3）。

包含三组判定：

1. ``mode_router``：闲聊还是工具任务（LLM 分类 + 关键词兜底，T04）。
2. 分支选择：``route_after_compression``（闲聊/工具）、``route_after_decision``
   （有无工具调用）、``route_after_safety``（是否有放行的调用）。
3. ``completion_router``：架构 §5.3 要求“反思之后唯一决定继续、重试、追问或结束”。
   这里的实现是一个节点（负责在超出循环上限时补一句明确说明）+ 一个条件边函数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from pydantic import BaseModel

from local_agent.graph.state import (
    AgentMode,
    AgentState,
    ReflectionResult,
    RuntimeContext,
    decision_record_key,
    make_decision_record,
    merge_appended,
)
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.protocol import call_structured

#: 节点名（架构 §5.1）。
MODE_ROUTER = "mode_router"
COMPLETION_ROUTER = "completion_router"

#: 条件边出口标识。
ROUTE_CHAT = "chat"
ROUTE_TOOL = "tool"
ROUTE_EXECUTE = "execute"
ROUTE_REFLECT = "reflect"
ROUTE_TO_APPROVAL = "approval"
ROUTE_APPROVE = "approve"
ROUTE_EDIT = "edit"
ROUTE_REJECT = "reject"
ROUTE_WITH_TOOLS = "tools"
ROUTE_WITHOUT_TOOLS = "final"
ROUTE_CONTINUE = "continue"
ROUTE_RETRY = "retry"
ROUTE_ASK_USER = "ask_user"
ROUTE_FINISH = "finish"
ROUTE_LOOP_LIMIT = "loop_limit"
ROUTE_MEMORY_WRITE = "memory_write"

#: 分类提示的固定标记；提示与测试都用它识别“这是一次分类调用”。
ROUTE_CLASSIFIER_MARKER = "意图分类器"

#: 工具循环达到上限时的说明（架构 §11.5）。
LOOP_LIMIT_REPLY = (
    "已达到本轮的模型调用上限，为避免无限循环我停下来了。"
    "当前任务尚未完成，请把需求拆得更具体，或直接指出下一步要做什么。"
)

#: LLM 分类不可用时的关键词兜底（按从上到下的顺序匹配，返回命中的词）。
TOOL_INTENT_KEYWORDS: tuple[str, ...] = (
    "读取",
    "列出",
    "打开文件",
    "目录",
    "文件夹",
    "文件内容",
    "搜索",
    "查找",
    "修改",
    "编辑",
    "替换",
    "写入",
    "新建",
    "创建文件",
    "删除",
    "重命名",
    "运行",
    "执行",
    "命令",
    "终端",
    "安装依赖",
    "跑测试",
    "read ",
    "list ",
    "open file",
    "search",
    "find ",
    "grep",
    "edit ",
    "replace",
    "write ",
    "create file",
    "delete",
    "remove",
    "run ",
    "execute",
    "command",
    "terminal",
    "install",
    "pytest",
    "git ",
)

class RouteDecision(BaseModel):
    """LLM 分类输出的结构化结果。"""

    mode: Literal["chat", "tool"]
    reason: str = ""


@dataclass(frozen=True)
class RouteResult:
    """最终路由结果。"""

    mode: AgentMode
    reason: str


# --------------------------------------------------------------------------- 模式判定


def mode_router_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """判定本次请求的模式并写入 State。"""
    session_mode = state.get("session_mode")
    if session_mode == "chat":
        result = RouteResult(mode="chat", reason="本会话固定为闲聊（仅可读取文件）")
    elif session_mode == "agent":
        result = RouteResult(mode="tool", reason="本会话固定为 Agent")
    else:
        # 旧的直接调用 Graph 的入口尚未传会话模式，保留原有自动判定。
        result = decide_mode(
            user_text=str(state.get("user_input", "")),
            context=runtime.context,
        )
    record = make_decision_record(
        str(state.get("run_id") or "run"),
        stage="mode_router",
        summary=f"{result.mode}：{result.reason}",
        loop_count=int(state.get("loop_count", 0)),
    )
    return {
        "mode": result.mode,
        "route_reason": result.reason,
        "decision_trace": merge_appended(
            state.get("decision_trace", []), [record], decision_record_key
        ),
    }


def decide_mode(*, user_text: str, context: RuntimeContext) -> RouteResult:
    """判定模式：LLM 分类优先，失败退化为规则，再不行保守判为闲聊。

    Args:
        user_text: 本轮用户输入。
        context: 运行上下文，提供模型实例与工作区信息。

    Returns:
        模式与判定依据（``route_reason``）。
    """
    request = user_text.strip()
    if not request:
        return RouteResult(mode="chat", reason="输入为空，按闲聊处理")

    try:
        decision = _classify_with_model(request, context)
    except Exception as exc:  # noqa: BLE001 — 任何分类失败都必须退化为规则判定
        failure = type(exc).__name__
        keyword = match_tool_keyword(request)
        if keyword is None:
            return RouteResult(
                mode="chat",
                reason=f"规则兜底：未命中工具意图，按闲聊处理（LLM 分类失败：{failure}）",
            )
        return RouteResult(
            mode="tool",
            reason=f"规则兜底：命中关键词「{keyword}」（LLM 分类失败：{failure}）",
        )

    return RouteResult(mode=decision.mode, reason=f"LLM 分类：{decision.reason or '未给出理由'}")


def match_tool_keyword(text: str) -> str | None:
    """返回文本中命中的第一个工具意图关键词；没有则返回 ``None``。"""
    lowered = text.lower()
    for keyword in TOOL_INTENT_KEYWORDS:
        if keyword in lowered:
            return keyword.strip()
    return None


def build_classifier_prompt(context: RuntimeContext) -> str:
    """分类提示（不使用结构化输出接口，T09 再接原生 JSON Schema）。"""
    return (
        f"{ROUTE_CLASSIFIER_MARKER}：判断用户请求属于闲聊，还是需要访问本机文件或执行命令。\n"
        "\n"
        "只输出一个 JSON 对象，不要输出任何其他文字：\n"
        '{"mode": "chat", "reason": "一句话说明理由"}\n'
        "\n"
        "判定规则：\n"
        '- 需要读取、列出、搜索、修改、创建、删除本机文件，或需要执行命令、运行测试时，mode 取 "tool"。\n'
        '- 普通问答、闲聊、解释概念、只给代码片段而不落地到文件时，mode 取 "chat"。\n'
        '- 询问"我之前说过的偏好/设定""我习惯用什么""我们上次的结论是什么"，以及**已经记住的项目'
        '事实或约定**（例如"我们这个项目默认用什么跑自动化测试"）这类**回忆型问题**，mode 取 "chat"：'
        '答案来自系统提示里已召回的记忆，不需要读写文件或执行命令；'
        '只有当用户明确要求检查当前文件或运行命令时才取 "tool"。\n'
        '- 判断标准是"完成这个请求**是否必须**访问本机文件或执行命令"；请求里只是**提到**文件名、'
        '工具名或"测试""配置"这类词，并不等于需要访问，不要据此取 "tool"。\n'
        '- 不确定时取 "chat"。\n'
        "\n"
        f"工作区根目录：{context.workspace_root.as_posix()}\n"
    )


def _classify_with_model(request: str, context: RuntimeContext) -> RouteDecision:
    """调用模型做一次分类，走结构化输出降级链（架构 §11.7）。"""
    capabilities = context.model_capabilities
    return call_structured(
        context.model_adapter,
        [
            SystemMessage(content=build_classifier_prompt(context)),
            HumanMessage(content=request),
        ],
        RouteDecision,
        capabilities=capabilities if isinstance(capabilities, ModelCapabilities) else None,
    )


# --------------------------------------------------------------------------- 分支选择


def route_by_mode(state: AgentState) -> str:
    """条件边：按 ``mode`` 返回 ``chat`` 或 ``tool``。"""
    return ROUTE_TOOL if state.get("mode") == "tool" else ROUTE_CHAT


def route_after_compression(state: AgentState) -> str:
    """压缩节点之后按模式分流（§5.2 走闲聊节点，§5.3 走决策节点）。"""
    if state.get("session_mode") == "chat":
        return ROUTE_TOOL  # 固定闲聊会话通过只读工具决策取得文件证据。
    return ROUTE_TOOL if state.get("mode") == "tool" else ROUTE_CHAT


def route_after_decision(state: AgentState) -> str:
    """有调用过安全门；新循环回答经机械收口，旧协议仍按计划反思。

    计划里还有未完成步骤时**不能直接收尾**：模型偶尔会在第二步就给出"任务完成了"的回答，
    直接进 ``final_response_node`` 会让整张图在第二步结束、第三步永远不执行——而且
    ``task_status`` 还显示 ``running``，用户看到一个半截任务（复审实证）。
    先走反思，由 ``_advance_plan`` 的机械规则决定继续还是收尾（完成一步不等于完成整个任务）。
    """
    if state.get("pending_tool_calls"):
        return ROUTE_WITH_TOOLS
    if state.get("session_mode") == "chat":
        return ROUTE_CHAT  # 决策无文件调用时，交 chat_node 流式输出最终回答。
    if state.get("agent_loop_version") == 1:
        if state.get("task_status") == "failed":
            return ROUTE_WITHOUT_TOOLS
        current_events = [event for event in state.get("tool_events", [])
                          if event.run_id == state.get("run_id")]
        if not current_events and not state.get("plan_managed"):
            return ROUTE_WITHOUT_TOOLS  # 原有直接回答路径，不伪造工具证据。
        return ROUTE_REFLECT  # 仅机械收口；不再另调模型判断答案。
    if _plan_has_unfinished_steps(state):
        return ROUTE_REFLECT
    return ROUTE_WITHOUT_TOOLS


def _plan_has_unfinished_steps(state: AgentState) -> bool:
    """多步计划里是否还有 ``pending`` / ``running`` 的步骤。

    **单步计划不走这条路**：单步任务的正常收尾就是"模型直接给回答"，把它也拉去反思会改变既有
    行为（验收基线：单步任务不回归）。多步计划里最后一步直接回答时多绕一次反思，结论不变。
    """
    plan = list(state.get("plan") or [])
    if len(plan) <= 1:
        return False
    return any(step.status in {"pending", "running"} for step in plan)


def route_after_safety(state: AgentState) -> str:
    """安全门之后：有待审批的动作先走审批，有放行调用就执行，否则直接反思。"""
    if state.get("pending_approval") is not None:
        return ROUTE_TO_APPROVAL
    if state.get("pending_tool_calls"):
        return ROUTE_EXECUTE
    return ROUTE_CHAT if state.get("session_mode") == "chat" else ROUTE_REFLECT


def route_after_tool_execution(state: AgentState) -> str:
    """闲聊读取后继续让模型形成回答；Agent 工具结果交反思节点。"""
    return ROUTE_CHAT if state.get("session_mode") == "chat" else ROUTE_TOOL


def route_after_approval(state: AgentState) -> str:
    """审批节点之后的三出口（架构 §5.3）：批准执行 / 修改回规划 / 拒绝结束。"""
    history = list(state.get("approval_history", []))
    decision = str(history[-1].get("decision")) if history else ""
    if decision == "approve":
        return ROUTE_APPROVE
    if decision == "edit":
        return ROUTE_EDIT
    return ROUTE_REJECT


# --------------------------------------------------------------------------- 完成判定


#: 达到循环上限时仍然"想继续"的动作：只有它们会被上限拦住（架构 §11.5）。
#: ``finish`` / ``ask_user`` 本轮就能收尾，不受上限影响——拦它们只会把正常结论变成"失败"。
#: 反思节点也用这个集合判"空转保护"（§5.3），所以是公开常量，避免两处各写一份。
LOOPING_ACTIONS: tuple[str, ...] = ("continue", "retry")


def completion_router_node(state: AgentState) -> AgentState:
    """架构 §5.3：反思之后唯一决定后续走向。

    本节点的状态改动只有一种情况：**还想继续/重试但已达循环上限**时补一句明确说明，
    让用户知道任务没做完、为什么停下，而不是看到一个半截回答。

    上限**不覆盖**已经确定的 ``finish`` / ``ask_user``：那一轮反思已经给出可收尾的结论
    （而且是基于机械证据的结论），把它改成"失败"会让正常终止在边界轮次丢失（复审实证：
    ``verified + finish`` 与 ``ask_user`` 都被改成了 ``loop_limit + failed``）。
    """
    if _loop_limit_blocks(state):
        return {"messages": [AIMessage(content=LOOP_LIMIT_REPLY)], "task_status": "failed"}
    return {}


def completion_route(state: AgentState) -> str:
    """条件边：把反思结论映射为下一个分支（循环上限只拦 ``continue`` / ``retry``）。

    ``finish`` 且反思给出了记忆候选时，先走 ``memory_write_node``（架构 §5.3）。写入链路要求
    候选经过反思验证，所以这是**唯一**的写入出口——其余分支都不直接写库。
    """
    action = _next_action(state)
    if action in LOOPING_ACTIONS and loop_limit_reached(state):
        return ROUTE_LOOP_LIMIT
    route = {
        "continue": ROUTE_CONTINUE,
        "retry": ROUTE_RETRY,
        "ask_user": ROUTE_ASK_USER,
        "finish": ROUTE_FINISH,
    }.get(action, ROUTE_FINISH)
    if route == ROUTE_FINISH and _has_memory_candidates(state):
        return ROUTE_MEMORY_WRITE
    return route


def route_after_task_memory(state: AgentState) -> str:
    return ROUTE_MEMORY_WRITE if _has_memory_candidates(state) else ROUTE_FINISH


def _has_memory_candidates(state: AgentState) -> bool:
    """反思结果里是否有记忆候选（决定本轮是否经过 ``memory_write_node``）。"""
    result = state.get("reflection_result")
    return bool(result.memory_candidates) if isinstance(result, ReflectionResult) else False


def _next_action(state: AgentState) -> str:
    """反思给出的下一步动作；没有反思结论时按 ``finish`` 处理（与条件边一致）。"""
    result = state.get("reflection_result")
    return result.next_action if isinstance(result, ReflectionResult) else "finish"


def _loop_limit_blocks(state: AgentState) -> bool:
    """循环上限是否真的拦住了本轮（只在"还想继续/重试"时成立）。"""
    return _next_action(state) in LOOPING_ACTIONS and loop_limit_reached(state)


def loop_limit_reached(state: AgentState) -> bool:
    """是否达到配置的模型调用上限（架构 §11.5）。"""
    limit = state.get("max_loop_count")
    if not limit:
        return False
    return int(state.get("loop_count", 0)) >= int(limit)

"""自主委派工具（契约 §18.7；2026-10-13）。

一个工具 `delegate_research`：主 Agent 判断某段只读调查**可以并行**、或者**不值得占用自己的
上下文**时，把 1–3 个只读子任务派给隔离子 Agent，只把父图核对过的原文报告拿回来。

## 为什么是工具，而不是页面上的一排参数

v0.7 把三类确定性工作流做成了输入区的参数表单（用户手填 glob / 源码与测试路径 / 子任务清单）。
2026-10-13 撤下表单并改成这个工具，理由是"谁来选"错了：**要不要派子 Agent 是编排决策，属于内部机制**，
不该让用户每次手工声明；用户要表达的是"允许/不允许"（页面上的一个开关），而不是"这次用什么模式"。
参照实现（CodeBuddy 的 agentic 子代理）也是这个分工：`Description` 必填、由主 Agent 据它判断何时调用。

## 边界（契约里写死的，不是这里的自由发挥）

- **子 Agent 只能是只读的**：能力固定为父有效能力与 ``files_read`` / ``code_search`` 的交集
  （:func:`~local_agent.capabilities.readonly_child_capabilities`），子策略在工作区内的 scope 目录上
  重建，外部路径固定 deny、不继承父审批、不允许 grep/glob 落盘。**没有写入、命令、联网与再次委派**，
  委派深度固定 1。
- **子 Agent 不读父消息、记忆、审批与运行快照**，也不写记忆；它只拿到显式问题与检索计划 Schema。
- **每个子任务一次模型调用**（生成严格 JSON 检索计划）+ 一次只读子图（0 次模型调用）；上限与并发来自
  ``workflows`` 段（``max_children`` / ``max_concurrency`` / ``child_timeout_seconds`` 与各项额度）。
- **失败不重试**：哪一路失败就如实说哪一路、为什么；父报告只给**核对过**的原文与行号。
- **审计不丢**：子任务的工具事件与模型调用记录由运行侧收集，节点收尾时并回父轮次（§18.7），
  所以轨迹页与用量页都看得到这次委派花了什么。

本模块只负责"工具外形 + 参数校验 + 结果形状"；真正的执行是
:func:`~local_agent.graph.delegated_workflow.run_delegation`——与显式子图**共用同一套**
``reserve → dispatch → report``，不引入第二套实现（§16.7）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, Field, ValidationError

#: 运行侧注入的委派执行入口：``(tasks, concurrency) -> JSON 字符串``。
#:
#: 为什么是一个回调而不是"工具自己去跑子图"：工具在 ``tool_execution_node`` 里只能拿到调用参数
#: （`graph/nodes.py` 的 `_invoke_tool` 只调 `tool.invoke(call)`），运行依赖（配置、模型、策略、
#: 用量录音机、取消信号）都在 ``RuntimeContext`` 里。这些依赖**只在装配时**可得，所以由
#: `ToolRegistry.build` 在构造注册表时把入口闭包进来（§18.7 的 A 方案）。
DelegateFn = Callable[[Sequence[Mapping[str, str]], int], str]

DOC = (
    "把一段**只读调查**派给隔离子 Agent 并行去查，只拿回核对过的原文（路径 + 行号 + 原文）。"
    "适合：需要同时铺开看多个目录/多个问题，或者检索过程很长、不想占满你自己的上下文。"
    "不适合：你自己一次 read_file / grep 就能答完的问题（每个子任务会多花一次模型调用，不要滥用）；"
    "子Agent可以联网和运行只读命令，但不能写文件、修改代码、Git提交或再次委派。"
    "每个子任务给出工作区内的相对 `scope` 目录（`.` 表示工作区根）与一个明确的问题。"
)


class DelegatedTask(BaseModel):
    """一个只读子任务。"""

    #: 工作区内相对目录；`.` 表示父工作区根。解析后的目录必须已存在且落在工作区内。
    scope: str = Field(default=".", min_length=1, max_length=200)
    #: 要查清的问题（字面文本，不是命令）。
    question: str = Field(min_length=1, max_length=1000)


class DelegatedTaskList(BaseModel):
    """工具的入参形状（1–3 个子任务；并发 1–2）。"""

    tasks: list[DelegatedTask] = Field(min_length=1)
    #: 同时执行几路；默认 1（串行），最多 2。
    concurrency: int = Field(default=1, ge=1)


class ChildOutcome(BaseModel):
    """一个子任务的结局（只报事实：状态、目录、计数与失败原因）。

    这几个计数是照着参照实现的任务面板给的（状态 / 工具调用次数），模型自己要用它们判断
    "这一路值不值得再派"，页面也直接把同一份数据摊成一行行（§18.7）。

    **刻意不含"预留 token"**（2026-10-13 裁定）：那是 `_prepare` 切出来的**上限**，同一份配置下每
    一路几乎一样，且长得像费用——放在对话卡与工具结果里只会被读成"这一路花了多少"
    （§15.7 要求预留与实际分开写）。它仍留在子报告（`ChildResearchReport`）里作审计事实，
    不进这两处面向用户与模型的载荷。
    """

    scope: str
    status: Literal["completed", "failed"]
    tool_calls: int = 0
    model_calls: int = 0
    evidence_count: int = 0
    error: str | None = None


class DelegatedResearchResult(BaseModel):
    """`delegate_research` 的结果。

    ``report`` 是**父图核对过**的原文摘录（每行带真实文件行号），没有核对通过就没有原文；
    ``status`` 为 ``failed`` 时 ``report`` 里写的是失败原因，不要把它当成结论。
    """

    status: Literal["completed", "failed"]
    report: str
    children: list[ChildOutcome] = Field(default_factory=list)
    note: str | None = None


def build_delegate_tools(delegate: DelegateFn | None) -> list[BaseTool]:
    """构造 `delegate_research`。

    与 `web_tools` / `git_tools` / `skill_tools` 同一口径：**工具总是被构造**（能力覆盖校验要求登记的
    工具都在注册表里），"能不能用"由能力层决定——页面上的开关关着时该能力不进有效集合，工具对模型
    不可见。`delegate` 为 ``None`` 时（没有运行依赖的纯测试/脚本路径）调用会得到一句明确的失败，
    而不是静默给个空结果。

    Args:
        delegate: 运行侧注入的执行入口；``None`` 表示当前上下文不支持自主委派。
    """

    @langchain_tool("delegate_research", args_schema=DelegatedTaskList, description=DOC)
    def delegate_research(tasks: list[DelegatedTask], concurrency: int = 1) -> str:
        """把只读调查派给隔离子 Agent。"""
        if delegate is None:
            return DelegatedResearchResult(
                status="failed",
                report="当前上下文不支持自主委派：委派入口没有接上（这是一条实现路径问题，不是你的调用方式问题）。",
                note="未执行任何子任务，也没有产生模型调用。",
            ).model_dump_json()
        payload = [{"scope": task.scope.strip(), "question": task.question.strip()} for task in tasks]
        if any(not item["scope"] or not item["question"] for item in payload):
            return DelegatedResearchResult(
                status="failed",
                report="子任务的 scope 与 question 都不能为空。",
            ).model_dump_json()
        try:
            return delegate(payload, concurrency)
        except (ValidationError, ValueError) as exc:
            # 参数 / 预算 / 授权类失败：如实说原因，不重试、不改跑自由循环。
            return DelegatedResearchResult(status="failed", report=str(exc)[:512]).model_dump_json()
        # 其余异常交给执行节点如实报告工具失败（不在这里吞掉）。

    return [delegate_research]

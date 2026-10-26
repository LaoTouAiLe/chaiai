"""计划推进（2026-10-26 从 `nodes.py` 拆出）。

这一组回答「下一步做哪一步、这一步算不算卡住」：推进当前步骤、判定重规划、停滞守卫、
重复失败的反馈阈值。判据都基于 State 里的 plan / step 计数，不碰工具执行本身。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from langchain_core.messages import (
    AIMessage,
    ToolMessage,
)

from local_agent.graph.routers import LOOPING_ACTIONS
from local_agent.graph.state import (
    AgentState,
    PlanStep,
)
from local_agent.reflection.reflector import (
    MAX_PLAN_STEPS,
    ReflectionDecision,
)
from local_agent.reflection.validators import (
    EvidenceLevel,
)

from .common import _delivered_answer
from .names import ReflectionAction

#: 连续多少轮没有动作就判为停滞（拆分前定义在 nodes.py）。
STALLED_ROUNDS_WITHOUT_ACTION = 2


@dataclass(frozen=True, kw_only=True)
class _PlanProgress:
    """一次反思之后计划的推进结果。"""

    plan: list[PlanStep]
    current_step_index: int
    #: 本次新增的已完成步骤 id（追加进 ``completed_steps``）。
    completed_steps: tuple[str, ...]
    next_action: ReflectionAction
    #: 计划还没走完时强制置 ``False``：否则"第一步做完"会被当成"任务完成"，
    #: ``task_status`` 会显示 completed，与"仍在继续执行计划"自相矛盾。
    goal_achieved: bool
    note: str | None = None

def _advance_plan(
    state: AgentState,
    *,
    goal_achieved: bool,
    next_action: ReflectionAction,
    decision: ReflectionDecision,
) -> _PlanProgress:
    """按反思结论推进计划（T20 的三条硬语义，复审定稿）。

    1. **完成一步 ≠ 完成整个任务**：当前步完成、但计划里还有未走完的步骤时，即使模型给了
       ``finish`` 也改成 ``continue``——这是机械规则，不接受"第一步做完就宣布收尾"；只有全部步骤
       完成后才允许收尾。当前步同时记入 ``completed_steps``（步骤 id 含 ``run_id``，天然按任务隔离）。
    2. **重规划必须真的改计划**：``plan_adjustment`` 非空且当前步失败时，把该步标为 ``failed``，
       并**在它之后插入一步**（描述即调整内容），``current_step_index`` 指向新步。只把调整写进
       ``result_summary`` 不算重规划——那样模型以为改了、执行仍按老计划走。T20 只实现这一种调整
       方式：够用、可验证，也不会误伤已完成步骤。计划已达 :data:`MAX_PLAN_STEPS`（与拆步上限同一个
       常量）时**不再插入**：改为原地按调整重做当前步骤并计一次重试，用既有重试上限兜底——
       否则计划会无限增长，任务永远收不了尾（实测）。
    3. **已完成步骤不可重写、不可重跑**：只更新当前步及其之后；``completed_steps`` 只追加、不清空。
    """
    plan = list(
        _updated_plan(
            state, goal_achieved=goal_achieved, next_action=next_action, decision=decision
        )
    )
    index = int(state.get("current_step_index", 0))
    if not plan or index < 0 or index >= len(plan):
        return _PlanProgress(
            plan=plan,
            current_step_index=index,
            completed_steps=(),
            next_action=next_action,
            goal_achieved=goal_achieved,
        )
    step = plan[index]
    known = set(state.get("completed_steps") or ())
    completed: tuple[str, ...] = ()
    action = next_action
    achieved = goal_achieved
    note: str | None = None
    if goal_achieved:
        if step.id not in known:
            completed = (step.id,)
        remaining = [item for item in plan[index + 1 :] if item.status != "completed"]
        if remaining and not _delivered_answer(state):
            action = "continue"
            # 计划没走完就不是"任务完成"：否则 task_status 会显示 completed，与"仍在继续"矛盾
            achieved = False
            note = (
                f"步骤 {index + 1}「{step.description}」已完成，计划还有 {len(remaining)} 步，"
                "继续执行（完成一步不等于完成整个任务）"
            )
            next_index = index + 1
            plan[next_index] = replace(plan[next_index], status="running")
        elif remaining:
            # **已经交付了可交付的结论**：不再让"计划记账还差几步"压过"任务目标已达成"
            # （见 :func:`_delivered_answer`）。剩余步骤标 ``skipped``——目标是一次性达成的，它们
            # 已经作废；留在那里当 ``pending`` 会让计划与任务状态互相矛盾。
            next_index = index
            note = (
                f"任务目标已达成且本轮已给出结论，计划中剩余 {len(remaining)} 步"
                "已被一次性完成，标记为 skipped 并收尾"
            )
            for position in range(index + 1, len(plan)):
                if plan[position].status != "completed":
                    plan[position] = replace(plan[position], status="skipped")
        else:
            next_index = index
    elif _should_replan(step, next_action=next_action, decision=decision):
        adjustment = decision.plan_adjustment.strip()
        if len(plan) >= MAX_PLAN_STEPS:
            # 计划已达拆步上限：再插入新步骤会让任务永远收不了尾（实测真实模型把一句问句拆成 4 步后
            # 又连续插入 5 个调整步骤，一直"继续"到循环上限也走不完）。改为**原地按调整重做当前步骤**
            # 并计一次重试——计划真的变了、不越上限，且沿用既有的重试上限兜底。
            plan[index] = replace(
                step,
                description=adjustment,
                status="running",
                result_summary=f"按调整重做：{step.description}",
                retry_count=step.retry_count + 1,
            )
            next_index = index
            action = "retry"
            note = (
                f"计划已达 {MAX_PLAN_STEPS} 步上限，不再插入新步骤，"
                f"改为按调整重做当前步骤「{adjustment}」"
            )
        else:
            plan[index] = replace(step, status="failed")
            plan.insert(
                index + 1,
                PlanStep(
                    id=f"{_step_scope(step.id)}:step-{len(plan) + 1}",
                    description=adjustment,
                    status="running",
                    expected_evidence="按调整做法之后的工具结果",
                ),
            )
            next_index = index + 1
            action = "continue"
            note = f"步骤 {index + 1} 失败，按反思的调整插入新步骤「{adjustment}」"
    else:
        next_index = index
    return _PlanProgress(
        plan=plan,
        current_step_index=next_index,
        completed_steps=completed,
        next_action=action,
        goal_achieved=achieved,
        note=note,
    )

def _should_replan(
    step: PlanStep, *, next_action: ReflectionAction, decision: ReflectionDecision
) -> bool:
    """是否要按 ``plan_adjustment`` 真的改计划（T20 只处理这一种调整方式）。"""
    if next_action not in ("continue", "retry"):
        return False
    if step.status == "completed":
        return False
    return bool(decision.plan_adjustment.strip())

def _apply_stall_guard(
    state: AgentState,
    progress: _PlanProgress,
    decision: ReflectionDecision,
    *,
    trust: EvidenceLevel,
) -> tuple[ReflectionAction, ReflectionDecision, list[PlanStep], str | None]:
    """空转保护：连续多轮没有任何可执行动作时，不再把预算烧在重复调用上（架构 §5.3）。

    为什么必须是机械规则：反思说"继续"，但模型连续若干轮既没调用工具、也没产生新证据，而每一轮的
    输入（目标、计划、证据）几乎完全相同——再调一次不会有新信息。实测真实模型读完文件后连续 10 轮
    输出"任务已完成 / 无新指令"，把 ``agent.max_tool_loops`` 烧完才被上限拦下。

    两条出口，按"**有没有可交付的结论**"分开（2026-09-30 修）：

    1. **已经写出结论且机械证据通过校验 → ``finish``（交付）**。此前只有 ``ask_user`` 一条路，
       于是出现了一个自相矛盾的形态：模型在前几轮已经给出完整、带行号的答案，只因计划记账还差几步，
       最终交付给用户的却是"请确认下一步要做什么"——**结论明明已经在会话里，用户却拿不到它**。
       实测被精度层用例 ``locate_all`` 抓到（内容全对、终点 ``ask_user``）。交付的门槛有两个：
       ``trust == "verified"``（§5.4 的"错误宣称"门槛不动：证据没过校验就绝不说完成）与
       :func:`_delivered_answer`（真的写出了结论，而不是一句客套话或在反问用户）。
    2. **没有可交付的结论 → ``ask_user``**。计划里还有没走完的步骤，"计划没完成不许收尾"是
       §5.3/T20 的硬语义；当前步骤同时标为 ``blocked``，与 :func:`_updated_plan` 对 ``ask_user``
       的口径保持一致。
    """
    if progress.next_action not in LOOPING_ACTIONS:
        return progress.next_action, decision, progress.plan, None
    stalled = _stalled_rounds(state)
    if stalled < STALLED_ROUNDS_WITHOUT_ACTION:
        return progress.next_action, decision, progress.plan, None

    answer = _delivered_answer(state)
    if trust == "verified" and answer:
        return (
            "finish",
            decision.model_copy(update={"goal_achieved": True, "next_action": "finish"}),
            progress.plan,
            (
                f"连续 {stalled} 轮没有可执行动作，但已给出结论（{len(answer)} 字符）且机械证据"
                "通过校验：直接交付该结论，不再向用户追问（空转保护）"
            ),
        )

    unfinished = sum(1 for step in progress.plan if step.status != "completed")
    reason = (
        f"我已经连续 {stalled} 轮没有产生可执行的动作（既没有调用工具，也没有新的证据），"
        f"而计划里还有 {unfinished} 步没有完成；继续重复同样的调用不会有新结果，所以我先停下来。"
        "请确认下一步要做什么，或把需求拆得更具体、把计划改小一些。"
    )
    plan = _block_current_step(progress.plan, progress.current_step_index)
    return (
        "ask_user",
        decision.model_copy(update={"ask_user_reason": reason}),
        plan,
        f"连续 {stalled} 轮没有可执行动作，改为向用户说明（空转保护）",
    )

def _stalled_rounds(state: AgentState) -> int:
    """从消息尾部数：连续多少轮模型只给文字、没有产生工具调用。

    不需要新增 State 字段：有工具调用的一轮一定留下带 ``tool_calls`` 的 AI 消息，从尾部往前数
    纯文本 AI 消息就是"没有可执行动作"的轮数（架构 §5.3）。
    """
    count = 0
    for message in reversed(list(state.get("messages") or [])):
        if isinstance(message, AIMessage):
            if message.tool_calls:
                break
            count += 1
            continue
        if isinstance(message, ToolMessage):
            break
    return count

def _block_current_step(plan: Sequence[PlanStep], index: int) -> list[PlanStep]:
    """把当前步骤标为 ``blocked``（交给用户后这一步不再算"正在执行"）。"""
    if 0 <= index < len(plan) and plan[index].status == "running":
        return [*plan[:index], replace(plan[index], status="blocked"), *plan[index + 1 :]]
    return list(plan)

def _updated_plan(
    state: AgentState,
    *,
    goal_achieved: bool,
    next_action: ReflectionAction,
    decision: ReflectionDecision,
) -> list[PlanStep]:
    """更新当前步骤的状态与结果摘要（多步计划、依赖与重规划属于 T20）。"""
    plan = list(state.get("plan") or [])
    index = int(state.get("current_step_index", 0))
    if not plan or index < 0 or index >= len(plan):
        return plan
    step = plan[index]
    if goal_achieved:
        status = "completed"
    elif next_action == "ask_user":
        status = "blocked"
    else:
        status = "running"
    plan[index] = replace(
        step,
        status=status,
        result_summary=decision.plan_adjustment.strip() or step.result_summary,
        retry_count=step.retry_count + (1 if next_action == "retry" else 0),
    )
    return plan

def _apply_mechanical_guard(
    trust: EvidenceLevel, decision: ReflectionDecision
) -> tuple[bool, ReflectionAction, list[str]]:
    """机械结论优先，并把自相矛盾的组合归一。

    返回 ``(goal_achieved, next_action, 规范化说明)``。三条归一规则：

    1. 证据等级不是 ``verified`` 却判完成 → ``goal_achieved=False`` + ``ask_user``；
    2. 未达成目标却用 ``finish`` 收尾 → 改为 ``ask_user``；
    3. 判定完成却还要 ``continue`` / ``retry`` / ``ask_user`` → 归一为 ``finish``（否则会出现
       "步骤已完成却继续执行"的矛盾状态）。
    """
    notes: list[str] = []
    if trust != "verified" and decision.goal_achieved:
        notes.append("证据等级不是 verified，不能判定完成，改为向用户说明")
        return False, "ask_user", notes
    if decision.goal_achieved and decision.next_action != "finish":
        notes.append("已判定完成，动作归一为 finish")
        return True, "finish", notes
    if not decision.goal_achieved and decision.next_action == "finish":
        notes.append("未达成目标不能用 finish 收尾，改为向用户说明")
        return False, "ask_user", notes
    return decision.goal_achieved, decision.next_action, notes

def _apply_retry_limit(
    state: AgentState, next_action: ReflectionAction, *, max_retries: int
) -> tuple[ReflectionAction, str | None]:
    """重试次数达到 ``agent.max_retries_per_step`` 后不再重试，改为向用户说明。

    T20 起按**当前步骤**的 ``retry_count`` 判定：进入下一步后从 0 重新开始。用整个任务的计数器会让
    第一步花掉的重试额度把后面每一步都锁死（复审指出的口径）。没有可用计划对象时（直接把 State 传给
    节点的单测）回退到顶层 ``retry_count``。
    """
    if next_action != "retry":
        return next_action, None
    used = _current_step_retry_count(state)
    if used >= max_retries:
        return "ask_user", f"当前步骤重试已达上限（{max_retries} 次），改为向用户说明"
    return next_action, None

def _current_step_retry_count(state: AgentState) -> int:
    """当前步骤已重试的次数；没有可用步骤时回退到顶层 ``retry_count``。"""
    plan = list(state.get("plan") or [])
    index = int(state.get("current_step_index", 0))
    if 0 <= index < len(plan):
        return int(plan[index].retry_count)
    return int(state.get("retry_count", 0))


def _step_scope(step_id: str) -> str:
    """从步骤 id 取出任务作用域前缀（``<run_id>:step-N`` → ``<run_id>``）。"""
    head, sep, _ = step_id.partition(":step-")
    return head if sep else step_id

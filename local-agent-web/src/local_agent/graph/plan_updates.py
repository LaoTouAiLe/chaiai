"""把显式计划工具的合法输出映射到已有计划状态；不依赖模型判官。"""

from dataclasses import replace

from local_agent.graph.state import AgentState, PlanStep
from local_agent.tools.plan_tools import PlanUpdate


def apply_plan_update(state: AgentState, update: PlanUpdate) -> AgentState:
    previous = list(state.get("plan") or [])
    by_description = {step.description: step for step in previous}
    supplied = {item.step.strip(): item.status for item in update.plan}
    for step in previous:
        if step.status == "completed" and supplied.get(step.description) != "completed":
            raise ValueError("已完成的步骤必须保留，不能删除或撤回")
    new_completions = [
        item for item in update.plan
        if item.status == "completed"
        and (item.step.strip() not in by_description
             or by_description[item.step.strip()].status != "completed")
    ]
    evidence = state.get("reflection_result")
    pending_work = [call for call in state.get("pending_tool_calls", [])
                    if call.get("name") != "update_plan"]
    if new_completions and pending_work:
        raise ValueError("同批工具结果尚未交回模型，不能同时将步骤记为完成；看到结果后再更新")
    if new_completions and (evidence is None or evidence.result_trust != "verified"):
        raise ValueError("尚无已核对的工具结果，不能将步骤记为完成；先执行并读取结果")
    used_ids = {step.id for step in previous} | set(state.get("completed_steps") or [])
    next_id = 1
    plan: list[PlanStep] = []
    for item in update.plan:
        description = item.step.strip()
        old = by_description.get(description)
        if old is None:
            while f"{state.get('run_id') or 'run'}:step-{next_id}" in used_ids:
                next_id += 1
            identity = f"{state.get('run_id') or 'run'}:step-{next_id}"
            used_ids.add(identity)
            old = PlanStep(id=identity, description=description)
        plan.append(replace(
            old, status="running" if item.status == "in_progress" else item.status,
            result_summary=update.explanation.strip() or old.result_summary,
        ))
    index = next((i for i, step in enumerate(plan) if step.status == "running"), None)
    if index is None:
        index = next((i for i, step in enumerate(plan)
                      if step.status in {"pending", "blocked"}), len(plan) - 1)
    known = set(state.get("completed_steps") or [])
    result: AgentState = {"plan": plan, "current_step_index": index, "plan_managed": True}
    completed = [step.id for step in plan if step.status == "completed" and step.id not in known]
    if completed:
        result["completed_steps"] = completed
    return result

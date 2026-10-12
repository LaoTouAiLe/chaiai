"""显式计划控制工具：模型更新清单，图负责把更新写入 Checkpoint。"""

from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PLAN_STEPS = 5


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: str = Field(min_length=1, max_length=500)
    status: Literal["pending", "in_progress", "completed", "blocked", "skipped"]


class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanation: str = Field(default="", max_length=1000)
    plan: list[PlanItem] = Field(min_length=1, max_length=MAX_PLAN_STEPS)

    @model_validator(mode="after")
    def validate_steps(self) -> "PlanUpdate":
        descriptions = [item.step.strip() for item in self.plan]
        if any(not text for text in descriptions) or len(set(descriptions)) != len(descriptions):
            raise ValueError("步骤描述不能为空或重复")
        if sum(item.status == "in_progress" for item in self.plan) > 1:
            raise ValueError("同一时间只能有一个正在执行的步骤")
        return self


def build_plan_tools() -> list[BaseTool]:
    @tool(args_schema=PlanUpdate)
    def update_plan(plan: list[dict[str, str]], explanation: str = "") -> str:
        """更新当前任务计划（不是文件操作）。提交完整清单，最多五步，最多一步 in_progress。

        用 pending / in_progress / completed / blocked / skipped 表达进度。
        只有已经看到结果并确认目标达成的步骤才能 completed。
        每次带新 completed 的更新必须单独调用，不与任何其它工具同批；成功后再发下一批工具。
        已完成步骤必须保留；调整计划时解释原因，任务受阻时用 blocked；任务全部做完才交付最终回答。
        """
        return PlanUpdate.model_validate({"plan": plan, "explanation": explanation}).model_dump_json()

    return [update_plan]

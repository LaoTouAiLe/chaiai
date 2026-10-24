"""显式计划控制工具：模型更新清单，图负责把更新写入 Checkpoint。"""

from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: str = Field(min_length=1, max_length=500)
    status: Literal["pending", "in_progress", "completed", "blocked", "skipped"]


class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanation: str = Field(default="", max_length=1000)
    plan: list[PlanItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_steps(self) -> "PlanUpdate":
        descriptions = [item.step.strip() for item in self.plan]
        if any(not text for text in descriptions) or len(set(descriptions)) != len(descriptions):
            raise ValueError("步骤描述不能为空或重复")
        return self


class TaskControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["continue", "complete", "need_input"]
    message: str = ""

    @model_validator(mode="after")
    def require_final_message(self) -> "TaskControl":
        if self.action != "continue" and not self.message.strip():
            raise ValueError("完成或需要用户输入时，请提供给用户的具体说明")
        return self


def build_plan_tools() -> list[BaseTool]:
    @tool(args_schema=TaskControl)
    def task_control(action: str, message: str = "") -> str:
        """由主模型声明任务继续、完成或需要用户输入，不使用额外完成度判定模型。

        continue表示继续工作；complete表示已完成，message为最终交付内容；
        need_input表示需要用户提供信息，message说明缺少什么。普通进度文字不会自动结束任务。
        根据用户目标和实际工具结果自行决定，不要求计划状态或程序校验通过。
        """
        return TaskControl.model_validate({"action": action, "message": message}).model_dump_json()

    @tool(args_schema=PlanUpdate)
    def update_plan(plan: list[dict[str, str]], explanation: str = "") -> str:
        """更新当前任务计划。步骤数量、并行进度及后续调整由模型根据任务决定。

        用 pending / in_progress / completed / blocked / skipped 表达进度。
        只有已经看到结果并确认目标达成的步骤才能 completed。
        可以与独立工具同批调用；需要修正时可以撤回、删除步骤，空清单表示清空计划。
        """
        return PlanUpdate.model_validate({"plan": plan, "explanation": explanation}).model_dump_json()

    return [update_plan, task_control]

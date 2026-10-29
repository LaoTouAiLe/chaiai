"""显式计划控制工具：模型更新清单，图负责把更新写入 Checkpoint。"""

from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


def _require_visible_text(value: str, *, field: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field}不能为空")
    if any(character < " " and character not in "\n\t" for character in text):
        raise ValueError(f"{field}里不能包含控制字符")
    return text


class AskUserItem(BaseModel):
    """题组里的一题：问题 + 最多 3 个可选答案 + 是否允许自由输入（架构 §6.14）。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(default_factory=list)
    allow_free_text: bool = True

    @field_validator("question")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        return _require_visible_text(value, field="问题")

    @field_validator("options")
    @classmethod
    def clean_options(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = _require_visible_text(str(item), field="选项")
            if len(text) > 80:
                raise ValueError("单个选项最长 80 字符")
            if text not in cleaned:
                cleaned.append(text)
        if len(cleaned) > 3:
            raise ValueError("每题最多 3 个选项")
        return cleaned


class AskUser(BaseModel):
    """一次问一组问题（1–5 题），用户答完一起提交（架构 §6.14）。

    保留 `question` 单值写法：模型只问一句时不必套数组（归一成一题组）。
    """

    model_config = ConfigDict(extra="forbid")

    questions: list[AskUserItem] = Field(default_factory=list)
    #: 兼容单题写法：`questions` 为空时才使用。
    question: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "AskUser":
        if not self.questions:
            text = (self.question or "").strip()
            if not text:
                raise ValueError("至少给一个要问的问题")
            self.questions = [AskUserItem(question=text)]
        if len(self.questions) > 5:
            raise ValueError("一次最多问 5 个问题（请合并或分多轮）")
        # 归一之后不再回传单题字段：模型看到的只有确定的题组结构。
        self.question = None
        return self


def build_plan_tools() -> list[BaseTool]:
    @tool(args_schema=TaskControl)
    def task_control(action: str, message: str = "") -> str:
        """由主模型声明任务继续、完成或需要用户输入，不使用额外完成度判定模型。

        continue表示继续工作；complete表示已完成，message为最终交付内容；
        need_input表示需要用户提供信息，message说明缺少什么。普通进度文字不会自动结束任务。
        根据用户目标和实际工具结果自行决定，不要求计划状态或程序校验通过。
        执行中途缺一个拿到就能继续的输入时用 ask_user（它会挂起等你回答），不要用 need_input 结束本轮。
        """
        return TaskControl.model_validate({"action": action, "message": message}).model_dump_json()

    @tool(args_schema=AskUser)
    def ask_user(questions: list[dict[str, object]] | None = None, question: str = "") -> str:
        """向用户提问并等待答复后继续当前任务（不结束这一轮）。

        只用于执行中缺少你无法从上下文、文件或工具结果推断的输入：目标目录、分支名、二选一的方案等。
        **缺多个信息时一次问清**（用 questions 传 1–5 题，每题最多给 3 个 options）——
        页面会把它做成可翻页的题组，用户答完一次性提交；不要一个问题挂一次。
        不要用它确认你能自行推断的默认值，也不要重复问同一件事。
        任务已经可以收尾、剩下的判断只能由用户做时，改用 task_control(action="need_input")——
        那个会结束本轮，本工具不会。
        """
        return AskUser.model_validate(
            {"questions": questions or [], "question": question or None}
        ).model_dump_json(exclude_none=True)

    @tool(args_schema=PlanUpdate)
    def update_plan(plan: list[dict[str, str]], explanation: str = "") -> str:
        """更新当前任务计划。步骤数量、并行进度及后续调整由模型根据任务决定。

        用 pending / in_progress / completed / blocked / skipped 表达进度。
        只有已经看到结果并确认目标达成的步骤才能 completed。
        可以与独立工具同批调用；需要修正时可以撤回、删除步骤，空清单表示清空计划。
        """
        return PlanUpdate.model_validate({"plan": plan, "explanation": explanation}).model_dump_json()

    return [update_plan, task_control, ask_user]

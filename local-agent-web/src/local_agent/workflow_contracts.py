"""v0.7 工作流输入与可序列化状态；不依赖 Graph 或应用服务。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

READONLY_RESEARCH = "readonly_research"
CODE_CHANGE = "code_change"
DELEGATED_RESEARCH = "delegated_research"


class ReadonlyResearchRequest(BaseModel):
    """只接受工作区内的相对 glob 与字面关键词，不接受控制流。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    pattern: str = Field(min_length=1, max_length=200)
    keywords: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("pattern")
    @classmethod
    def relative_pattern(cls, value: str) -> str:
        """拒绝绝对路径、逃逸和平台路径别名。"""
        if (value != value.strip() or "\\" in value or ":" in value
                or value.startswith(("/", "~")) or ".." in PurePosixPath(value).parts):
            raise ValueError("pattern 必须是工作区内相对 glob")
        return value

    @field_validator("keywords")
    @classmethod
    def literal_keywords(cls, value: list[str]) -> list[str]:
        """非空短关键词须互不重复；不解释为正则。"""
        if (len(set(value)) != len(value)
                or any(not word.strip() or word != word.strip() or len(word) > 80 for word in value)):
            raise ValueError("keywords 必须是互不重复的非空短文本")
        return value


class WorkflowReference(TypedDict):
    """成功读取的原文与可回溯行号，不含模型推理。"""

    path: str
    line: int
    text: str
    tool_call_id: str
    sha256: str


class ReadonlyRequestData(TypedDict):
    """已校验并可直接持久化的输入。"""

    pattern: str
    keywords: list[str]


class ReadonlyWorkflowState(TypedDict):
    """本轮覆盖型状态，SQLite Checkpoint 可直接序列化为 JSON。"""

    name: str
    run_id: str
    phase: str
    request: ReadonlyRequestData
    steps: int
    tool_calls: int
    cursor: int
    files: list[str]
    references: list[WorkflowReference]
    error: str | None


class CodeChangeRequest(BaseModel):
    """首版单文件精确修改与显式 unittest 验证范围。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    source_path: str = Field(min_length=1, max_length=200)
    test_paths: list[str] = Field(min_length=1, max_length=7)
    expected_test_count: int = Field(ge=1, le=10000)
    test_command: Literal["python -B -m unittest -v"] = "python -B -m unittest -v"

    @model_validator(mode="after")
    def distinct_relative_files(self) -> CodeChangeRequest:
        paths = [self.source_path, *self.test_paths]
        for value in paths:
            if (not value or len(value) > 200 or value != value.strip()
                    or any(char in value for char in "\\:*?[]\x00\r\n")
                    or value.startswith(("/", "~")) or ".." in PurePosixPath(value).parts
                    or PurePosixPath(value).as_posix() != value):
                raise ValueError("文件必须为规范工作区相对路径")
        if len(set(paths)) != len(paths):
            raise ValueError("源码与测试路径不可重复")
        return self


class CodeProposal(BaseModel):
    """模型只能提出唯一片段替换，不能选择工具、路径或命令。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    old_text: str = Field(min_length=1, max_length=64000)
    new_text: str = Field(min_length=1, max_length=64000)


class CodeRequestData(TypedDict):
    source_path: str
    test_paths: list[str]
    expected_test_count: int
    test_command: str


class CodeSourceData(TypedDict):
    path: str
    content: str
    sha256: str
    bom: bool
    tool_call_id: str


class CodeWorkflowState(TypedDict):
    """代码工作流的覆盖型业务状态；审计仍由原 State 字段保存。"""

    name: str
    run_id: str
    phase: str
    resume_phase: str
    request: CodeRequestData
    steps: int
    tool_calls: int
    model_calls: int
    model_tokens_reserved: int
    model_prompt: str
    model_input_reserved: int
    model_output_reserved: int
    cursor: int
    sources: dict[str, CodeSourceData]
    expected_content: str
    expected_sha256: str
    call: dict[str, Any]
    test_call_id: str
    error: str | None


class ChildResearchTask(BaseModel):
    """父任务明确授予的问题与目录，不接受模型生成授权。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    scope: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=1000)

    @field_validator("scope")
    @classmethod
    def relative_scope(cls, value: str) -> str:
        if value == ".":
            return value
        if (value != value.strip() or any(char in value for char in "\\:*?[]\x00\r\n")
                or value.startswith(("/", "~")) or ".." in PurePosixPath(value).parts
                or PurePosixPath(value).as_posix() != value):
            raise ValueError("scope 必须是规范工作区相对目录")
        return value

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question 不能为空")
        return value


class DelegatedResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tasks: list[ChildResearchTask] = Field(min_length=1, max_length=3)
    concurrency: int = Field(default=1, ge=1, le=2)


class ReadonlyWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Literal["readonly_research"]
    request: ReadonlyResearchRequest


class CodeWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Literal["code_change"]
    request: CodeChangeRequest


class DelegatedWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Literal["delegated_research"]
    request: DelegatedResearchRequest


WorkflowRequest = Annotated[
    ReadonlyWorkflowRequest | CodeWorkflowRequest | DelegatedWorkflowRequest,
    Field(discriminator="name"),
]


class ChildResearchReport(TypedDict):
    child_id: str
    scope: str
    status: str
    steps: int
    tool_calls: int
    model_calls: int
    model_tokens_reserved: int
    references: list[WorkflowReference]
    error: str | None


class DelegatedWorkflowState(TypedDict):
    name: str
    run_id: str
    phase: str
    steps: int
    tool_calls: int
    model_calls: int
    model_tokens_reserved: int
    children: list[ChildResearchReport]
    error: str | None
    request: dict[str, Any]
    child_specs: list[dict[str, Any]]

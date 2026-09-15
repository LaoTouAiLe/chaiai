"""测试共用夹具：假模型与配置对象。

假模型按提示内容区分两类调用：“路由分类”返回分类 JSON，其余按配置返回
工具调用或文本回答。这样两条链路都能断言，而且完全不依赖真实模型输出、不需要网络。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from local_agent.config import AppConfig, load_config
from local_agent.graph.routers import ROUTE_CLASSIFIER_MARKER

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RoutingFakeModel(GenericFakeChatModel):
    """按提示区分“路由分类”“工具调用”“文本回答”的假模型。

    - 系统提示里含 ``ROUTE_CLASSIFIER_MARKER`` → 返回分类 JSON；
    - 配置了 ``tool_calls`` 且是第一次非分类调用 → 返回带工具调用的 AI 消息；
    - 其余 → 返回 ``reply`` 文本。
    """

    decision: str = "chat"
    decision_reason: str = "假模型判定"
    reply: str = "假回答"
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    fail_classification: bool = False
    invalid_classification: bool = False
    support_json_schema: bool = True
    fail_bind_tools: bool = False
    seen_prompts: list[str] = Field(default_factory=list)
    bound_tools: list[str] = Field(default_factory=list)
    decision_calls: int = 0

    def bind_tools(self, tools, **kwargs: Any):  # type: ignore[no-untyped-def]
        """记录被绑定的工具名并返回自身（真实模型会返回绑定后的 Runnable）。"""
        del kwargs
        if self.fail_bind_tools:
            raise NotImplementedError("测试假模型不支持工具绑定（模拟 Tool Calling 不可用）")
        self.bound_tools = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    def with_structured_output(self, schema, **kwargs: Any):  # type: ignore[no-untyped-def]
        """模拟原生 JSON Schema 能力。

        构造阶段不报错（能力探测按可用记录）；调用阶段按测试开关抛错，
        用来分别模拟“不支持 JSON Schema”和“分类调用失败”。
        """
        del kwargs
        if not self.support_json_schema:
            raise NotImplementedError("测试假模型不提供原生 JSON Schema 输出")

        def _invoke(_input: Any) -> Any:
            if self.fail_classification:
                raise RuntimeError("分类调用失败（测试故意触发）")
            if self.invalid_classification:
                raise ValueError("模型输出中没有 JSON 对象")
            return schema.model_validate(self.classification())

        return RunnableLambda(_invoke)

    def classification(self) -> dict[str, Any]:
        """分类结果的结构化形式。"""
        return {"mode": self.decision, "reason": self.decision_reason}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # type: ignore[no-untyped-def]
        text = "\n".join(message.text for message in messages)
        self.seen_prompts.append(text)

        if ROUTE_CLASSIFIER_MARKER in text:
            if self.fail_classification:
                raise RuntimeError("分类调用失败（测试故意触发）")
            content = (
                "这不是 JSON"
                if self.invalid_classification
                else json.dumps(self.classification(), ensure_ascii=False)
            )
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

        self.decision_calls += 1
        if self.tool_calls and self.decision_calls == 1:
            message = AIMessage(content="", tool_calls=self.tool_calls)
            return ChatResult(generations=[ChatGeneration(message=message)])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def make_model(**overrides: Any) -> RoutingFakeModel:
    """构造假模型；可覆盖 ``decision``、``tool_calls``、``reply`` 等字段。"""
    return RoutingFakeModel(messages=iter([]), **overrides)


@pytest.fixture
def model_factory() -> Callable[..., RoutingFakeModel]:
    """构造假模型的工厂，便于每个用例指定分类结果与回答。"""
    return make_model


@pytest.fixture
def app_config() -> AppConfig:
    """用示例配置构造配置对象（该文件同时被 test_web 验证为合法）。"""
    return load_config(PROJECT_ROOT / "config.yaml.example")

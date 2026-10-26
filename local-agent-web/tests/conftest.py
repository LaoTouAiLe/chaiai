"""测试共用夹具：假模型与配置对象。

假模型按提示内容区分两类调用：“路由分类”返回分类 JSON，其余按配置返回
工具调用或文本回答。这样两条链路都能断言，而且完全不依赖真实模型输出、不需要网络。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from local_agent.config import AppConfig, load_config
from local_agent.graph.routers import ROUTE_CLASSIFIER_MARKER
from local_agent.memory.summarizer import SUMMARIZE_INSTRUCTION
from local_agent.reflection.reflector import MEMORY_INTENT_MARKER, PLAN_DRAFT_MARKER

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_search_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """离线回归不因开发机配置了搜索密钥而意外调用真实收费接口。

    两个 API 后端各有自己的变量名，漏删一个就会出现"本机配了 DeepSeek 密钥，测试却按
    ``configured=false`` 断言"的假失败。
    """
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def build_legacy_graph(*args: Any, **kwargs: Any) -> Any:
    """构造旧运行的恢复回归图，保留旧判官/自动推进契约断言。"""
    from unittest.mock import patch

    from local_agent.graph import builder

    original = builder.prepare_context_node

    # 入口节点现在要 runtime（2026-10-16：它从运行期上下文取这次提交的图片）。图节点是按
    # **参数注解**注入 runtime 的，所以这个包装器必须照写注解，否则 LangGraph 不注入、调用直接少参。
    from langgraph.runtime import Runtime

    from local_agent.graph.state import RuntimeContext

    def legacy_prepare(state: Any, runtime: Runtime[RuntimeContext]) -> Any:
        if isinstance(runtime.context.model_adapter, RoutingFakeModel):
            runtime.context.model_adapter.explicit_task_control = False
        return {**original(state, runtime), "agent_loop_version": 0}

    with patch.object(builder, "prepare_context_node", legacy_prepare):
        return builder.build_chat_graph(*args, **kwargs)


@pytest.fixture
def legacy_loop(monkeypatch):  # type: ignore[no-untyped-def]
    """只有显式请求此夹具的旧协议用例才切换；其余用例均运行新循环。"""
    from local_agent.graph import builder

    original = builder.prepare_context_node

    # 入口节点现在要 runtime（2026-10-16：它从运行期上下文取这次提交的图片）。图节点是按
    # **参数注解**注入 runtime 的，所以这个包装器必须照写注解，否则 LangGraph 不注入、调用直接少参。
    from langgraph.runtime import Runtime

    from local_agent.graph.state import RuntimeContext

    def legacy_prepare(state: Any, runtime: Runtime[RuntimeContext]) -> Any:
        if isinstance(runtime.context.model_adapter, RoutingFakeModel):
            runtime.context.model_adapter.explicit_task_control = False
        return {**original(state, runtime), "agent_loop_version": 0}

    monkeypatch.setattr(builder, "prepare_context_node", legacy_prepare)


class RoutingFakeModel(GenericFakeChatModel):
    """按提示区分“路由分类”“工具调用”“文本回答”的假模型。

    - 系统提示里含 ``ROUTE_CLASSIFIER_MARKER`` → 返回分类 JSON；
    - 配置了 ``tool_calls`` 且（第一次非分类调用 或 ``repeated_tool_calls``）→ 返回带工具调用的 AI 消息；
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
    summary: str = "会话摘要（假模型）"
    fail_summary: bool = False
    summary_delay_seconds: float = 0.0
    #: T17 反思结论的默认值：`goal_achieved=True` 只能配 `finish`（否则会被机械 guard 归一），
    #: 因此默认给"已完成、结束"——finish 会触发一次不绑定工具的最终回答合成，模型第二调用
    #: 返回 `reply`，既有断言（final_response == reply、task_status == completed）继续成立。
    reflection: dict[str, Any] = Field(
        default_factory=lambda: {"goal_achieved": True, "next_action": "finish"}
    )
    fail_reflection: bool = False
    #: ``True`` 时**每次**决策调用都返回 ``tool_calls``，用来构造"一个用户请求内的长工具链"
    #: （默认 ``False``：只有第一次带工具调用，之后返回文本——多数用例依赖这个行为）。
    repeated_tool_calls: bool = False
    #: 闲聊分支"记忆意图审核"给出的候选（默认空 = 普通闲聊，不写任何记忆）。
    memory_intent: list[dict[str, Any]] = Field(default_factory=list)
    fail_memory_intent: bool = False
    #: 计划拆步（T20）：模型给出的步骤描述。**空**表示拆不出步骤 → 调用方退化为单步，
    #: 因此所有既有用例的行为不变。
    plan_steps: list[str] = Field(default_factory=list)
    #: 审核结论里的 ``user_requested``（"用户本轮是否要求记住"）；默认 ``False``。它与候选列表
    #: 相互独立：用户要求记住但没能提炼出候选时，脚本可以只把这一项设为 ``True``。
    memory_requested: bool = False
    #: 流式测试用：把回答切成几块（确定性均分；``<=1`` 表示整段一块）。
    stream_chunks: int = 3
    #: 模拟供应商实际返回的独立思考字段；默认缺失，兼容无思考的模型。
    reasoning: str = ""
    reasoning_by_input: dict[str, str] = Field(default_factory=dict)
    #: 端到端观察中间态时可放慢假模型；默认不改变已有测试的速度。
    stream_delay_seconds: float = 0.0
    #: 流式测试用：产出第 N 块之后就抛错（模拟"中途失败"）；``None`` 表示不失败。
    fail_stream_after_chunks: int | None = None
    #: 只让普通回答失败；分类、记忆审核仍可完成，用于真实服务的模型错误路径。
    fail_answer: bool = False
    #: 假服务的页面边界用例：按最后一条用户消息选择确定性回复或故障。
    response_by_input: dict[str, str] = Field(default_factory=dict)
    fail_on_input: list[str] = Field(default_factory=list)
    #: 端到端（T32）按**用户输入**脚本化这一轮的工具调用：键是最后一条用户消息，值是这一轮要发
    #: 的工具调用。与 ``response_by_input`` 同一套"按输入脚本化"的口径，区别是它作用在决策调用上，
    #: 因此需要在同一进程里分别触发"批准"与"拒绝"两条路径时用它——``tool_calls`` 只认第一次决策调用。
    tool_calls_by_input: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    #: 已经脚本化过的输入（内部记账；见 ``_generate`` 里为什么必须记住）。
    scripted_inputs: set[str] = Field(default_factory=set)
    seen_prompts: list[str] = Field(default_factory=list)
    #: 结构化输出调用（反思、分类、记忆意图审核）收到的提示文本。
    structured_prompts: list[str] = Field(default_factory=list)
    bound_tools: list[str] = Field(default_factory=list)
    decision_calls: int = 0
    explicit_task_control: bool = True
    task_control_action: str = "complete"
    decision_tool_batches: list[list[dict[str, Any]]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs: Any):  # type: ignore[no-untyped-def]
        """记录被绑定的工具名并返回自身（真实模型会返回绑定后的 Runnable）。"""
        del kwargs
        if self.fail_bind_tools:
            raise NotImplementedError("测试假模型不支持工具绑定（模拟 Tool Calling 不可用）")
        self.bound_tools = [getattr(tool, "name", str(tool)) for tool in tools]
        return self

    def with_structured_output(self, schema, **kwargs: Any):  # type: ignore[no-untyped-def]
        """模拟原生 JSON Schema 能力。

        构造阶段不报错（能力探测按可用记录）；调用阶段按结构名分派：T17 的反思决策用
        ``reflection`` 字段，其余（路由分类）用 ``classification()``。
        """
        del kwargs
        if not self.support_json_schema:
            raise NotImplementedError("测试假模型不提供原生 JSON Schema 输出")

        def _invoke(_input: Any) -> Any:
            # 结构化输出调用的提示单独记一份（反思、分类、记忆意图审核都走这条路）。
            # 不能混进 seen_prompts：那里的约定是"主流程每次生成的提示"，既有用例用 seen_prompts[-1]
            # 断言"最后一次出站提示"（例如召回记忆/AGENTS 规则有没有进提示），混进来会把结论带偏。
            self._record_structured_prompt(_input)
            if schema.__name__ == "ReflectionDecision":
                if self.fail_reflection:
                    raise RuntimeError("反思调用失败（测试故意触发）")
                return schema.model_validate(self.reflection)
            if schema.__name__ == "MemoryIntentDecision":
                if self.fail_memory_intent:
                    raise RuntimeError("记忆意图审核失败（测试故意触发）")
                return schema.model_validate(
                    {
                        "user_requested": self.memory_requested,
                        "memory_candidates": self.memory_intent,
                    }
                )
            if schema.__name__ == "TaskMemoryDecision":
                # 既有工具任务夹具的候选数据保留；新协议只把候选交给独立记忆提取。
                return schema.model_validate({
                    "memory_candidates": self.reflection.get("memory_candidates", []),
                })
            if schema.__name__ == "PlanDraft":  # T20：计划拆步（空列表 → 退化单步）
                return schema.model_validate({"steps": self.plan_steps})
            if self.fail_classification:
                raise RuntimeError("分类调用失败（测试故意触发）")
            if self.invalid_classification:
                raise ValueError("模型输出中没有 JSON 对象")
            return schema.model_validate(self.classification())

        return RunnableLambda(_invoke)

    def classification(self) -> dict[str, Any]:
        """分类结果的结构化形式。"""
        return {"mode": self.decision, "reason": self.decision_reason}

    def _record_structured_prompt(self, payload: Any) -> None:
        """记录一次结构化输出调用的输入文本（供"提示里到底有没有某段内容"的断言）。"""
        if isinstance(payload, list):
            text = "\n".join(getattr(item, "text", str(item)) for item in payload)
        else:
            text = str(getattr(payload, "text", payload))
        self.structured_prompts.append(text)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # type: ignore[no-untyped-def]
        text = "\n".join(message.text for message in messages)
        self.seen_prompts.append(text)

        if "这不是完成度反思，不决定继续/重试" in text:
            content = json.dumps({"memory_candidates": self.reflection.get("memory_candidates", [])},
                                 ensure_ascii=False)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

        if SUMMARIZE_INSTRUCTION in text:  # T16：摘要请求（与路由分类/回答分开）
            if self.summary_delay_seconds > 0:
                time.sleep(self.summary_delay_seconds)
            if self.fail_summary:
                raise RuntimeError("摘要调用失败（测试故意触发）")
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=self.summary))]
            )

        if ROUTE_CLASSIFIER_MARKER in text:
            if self.fail_classification:
                raise RuntimeError("分类调用失败（测试故意触发）")
            content = (
                "这不是 JSON"
                if self.invalid_classification
                else json.dumps(self.classification(), ensure_ascii=False)
            )
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

        if PLAN_DRAFT_MARKER in text:  # T20：计划拆步（不支持原生 JSON Schema 时的降级路径）
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content=json.dumps({"steps": self.plan_steps}, ensure_ascii=False)
                        )
                    )
                ]
            )

        if MEMORY_INTENT_MARKER in text:  # T18：闲聊分支的记忆意图审核
            if self.fail_memory_intent:
                raise RuntimeError("记忆意图审核失败（测试故意触发）")
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content=json.dumps(
                                {
                                    "user_requested": self.memory_requested,
                                    "memory_candidates": self.memory_intent,
                                },
                                ensure_ascii=False,
                            )
                        )
                    )
                ]
            )

        self.decision_calls += 1
        latest_user = next((message.text for message in reversed(messages) if isinstance(message, HumanMessage)), "")
        reasoning = self.reasoning_by_input.get(latest_user, self.reasoning)
        provider_fields = {"reasoning_content": reasoning} if reasoning else {}
        if self.fail_answer or latest_user in self.fail_on_input:
            raise RuntimeError("模型回答失败（测试故意触发）")
        scripted = self.tool_calls_by_input.get(latest_user)
        if scripted and latest_user not in self.scripted_inputs:
            # 同一条输入只脚本化一次：工具结果回填后会对**同一条**用户消息再决策一次，
            # 不记住"已经发过"就会一直发同一个工具调用（这就是它和 repeated_tool_calls 的区别）。
            self.scripted_inputs.add(latest_user)
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="", tool_calls=[dict(call) for call in scripted], additional_kwargs=provider_fields,
                        )
                    )
                ]
            )
        if self.decision_calls <= len(self.decision_tool_batches):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="", tool_calls=self.decision_tool_batches[self.decision_calls - 1],
                additional_kwargs=provider_fields,
            ))])
        if self.tool_calls and (self.repeated_tool_calls or self.decision_calls == 1):
            message = AIMessage(content="", tool_calls=self._materialize_calls(), additional_kwargs=provider_fields)
            return ChatResult(generations=[ChatGeneration(message=message)])
        answer = self.response_by_input.get(latest_user, self.reply)
        if (self.explicit_task_control and "task_control" in self.bound_tools
                and "不要调用任何工具" not in text and "不要再次发起工具调用" not in text):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
                "name": "task_control", "id": f"finish-{self.decision_calls}",
                "args": {"action": self.task_control_action, "message": answer},
            }], additional_kwargs=provider_fields))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer, additional_kwargs=provider_fields))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        for chunk in self._stream_chunks(messages, stop, run_manager, **kwargs):
            if self.stream_delay_seconds > 0:
                time.sleep(self.stream_delay_seconds)
            yield chunk

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        # 使用可取消等待，避免把 time.sleep 放到传输事件循环中。
        for chunk in self._stream_chunks(messages, stop, run_manager, **kwargs):
            if self.stream_delay_seconds > 0:
                await asyncio.sleep(self.stream_delay_seconds)
            yield chunk

    def _stream_chunks(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        """按 ``_generate`` 的同一套脚本产出**增量块**（真实模型是 token 级，这里按块）。

        有了它，``llm.streaming=true`` 的路径在测试里也是确定性的：块的内容拼起来必须等于
        ``_generate`` 的整段回答（流式与不流式的最终消息因此完全一致）。
        """
        # 只为取脚本内容：提示只记一份、调用计数记 **1**（真正的"这次请求"是 stream 那一次）
        calls, prompts = self.decision_calls, len(self.seen_prompts)
        try:
            result = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        finally:
            self.decision_calls = calls + 1
            del self.seen_prompts[prompts:]
        message = result.generations[0].message
        provider_fields = dict(message.additional_kwargs)
        reasoning = provider_fields.pop("reasoning_content", "")
        for piece in _split_chunks(reasoning, self.stream_chunks):
            yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": piece}))
        if message.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=message.content, tool_calls=message.tool_calls,
                additional_kwargs=provider_fields, id=message.id,
            ))
            return
        for index, piece in enumerate(_split_chunks(str(message.content), self.stream_chunks)):
            if self.fail_stream_after_chunks is not None and index >= self.fail_stream_after_chunks:
                raise RuntimeError("流式中途失败（测试故意触发）")
            # `_stream` 的契约是产出 `ChatGenerationChunk`（langchain-core 会解包 .message）
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=piece,
                    additional_kwargs=provider_fields,
                    id=message.id,
                )
            )

    def _materialize_calls(self) -> list[dict[str, Any]]:
        """本轮返回的工具调用。

        重复模式下每次都给**唯一 id**：``tool_execution_node`` 按 ``(run_id, tool_call_id)`` 判重
        （"上一轮复用同一个调用 ID 不算已执行"），复用模板里的同一个 id 会让第 2 轮起全被当成
        "已有确认结果"跳过——图级测试就只剩 1 个完整步 + 若干无结果的 AI 调用（复审探针实测）。
        """
        calls: list[dict[str, Any]] = [dict(call) for call in self.tool_calls]
        if self.repeated_tool_calls:
            for call in calls:
                call["id"] = f"{call.get('id') or 'call'}-{self.decision_calls}"
        return calls


def _split_chunks(text: str, count: int) -> list[str]:
    """把文本切成若干块（确定性：按长度均分，保持顺序，拼起来等于原文）。"""
    if not text:
        return []
    if count <= 1:
        return [text]
    size = max(len(text) // count, 1)
    return [text[index : index + size] for index in range(0, len(text), size)]


def make_model(**overrides: Any) -> RoutingFakeModel:
    """构造假模型；可覆盖 ``decision``、``tool_calls``、``reply`` 等字段。"""
    return RoutingFakeModel(messages=iter([]), **overrides)


@pytest.fixture
def model_factory() -> Callable[..., RoutingFakeModel]:
    """构造假模型的工厂，便于每个用例指定分类结果与回答。"""
    return make_model


@pytest.fixture
def app_config(monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """通用单测配置：从示例构造，但关闭会增加模型调用或持久化状态的两项能力。

    示例文件自己的默认值由 ``test_config_example_is_valid`` 单独固定；与记忆/压缩无关的用例
    不应因为产品默认开关变化而悄悄多一次审核调用、创建长期库或触发摘要。

    同时把 ``llm.api_key_env`` 指向的变量**设上值**（T29A 之后运行期按连接取密钥：连接是从
    这段配置幂等导入的，指向的还是这个变量名）。不设的话，提交会正确地被 `MODEL_AUTH_FAILED`
    拒绝——那是产品行为没错，但会让"注入假模型"的用例莫名其妙地失败。
    """
    base = load_config(PROJECT_ROOT / "config.yaml.example")
    # 脚本测试的有界兜底；产品默认None另由自主策略专项覆盖。
    base.agent.max_tool_loops = 20
    monkeypatch.setenv(base.llm.api_key_env, "sk-test-secret")
    return base.model_copy(
        update={
            "memory": base.memory.model_copy(
                update={
                    "long_term": base.memory.long_term.model_copy(update={"enabled": False}),
                    "compression": base.memory.compression.model_copy(update={"enabled": False}),
                }
            ),
            # 安全删除（2026-10-20）在单测里**默认关**：开着时删除会移进回收站，
            # 而默认回收站根是真实数据目录——测试不该往用户目录里丢东西。
            # 这条能力由 `test_trash.py` / `test_file_system_sandbox.py` 显式开启来覆盖。
            "security": base.security.model_copy(update={"safe_delete": False}),
        }
    )

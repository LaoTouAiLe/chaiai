"""图节点的公共小工具（2026-10-26 从 `nodes.py` 下沉）。

这一层的判据是**形状**而不是主题：每个函数只读 State 的一两个字段，自己不再调用别的节点
（出度 0-1），却被很多节点调用（入度 3-13）。把它们沉到最底下，上层的快照、回答、计划
与记忆才能各自独立成文件而不互相 import。

依赖方向：本模块只依赖 `state` / `snapshot`，**不依赖任何节点**。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import ToolCall

from local_agent.graph.state import (
    AgentState,
    DecisionRecord,
    RuntimeContext,
    RuntimeContextSnapshot,
    make_decision_record,
)
from local_agent.llm.models import ModelCapabilities

# 这些名字在本文件里**不一定用到**，但 `builder.py` / `edges.py` / `readonly_workflow.py` 仍然
# 从 `nodes` 导入它们（拆分前它们就定义在这里）。所以这是一条**再导出**，不是普通导入：
# 删掉会让那些模块在运行时报 `has no attribute`。`noqa` 是必须的，不是凑数。
from .images import (  # noqa: F401 - 图片子系统，拆分前同样定义在 nodes 里
    _IMAGE_OMITTED_NOTE,
    _IMAGE_REFS_KEY,
    _TOOL_IMAGE_OMITTED_NOTE,
    _content_blocks_of,
    _image_refs_of,
    _is_image_block,
    _materialize_message_images,
    _read_image_refs,
    _resolve_message_images,
    _tool_images_after_replies,
)
from .names import (  # noqa: F401
    _STALE_SIDE_EFFECT_NOTE,
    AGENT_DECISION_NODE,
    APPROVAL_INTERRUPT_NODE,
    CHAT_NODE,
    COMPRESSION_NODE,
    FINAL_RESPONSE_NODE,
    LOAD_AGENTS,
    MEMORY_WRITE_NODE,
    PLANNER_NODE,
    PREPARE_CONTEXT,
    RECALL_MEMORY,
    REFLECTION_NODE,
    REJECTED_REPLY,
    RETRIEVE_CODE_NODE,
    SAFETY_GATE_NODE,
    TASK_MEMORY_NODE,
    TOOL_EXECUTION_NODE,
    TOOL_FEEDBACK_NODE,
    ReflectionAction,
    _MessageContent,
)

# 同上：`snapshot.py` 的全部定义在这里再导出一次。拆出去之后，测试仍在用
# `nodes._snapshot_text` / `nodes._rag_search_failed` 这样的写法访问它们（拆分前就在这个模块里），
# 不 re-export 就是一批 `has no attribute`。
from .snapshot import (  # noqa: F401
    _COMPRESSION_OVER_BUDGET_MARKER,
    _DECISION_BUDGET_METADATA,
    _bind_snapshot,
    _compression_over_budget_update,
    _compression_warning_reported,
    _covered_messages,
    _decision_budget_messages,
    _error,
    _frozen_snapshot,
    _prompt_window,
    _rag_search_failed,
    _recall_failed,
    _retain_tokens,
    _run_id,
    _runtime_snapshot,
    _skills_listing,
    _snapshot_insert_index,
    _snapshot_source_versions,
    _snapshot_text,
    _snapshot_tokens,
    _snapshot_update,
    _stable_prompt,
    _tool_definition_tokens,
    _with_snapshot,
)

_ASKING_PHRASES: tuple[str, ...] = (
    "请确认",
    "请告诉",
    "请补充",
    "需要你",
    "你想看",
    "你想了解",
    "你希望",
    "是否要我",
)


DELIVERED_ANSWER_MIN_CHARS = 40

#: 取消发生在产出内容之前时的回填文案（与 `nodes.py` 拆分前同一个值）。
CANCELLED_WITHOUT_CONTENT = (
    "本轮已停止：取消发生在产出内容之前，这一轮没有回答。已经执行的工具不会回滚。"
)


def _is_cancelled(context: RuntimeContext) -> bool:
    """本轮是否已被取消（架构 §17.4）。没挂令牌时永远为 `False`。

    取消**只作为两道门的输入**（安全 / 审批门与能力可见性门），不另开拒绝分支——
    节点的控制流因此与 v0.5 逐字一致。
    """
    return _cancel_refusal(context) is not None


def _cancel_refusal(context: RuntimeContext) -> str | None:
    """取消后拒绝工具调用时的说明；未取消返回 `None`（§17.4）。

    话术只有一处来源（`CancellationToken.refusal_reason`）：两道门说的是同一句话，
    不会出现"安全门说已取消、可见性门说档位不够"这种自相矛盾的提示。
    """
    token = context.cancellation
    return token.refusal_reason() if token is not None and token.cancelled else None


def _cancelled_turn_update(
    state: AgentState,
    context: RuntimeContext,
    *,
    partial_text: str = "",
    partial_message: AIMessage | None = None,
    snapshot: RuntimeContextSnapshot | None = None,
) -> AgentState:
    """本轮被取消时的**终态**更新（§17.4 的第三个动作）。

    三条口径写在这里，避免每个调用点各写一遍：

    - **取消不是失败**：不记 `ErrorEvent`、不写 `failed`，否则页面会把"你自己停的"说成"出错了"；
    - **保留已产出的部分**：`partial_text` 原样成为这一轮的消息，页面标「已停止」；
    - **不撤销副作用**：用了什么工具就是用了什么，这句话对用户明说，不留想象空间。

    没有任何内容可保留时，正文里带上**取消原因**（用户填的或默认说明）——否则"为什么停了"
    在回放里查不到（`tests/test_api.py` 的取消用例就是靠这句话核对原因可见的）。
    """
    text = partial_text.strip()
    if not text:
        token = context.cancellation
        reason = token.reason if token is not None and token.reason else ""
        text = f"本轮已停止：{reason}。已经执行的工具不会回滚。" if reason else CANCELLED_WITHOUT_CONTENT
    update: AgentState = {
        "messages": [partial_message.model_copy(update={"content": text})
                     if partial_message is not None else AIMessage(content=text)],
        "final_response": text,
        # 取消后不再有"待执行的动作"：留着会让下一轮把已经被拒的调用再放回执行列表。
        # 排队的待审批动作同理：取消之后不该再弹卡。
        "pending_tool_calls": [],
        "deferred_tool_calls": [],
        "task_status": "cancelled",
    }
    if snapshot is not None:
        update.update(_snapshot_update(state, snapshot))
    return update


def _model_capabilities(context: RuntimeContext) -> ModelCapabilities | None:
    """``RuntimeContext.model_capabilities`` 归一：只认本类型（单测可能塞别的对象）。"""
    capabilities = context.model_capabilities
    return capabilities if isinstance(capabilities, ModelCapabilities) else None


def _main_conversation_prefix(
    state: AgentState, context: RuntimeContext, snapshot: Any
) -> list[AnyMessage]:
    """主对话出站请求的开头那一段：稳定系统前缀 + 历史（含快照块）。

    **只允许有一个实现**（2026-09-30）。工具决策的请求与反思请求的**共享前缀**必须逐字节相同——
    供应商的前缀缓存从第一个 token 开始比对，差一个字节就等于没有共享，而表现不是报错、是
    "命中率悄悄掉回个位数"。

    反思原来另起一条 ``[反思指令, 事实]`` 的独立请求，与主对话不共享任何前缀：实测 65 次反思只命中
    **14.8%**，占掉整轮**未命中输入的 37%**。现在的做法是反思请求 = 这一段前缀 + 反思指令 + 事实。
    """
    condition = _stable_prompt(state, context)
    window = _prompt_window(state, context)
    _bind_snapshot(snapshot)
    return [
        SystemMessage(content=condition.system_prompt),
        *_with_snapshot(window.messages, snapshot, state),
    ]


def current_task_scope(state: AgentState) -> str:
    """当前任务的作用域键，用于筛出属于"本轮任务"的工具事件与结果（架构 §4.2）。

    v0.1 里"一个任务 = 一次用户提交"，所以作用域就是本轮的 ``run_id``。
    将来支持跨多轮续做同一个任务时，只需要把这里换成"任务开始时的 run_id"（或任务 ID）：
    执行与反思只依赖"作用域键"这个概念，调用方不用改，历史任务的证据也不会丢。
    """
    return _run_id(state)


def _in_current_task(record_run_id: str | None, *, scope: str) -> bool:
    """该记录是否属于当前任务：作用域键严格相等。

    升级前写入的记录没有 ``run_id``（``None``）：它们只保证"能读出来"，**不参与**当前
    任务的执行判定与反思统计——否则历史证据会混进每一轮的反思，同 ID 的新调用也会被
    当成"已执行"跳过。按严格相等处理，历史就只是历史。
    """
    return record_run_id == scope


def _decision(
    state: AgentState,
    *,
    stage: str,
    summary: str,
    tool_call_id: str | None = None,
    salt: str | None = None,
) -> DecisionRecord:
    """构造决策轨迹记录（``record_id`` 与循环轮次绑定，重放时自动去重）。

    ``salt`` 用来区分同一节点、同一轮次内的多条记录（例如一批记忆候选各自的写入结果）：
    没有它这些记录的 ID 完全相同，``merge_appended`` 只会保留第一条。
    """
    return make_decision_record(
        _run_id(state),
        stage=stage,
        summary=summary,
        loop_count=int(state.get("loop_count", 0)),
        tool_call_id=tool_call_id,
        salt=salt,
    )


def _call_id(call: ToolCall) -> str:
    """工具调用 ID：LangChain 的 ``ToolCall`` 允许 ``id`` 为 None，这里统一成字符串。"""
    return str(call.get("id") or "")


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _tool_payloads(messages: Sequence[AnyMessage]) -> dict[str, Mapping[str, Any]]:
    """从 ``ToolMessage`` 里解析出工具返回的结构化内容，供机械校验使用。

    工具结果已经脱敏，这里只做解析，不再改写内容。
    """
    payloads: dict[str, Mapping[str, Any]] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            parsed = json.loads(message.text)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, Mapping):
            payloads[str(message.tool_call_id or "")] = parsed
    return payloads


def _last_answer_text(state: AgentState) -> str:
    """本轮最后一段**有内容**的 AI 回答文本（供反思判断"模型已经答过什么"）。

    带 ``tool_calls`` 的 AI 消息 content 往往是空的，跳过它们才能拿到真正的回答；同一个请求内
    可能有多轮工具调用，所以从尾部往前找第一条非空 AI 文本。截断与标注由
    :func:`local_agent.reflection.reflector._render_answer` 负责。
    """
    for message in reversed(list(state.get("messages") or [])):
        if isinstance(message, AIMessage):
            text = str(message.text).strip()
            if text:
                return text
    return ""


def _delivered_answer(state: AgentState) -> str:
    """模型**已经交付**的可交付结论文本；空串表示没有（2026-09-30 新增）。

    为什么需要它：``goal_achieved`` 这个字段问的是"**当前任务目标**是否真的达成"
    （见 ``ReflectionDecision.goal_achieved`` 的说明），而 :func:`_advance_plan` 一直把它当成
    "当前步骤做完了"来驱动计划推进。两种含义压在一个字段上，于是出现了一个自相矛盾的形态：模型
    一次性把整个任务做完、写出了完整答案，反思如实回报"任务目标已达成"，却因为**计划记账还差几步**
    被改写成"继续执行"；接着它无事可做 → 空转 → 最终交付给用户的是"请确认下一步要做什么"。实测被
    精度层用例 ``locate_all`` 抓到（内容全对、终点 ``ask_user``）。

    这是一个**启发式**，不是语义判断——真正的解法是让那个字段区分"步骤"与"任务"，属于提示与 schema
    的改动，需要单独一轮。三条判据：有非空文本、长度过 :data:`DELIVERED_ANSWER_MIN_CHARS`、
    **不以问句收尾**。

    同一个判据同时服务 :func:`_advance_plan`（该不该收尾）与 :func:`_apply_stall_guard`（空转时
    该不该交付），避免两处各定一套、日后分叉。
    """
    answer = _last_answer_text(state)
    if len(answer) < DELIVERED_ANSWER_MIN_CHARS:
        return ""
    stripped = answer.rstrip()
    if stripped.endswith(("？", "?")):
        return ""
    tail = stripped[-80:]
    if any(phrase in tail for phrase in _ASKING_PHRASES):
        return ""
    return answer

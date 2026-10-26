"""图内记忆库复用、召回、候选审核与写入；不反向导入 nodes。"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import (
    dataclass,
    replace,
)
from pathlib import Path
from typing import (
    Any,
    cast,
)

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.runtime import Runtime

from local_agent.capabilities import untrusted_tool_names
from local_agent.config import (
    EmbeddingConfig,
    MediumTermMemoryConfig,
    resolve_api_key,
)
from local_agent.context.token_budget import (
    ContextBudget,
    TokenUsage,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    estimate_tools_tokens,
    prompt_budget_tokens,
    usage_ratio,
)
from local_agent.exceptions import (
    EmbeddingError,
    MemoryStoreError,
)
from local_agent.graph.projection import (
    current_turn_ai_message,
    message_id,
)
from local_agent.graph.state import (
    AgentState,
    ErrorEvent,
    MemoryCandidate,
    ReflectionResult,
    RuntimeContext,
    decision_record_key,
    error_event_key,
    merge_appended,
    tool_scope_key,
)
from local_agent.llm import embedding as embedding_module
from local_agent.llm.usage_recording import (
    record_model_call,
    tools_fingerprint,
)
from local_agent.memory import (
    long_term,
    medium_term,
    recall,
    writer,
)
from local_agent.memory.schemas import (
    ORIGIN_OWNER,
    ORIGIN_SYSTEM,
    ORIGIN_UNTRUSTED,
    MemoryReference,
    Origin,
)
from local_agent.reflection.memory_candidates import (
    RejectedCandidate,
    WriteIntent,
    plan_writes,
)
from local_agent.reflection.reflector import (
    extract_task_memory,
    memory_intent_text,
    reflect_memory_intent,
)
from local_agent.scope import project_id_for
from local_agent.session_kind import SESSION_KIND_INTERACTIVE

from .common import (
    _decision,
    _in_current_task,
    _main_conversation_prefix,
    current_task_scope,
)
from .names import (
    MEMORY_WRITE_NODE,
    RECALL_MEMORY,
    REFLECTION_NODE,
    TASK_MEMORY_NODE,
)
from .snapshot import (
    _covered_messages,
    _error,
    _run_id,
    _runtime_snapshot,
    _stable_prompt,
)

#: 进程内复用的中期记忆库（键含路径、collection 与 embedding 身份）。
_MEDIUM_TERM_STORES: dict[tuple[str, ...], medium_term.MediumTermStore] = {}


#: 建库/加载模型的并发保护：两个 Streamlit 会话可能同时首次召回，不加锁会各建一个模型实例。
_MEDIUM_TERM_STORES_LOCK = threading.Lock()


def _credential_fingerprint(api_key: str | None) -> str:
    """凭据变更标识：只放 Key 的完整哈希，**不把 Key 本体放进缓存键或日志**。

    用完整 SHA-256（不截断）：截断会把"凭据已更换"压缩成 32 位碰撞空间，一旦碰撞就会复用
    仍持有旧凭据的 Store；而完整摘要没有任何实际成本。
    """
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _store_cache_key(
    memory_config: MediumTermMemoryConfig,
    embedding_config: EmbeddingConfig,
    api_key: str | None,
) -> tuple[str, ...]:
    """商店缓存的键：任何影响"连哪个库、用哪套模型与凭据"的配置都要进键。

    漏掉任一项都会拿到不该复用的旧实例：换端点（``base_url``/``api_key_env``/Key）会命中旧
    Store，"先启用后关闭 embedding"也可能继续用旧实例。凭据只进完整哈希，不进原始 Key。
    """
    return (
        memory_config.path,
        memory_config.collection,
        str(embedding_config.enabled),
        embedding_config.provider,
        embedding_config.model,
        embedding_config.base_url,
        embedding_config.api_key_env,
        embedding_config.cache_dir,
        str(embedding_config.local_files_only),
        _credential_fingerprint(api_key),
    )


def medium_term_store(context: RuntimeContext) -> medium_term.MediumTermStore:
    """取（必要时创建）本进程复用的中期记忆库。

    复用是必需的：embedding 模型按进程惰性加载并复用（T14A），每次节点调用都新建适配器
    等于每轮重新加载一遍模型。整个"查缓存 + 建库"过程加锁：并发首次召回只会建一个实例
    （锁会在首次加载模型/下载时被持有，这正是我们要的串行化）。
    """
    memory_config = context.app_config.memory.medium_term
    embedding_config = context.app_config.embedding
    api_key = (
        resolve_api_key(embedding_config)
        if embedding_config.enabled and embedding_config.provider == "openai_compatible"
        else None
    )
    key = _store_cache_key(memory_config, embedding_config, api_key)
    with _MEDIUM_TERM_STORES_LOCK:
        cached = _MEDIUM_TERM_STORES.get(key)
        if cached is not None:
            return cached
        store = medium_term.open_store(
            memory_config,
            embedding_function=embedding_module.build_embedding_function(
                embedding_config, api_key=api_key
            ),
        )
        _MEDIUM_TERM_STORES[key] = store
        return store


#: 进程内复用的长期记忆库（缓存键是库文件路径）。
_LONG_TERM_STORES: dict[str, long_term.LongTermStore] = {}


_LONG_TERM_STORES_LOCK = threading.Lock()


def long_term_store(context: RuntimeContext) -> long_term.LongTermStore:
    """取（必要时创建）本进程复用的长期记忆库。

    与中期库一样按配置缓存：同一个库文件在进程内只开一条连接。T15 的写入用
    ``BEGIN IMMEDIATE`` 串行化、跨实例也安全，但没必要每次节点调用都重开连接。
    """
    config = context.app_config.memory.long_term
    path = str(Path(config.path).resolve())
    with _LONG_TERM_STORES_LOCK:
        cached = _LONG_TERM_STORES.get(path)
        if cached is not None:
            return cached
        store = long_term.open_store(Path(config.path))
        _LONG_TERM_STORES[path] = store
        return store


#: 合并中期与长期召回结果后，注入 Prompt 的记忆条数上限（各层自己的 ``top_k`` 仍然生效）。
MAX_MEMORY_REFS = 12


def recall_memory_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """召回中期与长期记忆并写入 ``memory_refs``（架构 §10.2、§10.3、§10.5）。

    三层记忆在这里汇合，但**互不牵连**：

    - 中期（Chroma 向量）与长期（SQLite 结构化）各自判断启用、各自捕获异常：一层打不开只丢
      这一层的结果并记一条错误事件，另一层照常召回（架构 §5：关闭的层不访问相应库；
      架构 §11.6：失败要可见，不允许静默回退）；
    - 两层都关闭时直接透传：**不打开任何库、不加载 embedding 模型**；
    - 结果按 ``memory_id`` 去重、按分数倒序、限制总条数后写进 ``memory_refs``（架构 §4.2）。
    """
    context = runtime.context
    memory = context.app_config.memory
    medium_enabled = memory.medium_term.enabled
    long_enabled = memory.long_term.enabled
    if not (medium_enabled or long_enabled):
        return {}
    query = str(state.get("normalized_request") or state.get("user_input") or "").strip()
    references: list[MemoryReference] = []
    errors: list[ErrorEvent] = []
    if medium_enabled and query:
        try:
            references.extend(
                recall.recall_memory(
                    medium_term_store(context),
                    query=query,
                    config=memory.medium_term,
                    workspace_root=context.workspace_root,
                    project_id=project_id_for(context.workspace_root),
                    user_id=context.user_id,
                    now=context.current_time,
                )
            )
        except (EmbeddingError, MemoryStoreError) as exc:
            errors.append(
                _error(
                    state,
                    stage=RECALL_MEMORY,
                    message=f"中期记忆召回失败，本轮不注入这类记忆：{exc}",
                )
            )
    if long_enabled:
        try:
            references.extend(
                recall.as_memory_reference(item)
                for item in recall.recall_long_term(
                    long_term_store(context),
                    config=memory.long_term,
                    workspace_root=context.workspace_root,
                    project_id=project_id_for(context.workspace_root),
                    user_id=context.user_id,
                    top_k=recall.DEFAULT_TOP_K,
                    now=context.current_time,
                )
            )
        except (MemoryStoreError, sqlite3.Error, OSError) as exc:
            errors.append(
                _error(
                    state,
                    stage=RECALL_MEMORY,
                    message=f"长期记忆召回失败，本轮不注入这类记忆：{exc}",
                )
            )
    update: AgentState = {"memory_refs": _merge_references(references)}
    if errors:
        update["error_events"] = errors
    return update


def _merge_references(references: Sequence[MemoryReference]) -> list[MemoryReference]:
    """合并各层召回结果：按 ``memory_id`` 去重、按分数倒序、限制总条数。

    两类分数**量纲不同**（中期是语义相似度乘系数，长期是 ``confidence × importance``），这里
    只做"同一列表内排序"，不把它们当成可比量互相校准；中期结果补上 ``kind`` 以便 Prompt 与
    页面区分来源。
    """
    unique: dict[str, MemoryReference] = {}
    for reference in references:
        item = cast(MemoryReference, dict(reference))
        if "kind" not in item:
            item["kind"] = "medium_term"
        unique.setdefault(str(item["memory_id"]), item)
    ordered = sorted(
        unique.values(), key=lambda entry: (-float(entry["score"]), str(entry["memory_id"]))
    )
    return ordered[:MAX_MEMORY_REFS]


def task_memory_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """新循环结束后的专用记忆提取；不影响任务终态，也不再生成回答。"""
    result = state.get("reflection_result")
    context = runtime.context
    memory = context.app_config.memory
    if (
        state.get("agent_loop_version") != 1
        or not isinstance(result, ReflectionResult)
        or not result.goal_achieved
        or not (memory.medium_term.enabled or memory.long_term.enabled)
    ):
        return {}
    try:
        registry = context.tool_registry
        tools = (
            registry.tools_for(str(state.get("mode", "chat")), context.agent_capabilities,
                           discovered_mcp=state.get("discovered_mcp_tools") or ())
            if registry
            else []
        )
        model = context.model_adapter.bind_tools(tools) if tools else context.model_adapter
        decision = extract_task_memory(
            model,
            prefix=_main_conversation_prefix(state, context, _runtime_snapshot(state, context)),
            capabilities=context.model_capabilities,
            estimated_tools_tokens=estimate_tools_tokens(tools),
            tools_schema_fingerprint=tools_fingerprint(tools),
        )
        candidates = _memory_candidates(decision.memory_candidates, state)
    except Exception as exc:  # noqa: BLE001 -- 记忆降级不抹掉已交付的答案
        return {"error_events": [_error(
            state, stage=f"{TASK_MEMORY_NODE}:memory_intent",
            message=f"任务记忆审核失败，本轮不写入记忆：{type(exc).__name__}",
        )]}
    return {
        "reflection_result": replace(result, memory_candidates=tuple(candidates)),
        "decision_trace": [_decision(
            state, stage="memory_intent", summary=f"任务记忆审核：{len(candidates)} 条候选",
        )],
    }


def _source_event_ids(state: AgentState) -> tuple[str, ...]:
    """候选的来源事件 ID——**由代码补齐，不用模型给的**。

    取当前任务的工具调用**作用域键** ``run_id:tool_call_id``，而不是裸的 ``tool_call_id``：
    不同轮次常常复用同一个 ``call_1``，裸 ID 会让新一轮的调用被当成"已写过的来源"而不再写入
    （复审实证）。没有工具事件（直接回答、闲聊）时退化为本轮 ``run_id``。

    架构 §10.4 要求候选携来源，而模型给的 ID 很容易是幻觉，所以提示里写死"不要编造 ID"、
    这里统一补：空来源的候选会被 ``memory_candidates`` 的静态校验拒绝。
    """
    scope = current_task_scope(state)
    keys = [
        tool_scope_key(event.run_id or scope, str(event.tool_call_id))
        for event in state.get("tool_events", [])
        if _in_current_task(event.run_id, scope=scope) and event.tool_call_id
        and not (state.get("agent_loop_version") == 1 and event.tool_name == "update_plan")
    ]
    if keys:
        return tuple(dict.fromkeys(keys))
    return (_run_id(state),)


def _turn_had_untrusted_input(state: AgentState) -> bool:
    """本轮是否真的取回过**外部内容**（架构 §16.3）。

    判定口径：

    - 只看**当前任务**（``run_id`` 作用域，与工具结果配对的既有口径一致）：上一轮查过的网页不该把这一轮的
      记忆也打成不可信，反过来更不行——只要本轮碰过外部内容，本轮的候选就按最保守的一侧处理；
    - 只看 ``status == "ok"`` 的结果：被拒绝或失败的调用没有带进任何外部内容；
    - 工具名取自 :func:`~local_agent.capabilities.untrusted_tool_names`（能力表是唯一真源）。
    """
    names = set(untrusted_tool_names())
    if not names:
        return False
    scope = current_task_scope(state)
    for record in state.get("tool_results", []):
        name = str(record.get("tool_name") or "")
        if name not in names or str(record.get("status") or "") != "ok":
            continue
        if _in_current_task(record.get("run_id"), scope=scope):
            return True
    return False


def _candidate_origin(state: AgentState) -> Origin:
    """本轮记忆候选的来源级别（架构 §16.2、§16.3、§17.1）。

    三步优先级链，**取最保守的一侧**，且**不取模型输出**（模型不能给自己的内容打信任标签）：

    1. 本轮取回过外部内容 → ``untrusted``；
    2. 否则会话来源不是 ``interactive``（定时 / 心跳）→ ``system``——这类会话**不产生可自动注入的
       持久记忆**（`docs/planning/FUTURE_WORK.md` B-03）；落库仍可读给用户看，只是永不自动进 Prompt；
    3. 否则 → ``owner``。

    缺 ``session_kind`` 的旧 Checkpoint 按 ``interactive`` 处理，与聊天库 v7 的列默认值一致
    （v0.6 之前每个会话都是用户在页面上创建的）。
    """
    if _turn_had_untrusted_input(state):
        return ORIGIN_UNTRUSTED
    kind = str(state.get("session_kind") or SESSION_KIND_INTERACTIVE)
    if kind != SESSION_KIND_INTERACTIVE:
        return ORIGIN_SYSTEM
    return ORIGIN_OWNER


def _memory_candidates(
    items: Sequence[Any] | None, state: AgentState
) -> list[MemoryCandidate]:
    """把模型给出的候选转成架构 §4.3 的 :class:`MemoryCandidate`。

    提示里已经限定产出条件，这里再补两道确定性处理：来源事件由代码补齐（``run_id:tool_call_id``），
    空内容的候选直接丢掉（校验层也会拒绝，先省一次映射）。**不在这里决定作用域**：作用域由
    ``plan_writes`` 按"候选类型 + ``entity_fact`` 的 ``entity_type``"机械推导（架构 §10.4 明确
    ``MemoryCandidate`` 不新增作用域字段）。
    """
    sources = _source_event_ids(state)
    # 来源可信级别由**代码**判定，不取模型输出（模型不能给自己的内容打信任标签）。
    # 三步优先级链（架构 §16.2/§16.3/§17.1，取最保守的一侧）见 :func:`_candidate_origin`：
    # 本轮取回过外部内容 → untrusted；否则非 interactive 会话 → system；否则 → owner。
    origin = _candidate_origin(state)
    candidates: list[MemoryCandidate] = []
    for item in items or []:
        content = str(getattr(item, "content", "") or "").strip()
        if not content:
            continue
        candidates.append(
            MemoryCandidate(
                memory_type=item.memory_type,
                content=content,
                origin=origin,
                subject=str(getattr(item, "subject", "") or "").strip(),
                importance=float(getattr(item, "importance", 0.5)),
                confidence=float(getattr(item, "confidence", 0.5)),
                source_event_ids=sources,
            )
        )
    return candidates


#: 闲聊里"真的写进去了"的确认（复审口径：**成功才确认**，不能预先承诺）。
CHAT_MEMORY_SAVED_NOTE = "（已保存到记忆：{count} 条。）"


#: 闲聊里"只写进去一部分"的说明：不能让部分成功看起来像全部成功。
CHAT_MEMORY_PARTIAL_NOTE = (
    "（只保存了 {saved} 条，另有 {failed} 条没有保存成功——{reason}。"
    "可以再让我记一次，或先在侧栏检查记忆配置。）"
)


#: 闲聊里"一条都没写进去"的说明：含"用户要求记住但没提炼出候选"与"记忆层全关"两种情况。
CHAT_MEMORY_NOTHING_SAVED_NOTE = (
    "（这条记忆**没有**保存成功——{reason}。"
    "你可以再让我记一次，或先在侧栏检查记忆配置。）"
)


#: 闲聊里审核失败时的确定性补充说明（只带异常类型名，不带原始堆栈）。
CHAT_MEMORY_AUDIT_FAILED_NOTE = (
    "（补充：这次没记住——记忆意图审核失败（{reason}）。可以再让我记一次。）"
)


def _chat_memory_reflection(state: AgentState, context: RuntimeContext) -> AgentState:
    """闲聊分支的**记忆意图审核**（架构 §5.2：闲聊也要经反思校验后才写记忆）。

    架构 §5.2 的规定是：识别到用户要求记住时，``chat_node`` 只产生 ``memory_write_intent``，
    **转入现有 ``reflection_node`` 校验**，再经 ``completion_router`` 与 ``memory_write_node``
    写入。这里就是那条路径——复用同一个节点与同一条结构化降级链，也不新增 State 字段（触发意图
    直接从本轮 ``user_input`` 解析）。

    与工具分支的区别是**不参与任务完成判定**：闲聊没有任务证据，所以 ``goal_achieved=False``、
    ``result_trust="uncertain"``、``next_action="finish"``。这既满足 T17 的"证据未 verified 不得
    判完成"，也不需要伪造一个"已完成"的结论——``final_response_node`` 对闲聊模式不看这个字段。

    **无论记忆层开关如何都会调用一次审核**：判定"用户是否要求记住"是回答诚实性的前提（记忆全关时
    也要能告诉用户"没保存"）。但审核**失败**只在记忆层开启时才提示与记事件——全关时审核只是附带
    判断，失败静默，否则普通闲聊每轮都会被追加一句"审核失败"（噪音）。

    ``user_requested=false`` 时**丢弃**模型给出的候选：结构化输出自相矛盾时不写入（复审实证：
    假模型默认 false 仍给候选，普通闲聊会被写成记忆）。所有"未保存"分支都只写 ``final_response``
    （展示文本，见 :func:`_chat_memory_final_text`），**不动 ``messages``**——历史是审计与恢复用的
    原始记录（架构 §10.1）。
    """
    config = context.app_config
    user_input = str(state.get("user_input") or "")
    memory_on = config.memory.medium_term.enabled or config.memory.long_term.enabled
    # 统计口径与**实际发送的请求**一致：审核只发截断后的文本（复审指出按完整输入报数字是错的）。
    sent_text = memory_intent_text(user_input)
    # 无论走哪条岔路都会写这两个字段：闲聊也经过 reflection_node，State 的形状保持一致
    # （下游 `completion_route` / `final_response` 与页面都按"有反思结果"读）。
    base: AgentState = {
        "reflection_result": ReflectionResult(
            # 闲聊没有任务，"完成"无从谈起——不给 True，避免绕过 T17 的机械约束。
            goal_achieved=False,
            # 没有工具证据；task_fragment 因此不会顺带写长期结论（与任务分支口径一致）。
            result_trust="uncertain",
            next_action="finish",
        ),
        "final_evidence": [],
    }
    if not sent_text:
        return base
    try:
        decision = reflect_memory_intent(
            context.model_adapter,
            user_input=user_input,
            capabilities=context.model_capabilities,
        )
    except Exception as exc:  # noqa: BLE001 —— 审核失败只是不写记忆，不能拖垮本轮对话
        reason = type(exc).__name__
        base["reflection_result"] = ReflectionResult(
            goal_achieved=False,
            result_trust="uncertain",
            next_action="finish",
            anomalies=(f"记忆意图审核失败：{reason}",),
        )
        if memory_on:
            # 记忆开启时审核失败要看得见（用户可能确实要求了记住）；记忆全关时审核只是"尽力而为"的
            # 附带判断，失败静默处理——否则普通闲聊每轮都会被追加一句审核失败（噪音）。
            _apply_chat_memory_answer(
                base,
                state,
                _chat_memory_final_text(
                    state,
                    context,
                    note=CHAT_MEMORY_AUDIT_FAILED_NOTE.format(reason=reason),
                    saved_all=False,
                ),
            )
            base["error_events"] = merge_appended(
                state.get("error_events", []),
                [
                    _error(
                        state,
                        stage=f"{REFLECTION_NODE}:memory_intent",
                        message=f"记忆意图审核失败，本轮不写入记忆：{reason}: {exc}",
                    )
                ],
                error_event_key,
            )
        return base
    requested = bool(decision.user_requested)
    # ``user_requested=false`` 却给出候选 → 结构化输出自相矛盾：此时**不写入**（复审实证：假模型
    # 默认 false 仍给候选，普通闲聊会被写成记忆）。只记一条审核不一致的说明。
    inconsistent = bool(decision.memory_candidates) and not requested
    candidates = _memory_candidates(decision.memory_candidates, state) if requested else []
    base["reflection_result"] = ReflectionResult(
        goal_achieved=False,  # 闲聊不判完成
        result_trust="uncertain",
        next_action="finish",
        memory_candidates=tuple(candidates),
    )
    base["decision_trace"] = merge_appended(
        state.get("decision_trace", []),
        [
            _decision(
                state,
                stage=f"{REFLECTION_NODE}:memory_intent",
                summary=(
                    f"闲聊记忆意图审核：要求记住={requested}、{len(candidates)} 条候选"
                    f"（发送 {len(sent_text)} 字符 / 约 {estimate_tokens(sent_text)} token）"
                    + ("；审核结果自相矛盾（未要求记住却给出候选），已丢弃候选、不写入" if inconsistent else "")
                ),
            )
        ],
        decision_record_key,
    )
    if not candidates:
        # 没有可写候选时，两种情况都要给出**确定性的未保存结果**（否则原回答里那句"我记住了"
        # 会直接展示给用户）：
        # ① 用户要求记住但没提炼出条目；② 审核自相矛盾（没要求记住却给出候选，已丢弃）。
        skip_reason: str | None = None
        if inconsistent:
            skip_reason = "审核结果自相矛盾（没有要求记住却给出候选），已丢弃、没有写入"
        elif requested:
            skip_reason = (
                "没有识别出可保存的条目"
                if memory_on
                else "记忆功能当前全部关闭（memory.medium_term / long_term 都是 false）"
            )
        if skip_reason is not None:
            _apply_chat_memory_answer(
                base,
                state,
                _chat_memory_final_text(
                    state,
                    context,
                    note=CHAT_MEMORY_NOTHING_SAVED_NOTE.format(reason=skip_reason),
                    saved_all=False,
                ),
            )
    return base


#: 闲聊记忆路径的收尾指令：让模型结合"记忆处理结果"**重写**最终回答。
CHAT_MEMORY_FINAL_INSTRUCTION = (
    "你在为一次**闲聊**收尾：用户本轮可能要求记住某些信息，系统已经处理完毕。\n"
    "请输出**最终回答正文**，不要输出前缀、标题或额外解释。\n"
    "\n"
    "硬要求：\n"
    "1. 直接回答用户本轮的问题；用户除了要求记住之外还问了别的事，必须一并回答。\n"
    "2. 记忆只按【记忆处理结果】表述：成功才可以说已记住 / 已保存；失败或部分失败必须如实说明"
    "没有保存成功及原因，**不得**声称已记住。\n"
    "3. 不要复述【你刚才的回答】里关于「已记住 / 已保存」的说法，它可能不准确。\n"
    "4. 不要编造系统没有提供的操作结果。\n"
)


#: "声称已保存"的常见表述；未全部成功时出现它就丢弃该文本，只用确定性说明（机械保险）。
_SAVED_PROMISE_PHRASES: tuple[str, ...] = (
    "已记住",
    "记住了",
    "已保存",
    "保存好了",
    "记下了",
    "已记录",
    "已经记",
    "帮你记",
)


def _chat_memory_final_text(
    state: AgentState,
    context: RuntimeContext,
    *,
    note: str,
    saved_all: bool,
) -> _ChatMemoryAnswer:
    """闲聊的**最终展示文本**：让模型结合"记忆处理结果"重写一次回答。

    为什么不直接改消息：Checkpoint 里的 ``messages`` 是**审计与恢复用的原始记录**（架构 §10.1、
    T19 的"原文恢复"验收），不能删也不能改；而原回答可能已经承诺"我记住了"，直接拼接会与
    "没有保存成功"自相矛盾，按句剥离承诺又不可靠（复审）。所以：**原文留存、投影可选**——这里产出
    展示文本，同时给出"按消息 id 的投影"（:mod:`local_agent.graph.projection`），页面与下一轮
    Prompt 窗口都用它。

    三个口径与工具分支的收尾合成（:func:`_synthesize_final_answer`）保持一致：

    1. **先过预算**：按"系统提示 + 素材 + 指令"估算，超 ``context_window_tokens - max_tokens``
       就不调用模型（发超预算请求会被真实供应商拒绝），退化为确定性说明并记降级原因；
    2. **用量快照**：这次调用的估算作为 ``TokenUsage`` / ``ContextBudget`` 返回，由调用方写回
       State——否则 State 自报的预算与真实出站请求对不上；
    3. **机械保险**：未全部成功时，若生成文本里仍出现"已记住 / 已保存"这类承诺，丢弃该文本、
       只用确定性说明（宁可少说，也不说错）。

    Args:
        state: 当前 State（取本轮原回答、系统提示、摘要边界）。
        context: 运行上下文（取模型与配置）。
        note: 确定性结果说明（成功 / 部分成功 / 失败的文案）。
        saved_all: 是否**全部**候选都写入成功。

    Returns:
        展示文本 + 投影表 + 预算快照 + 降级原因；文本永远不为空。
    """
    config = context.app_config
    turn_reply = current_turn_ai_message(state.get("messages", []))
    previous = turn_reply.text if turn_reply is not None else ""
    key = message_id(turn_reply) if turn_reply is not None else ""
    # 闲聊收尾重写只发"稳定前缀 + 记忆处理结果 + 指令"，**不注入快照块**：这条路径没有消息窗口，
    # 也没有"当前真实用户输入"可作插入锚点，硬塞快照块会变成一条无锚点的第二消息（§15.7 的
    # 插入规则要求落在真实用户输入之前）。因此这里保持与 T35 之前一致的行为，只换成稳定前缀。
    prompt = _stable_prompt(state, context)
    budget_tokens = prompt_budget_tokens(
        context_window_tokens=config.llm.context_window_tokens,
        completion_reserve_tokens=config.llm.max_tokens,
    )
    instruction = SystemMessage(content=CHAT_MEMORY_FINAL_INSTRUCTION)
    instruction_tokens = estimate_message_tokens(instruction)
    facts_text = _chat_memory_final_facts(
        user_input=str(state.get("user_input") or ""), previous=previous, note=note
    )
    facts_tokens = estimate_messages_tokens([HumanMessage(content=facts_text)])
    estimated = prompt.estimated_tokens + facts_tokens + instruction_tokens
    generated = ""
    degraded: str | None = None
    if estimated > budget_tokens:
        # 放不下就不发请求（与 T17 收尾合成同一口径）：结果说明仍然给出，只是少了模型的自然措辞。
        degraded = (
            f"收尾重写超出预算（估算 {estimated} > {budget_tokens} token），已跳过模型调用"
        )
    else:
        try:
            outbound = [
                SystemMessage(content=prompt.system_prompt),
                HumanMessage(content=facts_text),
                instruction,
            ]
            response = record_model_call(
                lambda: context.model_adapter.invoke(outbound),
                estimated_prompt_tokens=estimated,
                messages=outbound,
            )
        except Exception as exc:  # noqa: BLE001 —— 重写失败不能影响本轮回答
            degraded = f"收尾重写调用失败（{type(exc).__name__}: {exc}）"
        else:
            generated = response.text.strip() if isinstance(response, AIMessage) else ""
            if not generated:
                degraded = "收尾重写的返回没有文本，已只用确定性说明"
            elif not saved_all and _mentions_saved(generated):
                degraded = "重写后的回答仍声称已保存，已丢弃该文本、只用确定性说明"
                generated = ""
    ratio = usage_ratio(
        estimated_prompt_tokens=estimated, prompt_budget_tokens=budget_tokens
    )
    usage = TokenUsage(
        prompt_tokens=estimated,
        # 指令也是一条 SystemMessage：必须并进系统提示分项，否则分项之和小于总量
        system_prompt_tokens=prompt.estimated_tokens + instruction_tokens,
        window_messages_tokens=facts_tokens,
        completion_reserve_tokens=config.llm.max_tokens,
        tool_definition_tokens=0,  # 收尾重写不绑定工具
    )
    budget = ContextBudget(
        context_window_tokens=config.llm.context_window_tokens,
        completion_reserve_tokens=config.llm.max_tokens,
        prompt_budget_tokens=budget_tokens,
        estimated_prompt_tokens=estimated,
        usage_ratio=round(ratio, 6),
        trigger_ratio=config.memory.compression.trigger_ratio,
        triggered=False,  # 这是一次收尾调用，不代表"本轮触发过摘要压缩"
        window_messages=2,  # 素材 + 指令
        total_messages=len(state.get("messages") or []),
        summary_chars=len((state.get("conversation_summary") or "").strip()),
        summary_covered_messages=_covered_messages(state),
    )
    text = f"{generated}\n\n{note}" if generated else note
    return _ChatMemoryAnswer(
        text=text,
        # 投影让页面与下一轮 Prompt 看到修正后的回答（原文仍在 Checkpoint 里）
        overrides={key: text} if key else {},
        degraded=degraded,
        usage=usage,
        budget=budget,
    )


@dataclass(frozen=True, kw_only=True)
class _ChatMemoryAnswer:
    """闲聊收尾重写的结果：展示文本、回答投影、预算快照与降级原因。"""

    text: str
    overrides: dict[str, str]
    degraded: str | None
    usage: TokenUsage
    budget: ContextBudget


def _apply_chat_memory_answer(
    update: AgentState, state: AgentState, answer: _ChatMemoryAnswer
) -> None:
    """把收尾重写的结果写进节点更新：展示文本、投影、预算快照、降级事件。"""
    update["final_response"] = answer.text
    if answer.overrides:
        update["reply_overrides"] = answer.overrides
    update["token_usage"] = answer.usage
    update["context_budget"] = answer.budget
    if answer.degraded:
        # 降级不能静默：审计里要能区分"模型调用失败"与"模型仍作出保存承诺"（复审指出）。
        update["error_events"] = merge_appended(
            update.get("error_events") or state.get("error_events", []),
            [
                _error(
                    state,
                    stage=f"{REFLECTION_NODE}:memory_final",
                    message=f"记忆收尾重写降级：{answer.degraded}",
                )
            ],
            error_event_key,
        )


def _chat_memory_final_facts(*, user_input: str, previous: str, note: str) -> str:
    """收尾重写调用的素材（都是数据，不是指令）。"""
    return "\n".join(
        [
            f"【用户本轮输入】{memory_intent_text(user_input) or '（空）'}",
            f"【你刚才的回答】{previous or '（没有文本回答）'}",
            f"【记忆处理结果】{note}",
        ]
    )


def _mentions_saved(text: str) -> bool:
    """文本里是否出现"已记住 / 已保存"这类承诺（未全部成功时用它兜底）。"""
    return any(phrase in text for phrase in _SAVED_PROMISE_PHRASES)


def memory_write_node(state: AgentState, runtime: Runtime[RuntimeContext]) -> AgentState:
    """把反思（或闲聊审核）验证过的候选写入记忆（架构 §10.4）。

    **无候选就透传**：不打开任何库、不记事件——普通闲聊与大多数任务完成都属于这种情况，
    这也是"普通闲聊不每轮写长期记忆"的实现方式。

    有候选时：候选 → 落库计划（校验与映射见 :mod:`local_agent.reflection.memory_candidates`）
    → :func:`~local_agent.memory.writer.write_intents` 执行。四条硬语义（复审定稿）：

    1. **两层各自打开、各自降级**：中期库打不开不影响长期写入，反之亦然；并且"**启用但打不开**"
       必须与"配置关闭"区分开——前者按 ``failed`` 处理，后者才是 ``disabled``；
    2. **按候选判定完成**：只有该候选的**所有**目标都 ``satisfied`` **且至少有一个目标真正落库**
       （``written`` / ``duplicate`` / ``conflict``）才登记进 ``memory_write_candidates``；
       部分成功或零写入都不登记，下一轮还能补写（复审实证：只按 satisfied 判会让"库打不开"的
       候选以零写入登记完成）；
    3. **真实失败写错误事件**：``kind == "failed"`` 的结果进 ``error_events``，未启用与幂等跳过
       只进决策轨迹。事件 ID 带 ``salt``，同一轮的多条记录不会因 ID 相同被 ``merge_appended``
       丢掉；
    4. **闲聊必须说实话**：``mode == "chat"`` 时把写入结果交给 :func:`_chat_memory_final_text`
       生成**展示文本**（写进 ``final_response``）——全部成功才说"已记住"，有未保存必须如实说明；
       ``messages`` 保持原始记录不动（架构 §10.1：删改历史会破坏审计与"原文恢复"，复审实证）；
    5. **同一轮内的重复候选只处理一次**：按写入判重键去重，否则中期稳定 id 只会留下一条，确认
       文案却会报"保存 N 条"。
    """
    context = runtime.context
    result = state.get("reflection_result")
    if not isinstance(result, ReflectionResult) or not result.memory_candidates:
        return {}
    candidates = _distinct_candidates(result.memory_candidates)
    config = context.app_config
    medium_enabled = config.memory.medium_term.enabled
    long_enabled = config.memory.long_term.enabled
    chat_mode = state.get("mode") == "chat"
    if not (medium_enabled or long_enabled):
        update: AgentState = {
            "decision_trace": merge_appended(
                state.get("decision_trace", []),
                [
                    _decision(
                        state,
                        stage=MEMORY_WRITE_NODE,
                        summary=(
                            f"有 {len(candidates)} 条记忆候选，但中期与长期记忆都已关闭，"
                            "按透传处理、不写入"
                        ),
                    )
                ],
                decision_record_key,
            )
        }
        # 这一轮**有候选**却一条都没落：必须让界面说得出来（§10.4 的 `skipped`）。
        update["memory_write_skipped"] = "中期与长期记忆都已关闭，候选没有落库"
        update["memory_write_count"] = 0
        if chat_mode:
            _apply_chat_memory_answer(
                update,
                state,
                _chat_memory_final_text(
                    state,
                    context,
                    note=CHAT_MEMORY_NOTHING_SAVED_NOTE.format(reason="中期与长期记忆都已关闭"),
                    saved_all=False,
                ),
            )
        return update

    verified = result.result_trust == "verified"
    intents: list[WriteIntent] = []
    rejections: list[RejectedCandidate] = []
    for candidate in candidates:
        planned = plan_writes(
            candidate,
            workspace_root=context.workspace_root,
            user_id=context.user_id,
            task_verified=verified,
            # 用户级（跨项目可见）作用域需要显式授权：模型写的 entity_type 不足以决定隔离边界。
            user_scope_allowed=config.memory.long_term.allow_user_scope,
        )
        if isinstance(planned, RejectedCandidate):
            rejections.append(planned)
        else:
            intents.extend(planned)

    errors: list[ErrorEvent] = [
        _error(
            state,
            stage=MEMORY_WRITE_NODE,
            message=f"记忆候选被拒绝，未写入：{rejection.reason}",
            salt=f"reject:{index}",
        )
        for index, rejection in enumerate(rejections)
    ]
    medium_store: medium_term.MediumTermStore | None = None
    long_store_handle: long_term.LongTermStore | None = None
    #: 配置已启用但打不开的层：传给 writer 后，这些层上的目标算 failed 而不是"未启用"。
    #: 混为一谈会让零写入的候选也登记完成（复审实证：长期库打不开时仍标成完成）。
    failed_layers: set[str] = set()
    if medium_enabled and any(item.target == "medium_term" for item in intents):
        try:
            medium_store = medium_term_store(context)
        except Exception as exc:  # noqa: BLE001 —— 单层不可用不能拖垮另一层
            failed_layers.add("medium")
            errors.append(
                _error(
                    state,
                    stage=MEMORY_WRITE_NODE,
                    message=f"中期记忆库不可用，本轮跳过中期写入：{type(exc).__name__}: {exc}",
                    salt="open:medium",
                )
            )
    if long_enabled and any(item.target != "medium_term" for item in intents):
        try:
            long_store_handle = long_term_store(context)
        except Exception as exc:  # noqa: BLE001 —— 单层不可用不能拖垮另一层
            failed_layers.add("long")
            errors.append(
                _error(
                    state,
                    stage=MEMORY_WRITE_NODE,
                    message=f"长期记忆库不可用，本轮跳过长期写入：{type(exc).__name__}: {exc}",
                    salt="open:long",
                )
            )

    # 判重键含候选身份（不是只看来源事件）：同一事件下的其它候选不会被误伤（复审实证）。
    seen = {
        writer.candidate_write_key(item)
        for item in state.get("memory_write_candidates", [])
    }
    outcomes = writer.write_intents(
        intents,
        seen_keys=seen,
        failed_layers=failed_layers,
        medium_store=medium_store,
        long_store=long_store_handle,
        source_thread_id=str(state.get("thread_id") or "") or None,
    )
    completed = _completed_candidates(intents, outcomes)
    for index, outcome in enumerate(outcomes):
        if outcome.kind != "failed":
            continue
        errors.append(
            _error(
                state,
                stage=MEMORY_WRITE_NODE,
                message=f"记忆写入失败：{_write_summary(outcome)}",
                salt=f"write:{index}",
            )
        )
    update = {
        "decision_trace": merge_appended(
            state.get("decision_trace", []),
            [
                _decision(
                    state,
                    stage=MEMORY_WRITE_NODE,
                    summary=_write_summary(outcome),
                    salt=f"write:{index}",
                )
                for index, outcome in enumerate(outcomes)
            ],
            decision_record_key,
        )
    }
    if completed:
        update["memory_write_candidates"] = completed
    # 本轮口径的写入结果（覆盖语义，架构 §15）：投影的"已写入 N 条"只认这个数。
    # 这里**无条件**写：本节点跑过但一条都没落时必须是 0（不是 None），否则界面会把
    # 上一轮的条数当成这一轮的。没走到本节点的轮次才保持 None。
    update["memory_write_count"] = len(completed)
    if not completed:
        # 有候选、也走了写入流程，但一条都没落：把**具体**原因带给界面（§10.4 的 `skipped`）。
        update["memory_write_skipped"] = _write_skipped_reason(outcomes)
    if errors:
        update["error_events"] = merge_appended(
            state.get("error_events", []), errors, error_event_key
        )
    if chat_mode:
        # 闲聊里必须给出**写入结果**：展示文本进 ``final_response``，同时写**回答投影**（页面与
        # 下一轮 Prompt 都按投影取文本），``messages`` 保持原始记录不动（架构 §10.1）。
        all_saved = bool(candidates) and len(completed) >= len(candidates)
        _apply_chat_memory_answer(
            update,
            state,
            _chat_memory_final_text(
                state,
                context,
                note=_chat_memory_result_note(candidates, rejections, outcomes, completed),
                saved_all=all_saved,
            ),
        )
    return update


def _distinct_candidates(items: Sequence[MemoryCandidate]) -> list[MemoryCandidate]:
    """按写入判重键去掉**同一轮内的重复候选**。

    同一批里出现两条完全相同的候选时，它们的稳定 id 也相同：中期库最终只有一条，确认文案却会报
    "保存 2 条"（复审实证）。去重放在候选层，``task_fragment`` 的"中期 + 长期结论"双目标仍由
    同一候选产生，不受影响。
    """
    seen: set[str] = set()
    unique: list[MemoryCandidate] = []
    for item in items:
        key = writer.candidate_write_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _not_stored_reason(
    rejections: Sequence[RejectedCandidate], outcomes: Sequence[writer.WriteOutcome]
) -> str:
    """给用户看的"为什么没记住"。

    顺序很重要：**先看被拒绝的候选**（内容不是规范 JSON、实体名缺失等，原因用户看得懂也能改），
    再看真实写入失败，最后才是"目标层不可用"。只看写入结果会把"候选不合法"错报成"目标记忆层
    当前不可用"，把用户引到错误的排查方向（复审实证）。
    """
    for rejection in rejections:
        if rejection.reason:
            return rejection.reason
    for outcome in outcomes:
        if outcome.kind == "failed":
            return str(outcome.reason or "写入失败")
    if not outcomes:
        return "没有可写入的目标"
    return "目标记忆层当前不可用"


def _chat_memory_result_note(
    candidates: Sequence[MemoryCandidate],
    rejections: Sequence[RejectedCandidate],
    outcomes: Sequence[writer.WriteOutcome],
    completed: Sequence[MemoryCandidate],
) -> str:
    """按**每条候选**的实际结果生成闲聊侧的确定性说明。

    只看"有没有登记完成"是不够的：多候选里成功一部分时，用户会看到模型那句"都记住了"，而实际
    还有候选没写进去（复审实证）。被拒绝的候选也要计入"没保存"——否则一条成功 + 一条因 JSON
    不合法被拒时，用户会被告知"目标记忆层当前不可用"（原因错位）。这是闲聊侧唯一被允许的
    "已记住"确认。
    """
    saved = len(completed)
    if saved and saved >= len(candidates):
        return CHAT_MEMORY_SAVED_NOTE.format(count=saved)
    reason = _not_stored_reason(rejections, outcomes)
    if saved:
        return CHAT_MEMORY_PARTIAL_NOTE.format(
            saved=saved, failed=len(candidates) - saved, reason=reason
        )
    return CHAT_MEMORY_NOTHING_SAVED_NOTE.format(reason=reason)


def _completed_candidates(
    intents: Sequence[WriteIntent], outcomes: Sequence[writer.WriteOutcome]
) -> list[MemoryCandidate]:
    """**所有目标都满足、且至少有一个目标真正落库**的候选（按第一次出现的顺序）。

    两条都必要：

    - 所有目标 satisfied：一个候选可能对应多个目标（``task_fragment`` 同时写中期与长期结论），
      只要有一个真实失败就不登记，下一轮还能补写；
    - 至少一个目标 ``written`` / ``duplicate`` / ``conflict``：否则"目标层全关"或"库打不开"的
      候选会以零写入登记完成（复审实证）。真正落库过的候选重试也是幂等的（中期稳定 id、
      长期结论稳定 id、事实的同键同值语义）。
    """
    order: list[MemoryCandidate] = []
    grouped: dict[int, list[writer.WriteOutcome]] = {}
    for intent, outcome in zip(intents, outcomes):
        key = id(intent.candidate)
        if key not in grouped:
            grouped[key] = []
            order.append(intent.candidate)
        grouped[key].append(outcome)
    persisted: frozenset[str] = frozenset({"written", "duplicate", "conflict"})
    return [
        item
        for item in order
        if all(o.satisfied for o in grouped[id(item)])
        and any(o.kind in persisted for o in grouped[id(item)])
    ]


def _write_skipped_reason(outcomes: Sequence[writer.WriteOutcome]) -> str:
    """一条都没落库时给界面的**具体**原因（§10.4 的 `skipped` 要说明为什么）。

    只说"没写"等于没说：用户要分辨的是"这轮没东西可记"（根本不产出这一行）与"想记但被跳过了"，
    以及后者到底是**哪个开关**、还是库打不开、还是与已有记忆重复。
    """
    kinds = {outcome.kind for outcome in outcomes}
    reasons = [outcome.reason for outcome in outcomes if outcome.reason]
    if "failed" in kinds:
        return "记忆库不可用，本轮未写入任何内容（不影响回答）"
    if kinds and kinds <= {"disabled"}:
        # writer 的那句话最具体（它写着是哪个开关），直接用。
        return reasons[0] if reasons else "目标记忆层未启用，本轮未写入"
    if kinds & {"duplicate", "conflict"}:
        return "候选与已有记忆重复或冲突，本轮没有新增"
    return "候选没有落进任何记忆库，本轮未写入（详情见本轮轨迹）"


def _write_summary(outcome: writer.WriteOutcome) -> str:
    """一条写入结果的摘要（写进决策轨迹，便于事后解释"为什么没记住"）。"""
    target = {
        "medium_term": "中期记忆",
        "long_term_fact": "长期事实",
        "long_term_conclusion": "长期任务结论",
    }[outcome.target]
    if outcome.kind == "failed":
        return f"写入{target}失败：{outcome.reason or '未知原因'}"
    if not outcome.written:
        return f"未写入{target}：{outcome.reason or '按幂等或未启用跳过'}"
    if outcome.reason:
        return f"已写入{target}：{outcome.reason}"
    return f"已写入{target}"
